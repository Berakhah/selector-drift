"""IdChurn: every element id is regenerated, and every reference to it follows.

What a framework upgrade or a WebForms control-tree change does to ids. Breaks every selector
written as #results; the page still works, because labels, ARIA references and fragment links
are rewritten with the ids they point at.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Self

from testbench.mutations.base import MutationBase, parse, serialise
from testbench.mutations.grid import all_elements

SCHEMES = ("aspnet", "react", "suffix")
ID_LIST_ATTRS = (
    "for",
    "aria-labelledby",
    "aria-describedby",
    "aria-controls",
    "aria-owns",
    "aria-activedescendant",
    "headers",
    "list",
    "form",
)


@dataclass(frozen=True, slots=True)
class IdChurn(MutationBase):
    scheme: str = "aspnet"
    token: int = 7

    def __post_init__(self) -> None:
        if self.scheme not in SCHEMES:
            raise ValueError(f"unknown id-churn scheme {self.scheme!r}")
        if not 1 <= self.token < 4096:
            raise ValueError("token must be in [1, 4096)")

    @classmethod
    def from_rng(cls, rng: random.Random) -> Self:
        return cls(scheme=rng.choice(SCHEMES), token=rng.randrange(1, 4096))

    def new_id(self, old: str, position: int) -> str:
        if self.scheme == "aspnet":  # WebForms naming container
            return f"ctl{self.token:02d}_MainContent_{old}"
        if self.scheme == "react":  # useId(): opaque, positional
            return f":r{self.token + position:x}:"
        return f"{old}-{self.token:03x}"  # a build-hash suffix

    def apply(self, html: str) -> str:
        doc = parse(html)
        mapping: dict[str, str] = {}
        with_ids = [el for el in all_elements(doc) if el.get("id")]
        for position, el in enumerate(with_ids):
            old = str(el.get("id"))
            new = self.new_id(old, position)
            mapping.setdefault(old, new)
            el.set("id", new)
        for el in all_elements(doc):
            for attr in ID_LIST_ATTRS:
                refs = el.get(attr)
                if refs:
                    el.set(attr, " ".join(mapping.get(r, r) for r in refs.split()))
            href = el.get("href")
            if href and href.startswith("#") and href[1:] in mapping:
                el.set("href", "#" + mapping[href[1:]])
        return serialise(doc)
