"""End-to-end LangGraph orchestration tests with mocked agent boundaries."""

import uuid
from pathlib import Path

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from app.graph.nodes import WorkflowAgents
from app.graph.state import (
    HumanApprovalStatus,
    ProjectStatus,
    create_initial_state,
)
from app.graph.workflow import build_workflow
from app.models.schemas import (
    ArchitectureDesign,
    DebuggingCorrection,
    DebuggingResponse,
    DevelopmentTask,
    GeneratedFile,
    ProjectRequest,
    ReviewResult,
    TaskStatus,
)
from app.models.schemas import TestResult as _TestResult
from app.tools.code_analyzer import CodeAnalysisResult
from app.tools.file_manager import FileManager


def project_request() -> ProjectRequest:
    """Provide one request for all workflow tests."""
    return ProjectRequest(
        project_name="workflow-sample",
        description="Build a small calculator module.",
        requirements=["Provide an addition function."],
        programming_language="Python",
    )


def task(
    task_id: str = "implement",
    *,
    dependencies: list[str] | None = None,
    status: TaskStatus = TaskStatus.PENDING,
) -> DevelopmentTask:
    """Build a pending developer task."""
    return DevelopmentTask(
        task_id=task_id,
        title=f"Implement {task_id}",
        description="Implement the requested calculator behavior.",
        assigned_agent="developer",
        dependencies=dependencies or [],
        status=status,
    )


def architecture_design() -> ArchitectureDesign:
    """Provide a valid mocked design without calling an LLM."""
    return ArchitectureDesign(
        project_structure=["app/", "app/calculator.py"],
        technology_stack=["Python"],
        design_decisions=["Use a small pure-Python module."],
    )


def generated_file(
    path: str = "app/calculator.py", content: str = "def add(a, b):\n    return a + b\n"
) -> GeneratedFile:
    """Create a Python project file."""
    return GeneratedFile(
        file_path=path,
        content=content,
        programming_language="Python",
    )


class MockProjectManager:
    """Return a fixed task plan."""

    def __init__(
        self,
        tasks: list[DevelopmentTask] | None = None,
        error: Exception | None = None,
    ) -> None:
        self.tasks = tasks if tasks is not None else [task()]
        self.error = error

    def plan(self, request: ProjectRequest) -> list[DevelopmentTask]:
        if self.error:
            raise self.error
        return list(self.tasks)


class MockArchitect:
    """Return a fixed architecture."""

    def __init__(self) -> None:
        self.calls = 0

    def design(
        self,
        request: ProjectRequest,
        tasks: list[DevelopmentTask],
        technology_preferences: list[str],
    ) -> ArchitectureDesign:
        self.calls += 1
        assert technology_preferences == ["Python"]
        return architecture_design()


class MockDeveloper:
    """Return task-specific generated files and record execution order."""

    def __init__(self, outputs: list[list[GeneratedFile]] | None = None) -> None:
        self.outputs = outputs or [[generated_file()]]
        self.calls: list[str] = []

    def generate(
        self,
        architecture: ArchitectureDesign,
        task: DevelopmentTask,
        programming_language: str,
        previously_generated_files: list[GeneratedFile],
    ) -> list[GeneratedFile]:
        assert task.status is TaskStatus.IN_PROGRESS
        assert programming_language == "Python"
        self.calls.append(task.task_id)
        return self.outputs[len(self.calls) - 1]


class MockTester:
    """Return test results in sequence."""

    def __init__(self, results: list[_TestResult] | None = None) -> None:
        self.results = results or [_TestResult(total_tests=1, passed_tests=1)]
        self.calls = 0

    def test_generated_files(
        self,
        generated_files: list[GeneratedFile],
        programming_language: str,
    ) -> _TestResult:
        self.calls += 1
        assert generated_files
        assert programming_language == "Python"
        return self.results[min(self.calls - 1, len(self.results) - 1)]


class MockReviewer:
    """Return review outcomes in sequence while recording analyzer reports."""

    def __init__(self, results: list[ReviewResult] | None = None) -> None:
        self.results = results or [ReviewResult(quality_score=95, approved=True)]
        self.calls = 0
        self.analysis_reports: list[CodeAnalysisResult | None] = []

    def review(
        self,
        files: list[GeneratedFile],
        requirements: list[str],
        architecture: ArchitectureDesign,
        test_result: _TestResult | None,
        static_analysis: CodeAnalysisResult | None = None,
    ) -> ReviewResult:
        self.calls += 1
        self.analysis_reports.append(static_analysis)
        return self.results[min(self.calls - 1, len(self.results) - 1)]


