"""Result-set comparator per `_docs/scope.md`'s evaluation design: the
pass/fail engine behind the whole eval strategy. Grades on executed result,
never on SQL string.

Order invariance is achieved by sorting both row sets on a canonical,
type-safe key before comparing pairwise (not by inferring which column is
"the" ranking metric) — `scope.md`'s own tie-break convention (`ORDER BY
<metric> DESC, product_id ASC`) is a prompt/gold-authoring rule for
producing *a* deterministic order, not something this module needs to
parse back out of two already-materialized result sets.

Bug found and fixed post-S25, during S26: a DATE-typed cell compared
unequal to its own correct value across every single date-column gold
question (`basic_011`/`basic_017`/`basic_018` - confirmed via harness
report data: the agent's SQL was literally byte-identical to gold's for
017/018, yet `compare()` still returned False). Cause: a live agent
result carries real `datetime.date`/`datetime` objects (straight from
DuckDB), while a gold row loaded from `gold.jsonl` carries a JSON-
deserialized ISO string (`GoldQuestion.gold_result.rows` is typed
`list[list[Any]]`, so pydantic never coerces it back into a `date`) -
`date(2009, 12, 1) == "2009-12-01"` is `False` in plain Python, no matter
how correct the underlying value is. Fixed in `_values_equal` by
normalizing both sides to a `date` when either one already is a
`date`/`datetime`, before falling back to plain equality.
"""

from __future__ import annotations

import math
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from data_analyst_agent.models.entities import ResultData

RELATIVE_TOLERANCE = 0.001  # 0.1% per scope.md
_ABSOLUTE_TOLERANCE = 1e-6  # floor against float noise when comparing near-zero values


def compare(agent_result: ResultData, gold_result: ResultData) -> bool:
    """True when `agent_result` and `gold_result` represent the same result
    set: order-invariant, column-name insensitive (only positional values
    are compared), float-tolerant (relative diff < 0.1%). Empty-vs-empty is
    a match. False if row counts, column counts, or any value differ."""
    if len(agent_result.rows) != len(gold_result.rows):
        return False
    if len(agent_result.columns) != len(gold_result.columns):
        return False
    if not agent_result.rows:  # both empty, since counts already matched above
        return True

    agent_rows = sorted(agent_result.rows, key=_row_sort_key)
    gold_rows = sorted(gold_result.rows, key=_row_sort_key)

    return all(_rows_equal(a, g) for a, g in zip(agent_rows, gold_rows))


def _rows_equal(agent_row: list[Any], gold_row: list[Any]) -> bool:
    if len(agent_row) != len(gold_row):
        return False
    return all(_values_equal(a, g) for a, g in zip(agent_row, gold_row))


def _values_equal(agent_value: Any, gold_value: Any) -> bool:
    if agent_value is None or gold_value is None:
        return agent_value is None and gold_value is None
    if _is_numeric(agent_value) and _is_numeric(gold_value):
        return math.isclose(
            float(agent_value),
            float(gold_value),
            rel_tol=RELATIVE_TOLERANCE,
            abs_tol=_ABSOLUTE_TOLERANCE,
        )
    if isinstance(agent_value, (date, datetime)) or isinstance(gold_value, (date, datetime)):
        a_date, g_date = _as_date(agent_value), _as_date(gold_value)
        if a_date is not None and g_date is not None:
            return a_date == g_date
    return agent_value == gold_value


def _as_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


def _row_sort_key(row: list[Any]) -> tuple:
    return tuple(_value_sort_token(v) for v in row)


def _value_sort_token(value: Any) -> tuple:
    """(type_rank, normalized_value) so a row containing None or mixed
    types still sorts deterministically instead of raising TypeError on a
    cross-type comparison. Values only ever compare within the same rank.
    bool shares numeric's rank (not its own) so an agent value of True and
    a gold value of 1 land at the same sort position either way."""
    if value is None:
        return (0, "")
    if _is_numeric(value):
        return (1, float(value))
    return (2, str(value))


def _is_numeric(value: Any) -> bool:
    return isinstance(value, (int, float, Decimal, bool))
