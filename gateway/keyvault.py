"""Envelope encryption for per-tenant provider keys.

The provider API key (BYOK, or ours) is never stored in plaintext and never
logged. Envelope scheme:

  * a master key-encryption-key (KEK) comes from the env (TENANT_KEY_KEK),
    base64, 32 bytes — never in the DB;
  * each stored secret gets its own random 32-byte data key (DEK);
  * the DEK encrypts the provider key (AES-256-GCM);
  * the KEK wraps (encrypts) the DEK (AES-256-GCM);
  * only {wrapped DEK + ciphertext + nonces} is stored.

Both AES-GCM operations bind AAD = "tenant_id:provider", so a stored blob
cannot be lifted into another tenant/provider row and decrypted there.

KEK rotation re-wraps DEKs without touching ciphertext (the point of envelope
encryption); not implemented here, but the structure allows it.
"""

import base64
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_VERSION = b"\x01"


def _kek() -> bytes:
    raw = os.environ.get("TENANT_KEY_KEK", "")
    if not raw:
        raise RuntimeError("TENANT_KEY_KEK is not set — cannot (de)crypt tenant provider keys")
    key = base64.b64decode(raw)
    if len(key) != 32:
        raise RuntimeError("TENANT_KEY_KEK must decode to 32 bytes (AES-256)")
    return key


def _lp(b: bytes) -> bytes:
    return len(b).to_bytes(2, "big") + b


def _take(buf: memoryview, off: int) -> tuple[bytes, int]:
    n = int.from_bytes(buf[off : off + 2], "big")
    off += 2
    return bytes(buf[off : off + n]), off + n


def encrypt(plaintext: str, *, tenant_id: str, provider: str) -> str:
    """Return a base64 envelope blob for a provider key. Plaintext never leaves
    this function un-encrypted and is never logged."""
    aad = f"{tenant_id}:{provider}".encode()
    kek = _kek()
    dek = os.urandom(32)
    dek_nonce = os.urandom(12)
    wrapped = AESGCM(kek).encrypt(dek_nonce, dek, aad)
    data_nonce = os.urandom(12)
    ct = AESGCM(dek).encrypt(data_nonce, plaintext.encode(), aad)
    blob = _VERSION + _lp(dek_nonce) + _lp(wrapped) + _lp(data_nonce) + _lp(ct)
    return base64.b64encode(blob).decode()


def decrypt(blob_b64: str, *, tenant_id: str, provider: str) -> str:
    aad = f"{tenant_id}:{provider}".encode()
    kek = _kek()
    buf = memoryview(base64.b64decode(blob_b64))
    if bytes(buf[:1]) != _VERSION:
        raise ValueError("unknown key-envelope version")
    off = 1
    dek_nonce, off = _take(buf, off)
    wrapped, off = _take(buf, off)
    data_nonce, off = _take(buf, off)
    ct, off = _take(buf, off)
    dek = AESGCM(kek).decrypt(dek_nonce, wrapped, aad)
    return AESGCM(dek).decrypt(data_nonce, ct, aad).decode()
