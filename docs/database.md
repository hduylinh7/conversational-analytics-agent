# Database Architecture & Schema Documentation

> **Status Notice**: The database schema foundation has been implemented and migrated via Alembic.  
> **The Olist Brazilian E-Commerce dataset has NOT been downloaded or ingested yet.**  
> Business tables currently contain no production records. Data ingestion will be executed in a dedicated subsequent phase.

---

## 1. Purpose of Database

The database powers the **Conversational Analytics Agent**, serving three core system tiers:
1. **Business Intelligence Core (`business`)**: Houses e-commerce domain data structured to mirror the core entities of the Brazilian Olist dataset (customers, orders, items, products, payments).
2. **Application State & Audit (`app`)**: Manages conversation sessions, user accounts, chat message streams, query execution metadata/metrics, and saved analytical queries.
3. **Agent Semantic Layer (`semantic`)**: Stores metadata definitions for metrics, analytical dimensions, and data sources to support schema retrieval, prompt grounding, and safe Text-to-SQL generation.

---

## 2. PostgreSQL Schemas

The database uses three distinct schemas within PostgreSQL:

| Schema | Ownership & Lifecycle | Primary Function |
| :--- | :--- | :--- |
| `business` | E-Commerce Domain (Olist source structure) | Analytical query target for reporting, aggregations, and business metrics. |
| `app` | Conversational Agent Application | User session state, chat history, SQL audit logging, and saved queries. |
| `semantic` | Agent Semantic Catalog | Definitions of metrics, dimensions, and data sources for Text-to-SQL generation. |

---

## 3. Comprehensive Table Catalog (13 Tables)

```
Database: analytics
├── business (5 tables)
│   ├── customers
│   ├── orders
│   ├── order_items
│   ├── products
│   └── payments
├── app (5 tables)
│   ├── users
│   ├── conversations
│   ├── messages
│   ├── query_executions
│   └── saved_queries
└── semantic (3 tables)
    ├── metric_definitions
    ├── dimension_definitions
    └── data_sources
```

---

## 4. Table Specifications

### 4.1 Business Schema (`business`)

#### 1. `business.customers`
* **Purpose**: Customer profiles and geographical location attributes.
* **Source**: Olist (`olist_customers_dataset.csv`).
* **Columns**:
  * `customer_id` (`VARCHAR(32)`, PK, NOT NULL): Unique order-specific customer token.
  * `customer_unique_id` (`VARCHAR(32)`, NOT NULL): Global customer identifier across multiple orders.
  * `customer_zip_code_prefix` (`VARCHAR(5)`, nullable): 5-digit postal code prefix.
  * `customer_city` (`VARCHAR(100)`, nullable): Customer city name.
  * `customer_state` (`CHAR(2)`, nullable): Two-letter Brazilian state abbreviation (e.g. `SP`, `RJ`).
* **Cardinality**: `customers` 1 : N `orders`
* **Indexes**:
  * `ix_business_customers_customer_unique_id` on `customer_unique_id`
  * `ix_business_customers_customer_state` on `customer_state`

#### 2. `business.orders`
* **Purpose**: Core order transaction records with shipment milestone timestamps.
* **Source**: Olist (`olist_orders_dataset.csv`).
* **Columns**:
  * `order_id` (`VARCHAR(32)`, PK, NOT NULL): Unique order identifier.
  * `customer_id` (`VARCHAR(32)`, NOT NULL, FK → `business.customers.customer_id` ON DELETE CASCADE): Customer reference.
  * `order_status` (`VARCHAR(30)`, NOT NULL): Status (`delivered`, `shipped`, `canceled`, etc.).
  * `order_purchase_timestamp` (`TIMESTAMP`, NOT NULL): Purchase placement timestamp.
  * `order_approved_at` (`TIMESTAMP`, nullable): Payment approval timestamp.
  * `order_delivered_carrier_date` (`TIMESTAMP`, nullable): Carrier handover timestamp.
  * `order_delivered_customer_date` (`TIMESTAMP`, nullable): Final delivery timestamp.
  * `order_estimated_delivery_date` (`TIMESTAMP`, nullable): Estimated delivery SLA.
* **Note**: Does not include `created_at` or `updated_at` (omitted per source specification).
* **Cardinality**: `customers` 1 : N `orders`, `orders` 1 : N `order_items`, `orders` 1 : N `payments`
* **Indexes**:
  * `ix_business_orders_customer_id` on `customer_id`
  * `ix_business_orders_order_status` on `order_status`
  * `ix_business_orders_order_purchase_timestamp` on `order_purchase_timestamp`

