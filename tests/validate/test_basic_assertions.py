import datetime as dt
import time
from decimal import Decimal

import pytest

from selector_drift.spec.schema import SpecError
from selector_drift.validate.assertions import format as format_mod
from selector_drift.validate.assertions._numbers import parse_number
from selector_drift.validate.assertions.cross_field import (
    CrossFieldAssertion,
    UnsafeExpression,
    safe_eval,
)
from selector_drift.validate.assertions.domain import DomainAssertion
from selector_drift.validate.assertions.format import FormatAssertion
from selector_drift.validate.verdict import Outcome
from tests.validate.helpers import AMOUNT, CLAIM_NUMBER, candidate, column, ctx, field_target

CLAIM = r"^[A-Z]{2}\d{8}$"
FILED = field_target("filed")
STATUS = field_target("status")


# ---- format -------------------------------------------------------------------------------


@pytest.mark.unit
def test_format_assertion_reports_its_threshold_in_the_verdict():
    v = FormatAssertion(CLAIM).check(column("claim_number", ["AB12345678"]), None, ctx())
    assert v.outcome is Outcome.PASS and v.threshold == CLAIM


@pytest.mark.unit
def test_format_assertion_fails_and_names_the_offending_value():
    v = FormatAssertion(CLAIM).check(
        column("claim_number", ["AB12345678", "12345678"]), None, ctx()
    )
    assert v.outcome is Outcome.FAIL and "12345678" in v.observed and "1 of 2" in v.reason


@pytest.mark.unit
def test_format_assertion_bounds_its_input():
    """A crafted page value against a backtracking pattern hangs the run."""
    v = FormatAssertion(r"^(a+)+$").check(column("claim_number", ["a" * 10_000 + "b"]), None, ctx())
    assert v.outcome is Outcome.FAIL and "length" in v.reason.lower()
    assert v.observed == "length 10001"  # never the page text itself


@pytest.mark.unit
def test_a_timed_out_match_is_a_failure_that_says_so(monkeypatch):
    # Patched where it is USED (M31's lesson): format.py binds match_value at import.
    monkeypatch.setattr(format_mod, "match_value", lambda pattern, value: "timeout")
    v = FormatAssertion(CLAIM).check(column("claim_number", ["AB12345678"]), None, ctx())
    assert v.outcome is Outcome.FAIL and "timed out" in v.reason


@pytest.mark.unit
def test_a_backtracking_pattern_cannot_hang_the_run():
    # Under the cap, so only the regex timeout stands between this and a hung 04:12 run.
    started = time.monotonic()
    v = FormatAssertion(r"^(a+)+$").check(
        column("claim_number", ["a" * 200 + "b"] * 3), None, ctx()
    )
    assert v.outcome is Outcome.FAIL and time.monotonic() - started < 2.0


@pytest.mark.unit
def test_a_broken_pattern_is_refused_when_the_spec_loads():
    with pytest.raises(SpecError):
        FormatAssertion("[unclosed")


@pytest.mark.unit
def test_no_values_is_insufficient_never_pass():
    v = FormatAssertion(CLAIM).check(column("claim_number", []), None, ctx())
    assert v.outcome is Outcome.INSUFFICIENT


@pytest.mark.unit
def test_format_skips_rows_lacking_the_field_and_still_judges_the_rest():
    c = candidate([{"other": "x"}, {"claim_number": "AB12345678"}, {"claim_number": "bad"}])
    v = FormatAssertion(CLAIM).check(c, None, ctx(CLAIM_NUMBER))
    assert v.outcome is Outcome.FAIL and "1 of 2" in v.reason


@pytest.mark.unit
def test_format_verdict_never_copies_a_long_cell_into_its_reason():
    v = FormatAssertion(CLAIM).check(column("claim_number", ["Z" * 200]), None, ctx())
    assert v.outcome is Outcome.FAIL and "Z" * 50 not in v.reason + (v.observed or "")


# ---- domain -------------------------------------------------------------------------------


@pytest.mark.unit
def test_domain_assertion_catches_an_out_of_range_amount():
    v = DomainAssertion({"min": 100.00, "max": 250_000.00}).check(
        column("amount", ["1,200.00", "2,000,000.00"]), None, ctx(AMOUNT)
    )
    assert v.outcome is Outcome.FAIL and "2,000,000.00" in v.observed


@pytest.mark.unit
def test_domain_assertion_reads_thousands_separators():
    v = DomainAssertion({"min": 100.00, "max": 250_000.00}).check(
        column("amount", ["1,200.00", "90,500.50"]), None, ctx(AMOUNT)
    )
    assert v.outcome is Outcome.PASS and v.threshold == "100.0 <= x <= 250000.0"


