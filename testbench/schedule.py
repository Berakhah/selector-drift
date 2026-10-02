from __future__ import annotations

import datetime as dt
import hashlib
import random
from collections.abc import Sequence

from testbench.mutations import catalogue as _catalogue
from testbench.mutations.base import CatalogueEntry, Mutation

SECOND_BENIGN_PROBABILITY = 0.35
ADVERSARIAL_PROBABILITY = 1 / 7  # about one adversarial day a week


def seed_for(day: dt.date, target: str) -> str:
    return hashlib.sha256(f"{day.isoformat()}:{target}".encode()).hexdigest()


def mutations_for(
    day: dt.date, target: str, catalogue: Sequence[CatalogueEntry] | None = None
) -> list[Mutation]:
    """One or two benign classes every day, then -- about one day in seven -- one adversarial
    class applied last, on top. Seeded from (day, target) alone. `catalogue=None` reads
    CATALOGUE at call time."""
    entries = tuple(_catalogue.CATALOGUE if catalogue is None else catalogue)
    rng = random.Random(int(seed_for(day, target), 16))
    benign = sorted((e for e in entries if not e.adversarial), key=lambda e: e.name)
    hostile = sorted((e for e in entries if e.adversarial), key=lambda e: e.name)
    chosen: list[CatalogueEntry] = []
    if benign:
        count = min(len(benign), 1 + int(rng.random() < SECOND_BENIGN_PROBABILITY))
        chosen.extend(rng.sample(benign, count))
    if hostile and rng.random() < ADVERSARIAL_PROBABILITY:
        chosen.append(rng.choice(hostile))
    return [e.build(rng) for e in chosen]


def scheduled_names(
    day: dt.date, target: str, catalogue: Sequence[CatalogueEntry] | None = None
) -> list[str]:
    return [m.name for m in mutations_for(day, target, catalogue)]
