"""Local, operator-only monitoring dashboard, added post-v1. Reads the
three append-only JSONL logs the agent already writes unconditionally
(`query_audit.jsonl`, `failures.jsonl`, `llm_calls.jsonl`) and renders the
KPIs from that data - no new database, no Grafana, no external state
store, per `_docs/CLAUDE.md`'s "what not to add" list, which this
deliberately stays inside rather than reopening.

Deliberately not part of the deployed Streamlit Community Cloud app
(`app/streamlit_app.py`): these logs can contain error text, SQL, and
per-call cost, which has no reason to be public. Run this locally:

    uv run streamlit run data_analyst_agent/app/dashboard.py

"Concurrent users" (one of the originally-considered KPIs) is deliberately
not shown here - this app has no auth and no session-to-user mapping, so
there is no "user" for a session to belong to; showing it would just be a
relabeled request-volume chart under a misleading name.
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import streamlit as st

from data_analyst_agent.agent.audit_log import (
    FAILURE_LOG_PATH,
    LLM_CALL_LOG_PATH,
    QUERY_AUDIT_LOG_PATH,
)


def _load_jsonl(path: Path, columns: list[str]) -> pd.DataFrame:
    """Reads a JSONL log into a DataFrame. Returns an empty, correctly
    shaped DataFrame (not an error) when the file doesn't exist yet - a
    fresh clone with no traffic is a normal state, not a failure."""
    if not Path(path).exists():
        return pd.DataFrame(columns=columns)
    rows = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    df = pd.DataFrame(rows, columns=columns)
    if "logged_at" in df.columns:
        df["logged_at"] = pd.to_datetime(df["logged_at"], errors="coerce")
    return df


def _load_data() -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    audit = _load_jsonl(
        QUERY_AUDIT_LOG_PATH,
        [
            "turn_id",
            "session_id",
            "attempt_number",
            "query_text",
            "status",
            "execution_ms",
            "row_count",
            "error_message",
            "logged_at",
        ],
    )
    failures = _load_jsonl(
        FAILURE_LOG_PATH,
        ["turn_id", "session_id", "question_text", "attempts", "diagnosis", "fast_fail_triggered"],
    )
    llm_calls = _load_jsonl(
        LLM_CALL_LOG_PATH,
        [
            "call_type",
            "model",
            "turn_id",
            "session_id",
            "prompt_tokens",
            "completion_tokens",
            "cost_usd",
            "latency_ms",
            "logged_at",
        ],
    )
    if not llm_calls.empty:
        llm_calls["cost_usd"] = llm_calls["cost_usd"].astype(float)
    return audit, failures, llm_calls


def _render_availability(audit: pd.DataFrame) -> None:
    st.header("Availability and reliability")
    st.caption(
        "From every SQL attempt logged in query_audit.jsonl, regardless of which "
        "turn it belonged to or how that turn ended."
    )
    if audit.empty:
        st.info("No attempts logged yet.")
        return

    total = len(audit)
    counts = audit["status"].value_counts()
    success_rate = counts.get("success", 0) / total
    error_rate = (counts.get("error", 0) + counts.get("timeout", 0)) / total
    rejected_rate = counts.get("rejected", 0) / total

    col1, col2, col3 = st.columns(3)
    col1.metric("Success rate", f"{success_rate:.1%}")
    col2.metric("Error rate", f"{error_rate:.1%}", help="error + timeout statuses")
    col3.metric("Validation failure rate", f"{rejected_rate:.1%}", help="rejected statuses")
    st.bar_chart(counts)


def _render_performance(audit: pd.DataFrame, llm_calls: pd.DataFrame) -> None:
    st.header("Performance")
    st.caption(
        "Query execution time comes from query_audit.jsonl; model response time "
        "and end-to-end turn latency come from llm_calls.jsonl (added for this "
        "dashboard) joined back to query_audit.jsonl by turn_id."
    )
    executed = audit[audit["status"] != "rejected"] if not audit.empty else audit
    col1, col2 = st.columns(2)
    with col1:
        st.subheader("Query execution time (ms)")
        if executed.empty:
            st.info("No executed queries yet.")
        else:
            st.write(
                executed["execution_ms"].describe(percentiles=[0.5, 0.95])[["50%", "95%", "max"]]
            )

    with col2:
        st.subheader("Model response time (ms), by call type")
        if llm_calls.empty:
            st.info("No LLM calls logged yet.")
        else:
            st.write(
                llm_calls.groupby("call_type")["latency_ms"].describe(percentiles=[0.5, 0.95])[
                    ["50%", "95%", "max"]
                ]
            )

    st.subheader("End-to-end turn latency (ms)")
    if audit.empty and llm_calls.empty:
        st.info("No turns logged yet.")
    else:
        sql_ms = (
            audit.groupby("turn_id")["execution_ms"].sum()
            if not audit.empty
            else pd.Series(dtype=float)
        )
        llm_ms = (
            llm_calls.groupby("turn_id")["latency_ms"].sum()
            if not llm_calls.empty
            else pd.Series(dtype=float)
        )
        turn_latency = sql_ms.add(llm_ms, fill_value=0).dropna()
        turn_latency = turn_latency[turn_latency.index != ""]
        if turn_latency.empty:
            st.info("No completed turns with both SQL and LLM timing yet.")
        else:
            st.write(turn_latency.describe(percentiles=[0.5, 0.95])[["50%", "95%", "max"]])


def _render_usage(audit: pd.DataFrame) -> None:
    st.header("Usage and load")
    st.caption(
        "Request volume is distinct turns per day. See the module docstring for "
        "why 'concurrent users' isn't shown - this app has no per-user identity."
    )
    if audit.empty:
        st.info("No turns logged yet.")
        return

    turns = audit.dropna(subset=["turn_id"]).drop_duplicates("turn_id")
    by_day = turns.groupby(turns["logged_at"].dt.date).size()
    st.subheader("Request volume (turns/day)")
    st.line_chart(by_day)

    attempts_per_turn = audit.groupby("turn_id").size()
    st.subheader("Attempts-per-turn distribution")
    st.bar_chart(attempts_per_turn.value_counts().sort_index())


def _render_model_kpis(llm_calls: pd.DataFrame, audit: pd.DataFrame) -> None:
    st.header("Model-specific KPIs")
    if llm_calls.empty:
        st.info("No LLM calls logged yet.")
        return

    total_tokens = int(llm_calls["prompt_tokens"].sum() + llm_calls["completion_tokens"].sum())
    total_cost = llm_calls["cost_usd"].sum()

    col1, col2, col3 = st.columns(3)
    col1.metric("Total tokens", f"{total_tokens:,}")
    col2.metric("Total cost", f"${total_cost:,.4f}")
    if not audit.empty:
        attempts_per_turn = audit.groupby("turn_id").size()
        retry_rate = (attempts_per_turn > 1).mean()
        col3.metric(
            "Retry rate",
            f"{retry_rate:.1%}",
            help=(
                "Fraction of turns needing a 2nd or 3rd attempt. This system has no "
                "separate fallback model to measure a literal 'fallback rate' "
                "against - retry rate is the closest real signal it has."
            ),
        )

    st.subheader("Cost per day")
    by_day = llm_calls.groupby(llm_calls["logged_at"].dt.date)["cost_usd"].sum()
    st.line_chart(by_day)

    st.subheader("Tokens by call type")
    by_type = llm_calls.groupby("call_type")[["prompt_tokens", "completion_tokens"]].sum()
    st.bar_chart(by_type)


def _render_recovery(failures: pd.DataFrame, audit: pd.DataFrame) -> None:
    st.header("Debugging and recovery")
    st.caption(
        "failures.jsonl only records turns that fully exhausted their retry "
        "budget. This is real production traffic, not the gold-set harness, so "
        "there's no known-correct label to grade a 'graceful failure rate' "
        "against here - this section shows how often turns needed recovery and "
        "what the diagnosis step made of them, not whether it was 'right'."
    )
    if not audit.empty:
        attempts_per_turn = audit.groupby("turn_id").size()
        needed_recovery = (attempts_per_turn > 1).sum()
        st.metric(
            "Turns that needed a retry",
            f"{needed_recovery} / {len(attempts_per_turn)}",
        )

    if failures.empty:
        st.info("No fully-failed turns logged yet.")
        return

    st.metric("Fully-failed turns", len(failures))
    categories = failures["diagnosis"].apply(
        lambda d: d.get("category", "unknown") if isinstance(d, dict) else "unknown"
    )
    st.subheader("Diagnosis category breakdown")
    st.bar_chart(categories.value_counts())


def main() -> None:
    st.set_page_config(page_title="Data Analyst Agent - Monitoring", page_icon="📈")
    st.title("Data Analyst Agent - Monitoring")
    st.caption(
        "Local operator dashboard, not part of the public app. Reads "
        f"`{QUERY_AUDIT_LOG_PATH}`, `{FAILURE_LOG_PATH}`, and `{LLM_CALL_LOG_PATH}` "
        "from the current working directory."
    )

    audit, failures, llm_calls = _load_data()
    if audit.empty and failures.empty and llm_calls.empty:
        st.warning(
            "No logs found yet. Ask the app a few questions first "
            "(`uv run streamlit run data_analyst_agent/app/streamlit_app.py`), "
            "then reload this page."
        )
        return

    _render_availability(audit)
    st.divider()
    _render_performance(audit, llm_calls)
    st.divider()
    _render_usage(audit)
    st.divider()
    _render_model_kpis(llm_calls, audit)
    st.divider()
    _render_recovery(failures, audit)


if __name__ == "__main__":
    main()
