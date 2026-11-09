"""Import checks for the tool module scaffold."""

from importlib import import_module


def test_tool_modules_are_importable() -> None:
    for module_name in (
        "file_manager",
        "code_executor",
        "test_runner",
        "code_analyzer",
    ):
        import_module(f"app.tools.{module_name}")
