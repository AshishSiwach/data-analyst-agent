"""S04 acceptance tests for agent.session: check_cost_cap, normalize,
check_fast_fail."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from data_analyst_agent.agent.session import (
    SessionState,
    check_cost_cap,
    check_fast_fail,
    normalize,
)

NOW = datetime(2026, 9, 23, 12, 0, 0, tzinfo=timezone.utc)


def _session(**overrides) -> SessionState:
    base = dict(
        session_id="sess-1",
        started_at=NOW,
        cost_spent_usd=Decimal("0.00"),
        cost_cap_usd=Decimal("0.50"),
        turn_ids=[],
        failed_questions_cache={},
    )
    base.update(overrides)
    return SessionState(**base)


# --- check_cost_cap ---


def test_cost_cap_not_triggered_below_threshold():
    session = _session(cost_spent_usd=Decimal("0.49"), cost_cap_usd=Decimal("0.50"))
    assert check_cost_cap(session) is False


def test_cost_cap_triggers_exactly_at_threshold():
    session = _session(cost_spent_usd=Decimal("0.50"), cost_cap_usd=Decimal("0.50"))
    assert check_cost_cap(session) is True


def test_cost_cap_triggers_above_threshold():
    session = _session(cost_spent_usd=Decimal("0.51"), cost_cap_usd=Decimal("0.50"))
    assert check_cost_cap(session) is True


def test_cost_cap_default_is_fifty_cents():
    session = _session(cost_spent_usd=Decimal("0.50"))
    assert session.cost_cap_usd == Decimal("0.50")
    assert check_cost_cap(session) is True


# --- normalize ---


def test_normalize_lowercases():
    assert normalize("Top Products") == "top products"


def test_normalize_collapses_internal_whitespace():
    assert normalize("top    products\tby\nrevenue") == "top products by revenue"


def test_normalize_strips_leading_and_trailing_whitespace():
    assert normalize("  top products  ") == "top products"


def test_normalize_is_idempotent():
    once = normalize("  Top   Products ")
    assert normalize(once) == once


# --- check_fast_fail ---


def test_fast_fail_false_with_no_recorded_failures():
    session = _session(failed_questions_cache={})
    assert check_fast_fail(session, "top products") is False


def test_fast_fail_false_below_three_failures():
    session = _session(failed_questions_cache={"top products": 2})
    assert check_fast_fail(session, "top products") is False


def test_fast_fail_true_at_exactly_three_failures():
    session = _session(failed_questions_cache={"top products": 3})
    assert check_fast_fail(session, "top products") is True


def test_fast_fail_ignores_a_differently_worded_question():
    session = _session(failed_questions_cache={"top products": 3})
    assert check_fast_fail(session, "worst products") is False


def test_fast_fail_matches_across_case_and_whitespace_differences():
    session = _session(failed_questions_cache={"top products": 3})
    assert check_fast_fail(session, "  Top    Products ") is True
    assert check_fast_fail(session, "TOP PRODUCTS") is True
