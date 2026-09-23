# Data Analyst Agent — v1 Scope

Sep 22, 2026 · @Someone

## Project identity

A portfolio-first descriptive SQL analyst agent for a Shopify-shaped e-commerce dataset. Real-user-inspired (not real-user-served): the persona is a **UK-based solo founder of an online giftware/homewares store, with some international customers**. The dataset (Online Retail II) is literally that shape, so the persona and the data agree.

The one-line premise: *"Ask business questions in plain English, get correct answers backed by inspectable SQL, with an eval harness that proves it."* The differentiator is the eval — result-based grading against a hand-crafted gold set — not the agent itself. Recruiters see a working demo, a public accuracy score across three difficulty buckets, and a repo that reproduces both.

## Scope for v1

**In scope.** Descriptive questions only — *what / how many / which / when / where*, expressed in business terms. Single-turn stateless: each question is answered independently, no memory of prior turns. One SQL query per successful turn, wrapped with a short natural-language answer and a chart when the shape calls for it.

**Three canonical example questions** (the acceptance shape for v1):

1. *"Which products are selling the most in which region?"*
2. *"What is my average order value across all regions?"*
3. *"Which products are the least performing?"* (Answered narrowly — top-N by the default metric — not diagnostically.)

**Out of scope for v1**, declared explicitly:

- **Diagnostic questions** ("why did X drop?") — the agent politely narrows them to descriptive equivalents.
- **Multi-turn follow-ups** ("now filter to Q3") — v1 has no session memory; the seam is left open for Phase 2.
- **Write actions** — the agent never mutates the database. Enforced at the connection level.
- **Automated grading of natural-language wrapping or chart aesthetics** — spot-checked by hand only.

## Dataset and cleaning rules

**Dataset:** Online Retail II from the UCI Machine Learning Repository. \~1M invoice line items from a real UK-based online giftware retailer over 2009–2011. One wide invoice-line table plus a derived product-category lookup (see below).

**Known skew:** \~93% of transactions are UK. The three mitigations:

1. **Persona reframe.** The README opens with "UK-based solo founder of a giftware/homewares store, with some international customers" — an accurate description of the data. The skew is a feature of the persona, not a flaw.
2. **International-flavoured regional gold questions.** Rather than "top products by region" (which trivially returns UK), the semantic and adversarial buckets include questions like *"top 5 non-UK markets by revenue this year"*, *"which international markets grew fastest quarter-over-quarter?"*, *"which products sell disproportionately well outside the UK?"*.
3. **Documented cleaning rules**, applied inside the SQL views (not scattered through prompts):
   - Rows with `CustomerID IS NULL` (\~25% of rows) are excluded from customer-level analytics (`v_customers`, repeat-rate calculations) but retained for product-level and revenue totals.
   - Return transactions (negative quantity, StockCode prefixed `C`) are netted against gross revenue in `v_orders` and exposed as a separate `returns` view for return-rate analytics.
   - Cancellations without an offsetting order are excluded.
   - Product categories are derived once (offline, LLM classification pass on unique StockCode → Description pairs) into a lookup table `dim_product_category` for category-level questions.

All four rules land in the README as an explicit "Data cleaning decisions" section — portfolio-artifact in its own right.

## Semantic layer — views and metric dictionary

The semantic layer is a set of **curated SQL views** the agent queries. It never sees the raw table. Views are unit-tested; a broken view is invisible until the eval fails, and debugging "was it the agent or the view?" is a trap worth avoiding.

**Views for v1:**

- `v_orders` — one row per invoice: customer, country, order date, gross revenue, returned amount, net revenue, item count.
- `v_order_lines` — one row per line: joined with product info and clean category.
- `v_customers` — one row per customer: first order date, last order date, order count, lifetime revenue, country.
- `v_products` — one row per product: total units sold, total revenue, distinct customer count, top region.
- `v_daily_revenue` — one row per date: daily gross, net, order count, unique customers (for time-series questions).

