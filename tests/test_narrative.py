"""S22 acceptance tests for agent.narrative.wrap (mocked LLM call)."""

from __future__ import annotations

import pytest

from data_analyst_agent.agent.narrative import NarrativeGuardrailViolation, wrap
from data_analyst_agent.models.entities import ChartSpec, ColumnSpec, SqlExecutionResult


def _result(**overrides) -> SqlExecutionResult:
    base = dict(
        status="success",
        columns=[ColumnSpec(name="order_count", type="BIGINT")],
        rows=[[52612]],
        row_count=1,
        truncated=False,
        execution_ms=5,
    )
    base.update(overrides)
    return SqlExecutionResult(**base)


def _chart_spec(**overrides) -> ChartSpec:
    base = dict(chart_type="scalar", data=[[52612]])
    base.update(overrides)
    return ChartSpec(**base)


class _FakeMessage:
    def __init__(self, parsed):
        self.parsed = parsed


class _FakeChoice:
    def __init__(self, parsed):
        self.message = _FakeMessage(parsed)


class _FakeCompletion:
    def __init__(self, parsed):
        self.choices = [_FakeChoice(parsed)]


class _FakeCompletionsAPI:
    def __init__(self, answer_text: str, assumption_disclosed: str | None = None):
        from data_analyst_agent.agent.narrative import _NarrativeCompletion

        self._parsed = _NarrativeCompletion(
            answer_text=answer_text, assumption_disclosed=assumption_disclosed
        )
        self.captured_kwargs = None

    def parse(self, **kwargs):
        self.captured_kwargs = kwargs
        return _FakeCompletion(self._parsed)


class _FakeChatAPI:
    def __init__(self, answer_text: str, assumption_disclosed: str | None = None):
        self.completions = _FakeCompletionsAPI(answer_text, assumption_disclosed)


class _FakeClient:
    def __init__(self, answer_text: str = "You have 52,612 orders.", assumption_disclosed=None):
        self.chat = _FakeChatAPI(answer_text, assumption_disclosed)


def test_wrap_extracts_answer_text_and_assumption_disclosed_correctly():
    fake_client = _FakeClient(
        answer_text="You have 52,612 orders in total.",
        assumption_disclosed="ranked by revenue",
    )
    result = wrap("How many orders?", _result(), _chart_spec(), client=fake_client)
    assert result.answer_text == "You have 52,612 orders in total."
    assert result.assumption_disclosed == "ranked by revenue"


def test_wrap_allows_assumption_disclosed_to_be_null():
    fake_client = _FakeClient(answer_text="You have 52,612 orders.", assumption_disclosed=None)
    result = wrap("How many orders?", _result(), _chart_spec(), client=fake_client)
    assert result.assumption_disclosed is None


def test_wrap_raises_on_a_number_not_present_in_the_result():
    # This is the required test: a mocked response containing a number not
    # present in the fixture result must trigger a failure - proving the
    # guardrail check actually checks, not just exists as a stub.
    fake_client = _FakeClient(answer_text="You have 99,999 orders in total.")
    with pytest.raises(NarrativeGuardrailViolation) as exc_info:
        wrap("How many orders?", _result(), _chart_spec(), client=fake_client)
    assert "99999" in str(exc_info.value) or "99999.0" in str(exc_info.value)


def test_wrap_allows_a_percentage_display_of_a_fraction_value():
    result = _result(
        columns=[ColumnSpec(name="return_rate", type="DOUBLE")],
        rows=[[0.0436]],
    )
    fake_client = _FakeClient(answer_text="Your return rate is 4.36%.")
    wrapped = wrap("What is the return rate?", result, _chart_spec(), client=fake_client)
    assert wrapped.answer_text == "Your return rate is 4.36%."


def test_wrap_allows_rounded_currency_display():
    result = _result(
        columns=[ColumnSpec(name="aov", type="DOUBLE")],
        rows=[[378.3715197673571]],
    )
    fake_client = _FakeClient(answer_text="Your AOV is $378.37.")
    wrapped = wrap("What is my AOV?", result, _chart_spec(), client=fake_client)
    assert wrapped.answer_text == "Your AOV is $378.37."