#### 3. `business.order_items`
* **Purpose**: Order line item details with individual price, freight, and seller tracking.
* **Source**: Olist (`olist_order_items_dataset.csv`).
* **Columns**:
  * `order_id` (`VARCHAR(32)`, NOT NULL, FK → `business.orders.order_id` ON DELETE CASCADE): Order reference.
  * `order_item_id` (`INTEGER`, NOT NULL): Item sequential line number within order.
  * `product_id` (`VARCHAR(32)`, nullable, FK → `business.products.product_id` ON DELETE SET NULL): Catalog product reference.
  * `seller_id` (`VARCHAR(32)`, NOT NULL): Seller identifier (business attribute without separate sellers table).
  * `shipping_limit_date` (`TIMESTAMP`, nullable): Seller shipping deadline.
  * `price` (`NUMERIC(12,2)`, NOT NULL): Item monetary price.
  * `freight_value` (`NUMERIC(12,2)`, NOT NULL): Item freight/shipping cost.
* **Primary Key**: Composite `(order_id, order_item_id)`
* **Constraints**:
  * `chk_business_order_items_price_non_negative`: `price >= 0`
  * `chk_business_order_items_freight_value_non_negative`: `freight_value >= 0`
* **Cardinality**: `orders` 1 : N `order_items`, `products` 1 : N `order_items`
* **Indexes**:
  * `ix_business_order_items_product_id` on `product_id`
  * `ix_business_order_items_seller_id` on `seller_id`

#### 4. `business.products`
* **Purpose**: Catalog items including physical dimensions and categorization.
* **Source**: Olist (`olist_products_dataset.csv`).
* **Columns**:
  * `product_id` (`VARCHAR(32)`, PK, NOT NULL): Product identifier.
  * `product_category_name` (`VARCHAR(100)`, nullable): Primary product category in Portuguese.
  * `product_name_lenght` (`INTEGER`, nullable): Product title length (preserving Olist source spelling).
  * `product_description_lenght` (`INTEGER`, nullable): Product description length (preserving Olist source spelling).
  * `product_photos_qty` (`INTEGER`, nullable): Photo count.
  * `product_weight_g` (`NUMERIC`, nullable): Weight in grams.
  * `product_length_cm` (`NUMERIC`, nullable): Physical length in centimeters.
  * `product_height_cm` (`NUMERIC`, nullable): Physical height in centimeters.
  * `product_width_cm` (`NUMERIC`, nullable): Physical width in centimeters.
* **Cardinality**: `products` 1 : N `order_items`
* **Indexes**:
  * `ix_business_products_product_category_name` on `product_category_name`

#### 5. `business.payments`
* **Purpose**: Payment transactions, installment installments, and settlement amounts.
* **Source**: Olist (`olist_order_payments_dataset.csv`).
* **Columns**:
  * `payment_id` (`BIGSERIAL` / `BIGINT Identity`, PK, NOT NULL): Database surrogate key.
  * `order_id` (`VARCHAR(32)`, NOT NULL, FK → `business.orders.order_id` ON DELETE CASCADE): Order reference.
  * `payment_sequential` (`INTEGER`, NOT NULL): Sequence index for multi-payment orders.
  * `payment_type` (`VARCHAR(30)`, NOT NULL): Payment method (`credit_card`, `boleto`, `voucher`, `debit_card`).
  * `payment_installments` (`INTEGER`, NOT NULL): Installment count.
  * `payment_value` (`NUMERIC(12,2)`, NOT NULL): Total transaction value.
* **Constraints**:
  * `uq_business_payments_order_id_sequential`: `UNIQUE (order_id, payment_sequential)`
  * `chk_business_payments_payment_value_non_negative`: `payment_value >= 0`
  * `chk_business_payments_payment_installments_non_negative`: `payment_installments >= 0`
* **Cardinality**: `orders` 1 : N `payments`
* **Indexes**:
  * `ix_business_payments_order_id` on `order_id`
  * `ix_business_payments_payment_type` on `payment_type`

---

### 4.2 Application Schema (`app`)

#### 6. `app.users`
* **Purpose**: User identity for analyst access and workspace ownership.
* **Columns**:
  * `user_id` (`UUID`, PK, default `gen_random_uuid()`): Primary key.
  * `email` (`VARCHAR(255)`, UNIQUE, NOT NULL): User email.
  * `display_name` (`VARCHAR(100)`, nullable): Display name.
  * `role` (`VARCHAR(30)`, NOT NULL, default `'analyst'`): User role.
  * `created_at` (`TIMESTAMPTZ`, NOT NULL, default `now()`): Creation timestamp.
  * `updated_at` (`TIMESTAMPTZ`, NOT NULL, default `now()`): Last modified timestamp.
