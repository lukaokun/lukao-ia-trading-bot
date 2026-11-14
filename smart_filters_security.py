# -*- coding: utf-8 -*-
"""Read-only Solana mint authority and Token-2022 audit.

This module only calls getAccountInfo against Solana's public Mainnet RPC.
It never signs or sends transactions. It deliberately stores its snapshots in
smart_filters_security.db, separate from smart_filters_market.db.

Supported Token-2022 extension payloads:
TransferFeeConfig, MintCloseAuthority, DefaultAccountState, NonTransferable,
PermanentDelegate, TransferHook, MetadataPointer, and Pausable.
Other recognized or unknown extension payloads are retained as unknown
metadata (type, length, digest); they are never interpreted as safe or unsafe.
"""
from __future__ import annotations

import base64
from contextlib import closing
import hashlib
import json
import os
import re
import socket
import sqlite3
import struct
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


SOURCE = "solana_public_rpc"
PUBLIC_RPC_URL = "https://api.mainnet-beta.solana.com"
TOKEN_PROGRAM_ID = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022_PROGRAM_ID = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
MINT_BASE_LENGTH = 82
TOKEN_2022_BASE_ACCOUNT_LENGTH = 165
TOKEN_2022_ACCOUNT_TYPE_MINT = 1

EXTENSION_NAMES = {
    0: "Uninitialized",
    1: "TransferFeeConfig",
    2: "TransferFeeAmount",
    3: "MintCloseAuthority",
    4: "ConfidentialTransferMint",
    5: "ConfidentialTransferAccount",
    6: "DefaultAccountState",
    7: "ImmutableOwner",
    8: "MemoTransfer",
    9: "NonTransferable",
    10: "InterestBearingConfig",
    11: "CpiGuard",
    12: "PermanentDelegate",
    13: "NonTransferableAccount",
    14: "TransferHook",
    15: "TransferHookAccount",
    16: "ConfidentialTransferFeeConfig",
    17: "ConfidentialTransferFeeAmount",
    18: "MetadataPointer",
    19: "TokenMetadata",
    20: "GroupPointer",
    21: "TokenGroup",
    22: "GroupMemberPointer",
    23: "TokenGroupMember",
    24: "ConfidentialMintBurn",
    25: "ScaledUiAmount",
    26: "Pausable",
    27: "PausableAccount",
    28: "PermissionedBurn",
}
MINT_EXTENSION_IDS = {
    1, 3, 4, 6, 9, 10, 12, 14, 16, 18, 19, 20, 21, 22, 23, 24, 25, 26, 28
}
SUPPORTED_EXTENSION_IDS = {1, 3, 6, 9, 12, 14, 18, 26}
AUTHORITY_STATES = {"active", "revoked", "unknown"}


class RPCFailure(Exception):
    """A sanitized RPC failure with a stable quality status."""

    def __init__(self, status, message, retry_after_seconds=None):
        super().__init__(str(message))
        self.status = status
        self.retry_after_seconds = retry_after_seconds


def utc_now():
    return datetime.now(timezone.utc)


def _utc_text(value=None):
    value = utc_now() if value is None else value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        value = datetime.fromtimestamp(value, timezone.utc)
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("timestamp tem de incluir timezone")
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _b58encode(raw):
    alphabet = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    zeros = len(raw) - len(raw.lstrip(b"\0"))
    number = int.from_bytes(raw, "big")
    out = ""
    while number:
        number, rem = divmod(number, 58)
        out = alphabet[rem] + out
    return "1" * zeros + (out or ("" if zeros else "1"))


