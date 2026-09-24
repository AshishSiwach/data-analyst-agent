"""The eval CLI from `_docs/scope.md`: run every gold question through
S24's `answer_question`, grade with S03's `compare()`, produce
`report.json` + `report.md` stamped with the git commit hash and a hash
of the active prompts.

Two module-boundary notes, flagged per this project's established pattern
rather than silently worked around:

1. **`attempts_used` comes from `QueryAuditLog`, not from `Answer`.**
`Answer` (S01/S24) deliberately carries no attempt count - it's a
UI-rendering object. `InformationModel.md` already anticipates this
exactly: `QueryAuditLog` is "the complete raw material behind the eval
harness's attempts-until-success metric." Each `answer_question` call is
pointed at a harness-owned audit log file (via S24's own
`query_audit_log_path` parameter, added for exactly this kind of reuse);
the harness reads back every entry sharing the returned `Answer.turn_id`
to recover both the attempt count and the last attempted SQL text.

2. **`EvalResult.agent_result` is reconstructed from `Answer.chart_spec.data`,
not from a full `SqlExecutionResult`.** `Answer` intentionally has no raw
`columns`/`rows` field either (same "rendering object, not a data
object" design as above) - `chart_spec.data` is the one place a
successful turn's raw row values survive into `Answer`
(`build_chart_spec` sets `data=result.rows` verbatim, unconditionally, for
every chart type). `compare()` (S03) only ever checks column *count* and
compares values positionally - it never reads a `ColumnSpec.name` or
`.type` - so a placeholder column list of the right length is sufficient
and honest; this is verified against real column counts, not borrowed
from the gold row to avoid masking a real shape mismatch.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from openai import OpenAI

from data_analyst_agent.agent.diagnosis import SYSTEM_PROMPT_TEMPLATE as _DIAGNOSIS_PROMPT
from data_analyst_agent.agent.generate_sql import SYSTEM_PROMPT_TEMPLATE as _GENERATE_SQL_PROMPT
from data_analyst_agent.agent.narrative import SYSTEM_PROMPT as _NARRATIVE_PROMPT
from data_analyst_agent.agent.orchestrator import answer_question
from data_analyst_agent.agent.session import SessionState
from data_analyst_agent.eval.comparator import compare
from data_analyst_agent.models.entities import (
    Answer,
    ColumnSpec,
    EvalResult,
    EvalRun,
    GoldBucket,
    GoldQuestion,
    QueryAuditLog,
    ResultData,
)

GOLD_BUCKETS: tuple[GoldBucket, ...] = ("basic", "semantic", "adversarial")


def load_gold_questions(gold_dir: Path | str) -> list[GoldQuestion]:
    """Loads every `GoldQuestion` from every `*.jsonl` file in `gold_dir`
    (`basic.jsonl`/`semantic.jsonl`/`adversarial.jsonl`, or any small
    synthetic set a test points this at)."""
    questions: list[GoldQuestion] = []
    for path in sorted(Path(gold_dir).glob("*.jsonl")):
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    questions.append(GoldQuestion.model_validate_json(line))
    return questions


def _prompt_hash() -> str:
    combined = _GENERATE_SQL_PROMPT + _NARRATIVE_PROMPT + _DIAGNOSIS_PROMPT
    return hashlib.sha256(combined.encode("utf-8")).hexdigest()[:12]


def _git_commit_hash() -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True
        )
        return result.stdout.strip()
    except Exception:
        return "unknown"


def _attempt_records_for_turn(audit_log_path: Path, turn_id: str) -> list[QueryAuditLog]:
    if not audit_log_path.exists():
        return []
    records = []
    with open(audit_log_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            entry = QueryAuditLog.model_validate_json(line)
            if entry.turn_id == turn_id:
                records.append(entry)
    return records


def _reconstruct_agent_result(answer: Answer) -> ResultData:
    """See module docstring, note 2. Honest column *count* derived from
    the actual row shape, not borrowed from gold - so a real shape
    mismatch still fails `compare()`'s column-count check."""
    data = answer.chart_spec.data if answer.chart_spec else []
    n_cols = len(data[0]) if data else 0
    columns = [ColumnSpec(name=f"col{i}", type="ANY") for i in range(n_cols)]
    return ResultData(columns=columns, rows=data)


