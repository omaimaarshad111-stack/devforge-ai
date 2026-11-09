"""Agent that designs a practical architecture from a project request and plan."""

import json
from collections.abc import Mapping, Sequence

from pydantic import ValidationError

from app.agents.base_agent import (
    AgentExecutionError,
    AgentInput,
    AgentOutput,
    BaseAgent,
    LLMClient,
)
from app.llm import create_groq_chat_model
from app.models.schemas import (
    ArchitectureDesign,
    DevelopmentTask,
    ProjectRequest,
)

_ARCHITECT_SYSTEM_PROMPT = """\
You are DevForge AI's software architect. Propose a clear, maintainable
architecture that addresses the supplied project requirements and development
tasks. Respect the requested language, framework, and other technology
preferences. Prefer the simplest design that meets the stated requirements.

Return only one JSON object with these fields:
- project_structure: non-empty list of folder/file paths.
- technology_stack: non-empty list of technologies, including requested
  language/framework preferences.
- application_architecture: list of objects with name, responsibility, and
  dependencies (module names).
- database_design: null when persistence is not required, otherwise an object
  with database_engine, entities (name, fields, relationships), and design_notes.
- api_endpoints: list of objects with method, path, purpose, request_schema,
  response_schema. Use an empty list if no API is applicable.
- dependencies: list of runtime package names.
- design_decisions: list of concise decisions and rationale.
- requirement_coverage: one object per supplied requirement, preserving its
  exact text in "requirement"; include "covered_by", "missing_information",
  and "notes". Identify unknowns as missing information instead of inventing
  behavior.

Do not write source code. Do not invent requirements. Keep components focused,
separate presentation/API, business logic, and persistence concerns, and depend
on abstractions where that reduces coupling without adding needless complexity.
"""


class ArchitectureDesignError(AgentExecutionError):
    """Raised when an architecture response is invalid or incomplete."""


class ArchitectAgent(BaseAgent):
    """Create and validate a software architecture without implementing it."""

    def __init__(self, llm: LLMClient | None = None) -> None:
        super().__init__(
            agent_name="Software Architect",
            role="Design maintainable software architectures from project plans",
            llm=llm if llm is not None else create_groq_chat_model(),
            system_prompt=_ARCHITECT_SYSTEM_PROMPT,
        )

    def execute(self, request: AgentInput) -> AgentOutput:
        """Implement BaseAgent's interface using serialized request context."""
        raw_request = request.context.get("project_request")
        raw_tasks = request.context.get("development_tasks", [])
        raw_preferences = request.context.get("technology_preferences", [])
        if not isinstance(raw_request, Mapping):
            raise ArchitectureDesignError(
                "Architect requires a 'project_request' object in the input context."
            )
        if not isinstance(raw_tasks, list):
            raise ArchitectureDesignError("'development_tasks' must be a list.")
        if not isinstance(raw_preferences, list) or not all(
            isinstance(preference, str) for preference in raw_preferences
        ):
            raise ArchitectureDesignError(
                "'technology_preferences' must be a list of strings."
            )

        try:
            project_request = ProjectRequest.model_validate(raw_request)
            tasks = [DevelopmentTask.model_validate(task) for task in raw_tasks]
        except ValidationError as error:
            raise ArchitectureDesignError(
                "Architect received an invalid project request or task list."
            ) from error

        design = self.design(project_request, tasks, raw_preferences)
        return AgentOutput(
            agent_name=self.agent_name,
            content=json.dumps(design.model_dump(mode="json"), indent=2),
        )

    def design(
        self,
        request: ProjectRequest,
        tasks: Sequence[DevelopmentTask] = (),
        technology_preferences: Sequence[str] = (),
    ) -> ArchitectureDesign:
        """Ask the LLM for an architecture and validate its completeness."""
        preferences = list(technology_preferences)
        if request.programming_language not in preferences:
            preferences.insert(0, request.programming_language)
        if (
            request.preferred_framework
            and request.preferred_framework not in preferences
        ):
            preferences.append(request.preferred_framework)

        agent_request = AgentInput(
            task=(
                "Design the architecture for this project. Address every explicit "
                "requirement or mark it as needing clarification."
            ),
            context={
                "project_request": request.model_dump(mode="json"),
                "development_tasks": [task.model_dump(mode="json") for task in tasks],
                "technology_preferences": preferences,
            },
        )
        response = self.run_llm(agent_request)
        return self._validate_design(response.content, request, preferences)

    @staticmethod
    def _validate_design(
        content: str,
        request: ProjectRequest,
        preferences: Sequence[str],
    ) -> ArchitectureDesign:
        """Parse the architecture and verify requirement and preference coverage."""
        try:
            payload = json.loads(content)
        except json.JSONDecodeError as error:
            raise ArchitectureDesignError("Architect returned invalid JSON.") from error
        if not isinstance(payload, Mapping):
            raise ArchitectureDesignError("Architect response must be a JSON object.")

        raw_design = payload.get("architecture", payload)
        try:
            design = ArchitectureDesign.model_validate(raw_design)
        except ValidationError as error:
            raise ArchitectureDesignError(
                "Architect returned an invalid or incomplete architecture."
            ) from error

        coverage_by_requirement = {
            coverage.requirement: coverage for coverage in design.requirement_coverage
        }
        missing_coverage = [
            requirement
            for requirement in request.requirements
            if requirement not in coverage_by_requirement
        ]
        if missing_coverage:
            raise ArchitectureDesignError(
                "Architecture does not account for requirement(s): "
                + "; ".join(missing_coverage)
            )

        stack_items = {technology.casefold() for technology in design.technology_stack}
        absent_preferences = [
            preference
            for preference in preferences
            if not any(
                preference.casefold() in technology for technology in stack_items
            )
        ]
        if absent_preferences:
            raise ArchitectureDesignError(
                "Architecture does not include requested technology preference(s): "
                + ", ".join(absent_preferences)
            )

        return design
