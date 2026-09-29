"""Which day a request is served as: the UTC date (M20), or a validly signed override (M27)."""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import re
from collections.abc import Mapping

from testbench.portal.data import EPOCH

OVERRIDE_DATE_HEADER = "x-testbench-date"
OVERRIDE_SIGNATURE_HEADER = "x-testbench-signature"
_ISO_DAY = re.compile(r"\d{4}-\d{2}-\d{2}")


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def sign(day_text: str, secret: bytes) -> str:
    """hex(hmac_sha256(secret, day_text)) -- what `openssl dgst -sha256 -hmac` prints."""
    return hmac.new(secret, day_text.encode("ascii"), hashlib.sha256).hexdigest()


def request_day(
    headers: Mapping[str, str], *, now: dt.datetime, override_secret: bytes | None
) -> dt.date:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("request_day needs an aware datetime; a naive one is local time (M20)")
    override = _signed_override(headers, override_secret)
    return override if override is not None else now.astimezone(dt.UTC).date()


def _signed_override(headers: Mapping[str, str], secret: bytes | None) -> dt.date | None:
    """Ignored -- never rejected -- unless every check passes, so the public bench gives a
    stranger nothing to steer and nothing to probe."""
    if not secret:
        return None
    raw = headers.get(OVERRIDE_DATE_HEADER)
    signature = headers.get(OVERRIDE_SIGNATURE_HEADER)
    if raw is None or signature is None or not (raw.isascii() and signature.isascii()):
        return None
    if not _ISO_DAY.fullmatch(raw):
        return None
    if not hmac.compare_digest(sign(raw, secret), signature.strip().lower()):
        return None
    try:
        day = dt.date.fromisoformat(raw)
    except ValueError:
        return None
    return day if day >= EPOCH else None
