"""Durable append-only log writers from `_docs/InformationModel.md`:
`QueryAuditLog` (every attempt, unconditional) and `FailureLogEntry`
(`failures.jsonl`, only on full failure). Both are flat JSONL via stdlib
`json`, per `_docs/technology_stack.md` §8 - no logging framework.

`LlmCallLog` (`llm_calls.jsonl`) was added later, for the monitoring
dashboard - same unconditional-append-only pattern, one line per LLM call
rather than per SQL attempt. GPT-4o-mini pricing is hardcoded below since
OpenAI's SDK reports token counts but not a dollar cost; update the two
constants if pricing changes (last checked against OpenAI's published
rates, September 2026). This does not account for prompt-caching discount
pricing, since this project's calls don't use the cached-input feature.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from data_analyst_agent.models.entities import (
    FailureLogEntry,
    LlmCallLog,
    LlmCallType,
    QueryAuditLog,
    SqlAttempt,
)

QUERY_AUDIT_LOG_PATH = Path("query_audit.jsonl")
FAILURE_LOG_PATH = Path("failures.jsonl")
LLM_CALL_LOG_PATH = Path("llm_calls.jsonl")

# USD per token, GPT-4o-mini standard (non-cached) rates.
_PRICE_PER_INPUT_TOKEN = Decimal("0.15") / Decimal("1_000_000")
_PRICE_PER_OUTPUT_TOKEN = Decimal("0.60") / Decimal("1_000_000")


def compute_cost_usd(prompt_tokens: int, completion_tokens: int) -> Decimal:
    return prompt_tokens * _PRICE_PER_INPUT_TOKEN + completion_tokens * _PRICE_PER_OUTPUT_TOKEN


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


def log_llm_call(
    call_type: LlmCallType,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    latency_ms: int,
    turn_id: str | None = None,
    session_id: str | None = None,
    path: Path | str | None = None,
) -> None:
    """Append one `LlmCallLog` line. Written unconditionally by every LLM
    call site (generate_sql/narrative/diagnosis), independent of whether
    the call succeeds, fails, or is part of a real turn at all.
    """
    entry = LlmCallLog(
        call_type=call_type,
        model=model,
        turn_id=turn_id,
        session_id=session_id,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        cost_usd=compute_cost_usd(prompt_tokens, completion_tokens),
        latency_ms=latency_ms,
        logged_at=datetime.now(timezone.utc),
    )
    _append_line(path if path is not None else LLM_CALL_LOG_PATH, entry.model_dump_json())


def _append_line(path: Path | str, json_line: str) -> None:
    # One write() call of the complete, newline-terminated line - never
    # several partial writes - is what keeps each append atomic and
    # ensures the file is never truncated, only ever grown.
    with open(path, mode="a", encoding="utf-8") as f:
        f.write(json_line + "\n")
