# Tool Catalogue — v1 (MVP)

Companion to `scope.md`, `AUTONOMY.md`, and `ARCHITECTURE.md`. `ARCHITECTURE.md` already narrowed the original three-tool surface from `scope.md` down to a single genuine agent-invoked tool — this doc makes that narrowing concrete and complete, and sorts everything else in the system into its correct bucket.

## Three buckets, not two

The request behind this doc has an implicit binary: "tool" or "deterministic code." Real systems need a third bucket, and forcing everything into one of the other two would misdescribe how the MVP actually works:

1. **Agent-invoked tools** — the model decides to call these, against a typed contract, and the call can touch state outside the model's own context. **One qualifies in v1.**
2. **Orchestrator-invoked LLM completions** — the LLM is used, but the *decision to call it* belongs to the pipeline, not the model. The model never "chooses" to generate a narrative wrap or a failure diagnosis; code calls it at a fixed point every time. Not a tool (no I/O contract the model addresses) and not "ordinary deterministic code" either (an LLM sits inside it and can still misbehave). **Two qualify.**
3. **Ordinary deterministic code** — no LLM involved at any point. **Eight qualify.**

| Action | Bucket | One-line reason |
|---|---|---|
| `run_sql` | Tool (L2) | Genuine dynamic decision: query content, and whether/how to retry |
| Failure diagnosis | Orchestrated completion | Needs LLM judgment, but the pipeline decides when to call it, not the model |
| Narrative wrap | Orchestrated completion | Same — LLM needed, but pipeline-triggered on every successful execution |
| Metric-dictionary lookup | Deterministic code | Static and cheap enough to always inject; nothing to decide |
| SQL parser / statement validator | Deterministic code | Must be unconditional — a check the model can skip isn't a guardrail |
| Row-cap truncation | Deterministic code | Pure data-shape rule, no judgment involved |
| Chart-type selection | Deterministic code | Solved from result shape alone; also the least-evaluable decision if left to the model |
| Chart rendering | Deterministic code | No judgment left once type and data are fixed |
| Session cost-cap check | Deterministic code | System kill switch — must never be something the model can decide to skip |
| Session fast-fail check | Deterministic code | Overrides default retry behaviour; can't be optional |
| Retry / attempt-count gate | Deterministic code | Must be a hard counter, not a model-negotiated judgment call |

## Bucket 1 — Agent-invoked tools

### `run_sql`

**Business purpose.** Answer the founder's question by executing one read query against the store's data and returning the raw result the rest of the pipeline turns into a chart and a sentence.

**When the model should use it.** Exactly once per generation attempt, immediately after producing a candidate SQL query for the current question — including a corrected query after a prior attempt's error was fed back. Every call is a candidate *final* answer to the question in front of it; there is no exploratory or schema-sampling use case in v1.

**When it must not use it.**
- To attempt anything other than a single `SELECT` or `WITH` statement.
- More than 3 times for one user turn — enforced by the orchestrator's counter, not the model's judgment, but the model should not treat the tool as unlimited.
- With any parameter or table reference outside the one connected database and its known schema.
- After the session's fast-fail check has already determined this exact question failed 3/3 earlier in the session — the orchestrator won't offer the tool in that branch at all.

**Typed input schema.**
```json
{
  "query": "string, required — a single SQL statement, SELECT or WITH only",
  "attempt_number": "integer, required, 1-3 — which retry attempt this is, for logging"
}
```

**Typed output schema.**
```json
{
  "status": "enum: success | error | timeout | rejected",
  "columns": "array of {name: string, type: string} — present only if status == success",
  "rows": "array of row arrays — present only if status == success, capped at 10,000",
  "row_count": "integer — full count before truncation, present only if status == success",
  "truncated": "boolean — true if row_count > 10,000",
  "error_message": "string — present only if status in {error, timeout, rejected}",
  "execution_ms": "integer — wall-clock time of this attempt"
}
```

