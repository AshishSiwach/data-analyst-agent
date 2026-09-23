"""S16 acceptance tests for eval/gold/basic.jsonl.

Depends on the full real pipeline (raw table through all five views,
including dim_product_category, S07's real-cost dependency via
v_order_lines/v_products) - follows the same skip-if-missing pattern as
test_views.py's real_con fixture rather than rebuilding from scratch.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import duckdb
import pytest

from data_analyst_agent.data.ingest import DEFAULT_DB_PATH
from data_analyst_agent.eval.comparator import compare
from data_analyst_agent.models.entities import ColumnSpec, GoldQuestion, ResultData

GOLD_PATH = (
    Path(__file__).resolve().parent.parent / "data_analyst_agent" / "eval" / "gold" / "basic.jsonl"
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
        parsed = json.loads(line)  # raises if any line is malformed
        gq = GoldQuestion.model_validate_json(line)
        assert gq.bucket == "basic"
        assert gq.question_id == parsed["question_id"]


def test_question_ids_are_unique(gold_questions):
    ids = [gq.question_id for gq in gold_questions]
    assert len(ids) == len(set(ids))


def test_all_basic_questions_are_not_graceful_failure_cases(gold_questions):
    # is_graceful_failure_case is only meaningful within the adversarial
    # bucket (S18); every basic question has a real, verifiable answer.
    assert all(not gq.is_graceful_failure_case for gq in gold_questions)
    assert all(gq.gold_sql is not None and gq.gold_result is not None for gq in gold_questions)


def test_rerunning_every_gold_sql_reproduces_the_stored_gold_result_exactly(con, gold_questions):
    for gq in gold_questions:
        cursor = con.execute(gq.gold_sql)
        columns = [ColumnSpec(name=c[0], type=str(c[1])) for c in cursor.description]
        rows = [list(r) for r in cursor.fetchall()]
        live_result = ResultData(columns=columns, rows=rows)

        # Normalize through the same JSON round-trip the stored file went
        # through (e.g. date -> ISO string), so a live datetime.date and a
        # stored "YYYY-MM-DD" string compare like-for-like.
        live_normalized = ResultData.model_validate_json(live_result.model_dump_json())

        assert compare(live_normalized, gq.gold_result), (
            f"{gq.question_id} ({gq.question_text!r}): live result no longer "
            f"matches stored gold_result - {live_normalized.rows!r} vs "
            f"{gq.gold_result.rows!r}"
        )
