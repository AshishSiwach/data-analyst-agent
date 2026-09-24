"""Metric dictionary seed data per `_docs/InformationModel.md` and
`_docs/scope.md`: the nine business-vocabulary terms the semantic layer
defines. Read-only reference data about the founder's business domain
(Layer 1), not application bookkeeping.

Per `_docs/Tools.md` Bucket 3, this is deliberately *not* a model-callable
tool - the full dictionary is cheap enough to always inject into the
agent's prompt context at turn start (`get_metrics()`), removing a whole
decision point. `seed_metrics()` additionally materializes the same data
into a queryable `metric_dictionary` DuckDB table, per this slice's Goal
("...as queryable reference data") and to keep this module consistent
with its siblings (ingest/categorize/views all build a table) - useful
for the eval harness or ad-hoc inspection, not for the agent's runtime
tool surface.

Bug found and fixed post-S25, during S26: `revenue`/`units_sold`'s
`sql_fragment`s originally referenced `net_line_revenue`/`net_quantity` -
columns that don't exist anywhere in the real schema (`v_order_lines`
only has `line_revenue`/`quantity`; "net" is achieved by summing every
row, since a return row's quantity/line_revenue is already negative, not
by a separately-named column). Confirmed via S25's live eval report: with
the broken fragment injected into `generate_sql`'s prompt, the model
couldn't use it literally and instead improvised `WHERE is_return =
FALSE` for "net of returns" questions - which computes the opposite of
net (excludes returns entirely, rather than letting their negative values
offset the total). Fixed to reference the real columns directly.
"""

from __future__ import annotations

import os
from pathlib import Path

import duckdb

from data_analyst_agent.data.ingest import DEFAULT_DB_PATH
from data_analyst_agent.models.entities import MetricDefinition

TABLE_NAME = "metric_dictionary"

METRICS: tuple[MetricDefinition, ...] = (
    MetricDefinition(
        metric_id="revenue",
        display_name="Revenue",
        definition="Sum of (quantity x unit price), net of returns.",
        # v_order_lines has no "net_"-prefixed column - a return row's
        # line_revenue is already negative, so summing line_revenue over
        # every row (no is_return filter) is what nets returns out.
        sql_fragment="SUM(line_revenue)",
        unit="currency",
        default_direction="desc",
    ),
    MetricDefinition(
        metric_id="aov",
        display_name="Average Order Value",
        definition="Revenue divided by order count, per period.",
        sql_fragment="SUM(net_revenue) / COUNT(DISTINCT order_id)",
        unit="currency",
        default_direction="desc",
    ),
    MetricDefinition(
        metric_id="order_count",
        display_name="Order Count",
        definition="Distinct invoices in period.",
        sql_fragment="COUNT(DISTINCT order_id)",
        unit="count",
        default_direction="desc",
    ),
    MetricDefinition(
        metric_id="units_sold",
        display_name="Units Sold",
        definition="Sum of quantity, net of returns.",
        # Same reasoning as revenue above: a return row's quantity is
        # already negative, so SUM(quantity) over every row nets it out -
        # there is no separate "net_quantity" column.
        sql_fragment="SUM(quantity)",
        unit="count",
        default_direction="desc",
    ),
    MetricDefinition(
        metric_id="repeat_rate",
        display_name="Repeat Customer Rate",
        definition="Customers with >= 2 orders in period, divided by total customers in period.",
        sql_fragment="COUNT(customers, order_count>=2) / COUNT(DISTINCT customer_id)",
        unit="percentage",
        default_direction="desc",
    ),
    MetricDefinition(
        metric_id="return_rate",
        display_name="Return Rate",
        definition="Returned value divided by gross value.",
        sql_fragment="SUM(returned_amount) / SUM(gross_revenue)",
        unit="percentage",
        default_direction="asc",
    ),
    MetricDefinition(
        metric_id="top_selling",
        display_name="Top / Worst Selling",
        definition="Rank products by revenue by default.",
        sql_fragment="ORDER BY <metric> DESC (worst: ASC)",
        unit="rank",
        default_direction="desc",
    ),
    MetricDefinition(
        metric_id="growth_rate",
        display_name="Growth Rate",
        definition="Period-over-period percent change of any metric.",
        sql_fragment="(current_period - prior_period) / prior_period",
        unit="percentage",
        default_direction="desc",
    ),
    MetricDefinition(
        metric_id="clv",
        display_name="Customer Lifetime Value",
        definition="Cumulative revenue per customer to date.",
        sql_fragment="SUM(net_revenue) GROUP BY customer_id",
        unit="currency",
        default_direction="desc",
    ),
)


def get_metrics() -> tuple[MetricDefinition, ...]:
    """The nine MetricDefinition rows, for prompt injection or any other
    in-process use - no DB round-trip needed."""
    return METRICS


def seed_metrics(db_path: Path | str | None = None) -> int:
    """Materialize METRICS into a queryable `metric_dictionary` table.
    Returns the row count written. Idempotent: rerunning replaces the
    table wholesale."""
    resolved_db_path = (
        Path(db_path)
        if db_path is not None
        else Path(os.environ.get("DUCKDB_PATH", str(DEFAULT_DB_PATH)))
    )
    con = duckdb.connect(str(resolved_db_path))
    try:
        con.execute(f"DROP TABLE IF EXISTS {TABLE_NAME}")
        con.execute(
            f"""
            CREATE TABLE {TABLE_NAME} (
                metric_id VARCHAR,
                display_name VARCHAR,
                definition VARCHAR,
                sql_fragment VARCHAR,
                unit VARCHAR,
                default_direction VARCHAR
            )
            """
        )
        rows = [
            (
                m.metric_id,
                m.display_name,
                m.definition,
                m.sql_fragment,
                m.unit,
                m.default_direction,
            )
            for m in METRICS
        ]
        con.executemany(f"INSERT INTO {TABLE_NAME} VALUES (?, ?, ?, ?, ?, ?)", rows)
        (row_count,) = con.execute(f"SELECT COUNT(*) FROM {TABLE_NAME}").fetchone()
    finally:
        con.close()
    return row_count


if __name__ == "__main__":
    written = seed_metrics()
    print(f"Seeded {written} rows into {TABLE_NAME}")
