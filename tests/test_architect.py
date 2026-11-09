"""Tests for structured software architecture generation."""

import json

import pytest

from app.agents.architect import ArchitectAgent, ArchitectureDesignError
from app.agents.base_agent import AgentInput
from app.models.schemas import (
    ArchitectureDesign,
    DevelopmentTask,
    ProjectRequest,
)


class MockLLM:
    """Return a configured JSON architecture without making an API call."""

    def __init__(self, response: str) -> None:
        self.response = response
        self.prompts: list[str] = []

    def invoke(self, input: str) -> object:
        self.prompts.append(input)
        return {"content": self.response}


def project_request(requirements: list[str] | None = None) -> ProjectRequest:
    """Create a project request with explicit technology preferences."""
    return ProjectRequest(
        project_name="Task Tracker",
        description="A small task tracking API",
        requirements=requirements or ["Users can create and list tasks."],
        programming_language="Python",
        preferred_framework="FastAPI",
    )


def design_payload(
    *,
    requirement: str = "Users can create and list tasks.",
) -> dict[str, object]:
    """Build a representative valid architecture response."""
    return {
        "project_structure": ["app/api/", "app/services/", "app/models/", "tests/"],
        "technology_stack": ["Python", "FastAPI", "SQLite"],
        "application_architecture": [
            {
                "name": "Task API",
                "responsibility": "Validate HTTP requests and return responses.",
                "dependencies": ["Task service"],
            },
            {
                "name": "Task service",
                "responsibility": "Apply task lifecycle business rules.",
                "dependencies": ["Task repository interface"],
            },
        ],
        "database_design": {
            "database_engine": "SQLite",
            "entities": [
                {
                    "name": "Task",
                    "fields": ["id", "title", "description", "status"],
                    "relationships": [],
                }
            ],
            "design_notes": ["Use a repository abstraction for persistence."],
        },
        "api_endpoints": [
            {
                "method": "POST",
                "path": "/tasks",
                "purpose": "Create a task.",
                "request_schema": "TaskCreate",
                "response_schema": "Task",
            },
            {
                "method": "GET",
                "path": "/tasks",
                "purpose": "List tasks.",
                "request_schema": None,
                "response_schema": "list[Task]",
            },
        ],
        "dependencies": ["fastapi", "sqlalchemy"],
        "design_decisions": [
            "Keep HTTP handling, business rules, and persistence separate.",
            "Depend on a repository interface rather than a database implementation.",
        ],
        "requirement_coverage": [
            {
                "requirement": requirement,
                "covered_by": ["POST /tasks", "GET /tasks", "Task service"],
                "missing_information": False,
                "notes": None,
            }
        ],
    }


def architect_for(payload: object) -> ArchitectAgent:
    """Create an architect with a mocked LLM response."""
    return ArchitectAgent(MockLLM(json.dumps(payload)))


def test_returns_all_required_architecture_fields() -> None:
    agent = architect_for(design_payload())
    task = DevelopmentTask(
        task_id="api",
        title="Create task API",
        description="Implement task endpoints",
        assigned_agent="developer",
    )

    design = agent.design(
        project_request(),
        [task],
        technology_preferences=["SQLite"],
    )

    assert isinstance(design, ArchitectureDesign)
    assert design.technology_stack == ["Python", "FastAPI", "SQLite"]
    assert design.application_architecture
    assert design.database_design is not None
    assert {endpoint.path for endpoint in design.api_endpoints} == {
        "/tasks",
    }
    assert "app/services/" in design.project_structure
    assert "sqlalchemy" in design.dependencies
    assert design.design_decisions
    assert design.requirement_coverage[0].requirement == (
        "Users can create and list tasks."
    )


def test_rejects_invalid_architecture_fields() -> None:
    invalid_design = design_payload()
    invalid_design["technology_stack"] = []

    with pytest.raises(ArchitectureDesignError, match="invalid or incomplete"):
        architect_for(invalid_design).design(project_request())


def test_rejects_architecture_that_does_not_cover_requirements() -> None:
    with pytest.raises(ArchitectureDesignError, match="does not account"):
        architect_for(design_payload(requirement="Different requirement")).design(
            project_request()
        )


def test_rejects_architecture_that_ignores_technology_preferences() -> None:
    payload = design_payload()
    payload["technology_stack"] = ["Python", "SQLite"]

    with pytest.raises(ArchitectureDesignError, match="requested technology"):
        architect_for(payload).design(project_request())


def test_agent_prompt_includes_requirements_tasks_and_preferences() -> None:
    llm = MockLLM(json.dumps(design_payload()))
    agent = ArchitectAgent(llm)
    request = AgentInput(
        task="Propose the system architecture",
        context={
            "project_request": project_request().model_dump(mode="json"),
            "development_tasks": [
                {
                    "task_id": "api",
                    "title": "Create API",
                    "description": "Implement endpoints",
                    "assigned_agent": "developer",
                }
            ],
            "technology_preferences": ["SQLite"],
        },
    )

    response = agent.execute(request)

    assert response.agent_name == "Software Architect"
    assert (
        json.loads(response.content)["database_design"]["database_engine"] == "SQLite"
    )
    assert "Users can create and list tasks." in llm.prompts[0]
    assert "Create API" in llm.prompts[0]
    assert "SQLite" in llm.prompts[0]


def test_identifies_missing_information_without_inventing_details() -> None:
    payload = design_payload()
    payload["requirement_coverage"] = [
        {
            "requirement": "Users can create and list tasks.",
            "covered_by": ["Task API"],
            "missing_information": True,
            "notes": "Authentication requirements were not specified.",
        }
    ]

    design = architect_for(payload).design(project_request())

    assert design.requirement_coverage[0].missing_information is True
    assert "not specified" in (design.requirement_coverage[0].notes or "")
