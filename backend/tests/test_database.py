"""Database schema, constraints, relationships, and migration tests."""

from datetime import datetime
from decimal import Decimal

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.postgres import check_postgres_connection
from app.models.application import Conversation, Message, QueryExecution, SavedQuery, User
from app.models.business import Customer, Order, OrderItem, Payment, Product
from app.models.semantic import DataSource, DimensionDefinition, MetricDefinition
from tests.conftest import test_engine


@pytest.mark.asyncio
async def test_postgres_connection_works() -> None:
    """1. Verify PostgreSQL connection check and basic ping query work."""
    is_connected = await check_postgres_connection()
    assert is_connected is True

    async with test_engine.connect() as conn:
        result = await conn.execute(text("SELECT 1"))
        assert result.scalar() == 1


@pytest.mark.asyncio
async def test_schemas_exist() -> None:
    """2-4. Verify business, app, and semantic schemas exist."""
    async with test_engine.connect() as conn:
        result = await conn.execute(
            text(
                "SELECT schema_name FROM information_schema.schemata "
                "WHERE schema_name IN ('business', 'app', 'semantic') "
                "ORDER BY schema_name;"
            )
        )
        schemas = {row[0] for row in result.fetchall()}
        assert "business" in schemas
        assert "app" in schemas
        assert "semantic" in schemas


@pytest.mark.asyncio
async def test_all_13_tables_exist() -> None:
    """5. Verify all 13 tables exist in their designated schemas."""
    expected_tables = {
        "business": {"customers", "orders", "order_items", "products", "payments"},
        "app": {"users", "conversations", "messages", "query_executions", "saved_queries"},
        "semantic": {"metric_definitions", "dimension_definitions", "data_sources"},
    }

    async with test_engine.connect() as conn:
        result = await conn.execute(
            text(
                "SELECT table_schema, table_name FROM information_schema.tables "
                "WHERE table_schema IN ('business', 'app', 'semantic') "
                "ORDER BY table_schema, table_name;"
            )
        )
        found_tables: dict[str, set[str]] = {"business": set(), "app": set(), "semantic": set()}
        for schema_name, table_name in result.fetchall():
            if schema_name in found_tables:
                found_tables[schema_name].add(table_name)

        assert found_tables == expected_tables
        total_tables = sum(len(tables) for tables in found_tables.values())
        assert total_tables == 13


@pytest.mark.asyncio
async def test_primary_keys_exist() -> None:
    """6. Verify all 13 tables have a defined primary key constraint."""
    async with test_engine.connect() as conn:
        result = await conn.execute(
            text(
                "SELECT table_schema, table_name "
                "FROM information_schema.table_constraints "
                "WHERE constraint_type = 'PRIMARY KEY' "
                "  AND table_schema IN ('business', 'app', 'semantic');"
            )
        )
        pk_tables = {(row[0], row[1]) for row in result.fetchall()}
        assert len(pk_tables) == 13


@pytest.mark.asyncio
async def test_order_items_composite_primary_key() -> None:
    """8. Verify business.order_items has a composite PK: (order_id, order_item_id)."""
    async with test_engine.connect() as conn:
        result = await conn.execute(
            text(
                "SELECT kcu.column_name, kcu.ordinal_position "
                "FROM information_schema.table_constraints tc "
                "JOIN information_schema.key_column_usage kcu "
                "  ON tc.constraint_name = kcu.constraint_name "
                "  AND tc.table_schema = kcu.table_schema "
                "WHERE tc.constraint_type = 'PRIMARY KEY' "
                "  AND tc.table_schema = 'business' "
                "  AND tc.table_name = 'order_items' "
                "ORDER BY kcu.ordinal_position;"
            )
        )
        pk_columns = [row[0] for row in result.fetchall()]
        assert pk_columns == ["order_id", "order_item_id"]


