"""Process settings, from the environment only. No secret has a default.

The one Settings definition in the product (2026-09-26). Later tasks read fields from here and
define none: Task 6 (alert webhook), Task 18 (escalation corpus), Task 28 (lake root), Task 31
(session directory), Task 32 (credential store, KEK; per-target broker via `broker_env`),
Task 33 (fixture mode, web origin, public base URL, typegen).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from pydantic import SecretStr

DEFAULT_DATABASE_URL = "postgresql+psycopg://selector_drift@localhost:5432/selector_drift"
MIN_REDACTION_KEY_BYTES = 32


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class Settings:
    env: str  # SD_ENV: development | production | test | typegen
    database_url: str  # SD_DATABASE_URL -- db/session.py reads it from here
    spec_dir: Path  # SD_SPEC_DIR
    scope_dir: Path  # SD_SCOPE_DIR -- tests point it at a tmp copy
    evidence_root: Path  # SD_EVIDENCE_ROOT
    lake_root: Path  # SD_LAKE_ROOT
    fixture_mode: bool  # SD_FIXTURE_MODE=1 -- E2E only (web UI spec 6)
    fixture_evidence_root: Path  # SD_FIXTURE_EVIDENCE_ROOT
    web_origin: str  # SD_WEB_ORIGIN -- the Origin every non-GET must carry
    public_base_url: str  # SD_PUBLIC_BASE_URL -- problem-type URIs, method links
    alert_webhook_url: str | None  # SD_ALERT_WEBHOOK_URL -- None: every alert logged NOT SENT
    session_dir: Path  # SD_SESSION_DIR -- injected session state (Task 31)
    escalation_corpus: Path  # SD_ESCALATION_CORPUS -- rung 5's cases (Task 18)
    deploys_file: Path  # SD_DEPLOYS_FILE -- each target's deploy feed (Task 29A, M10)
    # SD_GITHUB_TOKEN -- optional, read-only deployments scope, for gh-actions feeds (Task 29A).
    # A SecretStr so the dataclass repr can never print it.
    github_token: SecretStr | None
    credential_store: Path  # SD_CREDENTIAL_STORE -- the encrypted store's root (Task 32)
    # SD_CREDENTIAL_KEK -- 64 hex chars, the key-encryption key (Task 32). Never on disk, never
    # committed; a SecretStr so no repr, log line or exception can print it.
    credential_kek: SecretStr | None
    _redaction_key_hex: str  # SD_REDACTION_KEY -- hex, >= 32 bytes, never committed

    @property
    def redaction_key(self) -> bytes:
        if not self._redaction_key_hex:
            raise ConfigError("SD_REDACTION_KEY is not set; evidence cannot be redacted")
        try:
            key = bytes.fromhex(self._redaction_key_hex)
        except ValueError as exc:
            # fix round 1, minor 5: bytes.fromhex raises ValueError, not ConfigError, on
            # non-hex input -- wrapped so every SD_REDACTION_KEY failure is one exception type.
            # The message never repeats the key itself (only ValueError's own generic position
            # text, e.g. "non-hexadecimal number found ... at position N").
            raise ConfigError("SD_REDACTION_KEY is not valid hex") from exc
        if len(key) < MIN_REDACTION_KEY_BYTES:
            raise ConfigError(f"SD_REDACTION_KEY must be at least {MIN_REDACTION_KEY_BYTES} bytes")
        return key

    @property
    def evidence_read_root(self) -> Path:
        """Where the API reads bundles. Fixture mode changes this and nothing else."""
        return self.fixture_evidence_root if self.fixture_mode else self.evidence_root

    @classmethod
    def for_typegen(cls) -> Settings:
        """For scripts/gen-types.sh: builds the app to export its schema. Holds no secret."""
        return cls(
            env="typegen",
            database_url="postgresql+psycopg://typegen@invalid/none",
            spec_dir=Path("specs"),
            scope_dir=Path("scopes"),
            evidence_root=Path("evidence"),
            lake_root=Path("lake"),
            fixture_mode=False,
            fixture_evidence_root=Path("web/e2e/fixtures"),
            web_origin="http://localhost:3000",
            public_base_url="https://selector-drift.invalid",
            alert_webhook_url=None,
            session_dir=Path("sessions"),
            escalation_corpus=Path("corpus/escalations"),
            deploys_file=Path("deploys.yaml"),
            github_token=None,
            credential_store=Path("credentials"),
            credential_kek=None,
            _redaction_key_hex="",
        )


def _env_or_default(name: str, default: str) -> str:
    """`os.environ.get(name, default)` only falls back to `default` when `name` is entirely
    unset; a variable set to the empty string (exactly what a bare `KEY=` line in `.env.example`
    produces once sourced, or a docker compose `env_file` entry with no value) is returned as
    `""` instead. Fix round 1, minor 6: that made `SD_DATABASE_URL=` a crash at `db/session`
    import time and `SD_PUBLIC_BASE_URL=` a silent empty base URL. A set-but-empty variable is
    treated exactly like an unset one here.
    """
    value = os.environ.get(name)
    return value if value else default


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    e = os.environ.get
    return Settings(
        env=_env_or_default("SD_ENV", "development"),
        database_url=_env_or_default("SD_DATABASE_URL", DEFAULT_DATABASE_URL),
        spec_dir=Path(_env_or_default("SD_SPEC_DIR", "specs")),
        scope_dir=Path(_env_or_default("SD_SCOPE_DIR", "scopes")),
        evidence_root=Path(_env_or_default("SD_EVIDENCE_ROOT", "evidence")),
        lake_root=Path(_env_or_default("SD_LAKE_ROOT", "lake")),
        fixture_mode=_env_or_default("SD_FIXTURE_MODE", "0") == "1",
        fixture_evidence_root=Path(_env_or_default("SD_FIXTURE_EVIDENCE_ROOT", "web/e2e/fixtures")),
        web_origin=_env_or_default("SD_WEB_ORIGIN", "http://localhost:3000"),
        public_base_url=_env_or_default("SD_PUBLIC_BASE_URL", "https://selector-drift.invalid"),
        # No default to fall back to (None means "no alert channel configured"): a set-but-empty
        # SD_ALERT_WEBHOOK_URL already collapses to None via `.strip() or None`, same as unset.
        alert_webhook_url=e("SD_ALERT_WEBHOOK_URL", "").strip() or None,
        session_dir=Path(_env_or_default("SD_SESSION_DIR", "sessions")),
        escalation_corpus=Path(_env_or_default("SD_ESCALATION_CORPUS", "/data/corpus/escalations")),
        deploys_file=Path(_env_or_default("SD_DEPLOYS_FILE", "deploys.yaml")),
        # No default (a credential): set-but-empty behaves as unset.
        github_token=SecretStr(token) if (token := e("SD_GITHUB_TOKEN", "").strip()) else None,
        credential_store=Path(_env_or_default("SD_CREDENTIAL_STORE", "credentials")),
        # No default (a secret): set-but-empty behaves as unset.
        credential_kek=SecretStr(kek) if (kek := e("SD_CREDENTIAL_KEK", "").strip()) else None,
        # No default (a secret): a set-but-empty SD_REDACTION_KEY already behaves as unset --
        # `redaction_key` raises ConfigError on any falsy `_redaction_key_hex`.
        _redaction_key_hex=e("SD_REDACTION_KEY", ""),
    )


# The target-name pattern of scope.record.TARGET_SLUG, repeated rather than imported: config is
# the bottom of the import graph and must not import the scope package.
_TARGET_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")


@dataclass(frozen=True)
class BrokerEnv:
    """One target's client-broker configuration (Task 32). The token is a SecretStr, so the
    dataclass repr can never print it; the variable names are kept for refusal messages."""

    url_var: str
    token_var: str
    url: str | None
    token: SecretStr | None


def broker_env(target: str) -> BrokerEnv:
    """`SD_BROKER_URL_<TARGET>` / `SD_BROKER_TOKEN_<TARGET>` for one target (`claims-portal` ->
    `CLAIMS_PORTAL`). Per-target names cannot be fixed fields on `Settings`, so this is the one
    accessor that reads them -- still here, the only module that reads the environment. Read on
    every call (not cached): a rotated token is picked up by the next run. Set-but-empty is
    unset."""
    if not _TARGET_NAME.fullmatch(target):
        raise ValueError(f"{target!r} is not a target name")
    suffix = target.upper().replace("-", "_")
    url_var, token_var = f"SD_BROKER_URL_{suffix}", f"SD_BROKER_TOKEN_{suffix}"
    url = os.environ.get(url_var, "").strip() or None
    token = os.environ.get(token_var, "").strip()
    return BrokerEnv(url_var, token_var, url, SecretStr(token) if token else None)
