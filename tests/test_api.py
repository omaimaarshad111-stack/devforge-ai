"""HTTP API tests with an isolated SQLite database and mocked workflows."""

import json
import time
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.main import (
    app,
    get_code_execution_runner,
    get_project_service,
    get_workflow_runner,
)
from app.models.database import Database
from app.models.schemas import (
    DevelopmentTask,
    ProjectRequest,
    TaskPriority,
    TaskStatus,
)
from app.models.schemas import (
    TestResult as _TestResult,
)
from app.services.project_service import ProjectService
from app.services.workflow_service import WorkflowRunner
from app.tools.code_executor import ExecutionResult, ExecutionStatus


class FakeRunner:
    """Small synchronous control surface for API handler tests."""

    def __init__(self) -> None:
        self.started: list[tuple[int, ProjectRequest]] = []
        self.resumed: list[tuple[int, bool]] = []
        self.state: dict[str, Any] | None = None

    def start(
        self,
        project_id: int,
        request: ProjectRequest,
        service: ProjectService,
    ) -> bool:
        self.started.append((project_id, request))
        service.update_project(project_id, status="in_progress")
        return True

    def resume(
        self,
        project_id: int,
        approved: bool,
        service: ProjectService,
    ) -> bool:
        self.resumed.append((project_id, approved))
        service.update_project(project_id, status="in_progress")
        return True

    def inspect(self, _: int) -> dict[str, Any] | None:
        return self.state


class FakeCodeExecutionRunner:
    """Persist a deterministic result without launching Docker in API unit tests."""

    def start(
        self,
        project_id: int,
        files: list[Any],
        programming_language: str,
        entrypoint: str,
        service: ProjectService,
        *,
        timeout_seconds: int = 30,
    ) -> int:
        assert files
        assert programming_language == "Python"
        assert entrypoint == "main.py"
        assert timeout_seconds == 30
        record = service.record_agent_execution(
            project_id,
            agent_name="code_executor",
            input_data=json.dumps({"entrypoint": entrypoint}),
            status="running",
        )
        service.update_agent_execution(
            project_id,
            record.id,
            output_data=ExecutionResult(
                status=ExecutionStatus.COMPLETED,
                exit_code=0,
                stdout="Docker execution successful",
                execution_duration=0.1,
            ).model_dump_json(),
            status="completed",
            execution_duration=0.1,
        )
        return record.id


@pytest.fixture
def api_dependencies(tmp_path: Any):
    database = Database(f"sqlite:///{tmp_path / 'api-test.sqlite3'}")
    database.create_all()
    service = ProjectService(database, initialize_schema=False)
    runner = FakeRunner()
    execution_runner = FakeCodeExecutionRunner()
    app.dependency_overrides[get_project_service] = lambda: service
    app.dependency_overrides[get_workflow_runner] = lambda: runner
    app.dependency_overrides[get_code_execution_runner] = lambda: execution_runner
    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        yield client, service, runner
    app.dependency_overrides.clear()
    database.dispose()


def project_payload(name: str = "sample") -> dict[str, Any]:
    return {
        "project_name": name,
        "description": "A test project.",
        "requirements": ["Provide a simple calculation."],
        "programming_language": "Python",
        "preferred_framework": None,
    }


def create_project(client: TestClient, name: str = "sample") -> dict[str, Any]:
    response = client.post("/api/projects", json=project_payload(name))
    assert response.status_code == 201
    return response.json()


def test_create_list_and_get_projects(api_dependencies: Any) -> None:
    client, _, _ = api_dependencies
    created = create_project(client)

    assert created["status"] == "pending"
    assert created["name"] == "sample"
    assert client.get("/api/projects").json()[0]["id"] == created["id"]
    assert client.get(f"/api/projects/{created['id']}").json() == created
    assert client.get(f"/api/projects/{created['id']}/logs").json() == []


def test_invalid_project_request_and_duplicate_name(api_dependencies: Any) -> None:
    client, _, _ = api_dependencies

    invalid = client.post("/api/projects", json={"project_name": ""})
    assert invalid.status_code == 422

    create_project(client)
    duplicate = client.post("/api/projects", json=project_payload())
    assert duplicate.status_code == 409
    assert "already exists" in duplicate.json()["detail"]


