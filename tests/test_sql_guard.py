"""S02 acceptance tests: >=12 parametrized cases (>=6 accept, >=6 reject)
covering every case sql_guard's docstring/S02's acceptance criteria name."""

from __future__ import annotations

import pytest

from data_analyst_agent.db.sql_guard import validate

ACCEPT_CASES = [
    ("plain_select", "SELECT * FROM v_orders"),
    ("with_select", "WITH cte AS (SELECT 1 AS x) SELECT * FROM cte"),
    ("union_of_selects", "SELECT 1 AS x UNION SELECT 2 AS x"),
    ("trailing_semicolon", "SELECT 1;"),
    (
        "comment_with_semicolon_inside_is_not_a_statement_separator",
        "SELECT * FROM v_orders WHERE 1=1 -- ; not a real statement separator",
    ),
    ("block_comment", "SELECT 1 /* just a comment */"),
    ("subquery_in_from", "SELECT * FROM (SELECT 1 AS x) AS sub"),
    ("multiple_read_only_ctes", "WITH a AS (SELECT 1), b AS (SELECT 2) SELECT * FROM a, b"),
]

REJECT_CASES = [
    ("insert", "INSERT INTO v_orders VALUES (1)"),
    ("update", "UPDATE v_orders SET net_revenue = 0"),
    ("delete", "DELETE FROM v_orders"),
    ("drop", "DROP TABLE v_orders"),
    ("create", "CREATE TABLE t (x INT)"),
    ("alter", "ALTER TABLE v_orders ADD COLUMN y INT"),
    ("merge", "MERGE INTO t USING s ON t.id = s.id WHEN MATCHED THEN UPDATE SET t.x = s.x"),
    ("truncate", "TRUNCATE TABLE v_orders"),
    (
        "with_clause_smuggling_an_insert",
        "WITH x AS (INSERT INTO v_orders VALUES (1) RETURNING *) SELECT * FROM x",
    ),
    (
        "with_clause_smuggling_a_delete",
        "WITH deleted AS (DELETE FROM v_orders RETURNING *) SELECT * FROM deleted",
    ),
    ("multi_statement_string", "SELECT 1; DROP TABLE v_orders"),
    (
        "comment_obfuscated_second_statement",
        "SELECT 1; -- looks like the query ends here\nDROP TABLE v_orders;",
    ),
    ("empty_string", ""),
    ("whitespace_only", "   "),
    ("unparseable_garbage", "not sql at all !!!"),
]


@pytest.mark.parametrize("query", [q for _, q in ACCEPT_CASES], ids=[i for i, _ in ACCEPT_CASES])
def test_accepts(query):
    assert validate(query) == "ok"


@pytest.mark.parametrize("query", [q for _, q in REJECT_CASES], ids=[i for i, _ in REJECT_CASES])
def test_rejects(query):
    assert validate(query) == "rejected"


def test_at_least_six_accept_and_six_reject_cases_exist():
    assert len(ACCEPT_CASES) >= 6
    assert len(REJECT_CASES) >= 6
