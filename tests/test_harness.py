"""S25 acceptance tests for eval.harness.run_harness, against a small
synthetic gold set with a mocked agent (mocked at the same
agent.orchestrator boundary S24's own tests use, so the harness's real
audit-log-based attempts/agent_sql recovery is exercised for real - not
bypassed by mocking answer_question itself)."""

from __future__ import annotations

from unittest.mock import patch

from data_analyst_agent.eval.harness import run_harness
from data_analyst_agent.models.entities import (
    ColumnSpec,
    FailureDiagnosis,
    GoldQuestion,
    NarrativeWrap,
    ResultData,
    SqlAttempt,
    SqlExecutionResult,
    SqlRetryOutcome,
)

_NARRATIVE = NarrativeWrap(answer_text="You have 52,612 orders.", assumption_disclosed=None)
_DIAGNOSIS = FailureDiagnosis(
    category="schema_mismatch",
    explanation="Email open rate isn't tracked anywhere in this dataset.",
    rephrase_suggestion="Try asking about order or revenue metrics instead.",
)


def _write_gold(gold_dir, filename: str, questions: list[GoldQuestion]) -> None:
    gold_dir.mkdir(parents=True, exist_ok=True)
    path = gold_dir / filename
    with open(path, "w", encoding="utf-8") as f:
        for q in questions:
            f.write(q.model_dump_json() + "\n")


def _normal_question(question_id: str) -> GoldQuestion:
    return GoldQuestion(
        question_id=question_id,
        question_text="How many orders are there?",
        bucket="basic",
        gold_sql="SELECT COUNT(*) FROM v_orders",
        gold_result=ResultData(
            columns=[ColumnSpec(name="order_count", type="BIGINT")], rows=[[52612]]
        ),
        is_graceful_failure_case=False,
    )


def _graceful_failure_question(question_id: str) -> GoldQuestion:
    return GoldQuestion(
        question_id=question_id,
        question_text="What's our email open rate?",
        bucket="adversarial",
        is_graceful_failure_case=True,
        expected_failure_category="schema_mismatch",
    )


@patch("data_analyst_agent.agent.orchestrator.wrap")
@patch("data_analyst_agent.agent.orchestrator.run_turn_sql")
def test_agent_that_always_succeeds_on_attempt_one_produces_full_accuracy_and_a_1s_histogram(
    mock_run_turn_sql, mock_wrap, tmp_path
):
    def run_turn_sql_side_effect(question, session, turn_id=None, db_path=None, client=None):
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
    _write_gold(
        gold_dir, "basic.jsonl", [_normal_question("synth_001"), _normal_question("synth_002")]
    )

    eval_run, results = run_harness(gold_dir, tmp_path / "out")

    assert eval_run.per_bucket_accuracy["basic"] == 1.0
    assert eval_run.attempts_until_success_distribution == {1: 2}
    assert all(r.passed for r in results)
    assert (tmp_path / "out" / "report.json").exists()
    assert (tmp_path / "out" / "report.md").exists()


@patch("data_analyst_agent.agent.orchestrator.diagnose")
@patch("data_analyst_agent.agent.orchestrator.run_turn_sql")
def test_agent_that_always_exhausts_produces_zero_accuracy_and_populated_gf_rate(
    mock_run_turn_sql, mock_diagnose, tmp_path
):
    def run_turn_sql_side_effect(question, session, turn_id=None, db_path=None, client=None):
        attempts = [
            SqlAttempt(
                turn_id=turn_id,
                attempt_number=n,
                query_text=f"SELECT bogus_col_{n} FROM v_orders",
                status="error",
                error_message="Binder Error: column not found",
                execution_ms=5,
                row_count=None,
                truncated=False,
            )
            for n in (1, 2, 3)
        ]
        result = SqlExecutionResult(
            status="error", error_message="boom", truncated=False, execution_ms=5
        )
        return SqlRetryOutcome(status="exhausted", result=result, attempts=attempts)

    mock_run_turn_sql.side_effect = run_turn_sql_side_effect
    mock_diagnose.return_value = _DIAGNOSIS

    gold_dir = tmp_path / "gold"
    _write_gold(
        gold_dir, "basic.jsonl", [_normal_question("synth_001"), _normal_question("synth_002")]
    )
    _write_gold(gold_dir, "adversarial.jsonl", [_graceful_failure_question("synth_003")])

    eval_run, results = run_harness(gold_dir, tmp_path / "out")

    assert eval_run.per_bucket_accuracy["basic"] == 0.0
    basic_results = [r for r in results if r.question_id in ("synth_001", "synth_002")]
    assert all(not r.passed for r in basic_results)
    assert all(r.attempts_used == 3 for r in basic_results)

    # "Populated" graceful-failure-rate: the graceful-failure-designed
    # question also exhausted, which is a correctly-shaped failure for
    # that row (per scope.md, graded on shape, not on category match), so
    # the rate is a real, non-empty computed value, not the 0.0 default
    # from an empty is_graceful_failure_case subset.
    assert eval_run.graceful_failure_rate == 1.0
    gf_result = next(r for r in results if r.question_id == "synth_003")
    assert gf_result.passed is True


@patch("data_analyst_agent.agent.orchestrator.wrap")
@patch("data_analyst_agent.agent.orchestrator.run_turn_sql")
def test_a_wrong_but_successfully_executed_answer_still_counts_in_the_attempts_histogram(
    mock_run_turn_sql, mock_wrap, tmp_path
):
    # Regression test: found live during S25's required 60-question
    # evaluation - a question whose SQL executed cleanly but returned the
    # wrong data (compare() fails) was silently dropped from
    # attempts_until_success_distribution entirely, because the histogram
    # was gated on eval-graded `passed` rather than on the retry loop's
    # own "did it execute" success signal. This is a distinct failure
    # mode from "never got a working query" and must still show up here.
    def run_turn_sql_side_effect(question, session, turn_id=None, db_path=None, client=None):
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
            rows=[[999999]],  # wrong - gold expects 52612
            row_count=1,
            truncated=False,
            execution_ms=5,
        )
        return SqlRetryOutcome(status="success", result=result, attempts=[attempt])

    mock_run_turn_sql.side_effect = run_turn_sql_side_effect
    mock_wrap.return_value = _NARRATIVE

    gold_dir = tmp_path / "gold"
    _write_gold(gold_dir, "basic.jsonl", [_normal_question("synth_001")])

    eval_run, results = run_harness(gold_dir, tmp_path / "out")

    assert eval_run.per_bucket_accuracy["basic"] == 0.0
    assert results[0].passed is False
    assert eval_run.attempts_until_success_distribution == {1: 1}


def test_load_gold_questions_reads_every_jsonl_file_in_the_directory(tmp_path):
    from data_analyst_agent.eval.harness import load_gold_questions

    gold_dir = tmp_path / "gold"
    _write_gold(gold_dir, "basic.jsonl", [_normal_question("b1")])
    _write_gold(gold_dir, "adversarial.jsonl", [_graceful_failure_question("a1")])

    questions = load_gold_questions(gold_dir)

    assert {q.question_id for q in questions} == {"b1", "a1"}
