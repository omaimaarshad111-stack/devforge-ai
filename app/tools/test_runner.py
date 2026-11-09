"""Isolated test execution through a resource-limited Docker container."""

import logging
import os
import re
import tempfile
import time
import uuid
from enum import StrEnum
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from app.models.schemas import GeneratedFile
from app.tools.docker_sandbox import run_docker_container

_SUMMARY_PATTERN = re.compile(r"(?P<count>\d+)\s+(?P<kind>passed|failed|errors?)")
_MAX_PROJECT_BYTES = 10_000_000
_MAX_PROJECT_FILES = 500
_MAX_FILE_BYTES = 2_000_000
_SENSITIVE_NAMES = frozenset(
    {
        ".npmrc",
        ".pypirc",
        "credentials",
        "credentials.json",
        "id_ed25519",
        "id_rsa",
        "secrets.json",
    }
)
_SENSITIVE_SUFFIXES = frozenset({".key", ".pem", ".p12", ".pfx"})


class TestExecutionStatus(StrEnum):
    """Outcome categories that separate test failures from runner problems."""

    __test__ = False

    PASSED = "passed"
    FAILED = "failed"
    ERROR = "error"
    TIMEOUT = "timeout"
    MISSING_TESTS = "missing_tests"
    UNSUPPORTED_LANGUAGE = "unsupported_language"
    SANDBOX_UNAVAILABLE = "sandbox_unavailable"


class TestExecutionResult(BaseModel):
    """Structured result returned by an isolated test runner."""

    __test__ = False
    model_config = ConfigDict(extra="forbid")

    status: TestExecutionStatus
    total_tests: int = Field(default=0, ge=0)
    passed_tests: int = Field(default=0, ge=0)
    failed_tests: int = Field(default=0, ge=0)
    exit_code: int | None = None
    execution_duration: float | None = Field(default=None, ge=0)
    output: str = ""
    stdout: str = ""
    stderr: str = ""
    execution_error: str | None = None


class TestRunner(Protocol):
    """Interface used by the Testing Agent to request an isolated test run."""

    __test__ = False

    def run(
        self,
        files: list[GeneratedFile],
        programming_language: str,
        timeout_seconds: int = 30,
    ) -> TestExecutionResult:
        """Run project tests without executing generated code on the host."""
        ...