def _describe_diff(agent_result: ResultData, gold_result: ResultData) -> str:
    if len(agent_result.rows) != len(gold_result.rows):
        return f"row count: agent={len(agent_result.rows)} gold={len(gold_result.rows)}"
    if len(agent_result.columns) != len(gold_result.columns):
        return f"column count: agent={len(agent_result.columns)} gold={len(gold_result.columns)}"
    return "values differ"


def _grade(
    run_id: str, question: GoldQuestion, answer: Answer, attempts_used: int, agent_sql: str
) -> EvalResult:
    if question.is_graceful_failure_case:
        # Graded on failure *shape* only, per scope.md: "did the failure
        # land in one of the three well-shaped categories" - not whether
        # the diagnosed category matches the gold row's expectation.
        passed = answer.status == "graceful_failure"
        diff = None if passed else f"expected a graceful failure, got status={answer.status!r}"
        return EvalResult(
            run_id=run_id,
            question_id=question.question_id,
            passed=passed,
            agent_sql=agent_sql,
            agent_result=None,
            diff=diff,
            attempts_used=attempts_used,
        )

    if answer.status != "success":
        return EvalResult(
            run_id=run_id,
            question_id=question.question_id,
            passed=False,
            agent_sql=agent_sql,
            agent_result=None,
            diff=f"expected a successful answer, got status={answer.status!r}",
            attempts_used=attempts_used,
        )

    agent_result = _reconstruct_agent_result(answer)
    passed = compare(agent_result, question.gold_result)
    diff = None if passed else _describe_diff(agent_result, question.gold_result)
    return EvalResult(
        run_id=run_id,
        question_id=question.question_id,
        passed=passed,
        agent_sql=agent_sql,
        agent_result=agent_result,
        diff=diff,
        attempts_used=attempts_used,
    )


def _summarize(run_id: str, questions: list[GoldQuestion], results: list[EvalResult]) -> EvalRun:
    by_id = {q.question_id: q for q in questions}

    per_bucket_accuracy: dict[str, float] = {}
    for bucket in GOLD_BUCKETS:
        bucket_results = [r for r in results if by_id[r.question_id].bucket == bucket]
        if bucket_results:
            per_bucket_accuracy[bucket] = sum(r.passed for r in bucket_results) / len(
                bucket_results
            )

    attempts_histogram: dict[int, int] = {}
    for r in results:
        # "Attempts until success" is the retry loop's own notion of
        # success (the SQL executed without error) - a property of
        # SqlRetryOutcome.status, not of eval-graded correctness. Gating
        # this on r.passed would silently drop a question that executed
        # cleanly but returned the wrong data (a real, distinct failure
        # mode from "never got a working query") out of the histogram
        # entirely. agent_result is populated exactly when status ==
        # "success" for a non-graceful-failure-case row (see _grade), so
        # it's the right signal here, independent of whether compare()
        # then passed.
        if not by_id[r.question_id].is_graceful_failure_case and r.agent_result is not None:
            attempts_histogram[r.attempts_used] = attempts_histogram.get(r.attempts_used, 0) + 1

    gf_results = [r for r in results if by_id[r.question_id].is_graceful_failure_case]
    graceful_failure_rate = (
        sum(r.passed for r in gf_results) / len(gf_results) if gf_results else 0.0
    )

    return EvalRun(
        run_id=run_id,
        git_commit_hash=_git_commit_hash(),
        prompt_hash=_prompt_hash(),
        timestamp=datetime.now(timezone.utc),
        per_bucket_accuracy=per_bucket_accuracy,
        attempts_until_success_distribution=attempts_histogram,
        graceful_failure_rate=graceful_failure_rate,
    )


