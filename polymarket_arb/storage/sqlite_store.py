"""SQLite 持久化：信号、安静窗口、订单、持仓、校准记录。"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Dict, Optional

_SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    market TEXT NOT NULL,
    action TEXT NOT NULL,
    x_c REAL, horizon_h REAL, p_success REAL, reason TEXT,
    gates_failed TEXT
);
CREATE TABLE IF NOT EXISTS quiet_windows (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    market TEXT NOT NULL, started_at TEXT NOT NULL, ended_at TEXT,
    baseline REAL, mu_estimate REAL
);
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL, market TEXT NOT NULL, leg INTEGER,
    side TEXT, bucket TEXT, token TEXT, price_c REAL,
    qty REAL, budget_usd REAL, status TEXT
);
CREATE TABLE IF NOT EXISTS positions (
    market TEXT PRIMARY KEY, state TEXT, entered_at TEXT,
    budget_usd REAL, notes TEXT
);
CREATE TABLE IF NOT EXISTS calibration (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL, mu_quiet REAL, kappa REAL, nu REAL,
    p_min_hit REAL, json TEXT
);
"""


class SqliteStore:
    def __init__(self, path: str):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.executescript(_SCHEMA)
        self.conn.commit()

    @staticmethod
    def _now() -> str:
        from datetime import datetime, timezone

        return datetime.now(timezone.utc).isoformat()

    def log_signal(self, market: str, action: str, x_c: Optional[float],
                   horizon_h: Optional[float], p_success: Optional[float],
                   reason: str, gates_failed: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO signals (ts,market,action,x_c,horizon_h,p_success,reason,gates_failed) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (self._now(), market, action, x_c, horizon_h, p_success, reason, gates_failed),
        )
        self.conn.commit()
        return cur.lastrowid

    def mark_quiet(self, market: str, baseline: float) -> int:
        cur = self.conn.execute(
            "INSERT INTO quiet_windows (market,started_at,baseline) VALUES (?,?,?)",
            (market, self._now(), baseline),
        )
        self.conn.commit()
        return cur.lastrowid

    def log_order(self, market: str, leg: int, side: str, bucket: str, token: str,
                  price_c: float, qty: float, budget_usd: float, status: str) -> None:
        self.conn.execute(
            "INSERT INTO orders (ts,market,leg,side,bucket,token,price_c,qty,budget_usd,status) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (self._now(), market, leg, side, bucket, token, price_c, qty, budget_usd, status),
        )
        self.conn.commit()

    def upsert_position(self, market: str, state: str, entered_at: str,
                        budget_usd: float, notes: str) -> None:
        self.conn.execute(
            "INSERT INTO positions (market,state,entered_at,budget_usd,notes) "
            "VALUES (?,?,?,?,?) "
            "ON CONFLICT(market) DO UPDATE SET "
            "state=excluded.state, entered_at=excluded.entered_at,"
            "budget_usd=excluded.budget_usd, notes=excluded.notes",
            (market, state, entered_at, budget_usd, notes),
        )
        self.conn.commit()

    def record_calibration(self, mu_quiet: float, kappa: float, nu: float,
                           p_min_hit: float, payload: Dict[str, Any]) -> None:
        import json

        self.conn.execute(
            "INSERT INTO calibration (at,mu_quiet,kappa,nu,p_min_hit,json) VALUES (?,?,?,?,?,?)",
            (self._now(), mu_quiet, kappa, nu, p_min_hit, json.dumps(payload)),
        )
        self.conn.commit()

    def recent_signals(self, limit: int = 100) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM signals ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        cols = [d[0] for d in self.conn.execute("SELECT * FROM signals LIMIT 0").description]
        return [dict(zip(cols, r)) for r in rows]