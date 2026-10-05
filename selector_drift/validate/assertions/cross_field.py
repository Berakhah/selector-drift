"""Row footing, evaluated by a whitelist walker. No eval, no exec, no compile (R13/H5)."""

from __future__ import annotations

import ast
from collections.abc import Callable, Mapping
from decimal import Decimal
from typing import TYPE_CHECKING, Final

from selector_drift.extract.models import CandidateExtraction
from selector_drift.spec.schema import SpecError
from selector_drift.validate.assertions._numbers import parse_number
from selector_drift.validate.history import History
from selector_drift.validate.verdict import Outcome, Verdict

if TYPE_CHECKING:
    from selector_drift.validate.gate import HealContext

MAX_EXPRESSION: Final = 500
MAX_DEPTH: Final = 40  # deeper than any real footing rule; bounds the recursive walker
type Value = Decimal | bool

_ARITH: Final[dict[type[ast.operator], Callable[[Decimal, Decimal], Decimal]]] = {
    ast.Add: lambda a, b: a + b,
    ast.Sub: lambda a, b: a - b,
    ast.Mult: lambda a, b: a * b,
    ast.Div: lambda a, b: a / b,
}
_COMPARE: Final[dict[type[ast.cmpop], Callable[[Decimal, Decimal], bool]]] = {
    ast.Eq: lambda a, b: a == b,
    ast.NotEq: lambda a, b: a != b,
    ast.Lt: lambda a, b: a < b,
    ast.LtE: lambda a, b: a <= b,
    ast.Gt: lambda a, b: a > b,
    ast.GtE: lambda a, b: a >= b,
}
_ALLOWED: Final = (
    ast.Expression,
    ast.BoolOp,
    ast.And,
    ast.Or,
    ast.BinOp,
    ast.UnaryOp,
    ast.USub,
    ast.UAdd,
    ast.Not,
    ast.Compare,
    ast.Name,
    ast.Load,
    ast.Constant,
    *_ARITH,
    *_COMPARE,
)


class UnsafeExpression(SpecError):
    """A spec expression that is not pure arithmetic-and-comparison over row fields."""


class UnknownName(UnsafeExpression, LookupError):
    """The expression names a field the row does not have."""


def _depth(node: ast.AST) -> int:
    """Tree depth, iteratively: the check that guards recursion cannot itself recurse."""
    deepest = 0
    stack: list[tuple[ast.AST, int]] = [(node, 1)]
    while stack:
        current, level = stack.pop()
        deepest = max(deepest, level)
        stack.extend((child, level + 1) for child in ast.iter_child_nodes(current))
    return deepest


def _kind(node: ast.expr) -> str:
    """Static typing of the whitelist: 'num' or 'bool'. Mismatches are refused at construction,
    so no row ever reaches a short-circuit that skips a type error."""

    def need(child: ast.expr, kind: str) -> None:
        if _kind(child) != kind:
            raise UnsafeExpression(f"a {_kind(child)} was used where a {kind} is needed")

    match node:
        case ast.Constant() | ast.Name():
            return "num"
        case ast.UnaryOp(op=ast.Not(), operand=operand):
            need(operand, "bool")
            return "bool"
        case ast.UnaryOp(operand=operand):
            need(operand, "num")
            return "num"
        case ast.BinOp(left=left, right=right):
            need(left, "num")
            need(right, "num")
            return "num"
        case ast.BoolOp(values=values):
            for v in values:
                need(v, "bool")
            return "bool"
        case ast.Compare(left=left, comparators=comparators):
            need(left, "num")
            for c in comparators:
                need(c, "num")
            return "bool"
    raise UnsafeExpression(f"{type(node).__name__} is not allowed in a spec expression")


def parse_expression(expression: str) -> tuple[ast.Expression, frozenset[str]]:
    if len(expression) > MAX_EXPRESSION:
        raise UnsafeExpression(f"expression longer than {MAX_EXPRESSION} characters")
    try:
        tree = ast.parse(expression, mode="eval")  # parses; compiles and runs nothing
    except (SyntaxError, ValueError, RecursionError, MemoryError) as exc:
        raise UnsafeExpression(f"not an expression: {type(exc).__name__}") from exc
    if _depth(tree) > MAX_DEPTH:
        raise UnsafeExpression(f"expression nested deeper than {MAX_DEPTH} levels")
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED):
            raise UnsafeExpression(f"{type(node).__name__} is not allowed in a spec expression")
        if isinstance(node, ast.Constant) and (
            isinstance(node.value, bool) or not isinstance(node.value, int | float)
        ):
            raise UnsafeExpression(f"literal of type {type(node.value).__name__} is not a number")
        if isinstance(node, ast.Constant):
            # Exact from the source text: str(float) turns 1e999 into Infinity and 0.1 + 0.2 into
            # binary noise. The tree is never compiled, so a Decimal constant is safe to store.
            text = ast.get_source_segment(expression, node) or ""
            try:
                exact = Decimal(text)
            except ArithmeticError as exc:
                raise UnsafeExpression("a numeric literal is not a plain decimal number") from exc
            if not exact.is_finite():
                raise UnsafeExpression("a numeric literal is not finite")
            node.value = exact  # type: ignore[assignment]
        if isinstance(node, ast.Name):
            if node.id.startswith("_"):
                raise UnsafeExpression(f"name {node.id!r} is not a field")
            names.add(node.id)
    if not names:
        raise UnsafeExpression("a cross-field check must name at least one field")
    if _kind(tree.body) != "bool":
        raise UnsafeExpression("a cross-field check must be a comparison, e.g. net + tax == gross")
    return tree, frozenset(names)


