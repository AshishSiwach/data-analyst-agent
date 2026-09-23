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


class ChartSpec(BaseModel):
    chart_type: ChartType
    data: list[list[Any]]
    x_axis: str | None = None
    y_axis: str | None = None


class FailureDiagnosis(BaseModel):
    category: FailureCategory
    explanation: str
    rephrase_suggestion: str


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
    (`is_graceful_failure_case == True`), per S18's acceptance criteria."""

    question_id: str
    question_text: str
    bucket: GoldBucket
    gold_sql: str | None = None
    gold_result: ResultData | None = None
    is_graceful_failure_case: bool

    @model_validator(mode="after")
    def _check_graceful_failure_shape(self) -> GoldQuestion:
        if self.is_graceful_failure_case:
            if self.gold_sql is not None or self.gold_result is not None:
                raise ValueError(
                    "is_graceful_failure_case == True requires gold_sql and gold_result to be null"
                )
        else:
            if self.gold_sql is None or self.gold_result is None:
                raise ValueError(
                    "is_graceful_failure_case == False requires gold_sql and gold_result"
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
