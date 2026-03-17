from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
import tomllib


@dataclass(slots=True)
class PolicyConfig:
    stop_percent: int
    resume_percent: int
    observe_hours: int
    min_action_interval_sec: int


@dataclass(slots=True)
class SamplingConfig:
    interval_sec: int
    timezone: str


@dataclass(slots=True)
class ControlConfig:
    enabled: bool
    allow_write_after_observe: bool


@dataclass(slots=True)
class ExecutorConfig:
    preferred: list[str]
    auto_fallback: bool
    command_timeout_sec: int


@dataclass(slots=True)
class NotifyConfig:
    terminal: bool
    macos_notification: bool


@dataclass(slots=True)
class PathsConfig:
    db: Path
    log: Path
    reports_dir: Path


@dataclass(slots=True)
class AppConfig:
    config_path: Path
    policy: PolicyConfig
    sampling: SamplingConfig
    control: ControlConfig
    executor: ExecutorConfig
    notify: NotifyConfig
    paths: PathsConfig


def project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def default_config_path() -> Path:
    return project_root() / "config" / "default.toml"


def _resolve_path(base_dir: Path, value: str) -> Path:
    p = Path(value)
    if p.is_absolute():
        return p
    return (base_dir / p).resolve()


def _get_section(data: dict[str, Any], key: str) -> dict[str, Any]:
    value = data.get(key)
    if not isinstance(value, dict):
        raise ValueError(f"Missing or invalid section: {key}")
    return value


def load_config(path: str | Path | None = None) -> AppConfig:
    cfg_path = Path(path) if path else default_config_path()
    cfg_path = cfg_path.expanduser().resolve()
    raw = tomllib.loads(cfg_path.read_text(encoding="utf-8"))

    policy = _get_section(raw, "policy")
    sampling = _get_section(raw, "sampling")
    control = _get_section(raw, "control")
    executor = _get_section(raw, "executor")
    notify = _get_section(raw, "notify")
    paths = _get_section(raw, "paths")

    policy_cfg = PolicyConfig(
        stop_percent=int(policy["stop_percent"]),
        resume_percent=int(policy["resume_percent"]),
        observe_hours=int(policy["observe_hours"]),
        min_action_interval_sec=int(policy["min_action_interval_sec"]),
    )
    if policy_cfg.resume_percent >= policy_cfg.stop_percent:
        raise ValueError("resume_percent must be lower than stop_percent")

    sampling_cfg = SamplingConfig(
        interval_sec=int(sampling["interval_sec"]),
        timezone=str(sampling["timezone"]),
    )
    control_cfg = ControlConfig(
        enabled=bool(control["enabled"]),
        allow_write_after_observe=bool(control["allow_write_after_observe"]),
    )
    preferred = executor.get("preferred", [])
    if not isinstance(preferred, list) or not preferred:
        raise ValueError("executor.preferred must be a non-empty list")
    executor_cfg = ExecutorConfig(
        preferred=[str(x) for x in preferred],
        auto_fallback=bool(executor["auto_fallback"]),
        command_timeout_sec=int(executor["command_timeout_sec"]),
    )
    notify_cfg = NotifyConfig(
        terminal=bool(notify["terminal"]),
        macos_notification=bool(notify["macos_notification"]),
    )

    base = cfg_path.parent
    paths_cfg = PathsConfig(
        db=_resolve_path(base, str(paths["db"])),
        log=_resolve_path(base, str(paths["log"])),
        reports_dir=_resolve_path(base, str(paths["reports_dir"])),
    )

    return AppConfig(
        config_path=cfg_path,
        policy=policy_cfg,
        sampling=sampling_cfg,
        control=control_cfg,
        executor=executor_cfg,
        notify=notify_cfg,
        paths=paths_cfg,
    )


def ensure_runtime_dirs(cfg: AppConfig) -> None:
    cfg.paths.db.parent.mkdir(parents=True, exist_ok=True)
    cfg.paths.log.parent.mkdir(parents=True, exist_ok=True)
    cfg.paths.reports_dir.mkdir(parents=True, exist_ok=True)


def save_config(cfg: AppConfig) -> None:
    lines = [
        "[policy]",
        f"stop_percent = {cfg.policy.stop_percent}",
        f"resume_percent = {cfg.policy.resume_percent}",
        f"observe_hours = {cfg.policy.observe_hours}",
        f"min_action_interval_sec = {cfg.policy.min_action_interval_sec}",
        "",
        "[sampling]",
        f"interval_sec = {cfg.sampling.interval_sec}",
        f'timezone = "{cfg.sampling.timezone}"',
        "",
        "[control]",
        f"enabled = {'true' if cfg.control.enabled else 'false'}",
        f"allow_write_after_observe = {'true' if cfg.control.allow_write_after_observe else 'false'}",
        "",
        "[executor]",
        "preferred = [" + ", ".join(f'\"{x}\"' for x in cfg.executor.preferred) + "]",
        f"auto_fallback = {'true' if cfg.executor.auto_fallback else 'false'}",
        f"command_timeout_sec = {cfg.executor.command_timeout_sec}",
        "",
        "[notify]",
        f"terminal = {'true' if cfg.notify.terminal else 'false'}",
        f"macos_notification = {'true' if cfg.notify.macos_notification else 'false'}",
        "",
        "[paths]",
        f'db = "{_relative_to_config_dir(cfg.config_path, cfg.paths.db)}"',
        f'log = "{_relative_to_config_dir(cfg.config_path, cfg.paths.log)}"',
        f'reports_dir = "{_relative_to_config_dir(cfg.config_path, cfg.paths.reports_dir)}"',
        "",
    ]
    cfg.config_path.write_text("\n".join(lines), encoding="utf-8")


def update_policy_thresholds(
    cfg_path: str | Path,
    *,
    stop_percent: int,
    resume_percent: int,
) -> AppConfig:
    cfg = load_config(cfg_path)
    if stop_percent < 50 or stop_percent > 100:
        raise ValueError("stop_percent must be between 50 and 100")
    if resume_percent < 40 or resume_percent >= stop_percent:
        raise ValueError("resume_percent must be >= 40 and lower than stop_percent")
    cfg.policy.stop_percent = int(stop_percent)
    cfg.policy.resume_percent = int(resume_percent)
    save_config(cfg)
    return load_config(cfg_path)


def update_control_enabled(
    cfg_path: str | Path,
    *,
    enabled: bool,
) -> AppConfig:
    cfg = load_config(cfg_path)
    cfg.control.enabled = bool(enabled)
    save_config(cfg)
    return load_config(cfg_path)


def update_dashboard_settings(
    cfg_path: str | Path,
    *,
    stop_percent: int,
    resume_percent: int,
    enabled: bool,
) -> AppConfig:
    cfg = load_config(cfg_path)
    if stop_percent < 50 or stop_percent > 100:
        raise ValueError("stop_percent must be between 50 and 100")
    if resume_percent < 40 or resume_percent >= stop_percent:
        raise ValueError("resume_percent must be >= 40 and lower than stop_percent")
    cfg.policy.stop_percent = int(stop_percent)
    cfg.policy.resume_percent = int(resume_percent)
    cfg.control.enabled = bool(enabled)
    save_config(cfg)
    return load_config(cfg_path)


def _relative_to_config_dir(config_path: Path, target_path: Path) -> str:
    base = config_path.parent.resolve()
    try:
        rel = target_path.resolve().relative_to(base)
        return rel.as_posix()
    except Exception:
        return str(target_path.resolve())
