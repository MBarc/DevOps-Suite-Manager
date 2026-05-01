"""Apache Guacamole auth-json envelope.

The guacamole-auth-json extension expects:
    plaintext = HMAC-SHA256(json, key) || json
    ciphertext = AES-128-CBC(plaintext, key=secret, iv=0x00...00)
    data = base64(ciphertext)

The same 16-byte key is used as both the AES key and the HMAC key.

References:
- https://guacamole.apache.org/doc/gug/json-auth.html
- guacamole-client/extensions/guacamole-auth-json
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets as _secrets
from pathlib import Path

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.padding import PKCS7

KEY_BYTES = 16  # AES-128


class AuthJsonError(RuntimeError):
    pass


class AuthJsonCodec:
    """Round-trippable encoder/decoder for the auth-json blob."""

    def __init__(self, key: bytes):
        if len(key) != KEY_BYTES:
            raise AuthJsonError(f"key must be {KEY_BYTES} bytes, got {len(key)}")
        self._key = key

    def encode(self, payload: dict) -> str:
        body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        sig = hmac.new(self._key, body, hashlib.sha256).digest()
        plaintext = sig + body
        padder = PKCS7(128).padder()
        padded = padder.update(plaintext) + padder.finalize()
        cipher = Cipher(algorithms.AES(self._key), modes.CBC(b"\x00" * 16))
        enc = cipher.encryptor()
        ct = enc.update(padded) + enc.finalize()
        return base64.b64encode(ct).decode("ascii")

    def decode(self, b64_data: str) -> dict:
        ct = base64.b64decode(b64_data)
        cipher = Cipher(algorithms.AES(self._key), modes.CBC(b"\x00" * 16))
        dec = cipher.decryptor()
        padded = dec.update(ct) + dec.finalize()
        unpadder = PKCS7(128).unpadder()
        plain = unpadder.update(padded) + unpadder.finalize()
        sig, body = plain[:32], plain[32:]
        expected = hmac.new(self._key, body, hashlib.sha256).digest()
        if not hmac.compare_digest(sig, expected):
            raise AuthJsonError("HMAC verification failed")
        return json.loads(body)


def generate_secret_key() -> bytes:
    return _secrets.token_bytes(KEY_BYTES)


def load_secret_key(path: Path, *, create_if_missing: bool = True) -> bytes:
    """Load (or create) the 128-bit shared key from `path`.

    The file format is plain hex (32 hex chars) so it's trivial to paste into
    the Guacamole `guacamole.properties` `json-secret-key` setting.
    """
    if path.exists():
        text = path.read_text().strip()
        try:
            data = bytes.fromhex(text)
        except ValueError as e:
            raise AuthJsonError(f"key file {path} is not valid hex: {e}") from e
        if len(data) != KEY_BYTES:
            raise AuthJsonError(f"key file {path} has wrong length: {len(data)}")
        return data
    if not create_if_missing:
        raise AuthJsonError(f"key file {path} does not exist")
    path.parent.mkdir(parents=True, exist_ok=True)
    key = generate_secret_key()
    path.write_text(key.hex())
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return key


def build_connection_id(connection_name: str, source: str = "json") -> str:
    """Construct the Guacamole client URL identifier for a named connection.

    Guacamole's UI references connections by base64( name || \\0 || 'c' || \\0 || source ).
    """
    raw = connection_name.encode("utf-8") + b"\0c\0" + source.encode("utf-8")
    return base64.b64encode(raw).decode("ascii")
