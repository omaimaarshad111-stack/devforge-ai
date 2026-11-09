"""Unit tests for the abstract agent foundation."""

import pytest
from langchain_core.exceptions import LangChainException

from app.agents.base_agent import (
    AgentExecutionError,
    AgentInput,
    AgentOutput,
    AgentResponseError,
    BaseAgent,
)


class FakeLLM:
    """Minimal LLM fake that records prompts and returns a configured result."""

    def __init__(
        self,
        response: object = "A mocked response.",
        error: Exception | None = None,
    ) -> None:
        self.response = response
        self.error = error
        self.prompts: list[str] = []

    def invoke(self, input: str) -> object:
        self.prompts.append(input)
        if self.error is not None:
            raise self.error
        return self.response


class ExampleAgent(BaseAgent):
    """Small test subclass demonstrating the required execute implementation."""

    def execute(self, request: AgentInput) -> AgentOutput:
        return self.run_llm(request)


def create_agent(
    llm: FakeLLM | None = None,
    agent_type: type[BaseAgent] = ExampleAgent,
) -> BaseAgent:
    """Create an agent using a fake model, with no external API calls."""
    return agent_type(
        agent_name="Example",
        role="Test tasks",
        llm=llm or FakeLLM(),
        system_prompt="Follow the test instructions.",
    )


def test_base_agent_cannot_be_instantiated_directly() -> None:
    with pytest.raises(TypeError, match="abstract"):
        BaseAgent(
            agent_name="Base",
            role="Abstract",
            llm=FakeLLM(),
            system_prompt="Instructions.",
        )


def test_subclass_must_implement_execute() -> None:
    class IncompleteAgent(BaseAgent):
        pass

    with pytest.raises(TypeError, match="execute"):
        IncompleteAgent(
            agent_name="Incomplete",
            role="Incomplete test",
            llm=FakeLLM(),
            system_prompt="Instructions.",
        )


def test_execute_builds_prompt_sends_request_and_returns_output() -> None:
    llm = FakeLLM(response={"content": "  Task completed.  "})
    agent = create_agent(llm)
    request = AgentInput(task="Review the module", context={"path": "app/main.py"})

    result = agent.execute(request)

    assert result == AgentOutput(agent_name="Example", content="Task completed.")
    assert len(llm.prompts) == 1
    assert "Follow the test instructions." in llm.prompts[0]
    assert "Role: Test tasks" in llm.prompts[0]
    assert "Review the module" in llm.prompts[0]
    assert '"path": "app/main.py"' in llm.prompts[0]


def test_response_handler_supports_text_content_blocks() -> None:
    agent = create_agent()

    result = agent.handle_response(
        {"content": [{"type": "text", "text": "First."}, {"text": "Second."}]}
    )

    assert result.content == "First.\nSecond."


def test_agent_attributes_are_read_only() -> None:
    agent = create_agent()

    with pytest.raises(AttributeError):
        agent.role = "Changed"  # type: ignore[misc]


def test_provider_errors_are_wrapped_without_exposing_provider_details() -> None:
    secret_like_detail = "request failed; GROQ_API_KEY=private-test-value"
    agent = create_agent(llm=FakeLLM(error=LangChainException(secret_like_detail)))

    with pytest.raises(AgentExecutionError, match="LangChainException") as error:
        agent.execute(AgentInput(task="Run a test"))

    assert "private-test-value" not in str(error.value)
    assert error.value.__cause__ is None


def test_empty_llm_response_raises_response_error() -> None:
    agent = create_agent(llm=FakeLLM(response="  "))

    with pytest.raises(AgentResponseError, match="empty or unsupported"):
        agent.execute(AgentInput(task="Run a test"))


def test_agent_input_rejects_blank_tasks() -> None:
    with pytest.raises(ValueError, match="non-whitespace"):
        AgentInput(task="   ")


def test_agent_input_requires_json_compatible_context() -> None:
    with pytest.raises(ValueError):
        AgentInput(task="Run a test", context={"not_json": object()})


def test_execute_can_be_overridden_with_distinct_behavior() -> None:
    class FixedResultAgent(ExampleAgent):
        def execute(self, request: AgentInput) -> AgentOutput:
            return AgentOutput(agent_name=self.agent_name, content=request.task)

    result = create_agent(agent_type=FixedResultAgent).execute(
        AgentInput(task="A custom implementation")
    )

    assert result.content == "A custom implementation"
