"""Synthetic authenticators exercise real registration, HPKE and signatures."""
import hashlib
import json

import pytest

pytest.importorskip("rfc8785")
pytest.importorskip("fido2")
pytest.importorskip("cryptography.hazmat.primitives.hpke")

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, ed25519
from fido2.cose import ES256
from fido2.webauthn import Aaguid, AttestedCredentialData, AttestationObject, AuthenticatorData, CollectedClientData

from sleeptalk import remote_crypto as crypto
from sleeptalk import remote_pairing as pairing

ORIGIN = "https://setup.example.com"
RP = "setup.example.com"
NONCE = b"n" * 32


@pytest.fixture
def enrollment():
    return pairing.create_enrollment(host_signing_key=ed25519.Ed25519PrivateKey.generate(),
        workspace_id=crypto.encode(b"w" * 16), runtime_binding_digest="a" * 64,
        setup_origin=ORIGIN, relay_origin="https://relay.example.com",
        display_host="Test host", display_workspace="Test workspace", now=1900000000)


@pytest.fixture
def operator():
    key = ec.generate_private_key(ec.SECP256R1())
    credential = AttestedCredentialData.create(Aaguid.NONE, b"synthetic-credential",
        ES256.from_cryptography_key(key.public_key()))
    return key, credential


def registration(enrollment, operator, *, origin=ORIGIN, rp=RP, flags=0x45,
                 challenge=None, cross_origin=False, extra_client=None):
    client = CollectedClientData.create(type="webauthn.create",
        challenge=challenge or crypto.decode(enrollment.descriptor.registration_challenge),
        origin=origin, cross_origin=cross_origin)
    if extra_client:
        client = CollectedClientData(crypto.canonical({**json.loads(bytes(client)), **extra_client}))
    auth = AuthenticatorData.create(hashlib.sha256(rp.encode()).digest(), flags, 0, operator[1])
    attestation = AttestationObject.create("none", auth, {})
    return {"id": crypto.encode(operator[1].credential_id), "rawId": crypto.encode(operator[1].credential_id),
        "type": "public-key", "clientExtensionResults": {},
        "response": {"clientDataJSON": crypto.encode(bytes(client)), "attestationObject": crypto.encode(bytes(attestation))}}


def encrypted(enrollment, operator, **options):
    return pairing.encrypt_registration(registration(enrollment, operator, **options),
        descriptor=enrollment.descriptor, capability=enrollment.capability)


def decrypt(enrollment, raw):
    return pairing.decrypt_registration(raw, expected=enrollment.descriptor, private_key=enrollment.private_key)


def assertion(enrollment, pending, operator, *, nonce=NONCE, origin=ORIGIN, rp=RP,
              flags=5, cross_origin=False, user_handle=None, extra_client=None):
    client = CollectedClientData.create(type="webauthn.get",
        challenge=pairing.possession_challenge(enrollment.descriptor, pending, nonce),
        origin=origin, cross_origin=cross_origin)
    if extra_client:
        client = CollectedClientData(crypto.canonical({**json.loads(bytes(client)), **extra_client}))
    auth = AuthenticatorData.create(hashlib.sha256(rp.encode()).digest(), flags, 0)
    signature = operator[0].sign(bytes(auth) + client.hash, ec.ECDSA(hashes.SHA256()))
    return crypto.canonical({"id": crypto.encode(operator[1].credential_id), "rawId": crypto.encode(operator[1].credential_id),
        "type": "public-key", "response": {"clientDataJSON": crypto.encode(bytes(client)),
        "authenticatorData": crypto.encode(bytes(auth)), "signature": crypto.encode(signature), "userHandle": user_handle}})


def prove(enrollment, pending, raw, nonce=NONCE):
    return pairing.verify_possession(raw, descriptor=enrollment.descriptor, pending=pending, nonce=nonce)


def test_complete_real_crypto_ceremony_produces_only_pending(enrollment, operator):
    pending = decrypt(enrollment, encrypted(enrollment, operator))
    assert pending.enrolled_credential == bytes(operator[1])
    assert pending == pairing.pending_credential(pending.enrolled_credential, enrollment.descriptor.enrollment_id)
    assert len(pending.fingerprint) == 32
    assert len(pending.credential_digest) == 64
    assert not hasattr(pending, "confirmed")
    assert prove(enrollment, pending, assertion(enrollment, pending, operator)) is None
    # Zero signature counters are accepted; the durable nonce ledger owns replay.
    assert prove(enrollment, pending, assertion(enrollment, pending, operator)) is None


