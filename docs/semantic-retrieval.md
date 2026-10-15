# Semantic Retrieval Architecture & Reference Documentation

The **Semantic Retrieval Module** bridges natural language business questions (in Vietnamese or English) and the machine-readable definitions stored in the **Semantic Layer** (`metrics.yaml`, `dimensions.yaml`, `data_sources.yaml`).

It selects relevant business metrics and dimensions, determines required database tables and columns, resolves safe declared join paths, and identifies analytical risks (e.g. Chasm Traps, 1:N fan-out row multiplication) without executing SQL.

---

## 1. Purpose and Architecture

In a Text-to-SQL system, feeding raw database schemas directly to an LLM produces common failure modes:
1. **Misinterpreting Business Intent**: "Doanh thu" vs. "Tiền thực thu" (merchandise sales vs. payment settlement).
2. **Hallucinating Unsafe JOIN Conditions**: Inferring ad-hoc JOIN conditions from column name similarities.
3. **Chasm Traps & Cartesian Row Multiplication**: Joining multiple 1:N tables (`order_items` and `payments` to `orders`) multiplying SUM aggregations.
4. **Multilingual Discrepancies**: Resolving Vietnamese/English questions into Portuguese or English physical column names.

The **Semantic Retrieval Module** serves as the deterministic and semantic retrieval engine that prepares structured context for downstream Text-to-SQL Generation.

### Architecture Overview

```text
User Question (Vietnamese / English)
               │
               ▼
┌────────────────────────────────────────────────────────┐
│                   Text Normalizer                      │
│ - Diacritics stripping (Vietnamese accents)            │
│ - Lowercasing, punctuation stripping                   │
│ - Stemming (English plurals: orders -> order)          │
│ - Conversational stop word filtering                   │
└───────────────────────┬────────────────────────────────┘
                        │
         ┌──────────────┴──────────────┐
         ▼                             ▼
┌──────────────────┐          ┌──────────────────────────┐
│  Keyword Engine  │          │     Embedding Engine     │
│ - Identifiers    │          │ - Dense similarity       │
│ - Exact synonyms │          │ - Precomputed caching    │
│ - Token overlap  │          │ - Optional & configurable│
│ - Descriptions   │          │ - Graceful fallback      │
└────────┬─────────┘          └────────────┬─────────────┘
         │                                 │
         └──────────────┬──────────────────┘
                        │
                        ▼
┌────────────────────────────────────────────────────────┐
│              Hybrid Ranking & Selection                │
│ - Combined weighted scoring (alpha * KW + beta * Emb)  │
│ - Top-K candidates & confidence thresholding           │
│ - Conjunction-aware margin filtering                   │
│ - Analytical intent validation                         │
└───────────────────────┬────────────────────────────────┘
                        │
                        ▼
┌────────────────────────────────────────────────────────┐
│                     Join Resolver                      │
│ - Required tables & columns extraction                 │
│ - BFS shortest path search on declared relationships   │
│ - Join restriction enforcement (Chasm Trap detection)  │
│ - Cardinality & 1:N fan-out risk warning preservation  │
│ - Mandatory metric filter predicate collection         │
└───────────────────────┬────────────────────────────────┘
                        │
                        ▼
                 RetrievalResult
 (JSON serializable, deterministic, structured DTO)
```

---

## 2. Metadata Loading & In-Memory Caching

Metadata is loaded directly from YAML definitions via `app.semantic.loader`:
- `data_sources.yaml`: Tables, primary keys, foreign keys, relationships, and join restrictions.
- `dimensions.yaml`: Business dimensions, source tables/columns, transformations, and multilingual synonyms.
- `metrics.yaml`: Business metrics, SQL aggregation expressions, filters, and warnings.

### In-Memory Caching

To avoid re-reading and re-parsing YAML files from the filesystem on every request:
- `get_semantic_catalog()` caches the validated `SemanticCatalog` in-memory.
- `reload_semantic_layer()` forces a hot reload when configurations or YAML files change.

