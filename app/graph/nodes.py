"""LangGraph nodes for each independent step in the DevForge workflow."""

import logging
from collections import defaultdict, deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

from langgraph.types import interrupt

from app.agents.architect import ArchitectAgent
from app.agents.debugger import DebuggerAgent
from app.agents.developer import DeveloperAgent
from app.agents.project_manager import ProjectManagerAgent, ProjectPlanError
from app.agents.reviewer import CodeReviewerAgent
from app.agents.tester import TesterAgent
from app.graph.state import (
    DebuggingAttempt,
    DevForgeState,
    HumanApprovalStatus,
    ProjectStatus,
    WorkflowStage,
)
from app.models.schemas import (
    DebuggingResponse,
    DevelopmentTask,
    GeneratedFile,
    ProjectRequest,
    ReviewResult,
    TaskStatus,
    TestResult,
)
from app.tools.code_analyzer import CodeAnalyzer
from app.tools.file_manager import FileManager

logger = logging.getLogger(__name__)

_VALID_TASK_AGENTS = frozenset(
    {"project_manager", "architect", "developer", "tester", "reviewer", "debugger"}
)
_TASK_AGENT_ORDER = {
    "project_manager": 0,
    "architect": 1,
    "developer": 2,
    "tester": 3,
    "reviewer": 4,
    "debugger": 5,
}


@dataclass(frozen=True)
class WorkflowAgents:
    """Injectable specialized agents used by the independent graph nodes."""

    project_manager: ProjectManagerAgent
    architect: ArchitectAgent
    developer: DeveloperAgent
    tester: TesterAgent
    reviewer: CodeReviewerAgent
    debugger: DebuggerAgent


def plan_project(
    state: DevForgeState,
    *,
    agents: WorkflowAgents,
    max_debugging_attempts: int = 3,
) -> dict[str, Any]:
    """Ask the Project Manager for a graph-validated, ordered task plan."""
    try:
        request = state["original_request"]
        tasks = agents.project_manager.plan(request)
        ordered_tasks = _validate_and_order_tasks(tasks)
        ordered_tasks = [
            task.model_copy(update={"status": TaskStatus.COMPLETED})
            if task.assigned_agent.casefold() == "project_manager"
            else task
            for task in ordered_tasks
        ]
        return {
            "development_tasks": ordered_tasks,
            "task_execution_order": [task.task_id for task in ordered_tasks],
            "current_stage": WorkflowStage.ARCHITECTURE,
            "project_status": ProjectStatus.IN_PROGRESS,
            "project_requirements": list(request.requirements),
            "max_debugging_attempts": max_debugging_attempts,
        }
    except Exception as error:
        logger.exception("Project planning node failed.")
        return _failure_update(state, WorkflowStage.PLANNING, error)


def design_architecture(
    state: DevForgeState,
    *,
    agents: WorkflowAgents,
) -> dict[str, Any]:
    """Generate an architecture and complete tasks assigned to the architect."""
    try:
        request = state["original_request"]
        design = agents.architect.design(
            request=request,
            tasks=state["development_tasks"],
            technology_preferences=_technology_preferences(request),
        )
        task_updates = [
            task.model_copy(update={"status": TaskStatus.COMPLETED})
            for task in state["development_tasks"]
            if task.assigned_agent.casefold() == "architect"
            and task.status is not TaskStatus.COMPLETED
        ]
        return {
            "architecture_design": design,
            "development_tasks": task_updates,
            "current_stage": WorkflowStage.ARCHITECTURE,
        }
    except Exception as error:
        logger.exception("Architecture node failed.")
        return _failure_update(state, WorkflowStage.ARCHITECTURE, error)


