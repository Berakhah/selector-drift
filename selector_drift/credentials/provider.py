"""What a target's credentials are, and the one operation only the client's own broker may
perform (design 2.1 Q2; brief rule 4). No field here, or anywhere, holds a TOTP seed --
tests/credentials/test_no_totp_seed.py inspects every model, dataclass and table to prove it."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, SecretStr

from selector_drift.scope.record import AuthorisedTarget


class NotSupported(Exception):
    """This provider cannot do that -- by design, not by omission."""


class CredentialsUnavailable(Exception):
    """No usable credential for the target. The run fails; it never proceeds logged out.
    Messages name files and counts, never a cookie value or a password."""


class Credentials(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    # A session cookie IS the login: excluded from repr (and so from str) like a password.
    cookies: list[dict[str, Any]] | None = Field(default=None, repr=False)
    username: str | None = None
    password: SecretStr | None = None


@runtime_checkable
class CredentialProvider(Protocol):
    def get(self, target: AuthorisedTarget) -> Credentials: ...

    def get_totp_code(self, target: AuthorisedTarget) -> str:
        """A one-time code, asked of the client's own broker (Task 32). Every other provider
        raises `NotSupported`: nothing in this product can compute one."""
        ...
