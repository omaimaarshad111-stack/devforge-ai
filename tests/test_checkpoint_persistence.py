"""Checkpoint recovery across a simulated API process restart."""

import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.graph.workflow import build_workflow
from app.main import app, get_project_service, get_workflow_runner
from app.models.database import Database
from app.models.schemas import ProjectRequest
from app.services.project_service import ProjectService
from app.services.workflow_service import WorkflowRunner
from app.tools.file_manager import FileManager
from tests.test_workflow import MockAnalyzer, make_agents


def _wait_for_status(
    client: TestClient, project_id: int, expected: str
) -> dict[str, Any]:
    deadline = time.monotonic() + 10
    latest: dict[str, Any] = {}
    while time.monotonic() < deadline:
        response = client.get(f"/api/projects/{project_id}/status")
        assert response.status_code == 200
        latest = response.json()
        if latest["status"] == expected:
            return latest
        time.sleep(0.02)
    raise AssertionError(f"Project did not reach {expected!r}; last status: {latest}")


def _runner(
    checkpoint_path: Path,
    generated_projects_path: Path,
    *,
    agents: Any,
) -> WorkflowRunner:
    return WorkflowRunner(
        workflow_builder=lambda checkpointer: build_workflow(
            agents=agents,
            file_manager=FileManager(generated_projects_path),
            analyzer=MockAnalyzer(),
            checkpointer=checkpointer,
        ),
        checkpoint_db_path=checkpoint_path,
        max_workers=1,
    )


def test_approval_checkpoint_survives_api_runner_restart(tmp_path: Path) -> None:
    project_database = Database(f"sqlite:///{tmp_path / 'projects.sqlite3'}")
    project_database.create_all()
    service = ProjectService(project_database, initialize_schema=False)
    request = ProjectRequest(
        project_name="restart-checkpoint-project",
        description="Prove approval survives an API process restart.",
        requirements=["Create one simple calculator function."],
        programming_language="Python",
    )
    checkpoint_path = tmp_path / "workflow-checkpoints.sqlite3"
    generated_projects_path = tmp_path / "generated"
    first_agents, _ = make_agents()
    first_runner = _runner(
        checkpoint_path,
        generated_projects_path,
        agents=first_agents,
    )
    app.dependency_overrides[get_project_service] = lambda: service
    app.dependency_overrides[get_workflow_runner] = lambda: first_runner

    try:
        with TestClient(app, client=("127.0.0.1", 50000)) as first_api:
            created = first_api.post(
                "/api/projects", json=request.model_dump(mode="json")
            )
            assert created.status_code == 201
            project_id = created.json()["id"]
            started = first_api.post(f"/api/projects/{project_id}/start")
            assert started.status_code == 202

            pending = _wait_for_status(first_api, project_id, "awaiting_approval")
            assert pending["human_approval_status"] == "pending"
            assert pending["architecture_design"] is not None
            assert checkpoint_path.exists()

            first_runner.shutdown()

        # A fresh runner and graph load the same persisted thread from disk.
        second_agents, second_parts = make_agents()
        second_runner = _runner(
            checkpoint_path,
            generated_projects_path,
            agents=second_agents,
        )
        app.dependency_overrides[get_workflow_runner] = lambda: second_runner
        try:
            with TestClient(app, client=("127.0.0.1", 50000)) as restarted_api:
                status_after_restart = restarted_api.get(
                    f"/api/projects/{project_id}/status"
                ).json()
                assert status_after_restart["status"] == "awaiting_approval"

                resumed = restarted_api.post(
                    f"/api/projects/{project_id}/approval",
                    json={"approved": True},
                )
                assert resumed.status_code == 202

                completed = _wait_for_status(restarted_api, project_id, "completed")
                assert completed["human_approval_status"] == "approved"
                final_details = restarted_api.get(
                    f"/api/projects/{project_id}/workflow"
                ).json()
                assert (
                    final_details["architecture_design"]
                    == pending["architecture_design"]
                )
                assert second_parts["developer"].calls == ["implement"]
                assert second_parts["architect"].calls == 0
        finally:
            second_runner.shutdown()
    finally:
        app.dependency_overrides.clear()
        first_runner.shutdown()
        project_database.dispose()


def test_workflow_thread_ids_are_stable_and_project_scoped() -> None:
    assert WorkflowRunner._config(27) == {"configurable": {"thread_id": "project-27"}}
    assert WorkflowRunner._config(28) == {"configurable": {"thread_id": "project-28"}}


@pytest.mark.parametrize("corrupted", [False, True])
def test_unavailable_checkpoint_fails_closed(
    tmp_path: Path,
    corrupted: bool,
) -> None:
    project_database = Database(f"sqlite:///{tmp_path / 'projects.sqlite3'}")
    project_database.create_all()
    service = ProjectService(project_database, initialize_schema=False)
    project = service.create_project(
        ProjectRequest(
            project_name=f"unavailable-{corrupted}",
            description="Exercise missing or invalid checkpoint handling.",
            requirements=["Wait for approval."],
            programming_language="Python",
        ),
        status="awaiting_approval",
    )
    checkpoint_path = tmp_path / f"checkpoint-{corrupted}.sqlite3"
    if corrupted:
        checkpoint_path.write_bytes(b"not a SQLite database")
    agents, _ = make_agents()
    runner = WorkflowRunner(
        workflow_builder=lambda checkpointer: build_workflow(
            agents=agents,
            file_manager=FileManager(tmp_path / "generated"),
            analyzer=MockAnalyzer(),
            checkpointer=checkpointer,
        ),
        checkpoint_db_path=checkpoint_path,
    )
    try:
        assert not runner.resume(project.id, True, service)
        assert service.get_project(project.id).status == "failed"
        latest_status = service.list_agent_executions(project.id)[-1]
        assert "checkpoint is missing or unreadable" in latest_status.output
    finally:
        runner.shutdown()
        project_database.dispose()
