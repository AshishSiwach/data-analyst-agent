"""Streamlit chat interface, per `_docs/scope.md`'s turn shape and
five-part failure UX, wired directly into S24's `answer_question` - the
second observable-user-value milestone
(`_docs/implementation_plan.md`): a human, in a browser, can ask a
question and get a real answer.

`st.session_state` holds the actual `SessionState` (S01) object -
`_docs/technology_stack.md`'s "Memory / session state" recommendation is
exactly this: a plain object with `SessionState`'s three fields, not a
framework. Streamlit reruns this whole script on every interaction, but
`st.session_state` is a persistent, per-browser-session dict of object
references, so the same `SessionState` instance - and `answer_question`'s
in-place mutations to it (`turn_ids`, `failed_questions_cache`) - survives
across turns within one session, per this slice's own acceptance
criterion.

Chat *history* (the list of past question/answer pairs shown on screen)
is a UI-only, display-purpose list - separate from `SessionState` and
never fed back into `answer_question`. `_docs/scope.md`'s turn shape is
explicit that each turn's `context` is empty in v1; this file honors
that by calling `answer_question` with only the newly-typed question each
time, never any prior conversation text.

Failure-UX detail, flagged rather than silently worked around: `Answer`
(S01/S24) carries a `diagnosis` on graceful failure, but not the
underlying `SqlAttempt` trace - `scope.md`'s five-part failure UX wants
"attempted SQL, error messages, retry count" on a details toggle (part 4,
"same toggle convention as the 'show SQL' on success"), and that trace
only exists in `QueryAuditLog` (`query_audit.jsonl`), which every attempt
is unconditionally logged to by `answer_question` regardless of outcome.
`_load_attempt_trace` below reads it back by `turn_id`, the same pattern
`eval/harness.py` already uses to recover its attempts-until-success
metric from the same file.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pandas as pd
import streamlit as st
from dotenv import load_dotenv

from data_analyst_agent.agent.audit_log import QUERY_AUDIT_LOG_PATH
from data_analyst_agent.agent.chart_select import render as render_chart
from data_analyst_agent.agent.orchestrator import answer_question
from data_analyst_agent.models.entities import Answer, QueryAuditLog, SessionState

load_dotenv()

_FAILURE_CATEGORY_LABELS = {
    "schema_mismatch": "Data not available",
    "ambiguity": "Question was ambiguous",
    "bug": "Something went wrong",
}


def _load_attempt_trace(
    turn_id: str, path: Path | str = QUERY_AUDIT_LOG_PATH
) -> list[QueryAuditLog]:
    path = Path(path)
    if not path.exists():
        return []
    records: list[QueryAuditLog] = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            entry = QueryAuditLog.model_validate_json(line)
            if entry.turn_id == turn_id:
                records.append(entry)
    return records


def _get_session() -> SessionState:
    if "session" not in st.session_state:
        st.session_state.session = SessionState(
            session_id=str(uuid.uuid4()),
            started_at=datetime.now(timezone.utc),
            cost_spent_usd=Decimal("0"),
            cost_cap_usd=Decimal("0.50"),
        )
    return st.session_state.session


def _get_history() -> list[tuple[str, Answer]]:
    if "history" not in st.session_state:
        st.session_state.history = []
    return st.session_state.history


def _render_success(answer: Answer) -> None:
    st.write(answer.answer_text)
    if answer.assumption_disclosed:
        st.caption(f"Assumption: {answer.assumption_disclosed}")
    chart_spec = answer.chart_spec
    if chart_spec is not None:
        render_chart(chart_spec)
        # A chart (scalar/line/bar) encodes the result visually, which
        # loses the exact values a table shows - offer the same rows as a
        # plain table alongside it. A "table"-type chart already *is*
        # this view, so there's nothing extra to add there.
        if chart_spec.chart_type != "table":
            with st.expander("Show data"):
                st.dataframe(pd.DataFrame(chart_spec.data, columns=chart_spec.column_names))
    with st.expander("Show SQL"):
        st.code(answer.sql_shown or "", language="sql")


def _render_graceful_failure(answer: Answer) -> None:
    # Five-part failure UX per scope.md, in order: plain refusal,
    # diagnosed cause, rephrase suggestion, technical details on toggle
    # (the log to failures.jsonl is part 5 - already done by
    # answer_question itself, nothing for the UI to render).
    st.write(
        "I tried three ways to answer this and couldn't get a reliable result. "
        "I'd rather tell you that than guess."
    )
    diagnosis = answer.diagnosis
    if diagnosis is not None:
        label = _FAILURE_CATEGORY_LABELS.get(diagnosis.category, diagnosis.category)
        st.write(f"**{label}.** {diagnosis.explanation}")
        st.write(f"**Try instead:** {diagnosis.rephrase_suggestion}")
    with st.expander("Technical details"):
        attempts = _load_attempt_trace(answer.turn_id)
        if not attempts:
            st.caption(
                "No SQL was attempted for this question - it already failed "
                "3/3 earlier in this session."
            )
        for attempt in attempts:
            st.write(f"Attempt {attempt.attempt_number} - status: {attempt.status}")
            if attempt.query_text:
                st.code(attempt.query_text, language="sql")
            if attempt.error_message:
                st.caption(attempt.error_message)


def _render_budget_stop() -> None:
    st.write(
        "This session has hit its cost budget, so I can't make any more attempts "
        "right now. This is a safety limit, not a judgment on whether your "
        "question is answerable - start a new session to continue."
    )


def _render_answer(answer: Answer) -> None:
    if answer.status == "success":
        _render_success(answer)
    elif answer.status == "graceful_failure":
        _render_graceful_failure(answer)
    else:  # budget_stop
        _render_budget_stop()


def main() -> None:
    st.set_page_config(page_title="Data Analyst Agent", page_icon="📊")
    st.title("Data Analyst Agent")
    st.caption(
        "Ask a business question in plain English about orders, products, "
        "customers, or revenue. Every answer is backed by inspectable SQL, "
        "executed against a read-only semantic layer."
    )

    session = _get_session()
    history = _get_history()

    for question, answer in history:
        with st.chat_message("user"):
            st.write(question)
        with st.chat_message("assistant"):
            _render_answer(answer)

    question = st.chat_input('Ask a question, e.g. "which products sell best?"')
    if question:
        with st.chat_message("user"):
            st.write(question)
        with st.chat_message("assistant"):
            with st.spinner("Thinking..."):
                answer = answer_question(question, session)
            _render_answer(answer)
        history.append((question, answer))


if __name__ == "__main__":
    main()
