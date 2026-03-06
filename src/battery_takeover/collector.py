from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import re
import subprocess

from .models import BatterySample


PMSET_CMD = ["pmset", "-g", "batt"]
SPPOWER_CMD = ["system_profiler", "SPPowerDataType"]


@dataclass(slots=True)
class CollectRaw:
    pmset: str
    system_profiler: str


class CollectorError(RuntimeError):
    pass


def _run(cmd: list[str], timeout_sec: int) -> str:
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_sec,
            check=False,
        )
    except Exception as exc:
        raise CollectorError(f"command failed: {' '.join(cmd)} ({exc})") from exc

    if proc.returncode != 0:
        raise CollectorError(
            f"command non-zero: {' '.join(cmd)} rc={proc.returncode} stderr={proc.stderr.strip()}"
        )
    return proc.stdout.strip()


def _parse_time_remaining(text: str) -> int | None:
    m = re.search(r"(\d+):(\d+)\s+remaining", text)
    if not m:
        return None
    hour = int(m.group(1))
    minute = int(m.group(2))
    return hour * 60 + minute


def _parse_pmset(pmset_raw: str) -> tuple[bool, int, bool, int | None]:
    lines = [line.strip() for line in pmset_raw.splitlines() if line.strip()]
    if len(lines) < 2:
        raise CollectorError(f"unexpected pmset output: {pmset_raw!r}")

    source_line = lines[0].lower()
    battery_line = lines[1].lower()

    on_ac = "ac power" in source_line

    m_percent = re.search(r"(\d+)%", battery_line)
    if not m_percent:
        raise CollectorError(f"cannot parse battery percent from: {lines[1]!r}")
    percent = int(m_percent.group(1))

    charging = "charging" in battery_line and "discharging" not in battery_line
    remaining = _parse_time_remaining(battery_line)

    return on_ac, percent, charging, remaining


def _parse_system_profiler(raw: str) -> tuple[int | None, int | None]:
    m_cycle = re.search(r"Cycle Count:\s*(\d+)", raw)
    cycle_count = int(m_cycle.group(1)) if m_cycle else None

    m_capacity = re.search(r"Maximum Capacity:\s*(\d+)%", raw)
    max_capacity = int(m_capacity.group(1)) if m_capacity else None

    return cycle_count, max_capacity


def collect_raw(timeout_sec: int) -> CollectRaw:
    pmset_raw = _run(PMSET_CMD, timeout_sec)
    sppower_raw = _run(SPPOWER_CMD, timeout_sec)
    return CollectRaw(pmset=pmset_raw, system_profiler=sppower_raw)


def collect_sample(timeout_sec: int) -> BatterySample:
    raw = collect_raw(timeout_sec=timeout_sec)
    on_ac, percent, charging, time_remaining = _parse_pmset(raw.pmset)
    cycle_count, max_capacity = _parse_system_profiler(raw.system_profiler)

    ts = datetime.now(timezone.utc).isoformat()
    source_raw = (
        "pmset:\n"
        + raw.pmset
        + "\n\n"
        + "system_profiler:\n"
        + raw.system_profiler
    )

    return BatterySample(
        ts=ts,
        on_ac=on_ac,
        percent=percent,
        charging=charging,
        time_remaining_min=time_remaining,
        cycle_count=cycle_count,
        max_capacity_pct=max_capacity,
        source_raw=source_raw,
    )
