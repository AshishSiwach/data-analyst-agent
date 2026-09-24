"""S24 acceptance tests for agent.orchestrator.answer_question (mocked
S20/S22/S23 calls - one integration test per ARCHITECTURE.md termination
condition, plus the `declined` path this codebase added beyond the
diagram)."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import patch

from data_analyst_agent.agent.orchestrator import answer_question
from data_analyst_agent.models.entities import (
    ColumnSpec,
    FailureDiagnosis,
    NarrativeWrap,
    SessionState,
    SqlAttempt,
    SqlExecutionResult,
    SqlRetryOutcome,
)

NOW = datetime(2026, 9, 24, 12, 0, 0, tzinfo=timezone.utc)


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


def _attempt(**overrides) -> SqlAttempt:
    base = dict(
        turn_id="turn-1",
        attempt_number=1,
        query_text="SELECT COUNT(*) FROM v_orders",
        status="success",
        error_message=None,
        execution_ms=10,
        row_count=1,
        truncated=False,
    )
    base.update(overrides)
    return SqlAttempt(**base)


def _success_result() -> SqlExecutionResult:
    return SqlExecutionResult(
        status="success",
        columns=[ColumnSpec(name="order_count", type="BIGINT")],
        rows=[[52612]],
        row_count=1,
        truncated=False,
        execution_ms=10,
    )


_DIAGNOSIS = FailureDiagnosis(
    category="schema_mismatch",
    explanation="Email open rate isn't tracked anywhere in this dataset.",
    rephrase_suggestion="Try asking about order or revenue metrics instead.",
)

_NARRATIVE = NarrativeWrap(answer_text="You have 52,612 orders.", assumption_disclosed=None)


@patch("data_analyst_agent.agent.orchestrator.log_attempt")
@patch("data_analyst_agent.agent.orchestrator.log_failure")
@patch("data_analyst_agent.agent.orchestrator.wrap")
@patch("data_analyst_agent.agent.orchestrator.diagnose")
@patch("data_analyst_agent.agent.orchestrator.run_turn_sql")
def test_success_termination_returns_populated_answer_and_logs_every_attempt(
    mock_run_turn_sql, mock_diagnose, mock_wrap, mock_log_failure, mock_log_attempt
):
    attempts = [_attempt(attempt_number=1, status="error"), _attempt(attempt_number=2)]
    mock_run_turn_sql.return_value = SqlRetryOutcome(
        status="success", result=_success_result(), attempts=attempts
    )
    mock_wrap.return_value = _NARRATIVE

    session = _session()
    answer = answer_question("How many orders are there?", session)

    assert answer.status == "success"
    assert answer.answer_text == "You have 52,612 orders."
    assert answer.assumption_disclosed is None
    assert answer.chart_spec is not None
    assert answer.chart_spec.chart_type == "scalar"
    assert answer.sql_shown == attempts[-1].query_text
    assert answer.diagnosis is None

    assert mock_log_attempt.call_count == len(attempts)
    mock_log_failure.assert_not_called()
    mock_diagnose.assert_not_called()
    assert session.turn_ids == [answer.turn_id]


@patch("data_analyst_agent.agent.orchestrator.log_attempt")
@patch("data_analyst_agent.agent.orchestrator.log_failure")
@patch("data_analyst_agent.agent.orchestrator.wrap")
@patch("data_analyst_agent.agent.orchestrator.diagnose")
@patch("data_analyst_agent.agent.orchestrator.run_turn_sql")
def test_exhausted_termination_returns_graceful_failure_and_logs_failure_entry(
    mock_run_turn_sql, mock_diagnose, mock_wrap, mock_log_failure, mock_log_attempt
):
    attempts = [_attempt(attempt_number=n, status="error") for n in (1, 2, 3)]
    mock_run_turn_sql.return_value = SqlRetryOutcome(
        status="exhausted",
        result=SqlExecutionResult(
            status="error", error_message="boom", truncated=False, execution_ms=5
        ),
        attempts=attempts,
    )
    mock_diagnose.return_value = _DIAGNOSIS

    session = _session()
    answer = answer_question("Why did sales drop?", session)

    assert answer.status == "graceful_failure"
    assert answer.diagnosis == _DIAGNOSIS
    assert answer.answer_text is None
    assert answer.chart_spec is None

    assert mock_log_attempt.call_count == 3
    mock_log_failure.assert_called_once()
    logged_entry = mock_log_failure.call_args.args[0]
    assert logged_entry.fast_fail_triggered is False
    assert logged_entry.attempts == attempts
    assert logged_entry.diagnosis == _DIAGNOSIS
    mock_wrap.assert_not_called()


@patch("data_analyst_agent.agent.orchestrator.log_attempt")
@patch("data_analyst_agent.agent.orchestrator.log_failure")
@patch("data_analyst_agent.agent.orchestrator.diagnose")
@patch("data_analyst_agent.agent.orchestrator.run_turn_sql")
def test_fast_fail_termination_sets_the_flag_and_logs_zero_attempts(
    mock_run_turn_sql, mock_diagnose, mock_log_failure, mock_log_attempt
):
    mock_run_turn_sql.return_value = SqlRetryOutcome(status="fast_fail", attempts=[])
    mock_diagnose.return_value = _DIAGNOSIS

    session = _session()
    answer = answer_question("Why did sales drop?", session)

    assert answer.status == "graceful_failure"
    mock_log_attempt.assert_not_called()
    logged_entry = mock_log_failure.call_args.args[0]
    assert logged_entry.fast_fail_triggered is True
    assert logged_entry.attempts == []


@patch("data_analyst_agent.agent.orchestrator.log_attempt")
@patch("data_analyst_agent.agent.orchestrator.log_failure")
@patch("data_analyst_agent.agent.orchestrator.diagnose")
@patch("data_analyst_agent.agent.orchestrator.run_turn_sql")
def test_declined_termination_is_treated_as_graceful_failure(
    mock_run_turn_sql, mock_diagnose, mock_log_failure, mock_log_attempt
):
    declined_attempt = _attempt(
        query_text="", status="rejected", error_message="Sub-national regions aren't tracked."
    )
    mock_run_turn_sql.return_value = SqlRetryOutcome(status="declined", attempts=[declined_attempt])
    mock_diagnose.return_value = _DIAGNOSIS

    session = _session()
    answer = answer_question("How many orders came from Scotland?", session)

    assert answer.status == "graceful_failure"
    assert answer.diagnosis == _DIAGNOSIS
    mock_log_attempt.assert_called_once()
    logged_entry = mock_log_failure.call_args.args[0]
    assert logged_entry.fast_fail_triggered is False
    assert logged_entry.attempts == [declined_attempt]


@patch("data_analyst_agent.agent.orchestrator.log_attempt")
@patch("data_analyst_agent.agent.orchestrator.log_failure")
@patch("data_analyst_agent.agent.orchestrator.diagnose")
@patch("data_analyst_agent.agent.orchestrator.run_turn_sql")
def test_budget_stop_from_the_retry_loop_carries_no_optional_fields(
    mock_run_turn_sql, mock_diagnose, mock_log_failure, mock_log_attempt
):
    attempts = [_attempt(attempt_number=1, status="error")]
    mock_run_turn_sql.return_value = SqlRetryOutcome(status="budget_stop", attempts=attempts)

    session = _session()
    answer = answer_question("How many orders are there?", session)

    assert answer.status == "budget_stop"
    assert answer.answer_text is None
    assert answer.diagnosis is None
    assert answer.chart_spec is None
    assert mock_log_attempt.call_count == 1
    mock_log_failure.assert_not_called()
    mock_diagnose.assert_not_called()


@patch("data_analyst_agent.agent.orchestrator.log_attempt")
@patch("data_analyst_agent.agent.orchestrator.log_failure")
@patch("data_analyst_agent.agent.orchestrator.wrap")
@patch("data_analyst_agent.agent.orchestrator.run_turn_sql")
def test_budget_stop_after_a_successful_query_but_over_cap_during_wrap(
    mock_run_turn_sql, mock_wrap, mock_log_failure, mock_log_attempt
):
    attempts = [_attempt(attempt_number=1)]
    mock_run_turn_sql.return_value = SqlRetryOutcome(
        status="success", result=_success_result(), attempts=attempts
    )

    def wrap_side_effect(question, result, chart_spec, client=None):
        session.cost_spent_usd = Decimal("0.60")
        return _NARRATIVE

    session = _session()
    mock_wrap.side_effect = wrap_side_effect

    answer = answer_question("How many orders are there?", session)

    assert answer.status == "budget_stop"
    assert answer.answer_text is None
    assert answer.chart_spec is None
    mock_log_failure.assert_not_called()


@patch("data_analyst_agent.agent.orchestrator.log_attempt")
@patch("data_analyst_agent.agent.orchestrator.wrap")
@patch("data_analyst_agent.agent.orchestrator.run_turn_sql")
def test_query_audit_log_entry_count_matches_attempts_using_real_writer(
    mock_run_turn_sql, mock_wrap, mock_log_attempt, tmp_path
):
    # Uses the real log_attempt writer (not mocked) against a tmp file to
    # verify the "audit log gains exactly the expected number of entries
    # per call" acceptance criterion end to end, not just via a mock's
    # call_count.
    from data_analyst_agent.agent.audit_log import log_attempt as real_log_attempt

    mock_log_attempt.side_effect = real_log_attempt
    attempts = [_attempt(attempt_number=1, status="error"), _attempt(attempt_number=2)]
    mock_run_turn_sql.return_value = SqlRetryOutcome(
        status="success", result=_success_result(), attempts=attempts
    )
    mock_wrap.return_value = _NARRATIVE

    path = tmp_path / "query_audit.jsonl"
    session = _session()
    answer_question("How many orders are there?", session, query_audit_log_path=path)

    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == len(attempts)
