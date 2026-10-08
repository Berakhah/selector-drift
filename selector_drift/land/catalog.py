"""DuckDB over the lake -- a query layer, never a store (design 9.1). Every call opens an
in-memory database and closes it; no .duckdb file exists anywhere, so parallel flows never share
a writer."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import duckdb
import pyarrow.parquet as pq

from selector_drift.config import get_settings
from selector_drift.land.layout import (
    DATA_GLOB,
    INGESTED_AT_COL,
    NATURAL_KEY_COL,
    PROVENANCE_COL,
    RUN_KEY_COL,
    WRITE_ID_COL,
    check_target,
    fs_key,
    partition_dir,
    split_run_key,
)

_ATTEMPTS = 5

_EMPTY = (
    'SELECT CAST(NULL AS VARCHAR) AS target, CAST(NULL AS VARCHAR) AS "window", '
    f"CAST(NULL AS VARCHAR) AS {RUN_KEY_COL}, "
    f"CAST(NULL AS TIMESTAMPTZ) AS {INGESTED_AT_COL}, "
    f"CAST(NULL AS VARCHAR) AS {NATURAL_KEY_COL}, CAST(NULL AS VARCHAR) AS {WRITE_ID_COL}, "
    f"CAST(NULL AS VARCHAR) AS {PROVENANCE_COL} WHERE false"
)

# Deduplicated on (target, window, natural key): the same key on two days is two days of history.
_DEDUP = (
    "SELECT * EXCLUDE (_sd_rank) FROM (SELECT *, row_number() OVER ("
    f'PARTITION BY target, "window", {NATURAL_KEY_COL} '
    f"ORDER BY {INGESTED_AT_COL} DESC, {WRITE_ID_COL} DESC) AS _sd_rank FROM lake_raw) "
    "WHERE _sd_rank = 1"
)


@dataclass(frozen=True)
class QueryResult:
    columns: tuple[str, ...]
    rows: list[tuple[Any, ...]]

    def fetchone(self) -> tuple[Any, ...] | None:
        return self.rows[0] if self.rows else None

    def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self.rows)

    def dicts(self) -> list[dict[str, Any]]:
        return [dict(zip(self.columns, r, strict=True)) for r in self.rows]


def lake_files(target: str | None = None) -> list[Path]:
    root = get_settings().lake_root
    pattern = DATA_GLOB if target is None else f"target={check_target(target)}/window=*/*.parquet"
    return sorted(root.glob(pattern))  # _staging/ and _retired/ never match


def _views(con: duckdb.DuckDBPyConnection, files: list[Path]) -> None:
    if files:
        listed = ", ".join("'" + p.as_posix().replace("'", "''") + "'" for p in files)
        con.execute(
            f"CREATE VIEW lake_raw AS SELECT * FROM read_parquet([{listed}], "
            "hive_partitioning = true, union_by_name = true, hive_types_autocast = false)"
        )
    else:
        con.execute(f"CREATE VIEW lake_raw AS {_EMPTY}")
    con.execute(f"CREATE VIEW lake AS {_DEDUP}")


def query(sql: str, params: Sequence[object] = (), *, target: str | None = None) -> QueryResult:
    """Runs `sql` against `lake` (deduplicated) and `lake_raw` (every row written). A file retired
    by compaction between listing and reading raises IOException; the listing is retaken."""
    last: duckdb.Error | None = None
    for _ in range(_ATTEMPTS):
        con = duckdb.connect(database=":memory:")
        try:
            con.execute("SET TimeZone = 'UTC'")
            _views(con, lake_files(target))
            cur = con.execute(sql, list(params))
            columns = tuple(str(d[0]) for d in (cur.description or ()))
            return QueryResult(columns=columns, rows=cur.fetchall())
        except duckdb.IOException as exc:
            last = exc
        finally:
            con.close()
    assert last is not None
    raise last


def run_columns(run_key: str) -> list[str]:
    """The columns this run's own files carry -- not the union across the target (Task 29)."""
    target, _version, window = split_run_key(run_key)
    names: list[str] = []
    part = partition_dir(get_settings().lake_root, target, window)
    for f in sorted(part.glob(f"{fs_key(run_key)}-*.parquet")):
        names += [n for n in pq.read_schema(f).names if n not in names]
    return names
