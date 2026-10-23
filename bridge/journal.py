"""Bitácora diaria en SQLite.

Las escrituras van a una cola y las vuelca un único hilo: imprimir nunca
espera a la base, y un error de SQLite (disco lleno, base bloqueada) se
anota en el log de texto sin afectar la impresión.
"""
from __future__ import annotations

import contextlib
import csv
import io
import logging
import queue
import sqlite3
import threading
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)

COLUMNS = (
    "ts", "day", "kind", "document_type", "target_label", "target_kind", "printer_alias",
    "origin", "client_ip", "bytes", "duration_ms", "result", "http_status", "message",
    "job_id", "test_mode", "payload_kind", "payload",
)
LIST_COLUMNS = ("id",) + tuple(c for c in COLUMNS if c != "payload")
CSV_HEADER = ("id", "hora", "accion", "documento", "destino", "tipo_destino", "origen",
              "ip_cliente", "bytes", "duracion_ms", "resultado", "http", "mensaje",
              "id_trabajo", "modo_prueba")

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL, day TEXT NOT NULL, kind TEXT NOT NULL,
    document_type TEXT, target_label TEXT, target_kind TEXT, printer_alias TEXT,
    origin TEXT, client_ip TEXT, bytes INTEGER, duration_ms INTEGER,
    result TEXT, http_status INTEGER, message TEXT, job_id TEXT,
    test_mode INTEGER DEFAULT 0, payload_kind TEXT, payload BLOB
);
CREATE INDEX IF NOT EXISTS idx_events_day ON events(day, ts);
"""

_STOP = object()


class Journal:
    def __init__(self, db_path: Path, clock: Callable[[], datetime] = datetime.now, max_queue: int = 1000):
        self.db_path = Path(db_path)
        self.clock = clock
        # Acotada: si la base se traba, se pierden registros, nunca memoria sin límite.
        self._queue: queue.Queue = queue.Queue(maxsize=max_queue)
        self._thread: threading.Thread | None = None
        self._health_seen: dict[str, datetime] = {}
        self._health_lock = threading.Lock()

    # -- ciclo de vida -------------------------------------------------
    def start(self) -> None:
        try:
            with contextlib.closing(self._connect()) as conn:
                conn.executescript(SCHEMA)
        except sqlite3.Error as exc:
            logger.error("No se pudo abrir la bitácora %s: %s", self.db_path, exc)
        self._thread = threading.Thread(target=self._writer, name="journal-writer", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._thread:
            self._queue.put(_STOP)
            self._thread.join(timeout=5)
            self._thread = None

    def flush(self, timeout: float = 5) -> None:
        done = threading.Event()
        try:
            self._queue.put(done, timeout=timeout)
        except queue.Full:
            return
        done.wait(timeout)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=5, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _writer(self) -> None:
        conn = None
        while True:
            item = self._queue.get()
            if item is _STOP:
                break
            if isinstance(item, threading.Event):
                item.set()
                continue
            try:
                if conn is None:
                    conn = self._connect()
                placeholders = ", ".join("?" for _ in COLUMNS)
                conn.execute(
                    f"INSERT INTO events ({', '.join(COLUMNS)}) VALUES ({placeholders})",
                    [item.get(c) for c in COLUMNS],
                )
                conn.commit()
            except Exception as exc:  # noqa: BLE001 - el hilo escritor no puede morir
                logger.error("No se pudo escribir en la bitácora: %s", exc)
                if conn is not None:
                    try:
                        conn.close()
                    except sqlite3.Error:
                        pass
                    conn = None
        if conn is not None:
            conn.close()

    # -- escritura -----------------------------------------------------
    def record(self, **fields) -> None:
        now = self.clock()
        row = {c: fields.get(c) for c in COLUMNS}
        row["ts"] = now.astimezone().isoformat(timespec="milliseconds")
        row["day"] = now.date().isoformat()
        row["test_mode"] = 1 if fields.get("test_mode") else 0
        if isinstance(row["payload"], (bytearray, memoryview)):
            row["payload"] = bytes(row["payload"])
        for key, value in row.items():
            if isinstance(value, str):
                # Un sustituto UTF-16 suelto (JSON válido) no se puede guardar en SQLite.
                row[key] = value.encode("utf-8", "replace").decode("utf-8")
        try:
            self._queue.put_nowait(row)
        except queue.Full:
            logger.error("Bitácora saturada: se descarta un registro (%s)", row.get("kind"))

    def should_log_health(self, origin: str) -> bool:
        now = self.clock()
        key = origin or ""
        with self._health_lock:
            last = self._health_seen.get(key)
            if last is not None and now - last < timedelta(seconds=60):
                return False
            self._health_seen[key] = now
            return True

    # -- lectura -------------------------------------------------------
    def _read(self, sql: str, params=()) -> list[sqlite3.Row]:
        try:
            with contextlib.closing(self._connect()) as conn:
                return conn.execute(sql, params).fetchall()
        except sqlite3.Error as exc:
            logger.error("No se pudo leer la bitácora: %s", exc)
            return []

    def query_day(self, day: date, result: str | None = None, target: str | None = None,
                  document_type: str | None = None, text: str | None = None,
                  limit: int = 500, offset: int = 0) -> list[dict]:
        where, params = ["day = ?"], [day.isoformat()]
        if result:
            where.append("result = ?")
            params.append(result)
        if target:
            where.append("target_label = ?")
            params.append(target)
        if document_type:
            where.append("document_type = ?")
            params.append(document_type)
        if text:
            where.append("(message LIKE ? OR target_label LIKE ? OR origin LIKE ? OR client_ip LIKE ?)")
            params.extend([f"%{text}%"] * 4)
        sql = (f"SELECT {', '.join(LIST_COLUMNS)} FROM events WHERE {' AND '.join(where)} "
               "ORDER BY ts DESC, id DESC LIMIT ? OFFSET ?")
        return [dict(r) for r in self._read(sql, params + [limit, offset])]

    def get(self, event_id: int) -> dict | None:
        rows = self._read("SELECT * FROM events WHERE id = ?", (event_id,))
        if not rows:
            return None
        row = dict(rows[0])
        row["payload"] = row["payload"].hex() if row["payload"] is not None else None
        return row

    def get_payload(self, event_id: int) -> bytes | None:
        rows = self._read("SELECT payload FROM events WHERE id = ?", (event_id,))
        return rows[0]["payload"] if rows else None

    def summary_day(self, day: date) -> dict:
        rows = self._read(
            "SELECT ts, kind, target_label, result, message FROM events WHERE day = ? ORDER BY ts",
            (day.isoformat(),),
        )
        summary = {"total": 0, "ok": 0, "error": 0, "rejected": 0,
                   "by_hour": [0] * 24, "errors_by_hour": [0] * 24, "by_target": {}}
        for row in rows:
            if row["kind"] not in ("print", "cashbox", "rejected"):
                continue
            summary["total"] += 1
            result = row["result"]
            hour = int(row["ts"][11:13])
            if result in ("ok", "duplicate"):
                summary["ok"] += 1
                summary["by_hour"][hour] += 1
            elif result == "rejected":
                summary["rejected"] += 1
            else:
                summary["error"] += 1
                summary["errors_by_hour"][hour] += 1
            label = row["target_label"]
            if label and result != "rejected":
                target = summary["by_target"].setdefault(
                    label, {"ok": 0, "error": 0, "last_ts": None, "last_message": None})
                target["ok" if result in ("ok", "duplicate") else "error"] += 1
                target["last_ts"] = row["ts"]
                target["last_message"] = row["message"]
        return summary

    def recent_ip_targets(self, days: int = 7) -> list[str]:
        since = (self.clock().date() - timedelta(days=days)).isoformat()
        rows = self._read(
            "SELECT DISTINCT target_label FROM events WHERE day >= ? AND target_kind = 'ip'",
            (since,),
        )
        return [r["target_label"] for r in rows if r["target_label"]]

    def recent_errors(self, minutes: int = 30) -> dict[str, int]:
        since = (self.clock() - timedelta(minutes=minutes)).astimezone().isoformat(timespec="milliseconds")
        rows = self._read(
            "SELECT target_label, COUNT(*) AS n FROM events WHERE ts >= ? AND result = 'error' "
            "AND kind IN ('print', 'cashbox') GROUP BY target_label",
            (since,),
        )
        return {r["target_label"]: r["n"] for r in rows}

    def last_rejected(self) -> dict | None:
        rows = self._read(
            "SELECT ts, origin, message FROM events WHERE result = 'rejected' ORDER BY ts DESC LIMIT 1")
        return dict(rows[0]) if rows else None

    # -- mantenimiento -------------------------------------------------
    def purge(self, retention_days: int) -> int:
        cutoff = (self.clock().date() - timedelta(days=retention_days)).isoformat()
        try:
            with contextlib.closing(self._connect()) as conn:
                deleted = conn.execute("DELETE FROM events WHERE day < ?", (cutoff,)).rowcount
                conn.commit()
            if deleted:
                with contextlib.closing(self._connect()) as conn:
                    conn.execute("VACUUM")
            return deleted
        except sqlite3.Error as exc:
            logger.error("No se pudo purgar la bitácora: %s", exc)
            return 0

    def delete_all(self) -> None:
        try:
            with contextlib.closing(self._connect()) as conn:
                conn.execute("DELETE FROM events")
                conn.commit()
            with contextlib.closing(self._connect()) as conn:
                conn.execute("VACUUM")
        except sqlite3.Error as exc:
            logger.error("No se pudo borrar la bitácora: %s", exc)

    def export_csv(self, day: date) -> str:
        rows = self._read(
            "SELECT id, ts, kind, document_type, target_label, target_kind, origin, client_ip, "
            "bytes, duration_ms, result, http_status, message, job_id, test_mode "
            "FROM events WHERE day = ? ORDER BY ts", (day.isoformat(),))
        out = io.StringIO()
        writer = csv.writer(out, delimiter=";", lineterminator="\r\n")
        writer.writerow(CSV_HEADER)
        for row in rows:
            values = list(row)
            values[1] = values[1][11:19]
            writer.writerow(values)
        return "﻿" + out.getvalue()

    def size_info(self) -> dict:
        size = 0
        for suffix in ("", "-wal"):
            path = Path(str(self.db_path) + suffix)
            if path.is_file():
                size += path.stat().st_size
        rows = self._read("SELECT COUNT(DISTINCT day) AS days FROM events")
        return {"bytes": size, "days": rows[0]["days"] if rows else 0}
