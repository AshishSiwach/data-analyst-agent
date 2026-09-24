"""Typed entities from `_docs/InformationModel.md`.

Covers the eleven entities S01 names explicitly: SqlAttempt, ChartSpec,
FailureDiagnosis, Answer, SessionState, QueryAuditLog, FailureLogEntry,
MetricDefinition, GoldQuestion, EvalRun, EvalResult. `Question` is defined
in InformationModel.md's Layer 2 diagram but is not in that list, so it is
deliberately not modeled here.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

SqlStatus = Literal["success", "error", "timeout", "rejected"]
ChartType = Literal["scalar", "line", "bar", "table"]
FailureCategory = Literal["schema_mismatch", "ambiguity", "bug"]
AnswerStatus = Literal["success", "graceful_failure", "budget_stop"]
MetricUnit = Literal["currency", "count", "percentage", "rank"]
MetricDirection = Literal["asc", "desc"]
GoldBucket = Literal["basic", "semantic", "adversarial"]


class ColumnSpec(BaseModel):
    name: str
    type: str


class ResultData(BaseModel):
    """The `{columns, rows}` shape referenced by GoldQuestion.gold_result and
    EvalResult.agent_result, matching Tools.md's run_sql output shape."""

    columns: list[ColumnSpec]
    rows: list[list[Any]]


class GeneratedSql(BaseModel):
    """`agent/generate_sql.py::generate_sql`'s (S19) output. Promoted here
    from a module-local model once it started crossing a module boundary
    (S20's retry loop needs to read `can_answer_from_schema`), per
    CLAUDE.md's "all data crossing a module boundary is a pydantic model
    from models/entities.py" rule.

    `can_answer_from_schema` is a structural signal added retroactively
    (post-S23) after the required S23 evaluation case showed the model
    would rather invent *some* query than say "I can't do this" - e.g.
    "How many orders came from Scotland?" silently became
    `WHERE country = 'Scotland'` (0 rows, reported as a normal success)
    instead of recognizing sub-national regions aren't tracked. Making
    this an explicit boolean field means the retry loop can act on the
    model's own judgment deterministically, instead of only inferring
    unanswerability from whether the resulting SQL happens to error out -
    which it usually doesn't, since a plausible-looking query is easy to
    construct even for questions the schema can't actually answer.
    """

    can_answer_from_schema: bool
    sql: str | None = None
    reason: str | None = None

    @model_validator(mode="after")
    def _check_shape(self) -> GeneratedSql:
        if self.can_answer_from_schema:
            if self.sql is None:
                raise ValueError("can_answer_from_schema == True requires sql")
        else:
            if self.sql is not None:
                raise ValueError("can_answer_from_schema == False must not carry sql")
            if self.reason is None:
                raise ValueError("can_answer_from_schema == False requires reason")
        return self


class SqlAttempt(BaseModel):
    """Turn-scoped. Matches run_sql's I/O contract from Tools.md, combined
    into one record."""

    turn_id: str
    attempt_number: int = Field(ge=1, le=3)
    query_text: str
    status: SqlStatus
    error_message: str | None = None
    execution_ms: int
    row_count: int | None = None
    truncated: bool


class SqlExecutionResult(BaseModel):
    """The exact output shape `run_sql` (S15) returns, per `_docs/Tools.md`'s
    typed output schema. Added at S15, beyond S01's original 11 models - a
    flagged gap from S01's completion report: Tools.md's run_sql output
    schema (status, columns, rows, row_count, truncated, error_message,
    execution_ms) carries more than `SqlAttempt` does (`SqlAttempt` is
    audit-log-shaped and has no `columns`/`rows`). `columns`, `rows`, and
    `row_count` are present only when `status == "success"`;
    `error_message` only when `status != "success"`.
    """

    status: SqlStatus
    columns: list[ColumnSpec] | None = None
    rows: list[list[Any]] | None = None
    row_count: int | None = None
    truncated: bool
    error_message: str | None = None
    execution_ms: int

    @model_validator(mode="after")
    def _check_status_shape(self) -> SqlExecutionResult:
        if self.status == "success":
            if self.columns is None or self.rows is None or self.row_count is None:
                raise ValueError("status == 'success' requires columns, rows, and row_count")
            if self.error_message is not None:
                raise ValueError("status == 'success' must not carry an error_message")
        else:
            if self.columns is not None or self.rows is not None or self.row_count is not None:
                raise ValueError(
                    f"status == {self.status!r} must not carry columns, rows, or row_count"
                )
            if self.error_message is None:
                raise ValueError(f"status == {self.status!r} requires error_message")
        return self


