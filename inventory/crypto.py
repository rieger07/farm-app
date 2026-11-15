"""Authenticated symmetric encryption for Zettle OAuth tokens at rest.

Uses only Python's standard library (HMAC-SHA256 + SHA-256 CTR stream cipher)
so the Alpine container needs no native crypto build tools.
"""

import base64
import hashlib
import hmac
import os
import secrets
from pathlib import Path

from homestead.database import get_db_path

PREFIX = "enc1:"


def _master_key() -> bytes:
    env_key = os.environ.get("INVENTORY_SECRET_KEY", "").strip()
    if env_key:
        return hashlib.sha256(env_key.encode("utf-8")).digest()

    db_dir = Path(get_db_path()).parent
    db_dir.mkdir(parents=True, exist_ok=True)
    key_file = db_dir / ".inventory_key"
    if key_file.exists():
        raw = key_file.read_bytes()
        if len(raw) >= 32:
            return hashlib.sha256(raw).digest()
    raw = secrets.token_bytes(32)
    fd = os.open(str(key_file), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, raw)
    finally:
        os.close(fd)
    return hashlib.sha256(raw).digest()


def _subkeys() -> tuple[bytes, bytes]:
    master = _master_key()
    enc_key = hashlib.sha256(master + b":enc").digest()
    mac_key = hashlib.sha256(master + b":mac").digest()
    return enc_key, mac_key


def _keystream(enc_key: bytes, nonce: bytes, length: int) -> bytes:
    out = bytearray()
    counter = 0
    while len(out) < length:
        block = hashlib.sha256(enc_key + nonce + counter.to_bytes(4, "big")).digest()
        out.extend(block)
        counter += 1
    return bytes(out[:length])


def encrypt_token(plaintext: str | None) -> str | None:
    if not plaintext:
        return plaintext
    if plaintext.startswith(PREFIX):
        return plaintext
    enc_key, mac_key = _subkeys()
    nonce = secrets.token_bytes(16)
    data = plaintext.encode("utf-8")
    stream = _keystream(enc_key, nonce, len(data))
    ct = bytes(a ^ b for a, b in zip(data, stream))
    tag = hmac.new(mac_key, nonce + ct, hashlib.sha256).digest()
    return PREFIX + base64.urlsafe_b64encode(nonce + ct + tag).decode("ascii")


def decrypt_token(token: str | None) -> str | None:
    if not token:
        return token
    if not token.startswith(PREFIX):
        # Backwards compatibility for legacy plaintext tokens.
        return token
    enc_key, mac_key = _subkeys()
    raw = base64.urlsafe_b64decode(token[len(PREFIX) :].encode("ascii"))
    if len(raw) < 16 + 32:
        raise ValueError("Corrupted encrypted token.")
    nonce, ct, tag = raw[:16], raw[16:-32], raw[-32:]
    expected = hmac.new(mac_key, nonce + ct, hashlib.sha256).digest()
    if not hmac.compare_digest(tag, expected):
        raise ValueError("Token authentication failed.")
    stream = _keystream(enc_key, nonce, len(ct))
    pt = bytes(a ^ b for a, b in zip(ct, stream))
    return pt.decode("utf-8")
