"""Durable append-only log writers from `_docs/InformationModel.md`:
`QueryAuditLog` (every attempt, unconditional) and `FailureLogEntry`
(`failures.jsonl`, only on full failure). Both are flat JSONL via stdlib
`json`, per `_docs/technology_stack.md` §8 - no logging framework.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from data_analyst_agent.models.entities import FailureLogEntry, QueryAuditLog, SqlAttempt

QUERY_AUDIT_LOG_PATH = Path("query_audit.jsonl")
FAILURE_LOG_PATH = Path("failures.jsonl")


def log_attempt(attempt: SqlAttempt, session_id: str, path: Path | str | None = None) -> None:
    """Append one `QueryAuditLog` line for this `SqlAttempt`. Written
    unconditionally - every attempt, success or failure.

    `QueryAuditLog` is a distinct, session-aware entity (InformationModel.md
    §Layer 2): it carries `session_id` and `logged_at`, neither of which is
    on `SqlAttempt`, and it drops `SqlAttempt.truncated`. This function is
    what assembles the two - the caller supplies the session id it already
    holds, and the timestamp is generated here at write time.
    """
    entry = QueryAuditLog(
        turn_id=attempt.turn_id,
        session_id=session_id,
        attempt_number=attempt.attempt_number,
        query_text=attempt.query_text,
        status=attempt.status,
        execution_ms=attempt.execution_ms,
        row_count=attempt.row_count,
        error_message=attempt.error_message,
        logged_at=datetime.now(timezone.utc),
    )
    _append_line(path if path is not None else QUERY_AUDIT_LOG_PATH, entry.model_dump_json())


def log_failure(entry: FailureLogEntry, path: Path | str | None = None) -> None:
    """Append one `FailureLogEntry` line. Written only when a turn fully
    fails: 3 attempts exhausted, or the fast-fail cache short-circuited it.
    """
    _append_line(path if path is not None else FAILURE_LOG_PATH, entry.model_dump_json())


def _append_line(path: Path | str, json_line: str) -> None:
    # One write() call of the complete, newline-terminated line - never
    # several partial writes - is what keeps each append atomic and
    # ensures the file is never truncated, only ever grown.
    with open(path, mode="a", encoding="utf-8") as f:
        f.write(json_line + "\n")
