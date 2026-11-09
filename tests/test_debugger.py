"""Tests for targeted debugging corrections and retry limits."""

import json

import pytest

from app.agents.debugger import (
    DebuggerAgent as _DebuggerAgent,
)
from app.agents.debugger import (
    DebuggingError,
    MaxDebuggingAttemptsExceeded,
)
from app.models.schemas import (
    ArchitectureDesign,
    DebuggingResponse,
    GeneratedFile,
    ReviewResult,
)
from app.models.schemas import (
    TestResult as _TestResult,
)


class MockLLM:
    """Return one configured debugging response."""

    def __init__(self, response: str) -> None:
        self.response = response
        self.prompts: list[str] = []

    def invoke(self, input: str) -> object:
        self.prompts.append(input)
        return {"content": self.response}


def original_file(content: str = "def add(a, b):\n    return a + b\n") -> GeneratedFile:
    """Create the original generated source file."""
    return GeneratedFile(
        file_path="app/calculator.py",
        content=content,
        programming_language="Python",
    )


def correction(
    *,
    content: str = "def add(a, b):\n    return a + b\n",
    file_path: str = "app/calculator.py",
    summary: str = "Correct addition logic",
) -> dict[str, object]:
    """Create a structured correction item."""
    return {
        "file": {
            "file_path": file_path,
            "content": content,
            "programming_language": "Python",
        },
        "change_summary": summary,
        "rationale": "The failure showed that the previous implementation was incorrect.",
    }


def valid_response(*corrections: dict[str, object]) -> str:
    """Serialize a root cause and one or more correction entries."""
    return json.dumps(
        {
            "root_cause": "The implementation did not match the failing assertion.",
            "corrections": list(corrections),
        }
    )


def architecture() -> ArchitectureDesign:
    """Create an architecture fixture for a small Python module."""
    return ArchitectureDesign(
        project_structure=["app/calculator.py", "tests/test_calculator.py"],
        technology_stack=["Python"],
        design_decisions=["Keep arithmetic operations in a small module."],
    )


def failed_result(
    error: str = "AssertionError: expected 5, got 4",
    failed_tests: int = 1,
) -> _TestResult:
    """Build test output that can be used to diagnose a failing run."""
    return _TestResult(
        total_tests=failed_tests,
        failed_tests=failed_tests,
        error_details=[error],
    )


def agent_for(
    response: str,
    max_attempts: int = 3,
) -> tuple[_DebuggerAgent, MockLLM]:
    """Create a debugger with a mocked LLM response."""
    llm = MockLLM(response)
    return _DebuggerAgent(llm=llm, max_attempts=max_attempts), llm


def test_corrects_syntax_errors_with_replacement_file() -> None:
    agent, _ = agent_for(
        valid_response(
            correction(
                summary="Fix the malformed return statement",
            )
        )
    )

    result = agent.propose_corrections(
        [original_file("def add(a, b)\n    return a + b\n")],
        failed_result("SyntaxError: expected ':'"),
        architecture(),
    )

    assert isinstance(result, DebuggingResponse)
    assert result.corrections[0].file.content == "def add(a, b):\n    return a + b\n"
    assert "malformed return" in result.corrections[0].change_summary


def test_corrects_failed_test_and_prompt_requires_testing_again() -> None:
    agent, llm = agent_for(
        valid_response(correction(content="def add(a, b):\n    return a + b\n"))
    )

    result = agent.propose_corrections(
        [original_file("def add(a, b):\n    return a - b\n")],
        failed_result(),
        architecture(),
    )

    assert result.corrections[0].file.content == "def add(a, b):\n    return a + b\n"
    assert "Testing Agent will verify" in llm.prompts[0]
    assert "do not" in llm.prompts[0].casefold()
    assert result.retesting_required is True


