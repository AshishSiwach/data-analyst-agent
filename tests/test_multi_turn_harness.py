"""Acceptance tests for eval.multi_turn_harness.run_multi_turn_harness,
against small synthetic gold conversations with a mocked agent (same
agent.orchestrator boundary tests/test_harness.py mocks at, so the
harness's real audit-log-based attempts/agent_sql recovery is exercised
for real)."""

from __future__ import annotations

from unittest.mock import patch

from data_analyst_agent.eval.multi_turn_harness import run_multi_turn_harness
from data_analyst_agent.models.entities import (
    ColumnSpec,
    GoldConversation,
    GoldConversationTurn,
    NarrativeWrap,
    ResultData,
    SqlAttempt,
    SqlExecutionResult,
    SqlRetryOutcome,
)

_NARRATIVE = NarrativeWrap(answer_text="You have 52,612 orders.", assumption_disclosed=None)


def _write_gold(gold_dir, filename: str, conversations: list[GoldConversation]) -> None:
    gold_dir.mkdir(parents=True, exist_ok=True)
    path = gold_dir / filename
    with open(path, "w", encoding="utf-8") as f:
        for c in conversations:
            f.write(c.model_dump_json() + "\n")


def _turn(question_text: str, expected_rows: list) -> GoldConversationTurn:
    return GoldConversationTurn(
        question_text=question_text,
        gold_sql="SELECT COUNT(*) FROM v_orders",
        gold_result=ResultData(
            columns=[ColumnSpec(name="order_count", type="BIGINT")], rows=expected_rows
        ),
        is_graceful_failure_case=False,
    )


def _success_side_effect(rows):
    def run_turn_sql_side_effect(
        question, session, turn_id=None, db_path=None, client=None, **kwargs
    ):
        attempt = SqlAttempt(
            turn_id=turn_id,
            attempt_number=1,
            query_text="SELECT COUNT(*) FROM v_orders",
            status="success",
            error_message=None,
            execution_ms=5,
            row_count=1,
            truncated=False,
        )
        result = SqlExecutionResult(
            status="success",
            columns=[ColumnSpec(name="order_count", type="BIGINT")],
            rows=rows,
            row_count=1,
            truncated=False,
            execution_ms=5,
        )
        return SqlRetryOutcome(status="success", result=result, attempts=[attempt])

    return run_turn_sql_side_effect


@patch("data_analyst_agent.agent.orchestrator.wrap")
@patch("data_analyst_agent.agent.orchestrator.run_turn_sql")
def test_every_turn_of_a_correctly_answered_conversation_passes(
    mock_run_turn_sql, mock_wrap, tmp_path
):
    mock_run_turn_sql.side_effect = _success_side_effect([[52612]])
    mock_wrap.return_value = _NARRATIVE

    gold_dir = tmp_path / "gold"
    conversation = GoldConversation(
        conversation_id="conv_001",
        scenario="follow_up_resolution",
        turns=[_turn("How many orders?", [[52612]]), _turn("And again?", [[52612]])],
    )
    _write_gold(gold_dir, "conversations.jsonl", [conversation])

    eval_run, results = run_multi_turn_harness(gold_dir, tmp_path / "out")

    assert len(results) == 2
    assert all(r.passed for r in results)
    assert eval_run.turn_accuracy == 1.0
    assert eval_run.per_scenario_accuracy == {"follow_up_resolution": 1.0}
    assert eval_run.conversations_fully_passed == 1.0
    assert (tmp_path / "out" / "report.json").exists()
    assert (tmp_path / "out" / "report.md").exists()


@patch("data_analyst_agent.agent.orchestrator.wrap")
@patch("data_analyst_agent.agent.orchestrator.run_turn_sql")
def test_one_wrong_turn_does_not_stop_later_turns_from_being_graded(
    mock_run_turn_sql, mock_wrap, tmp_path
):
    # A wrong turn 1 (compare() fails) must not prevent turn 2 from
    # running and being graded on its own merits - real conversations
    # keep going after a bad turn.
    mock_run_turn_sql.side_effect = _success_side_effect([[999999]])  # always wrong vs gold
    mock_wrap.return_value = _NARRATIVE

    gold_dir = tmp_path / "gold"
    conversation = GoldConversation(
        conversation_id="conv_001",
        scenario="entity_list_carryover",
        turns=[_turn("How many orders?", [[52612]]), _turn("And in Germany?", [[100]])],
    )
    _write_gold(gold_dir, "conversations.jsonl", [conversation])

    eval_run, results = run_multi_turn_harness(gold_dir, tmp_path / "out")

    assert len(results) == 2  # both turns graded, despite both failing
    assert all(not r.passed for r in results)
    assert results[0].turn_index == 0
    assert results[1].turn_index == 1
    assert eval_run.turn_accuracy == 0.0


