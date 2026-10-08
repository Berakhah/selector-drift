"""The client-controlled credential broker (design 2.1 Q2, second in the preference order). The
client keeps the password and the TOTP seed; we ask their endpoint for what one run needs and
keep nothing -- no file, no cache, no attribute (tests/credentials/test_broker.py proves it
against every storage root the product has).

The bearer token travels in the `Authorization` header only: never in the URL (proxies and
httpx's own INFO request log record URLs), never in an exception message, never in a repr.
Every `BrokerError` is raised outside the `except` block that caught the underlying failure, so
neither `__cause__` nor `__context__` carries httpx's request or pydantic's echo of the input.
"""

from __future__ import annotations

import contextlib
import re
from typing import Any
from urllib.parse import urlsplit

import httpx
from pydantic import SecretStr, ValidationError

from selector_drift.credentials.provider import Credentials
from selector_drift.scope.record import AuthorisedTarget

_CODE = re.compile(r"^\d{6,8}$")


class BrokerError(Exception):
    """The broker could not answer usefully. Carries no token, no credential, no response body."""


def _checked_endpoint(endpoint: str) -> str:
    parts = urlsplit(endpoint)
    if parts.scheme != "https" or not parts.hostname:
        raise ValueError("the client broker must be an https URL")
    if "@" in parts.netloc or parts.query or parts.fragment:
        raise ValueError(
            "no userinfo, query string or fragment in a broker URL: the token travels "
            "in the Authorization header only"
        )
    return endpoint.rstrip("/")


class ClientBroker:
    def __init__(self, endpoint: str, token: SecretStr, *, timeout: float = 10.0) -> None:
        if not timeout > 0:
            raise ValueError("the broker timeout must be positive")
        self._endpoint = _checked_endpoint(endpoint)
        self._token = token
        self._timeout = timeout

    def __repr__(self) -> str:
        return f"ClientBroker(endpoint={self._endpoint!r}, token=SecretStr('**********'))"

    def _get(self, path: str, target: AuthorisedTarget) -> dict[str, Any]:
        failure: str | None = None
        body: Any = None
        try:
            response = httpx.get(
                f"{self._endpoint}/{path}",
                params={"target": target.target},
                headers={
                    "Authorization": f"Bearer {self._token.get_secret_value()}",
                    "Accept": "application/json",
                },
                timeout=self._timeout,
                follow_redirects=False,  # a redirect would carry the token somewhere else
            )
            if response.status_code != 200:
                failure = f"client broker answered {response.status_code} for /{path}"
            else:
                body = response.json()
        except (httpx.HTTPError, ValueError):
            failure = f"client broker unreachable or not JSON for /{path}"
        # Raised here, outside the except block: no chained httpx/JSON error rides along.
        if failure is not None:
            raise BrokerError(failure)
        if not isinstance(body, dict):
            raise BrokerError(f"client broker answer for /{path} is not an object")
        return body

    def get(self, target: AuthorisedTarget) -> Credentials:
        body = self._get("credentials", target)
        password = body.get("password")
        if password is not None and not isinstance(password, str):
            raise BrokerError("client broker returned malformed credentials")
        creds: Credentials | None = None
        # pydantic's message echoes the input (the password): suppressed here, converted below
        # outside the handler so no ValidationError rides along as __context__.
        with contextlib.suppress(ValidationError):
            creds = Credentials(
                cookies=body.get("cookies"),
                username=body.get("username"),
                password=SecretStr(password) if password is not None else None,
            )
        if creds is None:
            raise BrokerError("client broker returned malformed credentials")
        if not creds.cookies and not (creds.username and creds.password):
            raise BrokerError(
                "client broker returned no usable credential (neither a cookie nor a "
                "username and password)"
            )
        return creds

    def get_totp_code(self, target: AuthorisedTarget) -> str:
        """A fresh one-time code from the client's broker. The seed stays with the client; the
        code is returned to the caller and held nowhere here."""
        code = self._get("totp", target).get("code")
        if not isinstance(code, str) or not _CODE.fullmatch(code):
            raise BrokerError("the broker's answer is not a 6-8 digit code")
        return code
