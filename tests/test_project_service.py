"""Persistence tests against an independent temporary SQLite database."""

import pytest
from sqlalchemy import func, select

from app.models.database import (
    AgentExecution,
    Base,
    Database,
    DevelopmentTaskRecord,
    GeneratedFileRecord,
)
from app.models.database import (
    TestExecution as TestExecutionRecord,
)
from app.models.schemas import (
    DevelopmentTask,
    ProjectRequest,
    TaskPriority,
    TaskStatus,
)
from app.models.schemas import (
    TestResult as _TestResult,
)
from app.services.project_service import (
    ProjectNotFoundError,
    ProjectService,
    ProjectServiceError,
)


@pytest.fixture
def database(tmp_path):
    """Create and dispose a separate on-disk SQLite database per test."""
    db = Database(f"sqlite:///{tmp_path / 'project-service-test.sqlite3'}")
    db.create_all()
    yield db
    db.drop_all()
    db.dispose()


@pytest.fixture
def service(database: Database) -> ProjectService:
    """Provide a service using the test-only SQLite database."""
    return ProjectService(database, initialize_schema=False)


def project_request(name: str = "sample") -> ProjectRequest:
    """Create a valid project input schema."""
    return ProjectRequest(
        project_name=name,
        description="A small test project.",
        requirements=["Persist project history."],
        programming_language="Python",
    )


def development_task(
    task_id: str,
    *,
    dependencies: list[str] | None = None,
    status: TaskStatus = TaskStatus.PENDING,
) -> DevelopmentTask:
    """Build a structured task input."""
    return DevelopmentTask(
        task_id=task_id,
        title=f"Task {task_id}",
        description=f"Description for {task_id}",
        assigned_agent="developer",
        dependencies=dependencies or [],
        priority=TaskPriority.HIGH,
        status=status,
    )


def test_create_and_retrieve_project_and_initialize_database(
    database: Database,
    service: ProjectService,
) -> None:
    created = service.create_project(project_request())
    retrieved = service.get_project(created.id)

    assert created.id > 0
    assert retrieved.name == "sample"
    assert retrieved.description == "A small test project."
    assert retrieved.status == "pending"
    assert retrieved.created_at is not None
    assert retrieved.updated_at is not None
    assert service.get_project_by_name("sample").id == created.id
    table_names = set(Base.metadata.tables)
    assert {
        "projects",
        "development_tasks",
        "task_dependencies",
        "agent_executions",
        "generated_files",
        "test_executions",
    } <= table_names
    database.create_all()


def test_projects_can_be_listed_filtered_and_updated(
    service: ProjectService,
) -> None:
    first = service.create_project(project_request("first"))
    service.create_project(project_request("second"), status="completed")

    updated = service.update_project(
        first.id,
        description="Updated description.",
        status="in_progress",
    )

    assert updated.name == "first"
    assert updated.description == "Updated description."
    assert updated.status == "in_progress"
    assert [project.name for project in service.list_projects()] == [
        "second",
        "first",
    ] or {project.name for project in service.list_projects()} == {
        "first",
        "second",
    }
    assert [project.name for project in service.list_projects(status="completed")] == [
        "second"
    ]
    assert len(service.list_projects(limit=1)) == 1


def test_duplicate_project_name_is_rejected(service: ProjectService) -> None:
    service.create_project(project_request())

    with pytest.raises(ProjectServiceError, match="already exists"):
        service.create_project(project_request())


def test_tasks_upsert_dependencies_and_crud(
    service: ProjectService,
) -> None:
    project = service.create_project(project_request())
    first = development_task("first", status=TaskStatus.COMPLETED)
    second = development_task("second", dependencies=["first"])

    saved = service.save_tasks(project.id, [first, second])
    task_record = service.get_task(project.id, "second")

    assert len(saved) == 2
    assert [dependency.task_id for dependency in task_record.dependencies] == ["first"]
    assert [
        dependent.task_id
        for dependent in service.get_task(project.id, "first").dependents
    ] == ["second"]

    service.save_tasks(
        project.id,
        [
            first.model_copy(update={"title": "Updated first"}),
            second.model_copy(update={"status": TaskStatus.IN_PROGRESS}),
        ],
    )
    tasks = service.list_tasks(project.id)
    assert len(tasks) == 2
    assert service.get_task(project.id, "first").title == "Updated first"
    assert (
        service.update_task(
            project.id,
            "second",
            status=TaskStatus.COMPLETED.value,
            priority=TaskPriority.CRITICAL.value,
        ).status
        == TaskStatus.COMPLETED.value
    )

    service.delete_task(project.id, "second")
    assert [record.task_id for record in service.list_tasks(project.id)] == ["first"]


def test_invalid_task_dependencies_roll_back_transaction(
    service: ProjectService,
) -> None:
    project = service.create_project(project_request())

    with pytest.raises(ProjectServiceError, match="unknown task"):
        service.save_tasks(
            project.id,
            [development_task("orphan", dependencies=["missing"])],
        )

    assert service.list_tasks(project.id) == []


