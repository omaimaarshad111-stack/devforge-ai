"""Tests for the Testing Agent using mocked LLM and runner responses."""

import json

from app.agents.base_agent import AgentInput
from app.agents.tester import TesterAgent as _TesterAgent
from app.models.schemas import GeneratedFile
from app.tools.test_runner import (
    TestExecutionResult as _TestExecutionResult,
)
from app.tools.test_runner import (
    TestExecutionStatus as _TestExecutionStatus,
)


class MockLLM:
    """Return generated pytest files without making an external model call."""

    def __init__(self, response: str = '{"files": []}') -> None:
        self.response = response
        self.prompts: list[str] = []

    def invoke(self, input: str) -> object:
        self.prompts.append(input)
        return {"content": self.response}


class MockTestRunner:
    """Record the files passed to the runner and return a configured result."""

    def __init__(self, result: _TestExecutionResult) -> None:
        self.result = result
        self.calls: list[tuple[list[GeneratedFile], str, int]] = []

    def run(
        self,
        files: list[GeneratedFile],
        programming_language: str,
        timeout_seconds: int = 30,
    ) -> _TestExecutionResult:
        self.calls.append((list(files), programming_language, timeout_seconds))
        return self.result


def source_file() -> GeneratedFile:
    """Build a generated Python source fixture."""
    return GeneratedFile(
        file_path="app/calculator.py",
        content="def add(left, right):\n    return left + right\n",
        programming_language="Python",
    )


def generated_test_file() -> GeneratedFile:
    """Build a generated pytest fixture."""
    return GeneratedFile(
        file_path="tests/test_calculator.py",
        content="from app.calculator import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n",
        programming_language="Python",
    )


def test_all_tests_passing_returns_structured_result() -> None:
    runner = MockTestRunner(
        _TestExecutionResult(
            status=_TestExecutionStatus.PASSED,
            total_tests=3,
            passed_tests=3,
            output="3 passed",
        )
    )
    agent = _TesterAgent(MockLLM(), runner)

    result = agent.test_generated_files(
        [source_file(), generated_test_file()], "Python"
    )

    assert result.total_tests == 3
    assert result.passed_tests == 3
    assert result.failed_tests == 0
    assert result.execution_status == "passed"
    assert result.output == "3 passed"
    assert result.execution_environment == "MockTestRunner"
    assert len(runner.calls) == 1
    assert [file.file_path for file in runner.calls[0][0]] == [
        "app/calculator.py",
        "tests/test_calculator.py",
    ]


def test_some_tests_failing_include_debugging_feedback() -> None:
    runner = MockTestRunner(
        _TestExecutionResult(
            status=_TestExecutionStatus.FAILED,
            total_tests=3,
            passed_tests=2,
            failed_tests=1,
            output="FAILED tests/test_calculator.py::test_subtract",
        )
    )
    agent = _TesterAgent(MockLLM(), runner)

    result = agent.test_generated_files(
        [source_file(), generated_test_file()], "Python"
    )

    assert result.failed_tests == 1
    assert "FAILED tests/test_calculator.py" in result.error_details[0]
    assert any("failing test" in item for item in result.actionable_feedback)
    assert result.execution_error is None


def test_syntax_error_is_reported_as_execution_error_not_test_failure() -> None:
    runner = MockTestRunner(
        _TestExecutionResult(
            status=_TestExecutionStatus.ERROR,
            output="SyntaxError: invalid syntax",
            execution_error="Test collection failed due to a syntax error.",
        )
    )
    agent = _TesterAgent(MockLLM(), runner)

    result = agent.test_generated_files(
        [source_file(), generated_test_file()], "Python"
    )

    assert result.total_tests == 0
    assert result.failed_tests == 0
    assert result.execution_error == "Test collection failed due to a syntax error."
    assert result.error_details == ["SyntaxError: invalid syntax"]


def test_execution_timeout_is_not_reported_as_a_test_failure() -> None:
    runner = MockTestRunner(
        _TestExecutionResult(
            status=_TestExecutionStatus.TIMEOUT,
            execution_error="Test execution exceeded the 30-second container timeout.",
        )
    )
    agent = _TesterAgent(MockLLM(), runner)

    result = agent.test_generated_files(
        [source_file(), generated_test_file()], "Python"
    )

    assert result.failed_tests == 0
    assert result.execution_error is not None
    assert any("slow or blocked" in item for item in result.actionable_feedback)


def test_missing_test_files_are_generated_then_sent_to_isolated_runner() -> None:
    generated_tests = {
        "files": [
            {
                "file_path": "tests/test_calculator.py",
                "content": "from app.calculator import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n",
                "programming_language": "Python",
            }
        ]
    }
    llm = MockLLM(json.dumps(generated_tests))
    runner = MockTestRunner(
        _TestExecutionResult(
            status=_TestExecutionStatus.PASSED,
            total_tests=1,
            passed_tests=1,
        )
    )
    agent = _TesterAgent(llm, runner)
    source = source_file()

    result = agent.test_generated_files([source], "Python")

    assert result.passed_tests == 1
    assert [file.file_path for file in runner.calls[0][0]] == [
        "app/calculator.py",
        "tests/test_calculator.py",
    ]
    assert source.content == "def add(left, right):\n    return left + right\n"


def test_missing_test_files_with_empty_generated_tests_returns_actionable_error() -> (
    None
):
    runner = MockTestRunner(
        _TestExecutionResult(status=_TestExecutionStatus.MISSING_TESTS)
    )
    agent = _TesterAgent(MockLLM('{"files": []}'), runner)

    result = agent.test_generated_files([source_file()], "Python")

    assert result.execution_error is not None
    assert "no tests" in result.execution_error
    assert not runner.calls


def test_unsupported_language_does_not_call_llm_or_runner() -> None:
    llm = MockLLM()
    runner = MockTestRunner(
        _TestExecutionResult(status=_TestExecutionStatus.UNSUPPORTED_LANGUAGE)
    )
    agent = _TesterAgent(llm, runner)

    result = agent.test_generated_files(
        [
            GeneratedFile(
                file_path="src/main.rs",
                content="fn main() {}",
                programming_language="Rust",
            )
        ],
        "Rust",
    )

    assert "No isolated testing framework is configured for 'Rust'" in (
        result.execution_error or ""
    )
    assert not llm.prompts
    assert not runner.calls


def test_generated_tests_must_stay_inside_tests_directory() -> None:
    invalid_tests = {
        "files": [
            {
                "file_path": "../app/calculator.py",
                "content": "def test_something(): pass",
                "programming_language": "Python",
            }
        ]
    }
    agent = _TesterAgent(
        MockLLM(json.dumps(invalid_tests)),
        MockTestRunner(_TestExecutionResult(status=_TestExecutionStatus.PASSED)),
    )

    result = agent.test_generated_files([source_file()], "Python")

    assert "must be a relative tests/test_*.py path" in (result.execution_error or "")


def test_execute_accepts_serialized_generated_files() -> None:
    runner = MockTestRunner(
        _TestExecutionResult(
            status=_TestExecutionStatus.PASSED,
            total_tests=1,
            passed_tests=1,
        )
    )
    agent = _TesterAgent(MockLLM(), runner)
    request = AgentInput(
        task="Test the generated project",
        context={
            "generated_files": [
                source_file().model_dump(mode="json"),
                generated_test_file().model_dump(mode="json"),
            ],
            "programming_language": "Python",
        },
    )

    result = json.loads(agent.execute(request).content)

    assert result["total_tests"] == 1
    assert result["passed_tests"] == 1
