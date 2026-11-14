# -*- coding: utf-8 -*-
"""Passive, local market snapshot storage for Smart Filters.

This module consumes DexScreener pair payloads already fetched by the bot. It
does not perform network requests and never influences analysis or trading.
At a 60-second interval it stores about 1,440 rows per observed mint/day
(about 144,000 rows/day for 100 mints). History is intentionally never pruned.
"""
from __future__ import annotations

import json
import logging
import math
import os
import queue
import sqlite3
import threading
from contextlib import closing
from datetime import datetime, timedelta, timezone


log = logging.getLogger(__name__)
SOURCE = "DexScreener"
DEFAULT_INTERVAL_SECONDS = 60
DEFAULT_STALE_AFTER_SECONDS = 60
_STOP = object()

METRIC_FIELDS = (
    "pair_address", "dex_id", "price_usd", "liquidity_usd", "market_cap_usd",
    "fdv_usd", "volume_m5", "volume_h1", "volume_h6", "volume_h24",
    "buys_m5", "sells_m5", "buys_h1", "sells_h1", "buys_h6", "sells_h6",
    "buys_h24", "sells_h24", "price_change_m5", "price_change_h1",
    "price_change_h6", "price_change_h24", "pair_created_at",
)

_FLOAT_PATHS = {
    "price_usd": ("priceUsd",),
    "liquidity_usd": ("liquidity", "usd"),
    "market_cap_usd": ("marketCap",),
    "fdv_usd": ("fdv",),
    "volume_m5": ("volume", "m5"),
    "volume_h1": ("volume", "h1"),
    "volume_h6": ("volume", "h6"),
    "volume_h24": ("volume", "h24"),
    "price_change_m5": ("priceChange", "m5"),
    "price_change_h1": ("priceChange", "h1"),
    "price_change_h6": ("priceChange", "h6"),
    "price_change_h24": ("priceChange", "h24"),
}
_COUNT_PATHS = {
    f"{side}_{window}": ("txns", window, side)
    for window in ("m5", "h1", "h6", "h24")
    for side in ("buys", "sells")
}


def utc_now():
    return datetime.now(timezone.utc)


def _utc_text(value=None):
    if value is None:
        value = utc_now()
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        value = datetime.fromtimestamp(value, timezone.utc)
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not isinstance(value, datetime):
        raise ValueError("observed_at_utc tem de ser datetime, epoch ou ISO-8601")
    if value.tzinfo is None:
        raise ValueError("observed_at_utc tem de incluir timezone")
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _path(data, path):
    value = data
    for key in path:
        if not isinstance(value, dict) or key not in value:
            return None
        value = value[key]
    return value


def _number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _integer(value):
    number = _number(value)
    if number is None or not number.is_integer():
        return None
    return int(number)


def _liquidity(pair):
    value = _number(_path(pair, ("liquidity", "usd")))
    return float("-inf") if value is None else value


def _empty_values():
    return {field: None for field in METRIC_FIELDS}


