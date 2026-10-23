"""Cola persistente de trabajos que no se pudieron imprimir.

Una comanda cuya impresora no responde queda aquí (escritura síncrona en
SQLite) y se reintenta hasta que vuelve. También guarda los `job_id` ya
impresos durante 24 h para que un reintento del POS no imprima dos veces,
aunque el puente se haya reiniciado entre medio.
"""
from __future__ import annotations

import contextlib
import json
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from bridge.backends import Target
from bridge.errors import BridgeError

SCHEMA = """
CREATE TABLE IF NOT EXISTS queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT, target_label TEXT NOT NULL, target TEXT NOT NULL, fallback TEXT,
    document_type TEXT, data BLOB NOT NULL, created REAL NOT NULL, next_try REAL NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT, status TEXT NOT NULL DEFAULT 'pending'
);
CREATE INDEX IF NOT EXISTS idx_queue_status ON queue(status, next_try);
CREATE TABLE IF NOT EXISTS done (
    job_id TEXT NOT NULL, target_label TEXT NOT NULL, test_mode INTEGER NOT NULL, ts REAL NOT NULL,
    PRIMARY KEY (job_id, target_label, test_mode)
);
"""


@dataclass
class QueuedJob:
    id: int
    job_id: str | None
    target: Target
    fallback: Target | None
    document_type: str
    data: bytes
    attempts: int
    created: float


def _target_to_json(target: Target | None) -> str | None:
    if target is None:
        return None
    return json.dumps({"kind": target.kind, "printer_name": target.printer_name, "ip": target.ip, "port": target.port})


def _target_from_json(text: str | None) -> Target | None:
    if not text:
        return None
    return Target(**json.loads(text))


