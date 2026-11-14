# -*- coding: utf-8 -*-
"""Ponte local entre o decoder Solana, o ledger SQLite e o agregador Smart Money.

Recebe respostas getTransaction(jsonParsed) já obtidas por um cliente RPC.
Não faz polling, não descobre carteiras e não envia transações.
"""
import math

from smart_wallet_tracker import SmartWalletTracker
from solana_swap_decoder import (decode_swaps, transaction_instruction_evidence,
                                 token_balance_deltas)
from smart_money_flow import score_smart_money_flow


class SmartMoneyPipeline:
    def __init__(self, db_path="smart_wallets.sqlite3"):
        self.tracker = SmartWalletTracker(db_path)

    @staticmethod
    def _signature(tx, supplied=None):
        if supplied:
            return str(supplied)
        try:
            sigs = tx.get("transaction", {}).get("signatures") or []
            return str(sigs[0]) if sigs and sigs[0] else None
        except (AttributeError, TypeError):
            return None

    @staticmethod
    def _scope(wallet_filter):
        if wallet_filter is None:
            return ""
        return ",".join(sorted(str(w) for w in wallet_filter if w))

    def record_unavailable(self, *, signature, wallet, reason="RPC getTransaction returned null",
                           rpc_endpoint=None):
        return self.tracker.record_transaction_status(
            signature=signature, wallet_scope=str(wallet or ""),
            status="unavailable", reason=str(reason), rpc_endpoint=rpc_endpoint)

    def record_failed(self, *, signature, wallet, reason, rpc_endpoint=None, raw_tx=None):
        return self.tracker.record_transaction_status(
            signature=signature, wallet_scope=str(wallet or ""),
            status="failed", raw_tx=raw_tx, reason=str(reason), rpc_endpoint=rpc_endpoint)

    def ingest_transaction(self, tx, quote_prices_usd=None, wallet_filter=None,
                           signature=None, rpc_endpoint=None):
        signature = self._signature(tx, signature)
        scope = self._scope(wallet_filter)
        if not isinstance(tx, dict):
            self.tracker.record_transaction_status(
                signature=signature, wallet_scope=scope, status="rejected_invalid",
                raw_tx=tx if isinstance(tx, (dict, list, str, int, float, bool, type(None))) else None,
                reason="RPC transaction payload is not a JSON object", rpc_endpoint=rpc_endpoint)
            return []
        try:
            meta = tx.get("meta")
            if not isinstance(meta, dict):
                raise ValueError("meta ausente ou inválido")
            if not signature:
                raise ValueError("assinatura ausente")
            block_time = tx.get("blockTime")
            if block_time is not None:
                timestamp = float(block_time)
                if not math.isfinite(timestamp) or timestamp < 0:
                    raise ValueError("blockTime inválido")
        except (AttributeError, TypeError, ValueError, OverflowError) as exc:
            self.tracker.record_transaction_status(
                signature=signature, wallet_scope=scope, status="rejected_invalid",
                raw_tx=tx, reason=str(exc), rpc_endpoint=rpc_endpoint)
            return []

        if meta.get("err") is not None:
            self.tracker.record_transaction_status(
                signature=signature, wallet_scope=scope, status="failed",
                raw_tx=tx, reason=f"meta.err={meta.get('err')!r}", rpc_endpoint=rpc_endpoint)
            return []

        try:
            # Validate raw token balance fields before calling the decoder,
            # which intentionally returns no event for malformed balances.
            token_balance_deltas(tx)
        except (AttributeError, TypeError, ValueError, OverflowError) as exc:
            self.tracker.record_transaction_status(
                signature=signature, wallet_scope=scope, status="rejected_invalid",
                raw_tx=tx, reason=str(exc), rpc_endpoint=rpc_endpoint)
            return []

        try:
            events = decode_swaps(tx, quote_prices_usd=quote_prices_usd,
                                  wallet_filter=wallet_filter)
            evidence = transaction_instruction_evidence(tx)
        except (AttributeError, TypeError, ValueError, OverflowError) as exc:
            self.tracker.record_transaction_status(
                signature=signature, wallet_scope=scope, status="rejected_invalid",
                raw_tx=tx, reason=str(exc), rpc_endpoint=rpc_endpoint)
            return []

        inserted = []
        for event in events:
            accepted = self.tracker.record_swap(
                signature=event["signature"],
                wallet=event["wallet"],
                mint=event["mint"],
                side=event["side"],
                token_amount=event["token_amount"],
                quote_usd=event.get("quote_usd"),
                fee_usd=event.get("fee_usd"),
                timestamp=event["timestamp"],
                quote_mint=event.get("quote_mint"),
                quote_amount=event.get("quote_amount"),
                quote_amount_raw=event.get("quote_amount_raw"),
                quote_decimals=event.get("quote_decimals"),
                fee_lamports=event.get("fee_lamports"),
                fee_sol=event.get("fee_sol"),
            )
            if accepted:
                inserted.append(event)
        status = "obtained_decoded" if events else "obtained_unsupported"
        reason = None if events else "; ".join(evidence.get("notes") or [evidence.get("status", "sem swap classificado")])
        self.tracker.record_transaction_status(
            signature=signature, wallet_scope=scope, status=status, raw_tx=tx,
            reason=reason, rpc_endpoint=rpc_endpoint)
        return inserted

    def reprocess_transaction(self, signature, wallet_scope="", quote_prices_usd=None):
        audit = self.tracker.get_transaction_audit(signature, wallet_scope)
        if not audit or audit.get("raw_tx") is None:
            return []
        tx = audit["raw_tx"]
        wallets = {w for w in str(wallet_scope or "").split(",") if w}
        return self.ingest_transaction(
            tx, quote_prices_usd=quote_prices_usd,
            wallet_filter=wallets if wallets else None,
            signature=signature, rpc_endpoint=audit.get("rpc_endpoint"))

    def wallet_report(self, wallet, min_closed_trades=5):
        return self.tracker.wallet_report(wallet, min_closed_trades)

    def score_recent_events(self, events, now=None, window_seconds=3600):
        wallets = {e.get("wallet") for e in events if e.get("wallet")}
        reports = {
            wallet: self.tracker.wallet_report(wallet)
            for wallet in wallets
        }
        return score_smart_money_flow(
            events,
            wallet_reports=reports,
            now=now,
            window_seconds=window_seconds,
        )
