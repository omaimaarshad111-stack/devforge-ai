"""Import checks for the workflow module scaffold."""

from importlib import import_module


def test_graph_modules_are_importable() -> None:
    for module_name in ("state", "workflow", "nodes"):
        import_module(f"app.graph.{module_name}")
