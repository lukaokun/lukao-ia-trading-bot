import csv
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import paper_operations as operations
from paper_operations import PaperOperationError, PaperOperationStore, atomic_write_bytes, atomic_write_json


FIELDS = ["data_hora", "tipo", "simbolo", "mint", "preco_mercado", "preco_efetivo",
          "valor_usd", "tokens", "custos_usd", "pnl_usd", "pnl_pct", "motivo",
          "pontuacao", "duracao_min", "saldo_depois"]


def state_fixture():
    return {
        "saldo": 100.0, "saldo_inicial": 100.0, "posicoes": {},
        "stats": {"fechados": 0}, "paper_state_revision": 0, "paper_ops_applied": [],
    }


def buy_candidate(state):
    candidate = json.loads(json.dumps(state))
    candidate["saldo"] = 95.0
    candidate["posicoes"]["mint-a"] = {"mint": "mint-a", "custo": 5.0}
    row = {"data_hora": "2026-11-13 12:00:00", "tipo": "COMPRA", "simbolo": "AAA",
           "mint": "mint-a", "valor_usd": "5.0000", "saldo_depois": "95.0000"}
    return candidate, row

def _test_commit(store, current, candidate, row, operation_type):
    if operation_type == "BUY":
        capability = getattr(store, "_test_buy_capability", None)
        if capability is None:
            capability = object()
            store._test_buy_capability = capability
            store.bind_buy_guard(
                lambda auth, _current, _candidate, _row: auth.get("capability") is capability
                and auth.get("manual_confirmation_consumed") is True,
                capability,
            )
        authorization = {"capability": capability, "manual_confirmation_consumed": True,
                         "proposal_id": "isolated-test-proposal", "execution_plan": "{}",
                         "plan_fields": {}}
        return store.commit(current, candidate, row, operation_type, authorization=authorization,
                            metadata={"proposal_id": "isolated-test-proposal"})
    return store.commit(current, candidate, row, operation_type)



class PaperOperationStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state_path = os.path.join(self.temp.name, "estado.json")
        self.csv_path = os.path.join(self.temp.name, "trades.csv")
        self.store = PaperOperationStore(self.state_path, self.csv_path, FIELDS)
        self.state = state_fixture()
        atomic_write_json(self.state_path, self.state)

    def read_state(self):
        return json.loads(Path(self.state_path).read_text(encoding="utf-8"))

    def csv_rows(self):
        with open(self.csv_path, newline="", encoding="utf-8-sig") as source:
            reader = csv.DictReader(source)
            return reader.fieldnames, list(reader)

    def test_generic_buy_commit_without_executor_authorization_is_rejected(self):
        candidate, row = buy_candidate(self.state)
        before = Path(self.state_path).read_bytes()
        with self.assertRaisesRegex(PaperOperationError, "autorização do Executor"):
            self.store.commit(self.state, candidate, row, "BUY")
        self.assertFalse(self.store.blocked)
        self.assertEqual(Path(self.state_path).read_bytes(), before)
        self.assertFalse(Path(self.csv_path).exists())
        self.assertFalse(any(name.endswith(".intent.json")
                             for name in os.listdir(self.store.directory)))

    def test_buy_transaction_and_recovery_are_idempotent_across_restarts(self):
        candidate, row = buy_candidate(self.state)
        operation_id = _test_commit(self.store, self.state, candidate, row, "BUY")
        self.assertEqual(self.state["saldo"], 95.0)
        self.assertEqual(self.state["posicoes"]["mint-a"]["custo"], 5.0)
        fields, rows = self.csv_rows()
        self.assertEqual(fields[-1], "operation_id")
        self.assertEqual([r["operation_id"] for r in rows], [operation_id])

        for _ in range(4):
            restarted = PaperOperationStore(self.state_path, self.csv_path, FIELDS)
            loaded = self.read_state()
            restarted.recover(loaded)
            fields, rows = self.csv_rows()
            self.assertEqual(loaded["saldo"], 95.0)
            self.assertEqual(loaded["paper_ops_applied"].count(operation_id), 1)
            self.assertEqual([r["operation_id"] for r in rows], [operation_id])

    def test_legacy_csv_rows_and_order_survive_header_upgrade(self):
        with open(self.csv_path, "w", newline="", encoding="utf-8") as output:
            writer = csv.DictWriter(output, fieldnames=FIELDS)
            writer.writeheader()
            writer.writerow({"tipo": "COMPRA", "simbolo": "OLD1", "mint": "mint-1", "valor_usd": "12.00"})
            writer.writerow({"tipo": "VENDA", "simbolo": "OLD2", "mint": "mint-2", "valor_usd": "8.50"})
        before = Path(self.csv_path).read_bytes().splitlines(keepends=True)
        old_data_lines = before[1:]
        candidate, row = buy_candidate(self.state)
        _test_commit(self.store, self.state, candidate, row, "BUY")
        after = Path(self.csv_path).read_bytes().splitlines(keepends=True)
        self.assertEqual(after[1:1 + len(old_data_lines)], old_data_lines)
        fields, rows = self.csv_rows()
        self.assertEqual([r["simbolo"] for r in rows], ["OLD1", "OLD2", "AAA"])
        self.assertEqual([r["valor_usd"] for r in rows], ["12.00", "8.50", "5.0000"])
        self.assertEqual([r["mint"] for r in rows], ["mint-1", "mint-2", "mint-a"])
        self.assertEqual(fields[-1], "operation_id")
        self.assertIsNone(rows[0]["operation_id"])
        self.assertIsNone(rows[1]["operation_id"])

    def test_legacy_bytes_survive_mixed_rows_recovery_and_reopen(self):
        with open(self.csv_path, "wb") as output:
            output.write((",".join(FIELDS) + "\r\n").encode("utf-8"))
            output.write(b'old,COMPRA,"OLD,ONE",mint-old,0.01,0.01,12.00,100,0.1,,,legacy,70,,88.00\r\n')
            output.write(b'old2,VENDA,OLD2,mint-old2,0.02,0.02,8.50,50,0.2,1.5,17.6,legacy,60,,96.50\r\n')
        before = Path(self.csv_path).read_bytes().splitlines(keepends=True)
        legacy_lines = before[1:]
        candidate, row = buy_candidate(self.state)
        operation_id = _test_commit(self.store, self.state, candidate, row, "BUY")
        current = Path(self.csv_path).read_bytes().splitlines(keepends=True)
        self.assertEqual(current[1:1 + len(legacy_lines)], legacy_lines)
        with open(self.csv_path, newline="", encoding="utf-8-sig") as source:
            self.assertEqual(len(list(csv.DictReader(source))), 3)
        self.assertEqual(current[-1].decode("utf-8").rstrip("\r\n").split(",")[-1], operation_id)

        for _ in range(3):
            reopened = PaperOperationStore(self.state_path, self.csv_path, FIELDS)
            reopened.recover(self.read_state())
            self.assertEqual(Path(self.csv_path).read_bytes().splitlines(keepends=True)[1:1 + len(legacy_lines)],
                             legacy_lines)
            self.assertEqual(self.csv_rows()[1][-1]["operation_id"], operation_id)

    def test_legacy_manifest_rejects_changed_or_truncated_legacy_and_new_rows(self):
        with open(self.csv_path, "w", newline="", encoding="utf-8") as output:
            writer = csv.DictWriter(output, fieldnames=FIELDS, lineterminator="\n")
            writer.writeheader()
            writer.writerow({"tipo": "COMPRA", "simbolo": "OLD", "mint": "mint-old", "valor_usd": "12.00"})
        candidate, row = buy_candidate(self.state)
        _test_commit(self.store, self.state, candidate, row, "BUY")
        complete_bytes = Path(self.csv_path).read_bytes()
        complete = complete_bytes.splitlines(keepends=True)

        for label, lines in (
            ("legacy", complete[:]),
            ("new", complete[:]),
        ):
            with self.subTest(label=label):
                if label == "legacy":
                    lines[1] = lines[1][:-2] + b"\n"
                else:
                    lines[-1] = lines[-1].rstrip(b"\r\n").rsplit(b",", 1)[0] + b"\n"
                corrupted = b"".join(lines)
                Path(self.csv_path).write_bytes(corrupted)
                with self.assertRaises(PaperOperationError):
                    PaperOperationStore(self.state_path, self.csv_path, FIELDS)._read_csv()
                self.assertEqual(Path(self.csv_path).read_bytes(), corrupted)
                Path(self.csv_path).write_bytes(complete_bytes)

    def test_legacy_schema_upgrade_failures_recover_without_duplicate_and_keep_bytes(self):
        real_write = operations.atomic_write_bytes
        for fail_at in (3, 4, 5):  # schema manifest, CSV replace, completion marker
            with self.subTest(fail_at=fail_at):
                folder = os.path.join(self.temp.name, f"legacy-stage-{fail_at}")
                os.makedirs(folder)
                state_path = os.path.join(folder, "estado.json")
                csv_path = os.path.join(folder, "trades.csv")
                base = state_fixture()
                atomic_write_json(state_path, base)
                with open(csv_path, "wb") as output:
                    output.write((",".join(FIELDS) + "\r\n").encode("utf-8"))
                    output.write(b"old,COMPRA,OLD,mint-old,0.01,0.01,12.00,100,0.1,,,legacy,70,,88.00\r\n")
                old_data = Path(csv_path).read_bytes().splitlines(keepends=True)[1:]
                store = PaperOperationStore(state_path, csv_path, FIELDS)
                candidate, row = buy_candidate(base)
                counter = {"n": 0}

                def fail_stage(path, payload, **kwargs):
                    counter["n"] += 1
                    if counter["n"] == fail_at:
                        raise OSError(f"simulated CSV migration interruption {fail_at}")
                    return real_write(path, payload, **kwargs)

                with patch.object(operations, "atomic_write_bytes", side_effect=fail_stage):
                    with self.assertRaises(PaperOperationError):
                        _test_commit(store, base, candidate, row, "BUY")
                recovered = PaperOperationStore(state_path, csv_path, FIELDS)
                loaded = json.loads(Path(state_path).read_text(encoding="utf-8"))
                recovered.recover(loaded)
                recovered.recover(loaded)
                data_lines = Path(csv_path).read_bytes().splitlines(keepends=True)
                self.assertEqual(data_lines[1:1 + len(old_data)], old_data)
                fields, rows = self.csv_rows_at(csv_path)
                self.assertEqual(len(rows), 2)
                self.assertEqual(rows[-1]["operation_id"], loaded["paper_ops_applied"][-1])

    @staticmethod
    def csv_rows_at(path):
        with open(path, newline="", encoding="utf-8-sig") as source:
            reader = csv.DictReader(source)
            return reader.fieldnames, list(reader)

    def test_every_commit_write_boundary_recovers_without_duplicate(self):
        real_write = operations.atomic_write_bytes
        for fail_at in range(1, 5):
            with self.subTest(fail_at=fail_at):
                folder = os.path.join(self.temp.name, f"stage-{fail_at}")
                os.makedirs(folder)
                state_path = os.path.join(folder, "estado.json")
                csv_path = os.path.join(folder, "trades.csv")
                base = state_fixture()
                atomic_write_json(state_path, base)
                store = PaperOperationStore(state_path, csv_path, FIELDS)
                candidate, row = buy_candidate(base)
                counter = {"n": 0}

                def fail_before_write(path, payload, **kwargs):
                    counter["n"] += 1
                    if counter["n"] == fail_at:
                        raise OSError(f"injected write boundary {fail_at}")
                    return real_write(path, payload, **kwargs)

                with patch.object(operations, "atomic_write_bytes", side_effect=fail_before_write):
                    with self.assertRaises(Exception):
                        _test_commit(store, base, candidate, row, "BUY")
                if fail_at == 1:
                    # No durable intent means no operation was applied.
                    self.assertEqual(json.loads(Path(state_path).read_text())["saldo"], 100.0)
                    self.assertFalse(os.path.exists(csv_path))
                    continue

                for _ in range(3):
                    recovered = PaperOperationStore(state_path, csv_path, FIELDS)
                    loaded = json.loads(Path(state_path).read_text(encoding="utf-8"))
                    recovered.recover(loaded)
                    self.assertEqual(loaded["saldo"], 95.0)
                with open(csv_path, newline="", encoding="utf-8-sig") as source:
                    rows = list(csv.DictReader(source))
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["tipo"], "COMPRA")

    def test_failure_before_replace_or_during_fsync_preserves_previous_file(self):
        target = os.path.join(self.temp.name, "atomic.txt")
        Path(target).write_bytes(b"old")
        with self.assertRaises(OSError):
            atomic_write_bytes(target, b"new", fsync_fn=lambda _fd: (_ for _ in ()).throw(OSError("fsync")))
        self.assertEqual(Path(target).read_bytes(), b"old")
        with self.assertRaises(OSError):
            atomic_write_bytes(target, b"new", replace_fn=lambda *_: (_ for _ in ()).throw(OSError("replace")))
        self.assertEqual(Path(target).read_bytes(), b"old")

    def test_failure_after_replace_is_recovered_as_applied_once(self):
        real_write = operations.atomic_write_bytes
        counter = {"n": 0}

        def fail_after_replace(path, payload, **kwargs):
            counter["n"] += 1
            result = real_write(path, payload, **kwargs)
            if counter["n"] == 3:
                raise OSError("crash after CSV replacement")
            return result

        candidate, row = buy_candidate(self.state)
        with patch.object(operations, "atomic_write_bytes", side_effect=fail_after_replace):
            with self.assertRaises(PaperOperationError):
                _test_commit(self.store, self.state, candidate, row, "BUY")
        recovered = PaperOperationStore(self.state_path, self.csv_path, FIELDS)
        loaded = self.read_state()
        recovered.recover(loaded)
        recovered.recover(loaded)
        _, rows = self.csv_rows()
        self.assertEqual(loaded["saldo"], 95.0)
        self.assertEqual(len(rows), 1)

    def test_completed_marker_with_restored_state_blocks_without_replay(self):
        candidate, row = buy_candidate(self.state)
        _test_commit(self.store, self.state, candidate, row, "BUY")
        completed_csv = Path(self.csv_path).read_bytes()
        legacy_state_bytes = json.dumps(state_fixture(), sort_keys=True, separators=(",", ":"), allow_nan=False)
        Path(self.state_path).write_text(legacy_state_bytes + "\n", encoding="utf-8")
        restored_state = Path(self.state_path).read_bytes()

        with self.assertRaises(PaperOperationError):
            PaperOperationStore(self.state_path, self.csv_path, FIELDS).recover(
                json.loads(restored_state.decode("utf-8"))
            )

        self.assertEqual(Path(self.state_path).read_bytes(), restored_state)
        self.assertEqual(Path(self.csv_path).read_bytes(), completed_csv)

    def test_completed_marker_with_restored_csv_blocks_without_rewrite(self):
        candidate, row = buy_candidate(self.state)
        _test_commit(self.store, self.state, candidate, row, "BUY")
        completed_state = Path(self.state_path).read_bytes()
        restored_csv = (",".join(FIELDS) + "\n").encode("utf-8")
        Path(self.csv_path).write_bytes(restored_csv)

        with self.assertRaises(PaperOperationError):
            PaperOperationStore(self.state_path, self.csv_path, FIELDS).recover(self.read_state())

        self.assertEqual(Path(self.state_path).read_bytes(), completed_state)
        self.assertEqual(Path(self.csv_path).read_bytes(), restored_csv)

    def test_same_operation_id_with_different_csv_values_blocks(self):
        candidate, row = buy_candidate(self.state)
        _test_commit(self.store, self.state, candidate, row, "BUY")
        state_bytes = Path(self.state_path).read_bytes()
        fields, rows = self.csv_rows()
        rows[0]["valor_usd"] = "4.9999"
        with open(self.csv_path, "w", newline="", encoding="utf-8") as output:
            writer = csv.DictWriter(output, fieldnames=fields, lineterminator="\n")
            writer.writeheader()
            writer.writerows(rows)
        divergent_csv = Path(self.csv_path).read_bytes()

        with self.assertRaises(PaperOperationError):
            PaperOperationStore(self.state_path, self.csv_path, FIELDS).recover(self.read_state())

        self.assertEqual(Path(self.state_path).read_bytes(), state_bytes)
        self.assertEqual(Path(self.csv_path).read_bytes(), divergent_csv)

    def test_csv_short_truncated_blank_and_malformed_rows_are_rejected_read_only(self):
        invalid_rows = (
            "2026-11-13,COMPRA,AAA,mint-a",
            "2026-11-13,COMPRA,AAA,mint-a,0.01,0.01,5,500,0.1,0,0,pontuacao,90,0,95,EXTRA",
            "2026-11-13,COMPRA,\"AAA,mint-a",
            "",
        )
        header = ",".join(FIELDS) + "\n"
        for index, invalid in enumerate(invalid_rows):
            with self.subTest(index=index):
                path = os.path.join(self.temp.name, f"bad-{index}.csv")
                contents = (header + invalid + "\n").encode("utf-8")
                Path(path).write_bytes(contents)
                store = PaperOperationStore.for_read_only_audit(self.state_path, path, FIELDS)
                with self.assertRaises(PaperOperationError):
                    store._read_csv()
                self.assertEqual(Path(path).read_bytes(), contents)

    def test_sell_and_cancel_recovery_are_idempotent_after_repeated_restarts(self):
        for operation, trade_type in (("SELL", "VENDA"), ("CANCEL", "ANULADA")):
            with self.subTest(operation=operation):
                folder = os.path.join(self.temp.name, f"repeat-{operation}")
                os.makedirs(folder)
                state_path = os.path.join(folder, "estado.json")
                csv_path = os.path.join(folder, "trades.csv")
                base = state_fixture()
                base["saldo"] = 95.0
                base["posicoes"] = {"mint-a": {"mint": "mint-a", "simbolo": "AAA",
                                                  "custo": 5.0, "tokens": 500.0,
                                                  "preco_entrada": 0.01, "preco_efetivo": 0.01}}
                atomic_write_json(state_path, base)
                candidate = json.loads(json.dumps(base))
                candidate["saldo"] = 100.0
                candidate["posicoes"] = {}
                row = {"data_hora": "2026-11-13 12:00:00", "tipo": trade_type,
                       "simbolo": "AAA", "mint": "mint-a", "valor_usd": "5.0000",
                       "custos_usd": "0.0000", "pnl_usd": "0.0000", "saldo_depois": "100.0000"}
                store = PaperOperationStore(state_path, csv_path, FIELDS)
                operation_id = _test_commit(store, base, candidate, row, operation)

                for _ in range(5):
                    loaded = json.loads(Path(state_path).read_text(encoding="utf-8"))
                    PaperOperationStore(state_path, csv_path, FIELDS).recover(loaded)
                    self.assertEqual(loaded["saldo"], 100.0)
                    self.assertEqual(loaded["posicoes"], {})
                    self.assertEqual(loaded["paper_ops_applied"].count(operation_id), 1)
                    with open(csv_path, newline="", encoding="utf-8-sig") as source:
                        rows = list(csv.DictReader(source))
                    self.assertEqual(len(rows), 1)
                    self.assertEqual(rows[0]["operation_id"], operation_id)
                    self.assertEqual(rows[0]["tipo"], trade_type)

    def test_process_restarts_recover_buy_sell_cancel_once(self):
        real_write = operations.atomic_write_bytes
        script = (
            "import sys; from paper_operations import PaperOperationStore; "
            f"fields={FIELDS!r}; p,t=sys.argv[1:]; "
            "s=PaperOperationStore(p,t,fields); s.recover()"
        )
        env = dict(os.environ)
        env["PYTHONPATH"] = os.path.dirname(os.path.abspath(__file__)) + os.pathsep + env.get("PYTHONPATH", "")

        for operation, trade_type in (("BUY", "COMPRA"), ("SELL", "VENDA"), ("CANCEL", "ANULADA")):
            with self.subTest(operation=operation):
                folder = os.path.join(self.temp.name, f"process-restart-{operation}")
                os.makedirs(folder)
                state_path = os.path.join(folder, "estado.json")
                csv_path = os.path.join(folder, "trades.csv")
                base = state_fixture()
                if operation != "BUY":
                    base["saldo"] = 95.0
                    base["posicoes"] = {"mint-a": {"mint": "mint-a", "simbolo": "AAA",
                                                      "custo": 5.0, "tokens": 500.0,
                                                      "preco_entrada": 0.01, "preco_efetivo": 0.01}}
                atomic_write_json(state_path, base)
                candidate = json.loads(json.dumps(base))
                if operation == "BUY":
                    candidate["saldo"] = 95.0
                    candidate["posicoes"]["mint-a"] = {"mint": "mint-a", "simbolo": "AAA",
                                                        "custo": 5.0, "tokens": 500.0,
                                                        "preco_entrada": 0.01, "preco_efetivo": 0.01}
                else:
                    candidate["saldo"] = 100.0
                    candidate["posicoes"] = {}
                row = {"data_hora": "fixture", "tipo": trade_type, "simbolo": "AAA",
                       "mint": "mint-a", "valor_usd": "5.0000", "custos_usd": "0.0000",
                       "pnl_usd": "0.0000", "saldo_depois": f"{candidate['saldo']:.4f}"}
                store = PaperOperationStore(state_path, csv_path, FIELDS)
                counter = {"n": 0}

                def fail_csv_write(path, payload, **kwargs):
                    counter["n"] += 1
                    if counter["n"] == 3:
                        raise OSError("simulated interruption before CSV replace")
                    return real_write(path, payload, **kwargs)

                with patch.object(operations, "atomic_write_bytes", side_effect=fail_csv_write):
                    with self.assertRaises(PaperOperationError):
                        _test_commit(store, base, candidate, row, operation)

                for _ in range(3):
                    result = subprocess.run(
                        [sys.executable, "-c", script, state_path, csv_path],
                        cwd=os.path.dirname(os.path.abspath(__file__)), env=env,
                        capture_output=True, text=True, timeout=8,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)

                final = json.loads(Path(state_path).read_text(encoding="utf-8"))
                with open(csv_path, newline="", encoding="utf-8-sig") as source:
                    rows = list(csv.DictReader(source))
                self.assertEqual(final["saldo"], candidate["saldo"])
                self.assertEqual(final["posicoes"], candidate["posicoes"])
                self.assertEqual(final["paper_state_revision"], 1)
                self.assertEqual(len(final["paper_ops_applied"]), 1)
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["tipo"], trade_type)

    def test_ambiguous_revision_blocks_recovery_without_rewriting_state(self):
        real_write = operations.atomic_write_bytes
        counter = {"n": 0}

        def fail_state(path, payload, **kwargs):
            counter["n"] += 1
            if counter["n"] == 2:
                raise OSError("state unavailable")
            return real_write(path, payload, **kwargs)

        candidate, row = buy_candidate(self.state)
        with patch.object(operations, "atomic_write_bytes", side_effect=fail_state):
            with self.assertRaises(Exception):
                _test_commit(self.store, self.state, candidate, row, "BUY")
        state = self.read_state()
        state["paper_state_revision"] = 99
        atomic_write_json(self.state_path, state)
        with self.assertRaises(PaperOperationError):
            PaperOperationStore(self.state_path, self.csv_path, FIELDS).recover(state)
        self.assertEqual(state["saldo"], 100.0)

    def test_completed_marker_with_state_projection_tamper_blocks_read_only(self):
        candidate, row = buy_candidate(self.state)
        _test_commit(self.store, self.state, candidate, row, "BUY")
        state = self.read_state()
        state["saldo"] = 96.0
        atomic_write_json(self.state_path, state)
        tampered_state = Path(self.state_path).read_bytes()
        csv_before = Path(self.csv_path).read_bytes()

        with self.assertRaises(PaperOperationError):
            PaperOperationStore(self.state_path, self.csv_path, FIELDS).recover(self.read_state())

        self.assertEqual(Path(self.state_path).read_bytes(), tampered_state)
        self.assertEqual(Path(self.csv_path).read_bytes(), csv_before)


if __name__ == "__main__":
    unittest.main()
