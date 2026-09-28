"""Multi-turn counterpart to `eval/harness.py`, added post-v1 as the
follow-up `_docs/phase2_memory.md` named explicitly: "A proper multi-turn
gold-set and harness extension (a structurally different shape - sequences
of questions sharing one memory object, not today's flat independent-
question rows) is a separate, later follow-up." That follow-up.

Every multi-turn regression found in this project so far (the France-leak
entity-list drop, the dropped date filter, the dropped ranking restriction,
the `NarrativeGuardrailViolation` crash, the sawtooth chart, the
`find_dropped_date_filters` false positive, the unbounded per-group
ranking) was found only through ad hoc live testing, never by an automated
check - `eval/harness.py`'s own 60-question set never passes
`conversation_memory` to `answer_question` at all. This module runs a
small, hand-authored set of multi-turn `GoldConversation`s, each against
one real, shared `ConversationMemory`, grading every turn in sequence with
the exact same `grade_turn` core `eval/harness.py` uses for its own
single-turn rows - not a parallel grading engine, per `CLAUDE.md`'s "no
general eval framework... the comparator's execute-and-compare logic is
this project's actual differentiator" stance.

Deliberately kept structurally separate from `eval/harness.py`'s own
`GoldQuestion`/`EvalResult`/`EvalRun`/`GoldBucket` types rather than
extending them - `GoldBucket` is a fixed 3-value Literal used to type
`EvalRun.per_bucket_accuracy`, and "scenario" here is an open-ended,
free-text label expected to grow, not a closed enum with the same shape.
"""

from __future__ import annotations

import argparse
import json
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from openai import OpenAI

from data_analyst_agent.agent.orchestrator import answer_question
from data_analyst_agent.agent.session import SessionState
from data_analyst_agent.eval.harness import (
    _git_commit_hash,
    _prompt_hash,
    attempt_records_for_turn,
    grade_turn,
)
from data_analyst_agent.models.entities import (
    ConversationMemory,
    GoldConversation,
    MultiTurnEvalResult,
    MultiTurnEvalRun,
)


def load_gold_conversations(gold_dir: Path | str) -> list[GoldConversation]:
    """Loads every `GoldConversation` from every `*.jsonl` file in
    `gold_dir` - one full conversation (its whole turn list embedded) per
    line, unlike `harness.py::load_gold_questions`'s one-row-per-line
    flat shape. Starts as a single `conversations.jsonl` file; splitting
    by scenario into multiple files later needs no code change."""
    conversations: list[GoldConversation] = []
    for path in sorted(Path(gold_dir).glob("*.jsonl")):
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    conversations.append(GoldConversation.model_validate_json(line))
    return conversations


def run_multi_turn_harness(
    gold_dir: Path | str,
    out_dir: Path | str,
    db_path: Path | str | None = None,
    client: OpenAI | None = None,
) -> tuple[MultiTurnEvalRun, list[MultiTurnEvalResult]]:
    """Runs every gold conversation in `gold_dir` turn by turn through
    `answer_question`, sharing one `SessionState` and one
    `ConversationMemory` across all of a conversation's turns (mirrors a
    single browser session/tab, and matches exactly how `answer_question`
    already expects to be called - it mutates both in place). Grades each
    turn independently via `grade_turn`; a failing turn does not stop
    later turns in the same conversation from running, since real
    conversations keep going after a bad turn and a later turn failing
    *because* an earlier one did is itself useful signal, not something to
    hide by stopping early. Writes `report.json`/`report.md` into
    `out_dir`, same two-file shape as `harness.py`."""
    conversations = load_gold_conversations(gold_dir)
    run_id = str(uuid.uuid4())

    out_path = Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)
    audit_log_path = out_path / "eval_query_audit.jsonl"
    failure_log_path = out_path / "eval_failures.jsonl"
    llm_call_log_path = out_path / "eval_llm_calls.jsonl"
    audit_log_path.unlink(missing_ok=True)
    failure_log_path.unlink(missing_ok=True)
    llm_call_log_path.unlink(missing_ok=True)

    results: list[MultiTurnEvalResult] = []
    for conversation in conversations:
        session = SessionState(
            session_id=f"eval-{conversation.conversation_id}",
            started_at=datetime.now(timezone.utc),
            cost_spent_usd=Decimal("0"),
            cost_cap_usd=Decimal("0.50"),
        )
        memory = ConversationMemory()

        for turn_index, turn in enumerate(conversation.turns):
            answer = answer_question(
                turn.question_text,
                session,
                db_path=db_path,
                client=client,
                query_audit_log_path=audit_log_path,
                failure_log_path=failure_log_path,
                llm_call_log_path=llm_call_log_path,
                conversation_memory=memory,
            )
            records = attempt_records_for_turn(audit_log_path, answer.turn_id)
            attempts_used = len(records)
            agent_sql = records[-1].query_text if records else ""

            passed, agent_result, diff = grade_turn(
                turn.gold_sql, turn.gold_result, turn.is_graceful_failure_case, answer
            )
            results.append(
                MultiTurnEvalResult(
                    run_id=run_id,
                    conversation_id=conversation.conversation_id,
                    scenario=conversation.scenario,
                    turn_index=turn_index,
                    passed=passed,
                    agent_sql=agent_sql,
                    agent_result=agent_result,
                    diff=diff,
                    attempts_used=attempts_used,
                )
            )

    eval_run = _summarize(run_id, conversations, results)
    _write_reports(out_path, eval_run, results)
    return eval_run, results


