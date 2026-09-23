# CLAUDE.md

Operational guide for anyone (human or agent) writing code in this repository. This file does not re-explain the product — see the source-of-truth docs below for that. It exists so that a coding-agent session can start work without re-deriving decisions that are already locked, and without accidentally violating one.

## Source of truth, in order of authority

1. `scope.md` (v1 scope — dataset, cleaning rules, metric list, accuracy floors, definition of done)
2. `autonomy.md` (what each action is allowed to decide on its own, and what tier it sits at)
3. `Architecture.md` (the control-flow diagram — node order, termination conditions, failure paths)
4. `Tools.md` (the exact I/O contract for `run_sql`, and which actions are deliberately *not* tools)
5. `InformationModel.md` (every entity's exact schema — this is what `models/entities.py` implements)
6. `technology_stack.md` (which library implements each layer, and why alternatives were rejected)
7. `implementation_plan.md` (the actual backlog — 30 ordered slices, S01–S30, each with its own acceptance criteria and verification command)

If a task in `implementation_plan.md` appears to require contradicting any of docs 1–6, stop and flag it rather than resolving the conflict silently — these six are the approved design; the plan is the build order, not a second design authority.

## Repo layout

```
data_analyst_agent/
  models/entities.py        # every entity from InformationModel.md, as pydantic models
  db/sql_guard.py             # sqlglot statement-type validator
  db/connection.py              # read-only DuckDB connection helper
  db/run_sql.py                   # the one agent-invoked tool (Tools.md contract)
  data/ingest.py                    # raw CSV -> DuckDB
  data/categorize.py                  # dim_product_category (LLM classification, offline)
  data/views.py                         # v_orders, v_order_lines, v_customers, v_products, v_daily_revenue
  data/metrics.py                         # MetricDefinition seed rows
  agent/session.py                          # SessionState, cost-cap check, fast-fail check
  agent/audit_log.py                          # QueryAuditLog + failures.jsonl writers
  agent/generate_sql.py                         # LLM call: question -> candidate SQL
  agent/retry_loop.py                             # bounded 3-attempt loop wiring generate_sql + run_sql
  agent/chart_select.py                             # deterministic chart-type rule + render
  agent/narrative.py                                  # LLM call: result -> founder-facing sentence
  agent/diagnosis.py                                    # LLM call: failed trace -> category + rephrase
  agent/orchestrator.py                                   # full turn: retry_loop -> chart/narrative or diagnosis -> Answer
eval/
  comparator.py               # result-set equality (order-invariant, float-tolerant)
  gold/{basic,semantic,adversarial}.jsonl
  harness.py                    # runs orchestrator against gold set, produces report.json / report.md
app/streamlit_app.py               # UI
Dockerfile
tests/test_*.py
README.md
failures.jsonl                       # runtime output, not committed with content — see .gitignore
query_audit.jsonl                      # runtime output, not committed with content
```

Every module above has exactly one design doc that defines its contract (named in the comment). When changing a module's behavior, check that doc first — don't infer the contract from the code that happens to exist.

## Environment & setup

- Python 3.11+ (pinned exactly once `Dockerfile` exists at slice S28; target 3.11 locally until then — this is an operational default, not a decision restated from the docs above).
- `pip install -r requirements.txt` (or `pyproject.toml`, whichever S01 establishes — pick one and don't maintain both).
- Required environment variables, read from `.env` (never committed — see `.gitignore`):
  - `OPENAI_API_KEY` — GPT-4o-mini per `technology_stack.md` §1.
  - `DUCKDB_PATH` — path to the database file built by `data/ingest.py`.
  - `COST_CAP_USD` — default `0.50`, overridable per `scope.md`; read once into `SessionState.cost_cap_usd`, never hardcoded a second time elsewhere.
- Commit `.env.example` with the three keys above and placeholder values; never commit `.env` itself.

## Working through `implementation_plan.md`

- Slices are a strict DAG. Do not start slice `S<n>` until every slice listed in its `Dependencies` row is complete (its own tests and verification command pass).
- A slice is "done" when: its files exist at the paths it names, its required tests pass, its verification command exits 0, and — where it specifies one — its evaluation case has been run and its result recorded (not silently skipped).
- Don't do work that belongs to a later slice inside an earlier one "while you're in there" — e.g. don't wire the retry loop (S20) while building `run_sql` (S15). Vertical-slice discipline is the point of the plan; scope creep across slice boundaries defeats it.
- Don't re-derive acceptance criteria from first principles — `implementation_plan.md` already states them per slice. If a slice's criteria seem wrong given the docs above, flag it rather than quietly building something different.

## Structural rules (code-enforced, not stylistic)

These come directly from `autonomy.md` and `Tools.md` and are the things most likely to regress silently during unrelated changes. Each is stated so it can be checked, not just remembered.

- **The DB connection is opened read-only, always.** `db/connection.py::get_connection()` must pass `read_only=True` (or the DuckDB equivalent) on every call. Any PR touching this file that removes or conditions that flag is wrong regardless of what else it does — this is one of the two independent write-blocks `Tools.md` requires.
- **Every SQL string reaches the database only through `db/sql_guard.py::validate()` first.** No code path calls DuckDB's execute directly with a string that hasn't passed `validate()`. `db/run_sql.py` is the only caller of the raw connection for LLM-generated queries.
- **`run_sql`'s bounds are hardcoded, not configurable at the call site:** 5-second timeout, 10,000-row cap, `status` restricted to `success | error | timeout | rejected`. These numbers live in one place (`db/run_sql.py`) — don't duplicate the literals elsewhere.
- **The retry loop (`agent/retry_loop.py`) hard-caps at 3 attempts via a counter, never a model-negotiated decision.** The model can be told it failed and asked to retry; it cannot be given a way to ask for a 4th attempt.
- **The session cost cap is checked after every LLM-invoking call** — every `generate_sql` attempt and every `narrative.wrap` call, not once per turn. Implement this as one function (`agent/session.py::check_cost_cap`) called from both sites, not two copies of the same check.
- **Exactly one agent-invoked tool exists: `run_sql`.** Do not add a second dynamically-invoked tool (e.g. reintroducing `list_metrics` or `plot_chart` as model-callable) without first updating `Tools.md` and `Architecture.md` — that would reopen a locked design decision, not just add a feature.
- **`agent/chart_select.py`'s type decision is a pure function of result shape.** No LLM call anywhere in this module.
- **`agent/narrative.py`'s output must never contain a number, trend, or comparison absent from the `SqlAttempt` result it was given.** Any change to this module's prompt or post-processing must keep this checkable: tests should extract every numeral from `answer_text` and assert each one traces back to a value in the result set.
- **`SessionState` (`agent/session.py`) holds exactly the fields `InformationModel.md` specifies** — `session_id`, `started_at`, `cost_spent_usd`, `cost_cap_usd`, `turn_ids`, `failed_questions_cache`. Do not extend it to store conversation history or prior answers; that would silently turn it into the Phase-2 multi-turn seam `InformationModel.md` explicitly says it is not.
- **`failed_questions_cache` keys on normalized (lowercased, whitespace-collapsed) question text, not a semantic match.** Don't "improve" this with embeddings or fuzzy matching without updating `InformationModel.md` first — the exact-ish match is a stated v1 scoping decision, not an oversight.
- **`QueryAuditLog` is written unconditionally on every attempt; `failures.jsonl` only on full failure.** Both are append-only — no code path rewrites or truncates an existing line in either file.

## What not to add

Each of these was evaluated and rejected in `technology_stack.md` or `Architecture.md` on cost/complexity grounds, not overlooked. Adding one is a scope change, not a bug fix:

- No LangGraph, PydanticAI, or other agent-orchestration framework — the retry loop is hand-rolled on the provider SDK by design.
- No Redis, Postgres, or any external state store — `st.session_state` is sufficient for v1's session scope.
- No general LLM-eval framework (promptfoo, DeepEval, OpenAI Evals) in place of `eval/harness.py` — the comparator's execute-and-compare-result-sets logic is bespoke and the harness is the project's actual differentiator.
- No Grafana or other monitoring stack in v1 — explicitly deferred in `technology_stack.md` §8; JSONL + the eval report are the only observability surface until that's revisited.
- No write-capable DB credential, even for a dev/debug convenience path. If a debugging need seems to require one, that's a sign to add a read-only diagnostic query instead, not to loosen the credential.

## Conventions

- **All data crossing a module boundary is a `pydantic` model from `models/entities.py`.** No ad-hoc dicts passed between `agent/*` modules — if a new field is needed, add it to the model, don't bag it onto an untyped kwarg.
- **Logging is stdlib `json`, one line per record, append mode only.** No `structlog`/`loguru` — matches `technology_stack.md` §8's reasoning; introducing one is a regression, not an upgrade.
- **Lint/format: `ruff`.** `ruff check . && ruff format --check .` must exit 0 before a slice is considered complete. No `mypy` requirement — `pydantic` already gives runtime validation at every module boundary, and static typing wasn't asked for; don't add the tooling overhead unless a real bug demonstrates the need.
- **Every module gets its tests in `tests/test_<module>.py`, run with `pytest`.** A slice's tests live alongside the module they test, not batched into one giant test file.

## Verification

- Unit/integration tests: `pytest -q` (or the specific file named in the slice's verification command in `implementation_plan.md`).
- Lint/format: `ruff check . && ruff format --check .`
- Eval harness (once S25 exists): `python -m data_analyst_agent.eval.harness --gold eval/gold --out report/`
- Local run (once S27 exists): `streamlit run app/streamlit_app.py`
- Docker (once S28 exists): `docker build -t data-analyst-agent . && docker run -p 8501:8501 data-analyst-agent`

A slice is not done because the code looks right — it's done when its own verification command, as stated in `implementation_plan.md`, actually exits 0.
