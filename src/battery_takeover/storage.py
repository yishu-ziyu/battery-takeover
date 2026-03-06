from __future__ import annotations

from contextlib import contextmanager
import sqlite3
from dataclasses import asdict
from pathlib import Path

from .models import BatterySample


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS samples (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  on_ac INTEGER NOT NULL,
  percent INTEGER NOT NULL,
  charging INTEGER NOT NULL,
  time_remaining_min INTEGER,
  cycle_count INTEGER,
  max_capacity_pct INTEGER,
  source_raw TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS actions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  action_type TEXT NOT NULL,
  backend TEXT NOT NULL,
  target_percent INTEGER,
  success INTEGER NOT NULL,
  error_code TEXT,
  error_msg TEXT
);

CREATE TABLE IF NOT EXISTS runtime_state (
  k TEXT PRIMARY KEY,
  v TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_samples_ts ON samples(ts);
CREATE INDEX IF NOT EXISTS idx_actions_ts ON actions(ts);
"""


class Storage:
    def __init__(self, db_path: Path):
        self.db_path = db_path

    @contextmanager
    def _session(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def init_db(self) -> None:
        with self._session() as conn:
            conn.executescript(SCHEMA_SQL)

    def insert_sample(self, sample: BatterySample) -> int:
        fields = asdict(sample)
        with self._session() as conn:
            cur = conn.execute(
                """
                INSERT INTO samples (
                    ts,on_ac,percent,charging,time_remaining_min,
                    cycle_count,max_capacity_pct,source_raw
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    fields["ts"],
                    1 if fields["on_ac"] else 0,
                    fields["percent"],
                    1 if fields["charging"] else 0,
                    fields["time_remaining_min"],
                    fields["cycle_count"],
                    fields["max_capacity_pct"],
                    fields["source_raw"],
                ),
            )
            return int(cur.lastrowid)

    def insert_action(
        self,
        *,
        ts: str,
        action_type: str,
        backend: str,
        target_percent: int | None,
        success: bool,
        error_code: str | None,
        error_msg: str | None,
    ) -> int:
        with self._session() as conn:
            cur = conn.execute(
                """
                INSERT INTO actions (
                    ts, action_type, backend, target_percent,
                    success, error_code, error_msg
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ts,
                    action_type,
                    backend,
                    target_percent,
                    1 if success else 0,
                    error_code,
                    error_msg,
                ),
            )
            return int(cur.lastrowid)

    def set_state(self, key: str, value: str, ts: str) -> None:
        with self._session() as conn:
            conn.execute(
                """
                INSERT INTO runtime_state (k, v, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(k) DO UPDATE SET v=excluded.v, updated_at=excluded.updated_at
                """,
                (key, value, ts),
            )

    def get_state(self, key: str) -> str | None:
        with self._session() as conn:
            row = conn.execute(
                "SELECT v FROM runtime_state WHERE k = ?",
                (key,),
            ).fetchone()
            return None if row is None else str(row["v"])

    def get_state_map(self) -> dict[str, str]:
        with self._session() as conn:
            rows = conn.execute("SELECT k, v FROM runtime_state").fetchall()
            return {str(row["k"]): str(row["v"]) for row in rows}

    def list_samples(self, start_ts: str, end_ts: str) -> list[sqlite3.Row]:
        with self._session() as conn:
            rows = conn.execute(
                """
                SELECT * FROM samples
                WHERE ts >= ? AND ts < ?
                ORDER BY ts ASC
                """,
                (start_ts, end_ts),
            ).fetchall()
            return rows

    def list_actions(self, start_ts: str, end_ts: str) -> list[sqlite3.Row]:
        with self._session() as conn:
            rows = conn.execute(
                """
                SELECT * FROM actions
                WHERE ts >= ? AND ts < ?
                ORDER BY ts ASC
                """,
                (start_ts, end_ts),
            ).fetchall()
            return rows

    def latest_action(self) -> sqlite3.Row | None:
        with self._session() as conn:
            return conn.execute(
                "SELECT * FROM actions ORDER BY ts DESC LIMIT 1"
            ).fetchone()

    def latest_sample(self) -> sqlite3.Row | None:
        with self._session() as conn:
            return conn.execute(
                "SELECT * FROM samples ORDER BY ts DESC LIMIT 1"
            ).fetchone()

    def count_samples(self) -> int:
        with self._session() as conn:
            row = conn.execute("SELECT COUNT(*) AS c FROM samples").fetchone()
            return int(row["c"]) if row else 0

    def count_actions(self) -> int:
        with self._session() as conn:
            row = conn.execute("SELECT COUNT(*) AS c FROM actions").fetchone()
            return int(row["c"]) if row else 0
