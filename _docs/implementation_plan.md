# Implementation Plan — v1

Companion to `scope.md`, `AUTONOMY.md`, `ARCHITECTURE.md`, `Tools.md`, `InformationModel.md`, and `technology_stack.md`. Those six docs decided what the system is; this plan decides the order to build it in, cut into slices small enough to hand to a coding agent one at a time.

## How to read this plan

Each slice is **vertical**, not a layer: it produces something checkable on its own — a query that runs, a view whose totals reconcile, a full turn that returns a real answer — rather than "half a component" that only means something once three other slices land too. Dependencies are a strict DAG: no slice below assumes a slice that comes after it in the list is already done. Where a slice's *design* is agent-independent (a pure function testable with fixtures) but its *narrative* home is later, that's called out explicitly — you can pull it forward if a coding-agent session has spare capacity.

Repo layout assumed throughout:
```
data_analyst_agent/
  models/entities.py          # S01
  db/sql_guard.py              # S02
  db/connection.py               # S14
  db/run_sql.py                    # S15
  data/ingest.py                     # S06
  data/categorize.py                   # S07
  data/views.py                          # S08–S12
  data/metrics.py                          # S13
  agent/session.py                           # S04
  agent/audit_log.py                            # S05
  agent/generate_sql.py                           # S19
  agent/retry_loop.py                               # S20
  agent/chart_select.py                               # S21
  agent/narrative.py                                    # S22
  agent/diagnosis.py                                      # S23
  agent/orchestrator.py                                     # S24
  eval/comparator.py                                          # S03
  eval/gold/{basic,semantic,adversarial}.jsonl                  # S16–S18
  eval/harness.py                                                  # S25
  app/streamlit_app.py                                               # S27
  Dockerfile                                                           # S28
  tests/test_*.py
```

## Slice index

| ID | Slice | Depends on |
|---|---|---|
| S01 | Typed entity models | — |
| S02 | SQL statement-type validator | — |
| S03 | Result-set comparator | S01 |
| S04 | Session state, cost cap, fast-fail check | S01 |
| S05 | Audit / failure JSONL writers | S01 |
| S06 | Raw data ingestion | S01 |
| S07 | Product category classification | S06 |
| S08 | Cleaning script + `v_orders` | S06 |
| S09 | `v_order_lines` | S06, S07, S08 |
| S10 | `v_customers` | S08 |
| S11 | `v_products` | S09, S07 |
| S12 | `v_daily_revenue` | S08 |
| S13 | Metric dictionary seed data | S01 |
| S14 | Read-only DB connection | S06 |
| S15 | `run_sql` tool | S02, S14, S08–S12 |
| S16 | Gold set — basic bucket | S08–S13 |
| S17 | Gold set — semantic bucket | S08–S13 |
| S18 | Gold set — adversarial bucket | S08–S13, S15 |
| S19 | SQL generation call | S01, S08–S13 |
| S20 | Bounded retry loop | S19, S15, S04 |
| S21 | Chart-type selection + rendering | S01 |
| S22 | Narrative wrap completion | S01 |
| S23 | Failure diagnosis completion | S01 |
| S24 | Full turn orchestration → `Answer` | S20, S21, S22, S23, S05, S04 |
| S25 | Eval harness | S03, S16–S18, S24 |
| S26 | Prompt iteration to accuracy floors | S25 |
| S27 | Streamlit UI | S24, S21, S04 |
| S28 | Docker containerization | S27, S25 |
| S29 | Hosted deployment | S27, S26 |
| S30 | README + portfolio write-up | S26, S28, S29 |

---

## Phase 0 — Foundations

### S01 — Typed entity models

- **Goal.** Define every entity from `InformationModel.md` as a `pydantic` model, so every later slice validates against the same schema instead of passing raw dicts around.
- **Dependencies.** None.
- **Files/modules.** `models/entities.py`.
- **Acceptance criteria.** One model per entity (`SqlAttempt`, `ChartSpec`, `FailureDiagnosis`, `Answer`, `SessionState`, `QueryAuditLog`, `FailureLogEntry`, `MetricDefinition`, `GoldQuestion`, `EvalRun`, `EvalResult`), field types and nullability matching `InformationModel.md` exactly (e.g. `Answer.answer_text` optional, required only when `status == "success"`).
- **Required tests.** `tests/test_entities.py`: construct one valid instance of each model; construct one instance per model with a missing required field and assert `ValidationError`; round-trip each through `.model_dump_json()` / `.model_validate_json()`.
- **Required evaluation cases.** None beyond the tests above — this slice has no domain-correctness dimension, only schema-correctness.
- **Verification commands.** `pytest tests/test_entities.py -v`

