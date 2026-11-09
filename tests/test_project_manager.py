"""Tests for planning and dependency validation in ProjectManagerAgent."""

import json

import pytest

from app.agents.base_agent import AgentInput
from app.agents.project_manager import ProjectManagerAgent, ProjectPlanError
from app.models.schemas import DevelopmentTask, ProjectRequest


class MockLLM:
    """LLM fake that returns a configured planning response."""

    def __init__(self, response: str) -> None:
        self.response = response
        self.prompts: list[str] = []

    def invoke(self, input: str) -> object:
        self.prompts.append(input)
        return {"content": self.response}


def project_request(requirements: list[str] | None = None) -> ProjectRequest:
    """Create a sample project request for planning tests."""
    return ProjectRequest(
        project_name="Task Tracker",
        description="Build a small task tracking API.",
        requirements=requirements or [],
        programming_language="Python",
        preferred_framework="FastAPI",
    )


def task(
    task_id: str,
    *,
    dependencies: list[str] | None = None,
    assigned_agent: str = "developer",
    status: str = "pending",
) -> dict[str, object]:
    """Create one JSON-compatible task fixture."""
    return {
        "task_id": task_id,
        "title": f"Implement {task_id}",
        "description": f"Complete {task_id}",
        "assigned_agent": assigned_agent,
        "dependencies": dependencies or [],
        "priority": "medium",
        "status": status,
    }


def agent_for(tasks: list[dict[str, object]]) -> ProjectManagerAgent:
    """Create a project manager with the provided mocked task response."""
    return ProjectManagerAgent(MockLLM(json.dumps({"tasks": tasks})))


def test_generates_structured_tasks_in_dependency_order() -> None:
    agent = agent_for(
        [
            task("test-api", dependencies=["implement-api"], assigned_agent="tester"),
            task("implement-api", dependencies=["design-api"]),
            task("design-api", assigned_agent="architect"),
        ]
    )

    plan = agent.plan(project_request(["Expose task endpoints"]))

    assert all(isinstance(item, DevelopmentTask) for item in plan)
    assert [item.task_id for item in plan] == [
        "design-api",
        "implement-api",
        "test-api",
    ]


def test_empty_requirements_uses_description_and_prompts_for_assumptions() -> None:
    llm = MockLLM(
        json.dumps({"tasks": [task("clarify-scope", assigned_agent="project_manager")]})
    )
    agent = ProjectManagerAgent(llm)

    plan = agent.plan(project_request())

    assert len(plan) == 1
    assert "Build a small task tracking API." in llm.prompts[0]
    assert "requirements" in llm.prompts[0]
    assert "assumptions explicit" in llm.prompts[0]


def test_rejects_empty_plan() -> None:
    agent = agent_for([])

    with pytest.raises(ProjectPlanError, match="returned no tasks"):
        agent.plan(project_request(["Create a task API"]))


def test_rejects_duplicate_task_ids() -> None:
    agent = agent_for([task("same-id"), task("same-id")])

    with pytest.raises(ProjectPlanError, match="duplicate task IDs"):
        agent.plan(project_request(["Create a task API"]))


def test_rejects_circular_dependencies() -> None:
    agent = agent_for(
        [
            task("first", dependencies=["second"]),
            task("second", dependencies=["first"]),
        ]
    )

    with pytest.raises(ProjectPlanError, match="circular"):
        agent.plan(project_request(["Create a task API"]))


def test_rejects_missing_dependencies() -> None:
    agent = agent_for([task("first", dependencies=["not-in-plan"])])

    with pytest.raises(ProjectPlanError, match="missing task"):
        agent.plan(project_request(["Create a task API"]))


def test_rejects_invalid_task_assignments() -> None:
    agent = agent_for([task("first", assigned_agent="unknown_agent")])

    with pytest.raises(ProjectPlanError, match="invalid agent assignment"):
        agent.plan(project_request(["Create a task API"]))


def test_pending_task_is_not_ready_until_dependencies_complete() -> None:
    blocked_task = DevelopmentTask.model_validate(
        task("second", dependencies=["first"])
    )
    completed_dependency = DevelopmentTask.model_validate(
        task("first", status="completed")
    )
    pending_dependency = DevelopmentTask.model_validate(task("first"))

    assert ProjectManagerAgent.get_ready_tasks([pending_dependency, blocked_task]) == [
        pending_dependency
    ]
    assert ProjectManagerAgent.get_ready_tasks(
        [completed_dependency, blocked_task]
    ) == [blocked_task]


def test_execute_returns_plan_through_base_agent_interface() -> None:
    agent = agent_for([task("one")])
    request = AgentInput(
        task="Plan the project",
        context={"project_request": project_request(["Create an API"]).model_dump()},
    )

    response = agent.execute(request)

    assert response.agent_name == "Project Manager"
    assert json.loads(response.content)[0]["task_id"] == "one"


def test_ready_task_check_rejects_missing_dependencies() -> None:
    pending_task = DevelopmentTask.model_validate(task("first", dependencies=["lost"]))

    with pytest.raises(ProjectPlanError, match="missing task"):
        ProjectManagerAgent.get_ready_tasks([pending_task])
