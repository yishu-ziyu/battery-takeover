from __future__ import annotations

from datetime import date as date_cls
from datetime import datetime, time, timedelta
from pathlib import Path
from statistics import mean
from zoneinfo import ZoneInfo

from .config import AppConfig
from .storage import Storage


def _parse_date(value: str | None) -> date_cls:
    if not value:
        return datetime.now().date()
    return datetime.strptime(value, "%Y-%m-%d").date()


def _to_utc_window(day: date_cls, tz_name: str) -> tuple[str, str]:
    tz = ZoneInfo(tz_name)
    start_local = datetime.combine(day, time.min).replace(tzinfo=tz)
    end_local = start_local + timedelta(days=1)
    start_utc = start_local.astimezone(ZoneInfo("UTC")).isoformat()
    end_utc = end_local.astimezone(ZoneInfo("UTC")).isoformat()
    return start_utc, end_utc


def generate_daily_report(cfg: AppConfig, storage: Storage, date_value: str | None = None) -> Path:
    report_day = _parse_date(date_value)
    start_ts, end_ts = _to_utc_window(report_day, cfg.sampling.timezone)

    samples = storage.list_samples(start_ts, end_ts)
    actions = storage.list_actions(start_ts, end_ts)

    percents = [int(row["percent"]) for row in samples]
    ac_count = sum(int(row["on_ac"]) for row in samples)
    charging_count = sum(int(row["charging"]) for row in samples)

    line_count = len(samples)
    if percents:
        p_min = min(percents)
        p_max = max(percents)
        p_avg = round(mean(percents), 2)
    else:
        p_min = p_max = p_avg = "n/a"

    action_total = len(actions)
    action_errors = sum(1 for row in actions if int(row["success"]) == 0)

    lines = [
        f"# 电池接管日报 - {report_day.isoformat()}",
        "",
        "## 样本概览",
        f"- 样本数: {line_count}",
        f"- 电量最小/最大/均值: {p_min}/{p_max}/{p_avg}",
        f"- AC 供电样本占比: {round((ac_count / line_count) * 100, 2) if line_count else 0}%",
        f"- 充电中样本占比: {round((charging_count / line_count) * 100, 2) if line_count else 0}%",
        "",
        "## 策略动作",
        f"- 动作总数: {action_total}",
        f"- 失败动作数: {action_errors}",
        "",
        "## 动作明细",
    ]

    if not actions:
        lines.append("- 无动作记录")
    else:
        for row in actions:
            lines.append(
                "- "
                f"{row['ts']} | {row['action_type']} | backend={row['backend']} | "
                f"success={row['success']} | target={row['target_percent']} | err={row['error_msg'] or ''}"
            )

    report_path = cfg.paths.reports_dir / f"{report_day.isoformat()}.md"
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    _trim_reports(cfg.paths.reports_dir, keep=30)
    return report_path


def _trim_reports(reports_dir: Path, keep: int) -> None:
    files = sorted([p for p in reports_dir.glob("*.md") if p.is_file()])
    if len(files) <= keep:
        return
    for p in files[: len(files) - keep]:
        p.unlink(missing_ok=True)
