"""Non-blocking, observational audit trail for Paper Trading risk evaluations."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import queue
import sqlite3
import threading
import time
import uuid


log = logging.getLogger(__name__)
_STOP = object()


class SmartProtectionObservability:
    """Writes immutable risk-evaluation records on a bounded background queue.

    Queue overflow and database errors never block the caller. No history is
    deleted automatically. The caller supplies only data already in memory.
    """

    def __init__(self, db_path="smart_protection_observability.db", max_queue_size=5000):
        self.db_path = str(Path(db_path))
        self._queue = queue.Queue(maxsize=max(1, int(max_queue_size)))
        self._lock = threading.Lock()
        self._closed = False
        self._stop_enqueued = False
        self._stop_inserting = False
        self._dropped = 0
        self._write_errors = 0
        self._enqueued = 0
        self._written = 0
        self._failed_events = 0
        self._in_flight = 0
        self._thread = threading.Thread(target=self._worker, name="smart-protection-db", daemon=True)
        self._thread.start()

    @staticmethod
    def _connect(path):
        db = sqlite3.connect(path, timeout=5)
        db.execute("PRAGMA busy_timeout=5000")
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA journal_mode=WAL")
        db.executescript("""
            CREATE TABLE IF NOT EXISTS risk_evaluations (
                event_id TEXT PRIMARY KEY,
                evaluated_at_utc TEXT NOT NULL,
                mint TEXT NOT NULL,
                position_id TEXT,
                entry_price REAL,
                observed_price REAL,
                entry_liquidity_usd REAL,
                observed_liquidity_usd REAL,
                entry_cost_usd REAL,
                token_quantity REAL,
                position_value_usd REAL,
                pair_address TEXT,
                entry_pair_address TEXT,
                previous_pair_address TEXT,
                data_source TEXT,
                source_timestamp_utc TEXT,
                observation_age_seconds REAL,
                rpc_slot INTEGER,
                missing_fields TEXT NOT NULL,
                invalid_fields TEXT NOT NULL,
                quality_status TEXT NOT NULL,
                freshness_status TEXT NOT NULL,
                pair_changed INTEGER NOT NULL,
                data_status TEXT NOT NULL,
                priority_condition TEXT,
                exit_reason TEXT,
                sem_dados_count INTEGER
            );
            CREATE TABLE IF NOT EXISTS risk_conditions (
                condition_id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT NOT NULL,
                condition_name TEXT NOT NULL,
                observed_value REAL,
                raw_observed_value REAL,
                threshold_value REAL,
                result TEXT NOT NULL CHECK(result IN
                    ('triggered','not_triggered','unavailable','not_evaluated')),
                evaluation_order INTEGER,
                reason TEXT,
                FOREIGN KEY(event_id) REFERENCES risk_evaluations(event_id)
                    ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_risk_eval_mint_time
                ON risk_evaluations(mint, evaluated_at_utc DESC);
            CREATE INDEX IF NOT EXISTS idx_risk_eval_position_time
                ON risk_evaluations(position_id, evaluated_at_utc DESC);
            CREATE INDEX IF NOT EXISTS idx_risk_eval_time
                ON risk_evaluations(evaluated_at_utc DESC);
            CREATE INDEX IF NOT EXISTS idx_risk_condition_event_order
                ON risk_conditions(event_id, evaluation_order);
        """)
        return db

    def record_evaluation(self, event):
        """Enqueue one event; returns False and counts it if unavailable/full."""
        if not isinstance(event, dict):
            return False
        with self._lock:
            if self._closed:
                self._dropped += 1
                return False
            try:
                self._queue.put_nowait(event)
                self._enqueued += 1
                return True
            except queue.Full:
                self._dropped += 1
                return False

    def _worker(self):
        db = None
        try:
            db = self._connect(self.db_path)
        except Exception:
            with self._lock:
                self._write_errors += 1
            log.exception("Não foi possível inicializar a base de observabilidade Smart Protection")
        try:
            while True:
                item = self._queue.get()
                try:
                    if item is _STOP:
                        return
                    with self._lock:
                        self._in_flight += 1
                    if db is None:
                        with self._lock:
                            self._write_errors += 1
                            self._failed_events += 1
                        continue
                    try:
                        self._write_event(db, item)
                        with self._lock:
                            self._written += 1
                    except Exception:
                        with self._lock:
                            self._write_errors += 1
                            self._failed_events += 1
                        log.exception("Falha ao gravar evento de observabilidade Smart Protection")
                        try:
                            db.rollback()
                        except Exception:
                            with self._lock:
                                self._write_errors += 1
                            log.exception("Falha ao reverter a escrita SQLite Smart Protection")
                finally:
                    if item is not _STOP:
                        with self._lock:
                            self._in_flight -= 1
                    self._queue.task_done()
        finally:
            if db is not None:
                try:
                    db.close()
                except Exception:
                    with self._lock:
                        self._write_errors += 1
                    log.exception("Falha ao fechar a ligação SQLite Smart Protection")

    @staticmethod
    def _write_event(db, event):
        event_id = str(event.get("event_id") or uuid.uuid4())
        evaluated_at = event.get("evaluated_at_utc")
        if not evaluated_at:
            evaluated_at = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        fields = (
            "mint", "position_id", "entry_price", "observed_price",
            "entry_liquidity_usd", "observed_liquidity_usd", "entry_cost_usd",
            "token_quantity", "position_value_usd", "pair_address", "entry_pair_address",
            "previous_pair_address",
            "data_source", "source_timestamp_utc", "observation_age_seconds", "rpc_slot",
        )
        values = [event.get(name) for name in fields]
        if not values[0]:
            raise ValueError("evento sem mint")
        db.execute("""INSERT INTO risk_evaluations (
            event_id,evaluated_at_utc,mint,position_id,entry_price,observed_price,
            entry_liquidity_usd,observed_liquidity_usd,entry_cost_usd,token_quantity,
            position_value_usd,pair_address,entry_pair_address,previous_pair_address,data_source,
            source_timestamp_utc,observation_age_seconds,rpc_slot,missing_fields,invalid_fields,
            quality_status,freshness_status,pair_changed,data_status,priority_condition,
            exit_reason,sem_dados_count)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            [event_id, evaluated_at, *values,
             json.dumps(event.get("missing_fields", []), ensure_ascii=False),
             json.dumps(event.get("invalid_fields", []), ensure_ascii=False),
             event.get("quality_status", "unknown"), event.get("freshness_status", "unknown"),
             int(bool(event.get("pair_changed"))), event.get("data_status", "unknown"),
             event.get("priority_condition"), event.get("exit_reason"), event.get("sem_dados_count")])
        for condition in event.get("conditions", []):
            db.execute("""INSERT INTO risk_conditions (
                event_id,condition_name,observed_value,raw_observed_value,threshold_value,
                result,evaluation_order,reason) VALUES (?,?,?,?,?,?,?,?)""",
                (event_id, condition.get("condition_name"), condition.get("observed_value"),
                 condition.get("raw_observed_value"), condition.get("threshold_value"),
                 condition.get("result", "unavailable"), condition.get("evaluation_order"),
                 condition.get("reason")))
        db.commit()

    def metrics(self):
        with self._lock:
            queued = max(0, self._queue.qsize() - int(self._stop_enqueued))
            return {"queued": queued, "pending_events": queued + self._in_flight,
                    "enqueued": self._enqueued, "written": self._written,
                    "failed_events": self._failed_events, "dropped": self._dropped,
                    "write_errors": self._write_errors, "worker_alive": self._thread.is_alive(),
                    "stop_enqueued": self._stop_enqueued}

    def note_caller_error(self):
        with self._lock:
            self._write_errors += 1

    def close(self, timeout=30):
        """Stop accepting events, drain the queue and wait for the worker boundedly."""
        deadline = time.monotonic() + max(0.0, float(timeout))
        with self._lock:
            self._closed = True
            insert_stop = (self._thread.is_alive() and not self._stop_enqueued
                           and not self._stop_inserting)
            if insert_stop:
                self._stop_inserting = True

        if insert_stop:
            try:
                remaining = max(0.0, deadline - time.monotonic())
                self._queue.put(_STOP, timeout=remaining)
                with self._lock:
                    self._stop_enqueued = True
                    self._stop_inserting = False
            except queue.Full:
                with self._lock:
                    self._stop_inserting = False

        self._thread.join(max(0.0, deadline - time.monotonic()))
        stats = self.metrics()
        if (stats["worker_alive"] or stats["pending_events"] or stats["failed_events"]
                or stats["dropped"] or stats["write_errors"]):
            log.error(
                "Smart Protection close incompleto: eventos pendentes=%d, escritos=%d, "
                "falhados=%d, descartados=%d, erros=%d, worker_ativo=%s",
                stats["pending_events"], stats["written"], stats["failed_events"],
                stats["dropped"], stats["write_errors"], stats["worker_alive"],
            )
            return False
        return True



__all__ = ["SmartProtectionObservability"]
