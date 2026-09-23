"""S06 acceptance tests for data.ingest.

Runs against the real Online Retail II source file (data/raw/, committed
to the repo per project decision) rather than a synthetic fixture, since
the required tests are specified against known, real values from that
file. `ingest()` reads a ~45MB workbook (~40s); to keep this file's total
runtime reasonable it is called exactly twice for the whole module (once,
then a rerun in place to prove replace-not-append), shared across every
test via a module-scoped fixture.
"""

from __future__ import annotations

from types import SimpleNamespace

import duckdb
import pytest

from data_analyst_agent.data.ingest import RAW_COLUMNS, ingest

EXPECTED_ROW_COUNT = 1_067_371
# Independently computed via `(df["Quantity"] * df["Price"]).sum()` per
# sheet, summed across both sheets, run by hand outside the ingestion
# pipeline - per S06's required evaluation case.
EXPECTED_TOTAL_GROSS_REVENUE = 19_287_250.568


@pytest.fixture(scope="module")
def ingested(tmp_path_factory):
    db_path = tmp_path_factory.mktemp("s06") / "test_ingest.duckdb"
    first_count = ingest(db_path=db_path)
    second_count = ingest(db_path=db_path)  # rerun in place: proves replace, not append
    return SimpleNamespace(db_path=db_path, first_count=first_count, second_count=second_count)


@pytest.fixture(scope="module")
def con(ingested):
    connection = duckdb.connect(str(ingested.db_path), read_only=True)
    yield connection
    connection.close()


def test_row_count_matches_known_source_count(con):
    (count,) = con.execute("SELECT COUNT(*) FROM raw_online_retail").fetchone()
    assert count == EXPECTED_ROW_COUNT


def test_all_raw_columns_are_present(con):
    columns = {row[0] for row in con.execute("DESCRIBE raw_online_retail").fetchall()}
    assert columns == set(RAW_COLUMNS)


def test_known_row_from_first_sheet_matches_exactly(con):
    row = con.execute(
        "SELECT InvoiceNo, StockCode, Description, Quantity, UnitPrice, CustomerID, Country "
        "FROM raw_online_retail WHERE InvoiceNo = '489434' AND StockCode = '85048'"
    ).fetchone()
    assert row == (
        "489434",
        "85048",
        "15CM CHRISTMAS GLASS BALL 20 LIGHTS",
        12,
        6.95,
        13085.0,
        "United Kingdom",
    )


def test_known_row_from_second_sheet_matches_exactly(con):
    row = con.execute(
        "SELECT InvoiceNo, StockCode, Description, Quantity, UnitPrice, CustomerID, Country "
        "FROM raw_online_retail WHERE InvoiceNo = '536365' AND StockCode = '85123A'"
    ).fetchone()
    assert row == (
        "536365",
        "85123A",
        "WHITE HANGING HEART T-LIGHT HOLDER",
        6,
        2.55,
        17850.0,
        "United Kingdom",
    )


def test_total_gross_revenue_matches_independently_computed_value(con):
    (revenue,) = con.execute("SELECT SUM(Quantity * UnitPrice) FROM raw_online_retail").fetchone()
    assert revenue == pytest.approx(EXPECTED_TOTAL_GROSS_REVENUE, rel=1e-6)


def test_ingest_returns_the_row_count_written(ingested):
    assert ingested.first_count == EXPECTED_ROW_COUNT


def test_rerunning_ingest_replaces_rather_than_appends(ingested, con):
    assert ingested.second_count == EXPECTED_ROW_COUNT
    (count,) = con.execute("SELECT COUNT(*) FROM raw_online_retail").fetchone()
    assert count == EXPECTED_ROW_COUNT
