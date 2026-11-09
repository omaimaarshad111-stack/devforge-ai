"""Validation tests for the application's Pydantic data contracts."""

import pytest
from pydantic import ValidationError

from app.models.schemas import (
    AgentResponse,
    ArchitectureDesign,
    DevelopmentTask,
    GeneratedFile,
    ProjectRequest,
    ReviewResult,
    TaskPriority,
    TaskStatus,
)
from app.models.schemas import TestResult as _TestResult


def test_project_request_accepts_required_fields_and_defaults() -> None:
    request = ProjectRequest(
        project_name="DevForge",
        description="A development assistant",
        programming_language="Python",
    )

    assert request.requirements == []
    assert request.preferred_framework is None


@pytest.mark.parametrize(
    "values",
    [
        {"description": "A tool", "programming_language": "Python"},
        {
            "project_name": " ",
            "description": "A tool",
            "programming_language": "Python",
        },
        {
            "project_name": "A tool",
            "description": "A tool",
            "programming_language": "Python",
            "unexpected": True,
        },
    ],
)
def test_project_request_rejects_missing_blank_or_extra_fields(
    values: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        ProjectRequest.model_validate(values)


def test_development_task_defaults_and_enum_validation() -> None:
    task = DevelopmentTask(
        task_id="task-1",
        title="Create API",
        description="Set up the health endpoint",
        assigned_agent="developer",
    )

    assert task.priority is TaskPriority.MEDIUM
    assert task.status is TaskStatus.PENDING

    with pytest.raises(ValidationError):
        DevelopmentTask(
            task_id="task-2",
            title="Bad priority",
            description="Invalid enum",
            assigned_agent="developer",
            priority="urgent",
        )


def test_architecture_design_requires_structure_and_stack() -> None:
    design = ArchitectureDesign(
        project_structure=["app/", "tests/"],
        technology_stack=["Python", "FastAPI"],
        design_decisions=["Keep API routes separate from services."],
    )

    assert design.dependencies == []

    with pytest.raises(ValidationError):
        ArchitectureDesign(project_structure=[], technology_stack=["Python"])


def test_generated_file_accepts_empty_file_content() -> None:
    generated_file = GeneratedFile(
        file_path="app/__init__.py",
        content="",
        programming_language="Python",
    )

    assert generated_file.content == ""

    with pytest.raises(ValidationError):
        GeneratedFile(file_path=" ", content="", programming_language="Python")


def test_test_result_validates_counts_and_defaults() -> None:
    empty_run = _TestResult()
    result = _TestResult(
        total_tests=3,
        passed_tests=2,
        failed_tests=1,
        error_details=["test_api failed"],
    )

    assert empty_run.total_tests == 0
    assert result.failed_tests == 1

    with pytest.raises(ValidationError, match="must equal total_tests"):
        _TestResult(total_tests=3, passed_tests=2, failed_tests=0)

    with pytest.raises(ValidationError):
        _TestResult(total_tests=-1)


def test_review_result_checks_score_range_and_defaults() -> None:
    review = ReviewResult(quality_score=85, approved=True)

    assert review.identified_issues == []
    assert review.suggestions == []

    with pytest.raises(ValidationError):
        ReviewResult(quality_score=101, approved=False)


def test_agent_response_validates_json_output() -> None:
    response = AgentResponse(
        agent_name="architect",
        success=True,
        message="Design complete",
        output={"technology_stack": ["Python", "FastAPI"]},
    )

    assert response.output == {"technology_stack": ["Python", "FastAPI"]}
    assert (
        AgentResponse(
            agent_name="developer",
            success=False,
            message="No generated output",
        ).output
        is None
    )

    with pytest.raises(ValidationError):
        AgentResponse(agent_name="tester", success=True, message=" ", output=object())
