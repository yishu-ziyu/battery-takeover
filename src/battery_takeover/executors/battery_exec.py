from __future__ import annotations

import shutil

from .base import ExecStatus, run_command
from ..models import ExecResult


class BatteryExecutor:
    name = "battery"

    def __init__(self, timeout_sec: int):
        self.timeout_sec = timeout_sec

    def probe(self) -> ExecStatus:
        if shutil.which("battery") is None:
            return ExecStatus(available=False, backend=self.name, detail="binary not found")
        rc, out, err = run_command(["battery", "status"], self.timeout_sec)
        if rc == 0 and not _looks_failed(out, err):
            return ExecStatus(available=True, backend=self.name, detail=out or "ok")
        return ExecStatus(
            available=False,
            backend=self.name,
            detail=err or out or f"status rc={rc}",
        )

    def set_limit(self, percent: int) -> ExecResult:
        candidates = [
            ["battery", "maintain", str(percent)],
            ["battery", "charge", str(percent)],
        ]
        return self._run_candidates(candidates, op=f"set_limit:{percent}")

    def clear_limit(self) -> ExecResult:
        candidates = [
            ["battery", "maintain", "stop"],
            ["battery", "charging", "on"],
        ]
        return self._run_candidates(candidates, op="clear_limit")

    def status(self) -> ExecStatus:
        return self.probe()

    def _run_candidates(self, candidates: list[list[str]], op: str) -> ExecResult:
        if shutil.which("battery") is None:
            return ExecResult(
                success=False,
                backend=self.name,
                error_code="NOT_FOUND",
                error_msg="binary not found",
            )

        last_err = ""
        raw = []
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
    if "error:" in text:
        return True
    if "failed" in text and "permission" in text:
        return True
    return False
