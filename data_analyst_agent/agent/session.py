"""Session-scoped deterministic mechanics from `_docs/autonomy.md` and
`_docs/Tools.md`: the cost-cap kill switch and the fast-fail cache.

`SessionState` itself is defined once, in `models/entities.py` (S01), as
one of InformationModel.md's typed entities - re-exported here rather than
redefined, since CLAUDE.md's "all data crossing a module boundary is a
pydantic model from models/entities.py" rule means there is exactly one
SessionState class, not a second copy local to this module.
"""

from __future__ import annotations

from data_analyst_agent.models.entities import SessionState

__all__ = ["SessionState", "check_cost_cap", "normalize", "check_fast_fail"]

_FAST_FAIL_THRESHOLD = 3


def check_cost_cap(session: SessionState) -> bool:
    """True (stop) once the session's spend has reached its cap. This is
    the system kill switch from autonomy.md, not an agent decision - every
    LLM-invoking call site checks this, never once per turn."""
    return session.cost_spent_usd >= session.cost_cap_usd


def normalize(question: str) -> str:
    """Lowercase, whitespace-collapsed form used as the fast-fail cache
    key - an exact-ish match, not a semantic one, per InformationModel.md's
    explicit v1 scoping note."""
    return " ".join(question.lower().split())


def check_fast_fail(session: SessionState, question: str) -> bool:
    """True when this exact (normalized) question has already exhausted
    the 3-attempt retry budget earlier in this session."""
    attempts = session.failed_questions_cache.get(normalize(question), 0)
    return attempts >= _FAST_FAIL_THRESHOLD