* **Cardinality**: `users` 1 : N `conversations`, `users` 1 : N `saved_queries`

#### 7. `app.conversations`
* **Purpose**: Conversational threads grouping multi-turn chat interactions.
* **Columns**:
  * `conversation_id` (`UUID`, PK, default `gen_random_uuid()`): Thread ID.
  * `user_id` (`UUID`, NOT NULL, FK → `app.users.user_id` ON DELETE CASCADE): Owner user.
  * `title` (`VARCHAR(255)`, nullable): Thread summary title.
  * `created_at` (`TIMESTAMPTZ`, NOT NULL, default `now()`)
  * `updated_at` (`TIMESTAMPTZ`, NOT NULL, default `now()`)
* **Cardinality**: `users` 1 : N `conversations`, `conversations` 1 : N `messages`, `conversations` 1 : N `query_executions`
* **Indexes**:
  * `ix_app_conversations_user_id` on `user_id`

#### 8. `app.messages`
* **Purpose**: Individual conversation turns with role and text content.
* **Columns**:
  * `message_id` (`UUID`, PK, default `gen_random_uuid()`): Message ID.
  * `conversation_id` (`UUID`, NOT NULL, FK → `app.conversations.conversation_id` ON DELETE CASCADE): Conversation reference.
  * `role` (`VARCHAR(20)`, NOT NULL): Turn participant (`user`, `assistant`, `system`).
  * `content` (`TEXT`, NOT NULL): Markdown / text body.
  * `created_at` (`TIMESTAMPTZ`, NOT NULL, default `now()`)
* **Constraints**:
  * `chk_app_messages_role`: `role IN ('user', 'assistant', 'system')`
* **Cardinality**: `conversations` 1 : N `messages`, `messages` 1 : N `query_executions`
* **Indexes**:
  * `ix_app_messages_conversation_id_created_at` on `(conversation_id, created_at)`

#### 9. `app.query_executions`
* **Purpose**: Audit logging, latency tracking, and execution metrics for generated SQL queries.
* **Columns**:
  * `execution_id` (`UUID`, PK, default `gen_random_uuid()`): Execution log ID.
  * `conversation_id` (`UUID`, nullable, FK → `app.conversations.conversation_id` ON DELETE SET NULL)
  * `message_id` (`UUID`, nullable, FK → `app.messages.message_id` ON DELETE SET NULL)
  * `natural_language_query` (`TEXT`, NOT NULL): Original user analytical prompt.
  * `generated_sql` (`TEXT`, nullable): Generated SQL statement.
  * `status` (`VARCHAR(30)`, NOT NULL): Execution status (`generated`, `blocked`, `validated`, `executing`, `succeeded`, `failed`, `repaired`).
  * `error_message` (`TEXT`, nullable): Execution or guardrail failure details.
  * `started_at` (`TIMESTAMPTZ`, nullable): Execution start.
  * `finished_at` (`TIMESTAMPTZ`, nullable): Execution end.
  * `duration_ms` (`INTEGER`, nullable): Execution duration in milliseconds.
  * `row_count` (`INTEGER`, nullable): Result row count.
  * `result_size_bytes` (`BIGINT`, nullable): Memory/wire size of output.
  * `cache_hit` (`BOOLEAN`, NOT NULL, default `false`): Cache flag.
* **Constraints**:
  * `chk_app_query_executions_status`: `status IN ('generated', 'blocked', 'validated', 'executing', 'succeeded', 'failed', 'repaired')`
* **Indexes**:
  * `ix_app_query_executions_conversation_id` on `conversation_id`
  * `ix_app_query_executions_status` on `status`
  * `ix_app_query_executions_started_at` on `started_at`

#### 10. `app.saved_queries`
* **Purpose**: Bookmarkable SQL and natural language queries saved by analysts.
* **Columns**:
  * `saved_query_id` (`UUID`, PK, default `gen_random_uuid()`): ID.
  * `user_id` (`UUID`, NOT NULL, FK → `app.users.user_id` ON DELETE CASCADE): User reference.
  * `name` (`VARCHAR(255)`, NOT NULL): Query bookmark name.
  * `description` (`TEXT`, nullable): Optional description.
  * `natural_language_query` (`TEXT`, NOT NULL): Prompt formulation.
  * `sql_query` (`TEXT`, NOT NULL): Saved SQL query string.
  * `created_at` (`TIMESTAMPTZ`, NOT NULL, default `now()`)
  * `updated_at` (`TIMESTAMPTZ`, NOT NULL, default `now()`)
* **Cardinality**: `users` 1 : N `saved_queries`
* **Indexes**:
  * `ix_app_saved_queries_user_id` on `user_id`

---

