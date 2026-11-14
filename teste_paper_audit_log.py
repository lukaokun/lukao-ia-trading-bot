import json
import hashlib
import os
import tempfile
import unittest
from pathlib import Path

from paper_audit_log import PaperAuditError, PaperAuditJournal


class PaperAuditJournalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = os.path.join(self.temp.name, "paper_audit.jsonl")

    def test_append_reopen_sequence_and_hash_chain(self):
        journal = PaperAuditJournal(self.path)
        first = journal.append("proposal_created", {"proposal_id": "p-1", "mint": "mint-1"})
        second = journal.append("manual_confirmation_accepted", {"proposal_id": "p-1"})
        self.assertNotEqual(first, second)
        reopened = PaperAuditJournal(self.path)
        report = reopened.verify()
        self.assertEqual(report["count"], 2)
        self.assertEqual([event["sequence"] for event in report["events"]], [1, 2])
        self.assertEqual(report["events"][1]["previous_sha256"], first)

    def test_corruption_blocks_append_and_preserves_original_bytes(self):
        journal = PaperAuditJournal(self.path)
        journal.append("proposal_created", {"proposal_id": "p-1"})
        original = Path(self.path).read_bytes()
        Path(self.path).write_bytes(original.replace(b"proposal_created", b"proposal_xreated"))
        corrupt = Path(self.path).read_bytes()
        reopened = PaperAuditJournal(self.path)
        self.assertTrue(reopened.blocked)
        with self.assertRaises(PaperAuditError):
            reopened.append("proposal_rejected", {"proposal_id": "p-1"})
        self.assertEqual(Path(self.path).read_bytes(), corrupt)

    def test_truncated_tail_blocks_without_automatic_truncation(self):
        journal = PaperAuditJournal(self.path)
        journal.append("proposal_created", {"proposal_id": "p-1"})
        with open(self.path, "ab") as output:
            output.write(b'{"sequence":2')
        original = Path(self.path).read_bytes()
        reopened = PaperAuditJournal(self.path)
        self.assertTrue(reopened.blocked)
        self.assertIn("truncado", reopened.error)
        self.assertEqual(Path(self.path).read_bytes(), original)

    def test_secret_fields_are_rejected(self):
        journal = PaperAuditJournal(self.path)
        with self.assertRaises(PaperAuditError):
            journal.append("proposal_created", {"confirmation_code": "never-store-this"})
        self.assertFalse(Path(self.path).exists())

    def test_reason_redacts_confirmation_code_shape(self):
        journal = PaperAuditJournal(self.path)
        journal.append("emergency_stop_activated", {
            "reason": "operador 0123456789ABCDEF0123456789ABCDEF pediu paragem",
            "stop_id": "stop-1",
        })
        payload = Path(self.path).read_text(encoding="utf-8")
        self.assertNotIn("0123456789ABCDEF0123456789ABCDEF", payload)
        self.assertIn("[redigido]", payload)

    def test_valid_hash_with_unknown_event_schema_is_rejected(self):
        journal = PaperAuditJournal(self.path)
        journal.append("proposal_created", {"proposal_id": "p-1"})
        record = json.loads(Path(self.path).read_text(encoding="utf-8"))
        record.pop("sha256")
        record["event"] = "unapproved_event"
        canonical = json.dumps(record, ensure_ascii=True, sort_keys=True,
                               separators=(",", ":"), allow_nan=False).encode("utf-8")
        record["sha256"] = hashlib.sha256(canonical).hexdigest()
        Path(self.path).write_text(json.dumps(record, ensure_ascii=True, sort_keys=True,
                                              separators=(",", ":")) + "\n", encoding="utf-8")
        damaged = Path(self.path).read_bytes()
        reopened = PaperAuditJournal(self.path)
        self.assertTrue(reopened.blocked)
        self.assertIn("campos do evento", reopened.error)
        self.assertEqual(Path(self.path).read_bytes(), damaged)

    def test_audit_failure_is_latched_until_reopen(self):
        journal = PaperAuditJournal(self.path, fsync_fn=lambda _fd: (_ for _ in ()).throw(OSError("fsync")))
        with self.assertRaises(PaperAuditError):
            journal.append("proposal_created", {"proposal_id": "p-1"})
        with self.assertRaises(PaperAuditError):
            journal.verify()
        self.assertTrue(journal.blocked)
        reopened = PaperAuditJournal(self.path)
        self.assertFalse(reopened.blocked)

    def test_fsync_failure_fails_closed_and_reopen_verifies_written_record(self):
        def fail_fsync(_fd):
            raise OSError("simulated fsync failure")
        journal = PaperAuditJournal(self.path, fsync_fn=fail_fsync)
        with self.assertRaises(PaperAuditError):
            journal.append("execution_validation_passed", {"proposal_id": "p-1"})
        self.assertTrue(journal.blocked)
        # The bytes may already have reached the filesystem; reopening verifies rather
        # than deleting or replaying the record.
        reopened = PaperAuditJournal(self.path)
        if Path(self.path).exists():
            self.assertFalse(reopened.blocked)
            self.assertEqual(reopened.verify()["count"], 1)


if __name__ == "__main__":
    unittest.main()
