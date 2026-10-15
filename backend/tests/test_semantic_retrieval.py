from pathlib import Path

import pytest

from app.semantic.loader import (
    SemanticFileNotFoundError,
    SemanticYamlSyntaxError,
    get_semantic_catalog,
    load_semantic_layer,
    reload_semantic_layer,
)
from app.semantic.models import SemanticCatalog
from app.semantic.retrieval import (
    MockEmbeddingProvider,
    RetrievalConfig,
    RetrievalResult,
    RetrievalStatus,
    SemanticRetriever,
    normalize_text,
    retrieve,
    stem_token,
    strip_vietnamese_diacritics,
    tokenize,
)
from app.semantic.retrieval.join_resolver import JoinResolver


@pytest.fixture
def catalog() -> SemanticCatalog:
    """Provide the default repository SemanticCatalog."""
    return get_semantic_catalog()


@pytest.fixture
def retriever(catalog: SemanticCatalog) -> SemanticRetriever:
    """Provide a default keyword-based SemanticRetriever."""
    config = RetrievalConfig(enable_embeddings=False)
    return SemanticRetriever(catalog=catalog, config=config)


# ==============================================================================
# 1. SEMANTIC LAYER LOADING & CACHING TESTS
# ==============================================================================


def test_semantic_catalog_loading_and_caching() -> None:
    """Verify that catalog loads, caches in memory, and reloads on demand."""
    cat1 = get_semantic_catalog()
    cat2 = get_semantic_catalog()
    assert cat1 is cat2  # Same object in memory, no redundant file read

    cat3 = reload_semantic_layer()
    assert cat3 is not None
    assert cat3.data_sources is not None
    assert cat3.metrics is not None
    assert cat3.dimensions is not None


def test_missing_semantic_file_handling(tmp_path: Path) -> None:

    """Verify that missing files raise SemanticFileNotFoundError."""
    empty_dir = tmp_path / "empty_semantic"
    empty_dir.mkdir()
    with pytest.raises(SemanticFileNotFoundError):
        load_semantic_layer(empty_dir)


def test_malformed_yaml_handling(tmp_path: Path) -> None:
    """Verify that malformed YAML files raise SemanticYamlSyntaxError."""
    bad_dir = tmp_path / "bad_semantic"
    bad_dir.mkdir()
    (bad_dir / "data_sources.yaml").write_text("invalid: [yaml: unclosed", encoding="utf-8")
    with pytest.raises(SemanticYamlSyntaxError):
        load_semantic_layer(bad_dir)



# ==============================================================================
# 2. TEXT NORMALIZATION & DIACRITICS TESTS
# ==============================================================================


def test_vietnamese_diacritics_stripping() -> None:
    """Test Vietnamese tone mark and accent removal."""
    accented = "Doanh thu theo danh mục sản phẩm của khách hàng ở Đắk Lắk"
    expected = "Doanh thu theo danh muc san pham cua khach hang o Dak Lak"
    assert strip_vietnamese_diacritics(accented) == expected


def test_text_normalization() -> None:
    """Test lowercasing, punctuation stripping, and whitespace collapse."""
    raw = "  Tổng số đơn hàng: trong năm 2018!??  "
    assert normalize_text(raw) == "tổng số đơn hàng trong năm 2018"


def test_english_stemming() -> None:
    """Test simple plural-to-singular stemming for domain terms."""
    assert stem_token("states") == "state"
    assert stem_token("costs") == "cost"
    assert stem_token("orders") == "order"
    assert stem_token("categories") == "category"
    assert stem_token("payments") == "payment"


def test_tokenization_and_stopword_filtering() -> None:
    """Test tokenization with and without stopword filtering."""
    query = "Phí vận chuyển theo bang của khách hàng"
    tokens = tokenize(query, remove_stopwords=True)
    # Stop words 'theo' and 'của' should be removed
    assert "theo" not in tokens
    assert "của" not in tokens
    assert "phí" in tokens
    assert "vận" in tokens
    assert "chuyển" in tokens
    assert "bang" in tokens
    assert "khách" in tokens
    assert "hàng" in tokens


# ==============================================================================
# 3. REALISTIC BENCHMARK QUESTIONS (VIETNAMESE & ENGLISH)
# ==============================================================================


def test_benchmark_query_1_vietnamese_revenue_by_category(retriever: SemanticRetriever) -> None:
    """Case 1: 'Doanh thu theo danh mục sản phẩm.'"""
    result = retriever.retrieve("Doanh thu theo danh mục sản phẩm.")

    assert result.status == RetrievalStatus.SUCCESS
    assert result.is_safe_for_direct_execution is True

    metric_names = [m.name for m in result.selected_metrics]
    assert "total_revenue" in metric_names

    dim_names = [d.name for d in result.selected_dimensions]
    assert "product_category" in dim_names

    # Check required tables & join
    assert "business.order_items" in result.required_tables
    assert "business.products" in result.required_tables
    assert any(rel.name == "order_items_to_products" for rel in result.join_paths)


