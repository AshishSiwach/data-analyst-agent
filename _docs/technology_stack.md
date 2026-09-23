# Technology Stack — v1 (MVP)

Companion to `scope.md`, `AUTONOMY.md`, `ARCHITECTURE.md`, `Tools.md`, and `InformationModel.md`. Those five docs already decided *what* the system does and *how* the pieces are wired together — this doc picks the actual technology for each piece, constrained by what's already locked, not reopening it.

## Constraints carried over (not re-litigated here)

Single fixed dataset (Online Retail II), read-only throughout, single-turn stateless, one genuine agent-invoked tool (`run_sql`) bounded to 3 attempts, hosted Streamlit already chosen as the v1 interface, a ~$0.50 per-session cost cap, and a solo ~9–11 day build budget. Every choice below is filtered through those, not evaluated in a vacuum — the "best" tool in general is often wrong here if it doesn't fit the budget or the deliberately narrow control flow `ARCHITECTURE.md` specified.

The stack has to visibly demonstrate four things: **tool calling** (`run_sql`, per `Tools.md`), **orchestration** (the bounded retry loop, per `ARCHITECTURE.md`), **memory** (session-scoped state, per `InformationModel.md` — scoped honestly, not inflated), and **evaluation** (the gold-set harness, per `scope.md` — the one layer with zero room to under-invest).

---

## 1. LLM provider

| Option | Pros | Cons |
|---|---|---|
| **OpenAI GPT-4o-mini** | Extremely mature function-calling API; huge ecosystem (most NL2SQL benchmark literature, e.g. Spider/BIRD, targets this API shape); cheap enough to fit comfortably inside the per-session cost cap | Nothing material for this narrow use case |
| **Anthropic Claude** (fast/cheap tier) | Mature native tool-calling with strict schema adherence; strong SQL-generation and self-correction on fed-back errors | Functionally comparable to GPT-4o-mini here — no decisive edge that would change the recommendation |
| **Self-hosted open-source (Llama/Qwen class)** | Near-zero marginal cost; strong "no vendor lock-in" portfolio story | Weaker structured/tool-calling reliability out of the box; adds real hosting/serving infrastructure the 9–11 day budget has no room for |

**Recommendation: GPT-4o-mini.** Mature, cheap, reliable function-calling — the bounded-3-retry loop and the eval harness both depend on the model consistently emitting well-formed tool calls rather than prose-wrapped SQL, and this tier delivers that at a cost that leaves comfortable headroom under the per-session cap. Claude is an equally valid alternative if provider preference changes later; nothing else in the stack depends on which one is chosen. Self-hosted is ruled out purely on budget, not capability.

## 2. Orchestration

| Option | Pros | Cons |
|---|---|---|
| **Hand-rolled loop, provider SDK directly** | Total control; maps 1:1 onto `ARCHITECTURE.md`'s diagram — every node in that mermaid chart becomes a few lines of inspectable Python, nothing hidden inside framework internals; matches the "deliberately narrow" philosophy exactly, since there's exactly one dynamic tool call to orchestrate; fastest to build and debug | Reimplements a few small conveniences (retry backoff, output validation) a framework gives for free — though minimal here given the narrow surface |
| **PydanticAI** | Typed tool I/O and structured outputs via Pydantic models — maps very well onto the entities `InformationModel.md` already specified (`SqlAttempt`, `Answer`, `FailureDiagnosis`); built-in retry-on-validation-failure | Still a framework abstraction layer to learn and debug through; the deterministic steps around the loop (fast-fail check, cost cap, chart-type rule) sit *outside* its agent loop anyway and need hand-wiring regardless, so the framework only covers part of the diagram |
| **LangGraph** | Closest structural mapping to the mermaid diagram (each diagram node becomes a graph node); well-known name, real production usage, built-in state checkpointing (useful if Phase-2 multi-turn ever needs resumable state) | Heaviest option for a system whose actual agentic decision surface is deliberately one tool call — `ARCHITECTURE.md` explicitly rejected an open, many-step pattern for v1; using a graph-orchestration framework to wrap a single bounded retry loop is over-engineering relative to what's needed, and adds ramp-up time the budget doesn't have |

**Recommendation: hand-rolled loop on the provider SDK, with `pydantic` (the validation library, not a framework) used to define and validate the typed entities from `InformationModel.md` at runtime.** This gets the type-safety benefit PydanticAI offers without inheriting its control-flow opinions — the loop itself stays exactly as narrow and legible as `ARCHITECTURE.md` specified. LangGraph is the pattern to reconsider only if Phase-2's open planner (`ARCHITECTURE.md`'s "justified later" pattern) actually gets built — that's a genuinely multi-step, dynamically-directed problem where graph orchestration earns its complexity.

## 3. Database engine