### 4.3 Semantic Schema (`semantic`)

#### 11. `semantic.metric_definitions`
* **Purpose**: Business metric definitions used by the agent during semantic planning.
* **Columns**:
  * `metric_id` (`UUID`, PK, default `gen_random_uuid()`): Metric ID.
  * `name` (`VARCHAR(100)`, UNIQUE, NOT NULL): Canonical token (e.g. `total_revenue`, `avg_delivery_days`).
  * `display_name` (`VARCHAR(255)`, NOT NULL): Human-readable name.
  * `description` (`TEXT`, NOT NULL): Detailed definition.
  * `sql_expression` (`TEXT`, NOT NULL): Standard aggregation SQL snippet.
  * `default_filters` (`JSONB`, nullable): Base filter predicate parameters.
  * `data_type` (`VARCHAR(30)`, NOT NULL): Metric value type (`numeric`, `integer`, etc.).
  * `is_active` (`BOOLEAN`, NOT NULL, default `true`): Enable flag.
  * `created_at` (`TIMESTAMPTZ`, NOT NULL, default `now()`)
  * `updated_at` (`TIMESTAMPTZ`, NOT NULL, default `now()`)

#### 12. `semantic.dimension_definitions`
* **Purpose**: Canonical dimensions used for grouping, slicing, and filtering.
* **Columns**:
  * `dimension_id` (`UUID`, PK, default `gen_random_uuid()`): Dimension ID.
  * `name` (`VARCHAR(100)`, UNIQUE, NOT NULL): Dimension token (e.g. `customer_state`, `order_status`).
  * `display_name` (`VARCHAR(255)`, NOT NULL): UI label.
  * `description` (`TEXT`, NOT NULL): Explanation.
  * `table_name` (`VARCHAR(100)`, NOT NULL): Physical database table.
  * `column_name` (`VARCHAR(100)`, NOT NULL): Physical column name.
  * `data_type` (`VARCHAR(30)`, NOT NULL): Dimension data type.
  * `is_active` (`BOOLEAN`, NOT NULL, default `true`): Enable flag.

#### 13. `semantic.data_sources`
* **Purpose**: Metadata catalog for target database connection endpoints.
* **Columns**:
  * `data_source_id` (`UUID`, PK, default `gen_random_uuid()`): Source ID.
  * `name` (`VARCHAR(100)`, UNIQUE, NOT NULL): Source name.
  * `description` (`TEXT`, nullable): Description.
  * `database_type` (`VARCHAR(30)`, NOT NULL): Engine type (e.g. `postgresql`).
  * `schema_name` (`VARCHAR(100)`, NOT NULL): Schema target.
  * `is_active` (`BOOLEAN`, NOT NULL, default `true`): Enable flag.
  * `created_at` (`TIMESTAMPTZ`, NOT NULL, default `now()`)

---

## 5. Excluded Olist Tables & Rationales

The full Olist dataset on Kaggle contains 9 CSV files. Four files were intentionally excluded from this database architecture:

1. **`olist_order_reviews_dataset.csv`**:
   * *Rationale*: Free-form review text and survey scores are high-cardinality unstructured data. They will be integrated in a later stage if sentiment analysis or NLP feedback queries are requested.
2. **`olist_geolocation_dataset.csv`**:
   * *Rationale*: Contains ~1 million spatial coordinate records for zip codes. It is large and redundant for tabular analytics; zip codes, cities, and states are already captured in `customers`.
3. **`olist_sellers_dataset.csv`**:
   * *Rationale*: In the Olist dataset, `seller_id` is an operational business attribute on `order_items`. A dedicated `sellers` table adds unnecessary join overhead without providing critical metrics beyond zip code and city.
4. **`product_category_name_translation.csv`**:
   * *Rationale*: Category names in `business.products` remain in their source Portuguese names (`product_category_name`), avoiding unnecessary lookup table joins during SQL generation. Translation mappings can be resolved in the semantic layer if needed.

---

## 6. Future Olist Ingestion Plan

When the dataset is added in the next project phase:
1. The user will download the official Olist CSV files from Kaggle and place the required five files into `data/raw/`:
   * `olist_customers_dataset.csv`
   * `olist_orders_dataset.csv`
   * `olist_order_items_dataset.csv`
   * `olist_products_dataset.csv`
   * `olist_order_payments_dataset.csv`
2. Dedicated ingestion pipeline scripts will:
   * Validate column presence, nullability, and data types without mutating schema.
   * Strip invalid rows or format date timestamps.
   * Insert data in strict foreign-key dependency order:
     `customers` → `orders` → `products` → `order_items` & `payments`.
   * Enforce transaction commit and log row ingestion counts.
