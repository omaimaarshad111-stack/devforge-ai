"""Independent code review agent combining LLM assessment and static analysis."""

import json
from collections.abc import Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field, ValidationError

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
    GeneratedFile,
    ReviewIssue,
    ReviewResult,
    ReviewSeverity,
    TestResult,
)
from app.tools.code_analyzer import (
    CodeAnalysisResult,
    CodeAnalyzer,
)

_REVIEW_SYSTEM_PROMPT = """\
You are DevForge AI's independent code reviewer. Review the submitted generated
files against the requirements, approved architecture, and test results. Never
assume tests passing proves correctness or safety. Do not change files.

Return only a JSON object with:
- functional_correctness (0-100)
- readability (0-100)
- maintainability (0-100)
- oop_principles (0-100)
- error_handling (0-100)
- security (0-100)
- architecture_consistency (0-100)
- critical_issues: list of ReviewIssue objects
- major_issues: list of ReviewIssue objects
- minor_suggestions: list of ReviewIssue objects
- identified_issues: list of concise issue strings
- suggestions: list of actionable suggestion strings

Each ReviewIssue must include severity (critical/major/minor/info), title,
description, confirmed (true only when directly evidenced by the submitted
files or test output), confidence (0-1), optional file_path and line_number,
and recommendation. Distinguish observed facts from risks that require runtime
or environmental confirmation. Do not invent requirements or report a
potential concern as a confirmed vulnerability.
"""

APPROVAL_SCORE_THRESHOLD = 80.0
"""Minimum weighted rubric score for approval."""

REVIEW_SCORE_WEIGHTS = {
    "functional_correctness": 30,
    "readability": 15,
    "maintainability": 15,
    "oop_principles": 10,
    "error_handling": 10,
    "security": 15,
    "architecture_consistency": 5,
}
"""Scoring weights; the seven categories sum to 100 points."""

_ISSUE_PENALTIES = {
    (ReviewSeverity.CRITICAL, True): 40,
    (ReviewSeverity.CRITICAL, False): 20,
    (ReviewSeverity.MAJOR, True): 15,
    (ReviewSeverity.MAJOR, False): 8,
    (ReviewSeverity.MINOR, True): 3,
    (ReviewSeverity.MINOR, False): 2,
    (ReviewSeverity.INFO, True): 0,
    (ReviewSeverity.INFO, False): 0,
}


class CodeReviewError(AgentExecutionError):
    """Raised when the reviewer cannot validate an input or LLM assessment."""


class ReviewAssessment(BaseModel):
    """Validated LLM assessment before deterministic scoring and approval."""

    model_config = ConfigDict(extra="forbid")

    functional_correctness: float = Field(ge=0, le=100)
    readability: float = Field(ge=0, le=100)
    maintainability: float = Field(ge=0, le=100)
    oop_principles: float = Field(ge=0, le=100)
    error_handling: float = Field(ge=0, le=100)
    security: float = Field(ge=0, le=100)
    architecture_consistency: float = Field(ge=0, le=100)
    critical_issues: list[ReviewIssue] = Field(default_factory=list)
    major_issues: list[ReviewIssue] = Field(default_factory=list)
    minor_suggestions: list[ReviewIssue] = Field(default_factory=list)
    identified_issues: list[str] = Field(default_factory=list)
    suggestions: list[str] = Field(default_factory=list)