**Metric dictionary — the \~9 business terms the semantic layer defines:**

- **Revenue** — sum of (quantity × unit price), net of returns.
- **AOV (Average Order Value)** — revenue ÷ order count, per period.
- **Order count** — distinct invoices in period.
- **Units sold** — sum of quantity, net of returns.
- **Repeat customer rate** — customers with ≥ 2 orders in period ÷ total customers in period.
- **Return rate** — returned value ÷ gross value.
- **Top-selling / worst-selling** — rank products by revenue by default (see clarification pattern in Agent design).
- **Growth rate** — period-over-period % change of any metric.
- **CLV (Customer Lifetime Value)** — cumulative revenue per customer to date.

*Optional:* items-per-order. Added if time permits.

**Views ship with unit tests** — row counts, null handling, aggregation identities (e.g. `SUM(v_orders.gross) == SUM(raw.qty * raw.price)`), tie-breaker determinism. Written in pytest, run before every eval sweep.

## Agent design

**Tool surface (three tools):**

- `run_sql(query: str) -> ResultSet` — executes SELECT/WITH against the read-only DB, returns rows + column types.
- `list_metrics() -> [Metric]` — returns the metric dictionary above with name, one-line definition, and the SQL fragment that implements it. Called by the agent when a business term appears in the user's question.
- `plot_chart(spec: ChartSpec) -> Image` — renders a chart from a compact spec (chart type, data, axes). Chart type chosen by the agent from a small enum (line, bar, table, scalar).

**Turn shape (single-turn stateless):**

1. Receive user question.
2. LLM step: interpret → produce SQL (using metric definitions from `list_metrics` when needed).
3. `run_sql` — execute.
4. If SQL errors, feed error + attempt back to LLM. Retry up to **3 attempts total** with per-case attempt-count logged in the eval output.
5. On success: LLM step wraps the result in a short natural-language answer, chooses a chart type, calls `plot_chart` when the shape calls for one.
6. Response returned: answer + chart (if any) + a *"show SQL"* toggle exposing the executed query.

The `context` object passed to the agent is always empty in v1 but exists in the signature — the seam for Phase-2 multi-turn.

**Clarification pattern — "assume, state, invite override":** when the user's phrasing is ambiguous (e.g. "top-selling" with no qualifier), the agent runs with the documented default (revenue), returns the answer, and closes with one line naming the assumption and offering the alternatives — *"I ranked by revenue. Ask again with 'by units' or 'by margin' if you meant something else."* Single-turn compatible; teaches the founder the vocabulary organically; upgrades gracefully in Phase 2.

## Failure UX

When the bounded-3 retry loop still fails, the founder sees five things — in this order, and never a hallucinated fallback answer:

1. **Plain-English refusal** — *"I tried three ways to answer this and couldn't get a reliable result. I'd rather tell you that than guess."*
2. **LLM-diagnosed cause** — a cheap extra LLM call over the failure trace (all attempts, all errors, schema, original question) classifies the failure into one of:
   - *Schema mismatch* — *"It looks like your data doesn't include email opens — I only have orders, products, customers, and shipping regions."*
   - *Ambiguity* — *"The question was ambiguous — I wasn't sure whether 'top' meant by revenue or by units."*
   - *Real error* — *"Something went wrong on my end — likely a bug I need to fix."*
3. **Concrete rephrase suggestion** — a specific rewrite the founder can try (*"Try: 'Which 5 products had the highest revenue in Q3?'"*), or a schema-matched alternative for schema-mismatch failures.
4. **Technical details on toggle** — attempted SQL, error messages, retry count. Not front-and-center. Same toggle convention as the "show SQL" on success.
5. **Full failure logged to `failures.jsonl`** — question, all attempts, all errors, diagnosis, timestamp, session id. Feeds bug fixes and gold-set additions.

