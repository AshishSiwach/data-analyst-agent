"""S03 acceptance tests for eval.comparator.compare()."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from data_analyst_agent.eval.comparator import RELATIVE_TOLERANCE, compare
from data_analyst_agent.models.entities import ColumnSpec, ResultData


def _result(columns: list[str], rows: list[list]) -> ResultData:
    return ResultData(columns=[ColumnSpec(name=c, type="ANY") for c in columns], rows=rows)


def test_identical_results_match():
    a = _result(["product", "revenue"], [["Mug", 100.0], ["Plate", 50.0]])
    g = _result(["product", "revenue"], [["Mug", 100.0], ["Plate", 50.0]])
    assert compare(a, g) is True


def test_reordered_rows_still_match():
    a = _result(["product", "revenue"], [["Plate", 50.0], ["Mug", 100.0]])
    g = _result(["product", "revenue"], [["Mug", 100.0], ["Plate", 50.0]])
    assert compare(a, g) is True


def test_float_within_tolerance_matches():
    # 100.05 vs 100.0 is a 0.05% relative diff, inside the 0.1% tolerance.
    a = _result(["revenue"], [[100.05]])
    g = _result(["revenue"], [[100.0]])
    assert compare(a, g) is True


def test_float_outside_tolerance_fails():
    # 100.2 vs 100.0 is a 0.2% relative diff, outside the 0.1% tolerance.
    a = _result(["revenue"], [[100.2]])
    g = _result(["revenue"], [[100.0]])
    assert compare(a, g) is False


def test_differently_named_but_same_valued_columns_match():
    a = _result(["SUM(price)"], [[100.0]])
    g = _result(["total_revenue"], [[100.0]])
    assert compare(a, g) is True


def test_extra_row_fails():
    a = _result(["product"], [["Mug"], ["Plate"], ["Bowl"]])
    g = _result(["product"], [["Mug"], ["Plate"]])
    assert compare(a, g) is False


def test_missing_row_fails():
    a = _result(["product"], [["Mug"]])
    g = _result(["product"], [["Mug"], ["Plate"]])
    assert compare(a, g) is False


def test_two_empty_result_sets_match():
    a = _result(["product", "revenue"], [])
    g = _result(["product", "revenue"], [])
    assert compare(a, g) is True


def test_column_count_mismatch_fails():
    a = _result(["product"], [["Mug"]])
    g = _result(["product", "revenue"], [["Mug", 100.0]])
    assert compare(a, g) is False


def test_value_mismatch_fails():
    a = _result(["product"], [["Mug"]])
    g = _result(["product"], [["Plate"]])
    assert compare(a, g) is False


def test_decimal_and_float_compare_equal_within_tolerance():
    a = _result(["revenue"], [[Decimal("100.00")]])
    g = _result(["revenue"], [[100.0]])
    assert compare(a, g) is True


def test_none_matches_none_but_not_zero():
    a = _result(["returned_amount"], [[None]])
    g = _result(["returned_amount"], [[0]])
    assert compare(a, g) is False
    a2 = _result(["returned_amount"], [[None]])
    g2 = _result(["returned_amount"], [[None]])
    assert compare(a2, g2) is True


def test_bool_matches_equivalent_int():
    # A gold file authored with 1/0 for a boolean column should still match
    # an agent result of True/False, per Python's own True == 1 semantics.
    a = _result(["is_return"], [[True]])
    g = _result(["is_return"], [[1]])
    assert compare(a, g) is True


def test_bool_does_not_match_unequal_int():
    a = _result(["is_return"], [[True]])
    g = _result(["is_return"], [[0]])
    assert compare(a, g) is False


def test_tolerance_constant_is_point_one_percent():
    assert RELATIVE_TOLERANCE == 0.001


def test_live_date_object_matches_json_round_tripped_iso_string():
    # Regression test: found live during S26, via S25's harness report -
    # an agent result carries real datetime.date objects (straight from
    # DuckDB); a gold row loaded from gold.jsonl carries a JSON-
    # deserialized ISO string, since GoldQuestion.gold_result.rows is
    # typed list[list[Any]] and pydantic never coerces it back to a date.
    # date(2009, 12, 1) == "2009-12-01" is False in plain Python even
    # though they're the same value - this silently failed 3/20 correct
    # basic-bucket answers before the fix.
    a = _result(["earliest_order_date"], [[date(2009, 12, 1)]])
    g = _result(["earliest_order_date"], [["2009-12-01"]])
    assert compare(a, g) is True


def test_datetime_object_matches_date_only_iso_string():
    a = _result(["order_date"], [[datetime(2011, 12, 5, 14, 30, 0)]])
    g = _result(["order_date"], [["2011-12-05"]])
    assert compare(a, g) is True


def test_mismatched_dates_still_fail():
    a = _result(["earliest_order_date"], [[date(2009, 12, 1)]])
    g = _result(["earliest_order_date"], [["2009-12-02"]])
    assert compare(a, g) is False


def test_two_iso_date_strings_still_compare_via_plain_equality():
    # Both sides already strings (e.g. two gold-authored fixtures) - no
    # date coercion needed, plain string equality still works.
    a = _result(["date"], [["2011-12-05"]])
    g = _result(["date"], [["2011-12-05"]])
    assert compare(a, g) is True
