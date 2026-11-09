"""Validated data contracts shared across DevForge AI components."""

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StringConstraints,
    model_validator,
)

NonEmptyString = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class SchemaModel(BaseModel):
    """Base for application schemas that reject unexpected input fields."""

    model_config = ConfigDict(extra="forbid")


class TaskPriority(StrEnum):
    """Relative urgency assigned to a development task."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class TaskStatus(StrEnum):
    """Lifecycle status of a development task."""

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    BLOCKED = "blocked"
    FAILED = "failed"


class ProjectRequest(SchemaModel):
    """Validated description of a project requested for development."""

    project_name: NonEmptyString
    description: NonEmptyString
    requirements: list[NonEmptyString] = Field(default_factory=list)
    programming_language: NonEmptyString
    preferred_framework: NonEmptyString | None = None


class DevelopmentTask(SchemaModel):
    """A unit of development work assigned to an agent."""

    task_id: NonEmptyString
    title: NonEmptyString
    description: NonEmptyString
    assigned_agent: NonEmptyString
    dependencies: list[NonEmptyString] = Field(default_factory=list)
    priority: TaskPriority = TaskPriority.MEDIUM
    status: TaskStatus = TaskStatus.PENDING


class ArchitectureComponent(SchemaModel):
    """Application module and its responsibility in the architecture."""

    name: NonEmptyString
    responsibility: NonEmptyString
    dependencies: list[NonEmptyString] = Field(default_factory=list)


class DatabaseEntity(SchemaModel):
    """Proposed logical database entity and its fields."""

    name: NonEmptyString
    fields: list[NonEmptyString] = Field(min_length=1)
    relationships: list[NonEmptyString] = Field(default_factory=list)


class DatabaseDesign(SchemaModel):
    """Database choice and its logical schema, when persistence is needed."""

    database_engine: NonEmptyString
    entities: list[DatabaseEntity] = Field(default_factory=list)
    design_notes: list[NonEmptyString] = Field(default_factory=list)


class APIEndpoint(SchemaModel):
    """Proposed HTTP API operation and its data contract summary."""

    method: NonEmptyString
    path: NonEmptyString
    purpose: NonEmptyString
    request_schema: str | None = None
    response_schema: str | None = None


class RequirementCoverage(SchemaModel):
    """Architecture elements that address a user requirement or clarify it."""

    requirement: NonEmptyString
    covered_by: list[NonEmptyString] = Field(default_factory=list)
    missing_information: bool = False
    notes: str | None = None


class ArchitectureDesign(SchemaModel):
    """Technology choices and design notes for a proposed project."""

    project_structure: list[NonEmptyString] = Field(min_length=1)
    technology_stack: list[NonEmptyString] = Field(min_length=1)
    design_decisions: list[NonEmptyString] = Field(default_factory=list)
    dependencies: list[NonEmptyString] = Field(default_factory=list)
    application_architecture: list[ArchitectureComponent] = Field(default_factory=list)
    database_design: DatabaseDesign | None = None
    api_endpoints: list[APIEndpoint] = Field(default_factory=list)
    requirement_coverage: list[RequirementCoverage] = Field(default_factory=list)


class GeneratedFile(SchemaModel):
    """A generated source or project file."""

    file_path: NonEmptyString
    content: str
    programming_language: NonEmptyString


class DebuggingCorrection(SchemaModel):
    """Targeted code correction and rationale proposed for a failing file."""

    file: GeneratedFile
    change_summary: NonEmptyString
    rationale: NonEmptyString


class DebuggingResponse(SchemaModel):
    """Structured debugging result tied to one diagnosed root cause."""

    root_cause: NonEmptyString
    corrections: list[DebuggingCorrection] = Field(min_length=1)
    retesting_required: Literal[True] = True


class TestResult(SchemaModel):
    """Summary of a test run and any failure details."""

    total_tests: int = Field(default=0, ge=0)
    passed_tests: int = Field(default=0, ge=0)
    failed_tests: int = Field(default=0, ge=0)
    execution_status: str | None = None
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    output: str = ""
    execution_duration: float | None = Field(default=None, ge=0)
    execution_environment: str | None = None
    error_details: list[NonEmptyString] = Field(default_factory=list)
    execution_error: str | None = None
    actionable_feedback: list[NonEmptyString] = Field(default_factory=list)

    @model_validator(mode="after")
    def test_counts_must_be_consistent(self) -> "TestResult":
        """Ensure every test is counted as either passed or failed."""
        if self.passed_tests + self.failed_tests != self.total_tests:
            raise ValueError("passed_tests + failed_tests must equal total_tests")
        return self


class ReviewSeverity(StrEnum):
    """Relative impact of a code-review finding."""

    CRITICAL = "critical"
    MAJOR = "major"
    MINOR = "minor"
    INFO = "info"


class ReviewIssue(SchemaModel):
    """A review finding with evidence, confidence, and a suggested action."""

    severity: ReviewSeverity
    title: NonEmptyString
    description: NonEmptyString
    confirmed: bool
    confidence: float = Field(ge=0, le=1)
    file_path: NonEmptyString | None = None
    line_number: int | None = Field(default=None, ge=1)
    recommendation: NonEmptyString


class ReviewResult(SchemaModel):
    """Code review outcome, rubric score, findings, and approval decision."""

    quality_score: float = Field(ge=0, le=100)
    rubric_scores: dict[str, Annotated[float, Field(ge=0, le=100)]] = Field(
        default_factory=dict
    )
    identified_issues: list[NonEmptyString] = Field(default_factory=list)
    suggestions: list[NonEmptyString] = Field(default_factory=list)
    approved: bool
    critical_issues: list[ReviewIssue] = Field(default_factory=list)
    major_issues: list[ReviewIssue] = Field(default_factory=list)
    minor_suggestions: list[ReviewIssue] = Field(default_factory=list)
    approval_rationale: NonEmptyString = "Approval criteria have not been evaluated."


class AgentResponse(SchemaModel):
    """A common response envelope for results returned by an agent."""

    agent_name: NonEmptyString
    success: bool
    message: NonEmptyString
    output: JsonValue | None = None
