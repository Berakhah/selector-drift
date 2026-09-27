from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from selector_drift.config import DEFAULT_DATABASE_URL, ConfigError, get_settings

VARS = (
    "SD_ENV",
    "SD_DATABASE_URL",
    "SD_SPEC_DIR",
    "SD_SCOPE_DIR",
    "SD_EVIDENCE_ROOT",
    "SD_LAKE_ROOT",
    "SD_REDACTION_KEY",
    "SD_FIXTURE_MODE",
    "SD_FIXTURE_EVIDENCE_ROOT",
    "SD_WEB_ORIGIN",
    "SD_PUBLIC_BASE_URL",
    "SD_ALERT_WEBHOOK_URL",
    "SD_SESSION_DIR",
    "SD_ESCALATION_CORPUS",
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for name in VARS:
        monkeypatch.delenv(name, raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.mark.unit
def test_defaults_are_repo_relative_and_hold_no_secret() -> None:
    s = get_settings()
    assert (s.env, s.spec_dir, s.scope_dir) == ("development", Path("specs"), Path("scopes"))
    assert (s.evidence_root, s.lake_root) == (Path("evidence"), Path("lake"))
    assert s.database_url.startswith("postgresql+psycopg://")


@pytest.mark.unit
def test_every_directory_is_overridable_from_the_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("SD_SPEC_DIR", str(tmp_path / "sp"))
    monkeypatch.setenv("SD_SCOPE_DIR", str(tmp_path / "sc"))
    monkeypatch.setenv("SD_EVIDENCE_ROOT", str(tmp_path / "ev"))
    monkeypatch.setenv("SD_LAKE_ROOT", str(tmp_path / "lk"))
    monkeypatch.setenv("SD_DATABASE_URL", "postgresql+psycopg://u@db:5432/x")
    s = get_settings()
    assert s.spec_dir == tmp_path / "sp" and s.scope_dir == tmp_path / "sc"
    assert s.evidence_root == tmp_path / "ev" and s.lake_root == tmp_path / "lk"
    assert s.database_url == "postgresql+psycopg://u@db:5432/x"


@pytest.mark.unit
def test_the_one_settings_class_carries_every_later_tasks_field(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """One definition (2026-09-26): Tasks 6, 18, 28, 31 and 33 read these and define none."""
    s = get_settings()
    assert s.alert_webhook_url is None and s.fixture_mode is False
    assert s.session_dir == Path("sessions")
    assert s.escalation_corpus == Path("/data/corpus/escalations")
    assert s.evidence_read_root == s.evidence_root
    monkeypatch.setenv("SD_ALERT_WEBHOOK_URL", "  https://hooks.example.invalid/x ")
    monkeypatch.setenv("SD_SESSION_DIR", str(tmp_path / "sess"))
    monkeypatch.setenv("SD_ESCALATION_CORPUS", str(tmp_path / "corpus"))
    monkeypatch.setenv("SD_FIXTURE_MODE", "1")
    get_settings.cache_clear()
    s = get_settings()
    assert s.alert_webhook_url == "https://hooks.example.invalid/x"
    assert s.session_dir == tmp_path / "sess" and s.escalation_corpus == tmp_path / "corpus"
    assert s.evidence_read_root == s.fixture_evidence_root


@pytest.mark.unit
def test_a_missing_redaction_key_is_an_error_not_a_default() -> None:
    with pytest.raises(ConfigError, match="SD_REDACTION_KEY"):
        _ = get_settings().redaction_key


@pytest.mark.unit
def test_a_short_redaction_key_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SD_REDACTION_KEY", "ab" * 16)  # 16 bytes
    with pytest.raises(ConfigError, match="32 bytes"):
        _ = get_settings().redaction_key


@pytest.mark.unit
def test_a_32_byte_hex_key_is_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SD_REDACTION_KEY", "ab" * 32)  # test-only, not a secret
    assert get_settings().redaction_key == bytes.fromhex("ab" * 32)


@pytest.mark.unit
def test_non_hex_redaction_key_is_a_config_error_not_a_value_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fix round 1, minor 5. `bytes.fromhex` itself raises ValueError; every SD_REDACTION_KEY
    failure must surface as ConfigError, and the message must not repeat the bad key."""
    secret = "zz" * 32  # not valid hex; must never appear in the raised message
    monkeypatch.setenv("SD_REDACTION_KEY", secret)
    with pytest.raises(ConfigError) as exc_info:
        _ = get_settings().redaction_key
    assert secret not in str(exc_info.value)


@pytest.mark.unit
def test_a_set_but_empty_variable_falls_back_to_the_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fix round 1, minor 6. `.env.example` sourced verbatim sets bare `KEY=` lines to the empty
    string, not unset; SD_DATABASE_URL='' must not crash db/session and SD_PUBLIC_BASE_URL=''
    must not silently become the empty string -- both fall back to their real defaults, exactly
    as if the variable were never set."""
    monkeypatch.setenv("SD_DATABASE_URL", "")
    monkeypatch.setenv("SD_PUBLIC_BASE_URL", "")
    monkeypatch.setenv("SD_SPEC_DIR", "")
    s = get_settings()
    assert s.database_url == DEFAULT_DATABASE_URL
    assert s.public_base_url == "https://selector-drift.invalid"
    assert s.spec_dir == Path("specs")
