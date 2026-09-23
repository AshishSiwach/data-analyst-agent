# Data Analyst Agent

A portfolio-first descriptive SQL analyst agent for a Shopify-shaped e-commerce dataset.

**Premise:** *Ask business questions in plain English, get correct answers backed by inspectable SQL, with an eval harness that proves it.* The differentiator is the eval — result-based grading against a hand-crafted gold set — not the agent itself.

**Persona:** a UK-based solo founder of an online giftware/homewares store, with some international customers. The dataset (Online Retail II, UCI) is that shape, so the persona and the data agree.

## Status

Design phase complete; implementation not yet started. See [`_docs/`](_docs) for the full design — read `_docs/CLAUDE.md` first, it indexes the rest in order of authority.

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

## Planned stack

Claude (tool-calling) · DuckDB (read-only) · `sqlglot` (SQL guardrail) · Streamlit (UI + session state) · hand-rolled eval harness (`pandas`-based comparator) · Docker · Streamlit Community Cloud.

## Definition of done (v1)

- ≥75% basic / ≥55% semantic / ≥40% adversarial accuracy on a 60-question gold set
- ≥80% graceful-failure rate on designed-to-fail adversarial questions
- Median turn latency <8s on the happy path
- Hosted, publicly reachable Streamlit demo
- README with data-cleaning decisions, evaluation strategy, headline accuracy numbers, and `failures.jsonl` commentary

Full details in [`_docs/scope.md`](_docs/scope.md#definition-of-done-and-phase-2-backlog).