class MockDebugger:
    """Return a changed file for each requested debugging attempt."""

    def __init__(self, corrected_content: list[str] | None = None) -> None:
        self.corrected_content = corrected_content or [
            "def add(a, b):\n    return a + b\n"
        ]
        self.calls = 0
        self.received_test_results: list[_TestResult] = []

    def propose_corrections(
        self,
        files: list[GeneratedFile],
        test_result: _TestResult,
        architecture: ArchitectureDesign,
        review_result: ReviewResult | None = None,
        *,
        attempt_number: int | None = None,
        maximum_attempts: int | None = None,
    ) -> DebuggingResponse:
        self.calls += 1
        self.received_test_results.append(test_result)
        content = self.corrected_content[
            min(self.calls - 1, len(self.corrected_content) - 1)
        ]
        corrected = files[0].model_copy(update={"content": content})
        return DebuggingResponse(
            root_cause="The returned implementation needs a targeted correction.",
            corrections=[
                DebuggingCorrection(
                    file=corrected,
                    change_summary="Correct the calculator implementation.",
                    rationale="The previous implementation did not satisfy the check.",
                )
            ],
        )


class MockAnalyzer:
    """Return a static report without invoking Ruff in orchestration tests."""

    def __init__(self) -> None:
        self.calls = 0

    def analyze(self, files: list[GeneratedFile]) -> CodeAnalysisResult:
        self.calls += 1
        assert files
        return CodeAnalysisResult()


def make_agents(
    *,
    tasks: list[DevelopmentTask] | None = None,
    test_results: list[_TestResult] | None = None,
    reviews: list[ReviewResult] | None = None,
    corrections: list[str] | None = None,
    developer_outputs: list[list[GeneratedFile]] | None = None,
    planning_error: Exception | None = None,
) -> tuple[WorkflowAgents, dict[str, object]]:
    """Build graph collaborators and return them for call assertions."""
    parts: dict[str, object] = {
        "project_manager": MockProjectManager(tasks, planning_error),
        "architect": MockArchitect(),
        "developer": MockDeveloper(developer_outputs),
        "tester": MockTester(test_results),
        "reviewer": MockReviewer(reviews),
        "debugger": MockDebugger(corrections),
    }
    return WorkflowAgents(**parts), parts


def run_approved_workflow(
    tmp_path: Path,
    agents: WorkflowAgents,
    *,
    analyzer: MockAnalyzer | None = None,
    max_debugging_attempts: int = 3,
) -> dict[str, object]:
    """Invoke a workflow with architecture approval pre-supplied."""
    state = create_initial_state(project_request())
    state["human_approval_status"] = HumanApprovalStatus.APPROVED
    graph = build_workflow(
        agents=agents,
        file_manager=FileManager(tmp_path),
        analyzer=analyzer or MockAnalyzer(),
        max_debugging_attempts=max_debugging_attempts,
        checkpointer=InMemorySaver(),
    )
    return graph.invoke(
        state,
        config={"configurable": {"thread_id": str(uuid.uuid4())}},
    )


def test_successful_development_sorts_tasks_and_saves_generated_files(
    tmp_path: Path,
) -> None:
    tasks = [
        task("second", dependencies=["first"]),
        task("first"),
    ]
    agents, parts = make_agents(
        tasks=tasks,
        developer_outputs=[
            [generated_file("app/first.py", "first = True\n")],
            [generated_file("app/second.py", "second = True\n")],
        ],
    )
    analyzer = MockAnalyzer()

    result = run_approved_workflow(tmp_path, agents, analyzer=analyzer)

    developer = parts["developer"]
    assert isinstance(developer, MockDeveloper)
    assert developer.calls == ["first", "second"]
    assert result["project_status"] is ProjectStatus.COMPLETED
    assert [task.task_id for task in result["development_tasks"]] == ["first", "second"]
    assert all(
        task.status is TaskStatus.COMPLETED for task in result["development_tasks"]
    )
    assert {file.file_path for file in result["generated_files"]} == {
        "app/first.py",
        "app/second.py",
    }
    assert (tmp_path / "workflow-sample" / "app" / "second.py").read_text() == (
        "second = True\n"
    )
    assert result["workflow_summary"].startswith(
        "Project 'workflow-sample' was generated"
    )
    assert analyzer.calls == 1


def test_architecture_rejection_stops_before_generation(tmp_path: Path) -> None:
    agents, parts = make_agents()
    state = create_initial_state(project_request())
    state["human_approval_status"] = HumanApprovalStatus.REJECTED
    graph = build_workflow(
        agents=agents,
        file_manager=FileManager(tmp_path),
        checkpointer=InMemorySaver(),
    )

    result = graph.invoke(
        state,
        config={"configurable": {"thread_id": str(uuid.uuid4())}},
    )

    assert result["project_status"] is ProjectStatus.BLOCKED
    assert "rejected" in result["workflow_summary"].casefold()
    assert parts["developer"].calls == []
    assert result["generated_files"] == []


