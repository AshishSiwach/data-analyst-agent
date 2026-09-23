"""S05 acceptance tests for agent.audit_log: log_attempt, log_failure."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from data_analyst_agent.agent.audit_log import log_attempt, log_failure
from data_analyst_agent.models.entities import (
    FailureDiagnosis,
    FailureLogEntry,
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
    from data_analyst_agent.agent.audit_log import FAILURE_LOG_PATH, QUERY_AUDIT_LOG_PATH

    assert str(QUERY_AUDIT_LOG_PATH) == "query_audit.jsonl"
    assert str(FAILURE_LOG_PATH) == "failures.jsonl"