### S02 — SQL statement-type validator

- **Goal.** Implement the `sqlglot`-based guardrail from `Tools.md`: accept `SELECT`/`WITH` only, reject everything else, before any query reaches the database.
- **Dependencies.** None (reuses `SqlAttempt.status` enum from S01 if available, but not blocking).
- **Files/modules.** `db/sql_guard.py`.
- **Acceptance criteria.** `validate(query: str) -> Literal["ok", "rejected"]`. Accepts plain `SELECT`, `WITH ... SELECT`. Rejects `INSERT`/`UPDATE`/`DELETE`/`DROP`/`CREATE`/`ALTER`, a `WITH` clause smuggling an `INSERT`, multi-statement strings (`SELECT 1; DROP TABLE x`), and comment-obfuscated write attempts.
- **Required tests.** `tests/test_sql_guard.py`: parametrized over ≥12 cases (6 accept, 6 reject) covering every case in the acceptance criteria above.
- **Required evaluation cases.** None — pure code-correctness, no business data involved.
- **Verification commands.** `pytest tests/test_sql_guard.py -v`

### S03 — Result-set comparator

- **Goal.** The pass/fail engine behind the whole eval strategy: compare an agent's executed result to a gold result with order invariance, float tolerance, and column-name insensitivity.
- **Dependencies.** S01 (uses the same row/column shape as `SqlAttempt`).
- **Files/modules.** `eval/comparator.py`.
- **Acceptance criteria.** `compare(agent_result, gold_result) -> bool`. True when row sets match after sorting by the declared tie-break key, values within 0.1% relative tolerance for floats, column values equal regardless of column name. False on any row missing, extra, or out of tolerance. Empty-vs-empty is a match.
- **Required tests.** `tests/test_comparator.py`: identical results match; re-ordered rows still match; float within tolerance matches, outside tolerance fails; differently-named-but-same-valued columns match; one extra/missing row fails; two empty result sets match.
- **Required evaluation cases.** None yet — this slice is exercised for real once the gold set (S16–S18) and harness (S25) exist; note that dependency explicitly rather than fabricate a case now.
- **Verification commands.** `pytest tests/test_comparator.py -v`

### S04 — Session state, cost cap, fast-fail check

- **Goal.** The three deterministic session-scoped mechanics from `AUTONOMY.md`/`Tools.md`: the cost-cap kill switch, the fast-fail cache (normalized-question-text match), and the plain session object holding both.
- **Dependencies.** S01.
- **Files/modules.** `agent/session.py`.
- **Acceptance criteria.** `SessionState` object per `InformationModel.md`'s fields. `check_cost_cap(session) -> bool` returns `True` (stop) once `cost_spent_usd >= cost_cap_usd` (default `0.50`). `normalize(question: str) -> str` lowercases and collapses whitespace. `check_fast_fail(session, question) -> bool` returns `True` when the normalized question already has 3 recorded failed attempts in `failed_questions_cache`.
- **Required tests.** `tests/test_session.py`: cost cap triggers exactly at the threshold, not before; fast-fail triggers only after 3 recorded failures for the *same normalized* question, not a differently-worded one; two questions differing only by case/whitespace are treated as the same key.
- **Required evaluation cases.** None — deterministic code, no model output to grade.
- **Verification commands.** `pytest tests/test_session.py -v`

### S05 — Audit / failure JSONL writers

- **Goal.** Implement the two durable log writers from `InformationModel.md`: `QueryAuditLog` (every attempt, unconditional) and `FailureLogEntry` (`failures.jsonl`, only on full failure).
- **Dependencies.** S01.
- **Files/modules.** `agent/audit_log.py`.
- **Acceptance criteria.** `log_attempt(attempt: SqlAttempt) -> None` appends one JSON line to `query_audit.jsonl` matching `InformationModel.md`'s field list. `log_failure(entry: FailureLogEntry) -> None` appends one JSON line to `failures.jsonl`. Both are append-only, never rewrite prior lines.
- **Required tests.** `tests/test_audit_log.py`: write one attempt, read the file back, assert field-for-field match against the model; write two failures, assert both lines present and independently parseable; assert a partially-written line never occurs (write is atomic per call).
- **Required evaluation cases.** None — pure I/O correctness.
- **Verification commands.** `pytest tests/test_audit_log.py -v`