| Option | Pros | Cons |
|---|---|---|
| **DuckDB** | Columnar, built for exactly this workload — `GROUP BY region`, `SUM(revenue)`-style aggregation over ~1M rows; embedded, zero server; can query the raw CSV directly without a separate ETL step | None material for this workload |
| **SQLite** | Even more ubiquitous, zero-config, embedded | Row-oriented storage is a worse fit for the aggregation-heavy query pattern every gold question in `scope.md` consists of |
| **Postgres (hosted)** | "Real" client-server RDBMS, would matter for genuine multi-tenant concurrent writes | Zero concurrent-write requirement exists in v1 to justify it; adds hosting/ops burden with no corresponding benefit |

**Recommendation: DuckDB, read-only connection — confirming `scope.md`'s existing guardrail, on the specific grounds of OLAP-style query fit**, not just because it was already written down.

## 4. SQL guardrail enforcement

| Option | Pros | Cons |
|---|---|---|
| **`sqlglot`** (AST-based SQL parser) | Properly parses the statement rather than pattern-matching it — catches edge cases a keyword check would miss (a CTE smuggling a write, comment-obfuscated statements, multi-statement injection via semicolons) | Small added dependency |
| **`sqlparse`** (lighter tokenizer) | Simple, small, adequate for basic statement-type sniffing | Less rigorous as a full parser than `sqlglot` |
| **Hand-rolled keyword/regex check** | Fastest to write | Exactly the kind of "best-effort" check `AUTONOMY.md` explicitly rejects for this guardrail — regex keyword-blocking is a well-known weak pattern, and `Tools.md` calls for "two independent structural blocks," not one fragile one |

**Recommendation: `sqlglot`.** This is the one guardrail in the whole system `AUTONOMY.md` insists must be "enforced structurally in code, never by prompt instruction alone" — it's specifically the wrong place to cut the corner a regex check would cut.

## 5. Memory / session state

