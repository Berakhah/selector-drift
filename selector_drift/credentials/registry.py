"""Which provider a target uses -- named by its scope record (`credential_posture`, Task 32),
never guessed. An unknown posture is refused when the scope record loads (the field is a
Literal); a known posture with no provider is refused here; neither ever falls back to a weaker
posture. A posture registers once: `register()` refuses to replace one, so a weaker provider can
never quietly stand in for a stronger one.

A factory receives the whole `ScopeRecord`, so a per-client provider (the encrypted store) can
find the client its key belongs to.
"""

from __future__ import annotations

import re
import typing
from collections.abc import Callable
from pathlib import Path

from selector_drift.config import broker_env, get_settings
from selector_drift.credentials.broker import ClientBroker
from selector_drift.credentials.provider import CredentialProvider
from selector_drift.credentials.session import SessionInjection
from selector_drift.credentials.store import EncryptedStore
from selector_drift.scope.errors import ScopeRefusal
from selector_drift.scope.record import TARGET_SLUG, CredentialPosture, ScopeRecord
from selector_drift.spec.loader import load_scope

Factory = Callable[[ScopeRecord], CredentialProvider]
DEFAULT_POSTURE: CredentialPosture = "session_injection"


def _checked(target: str) -> str:
    if not TARGET_SLUG.fullmatch(target):
        raise ValueError(f"{target!r} is not a target name")
    return target


def session_path(target: str) -> Path:
    # Settings.session_dir (SD_SESSION_DIR), not a path derived from evidence_root: sessions
    # are credentials and get their own, explicitly configured place (2026-09-26). The slug
    # check keeps a target name from escaping it.
    return get_settings().session_dir / f"{_checked(target)}.json"


def _broker(record: ScopeRecord) -> CredentialProvider:
    env = broker_env(record.target)  # config.py is the only reader of the environment
    if env.url is None or env.token is None:
        raise LookupError(
            f"{record.target}: posture client_broker needs {env.url_var} and {env.token_var} "
            "-- refusing, not downgrading"
        )
    return ClientBroker(env.url, env.token)


def client_slug(authorising_party: str) -> str:
    """`Acme Claims Ltd` -> `acme-claims-ltd`: the encrypted store's per-client directory."""
    return re.sub(r"[^a-z0-9]+", "-", authorising_party.lower()).strip("-")


def _store(record: ScopeRecord) -> CredentialProvider:
    return EncryptedStore(client_slug(record.authorising_party))  # per CLIENT, not per target


_FACTORIES: dict[str, Factory] = {
    DEFAULT_POSTURE: lambda record: SessionInjection(session_path(record.target)),
    "client_broker": _broker,
    "encrypted_store": _store,
}
_POSTURES: frozenset[str] = frozenset(typing.get_args(CredentialPosture))


def register(posture: CredentialPosture, factory: Factory) -> None:
    """Add a provider for a posture that has none. Refuses a posture outside the Literal and
    refuses to replace an existing provider (controller ruling, Task 32)."""
    if posture not in _POSTURES:
        raise ValueError(f"{posture!r} is not a credential posture")
    if posture in _FACTORIES:
        raise ValueError(f"posture {posture!r} is already registered; refusing to replace it")
    _FACTORIES[posture] = factory


def provider_for(target: str) -> CredentialProvider:
    record = load_scope(get_settings().scope_dir / f"{_checked(target)}.yaml")
    if record.target != target:
        raise ScopeRefusal(f"scope file {target}.yaml names target {record.target!r}")
    factory = _FACTORIES.get(record.credential_posture)
    if factory is None:
        raise LookupError(
            f"{target}: no provider registered for credential posture "
            f"{record.credential_posture!r} -- refusing, not downgrading"
        )
    return factory(record)
