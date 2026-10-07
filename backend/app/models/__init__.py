"""SQLAlchemy models package exporting Base and all entity models across schemas."""

from app.models.application import Conversation, Message, QueryExecution, SavedQuery, User
from app.models.base import Base
from app.models.business import Customer, Order, OrderItem, Payment, Product
from app.models.semantic import DataSource, DimensionDefinition, MetricDefinition

__all__ = [
    "Base",
    # Business models (5)
    "Customer",
    "Order",
    "OrderItem",
    "Product",
    "Payment",
    # Application models (5)
    "User",
    "Conversation",
    "Message",
    "QueryExecution",
    "SavedQuery",
    # Semantic models (3)
    "MetricDefinition",
    "DimensionDefinition",
    "DataSource",
]
