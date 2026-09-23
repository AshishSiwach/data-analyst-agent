"""The GPT-4o-mini completion that classifies an exhausted-retries trace,
per `_docs/Tools.md`'s Failure diagnosis spec. Triggered by the
orchestrator (S24) unconditionally once the 3-attempt budget is
exhausted or the fast-fail cache short-circuits a turn - never a model
decision to call this.

`FailureDiagnosis` (S01) already matches Tools.md's output schema
exactly (category, explanation, rephrase_suggestion) - unlike S15/S20/
S22, there's no SqlAttempt-vs-actual-data tension to resolve here: a
diagnosis is built entirely from the failed attempt trace (queries,
statuses, error messages), which is exactly what SqlAttempt carries.
There are no result rows to reference, because every attempt failed.
"""

from __future__ import annotations

from pathlib import Path

from openai import OpenAI

from data_analyst_agent.agent.generate_sql import build_schema_summary
from data_analyst_agent.models.entities import FailureDiagnosis, SqlAttempt

MODEL = "gpt-4o-mini"

SYSTEM_PROMPT_TEMPLATE = """\
You are diagnosing why an attempt to answer a UK-based solo founder's \
question failed after 3 attempts, or was skipped because this exact \
question already failed 3 times earlier in the session. You are given the \
question, the semantic layer's schema, and the full trace of attempts \
(each query tried and the error it hit, if any attempts were actually made).

## Schema (the only tables the agent could query)

{schema_summary}

Classify the failure into exactly one category:
- schema_mismatch: the question asks about data that genuinely isn't \
represented anywhere in the schema above (e.g. email opens, physical \
retail stores, marketing campaigns, social media activity).
- ambiguity: the question's phrasing allows multiple, materially different \
interpretations, and (if attempts were made) they show different \
interpretations being tried without any clearly succeeding.
- bug: the question is answerable from the schema and isn't ambiguous, but \
attempts still failed for what looks like a technical/implementation \
reason (e.g. a column name that IS in the schema was still gotten wrong \
repeatedly, or a persistent syntax error).

Only assert a cause the trace actually supports - do not guess beyond what \
the schema and the attempts show. Write a short, founder-facing \
explanation and one concrete rephrase suggestion the founder could try \
instead.
"""


def _format_trace(attempts: list[SqlAttempt]) -> str:
    if not attempts:
        return "(No attempts were made - this question already failed 3/3 earlier in the session.)"
    lines = []
    for a in attempts:
        lines.append(
            f"Attempt {a.attempt_number} (status={a.status}):\n"
            f"  Query: {a.query_text}\n"
            f"  Error: {a.error_message or '(none)'}"
        )
    return "\n\n".join(lines)


def diagnose(
    question: str,
    attempts: list[SqlAttempt],
    db_path: Path | str | None = None,
    client: OpenAI | None = None,
) -> FailureDiagnosis:
    active_client = client if client is not None else OpenAI()
    system_prompt = SYSTEM_PROMPT_TEMPLATE.format(schema_summary=build_schema_summary(db_path))
    user_content = f"Question: {question}\n\nAttempt trace:\n{_format_trace(attempts)}"

    completion = active_client.chat.completions.parse(
        model=MODEL,
        temperature=0,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        response_format=FailureDiagnosis,
    )
    return completion.choices[0].message.parsed
