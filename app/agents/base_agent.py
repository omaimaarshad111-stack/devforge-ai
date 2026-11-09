"""Reusable foundation for specialized development agents."""

import json
from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from typing import Protocol

from groq import GroqError
from httpx import HTTPError
from langchain_core.exceptions import LangChainException
from pydantic import BaseModel, Field, JsonValue, field_validator


class LLMClient(Protocol):
    """Small interface for language models that accept a prompt and return a result."""

    def invoke(self, input: str) -> object:
        """Send a prompt to the model and return its response."""
        ...


class AgentInput(BaseModel):
    """Task data passed to an agent, without a reference to shared workflow state."""

    task: str = Field(min_length=1)
    context: dict[str, JsonValue] = Field(default_factory=dict)

    @field_validator("task")
    @classmethod
    def task_must_not_be_blank(cls, value: str) -> str:
        """Reject whitespace-only tasks and normalize surrounding whitespace."""
        normalized = value.strip()
        if not normalized:
            raise ValueError("task must contain non-whitespace text")
        return normalized


class AgentOutput(BaseModel):
    """Structured result returned by an agent."""

    agent_name: str = Field(min_length=1)
    content: str = Field(min_length=1)


class AgentExecutionError(RuntimeError):
    """Raised when an agent cannot complete its language-model operation."""


class AgentResponseError(AgentExecutionError):
    """Raised when a language model returns no usable text."""


class BaseAgent(ABC):
    """Abstract agent that builds prompts and communicates with an LLM.

    Subclasses implement ``execute`` to add their own behavior. They can use
    ``run_llm`` for the shared prompt/request/response path. Agent inputs are
    task data only; the base class has no access to workflow state.
    """

    def __init__(
        self,
        agent_name: str,
        role: str,
        llm: LLMClient,
        system_prompt: str,
    ) -> None:
        self._agent_name = self._require_text(agent_name, "agent_name")
        self._role = self._require_text(role, "role")
        self._system_prompt = self._require_text(system_prompt, "system_prompt")
        self._llm = llm

    @property
    def agent_name(self) -> str:
        """Return this agent's display name."""
        return self._agent_name

    @property
    def role(self) -> str:
        """Return this agent's responsibility."""
        return self._role

    @property
    def llm(self) -> LLMClient:
        """Return the injected language-model client."""
        return self._llm

    @property
    def system_prompt(self) -> str:
        """Return this agent's system instructions."""
        return self._system_prompt

    @staticmethod
    def _require_text(value: str, field_name: str) -> str:
        """Validate and normalize required constructor text."""
        normalized = value.strip()
        if not normalized:
            raise ValueError(f"{field_name} must contain non-whitespace text")
        return normalized

    @abstractmethod
    def execute(self, request: AgentInput) -> AgentOutput:
        """Perform this agent's task and return a structured result."""
        raise NotImplementedError

    def build_prompt(self, request: AgentInput) -> str:
        """Combine system instructions and task data into a readable prompt."""
        context = json.dumps(request.context, indent=2, sort_keys=True)
        return (
            f"{self.system_prompt}\n\n"
            f"Role: {self.role}\n\n"
            f"Task:\n{request.task}\n\n"
            f"Context (JSON):\n{context}"
        )

    def send_request(self, prompt: str) -> object:
        """Send a prompt to the injected LLM and sanitize provider errors."""
        try:
            return self.llm.invoke(prompt)
        except (GroqError, HTTPError, LangChainException) as error:
            raise self.handle_error(error) from None

    def handle_response(self, response: object) -> AgentOutput:
        """Extract text from a LangChain-style response and validate its shape."""
        if isinstance(response, str):
            content: object = response
        elif isinstance(response, Mapping):
            content = response.get("content")
        else:
            content = getattr(response, "content", None)

        if isinstance(content, str):
            text = content
        elif isinstance(content, Sequence) and not isinstance(content, (str, bytes)):
            text = self._text_from_content_blocks(content)
        else:
            text = ""

        normalized = text.strip()
        if not normalized:
            raise AgentResponseError(
                f"{self.agent_name} received an empty or unsupported LLM response."
            )
        return AgentOutput(agent_name=self.agent_name, content=normalized)

    @staticmethod
    def _text_from_content_blocks(blocks: Sequence[object]) -> str:
        """Join text blocks from providers that return multimodal content lists."""
        text_parts: list[str] = []
        for block in blocks:
            if isinstance(block, str):
                text_parts.append(block)
            elif isinstance(block, Mapping):
                text = block.get("text")
                if isinstance(text, str):
                    text_parts.append(text)
        return "\n".join(text_parts)

    def handle_error(self, error: Exception) -> AgentExecutionError:
        """Create a safe error message without copying provider details or secrets."""
        return AgentExecutionError(
            f"{self.agent_name} could not complete its LLM request "
            f"({type(error).__name__}). Check provider configuration and try again."
        )

    def run_llm(self, request: AgentInput) -> AgentOutput:
        """Run the common prompt, request, and response-handling sequence."""
        prompt = self.build_prompt(request)
        response = self.send_request(prompt)
        return self.handle_response(response)
