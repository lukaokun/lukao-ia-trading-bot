import json
import tempfile
import unittest
from pathlib import Path
from wallet_monitor import WalletMonitor, load_watchlist
from solana_test_utils import test_pubkey

WALLET = test_pubkey(20)


class FakePipeline:
    def __init__(self):
        self.seen = []
        self.unavailable = []
        self.failed = []
    def ingest_transaction(self, tx, wallet_filter=None, **kwargs):
        self.seen.append((tx, wallet_filter))
        return [{"signature": tx["sig"], "wallet": next(iter(wallet_filter))}]
    def record_unavailable(self, **kwargs):
        self.unavailable.append(kwargs)
    def record_failed(self, **kwargs):
        self.failed.append(kwargs)


class FakeRPC:
    def __init__(self):
        self.calls = []
    def call(self, method, params):
        self.calls.append((method, params))
        if method == "getSignaturesForAddress":
            return [{"signature": "new2", "err": None},
                    {"signature": "new1", "err": None}]
        if method == "getTransaction":
            return {"sig": params[0]}
        raise AssertionError(method)


class WalletMonitorTests(unittest.TestCase):
    def test_poll_ingests_in_chronological_order_and_saves_cursor(self):
        with tempfile.TemporaryDirectory() as d:
            path = str(Path(d) / "state.json")
            pipe, rpc = FakePipeline(), FakeRPC()
            monitor = WalletMonitor(pipe, rpc, path, pause_seconds=0)
            result = monitor.poll_wallet(WALLET)
            self.assertEqual([x[0]["sig"] for x in pipe.seen], ["new1", "new2"])
            self.assertEqual(result["swaps_ingested"], 2)
            self.assertEqual(json.loads(Path(path).read_text())[WALLET], "new2")

    def test_uses_until_cursor_on_next_poll(self):
        with tempfile.TemporaryDirectory() as d:
            path = str(Path(d) / "state.json")
            Path(path).write_text(json.dumps({WALLET: "old"}))
            rpc = FakeRPC()
            monitor = WalletMonitor(FakePipeline(), rpc, path, pause_seconds=0)
            monitor.poll_wallet(WALLET)
            cfg = rpc.calls[0][1][1]
            self.assertEqual(cfg["until"], "old")


    def test_does_not_advance_cursor_when_rpc_cannot_return_transaction(self):
        class MissingTxRPC(FakeRPC):
            def call(self, method, params):
                if method == "getTransaction":
                    return None
                return super().call(method, params)
        with tempfile.TemporaryDirectory() as d:
            path = str(Path(d) / "state.json")
            rpc = MissingTxRPC()
            pipeline = FakePipeline()
            monitor = WalletMonitor(pipeline, rpc, path, pause_seconds=0)
            monitor.poll_wallet(WALLET)
            self.assertFalse(Path(path).exists())
            self.assertEqual(len(pipeline.unavailable), 1)
            self.assertEqual(pipeline.unavailable[0]["signature"], "new1")

    def test_failed_signature_is_audited_without_ingestion(self):
        class FailedRPC(FakeRPC):
            def call(self, method, params):
                if method == "getSignaturesForAddress":
                    return [{"signature": "failed-sig", "err": {"Custom": 1}}]
                return super().call(method, params)
        with tempfile.TemporaryDirectory() as d:
            path = str(Path(d) / "state.json")
            pipeline, rpc = FakePipeline(), FailedRPC()
            monitor = WalletMonitor(pipeline, rpc, path, pause_seconds=0)
            result = monitor.poll_wallet(WALLET)
            self.assertEqual(result["swaps_ingested"], 0)
            self.assertEqual(pipeline.seen, [])
            self.assertEqual(len(pipeline.failed), 1)
            self.assertEqual(pipeline.failed[0]["signature"], "failed-sig")

    def test_watchlist_deduplicates(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "watch.json"
            path.write_text(json.dumps({"wallets": ["a", "b", "a", "", 3]}))
            path.write_text(json.dumps({"wallets": [test_pubkey(21), "invalid", test_pubkey(21)]}))
            self.assertEqual(load_watchlist(str(path)), [test_pubkey(21)])


if __name__ == "__main__":
    unittest.main()
