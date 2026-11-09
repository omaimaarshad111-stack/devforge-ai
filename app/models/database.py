"""SQLAlchemy ORM models and SQLite session configuration."""

from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Table,
    Text,
    UniqueConstraint,
    create_engine,
    event,
)
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    mapped_column,
    relationship,
    sessionmaker,
)

from app.config.settings import settings


class DatabaseError(RuntimeError):
    """Raised when a database transaction cannot be completed."""


def utc_now() -> datetime:
    """Return a timezone-aware UTC timestamp."""
    return datetime.now(UTC)


class Base(DeclarativeBase):
    """Declarative base shared by all DevForge ORM models."""


task_dependencies = Table(
    "task_dependencies",
    Base.metadata,
    Column(
        "task_id",
        ForeignKey("development_tasks.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column(
        "dependency_id",
        ForeignKey("development_tasks.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Index("ix_task_dependencies_dependency_id", "dependency_id"),
)


class Project(Base):
    """A generated project and its persisted development history."""

    __tablename__ = "projects"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200), unique=True, index=True)
    description: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), default="pending")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )

    tasks: Mapped[list["DevelopmentTaskRecord"]] = relationship(
        back_populates="project",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    agent_executions: Mapped[list["AgentExecution"]] = relationship(
        back_populates="project",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    generated_files: Mapped[list["GeneratedFileRecord"]] = relationship(
        back_populates="project",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    test_executions: Mapped[list["TestExecution"]] = relationship(
        back_populates="project",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class DevelopmentTaskRecord(Base):
    """A project task with explicit task-to-task dependency relationships."""

    __tablename__ = "development_tasks"
    __table_args__ = (
        UniqueConstraint("project_id", "task_id", name="uq_project_task_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    task_id: Mapped[str] = mapped_column(String(200))
    title: Mapped[str] = mapped_column(String(300))
    description: Mapped[str] = mapped_column(Text)
    assigned_agent: Mapped[str] = mapped_column(String(100))
    priority: Mapped[str] = mapped_column(String(32), default="medium")
    status: Mapped[str] = mapped_column(String(32), default="pending")

    project: Mapped[Project] = relationship(back_populates="tasks")
    dependencies: Mapped[list["DevelopmentTaskRecord"]] = relationship(
        "DevelopmentTaskRecord",
        secondary=task_dependencies,
        primaryjoin=lambda: DevelopmentTaskRecord.id == task_dependencies.c.task_id,
        secondaryjoin=lambda: (
            DevelopmentTaskRecord.id == task_dependencies.c.dependency_id
        ),
        back_populates="dependents",
    )
    dependents: Mapped[list["DevelopmentTaskRecord"]] = relationship(
        "DevelopmentTaskRecord",
        secondary=task_dependencies,
        primaryjoin=lambda: (
            DevelopmentTaskRecord.id == task_dependencies.c.dependency_id
        ),
        secondaryjoin=lambda: DevelopmentTaskRecord.id == task_dependencies.c.task_id,
        back_populates="dependencies",
    )


class AgentExecution(Base):
    """One historical input/output record for a specialized agent invocation."""

    __tablename__ = "agent_executions"
    __table_args__ = (
        UniqueConstraint(
            "project_id",
            "idempotency_key",
            name="uq_agent_execution_idempotency",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    agent_name: Mapped[str] = mapped_column(String(100))
    input: Mapped[str] = mapped_column(Text)
    output: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(32), default="pending")
    execution_duration: Mapped[float | None] = mapped_column(Float, nullable=True)
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, index=True
    )
    idempotency_key: Mapped[str | None] = mapped_column(String(200), nullable=True)

    project: Mapped[Project] = relationship(back_populates="agent_executions")


class GeneratedFileRecord(Base):
    """An immutable version of a generated file or workspace reference."""

    __tablename__ = "generated_files"
    __table_args__ = (
        UniqueConstraint(
            "project_id",
            "file_path",
            "version",
            name="uq_generated_file_version",
        ),
        Index("ix_generated_files_project_path", "project_id", "file_path"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    file_path: Mapped[str] = mapped_column(String(1000))
    file_content: Mapped[str | None] = mapped_column(Text, nullable=True)
    workspace_reference: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    version: Mapped[int] = mapped_column(Integer)
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, index=True
    )

    project: Mapped[Project] = relationship(back_populates="generated_files")


class TestExecution(Base):
    """Historical pytest totals and execution outcome for a project."""

    __test__ = False
    __tablename__ = "test_executions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    total_tests: Mapped[int] = mapped_column(Integer, default=0)
    passed_tests: Mapped[int] = mapped_column(Integer, default=0)
    failed_tests: Mapped[int] = mapped_column(Integer, default=0)
    execution_status: Mapped[str] = mapped_column(String(32))
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, index=True
    )

    project: Mapped[Project] = relationship(back_populates="test_executions")


class Database:
    """Create SQLAlchemy engines, sessions, and schema for one database URL."""

    def __init__(
        self,
        database_url: str | None = None,
        *,
        engine: Engine | None = None,
    ) -> None:
        self.database_url = database_url or settings.database_url
        self.engine = engine or create_engine(
            self.database_url,
            connect_args={"check_same_thread": False}
            if self.database_url.startswith("sqlite")
            else {},
        )
        self._session_factory = sessionmaker(
            bind=self.engine,
            expire_on_commit=False,
        )
        if self.engine.dialect.name == "sqlite":
            event.listen(self.engine, "connect", self._enable_sqlite_foreign_keys)

    @staticmethod
    def _enable_sqlite_foreign_keys(
        connection: Any,
        _: Any,
    ) -> None:
        """Enable declared ON DELETE behavior for each SQLite connection."""
        cursor = connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    def create_all(self) -> None:
        """Create every table in the configured database."""
        try:
            Base.metadata.create_all(self.engine)
        except SQLAlchemyError as error:
            raise DatabaseError("Could not create the database schema.") from error

    def drop_all(self) -> None:
        """Drop all application tables; intended for isolated test databases."""
        try:
            Base.metadata.drop_all(self.engine)
        except SQLAlchemyError as error:
            raise DatabaseError("Could not drop the database schema.") from error

    @contextmanager
    def session_scope(self) -> Generator[Any, None, None]:
        """Yield a session inside a transaction, rolling back failures."""
        session = self._session_factory()
        try:
            with session.begin():
                yield session
        except SQLAlchemyError as error:
            session.rollback()
            raise DatabaseError("Database transaction failed.") from error
        finally:
            session.close()

    def dispose(self) -> None:
        """Dispose the engine and release its connection pool."""
        self.engine.dispose()


database = Database()


def init_database() -> None:
    """Initialize the configured application's schema."""
    database.create_all()