def run_harness(
    gold_dir: Path | str,
    out_dir: Path | str,
    db_path: Path | str | None = None,
    client: OpenAI | None = None,
) -> tuple[EvalRun, list[EvalResult]]:
    """Runs every gold question in `gold_dir` through `answer_question`
    (S24), grades each with `compare()` (S03), writes `report.json` and
    `report.md` into `out_dir`, and returns the same data in typed form.
    """
    questions = load_gold_questions(gold_dir)
    run_id = str(uuid.uuid4())

    out_path = Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)
    audit_log_path = out_path / "eval_query_audit.jsonl"
    failure_log_path = out_path / "eval_failures.jsonl"
    # Start each run with a clean audit trail so attempts_used counts
    # aren't contaminated by entries an earlier run appended.
    audit_log_path.unlink(missing_ok=True)
    failure_log_path.unlink(missing_ok=True)

    results: list[EvalResult] = []
    for question in questions:
        session = SessionState(
            session_id=f"eval-{question.question_id}",
            started_at=datetime.now(timezone.utc),
            cost_spent_usd=Decimal("0"),
            cost_cap_usd=Decimal("0.50"),
        )
        answer = answer_question(
            question.question_text,
            session,
            db_path=db_path,
            client=client,
            query_audit_log_path=audit_log_path,
            failure_log_path=failure_log_path,
        )
        records = _attempt_records_for_turn(audit_log_path, answer.turn_id)
        attempts_used = len(records)
        agent_sql = records[-1].query_text if records else ""

        results.append(_grade(run_id, question, answer, attempts_used, agent_sql))

    eval_run = _summarize(run_id, questions, results)
    _write_reports(out_path, eval_run, results)
    return eval_run, results


def _write_reports(out_dir: Path, eval_run: EvalRun, results: list[EvalResult]) -> None:
    report = {
        "run": json.loads(eval_run.model_dump_json()),
        "results": [json.loads(r.model_dump_json()) for r in results],
    }
    (out_dir / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    (out_dir / "report.md").write_text(_render_markdown(eval_run, results), encoding="utf-8")


def _render_markdown(eval_run: EvalRun, results: list[EvalResult]) -> str:
    lines = [
        "# Evaluation Report",
        "",
        f"- Run ID: `{eval_run.run_id}`",
        f"- Git commit: `{eval_run.git_commit_hash}`",
        f"- Prompt hash: `{eval_run.prompt_hash}`",
        f"- Timestamp: {eval_run.timestamp.isoformat()}",
        "",
        "## Per-bucket accuracy",
        "",
        "| Bucket | Accuracy |",
        "|---|---|",
    ]
    for bucket, accuracy in sorted(eval_run.per_bucket_accuracy.items()):
        lines.append(f"| {bucket} | {accuracy:.1%} |")

    lines += [
        "",
        "## Attempts-until-success distribution",
        "",
        "| Attempts | Count |",
        "|---|---|",
    ]
    for attempts, count in sorted(eval_run.attempts_until_success_distribution.items()):
        lines.append(f"| {attempts} | {count} |")

    lines += ["", f"## Graceful failure rate: {eval_run.graceful_failure_rate:.1%}"]

    failed = [r for r in results if not r.passed]
    if failed:
        lines += ["", "## Failed questions", "", "| Question ID | Diff |", "|---|---|"]
        for r in failed:
            diff_text = (r.diff or "").replace("|", "\\|").replace("\n", " ")
            lines.append(f"| {r.question_id} | {diff_text} |")

    return "\n".join(lines) + "\n"


def _main() -> None:
    from dotenv import load_dotenv

    load_dotenv()

    parser = argparse.ArgumentParser(description="Run the gold-set evaluation harness.")
    parser.add_argument("--gold", required=True, help="Directory of gold *.jsonl files")
    parser.add_argument("--out", required=True, help="Directory to write report.json/report.md")
    args = parser.parse_args()

    eval_run, results = run_harness(args.gold, args.out)
    print(f"Questions evaluated: {len(results)}")
    print(f"Per-bucket accuracy: {eval_run.per_bucket_accuracy}")
    print(f"Attempts-until-success distribution: {eval_run.attempts_until_success_distribution}")
    print(f"Graceful failure rate: {eval_run.graceful_failure_rate:.1%}")
    print(f"Reports written to {args.out}")


if __name__ == "__main__":
    _main()