@pytest.mark.asyncio
async def test_payments_unique_constraint() -> None:
    """9. Verify business.payments has UNIQUE(order_id, payment_sequential)."""
    async with test_engine.connect() as conn:
        result = await conn.execute(
            text(
                "SELECT kcu.column_name "
                "FROM information_schema.table_constraints tc "
                "JOIN information_schema.key_column_usage kcu "
                "  ON tc.constraint_name = kcu.constraint_name "
                "  AND tc.table_schema = kcu.table_schema "
                "WHERE tc.constraint_type = 'UNIQUE' "
                "  AND tc.table_schema = 'business' "
                "  AND tc.table_name = 'payments' "
                "ORDER BY kcu.ordinal_position;"
            )
        )
        unique_cols = [row[0] for row in result.fetchall()]
        assert "order_id" in unique_cols
        assert "payment_sequential" in unique_cols


@pytest.mark.asyncio
async def test_foreign_keys_exist() -> None:
    """7. Verify foreign key constraints are established across tables."""
    async with test_engine.connect() as conn:
        result = await conn.execute(
            text(
                "SELECT tc.table_schema, tc.table_name, kcu.column_name, ccu.table_name AS ref_table "
                "FROM information_schema.table_constraints tc "
                "JOIN information_schema.key_column_usage kcu "
                "  ON tc.constraint_name = kcu.constraint_name "
                "  AND tc.table_schema = kcu.table_schema "
                "JOIN information_schema.constraint_column_usage ccu "
                "  ON ccu.constraint_name = tc.constraint_name "
                "WHERE tc.constraint_type = 'FOREIGN KEY' "
                "  AND tc.table_schema IN ('business', 'app');"
            )
        )
        fks = [(row[0], row[1], row[2], row[3]) for row in result.fetchall()]

        # Business foreign keys
        assert ("business", "orders", "customer_id", "customers") in fks
        assert ("business", "order_items", "order_id", "orders") in fks
        assert ("business", "order_items", "product_id", "products") in fks
        assert ("business", "payments", "order_id", "orders") in fks

        # App foreign keys
        assert ("app", "conversations", "user_id", "users") in fks
        assert ("app", "messages", "conversation_id", "conversations") in fks
        assert ("app", "saved_queries", "user_id", "users") in fks
        assert ("app", "query_executions", "conversation_id", "conversations") in fks
        assert ("app", "query_executions", "message_id", "messages") in fks


@pytest.mark.asyncio
async def test_money_fields_use_numeric() -> None:
    """10. Verify monetary columns in business schema use NUMERIC(12,2)."""
    async with test_engine.connect() as conn:
        result = await conn.execute(
            text(
                "SELECT table_name, column_name, data_type, numeric_precision, numeric_scale "
                "FROM information_schema.columns "
                "WHERE table_schema = 'business' "
                "  AND column_name IN ('price', 'freight_value', 'payment_value');"
            )
        )
        rows = {f"{r[0]}.{r[1]}": (r[2], r[3], r[4]) for r in result.fetchall()}
        assert rows["order_items.price"] == ("numeric", 12, 2)
        assert rows["order_items.freight_value"] == ("numeric", 12, 2)
        assert rows["payments.payment_value"] == ("numeric", 12, 2)


