"""Tests for SQL generation agent: prompts, guardrail integration, self-repair loop."""

import pytest

from app.agents import (
    MockLLMClient,
    SQLGenerationRequest,
    SQLGenerationResponse,
    SQLGenerator,
    extract_sql,
    format_violations,
)
from app.agents.prompts import (
    build_initial_messages,
    render_repair_feedback_prompt,
    render_user_prompt,
)
from app.core.config import get_settings
from app.security.validator import validate_sql
from app.semantic.models import FilterCondition
from app.semantic.retrieval.schemas import (
    JoinStep,
    ResolvedJoinPath,
    RetrievalResult,
    RetrievalStatus,
    SelectedDimension,
    SelectedMetric,
    TransformationMetadata,
)

# ==============================================================================
# FIXTURES
# ==============================================================================

VALID_REVENUE_SQL = """```sql
SELECT
    p.product_category_name AS product_category,
    SUM(oi.price) AS total_revenue
FROM business.order_items oi
JOIN business.orders o ON oi.order_id = o.order_id
JOIN business.products p ON oi.product_id = p.product_id
WHERE o.order_status = 'delivered'
GROUP BY p.product_category_name
ORDER BY total_revenue DESC
```"""


@pytest.fixture
def retrieval_result() -> RetrievalResult:
    """Provide a realistic RetrievalResult with revenue metric, dimensions, and fan-out joins."""
    return RetrievalResult(
        question="Tổng doanh thu theo danh mục sản phẩm",
        status=RetrievalStatus.SUCCESS,
        confidence_score=0.92,
        selected_metrics=[
            SelectedMetric(
                name="revenue",
                description="Doanh thu các đơn đã giao",
                expression="SUM(oi.price)",
                aggregation="SUM",
                source_tables=["business.order_items", "business.orders"],
                filters=[
                    FilterCondition(
                        condition="o.order_status = 'delivered'",
                        description="Chỉ tính đơn đã giao",
                    )
                ],
                warnings=[
                    "Do NOT flat-join business.payments directly; pre-aggregate to avoid double counting."
                ],
                score=0.95,
            )
        ],
        selected_dimensions=[
            SelectedDimension(
                name="product_category",
                description="Danh mục sản phẩm",
                source_table="business.products",
                source_column="product_category_name",
                data_type="VARCHAR(64)",
                score=0.9,
            ),
            SelectedDimension(
                name="order_year",
                description="Năm đặt hàng",
                source_table="business.orders",
                source_column="order_purchase_timestamp",
                data_type="TIMESTAMP",
                transformations=[
                    TransformationMetadata(
                        name="year",
                        expression="EXTRACT(YEAR FROM o.order_purchase_timestamp)",
                        description="Year of purchase timestamp",
                    )
                ],
                score=0.8,
            ),
        ],
        required_tables=["business.order_items", "business.orders", "business.products"],
        resolved_join_path=ResolvedJoinPath(
            root_table="business.order_items",
            required_tables=["business.order_items", "business.orders", "business.products"],
            steps=[
                JoinStep(
                    from_table="business.order_items",
                    to_table="business.orders",
                    join_type="INNER",
                    join_condition="o.order_id = oi.order_id",
                    relationship_name="order_items_to_orders",
                    cardinality="many_to_one",
                    is_safe_for_aggregation=True,
                ),
                JoinStep(
                    from_table="business.orders",
                    to_table="business.products",
                    join_type="INNER",
                    join_condition="p.product_id = oi.product_id",
                    relationship_name="order_items_to_products",
                    cardinality="many_to_one",
                    is_safe_for_aggregation=True,
                ),
            ],
            is_safe_for_direct_execution=True,
        ),
        warnings=["payment metrics must use pre-aggregated CTE to avoid fan-out"],
        is_safe_for_direct_execution=True,
    )


# ==============================================================================
# SQL EXTRACTION TESTS
# ==============================================================================


