"""Tests for the Docker-isolated Python code executor."""

import io
import subprocess

import pytest

from app.models.schemas import GeneratedFile
from app.tools.code_executor import (
    DockerCodeExecutor,
    ExecutionStatus,
)


class FakeDockerProcess:
    """Provide deterministic stdout, stderr, and wait behavior for Docker."""

    def __init__(
        self,
        command: list[str],
        returncode: int = 0,
        stdout: bytes = b"",
        stderr: bytes = b"",
        timed_out: bool = False,
    ) -> None:
        self.command = command
        self.returncode: int | None = returncode
        self.stdout = io.BytesIO(stdout)
        self.stderr = io.BytesIO(stderr)
        self.timed_out = timed_out

    def wait(self, timeout: int) -> int:
        if self.timed_out:
            self.timed_out = False
            raise subprocess.TimeoutExpired(self.command, timeout)
        return self.returncode or 0

    def kill(self) -> None:
        self.returncode = -9


def project_files() -> list[GeneratedFile]:
    """Return a minimal project with an application entry point."""
    return [
        GeneratedFile(
            file_path="app/main.py",
            content="print('hello')\n",
            programming_language="Python",
        ),
        GeneratedFile(
            file_path="app/helper.py",
            content="VALUE = 1\n",
            programming_language="Python",
        ),
    ]


def test_executor_runs_entrypoint_in_restricted_docker_container(
    monkeypatch,
) -> None:
    observed_command: list[str] = []

    def fake_popen(
        command: list[str],
        *,
        stdin: int,
        stdout: int,
        stderr: int,
    ) -> FakeDockerProcess:
        observed_command.extend(command)
        assert stdin == subprocess.DEVNULL
        assert stdout == subprocess.PIPE
        assert stderr == subprocess.PIPE
        return FakeDockerProcess(
            command,
            stdout=b"hello\n",
        )

    monkeypatch.setattr("app.tools.docker_sandbox.subprocess.Popen", fake_popen)
    executor = DockerCodeExecutor(docker_executable="docker-test-double")

    result = executor.run(project_files(), "Python", entrypoint="app/main.py")

    assert result.status is ExecutionStatus.COMPLETED
    assert result.exit_code == 0
    assert result.stdout == "hello\n"
    assert observed_command[0:2] == ["docker-test-double", "run"]
    assert "--network=none" in observed_command
    assert "--read-only" in observed_command
    assert "--cap-drop=ALL" in observed_command
    assert "--security-opt=no-new-privileges" in observed_command
    assert "--user=65534:65534" in observed_command
    assert "--memory=512m" in observed_command
    assert "--cpus=1" in observed_command
    assert "--pids-limit=64" in observed_command
    assert "PYTHONPATH=/workspace" in observed_command
    assert "GROQ_API_KEY" not in observed_command
    assert "--env-file" not in observed_command
    mount = observed_command[observed_command.index("--mount") + 1]
    assert "target=/workspace,readonly" in mount
    assert observed_command[-2:] == ["-B", "/workspace/app/main.py"]


def test_executor_returns_separate_output_and_nonzero_exit(
    monkeypatch,
) -> None:
    def fake_popen(
        command: list[str],
        *,
        stdin: int,
        stdout: int,
        stderr: int,
    ) -> FakeDockerProcess:
        assert stdin == subprocess.DEVNULL
        assert stdout == subprocess.PIPE
        assert stderr == subprocess.PIPE
        return FakeDockerProcess(
            command,
            returncode=2,
            stdout=b"program output",
            stderr=b"traceback",
        )

    monkeypatch.setattr("app.tools.docker_sandbox.subprocess.Popen", fake_popen)

    result = DockerCodeExecutor().run(project_files(), "python", "app/main.py")

    assert result.status is ExecutionStatus.FAILED
    assert result.exit_code == 2
    assert result.stdout == "program output"
    assert result.stderr == "traceback"
    assert result.execution_error is not None