@pytest.mark.asyncio
async def test_check_constraints_reject_negative_money(db_session: AsyncSession) -> None:
    """11. Verify CHECK constraints reject negative money and installment values."""
    # Setup parent records for test
    customer = Customer(
        customer_id="cust_test_neg",
        customer_unique_id="cust_uniq_neg",
    )
    order = Order(
        order_id="order_test_neg",
        customer_id="cust_test_neg",
        order_status="created",
        order_purchase_timestamp=datetime.now(),
    )
    product = Product(product_id="prod_test_neg")

    db_session.add_all([customer, order, product])
    await db_session.flush()

    # Test negative price in order_items
    invalid_item = OrderItem(
        order_id="order_test_neg",
        order_item_id=1,
        product_id="prod_test_neg",
        seller_id="seller_test",
        price=Decimal("-10.00"),
        freight_value=Decimal("5.00"),
    )
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            db_session.add(invalid_item)
            await db_session.flush()

    # Test negative freight_value
    invalid_freight = OrderItem(
        order_id="order_test_neg",
        order_item_id=2,
        product_id="prod_test_neg",
        seller_id="seller_test",
        price=Decimal("10.00"),
        freight_value=Decimal("-5.00"),
    )
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            db_session.add(invalid_freight)
            await db_session.flush()

    # Test negative payment_value in payments
    invalid_payment = Payment(
        order_id="order_test_neg",
        payment_sequential=1,
        payment_type="credit_card",
        payment_installments=1,
        payment_value=Decimal("-50.00"),
    )
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            db_session.add(invalid_payment)
            await db_session.flush()

    # Test negative installments in payments
    invalid_installments = Payment(
        order_id="order_test_neg",
        payment_sequential=2,
        payment_type="credit_card",
        payment_installments=-1,
        payment_value=Decimal("50.00"),
    )
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            db_session.add(invalid_installments)
            await db_session.flush()


@pytest.mark.asyncio
async def test_check_constraints_role_and_status(db_session: AsyncSession) -> None:
    """Verify CHECK constraints enforce valid message roles and query execution statuses."""
    user = User(email="role_status_test@example.com")
    db_session.add(user)
    await db_session.flush()

    conversation = Conversation(user_id=user.user_id, title="Test thread")
    db_session.add(conversation)
    await db_session.flush()

    # Invalid message role
    invalid_message = Message(
        conversation_id=conversation.conversation_id,
        role="hacker",
        content="Hello",
    )
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            db_session.add(invalid_message)
            await db_session.flush()

    # Invalid query execution status
    invalid_execution = QueryExecution(
        conversation_id=conversation.conversation_id,
        natural_language_query="test query",
        status="arbitrary_invalid_status",
    )
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            db_session.add(invalid_execution)
            await db_session.flush()


@pytest.mark.asyncio
async def test_foreign_key_constraints_enforced(db_session: AsyncSession) -> None:
    """13. Verify foreign key constraints reject records with nonexistent parents."""
    # Attempt to insert order with non-existent customer
    orphan_order = Order(
        order_id="order_orphan",
        customer_id="nonexistent_customer",
        order_status="pending",
        order_purchase_timestamp=datetime.now(),
    )
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            db_session.add(orphan_order)
            await db_session.flush()


@pytest.mark.asyncio
async def test_basic_insert_and_query_business(db_session: AsyncSession) -> None:
    """12. Verify basic insert, relational joins, and queries work in business schema."""
    customer = Customer(
        customer_id="c_synth_1",
        customer_unique_id="u_synth_1",
        customer_city="Sao Paulo",
        customer_state="SP",
    )
    order = Order(
        order_id="o_synth_1",
        customer_id="c_synth_1",
        order_status="delivered",
        order_purchase_timestamp=datetime(2026, 1, 15, 10, 0, 0),
    )
    product = Product(
        product_id="p_synth_1",
        product_category_name="beleza_saude",
        product_name_lenght=45,
        product_description_lenght=250,
        product_photos_qty=2,
        product_weight_g=Decimal("500.0"),
    )
    item = OrderItem(
        order_id="o_synth_1",
        order_item_id=1,
        product_id="p_synth_1",
        seller_id="s_synth_1",
        price=Decimal("99.90"),
        freight_value=Decimal("15.50"),
    )
    payment = Payment(
        order_id="o_synth_1",
        payment_sequential=1,
        payment_type="credit_card",
        payment_installments=3,
        payment_value=Decimal("115.40"),
    )

    db_session.add_all([customer, order, product, item, payment])
    await db_session.flush()

    # Query back order with items and payments
    stmt = (
        select(Order)
        .options(selectinload(Order.items), selectinload(Order.payments))
        .where(Order.order_id == "o_synth_1")
    )
    result = await db_session.execute(stmt)
    fetched_order = result.scalar_one()

    assert fetched_order.order_status == "delivered"
    assert len(fetched_order.items) == 1
    assert fetched_order.items[0].price == Decimal("99.90")
    assert len(fetched_order.payments) == 1
    assert fetched_order.payments[0].payment_value == Decimal("115.40")


