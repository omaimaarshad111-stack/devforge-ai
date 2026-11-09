"""Agent that proposes targeted corrections for failed generated code."""

import json
from collections.abc import Sequence
from pathlib import PurePosixPath, PureWindowsPath

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
    DebuggingResponse,
    GeneratedFile,
    ReviewResult,
    TestResult,
)

_DEBUGGER_SYSTEM_PROMPT = """\
You are DevForge AI's debugging agent. Diagnose the likely root cause using
the failing test results, code-review feedback, architecture, and provided
source files. Return only a JSON object with "root_cause" and a non-empty
"corrections" array. Each correction contains "file" (file_path, content,
programming_language), "change_summary", and "rationale".

Make the smallest targeted changes needed to address the observed failures.
Preserve unrelated code and keep the approved architecture and language.
Return only files that need changes. Do not write to the filesystem, claim
the correction is fixed, or assert tests pass; the Testing Agent must rerun
tests on the proposed files.
"""

DEFAULT_MAX_DEBUGGING_ATTEMPTS = 3


class DebuggingError(AgentExecutionError):
    """Raised when debugging input is invalid or a correction is unsafe."""


class MaxDebuggingAttemptsExceeded(DebuggingError):
    """Raised when the configured debugging retry budget has been exhausted."""


class DebuggingAttemptLimit:
    """Encapsulate the maximum and current debugging-attempt count."""

    def __init__(self, max_attempts: int = DEFAULT_MAX_DEBUGGING_ATTEMPTS) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least one")
        self._max_attempts = max_attempts
        self._attempts_used = 0

    @property
    def max_attempts(self) -> int:
        """Return the configured maximum number of correction attempts."""
        return self._max_attempts

    @property
    def attempts_used(self) -> int:
        """Return how many debugging attempts have been started."""
        return self._attempts_used

    @property
    def remaining_attempts(self) -> int:
        """Return the number of attempts still available."""
        return self._max_attempts - self._attempts_used

    def consume(self) -> int:
        """Consume one attempt or raise when the retry budget is exhausted."""
        if self._attempts_used >= self._max_attempts:
            raise MaxDebuggingAttemptsExceeded(
                f"Maximum debugging attempts ({self._max_attempts}) reached. "
                "Run tests on previous corrections before retrying."
            )
        self._attempts_used += 1
        return self._attempts_used


