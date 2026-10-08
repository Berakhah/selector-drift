"""Reproduce the distributional check's published power and false-breach numbers.

Run from the repository root:  python scripts/measure_psi_power.py

Every breach decision goes through DistributionalAssertion._judge() -- the product's own breach
rule (PSI over max(threshold, chi-square critical), Jeffreys smoothing, under/overflow, blank and
unparseable bins), the same call assess() makes -- so these numbers are the product's, not a
re-implementation's. The private _judge() is used rather than assess() because assess() refuses
to judge below MIN_VALUES, and the rows below MIN_VALUES are how MIN_VALUES is chosen.

Data: the bench's own amount distribution, log-uniform over [100, 250,000] at 2 dp. Reference:
30 runs x PAGE_SIZE values. Seeds are fixed, so a rerun prints the same numbers.

MIN_VALUES rule (docs/methodology.md): the smallest current n at which BOTH a x10 shift and a x3
shift are detected at least 95% of the time, rounded up to a multiple of 10. (x10 alone was the
rule until the overflow bin made a x10 shift visible from a handful of values above the
reference maximum; x3 keeps most of a shifted sample inside the reference range.)
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
POWER_NS = tuple(range(10, 65, 5))
FALSE_BREACH_NS = (40, 50, 200)
DRAWS = 1000
DECOY_TRIALS = 300


def breach(reference: list[str], current: list[str]) -> bool:
    """The product's breach rule. A str from _judge() is "no basis": not a breach."""
    # _judge, not assess(): the derivation must measure below MIN_VALUES, where assess() refuses.
    j = CHECK._judge(reference, current, "amount")
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
    print("x10 and x3 shifts detected / false breach, by current n")
    first: int | None = None
    for n in POWER_NS:
        rng = np.random.default_rng(1000 + n)
        x10 = x3 = fp = 0
        for _ in range(DRAWS):
            reference = money(rng, REFERENCE_RUNS * PAGE_SIZE)
            x10 += breach(reference, money(rng, n, 10.0))
            x3 += breach(reference, money(rng, n, 3.0))
            fp += breach(reference, money(rng, n))
        if first is None and min(x10, x3) / DRAWS >= 0.95:
            first = n
        print(
            f"  n={n:3d}  x10 detected {x10 / DRAWS:6.1%}  x3 detected {x3 / DRAWS:6.1%}  "
            f"false breach {fp / DRAWS:6.1%}"
        )
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
        f"smallest n with x10 AND x3 detected >= 95%: {first}; MIN_VALUES by the rule: {derived}; "
        f"product MIN_VALUES: {MIN_VALUES}"
    )
    decoy()