def approve_architecture(state: DevForgeState) -> dict[str, Any]:
    """Pause at an interrupt until a human approves or rejects the design."""
    current_status = state["human_approval_status"]
    if current_status is HumanApprovalStatus.APPROVED:
        return {"current_stage": WorkflowStage.DEVELOPMENT}
    if current_status is HumanApprovalStatus.REJECTED:
        return {
            "current_stage": WorkflowStage.COMPLETE,
            "project_status": ProjectStatus.BLOCKED,
            "workflow_summary": "Architecture was rejected; no code was generated.",
            "error_messages": ["Human approval rejected the proposed architecture."],
        }
    if current_status is HumanApprovalStatus.NOT_REQUIRED:
        return {
            "human_approval_status": HumanApprovalStatus.APPROVED,
            "current_stage": WorkflowStage.DEVELOPMENT,
        }

    design = state.get("architecture_design")
    if design is None:
        return _failure_update(
            state,
            WorkflowStage.ARCHITECTURE,
            RuntimeError("Architecture approval cannot be requested before design."),
        )
    decision = interrupt(
        {
            "type": "architecture_approval",
            "project_name": state["original_request"].project_name,
            "architecture_design": design.model_dump(mode="json"),
            "message": "Approve the architecture to continue to code generation.",
        }
    )
    if isinstance(decision, bool):
        approved = decision
    elif isinstance(decision, Mapping) and isinstance(decision.get("approved"), bool):
        approved = decision["approved"]
    else:
        return _failure_update(
            state,
            WorkflowStage.ARCHITECTURE,
            ValueError(
                "Approval resume value must be a boolean or {'approved': bool}."
            ),
        )

    if not approved:
        return {
            "human_approval_status": HumanApprovalStatus.REJECTED,
            "current_stage": WorkflowStage.COMPLETE,
            "project_status": ProjectStatus.BLOCKED,
            "workflow_summary": "Architecture was rejected; no code was generated.",
            "error_messages": ["Human approval rejected the proposed architecture."],
        }
    return {
        "human_approval_status": HumanApprovalStatus.APPROVED,
        "current_stage": WorkflowStage.DEVELOPMENT,
    }


def develop_next_task(
    state: DevForgeState,
    *,
    agents: WorkflowAgents,
    file_manager: FileManager,
) -> dict[str, Any]:
    """Generate and persist one dependency-ready developer task at a time."""
    try:
        task = _next_developer_task(state["development_tasks"])
        if task is None:
            return {"current_stage": WorkflowStage.TESTING}

        design = state.get("architecture_design")
        if design is None:
            raise RuntimeError("Developer cannot run before architecture is approved.")
        in_progress = task.model_copy(update={"status": TaskStatus.IN_PROGRESS})
        generated = agents.developer.generate(
            architecture=design,
            task=in_progress,
            programming_language=state["original_request"].programming_language,
            previously_generated_files=state["generated_files"],
        )
        project_path = file_manager.create_project(
            state["original_request"].project_name
        )
        _persist_files(
            file_manager,
            state["original_request"].project_name,
            generated,
        )
        completed_task = task.model_copy(update={"status": TaskStatus.COMPLETED})
        return {
            "generated_files": generated,
            "development_tasks": [completed_task],
            "current_task_id": task.task_id,
            "project_directory": str(project_path),
            "current_stage": WorkflowStage.DEVELOPMENT,
        }
    except Exception as error:
        logger.exception("Developer node failed.")
        return _failure_update(state, WorkflowStage.DEVELOPMENT, error)


def run_tests(
    state: DevForgeState,
    *,
    agents: WorkflowAgents,
) -> dict[str, Any]:
    """Run isolated tests on the current merged project files."""
    try:
        result = agents.tester.test_generated_files(
            generated_files=state["generated_files"],
            programming_language=state["original_request"].programming_language,
        )
        task_updates = [
            task.model_copy(update={"status": TaskStatus.COMPLETED})
            for task in state["development_tasks"]
            if task.assigned_agent.casefold() == "tester"
            and task.status is not TaskStatus.COMPLETED
        ]
        return {
            "test_results": [result],
            "development_tasks": task_updates,
            "current_stage": WorkflowStage.TESTING,
        }
    except Exception as error:
        logger.exception("Testing node failed.")
        return _failure_update(state, WorkflowStage.TESTING, error)


def analyze_code(
    state: DevForgeState,
    *,
    analyzer: CodeAnalyzer,
) -> dict[str, Any]:
    """Run AST, Ruff, and Radon analysis independently of LLM judgment."""
    try:
        report = analyzer.analyze(state["generated_files"])
        return {
            "code_analysis_results": [report],
            "current_stage": WorkflowStage.REVIEW,
        }
    except Exception as error:
        logger.exception("Static-analysis node failed.")
        return _failure_update(state, WorkflowStage.REVIEW, error)