def _num(v: Value) -> Decimal:
    if isinstance(v, bool):
        raise UnsafeExpression("a comparison result was used as a number")
    return v


def _truth(v: Value) -> bool:
    if not isinstance(v, bool):
        raise UnsafeExpression("a number was used as a condition; compare it first")
    return v


def _eval(node: ast.expr, row: Mapping[str, Decimal]) -> Value:
    match node:
        case ast.Constant(value=value):
            return Decimal(str(value))
        case ast.Name(id=name):
            return row[name]
        case ast.UnaryOp(op=ast.USub(), operand=operand):
            return -_num(_eval(operand, row))
        case ast.UnaryOp(op=ast.UAdd(), operand=operand):
            return _num(_eval(operand, row))
        case ast.UnaryOp(op=ast.Not(), operand=operand):
            return not _truth(_eval(operand, row))
        case ast.BinOp(left=left, op=op, right=right):
            return _ARITH[type(op)](_num(_eval(left, row)), _num(_eval(right, row)))
        case ast.BoolOp(op=ast.And(), values=values):
            return all(_truth(_eval(v, row)) for v in values)
        case ast.BoolOp(op=ast.Or(), values=values):
            return any(_truth(_eval(v, row)) for v in values)
        case ast.Compare(left=left, ops=ops, comparators=comparators):
            current = _num(_eval(left, row))
            for cmp_op, right in zip(ops, comparators, strict=True):
                nxt = _num(_eval(right, row))
                if not _COMPARE[type(cmp_op)](current, nxt):
                    return False
                current = nxt
            return True
    raise UnsafeExpression(f"{type(node).__name__} is not allowed in a spec expression")


def safe_eval(expression: str, row: Mapping[str, Decimal]) -> Value:
    tree, names = parse_expression(expression)
    missing = names - row.keys()
    if missing:
        raise UnknownName(f"row has no {', '.join(sorted(missing))}")
    return _eval(tree.body, row)


class CrossFieldAssertion:
    name = "CrossFieldAssertion"

    def __init__(self, expression: str) -> None:
        self._tree, self.names = parse_expression(expression)  # hostile spec fails to load
        self.expression = expression

    def _problem(self, row: Mapping[str, object]) -> str | None:
        values: dict[str, Decimal] = {}
        for name in sorted(self.names):
            claim = row.get(name)
            if claim is None:
                return f"the row has no {name}"
            parsed = parse_number(str(getattr(claim, "value", "")))
            if parsed is None:
                return f"{name} is not a number"
            values[name] = parsed
        try:
            holds = _eval(self._tree.body, values)
        except (ArithmeticError, UnsafeExpression) as exc:
            return f"the expression could not be evaluated: {type(exc).__name__}"
        if holds is not True:
            return ", ".join(f"{k}={v}" for k, v in values.items())
        return None

    def check(
        self, candidate: CandidateExtraction, history: History | None, ctx: HealContext
    ) -> Verdict:
        if not candidate.rows:
            return Verdict(
                check=self.name,
                outcome=Outcome.INSUFFICIENT,
                threshold=self.expression,
                observed=None,
                reason="no rows were extracted",
            )
        problems = [
            (i, p) for i, row in enumerate(candidate.rows) if (p := self._problem(row)) is not None
        ]
        if problems:
            i, first = problems[0]
            return Verdict(
                check=self.name,
                outcome=Outcome.FAIL,
                threshold=self.expression,
                observed=f"{len(problems)} of {len(candidate.rows)} rows",
                reason=f"{len(problems)} row(s) do not foot: {self.expression} is "
                f"false; first at row {i}: {first}",
            )
        return Verdict(
            check=self.name,
            outcome=Outcome.PASS,
            threshold=self.expression,
            observed=f"{len(candidate.rows)} of {len(candidate.rows)} rows",
            reason=f"every row foots: {self.expression} holds",
        )
