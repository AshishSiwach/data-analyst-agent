"""S17 acceptance tests for eval/gold/semantic.jsonl.

Same structural checks as test_gold_basic.py, plus S17's own required
check: every one of the 9 metric_dictionary entries must be exercised by
at least one gold question's gold_sql - verified by fragment inspection
(a specific substring proving the SQL genuinely implements that metric's
formula), not just a keyword match on question_text.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import duckdb
import pytest

from data_analyst_agent.data.ingest import DEFAULT_DB_PATH
from data_analyst_agent.data.metrics import METRICS
from data_analyst_agent.eval.comparator import compare
from data_analyst_agent.models.entities import ColumnSpec, GoldQuestion, ResultData

GOLD_PATH = (
    Path(__file__).resolve().parent.parent
    / "data_analyst_agent"
    / "eval"
    / "gold"
    / "semantic.jsonl"
)
_DEFAULT_DB_PATH = Path(os.environ.get("DUCKDB_PATH", str(DEFAULT_DB_PATH)))

_REQUIRED_TABLES = (
    "raw_online_retail",
    "dim_product_category",
    "v_orders",
    "v_order_lines",
    "v_customers",
    "v_products",
    "v_daily_revenue",
)

# metric_id -> (question_id whose gold_sql implements it, a substring of
# that gold_sql that's only present because the formula is genuinely
# implemented there, not a coincidental keyword match).
METRIC_EVIDENCE: dict[str, tuple[str, str]] = {
    "revenue": ("semantic_013", "SUM(line_revenue)"),
    "aov": ("semantic_001", "SUM(net_revenue) / COUNT(DISTINCT order_id)"),
    "order_count": ("semantic_014", "COUNT(DISTINCT order_id) AS order_count"),
    "units_sold": ("semantic_009", "SUM(quantity) AS units_sold"),
    "repeat_rate": ("semantic_003", "order_count >= 2"),
    "return_rate": ("semantic_004", "SUM(returned_amount) / SUM(gross_revenue)"),
    "top_selling": ("semantic_006", "ORDER BY total_revenue DESC"),
    "growth_rate": (
        "semantic_010",
        "(MAX(CASE WHEN yr = 2011 THEN rev END) - MAX(CASE WHEN yr = 2010 THEN rev END))",
    ),
    "clv": ("semantic_011", "lifetime_revenue AS clv"),
}


def _table_exists(con: duckdb.DuckDBPyConnection, table: str) -> bool:
    (count,) = con.execute(
        "SELECT COUNT(*) FROM information_schema.tables WHERE table_name = ?", [table]
    ).fetchone()
    return count > 0


@pytest.fixture(scope="module")
def con():
    if not _DEFAULT_DB_PATH.exists():
        pytest.skip(f"{_DEFAULT_DB_PATH} does not exist - build the full pipeline first")
    connection = duckdb.connect(str(_DEFAULT_DB_PATH), read_only=True)
    for table in _REQUIRED_TABLES:
        if not _table_exists(connection, table):
            connection.close()
            pytest.skip(f"{table} missing - build this slice's dependencies first")
    yield connection
    connection.close()


@pytest.fixture(scope="module")
def raw_lines() -> list[str]:
    with open(GOLD_PATH, encoding="utf-8") as f:
        return [line for line in f.read().splitlines() if line.strip()]


@pytest.fixture(scope="module")
def gold_questions(raw_lines) -> list[GoldQuestion]:
    return [GoldQuestion.model_validate_json(line) for line in raw_lines]


def test_file_has_exactly_twenty_rows(raw_lines):
    assert len(raw_lines) == 20


def test_every_row_validates_against_gold_question(raw_lines):
    for line in raw_lines:
        parsed = json.loads(line)
        gq = GoldQuestion.model_validate_json(line)
        assert gq.bucket == "semantic"
        assert gq.question_id == parsed["question_id"]


def test_question_ids_are_unique(gold_questions):
    ids = [gq.question_id for gq in gold_questions]
    assert len(ids) == len(set(ids))


def test_all_semantic_questions_are_not_graceful_failure_cases(gold_questions):
    assert all(not gq.is_graceful_failure_case for gq in gold_questions)
    assert all(gq.gold_sql is not None and gq.gold_result is not None for gq in gold_questions)


def test_rerunning_every_gold_sql_reproduces_the_stored_gold_result_exactly(con, gold_questions):
    for gq in gold_questions:
        cursor = con.execute(gq.gold_sql)
        columns = [ColumnSpec(name=c[0], type=str(c[1])) for c in cursor.description]
        rows = [list(r) for r in cursor.fetchall()]
        live_result = ResultData(columns=columns, rows=rows)
        live_normalized = ResultData.model_validate_json(live_result.model_dump_json())
        assert compare(live_normalized, gq.gold_result), (
            f"{gq.question_id} ({gq.question_text!r}): live result no longer "
            f"matches stored gold_result"
        )


def test_all_nine_metrics_have_evidence_mapped(gold_questions):
    metric_ids = {m.metric_id for m in METRICS}
    assert set(METRIC_EVIDENCE) == metric_ids, "every metric_dictionary entry needs evidence"


def test_every_metric_is_exercised_by_at_least_one_gold_sql_fragment(gold_questions):
    by_id = {gq.question_id: gq for gq in gold_questions}
    for metric_id, (question_id, fragment) in METRIC_EVIDENCE.items():
        assert question_id in by_id, f"{metric_id}: {question_id} not found in gold set"
        gold_sql = by_id[question_id].gold_sql
        assert fragment in gold_sql, (
            f"{metric_id}: expected fragment {fragment!r} not found in "
            f"{question_id}'s gold_sql: {gold_sql!r}"
        )
