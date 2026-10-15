"""The lake's only writer (design 9.1). Append-only: a replay, a retry and a late record each add
a file; nothing already written is rewritten (M6). Accepts a ValidatedExtraction and nothing else --
the gate is the only way in.

Row values are client data: no error raised here ever quotes one (brief rule 6). Errors name a
row index or a spec field name, which are ours."""

from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
from ulid import ULID

from selector_drift.config import get_settings
from selector_drift.land import compact as _compact
from selector_drift.land.layout import (
    INGESTED_AT_COL,
    NATURAL_KEY_COL,
    PROVENANCE_COL,
    RUN_KEY_COL,
    WRITE_ID_COL,
    check_field_names,
    parse_file_name,
    partition_dir,
    split_run_key,
)
from selector_drift.land.layout import fs_key as fs_key  # re-exported: orchestrate/history.py
from selector_drift.spec.loader import load_spec
from selector_drift.validate.gate import ValidatedExtraction

COMPACT_AFTER_FILES = 24  # module global so a test can lower it (P4)
_META = (RUN_KEY_COL, INGESTED_AT_COL, NATURAL_KEY_COL, WRITE_ID_COL, PROVENANCE_COL)


class SpecVersionMismatch(ValueError):
    """The run was extracted under a spec version that is no longer the one on disk."""


def _natural_key(target: str, version: int) -> list[str]:
    """The key of the spec the run was extracted under. specs/ keeps only the current version,
    so a run from any other version is refused: its natural_key may since have changed, and
    rows landed under a different key are rows the dedup view can never collapse."""
    spec = load_spec(get_settings().spec_dir / f"{target}.yaml")
    if spec.version != version:
        raise SpecVersionMismatch(
            f"run for target {target!r} was extracted under spec version {version}, but the "
            f"spec on disk is version {spec.version}; its natural key cannot be trusted"
        )
    return list(spec.natural_key)


def _records(validated: ValidatedExtraction, key_fields: list[str]) -> list[dict[str, Any]]:
    now = dt.datetime.now(dt.UTC)
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for i, row in enumerate(validated.rows):
        check_field_names(row)
        missing = [k for k in key_fields if k not in row]
        if missing:
            raise ValueError(f"row {i} lacks natural key field(s) {missing}")
        values = {name: str(c.value) for name, c in row.items()}
        key = json.dumps([values[k] for k in key_fields], ensure_ascii=False)
        if key in seen:
            raise ValueError(
                f"row {i}: a natural key occurs twice in one run -- a key that is not unique "
                "cannot deduplicate anything"
            )
        seen.add(key)
        provenance = {name: c.model_dump(mode="json", exclude={"value"}) for name, c in row.items()}
        out.append(
            values
            | {
                RUN_KEY_COL: validated.run_key,
                INGESTED_AT_COL: now,
                NATURAL_KEY_COL: key,
                PROVENANCE_COL: json.dumps(provenance, sort_keys=True),
            }
        )
    return out


def _table(records: list[dict[str, Any]], write_id: str) -> pa.Table:
    data_cols: list[str] = []
    for r in records:
        data_cols += [k for k in r if k not in _META and k not in data_cols]
    schema = pa.schema(
        [pa.field(c, pa.string()) for c in data_cols]
        + [
            pa.field(RUN_KEY_COL, pa.string(), nullable=False),
            pa.field(INGESTED_AT_COL, pa.timestamp("us", tz="UTC"), nullable=False),
            pa.field(NATURAL_KEY_COL, pa.string()),
            pa.field(WRITE_ID_COL, pa.string()),
            pa.field(PROVENANCE_COL, pa.string()),
        ]
    )
    return pa.Table.from_pylist([r | {WRITE_ID_COL: write_id} for r in records], schema=schema)


def _next_attempt(part: Path, run_key: str) -> int:
    """One past the highest attempt on disk. Not a file count: a compacted file keeps the
    highest attempt it folded, so after compaction a count would reuse a number."""
    attempts = [parse_file_name(f.name)[1] for f in part.glob(f"{fs_key(run_key)}-*.parquet")]
    return 1 + max(attempts, default=0)


def compact_if_needed(written: Path) -> None:
    """Compaction folds files together, so it can no longer be undone by deleting the file that
    was written. A caller that may have to take its write back defers this to after it commits."""
    part = written.parent
    if sum(1 for _ in part.glob("*.parquet")) > COMPACT_AFTER_FILES:
        _compact.compact(part.parent.name.removeprefix("target="))


def write(
    validated: ValidatedExtraction, *, attempt: int | None = None, compact: bool = True
) -> Path:
    if not isinstance(validated, ValidatedExtraction):
        raise TypeError(
            "land.write accepts only a ValidatedExtraction, got "
            f"{type(validated).__name__} -- the gate is the only way into the lake"
        )
    target, version, window = split_run_key(validated.run_key)
    records = _records(validated, _natural_key(target, version))
    part = partition_dir(get_settings().lake_root, target, window)
    part.mkdir(parents=True, exist_ok=True)
    n = attempt if attempt is not None else _next_attempt(part, validated.run_key)
    if n < 1:
        raise ValueError("attempt numbers start at 1")
    write_id = str(ULID())
    final = part / f"{fs_key(validated.run_key)}-{n}-{write_id}.parquet"
    tmp = part / f".{final.name}.tmp"  # never matches *.parquet
    try:
        pq.write_table(_table(records, write_id), tmp)
        os.replace(tmp, final)  # a reader sees all of it or none of it
    finally:
        tmp.unlink(missing_ok=True)
    if compact:
        compact_if_needed(final)
    return final
