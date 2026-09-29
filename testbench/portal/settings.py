from __future__ import annotations

import os
import secrets
from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Settings:
    session_secret: str
    override_secret: bytes | None
    cookie_secure: bool

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        """Secrets come from the environment only (the VM's `.env`), never from the repo.

        An unset session secret becomes a random per-process value: sessions then do not survive
        a restart, which is harmless for a demo bench and never weak. An unset override secret
        disables the date override entirely.
        """
        source = os.environ if env is None else env
        session = source.get("TESTBENCH_SESSION_SECRET", "")
        override = source.get("TESTBENCH_OVERRIDE_SECRET", "")
        return cls(
            session_secret=session or secrets.token_hex(32),
            override_secret=override.encode("utf-8") if override else None,
            cookie_secure=source.get("TESTBENCH_COOKIE_SECURE", "1") != "0",
        )