def test_missing_project_routes_return_404(api_dependencies: Any) -> None:
    client, _, _ = api_dependencies

    assert client.get("/api/projects/999").status_code == 404
    assert client.get("/api/projects/999/status").status_code == 404
    assert client.get("/api/projects/999/tasks").status_code == 404
    assert client.get("/api/projects/999/files").status_code == 404
    assert client.get("/api/projects/999/logs").status_code == 404
    assert client.get("/api/projects/999/tests").status_code == 404
    assert client.get("/api/projects/999/workflow").status_code == 404
    assert client.get("/api/projects/999/execution").status_code == 404
    assert client.post("/api/projects/999/execute", json={}).status_code == 404


def test_start_uses_saved_request_and_prevents_duplicate_start(
    api_dependencies: Any,
) -> None:
    client, _, runner = api_dependencies
    project = create_project(client)

    response = client.post(f"/api/projects/{project['id']}/start")

    assert response.status_code == 202
    assert runner.started[0][1].project_name == "sample"
    assert client.post(f"/api/projects/{project['id']}/start").status_code == 409


def test_approval_is_only_accepted_while_waiting(api_dependencies: Any) -> None:
    client, service, runner = api_dependencies
    project = create_project(client)
    project_id = project["id"]

    assert (
        client.post(
            f"/api/projects/{project_id}/approval",
            json={"approved": True},
        ).status_code
        == 409
    )
    service.update_project(project_id, status="awaiting_approval")

    accepted = client.post(
        f"/api/projects/{project_id}/approval",
        json={"approved": False},
    )
    assert accepted.status_code == 202
    assert runner.resumed == [(project_id, False)]


def test_status_and_data_routes_expose_persisted_records_safely(
    api_dependencies: Any,
) -> None:
    client, service, runner = api_dependencies
    project = create_project(client)
    project_id = project["id"]
    runner.state = {
        "current_stage": "architecture",
        "project_status": "in_progress",
        "human_approval_status": "pending",
        "workflow_summary": None,
        "architecture_design": {
            "project_structure": ["src/"],
            "technology_stack": ["Python"],
            "design_decisions": [],
            "dependencies": [],
        },
    }
    service.save_tasks(
        project_id,
        [
            DevelopmentTask(
                task_id="base",
                title="Base",
                description="Build the base.",
                assigned_agent="developer",
                priority=TaskPriority.HIGH,
                status=TaskStatus.PENDING,
            ),
            DevelopmentTask(
                task_id="feature",
                title="Feature",
                description="Build the feature.",
                assigned_agent="developer",
                dependencies=["base"],
            ),
        ],
    )
    service.save_generated_file(
        project_id,
        file_path="main.py",
        file_content="print('not returned by files endpoint')",
    )
    service.record_agent_execution(
        project_id,
        agent_name="workflow",
        input_data="private prompt content",
        output_data=json.dumps(
            {
                "current_stage": "architecture",
                "project_status": "awaiting_approval",
                "human_approval_status": "pending",
                "workflow_summary": "Awaiting architecture approval.",
                "architecture_design": {
                    "project_structure": ["src/"],
                    "technology_stack": ["Python"],
                    "design_decisions": [],
                    "dependencies": [],
                },
                "programming_language": "Python",
                "requirements": ["Provide a simple calculation."],
                "development_tasks": [],
                "generated_files": [
                    {"file_path": "main.py", "programming_language": "Python"}
                ],
                "test_results": [],
                "code_review_results": [],
                "debugging_attempts": [],
                "error_messages": [],
                "max_debugging_attempts": 3,
            }
        ),
        status="in_progress",
    )
    service.record_test_execution(
        project_id,
        _TestResult(total_tests=1, passed_tests=1, failed_tests=0),
        execution_status="passed",
    )

    status_result = client.get(f"/api/projects/{project_id}/status").json()
    assert status_result["current_stage"] == "architecture"
    assert status_result["architecture_design"]["technology_stack"] == ["Python"]
    assert client.get(f"/api/projects/{project_id}/tasks").json()[1][
        "dependencies"
    ] == ["base"]
    files = client.get(f"/api/projects/{project_id}/files").json()
    assert files[0]["file_path"] == "main.py"
    assert "content" not in files[0]
    source = client.get(f"/api/projects/{project_id}/files/main.py")
    assert source.status_code == 200
    assert source.json()["content"] == "print('not returned by files endpoint')"
    workflow_details = client.get(f"/api/projects/{project_id}/workflow").json()
    assert workflow_details["requirements"] == ["Provide a simple calculation."]
    assert workflow_details["generated_files"][0]["file_path"] == "main.py"
    logs = client.get(f"/api/projects/{project_id}/logs").json()
    assert logs[0]["agent_name"] == "workflow"
    assert "private prompt content" not in str(logs)
    tests = client.get(f"/api/projects/{project_id}/tests").json()
    assert tests[0]["passed_tests"] == 1


