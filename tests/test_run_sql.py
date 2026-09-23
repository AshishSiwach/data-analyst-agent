"""S15 acceptance tests for db.run_sql.run_sql().

The "success against v_products" case needs the real, already-built
default DB (v_products depends on S07's real-cost dim_product_category);
every other case is self-contained against a tiny synthetic tmp_path DB
or DuckDB's built-in range() table function, needing no real data at all.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import duckdb
import pytest

from data_analyst_agent.data.ingest import DEFAULT_DB_PATH
from data_analyst_agent.db.run_sql import ROW_CAP, TIMEOUT_SECONDS, run_sql

_DEFAULT_DB_PATH = Path(os.environ.get("DUCKDB_PATH", str(DEFAULT_DB_PATH)))


@pytest.fixture
def empty_db_path(tmp_path):
    path = tmp_path / "empty.duckdb"
    con = duckdb.connect(str(path))
    con.execute("CREATE TABLE t (x INTEGER)")
    con.execute("INSERT INTO t VALUES (1), (2), (3)")
    con.close()
    return path


def test_success_against_v_products():
    if not _DEFAULT_DB_PATH.exists():
        pytest.skip(f"{_DEFAULT_DB_PATH} does not exist - build the pipeline first")
    con = duckdb.connect(str(_DEFAULT_DB_PATH), read_only=True)
    try:
        (expected_top_id, expected_revenue) = con.execute(
            "SELECT product_id, total_revenue FROM v_products ORDER BY total_revenue DESC LIMIT 1"
        ).fetchone()
    finally:
        con.close()

    result = run_sql(
        "SELECT product_id, total_revenue FROM v_products ORDER BY total_revenue DESC LIMIT 1",
        attempt_number=1,
        db_path=_DEFAULT_DB_PATH,
    )
    assert result.status == "success"
    assert result.row_count == 1
    assert result.truncated is False
    assert result.rows == [[expected_top_id, expected_revenue]]
    assert [c.name for c in result.columns] == ["product_id", "total_revenue"]


def test_rejected_does_not_touch_the_database(tmp_path):
    nonexistent_db_path = tmp_path / "never_created.duckdb"
    result = run_sql("DROP TABLE t", attempt_number=1, db_path=nonexistent_db_path)
    assert result.status == "rejected"
    assert result.error_message is not None
    assert not nonexistent_db_path.exists()


def test_error_status_for_a_runtime_sql_error(empty_db_path):
    result = run_sql(
        "SELECT * FROM totally_nonexistent_table",
        attempt_number=1,
        db_path=empty_db_path,
    )
    assert result.status == "error"
    assert result.error_message is not None
    assert result.rows is None


def test_timeout_status_for_a_deliberately_slow_query(empty_db_path):
    t0 = time.monotonic()
    result = run_sql(
        "SELECT COUNT(*) FROM range(300000) a, range(300000) b WHERE a.range % 7 = b.range % 11",
        attempt_number=1,
        db_path=empty_db_path,
    )
    wall_elapsed = time.monotonic() - t0
    assert result.status == "timeout"
    assert result.error_message is not None
    # The mechanism should cut off close to TIMEOUT_SECONDS, not run away.
    assert wall_elapsed < TIMEOUT_SECONDS + 3


def test_success_with_truncation_over_the_row_cap(empty_db_path):
    result = run_sql("SELECT * FROM range(15000)", attempt_number=1, db_path=empty_db_path)
    assert result.status == "success"
    assert result.row_count == 15000  # true, untruncated count
    assert result.truncated is True
    assert len(result.rows) == ROW_CAP


def test_execution_ms_is_populated_and_plausible(empty_db_path):
    result = run_sql("SELECT 1", attempt_number=1, db_path=empty_db_path)
    assert result.status == "success"
    assert isinstance(result.execution_ms, int)
    assert 0 <= result.execution_ms < 1000  # a trivial query should be near-instant


def test_attempt_number_out_of_range_raises(empty_db_path):
    with pytest.raises(ValueError):
        run_sql("SELECT 1", attempt_number=4, db_path=empty_db_path)
    with pytest.raises(ValueError):
        run_sql("SELECT 1", attempt_number=0, db_path=empty_db_path)
