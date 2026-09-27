"""S05 acceptance tests for agent.audit_log: log_attempt, log_failure.
Also covers the later monitoring-dashboard addition, log_llm_call."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal

from data_analyst_agent.agent.audit_log import (
    compute_cost_usd,
    log_attempt,
    log_failure,
    log_llm_call,
)
from data_analyst_agent.models.entities import (
    FailureDiagnosis,
    FailureLogEntry,
    LlmCallLog,
    QueryAuditLog,
    SqlAttempt,
)

NOW = datetime(2026, 9, 23, 12, 0, 0, tzinfo=timezone.utc)


def _attempt(**overrides) -> SqlAttempt:
    base = dict(
        turn_id="turn-1",
        attempt_number=1,
        query_text="SELECT 1",
        status="success",
        error_message=None,
        execution_ms=120,
        row_count=1,
        truncated=False,
    )
    base.update(overrides)
    return SqlAttempt(**base)


def _failure_entry(**overrides) -> FailureLogEntry:
    base = dict(
        turn_id="turn-1",
        session_id="sess-1",
        question_text="Why did sales drop?",
        attempts=[_attempt(status="error", error_message="unknown column")],
        diagnosis=FailureDiagnosis(
            category="ambiguity",
            explanation="The question implies causal reasoning v1 doesn't support.",
            rephrase_suggestion="Try: 'what was revenue in Q3 vs Q2?'",
        ),
        fast_fail_triggered=False,
        logged_at=NOW,
    )
    base.update(overrides)
    return FailureLogEntry(**base)


def test_log_attempt_appends_one_line_matching_the_model(tmp_path):
    path = tmp_path / "query_audit.jsonl"
    attempt = _attempt()
    log_attempt(attempt, session_id="sess-1", path=path)

    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    logged = QueryAuditLog.model_validate_json(lines[0])

    assert logged.turn_id == attempt.turn_id
    assert logged.session_id == "sess-1"
    assert logged.attempt_number == attempt.attempt_number
    assert logged.query_text == attempt.query_text
    assert logged.status == attempt.status
    assert logged.execution_ms == attempt.execution_ms
    assert logged.row_count == attempt.row_count
    assert logged.error_message == attempt.error_message
    assert isinstance(logged.logged_at, datetime)


def test_log_attempt_is_append_only_across_multiple_calls(tmp_path):
    path = tmp_path / "query_audit.jsonl"
    log_attempt(_attempt(attempt_number=1), session_id="sess-1", path=path)
    log_attempt(
        _attempt(attempt_number=2, status="error", error_message="boom"),
        session_id="sess-1",
        path=path,
    )

    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    first = QueryAuditLog.model_validate_json(lines[0])
    second = QueryAuditLog.model_validate_json(lines[1])
    assert first.attempt_number == 1
    assert second.attempt_number == 2
    assert second.status == "error"


def test_log_failure_writes_two_independently_parseable_lines(tmp_path):
    path = tmp_path / "failures.jsonl"
    log_failure(_failure_entry(turn_id="turn-1"), path=path)
    log_failure(_failure_entry(turn_id="turn-2"), path=path)

    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    first = FailureLogEntry.model_validate_json(lines[0])
    second = FailureLogEntry.model_validate_json(lines[1])
    assert first.turn_id == "turn-1"
    assert second.turn_id == "turn-2"


def test_writes_never_truncate_prior_lines(tmp_path):
    path = tmp_path / "failures.jsonl"
    for i in range(5):
        log_failure(_failure_entry(turn_id=f"turn-{i}"), path=path)

    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 5
    assert [json.loads(line)["turn_id"] for line in lines] == [f"turn-{i}" for i in range(5)]


def test_every_written_line_is_a_single_complete_valid_json_object_never_partial(tmp_path):
    # Each log_* call issues exactly one write() of a complete,
    # newline-terminated line, so re-reading never finds a truncated or
    # merged line - the practical, testable signature of atomic-per-call
    # writes in a single-process test.
    path = tmp_path / "query_audit.jsonl"
    for i in range(20):
        log_attempt(_attempt(attempt_number=(i % 3) + 1), session_id="sess-1", path=path)

    raw = path.read_text(encoding="utf-8")
    lines = raw.splitlines()
    assert len(lines) == 20
    assert raw.endswith("\n")
    for line in lines:
        parsed = json.loads(line)  # raises if any line is malformed/partial
        assert "turn_id" in parsed


def test_default_paths_are_the_fixed_repo_root_filenames():
    from data_analyst_agent.agent.audit_log import (
        FAILURE_LOG_PATH,
        LLM_CALL_LOG_PATH,
        QUERY_AUDIT_LOG_PATH,
    )

    assert str(QUERY_AUDIT_LOG_PATH) == "query_audit.jsonl"
    assert str(FAILURE_LOG_PATH) == "failures.jsonl"
    assert str(LLM_CALL_LOG_PATH) == "llm_calls.jsonl"


def test_compute_cost_usd_uses_gpt_4o_mini_input_and_output_rates():
    # 1M input tokens at $0.15/1M, 1M output tokens at $0.60/1M.
    assert compute_cost_usd(1_000_000, 0) == Decimal("0.15")
    assert compute_cost_usd(0, 1_000_000) == Decimal("0.60")
    assert compute_cost_usd(0, 0) == Decimal("0")


def test_log_llm_call_appends_one_line_matching_the_model(tmp_path):
    path = tmp_path / "llm_calls.jsonl"
    log_llm_call(
        call_type="generate_sql",
        model="gpt-4o-mini",
        prompt_tokens=1000,
        completion_tokens=200,
        latency_ms=850,
        turn_id="turn-1",
        session_id="sess-1",
        path=path,
    )

    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    logged = LlmCallLog.model_validate_json(lines[0])

    assert logged.call_type == "generate_sql"
    assert logged.model == "gpt-4o-mini"
    assert logged.turn_id == "turn-1"
    assert logged.session_id == "sess-1"
    assert logged.prompt_tokens == 1000
    assert logged.completion_tokens == 200
    assert logged.cost_usd == compute_cost_usd(1000, 200)
    assert logged.latency_ms == 850
    assert isinstance(logged.logged_at, datetime)


def test_log_llm_call_turn_and_session_id_are_optional(tmp_path):
    path = tmp_path / "llm_calls.jsonl"
    log_llm_call(
        call_type="narrative",
        model="gpt-4o-mini",
        prompt_tokens=500,
        completion_tokens=50,
        latency_ms=400,
        path=path,
    )

    logged = LlmCallLog.model_validate_json(path.read_text(encoding="utf-8").splitlines()[0])
    assert logged.turn_id is None
    assert logged.session_id is None
