"""Run generated Python entry points in disposable Docker containers."""

import logging
import os
import re
import tempfile
import time
import uuid
from collections.abc import Sequence
from enum import StrEnum
from pathlib import Path, PurePosixPath, PureWindowsPath

from pydantic import BaseModel, ConfigDict, Field

from app.models.schemas import GeneratedFile
from app.tools.docker_sandbox import run_docker_container

_DEFAULT_IMAGE = "devforge-ai-test-runner:latest"
_MAX_TIMEOUT_SECONDS = 300
_MAX_FILE_BYTES = 2_000_000
_MAX_PROJECT_BYTES = 10_000_000
_MAX_PROJECT_FILES = 500
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


class ExecutionStatus(StrEnum):
    """Outcome categories for a Python run in the Docker sandbox."""

    COMPLETED = "completed"
    FAILED = "failed"
    TIMEOUT = "timeout"
    ERROR = "error"
    UNSUPPORTED_LANGUAGE = "unsupported_language"
    SANDBOX_UNAVAILABLE = "sandbox_unavailable"


class ExecutionResult(BaseModel):
    """Structured process result with independent standard output streams."""

    model_config = ConfigDict(extra="forbid")

    status: ExecutionStatus
    exit_code: int | None = None
    execution_duration: float | None = Field(default=None, ge=0)
    stdout: str = ""
    stderr: str = ""
    execution_error: str | None = None

    @property
    def output(self) -> str:
        """Return stdout and stderr together for display-oriented consumers."""
        return "\n".join(part for part in (self.stdout, self.stderr) if part)


