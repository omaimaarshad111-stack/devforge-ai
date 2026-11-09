"""Bounded host-side control for disposable Docker sandbox containers."""

import logging
import subprocess
from threading import Thread
from typing import BinaryIO

_MAX_STREAM_BYTES = 12_000
_DOCKER_STOP_TIMEOUT_SECONDS = 5


class DockerCommandResult:
    """Captured Docker CLI outcome, including whether the client timed out."""

    def __init__(
        self,
        returncode: int | None,
        stdout: str,
        stderr: str,
        timed_out: bool,
    ) -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.timed_out = timed_out


class _BoundedStream:
    """Keep only the most recent bytes read from one child-process stream."""

    def __init__(self) -> None:
        self.data = bytearray()
        self.truncated = False

    def drain(self, stream: BinaryIO) -> None:
        """Consume a pipe continuously while retaining only a bounded tail."""
        while chunk := stream.read(4096):
            self.data.extend(chunk)
            overflow = len(self.data) - _MAX_STREAM_BYTES
            if overflow > 0:
                del self.data[:overflow]
                self.truncated = True

    def text(self) -> str:
        """Decode captured bytes without failing on partial UTF-8 sequences."""
        value = self.data.decode("utf-8", errors="replace")
        return f"[output truncated]\n{value}" if self.truncated else value


def run_docker_container(
    command: list[str],
    timeout_seconds: int,
    docker_executable: str,
    container_name: str,
    logger: logging.Logger,
) -> DockerCommandResult:
    """Run Docker with bounded output capture and terminate timed-out containers."""
    process = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if process.stdout is None or process.stderr is None:
        process.kill()
        raise RuntimeError("Docker process output pipes could not be opened.")

    stdout = _BoundedStream()
    stderr = _BoundedStream()
    readers = [
        Thread(target=stdout.drain, args=(process.stdout,), daemon=True),
        Thread(target=stderr.drain, args=(process.stderr,), daemon=True),
    ]
    for reader in readers:
        reader.start()

    timed_out = False
    try:
        process.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        timed_out = True
        try:
            process.kill()
            process.wait(timeout=_DOCKER_STOP_TIMEOUT_SECONDS)
        except (OSError, subprocess.SubprocessError):
            logger.warning("Could not terminate the timed-out Docker client process.")
    finally:
        for reader in readers:
            reader.join(timeout=_DOCKER_STOP_TIMEOUT_SECONDS)

    if timed_out:
        stop_container(docker_executable, container_name, logger)
    return DockerCommandResult(
        returncode=process.returncode,
        stdout=stdout.text(),
        stderr=stderr.text(),
        timed_out=timed_out,
    )


def stop_container(
    docker_executable: str,
    container_name: str,
    logger: logging.Logger,
) -> None:
    """Stop, then forcibly remove, a container after its client times out."""
    try:
        stopped = subprocess.run(
            [
                docker_executable,
                "stop",
                "--time=1",
                container_name,
            ],
            capture_output=True,
            text=True,
            timeout=_DOCKER_STOP_TIMEOUT_SECONDS,
            check=False,
        )
        if stopped.returncode == 0:
            return
    except (OSError, subprocess.SubprocessError):
        pass

    try:
        removed = subprocess.run(
            [
                docker_executable,
                "rm",
                "--force",
                container_name,
            ],
            capture_output=True,
            text=True,
            timeout=_DOCKER_STOP_TIMEOUT_SECONDS,
            check=False,
        )
        if removed.returncode != 0:
            logger.warning("Could not remove timed-out sandbox container.")
    except (OSError, subprocess.SubprocessError):
        logger.warning("Could not remove timed-out sandbox container.")