---

## Phase 1 — Domain data

### S06 — Raw data ingestion

- **Goal.** Load the Online Retail II source file into a raw DuckDB table, unmodified.
- **Dependencies.** S01 (for consistent path/config handling; otherwise standalone).
- **Files/modules.** `data/ingest.py`.
- **Acceptance criteria.** A `raw_online_retail` table exists in the DuckDB file with one row per source line item, all source columns preserved (`InvoiceNo`, `StockCode`, `Description`, `Quantity`, `InvoiceDate`, `UnitPrice`, `CustomerID`, `Country`), row count equal to the source file's line count minus header.
- **Required tests.** `tests/test_ingest.py`: row count matches a known source count; a handful of specific known rows (by `InvoiceNo`) match expected values exactly.
- **Required evaluation cases.** Cross-check: total gross revenue computed directly from `raw_online_retail` (`SUM(Quantity * UnitPrice)`) matches a value independently computed from the source file outside the pipeline (e.g. a spreadsheet or `pandas` one-liner run by hand once, recorded in the test as the expected constant).
- **Verification commands.** `python -m data_analyst_agent.data.ingest && pytest tests/test_ingest.py -v`

### S07 — Product category classification

- **Goal.** Build `dim_product_category` by classifying every unique `(StockCode, Description)` pair into a category, once, offline.
- **Dependencies.** S06.
- **Files/modules.** `data/categorize.py`.
- **Acceptance criteria.** `dim_product_category` table with one row per unique `StockCode`, columns `product_id, description, category`. Every `StockCode` present in `raw_online_retail` has exactly one row here. No null categories.
- **Required tests.** `tests/test_categorize.py`: every distinct `StockCode` in the raw table has a matching row; no duplicate `product_id`s.
- **Required evaluation cases.** Hand-check a random sample of 20 classified rows against their `Description` text for plausibility (e.g. "CHRISTMAS TREE DECORATION" → a decorations/seasonal category, not "electronics"); record the sample and the pass count in the PR description — this is a spot-check, not automated, and should be logged as such rather than silently skipped.
- **Verification commands.** `python -m data_analyst_agent.data.categorize && pytest tests/test_categorize.py -v`

### S08 — Cleaning script + `v_orders`