class DockerCodeExecutor:
    """Execute a Python entry point using only a temporary read-only workspace.

    Containers have no network, run as an unprivileged user, and receive no host
    environment variables or mounts other than the staged project workspace.
    """

    def __init__(
        self,
        image: str | None = None,
        docker_executable: str = "docker",
        memory_limit: str = "512m",
        cpu_limit: str = "1",
        process_limit: int = 64,
        logger: logging.Logger | None = None,
    ) -> None:
        self._image = image or os.getenv("DEVFORGE_CODE_RUNNER_IMAGE", _DEFAULT_IMAGE)
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
        self._logger = logger or logging.getLogger(__name__)

    def run(
        self,
        files: Sequence[GeneratedFile],
        programming_language: str,
        entrypoint: str = "main.py",
        timeout_seconds: int = 30,
    ) -> ExecutionResult:
        """Run one Python entry point and return its captured process result."""
        if programming_language.casefold() not in {"python", "python3"}:
            return ExecutionResult(
                status=ExecutionStatus.UNSUPPORTED_LANGUAGE,
                execution_error=(
                    f"No isolated code executor is configured for "
                    f"'{programming_language}'."
                ),
            )
        if not 1 <= timeout_seconds <= _MAX_TIMEOUT_SECONDS:
            raise ValueError(
                f"timeout_seconds must be between 1 and {_MAX_TIMEOUT_SECONDS}"
            )

        validation_error = self._validate_file_paths(files)
        if validation_error:
            return ExecutionResult(
                status=ExecutionStatus.ERROR,
                execution_error=validation_error,
            )
        if not self._is_safe_relative_path(entrypoint):
            return ExecutionResult(
                status=ExecutionStatus.ERROR,
                execution_error=f"Unsafe project-relative entry point: '{entrypoint}'.",
            )
        if entrypoint not in {file.file_path for file in files}:
            return ExecutionResult(
                status=ExecutionStatus.ERROR,
                execution_error=f"Entry point '{entrypoint}' was not supplied.",
            )
        if PurePosixPath(entrypoint).suffix.casefold() != ".py":
            return ExecutionResult(
                status=ExecutionStatus.ERROR,
                execution_error="The Python entry point must have a .py extension.",
            )

        container_name = f"devforge-run-{uuid.uuid4().hex}"
        started_at = time.perf_counter()
        try:
            with tempfile.TemporaryDirectory(prefix="devforge-run-") as directory:
                workspace = Path(directory)
                self._stage_files(workspace, files)
                command = self._build_command(
                    workspace,
                    entrypoint,
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
            return ExecutionResult(
                status=ExecutionStatus.SANDBOX_UNAVAILABLE,
                execution_error=(
                    "Docker is unavailable. Install and start Docker Desktop, then "
                    "build the isolated runner image."
                ),
            )
        except OSError:
            self._logger.exception(
                "Could not stage files or start the Docker CLI for isolated execution."
            )
            return ExecutionResult(
                status=ExecutionStatus.ERROR,
                execution_error=(
                    "Could not stage project files or start the Docker CLI."
                ),
            )

        if completed.timed_out:
            return ExecutionResult(
                status=ExecutionStatus.TIMEOUT,
                stdout=completed.stdout,
                stderr=completed.stderr,
                execution_duration=time.perf_counter() - started_at,
                execution_error=(
                    f"Execution exceeded the {timeout_seconds}-second "
                    "container timeout."
                ),
            )
        stdout = completed.stdout
        stderr = completed.stderr
        if completed.returncode in {125, 126, 127}:
            return ExecutionResult(
                status=ExecutionStatus.SANDBOX_UNAVAILABLE,
                exit_code=completed.returncode,
                execution_duration=time.perf_counter() - started_at,
                stdout=stdout,
                stderr=stderr,
                execution_error=(
                    "Docker could not start the sandbox. Start Docker Desktop and "
                    "build the configured runner image."
                ),
            )
        if completed.returncode == 0:
            if completed.returncode is None:
                return ExecutionResult(
                    status=ExecutionStatus.ERROR,
                    stdout=stdout,
                    stderr=stderr,
                    execution_error="Docker did not return an exit status.",
                )
            return ExecutionResult(
                status=ExecutionStatus.COMPLETED,
                exit_code=0,
                execution_duration=time.perf_counter() - started_at,
                stdout=stdout,
                stderr=stderr,
            )
        return ExecutionResult(
            status=ExecutionStatus.FAILED,
            exit_code=completed.returncode,
            execution_duration=time.perf_counter() - started_at,
            stdout=stdout,
            stderr=stderr,
            execution_error=(
                f"Generated program exited with status {completed.returncode}."
            ),
        )

    def _build_command(
        self,
        workspace: Path,
        entrypoint: str,
        container_name: str,
    ) -> list[str]:
        """Build a shell-free Docker invocation with fixed container controls."""
        return [
            self._docker_executable,
            "run",
            "--rm",
            f"--name={container_name}",
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
            "PYTHONIOENCODING=utf-8",
            "-e",
            "PYTHONPATH=/workspace",
            "--mount",
            f"type=bind,source={workspace},target=/workspace,readonly",
            "--workdir=/workspace",
            self._image,
            "python",
            "-B",
            f"/workspace/{entrypoint}",
        ]

    @classmethod
    def _validate_file_paths(cls, files: Sequence[GeneratedFile]) -> str | None:
        """Validate workspace paths and cap staged project size."""
        if len(files) > _MAX_PROJECT_FILES:
            return f"Project contains more than {_MAX_PROJECT_FILES} files."
        seen_paths: set[str] = set()
        total_bytes = 0
        for generated_file in files:
            path = generated_file.file_path
            if not cls._is_safe_relative_path(path):
                return f"Unsafe project-relative file path: '{path}'."
            path_key = PurePosixPath(path).as_posix().casefold()
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
    def _is_safe_relative_path(path: str) -> bool:
        """Reject absolute paths, traversal, Windows paths, and control chars."""
        posix_path = PurePosixPath(path)
        windows_path = PureWindowsPath(path)
        return not (
            not path
            or "\\" in path
            or "\x00" in path
            or ":" in path
            or posix_path.is_absolute()
            or windows_path.is_absolute()
            or windows_path.drive
            or any(part in {"", ".", ".."} for part in path.split("/"))
            or any(ord(character) < 32 for character in path)
        )

    @staticmethod
    def _stage_files(workspace: Path, files: Sequence[GeneratedFile]) -> None:
        """Stage validated project files without following generated symlinks."""
        for generated_file in files:
            relative_path = PurePosixPath(generated_file.file_path)
            target = workspace.joinpath(*relative_path.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(generated_file.content, encoding="utf-8")


CodeExecutor = DockerCodeExecutor