def review_code(
    state: DevForgeState,
    *,
    agents: WorkflowAgents,
) -> dict[str, Any]:
    """Review files alongside the latest test and static-analysis results."""
    try:
        design = state.get("architecture_design")
        if design is None:
            raise RuntimeError("Reviewer cannot run before architecture is available.")
        test_result = _latest(state.get("test_results", []))
        static_analysis = _latest(state.get("code_analysis_results", []))
        result = agents.reviewer.review(
            files=state["generated_files"],
            requirements=state["project_requirements"],
            architecture=design,
            test_result=test_result,
            static_analysis=static_analysis,
        )
        task_updates = [
            task.model_copy(update={"status": TaskStatus.COMPLETED})
            for task in state["development_tasks"]
            if task.assigned_agent.casefold() == "reviewer"
            and task.status is not TaskStatus.COMPLETED
        ]
        return {
            "code_review_results": [result],
            "development_tasks": task_updates,
            "current_stage": WorkflowStage.REVIEW,
        }
    except Exception as error:
        logger.exception("Code review node failed.")
        return _failure_update(state, WorkflowStage.REVIEW, error)


def debug_project(
    state: DevForgeState,
    *,
    agents: WorkflowAgents,
    file_manager: FileManager,
) -> dict[str, Any]:
    """Request a targeted correction, persist changed files, and log the attempt."""
    latest_test = _latest(state.get("test_results", []))
    latest_review = _latest(state.get("code_review_results", []))
    if latest_test is None or latest_review is None:
        return _failure_update(
            state,
            WorkflowStage.DEBUGGING,
            RuntimeError("Debugging requires test and review results."),
        )

    issue_summary = _debug_issue_summary(latest_test, latest_review)
    if not latest_test.failed_tests and not latest_test.execution_error:
        latest_test = TestResult(
            total_tests=1,
            failed_tests=1,
            error_details=[f"Review gate rejection: {issue_summary}"],
            actionable_feedback=latest_review.identified_issues
            or latest_review.suggestions
            or ["Address the review findings before requesting approval."],
        )
    try:
        design = state.get("architecture_design")
        if design is None:
            raise RuntimeError("Debugger cannot run before architecture is available.")
        response: DebuggingResponse = agents.debugger.propose_corrections(
            files=state["generated_files"],
            test_result=latest_test,
            architecture=design,
            review_result=latest_review,
            attempt_number=len(state.get("debugging_attempts", [])) + 1,
            maximum_attempts=state.get("max_debugging_attempts", 3),
        )
        corrections = [correction.file for correction in response.corrections]
        _persist_files(
            file_manager,
            state["original_request"].project_name,
            corrections,
        )
        attempt: DebuggingAttempt = {
            "issue": issue_summary,
            "hypothesis": response.root_cause,
            "action": "; ".join(
                correction.change_summary for correction in response.corrections
            ),
            "outcome": "Corrections saved; awaiting retest and review.",
            "successful": True,
            "corrected_files": [file.file_path for file in corrections],
        }
        return {
            "generated_files": corrections,
            "debugging_attempts": [attempt],
            "current_stage": WorkflowStage.DEBUGGING,
        }
    except Exception as error:
        logger.exception("Debugger node failed.")
        attempt = {
            "issue": issue_summary,
            "hypothesis": "Correction generation failed.",
            "action": "No correction was persisted.",
            "outcome": str(error),
            "successful": False,
            "corrected_files": [],
        }
        failure = _failure_update(state, WorkflowStage.DEBUGGING, error)
        failure["debugging_attempts"] = [attempt]
        return failure


def complete_project(state: DevForgeState) -> dict[str, Any]:
    """Finalize workflow progress and construct a concise project summary."""
    request = state["original_request"]
    latest_test = _latest(state.get("test_results", []))
    latest_review = _latest(state.get("code_review_results", []))
    return {
        "current_stage": WorkflowStage.COMPLETE,
        "project_status": ProjectStatus.COMPLETED,
        "workflow_summary": (
            f"Project '{request.project_name}' was generated and approved. "
            f"{len(state.get('generated_files', []))} file(s) saved to "
            f"{state.get('project_directory') or 'the configured project workspace'}. "
            f"Latest tests: {latest_test.passed_tests if latest_test else 0} passed, "
            f"{latest_test.failed_tests if latest_test else 0} failed. "
            f"Review score: {latest_review.quality_score if latest_review else 'N/A'}."
        ),
    }