def test_benchmark_query_2_vietnamese_orders_in_2018(retriever: SemanticRetriever) -> None:
    """Case 2: 'Tổng số đơn hàng trong năm 2018.'"""
    result = retriever.retrieve("Tổng số đơn hàng trong năm 2018.")

    assert result.status == RetrievalStatus.SUCCESS
    metric_names = [m.name for m in result.selected_metrics]
    assert "order_count" in metric_names

    dim_names = [d.name for d in result.selected_dimensions]
    assert "order_date" in dim_names

    assert "business.orders" in result.required_tables
    # Since single table is required, no join steps needed
    assert result.resolved_join_path is not None
    assert len(result.resolved_join_path.steps) == 0



def test_benchmark_query_3_vietnamese_freight_by_state(retriever: SemanticRetriever) -> None:
    """Case 3: 'Phí vận chuyển theo bang của khách hàng.'"""
    result = retriever.retrieve("Phí vận chuyển theo bang của khách hàng.")

    assert result.status == RetrievalStatus.SUCCESS
    metric_names = [m.name for m in result.selected_metrics]
    assert "total_freight" in metric_names

    dim_names = [d.name for d in result.selected_dimensions]
    assert "customer_state" in dim_names

    # order_items requires bridging through orders to reach customers
    assert "business.order_items" in result.required_tables
    assert "business.customers" in result.required_tables
    assert "business.orders" in result.required_tables  # intermediate table

    join_rel_names = [rel.name for rel in result.join_paths]
    assert "order_items_to_orders" in join_rel_names
    assert "orders_to_customers" in join_rel_names


def test_benchmark_query_4_english_revenue_by_category(retriever: SemanticRetriever) -> None:
    """Case 4: 'Show revenue by product category.'"""
    result = retriever.retrieve("Show revenue by product category.")

    assert result.status == RetrievalStatus.SUCCESS
    metric_names = [m.name for m in result.selected_metrics]
    assert "total_revenue" in metric_names

    dim_names = [d.name for d in result.selected_dimensions]
    assert "product_category" in dim_names


def test_benchmark_query_5_english_order_count_in_2018(retriever: SemanticRetriever) -> None:
    """Case 5: 'How many orders were placed in 2018?'"""
    result = retriever.retrieve("How many orders were placed in 2018?")

    assert result.status == RetrievalStatus.SUCCESS
    metric_names = [m.name for m in result.selected_metrics]
    assert "order_count" in metric_names

    dim_names = [d.name for d in result.selected_dimensions]
    assert "order_date" in dim_names


def test_benchmark_query_6_english_shipping_by_state(retriever: SemanticRetriever) -> None:
    """Case 6: 'Compare shipping costs across customer states.'"""
    result = retriever.retrieve("Compare shipping costs across customer states.")

    assert result.status == RetrievalStatus.SUCCESS
    metric_names = [m.name for m in result.selected_metrics]
    assert "total_freight" in metric_names

    dim_names = [d.name for d in result.selected_dimensions]
    assert "customer_state" in dim_names


# ==============================================================================
# 4. CHASM TRAP AND JOIN RESTRICTION TESTS
# ==============================================================================


def test_chasm_trap_detection(retriever: SemanticRetriever) -> None:
    """Verify that joining order_items and payments triggers critical chasm trap warning."""
    # Question asking for both merchandise item prices and payment values
    result = retriever.retrieve("Tổng tiền hàng và tổng tiền thanh toán theo người bán")

    metric_names = [m.name for m in result.selected_metrics]
    assert "total_revenue" in metric_names
    assert "total_payment_value" in metric_names

    # Check that chasm trap restriction was triggered
    assert result.is_safe_for_direct_execution is False
    assert result.status == RetrievalStatus.UNSAFE_JOIN

    chasm_warnings = [w for w in result.warnings if "chasm_trap_order_items_and_payments" in w]
    assert len(chasm_warnings) > 0
    assert "Cartesian" in chasm_warnings[0] or "Fan-out" in chasm_warnings[0]


def test_one_to_many_fan_out_warning_preservation(retriever: SemanticRetriever) -> None:
    """Verify that 1:N join relationships preserve fan-out warnings and note DISTINCT limitation."""
    # Connecting orders to order_items is 1:N
    resolver = JoinResolver(retriever.catalog.data_sources)
    sel_metric = retriever.keyword_engine.search_metrics("doanh thu")[0]
    sel_dim = retriever.keyword_engine.search_dimensions("ngày đặt hàng")[0]


    path, _, _, _, warnings, is_safe = resolver.resolve([sel_metric], [sel_dim])
    assert is_safe is True
    # The warning on 1:N fan out should be logged
    assert any("order_items_to_orders" in s.relationship_name for s in path.steps)


