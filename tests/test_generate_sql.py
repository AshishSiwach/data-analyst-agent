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

from data_analyst_agent.agent.generate_sql import VIEWS, GeneratedSql, generate_sql
from data_analyst_agent.data.ingest import DEFAULT_DB_PATH
from data_analyst_agent.data.metrics import METRICS

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


class _FakeMessage:
    def __init__(self, parsed: GeneratedSql):
        self.parsed = parsed


class _FakeChoice:
    def __init__(self, parsed: GeneratedSql):
        self.message = _FakeMessage(parsed)


class _FakeCompletion:
    def __init__(self, parsed: GeneratedSql):
        self.choices = [_FakeChoice(parsed)]


class _FakeCompletionsAPI:
    def __init__(self, fixed_sql: str):
        self.fixed_sql = fixed_sql
        self.captured_kwargs: dict | None = None

    def parse(self, **kwargs):
        self.captured_kwargs = kwargs
        return _FakeCompletion(GeneratedSql(sql=self.fixed_sql))


class _FakeChatAPI:
    def __init__(self, fixed_sql: str):
        self.completions = _FakeCompletionsAPI(fixed_sql)


class _FakeClient:
    def __init__(self, fixed_sql: str = "SELECT 1"):
        self.chat = _FakeChatAPI(fixed_sql)


def test_generate_sql_extracts_and_returns_the_sql_string():
    fake_client = _FakeClient(fixed_sql="SELECT COUNT(*) FROM v_orders")
    result = generate_sql("How many orders are there?", prior_error=None, client=fake_client)
    assert result == "SELECT COUNT(*) FROM v_orders"


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
