"""Read-only DB connection helper per `_docs/Tools.md`'s auth/authz spec:
the connection is opened read-only, always - one of the two independent
structural write-blocks the tool contract requires (the other is
`db/sql_guard.py`'s statement-type validator). Even a bug in the parser,
or a query that somehow slips past it, cannot mutate the database through
a connection opened this way - DuckDB itself rejects the write, per
`_docs/CLAUDE.md`'s structural rule.
"""

from __future__ import annotations

import os
from pathlib import Path

import duckdb

from data_analyst_agent.data.ingest import DEFAULT_DB_PATH


def get_connection(db_path: Path | str | None = None) -> duckdb.DuckDBPyConnection:
    """Opens the DuckDB file at `db_path` (defaults to `$DUCKDB_PATH`, or
    `DEFAULT_DB_PATH`) read-only. Every code path that executes
    LLM-generated SQL must go through this function - never
    `duckdb.connect()` directly."""
    resolved_db_path = (
        Path(db_path)
        if db_path is not None
        else Path(os.environ.get("DUCKDB_PATH", str(DEFAULT_DB_PATH)))
    )
    return duckdb.connect(str(resolved_db_path), read_only=True)
