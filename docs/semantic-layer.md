# Semantic Layer Architecture & Reference Documentation

The **Semantic Layer** serves as the single source of truth for business metrics, analytical dimensions, and safe database relationships. It bridges raw relational database schemas with analytical questions asked by business users and conversational agents.

---

## 1. Purpose of the Semantic Layer

In a Text-to-SQL system, generating queries directly from raw physical database catalogs introduces common failure modes:
1. **Ambiguous Business Metrics**: Users ask for "revenue", but is it merchandise revenue excluding freight, gross revenue including freight, or collected payment amounts?
2. **Chasm Traps & Fan-out Multiplications**: Joining multiple one-to-many tables (e.g. `orders` → `order_items` and `orders` → `payments`) multiplies row counts, silently distorting `SUM(...)` and `COUNT(...)` calculations.
3. **Multilingual Discrepancies**: Business analysts ask questions in Vietnamese or English, while physical catalog schemas use Portuguese or English table and column names.
4. **Ad-hoc Filters & Hardcoded Business Logic**: Filtering out canceled orders, using `customer_unique_id` instead of `customer_id` for customer counts, or knowing that `product_category_name` is stored in Portuguese.

The Semantic Layer standardizes these business rules in machine-readable YAML files, validated by Pydantic models and verified against actual SQLAlchemy schemas.

---

## 2. Directory & File Structure

All semantic definitions, schemas, and loader/validation utilities reside in `backend/app/semantic/`:

```text
backend/app/semantic/
├── __init__.py           # Package exports for models, loaders, and validators
├── data_sources.yaml     # Tables, columns, primary/foreign keys, joins, and restrictions
├── dimensions.yaml       # Filterable and groupable dimensions with multilingual synonyms
├── metrics.yaml          # Business metrics, SQL expressions, aggregations, and warnings
├── models.py             # Pydantic V2 schema definitions for validation
├── loader.py             # YAML reading, error handling, and catalog instantiation
└── validator.py          # Deep referential integrity, chasm trap checking, and SQLAlchemy verification
```

---

## 3. Structure and Meaning of Each YAML File

### 3.1 `data_sources.yaml`

Catalog of target database tables, columns, explicit join relationships, and join restrictions.

#### Table Definition Example:
```yaml
tables:
  - name: "business.order_items"
    alias: "oi"
    description: "Line items contained within orders, recording item price, freight allocation, and seller attribution."
    primary_key:
      - "order_id"
      - "order_item_id"
    foreign_keys:
      - columns: ["order_id"]
        referenced_table: "business.orders"
        referenced_columns: ["order_id"]
        on_delete: "CASCADE"
      - columns: ["product_id"]
        referenced_table: "business.products"
        referenced_columns: ["product_id"]
        on_delete: "SET NULL"
    columns:
      - name: "order_id"
        data_type: "VARCHAR(32)"
        description: "Foreign key referencing business.orders(order_id)."
        nullable: false
        is_primary_key: true
      - name: "price"
        data_type: "NUMERIC(12,2)"
        description: "Monetary price of single merchandise item (excluding freight)."
        nullable: false
        is_primary_key: false
```

#### Relationship Definition Example:
```yaml
relationships:
  - name: "orders_to_order_items"
    source_table: "business.orders"
    target_table: "business.order_items"
    cardinality: "one_to_many"
    join_type: "INNER"
    join_condition: "business.orders.order_id = business.order_items.order_id"
    description: "An order contains 1 or more line items."
    is_safe_for_aggregation: true
    warning: "Multiplying order-level metrics: Order counts require COUNT(DISTINCT orders.order_id)."
```

#### Join Restriction & Chasm Trap Rule:
```yaml
join_restrictions:
  - name: "chasm_trap_order_items_and_payments"
    forbidden_concurrent_joins:
      - "business.order_items"
      - "business.payments"
    severity: "CRITICAL"
    reason: "Both order_items and payments have 1:N cardinality from orders. Joining both tables in a single flat FROM ... JOIN query creates an M x N Cartesian row expansion per order, multiplying item price sums by payment counts and payment sums by item counts. DISTINCT does NOT fix sums because distinct amounts drop genuine identical item prices."
    remediation: "Never join business.order_items and business.payments directly in the same SELECT ... JOIN clause. Aggregate each table in isolated CTEs/subqueries grouped by order_id before joining at the order level, or execute as separate queries."
```

