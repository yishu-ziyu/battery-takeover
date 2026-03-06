from __future__ import annotations

from dataclasses import dataclass
import subprocess
from typing import Protocol

from ..models import ExecResult


@dataclass(slots=True)
class ExecStatus:
    available: bool
    backend: str
    detail: str


class Executor(Protocol):
    name: str

    def probe(self) -> ExecStatus:
        ...

    def set_limit(self, percent: int) -> ExecResult:
        ...

    def clear_limit(self) -> ExecResult:
        ...

    def status(self) -> ExecStatus:
        ...


def run_command(cmd: list[str], timeout_sec: int) -> tuple[int, str, str]:
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout_sec,
        check=False,
    )
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


class ExecutorRouter:
    def __init__(
        self,
        executors: dict[str, Executor],
        preferred: list[str],
        auto_fallback: bool,
    ):
        self.executors = executors
        self.preferred = preferred
        self.auto_fallback = auto_fallback

    def probe_map(self) -> dict[str, ExecStatus]:
        out: dict[str, ExecStatus] = {}
        for name, executor in self.executors.items():
            out[name] = executor.probe()
        return out

    def choose(self) -> tuple[str, Executor | None, ExecStatus | None]:
        for name in self.preferred:
            executor = self.executors.get(name)
            if executor is None:
                continue
            status = executor.probe()
            if status.available:
                return name, executor, status
            if not self.auto_fallback:
                return name, None, status
        return "noop", None, None