**Authentication and authorization.** No end-user auth in v1 — one shared demo database, no per-founder data isolation (there's exactly one dataset). The tool authenticates with a single service-level, read-only credential provisioned at deploy time, not per-session or per-user. Authorization is enforced at the connection level: the DB user behind this credential has SELECT-only grants, so even a query that slipped past the app-level parser would be rejected a second, independent time by the database itself.

**Read-only or state-changing.** Read-only, with no exception permitted. This is the entire reason the tool can sit at autonomy tier L2 rather than requiring approval.

**Idempotency requirements.** Must be idempotent by construction — the same query against an unchanged database returns the same result. This falls out of being read-only; there is no idempotency *mechanism* to build (no dedup keys, no idempotency tokens) because there's no mutation to de-duplicate. Worth stating explicitly anyway: idempotency is what makes unlimited-looking retries safe.

**Timeout.** 5 seconds per attempt, killed server-side (not just abandoned client-side). Returns `status: "timeout"`.

**Retry policy.** Not the tool's own responsibility. The tool is stateless and retry-agnostic — it executes what it's given and reports the outcome. Retries are orchestrator-level: on any non-success status, the error is fed back to the model and the counter increments, capped hard at 3 attempts per user turn.

**Possible errors.**
- `rejected` — the parser blocked a non-SELECT/WITH statement before it reached the database. Logged distinctly from an ordinary SQL error as a scope-violation attempt.
- `error` — the database rejected the query (unknown column, type mismatch, division by zero, or syntax the app-level parser allowed but the DB's own parser didn't).
- `timeout` — execution exceeded 5 seconds.
- There is no error variant on the success path — row-cap overflow is a `success` response with `truncated: true`, not a failure.

**Audit fields.** Session id, turn id, attempt number, exact query text, status, execution_ms, row_count (if applicable), and the full error message on any non-success status. This is the raw material both `failures.jsonl` and the eval harness's attempts-until-success metric are built from; the tool's log schema and the harness's expected input are the same shape on purpose, so nothing needs reshaping between them.

**Approval requirement.** None. Per `AUTONOMY.md`, `run_sql` sits at L2 (autonomous within hard bounds, fully logged) specifically because read-only + parser whitelist + timeout + row cap remove the need for a human gate. A per-query approval step would defeat the product's core value — zero-friction self-serve answers — for no safety gain the code-level bounds don't already provide.

**How success is independently verified.** Two separate layers, matching `scope.md`'s evaluation design. At build time, the result-based comparator runs the tool against the 60-question gold set and checks the returned result against the pre-computed gold result — order-invariant, float-tolerant, deterministic tie-breaking — which is what actually validates the tool is doing its job. At runtime, there is no per-call correctness check beyond the deterministic guards already listed: the tool can confirm a query *executed*, never that it's *semantically* right. That gap is exactly why the eval harness, not the tool, is the real verification mechanism — worth stating plainly rather than implying the tool self-verifies.

## Bucket 2 — Orchestrator-invoked LLM completions (not tools)

Neither of these has a model-facing I/O contract, because the model never decides to call them — the pipeline invokes them at a fixed point every time. Several of the 14 fields above don't apply for that reason, and are marked N/A rather than forced.

### Failure diagnosis

**Purpose.** Classify why the 3-attempt budget was exhausted (schema mismatch / ambiguity / genuine bug) and produce the founder-facing diagnosis text and a concrete rephrase suggestion — the five-part failure response specified in `scope.md`.

**Triggered by.** The orchestrator, unconditionally, the moment the attempt counter is exhausted or the session fast-fail check fires. Never a model decision.

**Input.** The full attempt trace (every query tried, every error received), the original question, the schema summary.

**Output schema.**
```json
{
  "category": "enum: schema_mismatch | ambiguity | bug",
  "explanation": "string — the founder-facing diagnosis sentence",
  "rephrase_suggestion": "string — a concrete alternative question"
}
```

**Content guardrail (from `AUTONOMY.md`).** Must never assert a cause it hasn't actually checked against the trace — a wrong diagnosis is a credibility cost that compounds if unmonitored.

**Timeout.** 10 seconds — generous relative to `run_sql`'s 5s since there's no retry budget riding on it, but still bounded for UX latency.

**Auth / idempotency / approval.** N/A — no external system is called, nothing is mutated, and the model has no discretion over whether this runs.

**Audit fields.** Logged in full to `failures.jsonl` alongside the attempt trace it was generated from.

**How success is independently verified.** The "graceful failure rate" metric in `scope.md`'s adversarial gold bucket — a held-out set of questions designed to fail, graded on whether the diagnosis lands in one of the three well-shaped categories rather than on whether an answer was produced.

### Narrative wrap

**Purpose.** Turn a successful execution's result into a short natural-language answer, and disclose any default assumption made (the "assume, state, invite override" pattern from `scope.md` — e.g. ranking by revenue when the question didn't specify a metric).

