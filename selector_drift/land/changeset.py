"""The reviewable change-set (design 9.1, 2.1 Q3). Reads the lake, diffs against the client's
prior snapshot, and writes a file the CLIENT applies. No connector, no network, no write
credential: every line defaults to accept=false (brief rule 7)."""

from __future__ import annotations

import csv
import io
import json
import os
from pathlib import Path
from typing import Any, Final, Literal

from selector_drift.config import get_settings
from selector_drift.land.catalog import query
from selector_drift.land.layout import fs_key, is_data_column, split_run_key
from selector_drift.spec.loader import load_spec

Action = Literal["add", "change", "unchanged"]
_FORMULA: Final = ("=", "+", "-", "@", "\t", "\r")


def _cell(value: str) -> str:
    """OWASP CSV injection: a value from a third party's page must not become a formula."""
    return f"'{value}" if value.startswith(_FORMULA) else value


def _read_prior(prior: Path, keys: list[str]) -> dict[tuple[str, ...], dict[str, str]]:
    if prior.suffix.lower() == ".json":
        loaded = json.loads(prior.read_text(encoding="utf-8"))
        if not isinstance(loaded, list):
            raise ValueError(f"{prior.name}: a JSON prior is a list of objects")
        records: list[dict[str, Any]] = [r for r in loaded if isinstance(r, dict)]
    else:
        with prior.open(newline="", encoding="utf-8") as fh:
            records = list(csv.DictReader(fh))
    out: dict[tuple[str, ...], dict[str, str]] = {}
    for i, r in enumerate(records):
        missing = [k for k in keys if k not in r]
        if missing:
            raise ValueError(f"{prior.name} record {i} lacks natural key field(s) {missing}")
        out[tuple(str(r[k]) for k in keys)] = {k: "" if v is None else str(v) for k, v in r.items()}
    return out


def _lines(run_key: str, prior: Path) -> tuple[list[str], list[dict[str, Any]]]:
    target, _version, _window = split_run_key(run_key)
    spec = load_spec(get_settings().spec_dir / f"{target}.yaml")
    keys = list(spec.natural_key)
    # `lake` is already deduplicated, so a later attempt of this run supersedes an earlier one;
    # the run_key filter keeps a different run that shares the window out of the export.
    result = query("select * from lake where run_key = ?", [run_key], target=target)
    current = result.dicts()
    if not current:
        raise LookupError(
            f"{run_key}: no landed rows -- a quarantined run has nothing to "
            "export until a human promotes it"
        )
    # Only the current spec's fields, in its order; never a column an older version left behind.
    fields = [
        f
        for f in spec.expected_schema
        if f not in keys and f in result.columns and is_data_column(f)
    ]
    before_by_key = _read_prior(prior, keys)
    lines: list[dict[str, Any]] = []
    for row in sorted(current, key=lambda r: [str(r[k]) for k in keys]):
        ident = tuple(str(row[k]) for k in keys)
        before = before_by_key.get(ident)
        for field in fields:
            new = "" if row.get(field) is None else str(row[field])
            action: Action
            if before is None:
                action, old = "add", ""
            else:
                old = before.get(field, "")
                action = "unchanged" if old.strip() == new.strip() else "change"
            lines.append(
                dict(zip(keys, ident, strict=True))
                | {
                    "field": field,
                    "prior_value": old,
                    "new_value": new,
                    "action": action,
                    "accept": False,
                }
            )
    return keys, lines


def changeset(
    run_key: str, prior: Path, fmt: Literal["csv", "json"], *, out_dir: Path | None = None
) -> Path:
    if fmt not in ("csv", "json"):
        raise ValueError(f"format must be csv or json, not {fmt!r}")
    keys, lines = _lines(run_key, prior)
    if fmt == "json":
        payload = json.dumps(lines, indent=2, ensure_ascii=False)
    else:
        buf = io.StringIO()
        writer = csv.DictWriter(
            buf,
            fieldnames=[*keys, "field", "prior_value", "new_value", "action", "accept"],
            lineterminator="\n",
        )
        writer.writeheader()
        for line in lines:
            writer.writerow(
                {k: "false" if k == "accept" else _cell(str(v)) for k, v in line.items()}
            )
        payload = buf.getvalue()
    target_dir = out_dir or get_settings().lake_root.parent / "changesets"
    target_dir.mkdir(parents=True, exist_ok=True)
    final = target_dir / f"{fs_key(run_key)}.{fmt}"
    tmp = target_dir / f".{final.name}.tmp"
    tmp.write_text(payload, encoding="utf-8")
    os.replace(tmp, final)
    return final
