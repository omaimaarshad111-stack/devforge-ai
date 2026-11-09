"""FastAPI endpoints for DevForge AI project management and workflows."""

import hmac
import ipaddress
import json
import logging
from datetime import UTC, datetime
from functools import lru_cache
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field

from app.config.settings import settings
from app.models.database import (
    AgentExecution,
    DevelopmentTaskRecord,
    GeneratedFileRecord,
    Project,
    TestExecution,
)
from app.models.schemas import (
    ArchitectureDesign,
    GeneratedFile,
    ProjectRequest,
    ReviewResult,
    TestResult,
)
from app.services.execution_service import CodeExecutionRunner, code_execution_runner
from app.services.project_service import (
    ProjectNotFoundError,
    ProjectService,
    ProjectServiceError,
)
from app.services.workflow_service import WorkflowRunner, workflow_runner
from app.tools.code_analyzer import CodeAnalysisResult
from app.tools.code_executor import ExecutionResult

logger = logging.getLogger(__name__)


class ProjectResponse(BaseModel):
    """Public project metadata without internal configuration or secrets."""

    id: int
    name: str
    description: str
    status: str
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_project(cls, project: Project) -> "ProjectResponse":
        """Convert a detached ORM project to the public API shape."""
        return cls(
            id=project.id,
            name=project.name,
            description=project.description,
            status=project.status,
            created_at=_utc_datetime(project.created_at),
            updated_at=_utc_datetime(project.updated_at),
        )


class TaskResponse(BaseModel):
    """Task details and dependency IDs."""

    task_id: str
    title: str
    description: str
    assigned_agent: str
    dependencies: list[str]
    priority: str
    status: str

    @classmethod
    def from_task(cls, task: DevelopmentTaskRecord) -> "TaskResponse":
        return cls(
            task_id=task.task_id,
            title=task.title,
            description=task.description,
            assigned_agent=task.assigned_agent,
            dependencies=[dependency.task_id for dependency in task.dependencies],
            priority=task.priority,
            status=task.status,
        )


class FileResponse(BaseModel):
    """Generated file metadata; source contents are not returned by list routes."""

    file_path: str
    version: int
    timestamp: datetime

    @classmethod
    def from_file(cls, generated_file: GeneratedFileRecord) -> "FileResponse":
        return cls(
            file_path=generated_file.file_path,
            version=generated_file.version,
            timestamp=_utc_datetime(generated_file.timestamp),
        )


class FileContentResponse(FileResponse):
    """A project-scoped generated file version returned for source inspection."""

    content: str
    programming_language: str | None = None


class LogResponse(BaseModel):
    """Safe execution metadata without raw prompts, outputs, or credentials."""

    agent_name: str
    status: str
    message: str
    timestamp: datetime
    execution_duration: float | None

    @classmethod
    def from_execution(cls, execution: AgentExecution) -> "LogResponse":
        message = "Execution metadata recorded."
        if execution.agent_name == "workflow":
            try:
                status_data: Any = json.loads(execution.output)
            except (TypeError, ValueError):
                status_data = {}
            stage = (
                status_data.get("current_stage")
                if isinstance(status_data, dict)
                else None
            )
            message = f"Workflow stage: {stage or execution.status}."
        return cls(
            agent_name=execution.agent_name,
            status=execution.status,
            message=message,
            timestamp=_utc_datetime(execution.timestamp),
            execution_duration=execution.execution_duration,
        )


class TestExecutionResponse(BaseModel):
    """Persisted test totals and execution status."""

    total_tests: int
    passed_tests: int
    failed_tests: int
    execution_status: str
    timestamp: datetime

    @classmethod
    def from_execution(cls, execution: TestExecution) -> "TestExecutionResponse":
        return cls(
            total_tests=execution.total_tests,
            passed_tests=execution.passed_tests,
            failed_tests=execution.failed_tests,
            execution_status=execution.execution_status,
            timestamp=_utc_datetime(execution.timestamp),
        )


