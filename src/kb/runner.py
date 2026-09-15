"""Subprocess seam: the real runner for systemctl / neo4j-admin and a recording fake."""

import os
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

Completed = subprocess.CompletedProcess[str]


class Runner(Protocol):
    """The subprocess seam service and setup call; SubprocessRunner and RecordingRunner fit it."""

    def run(
        self,
        argv: Sequence[str],
        *,
        env: dict[str, str] | None = None,
        check: bool = True,
        capture: bool = False,
        stdin_file: Path | None = None,
    ) -> Completed: ...


class SubprocessRunner:
    """Runs argv for real with os.environ plus *env*; optional stdin from a file."""

    def run(
        self,
        argv: Sequence[str],
        *,
        env: dict[str, str] | None = None,
        check: bool = True,
        capture: bool = False,
        stdin_file: Path | None = None,
    ) -> Completed:
        full_env = {**os.environ, **(env or {})}
        if stdin_file is None:
            return subprocess.run(
                list(argv), env=full_env, check=check, capture_output=capture, text=True
            )
        with stdin_file.open("rb") as fin:
            return subprocess.run(
                list(argv), env=full_env, check=check, capture_output=capture, text=True, stdin=fin
            )


@dataclass(frozen=True)
class Call:
    """One invocation as the RecordingRunner saw it."""

    argv: tuple[str, ...]
    env: dict[str, str]
    stdin_file: Path | None
    capture: bool = False


@dataclass
class RecordingRunner:
    """Records calls; *handler* fabricates the result (default: rc 0, empty output)."""

    handler: Callable[[Call], Completed] | None = None
    calls: list[Call] = field(default_factory=list)

    def run(
        self,
        argv: Sequence[str],
        *,
        env: dict[str, str] | None = None,
        check: bool = True,
        capture: bool = False,
        stdin_file: Path | None = None,
    ) -> Completed:
        call = Call(tuple(argv), dict(env or {}), stdin_file, capture)
        self.calls.append(call)
        result = (
            self.handler(call)
            if self.handler
            else subprocess.CompletedProcess(list(argv), 0, "", "")
        )
        if check and result.returncode != 0:
            raise subprocess.CalledProcessError(
                result.returncode, list(argv), result.stdout, result.stderr
            )
        return result


def format_process_error(exc: subprocess.CalledProcessError, lines: int = 20) -> str:
    """One line for the failure plus the last *lines* of captured output, when there is any."""
    message = f"{' '.join(str(a) for a in exc.cmd)} failed with exit code {exc.returncode}"
    output = [
        line
        for stream in (exc.stdout, exc.stderr)
        if isinstance(stream, str)
        for line in stream.splitlines()
        if line.strip()
    ]
    if not output:
        return message
    return f"{message}:\n" + "\n".join(output[-lines:])