def test_executor_reports_timeout_and_stops_container(monkeypatch) -> None:
    observed_commands: list[list[str]] = []

    def fake_popen(
        command: list[str],
        *,
        stdin: int,
        stdout: int,
        stderr: int,
    ) -> FakeDockerProcess:
        assert stdin == subprocess.DEVNULL
        assert stdout == subprocess.PIPE
        assert stderr == subprocess.PIPE
        observed_commands.append(command)
        return FakeDockerProcess(
            command,
            stdout=b"partial output",
            stderr=b"partial error",
            timed_out=True,
        )

    monkeypatch.setattr("app.tools.docker_sandbox.subprocess.Popen", fake_popen)
    stop_commands: list[list[str]] = []

    def fake_stop(
        command: list[str],
        *,
        capture_output: bool,
        text: bool,
        timeout: int,
        check: bool,
    ) -> subprocess.CompletedProcess[str]:
        assert capture_output is True
        assert text is True
        assert timeout == 5
        assert check is False
        stop_commands.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr("app.tools.docker_sandbox.subprocess.run", fake_stop)
    executor = DockerCodeExecutor()

    result = executor.run(
        project_files(),
        "Python",
        "app/main.py",
        timeout_seconds=1,
    )

    assert result.status is ExecutionStatus.TIMEOUT
    assert result.stdout == "partial output"
    assert result.stderr == "partial error"
    assert observed_commands[0][1] == "run"
    assert stop_commands[0][1] == "stop"
    assert stop_commands[0][-1].startswith("devforge-run-")


def test_executor_rejects_unsupported_language_and_invalid_entrypoints(
    monkeypatch,
) -> None:
    def unexpected_popen(*args: object, **kwargs: object) -> None:
        raise AssertionError(f"Unexpected Docker invocation: {args!r} {kwargs!r}")

    monkeypatch.setattr("app.tools.docker_sandbox.subprocess.Popen", unexpected_popen)
    executor = DockerCodeExecutor()

    unsupported = executor.run(project_files(), "Rust")
    missing_entry = executor.run(project_files(), "Python", "main.py")
    unsafe_entry = executor.run(project_files(), "Python", "../outside.py")

    assert unsupported.status is ExecutionStatus.UNSUPPORTED_LANGUAGE
    assert missing_entry.status is ExecutionStatus.ERROR
    assert "not supplied" in (missing_entry.execution_error or "")
    assert unsafe_entry.status is ExecutionStatus.ERROR
    assert "Unsafe" in (unsafe_entry.execution_error or "")


def test_executor_rejects_unsafe_file_paths_before_staging() -> None:
    files = project_files()
    files[0] = files[0].model_copy(update={"file_path": "../outside.py"})

    result = DockerCodeExecutor().run(files, "Python", "../outside.py")

    assert result.status is ExecutionStatus.ERROR
    assert "Unsafe" in (result.execution_error or "")


def test_executor_rejects_secret_files_before_staging() -> None:
    files = [
        GeneratedFile(
            file_path=".env",
            content="GROQ_API_KEY=not-staged",
            programming_language="Python",
        ),
        GeneratedFile(
            file_path="main.py",
            content="print('not run')",
            programming_language="Python",
        ),
    ]

    result = DockerCodeExecutor().run(files, "Python", "main.py")

    assert result.status is ExecutionStatus.ERROR
    assert "Sensitive project file" in (result.execution_error or "")


def test_executor_reports_missing_docker(monkeypatch) -> None:
    def missing_docker(*args: object, **kwargs: object) -> None:
        raise FileNotFoundError(f"docker not found: {args!r} {kwargs!r}")

    monkeypatch.setattr("app.tools.docker_sandbox.subprocess.Popen", missing_docker)

    result = DockerCodeExecutor().run(project_files(), "Python", "app/main.py")

    assert result.status is ExecutionStatus.SANDBOX_UNAVAILABLE
    assert "Docker is unavailable" in (result.execution_error or "")


def test_executor_limits_timeout_and_staged_project_size() -> None:
    executor = DockerCodeExecutor()
    files = project_files()

    oversized_file = files[0].model_copy(update={"content": "x" * 2_000_001})
    result = executor.run([oversized_file], "Python", "app/main.py")

    assert result.status is ExecutionStatus.ERROR
    assert "2 MB staging limit" in (result.execution_error or "")

    with pytest.raises(ValueError, match="between 1 and 300"):
        executor.run(files, "Python", "app/main.py", timeout_seconds=301)