class WorkflowStatusResponse(BaseModel):
    """Current persisted workflow state and approval checkpoint, if any."""

    project_id: int
    status: str
    current_stage: str | None = None
    human_approval_status: str | None = None
    workflow_summary: str | None = None
    architecture_design: ArchitectureDesign | None = None


class WorkflowDetailsResponse(WorkflowStatusResponse):
    """Persisted development artifacts for the workflow visualization/report."""

    programming_language: str | None = None
    preferred_framework: str | None = None
    requirements: list[str] = Field(default_factory=list)
    development_tasks: list[dict[str, Any]] = Field(default_factory=list)
    generated_files: list[dict[str, str]] = Field(default_factory=list)
    test_results: list[TestResult] = Field(default_factory=list)
    code_analysis_results: list[CodeAnalysisResult] = Field(default_factory=list)
    code_review_results: list[ReviewResult] = Field(default_factory=list)
    debugging_attempts: list[dict[str, Any]] = Field(default_factory=list)
    error_messages: list[str] = Field(default_factory=list)
    max_debugging_attempts: int = 3


class ApprovalRequest(BaseModel):
    """Human decision submitted for the proposed project architecture."""

    model_config = ConfigDict(extra="forbid")

    approved: bool


class WorkflowAcceptedResponse(BaseModel):
    """Acknowledgement for a newly scheduled background operation."""

    project_id: int
    status: str
    message: str


class ExecutionRequest(BaseModel):
    """Bounded request to run a saved project entry point in Docker."""

    model_config = ConfigDict(extra="forbid")

    entrypoint: str = "main.py"
    timeout_seconds: int = Field(default=30, ge=1, le=60)


class ExecutionAcceptedResponse(BaseModel):
    """Acknowledgement for a background Docker execution."""

    project_id: int
    execution_id: int
    status: str
    message: str


class ExecutionStatusResponse(BaseModel):
    """Latest persisted Docker execution result, if one has completed."""

    project_id: int
    execution_id: int
    entrypoint: str
    status: str
    timestamp: datetime
    result: ExecutionResult | None = None


@lru_cache(maxsize=1)
def get_project_service() -> ProjectService:
    """Create the configured persistence service on first API use."""
    return ProjectService()


def get_workflow_runner() -> WorkflowRunner:
    """Provide the process-wide background workflow runner."""
    return workflow_runner


def get_code_execution_runner() -> CodeExecutionRunner:
    """Provide the process-wide isolated execution worker."""
    return code_execution_runner


ProjectServiceDependency = Annotated[ProjectService, Depends(get_project_service)]
WorkflowRunnerDependency = Annotated[WorkflowRunner, Depends(get_workflow_runner)]
CodeExecutionRunnerDependency = Annotated[
    CodeExecutionRunner,
    Depends(get_code_execution_runner),
]

app = FastAPI(
    title=settings.app_name,
    description=(
        "Manage software projects and run asynchronous multi-agent "
        "development workflows."
    ),
    version="0.2.0",
)


@app.middleware("http")
async def protect_remote_api(request: Request, call_next: Any) -> Any:
    """Require a configured bearer token for non-loopback API clients."""
    if request.url.path == "/health":
        return await call_next(request)
    client_host = request.client.host if request.client is not None else ""
    try:
        is_loopback = ipaddress.ip_address(client_host).is_loopback
    except ValueError:
        is_loopback = False

    configured_token = settings.api_access_token
    if configured_token is None:
        if not is_loopback:
            return _http_error(
                status.HTTP_403_FORBIDDEN,
                "Remote API access is disabled until API_ACCESS_TOKEN is configured.",
            )
    else:
        supplied_token = request.headers.get("authorization", "")
        expected = f"Bearer {configured_token.get_secret_value()}"
        if not hmac.compare_digest(supplied_token, expected):
            return _http_error(
                status.HTTP_401_UNAUTHORIZED,
                "A valid bearer token is required.",
            )
    return await call_next(request)


@app.exception_handler(ProjectNotFoundError)
async def project_not_found_handler(_: Any, error: ProjectNotFoundError) -> Any:
    return _http_error(status.HTTP_404_NOT_FOUND, str(error))


