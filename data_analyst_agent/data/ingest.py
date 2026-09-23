"""Raw data ingestion per `_docs/scope.md`: loads the Online Retail II
source file into a raw DuckDB table, unmodified - no cleaning, no derived
columns, no filtering, no row drops. Cleaning rules are applied later, in
`data/views.py` (S08+), not here.

Source-column naming note (flagged per CLAUDE.md, not silently resolved):
`scope.md` and this slice's own acceptance criteria in
`implementation_plan.md` name the raw columns as
`InvoiceNo`/`UnitPrice`/`CustomerID` - the naming convention of the older,
single-year "Online Retail" dataset. The actual "Online Retail II" file
that `scope.md` specifies as the chosen dataset ships different raw
headers: `Invoice`, `Price`, `Customer ID`. This module renames columns to
the doc's stated names during load - a pure metadata rename, every row and
every cell value is preserved unchanged - so the raw table matches what
every downstream doc and this slice's own required tests expect.
"""

from __future__ import annotations

import os
from pathlib import Path

import duckdb
import pandas as pd

SOURCE_PATH = Path("data/raw/online_retail_II.xlsx")
SHEET_NAMES = ["Year 2009-2010", "Year 2010-2011"]

# Source workbook header -> InformationModel.md / scope.md's documented raw column name.
_COLUMN_RENAME = {
    "Invoice": "InvoiceNo",
    "Price": "UnitPrice",
    "Customer ID": "CustomerID",
}

RAW_COLUMNS = [
    "InvoiceNo",
    "StockCode",
    "Description",
    "Quantity",
    "InvoiceDate",
    "UnitPrice",
    "CustomerID",
    "Country",
]

DEFAULT_DB_PATH = Path("data_analyst_agent.duckdb")


def load_source(source_path: Path | str = SOURCE_PATH) -> pd.DataFrame:
    """Read both sheets of the source workbook, concatenate them in sheet
    (chronological) order, and rename columns to InformationModel.md's raw
    column names. No rows are dropped, no values are changed, no columns
    are computed - a straight read-and-rename.
    """
    sheets = [pd.read_excel(source_path, sheet_name=name) for name in SHEET_NAMES]
    df = pd.concat(sheets, ignore_index=True)
    df = df.rename(columns=_COLUMN_RENAME)
    return df[RAW_COLUMNS]


def ingest(source_path: Path | str = SOURCE_PATH, db_path: Path | str | None = None) -> int:
    """Load the source file into a `raw_online_retail` table in the DuckDB
    file at `db_path` (defaults to `$DUCKDB_PATH`, or `DEFAULT_DB_PATH`).
    Returns the row count written. Idempotent: rerunning replaces the
    table wholesale rather than appending to it or erroring.
    """
    resolved_db_path = (
        Path(db_path)
        if db_path is not None
        else Path(os.environ.get("DUCKDB_PATH", str(DEFAULT_DB_PATH)))
    )
    df = load_source(source_path)  # noqa: F841 - read by DuckDB's replacement scan below, not by name
    con = duckdb.connect(str(resolved_db_path))
    try:
        con.execute("CREATE OR REPLACE TABLE raw_online_retail AS SELECT * FROM df")
        (row_count,) = con.execute("SELECT COUNT(*) FROM raw_online_retail").fetchone()
    finally:
        con.close()
    return row_count


if __name__ == "__main__":
    written = ingest()
    print(f"Ingested {written} rows into raw_online_retail")
