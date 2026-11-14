"""Crash-recoverable, idempotent ledger for Paper Trading operations."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import tempfile
import uuid
from paper_trading_limits import canonical_state_path, state_access_lock


class PaperOperationError(RuntimeError):
    """An operation is pending or storage state cannot be reconciled safely."""


class PaperOperationAuthorizationError(PaperOperationError):
    """BUY authorization was refused before any journal mutation."""


def _canonical_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _fsync_directory(path):
    # Directory handles are not flushable through the portable Python API on Windows.
    if os.name == "nt":
        return
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_write_bytes(path, payload, *, fsync_fn=os.fsync, replace_fn=os.replace):
    """Write, flush, replace, and verify bytes; this is not power-loss proof on Windows.

    Windows has no portable directory-fsync equivalent here. File contents are flushed,
    replacement is atomic to readers on the same volume, and bytes are read back, but
    directory-entry durability after sudden physical power loss is not guaranteed.
    """
    folder = os.path.dirname(os.path.abspath(path))
    os.makedirs(folder, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".paper-write-", suffix=".tmp", dir=folder)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(payload)
            output.flush()
            fsync_fn(output.fileno())
        replace_fn(temporary, path)
        _fsync_directory(folder)
        with open(path, "rb") as check:
            if check.read() != payload:
                raise PaperOperationError(f"verificacao pos-escrita falhou: {path}")
    except Exception:
        try:
            if os.path.exists(temporary):
                os.remove(temporary)
        except OSError:
            pass
        raise


def atomic_write_json(path, value):
    return atomic_write_bytes(path, (_canonical_json(value) + "\n").encode("utf-8"))


def reset_pending_path(state_path):
    """Path of a fail-closed reset marker for this state generation."""
    return canonical_state_path(state_path) + ".reset.pending.json"


def sync_state_in_place(target, source):
    """Apply a verified state snapshot without invalidating shared dict/list references."""
    for key in list(target):
        if key not in source:
            del target[key]
    for key, value in source.items():
        current = target.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            sync_state_in_place(current, value)
        elif isinstance(current, list) and isinstance(value, list):
            for index, item in enumerate(value):
                if index < len(current) and isinstance(current[index], dict) and isinstance(item, dict):
                    sync_state_in_place(current[index], item)
                elif index < len(current) and isinstance(current[index], list) and isinstance(item, list):
                    sync_state_in_place_list(current[index], item)
                elif index < len(current):
                    current[index] = json.loads(_canonical_json(item)) if isinstance(item, (dict, list)) else item
                else:
                    current.append(json.loads(_canonical_json(item)) if isinstance(item, (dict, list)) else item)
            del current[len(value):]
        else:
            target[key] = json.loads(_canonical_json(value)) if isinstance(value, (dict, list)) else value
    return target


def sync_state_in_place_list(target, source):
    for index, item in enumerate(source):
        if index < len(target) and isinstance(target[index], dict) and isinstance(item, dict):
            sync_state_in_place(target[index], item)
        elif index < len(target):
            target[index] = json.loads(_canonical_json(item)) if isinstance(item, (dict, list)) else item
        else:
            target.append(json.loads(_canonical_json(item)) if isinstance(item, (dict, list)) else item)
    del target[len(source):]
    return target


class PaperOperationStore:
    """Journal immutable intents and make state/CSV application idempotent.

    Completed intents and IDs are intentionally retained. Each intent includes a full
    post-operation state snapshot, so storage grows approximately with state size per
    Paper operation. No automatic pruning/compaction is performed.
    """

    ID_FIELD = "operation_id"

    def __init__(self, state_path, trades_path, trade_fields):
        self.state_path = canonical_state_path(state_path)
        self.trades_path = canonical_state_path(trades_path)
        self.trade_fields = list(dict.fromkeys([*trade_fields, self.ID_FIELD]))
        self.directory = self.state_path + ".paper_operations"
        self.error = None
        self._buy_guard = None
        self._buy_capability = None
        with state_access_lock(self.state_path):
            os.makedirs(self.directory, exist_ok=True)

    @classmethod
    def for_read_only_audit(cls, state_path, trades_path, trade_fields):
        """Build an auditor without creating the journal directory."""
        instance = cls.__new__(cls)
        instance.state_path = canonical_state_path(state_path)
        instance.trades_path = canonical_state_path(trades_path)
        instance.trade_fields = list(dict.fromkeys([*trade_fields, cls.ID_FIELD]))
        instance.directory = instance.state_path + ".paper_operations"
        instance.error = None
        instance._buy_guard = None
        instance._buy_capability = None
        return instance

    def bind_buy_guard(self, validator, capability):
        """Bind the live Executor as the sole authorized BUY commit path."""
        if not callable(validator) or capability is None:
            raise PaperOperationError("guard de compra inválido")
        if self._buy_guard is not None:
            raise PaperOperationError("guard de compra já associado")
        self._buy_guard, self._buy_capability = validator, capability

    @property
    def blocked(self):
        return self.error is not None or bool(self.pending_ids())

    def _intent_path(self, operation_id):
        return os.path.join(self.directory, f"{operation_id}.intent.json")

    def _done_path(self, operation_id):
        return os.path.join(self.directory, f"{operation_id}.done.json")

    @property
    def _csv_schema_path(self):
        return os.path.join(self.directory, "trades_csv_schema.json")

    def pending_ids(self):
        try:
            with state_access_lock(self.state_path):
                return [name[:-12] for name in os.listdir(self.directory)
                        if name.endswith(".intent.json")
                        and not os.path.exists(self._done_path(name[:-12]))]
        except OSError as exc:
            self.error = f"nao foi possivel ler o diario Paper: {exc}"
            return ["<journal-unavailable>"]
        except RuntimeError as exc:
            self.error = str(exc)
            return ["<state-locked>"]

    @staticmethod
    def _digest(payload):
        return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()

    def _read_state(self):
        with open(self.state_path, "r", encoding="utf-8") as source:
            state = json.load(source)
        if not isinstance(state, dict) or not isinstance(state.get("posicoes"), dict):
            raise PaperOperationError("estado.json tem estrutura invalida durante recuperacao")
        state.setdefault("paper_state_revision", 0)
        state.setdefault("paper_ops_applied", [])
        return state

    @staticmethod
    def _row_digest(raw_row):
        return hashlib.sha256(raw_row).hexdigest()

    @staticmethod
    def _csv_header_bytes(fields, *, bom=False):
        output = io.StringIO(newline="")
        csv.writer(output, lineterminator="\n").writerow(fields)
        encoded = output.getvalue().encode("utf-8")
        return (b"\xef\xbb\xbf" if bom else b"") + encoded

    @staticmethod
    def _csv_row_bytes(fields, row):
        output = io.StringIO(newline="")
        csv.writer(output, lineterminator="\n").writerow([row.get(field, "") for field in fields])
        return output.getvalue().encode("utf-8")

    def _read_csv_document(self):
        if not os.path.exists(self.trades_path):
            return list(self.trade_fields), [], b"", [], []
        try:
            with open(self.trades_path, "rb") as source:
                raw = source.read()
            has_bom = raw.startswith(b"\xef\xbb\xbf")
            text = raw.decode("utf-8-sig")
            stream = io.StringIO(text, newline="")
            reader = csv.reader(stream, strict=True)
            try:
                fields = next(reader, None)
            except csv.Error as exc:
                raise PaperOperationError(f"trades.csv malformado no cabecalho: {exc}") from exc
            if not fields:
                raise PaperOperationError("trades.csv tem cabecalho ausente")
            fields = list(fields)
            if len(fields) != len(set(fields)):
                raise PaperOperationError("trades.csv tem colunas duplicadas")
            header_end = stream.tell()
            prefix = b"\xef\xbb\xbf" if has_bom else b""
            header_bytes = prefix + text[:header_end].encode("utf-8")
            rows, raw_rows, row_lengths = [], [], []
            record_start = header_end
            while True:
                try:
                    row = next(reader)
                except StopIteration:
                    break
                except csv.Error as exc:
                    raise PaperOperationError(f"trades.csv malformado: {exc}") from exc
                record_end = stream.tell()
                raw_row = text[record_start:record_end].encode("utf-8")
                record_start = record_end
                rows.append(dict(zip(fields, row)))
                raw_rows.append(raw_row)
                row_lengths.append(len(row))

            manifest_path = self._csv_schema_path
            if os.path.isfile(manifest_path):
                try:
                    with open(manifest_path, "r", encoding="utf-8") as source:
                        manifest_record = json.load(source)
                    manifest = manifest_record.get("payload")
                    if (not isinstance(manifest, dict)
                            or manifest_record.get("sha256") != self._digest(manifest)):
                        raise PaperOperationError("manifesto de schema do trades.csv invalido")
                    previous_fields = manifest.get("previous_header_fields")
                    target_fields = manifest.get("target_header_fields")
                    previous_rows = manifest.get("previous_rows")
                    if (not isinstance(previous_fields, list) or not isinstance(target_fields, list)
                            or not isinstance(previous_rows, list)):
                        raise PaperOperationError("manifesto de schema do trades.csv incompleto")
                    header_digest = self._row_digest(header_bytes)
                    if fields == previous_fields and header_digest == manifest.get("previous_header_sha256"):
                        expected_rows = previous_rows
                        if len(raw_rows) != len(expected_rows):
                            raise PaperOperationError("trades.csv diverge do manifesto previo")
                        for index, (row_length, raw_row, expected) in enumerate(
                                zip(row_lengths, raw_rows, expected_rows), 2):
                            if (self._row_digest(raw_row) != expected.get("sha256")
                                    or row_length != expected.get("field_count")):
                                raise PaperOperationError(
                                    f"trades.csv linha legada alterada ou truncada: {index}"
                                )
                    elif fields == target_fields and header_digest == manifest.get("target_header_sha256"):
                        if not target_fields[:len(previous_fields)] == previous_fields:
                            raise PaperOperationError("schema moderno nao preserva a ordem das colunas antigas")
                        if len(raw_rows) < len(previous_rows):
                            raise PaperOperationError("trades.csv perdeu linhas anteriores ao upgrade")
                        for index, expected in enumerate(previous_rows):
                            row_length, raw_row = row_lengths[index], raw_rows[index]
                            if (self._row_digest(raw_row) != expected.get("sha256")
                                    or row_length != expected.get("field_count")):
                                raise PaperOperationError(
                                    f"trades.csv linha legada alterada ou truncada: {index + 2}"
                                )
                        for index in range(len(previous_rows), len(rows)):
                            if row_lengths[index] != len(fields):
                                raise PaperOperationError(
                                    f"trades.csv linha nova incompleta: {index + 2}"
                                )
                    else:
                        raise PaperOperationError("cabecalho trades.csv nao corresponde ao manifesto de schema")
                except (OSError, json.JSONDecodeError) as exc:
                    raise PaperOperationError(f"nao foi possivel validar o manifesto do trades.csv: {exc}") from exc
            elif any(length != len(fields) for length in row_lengths):
                bad_index = next(index for index, length in enumerate(row_lengths, 2) if length != len(fields))
                raise PaperOperationError(
                    f"trades.csv linha {bad_index} incompleta: "
                    f"{row_lengths[bad_index - 2]} campos, esperado {len(fields)}"
                )
            return fields, rows, header_bytes, raw_rows, row_lengths
        except csv.Error as exc:
            raise PaperOperationError(f"trades.csv malformado: {exc}") from exc

    def _read_csv(self):
        fields, rows, _header, _raw_rows, _row_lengths = self._read_csv_document()
        return fields, rows

    def _normalized_trade_row(self, row, fields):
        result = {field: "" if row.get(field, "") is None else str(row.get(field, ""))
                  for field in fields}
        symbol = result.get("simbolo", "")
        if symbol[:1] in ("=", "+", "-", "@"):
            result["simbolo"] = "'" + symbol
        return result

    def _ensure_trade(self, expected):
        old_fields, rows, header_bytes, raw_rows, _row_lengths = self._read_csv_document()
        fields = list(old_fields)
        if self.ID_FIELD not in fields:
            fields.append(self.ID_FIELD)
        for field in self.trade_fields:
            if field not in fields:
                fields.append(field)
        matching = [row for row in rows if row.get(self.ID_FIELD) == expected[self.ID_FIELD]]
        if len(matching) > 1:
            raise PaperOperationError(f"operation_id duplicado em trades.csv: {expected[self.ID_FIELD]}")
        normalized_expected = self._normalized_trade_row(expected, fields)
        if matching:
            normalized_existing = self._normalized_trade_row(matching[0], fields)
            if normalized_existing != normalized_expected:
                raise PaperOperationError(f"linha CSV diverge do diario: {expected[self.ID_FIELD]}")
            return
        rows.append(normalized_expected)
        self._replace_csv(fields, rows, raw_rows, header_bytes, old_fields)

    def _write_csv_schema_manifest(self, old_fields, fields, header_bytes, rows, raw_rows):
        if not raw_rows:
            return
        if fields[:len(old_fields)] != old_fields:
            raise PaperOperationError("upgrade do CSV tentaria reordenar colunas existentes")
        payload = {
            "version": 1,
            "previous_header_fields": list(old_fields),
            "previous_header_sha256": self._row_digest(header_bytes),
            "previous_rows": [
                {"sha256": self._row_digest(raw_row), "field_count": len(row)}
                for row, raw_row in zip(rows, raw_rows)
            ],
            "target_header_fields": list(fields),
            "target_header_sha256": self._row_digest(
                self._csv_header_bytes(fields, bom=header_bytes.startswith(b"\xef\xbb\xbf"))),
        }
        atomic_write_json(self._csv_schema_path, {"payload": payload, "sha256": self._digest(payload)})

    def _replace_csv(self, fields, rows, raw_rows, header_bytes, old_fields):
        if len(rows) != len(raw_rows) + 1:
            raise PaperOperationError("append CSV inesperado; recusado para preservar linhas existentes")
        if raw_rows and not raw_rows[-1].endswith((b"\n", b"\r")):
            raise PaperOperationError("ultima linha trades.csv sem terminador; append seguro impossivel")
        if fields != old_fields:
            if raw_rows:
                self._write_csv_schema_manifest(old_fields, fields, header_bytes, rows[:-1], raw_rows)
            header = self._csv_header_bytes(fields, bom=header_bytes.startswith(b"\xef\xbb\xbf"))
        else:
            header = header_bytes or self._csv_header_bytes(fields)
            if not header.endswith((b"\n", b"\r")):
                header += b"\n"
        payload = header + b"".join(raw_rows) + self._csv_row_bytes(fields, rows[-1])
        atomic_write_bytes(self.trades_path, payload)
        saved_fields, saved_rows, _saved_header, saved_raw_rows, _saved_lengths = self._read_csv_document()
        if saved_fields != fields:
            raise PaperOperationError("cabecalho de trades.csv diverge apos substituicao")
        if saved_raw_rows[:len(raw_rows)] != raw_rows:
            raise PaperOperationError("linhas anteriores de trades.csv mudaram durante append")
        if len(saved_rows) != len(rows):
            raise PaperOperationError("contagem de trades.csv diverge apos substituicao")
        ids = [row.get(self.ID_FIELD) for row in saved_rows]
        populated_ids = [value for value in ids if value]
        if len(populated_ids) != len(set(populated_ids)):
            raise PaperOperationError("operation_id duplicado apos substituicao de trades.csv")

    def _write_state(self, state):
        atomic_write_json(self.state_path, state)
        return self._read_state()

    def _write_done(self, operation_id, digest):
        marker = {"operation_id": operation_id, "intent_sha256": digest, "status": "completed"}
        atomic_write_json(self._done_path(operation_id), marker)

    @staticmethod
    def _transaction_projection(state):
        """Fields changed by a Paper transaction, excluding live market marks."""
        position_fields = (
            "mint", "simbolo", "par", "url", "preco_entrada", "preco_efetivo",
            "tokens", "custo", "liq_entrada", "aberta_em", "pontuacao",
        )
        stats_fields = (
            "fechados", "ganhos", "perdas", "pnl_total", "custos_total",
            "melhor", "pior", "por_motivo",
        )
        positions = state.get("posicoes")
        stats = state.get("stats")
        if not isinstance(positions, dict) or not isinstance(stats, dict):
            raise PaperOperationError("projecao financeira invalida no estado Paper")
        return {
            "saldo": state.get("saldo"),
            "posicoes": {
                mint: {key: position[key] for key in position_fields if key in position}
                for mint, position in sorted(positions.items()) if isinstance(position, dict)
            },
            "stats": {key: stats[key] for key in stats_fields if key in stats},
        }

    def _audit_locked(self):
        """Validate all durable projections without writing or recovering anything."""
        state_exists = os.path.exists(self.state_path)
        disk_state = self._read_state() if state_exists else None
        fields, rows = self._read_csv()
        names = os.listdir(self.directory) if os.path.isdir(self.directory) else []
        intent_names = [name for name in names if name.endswith(".intent.json")]
        done_names = [name for name in names if name.endswith(".done.json")]
        intent_ids = {name[:-12] for name in intent_names}
        done_ids = {name[:-10] for name in done_names}
        orphan_done = sorted(done_ids - intent_ids)
        if orphan_done:
            raise PaperOperationError(f"marcadores sem intent correspondente: {orphan_done}")

        records = []
        for name in intent_names:
            operation_id, payload, digest = self._load_intent(os.path.join(self.directory, name))
            if name != f"{operation_id}.intent.json":
                raise PaperOperationError(f"nome de intent inconsistente: {name}")
            try:
                base = int(payload["base_revision"])
                target_revision = int(payload["target_revision"])
            except (KeyError, TypeError, ValueError) as exc:
                raise PaperOperationError(f"revisao invalida no intent {operation_id}") from exc
            if base < 0 or target_revision != base + 1:
                raise PaperOperationError(f"sequencia de revisao invalida no intent {operation_id}")
            state_after = payload.get("state_after")
            expected_row = payload.get("trade_row")
            if not isinstance(state_after, dict) or not isinstance(expected_row, dict):
                raise PaperOperationError(f"snapshot/linha ausente no intent {operation_id}")
            metadata = payload.get("metadata", {})
            if not isinstance(metadata, dict):
                raise PaperOperationError(f"metadata inválida no intent {operation_id}")
            if (state_after.get("paper_state_revision") != target_revision
                    or not isinstance(state_after.get("paper_ops_applied"), list)
                    or state_after["paper_ops_applied"].count(operation_id) != 1):
                raise PaperOperationError(f"estado futuro inconsistente no intent {operation_id}")
            if expected_row.get(self.ID_FIELD) != operation_id:
                raise PaperOperationError(f"linha CSV sem operation_id coerente: {operation_id}")
            type_map = {"BUY": "COMPRA", "SELL": "VENDA", "CANCEL": "ANULADA"}
            if (payload.get("operation_type") not in type_map
                    or expected_row.get("tipo") != type_map[payload["operation_type"]]):
                raise PaperOperationError(f"tipo de operacao/linha divergente: {operation_id}")
            records.append({
                "id": operation_id, "payload": payload, "digest": digest,
                "base": base, "target": target_revision,
                "done": f"{operation_id}.done.json" in done_names,
                "operation_type": payload.get("operation_type"),
                "metadata": payload.get("metadata", {}),
                "trade_row": expected_row,
            })

        records.sort(key=lambda record: record["base"])
        for index, record in enumerate(records):
            if record["base"] != index:
                raise PaperOperationError("revisoes do diario ausentes, duplicadas ou fora de ordem")
            expected_applied = [item["id"] for item in records[:index + 1]]
            if record["payload"]["state_after"].get("paper_ops_applied") != expected_applied:
                raise PaperOperationError(f"ledger de IDs inconsistente no intent {record['id']}")
            if index < len(records) - 1 and not record["done"]:
                raise PaperOperationError("existe intent pendente antes de outra operacao")

        csv_ids = {}
        for row in rows:
            operation_id = row.get(self.ID_FIELD, "")
            if operation_id:
                if operation_id in csv_ids:
                    raise PaperOperationError(f"operation_id duplicado em trades.csv: {operation_id}")
                csv_ids[operation_id] = row
        record_by_id = {record["id"]: record for record in records}
        unknown_csv_ids = sorted(set(csv_ids) - set(record_by_id))
        if unknown_csv_ids:
            raise PaperOperationError(f"operation_id sem intent correspondente no CSV: {unknown_csv_ids}")

        for record in records:
            operation_id = record["id"]
            matches = [row for row in rows if row.get(self.ID_FIELD) == operation_id]
            if len(matches) > 1:
                raise PaperOperationError(f"operation_id duplicado em trades.csv: {operation_id}")
            expected_row = record["payload"]["trade_row"]
            if matches:
                missing_fields = sorted(set(expected_row) - set(fields))
                if missing_fields:
                    raise PaperOperationError(
                        f"cabecalho CSV sem campos da operacao {operation_id}: {missing_fields}"
                    )
                normalized = self._normalized_trade_row(expected_row, fields)
                actual = self._normalized_trade_row(matches[0], fields)
                if actual != normalized:
                    raise PaperOperationError(f"linha CSV diverge do intent: {operation_id}")
            elif record["done"]:
                raise PaperOperationError(f"operacao concluida sem linha CSV: {operation_id}")

        pending = [record for record in records if not record["done"]]
        if disk_state is None:
            if records:
                raise PaperOperationError("diario existe mas estado.json esta ausente")
            revision, applied = 0, []
        else:
            revision = disk_state.get("paper_state_revision", 0)
            applied = disk_state.get("paper_ops_applied", [])
            if (isinstance(revision, bool) or not isinstance(revision, int) or revision < 0
                    or not isinstance(applied, list)
                    or any(not isinstance(item, str) for item in applied)):
                raise PaperOperationError("revisao/ledger invalidos em estado.json")
            if len(applied) != len(set(applied)):
                raise PaperOperationError("operation_id duplicado em estado.json")
            if records and ("paper_state_revision" not in disk_state or "paper_ops_applied" not in disk_state):
                raise PaperOperationError("campos do diario ausentes em estado.json moderno")
            if not records and (revision != 0 or applied):
                raise PaperOperationError("estado referencia operacoes sem diario correspondente")
            known_ids = {record["id"] for record in records}
            if set(applied) - known_ids:
                raise PaperOperationError("estado contem operation_id sem intent correspondente")
            expected_applied = [record["id"] for record in records if record["target"] <= revision]
            if applied != expected_applied:
                raise PaperOperationError("estado e diario divergem na lista/revisao de operacoes aplicadas")
            expected_revision = max((record["target"] for record in records if record["id"] in applied), default=0)
            if revision != expected_revision:
                raise PaperOperationError("revisao de estado nao corresponde ao diario")
            for record in records:
                operation_id = record["id"]
                if record["done"]:
                    marker_path = os.path.join(self.directory, f"{operation_id}.done.json")
                    try:
                        with open(marker_path, "r", encoding="utf-8") as source:
                            marker = json.load(source)
                    except (OSError, json.JSONDecodeError) as exc:
                        raise PaperOperationError(f"marcador de conclusao ilegivel: {operation_id}") from exc
                    if (marker.get("operation_id") != operation_id
                            or marker.get("intent_sha256") != record["digest"]
                            or marker.get("status") != "completed"):
                        raise PaperOperationError(f"marcador de conclusao inconsistente: {operation_id}")
                    if applied.count(operation_id) != 1:
                        raise PaperOperationError(f"estado nao confirma exatamente uma aplicacao: {operation_id}")
                else:
                    in_state = operation_id in applied
                    if in_state:
                        if revision != record["target"]:
                            raise PaperOperationError(f"estado ambiguo para intent aplicado: {operation_id}")
                    elif revision != record["base"]:
                        raise PaperOperationError(f"estado ambiguo para intent pendente: {operation_id}")

            if applied:
                latest = next(record for record in reversed(records) if record["id"] == applied[-1])
                expected_projection = self._transaction_projection(latest["payload"]["state_after"])
                actual_projection = self._transaction_projection(disk_state)
                if actual_projection != expected_projection:
                    raise PaperOperationError(
                        f"projecao financeira diverge da ultima operacao {latest['id']}"
                    )

        return {"state": disk_state, "records": records, "pending": pending,
                "csv_fields": fields, "csv_rows": rows}

    def audit(self):
        """Read-only validation used before reset and for diagnostics."""
        try:
            with state_access_lock(self.state_path):
                result = self._audit_locked()
            self.error = None
            return {"operation_count": len(result["records"]),
                    "pending_ids": [record["id"] for record in result["pending"]],
                    "state_revision": (result["state"] or {}).get("paper_state_revision", 0),
                    "operations": [{"operation_id": record["id"],
                                    "operation_type": record["operation_type"],
                                    "metadata": record["metadata"],
                                    "trade_row": record["trade_row"]}
                                   for record in result["records"] if record["done"]]}
        except Exception as exc:
            self.error = str(exc)
            if isinstance(exc, PaperOperationError):
                raise
            raise PaperOperationError(self.error) from exc

    def _load_intent(self, path):
        with open(path, "r", encoding="utf-8") as source:
            record = json.load(source)
        payload = record.get("payload")
        if not isinstance(payload, dict) or record.get("sha256") != self._digest(payload):
            raise PaperOperationError(f"intencao corrompida: {os.path.basename(path)}")
        if not record.get("operation_id") or payload.get("operation_id") != record["operation_id"]:
            raise PaperOperationError(f"identificador inconsistente: {os.path.basename(path)}")
        return record["operation_id"], payload, record["sha256"]

    def recover(self, in_memory_state=None):
        try:
            with state_access_lock(self.state_path):
                return self._recover_locked(in_memory_state)
        except Exception as exc:
            self.error = str(exc)
            if isinstance(exc, PaperOperationError):
                raise
            raise PaperOperationError(self.error) from exc

    def _recover_locked(self, in_memory_state=None):
        """Validate completed operations, then complete only safe pending intents."""
        try:
            audit = self._audit_locked()
            disk_state = audit["state"]
            if in_memory_state is not None:
                state = json.loads(_canonical_json(in_memory_state))
            elif disk_state is not None:
                state = disk_state
            else:
                raise PaperOperationError("estado.json ausente durante recuperacao")
            state.setdefault("paper_state_revision", 0)
            state.setdefault("paper_ops_applied", [])

            for record in audit["pending"]:
                operation_id = record["id"]
                payload = record["payload"]
                digest = record["digest"]
                # Always base replay/no-replay on the durable state, never a stale object.
                persisted = self._read_state() if os.path.exists(self.state_path) else None
                if persisted is None:
                    raise PaperOperationError("estado.json ausente para intent pendente")
                revision = persisted.get("paper_state_revision", 0)
                applied = persisted.get("paper_ops_applied", [])
                if operation_id in applied:
                    if (revision != record["target"]
                            or self._transaction_projection(persisted)
                            != self._transaction_projection(payload["state_after"])):
                        raise PaperOperationError(f"estado aplicado diverge do intent: {operation_id}")
                else:
                    if revision != record["base"]:
                        raise PaperOperationError(
                            f"estado ambiguo para {operation_id}: revisao {revision}, esperada {record['base']}"
                        )
                    persisted = self._write_state(payload["state_after"])
                self._ensure_trade(payload["trade_row"])
                verified = self._read_state()
                if (verified.get("paper_state_revision") != record["target"]
                        or verified.get("paper_ops_applied", []).count(operation_id) != 1):
                    raise PaperOperationError(f"operacao nao confirmada no estado: {operation_id}")
                self._write_done(operation_id, digest)
                state = verified

            final_audit = self._audit_locked()
            if final_audit["pending"]:
                raise PaperOperationError("permanecem intents pendentes apos recuperacao")
            if in_memory_state is not None:
                if audit["pending"]:
                    sync_state_in_place(in_memory_state, final_audit["state"])
                else:
                    # Keep unrelated in-memory scanner/market updates when no replay was needed.
                    # Their transaction projection was still checked against the durable state.
                    pass
                in_memory_state.setdefault("paper_state_revision", 0)
                in_memory_state.setdefault("paper_ops_applied", [])
                state = in_memory_state
            self.error = None
            return state if in_memory_state is None or not audit["pending"] else in_memory_state
        except Exception as exc:
            self.error = str(exc)
            raise PaperOperationError(self.error) from exc

    def commit(self, current_state, candidate_state, trade_row, operation_type, *,
               authorization=None, metadata=None):
        try:
            with state_access_lock(self.state_path):
                return self._commit_locked(current_state, candidate_state, trade_row, operation_type,
                                           authorization, metadata)
        except Exception as exc:
            if not isinstance(exc, PaperOperationAuthorizationError):
                self.error = str(exc)
            if isinstance(exc, PaperOperationError):
                raise
            raise PaperOperationError(self.error) from exc

    def _commit_locked(self, current_state, candidate_state, trade_row, operation_type,
                       authorization=None, metadata=None):
        """Persist one absolute state snapshot and its CSV row under the instance lock."""
        self.recover(current_state)
        if self.blocked:
            raise PaperOperationError(self.error or "existem operacoes Paper pendentes")
        if operation_type not in ("BUY", "SELL", "CANCEL"):
            raise PaperOperationError("tipo de operacao Paper desconhecido")
        if operation_type == "BUY":
            if (self._buy_guard is None or not isinstance(authorization, dict)
                    or authorization.get("capability") is not self._buy_capability
                    or not self._buy_guard(authorization, current_state, candidate_state, trade_row)):
                raise PaperOperationAuthorizationError("BUY recusado: autorização do Executor ausente ou inválida")

        operation_id = uuid.uuid4().hex
        _fields, _rows, _header, raw_rows, _row_lengths = self._read_csv_document()
        if raw_rows and not raw_rows[-1].endswith((b"\n", b"\r")):
            raise PaperOperationError(
                "ultima linha trades.csv sem terminador; operacao recusada antes de alterar estado"
            )
        base_revision = int(current_state.get("paper_state_revision", 0))
        candidate = json.loads(_canonical_json(candidate_state))
        candidate.setdefault("paper_ops_applied", list(current_state.get("paper_ops_applied", [])))
        candidate["paper_ops_applied"] = list(candidate["paper_ops_applied"]) + [operation_id]
        candidate["paper_state_revision"] = base_revision + 1
        row = dict(trade_row)
        row[self.ID_FIELD] = operation_id
        normalized_row = self._normalized_trade_row(row, self.trade_fields)
        payload = {
            "operation_id": operation_id,
            "operation_type": str(operation_type),
            "base_revision": base_revision,
            "target_revision": base_revision + 1,
            "state_after": candidate,
            "trade_row": normalized_row,
            "metadata": dict(metadata or {}),
        }
        digest = self._digest(payload)
        atomic_write_json(self._intent_path(operation_id), {"operation_id": operation_id,
                                                           "payload": payload, "sha256": digest})
        try:
            state = self._write_state(candidate)
            sync_state_in_place(current_state, state)
            self._ensure_trade(normalized_row)
            # Verify both durable projections before the intent is considered complete.
            if operation_id not in self._read_state().get("paper_ops_applied", []):
                raise PaperOperationError("estado nao confirmou a operacao")
            self._write_done(operation_id, digest)
            self.error = None
            return operation_id
        except Exception as exc:
            self.error = str(exc)
            # Reconcile in-memory state with whichever atomic state file survived.
            try:
                persisted = self._read_state()
                sync_state_in_place(current_state, persisted)
            except Exception:
                pass
            raise PaperOperationError(self.error) from exc