def test_options_are_browser_json_and_require_uv(enrollment, operator):
    options = pairing.registration_options(enrollment.descriptor)
    assert json.loads(json.dumps(options))["challenge"] == enrollment.descriptor.registration_challenge
    assert options["rp"]["id"] == RP
    assert options["authenticatorSelection"]["userVerification"] == "required"
    assert crypto.canonical(options)
    assert options["attestation"] == "none"
    assert options["user"]["id"] == enrollment.descriptor.enrollment_id
    pending = decrypt(enrollment, encrypted(enrollment, operator))
    proof = pairing.proof_options(enrollment.descriptor, pending, NONCE)
    assert proof["challenge"] == crypto.encode(pairing.possession_challenge(enrollment.descriptor, pending, NONCE))
    assert crypto.canonical(proof)
    assert proof["rpId"] == RP and proof["userVerification"] == "required"
    assert proof["allowCredentials"] == [{"type": "public-key", "id": crypto.encode(operator[1].credential_id)}]


def test_descriptor_signature_freshness_and_private_repr(enrollment):
    descriptor = enrollment.descriptor
    public_key = ed25519.Ed25519PublicKey.from_public_bytes(crypto.decode(descriptor.host_signing_public_key))
    crypto.verify(descriptor.model_dump(), enrollment.signature, public_key, purpose="enrollment")
    assert descriptor.expires_at - descriptor.issued_at == 600
    assert enrollment.capability not in repr(enrollment)
    with pytest.raises(crypto.ProtocolError):
        crypto.verify(descriptor.model_dump(), enrollment.signature,
            ed25519.Ed25519PrivateKey.generate().public_key(), purpose="enrollment")
    with pytest.raises(crypto.ProtocolError):
        crypto.verify({**descriptor.model_dump(), "display_host": "Other"}, enrollment.signature, public_key, purpose="enrollment")
    other = pairing.create_enrollment(host_signing_key=ed25519.Ed25519PrivateKey.generate(),
        workspace_id=descriptor.workspace_id, runtime_binding_digest=descriptor.runtime_binding_digest,
        setup_origin=ORIGIN, relay_origin=descriptor.relay_origin,
        display_host=descriptor.display_host, display_workspace=descriptor.display_workspace, now=descriptor.issued_at)
    for name in ("enrollment_id", "registration_challenge", "recipient_public_key", "capability_hash"):
        assert getattr(other.descriptor, name) != getattr(descriptor, name)


@pytest.mark.parametrize("options", [{"origin": "https://evil.example.com"}, {"rp": "evil.example.com"},
    {"flags": 0x41}, {"flags": 0x44}, {"cross_origin": True}, {"challenge": b"wrong" * 8}])
def test_registration_requires_exact_challenge_origin_rp_presence_uv(enrollment, operator, options):
    with pytest.raises(crypto.ProtocolError, match="registration verification failed"):
        decrypt(enrollment, encrypted(enrollment, operator, **options))


@pytest.mark.parametrize("options", [{"origin": "https://evil.example.com"}, {"rp": "evil.example.com"},
    {"flags": 1}, {"flags": 4}, {"cross_origin": True}, {"nonce": b"x" * 32},
    {"user_handle": crypto.encode(b"x" * 16)}])
def test_assertion_requires_exact_origin_rp_presence_uv_nonce_user(enrollment, operator, options):
    pending = decrypt(enrollment, encrypted(enrollment, operator))
    with pytest.raises(crypto.ProtocolError, match="possession verification failed"):
        prove(enrollment, pending, assertion(enrollment, pending, operator, **options))


def test_capability_key_context_and_hpke_domain_are_required(enrollment, operator):
    with pytest.raises(crypto.ProtocolError):
        decrypt(enrollment, pairing.encrypt_registration(registration(enrollment, operator),
            descriptor=enrollment.descriptor, capability=crypto.encode(b"x" * 32)))
    raw = encrypted(enrollment, operator)
    with pytest.raises(crypto.ProtocolError):
        pairing.decrypt_registration(raw, expected=enrollment.descriptor, private_key=ec.generate_private_key(ec.SECP256R1()))
    for name, value in [("workspace_id", crypto.encode(b"x" * 16)), ("setup_origin", "https://other.example.com"),
                        ("runtime_binding_digest", "b" * 64), ("enrollment_id", crypto.encode(b"x" * 16))]:
        changed = pairing.EnrollmentDescriptor.model_validate({**enrollment.descriptor.model_dump(), name: value})
        with pytest.raises(crypto.ProtocolError):
            pairing.decrypt_registration(raw, expected=changed, private_key=enrollment.private_key)
    body = crypto.parse(raw)
    body["context"]["host_id"] = "b" * 64
    with pytest.raises(crypto.ProtocolError):
        decrypt(enrollment, crypto.canonical(body))
    context = pairing.registration_context(enrollment.descriptor).model_dump()
    ct = crypto.SUITE.encrypt(crypto.canonical({"capability": enrollment.capability,
        "registration": registration(enrollment, operator)}), enrollment.private_key.public_key(),
        info=crypto.INFO + crypto.canonical(context))
    with pytest.raises(crypto.ProtocolError):
        decrypt(enrollment, crypto.canonical({"context": context, "enc": crypto.encode(ct[:65]), "ct": crypto.encode(ct[65:])}))


