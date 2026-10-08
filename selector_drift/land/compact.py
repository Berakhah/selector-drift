"""Compaction (design 9.1; L7, P4). Folds one run's files in a partition into one file. Built in
a sibling directory, swapped in with one atomic rename, and the superseded files renamed aside --
never unlinked -- so the lake view's rows are identical at every instant and a reader holding an
old listing retries rather than fails.

Never folds two runs together: a partition can hold one run_key per spec version, and
history.py finds a run's rows by the {fs_key(run_key)}-*.parquet prefix, which the folded file
keeps."""

from __future__ import annotations

import datetime as dt
import os
import shutil
import time
from collections import defaultdict
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
from ulid import ULID

from selector_drift.config import get_settings
from selector_drift.land.layout import (
    INGESTED_AT_COL,
    NATURAL_KEY_COL,
    RETIRED_DIR,
    STAGING_DIR,
    WRITE_ID_COL,
    check_target,
    dedup_sql,
    parse_file_name,
)

RETIRED_GRACE = dt.timedelta(minutes=15)
LOCK_STALE = dt.timedelta(hours=1)


def _move(src: Path, dst: Path) -> None:
    """os.replace is atomic on one filesystem. Windows refuses to rename a file a reader has
    open; wait for the reader rather than fail the compaction."""
    for i in range(20):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            time.sleep(0.05 * (i + 1))
    os.replace(src, dst)


def _acquire(lock: Path) -> bool:
    lock.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.close(os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
        return True
    except FileExistsError:
        try:
            born = dt.datetime.fromtimestamp(lock.stat().st_mtime, dt.UTC)
        except FileNotFoundError:
            return _acquire(lock)  # released while we looked
        if dt.datetime.now(dt.UTC) - born < LOCK_STALE:
            return False  # another compaction is running
        lock.unlink(missing_ok=True)  # a crashed compaction's lock
        return _acquire(lock)


def _dedup(table: pa.Table) -> pa.Table:
    """The view's own rule (layout.dedup_sql), within one run's files in one partition -- where
    target and window are constant, so the natural key alone partitions."""
    con = duckdb.connect(database=":memory:")
    try:
        con.execute("SET TimeZone = 'UTC'")
        con.register("t", table)
        return con.execute(
            f"{dedup_sql('t', [NATURAL_KEY_COL])} ORDER BY {INGESTED_AT_COL}, {WRITE_ID_COL}"
        ).to_arrow_table()
    finally:
        con.close()


def _compact_partition(root: Path, part: Path, generation: str) -> None:
    groups: dict[str, list[Path]] = defaultdict(list)
    for f in sorted(part.glob("*.parquet")):
        groups[parse_file_name(f.name)[0]].append(f)
    for key, files in groups.items():
        if len(files) < 2:
            continue
        attempt = max(parse_file_name(f.name)[1] for f in files)
        folded = _dedup(
            pa.concat_tables([pq.ParquetFile(f).read() for f in files], promote_options="default")
        )
        staging = root / STAGING_DIR / generation
        staging.mkdir(parents=True, exist_ok=True)
        name = f"{key}-{attempt}-{ULID()}.parquet"
        pq.write_table(folded, staging / name)
        _move(staging / name, part / name)  # swap in: one atomic rename
        retired = root / RETIRED_DIR / generation / part.relative_to(root)
        retired.mkdir(parents=True, exist_ok=True)
        for f in files:
            _move(f, retired / f.name)  # swap out: renamed aside, not unlinked


def _purge_retired(root: Path) -> None:
    now = dt.datetime.now(dt.UTC)
    for gen in (root / RETIRED_DIR).glob("*"):
        try:
            born = ULID.from_str(gen.name).datetime
        except ValueError:
            continue
        if now - born > RETIRED_GRACE:
            shutil.rmtree(gen, ignore_errors=True)


def compact(target: str) -> None:
    root = get_settings().lake_root
    lock = root / f".compact-{check_target(target)}.lock"
    if not _acquire(lock):
        return
    try:
        generation = str(ULID())
        for part in sorted((root / f"target={target}").glob("window=*")):
            _compact_partition(root, part, generation)
        shutil.rmtree(root / STAGING_DIR / generation, ignore_errors=True)
        _purge_retired(root)
    finally:
        lock.unlink(missing_ok=True)
