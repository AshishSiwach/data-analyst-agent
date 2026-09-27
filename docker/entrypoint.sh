#!/bin/sh
# Builds the DuckDB database on first run if it doesn't already exist at
# $DUCKDB_PATH, then execs the container's CMD (or an override passed to
# `docker run`). Skipping the build when the file is already present lets
# a pre-built database be reproduced exactly by mounting it as a volume
# (e.g. the same file a local eval report was generated against), instead
# of re-running data/categorize.py's live LLM classification pass - which
# is not seeded and can assign different categories run to run - on every
# container start.
set -e

if [ ! -f "$DUCKDB_PATH" ]; then
    echo "No database found at $DUCKDB_PATH - building from source..."
    python -m data_analyst_agent.data.ingest
    python -m data_analyst_agent.data.categorize
    python -m data_analyst_agent.data.views --build v_orders
    python -m data_analyst_agent.data.views --build v_order_lines
    python -m data_analyst_agent.data.views --build v_customers
    python -m data_analyst_agent.data.views --build v_products
    python -m data_analyst_agent.data.views --build v_daily_revenue
    python -m data_analyst_agent.data.metrics
else
    echo "Using existing database at $DUCKDB_PATH"
fi

exec "$@"