| Option | Pros | Cons |
|---|---|---|
| **`st.session_state`** (Streamlit's native per-browser-session store) | Already scoped exactly right — per session, ephemeral, lost on restart, matching `InformationModel.md`'s `SessionState` entity precisely; zero extra infrastructure | None — it's a plain dict-like object, which is all `SessionState`'s three fields (cost tracker, fast-fail cache, turn-id list) actually need |
| **Redis / external key-value store** | Would matter if the app needed to survive process restarts or scale across multiple workers | Solves a problem this single hosted-demo app doesn't have; a dependency added for portfolio appearance, not need |
| **A proper agent-memory library** (vector store, conversation buffer, etc.) | Solves conversational recall / semantic retrieval across turns | The wrong problem entirely — `scope.md`'s single-turn-stateless design and `InformationModel.md`'s explicit note that `SessionState` is *not* the Phase-2 multi-turn seam both rule this out for v1 |

**Recommendation: `st.session_state`, holding a plain object with the three fields `InformationModel.md` specified.** This directly answers "memory, if needed" — it's barely needed, and reaching for anything heavier here would misrepresent what v1 actually requires.

## 6. Evaluation framework

| Option | Pros | Cons |
|---|---|---|
| **Hand-rolled CLI harness** (`eval.py`, matching `scope.md`'s exact spec) | Full control over the bespoke metrics `scope.md` requires — per-bucket accuracy, attempts-until-success distribution, graceful-failure-rate, git-commit + prompt-hash stamping — none of which are built into general eval tools; fastest to build since it's a direct implementation of an already-fully-specified design | Nothing off-the-shelf to lean on for reporting UI |
| **A general LLM-eval framework** (promptfoo / DeepEval / OpenAI Evals) | Recognizable tooling, built-in assertion types, dashboard output for free | Built around grading one response against a rubric, not around executing SQL and comparing *result sets* with order-invariance and float-tolerance rules — you'd end up writing a custom grader plugin inside the framework anyway, which is most of the same work as hand-rolling, plus fighting the framework's chat-eval-first assumptions |
| **pytest with `@pytest.mark.parametrize`** | Free CLI, free parallelization (`pytest-xdist`), trivial CI integration later | Native pytest reporting isn't shaped like the specific JSON+Markdown, per-bucket report `scope.md` wants — still needs a custom aggregation layer on top, undercutting most of the convenience |

**Recommendation: the hand-rolled harness exactly as `scope.md` specified**, using `pandas` internally for the result-set comparator (`DataFrame` equality after sorting, with float tolerance) since that's the natural tool for exactly this comparison and nothing else in the stack already needs a heavier alternative. This is the layer with the least room to under-invest — it's the project's actual differentiator — and it's also the layer where a general framework fits worst, since the grading logic is genuinely bespoke (execute-and-compare-results, not judge-a-response).

## 7. Charting library

| Option | Pros | Cons |
|---|---|---|
| **Streamlit native** (`st.metric`, `st.line_chart`, `st.bar_chart`, `st.dataframe`) | Zero new dependency; maps exactly onto the four-value `chart_type` enum from `InformationModel.md`'s `ChartSpec`; least code | No interactivity beyond Streamlit's defaults |
| **Plotly** (`st.plotly_chart`) | Richer interactivity (hover, zoom) | More code per chart for a need — 4 fixed chart types, no interactivity requirement anywhere in the docs — that's already fully met by the simpler option |
| **Matplotlib** | Most portable/common | Static images only, needs rendering to PNG and displaying via `st.pyplot`/`st.image` — strictly more work than the other two for no benefit here |

**Recommendation: Streamlit native charts.** The deterministic chart-type rule (`Tools.md`, Bucket 3) already picks from exactly the four types Streamlit's built-ins cover natively.

## 8. Logging / audit persistence (brief — the format is already implied)

`scope.md` already named the file `failures.jsonl`; `InformationModel.md` specified `QueryAuditLog` and `FailureLogEntry`'s exact fields. The only real decision left is the write mechanism, and it's not a close call: **flat JSONL files, appended via Python's stdlib `json` module** — no logging library (`structlog`, `loguru`) is justified for a volume this small (one demo session at a time, capped by the cost cap anyway), and JSONL is already git-diff-friendly and directly loadable by `pandas` for the eval harness.

**Grafana — deferred, not v1.** Considered and explicitly parked: a single-session demo app with no concurrent traffic and no uptime target has nothing real to monitor, and Grafana doesn't read JSONL natively — it needs Prometheus or a database in between, or a JSON-datasource plugin, more infra than the problem justifies today. Worth revisiting only if (a) this becomes a genuinely multi-user product where live monitoring earns its keep, or (b) it's wanted purely as a portfolio dashboard screenshot, in which case the cheap path is Grafana Cloud's free tier plus a lightweight JSON/SQL datasource over the JSONL — not a self-hosted Prometheus stack.

## 9. Deployment (brief — already locked in `scope.md` / `ARCHITECTURE.md`)

**Streamlit Community Cloud.** Restated for completeness, not reopened: click-a-link demo, free hosting, fits the build budget; FastAPI + Render is Phase 2.

## 10. Containerization (added — reproducibility)

| Option | Pros | Cons |
|---|---|---|
| **Docker** (single `Dockerfile`; `docker-compose.yml` if a second service — e.g. Grafana, later — is ever added) | Pins the *entire* environment — Python version, system libraries, DuckDB build — not just the code version; anyone can `docker build && docker run` and reproduce the exact environment this was built and evaluated in; standard portfolio expectation | Small added maintenance keeping the image current |
| **`requirements.txt` / `pyproject.toml` + a setup README** | Zero extra tooling | Pins Python packages but not the OS/system layer — "works on my machine" risk remains, and doesn't demonstrate environment reproducibility as concretely |
| **Conda / Nix environment** | Also pins system-level dependencies | Heavier tooling than the project needs; less familiar to most reviewers than Docker |

**Recommendation: Docker.** A single `Dockerfile` covering the Streamlit app, the agent code, and the eval harness — `docker build -t data-analyst-agent . && docker run -p 8501:8501 data-analyst-agent` should be enough for anyone to reproduce the whole demo and rerun the eval harness in the same environment it was built in. If Grafana is added later, that's the point this becomes `docker-compose.yml` (app + Grafana + whatever bridges the JSONL to it), not before.

---

## Recommended MVP stack

| Layer | Choice |
|---|---|
| LLM provider | Claude, fast/cheap tier — native tool-calling |
| Orchestration | Hand-rolled bounded loop on the provider SDK; `pydantic` for the typed entities |
| Database | DuckDB, read-only connection |
| SQL guardrail | `sqlglot` statement-type validation |
| Memory / session state | `st.session_state`, a plain object with three fields |
| Evaluation | Hand-rolled `eval.py` harness + `pandas`-based comparator |
| Charting | Streamlit native (`st.metric` / `st.line_chart` / `st.bar_chart` / `st.dataframe`) |
| Logging | Flat JSONL, stdlib `json` |
| Deployment | Streamlit Community Cloud |

## How this stack demonstrates the four required capabilities

**Tool calling** — the provider's native function-calling, exercised by exactly one real tool (`run_sql`), typed and validated through `pydantic`. **Orchestration** — a hand-rolled loop that is a direct, inspectable implementation of `ARCHITECTURE.md`'s diagram, deliberately not hidden inside a framework. **Memory** — present but honestly minimal: `st.session_state` holding a three-field object, not a vector store or conversation buffer, because v1 genuinely doesn't need more than that. **Evaluation** — the one layer built to the highest standard: a bespoke harness with metrics no off-the-shelf tool provides, because it's the project's actual point.