def test_agent_execution_history_and_idempotency(
    service: ProjectService,
) -> None:
    project = service.create_project(project_request())
    first = service.record_agent_execution(
        project.id,
        agent_name="Architect",
        input_data='{"task":"design"}',
        output_data='{"architecture":"ready"}',
        status="completed",
        execution_duration=1.25,
        idempotency_key="design-run-1",
    )
    duplicate = service.record_agent_execution(
        project.id,
        agent_name="Architect",
        input_data='{"task":"design"}',
        output_data='{"architecture":"ready"}',
        status="completed",
        idempotency_key="design-run-1",
    )
    second = service.record_agent_execution(
        project.id,
        agent_name="Developer",
        input_data="generate",
        output_data="done",
    )

    history = service.list_agent_executions(project.id)
    assert first.id == duplicate.id
    assert second.id != first.id
    assert len(history) == 2
    assert history[0].input == '{"task":"design"}'
    assert history[0].execution_duration == 1.25
    assert history[1].agent_name == "Developer"


def test_generated_files_keep_content_versions_without_duplicate_writes(
    service: ProjectService,
) -> None:
    project = service.create_project(project_request())
    first = service.save_generated_file(
        project.id,
        file_path="app/main.py",
        file_content="value = 1\n",
    )
    duplicate = service.save_generated_file(
        project.id,
        file_path="app/main.py",
        file_content="value = 1\n",
    )
    second = service.save_generated_file(
        project.id,
        file_path="app/main.py",
        file_content="value = 2\n",
    )
    reference = service.save_generated_file(
        project.id,
        file_path="README.md",
        workspace_reference="generated_projects/sample/README.md",
    )

    assert first.version == 1
    assert duplicate.id == first.id
    assert second.version == 2
    assert reference.workspace_reference == "generated_projects/sample/README.md"
    assert (
        service.get_generated_file(project.id, "app/main.py").file_content
        == "value = 2\n"
    )
    assert (
        service.get_generated_file(project.id, "app/main.py", version=1).file_content
        == "value = 1\n"
    )
    assert len(service.list_generated_files(project.id)) == 2
    assert len(service.list_generated_files(project.id, include_history=True)) == 3


def test_test_execution_history_and_project_relationships(
    service: ProjectService,
) -> None:
    project = service.create_project(project_request())
    task_record = service.save_tasks(project.id, [development_task("one")])[0]
    agent_execution = service.record_agent_execution(
        project.id,
        agent_name="Tester",
        input_data="input",
    )
    file_record = service.save_generated_file(
        project.id,
        file_path="app/main.py",
        file_content="pass\n",
    )
    test_execution = service.record_test_execution(
        project.id,
        _TestResult(total_tests=3, passed_tests=2, failed_tests=1),
        execution_status="failed",
    )

    retrieved = service.get_project(project.id)

    assert retrieved.tasks[0].id == task_record.id
    assert retrieved.agent_executions[0].id == agent_execution.id
    assert retrieved.generated_files[0].id == file_record.id
    assert retrieved.test_executions[0].id == test_execution.id
    assert test_execution.total_tests == 3
    assert test_execution.passed_tests == 2
    assert test_execution.failed_tests == 1
    assert service.list_test_executions(project.id)[0].execution_status == "failed"


def test_project_delete_cascades_through_history(
    database: Database,
    service: ProjectService,
) -> None:
    project = service.create_project(project_request())
    service.save_tasks(project.id, [development_task("task")])
    service.record_agent_execution(
        project.id,
        agent_name="Tester",
        input_data="input",
    )
    service.save_generated_file(
        project.id,
        file_path="app/main.py",
        file_content="pass\n",
    )
    service.record_test_execution(
        project.id,
        _TestResult(total_tests=1, passed_tests=1),
        execution_status="passed",
    )

    service.delete_project(project.id)

    with pytest.raises(ProjectNotFoundError):
        service.get_project(project.id)
    with database.session_scope() as session:
        for model in (
            DevelopmentTaskRecord,
            AgentExecution,
            GeneratedFileRecord,
            TestExecutionRecord,
        ):
            count = session.scalar(
                select(func.count())
                .select_from(model)
                .where(model.project_id == project.id)
            )
            assert count == 0


def test_missing_project_and_invalid_values_are_reported(
    service: ProjectService,
) -> None:
    with pytest.raises(ProjectNotFoundError):
        service.get_project(999)

    project = service.create_project(project_request())
    with pytest.raises(ValueError, match="limit"):
        service.list_projects(limit=0)
    with pytest.raises(ValueError, match="offset"):
        service.list_agent_executions(project.id, offset=-1)
    with pytest.raises(ValueError, match="exactly one"):
        service.save_generated_file(
            project.id,
            file_path="app/main.py",
        )
    with pytest.raises(ValueError, match="negative"):
        service.record_agent_execution(
            project.id,
            agent_name="Developer",
            input_data="",
            execution_duration=-1,
        )
