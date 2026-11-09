"""Transactional project persistence operations, separate from agent logic."""

from collections.abc import Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from app.models.database import (
    AgentExecution,
    Database,
    DatabaseError,
    DevelopmentTaskRecord,
    GeneratedFileRecord,
    Project,
    TestExecution,
    database,
)
from app.models.schemas import (
    DevelopmentTask,
    ProjectRequest,
    TestResult,
)


class ProjectServiceError(RuntimeError):
    """Raised when a project persistence operation cannot be completed."""


class ProjectNotFoundError(ProjectServiceError):
    """Raised when a requested project does not exist."""


class ProjectService:
    """CRUD and history operations for projects and their development records.

    Every operation owns a database transaction. Agent implementations do not
    import or call this service; orchestration or API layers may use it.
    """

    def __init__(
        self,
        database_instance: Database | None = None,
        *,
        initialize_schema: bool = True,
    ) -> None:
        self._database = database_instance or database
        if initialize_schema:
            self._database.create_all()

    def create_project(
        self,
        request: ProjectRequest,
        *,
        status: str = "pending",
    ) -> Project:
        """Insert one project; its unique name makes creation idempotent-safe."""
        try:
            with self._database.session_scope() as session:
                project = Project(
                    name=request.project_name,
                    description=request.description,
                    status=status,
                )
                session.add(project)
                session.flush()
                return project
        except DatabaseError as error:
            if isinstance(error.__cause__, IntegrityError):
                raise ProjectServiceError(
                    f"Project '{request.project_name}' already exists."
                ) from error
            raise ProjectServiceError("Could not create project.") from error

    def get_project(self, project_id: int) -> Project:
        """Retrieve a project with its direct history collections loaded."""
        try:
            with self._database.session_scope() as session:
                statement = (
                    select(Project)
                    .where(Project.id == project_id)
                    .options(
                        selectinload(Project.tasks).selectinload(
                            DevelopmentTaskRecord.dependencies
                        ),
                        selectinload(Project.agent_executions),
                        selectinload(Project.generated_files),
                        selectinload(Project.test_executions),
                    )
                )
                project = session.scalar(statement)
                if project is None:
                    raise ProjectNotFoundError(
                        f"Project with ID {project_id} was not found."
                    )
                return project
        except DatabaseError as error:
            raise ProjectServiceError("Could not retrieve project.") from error

    def get_project_by_name(self, name: str) -> Project:
        """Retrieve a project by its unique name."""
        try:
            with self._database.session_scope() as session:
                project = session.scalar(
                    select(Project)
                    .where(Project.name == name)
                    .options(
                        selectinload(Project.tasks).selectinload(
                            DevelopmentTaskRecord.dependencies
                        ),
                        selectinload(Project.agent_executions),
                        selectinload(Project.generated_files),
                        selectinload(Project.test_executions),
                    )
                )
                if project is None:
                    raise ProjectNotFoundError(f"Project '{name}' was not found.")
                return project
        except DatabaseError as error:
            raise ProjectServiceError("Could not retrieve project.") from error

    def list_projects(
        self,
        *,
        status: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Project]:
        """Return projects ordered by most recently updated."""
        self._validate_pagination(limit, offset)
        try:
            with self._database.session_scope() as session:
                statement = select(Project).order_by(Project.updated_at.desc())
                if status is not None:
                    statement = statement.where(Project.status == status)
                return list(session.scalars(statement.limit(limit).offset(offset)))
        except DatabaseError as error:
            raise ProjectServiceError("Could not list projects.") from error

    def update_project(
        self,
        project_id: int,
        *,
        name: str | None = None,
        description: str | None = None,
        status: str | None = None,
    ) -> Project:
        """Update supplied project fields without replacing unspecified values."""
        updates = {
            "name": name,
            "description": description,
            "status": status,
        }
        try:
            with self._database.session_scope() as session:
                project = self._require_project(session, project_id)
                for field, value in updates.items():
                    if value is not None:
                        setattr(project, field, value)
                session.flush()
                return project
        except DatabaseError as error:
            if isinstance(error.__cause__, IntegrityError):
                raise ProjectServiceError(
                    "Project update conflicts with existing data."
                ) from error
            raise ProjectServiceError("Could not update project.") from error

    def delete_project(self, project_id: int) -> None:
        """Delete a project and all its dependent history records."""
        try:
            with self._database.session_scope() as session:
                session.delete(self._require_project(session, project_id))
        except DatabaseError as error:
            raise ProjectServiceError("Could not delete project.") from error

    def save_tasks(
        self,
        project_id: int,
        tasks: Sequence[DevelopmentTask],
    ) -> list[DevelopmentTaskRecord]:
        """Insert or update task records by project/task ID and set dependencies.

        Calling this repeatedly with the same task IDs updates existing rows
        rather than duplicating task history.
        """
        try:
            with self._database.session_scope() as session:
                self._require_project(session, project_id)
                records_by_task_id: dict[str, DevelopmentTaskRecord] = {}
                for task in tasks:
                    record = session.scalar(
                        select(DevelopmentTaskRecord).where(
                            DevelopmentTaskRecord.project_id == project_id,
                            DevelopmentTaskRecord.task_id == task.task_id,
                        )
                    )
                    if record is None:
                        record = DevelopmentTaskRecord(
                            project_id=project_id,
                            task_id=task.task_id,
                            title=task.title,
                            description=task.description,
                            assigned_agent=task.assigned_agent,
                            priority=task.priority.value,
                            status=task.status.value,
                        )
                        session.add(record)
                    else:
                        record.title = task.title
                        record.description = task.description
                        record.assigned_agent = task.assigned_agent
                        record.priority = task.priority.value
                        record.status = task.status.value
                    records_by_task_id[task.task_id] = record

                session.flush()
                all_records = {
                    record.task_id: record
                    for record in session.scalars(
                        select(DevelopmentTaskRecord).where(
                            DevelopmentTaskRecord.project_id == project_id
                        )
                    )
                }
                for task in tasks:
                    record = records_by_task_id[task.task_id]
                    missing_ids = set(task.dependencies) - all_records.keys()
                    if missing_ids:
                        raise ProjectServiceError(
                            f"Task '{task.task_id}' depends on unknown task(s): "
                            f"{', '.join(sorted(missing_ids))}."
                        )
                    record.dependencies = [
                        all_records[dependency_id]
                        for dependency_id in task.dependencies
                    ]
                session.flush()
                return list(records_by_task_id.values())
        except DatabaseError as error:
            raise ProjectServiceError("Could not save development tasks.") from error

    def get_task(
        self,
        project_id: int,
        task_id: str,
    ) -> DevelopmentTaskRecord:
        """Retrieve one task and its declared dependencies."""
        try:
            with self._database.session_scope() as session:
                task = session.scalar(
                    select(DevelopmentTaskRecord)
                    .where(
                        DevelopmentTaskRecord.project_id == project_id,
                        DevelopmentTaskRecord.task_id == task_id,
                    )
                    .options(
                        selectinload(DevelopmentTaskRecord.dependencies),
                        selectinload(DevelopmentTaskRecord.dependents),
                    )
                )
                if task is None:
                    raise ProjectServiceError(
                        f"Task '{task_id}' was not found in project {project_id}."
                    )
                return task
        except DatabaseError as error:
            raise ProjectServiceError("Could not retrieve development task.") from error

    def list_tasks(self, project_id: int) -> list[DevelopmentTaskRecord]:
        """List project tasks with dependency records loaded."""
        try:
            with self._database.session_scope() as session:
                self._require_project(session, project_id)
                statement = (
                    select(DevelopmentTaskRecord)
                    .where(DevelopmentTaskRecord.project_id == project_id)
                    .order_by(DevelopmentTaskRecord.id)
                    .options(
                        selectinload(DevelopmentTaskRecord.dependencies),
                        selectinload(DevelopmentTaskRecord.dependents),
                    )
                )
                return list(session.scalars(statement))
        except DatabaseError as error:
            raise ProjectServiceError("Could not list development tasks.") from error

    def update_task(
        self,
        project_id: int,
        task_id: str,
        *,
        title: str | None = None,
        description: str | None = None,
        assigned_agent: str | None = None,
        priority: str | None = None,
        status: str | None = None,
    ) -> DevelopmentTaskRecord:
        """Update fields on an existing task."""
        updates = {
            "title": title,
            "description": description,
            "assigned_agent": assigned_agent,
            "priority": priority,
            "status": status,
        }
        try:
            with self._database.session_scope() as session:
                record = session.scalar(
                    select(DevelopmentTaskRecord).where(
                        DevelopmentTaskRecord.project_id == project_id,
                        DevelopmentTaskRecord.task_id == task_id,
                    )
                )
                if record is None:
                    raise ProjectServiceError(
                        f"Task '{task_id}' was not found in project {project_id}."
                    )
                for field, value in updates.items():
                    if value is not None:
                        setattr(record, field, value)
                session.flush()
                return record
        except DatabaseError as error:
            raise ProjectServiceError("Could not update development task.") from error

    def delete_task(self, project_id: int, task_id: str) -> None:
        """Delete one task; dependent task references are removed by foreign keys."""
        try:
            with self._database.session_scope() as session:
                record = session.scalar(
                    select(DevelopmentTaskRecord).where(
                        DevelopmentTaskRecord.project_id == project_id,
                        DevelopmentTaskRecord.task_id == task_id,
                    )
                )
                if record is None:
                    raise ProjectServiceError(
                        f"Task '{task_id}' was not found in project {project_id}."
                    )
                session.delete(record)
        except DatabaseError as error:
            raise ProjectServiceError("Could not delete development task.") from error

    def record_agent_execution(
        self,
        project_id: int,
        *,
        agent_name: str,
        input_data: str,
        output_data: str = "",
        status: str = "completed",
        execution_duration: float | None = None,
        timestamp: datetime | None = None,
        idempotency_key: str | None = None,
    ) -> AgentExecution:
        """Append an agent execution, deduplicating only a supplied idempotency key."""
        if execution_duration is not None and execution_duration < 0:
            raise ValueError("execution_duration must not be negative.")
        try:
            with self._database.session_scope() as session:
                self._require_project(session, project_id)
                if idempotency_key is not None:
                    existing = session.scalar(
                        select(AgentExecution).where(
                            AgentExecution.project_id == project_id,
                            AgentExecution.idempotency_key == idempotency_key,
                        )
                    )
                    if existing is not None:
                        return existing
                execution = AgentExecution(
                    project_id=project_id,
                    agent_name=agent_name,
                    input=input_data,
                    output=output_data,
                    status=status,
                    execution_duration=execution_duration,
                    timestamp=timestamp,
                    idempotency_key=idempotency_key,
                )
                session.add(execution)
                session.flush()
                return execution
        except DatabaseError as error:
            if isinstance(error.__cause__, IntegrityError):
                raise ProjectServiceError(
                    "Agent execution idempotency key was already used."
                ) from error
            raise ProjectServiceError("Could not record agent execution.") from error

    def list_agent_executions(
        self,
        project_id: int,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> list[AgentExecution]:
        """List agent execution history chronologically."""
        self._validate_pagination(limit, offset)
        try:
            with self._database.session_scope() as session:
                self._require_project(session, project_id)
                statement = (
                    select(AgentExecution)
                    .where(AgentExecution.project_id == project_id)
                    .order_by(AgentExecution.timestamp, AgentExecution.id)
                )
                return list(session.scalars(statement.limit(limit).offset(offset)))
        except DatabaseError as error:
            raise ProjectServiceError("Could not list agent executions.") from error

    def update_agent_execution(
        self,
        project_id: int,
        execution_id: int,
        *,
        output_data: str,
        status: str,
        execution_duration: float | None = None,
    ) -> AgentExecution:
        """Finalize one project-scoped execution record transactionally."""
        if execution_duration is not None and execution_duration < 0:
            raise ValueError("execution_duration must not be negative.")
        try:
            with self._database.session_scope() as session:
                execution = session.scalar(
                    select(AgentExecution).where(
                        AgentExecution.id == execution_id,
                        AgentExecution.project_id == project_id,
                        AgentExecution.agent_name == "code_executor",
                    )
                )
                if execution is None:
                    raise ProjectServiceError(
                        "Code execution record was not found for this project."
                    )
                execution.output = output_data
                execution.status = status
                execution.execution_duration = execution_duration
                session.flush()
                return execution
        except DatabaseError as error:
            raise ProjectServiceError("Could not update code execution.") from error

    def save_generated_file(
        self,
        project_id: int,
        *,
        file_path: str,
        file_content: str | None = None,
        workspace_reference: str | None = None,
    ) -> GeneratedFileRecord:
        """Append a new content version only when the latest value changes."""
        if (file_content is None) == (workspace_reference is None):
            raise ValueError(
                "Provide exactly one of file_content or workspace_reference."
            )
        try:
            with self._database.session_scope() as session:
                self._require_project(session, project_id)
                latest = session.scalar(
                    select(GeneratedFileRecord)
                    .where(
                        GeneratedFileRecord.project_id == project_id,
                        GeneratedFileRecord.file_path == file_path,
                    )
                    .order_by(GeneratedFileRecord.version.desc())
                    .limit(1)
                )
                if latest is not None and (
                    latest.file_content == file_content
                    and latest.workspace_reference == workspace_reference
                ):
                    return latest
                record = GeneratedFileRecord(
                    project_id=project_id,
                    file_path=file_path,
                    file_content=file_content,
                    workspace_reference=workspace_reference,
                    version=1 if latest is None else latest.version + 1,
                )
                session.add(record)
                session.flush()
                return record
        except DatabaseError as error:
            raise ProjectServiceError("Could not save generated file.") from error

    def get_generated_file(
        self,
        project_id: int,
        file_path: str,
        *,
        version: int | None = None,
    ) -> GeneratedFileRecord:
        """Retrieve a specific or latest generated file version."""
        try:
            with self._database.session_scope() as session:
                statement = select(GeneratedFileRecord).where(
                    GeneratedFileRecord.project_id == project_id,
                    GeneratedFileRecord.file_path == file_path,
                )
                if version is not None:
                    statement = statement.where(GeneratedFileRecord.version == version)
                else:
                    statement = statement.order_by(GeneratedFileRecord.version.desc())
                record = session.scalar(statement.limit(1))
                if record is None:
                    raise ProjectServiceError(
                        f"Generated file '{file_path}' was not found in project "
                        f"{project_id}."
                    )
                return record
        except DatabaseError as error:
            raise ProjectServiceError("Could not retrieve generated file.") from error

    def list_generated_files(
        self,
        project_id: int,
        *,
        include_history: bool = False,
    ) -> list[GeneratedFileRecord]:
        """List latest versions by default or every immutable file version."""
        try:
            with self._database.session_scope() as session:
                self._require_project(session, project_id)
                statement = select(GeneratedFileRecord).where(
                    GeneratedFileRecord.project_id == project_id
                )
                if not include_history:
                    latest_version = (
                        select(
                            GeneratedFileRecord.file_path,
                            func.max(GeneratedFileRecord.version).label("version"),
                        )
                        .where(GeneratedFileRecord.project_id == project_id)
                        .group_by(GeneratedFileRecord.file_path)
                        .subquery()
                    )
                    statement = statement.join(
                        latest_version,
                        (GeneratedFileRecord.file_path == latest_version.c.file_path)
                        & (GeneratedFileRecord.version == latest_version.c.version),
                    )
                statement = statement.order_by(
                    GeneratedFileRecord.file_path,
                    GeneratedFileRecord.version,
                )
                return list(session.scalars(statement))
        except DatabaseError as error:
            raise ProjectServiceError("Could not list generated files.") from error

    def record_test_execution(
        self,
        project_id: int,
        result: TestResult,
        *,
        execution_status: str,
        timestamp: datetime | None = None,
    ) -> TestExecution:
        """Persist test totals and the runner's overall execution status."""
        try:
            with self._database.session_scope() as session:
                self._require_project(session, project_id)
                execution = TestExecution(
                    project_id=project_id,
                    total_tests=result.total_tests,
                    passed_tests=result.passed_tests,
                    failed_tests=result.failed_tests,
                    execution_status=execution_status,
                    timestamp=timestamp,
                )
                session.add(execution)
                session.flush()
                return execution
        except DatabaseError as error:
            raise ProjectServiceError("Could not record test execution.") from error

    def list_test_executions(
        self,
        project_id: int,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> list[TestExecution]:
        """List a project's test history from oldest to newest."""
        self._validate_pagination(limit, offset)
        try:
            with self._database.session_scope() as session:
                self._require_project(session, project_id)
                statement = (
                    select(TestExecution)
                    .where(TestExecution.project_id == project_id)
                    .order_by(TestExecution.timestamp, TestExecution.id)
                )
                return list(session.scalars(statement.limit(limit).offset(offset)))
        except DatabaseError as error:
            raise ProjectServiceError("Could not list test executions.") from error

    @staticmethod
    def _require_project(session: Any, project_id: int) -> Project:
        """Load a project in the caller's transaction or raise a clear error."""
        project = session.get(Project, project_id)
        if project is None:
            raise ProjectNotFoundError(f"Project with ID {project_id} was not found.")
        return project

    @staticmethod
    def _validate_pagination(limit: int, offset: int) -> None:
        """Reject invalid pagination values before issuing a query."""
        if limit < 1:
            raise ValueError("limit must be at least one.")
        if offset < 0:
            raise ValueError("offset must not be negative.")
