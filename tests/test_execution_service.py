"""Tests for background code execution orchestration and persistence."""

import json
import time
from threading import Event
from typing import Any

from app.models.database import Database
from app.models.schemas import GeneratedFile, ProjectRequest
from app.services.execution_service import CodeExecutionRunner
from app.services.project_service import ProjectService
from app.tools.code_executor import ExecutionResult, ExecutionStatus


class BlockingExecutor:
    """Wait for a test-controlled signal before returning a Docker-like result."""

    def __init__(self) -> None:
        self.started = Event()
        self.release = Event()

    def run(
        self,
        files: list[GeneratedFile],
        programming_language: str,
        entrypoint: str = "main.py",
        timeout_seconds: int = 30,
    ) -> ExecutionResult:
        assert files[0].file_path == entrypoint
        assert programming_language == "Python"
        assert timeout_seconds == 30
        self.started.set()
        assert self.release.wait(timeout=2)
        return ExecutionResult(
            status=ExecutionStatus.COMPLETED,
            exit_code=0,
            stdout="19 * 23 = 437",
            execution_duration=0.25,
        )


def test_execution_runs_in_background_and_persists_result(tmp_path: Any) -> None:
    database = Database(f"sqlite:///{tmp_path / 'execution.sqlite3'}")
    database.create_all()
    service = ProjectService(database, initialize_schema=False)
    project = service.create_project(
        ProjectRequest(
            project_name="sandbox-project",
            description="A harmless Docker execution test.",
            programming_language="Python",
        )
    )
    executor = BlockingExecutor()
    runner = CodeExecutionRunner(executor, max_workers=1)
    files = [
        GeneratedFile(
            file_path="main.py",
            content="print(19 * 23)",
            programming_language="Python",
        )
    ]

    try:
        execution_id = runner.start(
            project.id,
            files,
            "Python",
            "main.py",
            service,
        )
        assert execution_id is not None
        assert executor.started.wait(timeout=2)
        assert runner.is_active(project.id)
        assert runner.start(project.id, files, "Python", "main.py", service) is None

        executor.release.set()
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and runner.is_active(project.id):
            time.sleep(0.01)

        history = service.list_agent_executions(project.id)
        execution = next(item for item in history if item.id == execution_id)
        assert execution.status == "completed"
        assert execution.execution_duration == 0.25
        assert json.loads(execution.output)["stdout"] == "19 * 23 = 437"
    finally:
        executor.release.set()
        runner.shutdown()
        database.dispose()
