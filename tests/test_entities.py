"""S01 acceptance tests: one valid instance, one missing-required-field
failure, and one JSON round-trip per entity from InformationModel.md."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError

from data_analyst_agent.models.entities import (
    Answer,
    ChartSpec,
    ColumnSpec,
    EvalResult,
    EvalRun,
    FailureDiagnosis,
    FailureLogEntry,
    GoldQuestion,
    MetricDefinition,
    QueryAuditLog,
    ResultData,
    SessionState,
    SqlAttempt,
)

NOW = datetime(2026, 9, 23, 12, 0, 0, tzinfo=timezone.utc)


def _sql_attempt(**overrides) -> dict:
    base = dict(
        turn_id="turn-1",
        attempt_number=1,
        query_text="SELECT 1",
        status="success",
        error_message=None,
        execution_ms=120,
        row_count=1,
        truncated=False,
    )
    base.update(overrides)
    return base


def _chart_spec(**overrides) -> dict:
    base = dict(chart_type="bar", data=[[1, 2], [3, 4]], x_axis="region", y_axis="revenue")
    base.update(overrides)
    return base


def _failure_diagnosis(**overrides) -> dict:
    base = dict(
        category="ambiguity",
        explanation="The question didn't specify a ranking metric.",
        rephrase_suggestion="Try: 'top 5 products by revenue in Q3'.",
    )
    base.update(overrides)
    return base


def _result_data(**overrides) -> dict:
    base = dict(columns=[ColumnSpec(name="x", type="INTEGER")], rows=[[1]])
    base.update(overrides)
    return base


# Each entry: (model class, kwargs for one valid instance, a required field name to drop).
MODEL_CASES = [
    (SqlAttempt, _sql_attempt(), "query_text"),
    (ChartSpec, _chart_spec(), "data"),
    (FailureDiagnosis, _failure_diagnosis(), "explanation"),
    (
        Answer,
        dict(
            turn_id="turn-1",
            status="success",
            answer_text="Revenue was $100.",
            assumption_disclosed=None,
            chart_spec=ChartSpec(**_chart_spec()),
            sql_shown="SELECT 1",
            diagnosis=None,
        ),
        "status",
    ),
    (
        SessionState,
        dict(
            session_id="sess-1",
            started_at=NOW,
            cost_spent_usd=Decimal("0.10"),
            cost_cap_usd=Decimal("0.50"),
            turn_ids=["turn-1"],
            failed_questions_cache={"top products": 2},
        ),
        "started_at",
    ),
    (
        QueryAuditLog,
        dict(
            turn_id="turn-1",
            session_id="sess-1",
            attempt_number=1,
            query_text="SELECT 1",
            status="success",
            execution_ms=80,
            row_count=1,
            error_message=None,
            logged_at=NOW,
        ),
        "logged_at",
    ),
    (
        FailureLogEntry,
        dict(
            turn_id="turn-1",
            session_id="sess-1",
            question_text="Why did sales drop?",
            attempts=[SqlAttempt(**_sql_attempt(status="error", truncated=False))],
            diagnosis=FailureDiagnosis(**_failure_diagnosis()),
            fast_fail_triggered=False,
            logged_at=NOW,
        ),
        "diagnosis",
    ),
    (
        MetricDefinition,
        dict(
            metric_id="revenue",
            display_name="Revenue",
            definition="Sum of (quantity x unit price), net of returns.",
            sql_fragment="SUM(net_line_revenue)",
            unit="currency",
            default_direction="desc",
        ),
        "unit",
    ),
    (
        GoldQuestion,
        dict(
            question_id="q-1",
            question_text="What is total revenue in 2011?",
            bucket="basic",
            gold_sql="SELECT SUM(net_revenue) FROM v_orders",
            gold_result=ResultData(**_result_data()),
            is_graceful_failure_case=False,
        ),
        "bucket",
    ),
    (
        EvalRun,
        dict(
            run_id="run-1",
            git_commit_hash="abc123",
            prompt_hash="def456",
            timestamp=NOW,
            per_bucket_accuracy={"basic": 0.9, "semantic": 0.7, "adversarial": 0.5},
            attempts_until_success_distribution={1: 10, 2: 5, 3: 2},
            graceful_failure_rate=0.8,
        ),
        "graceful_failure_rate",
    ),
    (
        EvalResult,
        dict(
            run_id="run-1",
            question_id="q-1",
            passed=True,
            agent_sql="SELECT 1",
            agent_result=ResultData(**_result_data()),
            diff=None,
            attempts_used=1,
        ),
        "agent_sql",
    ),
]


@pytest.mark.parametrize(
    "model_cls, valid_kwargs, _drop_field", MODEL_CASES, ids=lambda v: getattr(v, "__name__", "")
)
def test_valid_instance_constructs(model_cls, valid_kwargs, _drop_field):
    instance = model_cls(**valid_kwargs)
    assert isinstance(instance, model_cls)


@pytest.mark.parametrize(
    "model_cls, valid_kwargs, drop_field", MODEL_CASES, ids=lambda v: getattr(v, "__name__", "")
)
def test_missing_required_field_raises(model_cls, valid_kwargs, drop_field):
    incomplete = {k: v for k, v in valid_kwargs.items() if k != drop_field}
    with pytest.raises(ValidationError):
        model_cls(**incomplete)


@pytest.mark.parametrize(
    "model_cls, valid_kwargs, _drop_field", MODEL_CASES, ids=lambda v: getattr(v, "__name__", "")
)
def test_json_round_trip(model_cls, valid_kwargs, _drop_field):
    instance = model_cls(**valid_kwargs)
    dumped = instance.model_dump_json()
    restored = model_cls.model_validate_json(dumped)
    assert restored == instance


# --- Targeted checks for the conditional-requirement rules InformationModel.md
# spells out in prose (Answer's per-status shape, GoldQuestion's
# graceful-failure shape). These aren't required by S01 but directly verify
# the "nullability matching InformationModel.md exactly" acceptance criterion.


def test_answer_success_requires_answer_chart_and_sql():
    with pytest.raises(ValidationError):
        Answer(turn_id="turn-1", status="success")


def test_answer_success_forbids_diagnosis():
    with pytest.raises(ValidationError):
        Answer(
            turn_id="turn-1",
            status="success",
            answer_text="x",
            chart_spec=ChartSpec(**_chart_spec()),
            sql_shown="SELECT 1",
            diagnosis=FailureDiagnosis(**_failure_diagnosis()),
        )


def test_answer_graceful_failure_requires_diagnosis_only():
    answer = Answer(
        turn_id="turn-1",
        status="graceful_failure",
        diagnosis=FailureDiagnosis(**_failure_diagnosis()),
    )
    assert answer.answer_text is None
    assert answer.chart_spec is None


def test_answer_graceful_failure_without_diagnosis_raises():
    with pytest.raises(ValidationError):
        Answer(turn_id="turn-1", status="graceful_failure")


def test_answer_budget_stop_carries_no_optional_fields():
    answer = Answer(turn_id="turn-1", status="budget_stop")
    assert answer.answer_text is None
    assert answer.diagnosis is None


def test_answer_budget_stop_with_extra_field_raises():
    with pytest.raises(ValidationError):
        Answer(turn_id="turn-1", status="budget_stop", answer_text="not allowed")


def test_gold_question_graceful_failure_forbids_gold_sql():
    with pytest.raises(ValidationError):
        GoldQuestion(
            question_id="q-2",
            question_text="Why did email opens drop?",
            bucket="adversarial",
            gold_sql="SELECT 1",
            is_graceful_failure_case=True,
        )


def test_gold_question_graceful_failure_allows_null_sql():
    gq = GoldQuestion(
        question_id="q-2",
        question_text="Why did email opens drop?",
        bucket="adversarial",
        is_graceful_failure_case=True,
    )
    assert gq.gold_sql is None
    assert gq.gold_result is None


def test_gold_question_non_graceful_failure_requires_gold_sql():
    with pytest.raises(ValidationError):
        GoldQuestion(
            question_id="q-3",
            question_text="What is total revenue?",
            bucket="basic",
            is_graceful_failure_case=False,
        )
