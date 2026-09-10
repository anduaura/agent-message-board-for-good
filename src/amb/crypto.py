"""Agent identity.

Every agent holds an Ed25519 keypair. The private half never leaves the operator's
machine; the board stores only the public half. Every message and every task claim is
signed, so the board can prove who said what without being able to say it for them.

This is Article III of the charter made mechanical: attribution is not a convention
that a clever agent can drop, it is a precondition for being heard at all.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from amb.errors import SignatureInvalid


def generate_keypair() -> tuple[str, str]:
    """Return (private_key_hex, public_key_hex)."""
    private = Ed25519PrivateKey.generate()
    priv_bytes = private.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return priv_bytes.hex(), public_key_hex(priv_bytes.hex())


def public_key_hex(private_key_hex: str) -> str:
    private = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(private_key_hex))
    return private.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    ).hex()


def canonical(payload: dict[str, Any]) -> bytes:
    """Deterministic bytes for a payload, so signatures are reproducible."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()


def sign(private_key_hex: str, payload: dict[str, Any]) -> str:
    private = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(private_key_hex))
    return private.sign(canonical(payload)).hex()


def verify(public_key_hex_: str, payload: dict[str, Any], signature_hex: str) -> None:
    """Raise SignatureInvalid unless the signature checks out."""
    try:
        public = Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_key_hex_))
        public.verify(bytes.fromhex(signature_hex), canonical(payload))
    except (InvalidSignature, ValueError) as exc:
        raise SignatureInvalid("signature does not verify for this agent") from exc


def content_hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(canonical(payload)).hexdigest()
