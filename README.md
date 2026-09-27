# Data Analyst Agent

A portfolio-first descriptive SQL analyst agent for a UK-based e-commerce dataset.

**Premise:** *Ask business questions in plain English, get correct answers backed by inspectable SQL, with an eval harness that proves it.* The differentiator is the eval — result-based grading against a hand-crafted gold set — not the agent itself.

**Persona:** a UK-based solo founder of an online giftware/homewares store, with some international customers. The dataset (Online Retail II, UCI) is that shape, so the persona and the data agree.

**Live demo:** **https://data-analyst-agent-shopify.streamlit.app/**

Try any of the three questions the whole build was validated against:
- *"Which products are selling the most in which region?"*
- *"What is my average order value across all regions?"*
- *"Which products are the least performing?"*

## Status

All 30 slices of the [implementation plan](_docs/implementation_plan.md) are complete: agent, eval harness, Streamlit UI, Docker image, and the hosted demo above are all live.

## How it works

A founder's question goes through a bounded, code-controlled pipeline, not an open-ended agent loop: **generate SQL → validate → execute (read-only, 5s timeout, 10k-row cap) → on error, retry with the failure fed back to the model, capped at 3 attempts → on success, a deterministic rule picks a chart type and an LLM wraps the result in one sentence → on exhausted retries, a second LLM call diagnoses *why* and suggests a rephrase**, rather than surfacing a raw stack trace. `agent/` holds each stage as its own module; `data/views.py` is the semantic layer the agent actually queries (`v_orders`, `v_order_lines`, `v_customers`, `v_products`, `v_daily_revenue`) — built once from the raw data, so the agent never re-derives a business definition (like "net revenue" or "lifetime value") from scratch inside a prompt. Full design rationale in [`_docs/Architecture.md`](_docs/Architecture.md).

## Results (final harness run)

| Bucket | Accuracy | Floor (`scope.md`) |
|---|---|---|
| Basic (20 questions) | 95.0% | ≥75% |
| Semantic (20 questions) | 95.0% | ≥55% |
| Adversarial (20 questions) | 90.0% | ≥40% |
| **Graceful failure rate** | 100.0% | ≥80% |

Median happy-path turn latency: ~1.8s (well under the <8s floor).

Git commit `0c0cc66`, prompt hash `ae0c58f4725f`, 60 hand-crafted (question, gold-SQL, gold-result) triples, 20 per bucket. Reproduce this exact run:

```bash
uv run python -m data_analyst_agent.eval.harness --gold data_analyst_agent/eval/gold --out report/
```

`report/report.md` is stamped with the git commit and a hash of the active prompt (including [`skills/sql_domain_knowledge.md`](data_analyst_agent/skills/sql_domain_knowledge.md)) for reproducibility. Numbers move a point or two run to run — the model isn't seeded — but stay well clear of the floors above across every run in this project's history.

## Data-cleaning decisions

Built from the raw Online Retail II workbook (~1M invoice line items, UK-based giftware retailer, 2009–2011) with no row drops or value changes at ingest — every cleaning rule lives in [`data/views.py`](data_analyst_agent/data/views.py), applied when the semantic-layer views are built, not scattered through prompts:

