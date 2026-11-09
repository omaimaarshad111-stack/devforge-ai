"""Opt-in integration tests that execute generated Python inside real Docker."""

import os
import subprocess
import tempfile
from pathlib import Path

import pytest

from app.models.schemas import GeneratedFile
from app.tools.code_executor import DockerCodeExecutor, ExecutionStatus
from app.tools.test_runner import DockerTestRunner, TestExecutionStatus

pytestmark = pytest.mark.skipif(
    os.getenv("DEVFORGE_RUN_DOCKER_INTEGRATION") != "1",
    reason="Set DEVFORGE_RUN_DOCKER_INTEGRATION=1 to run real Docker tests.",
)


def _docker_container_ids(name_prefix: str) -> set[str]:
    """Get matching real Docker containers to verify disposable cleanup."""
    completed = subprocess.run(
        [
            "docker",
            "ps",
            "--all",
            "--quiet",
            "--filter",
            f"name={name_prefix}",
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    return {line for line in completed.stdout.splitlines() if line}


def _temporary_workspaces(prefix: str) -> set[str]:
    """Snapshot the staging directories created by the execution adapters."""
    temp_root = Path(tempfile.gettempdir())
    return {str(path) for path in temp_root.glob(f"{prefix}*")}


def test_code_executor_runs_python_in_disposable_docker_container() -> None:
    """Execute a harmless calculation and verify Docker/staging cleanup."""
    containers_before = _docker_container_ids("devforge-run-")
    workspaces_before = _temporary_workspaces("devforge-run-")
    generated_file = GeneratedFile(
        file_path="main.py",
        programming_language="Python",
        content=(
            "print('DevForge AI Docker execution successful!')\n"
            "result = 19 * 23\n"
            "print(f'19 * 23 = {result}')\n"
        ),
    )

    result = DockerCodeExecutor().run(
        [generated_file],
        programming_language="Python",
        entrypoint="main.py",
    )

    assert result.status is ExecutionStatus.COMPLETED, result.execution_error
    assert result.exit_code == 0
    assert "DevForge AI Docker execution successful!" in result.stdout
    assert "19 * 23 = 437" in result.stdout
    assert _docker_container_ids("devforge-run-") == containers_before
    assert _temporary_workspaces("devforge-run-") == workspaces_before


def test_test_runner_runs_pytest_in_disposable_docker_container() -> None:
    """Run harmless pytest coverage and verify Docker/staging cleanup."""
    containers_before = _docker_container_ids("devforge-test-")
    workspaces_before = _temporary_workspaces("devforge-test-")
    test_file = GeneratedFile(
        file_path="tests/test_sample.py",
        programming_language="Python",
        content=("def test_simple_calculation():\n    assert 19 * 23 == 437\n"),
    )

    result = DockerTestRunner().run(
        [test_file],
        programming_language="Python",
    )

    assert result.status is TestExecutionStatus.PASSED, result.execution_error
    assert result.total_tests == 1
    assert result.passed_tests == 1
    assert result.failed_tests == 0
    assert "1 passed" in result.output
    assert _docker_container_ids("devforge-test-") == containers_before
    assert _temporary_workspaces("devforge-test-") == workspaces_before
