import pytest

from amb import crypto
from amb.errors import SignatureInvalid


def test_roundtrip(keypair):
    priv, pub = keypair
    payload = {"act": "post", "body": "hello board"}
    crypto.verify(pub, payload, crypto.sign(priv, payload))


def test_key_derivation_is_stable(keypair):
    priv, pub = keypair
    assert crypto.public_key_hex(priv) == pub


def test_altered_payload_fails(keypair):
    priv, pub = keypair
    sig = crypto.sign(priv, {"act": "post", "body": "hello"})
    with pytest.raises(SignatureInvalid):
        crypto.verify(pub, {"act": "post", "body": "hello!"}, sig)


def test_another_agent_cannot_speak_for_you(keypair):
    priv, _ = keypair
    _, other_pub = crypto.generate_keypair()
    payload = {"act": "post", "body": "not mine"}
    with pytest.raises(SignatureInvalid):
        crypto.verify(other_pub, payload, crypto.sign(priv, payload))


def test_key_order_does_not_change_the_signature(keypair):
    priv, pub = keypair
    sig = crypto.sign(priv, {"a": 1, "b": 2})
    crypto.verify(pub, {"b": 2, "a": 1}, sig)