@pytest.mark.unit
def test_a_locale_flipped_amount_is_not_a_number():
    # Task 11A: LocaleFlip must be refused by DomainAssertion (Task 26's table names it).
    v = DomainAssertion({"min": 100.00, "max": 250_000.00}).check(
        column("amount", ["1.234,56"]), None, ctx(AMOUNT)
    )
    assert v.outcome is Outcome.FAIL and "not a number" in v.reason


@pytest.mark.unit
def test_domain_assertion_catches_an_implausible_date():
    v = DomainAssertion({"date_after": "2000-01-01"}).check(
        column("filed", ["1899-01-01"]), None, ctx(FILED)
    )
    assert v.outcome is Outcome.FAIL


@pytest.mark.unit
def test_a_date_after_the_page_was_observed_is_refused_without_a_clock():
    v = DomainAssertion({"date_after": "2023-01-01"}).check(  # ISO string, as Task 5 loads it
        column("filed", ["2026-10-02"]), None, ctx(FILED)
    )  # observed 2026-10-01
    assert v.outcome is Outcome.FAIL and "observed" in v.reason


@pytest.mark.unit
def test_domain_assertion_checks_enum_membership():
    rule = {"enum": ["Open", "Pending", "Paid", "Closed"]}
    ok = DomainAssertion(rule).check(column("status", ["Open", "Paid"]), None, ctx(STATUS))
    assert ok.outcome is Outcome.PASS
    bad = DomainAssertion(rule).check(column("status", ["Reopened"]), None, ctx(STATUS))
    assert bad.outcome is Outcome.FAIL


@pytest.mark.unit
def test_currency_sanity_refuses_more_decimals_than_the_currency_has():
    rule = {"currency": "USD"}
    ok = DomainAssertion(rule).check(column("amount", ["1,200.50"]), None, ctx(AMOUNT))
    assert ok.outcome is Outcome.PASS
    bad = DomainAssertion(rule).check(column("amount", ["1,200.505"]), None, ctx(AMOUNT))
    assert bad.outcome is Outcome.FAIL


@pytest.mark.unit
@pytest.mark.parametrize(
    "rule",
    [
        {},
        {"minimum": 1},
        {"min": 10, "max": 1},
        {"currency": "XXX"},
        {"enum": "Open"},
        {"min": True},
        {"date_after": dt.date(2023, 1, 1)},
    ],
)
def test_a_malformed_domain_rule_is_refused_when_the_spec_loads(rule):
    with pytest.raises(SpecError):
        DomainAssertion(rule)


@pytest.mark.unit
def test_domain_verdict_never_copies_a_long_cell_into_its_reason():
    v = DomainAssertion({"min": 1}).check(column("amount", ["9" * 300]), None, ctx(AMOUNT))
    assert v.outcome is Outcome.FAIL and "9" * 50 not in v.reason + (v.observed or "")


@pytest.mark.unit
def test_domain_assertion_with_no_values_is_insufficient():
    v = DomainAssertion({"min": 1}).check(column("amount", []), None, ctx(AMOUNT))
    assert v.outcome is Outcome.INSUFFICIENT


@pytest.mark.unit
@pytest.mark.parametrize("hostile", ["NaN", "Infinity", "1e5", "٣٤", "", "--1", "1,2,3"])
def test_domain_rejects_hostile_numeric_text_without_raising(hostile):
    v = DomainAssertion({"min": 0}).check(column("amount", [hostile]), None, ctx(AMOUNT))
    assert v.outcome is Outcome.FAIL


@pytest.mark.unit
def test_parse_number_is_strict():
    assert parse_number("1,234.56") == Decimal("1234.56")
    assert parse_number(" -12 ") == Decimal("-12")
    assert parse_number("1.234,56") is None
    assert parse_number("1" * 65) is None


# ---- cross-field --------------------------------------------------------------------------


@pytest.mark.unit
def test_cross_field_catches_a_row_that_no_longer_foots():
    v = CrossFieldAssertion("net + tax == gross").check(
        candidate([{"net": "100", "tax": "20", "gross": "200"}]), None, ctx()
    )
    assert v.outcome is Outcome.FAIL and "foot" in v.reason.lower()


@pytest.mark.unit
def test_cross_field_passes_on_a_row_that_foots():
    v = CrossFieldAssertion("net + tax == gross").check(
        candidate([{"net": "1,000.10", "tax": "20.05", "gross": "1,020.15"}]), None, ctx()
    )
    assert v.outcome is Outcome.PASS


@pytest.mark.unit
def test_a_row_missing_a_named_field_is_a_failure_that_names_it():
    v = CrossFieldAssertion("net + tax == gross").check(
        candidate([{"net": "100", "gross": "120"}]), None, ctx()
    )
    assert v.outcome is Outcome.FAIL and "tax" in v.reason