def test_extract_sql_prefers_last_fenced_block() -> None:
    response = "Reasoning here.\n```sql\nSELECT 1\n```\nThen some text.\n```SQL\nSELECT 2\n```"
    assert extract_sql(response) == "SELECT 2"


def test_extract_sql_fenced_without_language_tag() -> None:
    assert extract_sql("```sql\nSELECT 1\n```") == "SELECT 1"
    assert extract_sql("```\nSELECT 1\n```") == "SELECT 1"


def test_extract_sql_plain_text_fallback() -> None:
    assert extract_sql("SELECT 1 FROM business.orders") == "SELECT 1 FROM business.orders"


def test_extract_sql_empty_response() -> None:
    for text in ("", "   ", "```sql\n   \n```"):
        assert extract_sql(text) is None


# ==============================================================================
# PROMPT RENDERING TESTS
# ==============================================================================


def test_user_prompt_contains_metric_context(retrieval_result: RetrievalResult) -> None:
    prompt = render_user_prompt("Doanh thu theo danh muc", retrieval_result)
    assert "Doanh thu theo danh muc" in prompt
    assert "SUM(oi.price)" in prompt
    assert "o.order_status = 'delivered'" in prompt
    assert "business.products.product_category_name" in prompt
    assert "EXTRACT(YEAR FROM o.order_purchase_timestamp)" in prompt


def test_user_prompt_flags_unsafe_flat_joins(retrieval_result: RetrievalResult) -> None:
    retrieval_result.is_safe_for_direct_execution = False
    retrieval_result.warnings.append("fan-out on payments join")
    prompt = render_user_prompt("Doanh thu theo phuong thuc thanh toan", retrieval_result)
    assert "UNSAFE" in prompt
    assert "pre-aggregate" in prompt.lower()
    assert "fan-out on payments join" in prompt


def test_user_prompt_renders_join_steps(retrieval_result: RetrievalResult) -> None:
    prompt = render_user_prompt("q", retrieval_result)
    assert "Root FROM: business.order_items" in prompt
    assert "INNER JOIN business.orders ON o.order_id = oi.order_id" in prompt


def test_system_prompt_request_messages(retrieval_result: RetrievalResult) -> None:
    request = SQLGenerationRequest(question="q", retrieval_result=retrieval_result)
    messages = build_initial_messages(request)
    assert messages[0]["role"] == "system"
    assert "SELECT-only" in messages[0]["content"] or "SELECT" in messages[0]["content"]
    assert messages[1]["role"] == "user"
    assert "SUM(oi.price)" in messages[1]["content"]


def test_repair_feedback_prompt_contains_violations() -> None:
    prompt = render_repair_feedback_prompt(
        generated_sql="DROP TABLE x",
        violations=["[CRITICAL] SELECT_ONLY: mutation not allowed"],
        warnings=["w1"],
        attempt=1,
        max_attempts=3,
    )
    assert "DROP TABLE x" in prompt
    assert "[CRITICAL] SELECT_ONLY" in prompt
    assert "repair attempt 1/3" in prompt
    assert "w1" in prompt


def test_format_violations_line_format() -> None:
    result = validate_sql("DROP TABLE business.orders")
    lines = format_violations(result)
    assert lines
    assert lines[0].startswith("[CRITICAL]")


# ==============================================================================
# SQL GENERATOR PIPELINE TESTS
# ==============================================================================


def make_request(retrieval_result: RetrievalResult, **overrides: object) -> SQLGenerationRequest:
    kwargs: dict = {"question": "Tong doanh thu theo danh muc", "retrieval_result": retrieval_result}
    kwargs.update(overrides)
    return SQLGenerationRequest(**kwargs)


def test_generate_valid_sql_first_try(retrieval_result: RetrievalResult) -> None:
    mock_llm = MockLLMClient(responses=[VALID_REVENUE_SQL])
    generator = SQLGenerator(llm_client=mock_llm)

    response = generator.generate(make_request(retrieval_result))

    assert isinstance(response, SQLGenerationResponse)
    assert response.is_valid is True
    assert response.repair_attempts == 0
    assert response.repair_history == []
    assert response.final_sql is not None
    assert "LIMIT 100" in response.final_sql
    assert len(mock_llm.call_history) == 1


