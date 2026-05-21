import base64
import hashlib

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)


def generate_keypair() -> tuple[bytes, str]:
    private_key = Ed25519PrivateKey.generate()
    private_key_bytes = private_key.private_bytes(
        encoding=Encoding.Raw,
        format=PrivateFormat.Raw,
        encryption_algorithm=NoEncryption(),
    )
    public_key_bytes = private_key.public_key().public_bytes(
        encoding=Encoding.Raw,
        format=PublicFormat.Raw,
    )
    return private_key_bytes, base64.b64encode(public_key_bytes).decode("ascii")


def sign(private_key_bytes: bytes, message: bytes) -> str:
    private_key = Ed25519PrivateKey.from_private_bytes(private_key_bytes)
    return base64.b64encode(private_key.sign(message)).decode("ascii")


def verify(public_key_b64: str, message: bytes, signature_b64: str) -> bool:
    try:
        public_key_bytes = base64.b64decode(public_key_b64, validate=True)
        signature = base64.b64decode(signature_b64, validate=True)
        public_key = Ed25519PublicKey.from_public_bytes(public_key_bytes)
        public_key.verify(signature, message)
    except (InvalidSignature, ValueError):
        return False
    return True


def canonical_request_string(
    method: str,
    path: str,
    timestamp: str,
    body_bytes: bytes,
) -> bytes:
    body_hash = hashlib.sha256(body_bytes).hexdigest()
    return f"{method.upper()}\n{path}\n{timestamp}\n{body_hash}".encode()
