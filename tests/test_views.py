"""S08+ acceptance tests for data.views. This file grows one `test_v_*`
function per view slice (S08-S12); S08 adds `test_v_orders`, S09 adds
`test_v_order_lines`.

Building v_orders and v_order_lines costs nothing (pure SQL over the
already-ingested raw table), so the v_orders tests below are fully
self-contained: they ingest into an isolated tmp_path DB and build the
view themselves.

v_order_lines is different: it depends on dim_product_category (S07),
which costs real (if small) money to build via the OpenAI API. Its tests
therefore follow S07's pattern instead - they target the already-built
default DB and skip with a clear message if raw_online_retail,
dim_product_category, or v_orders isn't there yet, rather than silently
re-triggering a paid classification pass on every test run.
"""

from __future__ import annotations

import os
from pathlib import Path

import duckdb
import pytest

from data_analyst_agent.data.ingest import DEFAULT_DB_PATH, ingest
from data_analyst_agent.data.views import build

# Independently computed via a separate pandas script (not this module's
# SQL) replicating the same three cleaning rules by hand, per S08's
# required evaluation case.
EXPECTED_UK_NET_REVENUE_2011 = 7_806_908.854

# Independently computed via a separate pandas script (not this module's
# SQL), per S09's required evaluation case.
EXPECTED_TOP_PRODUCT_ID = "84077"
EXPECTED_TOP_PRODUCT_UNITS_SOLD = 108_569

_DEFAULT_DB_PATH = Path(os.environ.get("DUCKDB_PATH", str(DEFAULT_DB_PATH)))


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


# --- v_order_lines (S09) ---


def _table_exists(connection: duckdb.DuckDBPyConnection, table: str) -> bool:
    (count,) = connection.execute(
        "SELECT COUNT(*) FROM information_schema.tables WHERE table_name = ?", [table]
    ).fetchone()
    return count > 0


def _real_db_connection(required_tables: tuple[str, ...]):
    """Connect to the already-built default DB, skipping with a clear
    message if it or any required table is missing, rather than silently
    rebuilding a costly (S07) dependency inside a test run."""
    if not _DEFAULT_DB_PATH.exists():
        pytest.skip(
            f"{_DEFAULT_DB_PATH} does not exist - run ingest, categorize, and "
            "`views --build v_orders` first"
        )
    connection = duckdb.connect(str(_DEFAULT_DB_PATH))
    for table in required_tables:
        if not _table_exists(connection, table):
            connection.close()
            pytest.skip(f"{table} missing - build this slice's dependencies first")
    return connection


@pytest.fixture(scope="module")
def real_con():
    connection = _real_db_connection(("raw_online_retail", "dim_product_category", "v_orders"))
    build("v_order_lines", db_path=_DEFAULT_DB_PATH)  # free to rebuild
    yield connection
    connection.close()


def test_v_order_lines(real_con):
    # Every order_id in v_order_lines exists in v_orders.
    (orphans,) = real_con.execute(
        "SELECT COUNT(*) FROM v_order_lines WHERE order_id NOT IN (SELECT order_id FROM v_orders)"
    ).fetchone()
    assert orphans == 0

    # is_return matches the negative-quantity/(corrected) InvoiceNo-prefix-C
    # rule, checked exhaustively rather than on just a sample.
    (mismatches,) = real_con.execute(
        "SELECT COUNT(*) FROM v_order_lines "
        "WHERE is_return != (order_id LIKE 'C%' AND quantity < 0)"
    ).fetchone()
    assert mismatches == 0

    # SUM(line_revenue) for non-return rows reconciles with v_orders.gross_revenue per order.
    (bad_orders,) = real_con.execute(
        """
        SELECT COUNT(*) FROM (
            SELECT l.order_id, SUM(l.line_revenue) AS line_sum, v.gross_revenue
            FROM v_order_lines l
            JOIN v_orders v ON v.order_id = l.order_id
            WHERE NOT l.is_return
            GROUP BY l.order_id, v.gross_revenue
        ) t
        WHERE ABS(t.line_sum - t.gross_revenue) > 0.01
        """
    ).fetchone()
    assert bad_orders == 0


def test_v_order_lines_schema_matches_information_model(real_con):
    columns = {row[0] for row in real_con.execute("DESCRIBE v_order_lines").fetchall()}
    assert columns == {
        "line_id",
        "order_id",
        "product_id",
        "category",
        "quantity",
        "unit_price",
        "line_revenue",
        "is_return",
    }


def test_v_order_lines_line_id_is_unique(real_con):
    (total,) = real_con.execute("SELECT COUNT(*) FROM v_order_lines").fetchone()
    (distinct,) = real_con.execute("SELECT COUNT(DISTINCT line_id) FROM v_order_lines").fetchone()
    assert total == distinct


def test_v_order_lines_category_is_never_null(real_con):
    (null_count,) = real_con.execute(
        "SELECT COUNT(*) FROM v_order_lines WHERE category IS NULL"
    ).fetchone()
    assert null_count == 0