@patch("data_analyst_agent.agent.orchestrator.wrap")
@patch("data_analyst_agent.agent.orchestrator.run_turn_sql")
def test_each_conversation_gets_a_fresh_session_and_memory(mock_run_turn_sql, mock_wrap, tmp_path):
    # Two independent conversations must not leak state into each other -
    # each gets its own SessionState and ConversationMemory.
    seen_session_ids = []

    def run_turn_sql_side_effect(
        question, session, turn_id=None, db_path=None, client=None, **kwargs
    ):
        seen_session_ids.append(session.session_id)
        attempt = SqlAttempt(
            turn_id=turn_id,
            attempt_number=1,
            query_text="SELECT COUNT(*) FROM v_orders",
            status="success",
            error_message=None,
            execution_ms=5,
            row_count=1,
            truncated=False,
        )
        result = SqlExecutionResult(
            status="success",
            columns=[ColumnSpec(name="order_count", type="BIGINT")],
            rows=[[52612]],
            row_count=1,
            truncated=False,
            execution_ms=5,
        )
        return SqlRetryOutcome(status="success", result=result, attempts=[attempt])

    mock_run_turn_sql.side_effect = run_turn_sql_side_effect
    mock_wrap.return_value = _NARRATIVE

    gold_dir = tmp_path / "gold"
    conversations = [
        GoldConversation(conversation_id="conv_a", scenario="s1", turns=[_turn("Q?", [[52612]])]),
        GoldConversation(conversation_id="conv_b", scenario="s2", turns=[_turn("Q?", [[52612]])]),
    ]
    _write_gold(gold_dir, "conversations.jsonl", conversations)

    eval_run, results = run_multi_turn_harness(gold_dir, tmp_path / "out")

    assert len(set(seen_session_ids)) == 2  # each conversation got its own session
    assert eval_run.per_scenario_accuracy == {"s1": 1.0, "s2": 1.0}
    assert eval_run.conversations_fully_passed == 1.0


@patch("data_analyst_agent.agent.orchestrator.wrap")
@patch("data_analyst_agent.agent.orchestrator.run_turn_sql")
def test_conversations_fully_passed_excludes_a_conversation_with_any_failing_turn(
    mock_run_turn_sql, mock_wrap, tmp_path
):
    mock_run_turn_sql.side_effect = _success_side_effect([[52612]])
    mock_wrap.return_value = _NARRATIVE

    gold_dir = tmp_path / "gold"
    conversations = [
        GoldConversation(
            conversation_id="conv_a",
            scenario="s1",
            turns=[_turn("Q1?", [[52612]]), _turn("Q2?", [[52612]])],  # both pass
        ),
        GoldConversation(
            conversation_id="conv_b",
            scenario="s1",
            turns=[_turn("Q1?", [[52612]]), _turn("Q2?", [[999999]])],  # second turn fails
        ),
    ]
    _write_gold(gold_dir, "conversations.jsonl", conversations)

    eval_run, results = run_multi_turn_harness(gold_dir, tmp_path / "out")

    assert len(results) == 4
    assert eval_run.conversations_fully_passed == 0.5  # only conv_a fully passed
    assert eval_run.turn_accuracy == 0.75  # 3 of 4 turns passed


def test_load_gold_conversations_reads_every_jsonl_file_in_the_directory(tmp_path):
    from data_analyst_agent.eval.multi_turn_harness import load_gold_conversations

    gold_dir = tmp_path / "gold"
    _write_gold(
        gold_dir,
        "conversations.jsonl",
        [GoldConversation(conversation_id="c1", scenario="s1", turns=[_turn("Q?", [[1]])])],
    )

    conversations = load_gold_conversations(gold_dir)

    assert {c.conversation_id for c in conversations} == {"c1"}
