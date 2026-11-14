import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import bot
from smart_protection_observability import SmartProtectionObservability


def event(event_id, mint="MintTest111"):
    return {
        "event_id": event_id,
        "evaluated_at_utc": "2026-11-13T12:00:00.000Z",
        "mint": mint,
        "position_id": "paper-position-1",
        "entry_price": 0.01,
        "observed_price": 0.012,
        "entry_cost_usd": 12.0,
        "token_quantity": 1000.0,
        "data_source": "fixture",
        "missing_fields": [],
        "invalid_fields": [],
        "quality_status": "valid",
        "freshness_status": "fresh",
        "data_status": "valid",
        "conditions": [{
            "condition_name": "stop_loss",
            "observed_value": 20.0,
            "threshold_value": -6.0,
            "result": "not_triggered",
            "evaluation_order": 1,
            "reason": "fixture",
        }],
    }


class SmartProtectionObservabilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "observability.db"
        self.store = SmartProtectionObservability(self.path)
        self.release_events = []

    def tearDown(self):
        for release in self.release_events:
            release.set()
        if self.store.metrics()["worker_alive"]:
            self.store.close(timeout=2)
        self.temp.cleanup()

    def rows(self):
        db = sqlite3.connect(self.path)
        try:
            records = db.execute(
                "SELECT event_id,mint,observed_price,quality_status FROM risk_evaluations ORDER BY event_id"
            ).fetchall()
            conditions = db.execute(
                "SELECT condition_name,result,evaluation_order FROM risk_conditions ORDER BY condition_id"
            ).fetchall()
        finally:
            db.close()
        return records, conditions

    def test_event_and_conditions_are_persisted_and_readable(self):
        self.assertTrue(self.store.record_evaluation(event("event-read")))
        self.assertTrue(self.store.close(timeout=2))
        records, conditions = self.rows()
        self.assertEqual(records, [("event-read", "MintTest111", 0.012, "valid")])
        self.assertEqual(conditions, [("stop_loss", "not_triggered", 1)])
        self.assertEqual(self.store.metrics()["written"], 1)

    def test_normal_close_with_empty_queue_closes_worker_and_connection(self):
        self.assertTrue(self.store.close(timeout=2))
        self.assertFalse(self.store.metrics()["worker_alive"])
        self.assertEqual(self.store.metrics()["pending_events"], 0)
        # Reopening the database proves the worker released its SQLite connection.
        db = sqlite3.connect(self.path)
        try:
            self.assertIsNotNone(db.execute("SELECT name FROM sqlite_master WHERE name='risk_evaluations'").fetchone())
        finally:
            db.close()

    def test_normal_close_drains_events_waiting_in_queue(self):
        for i in range(5):
            self.assertTrue(self.store.record_evaluation(event(f"event-{i}")))
        self.assertTrue(self.store.close(timeout=3))
        metrics = self.store.metrics()
        self.assertEqual(metrics["written"], 5)
        self.assertEqual(metrics["pending_events"], 0)
        self.assertEqual(len(self.rows()[0]), 5)

    def test_timeout_reports_pending_work_and_later_close_can_finish(self):
        started, release = threading.Event(), threading.Event()
        self.release_events.append(release)
        original = SmartProtectionObservability._write_event

        def paused_write(db, record):
            started.set()
            release.wait(3)
            return original(db, record)

        with patch.object(SmartProtectionObservability, "_write_event", side_effect=paused_write):
            self.assertTrue(self.store.record_evaluation(event("event-blocked")))
            self.assertTrue(started.wait(2))
            self.assertTrue(self.store.record_evaluation(event("event-queued")))
            t0 = time.monotonic()
            with self.assertLogs("smart_protection_observability", level="ERROR") as captured:
                self.assertFalse(self.store.close(timeout=0.05))
            self.assertIn("eventos pendentes=2", "\n".join(captured.output))
            self.assertLess(time.monotonic() - t0, 0.5)
            metrics = self.store.metrics()
            self.assertTrue(metrics["worker_alive"])
            self.assertGreaterEqual(metrics["pending_events"], 1)
            release.set()
            self.assertTrue(self.store.close(timeout=3))

        metrics = self.store.metrics()
        self.assertEqual(metrics["written"], 2)
        self.assertEqual(metrics["pending_events"], 0)
        self.assertFalse(metrics["worker_alive"])
        self.assertEqual(len(self.rows()[0]), 2)

    def test_write_error_rolls_back_and_counts_unwritten_event(self):
        original = SmartProtectionObservability._write_event

        def fail_after_parent_insert(db, record):
            if record["event_id"] == "event-invalid-condition":
                broken = dict(record, conditions=[dict(record["conditions"][0], result="invalid")])
                return original(db, broken)
            return original(db, record)

        with patch.object(SmartProtectionObservability, "_write_event", side_effect=fail_after_parent_insert):
            with self.assertLogs("smart_protection_observability", level="ERROR"):
                self.assertTrue(self.store.record_evaluation(event("event-invalid-condition")))
                self.assertTrue(self.store.record_evaluation(event("event-after-error")))
                self.assertFalse(self.store.close(timeout=3))

        metrics = self.store.metrics()
        self.assertEqual(metrics["failed_events"], 1)
        self.assertEqual(metrics["write_errors"], 1)
        self.assertEqual(metrics["written"], 1)
        records, conditions = self.rows()
        self.assertEqual(records, [("event-after-error", "MintTest111", 0.012, "valid")])
        self.assertEqual(conditions, [("stop_loss", "not_triggered", 1)])

    def test_repeated_close_is_idempotent_and_does_not_reopen_worker(self):
        self.assertTrue(self.store.record_evaluation(event("event-close-twice")))
        self.assertTrue(self.store.close(timeout=2))
        self.assertTrue(self.store.close(timeout=2))
        self.assertFalse(self.store.metrics()["worker_alive"])
        self.assertEqual(self.store.metrics()["written"], 1)
        self.assertFalse(self.store.record_evaluation(event("after-close")))
        self.assertEqual(self.store.metrics()["dropped"], 1)

    def test_bot_logs_incomplete_close_without_claiming_all_events_were_written(self):
        class FakeObservability:
            def close(self):
                return False

            def metrics(self):
                return {"written": 3, "pending_events": 1, "failed_events": 0,
                        "dropped": 1, "write_errors": 0, "worker_alive": True}

        instance = object.__new__(bot.Bot)
        instance.smart_protection_observability = FakeObservability()
        with self.assertLogs(bot.log, level="ERROR") as captured:
            instance._fechar_observabilidade()
        message = "\n".join(captured.output)
        self.assertIn("Encerramento Smart Protection incompleto", message)
        self.assertNotIn("encerrada sem pendências", message)

    def test_full_queue_drops_without_blocking_and_close_can_retry_stop(self):
        self.store.close(timeout=2)
        self.store = SmartProtectionObservability(self.path, max_queue_size=1)
        started, release = threading.Event(), threading.Event()
        self.release_events.append(release)
        original = SmartProtectionObservability._write_event

        def paused_write(db, record):
            if record["event_id"] == "event-in-flight":
                started.set()
                release.wait(3)
            return original(db, record)

        with patch.object(SmartProtectionObservability, "_write_event", side_effect=paused_write):
            self.assertTrue(self.store.record_evaluation(event("event-in-flight")))
            self.assertTrue(started.wait(2))
            self.assertTrue(self.store.record_evaluation(event("event-queued")))
            t0 = time.monotonic()
            self.assertFalse(self.store.record_evaluation(event("event-dropped")))
            self.assertLess(time.monotonic() - t0, 0.1)
            self.assertEqual(self.store.metrics()["dropped"], 1)
            with self.assertLogs("smart_protection_observability", level="ERROR"):
                self.assertFalse(self.store.close(timeout=0.05))
            release.set()
            with self.assertLogs("smart_protection_observability", level="ERROR"):
                self.assertFalse(self.store.close(timeout=3))

        metrics = self.store.metrics()
        self.assertEqual(metrics["written"], 2)
        self.assertEqual(metrics["dropped"], 1)
        self.assertEqual(metrics["pending_events"], 0)
        self.assertFalse(metrics["worker_alive"])
        self.assertEqual(len(self.rows()[0]), 2)


if __name__ == "__main__":
    unittest.main()
