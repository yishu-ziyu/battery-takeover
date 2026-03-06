from __future__ import annotations

from .base import ExecStatus
from ..models import ExecResult


class NoopExecutor:
    name = "noop"

    def probe(self) -> ExecStatus:
        return ExecStatus(available=True, backend=self.name, detail="read-only fallback")

    def set_limit(self, percent: int) -> ExecResult:
        return ExecResult(success=True, backend=self.name, raw_output=f"noop set_limit {percent}")

    def clear_limit(self) -> ExecResult:
        return ExecResult(success=True, backend=self.name, raw_output="noop clear_limit")

    def status(self) -> ExecStatus:
        return self.probe()