def test_proof_binds_private_key_and_entire_enrollment(enrollment, operator):
    pending = decrypt(enrollment, encrypted(enrollment, operator))
    raw = assertion(enrollment, pending, operator)
    other_key = ec.generate_private_key(ec.SECP256R1())
    with pytest.raises(crypto.ProtocolError):
        prove(enrollment, pending, assertion(enrollment, pending, (other_key, operator[1])))
    other_credential = AttestedCredentialData.create(Aaguid.NONE, operator[1].credential_id, ES256.from_cryptography_key(other_key.public_key()))
    other_pending = pairing.pending_credential(bytes(other_credential), enrollment.descriptor.enrollment_id)
    assert other_pending.fingerprint != pending.fingerprint
    with pytest.raises(crypto.ProtocolError):
        prove(enrollment, other_pending, raw)
    changed = pairing.EnrollmentDescriptor.model_validate({**enrollment.descriptor.model_dump(), "workspace_id": crypto.encode(b"x" * 16)})
    with pytest.raises(crypto.ProtocolError):
        pairing.verify_possession(raw, descriptor=changed, pending=pending, nonce=NONCE)
    assert pairing.pending_credential(pending.enrolled_credential, crypto.encode(b"x" * 16)).fingerprint != pending.fingerprint


def test_raw_id_must_match_registered_attested_id(enrollment, operator):
    value = registration(enrollment, operator)
    value["id"] = value["rawId"] = crypto.encode(b"another-credential")
    with pytest.raises(crypto.ProtocolError):
        decrypt(enrollment, pairing.encrypt_registration(value, descriptor=enrollment.descriptor, capability=enrollment.capability))


@pytest.mark.parametrize("raw", [b'{"x":1,"x":2}', b'{"x":1.0}', b'{"x":9007199254740992}',
    b'{"x":"\xff"}', b" " * 32769, b'{"context":{},"enc":"Zg==","ct":"x"}'])
def test_enrollment_wire_rejects_ambiguous_or_oversized_input(enrollment, raw):
    with pytest.raises(crypto.ProtocolError, match="registration verification failed"):
        decrypt(enrollment, raw)


def test_assertion_unknown_fields_noncanonical_encoding_and_error_redaction(enrollment, operator):
    pending = decrypt(enrollment, encrypted(enrollment, operator))
    valid = crypto.parse(assertion(enrollment, pending, operator))
    for body in [{**valid, "sentinel-secret": "synthetic-private-value"},
                 {**valid, "response": {**valid["response"], "signature": valid["response"]["signature"] + "="}}]:
        with pytest.raises(crypto.ProtocolError) as failure:
            prove(enrollment, pending, crypto.canonical(body))
        assert str(failure.value) == "Enrollment possession verification failed."
    with pytest.raises(crypto.ProtocolError):
        prove(enrollment, pending, b" " * 16385)


@pytest.mark.parametrize("field,value", [("version", True), ("issued_at", 1.0),
    ("setup_origin", "http://setup.example.com"), ("setup_origin", "https://setup.example.com/path"),
    ("host_id", "b" * 64), ("recipient_public_key", crypto.encode(b"x" * 65)),
    ("registration_challenge", crypto.encode(b"short")), ("expires_at", 1900000700)])
def test_descriptor_is_strict(enrollment, field, value):
    with pytest.raises(ValueError):
        pairing.EnrollmentDescriptor.model_validate({**enrollment.descriptor.model_dump(), field: value})


def test_fingerprint_exact_domain_and_cbor_wire_vector():
    # Public deterministic fixture, unrelated to any account or authenticator.
    key = ec.derive_private_key(7, ec.SECP256R1())
    credential = AttestedCredentialData.create(Aaguid.NONE, b"pairing-public-fixture", ES256.from_cryptography_key(key.public_key()))
    identifier = crypto.encode(b"e" * 16)
    pending = pairing.pending_credential(bytes(credential), identifier)
    assert pending.credential_digest == "b6ff60f8ac6d24f487b3a9631bdf7939704996a0aa75e60ceb8a25fae284b7ca"
    assert pending.fingerprint == "972e7fee3c2d7bd65a20433bf917dcf0"


