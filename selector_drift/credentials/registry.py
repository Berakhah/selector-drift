"""Which provider a target uses -- named by its scope record, never guessed. An unknown posture
is a refusal, never a fallback to a weaker one.

Until Task 32 adds `ScopeRecord.credential_posture`, every target uses session injection, which
is also the default offer (design 2.1 Q2). A factory receives the whole `ScopeRecord`, so a
per-client provider (Task 32's encrypted store) can find the client its key belongs to.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from selector_drift.config import get_settings
from selector_drift.credentials.provider import CredentialProvider
from selector_drift.credentials.session import SessionInjection
from selector_drift.scope.errors import ScopeRefusal
from selector_drift.scope.record import TARGET_SLUG, ScopeRecord
from selector_drift.spec.loader import load_scope

Factory = Callable[[ScopeRecord], CredentialProvider]
DEFAULT_POSTURE = "session_injection"


def _checked(target: str) -> str:
    if not TARGET_SLUG.fullmatch(target):
        raise ValueError(f"{target!r} is not a target name")
    return target


def session_path(target: str) -> Path:
    # Settings.session_dir (SD_SESSION_DIR), not a path derived from evidence_root: sessions
    # are credentials and get their own, explicitly configured place (2026-09-26). The slug
    # check keeps a target name from escaping it.
    return get_settings().session_dir / f"{_checked(target)}.json"


_FACTORIES: dict[str, Factory] = {
    DEFAULT_POSTURE: lambda record: SessionInjection(session_path(record.target)),
}


def register(posture: str, factory: Factory) -> None:
    _FACTORIES[posture] = factory


def provider_for(target: str) -> CredentialProvider:
    record = load_scope(get_settings().scope_dir / f"{_checked(target)}.yaml")
    if record.target != target:
        raise ScopeRefusal(f"scope file {target}.yaml names target {record.target!r}")
    # The field arrives in Task 32 (orchestrate.run_record mirrors the same default).
    posture = str(getattr(record, "credential_posture", DEFAULT_POSTURE))
    factory = _FACTORIES.get(posture)
    if factory is None:
        raise LookupError(
            f"{target}: no provider registered for credential posture {posture!r} -- "
            "refusing, not downgrading"
        )
    return factory(record)