def test_wrap_allows_referencing_a_number_from_the_question_itself():
    result = _result(
        columns=[ColumnSpec(name="revenue", type="DOUBLE")],
        rows=[[9308947.213999951]],
    )
    fake_client = _FakeClient(answer_text="In 2011, revenue was $9,308,947.21.")
    wrapped = wrap("What was revenue in 2011?", result, _chart_spec(), client=fake_client)
    assert "2011" in wrapped.answer_text


def test_wrap_raises_if_the_underlying_result_did_not_succeed():
    failed_result = SqlExecutionResult(
        status="error", error_message="boom", truncated=False, execution_ms=1
    )
    with pytest.raises(ValueError):
        wrap("How many orders?", failed_result, _chart_spec(), client=_FakeClient())


def test_wrap_allows_a_day_number_drawn_from_a_date_column():
    # Regression test: "December 5th" draws on the day component of a
    # real date value (2011-12-05) in the result - found live while
    # running this slice's required evaluation case, where this was
    # initially a false-positive guardrail violation (the day-of-month
    # number wasn't being extracted from date-typed cells at all).
    from datetime import date

    result = _result(
        columns=[
            ColumnSpec(name="date", type="DATE"),
            ColumnSpec(name="net_revenue", type="DOUBLE"),
        ],
        rows=[[date(2011, 12, 4), 24565.78], [date(2011, 12, 5), 88274.72]],
    )
    fake_client = _FakeClient(
        answer_text="The highest revenue was on December 5th at $88,274.72, "
        "versus $24,565.78 on December 4th."
    )
    wrapped = wrap(
        "Daily revenue for early December 2011?", result, _chart_spec(), client=fake_client
    )
    assert "December 5th" in wrapped.answer_text


def test_wrap_allows_a_digit_only_or_alphanumeric_product_id_from_a_varchar_column():
    # Regression test: found live while running S24's required evaluation
    # case (the "which products are selling the most" canonical question).
    # product_id is a VARCHAR column, but real values are often all-digits
    # ("22423") or digits-plus-a-letter ("85123A") - quoting one verbatim
    # in the narrative was flagged as a fabricated number, since
    # _numeric_candidates only ever looked at numeric/date-typed cells,
    # never string cells.
    result = _result(
        columns=[
            ColumnSpec(name="product_id", type="VARCHAR"),
            ColumnSpec(name="revenue", type="DOUBLE"),
        ],
        rows=[["22423", 327839.15], ["85123A", 253781.57]],
    )
    fake_client = _FakeClient(
        answer_text="The top-selling product is 22423 at $327,839.15, "
        "followed by 85123A at $253,781.57."
    )
    wrapped = wrap("Which products sell the most?", result, _chart_spec(), client=fake_client)
    assert "22423" in wrapped.answer_text


def test_wrap_allows_the_magnitude_of_a_negative_value_phrased_as_a_loss():
    # Regression test: found live while running S24's required evaluation
    # case (the "least performing products" canonical question, where
    # scope.md's admin-StockCode-as-worst-product finding produces a
    # genuinely negative revenue). "-147614.08" phrased as "a loss of
    # $147,614.08" is faithful to the data - the sign is conveyed in
    # words, not digits - but wasn't grounded since only the exact signed
    # value was a candidate.
    result = _result(
        columns=[
            ColumnSpec(name="product_id", type="VARCHAR"),
            ColumnSpec(name="revenue", type="DOUBLE"),
        ],
        rows=[["B", -147614.08], ["D", -1318.18]],
    )
    fake_client = _FakeClient(
        answer_text="Product B is the worst performer with a loss of $147,614.08, "
        "followed by product D with a loss of $1,318.18."
    )
    wrapped = wrap(
        "Which products are the least performing?", result, _chart_spec(), client=fake_client
    )
    assert "147,614.08" in wrapped.answer_text


def test_prompt_includes_the_question_and_result_rows():
    fake_client = _FakeClient()
    wrap("How many orders?", _result(), _chart_spec(), client=fake_client)
    kwargs = fake_client.chat.completions.captured_kwargs
    user_message = next(m["content"] for m in kwargs["messages"] if m["role"] == "user")
    assert "How many orders?" in user_message
    assert "52612" in user_message
