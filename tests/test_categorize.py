"""S07 acceptance tests for data.categorize.

These check the *already-built* `dim_product_category` table (per this
slice's own verification command: `python -m
data_analyst_agent.data.categorize && pytest tests/test_categorize.py
-v` - categorize runs once, as a separate step, before pytest). Classifying
5,305 products costs real (small) money via the OpenAI API, so pytest
itself never triggers that pass - if the table isn't there yet, these
tests skip with a message telling the developer to run the categorize
script first, rather than silently spending money on every test run.
"""

from __future__ import annotations

import os
from pathlib import Path

import duckdb
import pytest

from data_analyst_agent.data.categorize import CATEGORIES, FALLBACK_CATEGORY
from data_analyst_agent.data.ingest import DEFAULT_DB_PATH

_DB_PATH = Path(os.environ.get("DUCKDB_PATH", str(DEFAULT_DB_PATH)))


def _table_exists(con: duckdb.DuckDBPyConnection, table: str) -> bool:
    (count,) = con.execute(
        "SELECT COUNT(*) FROM information_schema.tables WHERE table_name = ?", [table]
    ).fetchone()
    return count > 0


@pytest.fixture(scope="module")
def con():
    if not _DB_PATH.exists():
        pytest.skip(
            f"{_DB_PATH} does not exist - run `python -m data_analyst_agent.data.ingest` first"
        )
    connection = duckdb.connect(str(_DB_PATH), read_only=True)
    if not _table_exists(connection, "raw_online_retail"):
        connection.close()
        pytest.skip(
            "raw_online_retail missing - run `python -m data_analyst_agent.data.ingest` first"
        )
    if not _table_exists(connection, "dim_product_category"):
        connection.close()
        pytest.skip(
            "dim_product_category missing - run "
            "`python -m data_analyst_agent.data.categorize` first"
        )
    yield connection
    connection.close()


def test_every_stock_code_has_exactly_one_row(con):
    (distinct_stock_codes,) = con.execute(
        "SELECT COUNT(DISTINCT StockCode) FROM raw_online_retail"
    ).fetchone()
    (category_rows,) = con.execute("SELECT COUNT(*) FROM dim_product_category").fetchone()
    assert category_rows == distinct_stock_codes

    (missing,) = con.execute(
        """
        SELECT COUNT(*) FROM (
            SELECT DISTINCT StockCode FROM raw_online_retail
            EXCEPT
            SELECT product_id FROM dim_product_category
        )
        """
    ).fetchone()
    assert missing == 0


def test_no_duplicate_product_ids(con):
    (total,) = con.execute("SELECT COUNT(*) FROM dim_product_category").fetchone()
    (distinct,) = con.execute(
        "SELECT COUNT(DISTINCT product_id) FROM dim_product_category"
    ).fetchone()
    assert total == distinct


def test_no_null_categories(con):
    (null_count,) = con.execute(
        "SELECT COUNT(*) FROM dim_product_category WHERE category IS NULL"
    ).fetchone()
    assert null_count == 0


def test_every_category_is_from_the_fixed_taxonomy(con):
    rows = con.execute("SELECT DISTINCT category FROM dim_product_category").fetchall()
    used_categories = {row[0] for row in rows}
    assert used_categories.issubset(set(CATEGORIES))


def test_stock_codes_with_no_description_fall_back_to_other(con):
    rows = con.execute(
        "SELECT category FROM dim_product_category WHERE description = ''"
    ).fetchall()
    assert rows, "expected at least one StockCode with no usable description in this dataset"
    assert all(category == FALLBACK_CATEGORY for (category,) in rows)


# --- Mocked unit test: covers _classify_batch's missing-id fallback logic
# directly, without any network call. Not required by S07, but exercises a
# defensive code path (the model omitting a requested product_id) that the
# real-data tests above wouldn't reliably trigger.


def test_classify_batch_fills_in_missing_ids_with_fallback_category():
    from data_analyst_agent.data.categorize import (
        CategorizationBatchResult,
        ProductClassification,
        _classify_batch,
    )

    class _FakeMessage:
        def __init__(self, parsed):
            self.parsed = parsed

    class _FakeChoice:
        def __init__(self, parsed):
            self.message = _FakeMessage(parsed)

    class _FakeCompletion:
        def __init__(self, parsed):
            self.choices = [_FakeChoice(parsed)]

    class _FakeCompletionsAPI:
        def parse(self, **kwargs):
            # Only classify the first product; the second is left out, to
            # exercise the fallback path.
            return _FakeCompletion(
                CategorizationBatchResult(
                    classifications=[ProductClassification(product_id="A1", category="Home Decor")]
                )
            )

    class _FakeChatAPI:
        completions = _FakeCompletionsAPI()

    class _FakeClient:
        chat = _FakeChatAPI()

    results = _classify_batch(_FakeClient(), [("A1", "a vase"), ("B2", "a mug")])
    assert results["A1"] == "Home Decor"
    assert results["B2"] == FALLBACK_CATEGORY
