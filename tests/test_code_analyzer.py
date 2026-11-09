"""Tests for structured, non-executing source analysis."""

from app.models.schemas import GeneratedFile
from app.tools.code_analyzer import CodeAnalyzer


def python_file(content: str, file_path: str = "app/sample.py") -> GeneratedFile:
    """Create one Python source fixture."""
    return GeneratedFile(
        file_path=file_path,
        content=content,
        programming_language="Python",
    )


def test_analyzes_clean_code_with_ast_and_radon_metrics() -> None:
    result = CodeAnalyzer().analyze(
        [
            python_file(
                "class Calculator:\n"
                "    def add(self, left, right):\n"
                "        return left + right\n"
            )
        ]
    )

    assert len(result.files) == 1
    file_result = result.files[0]
    assert file_result.supported is True
    assert not file_result.syntax_errors
    assert not file_result.linting_issues
    assert file_result.function_count == 1
    assert file_result.class_count == 1
    assert file_result.complexity[0].complexity == 1
    assert file_result.complexity[0].rank == "A"
    assert file_result.maintainability_index is not None
    assert not file_result.problematic_patterns
    assert not file_result.analysis_errors


def test_reports_python_syntax_error() -> None:
    result = CodeAnalyzer().analyze([python_file("def broken(:\n    pass\n")])

    file_result = result.files[0]
    assert file_result.syntax_errors
    assert file_result.syntax_errors[0].code == "SyntaxError"
    assert file_result.syntax_errors[0].line == 1
    assert file_result.syntax_errors[0].message


def test_reports_ruff_lint_violations() -> None:
    result = CodeAnalyzer().analyze(
        [python_file("import os\n\ndef value():\n    return undefined_name\n")]
    )

    diagnostics = result.files[0].linting_issues
    assert any(issue.code == "F401" for issue in diagnostics)
    assert any(issue.code == "F821" for issue in diagnostics)


def test_reports_high_cyclomatic_complexity() -> None:
    branches = "\n".join(
        f"    {'if' if index == 1 else 'elif'} value == {index}:\n"
        f"        return {index}"
        for index in range(1, 15)
    )
    source = f"def classify(value):\n{branches}\n    return -1\n"

    result = CodeAnalyzer().analyze([python_file(source)])

    metric = result.files[0].complexity[0]
    assert metric.name == "classify"
    assert metric.complexity >= 14
    assert metric.rank in {"C", "D", "E", "F"}


def test_empty_file_is_reported_without_claiming_syntax_error() -> None:
    result = CodeAnalyzer().analyze([python_file(" \n\t")])

    file_result = result.files[0]
    assert file_result.supported is True
    assert not file_result.syntax_errors
    assert file_result.function_count == 0
    assert file_result.class_count == 0
    assert "empty" in file_result.analysis_errors[0].casefold()


def test_unsupported_language_and_file_type_are_reported() -> None:
    result = CodeAnalyzer().analyze(
        [
            GeneratedFile(
                file_path="app/main.js",
                content="console.log('hello')",
                programming_language="JavaScript",
            ),
            GeneratedFile(
                file_path="app/not_python.txt",
                content="print('hello')",
                programming_language="Python",
            ),
        ]
    )

    assert result.unsupported_file_count == 2
    assert all(not file_result.supported for file_result in result.files)
    assert "not configured" in result.files[0].analysis_errors[0]
    assert ".py" in result.files[1].analysis_errors[0]


def test_reports_potentially_problematic_patterns() -> None:
    source = (
        "def parse(value, cache=[]):\n"
        "    try:\n"
        "        return eval(value)\n"
        "    except Exception:\n"
        "        return None\n"
    )

    result = CodeAnalyzer().analyze([python_file(source)])

    patterns = {pattern.pattern for pattern in result.files[0].problematic_patterns}
    assert {
        "mutable-default-argument",
        "dynamic-eval",
        "broad-exception-handler",
    } <= patterns
    assert any(
        issue.title == "Dynamic code execution via eval"
        for issue in result.review_issues
    )


def test_analysis_does_not_execute_submitted_code() -> None:
    result = CodeAnalyzer().analyze(
        [
            python_file(
                "raise RuntimeError('must not execute')\nprint('must not execute')\n"
            )
        ]
    )

    assert result.files[0].syntax_errors == []
    assert not any(
        "must not execute" in diagnostic.message
        for diagnostic in result.files[0].linting_issues
    )
