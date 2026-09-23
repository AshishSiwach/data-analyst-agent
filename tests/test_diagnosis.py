"""S23 acceptance tests for agent.diagnosis.diagnose (mocked LLM call)."""

from __future__ import annotations

import os
from pathlib import Path

import duckdb
import pytest

from data_analyst_agent.agent.diagnosis import diagnose
from data_analyst_agent.data.ingest import DEFAULT_DB_PATH
from data_analyst_agent.models.entities import FailureDiagnosis, SqlAttempt

_DEFAULT_DB_PATH = Path(os.environ.get("DUCKDB_PATH", str(DEFAULT_DB_PATH)))


def _table_exists(con: duckdb.DuckDBPyConnection, table: str) -> bool:
    (count,) = con.execute(
        "SELECT COUNT(*) FROM information_schema.tables WHERE table_name = ?", [table]
    ).fetchone()
    return count > 0


@pytest.fixture(scope="module", autouse=True)
def _require_real_views():
    # build_schema_summary introspects the live views.
    if not _DEFAULT_DB_PATH.exists():
        pytest.skip(f"{_DEFAULT_DB_PATH} does not exist - build the full pipeline first")
    con = duckdb.connect(str(_DEFAULT_DB_PATH), read_only=True)
    try:
        for view in ["v_orders", "v_order_lines", "v_customers", "v_products", "v_daily_revenue"]:
            if not _table_exists(con, view):
                pytest.skip(f"{view} missing - build this slice's dependencies first")
    finally:
        con.close()


def _attempt(**overrides) -> SqlAttempt:
    base = dict(
        turn_id="turn-1",
        attempt_number=1,
        query_text="SELECT nonexistent_col FROM v_products",
        status="error",
        error_message='Binder Error: column "nonexistent_col" not found',
        execution_ms=5,
        row_count=None,
        truncated=False,
    )
    base.update(overrides)
    return SqlAttempt(**base)


class _FakeMessage:
    def __init__(self, parsed):
        self.parsed = parsed


class _FakeChoice:
    def __init__(self, parsed):
        self.message = _FakeMessage(parsed)


class _FakeCompletion:
    def __init__(self, parsed):
        self.choices = [_FakeChoice(parsed)]


class _FakeCompletionsAPI:
    def __init__(self, diagnosis: FailureDiagnosis):
        self._diagnosis = diagnosis
        self.captured_kwargs = None

    def parse(self, **kwargs):
        self.captured_kwargs = kwargs
        return _FakeCompletion(self._diagnosis)


class _FakeChatAPI:
    def __init__(self, diagnosis: FailureDiagnosis):
        self.completions = _FakeCompletionsAPI(diagnosis)


class _FakeClient:
    def __init__(self, diagnosis: FailureDiagnosis | None = None):
        self.chat = _FakeChatAPI(
            diagnosis
            or FailureDiagnosis(
                category="bug",
                explanation="placeholder",
                rephrase_suggestion="placeholder",
            )
        )


def test_diagnose_extracts_fields_correctly_from_a_fixed_mock_response():
    fixed = FailureDiagnosis(
        category="schema_mismatch",
        explanation="Email open rate isn't tracked anywhere in this dataset.",
        rephrase_suggestion="Try asking about order or revenue metrics instead.",
    )
    fake_client = _FakeClient(diagnosis=fixed)

    result = diagnose("What's our email open rate?", [], client=fake_client)

    assert result.category == "schema_mismatch"
    assert result.explanation == fixed.explanation
    assert result.rephrase_suggestion == fixed.rephrase_suggestion


def test_diagnose_passes_the_attempt_trace_into_the_prompt():
    fake_client = _FakeClient()
    attempts = [
        _attempt(attempt_number=1, error_message="error one"),
        _attempt(attempt_number=2, error_message="error two"),
    ]
    diagnose("Which products are least performing?", attempts, client=fake_client)

    kwargs = fake_client.chat.completions.captured_kwargs
    user_message = next(m["content"] for m in kwargs["messages"] if m["role"] == "user")
    assert "error one" in user_message
    assert "error two" in user_message
    assert "Which products are least performing?" in user_message


def test_diagnose_handles_a_zero_attempt_fast_fail_trace():
    fake_client = _FakeClient()
    diagnose("How many orders came from Scotland?", [], client=fake_client)

    kwargs = fake_client.chat.completions.captured_kwargs
    user_message = next(m["content"] for m in kwargs["messages"] if m["role"] == "user")
    assert "already failed 3/3" in user_message


def test_diagnose_prompt_includes_the_schema():
    fake_client = _FakeClient()
    diagnose("How many orders came from Scotland?", [], client=fake_client)

    kwargs = fake_client.chat.completions.captured_kwargs
    system_message = next(m["content"] for m in kwargs["messages"] if m["role"] == "system")
    for view in ["v_orders", "v_order_lines", "v_customers", "v_products", "v_daily_revenue"]:
        assert view in system_message


def test_response_format_is_failure_diagnosis_model():
    fake_client = _FakeClient()
    diagnose("How many orders came from Scotland?", [], client=fake_client)
    kwargs = fake_client.chat.completions.captured_kwargs
    assert kwargs["response_format"] is FailureDiagnosis
