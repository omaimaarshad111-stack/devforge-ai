"""Tests for structured and safe code generation."""

import json

import pytest

from app.agents.base_agent import AgentInput
from app.agents.developer import CodeGenerationError, DeveloperAgent
from app.models.schemas import ArchitectureDesign, DevelopmentTask, GeneratedFile


class MockLLM:
    """LLM fake that records prompts and returns a configured response."""

    def __init__(self, response: str) -> None:
        self.response = response
        self.prompts: list[str] = []

    def invoke(self, input: str) -> object:
        self.prompts.append(input)
        return {"content": self.response}


def architecture() -> ArchitectureDesign:
    """Create a small approved architecture fixture."""
    return ArchitectureDesign(
        project_structure=["app/", "tests/"],
        technology_stack=["Python", "FastAPI"],
        design_decisions=["Keep routes separate from service logic."],
    )


def development_task(
    *,
    status: str = "in_progress",
    assigned_agent: str = "developer",
) -> DevelopmentTask:
    """Create a task fixture accepted for code generation."""
    return DevelopmentTask(
        task_id="add-health-route",
        title="Add health route",
        description="Return a healthy status from GET /health.",
        assigned_agent=assigned_agent,
        status=status,
    )


def file_data(
    file_path: str = "app/health.py",
    content: str = "def health() -> str:\n    return 'ok'\n",
) -> dict[str, str]:
    """Create one generated-file JSON object."""
    return {
        "file_path": file_path,
        "content": content,
        "programming_language": "Python",
    }


def agent_for(response: object) -> DeveloperAgent:
    """Create a developer with a mocked JSON response."""
    return DeveloperAgent(MockLLM(json.dumps(response)))


def test_generates_a_single_structured_file() -> None:
    generated = agent_for({"files": [file_data()]}).generate(
        architecture(), development_task(), "Python"
    )

    assert generated == [GeneratedFile.model_validate(file_data())]


def test_generates_multiple_files_and_uses_existing_files_as_context() -> None:
    llm = MockLLM(
        json.dumps({"files": [file_data(), file_data("tests/test_health.py")]})
    )
    agent = DeveloperAgent(llm)
    existing = GeneratedFile(
        file_path="app/main.py",
        content="from app.health import health",
        programming_language="Python",
    )

    files = agent.generate(
        architecture(),
        development_task(),
        "Python",
        previously_generated_files=[existing],
    )

    assert [item.file_path for item in files] == [
        "app/health.py",
        "tests/test_health.py",
    ]
    assert "app/main.py" in llm.prompts[0]
    assert "from app.health import health" in llm.prompts[0]


@pytest.mark.parametrize(
    "file_path",
    [
        "../outside.py",
        "app/../../outside.py",
        "/absolute/path.py",
        "C:/outside.py",
        r"app\outside.py",
        "app/file.py:stream",
    ],
)
def test_rejects_unsafe_file_paths(file_path: str) -> None:
    with pytest.raises(CodeGenerationError, match="path"):
        agent_for({"files": [file_data(file_path)]}).generate(
            architecture(), development_task(), "Python"
        )


def test_rejects_empty_code_response() -> None:
    with pytest.raises(CodeGenerationError, match="empty code"):
        agent_for({"files": [file_data(content="  \n")]}).generate(
            architecture(), development_task(), "Python"
        )


def test_rejects_malformed_structured_response() -> None:
    agent = DeveloperAgent(MockLLM("not valid JSON"))

    with pytest.raises(CodeGenerationError, match="malformed JSON"):
        agent.generate(architecture(), development_task(), "Python")


def test_rejects_missing_file_fields_and_duplicate_paths() -> None:
    with pytest.raises(CodeGenerationError, match="valid generated files"):
        agent_for({"files": [{"file_path": "app/health.py"}]}).generate(
            architecture(), development_task(), "Python"
        )

    with pytest.raises(CodeGenerationError, match="duplicate file path"):
        agent_for({"files": [file_data(), file_data()]}).generate(
            architecture(), development_task(), "Python"
        )


def test_rejects_wrong_agent_assignment_or_incomplete_task() -> None:
    with pytest.raises(CodeGenerationError, match="not developer"):
        agent_for({"files": [file_data()]}).generate(
            architecture(),
            development_task(assigned_agent="tester"),
            "Python",
        )

    with pytest.raises(CodeGenerationError, match="in progress"):
        agent_for({"files": [file_data()]}).generate(
            architecture(),
            development_task(status="pending"),
            "Python",
        )


def test_execute_returns_files_without_writing_them() -> None:
    agent = agent_for({"files": [file_data()]})
    request = AgentInput(
        task="Implement assigned task",
        context={
            "architecture_design": architecture().model_dump(mode="json"),
            "development_task": development_task().model_dump(mode="json"),
            "programming_language": "Python",
        },
    )

    response = agent.execute(request)

    assert response.agent_name == "Developer"
    assert json.loads(response.content)["files"][0]["file_path"] == "app/health.py"
