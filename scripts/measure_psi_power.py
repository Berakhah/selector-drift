"""Reproduce the distributional check's published power and false-breach numbers.

Run from the repository root:  python scripts/measure_psi_power.py

Every breach decision goes through DistributionalAssertion.judge() -- the product's own breach
rule (PSI over max(threshold, chi-square critical), Jeffreys smoothing, under/overflow bins), the
same call assess() makes -- so these numbers are the product's, not a re-implementation's.
judge() is used rather than assess() because assess() refuses to judge below MIN_VALUES, and the
rows below MIN_VALUES are how MIN_VALUES is chosen.

Data: the bench's own amount distribution, log-uniform over [100, 250,000] at 2 dp. Reference:
30 runs x PAGE_SIZE values. Seeds are fixed, so a rerun prints the same numbers.

MIN_VALUES rule (docs/methodology.md): the smallest current n at which a x10 shift is detected
at least 95% of the time, rounded up to a multiple of 10.
"""

from __future__ import annotations

import datetime as dt
import math
import random

import numpy as np

from selector_drift.validate.assertions.distribution import (
    MIN_VALUES,
    REFERENCE_RUNS,
    DistributionalAssertion,
)
from testbench.mutations.adversarial.decoy_column import DecoyColumn
from testbench.portal.render import PAGE_SIZE, result_set

CHECK = DistributionalAssertion()
POWER_NS = (10, 15, 20, 25, 30, 35, 40, 45)
FALSE_BREACH_NS = (40, 50, 200)
DRAWS = 1000
DECOY_TRIALS = 300


def breach(reference: list[str], current: list[str]) -> bool:
    """The product's breach rule. A str from judge() is "no basis": not a breach."""
    j = CHECK.judge(reference, current, "amount")
    return not isinstance(j, str) and j.breach


def money(rng: np.random.Generator, n: int, scale: float = 1.0) -> list[str]:
    values = np.exp(rng.uniform(math.log(100), math.log(250_000), n)) * scale
    return [f"{v:,.2f}" for v in values]


def false_breach() -> None:
    print(f"false breach, same distribution (reference {REFERENCE_RUNS} x {PAGE_SIZE} values)")
    for n in FALSE_BREACH_NS:
        rng = np.random.default_rng(2000 + n)
        hits = sum(
            breach(money(rng, REFERENCE_RUNS * PAGE_SIZE), money(rng, n)) for _ in range(DRAWS)
        )
        print(f"  n={n:4d}  false breach {hits / DRAWS:6.1%}  ({DRAWS} draws)")


def power() -> int | None:
    print("x10 shift detected / false breach, by current n")
    first: int | None = None
    for n in POWER_NS:
        rng = np.random.default_rng(1000 + n)
        hit = fp = 0
        for _ in range(DRAWS):
            reference = money(rng, REFERENCE_RUNS * PAGE_SIZE)
            hit += breach(reference, money(rng, n, 10.0))
            fp += breach(reference, money(rng, n))
        if first is None and hit / DRAWS >= 0.95:
            first = n
        print(f"  n={n:3d}  x10 detected {hit / DRAWS:6.1%}  false breach {fp / DRAWS:6.1%}")
    return first


def decoy() -> None:
    print(f"bench DecoyColumn at PAGE_SIZE={PAGE_SIZE} ({DECOY_TRIALS} trials)")
    rng = random.Random(23)
    start = dt.date(2026, 1, 1)
    detected = false = 0
    for t in range(DECOY_TRIALS):
        d0 = start + dt.timedelta(days=t)
        reference = [
            f"{c.amount:,.2f}"
            for i in range(REFERENCE_RUNS)
            for c in result_set(d0 + dt.timedelta(days=i))[:PAGE_SIZE]
        ]
        real = [
            f"{c.amount:,.2f}"
            for c in result_set(d0 + dt.timedelta(days=REFERENCE_RUNS))[:PAGE_SIZE]
        ]
        column = DecoyColumn.from_rng(rng)
        detected += breach(reference, [column.decoy_value(v, row) for row, v in enumerate(real)])
        false += breach(reference, real)
    print(f"  decoy detected {detected / DECOY_TRIALS:6.1%}")
    print(f"  true values on the same pages breached (false) {false / DECOY_TRIALS:6.1%}")


if __name__ == "__main__":
    false_breach()
    first = power()
    derived = None if first is None else 10 * math.ceil(first / 10)
    print(
        f"smallest n with x10 detected >= 95%: {first}; MIN_VALUES by the rule: {derived}; "
        f"product MIN_VALUES: {MIN_VALUES}"
    )
    decoy()