def test_generate_final_sql_differs_from_raw_sql(retrieval_result: RetrievalResult) -> None:
    limited_sql = VALID_REVENUE_SQL.replace("ORDER BY total_revenue DESC", "LIMIT 5000")
    mock_llm = MockLLMClient(responses=[limited_sql])
    generator = SQLGenerator(llm_client=mock_llm)

    response = generator.generate(make_request(retrieval_result))

    assert response.is_valid is True
    assert "LIMIT 1000" in (response.final_sql or "")
    assert response.validation_result is not None
    assert response.validation_result.original_limit == 5000
    assert any("clamped" in w.lower() for w in response.validation_result.warnings)


def test_generate_repair_recovers_after_violation(retrieval_result: RetrievalResult) -> None:
    bad_sql = "```sql\nDROP TABLE business.orders\n```"
    mock_llm = MockLLMClient(responses=[bad_sql, VALID_REVENUE_SQL])

    generator = SQLGenerator(llm_client=mock_llm, max_repair_attempts=3)
    response = generator.generate(make_request(retrieval_result))

    assert response.is_valid is True
    assert response.repair_attempts == 1
    assert len(response.repair_history) == 1

    step = response.repair_history[0]
    assert step.attempt == 1
    assert "DROP TABLE" in step.generated_sql
    assert step.violations
    assert any(v.rule == "SELECT_ONLY" for v in step.violations)
    assert "DROP TABLE" in step.feedback_prompt

    assert isinstance(response.validation_result, object)

    assert response.final_sql is not None
    assert "DROP" not in response.final_sql
    assert len(mock_llm.call_history) == 2

    repair_message = mock_llm.call_history[1][-1]["content"]
    assert "failed security and ast guardrail checks" in repair_message
    assert "[CRITICAL]" in repair_message

    assistant_message = mock_llm.call_history[1][-2]["content"]
    assert "DROP TABLE" in assistant_message


def test_generate_repair_history_logged(retrieval_result: RetrievalResult) -> None:
    bad_sql = "```sql\nDROP TABLE business.orders\n```"
    mock_llm = MockLLMClient(responses=[bad_sql, bad_sql, VALID_REVENUE_SQL])

    generator = SQLGenerator(llm_client=mock_llm, max_repair_attempts=3)
    response = generator.generate(make_request(retrieval_result))

    assert response.is_valid is True
    assert response.repair_attempts == 2
    steps = response.repair_history
    assert [step.attempt for step in steps] == [1, 2]
    assert all("[CRITICAL]" in step.feedback_prompt for step in steps)
    assert len(mock_llm.call_history) == 3


def test_generate_repair_exhausts_budget(retrieval_result: RetrievalResult) -> None:
    bad_sql = "DROP TABLE business.orders"
    mock_llm = MockLLMClient(responses=[bad_sql] * 10)
    generator = SQLGenerator(llm_client=mock_llm, max_repair_attempts=2)

    response = generator.generate(make_request(retrieval_result))

    assert response.is_valid is False
    assert response.final_sql is None
    assert response.repair_attempts == 2
    assert [step.attempt for step in response.repair_history] == [1, 2]
    assert "DROP TABLE" in response.raw_sql
    assert len(mock_llm.call_history) == 3
    assert response.validation_result is not None
    assert response.validation_result.is_valid is False


def test_generate_empty_llm_output_fails(retrieval_result: RetrievalResult) -> None:
    mock_llm = MockLLMClient(responses=["", VALID_REVENUE_SQL])
    generator = SQLGenerator(llm_client=mock_llm)

    response = generator.generate(make_request(retrieval_result))

    assert response.is_valid is True
    assert response.repair_attempts == 1
    assert response.repair_history[0].generated_sql == ""


