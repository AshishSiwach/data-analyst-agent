# Data Analyst Agent

A SQL analyst agent for a UK e-commerce dataset. Ask it a business question in plain English, and it answers with a real number backed by a real, inspectable SQL query, not a guess.

I built this as a portfolio project, but I tried to treat it like a real one: the eval harness came before the agent, every cleaning decision is written down somewhere, and the whole thing is meant to be picked apart. That's who this README is for, really: someone reviewing the work, someone deciding whether to hire me, or me in six months wondering why I made a particular call.

**Try it live: https://data-analyst-agent-shopify.streamlit.app/**

A few questions to start with, the same ones I used to validate the whole build:

- "Which products are selling the most in which region?"
- "What is my average order value across all regions?"
- "Which products are the least performing?"

## The premise

The interesting part of this project isn't the agent. Plenty of things can turn a question into SQL. The interesting part is the eval: a hand-built gold set of 60 questions with known-correct answers, graded automatically by actually running the SQL and comparing results, not by eyeballing whether the answer "sounds right." If I change a prompt, I can tell within a couple of minutes whether I made things better or worse. That loop is what let me iterate with any confidence at all.

The persona behind the questions is a UK-based solo founder running an online giftware and homewares store, with a bit of international business. The dataset (Online Retail II, from the UCI repository) is genuinely that shape, so the story isn't invented to fit the data.

## Where things stand

All 30 steps of the [build plan](_docs/implementation_plan.md) are done. The agent, the eval harness, the Streamlit UI, the Docker image, and the hosted demo linked above are all live and working together.

## How it actually works

