"""Background execution and persistence for project LangGraph workflows."""

import atexit
import json
import logging
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from pathlib import Path
from threading import Lock, RLock
from typing import Any

from langgraph.types import Command

from app.config.settings import settings
from app.graph.state import DevForgeState, ProjectStatus, create_initial_state
from app.graph.workflow import build_workflow
from app.models.schemas import ProjectRequest
from app.services.project_service import ProjectService

logger = logging.getLogger(__name__)


class WorkflowRunner:
    """Run workflows off-request while retaining in-process approval checkpoints."""

    def __init__(
        self,
        workflow_factory: Callable[[], Any] | None = None,
        *,
        workflow_builder: Callable[[Any], Any] | None = None,
        checkpoint_db_path: str | Path | None = None,
        max_workers: int = 4,
    ) -> None:
        if max_workers < 1:
            raise ValueError("max_workers must be positive.")
        if workflow_factory is not None and workflow_builder is not None:
            raise ValueError("Provide workflow_factory or workflow_builder, not both.")
        self._workflow_factory = workflow_factory
        self._workflow_builder = workflow_builder
        self._checkpoint_db_path = (
            Path(checkpoint_db_path or settings.workflow_checkpoint_db_path)
            .expanduser()
            .resolve()
        )
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="devforge-workflow",
        )
        self._lock = Lock()
        self._checkpoint_lock = RLock()
        self._execution_lock = Lock()
        self._active_projects: set[int] = set()
        self._workflow: Any | None = None
        self._checkpointer: Any | None = None
        self._checkpointer_context: ExitStack | None = None
        self._closed = False

    def start(
        self,
        project_id: int,
        request: ProjectRequest,
        service: ProjectService,
    ) -> bool:
        """Schedule a new workflow unless this project already has active work."""
        with self._lock:
            if self._closed:
                raise RuntimeError("WorkflowRunner has been shut down.")
            if project_id in self._active_projects:
                return False
            self._active_projects.add(project_id)
        try:
            service.update_project(project_id, status="in_progress")
            service.record_agent_execution(
                project_id,
                agent_name="workflow",
                input_data="Workflow scheduled.",
                output_data=self._status_json(
                    current_stage="intake",
                    project_status="in_progress",
                    human_approval_status="pending",
                ),
                status="in_progress",
            )
            self._executor.submit(
                self._run,
                project_id,
                service,
                create_initial_state(request),
                None,
            )
        except Exception:
            with self._lock:
                self._active_projects.discard(project_id)
            raise
        return True

    def resume(
        self,
        project_id: int,
        approved: bool,
        service: ProjectService,
    ) -> bool:
        """Resume an interrupted architecture-approval checkpoint."""
        with self._lock:
            if project_id in self._active_projects:
                return False
        try:
            workflow = self._get_workflow()
        except Exception:
            logger.exception(
                "Could not open the approval checkpoint for project %s.",
                project_id,
            )
            self._mark_checkpoint_unavailable(project_id, service)
            return False
        get_state = getattr(workflow, "get_state", None)
        if get_state is None:
            self._mark_checkpoint_unavailable(project_id, service)
            return False
        try:
            with self._checkpoint_lock:
                snapshot = get_state(self._config(project_id))
        except Exception:
            logger.exception(
                "Could not read the approval checkpoint for project %s.",
                project_id,
            )
            self._mark_checkpoint_unavailable(project_id, service)
            return False
        if not any(
            bool(getattr(task, "interrupts", ()))
            for task in getattr(snapshot, "tasks", ())
        ):
            self._mark_checkpoint_unavailable(project_id, service)
            return False
        with self._lock:
            if self._closed or project_id in self._active_projects:
                return False
            self._active_projects.add(project_id)
        try:
            service.update_project(project_id, status="in_progress")
            self._executor.submit(
                self._run,
                project_id,
                service,
                None,
                {"approved": approved},
            )
        except Exception:
            with self._lock:
                self._active_projects.discard(project_id)
            raise
        return True

    def shutdown(self) -> None:
        """Stop background work and close the persistent checkpoint connection."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self._executor.shutdown(wait=True)
        with self._lock:
            context = self._checkpointer_context
            self._checkpointer_context = None
            self._checkpointer = None
            self._workflow = None
        if context is not None:
            try:
                context.close()
            except Exception:
                logger.exception("Could not close the workflow checkpoint database.")

    def is_active(self, project_id: int) -> bool:
        """Return whether the project's graph is currently executing."""
        with self._lock:
            return project_id in self._active_projects

    def inspect(self, project_id: int) -> dict[str, Any] | None:
        """Read the current checkpoint state when it exists in this process."""
        workflow = self._workflow
        if workflow is None:
            return None
        get_state = getattr(workflow, "get_state", None)
        if get_state is None:
            return None
        try:
            with self._checkpoint_lock:
                snapshot = get_state(self._config(project_id))
        except Exception:
            logger.exception(
                "Could not inspect the workflow checkpoint for project %s.",
                project_id,
            )
            return None
        values = getattr(snapshot, "values", None)
        if not isinstance(values, dict) or not values:
            return None
        architecture = values.get("architecture_design")
        return {
            "current_stage": self._value(values.get("current_stage")),
            "project_status": self._value(values.get("project_status")),
            "human_approval_status": self._value(values.get("human_approval_status")),
            "workflow_summary": values.get("workflow_summary") or None,
            "architecture_design": (
                architecture.model_dump(mode="json")
                if architecture is not None
                else None
            ),
        }

    @staticmethod
    def _config(project_id: int) -> dict[str, Any]:
        return {"configurable": {"thread_id": f"project-{project_id}"}}

    def _run(
        self,
        project_id: int,
        service: ProjectService,
        initial_state: DevForgeState | None,
        resume_value: dict[str, bool] | None,
    ) -> None:
        latest_values: dict[str, Any] = {}
        try:
            with self._execution_lock:
                workflow = self._get_workflow()
                config = self._config(project_id)
                input_value: Any = (
                    Command(resume=resume_value)
                    if resume_value is not None
                    else initial_state
                )
                for values in workflow.stream(
                    input_value,
                    config,
                    stream_mode="values",
                ):
                    if isinstance(values, dict):
                        latest_values = values
                        self._persist_progress(project_id, service, values)

                snapshot = workflow.get_state(config)
                state_values = getattr(snapshot, "values", None)
                if isinstance(state_values, dict) and state_values:
                    latest_values = state_values
                interrupted = any(
                    bool(getattr(task, "interrupts", ()))
                    for task in getattr(snapshot, "tasks", ())
                )
            if interrupted:
                status = "awaiting_approval"
                approval = "pending"
            else:
                status = self._value(
                    latest_values.get("project_status", ProjectStatus.FAILED)
                )
                approval = self._value(latest_values.get("human_approval_status"))
            self._persist_state(project_id, service, latest_values)
            service.update_project(project_id, status=status)
            self._record_status(
                project_id,
                service,
                stage=self._value(latest_values.get("current_stage")),
                status=status,
                approval=approval,
                summary=latest_values.get("workflow_summary"),
                architecture=(
                    latest_values.get("architecture_design") if interrupted else None
                ),
                details=self._workflow_details(latest_values),
            )
        except Exception:
            logger.exception("Workflow execution failed for project %s.", project_id)
            try:
                service.update_project(project_id, status="failed")
                self._record_status(
                    project_id,
                    service,
                    stage=None,
                    status="failed",
                    approval=None,
                    summary="Workflow execution failed.",
                    details=self._workflow_details(latest_values),
                )
            except Exception:
                logger.exception(
                    "Could not persist workflow failure for project %s.", project_id
                )
        finally:
            with self._lock:
                self._active_projects.discard(project_id)

    def _get_workflow(self) -> Any:
        with self._lock:
            if self._closed:
                raise RuntimeError("WorkflowRunner has been shut down.")
            if self._workflow is None:
                if self._workflow_factory is not None:
                    self._workflow = self._workflow_factory()
                else:
                    checkpointer = self._get_checkpointer()
                    if self._workflow_builder is not None:
                        self._workflow = self._workflow_builder(checkpointer)
                    else:
                        self._workflow = build_workflow(checkpointer=checkpointer)
            return self._workflow

    def _get_checkpointer(self) -> Any:
        if self._checkpointer is None:
            from langgraph.checkpoint.sqlite import SqliteSaver

            self._checkpoint_db_path.parent.mkdir(parents=True, exist_ok=True)
            context = ExitStack()
            self._checkpointer = context.enter_context(
                SqliteSaver.from_conn_string(str(self._checkpoint_db_path))
            )
            self._checkpointer_context = context
        return self._checkpointer

    def _mark_checkpoint_unavailable(
        self,
        project_id: int,
        service: ProjectService,
    ) -> None:
        """Fail closed when an awaiting project has no usable approval checkpoint."""
        try:
            service.update_project(project_id, status="failed")
            self._record_status(
                project_id,
                service,
                stage=None,
                status="failed",
                approval=None,
                summary="The workflow approval checkpoint is missing or unreadable.",
            )
        except Exception:
            logger.exception(
                "Could not record missing checkpoint status for project %s.",
                project_id,
            )

    def _persist_progress(
        self,
        project_id: int,
        service: ProjectService,
        values: dict[str, Any],
    ) -> None:
        stage = self._value(values.get("current_stage"))
        approval = self._value(values.get("human_approval_status"))
        self._persist_artifacts(project_id, service, values)
        self._record_status(
            project_id,
            service,
            stage=stage,
            status="in_progress",
            approval=approval,
            summary=None,
            details=self._workflow_details(values),
        )

    def _persist_state(
        self,
        project_id: int,
        service: ProjectService,
        state: dict[str, Any],
    ) -> None:
        self._persist_artifacts(project_id, service, state)
        for result in state.get("test_results", []):
            service.record_test_execution(
                project_id,
                result,
                execution_status=(
                    "failed"
                    if result.execution_error or result.failed_tests
                    else "passed"
                ),
            )

    @staticmethod
    def _persist_artifacts(
        project_id: int,
        service: ProjectService,
        state: dict[str, Any],
    ) -> None:
        tasks = state.get("development_tasks", [])
        if tasks:
            service.save_tasks(project_id, tasks)
        for generated_file in state.get("generated_files", []):
            service.save_generated_file(
                project_id,
                file_path=generated_file.file_path,
                file_content=generated_file.content,
            )

    @staticmethod
    def _workflow_details(state: dict[str, Any]) -> dict[str, Any]:
        """Create a JSON-safe, source-free report snapshot for API consumers."""
        request = state.get("original_request")
        architecture = state.get("architecture_design")
        generated_files = state.get("generated_files", [])
        return {
            "architecture_design": (
                architecture.model_dump(mode="json")
                if architecture is not None
                else None
            ),
            "programming_language": (
                request.programming_language if request is not None else None
            ),
            "preferred_framework": (
                request.preferred_framework if request is not None else None
            ),
            "requirements": list(state.get("project_requirements", [])),
            "development_tasks": [
                task.model_dump(mode="json")
                for task in state.get("development_tasks", [])
            ],
            "generated_files": [
                {
                    "file_path": generated_file.file_path,
                    "programming_language": generated_file.programming_language,
                }
                for generated_file in generated_files
            ],
            "test_results": [
                result.model_dump(mode="json")
                for result in state.get("test_results", [])
            ],
            "code_analysis_results": [
                result.model_dump(mode="json")
                for result in state.get("code_analysis_results", [])
            ],
            "code_review_results": [
                result.model_dump(mode="json")
                for result in state.get("code_review_results", [])
            ],
            "debugging_attempts": list(state.get("debugging_attempts", [])),
            "error_messages": list(state.get("error_messages", [])),
            "max_debugging_attempts": state.get("max_debugging_attempts", 3),
        }

    def _record_status(
        self,
        project_id: int,
        service: ProjectService,
        *,
        stage: str | None,
        status: str,
        approval: str | None,
        summary: str | None,
        architecture: Any | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        data: dict[str, Any] = {
            "current_stage": stage,
            "project_status": status,
            "human_approval_status": approval,
            "workflow_summary": summary,
        }
        if architecture is not None:
            data["architecture_design"] = architecture.model_dump(mode="json")
        if details is not None:
            data.update(details)
        service.record_agent_execution(
            project_id,
            agent_name="workflow",
            input_data="Workflow status update.",
            output_data=json.dumps(data),
            status=status,
        )

    @staticmethod
    def _status_json(
        *,
        current_stage: str,
        project_status: str,
        human_approval_status: str,
    ) -> str:
        return json.dumps(
            {
                "current_stage": current_stage,
                "project_status": project_status,
                "human_approval_status": human_approval_status,
                "workflow_summary": None,
            }
        )

    @staticmethod
    def _value(value: Any) -> str | None:
        return getattr(value, "value", value)


workflow_runner = WorkflowRunner()
atexit.register(workflow_runner.shutdown)