def fail_project(state: DevForgeState) -> dict[str, Any]:
    """Finalize blocked or failed workflows with their recorded errors."""
    if state.get("project_status") is ProjectStatus.BLOCKED:
        return {"current_stage": WorkflowStage.COMPLETE}
    summary = state.get("error_messages", [])
    return {
        "current_stage": WorkflowStage.COMPLETE,
        "project_status": ProjectStatus.FAILED,
        "workflow_summary": (
            "Workflow failed: " + (summary[-1] if summary else "unknown failure.")
        ),
    }


def route_after_planning(state: DevForgeState) -> str:
    """Continue to design only when task planning succeeded."""
    return "failed" if state["project_status"] is ProjectStatus.FAILED else "design"


def route_after_design(state: DevForgeState) -> str:
    """Request approval only when an architecture was produced."""
    if state["project_status"] is ProjectStatus.FAILED:
        return "failed"
    return "approval" if state.get("architecture_design") is not None else "failed"


def route_after_approval(state: DevForgeState) -> str:
    """Proceed only for explicit approved architecture."""
    if state["project_status"] is ProjectStatus.BLOCKED:
        return "failed"
    if state["project_status"] is ProjectStatus.FAILED:
        return "failed"
    return (
        "development"
        if state["human_approval_status"]
        in {HumanApprovalStatus.APPROVED, HumanApprovalStatus.NOT_REQUIRED}
        else "failed"
    )


def route_after_development(state: DevForgeState) -> str:
    """Keep dequeuing ready developer tasks or continue to testing."""
    if state["project_status"] is ProjectStatus.FAILED:
        return "failed"
    try:
        if _next_developer_task(state["development_tasks"]) is not None:
            return "development"
        return "testing"
    except ProjectPlanError:
        return "failed"


def route_after_testing(state: DevForgeState) -> str:
    """Skip later stages if the test node itself failed to execute."""
    return "failed" if state["project_status"] is ProjectStatus.FAILED else "analysis"


def route_after_analysis(state: DevForgeState) -> str:
    """Review only when static analysis completed and returned a report."""
    if state["project_status"] is ProjectStatus.FAILED:
        return "failed"
    return "review" if state.get("code_analysis_results") else "failed"


def route_after_review(state: DevForgeState) -> str:
    """Finish approved work or debug within the configured retry budget."""
    if state["project_status"] is ProjectStatus.FAILED:
        return "failed"
    result = _latest(state.get("code_review_results", []))
    if result is not None and result.approved:
        return "complete"
    if len(state.get("debugging_attempts", [])) >= state.get(
        "max_debugging_attempts", 3
    ):
        return "failed"
    return "debug"


def route_after_debug(state: DevForgeState) -> str:
    """Retest successful corrections; stop on explicit debugging failure."""
    return (
        "testing" if state["project_status"] is not ProjectStatus.FAILED else "failed"
    )


def _validate_and_order_tasks(
    tasks: Sequence[DevelopmentTask],
) -> list[DevelopmentTask]:
    """Validate IDs/dependencies and Kahn-sort using a queue and hash maps."""
    if not tasks:
        raise ProjectPlanError("Project Manager returned no development tasks.")
    task_by_id: dict[str, DevelopmentTask] = {}
    for task in tasks:
        if task.task_id in task_by_id:
            raise ProjectPlanError(f"Duplicate task ID: '{task.task_id}'.")
        if task.assigned_agent.casefold() not in _VALID_TASK_AGENTS:
            raise ProjectPlanError(
                f"Task '{task.task_id}' has invalid assignment '{task.assigned_agent}'."
            )
        task_by_id[task.task_id] = task

    indegree: dict[str, int] = {}
    dependents: dict[str, list[str]] = defaultdict(list)
    for task in tasks:
        missing = set(task.dependencies) - task_by_id.keys()
        if missing:
            raise ProjectPlanError(
                f"Task '{task.task_id}' depends on missing task(s): "
                f"{', '.join(sorted(missing))}."
            )
        later_dependencies = [
            dependency
            for dependency in task.dependencies
            if _TASK_AGENT_ORDER[task_by_id[dependency].assigned_agent.casefold()]
            > _TASK_AGENT_ORDER[task.assigned_agent.casefold()]
        ]
        if later_dependencies:
            raise ProjectPlanError(
                f"Task '{task.task_id}' depends on work assigned to a later workflow "
                f"stage: {', '.join(sorted(later_dependencies))}."
            )
        indegree[task.task_id] = len(task.dependencies)
        for dependency in task.dependencies:
            dependents[dependency].append(task.task_id)

    ready = deque(task.task_id for task in tasks if indegree[task.task_id] == 0)
    ordered: list[DevelopmentTask] = []
    while ready:
        task_id = ready.popleft()
        ordered.append(task_by_id[task_id])
        for dependent in dependents[task_id]:
            indegree[dependent] -= 1
            if indegree[dependent] == 0:
                ready.append(dependent)
    if len(ordered) != len(tasks):
        raise ProjectPlanError("Task dependency graph contains a circular dependency.")
    return ordered


