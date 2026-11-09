"""Agent that generates reviewable source files from an approved architecture."""

import json
from collections.abc import Mapping, Sequence
from pathlib import PurePosixPath, PureWindowsPath

from pydantic import BaseModel, ConfigDict, ValidationError

from app.agents.base_agent import (
    AgentExecutionError,
    AgentInput,
    AgentOutput,
    BaseAgent,
    LLMClient,
)
from app.llm import create_groq_chat_model
from app.models.schemas import ArchitectureDesign, DevelopmentTask, GeneratedFile

_DEVELOPER_SYSTEM_PROMPT = """\
You are DevForge AI's developer agent. Implement only the assigned development
task, following the provided architecture and programming language/framework.
Return only a JSON object with a "files" array. Each file must have exactly
"file_path" (project-relative path using forward slashes), "content" (complete
file contents), and "programming_language".

Use the supplied architecture, task, and previously generated files as the
source of truth. Keep changes consistent with their structure, naming,
dependencies, and interfaces. Include imports and necessary configuration.
Do not add unrequested features, write outside the project, claim tests pass,
or claim that generated code is correct. Leave verification to the testing and
review stages. If required information is absent, do not invent product
requirements; produce only the clearly supportable files.
"""


class CodeGenerationError(AgentExecutionError):
    """Raised when generated files are malformed or unsafe to return."""


class GeneratedFilesResponse(BaseModel):
    """Validated envelope for the files returned by the language model."""

    model_config = ConfigDict(extra="forbid")

    files: list[GeneratedFile]


class DeveloperAgent(BaseAgent):
    """Generate structured code files without writing to the filesystem."""

    def __init__(self, llm: LLMClient | None = None) -> None:
        super().__init__(
            agent_name="Developer",
            role="Generate source files for assigned development tasks",
            llm=llm if llm is not None else create_groq_chat_model(),
            system_prompt=_DEVELOPER_SYSTEM_PROMPT,
        )

    def execute(self, request: AgentInput) -> AgentOutput:
        """Generate files from validated models supplied in the input context."""
        raw_architecture = request.context.get("architecture_design")
        raw_task = request.context.get("development_task")
        raw_language = request.context.get("programming_language")
        raw_existing_files = request.context.get("previously_generated_files", [])

        if not isinstance(raw_architecture, Mapping):
            raise CodeGenerationError(
                "Developer requires an 'architecture_design' object in the input context."
            )
        if not isinstance(raw_task, Mapping):
            raise CodeGenerationError(
                "Developer requires a 'development_task' object in the input context."
            )
        if not isinstance(raw_language, str) or not raw_language.strip():
            raise CodeGenerationError(
                "Developer requires a non-empty 'programming_language' in the input context."
            )
        if not isinstance(raw_existing_files, list):
            raise CodeGenerationError("'previously_generated_files' must be a list.")

        try:
            architecture = ArchitectureDesign.model_validate(raw_architecture)
            task = DevelopmentTask.model_validate(raw_task)
            existing_files = [
                GeneratedFile.model_validate(file_data)
                for file_data in raw_existing_files
            ]
        except ValidationError as error:
            raise CodeGenerationError(
                "Developer received invalid architecture, task, or existing-file data."
            ) from error

        files = self.generate(
            architecture=architecture,
            task=task,
            programming_language=raw_language.strip(),
            previously_generated_files=existing_files,
        )
        return AgentOutput(
            agent_name=self.agent_name,
            content=json.dumps(
                {"files": [file.model_dump(mode="json") for file in files]},
                indent=2,
            ),
        )

    def generate(
        self,
        architecture: ArchitectureDesign,
        task: DevelopmentTask,
        programming_language: str,
        previously_generated_files: Sequence[GeneratedFile] = (),
    ) -> list[GeneratedFile]:
        """Generate and validate files for one task using architecture context."""
        language = programming_language.strip()
        if not language:
            raise CodeGenerationError("Programming language must not be empty.")
        if task.assigned_agent.casefold() != "developer":
            raise CodeGenerationError(
                f"Task '{task.task_id}' is assigned to '{task.assigned_agent}', not developer."
            )
        if task.status.value != "in_progress":
            raise CodeGenerationError(
                f"Task '{task.task_id}' must be in progress before code generation."
            )

        agent_request = AgentInput(
            task=(
                f"Generate the files needed to complete development task "
                f"'{task.task_id}': {task.title}\n{task.description}"
            ),
            context={
                "programming_language": language,
                "architecture_design": architecture.model_dump(mode="json"),
                "development_task": task.model_dump(mode="json"),
                "previously_generated_files": [
                    file.model_dump(mode="json") for file in previously_generated_files
                ],
            },
        )
        response = self.run_llm(agent_request)
        return self._parse_files(response.content, language)

    @staticmethod
    def _parse_files(content: str, programming_language: str) -> list[GeneratedFile]:
        """Parse LLM output and reject empty, duplicate, or unsafe file paths."""
        try:
            payload = json.loads(content)
        except json.JSONDecodeError as error:
            raise CodeGenerationError(
                "Developer returned malformed JSON instead of a file response."
            ) from error
        if not isinstance(payload, Mapping):
            raise CodeGenerationError("Developer response must be a JSON object.")

        try:
            result = GeneratedFilesResponse.model_validate(payload)
        except ValidationError as error:
            raise CodeGenerationError(
                "Developer response must contain valid generated files."
            ) from error

        if not result.files:
            raise CodeGenerationError("Developer returned no generated files.")

        seen_paths: set[str] = set()
        for generated_file in result.files:
            normalized_path = DeveloperAgent._validate_relative_path(
                generated_file.file_path
            )
            path_key = normalized_path.casefold()
            if path_key in seen_paths:
                raise CodeGenerationError(
                    f"Developer returned duplicate file path '{generated_file.file_path}'."
                )
            seen_paths.add(path_key)

            if not generated_file.content.strip():
                raise CodeGenerationError(
                    f"Developer returned empty code for '{generated_file.file_path}'."
                )
            if (
                generated_file.programming_language.casefold()
                != programming_language.casefold()
            ):
                raise CodeGenerationError(
                    f"Generated file '{generated_file.file_path}' does not use the "
                    f"requested language '{programming_language}'."
                )

        return result.files

    @staticmethod
    def _validate_relative_path(file_path: str) -> str:
        """Reject absolute paths, traversal, and Windows-specific path escapes."""
        if "\\" in file_path or "\x00" in file_path:
            raise CodeGenerationError(
                f"Generated file path '{file_path}' must use safe forward-slash separators."
            )

        posix_path = PurePosixPath(file_path)
        windows_path = PureWindowsPath(file_path)
        if (
            posix_path.is_absolute()
            or windows_path.is_absolute()
            or windows_path.drive
            or any(part in {"", ".", ".."} for part in file_path.split("/"))
            or ":" in file_path
            or any(ord(character) < 32 for character in file_path)
        ):
            raise CodeGenerationError(
                f"Generated file path '{file_path}' must be a safe project-relative path."
            )
        return posix_path.as_posix()