# ==============================================================================
# 5. IRRELEVANT AND AMBIGUOUS QUERY TESTS
# ==============================================================================


def test_irrelevant_question_returns_insufficient_metadata(retriever: SemanticRetriever) -> None:
    """Verify that completely unrelated questions do not return false-positive metadata."""
    result = retriever.retrieve("Thời tiết ngày mai ở Hà Nội thế nào?")

    assert result.status == RetrievalStatus.INSUFFICIENT_METADATA
    assert len(result.selected_metrics) == 0
    assert len(result.selected_dimensions) == 0
    assert result.confidence_score == 0.0
    assert "No relevant business metrics" in result.warnings[0]


def test_english_irrelevant_question(retriever: SemanticRetriever) -> None:
    """Verify that random English questions return insufficient metadata."""
    result = retriever.retrieve("What is the capital city of Australia?")

    assert result.status == RetrievalStatus.INSUFFICIENT_METADATA
    assert len(result.selected_metrics) == 0
    assert len(result.selected_dimensions) == 0


# ==============================================================================
# 6. EMBEDDING RETRIEVAL & PARAPHRASING TESTS
# ==============================================================================


def test_embedding_retrieval_handles_paraphrases(catalog: SemanticCatalog) -> None:
    """Verify that when embeddings are enabled, paraphrased terms are retrieved."""
    # Use MockEmbeddingProvider with custom semantic aliases for paraphrases
    mock_provider = MockEmbeddingProvider(
        dim=64,
        semantic_aliases={
            "tien nong": "doanh thu",
            "hang hoa": "san pham",
            "phan hang": "product category",
        },
    )

    config = RetrievalConfig(
        enable_embeddings=True,
        keyword_weight=0.5,
        embedding_weight=0.5,
        min_score_threshold=0.20,
    )

    emb_retriever = SemanticRetriever(
        catalog=catalog,
        config=config,
        embedding_provider=mock_provider,
    )

    assert emb_retriever.embedding_engine.is_available is True

    # Test query with paraphrased terminology
    result = emb_retriever.retrieve("Doanh số hàng hóa theo phân hạng")
    assert len(result.selected_metrics) > 0 or len(result.selected_dimensions) > 0


def test_embedding_disabled_runs_pure_keyword(catalog: SemanticCatalog) -> None:
    """Verify retriever runs gracefully without errors when embedding is disabled."""
    config = RetrievalConfig(enable_embeddings=False)
    retriever = SemanticRetriever(catalog=catalog, config=config, embedding_provider=None)

    assert retriever.embedding_engine.is_available is False
    result = retriever.retrieve("Tổng số đơn hàng")
    assert result.status == RetrievalStatus.SUCCESS
    assert result.selected_metrics[0].name == "order_count"


# ==============================================================================
# 7. OUTPUT SCHEMA & DEDUPLICATION TESTS
# ==============================================================================


def test_output_schema_and_json_serialization(retriever: SemanticRetriever) -> None:
    """Verify that RetrievalResult follows the documented schema and serializes to JSON."""
    result = retriever.retrieve("Doanh thu theo danh mục sản phẩm.")

    # Validate Pydantic dump and JSON serialization
    json_data = result.model_dump_json()
    assert isinstance(json_data, str)
    assert "total_revenue" in json_data
    assert "product_category" in json_data

    # Verify structured fields exist
    assert hasattr(result, "question")
    assert hasattr(result, "status")
    assert hasattr(result, "confidence_score")
    assert hasattr(result, "selected_metrics")
    assert hasattr(result, "selected_dimensions")
    assert hasattr(result, "required_tables")
    assert hasattr(result, "required_columns")
    assert hasattr(result, "join_paths")
    assert hasattr(result, "resolved_join_path")
    assert hasattr(result, "mandatory_filters")
    assert hasattr(result, "warnings")
    assert hasattr(result, "is_safe_for_direct_execution")
    assert hasattr(result, "selection_explanations")


def test_deduplication_of_candidates(retriever: SemanticRetriever) -> None:
    """Verify that multiple matching synonyms do not duplicate candidates."""
    # Query repeats multiple synonyms for total_revenue: 'doanh thu', 'tiền hàng', 'doanh số'
    result = retriever.retrieve("Doanh thu tiền hàng doanh số theo danh mục sản phẩm")

    metric_names = [m.name for m in result.selected_metrics]
    assert metric_names.count("total_revenue") == 1


def test_top_level_convenience_retrieve_function() -> None:
    """Verify the top-level retrieve() functional interface."""
    res = retrieve("Tổng doanh thu")
    assert isinstance(res, RetrievalResult)
    assert res.status == RetrievalStatus.SUCCESS
    assert res.selected_metrics[0].name == "total_revenue"
