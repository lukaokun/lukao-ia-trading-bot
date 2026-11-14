# -*- coding: utf-8 -*-
"""Conservative decoder and evidence inspector for Solana transactions.

Owner-level token deltas are reported as net-balance pairs, not as proof of a
swap.  A separate instruction-evidence view recognizes only a small set of
Jupiter, Raydium AMM, Orca Whirlpool and SPL Token patterns.  Unknown or
incomplete instruction sequences remain explicitly unclassified.
"""
from collections import defaultdict
from decimal import Decimal, InvalidOperation, localcontext
import math
import re

WSOL = "So11111111111111111111111111111111111111112"
SOL = "SOL_NATIVE"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"
JUPITER = "routeUGWgWzqBWFcrCfv8tritsqukccJPu3q5GPP3xS"
RAYDIUM_AMM = "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8"
ORCA_WHIRLPOOL = "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK"
TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022_PROGRAM = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
ASSOCIATED_TOKEN_PROGRAM = "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"
SYSTEM_PROGRAM = "11111111111111111111111111111111"
COMPUTE_BUDGET_PROGRAM = "ComputeBudget111111111111111111111111111111"
ORCA_SWAP_V2_DISCRIMINATOR = bytes.fromhex("2b04ed0b1ac91e62")
_BASE58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def is_solana_address(value):
    """Validate a base58-encoded 32-byte public key without network access."""
    if not isinstance(value, str) or not value or any(c not in _BASE58 for c in value):
        return False
    number = 0
    for char in value:
        number = number * 58 + _BASE58.index(char)
    decoded = (number.to_bytes((number.bit_length() + 7) // 8, "big")
               if number else b"")
    leading_zeroes = len(value) - len(value.lstrip("1"))
    return leading_zeroes + len(decoded) == 32


def _amount_raw(entry):
    ui = entry.get("uiTokenAmount") or {}
    if not isinstance(ui, dict):
        raise ValueError("uiTokenAmount tem formato inválido")

    if "amount" in ui:
        raw = ui["amount"]
        if isinstance(raw, bool) or not isinstance(raw, (int, str)):
            raise ValueError("uiTokenAmount.amount não é um inteiro válido")
        try:
            amount = int(raw)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("uiTokenAmount.amount não é um inteiro válido") from exc
        if amount < 0 or (isinstance(raw, str) and not re.fullmatch(r"[0-9]+", raw.strip())):
            raise ValueError("uiTokenAmount.amount está fora do formato inteiro não negativo")
        return amount

    if "decimals" not in ui:
        raise ValueError("uiTokenAmount.decimals ausente para converter saldo decimal")
    decimals_raw = ui["decimals"]
    if isinstance(decimals_raw, bool) or not isinstance(decimals_raw, int) or decimals_raw < 0:
        raise ValueError("uiTokenAmount.decimals inválido")
    decimals = decimals_raw

    if "uiAmountString" in ui:
        value = ui["uiAmountString"]
        source = "uiAmountString"
    elif "uiAmount" in ui:
        value = ui["uiAmount"]
        source = "uiAmount"
    else:
        raise ValueError("saldo sem amount, uiAmountString ou uiAmount")
    if isinstance(value, bool) or value is None:
        raise ValueError(f"{source} inválido")
    try:
        decimal_value = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError(f"{source} não é um número decimal válido") from exc
    if not decimal_value.is_finite() or decimal_value < 0:
        raise ValueError(f"{source} não é um número decimal finito não negativo")

    # Raise precision so Decimal multiplication remains exact for large raw
    # balances instead of silently rounding using the default context.
    precision = max(28, len(decimal_value.as_tuple().digits) + decimals + 2)
    with localcontext() as context:
        context.prec = precision
        scaled = decimal_value * (10 ** decimals)
    integral = scaled.to_integral_value()
    if scaled != integral:
        raise ValueError(f"{source} tem mais casas decimais do que decimals permite")
    return int(integral)


def _token_balance_raw_deltas(tx):
    meta = tx.get("meta") or {}
    pre, post, decimals = {}, {}, {}
    for target, entries in ((pre, meta.get("preTokenBalances") or []),
                            (post, meta.get("postTokenBalances") or [])):
        for entry in entries:
            owner, mint = entry.get("owner"), entry.get("mint")
            if not is_solana_address(owner) or not is_solana_address(mint):
                continue
            key = (str(owner), str(mint))
            target[key] = target.get(key, 0) + _amount_raw(entry)
            decimals[mint] = int((entry.get("uiTokenAmount") or {}).get("decimals", 0))
    deltas = {key: post.get(key, 0) - pre.get(key, 0)
              for key in set(pre) | set(post)}
    return {k: v for k, v in deltas.items() if v}, decimals


def token_balance_deltas(tx):
    """Return {(owner,mint): post-pre}; ignores entries without owner/mint."""
    raw, decimals = _token_balance_raw_deltas(tx)
    return {k: v / (10 ** decimals.get(k[1], 0)) for k, v in raw.items()}


def transaction_fee(tx):
    """Return exact fee metadata; USD remains unknown without historical pricing."""
    raw = (tx.get("meta") or {}).get("fee")
    lamports = None
    if isinstance(raw, int) and not isinstance(raw, bool) and raw >= 0:
        lamports = raw
    return {
        "fee_lamports": lamports,
        "fee_sol": None if lamports is None else lamports / 1_000_000_000,
        "fee_usd": None,
    }


def _pubkey(key):
    return key.get("pubkey") if isinstance(key, dict) else key


def _account_keys(tx):
    message = ((tx.get("transaction") or {}).get("message") or {})
    keys = message.get("accountKeys") or []
    # Standard getTransaction responses already flatten lookup-table addresses
    # into accountKeys. Keep that order; use loadedAddresses only as fallback.
    result = [_pubkey(key) for key in keys]
    has_lookup_keys = any(isinstance(key, dict) and key.get("source") == "lookupTable"
                          for key in keys)
    if not has_lookup_keys:
        loaded = (tx.get("meta") or {}).get("loadedAddresses") or {}
        result.extend(loaded.get("writable") or [])
        result.extend(loaded.get("readonly") or [])
    return result


def _instruction_program(ix, keys):
    program = ix.get("programId")
    if program:
        return program
    index = ix.get("programIdIndex")
    if isinstance(index, int) and 0 <= index < len(keys):
        return keys[index]
    return None


def _decode_base58(value):
    if not isinstance(value, str) or any(c not in _BASE58 for c in value):
        return b""
    number = 0
    for char in value:
        number = number * 58 + _BASE58.index(char)
    body = number.to_bytes((number.bit_length() + 7) // 8, "big") if number else b""
    return b"\0" * (len(value) - len(value.lstrip("1"))) + body


def _token_account_metadata(tx, keys):
    mints, owners, decimals = {}, {}, {}
    meta = tx.get("meta") or {}
    for entry in (meta.get("preTokenBalances") or []) + (meta.get("postTokenBalances") or []):
        index = entry.get("accountIndex")
        if isinstance(index, int) and 0 <= index < len(keys):
            account = keys[index]
            mints[account] = entry.get("mint")
            owners[account] = entry.get("owner")
            decimals[entry.get("mint")] = (entry.get("uiTokenAmount") or {}).get("decimals")
    # Temporary token accounts may be created and closed in one transaction,
    # so they are absent from pre/post token balances. Their parsed init/create
    # instructions still identify their mint and owner.
    for group in meta.get("innerInstructions") or []:
        for ix in group.get("instructions") or []:
            info = ((ix.get("parsed") or {}).get("info") or {})
            kind = (ix.get("parsed") or {}).get("type")
            account = info.get("account")
            mint = info.get("mint")
            owner = info.get("owner") or info.get("wallet")
            if account and mint:
                mints[account] = mint
                if owner:
                    owners[account] = owner
                if mint == WSOL:
                    decimals[mint] = 9
    return mints, owners, decimals


def _flatten_instructions(tx, keys):
    message = ((tx.get("transaction") or {}).get("message") or {})
    meta = tx.get("meta") or {}
    inner = {group.get("index"): group.get("instructions") or []
             for group in (meta.get("innerInstructions") or [])}
    rows = []
    for outer_index, ix in enumerate(message.get("instructions") or []):
        rows.append(("outer", outer_index, ix, _instruction_program(ix, keys)))
        for inner_index, child in enumerate(inner.get(outer_index, [])):
            rows.append(("inner", f"{outer_index}.{inner_index}", child,
                         _instruction_program(child, keys)))
    return rows


def _classify_instruction(ix, program, token_mints, token_decimals):
    parsed = ix.get("parsed") or {}
    kind = parsed.get("type")
    info = parsed.get("info") or {}
    name = None
    category = "unsupported"
    raw = _decode_base58(ix.get("data"))
    if program == JUPITER:
        name, category = "router_instruction", "swap_router"
    elif program == RAYDIUM_AMM:
        if raw[:1] == b"\x09":
            name, category = "swap_base_in", "swap_instruction"
        else:
            name, category = "unparsed_raydium_instruction", "unsupported"
    elif program == ORCA_WHIRLPOOL:
        if raw.startswith(ORCA_SWAP_V2_DISCRIMINATOR) or kind == "swapV2":
            name, category = "swap_v2", "swap_instruction"
        else:
            name, category = "unparsed_orca_instruction", "unsupported"
    elif program == ASSOCIATED_TOKEN_PROGRAM:
        name = kind or "associated_token_instruction"
        category = "account_creation" if kind and "create" in kind.lower() else "account_lifecycle"
    elif program == SYSTEM_PROGRAM:
        name = kind or "system_instruction"
        if kind == "createAccount":
            category = "account_funding"
        elif kind == "transfer":
            category = "sol_transfer"
        else:
            category = "system_instruction"
    elif program == TOKEN_PROGRAM or program == TOKEN_2022_PROGRAM:
        name = kind or "spl_token_instruction"
        if kind in ("transfer", "transferChecked"):
            category = "token_transfer"
        elif kind in ("closeAccount",):
            category = "account_closure"
        elif kind and (kind.startswith("initialize") or kind == "getAccountDataSize"):
            category = "account_lifecycle"
        else:
            category = "token_instruction"
    elif program == COMPUTE_BUDGET_PROGRAM and raw:
        tags = {2: "set_compute_unit_limit", 3: "set_compute_unit_price"}
        name = tags.get(raw[0], "unknown_compute_budget_instruction")
        category = "compute_budget"
    else:
        name = kind or "unknown_instruction"

    accounts = ix.get("accounts") or []
    if not accounts and isinstance(info, dict):
        accounts = [value for key, value in info.items()
                    if key.lower() in ("account", "source", "destination", "owner", "wallet", "authority", "mint", "newaccount")
                    and isinstance(value, str)]
    item = {"program_id": program, "program": _program_label(program),
            "type": name, "category": category, "accounts": accounts}
    if program == COMPUTE_BUDGET_PROGRAM and raw:
        if raw[0] == 2 and len(raw) >= 5:
            item["compute_unit_limit"] = int.from_bytes(raw[1:5], "little")
        elif raw[0] == 3 and len(raw) >= 9:
            item["compute_unit_price_micro_lamports"] = int.from_bytes(raw[1:9], "little")
    if info:
        for field in ("source", "destination", "authority", "owner", "wallet", "account", "newAccount", "mint", "lamports"):
            if field in info:
                item[field[0].lower() + field[1:]] = info[field]
        if kind in ("transfer", "transferChecked"):
            token_amount = info.get("tokenAmount") or {}
            raw_amount = token_amount.get("amount", info.get("amount"))
            if raw_amount is not None:
                try:
                    raw_amount = int(raw_amount)
                except (TypeError, ValueError):
                    pass
            mint = info.get("mint") or token_mints.get(info.get("source")) or token_mints.get(info.get("destination"))
            decimals = token_amount.get("decimals")
            if decimals is None:
                decimals = token_decimals.get(mint)
            item["mint"] = mint
            item["amount_raw"] = raw_amount
            if raw_amount is not None and isinstance(decimals, int):
                item["amount"] = int(raw_amount) / (10 ** decimals)
            elif token_amount.get("uiAmountString") is not None:
                item["amount"] = float(token_amount["uiAmountString"])
        if kind == "createAccount":
            item["funding_lamports"] = info.get("lamports")
            item["rent_candidate"] = (info.get("space") == 165 and
                                      info.get("owner") in (TOKEN_PROGRAM, TOKEN_2022_PROGRAM))
            if isinstance(info.get("lamports"), int):
                item["funding_sol"] = info["lamports"] / 1_000_000_000
    if raw and program == RAYDIUM_AMM and raw[:1] == b"\x09" and len(raw) >= 9:
        item["amount_in_raw"] = int.from_bytes(raw[1:9], "little")
    if raw.startswith(ORCA_SWAP_V2_DISCRIMINATOR) and len(raw) >= 16:
        specified = int.from_bytes(raw[8:16], "little")
        item.update({"amount_specified_raw": specified,
                     "other_amount_threshold_raw": int.from_bytes(raw[16:24], "little") if len(raw) >= 24 else None,
                     "sqrt_price_limit_raw": int.from_bytes(raw[24:40], "little") if len(raw) >= 40 else None,
                     "amount_specified_is_input": bool(raw[40]) if len(raw) >= 41 else None})
        item["specified_amount_role"] = (
            "input" if item["amount_specified_is_input"] is True else
            "output" if item["amount_specified_is_input"] is False else "unknown")
        if item["amount_specified_is_input"] is True:
            item["amount_in_raw"] = specified
    return item


def _program_label(program):
    return {
        JUPITER: "jupiter_route", RAYDIUM_AMM: "raydium_amm",
        ORCA_WHIRLPOOL: "orca_whirlpool", TOKEN_PROGRAM: "spl_token",
        TOKEN_2022_PROGRAM: "spl_token_2022",
        ASSOCIATED_TOKEN_PROGRAM: "spl_associated_token_account",
        SYSTEM_PROGRAM: "system", COMPUTE_BUDGET_PROGRAM: "compute_budget",
    }.get(program, "unknown")


def transaction_instruction_evidence(tx):
    """Describe the supported instruction evidence without inventing a route."""
    meta = tx.get("meta") or {}
    keys = _account_keys(tx)
    token_mints, token_owners, decimals = _token_account_metadata(tx, keys)
    instructions = []
    for sequence, (scope, index, ix, program) in enumerate(_flatten_instructions(tx, keys)):
        item = _classify_instruction(ix, program, token_mints, decimals)
        item.update({"scope": scope, "index": index})
        item["sequence"] = sequence
        instructions.append(item)

    fee = transaction_fee(tx)
    result = {"status": "unsupported", "route": [], "instructions": instructions,
              "fee": fee, "movements": [], "lamport_deltas": [], "notes": []}
    if meta.get("err") is not None:
        result.update(status="failed", notes=["meta.err indica que a transação falhou."])
        return result

    moves = [item for item in instructions if item["category"] == "token_transfer"]
    # The owner-level delta is useful reconciliation evidence, but is not itself
    # treated as a swap instruction.
    for move in moves:
        mint = move.get("mint")
        source_owner = token_owners.get(move.get("source"))
        destination_owner = token_owners.get(move.get("destination"))
        if move.get("mint") is None:
            move["movement_kind"] = "ambiguous_token_transfer"
        elif mint == WSOL:
            move["movement_kind"] = "wsol_transfer"
        else:
            move["movement_kind"] = "token_transfer"
        move["source_owner"] = source_owner
        move["destination_owner"] = destination_owner
        result["movements"].append(move)
    result["movements"].extend(item for item in instructions
                                if item["category"] in ("account_funding", "account_creation", "account_closure", "sol_transfer"))

    # Explain net lamport movements separately from token movements. WSOL
    # accounts are lamport-backed token accounts; their changes are not
    # classified as native SOL swap legs.
    message = ((tx.get("transaction") or {}).get("message") or {})
    pre_balances, post_balances = meta.get("preBalances") or [], meta.get("postBalances") or []
    signer_indices = {i for i, key in enumerate(message.get("accountKeys") or [])
                      if isinstance(key, dict) and key.get("signer")}
    for index, (pre, post) in enumerate(zip(pre_balances, post_balances)):
        delta = post - pre
        if not delta or index >= len(keys):
            continue
        account = keys[index]
        if token_mints.get(account) == WSOL:
            kind = "wsol_lamport_backing"
        elif (index in signer_indices and fee["fee_lamports"] is not None and
              delta == -fee["fee_lamports"]):
            kind = "transaction_fee"
        else:
            kind = "unattributed_lamport_movement"
        result["lamport_deltas"].append({"account_index": index, "account": account,
                                        "delta_lamports": delta,
                                        "delta_sol": delta / 1_000_000_000,
                                        "kind": kind})

    programs = [item["program_id"] for item in instructions]
    logs = "\n".join(meta.get("logMessages") or [])
    base_in = "process_swap_base_in_with_user_account" in logs
    base_out = "process_swap_base_out_with_user_account" in logs
    router_present = JUPITER in programs and (base_in or base_out)
    if JUPITER in programs and base_in and base_out:
        result.update(status="ambiguous", notes=["Foram detetados padrões Jupiter base_in e base_out na mesma transação."])
        return result
    jupiter_limits = None
    if router_present:
        if base_in:
            match = re.search(r"process_swap_base_in_with_user_account:.*?amount_in: (\d+), minimum_amount_out: (\d+)", logs)
            if match:
                jupiter_limits = {"pattern": "base_in", "amount_in_raw": int(match.group(1)),
                                  "minimum_amount_out_raw": int(match.group(2))}
        else:
            match = re.search(r"process_swap_base_out_with_user_account:.*?max_amount_in: (\d+), amount_out: (\d+)", logs)
            if match:
                jupiter_limits = {"pattern": "base_out", "maximum_amount_in_raw": int(match.group(1)),
                                  "amount_out_raw": int(match.group(2))}
    ray_ix = [item for item in instructions if item["program_id"] == RAYDIUM_AMM and item["type"] == "swap_base_in"]
    orca_ix = [item for item in instructions if item["program_id"] == ORCA_WHIRLPOOL and item["type"] == "swap_v2"]
    if not router_present and not ray_ix and not orca_ix:
        result["notes"] = ["Não foi identificada uma sequência de swap suportada; movimentos de tokens ficam como transferências."]
        return result

    # A direct Jupiter single-hop route is supported when signer ownership,
    # SPL transfers and owner-level raw deltas agree. This covers Jupiter→Orca
    # without requiring a temporary WSOL account or a Raydium leg.
    if router_present and len(ray_ix) == 0 and len(orca_ix) == 1:
        message = ((tx.get("transaction") or {}).get("message") or {})
        account_keys = message.get("accountKeys") or []
        signers = {_pubkey(k) for k in account_keys if isinstance(k, dict) and k.get("signer")}
        if not signers and isinstance(message.get("header"), dict):
            required = message["header"].get("numRequiredSignatures", 0)
            signers = {_pubkey(k) for k in account_keys[:required]}
        try:
            raw_deltas, mint_decimals = _token_balance_raw_deltas(tx)
        except ValueError as exc:
            result.update(status="ambiguous", notes=[
                f"Saldos de token inválidos; a rota não é atribuída: {exc}"])
            return result
        transfers = [m for m in moves if m.get("mint") and isinstance(m.get("amount_raw"), int)]
        direct = []
        for signer in signers:
            changes = [(mint, delta) for (owner, mint), delta in raw_deltas.items() if owner == signer]
            spent = [(mint, -delta) for mint, delta in changes if delta < 0]
            received = [(mint, delta) for mint, delta in changes if delta > 0]
            if len(spent) != 1 or len(received) != 1:
                continue
            in_mint, in_raw = spent[0]
            out_mint, out_raw = received[0]
            in_transfers = [m for m in transfers if m.get("mint") == in_mint and
                            m.get("source_owner") == signer and m.get("amount_raw") == in_raw]
            out_transfers = [m for m in transfers if m.get("mint") == out_mint and
                             m.get("destination_owner") == signer and m.get("amount_raw") == out_raw]
            if len(in_transfers) != 1 or len(out_transfers) != 1:
                continue
            orca = orca_ix[0]
            specified_is_input = orca.get("amount_specified_is_input")
            if specified_is_input is True and orca.get("amount_specified_raw") != in_raw:
                continue
            if specified_is_input is False and orca.get("amount_specified_raw") != out_raw:
                continue
            if specified_is_input is None:
                continue
            if jupiter_limits is None:
                continue
            if base_in and (jupiter_limits["amount_in_raw"] != in_raw or
                            out_raw < jupiter_limits["minimum_amount_out_raw"]):
                continue
            if base_out and (jupiter_limits["amount_out_raw"] != out_raw or
                             in_raw > jupiter_limits["maximum_amount_in_raw"]):
                continue
            direct.append((signer, in_mint, in_raw, out_mint, out_raw,
                           in_transfers[0], out_transfers[0]))
        if len(direct) == 1:
            signer, in_mint, in_raw, out_mint, out_raw, input_move, output_move = direct[0]
            result.update(status="identified", route=[in_mint, out_mint], notes=[
                "Rota Jupiter de uma etapa reconciliada por signer, transferências SPL, deltas brutos e instrução Orca SwapV2."])
            result["route_evidence"] = {
                "router_pattern": "base_in" if base_in else "base_out",
                "jupiter_limits": jupiter_limits,
                "trader": signer, "input_transfer": input_move,
                "output_transfer": output_move, "orca_instruction": orca_ix[0],
                "input_effective_raw": in_raw, "output_effective_raw": out_raw,
                "input_effective": in_raw / (10 ** mint_decimals.get(in_mint, 0)),
                "output_effective": out_raw / (10 ** mint_decimals.get(out_mint, 0)),
                "orca_specified_amount_raw": orca.get("amount_specified_raw"),
                "orca_specified_amount_role": orca.get("specified_amount_role"),
                "orca_other_amount_threshold_raw": orca.get("other_amount_threshold_raw"),
                "orca_sqrt_price_limit_raw": orca.get("sqrt_price_limit_raw"),
            }
            return result
        result.update(status="partial", notes=[
            "Há instrução Jupiter→Orca, mas signer, transferências SPL e deltas não permitem atribuir uma única rota com segurança."])
        return result
    if len(ray_ix) > 1 or len(orca_ix) > 1:
        result.update(status="ambiguous", notes=["A transação contém múltiplas instruções de swap reconhecidas; a associação entre pernas não é segura nesta camada."])
        return result

    # Correlate the fixture-supported two-leg route only when transfer records,
    # mints, temporary WSOL account lifecycle and order all agree.
    inner = [item for item in instructions if item["scope"] == "inner"]
    creates = [item for item in inner if item["category"] == "account_creation" and item.get("account")]
    closes = [item for item in inner if item["category"] == "account_closure" and item.get("account")]
    initialized_wsol = {item.get("account") for item in inner
                        if item.get("type") in ("initializeAccount", "initializeAccount2", "initializeAccount3")
                        and item.get("mint") == WSOL}
    temp_accounts = {item.get("account") for item in creates} & initialized_wsol & {item.get("account") for item in closes}
    transfer_rows = [item for item in moves if item.get("mint")]
    candidates = []
    for temp in temp_accounts:
        wsol_in = [m for m in transfer_rows if m.get("mint") == WSOL and m.get("destination") == temp]
        wsol_out = [m for m in transfer_rows if m.get("mint") == WSOL and m.get("source") == temp]
        for incoming in wsol_in:
            for outgoing in wsol_out:
                if incoming.get("amount_raw") != outgoing.get("amount_raw"):
                    continue
                wallet = incoming.get("destination_owner")
                before = [m for m in transfer_rows if m.get("sequence") < incoming.get("sequence")
                          and m.get("mint") not in (USDC, USDT, WSOL)
                          and m.get("source_owner") == wallet]
                after = [m for m in transfer_rows if m.get("sequence") > outgoing.get("sequence")
                         and m.get("mint") in (USDC, USDT)
                         and m.get("destination_owner") == wallet]
                if len(before) == 1 and len(after) == 1:
                    candidates.append((temp, incoming, outgoing, before[0], after[0]))
    if len(candidates) != 1:
        result["status"] = "partial" if router_present or ray_ix or orca_ix else "unsupported"
        result["notes"] = ["Há evidência de swap, mas não foi possível ligar todas as pernas, transferências e ciclo WSOL sem ambiguidade."]
        return result

    temp, wsol_in, wsol_out, asset_in, quote_out = candidates[0]
    if not (router_present and len(ray_ix) == 1 and len(orca_ix) == 1):
        result["status"] = "partial"
        result["notes"] = ["A sequência de transferências é compatível, mas falta evidência de router/Raydium/Orca suficiente para identificar as duas etapas."]
        return result
    if not (ray_ix[0].get("sequence") < asset_in.get("sequence") < wsol_in.get("sequence") <
            orca_ix[0].get("sequence") < wsol_out.get("sequence") < quote_out.get("sequence")):
        result.update(status="ambiguous", notes=["A ordem das transferências não corresponde à sequência de rota esperada."])
        return result
    if (asset_in.get("amount_raw") != ray_ix[0].get("amount_in_raw") or
            wsol_in.get("amount_raw") != wsol_out.get("amount_raw") or
            orca_ix[0].get("amount_in_raw") != wsol_out.get("amount_raw")):
        result.update(status="ambiguous", notes=["Os montantes das transferências não reconciliam com as instruções de swap reconhecidas."])
        return result
    logged_amount_pairs = {(int(a), int(b)) for a, b in re.findall(
        r"Program log: (\d+) -> (\d+)", logs)}
    if ((asset_in.get("amount_raw"), wsol_in.get("amount_raw")) not in logged_amount_pairs or
            (wsol_out.get("amount_raw"), quote_out.get("amount_raw")) not in logged_amount_pairs):
        result.update(status="partial", notes=["Os movimentos são compatíveis, mas os logs de montante não confirmam ambas as pernas da rota."])
        return result
    route = [asset_in.get("mint"), WSOL, quote_out.get("mint")]
    result.update(status="identified", route=route,
                  notes=["A rota está apoiada por instruções reconhecidas, logs e movimentos de tokens reconciliáveis; isto não valida outras variantes de protocolo."])
    result["route_evidence"] = {
        "input_transfer": asset_in, "raydium_instruction": ray_ix[0],
        "wsol_to_temporary_account": wsol_in,
        "orca_instruction": orca_ix[0], "wsol_from_temporary_account": wsol_out,
        "output_transfer": quote_out, "temporary_wsol_account": temp,
        "temporary_account_created": temp in {item.get("account") for item in creates},
        "temporary_account_closed": temp in {item.get("account") for item in closes},
        "sync_native_present": any(item.get("type") == "syncNative" for item in inner),
    }
    return result


def decode_swaps(tx, quote_prices_usd=None, quote_mints=None, wallet_filter=None):
    """Decode owner-level net pairs; instruction evidence is reported separately.

    ``quote_prices_usd`` maps quote mint to an explicitly supplied price.
    No stablecoin peg or other USD price is assumed. Transaction
    fee USD is always None here because the function has no verified historical
    SOL/USD source; fee lamports and SOL are still reported exactly.
    """
    meta = tx.get("meta") or {}
    if meta.get("err") is not None:
        return []
    prices = dict(quote_prices_usd or {})
    quotes = set(quote_mints or [USDC, USDT, WSOL])
    quotes.discard(SOL)
    try:
        raw_deltas, mint_decimals = _token_balance_raw_deltas(tx)
    except ValueError:
        return []
    by_owner = defaultdict(list)
    for (owner, mint), delta in raw_deltas.items():
        by_owner[owner].append((mint, delta, mint_decimals.get(mint, 0)))
    sigs = ((tx.get("transaction") or {}).get("signatures") or [])
    signature = sigs[0] if sigs else tx.get("signature")
    if not signature:
        return []
    timestamp = tx.get("blockTime")
    if timestamp is not None:
        try:
            timestamp = float(timestamp)
            if timestamp < 0 or timestamp != timestamp or timestamp in (float("inf"), float("-inf")):
                return []
        except (TypeError, ValueError):
            return []
    valid_wallets = None if wallet_filter is None else {
        w for w in wallet_filter if is_solana_address(w)
    }
    fee = transaction_fee(tx)
    route_evidence = transaction_instruction_evidence(tx)
    if route_evidence.get("status") != "identified":
        return []
    trader = (route_evidence.get("route_evidence") or {}).get("trader")
    if not trader:
        message = ((tx.get("transaction") or {}).get("message") or {})
        keys = message.get("accountKeys") or []
        signers = {_pubkey(k) for k in keys if isinstance(k, dict) and k.get("signer")}
        if not signers and isinstance(message.get("header"), dict):
            signers = {_pubkey(k) for k in keys[:message["header"].get("numRequiredSignatures", 0)]}
        # The route analyzer must identify a unique signer whose token deltas
        # are reconciled by instruction evidence; otherwise do not attribute.
        matching = [s for s in signers if any(owner == s for owner, _ in raw_deltas)]
        if len(matching) == 1:
            trader = matching[0]
    if trader is None:
        return []
    events = []
    for owner, changes in by_owner.items():
        if owner != trader:
            continue
        if valid_wallets is not None and owner not in valid_wallets:
            continue
        quote_changes = [(m, d, decimals) for m, d, decimals in changes if m in quotes]
        asset_changes = [(m, d, decimals) for m, d, decimals in changes if m not in quotes and m != SOL]
        if len(asset_changes) != 1 or len(quote_changes) != 1:
            continue
        mint, asset_delta, asset_decimals = asset_changes[0]
        quote_mint, quote_delta, quote_decimals = quote_changes[0]
        if asset_delta * quote_delta >= 0:
            continue
        quote_amount = abs(quote_delta) / (10 ** quote_decimals)
        quote_amount_raw = abs(quote_delta)
        token_amount = abs(asset_delta) / (10 ** asset_decimals)
        price = prices.get(quote_mint)
        try:
            price = None if price is None or isinstance(price, bool) else float(price)
        except (TypeError, ValueError, OverflowError):
            price = None
        if price is not None and (not math.isfinite(price) or price < 0):
            price = None
        quote_usd = None if price is None else quote_amount * price
        events.append({
            "signature": str(signature), "wallet": owner, "mint": mint,
            "side": "buy" if asset_delta > 0 else "sell",
            "token_amount": token_amount, "quote_usd": quote_usd,
            "token_amount_raw": abs(asset_delta), "token_decimals": asset_decimals,
            "quote_amount": quote_amount, "quote_amount_raw": quote_amount_raw,
            "quote_decimals": quote_decimals,
            "fee_lamports": fee["fee_lamports"], "fee_sol": fee["fee_sol"],
            "fee_usd": None, "timestamp": timestamp,
            "quote_mint": quote_mint,
            "confidence": "owner_net_balance_pair",
            "route_evidence": {"status": route_evidence["status"],
                               "route": route_evidence.get("route", [])},
        })
    return events


def ingest_transaction(tracker, tx, quote_prices_usd=None, quote_mints=None,
                       wallet_filter=None):
    """Insert classified swaps without replacing unknown USD fields with zero."""
    inserted = []
    for event in decode_swaps(tx, quote_prices_usd, quote_mints, wallet_filter):
        record = {k: event[k] for k in (
            "signature", "wallet", "mint", "side", "token_amount",
            "quote_usd", "fee_usd", "timestamp", "quote_mint", "quote_amount",
            "quote_amount_raw", "quote_decimals", "fee_lamports", "fee_sol")}
        if tracker.record_swap(**record):
            inserted.append(event)
    return inserted
