"""ClassRename: every CSS class token on the page is renamed, consistently.

The commonest UI change a portal ships -- a CSS-modules rebuild, a design-system prefix, a BEM
refactor -- and the one that breaks every selector written as `td.col-amount`.
"""

from __future__ import annotations

import random
import string
from dataclasses import dataclass
from typing import Self

from testbench.mutations.base import MutationBase, parse, serialise
from testbench.mutations.grid import all_elements, digest

SCHEMES = ("hashed", "prefixed", "bem")
_SALT_ALPHABET = string.ascii_lowercase + string.digits


@dataclass(frozen=True, slots=True)
class ClassRename(MutationBase):
    scheme: str = "hashed"
    salt: str = "k3x9q"

    def __post_init__(self) -> None:
        if self.scheme not in SCHEMES:
            raise ValueError(f"unknown class-rename scheme {self.scheme!r}")
        if not self.salt.isalnum():
            raise ValueError("the salt must be non-empty and alphanumeric")

    @classmethod
    def from_rng(cls, rng: random.Random) -> Self:
        return cls(scheme=rng.choice(SCHEMES), salt="".join(rng.choices(_SALT_ALPHABET, k=5)))

    def rename(self, token: str) -> str:
        if self.scheme == "hashed":  # CSS modules: name_hash
            return f"{token}_{digest(self.salt, token) % 16**5:05x}"
        if self.scheme == "prefixed":  # a design-system prefix
            return f"{self.salt}-{token}"
        return f"c-{self.salt}__{token}"  # BEM: block__element

    def apply(self, html: str) -> str:
        doc = parse(html)
        for el in all_elements(doc):
            tokens = (el.get("class") or "").split()
            if tokens:
                el.set("class", " ".join(self.rename(t) for t in tokens))
        return serialise(doc)