@app.exception_handler(ProjectServiceError)
async def project_service_error_handler(_: Any, error: ProjectServiceError) -> Any:
    logger.exception("Project persistence operation failed.", exc_info=error)
    return _http_error(
        status.HTTP_500_INTERNAL_SERVER_ERROR,
        "The project operation could not be completed.",
    )


@app.get("/health", tags=["health"])
def health_check() -> dict[str, str]:
    """Report that the API process is running."""
    return {"status": "ok"}


@app.post(
    "/api/projects",
    response_model=ProjectResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["projects"],
)
def create_project(
    request: ProjectRequest,
    service: ProjectServiceDependency,
) -> ProjectResponse:
    """Validate and persist a new project request."""
    try:
        project = service.create_project(request)
        service.record_agent_execution(
            project.id,
            agent_name="api_request",
            input_data=request.model_dump_json(),
            output_data="",
            status="created",
            idempotency_key="project-request",
        )
    except ProjectServiceError as error:
        if "already exists" in str(error).casefold():
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="A project with this name already exists.",
            ) from error
        raise
    return ProjectResponse.from_project(project)


@app.get(
    "/api/projects",
    response_model=list[ProjectResponse],
    tags=["projects"],
)
def list_projects(
    service: ProjectServiceDependency,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[ProjectResponse]:
    """List projects with bounded pagination."""
    return [
        ProjectResponse.from_project(project)
        for project in service.list_projects(limit=limit, offset=offset)
    ]


@app.get(
    "/api/projects/{project_id}",
    response_model=ProjectResponse,
    tags=["projects"],
)
def get_project(
    project_id: int,
    service: ProjectServiceDependency,
) -> ProjectResponse:
    """Return one project's public metadata."""
    return ProjectResponse.from_project(service.get_project(project_id))


@app.post(
    "/api/projects/{project_id}/start",
    response_model=WorkflowAcceptedResponse,
    status_code=status.HTTP_202_ACCEPTED,
    tags=["workflows"],
)
def start_project(
    project_id: int,
    service: ProjectServiceDependency,
    runner: WorkflowRunnerDependency,
) -> WorkflowAcceptedResponse:
    """Schedule a project's long-running LangGraph workflow."""
    project = service.get_project(project_id)
    if project.status != "pending":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Project cannot start while its status is '{project.status}'.",
        )
    request = _load_project_request(service, project_id)
    if not runner.start(project_id, request, service):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A workflow is already running for this project.",
        )
    return WorkflowAcceptedResponse(
        project_id=project_id,
        status="in_progress",
        message="Workflow scheduled for background execution.",
    )


@app.get(
    "/api/projects/{project_id}/status",
    response_model=WorkflowStatusResponse,
    tags=["workflows"],
)
def get_workflow_status(
    project_id: int,
    service: ProjectServiceDependency,
    runner: WorkflowRunnerDependency,
) -> WorkflowStatusResponse:
    """Return persisted workflow progress and any pending approval design."""
    project = service.get_project(project_id)
    persisted = _latest_workflow_status(service, project_id)
    current = runner.inspect(project_id)
    values = current or persisted or {}
    architecture = values.get("architecture_design")
    if isinstance(architecture, dict):
        architecture = ArchitectureDesign.model_validate(architecture)
    return WorkflowStatusResponse(
        project_id=project_id,
        status=project.status,
        current_stage=values.get("current_stage"),
        human_approval_status=values.get("human_approval_status"),
        workflow_summary=values.get("workflow_summary"),
        architecture_design=architecture,
    )


@app.get(
    "/api/projects/{project_id}/tasks",
    response_model=list[TaskResponse],
    tags=["projects"],
)
def get_project_tasks(
    project_id: int,
    service: ProjectServiceDependency,
) -> list[TaskResponse]:
    """Return development tasks and their dependency IDs."""
    service.get_project(project_id)
    return [TaskResponse.from_task(task) for task in service.list_tasks(project_id)]