def test_registration_json_rejects_unknown_fields_before_verification(enrollment, operator):
    value = registration(enrollment, operator)
    plaintext = crypto.canonical({"capability": enrollment.capability, "registration": {**value, "unknown": "synthetic-private-value"}})
    context = pairing.registration_context(enrollment.descriptor).model_dump()
    ciphertext = crypto.SUITE.encrypt(plaintext, enrollment.private_key.public_key(),
        info=pairing.REGISTRATION_INFO + crypto.canonical(context))
    raw = crypto.canonical({"context": context, "enc": crypto.encode(ciphertext[:65]), "ct": crypto.encode(ciphertext[65:])})
    with pytest.raises(crypto.ProtocolError) as failure:
        decrypt(enrollment, raw)
    assert str(failure.value) == "Enrollment registration verification failed."


def test_browser_library_enrollment_encryption_interoperates(enrollment, operator):
    import os
    from pathlib import Path
    import subprocess

    if not os.environ.get("SLEEPTALK_HPKE_MODULE"):
        pytest.skip("Set SLEEPTALK_HPKE_MODULE to the isolated @hpke/core installation")
    raw = crypto.parse(encrypted(enrollment, operator))
    numbers = enrollment.private_key.private_numbers()
    public = {"kty": "EC", "crv": "P-256", "x": crypto.encode(numbers.public_numbers.x.to_bytes(32, "big")),
              "y": crypto.encode(numbers.public_numbers.y.to_bytes(32, "big"))}
    info = pairing.REGISTRATION_INFO + crypto.canonical(raw["context"])
    plaintext = crypto.SUITE.decrypt(crypto.decode(raw["enc"]) + crypto.decode(raw["ct"]), enrollment.private_key, info=info)
    data = {"publicKey": public, "privateKey": {**public, "d": crypto.encode(numbers.private_value.to_bytes(32, "big"))},
            "info": crypto.encode(info), "plaintext": crypto.encode(plaintext), "enc": raw["enc"], "ct": raw["ct"]}
    result = subprocess.run(["node", str(Path(__file__).resolve().parents[1] / "scripts/prove_remote_crypto.cjs")],
        input=json.dumps(data), text=True, capture_output=True, check=True, timeout=15)
    ciphertext = json.loads(result.stdout)
    pending = decrypt(enrollment, crypto.canonical({"context": raw["context"], **ciphertext}))
    assert pending.enrolled_credential == bytes(operator[1])
    prove(enrollment, pending, assertion(enrollment, pending, operator))


def test_client_challenge_rejects_noncanonical_encoding():
    canonical_challenge = crypto.encode(b"n" * 32)
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    index = alphabet.index(canonical_challenge[-1])
    alternate = canonical_challenge[:-1] + alphabet[index + 1]
    with pytest.raises(ValueError):
        pairing.ClientData(type="webauthn.get", origin=ORIGIN, challenge=alternate)


@pytest.mark.parametrize("extra_client", [
    {"other_keys_can_be_added_here": "do not compare clientDataJSON against a template. See https://goo.gl/yabPex"},
    {"future_client_member": {"nested": [1, "additional data"]}},
])
def test_browser_client_data_extra_members_preserve_registration_and_signed_possession(enrollment, operator, extra_client):
    pending = decrypt(enrollment, encrypted(enrollment, operator, extra_client=extra_client))
    raw = assertion(enrollment, pending, operator, extra_client=extra_client)
    assert prove(enrollment, pending, raw) is None
    changed = crypto.parse(raw)
    client = crypto.parse(crypto.decode(changed["response"]["clientDataJSON"]))
    client["tampered_after_signing"] = True
    changed["response"]["clientDataJSON"] = crypto.encode(crypto.canonical(client))
    with pytest.raises(crypto.ProtocolError):
        prove(enrollment, pending, crypto.canonical(changed))


@pytest.mark.parametrize("top_origin", [None, ORIGIN, "https://attacker.example"])
def test_client_data_top_origin_still_rejected(enrollment, operator, top_origin):
    with pytest.raises(crypto.ProtocolError):
        decrypt(enrollment, encrypted(enrollment, operator, extra_client={"topOrigin": top_origin}))
    pending = decrypt(enrollment, encrypted(enrollment, operator))
    with pytest.raises(crypto.ProtocolError):
        prove(enrollment, pending, assertion(enrollment, pending, operator, extra_client={"topOrigin": top_origin}))
