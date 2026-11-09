"""Unit tests for sandbox setup and pytest result classification."""

import io
import subprocess
from pathlib import Path

from app.models.schemas import GeneratedFile
from app.tools.test_runner import (
    DockerTestRunner,
)
from app.tools.test_runner import (
    TestExecutionStatus as _TestExecutionStatus,
)


class FakeDockerProcess:
    """Provide deterministic output and wait results for Docker CLI calls."""

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
    """Create one source file and one pytest module."""
    return [
        GeneratedFile(
            file_path="app/main.py",
            content="value = 1",
            programming_language="Python",
        ),
        GeneratedFile(
            file_path="tests/test_main.py",
            content="def test_value():\n    assert True\n",
            programming_language="Python",
        ),
    ]


def test_runner_rejects_unsupported_language_before_invoking_docker() -> None:
    result = DockerTestRunner().run(project_files(), "Rust")

    assert result.status is _TestExecutionStatus.UNSUPPORTED_LANGUAGE
    assert result.execution_error is not None


def test_runner_reports_missing_tests_without_invoking_docker() -> None:
    files = project_files()[:1]

    result = DockerTestRunner().run(files, "Python")

    assert result.status is _TestExecutionStatus.MISSING_TESTS


def test_runner_rejects_paths_that_could_escape_workspace() -> None:
    files = project_files()
    files[0] = files[0].model_copy(update={"file_path": "../outside.py"})

    result = DockerTestRunner().run(files, "Python")

    assert result.status is _TestExecutionStatus.ERROR
    assert "Unsafe" in (result.execution_error or "")


def test_runner_rejects_secret_files_before_staging() -> None:
    files = project_files()
    files.append(
        GeneratedFile(
            file_path=".env",
            content="GROQ_API_KEY=not-staged",
            programming_language="Python",
        )
    )

    result = DockerTestRunner().run(files, "Python")

    assert result.status is _TestExecutionStatus.ERROR
    assert "Sensitive project file" in (result.execution_error or "")


def test_docker_command_disables_network_and_limits_resources(tmp_path: Path) -> None:
    command = DockerTestRunner()._build_command(tmp_path, timeout_seconds=12)

    assert "--network=none" in command
    assert "--read-only" in command
    assert "--cap-drop=ALL" in command
    assert "--memory=512m" in command
    assert "--cpus=1" in command
    assert "--pids-limit=64" in command
    assert "--timeout" in command
    assert "12" in command
    assert "--network=none" in command
    assert "--user=65534:65534" in command
    assert "PYTEST_DISABLE_PLUGIN_AUTOLOAD=1" in command
    assert "--env-file" not in command


def test_runner_executes_only_docker_and_returns_pytest_summary(
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
        assert stdin == subprocess.DEVNULL
        assert stdout == subprocess.PIPE
        assert stderr == subprocess.PIPE
        observed_command.extend(command)
        return FakeDockerProcess(
            command,
            returncode=1,
            stdout=b"2 passed, 1 failed in 0.02s",
        )

    monkeypatch.setattr("app.tools.docker_sandbox.subprocess.Popen", fake_popen)
    runner = DockerTestRunner(docker_executable="docker-test-double")

    result = runner.run(project_files(), "Python", timeout_seconds=30)

    assert observed_command[0] == "docker-test-double"
    assert result.status is _TestExecutionStatus.FAILED
    assert result.total_tests == 3
    assert result.passed_tests == 2
    assert result.failed_tests == 1
    assert result.stdout == "2 passed, 1 failed in 0.02s"
    assert result.stderr == ""


def test_syntax_errors_are_classified_as_execution_errors() -> None:
    result = DockerTestRunner._classify_exit(
        2,
        "ERROR collecting tests/test_main.py\nSyntaxError: invalid syntax",
    )

    assert result.status is _TestExecutionStatus.ERROR
    assert result.execution_error is not None
    assert "syntax" in result.execution_error


def test_subprocess_timeout_is_reported_as_timeout(monkeypatch) -> None:
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
        return FakeDockerProcess(command, timed_out=True)

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

    result = DockerTestRunner().run(project_files(), "Python", timeout_seconds=30)

    assert result.status is _TestExecutionStatus.TIMEOUT
    assert result.execution_error is not None
    assert stop_commands[0][1] == "stop"


def test_runner_bounds_captured_output(monkeypatch) -> None:
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
        return FakeDockerProcess(command, stdout=b"x" * 50_000)

    monkeypatch.setattr("app.tools.docker_sandbox.subprocess.Popen", fake_popen)

    result = DockerTestRunner().run(project_files(), "Python")

    assert len(result.stdout) < 12_100
    assert result.stdout.startswith("[output truncated]")


def test_per_test_timeout_is_reported_as_timeout() -> None:
    result = DockerTestRunner._classify_exit(
        1,
        "1 failed\nFailed: Timeout (>2.0s)",
    )

    assert result.status is _TestExecutionStatus.TIMEOUT
    assert result.failed_tests == 1
    assert "per-test timeout" in (result.execution_error or "")


def test_collection_result_keeps_stdout_and_stderr_separate() -> None:
    result = DockerTestRunner._classify_exit(
        2,
        "ERROR collecting test_main.py\nSyntaxError: invalid syntax",
        stdout="ERROR collecting test_main.py",
        stderr="SyntaxError: invalid syntax",
    )

    assert result.stdout == "ERROR collecting test_main.py"
    assert result.stderr == "SyntaxError: invalid syntax"
    assert result.output == "ERROR collecting test_main.py\nSyntaxError: invalid syntax"
