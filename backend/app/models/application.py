"""SQLAlchemy models for the app schema (conversational analytics application)."""

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    VARCHAR,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base


class User(Base):
    """User entity in app.users."""

    __tablename__ = "users"
    __table_args__ = {"schema": "app"}

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )
    email: Mapped[str] = mapped_column(VARCHAR(255), unique=True, nullable=False)
    display_name: Mapped[str | None] = mapped_column(VARCHAR(100), nullable=True)
    role: Mapped[str] = mapped_column(
        VARCHAR(30), nullable=False, default="analyst", server_default=text("'analyst'")
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

    # Relationships
    conversations: Mapped[list["Conversation"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    saved_queries: Mapped[list["SavedQuery"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class Conversation(Base):
    """Conversation thread in app.conversations."""

    __tablename__ = "conversations"
    __table_args__ = (
        Index("ix_app_conversations_user_id", "user_id"),
        {"schema": "app"},
    )

    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("app.users.user_id", ondelete="CASCADE"),
        nullable=False,
    )
    title: Mapped[str | None] = mapped_column(VARCHAR(255), nullable=True)
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

    # Relationships
    user: Mapped["User"] = relationship(back_populates="conversations")
    messages: Mapped[list["Message"]] = relationship(
        back_populates="conversation", cascade="all, delete-orphan"
    )
    query_executions: Mapped[list["QueryExecution"]] = relationship(
        back_populates="conversation"
    )


class Message(Base):
    """Chat message in app.messages."""

    __tablename__ = "messages"
    __table_args__ = (
        CheckConstraint(
            "role IN ('user', 'assistant', 'system')",
            name="chk_app_messages_role",
        ),
        Index("ix_app_messages_conversation_id_created_at", "conversation_id", "created_at"),
        {"schema": "app"},
    )

    message_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("app.conversations.conversation_id", ondelete="CASCADE"),
        nullable=False,
    )
    role: Mapped[str] = mapped_column(VARCHAR(20), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=func.now(),
        server_default=func.now(),
    )

    # Relationships
    conversation: Mapped["Conversation"] = relationship(back_populates="messages")
    query_executions: Mapped[list["QueryExecution"]] = relationship(back_populates="message")


class QueryExecution(Base):
    """Metadata and audit logging for generated queries in app.query_executions."""

    __tablename__ = "query_executions"
    __table_args__ = (
        CheckConstraint(
            "status IN ('generated', 'blocked', 'validated', 'executing', 'succeeded', 'failed', 'repaired')",
            name="chk_app_query_executions_status",
        ),
        Index("ix_app_query_executions_conversation_id", "conversation_id"),
        Index("ix_app_query_executions_status", "status"),
        Index("ix_app_query_executions_started_at", "started_at"),
        {"schema": "app"},
    )

    execution_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("app.conversations.conversation_id", ondelete="SET NULL"),
        nullable=True,
    )
    message_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("app.messages.message_id", ondelete="SET NULL"),
        nullable=True,
    )
    natural_language_query: Mapped[str] = mapped_column(Text, nullable=False)
    generated_sql: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(VARCHAR(30), nullable=False)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    row_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    result_size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    cache_hit: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )

    # Relationships
    conversation: Mapped[Optional["Conversation"]] = relationship(
        back_populates="query_executions"
    )
    message: Mapped[Optional["Message"]] = relationship(back_populates="query_executions")


class SavedQuery(Base):
    """User-saved queries in app.saved_queries."""

    __tablename__ = "saved_queries"
    __table_args__ = (
        Index("ix_app_saved_queries_user_id", "user_id"),
        {"schema": "app"},
    )

    saved_query_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("app.users.user_id", ondelete="CASCADE"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(VARCHAR(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    natural_language_query: Mapped[str] = mapped_column(Text, nullable=False)
    sql_query: Mapped[str] = mapped_column(Text, nullable=False)
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

    # Relationships
    user: Mapped["User"] = relationship(back_populates="saved_queries")
