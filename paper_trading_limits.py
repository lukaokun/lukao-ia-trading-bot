"""Operational Paper Trading limits and the ECB EUR/USD reference rate."""

from __future__ import annotations

import csv
import io
import json
import os
import stat
import threading
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_DOWN

import requests


ECB_USD_EUR_URL = (
    "https://data-api.ecb.europa.eu/service/data/EXR/"
    "D.USD.EUR.SP00.A?lastNObservations=1&format=csvdata"
)
ECB_SOURCE = "European Central Bank (ECB) daily reference rate"
MAX_RATE_AGE_DAYS = 4
MAX_OPERATION_EUR = Decimal("5")
MAX_OPEN_POSITIONS = 5  # teto absoluto (autorizado a 19/11/2026); a config escolhe entre 1 e 5
# Maximum approved by the operator on 2026-11-14 (Paper only): 3% total simulated
# slippage per entry (2% base + market impact). A missing config cap still blocks proposals.
APPROVED_MAX_SLIPPAGE_PCT = Decimal("3")
USD_PRECISION = Decimal("0.0001")

_LOCK_REGISTRY_GUARD = threading.Lock()
_THREAD_LOCKS = {}
_LOCK_OWNERS = {}


def canonical_state_path(state_path):
    """Canonical key so relative paths and aliases share one instance lock."""
    return os.path.normcase(os.path.realpath(os.path.abspath(state_path)))


def _validate_state_link_count(state_path):
    """Fail closed if the existing Paper state has more than one hard link."""
    try:
        metadata = os.stat(state_path)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise ResetPathError(f"nao foi possivel validar o estado Paper antes do lock: {exc}") from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise ResetPathError(f"estado Paper nao e um ficheiro regular: {state_path}")
    if getattr(metadata, "st_nlink", 1) > 1:
        raise ResetPathError(
            f"estado Paper tem {metadata.st_nlink} hard links; operacao recusada: {state_path}"
        )


class ResetPathError(RuntimeError):
    """Reset paths are aliased, unsupported, or unsafe to mutate."""


