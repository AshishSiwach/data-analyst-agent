"""S19 acceptance tests for agent.generate_sql (mocked LLM call).

Schema introspection needs the real, already-built views (context
assembly is deterministic and reads the live DB, per Architecture.md);
only the LLM call itself is mocked here - the live smoke test
(`python -m data_analyst_agent.agent.generate_sql --smoke-test`) is the
required, separate, unmocked evaluation case.
"""

from __future__ import annotations

import os
from pathlib import Path

import duckdb
import pytest

from data_analyst_agent.agent import audit_log
from data_analyst_agent.agent.generate_sql import VIEWS, GeneratedSql, generate_sql
from data_analyst_agent.data.ingest import DEFAULT_DB_PATH
from data_analyst_agent.data.metrics import METRICS
from data_analyst_agent.models.entities import ConversationMemory, ConversationTurn

_DEFAULT_DB_PATH = Path(os.environ.get("DUCKDB_PATH", str(DEFAULT_DB_PATH)))


def _table_exists(con: duckdb.DuckDBPyConnection, table: str) -> bool:
    (count,) = con.execute(
        "SELECT COUNT(*) FROM information_schema.tables WHERE table_name = ?", [table]
    ).fetchone()
    return count > 0


@pytest.fixture(scope="module", autouse=True)
def _require_real_views():
    if not _DEFAULT_DB_PATH.exists():
        pytest.skip(f"{_DEFAULT_DB_PATH} does not exist - build the full pipeline first")
    con = duckdb.connect(str(_DEFAULT_DB_PATH), read_only=True)
    try:
        for view in VIEWS:
            if not _table_exists(con, view):
                pytest.skip(f"{view} missing - build this slice's dependencies first")
    finally:
        con.close()


@pytest.fixture(autouse=True)
def _llm_call_log_to_tmp(tmp_path, monkeypatch):
    # generate_sql() now logs every call to llm_calls.jsonl (monitoring
    # dashboard); redirect the default path so these tests don't append to
    # the real repo-root file every time the suite runs.
    monkeypatch.setattr(audit_log, "LLM_CALL_LOG_PATH", tmp_path / "llm_calls.jsonl")


class _FakeUsage:
    def __init__(self, prompt_tokens: int = 100, completion_tokens: int = 20):
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens


class _FakeMessage:
    def __init__(self, parsed: GeneratedSql):
        self.parsed = parsed


class _FakeChoice:
    def __init__(self, parsed: GeneratedSql):
        self.message = _FakeMessage(parsed)


class _FakeCompletion:
    def __init__(self, parsed: GeneratedSql):
        self.choices = [_FakeChoice(parsed)]
        self.usage = _FakeUsage()


class _FakeCompletionsAPI:
    def __init__(self, parsed: GeneratedSql):
        self.parsed = parsed
        self.captured_kwargs: dict | None = None

    def parse(self, **kwargs):
        self.captured_kwargs = kwargs
        return _FakeCompletion(self.parsed)


class _FakeChatAPI:
    def __init__(self, parsed: GeneratedSql):
        self.completions = _FakeCompletionsAPI(parsed)


class _FakeClient:
    def __init__(self, fixed_sql: str = "SELECT 1", parsed: GeneratedSql | None = None):
        self.chat = _FakeChatAPI(parsed or GeneratedSql(can_answer_from_schema=True, sql=fixed_sql))


def test_generate_sql_extracts_and_returns_the_sql_string():
    fake_client = _FakeClient(fixed_sql="SELECT COUNT(*) FROM v_orders")
    result = generate_sql("How many orders are there?", prior_error=None, client=fake_client)
    assert result.can_answer_from_schema is True
    assert result.sql == "SELECT COUNT(*) FROM v_orders"


def test_generate_sql_returns_a_declined_result_as_is():
    declined = GeneratedSql(
        can_answer_from_schema=False,
        reason="Email open rate isn't tracked anywhere in this dataset.",
    )
    fake_client = _FakeClient(parsed=declined)
    result = generate_sql("What's our email open rate?", prior_error=None, client=fake_client)
    assert result.can_answer_from_schema is False
    assert result.sql is None
    assert result.reason == declined.reason


def test_prompt_includes_full_metric_dictionary():
    fake_client = _FakeClient()
    generate_sql("How many orders are there?", prior_error=None, client=fake_client)
    system_message = _captured_system_message(fake_client)

    for metric in METRICS:
        assert metric.metric_id in system_message
        assert metric.sql_fragment in system_message


def test_prompt_includes_all_five_view_schemas():
    fake_client = _FakeClient()
    generate_sql("How many orders are there?", prior_error=None, client=fake_client)
    system_message = _captured_system_message(fake_client)

    for view in VIEWS:
        assert view in system_message
    # Spot-check real column names, not just the view names - this is what
    # would catch a stale/hardcoded schema summary drifting from reality.
    assert "total_revenue" in system_message  # v_products' real column
    assert "net_revenue" in system_message  # v_orders' real column
    assert "is_return" in system_message  # v_order_lines' real column


def test_prior_error_is_included_in_the_user_message():
    fake_client = _FakeClient()
    error_text = 'Binder Error: Referenced column "revenue" not found in FROM clause!'
    generate_sql(
        "Which products are the least performing?",
        prior_error=error_text,
        client=fake_client,
    )
    kwargs = fake_client.chat.completions.captured_kwargs
    user_message = next(m["content"] for m in kwargs["messages"] if m["role"] == "user")
    assert error_text in user_message


def test_no_prior_error_omits_retry_language_from_user_message():
    fake_client = _FakeClient()
    generate_sql("How many orders are there?", prior_error=None, client=fake_client)
    kwargs = fake_client.chat.completions.captured_kwargs
    user_message = next(m["content"] for m in kwargs["messages"] if m["role"] == "user")
    assert "previous attempt" not in user_message


def test_response_format_is_generated_sql_model():
    fake_client = _FakeClient()
    generate_sql("How many orders are there?", prior_error=None, client=fake_client)
    kwargs = fake_client.chat.completions.captured_kwargs
    assert kwargs["response_format"] is GeneratedSql


def _captured_system_message(fake_client: _FakeClient) -> str:
    kwargs = fake_client.chat.completions.captured_kwargs
    return next(m["content"] for m in kwargs["messages"] if m["role"] == "system")


def test_no_conversation_memory_omits_conversation_section_entirely():
    # The real regression guard for existing single-turn callers (including
    # the eval harness, which never passes conversation_memory): the
    # rendered *block* must be completely absent, not present-but-empty.
    # Checking for "Conversation so far" alone isn't discriminating enough -
    # the domain-knowledge rule itself now refers to that section by name -
    # so this checks for build_context_block's own framing text instead,
    # which only appears when it actually renders a populated block.
    fake_client = _FakeClient()
    generate_sql("How many orders are there?", prior_error=None, client=fake_client)
    system_message = _captured_system_message(fake_client)
    assert "for reference only" not in system_message
    assert "log of prior questions and answers" not in system_message


def test_conversation_memory_appears_in_the_system_prompt_when_given():
    fake_client = _FakeClient()
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
    generate_sql(
        "What about Germany?", prior_error=None, client=fake_client, conversation_memory=memory
    )
    system_message = _captured_system_message(fake_client)
    assert "Conversation so far" in system_message
    assert "Q3 2011 UK revenue" in system_message
    assert "£1.2M" in system_message