@app.get(
    "/api/projects/{project_id}/files",
    response_model=list[FileResponse],
    tags=["projects"],
)
def get_project_files(
    project_id: int,
    service: ProjectServiceDependency,
    include_history: bool = False,
) -> list[FileResponse]:
    """List generated file paths and versions without returning source contents."""
    return [
        FileResponse.from_file(generated_file)
        for generated_file in service.list_generated_files(
            project_id,
            include_history=include_history,
        )
    ]


@app.get(
    "/api/projects/{project_id}/files/{file_path:path}",
    response_model=FileContentResponse,
    tags=["projects"],
)
def get_project_file_content(
    project_id: int,
    file_path: str,
    service: ProjectServiceDependency,
    version: Annotated[int | None, Query(ge=1)] = None,
) -> FileContentResponse:
    """Return one stored file version; never reads an arbitrary host path."""
    service.get_project(project_id)
    try:
        generated_file = service.get_generated_file(
            project_id,
            file_path,
            version=version,
        )
    except ProjectServiceError as error:
        if "was not found" in str(error):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Generated file was not found.",
            ) from error
        raise
    if generated_file.file_content is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Generated file contents are unavailable.",
        )
    return FileContentResponse(
        **FileResponse.from_file(generated_file).model_dump(),
        content=generated_file.file_content,
    )


@app.post(
    "/api/projects/{project_id}/execute",
    response_model=ExecutionAcceptedResponse,
    status_code=status.HTTP_202_ACCEPTED,
    tags=["execution"],
)
def execute_project(
    project_id: int,
    request: ExecutionRequest,
    service: ProjectServiceDependency,
    runner: CodeExecutionRunnerDependency,
) -> ExecutionAcceptedResponse:
    """Schedule an existing project's entry point in the Docker sandbox."""
    service.get_project(project_id)
    project_request = _load_project_request(service, project_id)
    records = service.list_generated_files(project_id)
    if not records:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="No generated files are available to execute.",
        )
    if any(record.file_content is None for record in records):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="One or more generated file contents are unavailable.",
        )
    generated_files = [
        GeneratedFile(
            file_path=record.file_path,
            content=record.file_content or "",
            programming_language=project_request.programming_language,
        )
        for record in records
    ]
    execution_id = runner.start(
        project_id,
        generated_files,
        project_request.programming_language,
        request.entrypoint,
        service,
        timeout_seconds=request.timeout_seconds,
    )
    if execution_id is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A code execution is already running for this project.",
        )
    return ExecutionAcceptedResponse(
        project_id=project_id,
        execution_id=execution_id,
        status="running",
        message="Execution scheduled in the isolated Docker sandbox.",
    )


@app.get(
    "/api/projects/{project_id}/execution",
    response_model=ExecutionStatusResponse,
    tags=["execution"],
)
def get_project_execution(
    project_id: int,
    service: ProjectServiceDependency,
) -> ExecutionStatusResponse:
    """Return the latest saved Docker execution state and captured output."""
    service.get_project(project_id)
    executions = [
        execution
        for execution in service.list_agent_executions(project_id)
        if execution.agent_name == "code_executor"
    ]
    if not executions:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No code execution has been requested for this project.",
        )
    execution = executions[-1]
    try:
        input_data = json.loads(execution.input)
        result_data = json.loads(execution.output) if execution.output else None
        result = ExecutionResult.model_validate(result_data) if result_data else None
    except (TypeError, ValueError):
        logger.exception(
            "Stored code execution result is invalid for project %s.", project_id
        )
        result = None
        input_data = {}
    return ExecutionStatusResponse(
        project_id=project_id,
        execution_id=execution.id,
        entrypoint=input_data.get("entrypoint", "main.py"),
        status=execution.status,
        timestamp=_utc_datetime(execution.timestamp),
        result=result,
    )


@app.get(
    "/api/projects/{project_id}/logs",
    response_model=list[LogResponse],
    tags=["projects"],
)
def get_project_logs(
    project_id: int,
    service: ProjectServiceDependency,
) -> list[LogResponse]:
    """List execution metadata without returning prompts or agent output."""
    return [
        LogResponse.from_execution(execution)
        for execution in service.list_agent_executions(project_id)
        if execution.agent_name != "api_request"
    ]