**Adversarial gold cases graded on failure shape.** \~5–10 of the 20 adversarial-bucket gold questions are questions the agent is *supposed to fail on gracefully* (asks for absent data, genuinely ambiguous phrasings, empty-result-expected). Their grade is "did the failure land in one of the three well-shaped categories above" — call this the **graceful failure rate** metric. It's an unusual and defensible portfolio number.

## Guardrails

Minimal but explicit. Each is spelled out in the README and enforced in code:

- **Read-only DB connection.** DuckDB opened in read-only mode. Even a bug in the SQL parser can't mutate the DB.
- **SQL statement type whitelist.** A parser step rejects anything but `SELECT` and `WITH`. Belt-and-braces: even if read-only mode is somehow bypassed, `DROP`/`UPDATE`/`INSERT`/`DELETE`/`CREATE` never reach execution.
- **Per-query timeout — 5 seconds.** A SQL query exceeding 5s is killed and returned to the retry loop as an error.
- **Row cap on results — 10,000 rows.** Results are truncated at 10k and a note added to the response (*"showing first 10,000 rows"*). Prevents runaway large-result payloads to the LLM.
- **Per-session cost cap.** A dollar budget per session (default $0.50) tracked at the LLM call layer. Exceeded → session refuses further LLM calls with a clear error. Protects against runaway prompt-injection or agent-loop bugs.
- **Prompt-injection stance — minimal for v1.** System prompt clearly labels user input as untrusted; the agent is instructed to never follow user-supplied "ignore previous instructions" style content. No deeper defence — this is a portfolio project on a public dataset with no PII. Documented as a limitation, not solved.

None of these are portfolio showcases in themselves — they're table-stakes hygiene. A senior reviewer would notice their absence.

## Evaluation

The differentiator of the whole project. Eval infrastructure is built **before** the agent, so "better" means something objective throughout the build.

**Gold set — 60 hand-crafted (question, gold-SQL, gold-result) triples, split 20 / 20 / 20 across three buckets:**

- **Basic (20)** — textbook descriptive questions. If the agent scores under 50% here it's broken.
- **Semantic (20)** — deliberately use business vocabulary requiring the semantic layer (AOV, repeat rate, top-selling, growth rate, CLV). Tests the interesting engineering.
- **Adversarial (20)** — ambiguous phrasing, implicit time windows, missing entities, empty-result-expected, questions that should trigger graceful failure. \~5–10 of these are graded on **failure shape**, not on answer correctness.

**Result-based comparator** — the module that decides pass/fail. Grades on executed result, never on SQL string. Handles:

- Order invariance unless the question specifies `ORDER BY`.
- Float tolerance (relative diff < 0.1%).
- Column-name insensitivity (agent's `SUM(price)` matches gold's `total_revenue` if values match).
- Empty-set equivalence.
- Deterministic tie-breaker convention — both prompt and gold mandate `ORDER BY <metric> DESC, product_id ASC` for ranked results.

Shipped as its own module with its own unit tests. Portfolio artifact in its own right.

**Harness** — a CLI-runnable script:

- `python eval.py --gold gold.jsonl --agent v3`
- Reads gold set from JSONL (git-diff-friendly, one triple per line).
- Runs each case: agent → SQL → execute → comparator → pass/fail + structured diff on fail.
- Emits JSON report + Markdown summary.
- Reports **per-bucket accuracy**, **attempts-until-success distribution**, and **graceful failure rate**.
- Stamps report with git commit hash + prompt hash for reproducibility.

Headline number for the README, once run: something like *"87% basic / 71% semantic / 45% adversarial across 60 hand-crafted questions, harness reproduces in 3 minutes on any clone."*

## Deployment

**v1 target: hosted Streamlit Community Cloud.** Chat interface, single-file app, public URL, free hosting, one-click deploy from GitHub. Recruiter clicks a link, types a question, sees an answer + chart + the "show SQL" toggle. That's the demo.

**Repo structure — leaves the Phase-2 seam clean:**

