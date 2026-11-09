"""Import checks for the agent module scaffold."""

from importlib import import_module


def test_agent_modules_are_importable() -> None:
    agent_modules = (
        "base_agent",
        "project_manager",
        "architect",
        "developer",
        "tester",
        "reviewer",
        "debugger",
    )

    for module_name in agent_modules:
        import_module(f"app.agents.{module_name}")
