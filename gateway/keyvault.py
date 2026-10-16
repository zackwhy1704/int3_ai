"""Envelope encryption for per-tenant provider keys.

The provider API key (BYOK, or ours) is never stored in plaintext and never
logged. Each stored secret gets its own random 32-byte data key (DEK) that
AES-256-GCM-encrypts the provider key; the DEK itself is WRAPPED. Only
{wrapped DEK + ciphertext + nonces} is stored. Every AES-GCM op binds
AAD = "tenant_id:provider", so a stored blob cannot be lifted into another
tenant/provider row and decrypted there.

Two wrap schemes coexist (the version byte selects at decrypt time):

  * v1 (symmetric KEK): the DEK is wrapped with a master KEK from the env
    (TENANT_KEY_KEK, base64, 32 bytes). Anyone who can `encrypt` here can also
    `decrypt` — fine for the OPERATOR CLI, which holds the KEK.

  * v2 (asymmetric, ENCRYPT-ONLY): the DEK is sealed to an X25519 PUBLIC key
    (libsodium-style sealed box, built from `cryptography` primitives only).
    The holder of the PUBLIC key (the internet-facing key-intake service) can
    ONLY encrypt; it cannot decrypt any tenant key. The matching PRIVATE key
    (held by the gateway) is required to unseal. This is the whole point: the
    only component exposed to the internet must not be able to read keys.

`decrypt` accepts BOTH v1 and v2. In hosting this maps to Cloud KMS: encrypter-
only IAM for intake, decrypter for the gateway.

Migration (v1 -> v2): existing v1 blobs stay readable (the gateway still holds
the KEK). To retire the symmetric KEK, re-wrap each v1 blob with
`rewrap_v1_to_v2` (operator-run: it needs the KEK to read v1 and the public key
to write v2) and store the result; ciphertext of the provider key is unchanged
conceptually but re-encrypted under a fresh DEK. Not run automatically.
"""

import base64
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives import hashes

_VERSION = b"\x01"
_VERSION_V2 = b"\x02"


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
    """Decrypt a v1 (symmetric KEK) or v2 (asymmetric X25519) envelope. v2 needs the
    PRIVATE key (gateway only); an intake process with no KEK and no private key cannot
    decrypt either version — it raises."""
    aad = f"{tenant_id}:{provider}".encode()
    buf = memoryview(base64.b64decode(blob_b64))
    ver = bytes(buf[:1])
    off = 1
    if ver == _VERSION:
        dek_nonce, off = _take(buf, off)
        wrapped, off = _take(buf, off)
        data_nonce, off = _take(buf, off)
        ct, off = _take(buf, off)
        dek = AESGCM(_kek()).decrypt(dek_nonce, wrapped, aad)
        return AESGCM(dek).decrypt(data_nonce, ct, aad).decode()
    if ver == _VERSION_V2:
        eph_pub, off = _take(buf, off)
        dek_nonce, off = _take(buf, off)
        wrapped, off = _take(buf, off)
        data_nonce, off = _take(buf, off)
        ct, off = _take(buf, off)
        dek = _unseal_dek(eph_pub, dek_nonce, wrapped, aad, _recipient_private())
        return AESGCM(dek).decrypt(data_nonce, ct, aad).decode()
    raise ValueError("unknown key-envelope version")


# --- v2 asymmetric (encrypt-only intake) -----------------------------------

def _recipient_public() -> X25519PublicKey:
    raw = os.environ.get("KEYVAULT_PUBLIC_KEY", "")
    if not raw:
        raise RuntimeError("KEYVAULT_PUBLIC_KEY is not set — cannot seal a provider key (v2)")
    return X25519PublicKey.from_public_bytes(base64.b64decode(raw))


def _recipient_private() -> X25519PrivateKey:
    raw = os.environ.get("KEYVAULT_PRIVATE_KEY", "")
    if not raw:
        raise RuntimeError("KEYVAULT_PRIVATE_KEY is not set — this process cannot decrypt v2 "
                           "envelopes (expected for the encrypt-only intake service)")
    return X25519PrivateKey.from_private_bytes(base64.b64decode(raw))


def _wrap_key(shared: bytes, eph_pub: bytes, recip_pub: bytes) -> bytes:
    """HKDF the ECDH shared secret, binding both public keys (sealed-box style)."""
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None,
                info=b"cb-keyvault-v2|" + eph_pub + b"|" + recip_pub).derive(shared)


def _seal_dek(dek: bytes, aad: bytes, recipient: X25519PublicKey) -> tuple[bytes, bytes, bytes]:
    eph = X25519PrivateKey.generate()
    eph_pub = eph.public_key().public_bytes_raw()
    recip_pub = recipient.public_bytes_raw()
    wk = _wrap_key(eph.exchange(recipient), eph_pub, recip_pub)
    nonce = os.urandom(12)
    wrapped = AESGCM(wk).encrypt(nonce, dek, aad)
    return eph_pub, nonce, wrapped


def _unseal_dek(eph_pub: bytes, nonce: bytes, wrapped: bytes, aad: bytes, priv: X25519PrivateKey) -> bytes:
    recip_pub = priv.public_key().public_bytes_raw()
    wk = _wrap_key(priv.exchange(X25519PublicKey.from_public_bytes(eph_pub)), eph_pub, recip_pub)
    return AESGCM(wk).decrypt(nonce, wrapped, aad)


def encrypt_v2(plaintext: str, *, tenant_id: str, provider: str) -> str:
    """Seal a provider key to the X25519 PUBLIC key (KEYVAULT_PUBLIC_KEY). Encrypt-only:
    the caller cannot decrypt the result. Used by the internet-facing key-intake service."""
    aad = f"{tenant_id}:{provider}".encode()
    dek = os.urandom(32)
    data_nonce = os.urandom(12)
    ct = AESGCM(dek).encrypt(data_nonce, plaintext.encode(), aad)
    eph_pub, dek_nonce, wrapped = _seal_dek(dek, aad, _recipient_public())
    blob = (_VERSION_V2 + _lp(eph_pub) + _lp(dek_nonce) + _lp(wrapped)
            + _lp(data_nonce) + _lp(ct))
    return base64.b64encode(blob).decode()


def generate_keypair() -> tuple[str, str]:
    """(public_b64, private_b64) X25519 keypair. Operator sets KEYVAULT_PUBLIC_KEY on the
    intake service and KEYVAULT_PRIVATE_KEY on the gateway. Run: python -m keyvault gen-keypair."""
    priv = X25519PrivateKey.generate()
    pub = priv.public_key()
    return (base64.b64encode(pub.public_bytes_raw()).decode(),
            base64.b64encode(priv.private_bytes_raw()).decode())


def rewrap_v1_to_v2(blob_b64: str, *, tenant_id: str, provider: str) -> str:
    """Operator migration: read a v1 blob (needs the KEK) and re-seal it v2 (needs the public
    key). Lets a deployment retire the symmetric KEK once every row is v2."""
    return encrypt_v2(decrypt(blob_b64, tenant_id=tenant_id, provider=provider),
                      tenant_id=tenant_id, provider=provider)


if __name__ == "__main__":  # python -m keyvault gen-keypair
    import sys
    if len(sys.argv) == 2 and sys.argv[1] == "gen-keypair":
        pub, priv = generate_keypair()
        print(f"KEYVAULT_PUBLIC_KEY={pub}   # set on the key-intake service (encrypt-only)")
        print(f"KEYVAULT_PRIVATE_KEY={priv}  # set on the gateway only (decrypt); keep off intake")
    else:
        sys.exit("usage: python -m keyvault gen-keypair")