def _snapshot_row(mint, pair, observed_at, interval_seconds, previous_pair=None,
                  unavailable=False, no_pair=False, stale_after_seconds=DEFAULT_STALE_AFTER_SECONDS):
    observed_text = _utc_text(observed_at)
    observed_dt = datetime.fromisoformat(observed_text.replace("Z", "+00:00"))
    received_dt = utc_now()
    values = _empty_values()
    if isinstance(pair, dict):
        values["pair_address"] = pair.get("pairAddress") if isinstance(pair.get("pairAddress"), str) else None
        values["dex_id"] = pair.get("dexId") if isinstance(pair.get("dexId"), str) else None
        for field, path in _FLOAT_PATHS.items():
            values[field] = _number(_path(pair, path))
        for field, path in _COUNT_PATHS.items():
            values[field] = _integer(_path(pair, path))
        values["pair_created_at"] = _integer(pair.get("pairCreatedAt"))

    missing = [name for name in METRIC_FIELDS if values[name] is None]
    if unavailable:
        status = "unavailable"
        freshness = "not_observed"
    elif no_pair:
        status = "no_pair"
        freshness = "not_applicable"
    elif (received_dt - observed_dt).total_seconds() > stale_after_seconds:
        status = "stale"
        freshness = "stale_at_collection"
    elif missing:
        status = "incomplete"
        freshness = "unknown_source_timestamp"
    else:
        status = "valid"
        # DexScreener pair payloads do not carry a market-data update timestamp.
        freshness = "unknown_source_timestamp"

    epoch = observed_dt.timestamp()
    return {
        "mint": str(mint), "observed_at_utc": observed_text, "source": SOURCE,
        **values, "data_status": status,
        "missing_fields": json.dumps(missing, separators=(",", ":")),
        "freshness_status": freshness,
        "pair_changed": int(bool(previous_pair and values["pair_address"] and previous_pair != values["pair_address"])),
        "previous_pair_address": previous_pair if previous_pair != values["pair_address"] else None,
        "interval_seconds": interval_seconds,
        "sample_bucket": int(epoch // interval_seconds),
    }


class MarketSnapshotRecorder:
    """Select stable pairs and asynchronously persist observations to SQLite."""

    def __init__(self, db_path, interval_seconds=DEFAULT_INTERVAL_SECONDS,
                 stale_after_seconds=DEFAULT_STALE_AFTER_SECONDS, max_queue_size=1000):
        if not 30 <= int(interval_seconds) <= 60:
            raise ValueError("interval_seconds tem de estar entre 30 e 60")
        self.db_path = os.path.abspath(db_path)
        self.interval_seconds = int(interval_seconds)
        self.stale_after_seconds = max(1, int(stale_after_seconds))
        self._queue = queue.Queue(maxsize=max(1, int(max_queue_size)))
        self._thread = None
        self._closed = False
        folder = os.path.dirname(self.db_path)
        os.makedirs(folder, exist_ok=True)
        self._initialize()
        self._last_queued = self._load_latest_times()
        self._pair_by_mint = self._load_latest_pairs()

    def _connect(self):
        db = sqlite3.connect(self.db_path, timeout=5)
        db.execute("PRAGMA busy_timeout=5000")
        return db

    def _initialize(self):
        # The database is deliberately independent of smart_wallets.sqlite3.
        with closing(self._connect()) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("""CREATE TABLE IF NOT EXISTS market_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                mint TEXT NOT NULL,
                observed_at_utc TEXT NOT NULL,
                source TEXT NOT NULL,
                pair_address TEXT,
                dex_id TEXT,
                price_usd REAL,
                liquidity_usd REAL,
                market_cap_usd REAL,
                fdv_usd REAL,
                volume_m5 REAL,
                volume_h1 REAL,
                volume_h6 REAL,
                volume_h24 REAL,
                buys_m5 INTEGER,
                sells_m5 INTEGER,
                buys_h1 INTEGER,
                sells_h1 INTEGER,
                buys_h6 INTEGER,
                sells_h6 INTEGER,
                buys_h24 INTEGER,
                sells_h24 INTEGER,
                price_change_m5 REAL,
                price_change_h1 REAL,
                price_change_h6 REAL,
                price_change_h24 REAL,
                pair_created_at INTEGER,
                data_status TEXT NOT NULL,
                missing_fields TEXT NOT NULL,
                freshness_status TEXT NOT NULL,
                pair_changed INTEGER NOT NULL DEFAULT 0,
                previous_pair_address TEXT,
                interval_seconds INTEGER NOT NULL,
                sample_bucket INTEGER NOT NULL
            )""")
            db.execute("""CREATE UNIQUE INDEX IF NOT EXISTS uq_market_snapshot_mint_window
                ON market_snapshots(mint, interval_seconds, sample_bucket)""")
            db.execute("""CREATE INDEX IF NOT EXISTS idx_market_snapshot_mint_time
                ON market_snapshots(mint, observed_at_utc DESC)""")
            db.execute("""CREATE INDEX IF NOT EXISTS idx_market_snapshot_time
                ON market_snapshots(observed_at_utc DESC)""")
            db.commit()

    def _load_latest_times(self):
        with closing(self._connect()) as db:
            rows = db.execute("""SELECT mint, MAX(observed_at_utc) FROM market_snapshots
                WHERE interval_seconds=? GROUP BY mint""", (self.interval_seconds,)).fetchall()
        result = {}
        for mint, stamp in rows:
            try:
                result[mint] = datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()
            except (AttributeError, TypeError, ValueError):
                continue
        return result

    def _load_latest_pairs(self):
        with closing(self._connect()) as db:
            rows = db.execute("""SELECT m.mint,m.pair_address FROM market_snapshots m
                JOIN (SELECT mint,MAX(observed_at_utc) AS ts FROM market_snapshots
                      WHERE pair_address IS NOT NULL GROUP BY mint) latest
                ON latest.mint=m.mint AND latest.ts=m.observed_at_utc
                WHERE m.pair_address IS NOT NULL""").fetchall()
        return {mint: pair for mint, pair in rows if mint and pair}

    def _start_worker(self):
        if self._thread is None:
            self._thread = threading.Thread(target=self._worker, name="smart-filters-market-db", daemon=True)
            self._thread.start()

    @staticmethod
    def _insert(db, row):
        columns = tuple(row)
        sql = ("INSERT INTO market_snapshots (" + ",".join(columns) + ") VALUES (" +
               ",".join("?" for _ in columns) + ") ON CONFLICT(mint,interval_seconds,sample_bucket) DO NOTHING")
        db.execute(sql, tuple(row[name] for name in columns))

    def _worker(self):
        db = None
        try:
            db = self._connect()
            while True:
                item = self._queue.get()
                try:
                    if item is _STOP:
                        return
                    if isinstance(item, tuple) and item[0] == "barrier":
                        item[1].set()
                        continue
                    if item.get("data_status") in ("valid", "incomplete"):
                        observed = datetime.fromisoformat(item["observed_at_utc"].replace("Z", "+00:00"))
                        if (utc_now() - observed).total_seconds() > self.stale_after_seconds:
                            item = dict(item, data_status="stale", freshness_status="stale_in_queue")
                    self._insert(db, item)
                    db.commit()
                except Exception:  # persistence must never affect bot decisions
                    log.exception("Falha ao persistir snapshot Smart Filters")
                    if db is not None:
                        db.rollback()
                finally:
                    self._queue.task_done()
        except Exception:
            log.exception("Worker de snapshots Smart Filters terminou inesperadamente")
        finally:
            if db is not None:
                db.close()

    def _select_pair(self, mint, pairs):
        previous = self._pair_by_mint.get(mint)
        if not isinstance(pairs, list) or not pairs:
            return None, previous
        valid = [pair for pair in pairs if isinstance(pair, dict)]
        if not valid:
            return None, previous
        if previous:
            same = next((pair for pair in valid if pair.get("pairAddress") == previous), None)
            if same is not None:
                return same, previous
        selected = max(valid, key=_liquidity)
        return selected, previous

    def record_pairs(self, pairs_by_mint, mints, observed_at_utc=None):
        """Queue one observation per eligible mint using already-fetched payloads.

        ``None`` means the request failed; an empty list means a successful
        response with no pair. No HTTP request is made by this class.
        """
        if self._closed:
            return 0
        if observed_at_utc is None:
            observed_at_utc = utc_now()
        observed_text = _utc_text(observed_at_utc)
        try:
            observed_epoch = datetime.fromisoformat(observed_text.replace("Z", "+00:00")).timestamp()
        except ValueError:
            log.warning("Timestamp inválido para snapshot Smart Filters")
            return 0
        accepted = 0
        for mint in dict.fromkeys(str(value) for value in mints if value):
            last = self._last_queued.get(mint)
            if last is not None and observed_epoch - last < self.interval_seconds:
                continue
            response = pairs_by_mint.get(mint) if isinstance(pairs_by_mint, dict) else None
            unavailable = response is None
            no_pair = isinstance(response, list) and not response
            pair, previous = self._select_pair(mint, response) if not unavailable and not no_pair else (None, self._pair_by_mint.get(mint))
            row = _snapshot_row(mint, pair, observed_text, self.interval_seconds,
                                previous_pair=previous, unavailable=unavailable,
                                no_pair=no_pair, stale_after_seconds=self.stale_after_seconds)
            if not pair and not unavailable and not no_pair:
                row["data_status"] = "incomplete"
                row["freshness_status"] = "unknown_source_timestamp"
            try:
                self._start_worker()
                self._queue.put_nowait(row)
            except queue.Full:
                log.warning("Fila de snapshots cheia; observação descartada para %s", mint)
                continue
            except Exception:
                log.exception("Não foi possível enfileirar snapshot para %s", mint)
                continue
            self._last_queued[mint] = observed_epoch
            if row.get("pair_address"):
                self._pair_by_mint[mint] = row["pair_address"]
            accepted += 1
        return accepted

    def flush(self, timeout=10):
        """Wait for queued writes; intended for shutdown and tests, never scan cycles."""
        if self._thread is None:
            return True
        event = threading.Event()
        try:
            self._queue.put(("barrier", event), timeout=max(0.0, timeout))
        except queue.Full:
            return False
        return event.wait(timeout=max(0.0, timeout))

    def close(self, timeout=10):
        if self._closed:
            return
        self._closed = True
        if self._thread is None:
            return
        try:
            self._queue.put(_STOP, timeout=max(0.0, timeout))
            self._thread.join(timeout=max(0.0, timeout))
        except queue.Full:
            log.warning("Fila Smart Filters cheia durante encerramento; worker continua em daemon")


__all__ = ["MarketSnapshotRecorder", "DEFAULT_INTERVAL_SECONDS", "METRIC_FIELDS"]
