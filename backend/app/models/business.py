"""SQLAlchemy models for the business schema (Olist e-commerce dataset structure)."""

from datetime import datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import (
    CHAR,
    VARCHAR,
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    Numeric,
    PrimaryKeyConstraint,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base


class Customer(Base):
    """Customer entity from business.customers."""

    __tablename__ = "customers"
    __table_args__ = (
        Index("ix_business_customers_customer_unique_id", "customer_unique_id"),
        Index("ix_business_customers_customer_state", "customer_state"),
        {"schema": "business"},
    )

    customer_id: Mapped[str] = mapped_column(VARCHAR(32), primary_key=True)
    customer_unique_id: Mapped[str] = mapped_column(VARCHAR(32), nullable=False)
    customer_zip_code_prefix: Mapped[str | None] = mapped_column(VARCHAR(5), nullable=True)
    customer_city: Mapped[str | None] = mapped_column(VARCHAR(100), nullable=True)
    customer_state: Mapped[str | None] = mapped_column(CHAR(2), nullable=True)

    # Relationships
    orders: Mapped[list["Order"]] = relationship(
        back_populates="customer", cascade="all, delete-orphan"
    )


class Order(Base):
    """Order entity from business.orders."""

    __tablename__ = "orders"
    __table_args__ = (
        Index("ix_business_orders_customer_id", "customer_id"),
        Index("ix_business_orders_order_status", "order_status"),
        Index("ix_business_orders_order_purchase_timestamp", "order_purchase_timestamp"),
        {"schema": "business"},
    )

    order_id: Mapped[str] = mapped_column(VARCHAR(32), primary_key=True)
    customer_id: Mapped[str] = mapped_column(
        VARCHAR(32),
        ForeignKey("business.customers.customer_id", ondelete="CASCADE"),
        nullable=False,
    )
    order_status: Mapped[str] = mapped_column(VARCHAR(30), nullable=False)
    order_purchase_timestamp: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    order_approved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    order_delivered_carrier_date: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True
    )
    order_delivered_customer_date: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True
    )
    order_estimated_delivery_date: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True
    )

    # Relationships
    customer: Mapped["Customer"] = relationship(back_populates="orders")
    items: Mapped[list["OrderItem"]] = relationship(
        back_populates="order", cascade="all, delete-orphan"
    )
    payments: Mapped[list["Payment"]] = relationship(
        back_populates="order", cascade="all, delete-orphan"
    )


class OrderItem(Base):
    """Order item line entry from business.order_items."""

    __tablename__ = "order_items"
    __table_args__ = (
        PrimaryKeyConstraint("order_id", "order_item_id", name="pk_business_order_items"),
        CheckConstraint("price >= 0", name="chk_business_order_items_price_non_negative"),
        CheckConstraint(
            "freight_value >= 0", name="chk_business_order_items_freight_value_non_negative"
        ),
        Index("ix_business_order_items_product_id", "product_id"),
        Index("ix_business_order_items_seller_id", "seller_id"),
        {"schema": "business"},
    )

    order_id: Mapped[str] = mapped_column(
        VARCHAR(32),
        ForeignKey("business.orders.order_id", ondelete="CASCADE"),
        nullable=False,
    )
    order_item_id: Mapped[int] = mapped_column(Integer, nullable=False)
    product_id: Mapped[str | None] = mapped_column(
        VARCHAR(32),
        ForeignKey("business.products.product_id", ondelete="SET NULL"),
        nullable=True,
    )
    seller_id: Mapped[str] = mapped_column(VARCHAR(32), nullable=False)
    shipping_limit_date: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    price: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    freight_value: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)

    # Relationships
    order: Mapped["Order"] = relationship(back_populates="items")
    product: Mapped[Optional["Product"]] = relationship(back_populates="order_items")


class Product(Base):
    """Product catalog entry from business.products."""

    __tablename__ = "products"
    __table_args__ = (
        Index("ix_business_products_product_category_name", "product_category_name"),
        {"schema": "business"},
    )

    product_id: Mapped[str] = mapped_column(VARCHAR(32), primary_key=True)
    product_category_name: Mapped[str | None] = mapped_column(VARCHAR(100), nullable=True)
    # Preserve original Olist spelling: product_name_lenght, product_description_lenght
    product_name_lenght: Mapped[int | None] = mapped_column(Integer, nullable=True)
    product_description_lenght: Mapped[int | None] = mapped_column(Integer, nullable=True)
    product_photos_qty: Mapped[int | None] = mapped_column(Integer, nullable=True)
    product_weight_g: Mapped[Decimal | None] = mapped_column(Numeric, nullable=True)
    product_length_cm: Mapped[Decimal | None] = mapped_column(Numeric, nullable=True)
    product_height_cm: Mapped[Decimal | None] = mapped_column(Numeric, nullable=True)
    product_width_cm: Mapped[Decimal | None] = mapped_column(Numeric, nullable=True)

    # Relationships
    order_items: Mapped[list["OrderItem"]] = relationship(back_populates="product")


class Payment(Base):
    """Payment record from business.payments."""

    __tablename__ = "payments"
    __table_args__ = (
        UniqueConstraint(
            "order_id", "payment_sequential", name="uq_business_payments_order_id_sequential"
        ),
        CheckConstraint(
            "payment_value >= 0", name="chk_business_payments_payment_value_non_negative"
        ),
        CheckConstraint(
            "payment_installments >= 0",
            name="chk_business_payments_payment_installments_non_negative",
        ),
        Index("ix_business_payments_order_id", "order_id"),
        Index("ix_business_payments_payment_type", "payment_type"),
        {"schema": "business"},
    )

    payment_id: Mapped[int] = mapped_column(
        BigInteger, Identity(start=1), primary_key=True, autoincrement=True
    )
    order_id: Mapped[str] = mapped_column(
        VARCHAR(32),
        ForeignKey("business.orders.order_id", ondelete="CASCADE"),
        nullable=False,
    )
    payment_sequential: Mapped[int] = mapped_column(Integer, nullable=False)
    payment_type: Mapped[str] = mapped_column(VARCHAR(30), nullable=False)
    payment_installments: Mapped[int] = mapped_column(Integer, nullable=False)
    payment_value: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)

    # Relationships
    order: Mapped["Order"] = relationship(back_populates="payments")
