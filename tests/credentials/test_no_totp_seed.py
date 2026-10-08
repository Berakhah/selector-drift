"""Design 2.1 Q2 / brief rule 4. Storing a TOTP seed defeats the second factor. This is not a
policy we follow, it is a feature that does not exist -- in a Pydantic model, a dataclass, an
ORM-mapped class or a Core table, anywhere in `selector_drift`."""

import dataclasses
import importlib
import inspect
import pkgutil
import re
from pathlib import Path

import pytest
from sqlalchemy import Table

import selector_drift

FORBIDDEN = {
    "seed",
    "totp_seed",
    "totp_secret",
    "totp_key",
    "otp_seed",
    "otp_secret",
    "mfa_seed",
    "mfa_secret",
    "shared_secret",
    "otpauth_uri",
}
PATTERN = re.compile(r"(totp|otp|mfa|2fa|authenticator).*(seed|secret|key|uri)", re.IGNORECASE)


def _field_names(cls: type) -> set[str]:
    names: set[str] = set()
    model_fields = getattr(cls, "model_fields", None)
    if isinstance(model_fields, dict):  # Pydantic: field names and their aliases
        names |= set(model_fields)
        names |= {f.alias for f in model_fields.values() if getattr(f, "alias", None)}
    if dataclasses.is_dataclass(cls):
        names |= {f.name for f in dataclasses.fields(cls)}
    table = getattr(cls, "__table__", None)
    if isinstance(table, Table):  # SQLAlchemy ORM: the column names actually in the table
        names |= {c.name for c in table.columns}
        names |= {c.key for c in table.columns}
    return names


def _offending(name: str) -> bool:
    return name.lower() in FORBIDDEN or PATTERN.search(name) is not None


def _modules():
    for mod in pkgutil.walk_packages(selector_drift.__path__, "selector_drift."):
        if mod.name.endswith("__main__"):
            continue
        yield importlib.import_module(mod.name)


def find_offenders() -> tuple[list[str], int]:
    offenders: list[str] = []
    inspected = 0
    for m in _modules():
        for _, cls in inspect.getmembers(m, inspect.isclass):
            names = _field_names(cls)
            inspected += bool(names)
            offenders += [
                f"{m.__name__}.{cls.__name__}.{n}" for n in sorted(names) if _offending(n)
            ]
        for _, table in inspect.getmembers(m, lambda o: isinstance(o, Table)):
            inspected += 1
            offenders += [
                f"{m.__name__}.{table.name}.{c.name}" for c in table.columns if _offending(c.name)
            ]
    return offenders, inspected


@pytest.mark.unit
def test_no_field_anywhere_is_named_for_a_totp_seed():
    offenders, inspected = find_offenders()
    assert offenders == [], offenders
    assert inspected > 20, "the scan found almost nothing to inspect -- it is not looking"


@pytest.mark.unit
def test_the_scan_sees_pydantic_dataclass_and_orm_fields():
    """Non-vacuity: each of the three shapes the scan claims to cover is actually reached."""
    from selector_drift.credentials.provider import Credentials
    from selector_drift.db.models import ScopeRecordRow
    from selector_drift.scope.record import AuthorisedTarget

    assert "password" in _field_names(Credentials)  # Pydantic
    assert "record_sha256" in _field_names(AuthorisedTarget)  # dataclass
    assert "credential_posture" in _field_names(ScopeRecordRow)  # SQLAlchemy column


@pytest.mark.unit
@pytest.mark.parametrize(
    "name", ["seed", "totp_secret", "TOTP_KEY", "mfa_seed_b32", "authenticatorUri", "2fa_secret"]
)
def test_the_rule_catches_seed_shaped_names(name):
    assert _offending(name)


@pytest.mark.unit
@pytest.mark.parametrize("name", ["password", "username", "cookies", "credential_posture"])
def test_the_rule_leaves_ordinary_names_alone(name):
    assert not _offending(name)


@pytest.mark.unit
def test_totp_code_is_fetched_never_computed():
    # No TOTP library: a code is asked of the client's broker (Task 32), never derived here.
    root = Path(__file__).resolve().parents[2]
    for path in ("pyproject.toml", "uv.lock"):
        text = (root / path).read_text(encoding="utf-8").lower()
        assert "pyotp" not in text and "mintotp" not in text, path
