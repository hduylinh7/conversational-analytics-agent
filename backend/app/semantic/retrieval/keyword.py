"""Keyword-based semantic retrieval engine scoring metrics and dimensions via identifiers, synonyms, and descriptions."""

import re

from app.semantic.models import DimensionMetadata, MetricMetadata, SemanticCatalog
from app.semantic.retrieval.normalizer import (
    ENGLISH_STOP_WORDS,
    VIETNAMESE_STOP_WORDS,
    normalize_text,
    stem_token,
    strip_vietnamese_diacritics,
    tokenize,
)
from app.semantic.retrieval.schemas import (
    SelectedDimension,
    SelectedMetric,
    SelectionExplanation,
)

# Generic modifier words that cannot alone validate a multi-word synonym match
GENERIC_MODIFIERS = {
    "tổng",
    "tong",
    "số",
    "so",
    "lượng",
    "luong",
    "các",
    "cac",
    "những",
    "nhung",
    "toàn",
    "bộ",
    "total",
    "number",
    "count",
    "all",
}

# Single words that require domain qualification to prevent false positives
GENERIC_SINGLE_WORDS = {
    "city",
    "state",
    "status",
    "date",
    "year",
    "month",
    "bang",
    "ngay",
    "nam",
    "thang",
}


class KeywordEngine:
    """Keyword search engine matching natural language analytics queries against semantic metadata."""

    def __init__(self, catalog: SemanticCatalog) -> None:
        """Initialize and pre-index metadata terms from the SemanticCatalog.

        Args:
            catalog: Loaded and validated SemanticCatalog.
        """
        self.catalog = catalog
        self._year_regex = re.compile(r"\b(19\d\d|20\d\d)\b")

    def search_metrics(
        self,
        question: str,
        top_k: int = 3,
        min_threshold: float = 0.25,
    ) -> list[SelectedMetric]:
        """Search and rank metrics matching the input question.

        Args:
            question: Natural language question.
            top_k: Maximum candidate metrics to return.
            min_threshold: Minimum confidence score required.

        Returns:
            Ranked list of SelectedMetric models.
        """
        candidates: list[tuple[float, SelectedMetric]] = []

        for metric in self.catalog.metrics.metrics:
            score, explanation = self._score_metric(metric, question)
            if score >= min_threshold:
                selected = SelectedMetric(
                    name=metric.name,
                    description=metric.description,
                    expression=metric.expression,
                    aggregation=metric.aggregation,
                    source_tables=metric.source_tables,
                    filters=metric.filters,
                    warnings=metric.warnings,
                    score=score,
                    explanation=explanation,
                )
                candidates.append((score, selected))

        # Sort descending by score, tie-break by name for deterministic results
        candidates.sort(key=lambda x: (x[0], x[1].name), reverse=True)
        return [item[1] for item in candidates[:top_k]]

    def search_dimensions(
        self,
        question: str,
        top_k: int = 5,
        min_threshold: float = 0.25,
    ) -> list[SelectedDimension]:
        """Search and rank dimensions matching the input question.

        Args:
            question: Natural language question.
            top_k: Maximum candidate dimensions to return.
            min_threshold: Minimum confidence score required.

        Returns:
            Ranked list of SelectedDimension models.
        """
        candidates: list[tuple[float, SelectedDimension]] = []

        for dim in self.catalog.dimensions.dimensions:
            score, explanation = self._score_dimension(dim, question)
            if score >= min_threshold:
                selected = SelectedDimension(
                    name=dim.name,
                    description=dim.description,
                    source_table=dim.source_table,
                    source_column=dim.source_column,
                    data_type=dim.data_type,
                    transformations=dim.transformations,
                    allowed_values=dim.allowed_values,
                    score=score,
                    explanation=explanation,
                )
                candidates.append((score, selected))

        candidates.sort(key=lambda x: (x[0], x[1].name), reverse=True)
        return [item[1] for item in candidates[:top_k]]

    def _score_metric(
        self,
        metric: MetricMetadata,
        question: str,
    ) -> tuple[float, SelectionExplanation | None]:
        """Calculate relevance score for a metric against the question."""
        return self._score_metadata_item(
            item_name=metric.name,
            item_type="metric",
            description=metric.description,
            synonyms=metric.synonyms.all,
            extra_terms=[],
            question=question,
            aggregation=metric.aggregation,
        )

    def _score_dimension(
        self,
        dim: DimensionMetadata,
        question: str,
    ) -> tuple[float, SelectionExplanation | None]:
        """Calculate relevance score for a dimension against the question."""
        extra_terms: list[str] = []
        for trans in dim.transformations:
            extra_terms.append(trans.name)
        extra_terms.extend(dim.allowed_values)

        has_temporal = False
        if "date" in dim.name or "timestamp" in dim.source_column:
            has_temporal = True

        return self._score_metadata_item(
            item_name=dim.name,
            item_type="dimension",
            description=dim.description,
            synonyms=dim.synonyms.all,
            extra_terms=extra_terms,
            question=question,
            is_temporal=has_temporal,
        )

    def _score_metadata_item(
        self,
        item_name: str,
        item_type: str,
        description: str,
        synonyms: list[str],
        extra_terms: list[str],
        question: str,
        is_temporal: bool = False,
        aggregation: str | None = None,
    ) -> tuple[float, SelectionExplanation | None]:
        """Unified matching logic evaluating identifiers, exact synonyms, n-grams, and descriptions."""
        norm_q = normalize_text(question)
        if not norm_q:
            return 0.0, None

        unacc_q = strip_vietnamese_diacritics(norm_q)
        tokens_filtered = tokenize(norm_q, remove_stopwords=True)
        tokens_stemmed = tokenize(norm_q, remove_stopwords=True, stem=True)

        set_tokens_filtered = set(tokens_filtered)
        set_tokens_stemmed = set(tokens_stemmed)

        q_filtered_str = " ".join(tokens_filtered)
        q_stemmed_str = " ".join(tokens_stemmed)

        best_score = 0.0
        best_explanation: SelectionExplanation | None = None

        def update_best(
            score: float,
            matched_field: str,
            matched_term: str,
            reason: str,
        ) -> None:
            nonlocal best_score, best_explanation
            if score > best_score:
                best_score = score
                best_explanation = SelectionExplanation(
                    target_type="metric" if item_type == "metric" else "dimension",
                    target_name=item_name,
                    matched_field=matched_field,
                    matched_term=matched_term,
                    keyword_score=round(score, 4),
                    embedding_score=0.0,
                    combined_score=round(score, 4),
                    reason=reason,
                )

        # 1. Exact Identifier Match
        clean_name = item_name.replace("_", " ").lower()
        if item_name.lower() in norm_q or clean_name in norm_q or clean_name in unacc_q:
            update_best(
                1.0,
                "identifier",
                item_name,
                f"Exact match on identifier '{item_name}'.",
            )
            return best_score, best_explanation

        # 2. English counting intent ("how many orders", "how many items", etc.)
        if item_type == "metric" and aggregation in ("COUNT", "COUNT DISTINCT"):
            if "how many" in norm_q or "count of" in norm_q:
                # Check entity match (e.g. order in order_count, customer in customer_count)
                entity = item_name.split("_")[0]  # e.g. "order", "customer", "items"
                entity_stem = stem_token(entity)
                if entity_stem in set_tokens_stemmed or entity in norm_q:
                    update_best(
                        0.95,
                        "counting_intent",
                        f"how many {entity}",
                        f"Matched English counting intent for entity '{entity}'.",
                    )

        # 3. Temporal matching for date/timestamp dimensions
        if is_temporal:
            year_matches = self._year_regex.findall(norm_q)
            if year_matches:
                matched_year = year_matches[0]
                # Prioritize order_date as the primary order lifecycle event
                if item_name == "order_date":
                    update_best(
                        0.90,
                        "temporal_pattern",
                        f"year {matched_year}",
                        f"Matched calendar year '{matched_year}' on primary order date.",
                    )
                elif "delivered" in norm_q or "nhan hang" in unacc_q or "giao hang" in unacc_q:
                    if item_name == "order_delivered_date":
                        update_best(
                            0.92,
                            "temporal_pattern",
                            f"year {matched_year}",
                            f"Matched delivery year '{matched_year}'.",
                        )
                elif "estimated" in norm_q or "du kien" in unacc_q:
                    if item_name == "order_estimated_delivery_date":
                        update_best(
                            0.92,
                            "temporal_pattern",
                            f"year {matched_year}",
                            f"Matched estimated delivery year '{matched_year}'.",
                        )

        # 4. Synonym and Phrase Matching
        for syn in synonyms:
            syn_norm = normalize_text(syn)
            syn_unacc = strip_vietnamese_diacritics(syn_norm)
            syn_tokens = syn_norm.split()
            syn_stemmed = " ".join(stem_token(t) for t in syn_tokens)
            is_multiword = len(syn_tokens) > 1

            # Check if multi-word synonym requires content tokens (avoid modifier-only false matches)
            content_tokens = [
                t for t in syn_tokens if t not in GENERIC_MODIFIERS and len(t) > 1
            ]

            # Word-boundary matching
            word_boundary_pattern = rf"(^|\s){re.escape(syn_norm)}(\s|$)"
            word_boundary_unacc = rf"(^|\s){re.escape(syn_unacc)}(\s|$)"

            if re.search(word_boundary_pattern, norm_q) or re.search(word_boundary_unacc, unacc_q):
                if is_multiword:
                    score = 0.98
                else:
                    # Single word penalty if generic
                    score = 0.35 if syn_norm in GENERIC_SINGLE_WORDS else 0.92

                update_best(
                    score,
                    "exact_synonym",
                    syn,
                    f"Exact phrase match on synonym '{syn}' (score={score:.2f}).",
                )
                continue

            # Stemmed phrase match (e.g. shipping costs -> shipping cost)
            if syn_stemmed in q_stemmed_str:
                if is_multiword:
                    score = 0.98
                else:
                    score = 0.35 if syn_stemmed in GENERIC_SINGLE_WORDS else 0.91

                update_best(
                    score,
                    "exact_synonym",
                    syn,
                    f"Stemmed phrase match on synonym '{syn}' (score={score:.2f}).",
                )
                continue

            # Filtered phrase match
            if syn_norm in q_filtered_str or syn_unacc in strip_vietnamese_diacritics(q_filtered_str):
                score = 0.95 if is_multiword else (0.35 if syn_norm in GENERIC_SINGLE_WORDS else 0.89)
                update_best(
                    score,
                    "exact_synonym",
                    syn,
                    f"Filtered phrase match on synonym '{syn}' (score={score:.2f}).",
                )
                continue

            # Token overlap between synonym and query
            # Guard: At least one distinctive content token must be present!
            if content_tokens:
                has_content_hit = any(
                    ct in set_tokens_filtered
                    or strip_vietnamese_diacritics(ct) in {strip_vietnamese_diacritics(x) for x in set_tokens_filtered}
                    or stem_token(ct) in set_tokens_stemmed
                    for ct in content_tokens
                )
                if not has_content_hit:
                    # Skip overlap calculation if no content token matched
                    continue

            syn_token_set = set(syn_tokens)
            syn_stemmed_set = {stem_token(t) for t in syn_tokens}
            overlap = syn_token_set.intersection(set_tokens_filtered)
            overlap_stemmed = syn_stemmed_set.intersection(set_tokens_stemmed)

            max_overlap = max(len(overlap), len(overlap_stemmed))
            if max_overlap == len(syn_tokens) and len(syn_tokens) >= 2:
                score = 0.86
                update_best(
                    score,
                    "partial_synonym",
                    syn,
                    f"Complete token coverage for multi-word synonym '{syn}'.",
                )
            elif max_overlap >= 2 and (max_overlap / len(syn_tokens)) >= 0.6:
                ratio = max_overlap / len(syn_tokens)
                score = 0.55 * ratio + 0.15 * (max_overlap / max(1, len(tokens_filtered)))
                update_best(
                    score,
                    "partial_synonym",
                    syn,
                    f"Partial token overlap ({max_overlap}/{len(syn_tokens)}) for synonym '{syn}'.",
                )

        # 5. Extra Terms (Transformations, Allowed Values)
        for term in extra_terms:
            term_norm = normalize_text(term)
            if term_norm in norm_q or term_norm in q_stemmed_str:
                if term_norm not in GENERIC_SINGLE_WORDS:
                    score = 0.85
                    update_best(
                        score,
                        "allowed_value_or_transformation",
                        term,
                        f"Matched transformation or allowed value '{term}'.",
                    )

        # 6. Description keyword matching (secondary fallback)
        desc_tokens = set(tokenize(description, remove_stopwords=True, stem=True))
        desc_overlap = desc_tokens.intersection(set_tokens_stemmed)
        desc_overlap = {
            w
            for w in desc_overlap
            if len(w) > 3
            and w not in VIETNAMESE_STOP_WORDS
            and w not in ENGLISH_STOP_WORDS
            and w not in GENERIC_MODIFIERS
        }
        if desc_overlap and best_score < 0.4:
            desc_score = min(0.35, 0.15 * len(desc_overlap))
            update_best(
                desc_score,
                "description",
                ", ".join(sorted(desc_overlap)[:3]),
                f"Matched keywords {sorted(desc_overlap)[:3]} in business description.",
            )

        return best_score, best_explanation
