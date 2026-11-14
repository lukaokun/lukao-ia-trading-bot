# -*- coding: utf-8 -*-
import base64
import json
import socket
import struct
import tempfile
import unittest
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

from smart_filters_security import (
    PUBLIC_RPC_URL,
    TOKEN_2022_PROGRAM_ID,
    TOKEN_PROGRAM_ID,
    PublicSolanaRPC,
    RPCFailure,
    SecurityAuditStore,
    TokenMintAuditor,
    parse_mint_account,
)


MINT = "11111111111111111111111111111111"
STAMP = datetime(2026, 11, 13, 12, 0, tzinfo=timezone.utc)


def pubkey_bytes(seed):
    return bytes([seed]) * 32


def coption(seed=None):
    return struct.pack("<I", 0 if seed is None else 1) + (
        b"\0" * 32 if seed is None else pubkey_bytes(seed)
    )


def mint_data(mint_authority_seed=7, freeze_authority_seed=8, supply=987654321,
              decimals=6, initialized=1):
    data = bytearray(82)
    data[0:36] = coption(mint_authority_seed)
    struct.pack_into("<Q", data, 36, supply)
    data[44] = decimals
    data[45] = initialized
    data[46:82] = coption(freeze_authority_seed)
    return bytes(data)


def tlv(extension_id, payload):
    return struct.pack("<HH", extension_id, len(payload)) + payload


def token2022_data(extensions=(), **kwargs):
    return mint_data(**kwargs) + b"\0" * (165 - 82) + b"\x01" + b"".join(
        tlv(extension_id, payload) for extension_id, payload in extensions
    )


class FakeRPC:
    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.calls = []

    def get_account_info(self, mint):
        self.calls.append(mint)
        if self.error:
            raise self.error
        return self.result


def rpc_result(owner, data, slot=123456):
    return {
        "context": {"slot": slot},
        "value": {"owner": owner, "data": [base64.b64encode(data).decode("ascii"), "base64"]},
    }


class Response:
    def __init__(self, payload):
        self.body = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self):
        return self.body


