"""Product category classification per `_docs/scope.md`: builds
`dim_product_category` by classifying every unique `StockCode` into one
category from a fixed taxonomy, via a single offline GPT-4o-mini pass over
unique StockCode -> canonical-Description pairs. Runs once at build time;
the agent never calls an LLM to classify products at runtime - this is
reference data, per InformationModel.md's Layer 1.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Literal, get_args

import duckdb
from openai import OpenAI
from pydantic import BaseModel

from data_analyst_agent.data.ingest import DEFAULT_DB_PATH

MODEL = "gpt-4o-mini"
BATCH_SIZE = 100
MAX_ATTEMPTS_PER_BATCH = 3

FALLBACK_CATEGORY = "Other"

CategoryLiteral = Literal[
    "Home Decor",
    "Kitchen & Dining",
    "Christmas & Seasonal",
    "Toys & Games",
    "Stationery & Gift Wrap",
    "Bags & Accessories",
    "Garden & Outdoor",
    "Lighting",
    "Storage & Organization",
    "Bathroom",
    "Jewelry & Trinkets",
    "Textiles & Soft Furnishings",
    "Novelty & Party",
    "Other",
]
CATEGORIES: tuple[str, ...] = get_args(CategoryLiteral)


class ProductClassification(BaseModel):
    product_id: str
    category: CategoryLiteral


class CategorizationBatchResult(BaseModel):
    classifications: list[ProductClassification]


def _fetch_products(con: duckdb.DuckDBPyConnection) -> list[tuple[str, str]]:
    """One (product_id, description) pair per distinct StockCode in
    `raw_online_retail` - the most frequent non-null, non-blank description
    recorded for that StockCode, tie-broken alphabetically for determinism.
    A StockCode with no usable description anywhere gets `""`.
    """
    rows = con.execute(
        """
        WITH counts AS (
            SELECT StockCode, Description, COUNT(*) AS n
            FROM raw_online_retail
            WHERE Description IS NOT NULL AND TRIM(Description) != ''
            GROUP BY StockCode, Description
        ),
        ranked AS (
            SELECT
                StockCode,
                Description,
                ROW_NUMBER() OVER (
                    PARTITION BY StockCode ORDER BY n DESC, Description ASC
                ) AS rn
            FROM counts
        ),
        canonical AS (
            SELECT StockCode, Description FROM ranked WHERE rn = 1
        )
        SELECT DISTINCT r.StockCode, COALESCE(c.Description, '')
        FROM raw_online_retail r
        LEFT JOIN canonical c ON r.StockCode = c.StockCode
        ORDER BY r.StockCode
        """
    ).fetchall()
    return [(str(stock_code), description.strip()) for stock_code, description in rows]


def _classify_batch(client: OpenAI, products: list[tuple[str, str]]) -> dict[str, str]:
    """Classify one batch of (product_id, description) pairs. Returns a
    dict of every product_id in the batch to a category - falls back to
    FALLBACK_CATEGORY for any id the model's response omits, so a batch
    never produces a null/missing category for a product it was given.
    """
    listing = "\n".join(f"{product_id}: {description}" for product_id, description in products)
    last_error: Exception | None = None
    parsed: CategorizationBatchResult | None = None

    for _attempt in range(MAX_ATTEMPTS_PER_BATCH):
        try:
            completion = client.chat.completions.parse(
                model=MODEL,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "You classify UK giftware/homewares e-commerce products "
                            "into exactly one category from a fixed list, based only "
                            "on their product description. Pick the single "
                            "best-fitting category for every product listed. Use "
                            "'Other' only when no other category plausibly fits."
                        ),
                    },
                    {
                        "role": "user",
                        "content": (
                            f"Categories: {', '.join(CATEGORIES)}\n\n"
                            f"Classify every one of these {len(products)} products "
                            f"(product_id: description), one classification per "
                            f"product_id:\n{listing}"
                        ),
                    },
                ],
                response_format=CategorizationBatchResult,
            )
            parsed = completion.choices[0].message.parsed
            break
        except Exception as exc:  # noqa: BLE001 - retry any transient API failure
            last_error = exc
            time.sleep(1)

    if parsed is None:
        raise RuntimeError(
            f"Batch classification failed after {MAX_ATTEMPTS_PER_BATCH} attempts"
        ) from last_error

    results = {c.product_id: c.category for c in parsed.classifications}
    for product_id, _description in products:
        results.setdefault(product_id, FALLBACK_CATEGORY)
    return results


def categorize(db_path: Path | str | None = None, batch_size: int = BATCH_SIZE) -> int:
    """Build `dim_product_category`. Returns the row count written.
    Idempotent: rerunning replaces the table wholesale.
    """
    resolved_db_path = (
        Path(db_path)
        if db_path is not None
        else Path(os.environ.get("DUCKDB_PATH", str(DEFAULT_DB_PATH)))
    )
    con = duckdb.connect(str(resolved_db_path))
    try:
        products = _fetch_products(con)
        classifiable = [(pid, desc) for pid, desc in products if desc]
        no_description = [(pid, desc) for pid, desc in products if not desc]

        category_by_id: dict[str, str] = {pid: FALLBACK_CATEGORY for pid, _ in no_description}

        client = OpenAI()
        for i in range(0, len(classifiable), batch_size):
            batch = classifiable[i : i + batch_size]
            category_by_id.update(_classify_batch(client, batch))

        rows = [(pid, description, category_by_id[pid]) for pid, description in products]

        con.execute("DROP TABLE IF EXISTS dim_product_category")
        con.execute(
            "CREATE TABLE dim_product_category ("
            "product_id VARCHAR, description VARCHAR, category VARCHAR)"
        )
        con.executemany("INSERT INTO dim_product_category VALUES (?, ?, ?)", rows)
        (row_count,) = con.execute("SELECT COUNT(*) FROM dim_product_category").fetchone()
    finally:
        con.close()
    return row_count


if __name__ == "__main__":
    from dotenv import load_dotenv

    load_dotenv()
    written = categorize()
    print(f"Classified {written} products into dim_product_category")