![System architecture diagram: a founder's question flows from the Streamlit UI into the orchestrator, through a bounded generate-SQL / guard / run-SQL retry loop against a read-only DuckDB semantic layer, then either a deterministic chart-select and narrative LLM call on success or a diagnosis LLM call on failure - with conversation memory, OpenAI GPT-4o-mini, JSONL logging feeding a monitoring dashboard, two offline eval harnesses, and the CI/deployment pipeline shown around it.](_docs/diagrams/architecture.svg)

Nothing here is an open-ended agent freely deciding what to do next. It's a fixed pipeline, and I mean that as a design choice, not a limitation: generate a SQL query, validate it, run it read-only with a 5 second timeout and a 10,000 row cap. If it fails, the error goes back to the model and it gets up to three attempts total. If it succeeds, a plain deterministic rule (no LLM involved) picks a chart type, and a small model wraps the result in one sentence a founder could read. If all three attempts fail, a second model call looks at the failure and explains why, then suggests a better way to ask.

The other piece worth knowing about is `data/views.py`. Rather than let the agent invent what "net revenue" or "lifetime value" means every time it writes a query, those definitions are baked once into a set of semantic-layer views (`v_orders`, `v_order_lines`, `v_customers`, `v_products`, `v_daily_revenue`), and the agent just queries them. The full reasoning behind this architecture, including the alternatives I considered and rejected, is in [`_docs/Architecture.md`](_docs/Architecture.md).

## Results, from the last full run

| Bucket | Accuracy | Required floor |
|---|---|---|
| Basic (20 questions) | 95.0% | 75% |
| Semantic (20 questions) | 95.0% | 55% |
| Adversarial (20 questions) | 90.0% | 40% |
| Graceful failure rate | 100.0% | 80% |

Median latency on a happy-path question is around 1.8 seconds, well inside the 8 second target.

That run was against git commit `0c0cc66`, prompt hash `ae0c58f4725f`, across all 60 hand-written (question, gold SQL, gold result) triples. You can reproduce it yourself:

```bash
uv run python -m data_analyst_agent.eval.harness --gold data_analyst_agent/eval/gold --out report/
```

Every report gets stamped with the git commit and a hash of the active prompt (this includes [`skills/sql_domain_knowledge.md`](data_analyst_agent/skills/sql_domain_knowledge.md), where the actual rules live), so you can always tell exactly what produced a given number. The numbers do drift a point or two between runs since the model isn't seeded, but they've stayed comfortably clear of the floors above throughout the project.

## Decisions I made while cleaning the data

The source is the raw Online Retail II workbook, roughly a million invoice line items from a real UK-based online giftware retailer between 2009 and 2011. Nothing gets dropped or altered on the way in. Every cleaning rule lives in code, in [`data/views.py`](data_analyst_agent/data/views.py), not scattered across prompts where it would be easy to lose track of.

A few decisions worth explaining rather than just stating:

**Cancellations only count if there's a real order behind them.** The raw data has no explicit link between a cancellation invoice and the order it's cancelling. So I made a rule: a cancellation is treated as real, and excluded from `v_orders` entirely, only if the same customer bought at least one of the same products elsewhere in a normal, non-cancelled invoice. Otherwise it's noise, not a return. Out of 8,292 cancellation invoices, 7,276 had that evidence and got excluded properly. The other 1,016 didn't (625 belonged to a known customer with no matching purchase, 391 had no customer at all), and I checked those numbers by hand before trusting the rule. I wanted a split that looked plausible, not a rule that quietly excluded everything or nothing.

**Cancellation detection goes by invoice number, not stock code.** The original scope doc I wrote for myself said returns were "negative quantity, StockCode prefixed C." That's wrong; I checked it against the real data and it's actually the invoice number that's prefixed with C, never the stock code. Small thing, but it would have silently broken the cleaning logic if I'd trusted the doc over the data.

**Orders with no customer attached still count for revenue, just not for anything per-customer.** About a quarter of rows have no `CustomerID`. Those stay in `v_orders` and `v_products` so revenue and product totals are complete, but `v_customers` filters them out. If it didn't, a single "null customer" would aggregate over three million dollars in orders and show up as the store's best customer, which is obviously wrong.

**"Net of returns" means summing everything, returns included, not filtering returns out.** A returned line already carries a negative quantity and revenue, so a plain sum nets it out correctly on its own. `v_products.total_revenue` actually does the opposite on purpose (it excludes returns entirely), which is a narrower, different convention used only for ranking products, and I made sure it never gets substituted in for a question that explicitly asks for a net figure.

## How the eval actually works

This is the part of the project I'd point a fellow engineer to first. [`eval/comparator.py`](data_analyst_agent/eval/comparator.py) grades on the executed result, never on the SQL text. If the agent writes an uglier query that lands on the same numbers, it still passes, which is the right behavior since the founder never sees the SQL.

First, what "accuracy" actually means here, since it's not quite the same thing in every bucket. Each bucket has 20 questions, and accuracy is just the fraction of those 20 the agent got right, but "right" means something different depending on the question. For basic and semantic questions, right means the agent's final answer, after up to three attempts, matches the gold result exactly (within the tolerances below). There's a correct number, and the agent either landed on it or it didn't.

The adversarial bucket is mixed on purpose. Most of its 20 questions are normal, gradable questions, just harder ones (implicit time windows, ambiguous phrasing that still has a sensible default, missing entities). But 5 to 10 of them are questions I designed to be unanswerable: they ask about something the schema genuinely doesn't track, or reference something with no antecedent, or ask "why" something happened when this system only describes, never diagnoses. For those, there's no gold answer to match. "Right" means the agent recognized it couldn't answer and declined instead of guessing or hallucinating a number. So adversarial accuracy is a blend of "answered correctly" and "declined correctly," which is why it's held to a lower bar (40%) than basic and semantic.

Graceful failure rate reports that same "declined correctly" signal on its own, isolated from the rest of the adversarial bucket. Adversarial accuracy blends two different things into one number (did it answer the normal hard questions, did it decline the unanswerable ones), so a system could hide a weak decline rate behind a strong answer rate and still look fine. Graceful failure rate is the unanswerable subset by itself: of just those 5 to 10 questions, what fraction ended in a real decline, complete with an explanation of what's missing and a rephrased question to try instead, rather than a wrong guess or a raw error.

A few things it handles so grading stays fair:

- Row order doesn't matter unless the question specifically implies one.
- Numbers are compared with a 0.1% tolerance, so floating point noise never fails a correct answer.
- Columns are compared by position and value, not by name, so the agent's `SUM(price)` still matches gold's `total_revenue` as long as the actual numbers agree.

[`eval/harness.py`](data_analyst_agent/eval/harness.py) runs all 60 questions through the real agent, never a mock, and reports per-bucket accuracy, how many attempts each question needed, and the graceful failure rate. Every report is stamped with the commit and prompt hash it was generated from.

A second harness, [`eval/multi_turn_harness.py`](data_analyst_agent/eval/multi_turn_harness.py), does the same thing for follow-up questions: a small, hand-authored set of multi-turn conversations, each graded turn by turn against one real, shared conversation memory (`uv run python -m data_analyst_agent.eval.multi_turn_harness --gold data_analyst_agent/eval/gold_multi_turn --out report_multi_turn/`). It exists because every multi-turn bug this project has hit - a dropped filter, a dropped ranking, a follow-up that quietly drifted to a different country - was found by me testing the app by hand, never by an automated check, until this did.

## Testing

Separate from the eval harness above: 254 pytest tests across the codebase, everything mocked at the LLM boundary so the suite runs in about two minutes with no API key and no network access.

```bash
uv run pytest -q
uv run ruff check . && uv run ruff format --check .
```

Both run automatically on every push and pull request (see [CI](#ci) below). A few tests do need the real database to already exist (schema introspection reads the live views), and skip with a clear message rather than failing if it isn't there yet - the `data_analyst_agent.duckdb` committed in this repo already satisfies them.

## Monitoring

The agent writes three append-only JSONL logs unconditionally, independent of the eval harness: `query_audit.jsonl` (every SQL attempt, success or failure), `failures.jsonl` (turns that exhausted all three attempts), and `llm_calls.jsonl` (every LLM call, with token counts, cost, and latency). None of this needs a database or an external service to produce, per the project's "no Redis/Postgres/Grafana in v1" call in [`_docs/technology_stack.md`](_docs/technology_stack.md) - it's just structured logging, one line per event.

A local, operator-only dashboard reads those three files directly and renders them:

```bash
uv run streamlit run data_analyst_agent/app/dashboard.py
```

It shows success/error/validation-failure rates, query execution time and model response time, request volume and attempts-per-turn distribution, token usage and cost (broken down by day and by call type), and a diagnosis-category breakdown for failed turns. It's deliberately not part of the public Streamlit Community Cloud deployment - these logs can contain error text and per-call cost, which has no reason to be public, and running it separately means never having to think about that.

One KPI I considered and dropped: concurrent users. This app has no login and no per-user identity, so there's no "user" for a session to actually belong to - showing it would just be a relabeled request-volume chart under a misleading name.

## What failures.jsonl taught me

Every turn that burns through all three attempts and still fails gets logged to `failures.jsonl`, separately from the gold-set harness. It's an honest record of everything that actually went wrong during development, and rereading it was more useful than I expected.

Most of it looked exactly like I'd hoped: questions correctly declined because the schema genuinely can't answer them (Scotland-level detail, email open rates, social media activity, none of which this data tracks), and a handful correctly declined as ambiguous ("compare this to last year" has no "this" to point to in a system with no memory between turns).

Two things stood out as genuinely useful signal:

- One real SQL-generation bug ("rank each product's revenue within its category") got logged as a bug rather than misclassified as something else, which told me the failure taxonomy was actually doing its job.
- The exact same question, "what's driving the change in our numbers," got diagnosed as ambiguous once and as a schema mismatch on a later run. That's a good reminder that the diagnosis step is itself a model call, not a lookup table, so its category label is useful but not something to treat as ground truth.

Honestly, most of the log is just the paper trail of my own iteration. Several entries are the exact questions I later fixed prompt bugs for (a monthly sales trend for France, a per-customer order-ranking question, an AOV comparison), and this log is what surfaced some of those problems before they ever showed up in the 60-question gold set.

## Running it locally

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

The `data_analyst_agent.duckdb` file already committed in this repo has all of the above already applied. You only need the ingest-through-metrics steps if you want to rebuild it from scratch.

## Running it in Docker

```bash
docker build -t data-analyst-agent .
docker run -p 8501:8501 --env-file .env data-analyst-agent
```

If `data_analyst_agent.duckdb` isn't already there, the container builds it on first run (see [`docker/entrypoint.sh`](docker/entrypoint.sh)). You can skip that and start instantly by mounting the repo's own copy in as a volume:

```bash
docker run -p 8501:8501 --env-file .env \
  -v "$(pwd)/data_analyst_agent.duckdb:/app/data_analyst_agent.duckdb" \
  data-analyst-agent
```

The eval harness runs the same way, in the same environment:

```bash
docker run --env-file .env \
  -v "$(pwd)/data_analyst_agent.duckdb:/app/data_analyst_agent.duckdb" \
  data-analyst-agent \
  python -m data_analyst_agent.eval.harness --gold data_analyst_agent/eval/gold --out report/
```

## Design docs, if you want the full reasoning

I wrote these before writing any code, and kept them updated as decisions changed. If you're the kind of reviewer who wants to see the thinking, not just the result, start here.

| Doc | What's in it |
|---|---|
| [`scope.md`](_docs/scope.md) | What's in and out of scope, the dataset and cleaning rules, the metric dictionary, the eval design, and the definition of done |
| [`autonomy.md`](_docs/autonomy.md) | How much room each agent action gets to decide on its own |
| [`Architecture.md`](_docs/Architecture.md) | The control flow pattern, the full turn diagram, and why I didn't build an open-ended agent |
| [`Tools.md`](_docs/Tools.md) | The exact contract for `run_sql`, and why the other candidate tools stayed as plain deterministic code instead |
| [`InformationModel.md`](_docs/InformationModel.md) | Every entity's schema, domain data, operational data, and evaluation data |
| [`technology_stack.md`](_docs/technology_stack.md) | What library runs each layer, and what I considered and ruled out |
| [`implementation_plan.md`](_docs/implementation_plan.md) | The 30-step build plan, each step with its own acceptance criteria and a way to verify it |

## Stack

OpenAI GPT-4o-mini for structured outputs (no agent framework), DuckDB as a read-only database, `sqlglot` as a SQL guardrail, Streamlit for the UI and session state, a hand-rolled eval harness with a pandas-based comparator, Docker, and Streamlit Community Cloud for hosting.

## CI

Every push and pull request against `main` runs two jobs in [GitHub Actions](.github/workflows/ci.yml): one installs the project with `uv` and runs `ruff check` plus `ruff format --check`, the other runs the full pytest suite (254 tests, all mocked at the LLM boundary so no API key or network access is needed to pass). Neither job needs a secret, since the tests skip anything that needs the real database only if it isn't there, and the database committed in this repo already satisfies them.

The 60-question eval harness deliberately isn't part of this. It's a real, paid GPT-4o-mini call per question and isn't seeded, so running it on every push would cost money and produce a noisy, flaky-looking check for a point or two of natural run-to-run drift. It stays a manual, deliberate step (see [Results](#results-from-the-last-full-run)), not a merge gate. There's no deployment step either: the Streamlit Community Cloud app redeploys itself automatically on every push to `main`, so there's nothing for a workflow to trigger.

## Definition of done, and where it stands

- 95.0% / 95.0% / 90.0% basic, semantic, and adversarial accuracy, against floors of 75% / 55% / 40%. See [Results](#results-from-the-last-full-run).
- 100.0% graceful failure rate, against a required 80%.
- Median turn latency around 1.8 seconds, against an 8 second ceiling.
- A hosted, public Streamlit demo: https://data-analyst-agent-shopify.streamlit.app/
- This README, covering the cleaning decisions, the eval strategy, the headline numbers, and what `failures.jsonl` taught me.

All of it met. Full details in [`_docs/scope.md`](_docs/scope.md#definition-of-done-and-phase-2-backlog).