- **Goal.** Apply the three cleaning rules from `scope.md` (null-CustomerID handling, return netting, cancellation exclusion) and materialize `v_orders`.
- **Dependencies.** S06.
- **Files/modules.** `data/views.py`.
- **Acceptance criteria.** `v_orders` matches `InformationModel.md`'s schema exactly. `customer_id` is `NULL` wherever the source `CustomerID` was null (not dropped, not defaulted). `net_revenue = gross_revenue - returned_amount` for every row. Rows that are pure cancellations (no offsetting order) are excluded entirely, not zeroed.
- **Required tests.** `tests/test_views.py::test_v_orders`: `SUM(v_orders.gross_revenue)` equals `SUM(raw_online_retail non-return rows' revenue)`; every `customer_id IS NULL` row in `v_orders` corresponds to a null `CustomerID` in the raw table; row count of `v_orders` is strictly less than distinct `InvoiceNo` count in the raw table (cancellations removed).
- **Required evaluation cases.** Total UK net revenue for 2011 from `v_orders` matches an independently-computed value (same pattern as S06 — compute once by hand, hard-code as the test's expected constant).
- **Verification commands.** `python -m data_analyst_agent.data.views --build v_orders && pytest tests/test_views.py::test_v_orders -v`

### S09 — `v_order_lines`

- **Goal.** Materialize `v_order_lines`, joined to `v_orders` and `dim_product_category`, with the `is_return` flag.
- **Dependencies.** S06, S07, S08.
- **Files/modules.** `data/views.py`.
- **Acceptance criteria.** One row per raw line item. `order_id` present in `v_orders` for every non-cancelled row. `is_return = true` exactly for negative-quantity, `C`-prefixed-StockCode rows. `category` populated from `dim_product_category` for every row.
- **Required tests.** `tests/test_views.py::test_v_order_lines`: every `order_id` in `v_order_lines` exists in `v_orders`; `is_return` flag matches the negative-quantity/`C`-prefix rule on a sample of known return rows; `SUM(line_revenue)` for non-return rows reconciles with `v_orders.gross_revenue` per order.
- **Required evaluation cases.** Units sold for the single highest-volume `StockCode` from `v_order_lines` matches an independently-computed value.
- **Verification commands.** `python -m data_analyst_agent.data.views --build v_order_lines && pytest tests/test_views.py::test_v_order_lines -v`

### S10 — `v_customers`

- **Goal.** Materialize `v_customers`, aggregated from `v_orders`, excluding null-`customer_id` orders.
- **Dependencies.** S08.
- **Files/modules.** `data/views.py`.
- **Acceptance criteria.** One row per non-null `customer_id`. `order_count`, `lifetime_revenue`, `first_order_date`, `last_order_date` all correctly aggregated from `v_orders`. No row for `customer_id IS NULL`.
- **Required tests.** `tests/test_views.py::test_v_customers`: row count equals distinct non-null `customer_id` count in `v_orders`; `order_count` for a specific known customer matches a manually-verified count; `SUM(v_customers.lifetime_revenue)` equals `SUM(v_orders.net_revenue WHERE customer_id IS NOT NULL)`.
- **Required evaluation cases.** Repeat-customer rate (`repeat_rate` metric definition) computed directly from `v_customers` for 2011 matches an independently-computed value.
- **Verification commands.** `python -m data_analyst_agent.data.views --build v_customers && pytest tests/test_views.py::test_v_customers -v`

### S11 — `v_products`

- **Goal.** Materialize `v_products`, aggregated from `v_order_lines` and joined to `dim_product_category`.
- **Dependencies.** S09, S07.
- **Files/modules.** `data/views.py`.
- **Acceptance criteria.** One row per `product_id`. `total_units_sold`, `total_revenue`, `distinct_customer_count`, `top_region` all correctly aggregated.
- **Required tests.** `tests/test_views.py::test_v_products`: `SUM(v_products.total_revenue)` equals `SUM(v_order_lines.line_revenue WHERE NOT is_return)`; `top_region` for a known high-volume product matches manual verification against `v_order_lines` joined to `v_orders.country`.
- **Required evaluation cases.** The single highest-revenue product from `v_products` matches the answer to gold question "which product generated the most revenue overall" once S16 exists — flagged forward, not fabricated here.
- **Verification commands.** `python -m data_analyst_agent.data.views --build v_products && pytest tests/test_views.py::test_v_products -v`

### S12 — `v_daily_revenue`

- **Goal.** Materialize the date-grain rollup of `v_orders`.
- **Dependencies.** S08.
- **Files/modules.** `data/views.py`.
- **Acceptance criteria.** One row per calendar date present in `v_orders`. `gross_revenue`, `net_revenue`, `order_count`, `unique_customers` all correctly aggregated per date.
- **Required tests.** `tests/test_views.py::test_v_daily_revenue`: `SUM(v_daily_revenue.net_revenue)` equals `SUM(v_orders.net_revenue)` exactly (a rollup must reconcile to the total with no loss or double-count).
- **Required evaluation cases.** Highest single-day revenue date matches a manually-verified value.
- **Verification commands.** `python -m data_analyst_agent.data.views --build v_daily_revenue && pytest tests/test_views.py::test_v_daily_revenue -v`

### S13 — Metric dictionary seed data

- **Goal.** Load the nine `MetricDefinition` rows from `InformationModel.md` as queryable reference data.
- **Dependencies.** S01.
- **Files/modules.** `data/metrics.py`.
- **Acceptance criteria.** Exactly nine `MetricDefinition` rows, matching `InformationModel.md`'s table verbatim, including `return_rate`'s `default_direction = "asc"`.
- **Required tests.** `tests/test_metrics.py`: exactly 9 rows; every row validates against the `MetricDefinition` model from S01; `return_rate`'s direction is specifically asserted as `"asc"` (the one deliberate exception).
- **Required evaluation cases.** None — static reference data, no computation to check against a gold answer.
- **Verification commands.** `pytest tests/test_metrics.py -v`

---

## Phase 2 — SQL execution

### S14 — Read-only DB connection

- **Goal.** A connection helper that opens DuckDB read-only, per `Tools.md`'s auth/authz spec.
- **Dependencies.** S06 (a DB file must exist to connect to).
- **Files/modules.** `db/connection.py`.
- **Acceptance criteria.** `get_connection() -> DuckDBPyConnection` opens read-only. Any write attempt through this connection (even bypassing the app-level parser) fails at the database level — the second independent guardrail `Tools.md` requires.
- **Required tests.** `tests/test_connection.py`: a `SELECT` succeeds; an `INSERT` issued directly through this connection (simulating a parser bypass) raises a database-level permission error, not just an app-level rejection.
- **Required evaluation cases.** None — infrastructure correctness, not domain correctness.
- **Verification commands.** `pytest tests/test_connection.py -v`

### S15 — `run_sql` tool

- **Goal.** The full tool contract from `Tools.md`: validate (S02) → execute (S14) → timeout at 5s → truncate at 10k rows → return a typed `SqlAttempt`.
- **Dependencies.** S02, S14, S08–S12 (needs real views to execute meaningful queries against).
- **Files/modules.** `db/run_sql.py`.
- **Acceptance criteria.** Matches `Tools.md`'s input/output schema exactly. A valid `SELECT` against `v_products` returns `status: "success"` with correct rows. A non-`SELECT` statement returns `status: "rejected"` without touching the database. A deliberately slow query (e.g. an unindexed cross join on a large synthetic table) returns `status: "timeout"` at 5s. A query returning >10,000 rows returns `status: "success"` with `truncated: true` and `row_count` equal to the true (untruncated) count.
- **Required tests.** `tests/test_run_sql.py`: one case per status value above, plus a case asserting `execution_ms` is populated and plausible.
- **Required evaluation cases.** Run `run_sql` directly (bypassing the agent) against three hand-picked known-answer queries from `scope.md`'s three canonical example questions; assert each returns the manually-verified correct result.
- **Verification commands.** `pytest tests/test_run_sql.py -v`

---

## Phase 3 — Gold set (agent-independent; per `scope.md`'s "eval before agent" rule)

### S16 — Gold set — basic bucket

- **Goal.** Author 20 `GoldQuestion` records (basic bucket) with hand-verified `gold_sql` and `gold_result`, executed directly against the real views.
- **Dependencies.** S08–S13.
- **Files/modules.** `eval/gold/basic.jsonl`.
- **Acceptance criteria.** 20 rows, each validating against the `GoldQuestion` model. Every `gold_sql` executes successfully via S14's connection and its live result exactly matches the stored `gold_result` (i.e. the file is generated by running the query and capturing the output, not hand-typed and hoped-correct).
- **Required tests.** `tests/test_gold_basic.py`: file has exactly 20 rows; every row validates against `GoldQuestion`; re-running every `gold_sql` now reproduces the stored `gold_result` exactly (a regression guard against the views changing later).
- **Required evaluation cases.** The 20 questions themselves are the evaluation cases — this slice's output *is* eval content, not something separately evaluated.
- **Verification commands.** `pytest tests/test_gold_basic.py -v`

### S17 — Gold set — semantic bucket

- **Goal.** 20 `GoldQuestion` records deliberately using business vocabulary (AOV, repeat rate, top-selling, growth rate, CLV) requiring the metric dictionary (S13) to answer correctly.
- **Dependencies.** S08–S13.
- **Files/modules.** `eval/gold/semantic.jsonl`.
- **Acceptance criteria.** 20 rows; every metric in S13's dictionary is exercised by at least one question; `gold_sql` for each implements the exact `sql_fragment` definition from `MetricDefinition`, not an approximation.
- **Required tests.** `tests/test_gold_semantic.py`: same structural checks as S16; additionally assert each of the 9 metric definitions appears in at least one question's `gold_sql` (by fragment inspection, not just question text).
- **Required evaluation cases.** As S16 — the questions are the eval content.
- **Verification commands.** `pytest tests/test_gold_semantic.py -v`

### S18 — Gold set — adversarial bucket

- **Goal.** 20 `GoldQuestion` records: ambiguous phrasing, implicit time windows, missing-data questions, and international-flavoured regional questions per `scope.md`'s UK-skew mitigation. 5–10 marked `is_graceful_failure_case = true`.
- **Dependencies.** S08–S13, S15 (the graceful-failure cases' expected behavior is defined by `run_sql`'s actual guardrail responses).
- **Files/modules.** `eval/gold/adversarial.jsonl`.
- **Acceptance criteria.** 20 rows. The graceful-failure subset has `gold_sql = null` and instead specifies the expected failure category (`schema_mismatch` / `ambiguity`) rather than a result. At least 3 questions specifically target non-UK markets (per the international-flavoured mitigation).
- **Required tests.** `tests/test_gold_adversarial.py`: file has exactly 20 rows; 5–10 have `is_graceful_failure_case = true` with no `gold_sql`; the rest have verified `gold_sql`/`gold_result` as in S16.
- **Required evaluation cases.** As S16/S17 for the non-graceful-failure rows; the graceful-failure rows are evaluated later by S25 against the agent's actual diagnosis category, not against a result.
- **Verification commands.** `pytest tests/test_gold_adversarial.py -v`

---

## Phase 4 — Agent orchestration

### S19 — SQL generation call

- **Goal.** The GPT-4o-mini call that turns a question plus injected schema/metric context into a candidate SQL query — the one place in the system with genuine dynamic reasoning.
- **Dependencies.** S01, S08–S13 (needs the real schema and metric dictionary to build the context).
- **Files/modules.** `agent/generate_sql.py`.
- **Acceptance criteria.** `generate_sql(question: str, prior_error: str | None) -> str`. Given `scope.md`'s three canonical questions, produces SQL referencing the correct views. Given a prior error, produces a materially different query, not a byte-for-byte repeat.
- **Required tests.** `tests/test_generate_sql.py` (mocked LLM call): given a fixed mock response, the function correctly extracts and returns the SQL string; the context passed to the LLM includes the full metric dictionary and view schemas (asserted on the captured prompt).
- **Required evaluation cases.** Live call (not mocked) against the 3 canonical questions from `scope.md`; manually confirm each generated query references the semantically correct view/metric. This is a smoke check, not the full gold-set run (that's S25).
- **Verification commands.** `pytest tests/test_generate_sql.py -v` (mocked); `python -m data_analyst_agent.agent.generate_sql --smoke-test` (live)

### S20 — Bounded retry loop

- **Goal.** Wire S19 + S15 into the exact retry mechanics from `ARCHITECTURE.md`: up to 3 attempts, error fed back, cost-cap and fast-fail checks enforced per attempt.
- **Dependencies.** S19, S15, S04.
- **Files/modules.** `agent/retry_loop.py`.
- **Acceptance criteria.** `run_turn_sql(question, session) -> SqlAttempt` (the winning or final attempt). Never exceeds 3 calls to S19/S15. On the fast-fail check triggering, returns immediately with zero new attempts. On the cost cap triggering mid-loop, stops immediately and marks the turn as budget-stopped rather than continuing.
- **Required tests.** `tests/test_retry_loop.py` (mocked S19/S15): a query that fails twice then succeeds on attempt 3 returns success with 3 logged attempts; a query that fails all 3 times returns the exhausted state with exactly 3 attempts logged, never a 4th; fast-fail short-circuits to zero attempts when the cache says so; cost-cap exceeded after attempt 1 halts before attempt 2.
- **Required evaluation cases.** Live run of all 20 basic-bucket gold questions (S16) through this function only (not the full turn) — record attempts-used per question as a smoke check ahead of the full harness (S25).
- **Verification commands.** `pytest tests/test_retry_loop.py -v`

### S21 — Chart-type selection + rendering

- **Goal.** The deterministic result-shape rule from `Tools.md` Bucket 3, plus the Streamlit rendering calls.
- **Dependencies.** S01 (pure function of typed result shape; testable with fixtures independent of S20).
- **Files/modules.** `agent/chart_select.py`.
- **Acceptance criteria.** `select_chart_type(result) -> Literal["scalar","line","bar","table"]`: 1×1 → `scalar`; a date-indexed column present → `line`; one categorical grouping column + one metric → `bar`; anything wider → `table`. A corresponding `render(chart_spec)` call using Streamlit's native widgets per `technology_stack.md`.
- **Required tests.** `tests/test_chart_select.py`: one fixture per chart-type rule above, asserting the correct type is chosen; no fixture is ambiguous between two rules.
- **Required evaluation cases.** For each of the 60 gold questions (once S16–S18 exist), assert the chart type chosen for its `gold_result` shape is the intuitively correct one — spot-checked by hand on a sample of 10, not automated (this is `scope.md`'s Layer 4, explicitly hand-rubric-only).
- **Verification commands.** `pytest tests/test_chart_select.py -v`

### S22 — Narrative wrap completion

- **Goal.** The GPT-4o-mini completion that turns a successful result into the founder-facing sentence, bound to the executed result, disclosing any default assumption.
- **Dependencies.** S01 (testable with fixture results independent of S20).
- **Files/modules.** `agent/narrative.py`.
- **Acceptance criteria.** `wrap(question, sql_attempt, chart_spec) -> {answer_text, assumption_disclosed}`. Never states a number absent from `sql_attempt`'s rows (checked by extracting all numbers in `answer_text` and asserting each appears in the result set). States the ranking metric used whenever the question was ambiguous about it (e.g. "top-selling" with no qualifier).
- **Required tests.** `tests/test_narrative.py` (mocked LLM call): given a fixed mock response, the function extracts fields correctly; a mocked response containing a number not present in the fixture result triggers an assertion failure in the test harness (proving the check works, not just exists).
- **Required evaluation cases.** Live call against 5 fixture results spanning all four chart types; hand-check each answer for the "never state an absent number" rule — this is `scope.md`'s Layer 5, explicitly the one layer without automated grading.
- **Verification commands.** `pytest tests/test_narrative.py -v`

### S23 — Failure diagnosis completion

- **Goal.** The GPT-4o-mini completion that classifies an exhausted-retries trace into `schema_mismatch` / `ambiguity` / `bug` and produces a rephrase suggestion.
- **Dependencies.** S01 (testable with fixture failure traces independent of S20).
- **Files/modules.** `agent/diagnosis.py`.
- **Acceptance criteria.** `diagnose(question, attempts: list[SqlAttempt]) -> FailureDiagnosis`. Given a fixture trace where all 3 attempts reference a non-existent column, returns `schema_mismatch`. Given a fixture trace showing 3 different metric interpretations tried, returns `ambiguity`.
- **Required tests.** `tests/test_diagnosis.py` (mocked LLM call): field extraction correctness on a fixed mock response.
- **Required evaluation cases.** Run against the 5–10 graceful-failure-designed adversarial gold questions (S18) once written; this is the "graceful failure rate" metric's raw material — assert the returned category matches each question's expected category.
- **Verification commands.** `pytest tests/test_diagnosis.py -v`

### S24 — Full turn orchestration → `Answer`

- **Goal.** Wire S20 → (S21 + S22 on success, or S23 on exhaustion) → S05's logging → the final `Answer` object, exactly matching `ARCHITECTURE.md`'s diagram end to end. **First observable-user-value milestone**: a real question, asked through a plain Python call, returns a real answer.
- **Dependencies.** S20, S21, S22, S23, S05, S04.
- **Files/modules.** `agent/orchestrator.py`.
- **Acceptance criteria.** `answer_question(question: str, session: SessionState) -> Answer`. All three `Answer.status` values (`success`, `graceful_failure`, `budget_stop`) are reachable and correctly populated per `InformationModel.md`'s field rules. Every call writes to `QueryAuditLog`; failed turns additionally write to `FailureLogEntry`.
- **Required tests.** `tests/test_orchestrator.py`: one integration test per termination condition from `ARCHITECTURE.md` (success, graceful failure, budget stop), using mocked LLM calls so the test is deterministic; assert the audit log gains exactly the expected number of entries per call.
- **Required evaluation cases.** Live, unmocked run of all three of `scope.md`'s canonical example questions end to end; manually confirm each `Answer` is correct and well-formed. This is the last manual smoke check before the automated harness (S25) takes over.
- **Verification commands.** `pytest tests/test_orchestrator.py -v`; `python -m data_analyst_agent.agent.orchestrator --smoke-test`

---

## Phase 5 — Evaluation harness & iteration

### S25 — Eval harness

- **Goal.** The CLI script from `scope.md`: run every gold question through S24, grade with S03, produce the JSON + Markdown report with per-bucket accuracy, attempts-until-success distribution, and graceful-failure rate.
- **Dependencies.** S03, S16–S18, S24.
- **Files/modules.** `eval/harness.py`.
- **Acceptance criteria.** `python -m data_analyst_agent.eval.harness --gold eval/gold --out report/` produces `report.json` and `report.md`. Report includes per-bucket accuracy (basic/semantic/adversarial), the attempts-until-success histogram, graceful-failure-rate over the designated subset, and is stamped with the current git commit hash and a hash of the active prompts.
- **Required tests.** `tests/test_harness.py`: running the harness against a small synthetic gold set with a mocked agent that always succeeds on attempt 1 produces 100% accuracy and an all-1s attempts histogram; a mocked agent that always exhausts retries produces 0% accuracy and a populated graceful-failure-rate.
- **Required evaluation cases.** The full 60-question gold set is the evaluation case — this is the slice that actually produces the headline numbers `scope.md`'s definition of done requires.
- **Verification commands.** `pytest tests/test_harness.py -v`; `python -m data_analyst_agent.eval.harness --gold eval/gold --out report/`

### S26 — Prompt iteration to accuracy floors

- **Goal.** Iterate on `agent/generate_sql.py`'s prompt (and, if needed, `agent/narrative.py`/`agent/diagnosis.py`'s) until `scope.md`'s definition-of-done floors are met.
- **Dependencies.** S25.
- **Files/modules.** `agent/generate_sql.py`, `agent/narrative.py`, `agent/diagnosis.py` (prompts only — no new modules).
- **Acceptance criteria.** Per `scope.md`: ≥75% basic-bucket accuracy, ≥55% semantic-bucket accuracy, ≥40% adversarial-bucket accuracy, ≥80% graceful-failure rate on the designated subset, median happy-path latency <8s.
- **Required tests.** None new — re-run S25's existing tests to confirm no regression from prompt edits.
- **Required evaluation cases.** Repeated full harness runs (S25) after each prompt revision; each run's report is retained (stamped by commit hash) so the iteration history itself is evidence of the process, not just the final number.
- **Verification commands.** `python -m data_analyst_agent.eval.harness --gold eval/gold --out report/` (repeated until floors are met)

---

## Phase 6 — Interface & deployment

### S27 — Streamlit UI

- **Goal.** Wire S24 into an actual chat interface: question box, answer + chart rendering, the "show SQL" toggle, and the five-part failure UX. **Second observable-user-value milestone**: a human, in a browser, can ask a question.
- **Dependencies.** S24, S21, S04.
- **Files/modules.** `app/streamlit_app.py`.
- **Acceptance criteria.** Running locally, a founder can type any of `scope.md`'s three canonical questions and see a correct answer, a chart, and a working SQL-toggle. A deliberately bad question (e.g. asking about data the schema doesn't have) shows the full five-part failure response. `st.session_state` correctly persists `SessionState` across turns within one browser session.
- **Required tests.** None automated for the UI layer itself (Streamlit UI testing is out of scope for v1); covered by the manual acceptance criteria above plus S24's existing tests underneath it.
- **Required evaluation cases.** Manual run-through of all three canonical questions plus at least 2 of the graceful-failure-designed adversarial questions, in the running app, by a human.
- **Verification commands.** `streamlit run app/streamlit_app.py`

### S28 — Docker containerization

- **Goal.** A single `Dockerfile` that reproduces the whole environment — app, agent, and eval harness — per `technology_stack.md`.
- **Dependencies.** S27, S25.
- **Files/modules.** `Dockerfile`, `requirements.txt` (or `pyproject.toml`).
- **Acceptance criteria.** `docker build -t data-analyst-agent .` succeeds. `docker run -p 8501:8501 data-analyst-agent` serves the same working Streamlit app as S27. Running the eval harness inside the container (`docker run data-analyst-agent python -m data_analyst_agent.eval.harness ...`) reproduces the same report S25 produces locally.
- **Required tests.** None beyond the two commands above — this slice's own acceptance criteria are its test.
- **Required evaluation cases.** The eval harness run *inside the container* must match the locally-run report from S26 on the accuracy numbers (proving environment parity, not just "it starts").
- **Verification commands.** `docker build -t data-analyst-agent . && docker run -p 8501:8501 data-analyst-agent`

### S29 — Hosted deployment

- **Goal.** Deploy S27 to Streamlit Community Cloud.
- **Dependencies.** S27, S26 (don't go live before the accuracy floors are met).
- **Files/modules.** None new — deployment configuration only.
- **Acceptance criteria.** Public URL responds. All three canonical questions work correctly on a cold start (matching `scope.md`'s "deployment liveness" criterion).
- **Required tests.** None automated.
- **Required evaluation cases.** Manual cold-start run-through of the three canonical questions against the live public URL.
- **Verification commands.** `curl -sI https://<app-url>.streamlit.app` (expect `200`)

---

## Phase 7 — Portfolio artifacts

### S30 — README + portfolio write-up

- **Goal.** The final deliverable per `scope.md`'s definition of done: data-cleaning decisions, evaluation strategy, headline accuracy numbers, run-locally instructions, and `failures.jsonl` commentary.
- **Dependencies.** S26, S28, S29.
- **Files/modules.** `README.md`.
- **Acceptance criteria.** Contains, at minimum: the data-cleaning decisions section (S07/S08's rules, stated plainly); the evaluation-strategy section (S03/S25's design); the final headline numbers from S26's last harness run; instructions to run locally (S27) and via Docker (S28); a short commentary on what `failures.jsonl` revealed during development.
- **Required tests.** None.
- **Required evaluation cases.** None — this is documentation, not code; its correctness is checked by cross-referencing every claimed number against S26's actual last report.
- **Verification commands.** None (manual review against the acceptance criteria).
