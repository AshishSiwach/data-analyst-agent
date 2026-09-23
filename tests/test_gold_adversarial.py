"""S18 acceptance tests for eval/gold/adversarial.jsonl.

Same structural checks as test_gold_basic.py, plus S18's own required
checks: exactly 5-10 graceful-failure rows with no gold_sql, the rest
verified like S16/S17.
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
    Path(__file__).resolve().parent.parent
    / "data_analyst_agent"
    / "eval"
    / "gold"
    / "adversarial.jsonl"
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

NON_UK_KEYWORDS = ("non-UK", "outside the UK", "international")


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
        assert gq.bucket == "adversarial"
        assert gq.question_id == parsed["question_id"]


def test_question_ids_are_unique(gold_questions):
    ids = [gq.question_id for gq in gold_questions]
    assert len(ids) == len(set(ids))


def test_between_five_and_ten_graceful_failure_cases_with_no_gold_sql(gold_questions):
    graceful = [gq for gq in gold_questions if gq.is_graceful_failure_case]
    assert 5 <= len(graceful) <= 10
    for gq in graceful:
        assert gq.gold_sql is None
        assert gq.gold_result is None
        assert gq.expected_failure_category in {"schema_mismatch", "ambiguity"}


def test_the_rest_have_verified_gold_sql_and_gold_result(gold_questions):
    non_graceful = [gq for gq in gold_questions if not gq.is_graceful_failure_case]
    assert len(non_graceful) == 20 - sum(1 for gq in gold_questions if gq.is_graceful_failure_case)
    for gq in non_graceful:
        assert gq.gold_sql is not None
        assert gq.gold_result is not None
        assert gq.expected_failure_category is None


def test_at_least_three_questions_target_non_uk_markets(gold_questions):
    matches = [
        gq
        for gq in gold_questions
        if any(kw.lower() in gq.question_text.lower() for kw in NON_UK_KEYWORDS)
        or (gq.gold_sql is not None and "!= 'United Kingdom'" in gq.gold_sql)
    ]
    assert len(matches) >= 3, (
        f"expected >=3 non-UK-targeted questions, found {len(matches)}: "
        f"{[m.question_id for m in matches]}"
    )


def test_rerunning_every_non_graceful_gold_sql_reproduces_the_stored_gold_result(
    con, gold_questions
):
    for gq in gold_questions:
        if gq.is_graceful_failure_case:
            continue
        cursor = con.execute(gq.gold_sql)
        columns = [ColumnSpec(name=c[0], type=str(c[1])) for c in cursor.description]
        rows = [list(r) for r in cursor.fetchall()]
        live_result = ResultData(columns=columns, rows=rows)
        live_normalized = ResultData.model_validate_json(live_result.model_dump_json())
        assert compare(live_normalized, gq.gold_result), (
            f"{gq.question_id} ({gq.question_text!r}): live result no longer "
            f"matches stored gold_result"
        )