```python
from app.semantic.loader import get_semantic_catalog, reload_semantic_layer

# Returns cached instance without disk I/O:
catalog = get_semantic_catalog()

# Forces re-read from disk:
fresh_catalog = reload_semantic_layer()
```

---

## 3. Retrieval Strategies: Keyword & Embedding

### A. Keyword Retrieval Engine (`KeywordEngine`)

The keyword engine indexes metadata across identifiers, multilingual synonyms, allowed values, and descriptions:

1. **Identifier Matching**:
   Exact match on candidate names (e.g. `total_revenue`, `customer_state`) achieves highest confidence (score = 1.0).
2. **Synonym Matching (Vietnamese & English)**:
   - Matches both accented and unaccented Vietnamese (e.g. "doanh thu" matches "Doanh thu" and "doanh thu").
   - Applies domain-specific plural stemming in English (e.g. "shipping costs" matches "shipping cost", "customer states" matches "customer state").
   - Stop-word filtering isolates keywords across filler words (e.g. "bang của khách hàng" matches "bang khách hàng").
   - Content-token requirement: Multi-word synonyms require distinctive root content tokens (e.g. "đơn" for orders, "khách" for customers, "tiền" for revenue) avoiding accidental matches from generic words ("tổng", "số", "lượng").
3. **Temporal Dimensions & Year Extraction**:
   Detects 4-digit years (e.g. 2018) and temporal keywords ("năm", "tháng", "ngày", "year"), prioritizing primary order lifecycle events (`order_date`).
4. **Description & Allowed Value Fallback**:
   Matches informative tokens within descriptions or enumerated values (score up to 0.40).

### B. Embedding Retrieval Engine (`EmbeddingEngine`)

Dense semantic similarity enables retrieving metadata for paraphrased questions that do not share exact keywords.

- **Offline / Default Mode**: The application does **not** require an external embedding API. If disabled or unconfigured, the engine falls back gracefully to pure keyword retrieval without errors.
- **Precomputed Metadata Vector Caching**: When initialized, embeddings for all metrics and dimensions are precomputed and cached in-memory. Query vectors are compared using cosine similarity.
- **Providers Supported**:
  - `MockEmbeddingProvider`: Deterministic feature-hashing provider for test suites and offline environments.
  - `OpenAIEmbeddingProvider`: Lightweight `httpx` client targeting OpenAI embedding models (e.g. `text-embedding-3-small`).

---

## 4. Candidate Ranking and Selection

The hybrid ranker scores candidates using configurable weights:

```text
combined_score = (keyword_weight * kw_score) + (embedding_weight * emb_score)
```

*(When embeddings are disabled, `combined_score = kw_score`).*

Selection rules:
1. **Thresholding**: Candidates scoring below `min_score_threshold` (default: 0.25) are pruned.
2. **Deduplication**: Repeated candidates resulting from multiple matching synonyms are deduplicated.
3. **Margin-based Conjunction Filtering**: When the top candidate is an exact match (score >= 0.90), secondary candidates are only retained if the query explicitly contains coordinating conjunctions ("và", "and", "cùng với") or scores >= 0.88.
4. **Analytical Intent Validation**: If no business metric is matched, single generic dimensions (like "city") are pruned unless analytical inquiry keywords ("list", "show", "group by", "theo") are present.

---

## 5. Table Dependency & JOIN Resolution (`JoinResolver`)

Once metrics and dimensions are selected, `JoinResolver` resolves relational dependencies using **only** declared metadata from `data_sources.yaml`:

### A. Graph Traversal

Declared relationships form an undirected graph where nodes are database tables.
- If only one table is required (e.g. `business.orders` for `order_count` by `order_date`), no JOIN is performed (`steps = []`).
- If multiple tables are required, Breadth-First Search (BFS) identifies the shortest valid join sequence.
- Intermediate bridge tables are automatically incorporated (e.g. `business.orders` is added as a bridge when joining `business.order_items` to `business.customers`).

