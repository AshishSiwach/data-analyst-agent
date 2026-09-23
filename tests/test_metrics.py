"""S13 acceptance tests for data.metrics."""

from __future__ import annotations

import duckdb

from data_analyst_agent.data.metrics import METRICS, TABLE_NAME, get_metrics, seed_metrics
from data_analyst_agent.models.entities import MetricDefinition

EXPECTED_METRIC_IDS = {
    "revenue",
    "aov",
    "order_count",
    "units_sold",
    "repeat_rate",
    "return_rate",
    "top_selling",
    "growth_rate",
    "clv",
}


def test_exactly_nine_rows():
    assert len(METRICS) == 9
    assert len(get_metrics()) == 9


def test_every_row_validates_against_the_metric_definition_model():
    for metric in METRICS:
        assert isinstance(metric, MetricDefinition)
        # Round-trip through the model to prove each row is genuinely
        # schema-valid, not just true by construction.
        revalidated = MetricDefinition.model_validate(metric.model_dump())
        assert revalidated == metric


def test_metric_ids_match_information_model_exactly():
    assert {m.metric_id for m in METRICS} == EXPECTED_METRIC_IDS


def test_metric_ids_are_unique():
    ids = [m.metric_id for m in METRICS]
    assert len(ids) == len(set(ids))


def test_return_rate_direction_is_asc():
    (return_rate,) = [m for m in METRICS if m.metric_id == "return_rate"]
    assert return_rate.default_direction == "asc"
    assert return_rate.unit == "percentage"


def test_every_other_metric_direction_is_desc():
    for metric in METRICS:
        if metric.metric_id != "return_rate":
            assert metric.default_direction == "desc", (
                f"{metric.metric_id} should default to desc; "
                "return_rate is the one documented exception"
            )


def test_seed_metrics_writes_nine_rows_to_a_queryable_table(tmp_path):
    db_path = tmp_path / "test_metrics.duckdb"
    written = seed_metrics(db_path=db_path)
    assert written == 9

    con = duckdb.connect(str(db_path), read_only=True)
    (count,) = con.execute(f"SELECT COUNT(*) FROM {TABLE_NAME}").fetchone()
    assert count == 9

    (direction,) = con.execute(
        f"SELECT default_direction FROM {TABLE_NAME} WHERE metric_id = 'return_rate'"
    ).fetchone()
    assert direction == "asc"
    con.close()


def test_seed_metrics_is_idempotent(tmp_path):
    db_path = tmp_path / "test_metrics_rerun.duckdb"
    seed_metrics(db_path=db_path)
    second_count = seed_metrics(db_path=db_path)
    assert second_count == 9

    con = duckdb.connect(str(db_path), read_only=True)
    (count,) = con.execute(f"SELECT COUNT(*) FROM {TABLE_NAME}").fetchone()
    assert count == 9
    con.close()
