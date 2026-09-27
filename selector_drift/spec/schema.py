"""The task spec: what to extract, described in words. Config, never code (R13/H5).

A spec names each field by its meaning -- "the claim number cell in the results table" -- never
by a DOM path; the cascade (Tasks 13-18) turns a meaning into an element. Every model forbids
unknown keys: a misspelt `asertions:` that loaded silently would drop a field's checks.
"""

from __future__ import annotations

import ast
import re
from datetime import date
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Annotated, Final, Literal, Self, TypedDict

import regex
from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
    with_config,
)

MAX_MATCH_LEN: Final = 256  # 3x the longest bounded field in specs/ (claimant, 80)
"""Longest value the matcher will test. A spec pattern meeting a page value is untrusted input on
both sides; bounding the input bounds polynomial backtracking. Task 21's FormatAssertion and
Task 17's rung 1 share it."""
MATCH_TIMEOUT_S: Final = 0.05  # per value; checked inside the regex engine's loop
MatchResult = Literal["match", "no_match", "too_long", "timeout"]


class SpecError(ValueError):
    """A task spec that cannot be used as written. Refused, never repaired."""


def compile_pattern(pattern: str) -> regex.Pattern[str]:
    try:
        return regex.compile(pattern)  # the module caches compiled patterns
    except regex.error as exc:
        raise SpecError(f"invalid format pattern {pattern!r}: {exc}") from exc


def match_value(pattern: str, value: str) -> MatchResult:
    """Untrusted pattern (spec) meets untrusted value (page). Bounded twice (H5): a length cap and
    a per-match timeout, which catches exponential backtracking a cap alone does not (Task 21)."""
    if len(value) > MAX_MATCH_LEN:
        return "too_long"
    try:
        hit = compile_pattern(pattern).fullmatch(value, timeout=MATCH_TIMEOUT_S)
    except TimeoutError:
        return "timeout"
    return "no_match" if hit is None else "match"


class Relation(StrEnum):
    RIGHT_OF = "right_of"
    BELOW = "below"
    WITHIN = "within"


class FieldType(StrEnum):
    STRING = "string"
    INTEGER = "integer"
    DECIMAL = "decimal"
    DATE = "date"


def _iso_date(value: str) -> str:
    if date.fromisoformat(value).isoformat() != value:
        raise ValueError(f"{value!r} is not a YYYY-MM-DD date")
    return value


IsoDate = Annotated[str, AfterValidator(_iso_date)]
FieldName = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")]


@with_config(ConfigDict(extra="forbid"))
class DomainRule(TypedDict, total=False):
    min: float
    max: float
    enum: list[str]
    date_after: IsoDate
    date_before: IsoDate
    currency: Annotated[str, Field(pattern=r"^[A-Z]{3}$")]  # ISO 4217; Task 21's sanity rule


def _check_rule(rule: DomainRule) -> None:
    if not rule:
        raise ValueError(
            "a domain rule needs at least one of min, max, enum, date_after, date_before, currency"
        )
    lo, hi = rule.get("min"), rule.get("max")
    if lo is not None and hi is not None and lo > hi:
        raise ValueError(f"domain rule has min {lo} above max {hi}")
    if "enum" in rule and not rule["enum"]:
        raise ValueError("a domain enum lists at least one value")
    after, before = rule.get("date_after"), rule.get("date_before")
    if after is not None and before is not None and after >= before:
        raise ValueError(f"domain rule date_after {after} is not before date_before {before}")


_PAYLOAD: dict[str, str] = {"format": "pattern", "domain": "rule", "cross_field": "expression"}


class AssertionSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    type: Literal["format", "domain", "cross_field"]
    pattern: str | None = None
    rule: DomainRule | None = None
    expression: str | None = Field(default=None, max_length=200)

    @model_validator(mode="after")
    def _exactly_its_own_payload(self) -> Self:
        given = {k for k in _PAYLOAD.values() if getattr(self, k) is not None}
        needed = _PAYLOAD[self.type]
        if given != {needed}:
            raise ValueError(
                f"a {self.type} assertion takes exactly `{needed}`, "
                f"got {sorted(given) or 'nothing'}"
            )
        if self.pattern is not None:
            try:
                compile_pattern(self.pattern)  # the engine match_value() runs
            except SpecError as exc:
                raise ValueError(f"pattern does not compile: {exc}") from exc
        if self.rule is not None:
            _check_rule(self.rule)
        if self.expression is not None:
            try:
                ast.parse(self.expression, mode="eval")  # parsed, NEVER evaluated here
            except SyntaxError as exc:
                raise ValueError(f"cross_field expression does not parse: {exc.msg}") from exc
        return self


_DOM_SYNTAX = re.compile(r"[#\[\]>=]|//|::")


