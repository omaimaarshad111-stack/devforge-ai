"""Tests for independent code review and approval criteria."""

import json

from app.agents.base_agent import AgentInput
from app.agents.reviewer import CodeReviewerAgent as _CodeReviewerAgent
from app.models.schemas import (
    ArchitectureDesign,
    GeneratedFile,
    ReviewIssue,
    ReviewSeverity,
)
from app.models.schemas import (
    TestResult as _TestResult,
)


class MockLLM:
    """Return a configured structured review assessment."""

    def __init__(self, assessment: dict[str, object]) -> None:
        self.assessment = assessment
        self.prompts: list[str] = []

    def invoke(self, input: str) -> object:
        self.prompts.append(input)
        return {"content": json.dumps(self.assessment)}


class MockAnalyzer:
    """Return configured static findings and record inspected files."""

    def __init__(self, findings: list[ReviewIssue] | None = None) -> None:
        self.findings = findings or []
        self.inspected: list[GeneratedFile] = []

    def analyze(self, files: list[GeneratedFile]) -> list[ReviewIssue]:
        self.inspected = list(files)
        return self.findings


def high_quality_assessment() -> dict[str, object]:
    """Return a strong assessment on every weighted review dimension."""
    return {
        "functional_correctness": 95,
        "readability": 95,
        "maintainability": 95,
        "oop_principles": 95,
        "error_handling": 95,
        "security": 95,
        "architecture_consistency": 95,
        "critical_issues": [],
        "major_issues": [],
        "minor_suggestions": [],
        "identified_issues": [],
        "suggestions": [],
    }


def architecture() -> ArchitectureDesign:
    """Build an approved architecture fixture."""
    return ArchitectureDesign(
        project_structure=["app/", "tests/"],
        technology_stack=["Python", "FastAPI"],
        application_architecture=[
            {
                "name": "Health route",
                "responsibility": "Expose service health over HTTP.",
                "dependencies": [],
            }
        ],
        design_decisions=["Keep API routes in the app package."],
    )


def source_file(content: str = "def health():\n    return 'ok'\n") -> GeneratedFile:
    """Create one Python source fixture."""
    return GeneratedFile(
        file_path="app/health.py",
        content=content,
        programming_language="Python",
    )


def passing_tests() -> _TestResult:
    """Create a completed successful test run."""
    return _TestResult(total_tests=2, passed_tests=2)


def reviewer(
    assessment: dict[str, object] | None = None,
    findings: list[ReviewIssue] | None = None,
) -> tuple[_CodeReviewerAgent, MockAnalyzer]:
    """Create a reviewer with mocked LLM and static analysis."""
    analyzer = MockAnalyzer(findings)
    return _CodeReviewerAgent(
        MockLLM(assessment or high_quality_assessment()), analyzer
    ), analyzer


def issue(
    severity: ReviewSeverity,
    title: str,
    *,
    confirmed: bool = True,
    file_path: str = "app/health.py",
    line_number: int = 1,
) -> ReviewIssue:
    """Create a review finding with realistic evidence fields."""
    return ReviewIssue(
        severity=severity,
        title=title,
        description=f"Evidence for {title}.",
        confirmed=confirmed,
        confidence=0.95,
        file_path=file_path,
        line_number=line_number,
        recommendation=f"Address {title}.",
    )


def test_high_quality_code_can_be_approved_after_successful_tests() -> None:
    agent, analyzer = reviewer()

    result = agent.review(
        [source_file()],
        ["GET /health returns a healthy status."],
        architecture(),
        passing_tests(),
    )

    assert result.approved
    assert result.quality_score == 95
    assert "Approved:" in result.approval_rationale
    assert analyzer.inspected == [source_file()]


def test_security_finding_is_preserved_and_prevents_approval() -> None:
    security_finding = issue(
        ReviewSeverity.MAJOR,
        "Subprocess invoked with shell=True",
        confirmed=False,
        line_number=12,
    )
    agent, _ = reviewer(findings=[security_finding])

    result = agent.review(
        [source_file()],
        ["Run a health endpoint."],
        architecture(),
        passing_tests(),
    )

    assert not result.approved
    assert result.major_issues == [security_finding]
    assert result.quality_score == 87
    assert "major finding" in result.approval_rationale


def test_missing_functionality_finding_blocks_approval() -> None:
    assessment = high_quality_assessment()
    assessment["major_issues"] = [
        issue(ReviewSeverity.MAJOR, "Required endpoint is missing").model_dump(
            mode="json"
        )
    ]
    agent, _ = reviewer(assessment)

    result = agent.review(
        [source_file()],
        ["Expose GET /health."],
        architecture(),
        passing_tests(),
    )

    assert not result.approved
    assert result.major_issues[0].title == "Required endpoint is missing"
    assert "Required endpoint is missing" in result.identified_issues[0]


def test_passing_tests_do_not_approve_poorly_maintainable_code() -> None:
    assessment = high_quality_assessment()
    assessment["maintainability"] = 10
    assessment["minor_suggestions"] = [
        issue(
            ReviewSeverity.MINOR,
            "Responsibilities are concentrated in one function",
            confirmed=True,
        ).model_dump(mode="json")
    ]
    agent, _ = reviewer(assessment)

    result = agent.review(
        [source_file()],
        ["Return health status."],
        architecture(),
        passing_tests(),
    )

    assert not result.approved
    assert result.quality_score < 80
    assert result.minor_suggestions
    assert "below 80" in result.approval_rationale


def test_static_analyzer_finds_syntax_and_dynamic_execution_issues() -> None:
    agent = _CodeReviewerAgent(MockLLM(high_quality_assessment()))

    result = agent.review(
        [source_file("def run(value):\n    return eval(value)\n")],
        ["Parse a value."],
        architecture(),
        passing_tests(),
    )

    assert not result.approved
    assert any(
        "Dynamic code execution" in finding.title for finding in result.major_issues
    )


def test_no_test_result_prevents_approval_even_for_high_score() -> None:
    agent, _ = reviewer()

    result = agent.review([source_file()], [], architecture(), None)

    assert result.quality_score > 80
    assert not result.approved
    assert "no test result" in result.approval_rationale


def test_review_does_not_change_source_files() -> None:
    source = source_file()
    original_content = source.content
    agent, _ = reviewer()

    agent.review([source], [], architecture(), passing_tests())

    assert source.content == original_content


def test_execute_accepts_serialized_review_inputs() -> None:
    agent, _ = reviewer()

    response = agent.execute(
        AgentInput(
            task="Review this implementation",
            context={
                "architecture_design": architecture().model_dump(mode="json"),
                "generated_files": [source_file().model_dump(mode="json")],
                "requirements": ["Return health status."],
                "test_result": passing_tests().model_dump(mode="json"),
            },
        )
    )

    review = json.loads(response.content)
    assert review["approved"] is True
    assert review["quality_score"] == 95
