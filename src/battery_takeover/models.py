from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional


class RuntimeMode(str, Enum):
    OBSERVE_ONLY = "OBSERVE_ONLY"
    ACTIVE_CONTROL = "ACTIVE_CONTROL"
    DEGRADED_READONLY = "DEGRADED_READONLY"


class ActionType(str, Enum):
    SET_LIMIT = "SET_LIMIT"
    CLEAR_LIMIT = "CLEAR_LIMIT"
    NOOP = "NOOP"
    DEGRADE = "DEGRADE"


@dataclass(slots=True)
class BatterySample:
    ts: str
    on_ac: bool
    percent: int
    charging: bool
    time_remaining_min: Optional[int]
    cycle_count: Optional[int]
    max_capacity_pct: Optional[int]
    source_raw: str


@dataclass(slots=True)
class ExecResult:
    success: bool
    backend: str
    error_code: Optional[str] = None
    error_msg: Optional[str] = None
    raw_output: Optional[str] = None


@dataclass(slots=True)
class PolicyDecision:
    action_type: ActionType
    reason: str
    target_percent: Optional[int] = None


@dataclass(slots=True)
class RuntimeSnapshot:
    mode: RuntimeMode
    observe_started_at: str
    consecutive_failures: int
    charging_paused: bool
    last_action_at: Optional[str]
    last_backend: Optional[str]
    last_error: Optional[str]
