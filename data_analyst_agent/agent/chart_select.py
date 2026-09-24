"""Deterministic chart-type selection and rendering per `_docs/Tools.md`
Bucket 3: "a pure rule on result shape... no judgment content remains at
this point." No LLM call anywhere in this module, per `_docs/CLAUDE.md`'s
structural rule - `select_chart_type` is a pure function of a
`ResultData`'s shape (row count, column count, column types), nothing else.
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from data_analyst_agent.models.entities import ChartSpec, ChartType, ColumnSpec, ResultData

_DATE_MARKERS = ("DATE", "TIMESTAMP")
_NUMERIC_MARKERS = ("INT", "DOUBLE", "DECIMAL", "FLOAT", "REAL", "NUMERIC")


def _is_date_type(type_str: str) -> bool:
    upper = type_str.upper()
    return any(marker in upper for marker in _DATE_MARKERS)


def _is_numeric_type(type_str: str) -> bool:
    upper = type_str.upper()
    return any(marker in upper for marker in _NUMERIC_MARKERS)


def _column_kind(column: ColumnSpec) -> str:
    if _is_numeric_type(column.type):
        return "metric"
    return "categorical"


def select_chart_type(result: ResultData) -> ChartType:
    """1x1 -> scalar; a date column present (alongside at least one other
    column, so there's something to plot against it) -> line; exactly one
    categorical column + one metric column -> bar; anything wider ->
    table."""
    n_rows = len(result.rows)
    n_cols = len(result.columns)

    if n_rows == 1 and n_cols == 1:
        return "scalar"

    if n_cols >= 2 and any(_is_date_type(c.type) for c in result.columns):
        return "line"

    if n_cols == 2:
        kinds = sorted(_column_kind(c) for c in result.columns)
        if kinds == ["categorical", "metric"]:
            return "bar"

    return "table"


def build_chart_spec(result: ResultData) -> ChartSpec:
    """Assembles a fully-populated `ChartSpec` (data + x_axis/y_axis) from
    a `ResultData` - added for S24, beyond S21's literal acceptance
    criteria (`select_chart_type(result) -> ChartType` only). `Answer.chart_spec`
    (S01) needs a complete `ChartSpec` to render, not just the bare type,
    and this is the natural place for that assembly: it's still a pure,
    deterministic function of result shape, reusing the same
    `select_chart_type`/`_column_kind`/`_is_date_type` logic rather than
    duplicating it in the orchestrator.

    Axis convention (a design decision beyond anything a doc specifies):
    `scalar` sets `y_axis` to the single column's name (`render`'s own
    label fallback); `line` sets `x_axis` to the first date column and
    `y_axis` to the other column's name when there are exactly two columns
    total (the common date+metric case), else leaves `y_axis` unset when
    there's more than one candidate metric column - ambiguous, not
    guessed; `bar` sets `x_axis`/`y_axis` to the categorical/metric column
    names respectively, per `select_chart_type`'s own bar rule; `table`
    sets neither, since `render` never reads them for a table.
    """
    chart_type = select_chart_type(result)
    x_axis: str | None = None
    y_axis: str | None = None

    if chart_type == "scalar":
        y_axis = result.columns[0].name
    elif chart_type == "line":
        date_column = next(c for c in result.columns if _is_date_type(c.type))
        x_axis = date_column.name
        others = [c for c in result.columns if c.name != x_axis]
        if len(others) == 1:
            y_axis = others[0].name
    elif chart_type == "bar":
        by_kind = {_column_kind(c): c.name for c in result.columns}
        x_axis = by_kind["categorical"]
        y_axis = by_kind["metric"]

    return ChartSpec(chart_type=chart_type, data=result.rows, x_axis=x_axis, y_axis=y_axis)


def _to_dataframe(chart_spec: ChartSpec, columns: list[ColumnSpec] | None) -> pd.DataFrame:
    column_names = [c.name for c in columns] if columns else None
    return pd.DataFrame(chart_spec.data, columns=column_names)


def render(chart_spec: ChartSpec, columns: list[ColumnSpec] | None = None) -> None:
    """Renders `chart_spec` via Streamlit's native widgets, per
    `_docs/technology_stack.md` §7. `columns` (from the same `ResultData`
    `chart_spec.data` was built from) supplies real column names for
    axis labeling - `ChartSpec.data` itself is just raw row arrays.
    """
    if chart_spec.chart_type == "scalar":
        value = chart_spec.data[0][0] if chart_spec.data and chart_spec.data[0] else None
        label = chart_spec.y_axis or (columns[0].name if columns else "Value")
        st.metric(label=label, value=value)
        return

    df = _to_dataframe(chart_spec, columns)

    if chart_spec.chart_type == "line":
        x = chart_spec.x_axis if chart_spec.x_axis in df.columns else df.columns[0]
        df = df.set_index(x)
        st.line_chart(df)
    elif chart_spec.chart_type == "bar":
        x = chart_spec.x_axis if chart_spec.x_axis in df.columns else df.columns[0]
        df = df.set_index(x)
        st.bar_chart(df)
    else:  # table
        st.dataframe(df)