---

### 3.2 `dimensions.yaml`

Attributes that users can use to slice, filter, or group metrics.

Each dimension defines:
* `name`: Stable unique identifier.
* `description`: Business explanation.
* `synonyms`: Multilingual phrases (`vi` for Vietnamese, `en` for English).
* `source_table`: Physical table (e.g. `business.orders`).
* `source_column`: Physical column (e.g. `order_purchase_timestamp`).
* `data_type`: SQL data type.
* `filterable` / `groupable`: Boolean flags.
* `transformations`: Recommended SQL expressions (e.g. `DATE(...)`, `EXTRACT(YEAR FROM ...)`).

Example:
```yaml
dimensions:
  - name: "order_date"
    description: "Calendar date and timestamp when the customer placed the order."
    synonyms:
      vi:
        - "ngày đặt hàng"
        - "ngày mua"
        - "năm đặt hàng"
      en:
        - "order date"
        - "purchase date"
        - "order year"
    source_table: "business.orders"
    source_column: "order_purchase_timestamp"
    data_type: "TIMESTAMP"
    filterable: true
    groupable: true
    transformations:
      - name: "year"
        expression: "EXTRACT(YEAR FROM business.orders.order_purchase_timestamp)::INTEGER"
        description: "Extract 4-digit calendar year"
```

---

### 3.3 `metrics.yaml`

Quantifiable business indicators calculated via SQL aggregations.

Each metric defines:
* `name`: Stable unique identifier.
* `description`: Clear business definition explicitly distinguishing nuances.
* `synonyms`: Multilingual keywords (`vi` and `en`).
* `expression`: Canonical SQL aggregation snippet.
* `source_tables`: Tables required for the metric.
* `aggregation`: Method (`SUM`, `COUNT DISTINCT`, `COUNT`, `AVG`, `RATIO`).
* `join_requirements`: Any specific table relationships needed.
* `filters`: Predefined filters / mandatory constraints.
* `warnings`: Double-counting or fan-out warnings.

#### Distinction Between Revenue Types:
* **`total_revenue`** = `SUM(business.order_items.price)`: Pure merchandise catalog revenue excluding shipping charges.
* **`total_gross_revenue`** = `SUM(business.order_items.price + business.order_items.freight_value)`: Total customer gross order value (merchandise + freight).
* **`total_freight`** = `SUM(business.order_items.freight_value)`: Freight/shipping logistics revenue.
* **`total_payment_value`** = `SUM(business.payments.payment_value)`: Settled transaction amount from payments (may differ due to vouchers or credit surcharges).

---

## 4. Analytical Question Mapping Example

### Question:
> *"What was the merchandise revenue by product category in 2018?"*  
> *(Vietnamese: "Tổng doanh thu hàng hóa theo danh mục sản phẩm trong năm 2018 là bao nhiêu?")*

### Semantic Layer Resolution:
1. **Target Metric**:
   * Resolved Metric: `total_revenue`
   * Aggregation: `SUM(business.order_items.price)`
   * Source Table: `business.order_items`
2. **Grouping Dimension**:
   * Resolved Dimension: `product_category`
   * Source Table: `business.products`
   * Source Column: `product_category_name`
3. **Filter Dimension**:
   * Resolved Dimension: `order_date`
   * Source Table: `business.orders`
   * Source Column: `order_purchase_timestamp`
   * Transformation: `EXTRACT(YEAR FROM business.orders.order_purchase_timestamp) = 2018`
4. **Required Table Relationships**:
   * `order_items_to_products`: `business.order_items` LEFT JOIN `business.products` ON `order_items.product_id = products.product_id`
   * `order_items_to_orders`: `business.order_items` INNER JOIN `business.orders` ON `order_items.order_id = orders.order_id`
