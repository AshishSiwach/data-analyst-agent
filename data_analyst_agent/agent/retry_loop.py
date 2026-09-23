"""Bounded retry loop per `_docs/Architecture.md`: wires `generate_sql`
(S19) and `run_sql` (S15) together, hard-capped at 3 attempts via a
counter - never a model-negotiated decision, per `_docs/CLAUDE.md`'s
structural rule. The fast-fail check runs once at turn start; the cost
cap is checked after every `generate_sql` call, matching the diagram's
`GenSQL -> CostCheck1` order exactly (cost is only knowable after a call
completes, not predicted before one).

Return type note (flagged, not silently resolved): this slice's
acceptance criteria says `run_turn_sql(question, session) -> SqlAttempt`.
Implemented as `-> SqlRetryOutcome` instead (see `models/entities.py`) -
`SqlAttempt` can't carry the actual result rows a successful turn needs
to hand to chart rendering (S21) and narrative wrap (S22), and has no way
to represent `fast_fail`/`budget_stop` as distinct outcomes from an
ordinary SQL failure. Same resolution pattern as S15's `SqlAttempt` ->
`SqlExecutionResult` gap.

`declined` (added retroactively, post-S23): if `generate_sql` itself
judges a question unanswerable from the schema (`can_answer_from_schema
== False`), the loop stops immediately rather than burning the remaining
attempts hoping the model reconsiders - same "stop retrying blind"
reasoning as fast-fail. This was added after S23's required evaluation
case showed the model would rather invent a plausible-but-wrong query
(e.g. `WHERE country = 'Scotland'` returning 0 rows, reported as a normal
success) than recognize a genuine schema gap.

Not this module's job (left to S24's orchestrator, which is the layer
that sees every LLM-invoking call in a turn, not just this one):
- Incrementing `session.cost_spent_usd` from actual API usage - S19's
  `generate_sql` doesn't currently expose token/cost data, and cost
  tracking needs to span `generate_sql` *and* `narrative.wrap` (S22),
  which this loop never calls.
- Appending the turn to `session.turn_ids` - a whole-turn lifecycle
  concern, not specific to the SQL-retry sub-loop.
"""

from __future__ import annotations

import uuid
from pathlib import Path

from openai import OpenAI

from data_analyst_agent.agent.generate_sql import generate_sql
from data_analyst_agent.agent.session import check_cost_cap, check_fast_fail, normalize
from data_analyst_agent.db.run_sql import run_sql
from data_analyst_agent.models.entities import (
    SessionState,
    SqlAttempt,
    SqlExecutionResult,
    SqlRetryOutcome,
)

MAX_ATTEMPTS = 3


def _record_failure_for_fast_fail(session: SessionState, question: str, attempt_count: int) -> None:
    normalized = normalize(question)
    prior_failures = session.failed_questions_cache.get(normalized, 0)
    session.failed_questions_cache[normalized] = prior_failures + attempt_count


def run_turn_sql(
    question: str,
    session: SessionState,
    turn_id: str | None = None,
    db_path: Path | str | None = None,
    client: OpenAI | None = None,
) -> SqlRetryOutcome:
    """Runs the bounded SQL-generation-and-execution loop for one turn.
    Mutates `session.failed_questions_cache` in place when a turn
    exhausts its budget or is declined, so a later identical question
    fast-fails - this is the only writer of that cache field anywhere in
    the system.
    """
    resolved_turn_id = turn_id if turn_id is not None else str(uuid.uuid4())

    if check_fast_fail(session, question):
        return SqlRetryOutcome(status="fast_fail")

    attempts: list[SqlAttempt] = []
    prior_error: str | None = None
    result: SqlExecutionResult | None = None

    for attempt_number in range(1, MAX_ATTEMPTS + 1):
        generated = generate_sql(question, prior_error=prior_error, db_path=db_path, client=client)

        if check_cost_cap(session):
            return SqlRetryOutcome(status="budget_stop", attempts=attempts)

        if not generated.can_answer_from_schema:
            attempts.append(
                SqlAttempt(
                    turn_id=resolved_turn_id,
                    attempt_number=attempt_number,
                    query_text="",
                    status="rejected",
                    error_message=generated.reason,
                    execution_ms=0,
                    row_count=None,
                    truncated=False,
                )
            )
            _record_failure_for_fast_fail(session, question, len(attempts))
            return SqlRetryOutcome(status="declined", attempts=attempts)

        result = run_sql(generated.sql, attempt_number=attempt_number, db_path=db_path)
        attempts.append(
            SqlAttempt(
                turn_id=resolved_turn_id,
                attempt_number=attempt_number,
                query_text=generated.sql,
                status=result.status,
                error_message=result.error_message,
                execution_ms=result.execution_ms,
                row_count=result.row_count,
                truncated=result.truncated,
            )
        )

        if result.status == "success":
            return SqlRetryOutcome(status="success", result=result, attempts=attempts)

        prior_error = result.error_message

    _record_failure_for_fast_fail(session, question, len(attempts))
    return SqlRetryOutcome(status="exhausted", result=result, attempts=attempts)
