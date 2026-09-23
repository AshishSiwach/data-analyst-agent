"""The `run_sql` tool per `_docs/Tools.md`'s exact contract: validate (S02)
-> execute read-only (S14) -> 5s timeout -> 10k row cap -> a typed
`SqlExecutionResult`. This is the one genuine agent-invoked tool in the
system (`_docs/autonomy.md`: L2, autonomous within hard bounds, fully
logged) - every bound here is hardcoded, not configurable at the call
site, per `_docs/CLAUDE.md`'s structural rule.

Timeout mechanism: DuckDB has no native query-timeout setting (verified -
`SET query_timeout` and `PRAGMA set_timeout` both fail with
`CatalogException: unrecognized configuration parameter`). Enforced here
instead by running the query on a worker thread and calling
`connection.interrupt()` from the caller's thread if it's still running
after `TIMEOUT_SECONDS` - verified empirically this cancels the query
within the same second and leaves the connection usable for the next
attempt (`InterruptException` is a `duckdb.Error` subclass, not a crash).
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

import duckdb

from data_analyst_agent.db.connection import get_connection
from data_analyst_agent.db.sql_guard import validate
from data_analyst_agent.models.entities import ColumnSpec, SqlExecutionResult

TIMEOUT_SECONDS = 5
ROW_CAP = 10_000


def run_sql(
    query: str, attempt_number: int, db_path: Path | str | None = None
) -> SqlExecutionResult:
    """Executes `query` (SELECT/WITH only) and returns a typed result.
    `attempt_number` (1-3) is accepted per Tools.md's input schema for the
    caller's own logging - this function is stateless and retry-agnostic,
    it never uses it internally.
    """
    if not (1 <= attempt_number <= 3):
        raise ValueError(f"attempt_number must be 1-3, got {attempt_number}")

    start = time.monotonic()

    if validate(query) == "rejected":
        return SqlExecutionResult(
            status="rejected",
            error_message="Only a single SELECT or WITH statement is permitted.",
            truncated=False,
            execution_ms=_elapsed_ms(start),
        )

    con = get_connection(db_path)
    try:
        outcome = _execute_with_timeout(con, query, TIMEOUT_SECONDS)
    finally:
        con.close()

    execution_ms = _elapsed_ms(start)

    if outcome["status"] == "timeout":
        return SqlExecutionResult(
            status="timeout",
            error_message=f"Query exceeded the {TIMEOUT_SECONDS}-second timeout.",
            truncated=False,
            execution_ms=execution_ms,
        )
    if outcome["status"] == "error":
        return SqlExecutionResult(
            status="error",
            error_message=str(outcome["error"]),
            truncated=False,
            execution_ms=execution_ms,
        )

    all_rows: list[list[Any]] = outcome["rows"]
    row_count = len(all_rows)
    truncated = row_count > ROW_CAP
    rows = all_rows[:ROW_CAP] if truncated else all_rows

    return SqlExecutionResult(
        status="success",
        columns=outcome["columns"],
        rows=rows,
        row_count=row_count,
        truncated=truncated,
        execution_ms=execution_ms,
    )


def _elapsed_ms(start: float) -> int:
    return int((time.monotonic() - start) * 1000)


def _execute_with_timeout(
    con: duckdb.DuckDBPyConnection, query: str, timeout_seconds: float
) -> dict[str, Any]:
    result: dict[str, Any] = {}

    def worker() -> None:
        try:
            cursor = con.execute(query)
            columns = [ColumnSpec(name=c[0], type=str(c[1])) for c in cursor.description]
            rows = [list(row) for row in cursor.fetchall()]
            result["status"] = "success"
            result["columns"] = columns
            result["rows"] = rows
        except duckdb.Error as exc:
            result["status"] = "error"
            result["error"] = exc

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    thread.join(timeout=timeout_seconds)

    if thread.is_alive():
        con.interrupt()
        thread.join(timeout=timeout_seconds)
        # Whatever the worker recorded after being interrupted is
        # discarded - the caller already exceeded its budget.
        return {"status": "timeout"}

    return result
