"""Path-confined filesystem operations for generated project workspaces."""

import logging
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Final

from app.config.settings import settings

_DEFAULT_ALLOWED_EXTENSIONS: Final[frozenset[str]] = frozenset(
    {
        ".c",
        ".cfg",
        ".cpp",
        ".css",
        ".cs",
        ".go",
        ".h",
        ".hpp",
        ".html",
        ".ini",
        ".java",
        ".js",
        ".json",
        ".jsx",
        ".md",
        ".php",
        ".py",
        ".rb",
        ".rs",
        ".sql",
        ".toml",
        ".ts",
        ".tsx",
        ".tmp",
        ".temp",
        ".txt",
        ".xml",
        ".yaml",
        ".yml",
    }
)
_ALLOWED_EXTENSIONLESS_NAMES: Final[frozenset[str]] = frozenset(
    {"dockerfile", "makefile", ".dockerignore", ".gitignore"}
)
_TEMPORARY_EXTENSIONS: Final[frozenset[str]] = frozenset({".tmp", ".temp"})
_PROTECTED_FILENAMES: Final[frozenset[str]] = frozenset(
    {".env", ".env.local", ".env.production", "id_rsa", "id_ed25519"}
)


class FileManagerError(RuntimeError):
    """Raised when a generated-project file operation is invalid or fails."""


class UnsafePathError(FileManagerError):
    """Raised when a project name or relative path escapes its workspace."""


