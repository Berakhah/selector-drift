from __future__ import annotations

import datetime as dt
import secrets

import pytest
from starlette.datastructures import Headers

from testbench.portal.day import request_day, sign

SECRET = secrets.token_bytes(32)
NOW = dt.datetime(2026, 9, 9, 12, 0, tzinfo=dt.UTC)


def signed(day_text: str, secret: bytes = SECRET, *, for_text: str | None = None) -> Headers:
    return Headers(
        {"X-Testbench-Date": day_text, "X-Testbench-Signature": sign(for_text or day_text, secret)}
    )


@pytest.mark.unit
def test_the_day_is_utc_not_local() -> None:
    """M20. 23:30 on 9 Sep in UTC-5 is 04:30 on 10 Sep in UTC. The seed, the run window and the
    scope expiry all use the UTC calendar; a local-time bench would shift its seed by a day."""
    utc_minus_5 = dt.timezone(dt.timedelta(hours=-5))
    now = dt.datetime(2026, 9, 9, 23, 30, tzinfo=utc_minus_5)
    assert request_day(Headers(), now=now, override_secret=None) == dt.date(2026, 9, 10)


@pytest.mark.unit
def test_a_naive_clock_is_refused() -> None:
    with pytest.raises(ValueError, match="aware"):
        request_day(Headers(), now=dt.datetime(2026, 9, 9, 12, 0), override_secret=None)


@pytest.mark.unit
def test_an_unsigned_date_override_is_ignored() -> None:
    headers = Headers({"X-Testbench-Date": "2026-09-01"})
    assert request_day(headers, now=NOW, override_secret=SECRET) == NOW.date()


@pytest.mark.unit
def test_a_signed_date_override_is_honoured() -> None:
    assert request_day(signed("2026-09-01"), now=NOW, override_secret=SECRET) == dt.date(2026, 9, 1)


@pytest.mark.unit
def test_an_upper_case_signature_is_honoured() -> None:
    h = Headers(
        {
            "X-Testbench-Date": "2026-09-01",
            "X-Testbench-Signature": sign("2026-09-01", SECRET).upper(),
        }
    )
    assert request_day(h, now=NOW, override_secret=SECRET) == dt.date(2026, 9, 1)


@pytest.mark.unit
def test_a_signature_for_another_date_is_ignored() -> None:
    h = signed("2026-09-01", for_text="2026-09-02")
    assert request_day(h, now=NOW, override_secret=SECRET) == NOW.date()


@pytest.mark.unit
def test_a_signature_made_with_another_secret_is_ignored() -> None:
    h = signed("2026-09-01", secrets.token_bytes(32))
    assert request_day(h, now=NOW, override_secret=SECRET) == NOW.date()


@pytest.mark.unit
def test_the_override_is_off_when_no_secret_is_configured() -> None:
    assert request_day(signed("2026-09-01"), now=NOW, override_secret=None) == NOW.date()


@pytest.mark.unit
def test_an_override_before_the_dataset_epoch_is_ignored() -> None:
    assert request_day(signed("2025-12-31"), now=NOW, override_secret=SECRET) == NOW.date()


@pytest.mark.unit
def test_a_malformed_override_is_ignored_even_when_signed() -> None:
    assert request_day(signed("2026-9-1"), now=NOW, override_secret=SECRET) == NOW.date()
