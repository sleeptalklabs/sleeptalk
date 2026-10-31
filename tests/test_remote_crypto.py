"""Real HPKE and WebAuthn signatures with synthetic keys, never user devices."""
import hashlib
import json

import pytest

pytest.importorskip("rfc8785")
pytest.importorskip("fido2")
pytest.importorskip("cryptography.hazmat.primitives.hpke")

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, ed25519
from fido2.cose import ES256
from fido2.webauthn import Aaguid, AttestedCredentialData, AuthenticatorData, CollectedClientData

from sleeptalk import remote_crypto as crypto

ORIGIN = "https://setup.example.com"
RP = "setup.example.com"
SECRET = "synthetic-secret-\u00e9"


@pytest.fixture
def context():
    identifier = crypto.encode(b"i" * 16)
    return crypto.Context(version=1, request_id=identifier, descriptor_digest="a" * 64,
        host_id="b" * 64, operator_id=identifier, operator_binding_revision=1,
        workspace_id=identifier, runtime_binding_digest="c" * 64,
        provider_field="deepgram.api_key", action="create", expected_credential_generation=0,
        submission_id=identifier, challenge_id=identifier, challenge_nonce=crypto.encode(b"n" * 32),
        connection_generation=identifier, request_revision=0, request_expires_at=2000000000)


@pytest.fixture
def keys():
    operator = ec.generate_private_key(ec.SECP256R1())
    credential = AttestedCredentialData.create(Aaguid.NONE, b"synthetic-credential", ES256.from_cryptography_key(operator.public_key()))
    return ec.generate_private_key(ec.SECP256R1()), operator, credential


def assertion(challenge, operator, credential, *, origin=ORIGIN, rp=RP, flags=5, cross_origin=False):
    client = CollectedClientData.create(type="webauthn.get", challenge=challenge, origin=origin, cross_origin=cross_origin)
    auth = AuthenticatorData.create(hashlib.sha256(rp.encode()).digest(), flags, 0)
    signature = operator.sign(bytes(auth) + client.hash, ec.ECDSA(hashes.SHA256()))
    return {"id": crypto.encode(credential.credential_id), "rawId": crypto.encode(credential.credential_id),
        "type": "public-key", "response": {"clientDataJSON": crypto.encode(bytes(client)),
        "authenticatorData": crypto.encode(bytes(auth)), "signature": crypto.encode(signature)}}


def envelope(context, keys, **assertion_options):
    recipient, operator, credential = keys
    enc, ct = crypto.encrypt(SECRET, context, recipient.public_key())
    challenge = crypto.authorization_challenge(context, crypto.decode(enc), crypto.decode(ct))
    return {"context": context.model_dump(), "enc": enc, "ct": ct,
        "assertion": assertion(challenge, operator, credential, **assertion_options)}


def decrypt(body, context, keys):
    return crypto.decrypt_authorized(crypto.canonical(body), expected=context,
        private_key=keys[0], enrolled_credential=bytes(keys[2]), rp_id=RP, origin=ORIGIN)


def test_real_hpke_and_verified_operator_roundtrip(context, keys):
    body = envelope(context, keys)
    assert SECRET not in json.dumps(body)
    assert decrypt(body, context, keys) == SECRET
    assert envelope(context, keys)["enc"] != body["enc"]


@pytest.mark.parametrize("options", [{"origin": "https://evil.example.com"}, {"rp": "evil.example.com"},
    {"flags": 1}, {"flags": 4}, {"cross_origin": True}])
def test_origin_rp_presence_and_verification_are_required(context, keys, options):
    with pytest.raises(crypto.ProtocolError, match="authorization failed"):
        decrypt(envelope(context, keys, **options), context, keys)


@pytest.mark.parametrize("field,value", [("workspace_id", crypto.encode(b"x" * 16)), ("request_revision", 1),
    ("connection_generation", crypto.encode(b"x" * 16)), ("provider_field", "cartesia.api_key"),
    ("expected_credential_generation", 1), ("operator_id", crypto.encode(b"x" * 16))])
def test_signed_submission_cannot_escape_ledger_context(context, keys, field, value):
    body = envelope(context, keys)
    expected = crypto.Context.model_validate({**context.model_dump(), field: value})
    with pytest.raises(crypto.ProtocolError):
        decrypt(body, expected, keys)


def test_ciphertext_and_operator_are_bound_to_assertion(context, keys):
    body = envelope(context, keys)
    body["ct"] = crypto.encode(b"tampered-ciphertext")
    with pytest.raises(crypto.ProtocolError):
        decrypt(body, context, keys)
    body = envelope(context, keys)
    other = ec.generate_private_key(ec.SECP256R1())
    other_credential = AttestedCredentialData.create(Aaguid.NONE, keys[2].credential_id, ES256.from_cryptography_key(other.public_key()))
    with pytest.raises(crypto.ProtocolError):
        decrypt(body, context, (keys[0], other, other_credential))


def test_authorized_bad_hpke_tag_still_fails(context, keys):
    body = envelope(context, keys)
    ct = bytearray(crypto.decode(body["ct"]))
    ct[-1] ^= 1
    body["ct"] = crypto.encode(bytes(ct))
    challenge = crypto.authorization_challenge(context, crypto.decode(body["enc"]), bytes(ct))
    body["assertion"] = assertion(challenge, keys[1], keys[2])
    with pytest.raises(crypto.ProtocolError):
        decrypt(body, context, keys)


@pytest.mark.parametrize("raw", [b'{"x":1,"x":2}', b'{"x":1.0}', b'{"x":NaN}',
    b'{"x":9007199254740992}', b'[]', b'{"x":"\xff"}', b" " * 32769])
def test_wire_parser_rejects_ambiguous_or_unbounded_data(raw):
    with pytest.raises(crypto.ProtocolError):
        crypto.parse(raw)


@pytest.mark.parametrize("value", ["Zg==", "Zh", "+/", "=", "\u00e9"])
def test_base64url_must_be_canonical(value):
    with pytest.raises(crypto.ProtocolError):
        crypto.decode(value)


@pytest.mark.parametrize("field,value", [("version", True), ("request_id", "x" * 22), ("challenge_nonce", crypto.encode(b"short"))])
def test_context_wire_types_are_strict(context, field, value):
    with pytest.raises(ValueError):
        crypto.Context.model_validate({**context.model_dump(), field: value})


def test_host_signatures_require_pinned_key_and_purpose():
    key = ed25519.Ed25519PrivateKey.generate()
    document = {"revision": 1, "status": "saved"}
    signature = crypto.sign(document, key, purpose="receipt")
    crypto.verify(document, signature, key.public_key(), purpose="receipt")
    for document2, key2, purpose in [(document, key.public_key(), "invitation"),
        ({**document, "revision": 2}, key.public_key(), "receipt"),
        (document, ed25519.Ed25519PrivateKey.generate().public_key(), "receipt")]:
        with pytest.raises(crypto.ProtocolError):
            crypto.verify(document2, signature, key2, purpose=purpose)
