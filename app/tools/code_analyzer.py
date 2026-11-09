"""Static source analysis for generated projects; never executes analyzed code."""

import ast
import json
import logging
import subprocess
import sys
from collections.abc import Sequence
from pathlib import PurePosixPath, PureWindowsPath

from pydantic import BaseModel, ConfigDict, Field
from radon.complexity import cc_rank, cc_visit
from radon.metrics import mi_visit
from radon.visitors import Class

from app.models.schemas import GeneratedFile, ReviewIssue, ReviewSeverity

_RUFF_TIMEOUT_SECONDS = 10
_MAX_PYTHON_FILE_BYTES = 1_000_000
_MAX_FILES = 500
_SUPPORTED_LANGUAGES = frozenset({"python", "python3"})


class AnalysisDiagnostic(BaseModel):
    """One syntax, lint, tool, or unsupported-file diagnostic."""

    model_config = ConfigDict(extra="forbid")

    code: str | None = None
    message: str
    line: int | None = Field(default=None, ge=1)
    column: int | None = Field(default=None, ge=1)


class ComplexityMetric(BaseModel):
    """Cyclomatic complexity and Radon rank for one function or method."""

    model_config = ConfigDict(extra="forbid")

    name: str
    line: int = Field(ge=1)
    complexity: int = Field(ge=1)
    rank: str


class ProblematicPattern(BaseModel):
    """A potentially risky construct discovered directly in the Python AST."""

    model_config = ConfigDict(extra="forbid")

    pattern: str
    description: str
    line: int = Field(ge=1)


class FileAnalysisResult(BaseModel):
    """Static-analysis details for one submitted project file."""

    model_config = ConfigDict(extra="forbid")

    file_path: str
    programming_language: str
    supported: bool
    syntax_errors: list[AnalysisDiagnostic] = Field(default_factory=list)
    linting_issues: list[AnalysisDiagnostic] = Field(default_factory=list)
    complexity: list[ComplexityMetric] = Field(default_factory=list)
    function_count: int = Field(default=0, ge=0)
    class_count: int = Field(default=0, ge=0)
    maintainability_index: float | None = Field(default=None, ge=0, le=100)
    problematic_patterns: list[ProblematicPattern] = Field(default_factory=list)
    analysis_errors: list[str] = Field(default_factory=list)


class CodeAnalysisResult(BaseModel):
    """Structured static-analysis report for a set of generated files."""

    model_config = ConfigDict(extra="forbid")

    files: list[FileAnalysisResult] = Field(default_factory=list)
    analysis_errors: list[str] = Field(default_factory=list)
    review_issues: list[ReviewIssue] = Field(default_factory=list)

    @property
    def supported_file_count(self) -> int:
        """Return the number of submitted files analyzed as Python."""
        return sum(file_result.supported for file_result in self.files)

    @property
    def unsupported_file_count(self) -> int:
        """Return the number of submitted files without an analyzer."""
        return sum(not file_result.supported for file_result in self.files)