def _next_developer_task(
    tasks: Sequence[DevelopmentTask],
) -> DevelopmentTask | None:
    """Select the first dependency-ready developer task from a pending queue."""
    task_by_id = {task.task_id: task for task in tasks}
    pending = deque(
        task
        for task in tasks
        if task.assigned_agent.casefold() == "developer"
        and task.status is TaskStatus.PENDING
    )
    for task in pending:
        missing = set(task.dependencies) - task_by_id.keys()
        if missing:
            raise ProjectPlanError(
                f"Task '{task.task_id}' depends on missing task(s): "
                f"{', '.join(sorted(missing))}."
            )
        if all(
            task_by_id[dependency].status is TaskStatus.COMPLETED
            for dependency in task.dependencies
        ):
            return task
    if pending:
        blocked = ", ".join(task.task_id for task in pending)
        raise ProjectPlanError(
            f"No developer task is dependency-ready; pending task(s): {blocked}."
        )
    return None


def _technology_preferences(request: ProjectRequest) -> list[str]:
    """Build architecture preferences from the user's explicitly named stack."""
    preferences = [request.programming_language]
    if request.preferred_framework:
        preferences.append(request.preferred_framework)
    return preferences


def _persist_files(
    file_manager: FileManager,
    project_name: str,
    files: Sequence[GeneratedFile],
) -> None:
    """Persist generated file updates through the confined FileManager."""
    existing_files = set(file_manager.list_files(project_name))
    for generated_file in files:
        if generated_file.file_path in existing_files:
            file_manager.update_file(
                project_name,
                generated_file.file_path,
                generated_file.content,
                overwrite_authorized=True,
            )
        else:
            parent = PurePosixPath(generated_file.file_path).parent
            if parent.as_posix() != ".":
                file_manager.create_directory(project_name, parent.as_posix())
            file_manager.create_file(
                project_name,
                generated_file.file_path,
                generated_file.content,
            )
            existing_files.add(generated_file.file_path)


def _debug_issue_summary(
    test_result: TestResult,
    review_result: ReviewResult,
) -> str:
    """Summarize concrete failures and review blockers for the Debugger."""
    parts = [
        *test_result.error_details,
        *(
            issue.title + ": " + issue.description
            for issue in review_result.critical_issues
        ),
        *(
            issue.title + ": " + issue.description
            for issue in review_result.major_issues
        ),
    ]
    if test_result.execution_error:
        parts.append(test_result.execution_error)
    if not parts:
        parts.extend(review_result.identified_issues or review_result.suggestions)
    return "; ".join(parts) or "Review gate rejected the generated project."


def _latest(values: Sequence[Any]) -> Any | None:
    """Return the newest reducer-accumulated item, if available."""
    return values[-1] if values else None


def _failure_update(
    state: DevForgeState,
    stage: WorkflowStage,
    error: Exception,
) -> dict[str, Any]:
    """Mark a node failure without hiding the actionable exception message."""
    message = f"{stage.value.capitalize()} stage failed: {error}"
    logger.error(message)
    if state.get("project_status") is ProjectStatus.BLOCKED:
        return {
            "current_stage": WorkflowStage.COMPLETE,
            "error_messages": [message],
            "workflow_summary": message,
        }
    return {
        "current_stage": stage,
        "project_status": ProjectStatus.FAILED,
        "error_messages": [message],
        "workflow_summary": message,
    }