@pytest.mark.unit
def test_cross_field_with_no_rows_is_insufficient_and_exposes_names():
    a = CrossFieldAssertion("net + tax == gross")
    assert a.names == frozenset({"net", "tax", "gross"})
    assert a.check(candidate([]), None, ctx()).outcome is Outcome.INSUFFICIENT


@pytest.mark.unit
def test_cross_field_division_by_zero_and_non_numbers_fail_without_raising():
    a = CrossFieldAssertion("net / tax == 1")
    z = a.check(candidate([{"net": "1", "tax": "0"}]), None, ctx())
    assert z.outcome is Outcome.FAIL
    n = a.check(candidate([{"net": "1", "tax": "abc"}]), None, ctx())
    assert n.outcome is Outcome.FAIL and "abc" not in n.reason


@pytest.mark.unit
@pytest.mark.parametrize(
    "hostile",
    [
        "__import__('os').system('id')",
        "().__class__.__bases__[0].__subclasses__()",
        "open('/etc/passwd').read()",
        "net ** 9 ** 9 ** 9 == 1",
        "_secret + 1 == 2",
        "(lambda: 1)() == 1",
        "net == " + "1 + " * 200 + "1",
        "net.real == 1",
        "net[0] == 1",
        "[x for x in net] == 1",
        "{net: 1} == 1",
        "net == 'a'",
        "net == True",
        "net if tax else gross == 1",
        "net := 1",
        "net == 1; import os",
        "net // tax == 1",
        "net % tax == 1",
        "net in tax",
        "net is tax",
        "net == " + "-" * 100 + "1",
        "not " * 100 + "net == 1",
        "f'{net}' == 1",
        "net == 1j",
        "net == ...",
        "await net",
        "net and",
        "",
    ],
)
def test_a_spec_expression_cannot_reach_the_interpreter_adversarial(hostile):
    started = time.monotonic()
    with pytest.raises(UnsafeExpression):
        CrossFieldAssertion(hostile)
    with pytest.raises(UnsafeExpression):
        safe_eval(hostile, {"net": Decimal(1), "tax": Decimal(1), "gross": Decimal(2)})
    assert time.monotonic() - started < 1.0


@pytest.mark.unit
def test_an_expression_that_computes_a_number_is_not_a_check():
    with pytest.raises(UnsafeExpression, match="comparison"):
        CrossFieldAssertion("net + tax")


@pytest.mark.unit
def test_safe_eval_is_exact_decimal_arithmetic():
    row = {"net": Decimal("0.1"), "tax": Decimal("0.2"), "gross": Decimal("0.3")}
    assert safe_eval("net + tax == gross", row) is True  # float would say False
    assert safe_eval("net * 2 < gross and not gross < 0", row) is True


@pytest.mark.unit
def test_safe_eval_names_not_in_the_row_are_a_lookup_error_and_unsafe():
    with pytest.raises(LookupError, match="tax"):
        safe_eval("net + tax == 1", {"net": Decimal(1)})
    with pytest.raises(UnsafeExpression):
        safe_eval("net + tax == 1", {"net": Decimal(1)})


@pytest.mark.unit
def test_safe_eval_refuses_numbers_as_conditions_and_booleans_as_numbers():
    row = {"net": Decimal(1), "tax": Decimal(1)}
    with pytest.raises(UnsafeExpression):
        safe_eval("net and tax", row)
    with pytest.raises(UnsafeExpression):
        safe_eval("(net < tax) + 1 == 2", row)


@pytest.mark.unit
def test_safe_eval_bounds_huge_arithmetic():
    big = {"net": Decimal("9" * 60)}
    expr = "net * net * net * net * net * net * net * net * net * net * net * net == 1"
    started = time.monotonic()
    assert safe_eval(expr, big) is False
    assert time.monotonic() - started < 1.0
    with pytest.raises(ArithmeticError):
        safe_eval("1e999 - 1e999 == 0", {})  # inf - inf: the gate maps this to a FAIL row


@pytest.mark.unit
def test_deeply_parenthesised_expressions_are_bounded_not_hung():
    started = time.monotonic()
    ok = "(" * 100 + "net" + ")" * 100 + " == 1"  # parens add no AST depth
    assert safe_eval(ok, {"net": Decimal(1)}) is True
    for n in (240, 300):  # the parser's own nesting limit, then the length cap
        with pytest.raises(UnsafeExpression):
            safe_eval("(" * n + "net" + ")" * n + " == 1", {"net": Decimal(1)})
    assert time.monotonic() - started < 1.0
