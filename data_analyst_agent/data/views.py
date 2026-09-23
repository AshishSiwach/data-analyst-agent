"""Cleaning script and semantic-layer views per `_docs/scope.md` /
`_docs/InformationModel.md`. Each view is a materialized DuckDB table (the
product-design sense of "view" - the curated semantic layer the agent
queries - not the SQL `VIEW` keyword), built from `raw_online_retail` (+
`dim_product_category` where needed). Built incrementally: S08 adds
`v_orders`; S09-S12 add the rest.

Cancellation-detection note (flagged per CLAUDE.md, not silently
resolved): `scope.md` says returns are "negative quantity, StockCode
prefixed C". Verified against the real data in S06: cancellations are
`InvoiceNo`-prefixed with `C`, not `StockCode`-prefixed - `StockCode`
values are never `C`-prefixed in this dataset. This module uses the
verified real pattern (`InvoiceNo LIKE 'C%'`).

"Offsetting order" note (a genuine design decision, not in any doc at this
precision - flagged): the raw data has no explicit link between a
cancellation invoice and the order it cancels (verified: stripping the `C`
prefix never matches a real invoice number). A cancellation invoice is
treated as having an offsetting order if, for its own `CustomerID`, at
least one non-cancelled invoice elsewhere in the raw data contains at
least one of the same `StockCode`s - i.e. this customer is known to have
actually bought at least one of the returned items. A cancellation with a
null `CustomerID`, or with zero `StockCode` overlap against that
customer's other orders, has no offsetting order and is excluded from
`v_orders` entirely (not zeroed). Verified against real data before
committing to this rule: of 8,292 distinct cancellation invoices, 7,276
have offsetting evidence and 1,016 don't (625 with a known customer and no
stock overlap, 391 with a null customer) - a plausible, non-degenerate
split, not "excludes everything" or "excludes nothing".
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

import duckdb

from data_analyst_agent.data.ingest import DEFAULT_DB_PATH

V_ORDERS_SQL = """
WITH cancel_invoices AS (
    SELECT DISTINCT InvoiceNo, CustomerID
    FROM raw_online_retail
    WHERE InvoiceNo LIKE 'C%'
),
cancel_without_offset AS (
    SELECT c.InvoiceNo
    FROM cancel_invoices c
    WHERE c.CustomerID IS NULL
       OR NOT EXISTS (
           SELECT 1
           FROM raw_online_retail cl
           JOIN raw_online_retail r
             ON r.StockCode = cl.StockCode
            AND r.CustomerID = c.CustomerID
            AND r.InvoiceNo NOT LIKE 'C%'
           WHERE cl.InvoiceNo = c.InvoiceNo
       )
),
kept AS (
    SELECT *
    FROM raw_online_retail
    WHERE InvoiceNo NOT IN (SELECT InvoiceNo FROM cancel_without_offset)
),
aggregated AS (
    SELECT
        InvoiceNo AS order_id,
        CAST(CAST(MAX(CustomerID) AS BIGINT) AS VARCHAR) AS customer_id,
        MAX(Country) AS country,
        CAST(MIN(InvoiceDate) AS DATE) AS order_date,
        CASE WHEN InvoiceNo LIKE 'C%' THEN 0.0 ELSE SUM(Quantity * UnitPrice) END AS gross_revenue,
        CASE
            WHEN InvoiceNo LIKE 'C%' THEN ABS(SUM(Quantity * UnitPrice))
            ELSE 0.0
        END AS returned_amount,
        SUM(Quantity) AS item_count
    FROM kept
    GROUP BY InvoiceNo
)
SELECT
    order_id,
    customer_id,
    country,
    order_date,
    gross_revenue,
    returned_amount,
    gross_revenue - returned_amount AS net_revenue,
    item_count
FROM aggregated
"""


def build_v_orders(con: duckdb.DuckDBPyConnection) -> int:
    con.execute("DROP TABLE IF EXISTS v_orders")
    con.execute(f"CREATE TABLE v_orders AS {V_ORDERS_SQL}")
    (row_count,) = con.execute("SELECT COUNT(*) FROM v_orders").fetchone()
    return row_count


# v_order_lines is scoped to raw lines whose InvoiceNo made it into v_orders
# (an inner join, not "every raw row unconditionally") - this is what makes
# "every order_id in v_order_lines exists in v_orders" (S09's required
# test) hold universally, including for kept cancellation invoices, not
# just non-cancelled ones. The ~1,016 pure-cancellation invoices S08
# excludes from v_orders are excluded here too, for the same reason: a
# line item with no valid parent order has nothing consistent to report.
#
# is_return uses the same corrected InvoiceNo-prefix pattern as v_orders,
# combined with the negative-quantity condition scope.md's rule states -
# verified against real data that this compound condition matters: exactly
# one cancellation-invoice row has non-negative quantity, so "is a
# cancellation invoice" alone isn't equivalent to the doc's rule.
V_ORDER_LINES_SQL = """
WITH lines AS (
    SELECT
        r.InvoiceNo,
        r.StockCode,
        r.Quantity,
        r.UnitPrice,
        ROW_NUMBER() OVER (PARTITION BY r.InvoiceNo ORDER BY r.StockCode) AS line_num
    FROM raw_online_retail r
    WHERE r.InvoiceNo IN (SELECT order_id FROM v_orders)
)
SELECT
    l.InvoiceNo || '-' || CAST(l.line_num AS VARCHAR) AS line_id,
    l.InvoiceNo AS order_id,
    l.StockCode AS product_id,
    COALESCE(c.category, 'Other') AS category,
    l.Quantity AS quantity,
    l.UnitPrice AS unit_price,
    l.Quantity * l.UnitPrice AS line_revenue,
    (l.InvoiceNo LIKE 'C%' AND l.Quantity < 0) AS is_return
FROM lines l
LEFT JOIN dim_product_category c ON c.product_id = l.StockCode
"""


def build_v_order_lines(con: duckdb.DuckDBPyConnection) -> int:
    con.execute("DROP TABLE IF EXISTS v_order_lines")
    con.execute(f"CREATE TABLE v_order_lines AS {V_ORDER_LINES_SQL}")
    (row_count,) = con.execute("SELECT COUNT(*) FROM v_order_lines").fetchone()
    return row_count


BUILDERS = {
    "v_orders": build_v_orders,
    "v_order_lines": build_v_order_lines,
}


def build(view_name: str, db_path: Path | str | None = None) -> int:
    if view_name not in BUILDERS:
        raise ValueError(f"Unknown view {view_name!r}. Known views: {sorted(BUILDERS)}")
    resolved_db_path = (
        Path(db_path)
        if db_path is not None
        else Path(os.environ.get("DUCKDB_PATH", str(DEFAULT_DB_PATH)))
    )
    con = duckdb.connect(str(resolved_db_path))
    try:
        return BUILDERS[view_name](con)
    finally:
        con.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build", required=True, choices=sorted(BUILDERS), help="View to build")
    args = parser.parse_args()
    row_count = build(args.build)
    print(f"Built {args.build}: {row_count} rows")


if __name__ == "__main__":
    main()
