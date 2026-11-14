"""Deterministic valid public keys for offline Solana regression tests."""

_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def test_pubkey(seed):
    number = int(seed) + 1
    raw = number.to_bytes(32, "big")
    encoded = ""
    while number:
        number, rem = divmod(number, 58)
        encoded = _ALPHABET[rem] + encoded
    return "1" * (len(raw) - len(raw.lstrip(b"\0"))) + encoded