**Triggered by.** The orchestrator, unconditionally, immediately after chart rendering succeeds. Never a model decision.

**Input.** The original question, the executed SQL and its result (or the truncation note), the chart spec chosen.

**Output schema.**
```json
{
  "answer_text": "string — the founder-facing sentence(s)",
  "assumption_disclosed": "string | null — the assumption made, if any, e.g. 'ranked by revenue'"
}
```

**Content guardrail (from `AUTONOMY.md`).** The tightest one in the system: must never state a number, trend, or comparison absent from the executed result. This is also the one layer `scope.md`'s evaluation doesn't grade automatically (Layer 5) — the safety net here is thinner than everywhere else, which is why the rule is a hard constraint rather than a guideline.

**Timeout.** 5 seconds.

**Auth / idempotency / approval.** N/A — same reasoning as failure diagnosis.

**Audit fields.** Logged alongside the turn's full trace (question, SQL, result summary, answer text, assumption disclosed if any).

**How success is independently verified.** Spot-checked by hand on a slice of the gold set (`scope.md`'s Layer 4/5 rubric) rather than automatically graded — an explicit, documented limitation, not an oversight.

## Bucket 3 — Flagged as ordinary deterministic code

Each of these was a candidate for tool- or LLM-completion-hood at some point in `scope.md` or `ARCHITECTURE.md`. All eight are deliberately kept as plain code, with the specific reason making a model (or an LLM completion) unsuitable for the role.

**Metric-dictionary lookup** (was `list_metrics` in `scope.md`). The full dictionary is nine short entries — cheap enough to always inject into context at turn start rather than exposed as something the model has to remember to call. Making it a tool call adds a decision point (does the model remember to look it up when a business term appears?) for content that costs almost nothing to just always have present.

**SQL parser / statement-type validator.** Runs unconditionally on every candidate query before execution is attempted, inside `run_sql`'s own implementation — never a separate call the model makes. A guardrail the model could choose not to invoke, or invoke and ignore, isn't a guardrail. It has to sit in the query's path, not behind a request.

**Row-cap truncation.** A pure rule on row count (`> 10,000` → slice + note). Giving the model a "should I truncate?" decision would introduce inconsistency, and a route to accidentally showing more or fewer rows than the cap intends, for no benefit.

**Chart-type selection.** A deterministic rule on result shape (1×1 → scalar; time-indexed → line; categorical breakdown → bar; otherwise → table). `ARCHITECTURE.md` deliberately pulled this out of model discretion: it's solved from shape alone, and removing it also removes the least-evaluable, most subjective decision that existed in `scope.md`'s original tool surface.

**Chart rendering.** The actual draw call, fired automatically once type and data are fixed. No judgment content remains at this point; exposing it as a tool would add a round-trip with nothing for the model to decide.

**Session cost-cap check.** Runs after every LLM-invoking step (each `run_sql` attempt, the diagnosis completion, the wrap completion). Per `AUTONOMY.md`, this is explicitly "not agent autonomy — a system kill switch," and a kill switch that's a tool the model can choose not to call is not a kill switch.

**Session fast-fail check.** A deterministic lookup, at turn start, against session-local state: has this exact question already failed 3/3 earlier this session? This exists specifically to override the model's default instinct to retry, so it can't be conditional on the model's willingness to check it.

**Retry / attempt-count gate.** The hard counter around calls to `run_sql`, capped at 3. If "should I retry" were the model's judgment call rather than a counted, code-enforced limit, the cap from `scope.md` would become advisory rather than enforced — precisely the failure mode `AUTONOMY.md`'s L2 tier exists to prevent.