class DebuggerAgent(BaseAgent):
    """Propose replacement file values; the workflow retains control of files."""

    def __init__(
        self,
        llm: LLMClient | None = None,
        max_attempts: int = DEFAULT_MAX_DEBUGGING_ATTEMPTS,
        attempt_limit: DebuggingAttemptLimit | None = None,
    ) -> None:
        super().__init__(
            agent_name="Debugger",
            role="Diagnose test failures and propose minimal code corrections",
            llm=llm if llm is not None else create_groq_chat_model(),
            system_prompt=_DEBUGGER_SYSTEM_PROMPT,
        )
        if attempt_limit is not None and max_attempts != DEFAULT_MAX_DEBUGGING_ATTEMPTS:
            raise ValueError(
                "Pass either attempt_limit or a non-default max_attempts, not both."
            )
        self._attempt_limit = (
            attempt_limit
            if attempt_limit is not None
            else DebuggingAttemptLimit(max_attempts)
        )

    @property
    def attempts_used(self) -> int:
        """Return the number of correction attempts made by this instance."""
        return self._attempt_limit.attempts_used

    @property
    def max_attempts(self) -> int:
        """Return this instance's configured retry limit."""
        return self._attempt_limit.max_attempts

    def execute(self, request: AgentInput) -> AgentOutput:
        """Implement BaseAgent's interface using serialized debugging context."""
        raw_files = request.context.get("generated_files")
        raw_test_result = request.context.get("test_result")
        raw_architecture = request.context.get("architecture_design")
        raw_review = request.context.get("review_result")
        if not isinstance(raw_files, list):
            raise DebuggingError("'generated_files' must be a list.")
        if not isinstance(raw_test_result, dict):
            raise DebuggingError("'test_result' must be an object.")
        if not isinstance(raw_architecture, dict):
            raise DebuggingError("'architecture_design' must be an object.")
        if raw_review is not None and not isinstance(raw_review, dict):
            raise DebuggingError("'review_result' must be an object when provided.")

        try:
            files = [GeneratedFile.model_validate(file_data) for file_data in raw_files]
            test_result = TestResult.model_validate(raw_test_result)
            architecture = ArchitectureDesign.model_validate(raw_architecture)
            review_result = (
                ReviewResult.model_validate(raw_review)
                if raw_review is not None
                else None
            )
        except ValidationError as error:
            raise DebuggingError(
                "Debugger received invalid structured input."
            ) from error

        response = self.propose_corrections(
            files=files,
            test_result=test_result,
            architecture=architecture,
            review_result=review_result,
        )
        return AgentOutput(
            agent_name=self.agent_name,
            content=json.dumps(response.model_dump(mode="json"), indent=2),
        )

    def propose_corrections(
        self,
        files: Sequence[GeneratedFile],
        test_result: TestResult,
        architecture: ArchitectureDesign,
        review_result: ReviewResult | None = None,
        *,
        attempt_number: int | None = None,
        maximum_attempts: int | None = None,
    ) -> DebuggingResponse:
        """Consume an attempt, diagnose failures, and return validated file updates."""
        if not test_result.failed_tests and not test_result.execution_error:
            raise DebuggingError(
                "Debugger requires failed tests or a test execution error."
            )
        if not files:
            raise DebuggingError("Debugger requires source files to analyze.")

        if attempt_number is None:
            attempt_number = self._attempt_limit.consume()
            maximum_attempts = self.max_attempts
        else:
            maximum_attempts = maximum_attempts or self.max_attempts
            if not 1 <= attempt_number <= maximum_attempts:
                raise DebuggingError(
                    "The supplied debugging attempt number is outside its retry limit."
                )
        request = AgentInput(
            task=(
                f"Diagnose test attempt {attempt_number} and propose minimal "
                "corrections. The Testing Agent will verify them afterward."
            ),
            context={
                "attempt_number": attempt_number,
                "maximum_attempts": maximum_attempts,
                "remaining_attempts": maximum_attempts - attempt_number,
                "approved_architecture": architecture.model_dump(mode="json"),
                "generated_files": [
                    generated_file.model_dump(mode="json") for generated_file in files
                ],
                "test_result": test_result.model_dump(mode="json"),
                "review_result": (
                    review_result.model_dump(mode="json")
                    if review_result is not None
                    else None
                ),
            },
        )
        raw_response = self.run_llm(request)
        try:
            payload = json.loads(raw_response.content)
            response = DebuggingResponse.model_validate(payload)
        except (json.JSONDecodeError, ValidationError) as error:
            raise DebuggingError(
                "Debugger returned a malformed or incomplete correction response."
            ) from error

        self._validate_corrections(response, files)
        return response

    @staticmethod
    def _validate_corrections(
        response: DebuggingResponse,
        original_files: Sequence[GeneratedFile],
    ) -> None:
        """Prevent corrections from introducing or renaming unrelated files."""
        original_by_path = {
            generated_file.file_path.casefold(): generated_file
            for generated_file in original_files
        }
        changed_paths: set[str] = set()
        for correction in response.corrections:
            path = correction.file.file_path
            posix_path = PurePosixPath(path)
            windows_path = PureWindowsPath(path)
            if (
                "\\" in path
                or "\x00" in path
                or ":" in path
                or posix_path.is_absolute()
                or windows_path.is_absolute()
                or windows_path.drive
                or any(part in {"", ".", ".."} for part in path.split("/"))
                or any(ord(character) < 32 for character in path)
            ):
                raise DebuggingError(
                    f"Correction path '{path}' must be a safe project-relative path."
                )
            path_key = posix_path.as_posix().casefold()
            if path_key not in original_by_path:
                raise DebuggingError(
                    f"Correction path '{path}' does not match an existing generated file."
                )
            if path_key in changed_paths:
                raise DebuggingError(
                    f"Debugger returned duplicate correction for '{path}'."
                )
            changed_paths.add(path_key)

            original = original_by_path[path_key]
            if (
                correction.file.programming_language.casefold()
                != original.programming_language.casefold()
            ):
                raise DebuggingError(
                    f"Correction for '{path}' changes the file's programming language."
                )
            if correction.file.content == original.content:
                raise DebuggingError(
                    f"Correction for '{path}' does not change the file content."
                )


__all__ = [
    "DEFAULT_MAX_DEBUGGING_ATTEMPTS",
    "DebuggerAgent",
    "DebuggingAttemptLimit",
    "DebuggingError",
    "MaxDebuggingAttemptsExceeded",
]
