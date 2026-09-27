"""Full turn orchestration, per `_docs/Architecture.md`'s workflow diagram
end to end: wires S20's retry loop into S21 (chart) + S22 (narrative) on
success, or S23 (diagnosis) on any failure path, then S05's audit logging,
producing the one object the Streamlit layer (S27) renders - `Answer`.

This is the only module that sees every LLM-invoking call in a turn
(`generate_sql` and `narrative.wrap`/`diagnose`, the latter two indirectly
via `run_turn_sql`/`diagnose`), so it - not S20's retry loop - is where
`session.turn_ids` gets appended, per `retry_loop.py`'s own docstring.

`declined` (`SqlRetryOutcome.status`, added post-S23) has no dedicated
mapping in `Architecture.md`'s diagram, which predates it - it is treated
identically to `exhausted`/`fast_fail`: all three route through `diagnose()`
and become a `graceful_failure` Answer, and all three write a
`FailureLogEntry`. This is a deliberate extension of the diagram's intent
(every failure path funnels through diagnosis before composing the failure
response), not a literal instruction from any doc, since `declined` did not
exist when `Architecture.md`/`InformationModel.md` were written. Already
verified empirically in the post-S23 `can_answer_from_schema` fix: calling
`diagnose()` on a `declined` outcome's single `rejected` attempt produces
the correct category 7/7 times against S18's gold `expected_failure_category`.

Flagged gap, partially resolved, not silently closed: `session.cost_spent_usd`
is still never incremented anywhere in the codebase from real OpenAI usage.
`scope.md` says the cost cap is "tracked at the LLM call layer." Post-v1,
for the monitoring dashboard, `generate_sql`/`narrative.wrap`/`diagnose`
were changed to capture real `completion.usage` and log it (token counts,
cost, latency) to `llm_calls.jsonl` unconditionally - see
`agent/audit_log.py::log_llm_call`. That log is fire-and-forget, though,
not a return value any of the three functions hand back to their caller,
so it does not by itself feed `session.cost_spent_usd` - wiring that
still means deciding how a live-session cost figure should be accumulated
across a turn (this module) and fed back into `SessionState`, which is a
distinct, still-open piece of work, deliberately not bundled into the
monitoring-dashboard change that added the underlying data. The
`budget_stop` path itself is fully correct and reachable (proven by this
slice's own mocked tests, which set `session.cost_spent_usd` directly,
exactly as S20's retry-loop tests already did) - it just will not trigger
organically in a live run until that remaining wiring is done.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path

from openai import OpenAI

from data_analyst_agent.agent.audit_log import log_attempt, log_failure
from data_analyst_agent.agent.chart_select import build_chart_spec
from data_analyst_agent.agent.conversation_memory import update_memory
from data_analyst_agent.agent.diagnosis import diagnose
from data_analyst_agent.agent.narrative import wrap
from data_analyst_agent.agent.retry_loop import run_turn_sql
from data_analyst_agent.agent.session import check_cost_cap
from data_analyst_agent.models.entities import (
    Answer,
    ConversationMemory,
    FailureLogEntry,
    ResultData,
    SessionState,
)

_FAILED_TURN_PLACEHOLDER = "(could not be answered)"

_GRACEFUL_FAILURE_STATUSES = frozenset({"declined", "exhausted", "fast_fail"})


def answer_question(
    question: str,
    session: SessionState,
    db_path: Path | str | None = None,
    client: OpenAI | None = None,
    query_audit_log_path: Path | str | None = None,
    failure_log_path: Path | str | None = None,
    llm_call_log_path: Path | str | None = None,
    conversation_memory: ConversationMemory | None = None,
) -> Answer:
    """Runs one full turn end to end and returns the `Answer` the UI
    renders. Mutates `session` in place: appends `turn_id` to
    `session.turn_ids` (this is the whole-turn lifecycle concern
    `run_turn_sql` explicitly leaves to its caller), and `run_turn_sql`
    itself already mutates `session.failed_questions_cache` on exhaustion
    or decline. When given, also mutates `conversation_memory` in place on
    a success or graceful-failure outcome (never on `budget_stop`, since a
    turn that spent nothing produced nothing worth remembering either) -
    see `agent/conversation_memory.py`. Kept inside this function rather
    than left to the caller specifically because this is "the only module
    that sees every LLM-invoking call in a turn" (see the module docstring
    above) - the summarization call `update_memory` can make is checked
    against the cost cap at the same site as every other LLM-invoking call
    in this function, not from a fourth, uncovered call site.
    """
    turn_id = str(uuid.uuid4())
    session.turn_ids.append(turn_id)

    outcome = run_turn_sql(
        question,
        session,
        turn_id=turn_id,
        db_path=db_path,
        client=client,
        llm_call_log_path=llm_call_log_path,
        conversation_memory=conversation_memory,
    )

    for attempt in outcome.attempts:
        log_attempt(attempt, session_id=session.session_id, path=query_audit_log_path)

    if outcome.status == "budget_stop":
        return Answer(turn_id=turn_id, status="budget_stop")

    if outcome.status in _GRACEFUL_FAILURE_STATUSES:
        diagnosis = diagnose(
            question,
            outcome.attempts,
            db_path=db_path,
            client=client,
            turn_id=turn_id,
            session_id=session.session_id,
            llm_call_log_path=llm_call_log_path,
        )
        log_failure(
            FailureLogEntry(
                turn_id=turn_id,
                session_id=session.session_id,
                question_text=question,
                attempts=outcome.attempts,
                diagnosis=diagnosis,
                fast_fail_triggered=(outcome.status == "fast_fail"),
                logged_at=datetime.now(timezone.utc),
            ),
            path=failure_log_path,
        )
        if conversation_memory is not None:
            update_memory(
                conversation_memory,
                session,
                question,
                sql=None,
                answer_text=_FAILED_TURN_PLACEHOLDER,
                client=client,
                turn_id=turn_id,
                session_id=session.session_id,
                llm_call_log_path=llm_call_log_path,
            )
            if check_cost_cap(session):
                return Answer(turn_id=turn_id, status="budget_stop")
        return Answer(turn_id=turn_id, status="graceful_failure", diagnosis=diagnosis)

    # outcome.status == "success"
    result = outcome.result
    result_data = ResultData(columns=result.columns, rows=result.rows)
    chart_spec = build_chart_spec(result_data)
    narrative = wrap(
        question,
        result,
        chart_spec,
        client=client,
        turn_id=turn_id,
        session_id=session.session_id,
        llm_call_log_path=llm_call_log_path,
    )

    if check_cost_cap(session):
        return Answer(turn_id=turn_id, status="budget_stop")

    sql_shown = outcome.attempts[-1].query_text
    if conversation_memory is not None:
        update_memory(
            conversation_memory,
            session,
            question,
            sql=sql_shown,
            answer_text=narrative.answer_text,
            client=client,
            turn_id=turn_id,
            session_id=session.session_id,
            llm_call_log_path=llm_call_log_path,
        )
        if check_cost_cap(session):
            return Answer(turn_id=turn_id, status="budget_stop")

    return Answer(
        turn_id=turn_id,
        status="success",
        answer_text=narrative.answer_text,
        assumption_disclosed=narrative.assumption_disclosed,
        chart_spec=chart_spec,
        sql_shown=sql_shown,
    )


CANONICAL_QUESTIONS = [
    "Which products are selling the most in which region?",
    "What is my average order value across all regions?",
    "Which products are the least performing?",
]


def _smoke_test() -> None:
    from decimal import Decimal

    from dotenv import load_dotenv

    load_dotenv()
    for question in CANONICAL_QUESTIONS:
        session = SessionState(
            session_id=str(uuid.uuid4()),
            started_at=datetime.now(timezone.utc),
            cost_spent_usd=Decimal("0"),
            cost_cap_usd=Decimal("0.50"),
        )
        answer = answer_question(question, session)
        print(f"Q: {question}")
        print(f"Status: {answer.status}")
        if answer.status == "success":
            print(f"Answer: {answer.answer_text}")
            print(f"Chart: {answer.chart_spec.chart_type}")
            print(f"SQL: {answer.sql_shown}")
        elif answer.status == "graceful_failure":
            print(f"Diagnosis: {answer.diagnosis.category} - {answer.diagnosis.explanation}")
            print(f"Rephrase: {answer.diagnosis.rephrase_suggestion}")
        print()


if __name__ == "__main__":
    import sys

    if "--smoke-test" in sys.argv:
        _smoke_test()
    else:
        print("Usage: python -m data_analyst_agent.agent.orchestrator --smoke-test")
