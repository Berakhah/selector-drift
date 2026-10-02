"""How a field's name becomes the text the rungs look for.

Task 5's FieldTarget carries no label, so the expected visible label is the field name,
humanised: `claim_number` -> "claim number". Comparison is on normalised text only --
case-folded, whitespace-collapsed, a trailing ':' or '*' dropped -- never on substrings, so
"Prior Amount" never matches `amount`.
"""

from __future__ import annotations

import re

from selector_drift.spec.schema import FieldTarget

_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_NON_ALNUM = re.compile(r"[^A-Za-z0-9]+")


def squash(text: str | None) -> str:
    return " ".join((text or "").split())


def normalise(text: str | None) -> str:
    return squash(text).rstrip(":*").strip().casefold()


def expected_label(target: FieldTarget) -> str:
    # The spec's `label` when the portal's visible label differs from the field name (Task 5,
    # added 2026-09-26); otherwise the humanised name. Every rung reads the label through here.
    return normalise(target.label or target.name.replace("_", " "))


def tokens(value: str) -> tuple[str, ...]:
    """`claimNumber`, `claim-number`, `col-claim_number` -> ('claim', 'number') and friends."""
    spaced = _CAMEL.sub(" ", value)
    return tuple(t.casefold() for t in _NON_ALNUM.split(spaced) if t)


def field_tokens(target: FieldTarget) -> tuple[str, ...]:
    return tokens(target.name)


__all__ = ["expected_label", "field_tokens", "normalise", "squash", "tokens"]