def test_test_failure_is_debugged_then_retested_and_approved(tmp_path: Path) -> None:
    agents, parts = make_agents(
        test_results=[
            _TestResult(
                total_tests=1,
                failed_tests=1,
                error_details=["Expected 5, got 4."],
            ),
            _TestResult(total_tests=1, passed_tests=1),
        ],
        reviews=[
            ReviewResult(
                quality_score=65,
                approved=False,
                identified_issues=["Addition result is incorrect."],
            ),
            ReviewResult(quality_score=90, approved=True),
        ],
        corrections=["def add(a, b):\n    return a + b\n"],
    )

    result = run_approved_workflow(tmp_path, agents)

    assert result["project_status"] is ProjectStatus.COMPLETED
    assert len(result["test_results"]) == 2
    assert len(result["debugging_attempts"]) == 1
    assert result["generated_files"][0].content.endswith("return a + b\n")
    assert parts["tester"].calls == 2
    assert parts["debugger"].calls == 1


def test_reviewer_rejection_after_passing_tests_triggers_debugging(
    tmp_path: Path,
) -> None:
    agents, parts = make_agents(
        test_results=[
            _TestResult(total_tests=1, passed_tests=1),
            _TestResult(total_tests=1, passed_tests=1),
        ],
        reviews=[
            ReviewResult(
                quality_score=70,
                approved=False,
                identified_issues=["Improve input validation."],
            ),
            ReviewResult(quality_score=92, approved=True),
        ],
    )

    result = run_approved_workflow(tmp_path, agents)

    assert result["project_status"] is ProjectStatus.COMPLETED
    assert parts["debugger"].calls == 1
    assert parts["debugger"].received_test_results[0].failed_tests == 1
    assert (
        "Review gate rejection"
        in parts["debugger"].received_test_results[0].error_details[0]
    )


def test_retry_limit_stops_before_an_additional_debug_attempt(
    tmp_path: Path,
) -> None:
    agents, parts = make_agents(
        test_results=[_TestResult(total_tests=1, failed_tests=1)],
        reviews=[ReviewResult(quality_score=50, approved=False)],
    )

    result = run_approved_workflow(
        tmp_path,
        agents,
        max_debugging_attempts=0,
    )

    assert result["project_status"] is ProjectStatus.FAILED
    assert parts["debugger"].calls == 0
    assert result["debugging_attempts"] == []


def test_repeated_debugging_failure_exhausts_retry_budget(tmp_path: Path) -> None:
    agents, parts = make_agents(
        test_results=[
            _TestResult(total_tests=1, failed_tests=1, error_details=["still failing"]),
            _TestResult(total_tests=1, failed_tests=1, error_details=["still failing"]),
        ],
        reviews=[ReviewResult(quality_score=50, approved=False)],
    )

    result = run_approved_workflow(
        tmp_path,
        agents,
        max_debugging_attempts=1,
    )

    assert result["project_status"] is ProjectStatus.FAILED
    assert len(result["debugging_attempts"]) == 1
    assert parts["debugger"].calls == 1
    assert parts["tester"].calls == 2


def test_agent_execution_failure_has_an_explicit_failure_path(tmp_path: Path) -> None:
    agents, parts = make_agents(planning_error=RuntimeError("planner unavailable"))

    result = run_approved_workflow(tmp_path, agents)

    assert result["project_status"] is ProjectStatus.FAILED
    assert "planner unavailable" in result["workflow_summary"]
    assert parts["architect"].calls == 0
    assert parts["developer"].calls == []


def test_invalid_dependency_graph_fails_before_architecture(tmp_path: Path) -> None:
    agents, parts = make_agents(
        tasks=[
            task("first", dependencies=["missing-task"]),
        ]
    )

    result = run_approved_workflow(tmp_path, agents)

    assert result["project_status"] is ProjectStatus.FAILED
    assert "missing task" in result["workflow_summary"].casefold()
    assert parts["architect"].calls == 0


def test_architecture_approval_interrupt_can_be_resumed(tmp_path: Path) -> None:
    agents, parts = make_agents()
    graph = build_workflow(
        agents=agents,
        file_manager=FileManager(tmp_path),
        checkpointer=InMemorySaver(),
    )
    config = {"configurable": {"thread_id": str(uuid.uuid4())}}
    initial = create_initial_state(project_request())

    paused = graph.invoke(initial, config=config)

    assert "__interrupt__" in paused
    assert parts["developer"].calls == []

    result = graph.invoke(Command(resume={"approved": True}), config=config)

    assert result["project_status"] is ProjectStatus.COMPLETED
    assert result["human_approval_status"] is HumanApprovalStatus.APPROVED
    assert parts["developer"].calls == ["implement"]
