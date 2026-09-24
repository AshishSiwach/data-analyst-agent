"""S21 acceptance tests for agent.chart_select."""

from __future__ import annotations

from unittest.mock import patch

from data_analyst_agent.agent.chart_select import build_chart_spec, render, select_chart_type
from data_analyst_agent.models.entities import ChartSpec, ColumnSpec, ResultData


def _result(columns: list[tuple[str, str]], rows: list[list]) -> ResultData:
    return ResultData(columns=[ColumnSpec(name=n, type=t) for n, t in columns], rows=rows)


# --- select_chart_type: one fixture per rule, each unambiguous ---


def test_one_by_one_result_is_scalar():
    result = _result([("order_count", "BIGINT")], [[52612]])
    assert select_chart_type(result) == "scalar"


def test_date_column_with_a_metric_is_line():
    result = _result(
        [("date", "DATE"), ("net_revenue", "DOUBLE")],
        [["2011-01-01", 100.0], ["2011-01-02", 200.0]],
    )
    assert select_chart_type(result) == "line"


def test_one_categorical_plus_one_metric_is_bar():
    result = _result(
        [("country", "VARCHAR"), ("revenue", "DOUBLE")],
        [["United Kingdom", 100.0], ["Germany", 50.0]],
    )
    assert select_chart_type(result) == "bar"


def test_three_or_more_columns_is_table():
    result = _result(
        [("product_id", "VARCHAR"), ("description", "VARCHAR"), ("revenue", "DOUBLE")],
        [["85123A", "WHITE HANGING HEART T-LIGHT HOLDER", 263109.67]],
    )
    assert select_chart_type(result) == "table"


# --- edge cases, each still unambiguous between exactly one rule ---


def test_empty_result_is_table():
    result = _result([("country", "VARCHAR")], [])
    assert select_chart_type(result) == "table"


def test_single_categorical_column_with_no_metric_is_table():
    result = _result([("country", "VARCHAR")], [["UK"], ["Germany"]])
    assert select_chart_type(result) == "table"


def test_single_date_column_alone_with_no_metric_is_table():
    # A lone date column has nothing to plot against it - not "line".
    result = _result([("order_date", "DATE")], [["2011-01-01"], ["2011-01-02"]])
    assert select_chart_type(result) == "table"


def test_two_numeric_columns_is_table_not_bar():
    result = _result([("units", "BIGINT"), ("revenue", "DOUBLE")], [[10, 100.0]])
    assert select_chart_type(result) == "table"


def test_single_row_categorical_and_metric_is_bar_not_scalar():
    # 1 row but 2 columns is not 1x1, so it's bar, not scalar.
    result = _result([("country", "VARCHAR"), ("revenue", "DOUBLE")], [["United Kingdom", 100.0]])
    assert select_chart_type(result) == "bar"


def test_date_plus_multiple_metrics_is_still_line():
    result = _result(
        [("date", "DATE"), ("revenue", "DOUBLE"), ("order_count", "BIGINT")],
        [["2011-01-01", 100.0, 5]],
    )
    assert select_chart_type(result) == "line"


# --- build_chart_spec(): full ChartSpec assembly, added for S24 ---


def test_build_chart_spec_scalar_sets_y_axis_to_the_column_name():
    result = _result([("order_count", "BIGINT")], [[52612]])
    spec = build_chart_spec(result)
    assert spec.chart_type == "scalar"
    assert spec.data == [[52612]]
    assert spec.x_axis is None
    assert spec.y_axis == "order_count"


def test_build_chart_spec_line_sets_x_and_y_for_a_date_plus_one_metric():
    result = _result(
        [("date", "DATE"), ("net_revenue", "DOUBLE")],
        [["2011-01-01", 100.0], ["2011-01-02", 200.0]],
    )
    spec = build_chart_spec(result)
    assert spec.chart_type == "line"
    assert spec.x_axis == "date"
    assert spec.y_axis == "net_revenue"
    assert spec.data == result.rows


def test_build_chart_spec_line_leaves_y_axis_unset_with_multiple_metrics():
    result = _result(
        [("date", "DATE"), ("revenue", "DOUBLE"), ("order_count", "BIGINT")],
        [["2011-01-01", 100.0, 5]],
    )
    spec = build_chart_spec(result)
    assert spec.chart_type == "line"
    assert spec.x_axis == "date"
    assert spec.y_axis is None


def test_build_chart_spec_bar_sets_x_to_categorical_and_y_to_metric():
    result = _result(
        [("country", "VARCHAR"), ("revenue", "DOUBLE")],
        [["United Kingdom", 100.0], ["Germany", 50.0]],
    )
    spec = build_chart_spec(result)
    assert spec.chart_type == "bar"
    assert spec.x_axis == "country"
    assert spec.y_axis == "revenue"


def test_build_chart_spec_table_leaves_axes_unset():
    result = _result(
        [("product_id", "VARCHAR"), ("description", "VARCHAR"), ("revenue", "DOUBLE")],
        [["85123A", "WHITE HANGING HEART T-LIGHT HOLDER", 263109.67]],
    )
    spec = build_chart_spec(result)
    assert spec.chart_type == "table"
    assert spec.x_axis is None
    assert spec.y_axis is None


# --- render(): mocked Streamlit, verifying the right widget is called ---


@patch("data_analyst_agent.agent.chart_select.st")
def test_render_scalar_calls_st_metric(mock_st):
    spec = ChartSpec(chart_type="scalar", data=[[52612]], y_axis="Order Count")
    render(spec)
    mock_st.metric.assert_called_once()
    assert mock_st.metric.call_args.kwargs["value"] == 52612


@patch("data_analyst_agent.agent.chart_select.st")
def test_render_line_calls_st_line_chart(mock_st):
    spec = ChartSpec(
        chart_type="line", data=[["2011-01-01", 100.0]], x_axis="date", y_axis="revenue"
    )
    columns = [ColumnSpec(name="date", type="DATE"), ColumnSpec(name="revenue", type="DOUBLE")]
    render(spec, columns=columns)
    mock_st.line_chart.assert_called_once()


@patch("data_analyst_agent.agent.chart_select.st")
def test_render_bar_calls_st_bar_chart(mock_st):
    spec = ChartSpec(chart_type="bar", data=[["UK", 100.0]], x_axis="country", y_axis="revenue")
    columns = [
        ColumnSpec(name="country", type="VARCHAR"),
        ColumnSpec(name="revenue", type="DOUBLE"),
    ]
    render(spec, columns=columns)
    mock_st.bar_chart.assert_called_once()


@patch("data_analyst_agent.agent.chart_select.st")
def test_render_table_calls_st_dataframe(mock_st):
    spec = ChartSpec(chart_type="table", data=[["A", "desc", 1.0]])
    columns = [
        ColumnSpec(name="product_id", type="VARCHAR"),
        ColumnSpec(name="description", type="VARCHAR"),
        ColumnSpec(name="revenue", type="DOUBLE"),
    ]
    render(spec, columns=columns)
    mock_st.dataframe.assert_called_once()