```
agent/
  core.py          # the agent loop (imported by both interfaces)
  tools.py         # run_sql, list_metrics, plot_chart
  semantic/        # views + metric dictionary
  eval/            # gold set + comparator + harness
app/
  streamlit_app.py # v1 demo interface
  api.py           # Phase-2 FastAPI wrapper (stubbed in v1)
```

Streamlit and (later) FastAPI both import from `agent/core.py`. No agent logic in either interface layer.

**Local Streamlit is the fallback** if hosted deploy hits an edge case. Not preferable — the click-a-link demo is much stronger portfolio.

**Phase 2 (explicitly deferred):** FastAPI wrapper deployed on Render's free tier, giving a public API endpoint alongside the chat demo. README highlights both — *"chat demo at Streamlit URL, API at Render URL, same agent core, separated by design."* This is the API-skill signal without any v1 build cost.

## Build sequence and calendar

**9–11 working days of focused solo time.** Order matters — each step depends on the previous.

| # | Step | Days |
| --- | --- | --- |
| 1 | Dataset load into DuckDB, cleaning script, views authored + unit tests | 1.0 |
| 2 | Gold set — 60 (question, gold-SQL, gold-result) triples across three buckets | 1.5 |
| 3 | Comparator module + eval harness (CLI, JSON + Markdown report) | 1.0 |
| 4 | Agent loop — tools, retry, clarification, failure UX, `failures.jsonl` | 2.5 |
| 5 | Prompt iteration against the eval — until per-bucket targets hit | 1.5 |
| 6 | Streamlit app, hosted deploy, secrets config, one round of cloud-debugging | 1.5 |
| 7 | README, headline eval number, screenshots, short demo video, blog draft | 1.0 |
|  | **Total** | **10.0** |

**The single most important ordering rule: eval before agent.** Steps 1–3 build the machinery that decides whether the agent is any good. If you open an LLM call before step 3 is done, stop — you'll unconsciously write gold questions your agent already answers.

**If timeline slips**, cuts in this priority order (most disposable first):

1. Cut demo video, keep screenshots.
2. Cut hosted deploy, ship local Streamlit + README with run instructions.
3. Cut gold set from 60 to 30, keeping the 20 basic + 10 semantic buckets. Adversarial cut last.
4. Cut Phase-2-seam factoring — collapse `app/api.py` stub, keep only `streamlit_app.py`.
5. Cut failure-diagnosis LLM step, fall back to generic refusal.

## Definition of done, and Phase 2 backlog

**v1 is done when all of these hold:**

- **Accuracy floor** — ≥ 75% on basic bucket, ≥ 55% on semantic bucket, ≥ 40% on adversarial. Numbers below this on any bucket means iterate, don't ship.
- **Graceful failure rate** — ≥ 80% of the failure-shape adversarial cases produce a well-shaped refusal (plain refusal + diagnosis + rephrase suggestion).
- **Latency** — median turn under 8 seconds on the happy path; under 20 seconds on 3-retry cases. Streamlit UI shows a visible loading state at all times.
- **Deployment liveness** — hosted Streamlit URL responds and produces a correct answer for at least one canonical question on cold-start.
- **Portfolio artifacts complete** — README with data cleaning decisions section, evaluation strategy section, headline accuracy numbers, run-locally instructions, and the `failures.jsonl` log committed with commentary on what it taught. Blog draft outlining the semantic-layer-as-views design decision.

**Phase 2 backlog — explicitly deferred, listed so v1 doesn't accidentally consume any of it:**

- **Diagnostic why-questions** with a planner + hypothesis loop. The bigger project.
- **Multi-turn follow-ups** with session memory (`context` object filled).
- **FastAPI + Render endpoint** — same agent core behind a public API.
- **Richer clarification behaviour** — clarify-first when the agent detects high-cost ambiguity, plus follow-up detection ("rerun by units").
- **Chart-appropriateness LLM judge** — automated evaluation of layer 4.
- **User feedback capture** — thumbs-up/down per turn, stored alongside the answer.
- **One real user** — post the demo in one Shopify founder community, capture one round of feedback, add a "what a real founder said" section to the README.
