"""Initial database schema migration creating business, app, and semantic schemas.

Revision ID: 0001_initial_schema
Revises:
Create Date: 2026-10-07 21:45:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0001_initial_schema"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1. Create PostgreSQL schemas
    op.execute("CREATE SCHEMA IF NOT EXISTS business")
    op.execute("CREATE SCHEMA IF NOT EXISTS app")
    op.execute("CREATE SCHEMA IF NOT EXISTS semantic")

    # ==========================================
    # 2. APP SCHEMA TABLES
    # ==========================================

    # app.users
    op.create_table(
        "users",
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("email", sa.VARCHAR(length=255), nullable=False),
        sa.Column("display_name", sa.VARCHAR(length=100), nullable=True),
        sa.Column(
            "role",
            sa.VARCHAR(length=30),
            server_default=sa.text("'analyst'"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("user_id", name="pk_app_users"),
        sa.UniqueConstraint("email", name="uq_app_users_email"),
        schema="app",
    )

    # app.conversations
    op.create_table(
        "conversations",
        sa.Column(
            "conversation_id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("title", sa.VARCHAR(length=255), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["app.users.user_id"],
            name="fk_app_conversations_user_id",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("conversation_id", name="pk_app_conversations"),
        schema="app",
    )
    op.create_index(
        "ix_app_conversations_user_id",
        "conversations",
        ["user_id"],
        unique=False,
        schema="app",
    )

    # app.messages
    op.create_table(
        "messages",
        sa.Column(
            "message_id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("role", sa.VARCHAR(length=20), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "role IN ('user', 'assistant', 'system')",
            name="chk_app_messages_role",
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["app.conversations.conversation_id"],
            name="fk_app_messages_conversation_id",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("message_id", name="pk_app_messages"),
        schema="app",
    )
    op.create_index(
        "ix_app_messages_conversation_id_created_at",
        "messages",
        ["conversation_id", "created_at"],
        unique=False,
        schema="app",
    )

    # app.query_executions
    op.create_table(
        "query_executions",
        sa.Column(
            "execution_id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("message_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("natural_language_query", sa.Text(), nullable=False),
        sa.Column("generated_sql", sa.Text(), nullable=True),
        sa.Column("status", sa.VARCHAR(length=30), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("row_count", sa.Integer(), nullable=True),
        sa.Column("result_size_bytes", sa.BigInteger(), nullable=True),
        sa.Column(
            "cache_hit",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('generated', 'blocked', 'validated', 'executing', 'succeeded', 'failed', 'repaired')",
            name="chk_app_query_executions_status",
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["app.conversations.conversation_id"],
            name="fk_app_query_executions_conversation_id",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["message_id"],
            ["app.messages.message_id"],
            name="fk_app_query_executions_message_id",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("execution_id", name="pk_app_query_executions"),
        schema="app",
    )
    op.create_index(
        "ix_app_query_executions_conversation_id",
        "query_executions",
        ["conversation_id"],
        unique=False,
        schema="app",
    )
    op.create_index(
        "ix_app_query_executions_status",
        "query_executions",
        ["status"],
        unique=False,
        schema="app",
    )
    op.create_index(
        "ix_app_query_executions_started_at",
        "query_executions",
        ["started_at"],
        unique=False,
        schema="app",
    )

    # app.saved_queries
    op.create_table(
        "saved_queries",
        sa.Column(
            "saved_query_id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.VARCHAR(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("natural_language_query", sa.Text(), nullable=False),
        sa.Column("sql_query", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["app.users.user_id"],
            name="fk_app_saved_queries_user_id",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("saved_query_id", name="pk_app_saved_queries"),
        schema="app",
    )
    op.create_index(
        "ix_app_saved_queries_user_id",
        "saved_queries",
        ["user_id"],
        unique=False,
        schema="app",
    )

    # ==========================================
    # 3. BUSINESS SCHEMA TABLES
    # ==========================================

    # business.customers
    op.create_table(
        "customers",
        sa.Column("customer_id", sa.VARCHAR(length=32), nullable=False),
        sa.Column("customer_unique_id", sa.VARCHAR(length=32), nullable=False),
        sa.Column("customer_zip_code_prefix", sa.VARCHAR(length=5), nullable=True),
        sa.Column("customer_city", sa.VARCHAR(length=100), nullable=True),
        sa.Column("customer_state", sa.CHAR(length=2), nullable=True),
        sa.PrimaryKeyConstraint("customer_id", name="pk_business_customers"),
        schema="business",
    )
    op.create_index(
        "ix_business_customers_customer_unique_id",
        "customers",
        ["customer_unique_id"],
        unique=False,
        schema="business",
    )
    op.create_index(
        "ix_business_customers_customer_state",
        "customers",
        ["customer_state"],
        unique=False,
        schema="business",
    )

    # business.orders
    op.create_table(
        "orders",
        sa.Column("order_id", sa.VARCHAR(length=32), nullable=False),
        sa.Column("customer_id", sa.VARCHAR(length=32), nullable=False),
        sa.Column("order_status", sa.VARCHAR(length=30), nullable=False),
        sa.Column("order_purchase_timestamp", sa.DateTime(), nullable=False),
        sa.Column("order_approved_at", sa.DateTime(), nullable=True),
        sa.Column("order_delivered_carrier_date", sa.DateTime(), nullable=True),
        sa.Column("order_delivered_customer_date", sa.DateTime(), nullable=True),
        sa.Column("order_estimated_delivery_date", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(
            ["customer_id"],
            ["business.customers.customer_id"],
            name="fk_business_orders_customer_id",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("order_id", name="pk_business_orders"),
        schema="business",
    )
    op.create_index(
        "ix_business_orders_customer_id",
        "orders",
        ["customer_id"],
        unique=False,
        schema="business",
    )
    op.create_index(
        "ix_business_orders_order_status",
        "orders",
        ["order_status"],
        unique=False,
        schema="business",
    )
    op.create_index(
        "ix_business_orders_order_purchase_timestamp",
        "orders",
        ["order_purchase_timestamp"],
        unique=False,
        schema="business",
    )

    # business.products
    op.create_table(
        "products",
        sa.Column("product_id", sa.VARCHAR(length=32), nullable=False),
        sa.Column("product_category_name", sa.VARCHAR(length=100), nullable=True),
        sa.Column("product_name_lenght", sa.Integer(), nullable=True),
        sa.Column("product_description_lenght", sa.Integer(), nullable=True),
        sa.Column("product_photos_qty", sa.Integer(), nullable=True),
        sa.Column("product_weight_g", sa.Numeric(), nullable=True),
        sa.Column("product_length_cm", sa.Numeric(), nullable=True),
        sa.Column("product_height_cm", sa.Numeric(), nullable=True),
        sa.Column("product_width_cm", sa.Numeric(), nullable=True),
        sa.PrimaryKeyConstraint("product_id", name="pk_business_products"),
        schema="business",
    )
    op.create_index(
        "ix_business_products_product_category_name",
        "products",
        ["product_category_name"],
        unique=False,
        schema="business",
    )

    # business.order_items (Composite PK: order_id, order_item_id)
    op.create_table(
        "order_items",
        sa.Column("order_id", sa.VARCHAR(length=32), nullable=False),
        sa.Column("order_item_id", sa.Integer(), nullable=False),
        sa.Column("product_id", sa.VARCHAR(length=32), nullable=True),
        sa.Column("seller_id", sa.VARCHAR(length=32), nullable=False),
        sa.Column("shipping_limit_date", sa.DateTime(), nullable=True),
        sa.Column("price", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("freight_value", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.CheckConstraint(
            "price >= 0",
            name="chk_business_order_items_price_non_negative",
        ),
        sa.CheckConstraint(
            "freight_value >= 0",
            name="chk_business_order_items_freight_value_non_negative",
        ),
        sa.ForeignKeyConstraint(
            ["order_id"],
            ["business.orders.order_id"],
            name="fk_business_order_items_order_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["product_id"],
            ["business.products.product_id"],
            name="fk_business_order_items_product_id",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("order_id", "order_item_id", name="pk_business_order_items"),
        schema="business",
    )
    op.create_index(
        "ix_business_order_items_product_id",
        "order_items",
        ["product_id"],
        unique=False,
        schema="business",
    )
    op.create_index(
        "ix_business_order_items_seller_id",
        "order_items",
        ["seller_id"],
        unique=False,
        schema="business",
    )

    # business.payments
    op.create_table(
        "payments",
        sa.Column(
            "payment_id",
            sa.BigInteger(),
            sa.Identity(start=1),
            nullable=False,
        ),
        sa.Column("order_id", sa.VARCHAR(length=32), nullable=False),
        sa.Column("payment_sequential", sa.Integer(), nullable=False),
        sa.Column("payment_type", sa.VARCHAR(length=30), nullable=False),
        sa.Column("payment_installments", sa.Integer(), nullable=False),
        sa.Column("payment_value", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.CheckConstraint(
            "payment_value >= 0",
            name="chk_business_payments_payment_value_non_negative",
        ),
        sa.CheckConstraint(
            "payment_installments >= 0",
            name="chk_business_payments_payment_installments_non_negative",
        ),
        sa.ForeignKeyConstraint(
            ["order_id"],
            ["business.orders.order_id"],
            name="fk_business_payments_order_id",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("payment_id", name="pk_business_payments"),
        sa.UniqueConstraint(
            "order_id",
            "payment_sequential",
            name="uq_business_payments_order_id_sequential",
        ),
        schema="business",
    )
    op.create_index(
        "ix_business_payments_order_id",
        "payments",
        ["order_id"],
        unique=False,
        schema="business",
    )
    op.create_index(
        "ix_business_payments_payment_type",
        "payments",
        ["payment_type"],
        unique=False,
        schema="business",
    )

    # ==========================================
    # 4. SEMANTIC SCHEMA TABLES
    # ==========================================

    # semantic.metric_definitions
    op.create_table(
        "metric_definitions",
        sa.Column(
            "metric_id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("name", sa.VARCHAR(length=100), nullable=False),
        sa.Column("display_name", sa.VARCHAR(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("sql_expression", sa.Text(), nullable=False),
        sa.Column("default_filters", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("data_type", sa.VARCHAR(length=30), nullable=False),
        sa.Column(
            "is_active",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("metric_id", name="pk_semantic_metric_definitions"),
        sa.UniqueConstraint("name", name="uq_semantic_metric_definitions_name"),
        schema="semantic",
    )

    # semantic.dimension_definitions
    op.create_table(
        "dimension_definitions",
        sa.Column(
            "dimension_id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("name", sa.VARCHAR(length=100), nullable=False),
        sa.Column("display_name", sa.VARCHAR(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("table_name", sa.VARCHAR(length=100), nullable=False),
        sa.Column("column_name", sa.VARCHAR(length=100), nullable=False),
        sa.Column("data_type", sa.VARCHAR(length=30), nullable=False),
        sa.Column(
            "is_active",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("dimension_id", name="pk_semantic_dimension_definitions"),
        sa.UniqueConstraint("name", name="uq_semantic_dimension_definitions_name"),
        schema="semantic",
    )

    # semantic.data_sources
    op.create_table(
        "data_sources",
        sa.Column(
            "data_source_id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("name", sa.VARCHAR(length=100), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("database_type", sa.VARCHAR(length=30), nullable=False),
        sa.Column("schema_name", sa.VARCHAR(length=100), nullable=False),
        sa.Column(
            "is_active",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("data_source_id", name="pk_semantic_data_sources"),
        sa.UniqueConstraint("name", name="uq_semantic_data_sources_name"),
        schema="semantic",
    )


def downgrade() -> None:
    # Drop tables in reverse dependency order

    # Semantic
    op.drop_table("data_sources", schema="semantic")
    op.drop_table("dimension_definitions", schema="semantic")
    op.drop_table("metric_definitions", schema="semantic")

    # Business
    op.drop_index(
        "ix_business_payments_payment_type",
        table_name="payments",
        schema="business",
    )
    op.drop_index(
        "ix_business_payments_order_id",
        table_name="payments",
        schema="business",
    )
    op.drop_table("payments", schema="business")

    op.drop_index(
        "ix_business_order_items_seller_id",
        table_name="order_items",
        schema="business",
    )
    op.drop_index(
        "ix_business_order_items_product_id",
        table_name="order_items",
        schema="business",
    )
    op.drop_table("order_items", schema="business")

    op.drop_index(
        "ix_business_products_product_category_name",
        table_name="products",
        schema="business",
    )
    op.drop_table("products", schema="business")

    op.drop_index(
        "ix_business_orders_order_purchase_timestamp",
        table_name="orders",
        schema="business",
    )
    op.drop_index(
        "ix_business_orders_order_status",
        table_name="orders",
        schema="business",
    )
    op.drop_index(
        "ix_business_orders_customer_id",
        table_name="orders",
        schema="business",
    )
    op.drop_table("orders", schema="business")

    op.drop_index(
        "ix_business_customers_customer_state",
        table_name="customers",
        schema="business",
    )
    op.drop_index(
        "ix_business_customers_customer_unique_id",
        table_name="customers",
        schema="business",
    )
    op.drop_table("customers", schema="business")

    # App
    op.drop_index(
        "ix_app_saved_queries_user_id",
        table_name="saved_queries",
        schema="app",
    )
    op.drop_table("saved_queries", schema="app")

    op.drop_index(
        "ix_app_query_executions_started_at",
        table_name="query_executions",
        schema="app",
    )
    op.drop_index(
        "ix_app_query_executions_status",
        table_name="query_executions",
        schema="app",
    )
    op.drop_index(
        "ix_app_query_executions_conversation_id",
        table_name="query_executions",
        schema="app",
    )
    op.drop_table("query_executions", schema="app")

    op.drop_index(
        "ix_app_messages_conversation_id_created_at",
        table_name="messages",
        schema="app",
    )
    op.drop_table("messages", schema="app")

    op.drop_index(
        "ix_app_conversations_user_id",
        table_name="conversations",
        schema="app",
    )
    op.drop_table("conversations", schema="app")

    op.drop_table("users", schema="app")

    # Drop schemas
    op.execute("DROP SCHEMA IF EXISTS semantic CASCADE")
    op.execute("DROP SCHEMA IF EXISTS business CASCADE")
    op.execute("DROP SCHEMA IF EXISTS app CASCADE")
