"""S14 acceptance tests for db.connection.get_connection()."""

from __future__ import annotations

import duckdb
import pytest

from data_analyst_agent.db.connection import get_connection


@pytest.fixture
def seeded_db_path(tmp_path):
    """A small DB file with one table, written by a normal (writable)
    connection - get_connection() itself must never be able to create or
    write to a file, only open an existing one read-only."""
    path = tmp_path / "test_connection.duckdb"
    setup_con = duckdb.connect(str(path))
    setup_con.execute("CREATE TABLE t (x INTEGER)")
    setup_con.execute("INSERT INTO t VALUES (1), (2), (3)")
    setup_con.close()
    return path


def test_select_succeeds(seeded_db_path):
    con = get_connection(seeded_db_path)
    try:
        rows = con.execute("SELECT * FROM t ORDER BY x").fetchall()
        assert rows == [(1,), (2,), (3,)]
    finally:
        con.close()


def test_insert_raises_a_database_level_permission_error(seeded_db_path):
    con = get_connection(seeded_db_path)
    try:
        with pytest.raises(duckdb.InvalidInputException, match="read-only"):
            con.execute("INSERT INTO t VALUES (4)")
    finally:
        con.close()


def test_create_table_raises_a_database_level_permission_error(seeded_db_path):
    con = get_connection(seeded_db_path)
    try:
        with pytest.raises(duckdb.InvalidInputException, match="read-only"):
            con.execute("CREATE TABLE other (y INTEGER)")
    finally:
        con.close()


def test_write_attempts_do_not_actually_mutate_the_database(seeded_db_path):
    con = get_connection(seeded_db_path)
    try:
        with pytest.raises(duckdb.InvalidInputException):
            con.execute("INSERT INTO t VALUES (999)")
        # The failed write must not have partially applied.
        rows = con.execute("SELECT * FROM t ORDER BY x").fetchall()
        assert rows == [(1,), (2,), (3,)]
    finally:
        con.close()


def test_get_connection_uses_duckdb_path_env_var(monkeypatch, seeded_db_path):
    monkeypatch.setenv("DUCKDB_PATH", str(seeded_db_path))
    con = get_connection()
    try:
        rows = con.execute("SELECT * FROM t ORDER BY x").fetchall()
        assert rows == [(1,), (2,), (3,)]
    finally:
        con.close()