class FileManager:
    """Manage files under one generated-project root without executing them.

    Every operation requires a project name and project-relative path.
    Symlinks and special files are rejected to reduce path-escape and
    unintended-device-file risks. File contents are never written to logs.
    """

    def __init__(
        self,
        generated_projects_dir: str | Path | None = None,
        allowed_extensions: frozenset[str] | None = None,
        logger: logging.Logger | None = None,
        max_file_size_bytes: int = 2_000_000,
    ) -> None:
        root = Path(generated_projects_dir or settings.generated_projects_dir)
        self._root = root.expanduser().resolve()
        self._allowed_extensions = (
            frozenset(extension.casefold() for extension in allowed_extensions)
            if allowed_extensions is not None
            else _DEFAULT_ALLOWED_EXTENSIONS
        )
        self._logger = logger or logging.getLogger(__name__)
        if max_file_size_bytes < 1:
            raise ValueError("max_file_size_bytes must be positive")
        self._max_file_size_bytes = max_file_size_bytes

    @property
    def root_directory(self) -> Path:
        """Return the resolved root that confines all managed projects."""
        return self._root

    def create_project(self, project_name: str) -> Path:
        """Create and return a project directory under the generated root."""
        try:
            project_path = self._project_path(project_name)
            self._ensure_no_symlink_components(project_path)
            project_path.mkdir(parents=True, exist_ok=True)
            if not project_path.is_dir():
                raise FileManagerError("Project path exists and is not a directory.")
            self._logger.info("Created project directory: %s", project_name)
            return project_path
        except (OSError, FileManagerError) as error:
            self._log_failure("create_project", project_name, error)
            raise

    def create_directory(self, project_name: str, relative_path: str) -> Path:
        """Create a project subdirectory and return its absolute path."""
        try:
            directory = self._resolve_project_path(project_name, relative_path)
            self._ensure_no_symlink_components(directory)
            directory.mkdir(parents=True, exist_ok=True)
            if not directory.is_dir():
                raise FileManagerError(
                    "The requested directory path is not a directory."
                )
            self._logger.info(
                "Created project directory: %s/%s", project_name, relative_path
            )
            return directory
        except (OSError, FileManagerError) as error:
            self._log_failure("create_directory", relative_path, error)
            raise

    def create_file(
        self,
        project_name: str,
        relative_path: str,
        content: str,
        *,
        overwrite_authorized: bool = False,
    ) -> Path:
        """Create a file; replacing an existing file requires explicit approval."""
        try:
            file_path = self._resolve_project_path(
                project_name, relative_path, require_extension=True
            )
            self._ensure_no_symlink_components(file_path)
            self._ensure_parent_directory(file_path)
            self._check_content_size(content)
            mode = "w" if overwrite_authorized else "x"
            with file_path.open(mode, encoding="utf-8", newline="") as file:
                file.write(content)
            self._logger.info(
                "%s project file: %s/%s",
                "Updated" if overwrite_authorized else "Created",
                project_name,
                relative_path,
            )
            return file_path
        except (OSError, FileManagerError) as error:
            self._log_failure("create_file", relative_path, error)
            raise

    def read_file(self, project_name: str, relative_path: str) -> str:
        """Read a UTF-8 project file without following symlinks."""
        try:
            file_path = self._resolve_project_path(project_name, relative_path)
            self._ensure_regular_file(file_path)
            if file_path.stat().st_size > self._max_file_size_bytes:
                raise FileManagerError("File exceeds the configured read size limit.")
            content = file_path.read_text(encoding="utf-8")
            self._logger.info("Read project file: %s/%s", project_name, relative_path)
            return content
        except (OSError, FileManagerError) as error:
            self._log_failure("read_file", relative_path, error)
            raise

    def update_file(
        self,
        project_name: str,
        relative_path: str,
        content: str,
        *,
        overwrite_authorized: bool = False,
    ) -> Path:
        """Replace an existing file only when overwrite is explicitly authorized."""
        if not overwrite_authorized:
            error = FileManagerError(
                "Updating a file requires overwrite_authorized=True."
            )
            self._log_failure("update_file", relative_path, error)
            raise error
        try:
            file_path = self._resolve_project_path(
                project_name, relative_path, require_extension=True
            )
            self._ensure_regular_file(file_path)
            self._check_content_size(content)
            with file_path.open("w", encoding="utf-8", newline="") as file:
                file.write(content)
            self._logger.info(
                "Updated project file: %s/%s", project_name, relative_path
            )
            return file_path
        except (OSError, FileManagerError) as error:
            self._log_failure("update_file", relative_path, error)
            raise

    def list_files(
        self,
        project_name: str,
        relative_path: str = ".",
        *,
        recursive: bool = True,
    ) -> list[str]:
        """List regular project files using project-relative POSIX paths."""
        try:
            directory = self._resolve_project_path(project_name, relative_path)
            self._ensure_directory(directory)
            iterator = directory.rglob("*") if recursive else directory.iterdir()
            files: list[str] = []
            for entry in iterator:
                if entry.is_symlink():
                    continue
                if entry.is_file():
                    self._ensure_within_root(entry)
                    files.append(
                        entry.relative_to(self._project_path(project_name)).as_posix()
                    )
            result = sorted(files)
            self._logger.info(
                "Listed files in project directory: %s/%s", project_name, relative_path
            )
            return result
        except (OSError, FileManagerError) as error:
            self._log_failure("list_files", relative_path, error)
            raise

    def delete_temporary_file(
        self,
        project_name: str,
        relative_path: str,
        *,
        authorized: bool = False,
    ) -> None:
        """Delete only a temporary file, and only after explicit authorization."""
        if not authorized:
            error = FileManagerError(
                "Deleting a temporary file requires authorized=True."
            )
            self._log_failure("delete_temporary_file", relative_path, error)
            raise error
        try:
            file_path = self._resolve_project_path(project_name, relative_path)
            if file_path.suffix.casefold() not in _TEMPORARY_EXTENSIONS:
                raise FileManagerError(
                    "Only files with .tmp or .temp extensions may be deleted."
                )
            self._ensure_regular_file(file_path)
            file_path.unlink()
            self._logger.info(
                "Deleted authorized temporary file: %s/%s",
                project_name,
                relative_path,
            )
        except (OSError, FileManagerError) as error:
            self._log_failure("delete_temporary_file", relative_path, error)
            raise

    def _project_path(self, project_name: str) -> Path:
        """Resolve a single safe project-name segment under the root."""
        if (
            not project_name
            or project_name in {".", ".."}
            or "/" in project_name
            or "\\" in project_name
            or ":" in project_name
            or "\x00" in project_name
            or any(ord(character) < 32 for character in project_name)
        ):
            raise UnsafePathError("Project name must be a single safe path segment.")
        project_path = self._root / project_name
        self._ensure_within_root(project_path)
        return project_path

    def _resolve_project_path(
        self,
        project_name: str,
        relative_path: str,
        *,
        require_extension: bool = False,
    ) -> Path:
        """Resolve and validate a project-relative POSIX path."""
        project_path = self._project_path(project_name)
        if (
            not relative_path
            or "\x00" in relative_path
            or "\\" in relative_path
            or ":" in relative_path
            or PurePosixPath(relative_path).is_absolute()
            or PureWindowsPath(relative_path).is_absolute()
            or PureWindowsPath(relative_path).drive
            or any(ord(character) < 32 for character in relative_path)
        ):
            raise UnsafePathError(
                "Path must be relative to the project and use '/' separators."
            )

        parts = relative_path.split("/")
        if relative_path != "." and any(part in {"", ".", ".."} for part in parts):
            raise UnsafePathError(
                "Path traversal and empty path segments are not allowed."
            )
        if relative_path == ".":
            if require_extension:
                raise FileManagerError("A file path is required.")
            candidate = project_path
        else:
            candidate = project_path.joinpath(*PurePosixPath(relative_path).parts)
        self._ensure_within_root(candidate)
        if require_extension:
            self._validate_extension(candidate)
        return candidate

    def _ensure_within_root(self, path: Path) -> None:
        """Reject paths that resolve outside the configured root."""
        resolved = path.resolve(strict=False)
        if not resolved.is_relative_to(self._root):
            raise UnsafePathError(
                "Path resolves outside the generated-project workspace."
            )

    def _ensure_no_symlink_components(self, path: Path) -> None:
        """Reject symlinks along the path from the managed root to the target."""
        self._ensure_within_root(path)
        relative_parts = path.relative_to(self._root).parts
        current = self._root
        for part in relative_parts:
            current = current / part
            if current.is_symlink():
                raise UnsafePathError(
                    "Symbolic links are not allowed in project paths."
                )

    def _ensure_regular_file(self, file_path: Path) -> None:
        """Require a regular, non-symlink file inside an existing project."""
        self._ensure_no_symlink_components(file_path)
        if not file_path.is_file():
            raise FileManagerError("The requested project file does not exist.")

    def _ensure_directory(self, directory: Path) -> None:
        """Require an existing project directory without symlink components."""
        self._ensure_no_symlink_components(directory)
        if not directory.is_dir():
            raise FileManagerError("The requested project directory does not exist.")

    def _ensure_parent_directory(self, file_path: Path) -> None:
        """Require the file's parent directory to exist within the project."""
        self._ensure_directory(file_path.parent)
        if file_path.exists():
            raise FileManagerError(
                "File already exists; pass overwrite_authorized=True to replace it."
            )

    def _validate_extension(self, file_path: Path) -> None:
        """Allow documented project file types and reject secret/environment files."""
        if file_path.name.casefold() in _PROTECTED_FILENAMES:
            raise FileManagerError("Secret and environment files are not allowed.")
        if (
            file_path.suffix.casefold() not in self._allowed_extensions
            and file_path.name.casefold() not in _ALLOWED_EXTENSIONLESS_NAMES
        ):
            raise FileManagerError(
                f"File extension '{file_path.suffix}' is not allowed."
            )

    def _check_content_size(self, content: str) -> None:
        """Reject file contents larger than the configured size limit."""
        size = len(content.encode("utf-8"))
        if size > self._max_file_size_bytes:
            raise FileManagerError("Content exceeds the configured file size limit.")

    def _log_failure(self, operation: str, target: str, error: Exception) -> None:
        """Log failures without including file contents or sensitive data."""
        self._logger.warning(
            "File operation %s failed for %s: %s",
            operation,
            repr(target),
            type(error).__name__,
        )
