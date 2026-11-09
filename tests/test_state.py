"""Tests for DevForge workflow state initialization and reducers."""

from langgraph.graph import END, START, StateGraph

from app.graph.state import (
    DevForgeState,
    HumanApprovalStatus,
    ProjectStatus,
    WorkflowStage,
    create_initial_state,
    merge_generated_files,
    merge_tasks,
)
from app.models.schemas import (
    DevelopmentTask,
    GeneratedFile,
    ProjectRequest,
    ReviewResult,
)
from app.models.schemas import (
    TestResult as _TestResult,
)


def make_task(task_id: str, title: str, status: str = "pending") -> DevelopmentTask:
    """Build a task fixture with the requested identifier and status."""
    return DevelopmentTask(
        task_id=task_id,
        title=title,
        description=f"{title} description",
        assigned_agent="developer",
        status=status,
    )


def test_initial_state_contains_request_and_defaults() -> None:
    request = ProjectRequest(
        project_name="DevForge",
        description="Build a project assistant",
        requirements=["Create an API"],
        programming_language="Python",
    )

    state = create_initial_state(request)

    assert state["original_request"] == request
    assert state["project_requirements"] == ["Create an API"]
    assert state["current_stage"] is WorkflowStage.INTAKE
    assert state["project_status"] is ProjectStatus.PENDING
    assert state["human_approval_status"] is HumanApprovalStatus.PENDING
    assert state["development_tasks"] == []


def test_initial_states_do_not_share_mutable_collections() -> None:
    request = ProjectRequest(
        project_name="DevForge",
        description="Build a project assistant",
        programming_language="Python",
    )

    first = create_initial_state(request)
    second = create_initial_state(request)
    first["error_messages"].append("isolated failure")

    assert second["error_messages"] == []


def test_task_reducer_appends_and_updates_by_task_id() -> None:
    original = make_task("task-1", "Create API")
    added = make_task("task-2", "Write tests")
    updated = make_task("task-1", "Create API", status="completed")

    result = merge_tasks([original], [added, updated])

    assert [task.task_id for task in result] == ["task-1", "task-2"]
    assert result[0].status == "completed"
    assert result[1] == added


def test_task_reducer_does_not_mutate_existing_values() -> None:
    original = [make_task("task-1", "Create API")]

    merge_tasks(original, [make_task("task-2", "Write tests")])

    assert len(original) == 1


def test_generated_file_reducer_keeps_latest_content_by_path() -> None:
    original = GeneratedFile(
        file_path="app/main.py",
        content="old",
        programming_language="Python",
    )
    updated = GeneratedFile(
        file_path="app/main.py",
        content="new",
        programming_language="Python",
    )
    added = GeneratedFile(
        file_path="tests/test_main.py",
        content="test",
        programming_language="Python",
    )

    result = merge_generated_files([original], [updated, added])

    assert [file.file_path for file in result] == ["app/main.py", "tests/test_main.py"]
    assert result[0].content == "new"


def test_accumulating_reducers_append_results_and_logs() -> None:
    state: DevForgeState = {
        "test_results": [_TestResult(total_tests=1, passed_tests=1)],
        "code_review_results": [ReviewResult(quality_score=80, approved=True)],
        "debugging_attempts": [
            {
                "issue": "Import error",
                "hypothesis": "Missing dependency",
                "action": "Check requirements",
                "outcome": "Dependency exists",
                "successful": False,
            }
        ],
        "error_messages": ["First warning"],
    }

    state["test_results"] = state["test_results"] + [
        _TestResult(total_tests=1, failed_tests=1, error_details=["failure"])
    ]
    state["code_review_results"] = state["code_review_results"] + [
        ReviewResult(quality_score=90, approved=True)
    ]
    state["debugging_attempts"] = state["debugging_attempts"] + [
        {
            "issue": "Import error",
            "hypothesis": "Incorrect import path",
            "action": "Correct the import",
            "outcome": "Tests pass",
            "successful": True,
        }
    ]
    state["error_messages"] = state["error_messages"] + ["Second warning"]

    assert len(state["test_results"]) == 2
    assert len(state["code_review_results"]) == 2
    assert len(state["debugging_attempts"]) == 2
    assert state["error_messages"] == ["First warning", "Second warning"]


def test_langgraph_node_updates_apply_state_reducers() -> None:
    initial_state = create_initial_state(
        ProjectRequest(
            project_name="DevForge",
            description="Build a project assistant",
            programming_language="Python",
        )
    )
    initial_state["development_tasks"] = [make_task("task-1", "Create API")]
    initial_state["error_messages"] = ["Initial warning"]
    initial_state["test_results"] = [_TestResult(total_tests=1, passed_tests=1)]

    def update_state(_: DevForgeState) -> dict[str, object]:
        return {
            "development_tasks": [make_task("task-1", "Create API", "completed")],
            "error_messages": ["Node warning"],
            "test_results": [_TestResult(total_tests=1, failed_tests=1)],
        }

    graph_builder = StateGraph(DevForgeState)
    graph_builder.add_node("update", update_state)
    graph_builder.add_edge(START, "update")
    graph_builder.add_edge("update", END)
    graph = graph_builder.compile()

    result = graph.invoke(initial_state)

    assert len(result["development_tasks"]) == 1
    assert result["development_tasks"][0].status == "completed"
    assert result["error_messages"] == ["Initial warning", "Node warning"]
    assert len(result["test_results"]) == 2