class FieldTarget(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: FieldName
    description: str = Field(min_length=1, max_length=200)
    label: str | None = Field(default=None, min_length=1, max_length=80)  # portal's visible label
    relation: Relation | None = None
    assertions: list[AssertionSpec]
    distributional: bool = False

    @field_validator("description")
    @classmethod
    def _words_not_a_selector(cls, value: str) -> str:
        if _DOM_SYNTAX.search(value):
            raise ValueError(
                "describe the field in words; selector syntax (# [ ] > = // ::) is a DOM path"
            )
        return value


_NAV_KEYS: dict[str, frozenset[str]] = {
    "goto": frozenset({"path"}),
    "fill": frozenset({"field", "value_from"}),
    "click": frozenset({"role", "name"}),
}
_PATH = re.compile(r"^/(?!/)[^\s\\]*$")  # one leading '/', no scheme, no host
_PLAIN_FIELD = re.compile(r"^[A-Za-z0-9_-]{1,64}$")  # a form field name, never a selector
_ROLE = re.compile(r"^[a-z]{2,20}$")  # an ARIA role name


class NavStep(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    step: Literal["goto", "fill", "click"]
    path: str | None = None
    field: str | None = None
    value_from: Literal["credentials.username", "credentials.password"] | None = None
    role: str | None = None
    name: str | None = None

    @model_validator(mode="after")
    def _exactly_the_keys_its_kind_takes(self) -> Self:
        given = {
            k
            for k in ("path", "field", "value_from", "role", "name")
            if getattr(self, k) is not None
        }
        wanted = _NAV_KEYS[self.step]
        if given != wanted:
            raise ValueError(
                f"a {self.step} step takes exactly {sorted(wanted)}, got {sorted(given)}"
            )
        if self.path is not None and not _PATH.fullmatch(self.path):
            raise ValueError(
                "goto takes a same-origin path starting with a single '/', never a URL (H2)"
            )
        if self.field is not None and not _PLAIN_FIELD.fullmatch(self.field):
            raise ValueError("fill takes a plain form field name, never a selector")
        if self.role is not None and not _ROLE.fullmatch(self.role):
            raise ValueError("click takes an ARIA role name such as `button`")
        if self.name is not None and not 1 <= len(self.name) <= 80:
            raise ValueError("click takes an accessible name of 1-80 characters")
        return self


_CRON_FIELD = re.compile(r"^[0-9*/,-]+$")


class TaskSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: int = Field(ge=1)
    scope_ref: str
    schedule: str
    row_scope: str = Field(min_length=1, max_length=200)
    natural_key: list[FieldName] = Field(min_length=1)
    expected_schema: dict[FieldName, FieldType] = Field(min_length=1)
    navigate: list[NavStep]
    targets: list[FieldTarget] = Field(min_length=1)

    @field_validator("scope_ref")
    @classmethod
    def _a_file_under_scopes(cls, value: str) -> str:
        parts = PurePosixPath(value).parts
        if (
            "\\" in value
            or len(parts) != 2
            or parts[0] != "scopes"
            or not parts[1].endswith(".yaml")
        ):
            raise ValueError("scope_ref is scopes/<target>.yaml")
        return value

    @field_validator("schedule")
    @classmethod
    def _five_field_cron(cls, value: str) -> str:
        fields = value.split()
        if len(fields) != 5 or not all(_CRON_FIELD.fullmatch(f) for f in fields):
            raise ValueError("schedule is a five-field cron expression, evaluated in UTC (M20)")
        return value

    @model_validator(mode="after")
    def _keys_and_names_agree(self) -> Self:
        names = [t.name for t in self.targets]
        dupes = sorted({n for n in names if names.count(n) > 1})
        if dupes:
            raise ValueError(f"duplicate target names: {dupes}")
        outside = sorted(set(self.natural_key) - set(self.expected_schema))
        if outside:
            raise ValueError(f"natural_key {outside} is not in expected_schema (M5)")
        unextracted = sorted(set(self.natural_key) - set(names))
        if unextracted:
            raise ValueError(
                f"natural_key {unextracted} names no target: the lake cannot "
                "deduplicate on a column nobody extracts (M5)"
            )
        return self


def matches(target: FieldTarget, value: str) -> bool:
    """Rung 1's format check (Task 17, M23): every declared format pattern matches.

    A value longer than MAX_MATCH_LEN, or one whose match times out, never matches. A field with
    no format assertion has nothing to contradict, so it matches; rung 1 then relies on its
    anchor checks (Task 17).
    """
    text = value.strip()
    return all(
        match_value(a.pattern, text) == "match"
        for a in target.assertions
        if a.type == "format" and a.pattern is not None
    )