### B. Chasm Trap Detection

The resolver checks all `join_restrictions` declared in `data_sources.yaml`.

When both `business.order_items` and `business.payments` are required concurrently:
- Flags a `CRITICAL JOIN RESTRICTION` warning detailing the M x N Cartesian fan-out multiplication.
- Sets `is_safe_for_direct_execution = False`.
- Assigns `status = RetrievalStatus.UNSAFE_JOIN`.
- Instructs downstream generation to isolate tables in subqueries or CTEs.

### C. Cardinality Warnings

When a relationship exhibits `one_to_many` cardinality (e.g. `orders` -> `order_items`):
- Includes warning notes that `COUNT(DISTINCT)` is required for order-level metrics.
- Warns that SQL `DISTINCT` does **not** resolve aggregation errors on `SUM(price)`.

---

## 6. Output Interface (`RetrievalResult`)

The module exposes:

```python
def retrieve(question: str) -> RetrievalResult:
    ...
```

### Schema Attributes

| Attribute | Type | Description |
| :--- | :--- | :--- |
| `question` | `str` | Original user query. |
| `status` | `RetrievalStatus` | `SUCCESS`, `PARTIAL`, `INSUFFICIENT_METADATA`, `AMBIGUOUS`, or `UNSAFE_JOIN`. |
| `confidence_score` | `float` | Highest confidence score across selections (0.0 to 1.0). |
| `selected_metrics` | `list[SelectedMetric]` | Ranked metrics with expressions, aggregations, and scores. |
| `selected_dimensions` | `list[SelectedDimension]` | Ranked dimensions with source columns, transformations, and scores. |
| `required_tables` | `list[str]` | Complete set of source and intermediate bridge tables. |
| `required_columns` | `list[str]` | Physical columns referenced by expressions and dimensions. |
| `join_paths` | `list[RelationshipMetadata]` | Declarative relationship models required for the query. |
| `resolved_join_path` | `ResolvedJoinPath` | Detailed sequence of join hops from root table. |
| `mandatory_filters` | `list[FilterCondition]` | Enforced calculation filters (e.g. `order_status = 'delivered'`). |
| `warnings` | `list[str]` | All analytical risks, fan-out caveats, and restriction notices. |
| `is_safe_for_direct_execution` | `bool` | `False` if chasm trap or missing join path detected. |
| `selection_explanations` | `list[SelectionExplanation]` | Audit trail of matching rules and scores for debugging. |

---

## 7. Concrete Examples

### Example 1: Vietnamese Query with Multi-Table Bridge

**Input Question**: `"Phí vận chuyển theo bang của khách hàng."`

**Output `RetrievalResult` (Summary)**:
```json
{
  "question": "Phí vận chuyển theo bang của khách hàng.",
  "status": "SUCCESS",
  "confidence_score": 0.98,
  "selected_metrics": [
    {
      "name": "total_freight",
      "aggregation": "SUM",
      "expression": "SUM(business.order_items.freight_value)",
      "score": 0.98
    }
  ],
  "selected_dimensions": [
    {
      "name": "customer_state",
      "source_table": "business.customers",
      "source_column": "customer_state",
      "score": 0.95
    }
  ],
  "required_tables": [
    "business.order_items",
    "business.customers",
    "business.orders"
  ],
  "resolved_join_path": {
    "root_table": "business.order_items",
    "intermediate_tables": ["business.orders"],
    "steps": [
      {
        "from_table": "business.order_items",
        "to_table": "business.orders",
        "join_condition": "business.order_items.order_id = business.orders.order_id",
        "cardinality": "many_to_one"
      },
      {
        "from_table": "business.orders",
        "to_table": "business.customers",
        "join_condition": "business.orders.customer_id = business.customers.customer_id",
        "cardinality": "many_to_one"
      }
    ]
  },
  "is_safe_for_direct_execution": true
}
```