class CodeReviewerAgent(BaseAgent):
    """Review code without modifying it or trusting tests as proof of quality.

    The weighted rubric allocates points to functional correctness (30),
    readability (15), maintainability (15), OOP principles (10), error handling
    (10), security (15), and architecture consistency (5). Static findings
    subtract severity-weighted points from that assessment.

    Approval requires a score of at least 80, no critical or major findings,
    and a completed non-empty test run with no test failures or execution error.
    """

    def __init__(
        self,
        llm: LLMClient | None = None,
        code_analyzer: CodeAnalyzer | None = None,
    ) -> None:
        super().__init__(
            agent_name="Code Reviewer",
            role="Independently assess generated code quality and risks",
            llm=llm if llm is not None else create_groq_chat_model(),
            system_prompt=_REVIEW_SYSTEM_PROMPT,
        )
        self._code_analyzer = (
            code_analyzer if code_analyzer is not None else CodeAnalyzer()
        )

    def execute(self, request: AgentInput) -> AgentOutput:
        """Review serialized architecture, files, requirements, and test result."""
        raw_architecture = request.context.get("architecture_design")
        raw_files = request.context.get("generated_files")
        raw_requirements = request.context.get("requirements", [])
        raw_test_result = request.context.get("test_result")

        if not isinstance(raw_architecture, Mapping):
            raise CodeReviewError(
                "Code Reviewer requires an 'architecture_design' object."
            )
        if not isinstance(raw_files, list):
            raise CodeReviewError("'generated_files' must be a list.")
        if not isinstance(raw_requirements, list) or not all(
            isinstance(requirement, str) for requirement in raw_requirements
        ):
            raise CodeReviewError("'requirements' must be a list of strings.")

        try:
            architecture = ArchitectureDesign.model_validate(raw_architecture)
            files = [GeneratedFile.model_validate(file_data) for file_data in raw_files]
            test_result = (
                TestResult.model_validate(raw_test_result)
                if raw_test_result is not None
                else None
            )
        except ValidationError as error:
            raise CodeReviewError(
                "Code Reviewer received invalid architecture, files, or test results."
            ) from error

        result = self.review(
            files=files,
            requirements=raw_requirements,
            architecture=architecture,
            test_result=test_result,
        )
        return AgentOutput(
            agent_name=self.agent_name,
            content=json.dumps(result.model_dump(mode="json"), indent=2),
        )

    def review(
        self,
        files: Sequence[GeneratedFile],
        requirements: Sequence[str],
        architecture: ArchitectureDesign,
        test_result: TestResult | None,
        static_analysis: CodeAnalysisResult | None = None,
    ) -> ReviewResult:
        """Combine LLM review with deterministic analysis and approval checks."""
        if not files:
            raise CodeReviewError("Code Reviewer requires generated files to inspect.")

        file_list = list(files)
        analysis = static_analysis or self._code_analyzer.analyze(file_list)
        static_findings = (
            analysis.review_issues
            if isinstance(analysis, CodeAnalysisResult)
            else analysis
        )
        request = AgentInput(
            task="Review the submitted implementation and return a rubric assessment.",
            context={
                "requirements": list(requirements),
                "approved_architecture": architecture.model_dump(mode="json"),
                "generated_files": [
                    generated_file.model_dump(mode="json")
                    for generated_file in file_list
                ],
                "test_result": (
                    test_result.model_dump(mode="json")
                    if test_result is not None
                    else None
                ),
                "static_analysis_findings": [
                    finding.model_dump(mode="json") for finding in static_findings
                ],
                "static_analysis_report": (
                    analysis.model_dump(mode="json")
                    if isinstance(analysis, CodeAnalysisResult)
                    else None
                ),
            },
        )
        response = self.run_llm(request)
        assessment = self._parse_assessment(response.content)

        all_findings = self._merge_findings(
            [
                *assessment.critical_issues,
                *assessment.major_issues,
                *assessment.minor_suggestions,
            ],
            static_findings,
        )
        critical = [
            finding
            for finding in all_findings
            if finding.severity is ReviewSeverity.CRITICAL
        ]
        major = [
            finding
            for finding in all_findings
            if finding.severity is ReviewSeverity.MAJOR
        ]
        minor = [
            finding
            for finding in all_findings
            if finding.severity in {ReviewSeverity.MINOR, ReviewSeverity.INFO}
        ]

        weighted_score = sum(
            getattr(assessment, category) * weight
            for category, weight in REVIEW_SCORE_WEIGHTS.items()
        ) / sum(REVIEW_SCORE_WEIGHTS.values())
        penalty = sum(
            _ISSUE_PENALTIES[(issue.severity, issue.confirmed)]
            for issue in all_findings
        )
        score = round(max(0.0, min(100.0, weighted_score - penalty)), 2)

        approval_reasons = self._approval_blockers(score, critical, major, test_result)
        identified_issues = list(assessment.identified_issues)
        identified_issues.extend(
            f"{issue.title}: {issue.description}"
            for issue in [*critical, *major]
            if f"{issue.title}: {issue.description}" not in identified_issues
        )
        suggestions = list(assessment.suggestions)
        suggestions.extend(
            issue.recommendation
            for issue in minor
            if issue.recommendation not in suggestions
        )

        return ReviewResult(
            quality_score=score,
            rubric_scores={
                category: getattr(assessment, category)
                for category in REVIEW_SCORE_WEIGHTS
            },
            identified_issues=identified_issues,
            suggestions=suggestions,
            approved=not approval_reasons,
            critical_issues=critical,
            major_issues=major,
            minor_suggestions=minor,
            approval_rationale=(
                "Approved: score meets the threshold, no critical or major findings "
                "remain, and tests completed successfully."
                if not approval_reasons
                else "Not approved: " + "; ".join(approval_reasons)
            ),
        )

    @staticmethod
    def _parse_assessment(content: str) -> ReviewAssessment:
        """Parse and validate structured rubric output from the reviewer LLM."""
        try:
            payload = json.loads(content)
            if not isinstance(payload, Mapping):
                raise TypeError("assessment must be a JSON object")
            return ReviewAssessment.model_validate(payload)
        except (json.JSONDecodeError, ValidationError, TypeError) as error:
            raise CodeReviewError(
                "Code Reviewer returned a malformed or incomplete review assessment."
            ) from error

    @staticmethod
    def _merge_findings(
        llm_findings: Sequence[ReviewIssue],
        static_findings: Sequence[ReviewIssue],
    ) -> list[ReviewIssue]:
        """Merge findings by location/title, keeping the stronger evidence."""
        merged: dict[tuple[str | None, int | None, str], ReviewIssue] = {}
        for finding in [*llm_findings, *static_findings]:
            key = (
                finding.file_path.casefold() if finding.file_path else None,
                finding.line_number,
                finding.title.casefold(),
            )
            previous = merged.get(key)
            if previous is None or (
                (finding.confirmed, finding.confidence)
                > (previous.confirmed, previous.confidence)
            ):
                merged[key] = finding
        return list(merged.values())

    @staticmethod
    def _approval_blockers(
        score: float,
        critical: Sequence[ReviewIssue],
        major: Sequence[ReviewIssue],
        test_result: TestResult | None,
    ) -> list[str]:
        """Return each unmet, explicit approval condition."""
        blockers: list[str] = []
        if score < APPROVAL_SCORE_THRESHOLD:
            blockers.append(
                f"quality score {score:.2f} is below {APPROVAL_SCORE_THRESHOLD:.0f}"
            )
        if critical:
            blockers.append(f"{len(critical)} critical finding(s) remain")
        if major:
            blockers.append(f"{len(major)} major finding(s) remain")
        if test_result is None:
            blockers.append("no test result was provided")
        elif test_result.total_tests == 0:
            blockers.append("no tests completed")
        elif test_result.failed_tests:
            blockers.append(f"{test_result.failed_tests} test(s) failed")
        if test_result is not None and test_result.execution_error:
            blockers.append("test execution did not complete successfully")
        return blockers