class Outbox:
    def __init__(self, db_path: Path, clock: Callable[[], float] = time.time, max_per_target: int = 200,
                 expire_s: int = 7200, done_ttl_s: int = 86400):
        self.db_path = Path(db_path)
        self.clock = clock
        self.max_per_target = max_per_target
        self.expire_s = expire_s
        self.done_ttl_s = done_ttl_s
        self._lock = threading.Lock()
        with self._conn() as conn:
            conn.executescript(SCHEMA)

    @contextlib.contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.db_path, timeout=10, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            yield conn
            conn.commit()
        finally:
            conn.close()

    # -- cola -----------------------------------------------------------
    def add(self, job_id: str | None, target: Target, document_type: str, data: bytes,
            fallback: Target | None = None) -> int:
        now = self.clock()
        with self._lock, self._conn() as conn:
            if job_id:
                row = conn.execute("SELECT id FROM queue WHERE job_id = ? AND target_label = ? AND status = 'pending'",
                                   (job_id, target.label)).fetchone()
                if row:
                    return row["id"]
            count = conn.execute("SELECT COUNT(*) FROM queue WHERE target_label = ? AND status = 'pending'",
                                 (target.label,)).fetchone()[0]
            if count >= self.max_per_target:
                raise BridgeError(f"La cola de {target.label} está llena ({count} trabajos).", 503, code="queue_full")
            cur = conn.execute(
                "INSERT INTO queue (job_id, target_label, target, fallback, document_type, data, created, next_try) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (job_id, target.label, _target_to_json(target), _target_to_json(fallback), document_type,
                 bytes(data), now, now))
            return cur.lastrowid

    def due(self, now: float | None = None) -> list[QueuedJob]:
        now = self.clock() if now is None else now
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM queue WHERE status = 'pending' AND next_try <= ? ORDER BY id",
                                (now,)).fetchall()
        return [QueuedJob(r["id"], r["job_id"], _target_from_json(r["target"]), _target_from_json(r["fallback"]),
                          r["document_type"] or "kitchen", r["data"], r["attempts"], r["created"]) for r in rows]

    def mark_done(self, queue_id: int, test_mode: bool = False) -> None:
        with self._lock, self._conn() as conn:
            row = conn.execute("SELECT job_id, target_label FROM queue WHERE id = ?", (queue_id,)).fetchone()
            conn.execute("DELETE FROM queue WHERE id = ?", (queue_id,))
            if row and row["job_id"]:
                conn.execute("INSERT OR REPLACE INTO done VALUES (?, ?, ?, ?)",
                             (row["job_id"], row["target_label"], int(test_mode), self.clock()))

    def mark_failed(self, queue_id: int, message: str) -> None:
        now = self.clock()
        with self._lock, self._conn() as conn:
            row = conn.execute("SELECT attempts, created FROM queue WHERE id = ?", (queue_id,)).fetchone()
            if not row:
                return
            attempts = row["attempts"] + 1
            if now - row["created"] >= self.expire_s:
                conn.execute("UPDATE queue SET status = 'expired', attempts = ?, last_error = ?, next_try = ? "
                             "WHERE id = ?", (attempts, message, now, queue_id))
                return
            wait = min(10 * 2 ** (attempts - 1), 60)
            conn.execute("UPDATE queue SET attempts = ?, last_error = ?, next_try = ? WHERE id = ?",
                         (attempts, message, now + wait, queue_id))

    def discard(self, queue_id: int) -> None:
        with self._lock, self._conn() as conn:
            conn.execute("DELETE FROM queue WHERE id = ?", (queue_id,))

    def retry_now(self, queue_id: int) -> None:
        with self._lock, self._conn() as conn:
            conn.execute("UPDATE queue SET next_try = ? WHERE id = ? AND status = 'pending'", (self.clock(), queue_id))

    def is_pending(self, job_id: str, target_label: str) -> bool:
        with self._conn() as conn:
            return conn.execute("SELECT 1 FROM queue WHERE job_id = ? AND target_label = ? AND status = 'pending'",
                                (job_id, target_label)).fetchone() is not None

    def pending(self, target_label: str | None = None) -> list[dict]:
        sql = ("SELECT id, job_id, target_label, document_type, length(data) AS bytes, created, next_try, attempts, "
               "last_error FROM queue WHERE status = 'pending'")
        params: tuple = ()
        if target_label:
            sql += " AND target_label = ?"
            params = (target_label,)
        with self._conn() as conn:
            return [dict(r) for r in conn.execute(sql + " ORDER BY id", params).fetchall()]

    def counts(self) -> dict:
        now = self.clock()
        with self._conn() as conn:
            rows = conn.execute("SELECT target_label, COUNT(*) AS n, MIN(created) AS oldest FROM queue "
                                "WHERE status = 'pending' GROUP BY target_label").fetchall()
        oldest = min((r["oldest"] for r in rows), default=None)
        return {"total": sum(r["n"] for r in rows), "by_target": {r["target_label"]: r["n"] for r in rows},
                "oldest_age_s": round(now - oldest) if oldest is not None else 0}

    def expired(self, within_s: int = 86400) -> list[dict]:
        since = self.clock() - within_s
        with self._conn() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT id, job_id, target_label, document_type, created, attempts, last_error FROM queue "
                "WHERE status = 'expired' AND next_try >= ? ORDER BY id", (since,)).fetchall()]

    # -- impresos -------------------------------------------------------
    def remember_done(self, job_id: str, target_label: str, test_mode: bool) -> None:
        now = self.clock()
        with self._lock, self._conn() as conn:
            conn.execute("INSERT OR REPLACE INTO done VALUES (?, ?, ?, ?)", (job_id, target_label, int(test_mode), now))
            conn.execute("DELETE FROM done WHERE ts < ?", (now - self.done_ttl_s,))

    def was_done(self, job_id: str, target_label: str, test_mode: bool) -> bool:
        with self._conn() as conn:
            row = conn.execute("SELECT ts FROM done WHERE job_id = ? AND target_label = ? AND test_mode = ?",
                               (job_id, target_label, int(test_mode))).fetchone()
        return bool(row) and self.clock() - row["ts"] < self.done_ttl_s