### Example 2: Chasm Trap Detection

**Input Question**: `"Tổng tiền hàng và tổng tiền thanh toán theo người bán"`

**Output `RetrievalResult` (Summary)**:
```json
{
  "question": "Tổng tiền hàng và tổng tiền thanh toán theo người bán",
  "status": "UNSAFE_JOIN",
  "selected_metrics": ["total_revenue", "total_payment_value"],
  "selected_dimensions": ["seller_id"],
  "required_tables": ["business.order_items", "business.payments"],
  "is_safe_for_direct_execution": false,
  "warnings": [
    "CRITICAL JOIN RESTRICTION [chasm_trap_order_items_and_payments]: Chasm Trap / Fan-out Multiplication: Both order_items and payments have 1:N cardinality from orders. Joining both tables in a single flat FROM ... JOIN query generates an M x N Cartesian row expansion per order... Remediation: Never join business.order_items and business.payments directly in the same SELECT ... JOIN clause when calculating aggregates. Always aggregate each table in an isolated subquery or CTE grouped by order_id before joining at the order level, or execute as separate analytical queries."
  ]
}
```

---

## 8. Configuration Options

Configuration options can be supplied via environment variables (`.env`) or passed directly to `RetrievalConfig`:

| Environment Variable | Default | Description |
| :--- | :--- | :--- |
| `SEMANTIC_LAYER_DIR` | `None` | Custom path to directory containing semantic YAML files. |
| `RETRIEVAL_KEYWORD_WEIGHT` | `0.6` | Weight assigned to keyword matching in hybrid ranking. |
| `RETRIEVAL_EMBEDDING_WEIGHT` | `0.4` | Weight assigned to embedding similarity in hybrid ranking. |
| `RETRIEVAL_ENABLE_EMBEDDINGS` | `False` | Activate dense embedding similarity matching. |
| `RETRIEVAL_TOP_K_METRICS` | `3` | Maximum number of candidate metrics to select. |
| `RETRIEVAL_TOP_K_DIMENSIONS` | `5` | Maximum number of candidate dimensions to select. |
| `RETRIEVAL_MIN_CONFIDENCE_SCORE` | `0.25` | Minimum score threshold for candidate selection. |
| `OPENAI_API_KEY` | `None` | API key for optional external embedding provider. |
| `EMBEDDING_MODEL` | `text-embedding-3-small` | Model name for external embedding provider. |

---

## 9. Running Tests

Unit tests verify keyword matching, Vietnamese diacritics normalization, English stemming, join path resolution, Chasm trap detection, embedding fallback, and JSON serialization:

```bash
cd backend
pytest tests/test_semantic_retrieval.py -v
```

Execute the full backend test suite:
```bash
pytest
```

Run code formatting and type checks:
```bash
ruff check
mypy app tests
```

---

## 10. Known Limitations & Analytical Assumptions

1. **Complex Intent Parsing Delegated to Agent Planner**:
   Semantic Retrieval focuses on identifying candidate metrics, dimensions, and join paths. Extracting temporal inequality ranges (e.g. `BETWEEN '2018-01-01' AND '2018-06-30'`) or categorical filters is handled by the downstream Question Understanding / Planner Agent.
2. **Portuguese Categorical Dimension Values**:
   Product category values in `business.products` are in Portuguese (e.g. `beleza_saude`). While the dimension `product_category` is retrieved accurately, mapping user mentions (e.g. "sức khỏe làm đẹp") to specific Portuguese database values is handled during filter construction.
3. **Chasm Trap Execution**:
   When `UNSAFE_JOIN` is flagged, the retrieval module informs the caller that flat `JOIN` generation is unsafe. The downstream SQL Generator must generate isolated CTEs or subqueries.