class SecurityAuditTests(unittest.TestCase):
    def test_classic_token_program_with_active_authorities_and_raw_supply(self):
        data = mint_data(supply=1234567890123, decimals=9)
        out = parse_mint_account(TOKEN_PROGRAM_ID, data, slot=12, observed_at_utc=STAMP)
        self.assertEqual(out["token_program"], "spl_token")
        self.assertEqual(out["supply_raw"], "1234567890123")
        self.assertEqual(out["decimals"], 9)
        self.assertEqual(out["mint_authority_state"], "active")
        self.assertEqual(out["freeze_authority_state"], "active")
        self.assertEqual(out["quality_status"], "valid")
        self.assertEqual(out["missing_fields"], [])
        self.assertEqual(out["slot"], 12)

    def test_classic_authorities_revoked_are_distinct_from_unknown(self):
        out = parse_mint_account(TOKEN_PROGRAM_ID, mint_data(None, None), slot=13)
        self.assertIsNone(out["mint_authority"])
        self.assertEqual(out["mint_authority_state"], "revoked")
        self.assertIsNone(out["freeze_authority"])
        self.assertEqual(out["freeze_authority_state"], "revoked")

    def test_token_2022_supported_extensions_and_authorities(self):
        fee = (
            pubkey_bytes(11) + pubkey_bytes(12) + struct.pack("<Q", 44) +
            struct.pack("<QQH", 5, 1000, 150) +
            struct.pack("<QQH", 9, 2000, 250)
        )
        extensions = [
            (1, fee),
            (3, pubkey_bytes(13)),
            (6, b"\x02"),
            (9, b""),
            (12, pubkey_bytes(14)),
            (14, pubkey_bytes(15) + pubkey_bytes(16)),
            (18, pubkey_bytes(17) + pubkey_bytes(18)),
            (26, pubkey_bytes(19) + b"\x01"),
        ]
        out = parse_mint_account(
            TOKEN_2022_PROGRAM_ID, token2022_data(extensions), slot=99
        )
        self.assertEqual(out["token_program"], "token_2022")
        self.assertEqual(out["quality_status"], "valid")
        self.assertEqual([x["extension_type_id"] for x in out["extensions"]],
                         [1, 3, 6, 9, 12, 14, 18, 26])
        fee_config = out["extensions"][0]["configuration"]
        self.assertEqual(fee_config["transfer_fee_config_authority_state"], "active")
        self.assertEqual(fee_config["withdraw_withheld_authority_state"], "active")
        self.assertEqual(fee_config["withheld_amount_raw"], "44")
        self.assertEqual(fee_config["older_transfer_fee"]["transfer_fee_basis_points"], 150)
        self.assertEqual(out["extensions"][2]["configuration"]["default_account_state"], "frozen")
        self.assertTrue(out["extensions"][3]["configuration"]["non_transferable"])
        self.assertEqual(out["extensions"][4]["configuration"]["delegate_state"], "active")
        self.assertTrue(out["extensions"][7]["configuration"]["paused"])

    def test_supported_extension_with_revoked_optional_authority(self):
        out = parse_mint_account(
            TOKEN_2022_PROGRAM_ID,
            token2022_data([(3, b"\0" * 32)]),
        )
        ext = out["extensions"][0]
        self.assertEqual(ext["decode_status"], "decoded")
        self.assertIsNone(ext["configuration"]["close_authority"])
        self.assertEqual(ext["configuration"]["close_authority_state"], "revoked")

    def test_known_but_unparsed_extension_is_unknown_not_safe(self):
        out = parse_mint_account(
            TOKEN_2022_PROGRAM_ID,
            token2022_data([(10, b"\0" * 52)]),
        )
        self.assertEqual(out["quality_status"], "partial_unknown_extensions")
        self.assertEqual(out["extensions"][0]["extension_name"], "InterestBearingConfig")
        self.assertEqual(out["extensions"][0]["decode_status"], "recognized_unparsed")
        self.assertIsNone(out["extensions"][0]["configuration"])

    def test_unknown_extension_is_preserved_as_unknown(self):
        out = parse_mint_account(
            TOKEN_2022_PROGRAM_ID,
            token2022_data([(65000, b"\x01\x02\x03")]),
        )
        ext = out["extensions"][0]
        self.assertEqual(out["quality_status"], "partial_unknown_extensions")
        self.assertEqual(ext["extension_name"], "UnknownExtension(65000)")
        self.assertEqual(ext["decode_status"], "unknown")
        self.assertEqual(ext["data_length"], 3)
        self.assertIsNotNone(ext["data_sha256"])

    def test_incomplete_and_malformed_mint_data_stay_unknown_or_malformed(self):
        short = parse_mint_account(TOKEN_PROGRAM_ID, b"\0" * 20)
        self.assertEqual(short["quality_status"], "malformed")
        self.assertEqual(short["mint_authority_state"], "unknown")

        bad_option = bytearray(mint_data())
        struct.pack_into("<I", bad_option, 0, 2)
        malformed = parse_mint_account(TOKEN_PROGRAM_ID, bytes(bad_option))
        self.assertEqual(malformed["quality_status"], "malformed")
        self.assertEqual(malformed["mint_authority_state"], "unknown")

        bad_tlv = token2022_data([(3, b"\x01")])
        malformed_ext = parse_mint_account(TOKEN_2022_PROGRAM_ID, bad_tlv)
        self.assertEqual(malformed_ext["quality_status"], "malformed")
        self.assertEqual(malformed_ext["extensions"][0]["decode_status"], "malformed")

    def test_wrong_token_program_is_not_parsed_as_a_mint(self):
        out = parse_mint_account("11111111111111111111111111111111", mint_data())
        self.assertEqual(out["token_program"], "unknown")
        self.assertEqual(out["quality_status"], "unknown_program")
        self.assertIsNone(out["supply_raw"])
        self.assertEqual(out["mint_authority_state"], "unknown")

    def test_incomplete_rpc_response_and_nonexistent_account(self):
        with self.subTest("missing value"):
            auditor = TokenMintAuditor(FakeRPC(result={"context": {"slot": 3}}), clock=lambda: STAMP)
            out = auditor.audit_mint(MINT)
            self.assertEqual(out["quality_status"], "not_found")
            self.assertIn("supply_raw", out["missing_fields"])
        with self.subTest("missing base64"):
            auditor = TokenMintAuditor(
                FakeRPC(result={"context": {"slot": 4}, "value": {"owner": TOKEN_PROGRAM_ID}}),
                clock=lambda: STAMP,
            )
            out = auditor.audit_mint(MINT)
            self.assertEqual(out["quality_status"], "malformed_response")
            self.assertEqual(out["mint_authority_state"], "unknown")

    def test_timeout_is_returned_as_unavailable_data_without_crashing(self):
        auditor = TokenMintAuditor(FakeRPC(error=TimeoutError()), clock=lambda: STAMP)
        out = auditor.audit_mint(MINT)
        self.assertEqual(out["quality_status"], "timeout")
        self.assertEqual(out["mint_authority_state"], "unknown")
        self.assertIsNone(out["supply_raw"])

    def test_http_429_exposes_retry_after_without_retrying_or_signing(self):
        error = urllib.error.HTTPError(
            PUBLIC_RPC_URL, 429, "Too Many Requests", {"Retry-After": "7"}, None
        )
        rpc = PublicSolanaRPC(opener=lambda *_args, **_kwargs: (_ for _ in ()).throw(error))
        with self.assertRaises(RPCFailure) as caught:
            rpc.get_account_info(MINT)
        self.assertEqual(caught.exception.status, "rate_limited")
        self.assertEqual(caught.exception.retry_after_seconds, 7)

    def test_rpc_timeout_is_a_data_quality_state(self):
        rpc = PublicSolanaRPC(
            opener=lambda *_args, **_kwargs: (_ for _ in ()).throw(socket.timeout())
        )
        with self.assertRaises(RPCFailure) as caught:
            rpc.get_account_info(MINT)
        self.assertEqual(caught.exception.status, "timeout")

    def test_rpc_429_is_reported_without_treating_data_as_zero(self):
        response = Response({"jsonrpc": "2.0", "id": 1,
                             "error": {"code": 429, "message": "rate limit"}})
        rpc = PublicSolanaRPC(opener=lambda *_args, **_kwargs: response)
        with self.assertRaises(RPCFailure) as caught:
            rpc.get_account_info(MINT)
        self.assertEqual(caught.exception.status, "rate_limited")

    def test_rpc_request_is_read_only_get_account_info(self):
        captured = {}

        def opener(request, timeout):
            captured["method"] = request.method
            captured["body"] = json.loads(request.data.decode("utf-8"))
            captured["timeout"] = timeout
            return Response({"jsonrpc": "2.0", "id": 1,
                             "result": {"context": {"slot": 1}, "value": None}})

        PublicSolanaRPC(opener=opener).get_account_info(MINT)
        self.assertEqual(captured["method"], "POST")
        self.assertEqual(captured["body"]["method"], "getAccountInfo")
        self.assertNotIn("sendTransaction", captured["body"]["method"])
        self.assertEqual(captured["body"]["params"][1]["encoding"], "base64")

    def test_database_persists_snapshot_and_extensions_after_reopen(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "smart_filters_security.db"
            result = rpc_result(TOKEN_2022_PROGRAM_ID, token2022_data([(3, pubkey_bytes(22))]))
            first = TokenMintAuditor(FakeRPC(result), SecurityAuditStore(path),
                                     clock=lambda: STAMP).audit_mint(MINT)
            self.assertEqual(first["persistence_status"], "saved")
            snapshot_id = first["snapshot_id"]

            reopened = SecurityAuditStore(path)
            saved = reopened.get(snapshot_id)
            self.assertEqual(saved["mint"], MINT)
            self.assertEqual(saved["slot"], 123456)
            self.assertEqual(saved["token_program"], "token_2022")
            self.assertEqual(saved["extensions"][0]["extension_name"], "MintCloseAuthority")
            self.assertEqual(saved["extensions"][0]["configuration"]["close_authority_state"],
                             "active")
            self.assertEqual(len(reopened.list_by_mint(MINT)), 1)


if __name__ == "__main__":
    unittest.main()