- **Cancellations need an "offsetting order" to be excluded.** The raw data has no explicit link between a cancellation invoice and the order it cancels. A cancellation is treated as cancelling a real purchase — and excluded from `v_orders` entirely — only if the same customer bought at least one of the same `StockCode`s elsewhere in a non-cancelled invoice. Of 8,292 cancellation invoices, 7,276 have this evidence; 1,016 don't (625 with a known customer and no stock overlap, 391 with no customer at all) and are excluded. Verified against the real data before committing to the rule, specifically to avoid a rule that excluded either everything or nothing.
- **Cancellation detection is by `InvoiceNo`, not `StockCode`.** `scope.md`'s draft text described returns as "negative quantity, `StockCode` prefixed `C`" — checked against the real data and that's backwards; cancellations are `InvoiceNo`-prefixed with `C`, and `StockCode` is never `C`-prefixed. The views use the verified pattern.
- **Orders with no `CustomerID` (~25% of rows) are kept for order- and product-level totals, excluded from customer-level views.** `v_orders` and `v_products` include them; `v_customers` filters `customer_id IS NOT NULL` — otherwise a single bogus "null customer" aggregates over $3M in orders and would rank as the top customer by revenue in any naive `GROUP BY customer_id`.
- **"Net of returns" means summing every line, returns included** (a return row's quantity and revenue are already negative), not filtering returns out. `v_products.total_revenue`, by contrast, deliberately excludes return lines entirely — a narrower, different convention, used only for per-product ranking, never substituted into a question that explicitly asks for a *net* figure.

## Evaluation strategy

The actual differentiator of this project, built before the agent so "better" meant something objective throughout. [`eval/comparator.py`](data_analyst_agent/eval/comparator.py) grades on **executed result, never on SQL string** — an agent query that reaches the same numbers via a different (even more convoluted) route still passes:

- Result sets are compared order-invariantly unless the question implies a specific order.
- Numeric values tolerate a 0.1% relative difference, so float rounding never causes a false failure.
- Column count and values are checked positionally, not by column name — the agent's `SUM(price)` matches gold's `total_revenue` as long as the numbers agree.

[`eval/harness.py`](data_analyst_agent/eval/harness.py) runs all 60 gold questions through the real agent (never mocked), reports per-bucket accuracy, the attempts-until-success distribution, and the graceful failure rate, and stamps every report with the git commit and prompt hash it ran against.

## What `failures.jsonl` revealed

Every fully-exhausted turn (all 3 attempts failed) is logged unconditionally to `failures.jsonl`, independent of the gold-set harness. Across development, the categories split roughly as designed — most failures (`schema_mismatch`) correctly identified a genuinely missing concept (sub-national regions like Scotland, email open rates, social media activity — none of which this schema tracks), and a handful (`ambiguity`) correctly caught questions with no answerable default ("compare *this* to last year" — nothing for "this" to refer to in a stateless, single-turn system).

Two things it surfaced that weren't obvious from the gold set alone:
- **A real SQL-generation bug** ("rank each product's revenue within its category") landed in the `bug` category exactly once — everything else was a correct refusal, not a crash, which is the failure-mode balance the design was aiming for.
- **The same question classified differently on different days** ("what's driving the change in our numbers?" was diagnosed `ambiguity` once and `schema_mismatch` on a later run) — a reminder that the diagnosis step is itself an LLM call, not a deterministic classifier, so its category label is informative but not perfectly stable.

Most entries, though, were the paper trail of this project's own iteration: several rows are the exact false-decline questions (a France monthly sales trend, a per-customer order-ranking question, an AOV-vs-average comparison) that got fixed during prompt tuning — `failures.jsonl` is what made those regressions visible in the first place, not the 60-question harness, since a few of them weren't in the gold set at all.

## Running locally

```bash
uv venv --python 3.11 .venv && uv pip install -e ".[dev]"
cp .env.example .env   # fill in OPENAI_API_KEY
uv run python -m data_analyst_agent.data.ingest
uv run python -m data_analyst_agent.data.categorize
uv run python -m data_analyst_agent.data.views --build v_orders
uv run python -m data_analyst_agent.data.views --build v_order_lines
uv run python -m data_analyst_agent.data.views --build v_customers
uv run python -m data_analyst_agent.data.views --build v_products
uv run python -m data_analyst_agent.data.views --build v_daily_revenue
uv run python -m data_analyst_agent.data.metrics
uv run streamlit run data_analyst_agent/app/streamlit_app.py
```

(The `data_analyst_agent.duckdb` already committed to this repo has all of the above already applied — the ingest-through-metrics steps are only needed to rebuild it from scratch.)

## Running via Docker

```bash
docker build -t data-analyst-agent .
docker run -p 8501:8501 --env-file .env data-analyst-agent
```

The container builds the database itself on first run if `data_analyst_agent.duckdb` isn't already present (see [`docker/entrypoint.sh`](docker/entrypoint.sh)); mounting the repo's own pre-built file in as a volume skips that and starts immediately:

```bash
docker run -p 8501:8501 --env-file .env \
  -v "$(pwd)/data_analyst_agent.duckdb:/app/data_analyst_agent.duckdb" \
  data-analyst-agent
```

Run the eval harness the same way, in the same environment:

```bash
docker run --env-file .env \
  -v "$(pwd)/data_analyst_agent.duckdb:/app/data_analyst_agent.duckdb" \
  data-analyst-agent \
  python -m data_analyst_agent.eval.harness --gold data_analyst_agent/eval/gold --out report/
```

## Design docs

| Doc | Answers |
|---|---|
| [`scope.md`](_docs/scope.md) | What's in/out of scope, dataset & cleaning rules, metric dictionary, eval design, definition of done |
| [`autonomy.md`](_docs/autonomy.md) | How much room each agent action gets to decide on its own (L0–L3 tiers) |
| [`Architecture.md`](_docs/Architecture.md) | The control-flow pattern (bounded tool-use loop), full turn diagram, termination/failure paths |
| [`Tools.md`](_docs/Tools.md) | The exact I/O contract for `run_sql`, and why the other candidate tools stayed deterministic code |
| [`InformationModel.md`](_docs/InformationModel.md) | Every entity's schema — domain data, operational data, evaluation data |
| [`technology_stack.md`](_docs/technology_stack.md) | Which library implements each layer, and why alternatives were rejected |
| [`implementation_plan.md`](_docs/implementation_plan.md) | The 30-slice build backlog (S01–S30), each with acceptance criteria and a verification command |

## Stack

OpenAI GPT-4o-mini (structured outputs, no tool-calling framework) · DuckDB (read-only) · `sqlglot` (SQL guardrail) · Streamlit (UI + session state) · a hand-rolled eval harness (`pandas`-based comparator) · Docker · Streamlit Community Cloud.

## Definition of done (v1) — all met

- ✅ ≥75% basic / ≥55% semantic / ≥40% adversarial accuracy on a 60-question gold set — see [Results](#results-final-harness-run)
- ✅ ≥80% graceful-failure rate on designed-to-fail adversarial questions
- ✅ Median turn latency <8s on the happy path
- ✅ Hosted, publicly reachable Streamlit demo — https://data-analyst-agent-shopify.streamlit.app/
- ✅ README with data-cleaning decisions, evaluation strategy, headline accuracy numbers, and `failures.jsonl` commentary (this file)

Full details in [`_docs/scope.md`](_docs/scope.md#definition-of-done-and-phase-2-backlog).