5. **Safety Check**:
   * Candidate Tables: `business.order_items`, `business.products`, `business.orders`.
   * Does NOT involve `business.payments`. No chasm trap violation.
6. **Resulting Safe SQL**:
```sql
SELECT
    p.product_category_name AS product_category,
    SUM(oi.price) AS total_revenue
FROM business.order_items AS oi
LEFT JOIN business.products AS p
    ON oi.product_id = p.product_id
INNER JOIN business.orders AS o
    ON oi.order_id = o.order_id
WHERE EXTRACT(YEAR FROM o.order_purchase_timestamp) = 2018
GROUP BY p.product_category_name
ORDER BY total_revenue DESC;
```

---

## 5. How to Add a New Metric or Dimension

### Adding a Metric to `metrics.yaml`:
1. Check that the metric's source columns exist in `data_sources.yaml`.
2. Add a new block in `backend/app/semantic/metrics.yaml`:
   ```yaml
   - name: "my_new_metric"
     description: "Clear business definition explaining what is included and excluded."
     synonyms:
       vi: ["thuật ngữ tiếng việt"]
       en: ["english term"]
     expression: "SUM(business.table.column)"
     source_tables:
       - "business.table"
     aggregation: "SUM"
     join_requirements: []
     filters: []
     warnings:
       - "Any relevant calculation caveat."
   ```
3. Run the validation suite to verify syntax and references:
   ```bash
   pytest backend/tests/test_semantic_layer.py -v
   ```

### Adding a Dimension to `dimensions.yaml`:
1. Check that the dimension's table and column exist in `data_sources.yaml`.
2. Add a new block in `backend/app/semantic/dimensions.yaml`:
   ```yaml
   - name: "my_new_dimension"
     description: "Business attribute explanation."
     synonyms:
       vi: ["tên gọi tiếng việt"]
       en: ["english synonym"]
     source_table: "business.table_name"
     source_column: "column_name"
     data_type: "VARCHAR(50)"
     filterable: true
     groupable: true
   ```
3. Run validation tests:
   ```bash
   pytest backend/tests/test_semantic_layer.py -v
   ```

---

## 6. How to Run Semantic Layer Validation and Tests

```bash
cd backend

# Run all semantic tests
pytest tests/test_semantic_layer.py -v

# Run the complete backend test suite
pytest tests -v

# Check linter and code formatting
ruff check .

# Static type verification
mypy app tests
```

---

## 7. Known Business Assumptions & Unresolved Ambiguities

1. **`customer_id` vs. `customer_unique_id`**:
   * In Olist, `customer_id` is an order-scoped surrogate key generated per transaction.
   * `customer_unique_id` tracks the individual person across repeat purchases.
   * **Assumption**: Metric `customer_count` aggregates `COUNT(DISTINCT customer_unique_id)`.
2. **Product Category Language**:
   * Values in `business.products.product_category_name` are in Portuguese (e.g. `cama_mesa_banho`).
   * The Kaggle translation dataset was intentionally excluded during database design to avoid extra join overhead.
   * **Assumption**: Categorical filtering matches Portuguese category values, or will be resolved via semantic retrieval mappings in future agent stages.
3. **Payments vs. Order Items Discrepancy**:
   * In e-commerce, total payments for an order do not always exactly equal merchandise price + freight due to promotional vouchers or installment surcharges.
   * **Assumption**: `total_revenue` strictly tracks merchandise sold; `total_payment_value` strictly tracks payment collections. They must not be conflated.
4. **Order Status for Revenue Recognition**:
   * Revenue can be calculated across all orders, or restricted to completed (`delivered`) orders.
   * **Assumption**: `total_revenue` by default aggregates all placed item records. For realized sales, the agent should combine with `order_status = 'delivered'` or use `delivered_order_count`.

---

## 8. Semantic Retrieval Module

For details on the retrieval component that selects metrics, dimensions, tables, and join rules for user queries, see the [Semantic Retrieval Architecture & Reference](file:///d:/conversational-analytics-agent/docs/semantic-retrieval.md).

