"""LangGraph-compatible shared state and reducer functions."""

from enum import StrEnum
from operator import add
from typing import Annotated, NotRequired

from typing_extensions import TypedDict

from app.models.schemas import (
    ArchitectureDesign,
    DevelopmentTask,
    GeneratedFile,
    ProjectRequest,
    ReviewResult,
    TestResult,
)
from app.tools.code_analyzer import CodeAnalysisResult


class WorkflowStage(StrEnum):
    """Current phase of the project workflow."""

    INTAKE = "intake"
    PLANNING = "planning"
    ARCHITECTURE = "architecture"
    DEVELOPMENT = "development"
    TESTING = "testing"
    REVIEW = "review"
    DEBUGGING = "debugging"
    COMPLETE = "complete"


class ProjectStatus(StrEnum):
    """Overall project execution status."""

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    BLOCKED = "blocked"
    COMPLETED = "completed"
    FAILED = "failed"


class HumanApprovalStatus(StrEnum):
    """Human approval state for workflow checkpoints."""

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    NOT_REQUIRED = "not_required"


class DebuggingAttempt(TypedDict):
    """Structured record of a single attempt to diagnose or fix an issue."""

    issue: str
    hypothesis: str
    action: str
    outcome: str
    successful: bool
    corrected_files: NotRequired[list[str]]


def merge_tasks(
    existing: list[DevelopmentTask],
    updates: list[DevelopmentTask],
) -> list[DevelopmentTask]:
    """Merge tasks by ID so an update changes a task instead of duplicating it."""
    merged = {task.task_id: task for task in existing}
    for task in updates:
        merged[task.task_id] = task
    return list(merged.values())


def merge_generated_files(
    existing: list[GeneratedFile],
    updates: list[GeneratedFile],
) -> list[GeneratedFile]:
    """Merge generated files by path, retaining history order and latest content."""
    merged = {generated_file.file_path: generated_file for generated_file in existing}
    for generated_file in updates:
        merged[generated_file.file_path] = generated_file
    return list(merged.values())


class DevForgeState(TypedDict, total=False):
    """Data shared between graph nodes during one project workflow.

    Graph nodes receive the current state and return partial mappings containing
    only their updates. Reducers merge accumulated values; ordinary fields are
    replaced when a node returns a new value for that field.
    """

    original_request: ProjectRequest
    project_requirements: list[str]
    development_tasks: Annotated[list[DevelopmentTask], merge_tasks]
    architecture_design: ArchitectureDesign | None
    generated_files: Annotated[list[GeneratedFile], merge_generated_files]
    test_results: Annotated[list[TestResult], add]
    code_review_results: Annotated[list[ReviewResult], add]
    code_analysis_results: Annotated[list[CodeAnalysisResult], add]
    debugging_attempts: Annotated[list[DebuggingAttempt], add]
    current_stage: WorkflowStage
    project_status: ProjectStatus
    error_messages: Annotated[list[str], add]
    human_approval_status: HumanApprovalStatus
    task_execution_order: list[str]
    current_task_id: str | None
    project_directory: str
    workflow_summary: str
    max_debugging_attempts: int


def create_initial_state(request: ProjectRequest) -> DevForgeState:
    """Create an independent initial state from a validated project request."""
    return {
        "original_request": request,
        "project_requirements": list(request.requirements),
        "development_tasks": [],
        "architecture_design": None,
        "generated_files": [],
        "test_results": [],
        "code_review_results": [],
        "code_analysis_results": [],
        "debugging_attempts": [],
        "current_stage": WorkflowStage.INTAKE,
        "project_status": ProjectStatus.PENDING,
        "error_messages": [],
        "human_approval_status": HumanApprovalStatus.PENDING,
        "task_execution_order": [],
        "current_task_id": None,
        "project_directory": "",
        "workflow_summary": "",
        "max_debugging_attempts": 3,
    }
