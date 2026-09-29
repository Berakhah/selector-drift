"""The synthetic claims dataset and its daily delta (bench spec §4).

Everything here is a pure function of the date. No real person's data exists at any point:
claimants are drawn from two fixed lists of obviously synthetic words.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import math
import random
import string
from collections.abc import Container
from dataclasses import dataclass, replace
from decimal import Decimal
from enum import StrEnum

EPOCH = dt.date(2026, 1, 1)  # the dataset's origin -- not a claim about when anything ran
BASE_SIZE = 500
BASE_SEED = 0
RETENTION = dt.timedelta(days=3 * 365)
CHANGE_RATE = 0.02
CENT = Decimal("0.01")
MIN_MONEY = Decimal("100.00")
MAX_MONEY = Decimal("250000.00")

COLUMNS: tuple[tuple[str, str], ...] = (
    ("claim_number", "Claim Number"),
    ("claimant", "Claimant"),
    ("amount", "Amount"),
    ("reserve", "Reserve"),
    ("status", "Status"),
    ("filed", "Filed"),
)

FIRST_NAMES = (
    "Ada",
    "Bram",
    "Cleo",
    "Dov",
    "Esme",
    "Finn",
    "Gus",
    "Hana",
    "Ivo",
    "Juno",
    "Kai",
    "Lea",
)
LAST_NAMES = (
    "Placeholder",
    "Fixture",
    "Synthetic",
    "Mockley",
    "Stubbs",
    "Testwell",
    "Dummett",
    "Sampleton",
)


class Status(StrEnum):
    OPEN = "Open"
    PENDING = "Pending"
    PAID = "Paid"
    CLOSED = "Closed"


_NEXT_STATUS: dict[Status, Status] = {
    Status.OPEN: Status.PENDING,
    Status.PENDING: Status.PAID,
    Status.PAID: Status.CLOSED,
}
_BASE_STATUS_WEIGHTS = (35, 25, 20, 20)


@dataclass(frozen=True, slots=True)
class Claim:
    claim_number: str
    claimant: str
    amount: Decimal
    reserve: Decimal
    status: Status
    filed: dt.date


def _seed(text: str) -> int:
    return int.from_bytes(hashlib.sha256(text.encode("utf-8")).digest()[:8], "big")


def _clamp(value: Decimal) -> Decimal:
    return min(max(value, MIN_MONEY), MAX_MONEY)


def _money(rng: random.Random) -> Decimal:
    """Log-uniform over [100.00, 250,000.00]: most claims small, a few large. Amount and Reserve
    are two independent draws -- no footing rule relates them (bench spec §4)."""
    value = math.exp(rng.uniform(math.log(float(MIN_MONEY)), math.log(float(MAX_MONEY))))
    return _clamp(Decimal(repr(value)).quantize(CENT))


def _nudge(amount: Decimal, rng: random.Random) -> Decimal:
    moved = _clamp((amount * Decimal(repr(rng.uniform(0.85, 1.15)))).quantize(CENT))
    if moved == amount:
        moved = amount - Decimal("1.00") if amount > MIN_MONEY else amount + Decimal("1.00")
    return moved


def _claim_number(rng: random.Random, taken: Container[str]) -> str:
    while True:
        number = "".join(rng.choices(string.ascii_uppercase, k=2)) + f"{rng.randrange(10**8):08d}"
        if number not in taken:
            return number


def _new_claim(
    rng: random.Random, *, filed: dt.date, status: Status, taken: Container[str]
) -> Claim:
    return Claim(
        claim_number=_claim_number(rng, taken),
        claimant=f"{rng.choice(FIRST_NAMES)} {rng.choice(LAST_NAMES)}",
        amount=_money(rng),
        reserve=_money(rng),
        status=status,
        filed=filed,
    )


def _display_order(claim: Claim) -> tuple[int, str]:
    return (-claim.filed.toordinal(), claim.claim_number)  # newest first, like a real portal


def _base_set() -> tuple[Claim, ...]:
    rng = random.Random(BASE_SEED)
    claims: dict[str, Claim] = {}
    statuses = list(Status)
    while len(claims) < BASE_SIZE:
        filed = EPOCH - dt.timedelta(days=rng.randrange(RETENTION.days))
        status = rng.choices(statuses, weights=_BASE_STATUS_WEIGHTS)[0]
        claim = _new_claim(rng, filed=filed, status=status, taken=claims)
        claims[claim.claim_number] = claim
    return tuple(sorted(claims.values(), key=_display_order))


def _apply_delta(claims: tuple[Claim, ...], day: dt.date) -> tuple[Claim, ...]:
    """About 2% of claims change Amount or Status, about 3 close, about 5 are filed, and claims
    older than three years are archived. Seeded from the date alone."""
    rng = random.Random(_seed(f"delta:{day.isoformat()}"))
    by_key = {c.claim_number: c for c in claims}

    live = sorted(k for k, c in by_key.items() if c.status is not Status.CLOSED)
    for key in rng.sample(live, min(len(live), max(1, round(len(by_key) * CHANGE_RATE)))):
        claim = by_key[key]
        if rng.random() < 0.5:
            by_key[key] = replace(claim, amount=_nudge(claim.amount, rng))
        else:
            by_key[key] = replace(claim, status=_NEXT_STATUS[claim.status])

    still_live = sorted(k for k, c in by_key.items() if c.status is not Status.CLOSED)
    for key in rng.sample(still_live, min(len(still_live), rng.randint(2, 4))):
        by_key[key] = replace(by_key[key], status=Status.CLOSED)

    for _ in range(rng.randint(3, 7)):
        claim = _new_claim(rng, filed=day, status=Status.OPEN, taken=by_key)
        by_key[claim.claim_number] = claim

    horizon = day - RETENTION
    return tuple(sorted((c for c in by_key.values() if c.filed >= horizon), key=_display_order))


_MEMO: dict[dt.date, tuple[Claim, ...]] = {}


def dataset_for(day: dt.date) -> tuple[Claim, ...]:
    """The claims as they stand on `day`, in display order. Stepped forward from the nearest
    day already materialised; deterministic whichever day it steps from."""
    if day < EPOCH:
        raise ValueError(
            f"the dataset begins on {EPOCH.isoformat()}; {day.isoformat()} is before it"
        )
    cached = _MEMO.get(day)
    if cached is not None:
        return cached
    earlier = [d for d in _MEMO if d < day]
    if earlier:
        current = max(earlier)
        claims = _MEMO[current]
    else:
        current, claims = EPOCH, _base_set()
        _MEMO[EPOCH] = claims
    while current < day:
        current += dt.timedelta(days=1)
        claims = _apply_delta(claims, current)
        _MEMO[current] = claims
    return claims


def clear_cache() -> None:
    _MEMO.clear()


def find_claim(day: dt.date, claim_number: str) -> Claim | None:
    return next((c for c in dataset_for(day) if c.claim_number == claim_number), None)


def display_row(claim: Claim) -> dict[str, str]:
    return {
        "claim_number": claim.claim_number,
        "claimant": claim.claimant,
        "amount": f"{claim.amount:,.2f}",
        "reserve": f"{claim.reserve:,.2f}",
        "status": claim.status.value,
        "filed": claim.filed.isoformat(),
    }
