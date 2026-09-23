# Agent Autonomy — v1

Companion to `scope.md`. Where the scope doc says *what* the agent does, this doc says *how much room it gets to decide on its own* while doing it.

## Why this matters here

Because v1 is read-only, single-turn, stateless, and running against a public sample dataset (per the scope doc), the ceiling on how much autonomy is safe to grant is much higher than it would be for an agent with write access to a real store's live data. But "safe in principle" isn't a license to skip the analysis — it's the reason most of the actions below can sit at a high tier without extra machinery. Every action is rated on three questions: what happens if the agent is wrong, whether the action can be undone, and whether a human would notice the mistake before it caused harm.

## The four-tier scale

- **L0 — Forbidden.** The agent cannot take this action, period. Enforced structurally in code, never by prompt instruction alone — a prompt-only prohibition is not a safety control.
- **L1 — Propose, don't commit.** The agent produces a candidate output or decision but presents it as provisional, with an explicit, cheap way for a human to override it.
- **L2 — Autonomous within hard bounds, fully logged.** The agent decides and acts without asking permission first, but only inside a pre-defined, code-enforced boundary (a whitelist, a cap, a timeout), and the action is always logged for after-the-fact review.
- **L3 — Fully autonomous.** The agent decides and acts freely. No special bound, no special logging — because there's no failure mode worth guarding against.

The tiers are a ladder of "what backs up the agent if it's wrong," not a ladder of trust. L3 needs no backup because nothing bad can happen regardless of the decision. L2 needs a hard boundary because the failure mode exists but code contains it, not judgment. L1 needs a human or a downstream check because code alone can't contain it. L0 means the failure mode is bad enough that "contained" isn't good enough — the door is welded shut.

## Action-by-action

| Action | Tier | Why |
|---|---|---|
| `list_metrics` — read the metric dictionary | **L3** | Pure read of static, versioned config. No user data touched, no side effects possible. |
| `run_sql` — execute a query | **L2** | This is the core value of the product — the agent must decide and run queries with zero friction or the UX doesn't work. Safe at L2 because it's backed by code, not judgment: read-only connection, SELECT/WITH-only parser, 5s timeout, 10k row cap. Every query and result is logged. Worst case if the agent writes a bad query: a wrong number gets computed, never data damage. |
| `plot_chart` — choose and render a chart | **L2** | The agent picks chart type and shape autonomously, but the choice space is a small closed enum (bar / line / table / scalar) — it structurally cannot render something malformed. Bounding the *menu*, not the decision, is what keeps this safe without adding friction. |
| Retry-on-error loop | **L2** | The agent decides on its own to retry and how to adjust the query. A human check between retries would kill the UX for no safety benefit — a bad retry just produces another loggable, boundable SQL error. Capped hard at 3 attempts; every attempt logged (the "attempts-until-success" eval metric from the scope doc). |
| Ambiguity resolution ("assume, state, invite override") | **L1 — executed** | The agent does act — it computes and returns an answer rather than stalling on a clarifying question — but it must name its assumption in the response itself, not bury it. Deliberately not plain L2: the point is that the human sees the seam and can override it in one sentence. |
| Failure diagnosis + rephrase suggestion | **L2 + content guardrail** | Autonomous to generate — producing words carries no risk by itself — but logged to `failures.jsonl`, and bound by one content rule that isn't about tier: it must never assert a cause it hasn't actually checked. A wrong diagnosis is a credibility cost, not a data-safety one, but the kind that compounds if unmonitored — hence the log. |
| Natural-language "wrapping" of the numeric answer | **L2 — the tightest one** | The highest-risk L2 action in the system. A wrong SQL result is self-evident once you look at the number; a wrong *sentence* about the number is not — it's exactly the kind of error a founder acts on without checking. Bound by one hard rule: never state a number, trend, or comparison that isn't directly present in the executed result. This is also the one layer v1's evaluation doesn't grade automatically (Layer 5 in the scope doc) — the safety net here is thinner than everywhere else, which is a reason to keep the bound tight rather than relax it later. |
| Session cost-cap enforcement | **Not agent autonomy — a system kill switch** | Not the agent choosing anything; a circuit breaker that overrides it regardless of what it "wants" to do next. Listed here only so it isn't mistaken for an L1/L2 decision: the agent has zero discretion once the cap is hit. |
| Any write action (INSERT / UPDATE / DELETE / DDL) | **L0** | Forbidden absolutely, enforced twice over: the DB connection is opened read-only, and a parser step rejects anything but SELECT/WITH before execution is even attempted. Two independent structural blocks, not one — a single point of enforcement that depends on the LLM behaving is not enforcement. |

## Escalation — what pulls an action down a tier dynamically

Autonomy isn't static within a session; a few conditions should demote an action automatically, without the agent "deciding" to be more careful:

- **Repeated identical failures across a session** (the same question failing all 3 retries more than once) — demotes retry from L2 to effectively L1: stop retrying blind, surface the failure immediately instead of spending the full budget again.
- **Cost cap approached** (e.g. 80% of the per-session budget spent) — every subsequent action gets a warning surfaced to the user before continuing, even though nothing yet strictly requires it. Cheap insurance against a bug burning the rest of the budget silently.
- **A query the parser rejects more than once in the same retry loop** — suggests the LLM is trying to do something outside SELECT/WITH, worth logging distinctly from an ordinary SQL syntax error since it's a different failure signature (attempted scope violation vs. honest mistake).

## What this doc deliberately doesn't cover

Build-time and deployment actions (writing code, deploying to Streamlit, committing to git) aren't "agent actions" in this sense — they're the builder's actions, not the running agent's. This doc is scoped to what the *deployed* agent decides and does at runtime, matching the scope doc's Agent design section.

## Phase 2 flag

Diagnostic "why" reasoning (the multi-step hypothesis loop deferred to Phase 2) does not inherit these tiers by default. A wrong single number is a visible, cheap-to-catch mistake; a wrong *causal* claim ("your sales dropped because of X") is not — it's more persuasive, more actionable, and harder for a founder to sanity-check. When Phase 2 is scoped, diagnostic conclusions should probably start at **L1** even though the descriptive queries underneath them stay at L2/L3 — propose hypotheses and show the evidence for each, don't assert a single cause as fact. That's a decision for the Phase 2 scoping conversation, not this one, but it shouldn't be inherited silently from v1's looser bar.