def _summarize(
    run_id: str, conversations: list[GoldConversation], results: list[MultiTurnEvalResult]
) -> MultiTurnEvalRun:
    turn_accuracy = sum(r.passed for r in results) / len(results) if results else 0.0

    per_scenario_accuracy: dict[str, float] = {}
    for scenario in sorted({c.scenario for c in conversations}):
        scenario_results = [r for r in results if r.scenario == scenario]
        if scenario_results:
            per_scenario_accuracy[scenario] = sum(r.passed for r in scenario_results) / len(
                scenario_results
            )

    by_conversation: dict[str, list[MultiTurnEvalResult]] = {}
    for r in results:
        by_conversation.setdefault(r.conversation_id, []).append(r)
    fully_passed = sum(1 for turns in by_conversation.values() if all(r.passed for r in turns))
    conversations_fully_passed = fully_passed / len(by_conversation) if by_conversation else 0.0

    attempts_histogram: dict[int, int] = {}
    by_id = {(c.conversation_id, i): t for c in conversations for i, t in enumerate(c.turns)}
    for r in results:
        turn = by_id[(r.conversation_id, r.turn_index)]
        # Same reasoning as harness.py::_summarize: "attempts until
        # success" is the retry loop's own notion (the SQL executed
        # cleanly), independent of whether grade_turn then passed -
        # gating this on r.passed would drop a turn that executed cleanly
        # but returned the wrong data from the histogram entirely.
        if not turn.is_graceful_failure_case and r.agent_result is not None:
            attempts_histogram[r.attempts_used] = attempts_histogram.get(r.attempts_used, 0) + 1

    return MultiTurnEvalRun(
        run_id=run_id,
        git_commit_hash=_git_commit_hash(),
        prompt_hash=_prompt_hash(),
        timestamp=datetime.now(timezone.utc),
        turn_accuracy=turn_accuracy,
        per_scenario_accuracy=per_scenario_accuracy,
        conversations_fully_passed=conversations_fully_passed,
        attempts_until_success_distribution=attempts_histogram,
    )


def _write_reports(
    out_dir: Path, eval_run: MultiTurnEvalRun, results: list[MultiTurnEvalResult]
) -> None:
    report = {
        "run": json.loads(eval_run.model_dump_json()),
        "results": [json.loads(r.model_dump_json()) for r in results],
    }
    (out_dir / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    (out_dir / "report.md").write_text(_render_markdown(eval_run, results), encoding="utf-8")


def _render_markdown(eval_run: MultiTurnEvalRun, results: list[MultiTurnEvalResult]) -> str:
    lines = [
        "# Multi-Turn Evaluation Report",
        "",
        f"- Run ID: `{eval_run.run_id}`",
        f"- Git commit: `{eval_run.git_commit_hash}`",
        f"- Prompt hash: `{eval_run.prompt_hash}`",
        f"- Timestamp: {eval_run.timestamp.isoformat()}",
        "",
        f"## Turn accuracy: {eval_run.turn_accuracy:.1%}",
        "",
        f"## Conversations fully passed: {eval_run.conversations_fully_passed:.1%}",
        "",
        "## Per-scenario accuracy",
        "",
        "| Scenario | Accuracy |",
        "|---|---|",
    ]
    for scenario, accuracy in sorted(eval_run.per_scenario_accuracy.items()):
        lines.append(f"| {scenario} | {accuracy:.1%} |")

    lines += [
        "",
        "## Attempts-until-success distribution",
        "",
        "| Attempts | Count |",
        "|---|---|",
    ]
    for attempts, count in sorted(eval_run.attempts_until_success_distribution.items()):
        lines.append(f"| {attempts} | {count} |")

    failed = [r for r in results if not r.passed]
    if failed:
        lines += [
            "",
            "## Failed turns",
            "",
            "| Conversation ID | Turn | Scenario | Diff |",
            "|---|---|---|---|",
        ]
        for r in failed:
            diff_text = (r.diff or "").replace("|", "\\|").replace("\n", " ")
            lines.append(f"| {r.conversation_id} | {r.turn_index} | {r.scenario} | {diff_text} |")

    return "\n".join(lines) + "\n"


def _main() -> None:
    from dotenv import load_dotenv

    load_dotenv()

    parser = argparse.ArgumentParser(description="Run the multi-turn gold-set evaluation harness.")
    parser.add_argument(
        "--gold", required=True, help="Directory of gold *.jsonl conversation files"
    )
    parser.add_argument("--out", required=True, help="Directory to write report.json/report.md")
    args = parser.parse_args()

    eval_run, results = run_multi_turn_harness(args.gold, args.out)
    print(f"Conversations evaluated: {len({r.conversation_id for r in results})}")
    print(f"Turns evaluated: {len(results)}")
    print(f"Turn accuracy: {eval_run.turn_accuracy:.1%}")
    print(f"Conversations fully passed: {eval_run.conversations_fully_passed:.1%}")
    print(f"Per-scenario accuracy: {eval_run.per_scenario_accuracy}")
    print(f"Reports written to {args.out}")


if __name__ == "__main__":
    _main()