def test_handles_multiple_errors_with_minimal_file_updates() -> None:
    agent, _ = agent_for(
        valid_response(
            correction(content="def add(a, b):\n    return a + b\n"),
            {
                "file": {
                    "file_path": "app/formatting.py",
                    "content": "def format_value(value):\n    return str(value)\n",
                    "programming_language": "Python",
                },
                "change_summary": "Handle non-string values in formatting",
                "rationale": "The second failure showed integer inputs were unsupported.",
            },
        )
    )
    files = [
        original_file("def add(a, b):\n    return a - b\n"),
        GeneratedFile(
            file_path="app/formatting.py",
            content="def format_value(value):\n    return value.upper()\n",
            programming_language="Python",
        ),
    ]

    result = agent.propose_corrections(
        files,
        failed_result("Two tests failed", failed_tests=2),
        architecture(),
        ReviewResult(quality_score=60, approved=False),
    )

    assert len(result.corrections) == 2
    assert {item.file.file_path for item in result.corrections} == {
        "app/calculator.py",
        "app/formatting.py",
    }
    assert files[0].content == "def add(a, b):\n    return a - b\n"


def test_invalid_or_empty_correction_is_rejected() -> None:
    agent, _ = agent_for('{"root_cause": "bad", "corrections": []}')

    with pytest.raises(DebuggingError, match="malformed or incomplete correction"):
        agent.propose_corrections([original_file()], failed_result(), architecture())


def test_unsuccessful_correction_response_does_not_claim_a_fix() -> None:
    agent, _ = agent_for(
        valid_response(correction(content="def add(a, b):\n    return a - b\n"))
    )

    with pytest.raises(DebuggingError, match="does not change"):
        agent.propose_corrections(
            [original_file("def add(a, b):\n    return a - b\n")],
            failed_result(),
            architecture(),
        )

    assert agent.attempts_used == 1


def test_rejects_correction_for_a_file_not_in_original_set() -> None:
    agent, _ = agent_for(
        valid_response(correction(file_path="../outside.py", content="changed"))
    )

    with pytest.raises(DebuggingError, match="project-relative"):
        agent.propose_corrections([original_file()], failed_result(), architecture())


def test_rejects_correction_that_adds_unrelated_new_file() -> None:
    agent, _ = agent_for(
        valid_response(correction(file_path="app/new_module.py", content="new = True"))
    )

    with pytest.raises(DebuggingError, match="does not match an existing"):
        agent.propose_corrections([original_file()], failed_result(), architecture())


def test_rejects_correction_that_does_not_change_content() -> None:
    agent, _ = agent_for(valid_response(correction()))

    with pytest.raises(DebuggingError, match="does not change"):
        agent.propose_corrections([original_file()], failed_result(), architecture())


def test_rejects_corrections_without_test_failures_or_execution_errors() -> None:
    agent, _ = agent_for(valid_response(correction(content="updated")))

    with pytest.raises(DebuggingError, match="requires failed tests"):
        agent.propose_corrections(
            [original_file()],
            _TestResult(total_tests=1, passed_tests=1),
            architecture(),
        )


def test_maximum_retry_limit_prevents_infinite_debugging_loop() -> None:
    agent, _ = agent_for(
        valid_response(correction(content="def add(a, b):\n    return a + b\n")),
        max_attempts=2,
    )
    original = original_file("def add(a, b):\n    return a - b\n")

    for _ in range(2):
        agent.propose_corrections([original], failed_result(), architecture())

    assert agent.attempts_used == 2
    with pytest.raises(
        MaxDebuggingAttemptsExceeded, match="Maximum debugging attempts"
    ):
        agent.propose_corrections([original], failed_result(), architecture())


def test_review_feedback_is_included_in_llm_input() -> None:
    agent, llm = agent_for(valid_response(correction(content="updated")))
    review = ReviewResult(
        quality_score=50,
        approved=False,
        identified_issues=["Incorrect addition"],
    )

    agent.propose_corrections(
        [original_file()],
        failed_result(),
        architecture(),
        review,
    )

    assert "Incorrect addition" in llm.prompts[0]
    assert "attempt_number" in llm.prompts[0]