def _valid_pubkey(value):
    if not isinstance(value, str) or not value:
        return False
    alphabet = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    if any(ch not in alphabet for ch in value):
        return False
    number = 0
    for ch in value:
        number = number * 58 + alphabet.index(ch)
    decoded = number.to_bytes((number.bit_length() + 7) // 8, "big") if number else b""
    return len(value) - len(value.lstrip("1")) + len(decoded) == 32


def _read_pubkey(raw, offset):
    value = raw[offset:offset + 32]
    if len(value) != 32:
        raise ValueError("pubkey truncada")
    return _b58encode(value)


def _read_optional_nonzero_pubkey(raw, offset):
    value = raw[offset:offset + 32]
    if len(value) != 32:
        raise ValueError("pubkey opcional truncada")
    return None if value == b"\0" * 32 else _b58encode(value)


def _authority_state(value):
    if value is None:
        return "revoked"
    return "active"


def _coption_pubkey(data, offset):
    if offset + 36 > len(data):
        raise ValueError("COption<Pubkey> truncada")
    tag = struct.unpack_from("<I", data, offset)[0]
    key = data[offset + 4:offset + 36]
    if tag == 0:
        return None
    if tag == 1:
        return _b58encode(key)
    raise ValueError("tag COption<Pubkey> inválida")


def _fee(raw, offset):
    epoch, max_fee, basis_points = struct.unpack_from("<QQH", raw, offset)
    if basis_points > 10_000:
        raise ValueError("basis points de taxa fora do intervalo")
    return {
        "epoch": epoch,
        "maximum_fee_raw": str(max_fee),
        "transfer_fee_basis_points": basis_points,
    }


def _decode_extension(extension_id, payload):
    """Decode a deliberately small, reviewed subset of fixed-size extensions."""
    if extension_id == 1:
        if len(payload) != 108:
            raise ValueError("TransferFeeConfig deve ter 108 bytes")
        config_authority = _read_optional_nonzero_pubkey(payload, 0)
        withdraw_authority = _read_optional_nonzero_pubkey(payload, 32)
        return {
            "transfer_fee_config_authority": config_authority,
            "transfer_fee_config_authority_state": _authority_state(config_authority),
            "withdraw_withheld_authority": withdraw_authority,
            "withdraw_withheld_authority_state": _authority_state(withdraw_authority),
            "withheld_amount_raw": str(struct.unpack_from("<Q", payload, 64)[0]),
            "older_transfer_fee": _fee(payload, 72),
            "newer_transfer_fee": _fee(payload, 90),
        }
    if extension_id == 3:
        if len(payload) != 32:
            raise ValueError("MintCloseAuthority deve ter 32 bytes")
        authority = _read_optional_nonzero_pubkey(payload, 0)
        return {"close_authority": authority, "close_authority_state": _authority_state(authority)}
    if extension_id == 6:
        if len(payload) != 1 or payload[0] not in (0, 1, 2):
            raise ValueError("DefaultAccountState inválido")
        return {"default_account_state": {0: "uninitialized", 1: "initialized", 2: "frozen"}[payload[0]]}
    if extension_id == 9:
        if payload:
            raise ValueError("NonTransferable deve ter zero bytes")
        return {"non_transferable": True}
    if extension_id == 12:
        if len(payload) != 32:
            raise ValueError("PermanentDelegate deve ter 32 bytes")
        delegate = _read_optional_nonzero_pubkey(payload, 0)
        return {"delegate": delegate, "delegate_state": _authority_state(delegate)}
    if extension_id == 14:
        if len(payload) != 64:
            raise ValueError("TransferHook deve ter 64 bytes")
        authority = _read_optional_nonzero_pubkey(payload, 0)
        program_id = _read_optional_nonzero_pubkey(payload, 32)
        return {
            "authority": authority, "authority_state": _authority_state(authority),
            "program_id": program_id, "program_id_state": _authority_state(program_id),
        }
    if extension_id == 18:
        if len(payload) != 64:
            raise ValueError("MetadataPointer deve ter 64 bytes")
        authority = _read_optional_nonzero_pubkey(payload, 0)
        address = _read_optional_nonzero_pubkey(payload, 32)
        return {
            "authority": authority, "authority_state": _authority_state(authority),
            "metadata_address": address,
        }
    if extension_id == 26:
        if len(payload) != 33 or payload[32] not in (0, 1):
            raise ValueError("Pausable deve ter 33 bytes e booleano válido")
        authority = _read_optional_nonzero_pubkey(payload, 0)
        return {
            "authority": authority, "authority_state": _authority_state(authority),
            "paused": bool(payload[32]),
        }
    raise LookupError("extensão reconhecida mas sem descodificador suportado")


def parse_mint_account(program_owner, data, slot=None, observed_at_utc=None):
    """Parse raw base64-decoded account data from getAccountInfo."""
    observed_at_utc = _utc_text(observed_at_utc)
    result = {
        "source": SOURCE,
        "rpc_endpoint": PUBLIC_RPC_URL,
        "observed_at_utc": observed_at_utc,
        "slot": slot if isinstance(slot, int) and not isinstance(slot, bool) and slot >= 0 else None,
        "program_owner": program_owner if isinstance(program_owner, str) else None,
        "token_program": (
            "spl_token" if program_owner == TOKEN_PROGRAM_ID else
            "token_2022" if program_owner == TOKEN_2022_PROGRAM_ID else "unknown"
        ),
        "supply_raw": None,
        "decimals": None,
        "initialized": None,
        "mint_authority": None,
        "mint_authority_state": "unknown",
        "freeze_authority": None,
        "freeze_authority_state": "unknown",
        "extensions": [],
        "missing_fields": [],
        "parse_errors": [],
        "quality_status": "unknown",
        "response_status": "received",
        "retry_after_seconds": None,
        "persistence_status": "not_configured",
    }
    missing = []
    if result["slot"] is None:
        missing.append("slot")
    if result["program_owner"] is None:
        missing.append("program_owner")
    if result["token_program"] == "unknown":
        result["missing_fields"] = missing + ["token_program", "supply_raw", "decimals",
                                               "initialized", "mint_authority",
                                               "freeze_authority", "extensions"]
        result["quality_status"] = "unknown_program"
        return result
    if not isinstance(data, (bytes, bytearray)) or len(data) < MINT_BASE_LENGTH:
        result["missing_fields"] = missing + ["mint_account_data"]
        result["parse_errors"] = ["dados base da conta mint truncados"]
        result["quality_status"] = "malformed"
        return result
    data = bytes(data)
    try:
        mint_authority = _coption_pubkey(data, 0)
        supply = struct.unpack_from("<Q", data, 36)[0]
        decimals = data[44]
        initialized_flag = data[45]
        freeze_authority = _coption_pubkey(data, 46)
        if initialized_flag not in (0, 1):
            raise ValueError("flag initialized inválida")
        result.update({
            "supply_raw": str(supply),
            "decimals": decimals,
            "initialized": bool(initialized_flag),
            "mint_authority": mint_authority,
            "mint_authority_state": _authority_state(mint_authority),
            "freeze_authority": freeze_authority,
            "freeze_authority_state": _authority_state(freeze_authority),
        })
    except (ValueError, struct.error) as exc:
        result["missing_fields"] = missing + ["mint_base_state"]
        result["parse_errors"] = [str(exc)]
        result["quality_status"] = "malformed"
        return result

    result["missing_fields"] = list(missing)

    if not result["initialized"]:
        result["quality_status"] = "uninitialized"
        result["missing_fields"] = list(missing) + ["initialized_mint_state"]
        return result

    if result["token_program"] == "spl_token":
        if len(data) != MINT_BASE_LENGTH:
            result["quality_status"] = "malformed"
            result["missing_fields"].append("mint_account_layout")
            result["parse_errors"] = ["tamanho inesperado para mint do Token Program clássico"]
        else:
            result["quality_status"] = "valid"
        return result

    if len(data) == MINT_BASE_LENGTH:
        result["quality_status"] = "valid"
        return result
    if len(data) < TOKEN_2022_BASE_ACCOUNT_LENGTH + 1:
        result["quality_status"] = "malformed"
        result["missing_fields"].append("token_2022_extensions")
        result["parse_errors"] = ["conta Token-2022 com tamanho insuficiente para a área TLV"]
        return result
    if any(data[MINT_BASE_LENGTH:TOKEN_2022_BASE_ACCOUNT_LENGTH]):
        result["quality_status"] = "malformed"
        result["missing_fields"].append("token_2022_extensions")
        result["parse_errors"] = ["padding Token-2022 do mint contém bytes inesperados"]
        return result
    if data[TOKEN_2022_BASE_ACCOUNT_LENGTH] != TOKEN_2022_ACCOUNT_TYPE_MINT:
        result["quality_status"] = "malformed"
        result["missing_fields"].append("token_2022_extensions")
        result["parse_errors"] = ["AccountType não identifica uma conta Mint"]
        return result

    tlv = data[TOKEN_2022_BASE_ACCOUNT_LENGTH + 1:]
    offset = 0
    seen_types = set()
    has_unknown = False
    while offset < len(tlv):
        remaining = tlv[offset:]
        if len(remaining) < 4:
            if any(remaining):
                result["parse_errors"].append("cabeçalho TLV incompleto")
                result["missing_fields"].append("extension_configuration")
            break
        extension_id, length = struct.unpack_from("<HH", tlv, offset)
        if extension_id == 0 and length == 0:
            if any(remaining):
                result["parse_errors"].append("dados após marcador TLV não inicializado")
            break
        if extension_id == 0:
            result["parse_errors"].append("extensão Uninitialized com payload não vazio")
            result["missing_fields"].append("extension_configuration")
            break
        start, end = offset + 4, offset + 4 + length
        if end > len(tlv):
            result["parse_errors"].append(f"payload TLV truncado para tipo {extension_id}")
            result["missing_fields"].append("extension_configuration")
            break
        payload = tlv[start:end]
        name = EXTENSION_NAMES.get(extension_id, f"UnknownExtension({extension_id})")
        extension = {
            "extension_type_id": extension_id,
            "extension_name": name,
            "supported": extension_id in SUPPORTED_EXTENSION_IDS,
            "decode_status": "unknown",
            "data_length": length,
            "data_sha256": hashlib.sha256(payload).hexdigest(),
            "configuration": None,
        }
        if extension_id in seen_types:
            result["parse_errors"].append(f"extensão duplicada: {extension_id}")
            extension["decode_status"] = "malformed"
        else:
            seen_types.add(extension_id)
            if extension_id in EXTENSION_NAMES and extension_id not in MINT_EXTENSION_IDS and extension_id != 0:
                result["parse_errors"].append(f"tipo {extension_id} não é uma extensão de mint")
                extension["decode_status"] = "malformed"
            elif extension_id in SUPPORTED_EXTENSION_IDS:
                try:
                    extension["configuration"] = _decode_extension(extension_id, payload)
                    extension["decode_status"] = "decoded"
                except (ValueError, struct.error) as exc:
                    extension["decode_status"] = "malformed"
                    result["parse_errors"].append(f"{name}: {exc}")
            else:
                has_unknown = True
                extension["decode_status"] = "unknown" if extension_id not in EXTENSION_NAMES else "recognized_unparsed"
        if extension["decode_status"] in ("unknown", "recognized_unparsed"):
            has_unknown = True
            result["missing_fields"].append(f"extension_configuration:{name}")
        elif extension["decode_status"] == "malformed":
            result["missing_fields"].append(f"extension_configuration:{name}")
        result["extensions"].append(extension)
        offset = end

    if result["parse_errors"]:
        result["quality_status"] = "malformed"
    elif has_unknown:
        result["quality_status"] = "partial_unknown_extensions"
    else:
        result["quality_status"] = "valid"
    return result


def _retry_after(value):
    if value is None:
        return None
    try:
        seconds = float(value)
        return seconds if seconds >= 0 else None
    except (TypeError, ValueError, OverflowError):
        return None


class PublicSolanaRPC:
    """Minimal public-RPC client exposing only read-only getAccountInfo."""

    def __init__(self, timeout=12, opener=None):
        self.timeout = max(0.1, float(timeout))
        self._opener = opener or urllib.request.urlopen

    def get_account_info(self, mint):
        request_body = json.dumps({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getAccountInfo",
            "params": [mint, {"encoding": "base64", "commitment": "finalized"}],
        }).encode("utf-8")
        request = urllib.request.Request(
            PUBLIC_RPC_URL,
            data=request_body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with self._opener(request, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            retry_after = _retry_after(exc.headers.get("Retry-After") if exc.headers else None)
            status = "rate_limited" if exc.code == 429 else "unavailable"
            raise RPCFailure(status, f"HTTP {exc.code}", retry_after) from exc
        except (TimeoutError, socket.timeout) as exc:
            raise RPCFailure("timeout", "timeout no RPC") from exc
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", None)
            if isinstance(reason, TimeoutError) or isinstance(reason, socket.timeout):
                raise RPCFailure("timeout", "timeout no RPC") from exc
            raise RPCFailure("unavailable", "falha de ligação ao RPC") from exc
        except (ValueError, UnicodeDecodeError) as exc:
            raise RPCFailure("malformed_response", "resposta JSON inválida") from exc
        if not isinstance(payload, dict):
            raise RPCFailure("malformed_response", "resposta RPC não é um objeto")
        error = payload.get("error")
        if error is not None:
            if not isinstance(error, dict):
                raise RPCFailure("malformed_response", "campo error RPC inválido")
            message = str(error.get("message", "erro RPC"))
            code = error.get("code")
            if code == 429 or re.search(r"429|rate.?limit|too many requests", message, re.I):
                raise RPCFailure("rate_limited", "RPC devolveu limite de pedidos")
            raise RPCFailure("rpc_error", "erro devolvido pelo RPC")
        result = payload.get("result")
        if not isinstance(result, dict):
            raise RPCFailure("malformed_response", "campo result RPC ausente ou inválido")
        return result


class SecurityAuditStore:
    """Append-only local store, isolated from smart_filters_market.db."""

    def __init__(self, db_path=None):
        self.db_path = os.path.abspath(
            db_path or Path(__file__).with_name("smart_filters_security.db")
        )
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self._initialize()

    def _connect(self):
        db = sqlite3.connect(self.db_path, timeout=5)
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=5000")
        return db

    def _initialize(self):
        with closing(self._connect()) as db:
            db.execute("""CREATE TABLE IF NOT EXISTS security_audit_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                mint TEXT NOT NULL,
                observed_at_utc TEXT NOT NULL,
                slot INTEGER,
                source TEXT NOT NULL,
                rpc_endpoint TEXT NOT NULL,
                program_owner TEXT,
                token_program TEXT NOT NULL,
                supply_raw TEXT,
                decimals INTEGER,
                initialized INTEGER CHECK(initialized IS NULL OR initialized IN (0,1)),
                mint_authority TEXT,
                mint_authority_state TEXT NOT NULL CHECK(mint_authority_state IN ('active','revoked','unknown')),
                freeze_authority TEXT,
                freeze_authority_state TEXT NOT NULL CHECK(freeze_authority_state IN ('active','revoked','unknown')),
                quality_status TEXT NOT NULL,
                response_status TEXT NOT NULL,
                missing_fields_json TEXT NOT NULL,
                parse_errors_json TEXT NOT NULL,
                retry_after_seconds REAL
            )""")
            db.execute("""CREATE TABLE IF NOT EXISTS token_2022_extensions (
                snapshot_id INTEGER NOT NULL REFERENCES security_audit_snapshots(id),
                extension_index INTEGER NOT NULL,
                extension_type_id INTEGER NOT NULL,
                extension_name TEXT NOT NULL,
                supported INTEGER NOT NULL CHECK(supported IN (0,1)),
                decode_status TEXT NOT NULL,
                data_length INTEGER NOT NULL,
                data_sha256 TEXT NOT NULL,
                configuration_json TEXT,
                PRIMARY KEY(snapshot_id, extension_index)
            )""")
            db.execute("""CREATE INDEX IF NOT EXISTS idx_security_snapshot_mint_time
                ON security_audit_snapshots(mint, observed_at_utc DESC)""")
            db.execute("""CREATE INDEX IF NOT EXISTS idx_security_snapshot_time
                ON security_audit_snapshots(observed_at_utc DESC)""")
            db.commit()

    def save(self, record):
        with closing(self._connect()) as db:
            cur = db.execute("""INSERT INTO security_audit_snapshots (
                mint,observed_at_utc,slot,source,rpc_endpoint,program_owner,token_program,
                supply_raw,decimals,initialized,mint_authority,mint_authority_state,
                freeze_authority,freeze_authority_state,quality_status,response_status,
                missing_fields_json,parse_errors_json,retry_after_seconds
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
                record["mint"], record["observed_at_utc"], record.get("slot"),
                record["source"], record["rpc_endpoint"], record.get("program_owner"),
                record["token_program"], record.get("supply_raw"), record.get("decimals"),
                None if record.get("initialized") is None else int(record["initialized"]),
                record.get("mint_authority"), record["mint_authority_state"],
                record.get("freeze_authority"), record["freeze_authority_state"],
                record["quality_status"], record["response_status"],
                json.dumps(record.get("missing_fields", []), separators=(",", ":")),
                json.dumps(record.get("parse_errors", []), separators=(",", ":")),
                record.get("retry_after_seconds"),
            ))
            snapshot_id = cur.lastrowid
            for index, ext in enumerate(record.get("extensions", [])):
                db.execute("""INSERT INTO token_2022_extensions (
                    snapshot_id,extension_index,extension_type_id,extension_name,
                    supported,decode_status,data_length,data_sha256,configuration_json
                ) VALUES (?,?,?,?,?,?,?,?,?)""", (
                    snapshot_id, index, ext["extension_type_id"], ext["extension_name"],
                    int(bool(ext["supported"])), ext["decode_status"], ext["data_length"],
                    ext["data_sha256"],
                    None if ext.get("configuration") is None else
                    json.dumps(ext["configuration"], separators=(",", ":"), sort_keys=True),
                ))
            db.commit()
            return snapshot_id

    def get(self, snapshot_id):
        with closing(self._connect()) as db:
            row = db.execute("""SELECT id,mint,observed_at_utc,slot,source,rpc_endpoint,
                program_owner,token_program,supply_raw,decimals,initialized,mint_authority,
                mint_authority_state,freeze_authority,freeze_authority_state,quality_status,
                response_status,missing_fields_json,parse_errors_json,retry_after_seconds
                FROM security_audit_snapshots WHERE id=?""", (int(snapshot_id),)).fetchone()
            if row is None:
                return None
            names = ("id","mint","observed_at_utc","slot","source","rpc_endpoint",
                     "program_owner","token_program","supply_raw","decimals","initialized",
                     "mint_authority","mint_authority_state","freeze_authority",
                     "freeze_authority_state","quality_status","response_status",
                     "missing_fields_json","parse_errors_json","retry_after_seconds")
            result = dict(zip(names, row))
            result["missing_fields"] = json.loads(result.pop("missing_fields_json"))
            result["parse_errors"] = json.loads(result.pop("parse_errors_json"))
            extensions = db.execute("""SELECT extension_type_id,extension_name,supported,
                decode_status,data_length,data_sha256,configuration_json
                FROM token_2022_extensions WHERE snapshot_id=? ORDER BY extension_index""",
                (int(snapshot_id),)).fetchall()
            result["extensions"] = []
            for ext in extensions:
                result["extensions"].append({
                    "extension_type_id": ext[0], "extension_name": ext[1],
                    "supported": bool(ext[2]), "decode_status": ext[3],
                    "data_length": ext[4], "data_sha256": ext[5],
                    "configuration": None if ext[6] is None else json.loads(ext[6]),
                })
            return result

    def list_by_mint(self, mint, limit=100):
        with closing(self._connect()) as db:
            ids = [r[0] for r in db.execute(
                "SELECT id FROM security_audit_snapshots WHERE mint=? "
                "ORDER BY observed_at_utc DESC,id DESC LIMIT ?",
                (str(mint), max(1, min(1000, int(limit))))).fetchall()]
        return [self.get(snapshot_id) for snapshot_id in ids]


class TokenMintAuditor:
    """Fetch and describe one mint; no trading or decision code is referenced."""

    def __init__(self, rpc=None, store=None, clock=utc_now):
        self.rpc = rpc or PublicSolanaRPC()
        self.store = store
        self.clock = clock

    def audit_mint(self, mint):
        if not _valid_pubkey(mint):
            raise ValueError("mint tem de ser uma public key Solana de 32 bytes")
        observed = _utc_text(self.clock())
        try:
            response = self.rpc.get_account_info(mint)
        except RPCFailure as exc:
            record = self._empty_record(mint, observed, exc.status)
            record["parse_errors"] = [str(exc)]
            record["retry_after_seconds"] = exc.retry_after_seconds
            return self._persist(record)
        except TimeoutError:
            record = self._empty_record(mint, observed, "timeout")
            record["parse_errors"] = ["timeout no RPC"]
            return self._persist(record)
        except Exception as exc:
            record = self._empty_record(mint, observed, "unavailable")
            record["parse_errors"] = [f"falha RPC: {type(exc).__name__}"]
            return self._persist(record)

        if not isinstance(response, dict):
            record = self._empty_record(mint, observed, "malformed_response")
            record["parse_errors"] = ["resultado de getAccountInfo não é objeto"]
            return self._persist(record)
        context = response.get("context")
        slot = context.get("slot") if isinstance(context, dict) else None
        if isinstance(slot, bool) or not isinstance(slot, int) or slot < 0:
            slot = None
        account = response.get("value")
        if account is None:
            record = self._empty_record(mint, observed, "not_found")
            record["slot"] = slot
            record["missing_fields"] = ["account", "program_owner", "token_program",
                                        "supply_raw", "decimals", "mint_authority",
                                        "freeze_authority", "extensions"]
            return self._persist(record)
        if not isinstance(account, dict):
            record = self._empty_record(mint, observed, "malformed_response")
            record["slot"] = slot
            record["parse_errors"] = ["value de getAccountInfo não é objeto nem null"]
            return self._persist(record)
        owner = account.get("owner")
        encoded = account.get("data")
        if (not isinstance(encoded, list) or len(encoded) < 2 or
                encoded[1] != "base64" or not isinstance(encoded[0], str)):
            record = self._empty_record(mint, observed, "malformed_response")
            record["slot"] = slot
            record["program_owner"] = owner if isinstance(owner, str) else None
            record["token_program"] = (
                "spl_token" if owner == TOKEN_PROGRAM_ID else
                "token_2022" if owner == TOKEN_2022_PROGRAM_ID else "unknown"
            )
            record["parse_errors"] = ["data não está no formato base64 esperado"]
            record["missing_fields"] = ["mint_account_data"]
            return self._persist(record)
        try:
            raw_data = base64.b64decode(encoded[0], validate=True)
        except (ValueError, TypeError) as exc:
            record = self._empty_record(mint, observed, "malformed_response")
            record["slot"] = slot
            record["program_owner"] = owner if isinstance(owner, str) else None
            record["parse_errors"] = ["base64 da conta mint inválido"]
            return self._persist(record)
        record = parse_mint_account(owner, raw_data, slot=slot, observed_at_utc=observed)
        record["mint"] = mint
        return self._persist(record)

    @staticmethod
    def _empty_record(mint, observed, status):
        return {
            "mint": mint, "observed_at_utc": observed, "slot": None,
            "source": SOURCE, "rpc_endpoint": PUBLIC_RPC_URL,
            "program_owner": None, "token_program": "unknown",
            "supply_raw": None, "decimals": None, "initialized": None,
            "mint_authority": None, "mint_authority_state": "unknown",
            "freeze_authority": None, "freeze_authority_state": "unknown",
            "extensions": [], "missing_fields": [
                "slot", "program_owner", "token_program", "supply_raw",
                "decimals", "initialized", "mint_authority", "freeze_authority",
                "extensions",
            ],
            "parse_errors": [], "quality_status": status,
            "response_status": status, "retry_after_seconds": None,
            "persistence_status": "not_configured",
        }

    def _persist(self, record):
        if self.store is None:
            record["persistence_status"] = "not_configured"
            return record
        try:
            record["snapshot_id"] = self.store.save(record)
            record["persistence_status"] = "saved"
        except sqlite3.Error as exc:
            record["persistence_status"] = "failed"
            record["parse_errors"] = list(record.get("parse_errors", [])) + [
                f"persistência SQLite: {type(exc).__name__}"
            ]
        return record


__all__ = [
    "EXTENSION_NAMES", "PUBLIC_RPC_URL", "SUPPORTED_EXTENSION_IDS",
    "TOKEN_2022_PROGRAM_ID", "TOKEN_PROGRAM_ID", "PublicSolanaRPC",
    "RPCFailure", "SecurityAuditStore", "TokenMintAuditor", "parse_mint_account",
]
