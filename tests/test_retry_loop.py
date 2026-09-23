"""S20 acceptance tests for agent.retry_loop.run_turn_sql (mocked S19/S15)."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import patch

from data_analyst_agent.agent.retry_loop import MAX_ATTEMPTS, run_turn_sql
from data_analyst_agent.agent.session import SessionState, normalize
from data_analyst_agent.models.entities import SqlExecutionResult

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


def _error_result(msg: str) -> SqlExecutionResult:
    return SqlExecutionResult(status="error", error_message=msg, truncated=False, execution_ms=5)


def _success_result() -> SqlExecutionResult:
    return SqlExecutionResult(
        status="success", columns=[], rows=[[1]], row_count=1, truncated=False, execution_ms=5
    )


@patch("data_analyst_agent.agent.retry_loop.run_sql")
@patch("data_analyst_agent.agent.retry_loop.generate_sql")
def test_fails_twice_then_succeeds_on_attempt_three(mock_gen, mock_run):
    mock_gen.return_value = "SELECT 1"
    mock_run.side_effect = [_error_result("e1"), _error_result("e2"), _success_result()]

    session = _session()
    outcome = run_turn_sql("How many orders?", session)

    assert outcome.status == "success"
    assert len(outcome.attempts) == 3
    assert [a.attempt_number for a in outcome.attempts] == [1, 2, 3]
    assert [a.status for a in outcome.attempts] == ["error", "error", "success"]
    assert mock_run.call_count == 3


@patch("data_analyst_agent.agent.retry_loop.run_sql")
@patch("data_analyst_agent.agent.retry_loop.generate_sql")
def test_fails_all_three_times_returns_exhausted_never_a_fourth_call(mock_gen, mock_run):
    mock_gen.return_value = "SELECT 1"
    mock_run.side_effect = [_error_result("e1"), _error_result("e2"), _error_result("e3")]

    session = _session()
    outcome = run_turn_sql("How many orders?", session)

    assert outcome.status == "exhausted"
    assert len(outcome.attempts) == MAX_ATTEMPTS == 3
    assert mock_run.call_count == 3
    assert mock_gen.call_count == 3


@patch("data_analyst_agent.agent.retry_loop.run_sql")
@patch("data_analyst_agent.agent.retry_loop.generate_sql")
def test_exhaustion_updates_failed_questions_cache_for_future_fast_fail(mock_gen, mock_run):
    mock_gen.return_value = "SELECT 1"
    mock_run.side_effect = [_error_result("e1"), _error_result("e2"), _error_result("e3")]

    session = _session()
    run_turn_sql("How many orders?", session)

    assert session.failed_questions_cache[normalize("How many orders?")] >= 3


@patch("data_analyst_agent.agent.retry_loop.run_sql")
@patch("data_analyst_agent.agent.retry_loop.generate_sql")
def test_fast_fail_short_circuits_to_zero_new_attempts(mock_gen, mock_run):
    session = _session(failed_questions_cache={normalize("How many orders?"): 3})

    outcome = run_turn_sql("How many orders?", session)

    assert outcome.status == "fast_fail"
    assert outcome.attempts == []
    assert outcome.result is None
    mock_gen.assert_not_called()
    mock_run.assert_not_called()


@patch("data_analyst_agent.agent.retry_loop.run_sql")
@patch("data_analyst_agent.agent.retry_loop.generate_sql")
def test_cost_cap_exceeded_after_attempt_one_halts_before_attempt_two(mock_gen, mock_run):
    session = _session(cost_spent_usd=Decimal("0.00"), cost_cap_usd=Decimal("0.50"))

    def generate_side_effect(question, prior_error, db_path=None, client=None):
        # Simulate attempt 1's generate_sql call being the one that
        # tips the session over budget (checked right after this call,
        # per Architecture.md's GenSQL -> CostCheck1 order).
        if mock_gen.call_count == 1:
            session.cost_spent_usd = Decimal("0.10")
        else:
            session.cost_spent_usd = Decimal("0.60")
        return "SELECT 1"

    mock_gen.side_effect = generate_side_effect
    mock_run.side_effect = [_error_result("e1")]

    outcome = run_turn_sql("How many orders?", session)

    assert outcome.status == "budget_stop"
    assert len(outcome.attempts) == 1  # attempt 1 completed
    assert mock_run.call_count == 1  # attempt 2's run_sql never happened
    assert mock_gen.call_count == 2  # attempt 2's generate_sql happened, then the check stopped it


@patch("data_analyst_agent.agent.retry_loop.run_sql")
@patch("data_analyst_agent.agent.retry_loop.generate_sql")
def test_prior_error_is_threaded_into_the_next_generate_sql_call(mock_gen, mock_run):
    mock_gen.return_value = "SELECT 1"
    mock_run.side_effect = [_error_result("boom"), _success_result()]

    session = _session()
    run_turn_sql("How many orders?", session)

    second_call_kwargs = mock_gen.call_args_list[1]
    assert second_call_kwargs.kwargs.get("prior_error") == "boom" or (
        len(second_call_kwargs.args) > 1 and second_call_kwargs.args[1] == "boom"
    )


@patch("data_analyst_agent.agent.retry_loop.run_sql")
@patch("data_analyst_agent.agent.retry_loop.generate_sql")
def test_turn_id_is_consistent_across_all_attempts(mock_gen, mock_run):
    mock_gen.return_value = "SELECT 1"
    mock_run.side_effect = [_error_result("e1"), _error_result("e2"), _error_result("e3")]

    session = _session()
    outcome = run_turn_sql("How many orders?", session, turn_id="turn-abc")

    assert all(a.turn_id == "turn-abc" for a in outcome.attempts)


def test_max_attempts_constant_is_three():
    assert MAX_ATTEMPTS == 3
