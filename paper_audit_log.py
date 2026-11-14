"""Append-only hash-chained audit journal for Paper proposal lifecycle events."""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import uuid
from datetime import datetime, timezone


class PaperAuditError(RuntimeError):
    """The audit journal is unavailable or its hash chain is invalid."""


_GENESIS = "0" * 64
_ALLOWED_EVENTS = {
    "proposal_created", "manual_confirmation_accepted", "execution_validation_passed",
    "execution_committed", "execution_blocked", "proposal_expired", "proposal_rejected",
    "emergency_stop_activated", "emergency_stop_rearmed",
}
_ALLOWED_FIELDS = {
    "proposal_id", "operation_id", "mint", "pair_address", "amount_usd",
    "price_usd", "reason", "source", "stop_id", "active",
}
_TOP_LEVEL_FIELDS = {
    "sequence", "event_id", "occurred_at_utc", "event", "previous_sha256", "fields", "sha256",
}
_SENSITIVE_TEXT = re.compile(
    r"(?i)\b(?:bearer\s+\S+|api[_-]?key\s*[:=]\s*\S+|secret\s*[:=]\s*\S+|"
    r"private[_-]?key\s*[:=]\s*\S+)\b|\b[A-F0-9]{32,}\b"
)


def sanitize_audit_text(value, limit=240):
    """Bound audit text and redact common credentials/approval-code shapes."""
    return _SENSITIVE_TEXT.sub("[redigido]", str(value or "")[:limit])


def _canonical(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


class PaperAuditJournal:
    def __init__(self, path, *, fsync_fn=os.fsync):
        self.path = os.path.abspath(path)
        self._fsync = fsync_fn
        self._lock = threading.RLock()
        self._blocked = False
        self._error = None
        try:
            self.verify()
        except PaperAuditError:
            # Preserve the damaged file and expose a fail-closed object to the caller.
            pass

    @property
    def blocked(self):
        return self._blocked

    @property
    def error(self):
        return self._error

    def block(self, reason):
        with self._lock:
            self._blocked = True
            self._error = str(reason)

    def _read_verify_locked(self):
        try:
            with open(self.path, "rb") as source:
                raw = source.read()
        except FileNotFoundError:
            return [], _GENESIS
        except OSError as exc:
            raise PaperAuditError(f"não foi possível ler o diário de auditoria: {exc}") from exc
        if raw and not raw.endswith(b"\n"):
            raise PaperAuditError("último evento de auditoria está truncado; diário preservado")
        events, previous, expected_seq, seen_ids = [], _GENESIS, 1, set()
        for index, line in enumerate(raw.splitlines(), 1):
            try:
                event = json.loads(line.decode("utf-8"))
            except (UnicodeError, json.JSONDecodeError) as exc:
                raise PaperAuditError(f"evento de auditoria inválido na linha {index}") from exc
            if not isinstance(event, dict):
                raise PaperAuditError(f"evento de auditoria inválido na linha {index}")
            if set(event) != _TOP_LEVEL_FIELDS:
                raise PaperAuditError("estrutura do evento invalida na linha %s" % index)
            fields = event.get("fields")
            event_id = event.get("event_id")
            if (event.get("event") not in _ALLOWED_EVENTS
                    or not isinstance(fields, dict)
                    or set(fields) - _ALLOWED_FIELDS
                    or not isinstance(event_id, str) or not event_id
                    or event_id in seen_ids
                    or not isinstance(event.get("occurred_at_utc"), str)
                    or isinstance(event.get("sequence"), bool)
                    or not isinstance(event.get("sequence"), int)
                    or not isinstance(event.get("previous_sha256"), str)):
                raise PaperAuditError("campos do evento invalidos na linha %s" % index)
            digest = event.pop("sha256", None)
            if (event.get("sequence") != expected_seq
                    or event.get("previous_sha256") != previous
                    or not isinstance(digest, str)
                    or hashlib.sha256(_canonical(event)).hexdigest() != digest):
                raise PaperAuditError(f"cadeia de hashes inválida na linha {index}")
            event["sha256"] = digest
            events.append(event)
            seen_ids.add(event_id)
            previous, expected_seq = digest, expected_seq + 1
        return events, previous

    def verify(self):
        with self._lock:
            if self._blocked:
                raise PaperAuditError(self._error or "diario de auditoria bloqueado")
            try:
                events, last_hash = self._read_verify_locked()
            except PaperAuditError as exc:
                self._blocked, self._error = True, str(exc)
                raise
            self._blocked, self._error = False, None
            return {"count": len(events), "last_sha256": last_hash, "events": events}

    def append(self, event_type, fields=None):
        if event_type not in _ALLOWED_EVENTS:
            raise PaperAuditError("tipo de evento de auditoria não permitido")
        fields = dict(fields or {})
        if "reason" in fields and fields["reason"] is not None:
            fields["reason"] = sanitize_audit_text(fields["reason"])
        if set(fields) - _ALLOWED_FIELDS:
            raise PaperAuditError("campo não permitido no diário de auditoria")
        with self._lock:
            if self._blocked:
                raise PaperAuditError(self._error or "diário de auditoria bloqueado")
            try:
                events, previous = self._read_verify_locked()
                body = {
                    "sequence": len(events) + 1,
                    "event_id": uuid.uuid4().hex,
                    "occurred_at_utc": datetime.now(timezone.utc).isoformat(),
                    "event": event_type,
                    "previous_sha256": previous,
                    "fields": fields,
                }
                digest = hashlib.sha256(_canonical(body)).hexdigest()
                encoded = _canonical({**body, "sha256": digest}) + b"\n"
                os.makedirs(os.path.dirname(self.path), exist_ok=True)
                with open(self.path, "ab") as output:
                    output.write(encoded)
                    output.flush()
                    self._fsync(output.fileno())
                # Verify the appended record before returning success.
                result = self._read_verify_locked()
                if result[1] != digest:
                    raise PaperAuditError("verificação do evento acrescentado falhou")
                return digest
            except Exception as exc:
                self._blocked = True
                self._error = str(exc)
                if isinstance(exc, PaperAuditError):
                    raise
                raise PaperAuditError(f"falha ao persistir evento de auditoria: {exc}") from exc