def test_generate_respects_custom_model_override(retrieval_result: RetrievalResult) -> None:
    mock_llm = MockLLMClient(responses=[VALID_REVENUE_SQL])
    generator = SQLGenerator(llm_client=mock_llm)

    response = generator.generate(
        SQLGenerationRequest(
            question="q",
            retrieval_result=retrieval_result,
            model="google/gemini-2.5-flash",
            temperature=0.3,
        )
    )

    assert response.model_used == "google/gemini-2.5-flash"
    first_payload = mock_llm.call_history[0]
    assert isinstance(first_payload, list)


def test_generate_default_model_used(retrieval_result: RetrievalResult) -> None:
    mock_llm = MockLLMClient(responses=[VALID_REVENUE_SQL])
    generator = SQLGenerator(llm_client=mock_llm)

    response = generator.generate(make_request(retrieval_result))

    assert response.model_used == get_settings().LLM_MODEL


def test_generate_skips_non_analytics_question() -> None:
    empty_retrieval = RetrievalResult(
        question="xin chao ban",
        status=RetrievalStatus.INSUFFICIENT_METADATA,
    )
    mock_llm = MockLLMClient(responses=["SELECT 'Hello' AS greeting"])
    generator = SQLGenerator(llm_client=mock_llm)

    response = generator.generate(
        SQLGenerationRequest(question="xin chao ban", retrieval_result=empty_retrieval)
    )

    assert len(mock_llm.call_history) == 0
    assert response.is_valid is False
    assert response.final_sql is None
    assert response.raw_sql == ""
    assert response.repair_attempts == 0
    assert response.llm_response == ""
    assert response.explanation is not None
    assert "not an analytics question" in response.explanation


async def test_generate_async_skips_non_analytics_question() -> None:
    empty_retrieval = RetrievalResult(
        question="hi there",
        status=RetrievalStatus.INSUFFICIENT_METADATA,
    )
    mock_llm = MockLLMClient(responses=["SELECT 'Hello' AS greeting"])
    generator = SQLGenerator(llm_client=mock_llm)

    response = await generator.generate_async(
        SQLGenerationRequest(question="hi there", retrieval_result=empty_retrieval)
    )

    assert len(mock_llm.call_history) == 0
    assert response.is_valid is False
    assert response.explanation is not None


# ==============================================================================
# ASYNC PIPELINE TESTS
# ==============================================================================


async def test_generate_async_valid_sql(retrieval_result: RetrievalResult) -> None:
    mock_llm = MockLLMClient(responses=[VALID_REVENUE_SQL])
    generator = SQLGenerator(llm_client=mock_llm)

    response = await generator.generate_async(make_request(retrieval_result))

    assert response.is_valid is True
    assert response.repair_attempts == 0
    assert "business.order_items" in (response.final_sql or "")


async def test_generate_async_repair_recovers(retrieval_result: RetrievalResult) -> None:
    bad_sql = "SELECT pg_sleep(10)"
    mock_llm = MockLLMClient(responses=[bad_sql, VALID_REVENUE_SQL])
    generator = SQLGenerator(llm_client=mock_llm, max_repair_attempts=2)

    response = await generator.generate_async(make_request(retrieval_result))

    assert response.is_valid is True
    assert response.repair_attempts == 1
    assert response.repair_history[0].violations
    assert len(mock_llm.call_history) == 2


# ==============================================================================
# INTEGRATION WITH REAL RETRIEVAL OUTPUT
# ==============================================================================


def test_generate_with_mock_rule_based_response(retrieval_result: RetrievalResult) -> None:
    mock_llm = MockLLMClient()
    generator = SQLGenerator(llm_client=mock_llm)

    response = generator.generate(make_request(retrieval_result))

    assert response.is_valid is True
    assert response.final_sql is not None
    assert "SUM(oi.price)" in response.final_sql
    assert "business.products" in response.final_sql