@pytest.mark.asyncio
async def test_application_relationships(db_session: AsyncSession) -> None:
    """14. Verify application schema relationships work end-to-end."""
    user = User(
        email="analyst_test@example.com",
        display_name="Data Analyst",
        role="analyst",
    )
    db_session.add(user)
    await db_session.flush()

    conv = Conversation(user_id=user.user_id, title="Revenue Analysis")
    saved_q = SavedQuery(
        user_id=user.user_id,
        name="Top Categories",
        natural_language_query="Show top categories by revenue",
        sql_query="SELECT product_category_name, SUM(price) FROM business.order_items ...",
    )
    db_session.add_all([conv, saved_q])
    await db_session.flush()

    msg = Message(
        conversation_id=conv.conversation_id,
        role="user",
        content="What was the total GMV last month?",
    )
    db_session.add(msg)
    await db_session.flush()

    q_exec = QueryExecution(
        conversation_id=conv.conversation_id,
        message_id=msg.message_id,
        natural_language_query="What was the total GMV last month?",
        generated_sql="SELECT SUM(price) FROM business.order_items;",
        status="succeeded",
        duration_ms=45,
        row_count=1,
        cache_hit=False,
    )
    db_session.add(q_exec)
    await db_session.flush()

    # Query User with relationships
    stmt = (
        select(User)
        .options(
            selectinload(User.conversations).selectinload(Conversation.messages),
            selectinload(User.saved_queries),
        )
        .where(User.user_id == user.user_id)
    )
    result = await db_session.execute(stmt)
    loaded_user = result.scalar_one()

    assert loaded_user.email == "analyst_test@example.com"
    assert len(loaded_user.conversations) == 1
    assert len(loaded_user.conversations[0].messages) == 1
    assert loaded_user.conversations[0].messages[0].content == "What was the total GMV last month?"
    assert len(loaded_user.saved_queries) == 1


@pytest.mark.asyncio
async def test_semantic_schema_crud(db_session: AsyncSession) -> None:
    """Verify semantic schema models can be inserted, queried, and updated."""
    metric = MetricDefinition(
        name="total_gmv",
        display_name="Total Gross Merchandise Value",
        description="Sum of price of delivered order items",
        sql_expression="SUM(price)",
        default_filters={"order_status": "delivered"},
        data_type="numeric",
    )
    dimension = DimensionDefinition(
        name="customer_state",
        display_name="Customer State",
        description="Two-letter Brazilian state abbreviation",
        table_name="customers",
        column_name="customer_state",
        data_type="char(2)",
    )
    data_source = DataSource(
        name="olist_ecommerce",
        description="Olist Brazilian E-Commerce Postgres Database",
        database_type="postgresql",
        schema_name="business",
    )

    db_session.add_all([metric, dimension, data_source])
    await db_session.flush()

    # Query back
    result_metric = await db_session.execute(
        select(MetricDefinition).where(MetricDefinition.name == "total_gmv")
    )
    m = result_metric.scalar_one()
    assert m.default_filters == {"order_status": "delivered"}
    assert m.is_active is True

    result_dim = await db_session.execute(
        select(DimensionDefinition).where(DimensionDefinition.name == "customer_state")
    )
    d = result_dim.scalar_one()
    assert d.table_name == "customers"

    result_ds = await db_session.execute(
        select(DataSource).where(DataSource.name == "olist_ecommerce")
    )
    ds = result_ds.scalar_one()
    assert ds.schema_name == "business"