def _is_reparse_stat(metadata):
    attributes = getattr(metadata, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return stat.S_ISLNK(metadata.st_mode) or bool(attributes & reparse_flag)


def _reject_reparse_components(path, label):
    """Reject symlinks/junctions in an existing path or any of its parents."""
    raw_path = os.fspath(path)
    if not os.path.isabs(raw_path):
        raw_path = os.path.join(os.getcwd(), raw_path)
    drive, tail = os.path.splitdrive(raw_path)
    current = drive + os.sep if drive else os.sep
    if os.altsep:
        tail = tail.replace(os.altsep, os.sep)
    components = [part for part in tail.split(os.sep) if part]
    for component in components:
        if component == ".":
            continue
        if component == "..":
            parent = os.path.dirname(current.rstrip(os.sep))
            current = parent or current
            continue
        current = os.path.join(current, component)
        try:
            metadata = os.lstat(current)
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise ResetPathError(f"nao foi possivel validar o caminho {label}: {current}: {exc}") from exc
        if _is_reparse_stat(metadata):
            raise ResetPathError(f"caminho {label} usa symlink/reparse point nao suportado: {current}")


def _require_path_kind(path, label, expected, *, allow_missing=True):
    if not os.path.lexists(path):
        if allow_missing:
            return
        raise ResetPathError(f"caminho {label} inexistente: {path}")
    metadata = os.lstat(path)
    if _is_reparse_stat(metadata):
        raise ResetPathError(f"caminho {label} e symlink/reparse point nao suportado: {path}")
    valid = stat.S_ISREG(metadata.st_mode) if expected == "file" else stat.S_ISDIR(metadata.st_mode)
    if not valid:
        raise ResetPathError(f"caminho {label} nao e {expected}: {path}")
    if expected == "file" and getattr(metadata, "st_nlink", 1) > 1:
        raise ResetPathError(f"caminho {label} tem hard links adicionais: {path}")


def _validate_journal_tree(path):
    """Validate the whole journal tree without following or moving reparse points."""
    def raise_walk_error(error):
        raise ResetPathError(f"nao foi possivel validar o diario: {error}") from error

    for root, directories, files in os.walk(path, followlinks=False, onerror=raise_walk_error):
        for name in [*directories, *files]:
            child = os.path.join(root, name)
            metadata = os.lstat(child)
            if _is_reparse_stat(metadata):
                raise ResetPathError(f"diario contem symlink/reparse point nao suportado: {child}")
            if name in directories and not stat.S_ISDIR(metadata.st_mode):
                raise ResetPathError(f"entrada de diretorio invalida no diario: {child}")
            if name in files and not stat.S_ISREG(metadata.st_mode):
                raise ResetPathError(f"ficheiro especial nao suportado no diario: {child}")
            if name in files and getattr(metadata, "st_nlink", 1) > 1:
                raise ResetPathError(f"ficheiro no diario tem hard links adicionais: {child}")


def canonical_reset_paths(state_path, trades_path):
    """Resolve and preflight every path the reset will use, without writing."""
    _reject_reparse_components(state_path, "estado")
    _reject_reparse_components(trades_path, "CSV")
    state = canonical_state_path(state_path)
    trades = canonical_state_path(trades_path)
    if state == trades:
        raise ResetPathError("estado e CSV resolvem para a mesma localizacao")

    paths = {
        "state": state,
        "trades": trades,
        "lock": state + ".paper.lock",
        "journal": state + ".paper_operations",
        "marker": state + ".reset.pending.json",
        "archive_root": os.path.join(os.path.dirname(state), "reset_archives"),
    }
    for label in ("state", "trades", "lock", "journal", "marker", "archive_root"):
        _reject_reparse_components(paths[label], label)
    for left_index, left in enumerate(paths):
        for right in list(paths)[left_index + 1:]:
            left_path, right_path = paths[left], paths[right]
            if left_path == right_path:
                raise ResetPathError(f"caminhos {left} e {right} colidem: {left_path}")
            if os.path.lexists(left_path) and os.path.lexists(right_path):
                try:
                    if os.path.samefile(left_path, right_path):
                        raise ResetPathError(
                            f"caminhos {left} e {right} referem o mesmo ficheiro/diretorio"
                        )
                except OSError as exc:
                    raise ResetPathError(
                        f"nao foi possivel comparar caminhos {left} e {right}"
                    ) from exc
            try:
                common = os.path.commonpath((left_path, right_path))
            except ValueError:
                # Different volumes are allowed when paths are distinct; ancestry
                # cannot exist across volumes, and each file is replaced locally.
                continue
            if common in (left_path, right_path):
                raise ResetPathError(f"caminhos {left} e {right} sobrepoem-se")

    _require_path_kind(state, "estado", "file")
    _require_path_kind(trades, "CSV", "file")
    _require_path_kind(paths["lock"], "lock", "file")
    _require_path_kind(paths["marker"], "marcador", "file")
    _require_path_kind(paths["journal"], "diario", "directory")
    _require_path_kind(paths["archive_root"], "arquivo", "directory")
    _validate_state_link_count(state)
    if os.path.isdir(paths["journal"]):
        _validate_journal_tree(paths["journal"])
    return paths


class OperationalConfigError(ValueError):
    pass


class EcbRateError(ValueError):
    pass


def load_operational_config(path):
    """Load and strictly validate the operational Paper-only controls."""
    try:
        with open(path, "r", encoding="utf-8") as source:
            data = json.load(source)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise OperationalConfigError(f"config_operacoes.json indisponivel ou invalido: {exc}") from exc
    if not isinstance(data, dict):
        raise OperationalConfigError("config_operacoes.json tem de conter um objeto JSON")
    if data.get("mode") != "paper":
        raise OperationalConfigError("mode tem de ser paper")
    if data.get("live_trading_enabled") is not False:
        raise OperationalConfigError("live_trading_enabled tem de ser false")
    if data.get("private_keys_in_application") is not False:
        raise OperationalConfigError("private_keys_in_application tem de ser false")
    if data.get("manual_confirmation_required") is not True:
        raise OperationalConfigError("manual_confirmation_required tem de ser true")
    required_true = (
        "reject_duplicate_orders",
        "require_cost_estimate_before_approval",
        "require_slippage_estimate_before_approval",
        "block_if_costs_or_minimum_order_cannot_be_verified",
        "emergency_stop_enabled",
    )
    for key in required_true:
        if data.get(key) is not True:
            raise OperationalConfigError(f"{key} tem de ser true")

    try:
        eur_limit = Decimal(str(data["max_eur_per_operation"]))
    except (KeyError, InvalidOperation, TypeError, ValueError) as exc:
        raise OperationalConfigError("max_eur_per_operation invalido") from exc
    if not eur_limit.is_finite() or eur_limit <= 0 or eur_limit > MAX_OPERATION_EUR:
        raise OperationalConfigError("max_eur_per_operation tem de ser > 0 e <= 5 EUR")

    raw_slippage_cap = data.get("max_slippage_pct")
    slippage_cap = None
    if APPROVED_MAX_SLIPPAGE_PCT is None:
        if raw_slippage_cap is not None:
            raise OperationalConfigError(
                "max_slippage_pct foi indicado, mas ainda não existe limite máximo aprovado"
            )
    else:
        try:
            slippage_cap = Decimal(str(raw_slippage_cap))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise OperationalConfigError("max_slippage_pct inválido ou não aprovado") from exc
        if (not slippage_cap.is_finite() or slippage_cap <= 0
                or slippage_cap > APPROVED_MAX_SLIPPAGE_PCT):
            raise OperationalConfigError("max_slippage_pct excede ou viola o limite aprovado")

    count = data.get("max_open_positions")
    if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= MAX_OPEN_POSITIONS:
        raise OperationalConfigError(f"max_open_positions tem de ser um inteiro entre 1 e {MAX_OPEN_POSITIONS}")

    return {
        "mode": "paper",
        "live_trading_enabled": False,
        "private_keys_in_application": False,
        "manual_confirmation_required": True,
        "max_eur_per_operation": eur_limit,
        "max_open_positions": count,
        "reject_duplicate_orders": True,
        "require_cost_estimate_before_approval": True,
        "require_slippage_estimate_before_approval": True,
        "block_if_costs_or_minimum_order_cannot_be_verified": True,
        "emergency_stop_enabled": True,
        "max_slippage_pct": slippage_cap,
        "source_path": os.path.abspath(path),
    }


def validate_rate(rate, reference_date, today=None, max_age_days=MAX_RATE_AGE_DAYS):
    """Validate a stored/fetched USD-per-EUR quote and its ECB reference date."""
    try:
        value = Decimal(str(rate))
        observed = date.fromisoformat(str(reference_date))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise EcbRateError("taxa BCE ou data de referencia invalida") from exc
    today = today or datetime.now().astimezone().date()
    if not value.is_finite() or value <= 0:
        raise EcbRateError("taxa BCE tem de ser positiva e finita")
    if observed > today:
        raise EcbRateError("data da taxa BCE esta no futuro")
    if (today - observed).days > max_age_days:
        raise EcbRateError("taxa BCE demasiado antiga para novas compras")
    return value


def validate_locked_session_rate(session, today=None, max_age_days=MAX_RATE_AGE_DAYS):
    """Require a complete, sourced, timezone-aware persisted session lock."""
    if not isinstance(session, dict) or not session.get("id"):
        raise EcbRateError("sessao cambial ausente")
    if session.get("fx_source") != ECB_SOURCE:
        raise EcbRateError("origem da taxa fixada nao corresponde ao BCE")
    try:
        locked_at = datetime.fromisoformat(str(session.get("fx_locked_at_utc", "")).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise EcbRateError("timestamp da taxa fixada invalido") from exc
    if locked_at.tzinfo is None or locked_at.utcoffset() is None:
        raise EcbRateError("timestamp da taxa fixada sem fuso horario")
    if locked_at.astimezone(timezone.utc) > datetime.now(timezone.utc) + timedelta(minutes=5):
        raise EcbRateError("timestamp da taxa fixada esta no futuro")
    return validate_rate(session.get("fx_usd_per_eur"), session.get("fx_reference_date"),
                         today=today, max_age_days=max_age_days)


def parse_ecb_rate_csv(payload, today=None, max_age_days=MAX_RATE_AGE_DAYS):
    """Parse the ECB SDMX CSV response without substituting missing values."""
    if not isinstance(payload, str) or not payload.strip():
        raise EcbRateError("resposta BCE vazia")
    reader = csv.DictReader(io.StringIO(payload.lstrip("\ufeff")))
    names = {name.upper() for name in (reader.fieldnames or [])}
    required = {"TIME_PERIOD", "OBS_VALUE", "CURRENCY", "CURRENCY_DENOM", "EXR_TYPE", "EXR_SUFFIX"}
    if not required.issubset(names):
        raise EcbRateError("resposta BCE sem as dimensoes esperadas")

    candidates = []
    for row in reader:
        normalized = {str(key).upper(): value for key, value in row.items() if key is not None}
        if (normalized.get("CURRENCY") != "USD" or normalized.get("CURRENCY_DENOM") != "EUR"
                or normalized.get("EXR_TYPE") != "SP00" or normalized.get("EXR_SUFFIX") != "A"):
            continue
        period = normalized.get("TIME_PERIOD")
        raw_value = normalized.get("OBS_VALUE")
        if not period or not raw_value:
            continue
        try:
            day = date.fromisoformat(period)
            value = Decimal(raw_value)
        except (InvalidOperation, TypeError, ValueError):
            continue
        if value.is_finite() and value > 0:
            candidates.append((day, value))
    if not candidates:
        raise EcbRateError("resposta BCE sem observacao USD/EUR valida")
    observed, value = max(candidates, key=lambda item: item[0])
    validate_rate(value, observed.isoformat(), today=today, max_age_days=max_age_days)
    return {"rate_usd_per_eur": format(value, "f"), "reference_date": observed.isoformat(),
            "source": ECB_SOURCE}


def fetch_ecb_rate(get=None, today=None, timeout=5):
    """Fetch one public daily ECB observation. Network failures fail closed."""
    get = get or requests.get
    try:
        response = get(ECB_USD_EUR_URL, timeout=timeout,
                       headers={"Accept": "text/csv", "User-Agent": "paper-bot/1.0"})
    except requests.RequestException as exc:
        raise EcbRateError(f"consulta BCE falhou: {type(exc).__name__}") from exc
    if response.status_code != 200:
        raise EcbRateError(f"consulta BCE devolveu HTTP {response.status_code}")
    try:
        return parse_ecb_rate_csv(response.text, today=today)
    except (AttributeError, UnicodeError) as exc:
        raise EcbRateError("resposta BCE nao pode ser lida") from exc


def usd_cap_from_eur(eur_amount, usd_per_eur):
    """Convert the EUR ceiling down to USD cents-of-cent precision."""
    try:
        amount = Decimal(str(eur_amount))
        rate = Decimal(str(usd_per_eur))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise EcbRateError("valor EUR/USD invalido") from exc
    if not amount.is_finite() or amount <= 0 or not rate.is_finite() or rate <= 0:
        raise EcbRateError("valor EUR/USD tem de ser positivo e finito")
    return (amount * rate).quantize(USD_PRECISION, rounding=ROUND_DOWN)


class ProcessInstanceLock:
    """Non-blocking OS lock held for the bot process lifetime."""

    def __init__(self, state_path):
        self.state_path = canonical_state_path(state_path)
        self.path = self.state_path + ".paper.lock"
        self._file = None
        self._locked = False
        self.error = None
        self._owner_thread_id = None
        with _LOCK_REGISTRY_GUARD:
            self._thread_lock = _THREAD_LOCKS.setdefault(self.state_path, threading.Lock())
        self._thread_acquired = False

    def acquire(self):
        if self._locked:
            return self._owner_thread_id == threading.get_ident()
        try:
            _validate_state_link_count(self.state_path)
        except ResetPathError as exc:
            self.error = str(exc)
            return False
        if not self._thread_lock.acquire(blocking=False):
            self.error = "estado Paper ja bloqueado por outra thread"
            return False
        self._thread_acquired = True
        try:
            _reject_reparse_components(self.path, "lock")
            _require_path_kind(self.path, "lock", "file")
            self._file = open(self.path, "a+b")
            self._file.seek(0, os.SEEK_END)
            if self._file.tell() == 0:
                self._file.write(b"\0")
                self._file.flush()
            self._file.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self._file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._locked = True
            self._owner_thread_id = threading.get_ident()
            with _LOCK_REGISTRY_GUARD:
                _LOCK_OWNERS[self.state_path] = self._owner_thread_id
            try:
                _validate_state_link_count(self.state_path)
            except ResetPathError as exc:
                self.error = str(exc)
                self.release()
                return False
            self.error = None
            return True
        except (OSError, ImportError, ResetPathError) as exc:
            self.error = str(exc)
            if self._file is not None:
                self._file.close()
            self._file = None
            if self._thread_acquired:
                self._thread_lock.release()
                self._thread_acquired = False
            return False

    def release(self):
        if not self._locked or self._file is None:
            return
        if self._owner_thread_id != threading.get_ident():
            raise RuntimeError("instance lock must be released by its owning thread")
        try:
            if os.name == "nt":
                import msvcrt
                self._file.seek(0)
                msvcrt.locking(self._file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
        finally:
            self._file.close()
            self._file = None
            self._locked = False
            with _LOCK_REGISTRY_GUARD:
                if _LOCK_OWNERS.get(self.state_path) == self._owner_thread_id:
                    _LOCK_OWNERS.pop(self.state_path, None)
            self._owner_thread_id = None
            if self._thread_acquired:
                self._thread_lock.release()
                self._thread_acquired = False

    @property
    def acquired(self):
        return self._locked and self._file is not None


def state_lock_held_by_current_thread(state_path):
    key = canonical_state_path(state_path)
    with _LOCK_REGISTRY_GUARD:
        return _LOCK_OWNERS.get(key) == threading.get_ident()


@contextmanager
def state_access_lock(state_path):
    """Reuse the current owner lock or acquire a temporary guard for direct helpers."""
    if state_lock_held_by_current_thread(state_path):
        yield
        return
    lock = ProcessInstanceLock(state_path)
    if not lock.acquire():
        raise RuntimeError("estado Paper ja esta bloqueado por outra instancia/thread")
    try:
        yield
    finally:
        lock.release()

