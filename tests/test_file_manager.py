"""Tests for path-confined generated-project filesystem operations."""

import logging
from pathlib import Path

import pytest

from app.tools.file_manager import (
    FileManager,
    FileManagerError,
    UnsafePathError,
)


@pytest.fixture
def file_manager(tmp_path: Path) -> FileManager:
    """Create a file manager rooted in a temporary generated-project directory."""
    return FileManager(tmp_path / "generated_projects")


def test_create_project_directories_and_files(file_manager: FileManager) -> None:
    project = file_manager.create_project("sample")
    nested = file_manager.create_directory("sample", "app/services")

    created = file_manager.create_file(
        "sample",
        "app/services/project.py",
        "def get_project():\n    return None\n",
    )

    assert project.is_dir()
    assert nested.is_dir()
    assert created.read_text(encoding="utf-8").startswith("def get_project")


def test_create_file_does_not_overwrite_without_authorization(
    file_manager: FileManager,
) -> None:
    file_manager.create_project("sample")
    file_manager.create_file("sample", "README.md", "original")

    with pytest.raises(FileManagerError, match="already exists"):
        file_manager.create_file("sample", "README.md", "replacement")

    assert file_manager.read_file("sample", "README.md") == "original"


def test_update_requires_authorization_and_replaces_existing_file(
    file_manager: FileManager,
) -> None:
    file_manager.create_project("sample")
    file_manager.create_file("sample", "README.md", "before")

    with pytest.raises(FileManagerError, match="overwrite_authorized"):
        file_manager.update_file("sample", "README.md", "unauthorized")

    file_manager.update_file(
        "sample",
        "README.md",
        "after",
        overwrite_authorized=True,
    )

    assert file_manager.read_file("sample", "README.md") == "after"


def test_list_files_returns_project_relative_paths(file_manager: FileManager) -> None:
    file_manager.create_project("sample")
    file_manager.create_directory("sample", "app")
    file_manager.create_file("sample", "app/main.py", "main = True")
    file_manager.create_file("sample", "README.md", "docs")

    assert file_manager.list_files("sample") == ["README.md", "app/main.py"]
    assert file_manager.list_files("sample", recursive=False) == ["README.md"]
    assert file_manager.list_files("sample", "app") == ["app/main.py"]


def test_delete_temporary_file_requires_authorization_and_temp_extension(
    file_manager: FileManager,
) -> None:
    file_manager.create_project("sample")
    file_manager.create_file("sample", "scratch.tmp", "temporary")

    with pytest.raises(FileManagerError, match="authorized=True"):
        file_manager.delete_temporary_file("sample", "scratch.tmp")

    file_manager.create_file("sample", "keep.txt", "important")
    with pytest.raises(FileManagerError, match="Only files"):
        file_manager.delete_temporary_file(
            "sample",
            "keep.txt",
            authorized=True,
        )
    file_manager.delete_temporary_file("sample", "scratch.tmp", authorized=True)

    assert file_manager.list_files("sample") == ["keep.txt"]


@pytest.mark.parametrize(
    "relative_path",
    [
        "../outside.txt",
        "app/../../outside.py",
        "/absolute/path.py",
        "C:/outside.py",
        r"app\outside.py",
        "app//empty.py",
        "app/./file.py",
        ".env",
        "credentials.pem",
    ],
)
def test_rejects_traversal_absolute_paths_and_disallowed_extensions(
    file_manager: FileManager,
    relative_path: str,
) -> None:
    file_manager.create_project("sample")

    with pytest.raises((UnsafePathError, FileManagerError)):
        file_manager.create_file("sample", relative_path, "content")


def test_rejects_unsafe_project_names(file_manager: FileManager) -> None:
    for project_name in ("../escape", "C:", "nested/project", r"nested\project", ""):
        with pytest.raises(UnsafePathError):
            file_manager.create_project(project_name)


def test_rejects_project_root_outside_generated_projects(tmp_path: Path) -> None:
    manager = FileManager(tmp_path / "generated_projects")

    with pytest.raises(UnsafePathError):
        manager.create_project("../outside")


def test_read_write_size_limit_is_enforced(file_manager: FileManager) -> None:
    manager = FileManager(
        file_manager.root_directory,
        max_file_size_bytes=5,
    )
    manager.create_project("sample")

    with pytest.raises(FileManagerError, match="size limit"):
        manager.create_file("sample", "large.txt", "123456")

    manager.create_file("sample", "small.txt", "12345")
    (manager.root_directory / "sample" / "small.txt").write_text(
        "123456",
        encoding="utf-8",
    )
    with pytest.raises(FileManagerError, match="read size limit"):
        manager.read_file("sample", "small.txt")


def test_permission_errors_are_propagated_and_logged(
    file_manager: FileManager,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    file_manager.create_project("sample")
    file_manager.create_directory("sample", "app")
    caplog.set_level(logging.WARNING, logger="app.tools.file_manager")

    original_open = Path.open

    def reject_file_open(
        path: Path,
        *args: object,
        **kwargs: object,
    ):
        if path.name == "blocked.py":
            raise PermissionError("permission denied")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", reject_file_open)

    with pytest.raises(PermissionError):
        file_manager.create_file("sample", "app/blocked.py", "content")

    assert "create_file failed" in caplog.text
    assert "PermissionError" in caplog.text
    assert "content" not in caplog.text


def test_every_successful_operation_is_logged(
    file_manager: FileManager, caplog
) -> None:
    caplog.set_level(logging.INFO, logger="app.tools.file_manager")
    file_manager.create_project("sample")
    file_manager.create_file("sample", "README.md", "not logged")
    file_manager.read_file("sample", "README.md")
    file_manager.update_file(
        "sample",
        "README.md",
        "updated",
        overwrite_authorized=True,
    )
    file_manager.list_files("sample")

    assert "Created project directory: sample" in caplog.text
    assert "Created project file: sample/README.md" in caplog.text
    assert "Read project file: sample/README.md" in caplog.text
    assert "Updated project file: sample/README.md" in caplog.text
    assert "Listed files in project directory: sample/." in caplog.text
    assert "not logged" not in caplog.text
