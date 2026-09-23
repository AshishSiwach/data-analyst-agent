"""S08+ acceptance tests for data.views. This file grows one `test_v_*`
function per view slice (S08-S12); S08 adds `test_v_orders`.

Building v_orders costs nothing (pure SQL over the already-ingested raw
table), so - unlike S07's categorize tests - this file is fully
self-contained: it ingests into an isolated tmp_path DB and builds the
view itself, rather than assuming a pre-built table.
"""

from __future__ import annotations

import duckdb
import pytest

from data_analyst_agent.data.ingest import ingest
from data_analyst_agent.data.views import build

# Independently computed via a separate pandas script (not this module's
# SQL) replicating the same three cleaning rules by hand, per S08's
# required evaluation case.
EXPECTED_UK_NET_REVENUE_2011 = 7_806_908.854


@pytest.fixture(scope="module")
def db_path(tmp_path_factory):
    path = tmp_path_factory.mktemp("s08") / "test_views.duckdb"
    ingest(db_path=path)
    build("v_orders", db_path=path)
    return path


@pytest.fixture(scope="module")
def con(db_path):
    connection = duckdb.connect(str(db_path), read_only=True)
    yield connection
    connection.close()


def test_v_orders(con):
    # SUM(v_orders.gross_revenue) equals SUM(raw_online_retail non-return rows' revenue).
    (v_orders_gross,) = con.execute("SELECT SUM(gross_revenue) FROM v_orders").fetchone()
    (raw_gross,) = con.execute(
        "SELECT SUM(Quantity * UnitPrice) FROM raw_online_retail WHERE InvoiceNo NOT LIKE 'C%'"
    ).fetchone()
    assert v_orders_gross == pytest.approx(raw_gross, rel=1e-6)

    # Every customer_id IS NULL row in v_orders corresponds to a null CustomerID in the raw table.
    (mismatched,) = con.execute(
        """
        SELECT COUNT(*) FROM v_orders v
        WHERE v.customer_id IS NULL
          AND v.order_id NOT IN (
              SELECT DISTINCT InvoiceNo FROM raw_online_retail WHERE CustomerID IS NULL
          )
        """
    ).fetchone()
    assert mismatched == 0

    # Row count of v_orders is strictly less than distinct InvoiceNo count in
    # the raw table (cancellations without an offsetting order removed).
    (v_orders_count,) = con.execute("SELECT COUNT(*) FROM v_orders").fetchone()
    (raw_invoice_count,) = con.execute(
        "SELECT COUNT(DISTINCT InvoiceNo) FROM raw_online_retail"
    ).fetchone()
    assert v_orders_count < raw_invoice_count


def test_v_orders_schema_matches_information_model(con):
    columns = {row[0]: row[1] for row in con.execute("DESCRIBE v_orders").fetchall()}
    assert set(columns) == {
        "order_id",
        "customer_id",
        "country",
        "order_date",
        "gross_revenue",
        "returned_amount",
        "net_revenue",
        "item_count",
    }


def test_net_revenue_equals_gross_minus_returned_for_every_row(con):
    (bad_rows,) = con.execute(
        "SELECT COUNT(*) FROM v_orders "
        "WHERE ABS(net_revenue - (gross_revenue - returned_amount)) > 1e-6"
    ).fetchone()
    assert bad_rows == 0


def test_cancellation_invoices_without_offsetting_order_are_excluded_not_zeroed(con):
    # A cancellation invoice kept in v_orders must have offsetting evidence:
    # its CustomerID must be non-null and must appear on a non-cancelled
    # invoice sharing at least one StockCode with this cancellation.
    (violations,) = con.execute(
        """
        SELECT COUNT(*) FROM v_orders v
        WHERE v.order_id LIKE 'C%'
          AND (
            v.customer_id IS NULL
            OR NOT EXISTS (
                SELECT 1
                FROM raw_online_retail cl
                JOIN raw_online_retail r
                  ON r.StockCode = cl.StockCode
                 AND CAST(CAST(r.CustomerID AS BIGINT) AS VARCHAR) = v.customer_id
                 AND r.InvoiceNo NOT LIKE 'C%'
                WHERE cl.InvoiceNo = v.order_id
            )
          )
        """
    ).fetchone()
    assert violations == 0


def test_return_invoice_rows_have_zero_gross_and_positive_returned_amount(con):
    rows = con.execute(
        "SELECT gross_revenue, returned_amount FROM v_orders WHERE order_id LIKE 'C%'"
    ).fetchall()
    assert rows, "expected at least one kept cancellation invoice"
    assert all(gross == 0.0 and returned > 0.0 for gross, returned in rows)


def test_uk_net_revenue_2011_matches_independently_computed_value(con):
    (revenue,) = con.execute(
        """
        SELECT SUM(net_revenue) FROM v_orders
        WHERE country = 'United Kingdom' AND EXTRACT(YEAR FROM order_date) = 2011
        """
    ).fetchone()
    assert revenue == pytest.approx(EXPECTED_UK_NET_REVENUE_2011, rel=1e-6)
