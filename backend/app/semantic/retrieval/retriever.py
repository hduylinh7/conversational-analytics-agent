"""Semantic Retriever orchestrating hybrid keyword and embedding retrieval, ranking, and join resolution."""

from app.core.config import get_settings
from app.core.logging import get_logger
from app.semantic.loader import get_semantic_catalog
from app.semantic.models import SemanticCatalog
from app.semantic.retrieval.embedding import (
    BaseEmbeddingProvider,
    EmbeddingEngine,
    OpenAIEmbeddingProvider,
)
from app.semantic.retrieval.join_resolver import JoinResolver
from app.semantic.retrieval.keyword import KeywordEngine
from app.semantic.retrieval.schemas import (
    RetrievalConfig,
    RetrievalResult,
    RetrievalStatus,
    SelectedDimension,
    SelectedMetric,
    SelectionExplanation,
)

logger = get_logger(__name__)


class SemanticRetriever:
    """Production-oriented Semantic Retrieval module for Text-to-SQL analytics."""

    def __init__(
        self,
        catalog: SemanticCatalog | None = None,
        config: RetrievalConfig | None = None,
        embedding_provider: BaseEmbeddingProvider | None = None,
    ) -> None:
        """Initialize retriever with semantic catalog, configuration, and retrieval engines.

        Args:
            catalog: Populated SemanticCatalog. If None, loaded via get_semantic_catalog().
            config: Runtime RetrievalConfig. If None, derived from application Settings.
            embedding_provider: Optional concrete embedding provider.
        """
        settings = get_settings()

        self.catalog = catalog or get_semantic_catalog(settings.SEMANTIC_LAYER_DIR)
        self.config = config or RetrievalConfig(
            keyword_weight=settings.RETRIEVAL_KEYWORD_WEIGHT,
            embedding_weight=settings.RETRIEVAL_EMBEDDING_WEIGHT,
            min_score_threshold=settings.RETRIEVAL_MIN_CONFIDENCE_SCORE,
            top_k_metrics=settings.RETRIEVAL_TOP_K_METRICS,
            top_k_dimensions=settings.RETRIEVAL_TOP_K_DIMENSIONS,
            enable_embeddings=settings.RETRIEVAL_ENABLE_EMBEDDINGS,
            embedding_model=settings.EMBEDDING_MODEL,
        )

        # Initialize engines
        self.keyword_engine = KeywordEngine(self.catalog)

        # Setup embedding provider
        prov = embedding_provider
        if prov is None and self.config.enable_embeddings and settings.OPENAI_API_KEY:
            prov = OpenAIEmbeddingProvider(
                api_key=settings.OPENAI_API_KEY,
                model=self.config.embedding_model,
            )

        self.embedding_engine = EmbeddingEngine(
            provider=prov,
            enabled=self.config.enable_embeddings,
        )

        # Precompute metadata embeddings if provider is active
        if self.embedding_engine.is_available:
            self.embedding_engine.build_metadata_index(
                metrics=self.catalog.metrics.metrics,
                dimensions=self.catalog.dimensions.dimensions,
            )

        self.join_resolver = JoinResolver(self.catalog.data_sources)

    def reload_catalog(self, catalog: SemanticCatalog | None = None) -> None:
        """Reload semantic metadata catalog and re-index metadata terms and embeddings.

        Args:
            catalog: Optional replacement SemanticCatalog.
        """
        settings = get_settings()
        self.catalog = catalog or get_semantic_catalog(settings.SEMANTIC_LAYER_DIR, force_reload=True)
        self.keyword_engine = KeywordEngine(self.catalog)
        if self.embedding_engine.is_available:
            self.embedding_engine.build_metadata_index(
                metrics=self.catalog.metrics.metrics,
                dimensions=self.catalog.dimensions.dimensions,
            )
        self.join_resolver = JoinResolver(self.catalog.data_sources)
        logger.info("SemanticRetriever reloaded catalog successfully.")

    def retrieve(
        self,
        question: str,
        top_k_metrics: int | None = None,
        top_k_dimensions: int | None = None,
    ) -> RetrievalResult:
        """Retrieve relevant metrics, dimensions, tables, and join rules for a question.

        Args:
            question: User's natural language analytics question in Vietnamese or English.
            top_k_metrics: Optional override for candidate metric limit.
            top_k_dimensions: Optional override for candidate dimension limit.

        Returns:
            Structured RetrievalResult containing selected metadata, joins, and warnings.
        """
        k_metrics = top_k_metrics or self.config.top_k_metrics
        k_dimensions = top_k_dimensions or self.config.top_k_dimensions
        min_thresh = self.config.min_score_threshold

        # 1. Keyword search candidates
        kw_metrics = self.keyword_engine.search_metrics(
            question=question,
            top_k=len(self.catalog.metrics.metrics),
            min_threshold=0.0,
        )
        kw_dimensions = self.keyword_engine.search_dimensions(
            question=question,
            top_k=len(self.catalog.dimensions.dimensions),
            min_threshold=0.0,
        )

        kw_metric_map = {m.name: m for m in kw_metrics}
        kw_dim_map = {d.name: d for d in kw_dimensions}

        # 2. Embedding similarity candidates (if active)
        emb_metric_scores, emb_dim_scores = self.embedding_engine.search(question)

        # 3. Hybrid scoring & selection for Metrics
        selected_metrics: list[SelectedMetric] = []
        metric_explanations: list[SelectionExplanation] = []

        all_metric_names = set(kw_metric_map.keys()) | set(emb_metric_scores.keys())
        ranked_metrics: list[tuple[float, SelectedMetric]] = []

        for m_name in all_metric_names:
            kw_candidate = kw_metric_map.get(m_name)
            kw_score = kw_candidate.score if kw_candidate else 0.0
            emb_score = emb_metric_scores.get(m_name, 0.0)

            if self.embedding_engine.is_available:
                combined_score = (
                    self.config.keyword_weight * kw_score
                    + self.config.embedding_weight * emb_score
                )
            else:
                combined_score = kw_score

            if combined_score >= min_thresh:
                # Retrieve original metric definition
                m_def = self.catalog.metrics.metric_map[m_name]
                explanation = kw_candidate.explanation if kw_candidate else None
                if explanation:
                    explanation.embedding_score = round(emb_score, 4)
                    explanation.combined_score = round(combined_score, 4)
                else:
                    explanation = SelectionExplanation(
                        target_type="metric",
                        target_name=m_name,
                        matched_field="embedding",
                        matched_term=question,
                        keyword_score=round(kw_score, 4),
                        embedding_score=round(emb_score, 4),
                        combined_score=round(combined_score, 4),
                        reason=f"Matched via semantic embedding similarity (score={emb_score:.2f}).",
                    )

                metric_item = SelectedMetric(
                    name=m_def.name,
                    description=m_def.description,
                    expression=m_def.expression,
                    aggregation=m_def.aggregation,
                    source_tables=m_def.source_tables,
                    filters=m_def.filters,
                    warnings=m_def.warnings,
                    score=round(combined_score, 4),
                    explanation=explanation,
                )
                ranked_metrics.append((combined_score, metric_item))

        ranked_metrics.sort(key=lambda x: (x[0], x[1].name), reverse=True)
        if ranked_metrics:
            top_m_score = ranked_metrics[0][0]
            filtered_metrics: list[SelectedMetric] = [ranked_metrics[0][1]]
            has_m_conjunction = any(
                w in question.lower().split()
                for w in ("và", "and", "cùng", "với", "plus", "also", ",")
            )
            for score, m in ranked_metrics[1:k_metrics]:
                if top_m_score >= 0.90:
                    if has_m_conjunction and score >= 0.70:
                        filtered_metrics.append(m)
                    elif score >= 0.88:
                        filtered_metrics.append(m)
                elif score >= top_m_score - 0.20:
                    filtered_metrics.append(m)
            selected_metrics = filtered_metrics
        else:
            selected_metrics = []

        metric_explanations = [
            m.explanation for m in selected_metrics if m.explanation is not None
        ]

        # 4. Hybrid scoring & selection for Dimensions
        selected_dimensions: list[SelectedDimension] = []
        dim_explanations: list[SelectionExplanation] = []

        all_dim_names = set(kw_dim_map.keys()) | set(emb_dim_scores.keys())
        ranked_dims: list[tuple[float, SelectedDimension]] = []

        for d_name in all_dim_names:
            kw_dim_candidate = kw_dim_map.get(d_name)
            kw_score = kw_dim_candidate.score if kw_dim_candidate else 0.0
            emb_score = emb_dim_scores.get(d_name, 0.0)

            if self.embedding_engine.is_available:
                combined_score = (
                    self.config.keyword_weight * kw_score
                    + self.config.embedding_weight * emb_score
                )
            else:
                combined_score = kw_score

            if combined_score >= min_thresh:
                d_def = self.catalog.dimensions.dimension_map[d_name]
                explanation = kw_dim_candidate.explanation if kw_dim_candidate else None

                if explanation:
                    explanation.embedding_score = round(emb_score, 4)
                    explanation.combined_score = round(combined_score, 4)
                else:
                    explanation = SelectionExplanation(
                        target_type="dimension",
                        target_name=d_name,
                        matched_field="embedding",
                        matched_term=question,
                        keyword_score=round(kw_score, 4),
                        embedding_score=round(emb_score, 4),
                        combined_score=round(combined_score, 4),
                        reason=f"Matched via semantic embedding similarity (score={emb_score:.2f}).",
                    )

                dim_item = SelectedDimension(
                    name=d_def.name,
                    description=d_def.description,
                    source_table=d_def.source_table,
                    source_column=d_def.source_column,
                    data_type=d_def.data_type,
                    transformations=d_def.transformations,
                    allowed_values=d_def.allowed_values,
                    score=round(combined_score, 4),
                    explanation=explanation,
                )
                ranked_dims.append((combined_score, dim_item))

        ranked_dims.sort(key=lambda x: (x[0], x[1].name), reverse=True)
        if ranked_dims:
            top_d_score = ranked_dims[0][0]
            filtered_dims: list[SelectedDimension] = [ranked_dims[0][1]]
            for score, d in ranked_dims[1:k_dimensions]:
                # Avoid dragging multiple redundant date dimensions if primary order_date is chosen
                if "date" in d.name and any("date" in fd.name for fd in filtered_dims):
                    continue
                if score >= top_d_score - 0.20:
                    filtered_dims.append(d)
            selected_dimensions = filtered_dims
        else:
            selected_dimensions = []

        # Analytical intent validation: if no metrics are selected, require analytical keywords
        if not selected_metrics:
            analytical_keywords = {
                "list",
                "show",
                "get",
                "find",
                "danh",
                "sách",
                "liệt",
                "kê",
                "thống",
                "group",
                "by",

                "theo",
                "customer",
                "khách",
                "hàng",
                "order",
                "đơn",
            }
            q_words = set(question.lower().split())
            has_analytical_intent = bool(q_words.intersection(analytical_keywords))
            max_d_score = max((d.score for d in selected_dimensions), default=0.0)
            if not has_analytical_intent or max_d_score < 0.60:
                selected_dimensions = []

        dim_explanations = [
            d.explanation for d in selected_dimensions if d.explanation is not None
        ]

        all_explanations = metric_explanations + dim_explanations

        # 5. Handle empty retrieval

        if not selected_metrics and not selected_dimensions:
            return RetrievalResult(
                question=question,
                status=RetrievalStatus.INSUFFICIENT_METADATA,
                confidence_score=0.0,
                selected_metrics=[],
                selected_dimensions=[],
                required_tables=[],
                required_columns=[],
                join_paths=[],
                resolved_join_path=None,
                mandatory_filters=[],
                warnings=[
                    "No relevant business metrics or dimensions identified for the question."
                ],
                is_safe_for_direct_execution=False,
                selection_explanations=[],
            )

        # 6. Resolve tables, columns, join paths, and risks
        (
            resolved_path,
            required_tables,
            required_columns,
            mandatory_filters,
            warnings,
            is_safe,
        ) = self.join_resolver.resolve(
            selected_metrics=selected_metrics,
            selected_dimensions=selected_dimensions,
        )

        # 7. Determine Status
        if not is_safe:
            status = RetrievalStatus.UNSAFE_JOIN
        elif not selected_metrics:
            status = RetrievalStatus.PARTIAL
        elif not selected_dimensions:
            # Metric-only queries can be complete (e.g. "What is total revenue?")
            status = RetrievalStatus.SUCCESS
        else:
            status = RetrievalStatus.SUCCESS

        # 8. Compute overall confidence score
        scores = [m.score for m in selected_metrics] + [d.score for d in selected_dimensions]
        confidence_score = round(max(scores) if scores else 0.0, 4)

        return RetrievalResult(
            question=question,
            status=status,
            confidence_score=confidence_score,
            selected_metrics=selected_metrics,
            selected_dimensions=selected_dimensions,
            required_tables=required_tables,
            required_columns=required_columns,
            join_paths=resolved_path.relationships,
            resolved_join_path=resolved_path,
            mandatory_filters=mandatory_filters,
            warnings=warnings,
            is_safe_for_direct_execution=is_safe,
            selection_explanations=all_explanations,
        )


_global_retriever: SemanticRetriever | None = None


def get_semantic_retriever(
    catalog: SemanticCatalog | None = None,
    config: RetrievalConfig | None = None,
    force_reload: bool = False,
) -> SemanticRetriever:
    """Return a singleton instance of SemanticRetriever, instantiating on first call.

    Args:
        catalog: Optional custom SemanticCatalog.
        config: Optional custom RetrievalConfig.
        force_reload: If True, re-instantiate retriever.

    Returns:
        Configured SemanticRetriever instance.
    """
    global _global_retriever
    if _global_retriever is None or force_reload or catalog is not None or config is not None:
        _global_retriever = SemanticRetriever(catalog=catalog, config=config)
    return _global_retriever


def retrieve(question: str) -> RetrievalResult:
    """Convenience top-level retrieval interface matching standard pipeline convention.

    Args:
        question: User's analytics question in Vietnamese or English.

    Returns:
        Structured RetrievalResult.
    """
    retriever = get_semantic_retriever()
    return retriever.retrieve(question)
