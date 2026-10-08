"""Names and paths of the lake, in one place (design 9.1)."""

from __future__ import annotations

import datetime as dt
import re
from pathlib import Path
from typing import Final

DATA_GLOB: Final = "target=*/window=*/*.parquet"
RUN_KEY_COL: Final = "run_key"
INGESTED_AT_COL: Final = "ingested_at"
META_PREFIX: Final = "_sd_"
NATURAL_KEY_COL: Final = "_sd_natural_key"
WRITE_ID_COL: Final = "_sd_write_id"
PROVENANCE_COL: Final = "_sd_provenance"
RESERVED: Final = frozenset({RUN_KEY_COL, INGESTED_AT_COL, "target", "window"})
STAGING_DIR: Final = "_staging"
RETIRED_DIR: Final = "_retired"
_TARGET = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")


def fs_key(run_key: str) -> str:
    """':' is illegal in Windows file names, and a run_key carries two."""
    return run_key.replace(":", "_")


def check_target(target: str) -> str:
    if not _TARGET.fullmatch(target):
        raise ValueError(f"{target!r} is not a target name (lowercase, digits, '-')")
    return target


def _window(window: str) -> str:
    try:
        exact = dt.date.fromisoformat(window).isoformat() == window
    except ValueError:
        exact = False
    if not exact:
        raise ValueError(f"window {window!r} is not a YYYY-MM-DD date")
    return window


def split_run_key(run_key: str) -> tuple[str, int, str]:
    parts = run_key.split(":")
    if len(parts) != 3 or not parts[1].isdigit():
        raise ValueError(f"{run_key!r} is not <target>:<spec_version>:<window>")
    target, version, window = parts
    return check_target(target), int(version), _window(window)


def partition_dir(root: Path, target: str, window: str) -> Path:
    return root / f"target={check_target(target)}" / f"window={_window(window)}"


def parse_file_name(name: str) -> tuple[str, int, str]:
    """{fs_key}-{attempt}-{ulid}.parquet. fs_key contains dashes (the date); a ULID has none."""
    key, attempt, ulid = name.removesuffix(".parquet").rsplit("-", 2)
    return key, int(attempt), ulid


def is_data_column(name: str) -> bool:
    return name not in RESERVED and not name.startswith(META_PREFIX)
