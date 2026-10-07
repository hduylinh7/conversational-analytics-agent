"""SQLAlchemy models for the semantic schema (metrics, dimensions, data sources)."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    VARCHAR,
    Boolean,
    DateTime,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class MetricDefinition(Base):
    """Semantic metric definition in semantic.metric_definitions."""

    __tablename__ = "metric_definitions"
    __table_args__ = {"schema": "semantic"}

    metric_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )
    name: Mapped[str] = mapped_column(VARCHAR(100), unique=True, nullable=False)
    display_name: Mapped[str] = mapped_column(VARCHAR(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    sql_expression: Mapped[str] = mapped_column(Text, nullable=False)
    default_filters: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    data_type: Mapped[str] = mapped_column(VARCHAR(30), nullable=False)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=func.now(),
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=func.now(),
        onupdate=func.now(),
        server_default=func.now(),
    )


class DimensionDefinition(Base):
    """Semantic dimension definition in semantic.dimension_definitions."""

    __tablename__ = "dimension_definitions"
    __table_args__ = {"schema": "semantic"}

    dimension_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )
    name: Mapped[str] = mapped_column(VARCHAR(100), unique=True, nullable=False)
    display_name: Mapped[str] = mapped_column(VARCHAR(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    table_name: Mapped[str] = mapped_column(VARCHAR(100), nullable=False)
    column_name: Mapped[str] = mapped_column(VARCHAR(100), nullable=False)
    data_type: Mapped[str] = mapped_column(VARCHAR(30), nullable=False)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )


class DataSource(Base):
    """Semantic data source catalog in semantic.data_sources."""

    __tablename__ = "data_sources"
    __table_args__ = {"schema": "semantic"}

    data_source_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )
    name: Mapped[str] = mapped_column(VARCHAR(100), unique=True, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    database_type: Mapped[str] = mapped_column(VARCHAR(30), nullable=False)
    schema_name: Mapped[str] = mapped_column(VARCHAR(100), nullable=False)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=func.now(),
        server_default=func.now(),
    )