def test_v_order_lines_excludes_pure_cancellation_lines(real_con):
    # Raw lines belonging to a cancellation invoice with no offsetting
    # order (S08) must not appear here either - v_order_lines is scoped to
    # raw lines whose InvoiceNo made it into v_orders.
    (raw_count,) = real_con.execute("SELECT COUNT(*) FROM raw_online_retail").fetchone()
    (kept_count,) = real_con.execute("SELECT COUNT(*) FROM v_order_lines").fetchone()
    assert kept_count < raw_count


def test_top_product_units_sold_matches_independently_computed_value(real_con):
    row = real_con.execute(
        """
        SELECT product_id, SUM(quantity) AS units_sold
        FROM v_order_lines
        GROUP BY product_id
        ORDER BY units_sold DESC
        LIMIT 1
        """
    ).fetchone()
    assert row == (EXPECTED_TOP_PRODUCT_ID, EXPECTED_TOP_PRODUCT_UNITS_SOLD)


# --- v_customers (S10) ---

# v_customers only depends on v_orders (free to build), so - unlike
# v_order_lines - there's no cost reason to isolate these tests in a fresh
# tmp_path DB. Reusing the already-built default DB just avoids paying
# another ~40s ingest() cycle for isolation that wouldn't buy much, since
# v_orders' own correctness is already covered by its own tests.

EXPECTED_CUSTOMER_ID = "17850"
# Manually verified: SELECT COUNT(*) FROM v_orders WHERE customer_id = '17850'.
EXPECTED_CUSTOMER_ORDER_COUNT = 159

# Independently computed via a separate pandas script (not this module's
# SQL), per S10's required evaluation case. Note this is a genuinely
# period-scoped metric (orders *within* 2011), which v_customers' own
# order_count column can't answer on its own since it's lifetime-scoped -
# computed here by filtering v_orders to 2011 directly, then applying the
# repeat_rate definition, per the metric dictionary.
EXPECTED_REPEAT_RATE_2011 = 2914 / 4232


@pytest.fixture(scope="module")
def customers_con():
    connection = _real_db_connection(("raw_online_retail", "v_orders"))
    build("v_customers", db_path=_DEFAULT_DB_PATH)  # free to rebuild
    yield connection
    connection.close()


def test_v_customers(customers_con):
    # Row count equals distinct non-null customer_id count in v_orders.
    (v_customers_count,) = customers_con.execute("SELECT COUNT(*) FROM v_customers").fetchone()
    (distinct_customers,) = customers_con.execute(
        "SELECT COUNT(DISTINCT customer_id) FROM v_orders WHERE customer_id IS NOT NULL"
    ).fetchone()
    assert v_customers_count == distinct_customers

    # order_count for a specific known customer matches a manually-verified count.
    (order_count,) = customers_con.execute(
        "SELECT order_count FROM v_customers WHERE customer_id = ?", [EXPECTED_CUSTOMER_ID]
    ).fetchone()
    assert order_count == EXPECTED_CUSTOMER_ORDER_COUNT

    # SUM(v_customers.lifetime_revenue) equals
    # SUM(v_orders.net_revenue WHERE customer_id IS NOT NULL).
    (lifetime_sum,) = customers_con.execute(
        "SELECT SUM(lifetime_revenue) FROM v_customers"
    ).fetchone()
    (net_revenue_sum,) = customers_con.execute(
        "SELECT SUM(net_revenue) FROM v_orders WHERE customer_id IS NOT NULL"
    ).fetchone()
    assert lifetime_sum == pytest.approx(net_revenue_sum, rel=1e-6)


def test_v_customers_schema_matches_information_model(customers_con):
    columns = {row[0] for row in customers_con.execute("DESCRIBE v_customers").fetchall()}
    assert columns == {
        "customer_id",
        "country",
        "first_order_date",
        "last_order_date",
        "order_count",
        "lifetime_revenue",
    }


def test_v_customers_has_no_null_customer_id_row(customers_con):
    (null_count,) = customers_con.execute(
        "SELECT COUNT(*) FROM v_customers WHERE customer_id IS NULL"
    ).fetchone()
    assert null_count == 0


def test_v_customers_first_and_last_order_date_bound_all_of_that_customers_orders(customers_con):
    (violations,) = customers_con.execute(
        """
        SELECT COUNT(*) FROM (
            SELECT c.customer_id
            FROM v_customers c
            JOIN v_orders o ON o.customer_id = c.customer_id
            WHERE o.order_date < c.first_order_date OR o.order_date > c.last_order_date
        )
        """
    ).fetchone()
    assert violations == 0


def test_repeat_rate_2011_matches_independently_computed_value(customers_con):
    (repeat_customers, total_customers) = customers_con.execute(
        """
        WITH per_customer_2011 AS (
            SELECT customer_id, COUNT(*) AS order_count
            FROM v_orders
            WHERE customer_id IS NOT NULL AND EXTRACT(YEAR FROM order_date) = 2011
            GROUP BY customer_id
        )
        SELECT
            COUNT(*) FILTER (WHERE order_count >= 2),
            COUNT(*)
        FROM per_customer_2011
        """
    ).fetchone()
    repeat_rate = repeat_customers / total_customers
    assert repeat_rate == pytest.approx(EXPECTED_REPEAT_RATE_2011, rel=1e-9)
