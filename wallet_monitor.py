# -*- coding: utf-8 -*-
"""Monitor de carteiras Solana via JSON-RPC padrão (sem subscrição paga).

Por segurança/limites, recolhe apenas endereços explicitamente colocados
na watchlist e limita o número de assinaturas por ciclo.
"""
import json
import os
import time
import urllib.request
from solana_swap_decoder import is_solana_address


class SolanaRPC:
    def __init__(self, endpoint=None, timeout=15):
        self.endpoint = endpoint or os.environ.get(
            "SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com"
        )
        self.timeout = timeout
        self._id = 0

    def call(self, method, params):
        self._id += 1
        body = json.dumps({
            "jsonrpc": "2.0", "id": self._id,
            "method": method, "params": params
        }).encode("utf-8")
        req = urllib.request.Request(
            self.endpoint, data=body,
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
        if payload.get("error"):
            raise RuntimeError(f"Solana RPC error: {payload['error']}")
        return payload.get("result")


class WalletMonitor:
    def __init__(self, pipeline, rpc, state_path="wallet_monitor_state.json",
                 max_signatures_per_wallet=20, pause_seconds=0.25):
        self.pipeline = pipeline
        self.rpc = rpc
        self.state_path = state_path
        self.max_signatures = max(1, min(100, int(max_signatures_per_wallet)))
        self.pause = max(0.0, float(pause_seconds))
        try:
            with open(state_path, "r", encoding="utf-8") as f:
                self.state = json.load(f)
        except (OSError, ValueError):
            self.state = {}

    def _save(self):
        tmp = self.state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.state, f, indent=2)
        os.replace(tmp, self.state_path)

    def poll_wallet(self, wallet):
        if not is_solana_address(wallet):
            raise ValueError("endereco de carteira Solana invalido")
        before = self.state.get(wallet)
        config = {"limit": self.max_signatures}
        if before:
            config["until"] = before
        signatures = []
        seen_signatures = set()
        cursor_complete = False
        page_config = dict(config)
        while True:
            page = self.rpc.call("getSignaturesForAddress", [wallet, page_config]) or []
            if not page:
                cursor_complete = True
                break
            fresh = [item for item in page if item.get("signature") and
                     item["signature"] not in seen_signatures]
            signatures.extend(fresh)
            seen_signatures.update(item["signature"] for item in fresh)
            if before and any(item.get("signature") == before for item in page):
                cursor_complete = True
                break
            if len(page) < self.max_signatures:
                # A short response marks the end of the interval bounded by
                # `until` (or of initial history when no cursor exists).
                cursor_complete = True
                break
            oldest = page[-1].get("signature")
            if not oldest or oldest in seen_signatures and not fresh:
                break
            page_config = dict(config)
            page_config["before"] = oldest
        # RPC returns newest first. Process oldest first for chronological ledger.
        if before:
            signatures = [item for item in signatures if item.get("signature") != before]
        signatures = list(reversed(signatures))
        ingested = []
        newest_seen = before
        transactions_complete = True
        for item in signatures:
            sig = item.get("signature")
            if not sig:
                continue
            try:
                tx = self.rpc.call("getTransaction", [
                    sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}
                ])
            except Exception as exc:
                reason = f"getTransaction falhou: {exc}"
                if item.get("err") is not None and hasattr(self.pipeline, "record_failed"):
                    self.pipeline.record_failed(
                        signature=sig, wallet=wallet,
                        reason=f"signature err={item['err']!r}; {reason}",
                        rpc_endpoint=getattr(self.rpc, "endpoint", None))
                elif hasattr(self.pipeline, "record_unavailable"):
                    self.pipeline.record_unavailable(
                        signature=sig, wallet=wallet, reason=reason,
                        rpc_endpoint=getattr(self.rpc, "endpoint", None))
                transactions_complete = False
                break
            if tx is None:
                reason = "getTransaction devolveu null"
                if item.get("err") is not None and hasattr(self.pipeline, "record_failed"):
                    self.pipeline.record_failed(
                        signature=sig, wallet=wallet,
                        reason=f"signature err={item['err']!r}; {reason}",
                        rpc_endpoint=getattr(self.rpc, "endpoint", None))
                elif hasattr(self.pipeline, "record_unavailable"):
                    self.pipeline.record_unavailable(
                        signature=sig, wallet=wallet, reason=reason,
                        rpc_endpoint=getattr(self.rpc, "endpoint", None))
                # Do not move the cursor past missing transaction JSON; a later
                # poll can retrieve it and upgrade the audit record.
                transactions_complete = False
                break
            if item.get("err") is not None:
                if hasattr(self.pipeline, "record_failed"):
                    self.pipeline.record_failed(
                        signature=sig, wallet=wallet,
                        reason=f"signature err={item['err']!r}",
                        rpc_endpoint=getattr(self.rpc, "endpoint", None), raw_tx=tx)
                newest_seen = sig
                if self.pause:
                    time.sleep(self.pause)
                continue
            ingested.extend(self.pipeline.ingest_transaction(
                tx, wallet_filter={wallet}, signature=sig,
                rpc_endpoint=getattr(self.rpc, "endpoint", None)
            ))
            newest_seen = sig
            if self.pause:
                time.sleep(self.pause)
        if cursor_complete and transactions_complete and newest_seen and newest_seen != before:
            self.state[wallet] = newest_seen
            self._save()
        return {
            "wallet": wallet,
            "signatures_received": len(signatures),
            "swaps_ingested": len(ingested),
            "cursor_updated": bool(cursor_complete and transactions_complete and
                                    newest_seen and newest_seen != before),
        }

    def poll_watchlist(self, wallets):
        results = []
        for wallet in wallets:
            try:
                results.append(self.poll_wallet(str(wallet)))
            except Exception as exc:
                results.append({"wallet": str(wallet), "error": str(exc)})
        return results


def load_watchlist(path="wallet_watchlist.json"):
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    wallets = data.get("wallets", []) if isinstance(data, dict) else data
    # Keep only unique, structurally valid base58 32-byte public keys.
    return list(dict.fromkeys(x.strip() for x in wallets
                              if isinstance(x, str) and is_solana_address(x.strip())))
