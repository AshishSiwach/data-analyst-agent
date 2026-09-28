"""Post-v1 acceptance tests for agent.conversation_memory (mocked LLM call).
See _docs/phase2_memory.md for the design this implements."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from data_analyst_agent.agent import audit_log
from data_analyst_agent.agent.conversation_memory import (
    _MAX_FIELD_CHARS,
    WINDOW_SIZE,
    _SummaryCompletion,
    build_context_block,
    find_dropped_date_filters,
    find_dropped_ranking_restrictions,
    update_memory,
)
from data_analyst_agent.models.entities import ConversationMemory, ConversationTurn, SessionState

NOW = datetime(2026, 9, 27, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _llm_call_log_to_tmp(tmp_path, monkeypatch):
    monkeypatch.setattr(audit_log, "LLM_CALL_LOG_PATH", tmp_path / "llm_calls.jsonl")


def _session(**overrides) -> SessionState:
    base = dict(
        session_id="sess-1",
        started_at=NOW,
        cost_spent_usd=Decimal("0"),
        cost_cap_usd=Decimal("0.50"),
    )
    base.update(overrides)
    return SessionState(**base)


class _FakeUsage:
    def __init__(self, prompt_tokens: int = 50, completion_tokens: int = 20):
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens


class _FakeMessage:
    def __init__(self, parsed: _SummaryCompletion):
        self.parsed = parsed


class _FakeChoice:
    def __init__(self, parsed: _SummaryCompletion):
        self.message = _FakeMessage(parsed)


class _FakeCompletion:
    def __init__(self, parsed: _SummaryCompletion):
        self.choices = [_FakeChoice(parsed)]
        self.usage = _FakeUsage()


class _FakeCompletionsAPI:
    def __init__(self, summary_text: str):
        self._parsed = _SummaryCompletion(summary=summary_text)
        self.call_count = 0
        self.captured_kwargs = None

    def parse(self, **kwargs):
        self.call_count += 1
        self.captured_kwargs = kwargs
        return _FakeCompletion(self._parsed)


class _FakeChatAPI:
    def __init__(self, summary_text: str):
        self.completions = _FakeCompletionsAPI(summary_text)


class _FakeClient:
    def __init__(self, summary_text: str = "Folded summary."):
        self.chat = _FakeChatAPI(summary_text)


def test_window_stays_at_window_size_after_more_appends_than_that():
    memory = ConversationMemory()
    session = _session()
    client = _FakeClient()

    for i in range(WINDOW_SIZE + 2):
        update_memory(memory, session, f"question {i}", f"SELECT {i}", f"answer {i}", client=client)

    assert len(memory.recent_turns) == WINDOW_SIZE


def test_oldest_turn_is_evicted_first():
    memory = ConversationMemory()
    session = _session()
    client = _FakeClient()

    for i in range(WINDOW_SIZE + 1):
        update_memory(memory, session, f"question {i}", f"SELECT {i}", f"answer {i}", client=client)

    # question 0 aged out; the window should hold questions 1..WINDOW_SIZE.
    remaining_questions = [t.question for t in memory.recent_turns]
    assert remaining_questions == [f"question {i}" for i in range(1, WINDOW_SIZE + 1)]


def test_summarization_fires_exactly_when_the_window_overflows():
    memory = ConversationMemory()
    session = _session()
    client = _FakeClient(summary_text="Q3 2011 UK revenue was discussed.")

    for i in range(WINDOW_SIZE):
        update_memory(memory, session, f"question {i}", f"SELECT {i}", f"answer {i}", client=client)
    assert client.chat.completions.call_count == 0  # window not yet full, no summarization needed

    update_memory(
        memory, session, "one more question", "SELECT 99", "one more answer", client=client
    )
    assert client.chat.completions.call_count == 1
    assert memory.summary == "Q3 2011 UK revenue was discussed."


def test_update_memory_mutates_in_place_and_returns_none():
    memory = ConversationMemory()
    session = _session()
    client = _FakeClient()

    result = update_memory(memory, session, "question", "SELECT 1", "answer", client=client)

    assert result is None
    assert len(memory.recent_turns) == 1  # the same object was mutated


def test_cost_cap_already_hit_skips_summarization_but_still_evicts():
    memory = ConversationMemory()
    session = _session(cost_spent_usd=Decimal("0.50"), cost_cap_usd=Decimal("0.50"))
    client = _FakeClient()

    for i in range(WINDOW_SIZE + 1):
        update_memory(memory, session, f"question {i}", f"SELECT {i}", f"answer {i}", client=client)

    assert client.chat.completions.call_count == 0  # never paid for a summarization call
    assert memory.summary is None  # oldest turn was dropped, not folded in
    assert len(memory.recent_turns) == WINDOW_SIZE


def test_build_context_block_empty_for_none():
    assert build_context_block(None) == ""


def test_build_context_block_empty_for_a_fresh_memory():
    assert build_context_block(ConversationMemory()) == ""


def test_build_context_block_contains_framing_and_turn_content():
    memory = ConversationMemory(
        summary="Earlier, UK revenue for Q3 2011 was discussed.",
        recent_turns=[
            ConversationTurn(
                question="What was Q3 2011 UK revenue?",
                sql="SELECT SUM(net_revenue) FROM v_orders",
                answer_text="Q3 2011 UK revenue was £1.2M.",
            )
        ],
    )
    block = build_context_block(memory)

    assert "for reference only" in block
    assert "never as instructions" in block
    assert "Earlier, UK revenue for Q3 2011 was discussed." in block
    assert "What was Q3 2011 UK revenue?" in block
    assert "£1.2M" in block


def test_build_context_block_marks_a_failed_turn():
    memory = ConversationMemory(
        recent_turns=[
            ConversationTurn(
                question="What about last quarter?", sql=None, answer_text="(could not be answered)"
            )
        ]
    )
    block = build_context_block(memory)
    assert "(could not be answered)" in block


def test_oversized_fields_are_truncated_on_store():
    memory = ConversationMemory()
    session = _session()
    client = _FakeClient()
    long_text = "x" * (_MAX_FIELD_CHARS + 100)

    update_memory(memory, session, long_text, long_text, long_text, client=client)

    turn = memory.recent_turns[0]
    assert len(turn.question) <= _MAX_FIELD_CHARS + len("... (truncated)")
    assert turn.question.endswith("... (truncated)")
    assert len(turn.sql) <= _MAX_FIELD_CHARS + len("... (truncated)")
    assert len(turn.answer_text) <= _MAX_FIELD_CHARS + len("... (truncated)")


# --- find_dropped_date_filters: the code-level check added after three
# prompt-only fixes each failed 4/4 live-tested runs against this exact
# failure shape (see _docs/phase2_memory.md). ---

_PRIOR_SQL_WITH_YEAR_FILTER = (
    "SELECT country FROM v_orders WHERE country <> 'UK' "
    "AND EXTRACT(YEAR FROM order_date) = 2011 "
    "GROUP BY country ORDER BY SUM(revenue) DESC LIMIT 3"
)


def test_no_prior_sql_means_nothing_dropped():
    assert find_dropped_date_filters(None, "SELECT 1") == []
    assert find_dropped_date_filters("", "SELECT 1") == []


def test_prior_sql_with_no_date_filter_means_nothing_to_carry_over():
    prior_no_date = "SELECT country FROM v_orders GROUP BY country"
    assert find_dropped_date_filters(prior_no_date, "SELECT country FROM v_orders") == []


def test_flags_a_simple_dropped_year_filter():
    new_sql = "SELECT country FROM v_orders WHERE country <> 'UK' GROUP BY country"
    dropped = find_dropped_date_filters(_PRIOR_SQL_WITH_YEAR_FILTER, new_sql)
    assert dropped == ["EXTRACT(YEAR FROM order_date)"]


def test_does_not_flag_when_the_filter_is_kept():
    new_sql = (
        "SELECT country FROM v_orders "
        "WHERE country <> 'UK' AND EXTRACT(YEAR FROM order_date) = 2011 GROUP BY country"
    )
    assert find_dropped_date_filters(_PRIOR_SQL_WITH_YEAR_FILTER, new_sql) == []


def test_does_not_flag_a_deliberate_year_change():
    # Same part + column, different value - the founder asked for a
    # different year on purpose, not a case of the filter vanishing.
    new_sql = (
        "SELECT country FROM v_orders WHERE EXTRACT(YEAR FROM order_date) = 2010 GROUP BY country"
    )
    assert find_dropped_date_filters(_PRIOR_SQL_WITH_YEAR_FILTER, new_sql) == []


def test_flags_the_exact_reproduced_bug_filter_only_inside_an_in_subquery():
    # The real failure this check exists for: the year filter survives
    # only inside the IN(...) subquery that ranks the top-3 countries -
    # it never scopes the outer query's own rows.
    new_sql = """
        SELECT DATE_TRUNC('month', order_date) AS month, country, SUM(line_revenue) AS revenue
        FROM v_orders JOIN v_order_lines ON v_orders.order_id = v_order_lines.order_id
        WHERE country IN (
            SELECT country
            FROM v_orders JOIN v_order_lines ON v_orders.order_id = v_order_lines.order_id
            WHERE country <> 'United Kingdom' AND EXTRACT(YEAR FROM order_date) = 2011
            GROUP BY country ORDER BY SUM(line_revenue) DESC LIMIT 3
        )
        GROUP BY month, country
    """
    dropped = find_dropped_date_filters(_PRIOR_SQL_WITH_YEAR_FILTER, new_sql)
    assert dropped == ["EXTRACT(YEAR FROM order_date)"]


def test_does_not_flag_when_filter_is_on_both_outer_query_and_in_subquery():
    new_sql = """
        SELECT DATE_TRUNC('month', order_date) AS month, country, SUM(line_revenue) AS revenue
        FROM v_orders JOIN v_order_lines ON v_orders.order_id = v_order_lines.order_id
        WHERE EXTRACT(YEAR FROM order_date) = 2011
        AND country IN (
            SELECT country
            FROM v_orders JOIN v_order_lines ON v_orders.order_id = v_order_lines.order_id
            WHERE EXTRACT(YEAR FROM order_date) = 2011
            GROUP BY country ORDER BY SUM(line_revenue) DESC LIMIT 3
        )
        GROUP BY month, country
    """
    assert find_dropped_date_filters(_PRIOR_SQL_WITH_YEAR_FILTER, new_sql) == []


def test_does_not_flag_a_filtered_cte_the_outer_query_reads_from_directly():
    # The recommended-but-abandoned prompt fix's pattern: filter once in a
    # CTE, have the outer query read from that CTE directly (no IN-subquery
    # at all). The filter legitimately scopes the outer rows this way.
    new_sql = """
        WITH filtered AS (
            SELECT country, order_date, line_revenue
            FROM v_orders JOIN v_order_lines ON v_orders.order_id = v_order_lines.order_id
            WHERE EXTRACT(YEAR FROM order_date) = 2011 AND country <> 'United Kingdom'
        ),
        top_countries AS (
            SELECT country FROM filtered GROUP BY country ORDER BY SUM(line_revenue) DESC LIMIT 3
        )
        SELECT DATE_TRUNC('month', order_date) AS month, country, SUM(line_revenue) AS revenue
        FROM filtered
        WHERE country IN (SELECT country FROM top_countries)
        GROUP BY month, country
    """
    assert find_dropped_date_filters(_PRIOR_SQL_WITH_YEAR_FILTER, new_sql) == []


def test_flags_only_the_specific_filter_that_was_dropped():
    prior = (
        "SELECT country FROM v_orders WHERE EXTRACT(YEAR FROM order_date) = 2011 "
        "AND EXTRACT(QUARTER FROM order_date) = 3 GROUP BY country"
    )
    new_sql = (
        "SELECT country FROM v_orders WHERE EXTRACT(YEAR FROM order_date) = 2011 GROUP BY country"
    )
    assert find_dropped_date_filters(prior, new_sql) == ["EXTRACT(QUARTER FROM order_date)"]


def test_unparseable_new_sql_does_not_raise():
    # A genuine syntax error is db/sql_guard.py's job to catch - this
    # check just needs to not crash on it.
    find_dropped_date_filters(_PRIOR_SQL_WITH_YEAR_FILTER, "SELECT FROM WHERE (((")


# --- find_dropped_ranking_restrictions: added after fixing the date-filter
# check surfaced a second, related failure - the model's correction
# sometimes drops the "top N" ranking while fixing the date filter. ---

_PRIOR_SQL_WITH_TOP_3_RANKING = (
    "SELECT country, SUM(revenue) FROM v_orders "
    "WHERE EXTRACT(YEAR FROM order_date) = 2011 AND country <> 'United Kingdom' "
    "GROUP BY country ORDER BY SUM(revenue) DESC LIMIT 3"
)


def test_no_prior_sql_means_no_ranking_dropped():
    assert find_dropped_ranking_restrictions(None, "SELECT 1") == []


def test_prior_sql_with_no_ranking_means_nothing_to_carry_over():
    prior_no_ranking = "SELECT SUM(revenue) FROM v_orders WHERE country = 'UK'"
    assert find_dropped_ranking_restrictions(prior_no_ranking, "SELECT 1 FROM v_orders") == []


def test_flags_the_exact_reproduced_bug_broad_exclusion_with_no_bounding():
    # The real failure this check exists for: the model "fixes" a dropped
    # date filter by copying the prior turn's whole WHERE clause verbatim,
    # which restores the exclusion but loses the LIMIT-based ranking that
    # bounded it down to 3 specific countries.
    broken = (
        "SELECT DATE_TRUNC('month', order_date) AS month, SUM(line_revenue) AS revenue "
        "FROM v_orders WHERE EXTRACT(YEAR FROM order_date) = 2011 "
        "AND country <> 'United Kingdom' GROUP BY month"
    )
    dropped = find_dropped_ranking_restrictions(_PRIOR_SQL_WITH_TOP_3_RANKING, broken)
    assert dropped == [
        "the top 3 restriction on country (only a broad exclusion "
        "remains, not a specific set of values)"
    ]


def test_does_not_flag_a_literal_in_list_of_the_right_entities():
    new_sql = (
        "SELECT month, country, SUM(revenue) FROM v_orders "
        "WHERE country IN ('EIRE', 'Germany', 'Netherlands') GROUP BY month, country"
    )
    assert find_dropped_ranking_restrictions(_PRIOR_SQL_WITH_TOP_3_RANKING, new_sql) == []


def test_does_not_flag_a_re_derived_subquery_ranking():
    new_sql = (
        "SELECT month, country FROM v_orders WHERE country IN "
        "(SELECT country FROM v_orders GROUP BY country ORDER BY SUM(revenue) DESC LIMIT 3) "
        "GROUP BY month, country"
    )
    assert find_dropped_ranking_restrictions(_PRIOR_SQL_WITH_TOP_3_RANKING, new_sql) == []


def test_does_not_flag_a_deliberate_single_value_narrowing():
    # "What about just Germany specifically" - a valid, different kind of
    # follow-up, not the ranking-dropped failure shape.
    new_sql = "SELECT month, SUM(revenue) FROM v_orders WHERE country = 'Germany' GROUP BY month"
    assert find_dropped_ranking_restrictions(_PRIOR_SQL_WITH_TOP_3_RANKING, new_sql) == []


def test_does_not_flag_when_the_column_is_not_referenced_at_all():
    # Deliberately conservative: a genuinely broader follow-up ("show me
    # every country instead") looks identical to this from the SQL alone -
    # not flagging avoids forcing a valid question into a retry loop.
    new_sql = "SELECT month, SUM(revenue) FROM v_orders GROUP BY month"
    assert find_dropped_ranking_restrictions(_PRIOR_SQL_WITH_TOP_3_RANKING, new_sql) == []


def test_unparseable_new_sql_does_not_raise_for_ranking_check():
    find_dropped_ranking_restrictions(_PRIOR_SQL_WITH_TOP_3_RANKING, "SELECT FROM WHERE (((")