class DockerTestRunner:
    """Stage project files and execute pytest in a constrained Docker sandbox.

    Build ``app/tools/test_runner.Dockerfile`` before use. The project is mounted
    read-only, container networking is disabled, and CPU, memory, process, and
    wall-clock limits are applied. Generated project files are never executed
    by the host Python interpreter.
    """

    def __init__(
        self,
        image: str | None = None,
        docker_executable: str = "docker",
        memory_limit: str = "512m",
        cpu_limit: str = "1",
        process_limit: int = 64,
    ) -> None:
        self._image = image or os.getenv(
            "DEVFORGE_TEST_RUNNER_IMAGE", "devforge-ai-test-runner:latest"
        )
        self._docker_executable = docker_executable
        if (
            not self._image
            or self._image.startswith("-")
            or any(
                ord(character) < 33 or ord(character) > 126 for character in self._image
            )
        ):
            raise ValueError("image must be a non-empty Docker image reference.")
        memory_match = re.fullmatch(r"(\d+)[bkmg]?", memory_limit, re.IGNORECASE)
        if memory_match is None or int(memory_match.group(1)) < 1:
            raise ValueError("memory_limit must be a positive Docker memory value.")
        if not re.fullmatch(r"(?:\d+(?:\.\d+)?|\.\d+)", cpu_limit):
            raise ValueError("cpu_limit must be a positive number.")
        if float(cpu_limit) <= 0:
            raise ValueError("cpu_limit must be a positive number.")
        self._memory_limit = memory_limit
        self._cpu_limit = cpu_limit
        if process_limit < 1:
            raise ValueError("process_limit must be positive")
        self._process_limit = process_limit
        self._logger = logging.getLogger(__name__)

    def run(
        self,
        files: list[GeneratedFile],
        programming_language: str,
        timeout_seconds: int = 30,
    ) -> TestExecutionResult:
        """Run Python pytest files in Docker and classify its exit result."""
        if programming_language.casefold() not in {"python", "python3"}:
            return TestExecutionResult(
                status=TestExecutionStatus.UNSUPPORTED_LANGUAGE,
                execution_error=(
                    f"No isolated test runner is configured for "
                    f"'{programming_language}'."
                ),
            )
        if timeout_seconds < 1:
            raise ValueError("timeout_seconds must be at least one")
        if timeout_seconds > 300:
            raise ValueError("timeout_seconds must not exceed 300")

        validation_error = self._validate_file_paths(files)
        if validation_error:
            return TestExecutionResult(
                status=TestExecutionStatus.ERROR,
                execution_error=validation_error,
            )
        if not any(self._is_test_file(file.file_path) for file in files):
            return TestExecutionResult(
                status=TestExecutionStatus.MISSING_TESTS,
                execution_error=(
                    "No pytest files found. Add tests/test_*.py or test_*.py."
                ),
            )

        started_at = time.perf_counter()
        try:
            with tempfile.TemporaryDirectory(prefix="devforge-test-") as directory:
                workspace = Path(directory)
                self._stage_files(workspace, files)
                container_name = f"devforge-test-{uuid.uuid4().hex}"
                command = self._build_command(
                    workspace,
                    timeout_seconds,
                    container_name,
                )
                completed = run_docker_container(
                    command,
                    timeout_seconds + 5,
                    self._docker_executable,
                    container_name,
                    self._logger,
                )
        except FileNotFoundError:
            return TestExecutionResult(
                status=TestExecutionStatus.SANDBOX_UNAVAILABLE,
                execution_error=(
                    "Docker is unavailable. Install and start Docker Desktop, then "
                    "build the isolated runner image."
                ),
            )
        except OSError:
            return TestExecutionResult(
                status=TestExecutionStatus.ERROR,
                execution_error=(
                    "Could not stage project files or start the Docker CLI."
                ),
            )

        stdout = completed.stdout
        stderr = completed.stderr
        output = self._combine_output(stdout, stderr)
        if completed.timed_out:
            return TestExecutionResult(
                status=TestExecutionStatus.TIMEOUT,
                exit_code=completed.returncode,
                execution_duration=time.perf_counter() - started_at,
                output=output,
                stdout=stdout,
                stderr=stderr,
                execution_error=(
                    f"Test execution exceeded the {timeout_seconds}-second "
                    "container timeout."
                ),
            )
        if completed.returncode is None:
            return TestExecutionResult(
                status=TestExecutionStatus.ERROR,
                execution_duration=time.perf_counter() - started_at,
                output=output,
                stdout=stdout,
                stderr=stderr,
                execution_error="Docker did not return an exit status.",
            )
        result = self._classify_exit(
            completed.returncode,
            output,
            stdout=stdout,
            stderr=stderr,
        )
        return result.model_copy(
            update={
                "exit_code": completed.returncode,
                "execution_duration": time.perf_counter() - started_at,
            }
        )

    def _build_command(
        self,
        workspace: Path,
        timeout_seconds: int,
        container_name: str | None = None,
    ) -> list[str]:
        """Build a Docker command with no network and strict container limits."""
        name = container_name or f"devforge-test-{uuid.uuid4().hex}"
        return [
            self._docker_executable,
            "run",
            "--rm",
            f"--name={name}",
            "--network=none",
            f"--memory={self._memory_limit}",
            f"--cpus={self._cpu_limit}",
            f"--pids-limit={self._process_limit}",
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--user=65534:65534",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,size=64m,uid=65534,gid=65534",
            "-e",
            "PYTHONDONTWRITEBYTECODE=1",
            "-e",
            "PYTHONPATH=/workspace",
            "-e",
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD=1",
            "--mount",
            f"type=bind,source={workspace},target=/workspace,readonly",
            "--workdir=/workspace",
            self._image,
            "python",
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            "-p",
            "pytest_timeout",
            "--tb=short",
            "--timeout",
            str(timeout_seconds),
        ]

    @staticmethod
    def _stage_files(workspace: Path, files: list[GeneratedFile]) -> None:
        """Write validated relative paths into the temporary sandbox workspace."""
        for generated_file in files:
            relative_path = PurePosixPath(generated_file.file_path)
            target = workspace.joinpath(*relative_path.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(generated_file.content, encoding="utf-8")

    @staticmethod
    def _validate_file_paths(files: list[GeneratedFile]) -> str | None:
        """Reject path traversal and duplicate paths before staging files."""
        if len(files) > _MAX_PROJECT_FILES:
            return f"Project contains more than {_MAX_PROJECT_FILES} files."
        seen_paths: set[str] = set()
        total_bytes = 0
        for generated_file in files:
            path = generated_file.file_path
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
                return f"Unsafe project-relative file path: '{path}'."
            path_key = posix_path.as_posix().casefold()
            if path_key in seen_paths:
                return f"Duplicate project file path: '{path}'."
            seen_paths.add(path_key)
            file_name = PurePosixPath(path).name.casefold()
            if (
                file_name.startswith(".env")
                or file_name in _SENSITIVE_NAMES
                or PurePosixPath(file_name).suffix in _SENSITIVE_SUFFIXES
            ):
                return f"Sensitive project file '{path}' cannot be staged."
            try:
                file_bytes = len(generated_file.content.encode("utf-8"))
            except UnicodeEncodeError:
                return f"Project file '{path}' is not valid UTF-8 text."
            if file_bytes > _MAX_FILE_BYTES:
                return f"Project file '{path}' exceeds the 2 MB staging limit."
            total_bytes += file_bytes
        if total_bytes > _MAX_PROJECT_BYTES:
            return "Project exceeds the 10 MB staging limit."
        return None

    @staticmethod
    def _is_test_file(file_path: str) -> bool:
        """Recognize pytest's standard test file naming conventions."""
        name = PurePosixPath(file_path).name
        return name.startswith("test_") and name.endswith(".py")

    @staticmethod
    def _combine_output(stdout: str | bytes | None, stderr: str | bytes | None) -> str:
        """Join process output while handling timeout output returned as bytes."""
        parts: list[str] = []
        for output in (stdout, stderr):
            if isinstance(output, bytes):
                parts.append(output.decode("utf-8", errors="replace"))
            elif isinstance(output, str) and output:
                parts.append(output)
        return "\n".join(parts)[-12000:]

    @staticmethod
    def _classify_exit(
        return_code: int,
        output: str,
        *,
        stdout: str = "",
        stderr: str = "",
    ) -> TestExecutionResult:
        """Translate pytest output to test failure or execution error."""
        summary_counts: dict[str, int] = {}
        for line in reversed(output.splitlines()):
            matches = list(_SUMMARY_PATTERN.finditer(line))
            if matches:
                summary_counts = {
                    match.group("kind").rstrip("s"): int(match.group("count"))
                    for match in matches
                }
                break
        passed = summary_counts.get("passed", 0)
        failed = summary_counts.get("failed", 0)
        errors = summary_counts.get("error", 0)
        total = passed + failed

        if (
            return_code == 125
            or "Cannot connect to the Docker daemon" in output
            or "No such image" in output
        ):
            return TestExecutionResult(
                status=TestExecutionStatus.SANDBOX_UNAVAILABLE,
                exit_code=return_code,
                output=output,
                stdout=stdout,
                stderr=stderr,
                execution_error=(
                    "Docker could not start the sandbox. Start Docker Desktop and "
                    "build the configured test runner image."
                ),
            )
        if return_code == 0:
            return TestExecutionResult(
                status=TestExecutionStatus.PASSED,
                exit_code=return_code,
                total_tests=total,
                passed_tests=passed,
                output=output,
                stdout=stdout,
                stderr=stderr,
            )
        if "Failed: Timeout" in output or "pytest-timeout" in output:
            return TestExecutionResult(
                status=TestExecutionStatus.TIMEOUT,
                exit_code=return_code,
                total_tests=total,
                passed_tests=passed,
                failed_tests=failed,
                output=output,
                stdout=stdout,
                stderr=stderr,
                execution_error="A test exceeded the configured per-test timeout.",
            )
        if return_code == 1 and failed:
            return TestExecutionResult(
                status=TestExecutionStatus.FAILED,
                exit_code=return_code,
                total_tests=total,
                passed_tests=passed,
                failed_tests=failed,
                output=output,
                stdout=stdout,
                stderr=stderr,
            )

        if errors or "SyntaxError" in output or "ERROR collecting" in output:
            message = "Test collection failed, likely due to a syntax or import error."
        elif return_code == 5:
            message = "Pytest could not collect any tests from the supplied files."
        else:
            message = (
                f"Isolated test runner exited unexpectedly with code {return_code}."
            )
        return TestExecutionResult(
            status=TestExecutionStatus.ERROR,
            exit_code=return_code,
            output=output,
            stdout=stdout,
            stderr=stderr,
            execution_error=message,
        )
