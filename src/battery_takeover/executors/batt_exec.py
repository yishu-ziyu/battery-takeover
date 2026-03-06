from __future__ import annotations

import shutil

from .base import ExecStatus, run_command
from ..models import ExecResult


class BattExecutor:
    name = "batt"

    def __init__(self, timeout_sec: int):
        self.timeout_sec = timeout_sec

    def probe(self) -> ExecStatus:
        if shutil.which("batt") is None:
            return ExecStatus(available=False, backend=self.name, detail="binary not found")

        rc, out, err = run_command(["batt", "status"], self.timeout_sec)
        # batt may require service/privilege; treat that as not available for write mode.
        if rc == 0 and not _looks_failed(out, err):
            return ExecStatus(available=True, backend=self.name, detail=out or "ok")

        detail = err or out or f"status rc={rc}"
        return ExecStatus(available=False, backend=self.name, detail=detail)

    def set_limit(self, percent: int) -> ExecResult:
        candidates = [
            ["batt", "limit", str(percent)],
            ["batt", str(percent)],
        ]
        return self._run_candidates(candidates, op=f"set_limit:{percent}")

    def clear_limit(self) -> ExecResult:
        candidates = [
            ["batt", "disable"],
            ["batt", "resume"],
        ]
        return self._run_candidates(candidates, op="clear_limit")

    def status(self) -> ExecStatus:
        return self.probe()

    def _run_candidates(self, candidates: list[list[str]], op: str) -> ExecResult:
        if shutil.which("batt") is None:
            return ExecResult(
                success=False,
                backend=self.name,
                error_code="NOT_FOUND",
                error_msg="binary not found",
            )

        raw = []
        last_err = ""
        for cmd in candidates:
            rc, out, err = run_command(cmd, self.timeout_sec)
            raw.append(f"$ {' '.join(cmd)}\nrc={rc}\nout={out}\nerr={err}")
            if rc == 0 and not _looks_failed(out, err):
                return ExecResult(
                    success=True,
                    backend=self.name,
                    raw_output="\n\n".join(raw),
                )
            last_err = err or out or f"rc={rc}"

        return ExecResult(
            success=False,
            backend=self.name,
            error_code="EXEC_FAILED",
            error_msg=f"{op}: {last_err}",
            raw_output="\n\n".join(raw),
        )


def _looks_failed(out: str, err: str) -> bool:
    text = f"{out}\n{err}".lower()
    if "daemon is not running" in text:
        return True
    if "failed to" in text and "error" in text:
        return True
    if text.strip().startswith("error:"):
        return True
    return False
