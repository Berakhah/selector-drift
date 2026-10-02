"""The Mutation protocol, the catalogue entry, and the one lxml round trip every mutation shares.

Rules for every mutation (Tasks 9-11A): `apply` is pure and deterministic; it never decides
anything based on `data-sd-*` attributes and never writes one, never drops an attribute it does
not own, removes `data-sd-truth` from any element it clones (which means it must look at the
attribute to strip it -- hence "decides nothing by", not "never reads"; reworded 2026-09-26), and
never renames a form field's `name` or a submit button.
"""

from __future__ import annotations

import random
from abc import ABC, abstractmethod
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Protocol, Self, runtime_checkable

import lxml.html

DOCTYPE = "<!DOCTYPE html>"


@runtime_checkable
class Mutation(Protocol):
    @property
    def name(self) -> str: ...

    def apply(self, html: str) -> str: ...


class MutationBase(ABC):
    @property
    def name(self) -> str:
        return type(self).__name__

    @abstractmethod
    def apply(self, html: str) -> str: ...

    @classmethod
    def from_rng(cls, rng: random.Random) -> Self:
        """Build the day's instance. Parameterised classes (e.g. LabelReword's wording) override
        this and draw from `rng`; parameterless classes inherit it."""
        return cls()


@dataclass(frozen=True, slots=True)
class CatalogueEntry:
    name: str
    build: Callable[[random.Random], Mutation]
    adversarial: bool


def entry(cls: type[MutationBase], *, adversarial: bool) -> CatalogueEntry:
    return CatalogueEntry(cls.__name__, cls.from_rng, adversarial)


def parse(html: str) -> lxml.html.HtmlElement:
    return lxml.html.document_fromstring(html)


def serialise(root: lxml.html.HtmlElement) -> str:
    out = lxml.html.tostring(root, doctype=DOCTYPE, encoding="unicode", method="html")
    assert isinstance(out, str)
    return out


def apply_all(mutations: Sequence[Mutation], html: str) -> str:
    for mutation in mutations:
        html = mutation.apply(html)
    return html
