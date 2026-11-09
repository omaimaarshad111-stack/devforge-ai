"""Background orchestration for the existing isolated Docker code executor."""

import atexit
import json
import logging
from concurrent.futures import ThreadPoolExecutor
from threading import Lock
from typing import Protocol

from app.models.schemas import GeneratedFile
from app.services.project_service import ProjectService
from app.tools.code_executor import (
    DockerCodeExecutor,
    ExecutionResult,
    ExecutionStatus,
)

logger = logging.getLogger(__name__)


class CodeExecutor(Protocol):
    """Interface implemented by the Docker sandbox and deterministic test fakes."""

    def run(
        self,
        files: list[GeneratedFile],
        programming_language: str,
        entrypoint: str = "main.py",
        timeout_seconds: int = 30,
    ) -> ExecutionResult: ...


class CodeExecutionRunner:
    """Execute saved generated files off-request with one run per project."""

    def __init__(
        self,
        executor: CodeExecutor | None = None,
        *,
        max_workers: int = 2,
    ) -> None:
        if max_workers < 1:
            raise ValueError("max_workers must be positive.")
        self._executor = executor or DockerCodeExecutor()
        self._pool = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="devforge-code-execution",
        )
        self._lock = Lock()
        self._active: set[int] = set()
        self._closed = False

    def start(
        self,
        project_id: int,
        files: list[GeneratedFile],
        programming_language: str,
        entrypoint: str,
        service: ProjectService,
        timeout_seconds: int = 30,
    ) -> int | None:
        """Create a running record and schedule the sandbox job once."""
        with self._lock:
            if self._closed:
                raise RuntimeError("CodeExecutionRunner has been shut down.")
            if project_id in self._active:
                return None
            self._active.add(project_id)
        try:
            record = service.record_agent_execution(
                project_id,
                agent_name="code_executor",
                input_data=json.dumps({"entrypoint": entrypoint}),
                status="running",
            )
            self._pool.submit(
                self._run,
                project_id,
                record.id,
                files,
                programming_language,
                entrypoint,
                timeout_seconds,
                service,
            )
            return record.id
        except Exception:
            with self._lock:
                self._active.discard(project_id)
            raise

    def is_active(self, project_id: int) -> bool:
        """Report whether a project currently has an execution in flight."""
        with self._lock:
            return project_id in self._active

    def shutdown(self) -> None:
        """Wait for active isolated executions before stopping the worker pool."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self._pool.shutdown(wait=True)

    def _run(
        self,
        project_id: int,
        execution_id: int,
        files: list[GeneratedFile],
        programming_language: str,
        entrypoint: str,
        timeout_seconds: int,
        service: ProjectService,
    ) -> None:
        try:
            result = self._executor.run(
                files,
                programming_language,
                entrypoint=entrypoint,
                timeout_seconds=timeout_seconds,
            )
        except Exception:
            logger.exception(
                "Isolated code execution failed unexpectedly for project %s.",
                project_id,
            )
            result = ExecutionResult(
                status=ExecutionStatus.ERROR,
                execution_error="The isolated execution tool failed unexpectedly.",
            )
        try:
            service.update_agent_execution(
                project_id,
                execution_id,
                output_data=result.model_dump_json(),
                status=result.status.value,
                execution_duration=result.execution_duration,
            )
        except Exception:
            logger.exception(
                "Could not persist code execution result for project %s.",
                project_id,
            )
        finally:
            with self._lock:
                self._active.discard(project_id)


code_execution_runner = CodeExecutionRunner()
atexit.register(code_execution_runner.shutdown)
