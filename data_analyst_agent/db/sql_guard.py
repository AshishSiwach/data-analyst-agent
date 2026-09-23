"""SQL statement-type validator per `_docs/Tools.md`'s guardrail contract:
accept a single SELECT/WITH statement, reject everything else, before any
query reaches the database.

Two checks, both structural (never a keyword/regex match, per
`_docs/technology_stack.md` §4's rationale for choosing sqlglot):

1. The parsed statement must be exactly one `exp.Query` (the sqlglot base
   class covering `SELECT`, `WITH ... SELECT`, and set operations
   UNION/EXCEPT/INTERSECT — a `WITH` clause attaches to its `Select` node
   rather than being its own top-level statement type). Anything else —
   INSERT/UPDATE/DELETE/DROP/CREATE/ALTER/MERGE, multiple statements in one
   string, or a string that fails to parse at all — is rejected here.
2. A full AST walk denies any write/DDL node found *anywhere* in the tree,
   including inside a CTE body — this is what catches a data-modifying CTE
   like `WITH x AS (DELETE FROM t RETURNING *) SELECT * FROM x`, which
   parses to a top-level `Select` and would slip past check 1 alone.
"""

from __future__ import annotations

from typing import Literal

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError

DIALECT = "duckdb"

_WRITE_NODE_TYPES = (
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Drop,
    exp.Create,
    exp.Alter,
    exp.Merge,
    exp.TruncateTable,
    exp.Command,  # sqlglot's catch-all for syntax it doesn't model explicitly
)


def validate(query: str) -> Literal["ok", "rejected"]:
    try:
        statements = [s for s in sqlglot.parse(query, read=DIALECT) if s is not None]
    except SqlglotError:
        return "rejected"

    if len(statements) != 1:
        return "rejected"

    statement = statements[0]
    if not isinstance(statement, exp.Query):
        return "rejected"

    if any(isinstance(node, _WRITE_NODE_TYPES) for node in statement.walk()):
        return "rejected"

    return "ok"