@app.get(
    "/api/projects/{project_id}/tests",
    response_model=list[TestExecutionResponse],
    tags=["projects"],
)
def get_project_tests(
    project_id: int,
    service: ProjectServiceDependency,
) -> list[TestExecutionResponse]:
    """Return historical test run counts and outcomes."""
    return [
        TestExecutionResponse.from_execution(execution)
        for execution in service.list_test_executions(project_id)
    ]


@app.get(
    "/api/projects/{project_id}/workflow",
    response_model=WorkflowDetailsResponse,
    tags=["workflows"],
)
def get_workflow_details(
    project_id: int,
    service: ProjectServiceDependency,
) -> WorkflowDetailsResponse:
    """Return the persisted workflow snapshot, including actual agent results."""
    project = service.get_project(project_id)
    persisted = _latest_workflow_status(service, project_id) or {}
    return WorkflowDetailsResponse(
        project_id=project_id,
        status=project.status,
        current_stage=persisted.get("current_stage"),
        human_approval_status=persisted.get("human_approval_status"),
        workflow_summary=persisted.get("workflow_summary"),
        architecture_design=persisted.get("architecture_design"),
        programming_language=persisted.get("programming_language"),
        preferred_framework=persisted.get("preferred_framework"),
        requirements=persisted.get("requirements", []),
        development_tasks=persisted.get("development_tasks", []),
        generated_files=persisted.get("generated_files", []),
        test_results=persisted.get("test_results", []),
        code_analysis_results=persisted.get("code_analysis_results", []),
        code_review_results=persisted.get("code_review_results", []),
        debugging_attempts=persisted.get("debugging_attempts", []),
        error_messages=persisted.get("error_messages", []),
        max_debugging_attempts=persisted.get("max_debugging_attempts", 3),
    )


@app.post(
    "/api/projects/{project_id}/approval",
    response_model=WorkflowAcceptedResponse,
    status_code=status.HTTP_202_ACCEPTED,
    tags=["workflows"],
)
def submit_approval(
    project_id: int,
    request: ApprovalRequest,
    service: ProjectServiceDependency,
    runner: WorkflowRunnerDependency,
) -> WorkflowAcceptedResponse:
    """Resume a paused workflow with the submitted architecture decision."""
    project = service.get_project(project_id)
    if project.status != "awaiting_approval":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This project is not waiting for architecture approval.",
        )
    if not runner.resume(project_id, request.approved, service):
        current_project = service.get_project(project_id)
        if current_project.status == "failed":
            detail = (
                "The workflow approval checkpoint is unavailable; "
                "the workflow was marked failed."
            )
        else:
            detail = "A workflow operation is already running for this project."
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=detail,
        )
    return WorkflowAcceptedResponse(
        project_id=project_id,
        status="in_progress",
        message="Approval was recorded; workflow resumption was scheduled.",
    )


def _load_project_request(service: ProjectService, project_id: int) -> ProjectRequest:
    for execution in service.list_agent_executions(project_id):
        if execution.agent_name == "api_request":
            try:
                return ProjectRequest.model_validate_json(execution.input)
            except ValueError as error:
                logger.exception("Stored project request is invalid.")
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="The saved project request cannot be used to start a workflow.",
                ) from error
    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail="No saved project request is available for this project.",
    )


def _latest_workflow_status(
    service: ProjectService,
    project_id: int,
) -> dict[str, Any] | None:
    workflow_records = [
        execution
        for execution in service.list_agent_executions(project_id)
        if execution.agent_name == "workflow"
    ]
    if not workflow_records:
        return None
    try:
        parsed: Any = json.loads(workflow_records[-1].output)
    except (TypeError, ValueError):
        logger.exception(
            "Stored workflow status is invalid for project %s.", project_id
        )
        return None
    return parsed if isinstance(parsed, dict) else None


def _http_error(code: int, detail: str) -> Any:
    from fastapi.responses import JSONResponse

    return JSONResponse(status_code=code, content={"detail": detail})


def _utc_datetime(value: datetime) -> datetime:
    """Normalize SQLite's naive UTC timestamps for stable API serialization."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value
