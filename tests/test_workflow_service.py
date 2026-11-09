"""Background runner tests using a deterministic mocked LangGraph."""

import time
from types import SimpleNamespace
from typing import Any

from langgraph.types import Command

from app.graph.state import ProjectStatus
from app.models.database import Database
from app.models.schemas import ArchitectureDesign, ProjectRequest
from app.services.project_service import ProjectService
from app.services.workflow_service import WorkflowRunner


class PausedWorkflow:
    """Mock a graph interrupt followed by its Command-based resume."""

    def __init__(self) -> None:
        self.snapshot = SimpleNamespace(values={}, tasks=())

    def stream(
        self,
        input_value: Any,
        _: dict[str, Any],
        *,
        stream_mode: str,
    ):
        assert stream_mode == "values"
        if isinstance(input_value, Command):
            assert input_value.resume == {"approved": True}
            values = {
                "current_stage": "complete",
                "project_status": ProjectStatus.COMPLETED,
                "human_approval_status": "approved",
                "workflow_summary": "Project completed.",
                "development_tasks": [],
                "generated_files": [],
                "test_results": [],
            }
            self.snapshot = SimpleNamespace(values=values, tasks=())
            yield values
            return

        values = {
            "current_stage": "architecture",
            "project_status": ProjectStatus.IN_PROGRESS,
            "human_approval_status": "pending",
            "workflow_summary": "",
            "development_tasks": [],
            "generated_files": [],
            "test_results": [],
        }
        self.snapshot = SimpleNamespace(
            values=values,
            tasks=(SimpleNamespace(interrupts=(object(),)),),
        )
        yield values

    def get_state(self, _: dict[str, Any]) -> Any:
        return self.snapshot


def _wait_for_status(
    runner: WorkflowRunner,
    service: ProjectService,
    project_id: int,
    expected: str,
) -> None:
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if service.get_project(project_id).status == expected and not runner.is_active(
            project_id
        ):
            return
        time.sleep(0.01)
    raise AssertionError(f"Workflow did not reach status {expected!r}.")


def test_runner_persists_interrupt_and_resumes_same_thread(tmp_path: Any) -> None:
    database = Database(f"sqlite:///{tmp_path / 'runner-test.sqlite3'}")
    database.create_all()
    service = ProjectService(database, initialize_schema=False)
    project = service.create_project(
        ProjectRequest(
            project_name="runner-project",
            description="Exercise the approval checkpoint.",
            requirements=["Wait for human approval."],
            programming_language="Python",
        )
    )
    workflow = PausedWorkflow()
    runner = WorkflowRunner(
        lambda: workflow,
        checkpoint_db_path=tmp_path / "workflow-checkpoints.sqlite3",
        max_workers=1,
    )
    try:
        assert runner.start(
            project.id,
            ProjectRequest(
                project_name="runner-project",
                description="Exercise the approval checkpoint.",
                requirements=["Wait for human approval."],
                programming_language="Python",
            ),
            service,
        )
        _wait_for_status(runner, service, project.id, "awaiting_approval")
        status_events = service.list_agent_executions(project.id)
        assert '"human_approval_status": "pending"' in status_events[-1].output
        assert runner.resume(project.id, True, service)
        _wait_for_status(runner, service, project.id, "completed")
        final_status = service.list_agent_executions(project.id)[-1].output
        assert '"human_approval_status": "approved"' in final_status
    finally:
        runner.shutdown()
        database.dispose()


def test_workflow_details_preserves_architecture_after_approval() -> None:
    architecture = ArchitectureDesign(
        project_structure=["src/main.py"],
        technology_stack=["Python"],
    )

    details = WorkflowRunner._workflow_details(
        {
            "architecture_design": architecture,
            "original_request": ProjectRequest(
                project_name="snapshot-project",
                description="Check persisted report details.",
                programming_language="Python",
            ),
        }
    )

    assert details["architecture_design"] == architecture.model_dump(mode="json")