def test_docker_execution_routes_use_saved_project_files(
    api_dependencies: Any,
) -> None:
    client, service, _ = api_dependencies
    project = create_project(client)
    project_id = project["id"]

    unavailable = client.post(f"/api/projects/{project_id}/execute", json={})
    assert unavailable.status_code == 409
    service.save_generated_file(
        project_id,
        file_path="main.py",
        file_content="print('real file contents')",
    )

    scheduled = client.post(f"/api/projects/{project_id}/execute", json={})
    assert scheduled.status_code == 202
    assert scheduled.json()["status"] == "running"
    result = client.get(f"/api/projects/{project_id}/execution")
    assert result.status_code == 200
    assert result.json()["status"] == "completed"
    assert result.json()["result"]["stdout"] == "Docker execution successful"

    invalid_timeout = client.post(
        f"/api/projects/{project_id}/execute",
        json={"timeout_seconds": 500},
    )
    assert invalid_timeout.status_code == 422


def test_workflow_execution_failure_is_persisted(tmp_path: Any) -> None:
    database = Database(f"sqlite:///{tmp_path / 'workflow-error.sqlite3'}")
    database.create_all()
    service = ProjectService(database, initialize_schema=False)
    project = service.create_project(ProjectRequest(**project_payload()))
    service.record_agent_execution(
        project.id,
        agent_name="api_request",
        input_data=ProjectRequest(**project_payload()).model_dump_json(),
        status="created",
    )

    class BrokenWorkflow:
        def stream(
            self,
            input_value: Any,
            config: dict[str, Any],
            *,
            stream_mode: str,
        ) -> Any:
            assert input_value is not None
            assert config["configurable"]["thread_id"] == f"project-{project.id}"
            assert stream_mode == "values"
            raise RuntimeError("mock agent execution failed")

    runner = WorkflowRunner(
        lambda: BrokenWorkflow(),
        checkpoint_db_path=tmp_path / "workflow-checkpoints.sqlite3",
        max_workers=1,
    )
    app.dependency_overrides[get_project_service] = lambda: service
    app.dependency_overrides[get_workflow_runner] = lambda: runner
    try:
        with TestClient(app, client=("127.0.0.1", 50000)) as client:
            response = client.post(f"/api/projects/{project.id}/start")
            assert response.status_code == 202
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                status_response = client.get(f"/api/projects/{project.id}/status")
                if (
                    status_response.json()["status"] == "failed"
                    and status_response.json()["workflow_summary"]
                    == "Workflow execution failed."
                ):
                    break
                time.sleep(0.01)
            assert status_response.json()["status"] == "failed"
            assert status_response.json()["workflow_summary"] == (
                "Workflow execution failed."
            )
    finally:
        app.dependency_overrides.clear()
        runner.shutdown()
        database.dispose()


def test_response_models_do_not_serialize_secrets_or_file_contents() -> None:
    schemas = app.openapi()["components"]["schemas"]
    assert "groq_api_key" not in schemas["ProjectResponse"]["properties"]
    assert "input" not in schemas["LogResponse"]["properties"]
    assert "output" not in schemas["LogResponse"]["properties"]
    assert "file_content" not in schemas["FileResponse"]["properties"]
