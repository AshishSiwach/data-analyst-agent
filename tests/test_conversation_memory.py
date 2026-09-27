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