class SqlRetryOutcome(BaseModel):
    """What `agent/retry_loop.py::run_turn_sql` (S20) returns. Added beyond
    S01's original models - a flagged gap: S20's acceptance criteria says
    `-> SqlAttempt`, but `SqlAttempt` is audit-log-shaped (no rows/columns,
    per the same S15 gap) and has no way to represent a `fast_fail` or
    `budget_stop` outcome (`SqlExecutionResult.status` is the run_sql-only
    enum `success | error | timeout | rejected`). This type carries the
    three things the retry loop's caller (S24's orchestrator) actually
    needs: which of five ways the loop ended, the winning-or-final
    `SqlExecutionResult` (absent for `fast_fail`, which makes zero
    attempts, `budget_stop`, which discards its final attempt, and
    `declined`, which never executes any SQL), and the full turn-scoped
    attempt trace for S05's audit logging and S23's diagnosis - both done
    by the orchestrator, not the retry loop.

    `declined` (added post-S23, alongside `GeneratedSql.can_answer_from_schema`):
    `generate_sql` itself judged the question unanswerable from the
    schema. Short-circuits immediately rather than burning the remaining
    attempts hoping the model reconsiders - the same "stop retrying
    blind" reasoning `autonomy.md` already applies to fast-fail.
    """

    status: Literal["success", "exhausted", "fast_fail", "budget_stop", "declined"]
    result: SqlExecutionResult | None = None
    attempts: list[SqlAttempt] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_shape(self) -> SqlRetryOutcome:
        if len(self.attempts) > 3:
            raise ValueError("attempts must never exceed 3")
        if self.status == "fast_fail" and (self.attempts or self.result is not None):
            raise ValueError("status == 'fast_fail' must have zero attempts and no result")
        if self.status == "success" and (self.result is None or self.result.status != "success"):
            raise ValueError("status == 'success' requires a successful result")
        if self.status == "exhausted" and (
            len(self.attempts) != 3 or self.result is None or self.result.status == "success"
        ):
            raise ValueError(
                "status == 'exhausted' requires exactly 3 attempts and a non-success result"
            )
        if self.status == "declined" and (not self.attempts or self.result is not None):
            raise ValueError("status == 'declined' requires at least one attempt and no result")
        return self


class ChartSpec(BaseModel):
    """`x_axis`/`y_axis` match InformationModel.md's literal schema
    exactly - "set only for line/bar." `column_names` is an addition
    (found live, post-S27, via direct user feedback that rendered
    tables showed no headers at all): a `table`-type ChartSpec has no
    x_axis/y_axis by design (S24's build_chart_spec sets neither for
    `table`, per InformationModel.md's own scoping), so there was no
    field anywhere in Answer's object graph carrying a table's column
    names into the UI. `column_names` (all columns, in order, for every
    chart type - not just table) closes that gap without touching
    x_axis/y_axis's existing, doc-specified meaning.
    """

    chart_type: ChartType
    data: list[list[Any]]
    x_axis: str | None = None
    y_axis: str | None = None
    column_names: list[str] | None = None


class FailureDiagnosis(BaseModel):
    category: FailureCategory
    explanation: str
    rephrase_suggestion: str


class NarrativeWrap(BaseModel):
    """`agent/narrative.py::wrap`'s (S22) output, matching Tools.md's
    Narrative wrap output schema exactly: `answer_text` and
    `assumption_disclosed` (null when the question wasn't ambiguous). This
    is deliberately smaller than `Answer` (S01) - the orchestrator (S24)
    combines this with turn_id/chart_spec/sql_shown, which `wrap` itself
    has no business setting."""

    answer_text: str
    assumption_disclosed: str | None = None