class CodeAnalyzer:
    """Analyze Python syntax, lint, complexity, and risky AST patterns.

    Ruff receives source via stdin and Radon parses the AST in-process. Neither
    tool imports or executes the submitted source code.
    """

    def __init__(
        self,
        ruff_timeout_seconds: int = _RUFF_TIMEOUT_SECONDS,
        logger: logging.Logger | None = None,
    ) -> None:
        if ruff_timeout_seconds < 1:
            raise ValueError("ruff_timeout_seconds must be positive.")
        self._ruff_timeout_seconds = ruff_timeout_seconds
        self._logger = logger or logging.getLogger(__name__)

    def analyze(self, files: Sequence[GeneratedFile]) -> CodeAnalysisResult:
        """Return structured static-analysis results without executing files."""
        if len(files) > _MAX_FILES:
            return CodeAnalysisResult(
                analysis_errors=[
                    f"Project contains more than the maximum of {_MAX_FILES} files."
                ]
            )

        report = CodeAnalysisResult()
        seen_paths: set[str] = set()
        for generated_file in files:
            path = generated_file.file_path
            normalized_path = PurePosixPath(path).as_posix().casefold()
            if normalized_path in seen_paths:
                report.analysis_errors.append(f"Duplicate project file path: '{path}'.")
                continue
            seen_paths.add(normalized_path)
            file_result = self._analyze_file(generated_file)
            report.files.append(file_result)
            report.review_issues.extend(self._review_issues(file_result))
        return report

    def _analyze_file(self, generated_file: GeneratedFile) -> FileAnalysisResult:
        """Analyze a single file or record why its type is unsupported."""
        language = generated_file.programming_language.strip()
        file_path = generated_file.file_path
        if language.casefold() not in _SUPPORTED_LANGUAGES:
            return FileAnalysisResult(
                file_path=file_path,
                programming_language=language,
                supported=False,
                analysis_errors=[
                    f"Static analysis is not configured for '{language}'."
                ],
            )
        if PurePosixPath(file_path).suffix.casefold() != ".py":
            return FileAnalysisResult(
                file_path=file_path,
                programming_language=language,
                supported=False,
                analysis_errors=[
                    f"Python analyzer requires a .py file; received '{file_path}'."
                ],
            )
        if not self._is_safe_path(file_path):
            return FileAnalysisResult(
                file_path=file_path,
                programming_language=language,
                supported=False,
                analysis_errors=[f"Unsafe project-relative file path: '{file_path}'."],
            )
        if not generated_file.content.strip():
            return FileAnalysisResult(
                file_path=file_path,
                programming_language=language,
                supported=True,
                analysis_errors=["File is empty; syntax and metrics are unavailable."],
            )
        content_bytes = generated_file.content.encode("utf-8", errors="replace")
        if len(content_bytes) > _MAX_PYTHON_FILE_BYTES:
            return FileAnalysisResult(
                file_path=file_path,
                programming_language=language,
                supported=True,
                analysis_errors=["File exceeds the 1 MB static-analysis size limit."],
            )

        result = FileAnalysisResult(
            file_path=file_path,
            programming_language=language,
            supported=True,
        )
        try:
            tree = ast.parse(generated_file.content, filename=file_path)
        except SyntaxError as error:
            result.syntax_errors.append(
                AnalysisDiagnostic(
                    code="SyntaxError",
                    message=error.msg,
                    line=error.lineno,
                    column=error.offset,
                )
            )
            result.linting_issues.extend(
                self._run_ruff(file_path, generated_file.content, result)
            )
            return result
        except (ValueError, RecursionError, MemoryError) as error:
            result.analysis_errors.append(
                f"AST parsing failed ({type(error).__name__}): {error}"
            )
            return result

        result.function_count = sum(
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            for node in ast.walk(tree)
        )
        result.class_count = sum(
            isinstance(node, ast.ClassDef) for node in ast.walk(tree)
        )
        result.problematic_patterns = self._find_patterns(tree)
        try:
            result.complexity = [
                ComplexityMetric(
                    name=block.name,
                    line=block.lineno,
                    complexity=block.complexity,
                    rank=cc_rank(block.complexity),
                )
                for block in cc_visit(generated_file.content)
                if not isinstance(block, Class)
            ]
            result.maintainability_index = round(
                float(mi_visit(generated_file.content, multi=False)),
                2,
            )
        except (SyntaxError, ValueError, RecursionError, TypeError) as error:
            result.analysis_errors.append(
                f"Radon metrics failed ({type(error).__name__}): {error}"
            )

        result.linting_issues.extend(
            self._run_ruff(file_path, generated_file.content, result)
        )
        return result

    def _run_ruff(
        self,
        file_path: str,
        content: str,
        file_result: FileAnalysisResult,
    ) -> list[AnalysisDiagnostic]:
        """Run Ruff on source passed through stdin, never on an executable file."""
        if not self._is_safe_path(file_path):
            file_result.analysis_errors.append(
                f"Unsafe project-relative file path cannot be passed to Ruff: '{file_path}'."
            )
            return []
        command = [
            sys.executable,
            "-m",
            "ruff",
            "check",
            "--output-format=json",
            "--stdin-filename",
            file_path,
            "-",
        ]
        try:
            completed = subprocess.run(
                command,
                input=content,
                capture_output=True,
                text=True,
                timeout=self._ruff_timeout_seconds,
                check=False,
            )
        except FileNotFoundError:
            return self._ruff_error(
                file_result, "Ruff is unavailable in this Python environment."
            )
        except subprocess.TimeoutExpired:
            return self._ruff_error(
                file_result,
                f"Ruff exceeded its {self._ruff_timeout_seconds}-second analysis timeout.",
            )
        except OSError as error:
            return self._ruff_error(file_result, f"Could not start Ruff: {error}")

        if completed.returncode not in {0, 1}:
            detail = completed.stderr.strip() or "Ruff exited with an unexpected error."
            return self._ruff_error(file_result, detail)
        try:
            diagnostics = json.loads(completed.stdout)
        except json.JSONDecodeError as error:
            return self._ruff_error(
                file_result,
                f"Could not parse Ruff's JSON output: {error.msg}.",
            )
        if not isinstance(diagnostics, list):
            return self._ruff_error(
                file_result, "Ruff returned an invalid diagnostic format."
            )
        return [
            AnalysisDiagnostic(
                code=diagnostic.get("code"),
                message=diagnostic.get("message", "Ruff reported an issue."),
                line=(diagnostic.get("location") or {}).get("row"),
                column=(diagnostic.get("location") or {}).get("column"),
            )
            for diagnostic in diagnostics
            if isinstance(diagnostic, dict)
        ]

    @staticmethod
    def _ruff_error(
        file_result: FileAnalysisResult,
        message: str,
    ) -> list[AnalysisDiagnostic]:
        """Record a tool failure explicitly rather than hiding missing analysis."""
        file_result.analysis_errors.append(message)
        return []

    @staticmethod
    def _find_patterns(tree: ast.AST) -> list[ProblematicPattern]:
        """Report selected risky constructs, without asserting exploitability."""
        patterns: list[ProblematicPattern] = []
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in {"eval", "exec"}
            ):
                patterns.append(
                    ProblematicPattern(
                        pattern=f"dynamic-{node.func.id}",
                        description=(
                            f"{node.func.id}() dynamically evaluates source and "
                            "requires review at the trust boundary."
                        ),
                        line=node.lineno,
                    )
                )
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr
                in {"Popen", "run", "call", "check_call", "check_output"}
                and any(
                    keyword.arg == "shell"
                    and isinstance(keyword.value, ast.Constant)
                    and keyword.value.value is True
                    for keyword in node.keywords
                )
            ):
                patterns.append(
                    ProblematicPattern(
                        pattern="subprocess-shell-true",
                        description=(
                            "Subprocess enables shell parsing; verify that "
                            "untrusted input cannot influence the command."
                        ),
                        line=node.lineno,
                    )
                )
            if isinstance(node, ast.ExceptHandler) and (
                node.type is None
                or (
                    isinstance(node.type, ast.Name)
                    and node.type.id in {"Exception", "BaseException"}
                )
            ):
                patterns.append(
                    ProblematicPattern(
                        pattern="broad-exception-handler",
                        description=(
                            "A bare or broad exception handler may conceal "
                            "unexpected failures."
                        ),
                        line=node.lineno,
                    )
                )
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                defaults = [*node.args.defaults, *node.args.kw_defaults]
                if any(
                    isinstance(default, (ast.List, ast.Dict, ast.Set))
                    for default in defaults
                ):
                    patterns.append(
                        ProblematicPattern(
                            pattern="mutable-default-argument",
                            description=(
                                f"Function '{node.name}' has a mutable default "
                                "argument shared between calls."
                            ),
                            line=node.lineno,
                        )
                    )
        return patterns

    @staticmethod
    def _review_issues(file_result: FileAnalysisResult) -> list[ReviewIssue]:
        """Convert selected static facts to the reviewer's shared issue model."""
        issues: list[ReviewIssue] = []
        for syntax_error in file_result.syntax_errors:
            issues.append(
                ReviewIssue(
                    severity=ReviewSeverity.MAJOR,
                    title="Python syntax error",
                    description=syntax_error.message,
                    confirmed=True,
                    confidence=1.0,
                    file_path=file_result.file_path,
                    line_number=syntax_error.line,
                    recommendation="Correct the syntax error before running tests.",
                )
            )
        for pattern in file_result.problematic_patterns:
            title = (
                f"Dynamic code execution via {pattern.pattern.removeprefix('dynamic-')}"
                if pattern.pattern in {"dynamic-eval", "dynamic-exec"}
                else pattern.pattern.replace("-", " ").title()
            )
            issues.append(
                ReviewIssue(
                    severity=ReviewSeverity.MAJOR,
                    title=title,
                    description=pattern.description,
                    confirmed=False,
                    confidence=0.85,
                    file_path=file_result.file_path,
                    line_number=pattern.line,
                    recommendation=(
                        "Review the construct and replace it with a narrower, "
                        "explicit alternative when practical."
                    ),
                )
            )
        for diagnostic in file_result.linting_issues:
            if diagnostic.code == "E999" and not file_result.syntax_errors:
                issues.append(
                    ReviewIssue(
                        severity=ReviewSeverity.MAJOR,
                        title="Python syntax error",
                        description=diagnostic.message,
                        confirmed=True,
                        confidence=1.0,
                        file_path=file_result.file_path,
                        line_number=diagnostic.line,
                        recommendation="Correct the syntax error before running tests.",
                    )
                )
        return issues

    @staticmethod
    def _is_safe_path(path: str) -> bool:
        """Prevent command-option injection and invalid Ruff virtual paths."""
        posix_path = PurePosixPath(path)
        windows_path = PureWindowsPath(path)
        return not (
            not path
            or path.startswith("-")
            or "\\" in path
            or "\x00" in path
            or ":" in path
            or posix_path.is_absolute()
            or windows_path.is_absolute()
            or windows_path.drive
            or any(part in {"", ".", ".."} for part in path.split("/"))
            or any(ord(character) < 32 for character in path)
        )


class PythonCodeAnalyzer(CodeAnalyzer):
    """Backward-compatible name for the Python-capable CodeAnalyzer."""
