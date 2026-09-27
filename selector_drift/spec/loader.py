"""YAML in, typed config out. `yaml.safe_load` and nothing else (R13/H5).

A scope record that is missing, malformed or outside the v1 enum is a refusal, never a crash:
design 9.3 lists "unknown target" and "basis outside the v1 enum" as Refuse, and a refusal is
what the run record and the alert channel report.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import ValidationError

from selector_drift.config import get_settings
from selector_drift.scope.errors import ScopeRefusal
from selector_drift.scope.record import ScopeRecord
from selector_drift.spec.schema import SpecError, TaskSpec


def load_spec_text(text: str) -> TaskSpec:
    data = yaml.safe_load(text)  # yaml.YAMLError propagates: an object tag is refused here
    if not isinstance(data, dict):
        raise SpecError("a task spec is a YAML mapping")
    try:
        return TaskSpec.model_validate(data)
    except ValidationError as exc:
        raise SpecError(f"invalid task spec: {exc}") from exc


def load_spec(path: Path) -> TaskSpec:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise SpecError(f"no task spec at {path}") from exc
    try:
        return load_spec_text(text)
    except SpecError as exc:
        raise SpecError(f"{path}: {exc}") from exc


def load_scope_text(text: str) -> ScopeRecord:
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ScopeRefusal(f"scope record is not valid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise ScopeRefusal("a scope record is a YAML mapping")
    try:
        return ScopeRecord.model_validate(data)
    except ValidationError as exc:
        raise ScopeRefusal(f"invalid scope record: {exc}") from exc


def load_scope(path: Path) -> ScopeRecord:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ScopeRefusal(f"unknown target: no scope record at {path}") from exc
    try:
        return load_scope_text(text)
    except ScopeRefusal as exc:
        raise ScopeRefusal(f"{path}: {exc.reason}") from exc


def spec_file(spec_path: str | Path) -> Path:
    """An absolute path as given; anything else by file name under SD_SPEC_DIR."""
    p = Path(spec_path)
    return p if p.is_absolute() else get_settings().spec_dir / p.name


def scope_file(scope_ref: str) -> Path:
    """A spec's `scopes/<target>.yaml`, resolved under SD_SCOPE_DIR."""
    return get_settings().scope_dir / Path(scope_ref).name


def target_of(spec_path: str | Path) -> str:
    """Spec files are named `<target>.yaml`; test_loader pins it for every file in specs/."""
    return Path(spec_path).stem