class Answer(BaseModel):
    """The object the Streamlit layer renders directly.

    `answer_text`, `chart_spec`, and `sql_shown` are populated only when
    `status == "success"`; `diagnosis` only when `status ==
    "graceful_failure"`. A `budget_stop` Answer carries none of the optional
    fields.
    """

    turn_id: str
    status: AnswerStatus
    answer_text: str | None = None
    assumption_disclosed: str | None = None
    chart_spec: ChartSpec | None = None
    sql_shown: str | None = None
    diagnosis: FailureDiagnosis | None = None

    @model_validator(mode="after")
    def _check_status_shape(self) -> Answer:
        if self.status == "success":
            if self.answer_text is None or self.chart_spec is None or self.sql_shown is None:
                raise ValueError(
                    "status == 'success' requires answer_text, chart_spec, and sql_shown"
                )
            if self.diagnosis is not None:
                raise ValueError("status == 'success' must not carry a diagnosis")
        elif self.status == "graceful_failure":
            if self.diagnosis is None:
                raise ValueError("status == 'graceful_failure' requires diagnosis")
            optional_fields = (
                self.answer_text,
                self.assumption_disclosed,
                self.chart_spec,
                self.sql_shown,
            )
            if any(f is not None for f in optional_fields):
                raise ValueError(
                    "status == 'graceful_failure' must not carry answer_text, "
                    "assumption_disclosed, chart_spec, or sql_shown"
                )
        elif self.status == "budget_stop":
            if any(
                f is not None
                for f in (
                    self.answer_text,
                    self.assumption_disclosed,
                    self.chart_spec,
                    self.sql_shown,
                    self.diagnosis,
                )
            ):
                raise ValueError("status == 'budget_stop' must carry none of the optional fields")
        return self


class SessionState(BaseModel):
    session_id: str
    started_at: datetime
    cost_spent_usd: Decimal
    cost_cap_usd: Decimal = Decimal("0.50")
    turn_ids: list[str] = Field(default_factory=list)
    failed_questions_cache: dict[str, int] = Field(default_factory=dict)


class QueryAuditLog(BaseModel):
    """One entry per SqlAttempt, written unconditionally."""

    turn_id: str
    session_id: str
    attempt_number: int = Field(ge=1, le=3)
    query_text: str
    status: SqlStatus
    execution_ms: int
    row_count: int | None = None
    error_message: str | None = None
    logged_at: datetime


class FailureLogEntry(BaseModel):
    """Written to failures.jsonl only when a turn fully fails."""

    turn_id: str
    session_id: str
    question_text: str
    attempts: list[SqlAttempt]
    diagnosis: FailureDiagnosis
    fast_fail_triggered: bool
    logged_at: datetime


class MetricDefinition(BaseModel):
    metric_id: str
    display_name: str
    definition: str
    sql_fragment: str
    unit: MetricUnit
    default_direction: MetricDirection


class GoldQuestion(BaseModel):
    """One row of the hand-crafted gold set. `gold_sql`/`gold_result` are
    null exactly for the graceful-failure-designed adversarial subset
    (`is_graceful_failure_case == True`), per S18's acceptance criteria.

    `expected_failure_category` is a field beyond InformationModel.md's
    original GoldQuestion schema - added at S18, a flagged gap: S18's own
    acceptance criteria requires storing "the expected failure category
    (schema_mismatch / ambiguity)" for graceful-failure rows, but no doc
    ever gave GoldQuestion a field for it. Reuses `FailureCategory`, the
    same enum `FailureDiagnosis.category` already uses - `"bug"` is a
    valid `FailureCategory` value but never a sensible *expected* one
    here, since these rows are deliberately designed to fail, not
    accidentally broken.
    """

    question_id: str
    question_text: str
    bucket: GoldBucket
    gold_sql: str | None = None
    gold_result: ResultData | None = None
    is_graceful_failure_case: bool
    expected_failure_category: FailureCategory | None = None

    @model_validator(mode="after")
    def _check_graceful_failure_shape(self) -> GoldQuestion:
        if self.is_graceful_failure_case:
            if self.gold_sql is not None or self.gold_result is not None:
                raise ValueError(
                    "is_graceful_failure_case == True requires gold_sql and gold_result to be null"
                )
            if self.expected_failure_category is None:
                raise ValueError(
                    "is_graceful_failure_case == True requires expected_failure_category"
                )
        else:
            if self.gold_sql is None or self.gold_result is None:
                raise ValueError(
                    "is_graceful_failure_case == False requires gold_sql and gold_result"
                )
            if self.expected_failure_category is not None:
                raise ValueError(
                    "is_graceful_failure_case == False must not carry expected_failure_category"
                )
        return self


class EvalRun(BaseModel):
    run_id: str
    git_commit_hash: str
    prompt_hash: str
    timestamp: datetime
    per_bucket_accuracy: dict[GoldBucket, float]
    attempts_until_success_distribution: dict[int, int]
    graceful_failure_rate: float


class EvalResult(BaseModel):
    run_id: str
    question_id: str
    passed: bool
    agent_sql: str
    agent_result: ResultData | None = None
    diff: str | None = None
    attempts_used: int
