"""Cryptographic boundary used by the paired remote setup service.

The caller must supply an enrolled credential and an exact, unexpired context
from its durable challenge ledger. Pairing, nonce consumption, profile authorization and credential commits
are owned by remote_pairing, remote_store and remote_host.
"""
from __future__ import annotations

import base64
import hashlib
import json
from typing import Annotated, Literal
from urllib.parse import urlsplit

import rfc8785
from cryptography.hazmat.primitives.asymmetric import ec, ed25519
from cryptography.hazmat.primitives.hpke import AEAD, KDF, KEM, Suite
from fido2.server import Fido2Server
from fido2.webauthn import AttestedCredentialData, AuthenticationResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

SUITE = Suite(KEM.P256, KDF.HKDF_SHA256, AEAD.AES_128_GCM)
INFO = b"sleeptalk/setup/submission/v1\0"
AUTHORIZE = b"sleeptalk/setup/authorize/v1\0"
MAX_BODY = 32768
MAX_SECRET = 8192
MAX_INT = 2**53 - 1
Identifier = Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{22,128}$")]
Digest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
Counter = Annotated[int, Field(ge=0, le=MAX_INT)]


class ProtocolError(ValueError):
    """A fixed, value-free error at the untrusted message boundary."""


class Context(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    version: Literal[1]
    request_id: Identifier
    descriptor_digest: Digest
    host_id: Digest
    operator_id: Identifier
    operator_binding_revision: Counter
    workspace_id: Identifier
    runtime_binding_digest: Digest
    provider_field: Literal["deepgram.api_key", "cartesia.api_key", "elevenlabs.api_key", "discord.bot_token", "agent.credential"]
    action: Literal["create", "replace"]
    expected_credential_generation: Counter
    submission_id: Identifier
    challenge_id: Identifier
    challenge_nonce: Identifier
    connection_generation: Identifier
    request_revision: Counter
    request_expires_at: Counter

    @field_validator("version", mode="before")
    @classmethod
    def integer_version(cls, value):
        if type(value) is not int:
            raise ValueError("Version must be an integer.")
        return value

    @field_validator("request_id", "operator_id", "workspace_id", "submission_id", "challenge_id", "connection_generation")
    @classmethod
    def identifier_encoding(cls, value):
        if len(decode(value)) != 16:
            raise ValueError("Identifier must contain 128 bits.")
        return value

    @field_validator("challenge_nonce")
    @classmethod
    def nonce_encoding(cls, value):
        if len(decode(value)) != 32:
            raise ValueError("Nonce must contain 256 bits.")
        return value


class Envelope(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    context: Context
    enc: str = Field(max_length=128)
    ct: str = Field(max_length=16384)
    assertion: dict


def encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def decode(value: str) -> bytes:
    try:
        raw = base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
        if encode(raw) != value:
            raise ValueError
        return raw
    except (ValueError, UnicodeError):
        raise ProtocolError("Invalid protocol encoding.") from None


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ProtocolError("Duplicate protocol field.")
        result[key] = value
    return result


def _forbidden_number(value):
    raise ProtocolError("Invalid protocol number.")


def parse(raw: bytes) -> dict:
    if len(raw) > MAX_BODY:
        raise ProtocolError("Protocol message is too large.")
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs,
                           parse_float=_forbidden_number, parse_constant=_forbidden_number)
        if not isinstance(value, dict):
            raise ValueError
        canonical(value)
        return value
    except (ValueError, UnicodeError, RecursionError):
        raise ProtocolError("Invalid protocol message.") from None


def canonical(value: dict) -> bytes:
    try:
        _canonical_types(value)
        return rfc8785.dumps(value)
    except (ValueError, TypeError, RecursionError):
        raise ProtocolError("Invalid canonical protocol value.") from None


def _canonical_types(value, depth=0):
    if depth > 20:
        raise ValueError
    if type(value) in (str, bool) or value is None:
        return
    if type(value) is int and -MAX_INT <= value <= MAX_INT:
        return
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        for item in value.values():
            _canonical_types(item, depth + 1)
        return
    if isinstance(value, list):
        for item in value:
            _canonical_types(item, depth + 1)
        return
    raise ValueError


def authorization_challenge(context: Context, enc: bytes, ct: bytes) -> bytes:
    return hashlib.sha256(AUTHORIZE + canonical(context.model_dump()) + hashlib.sha256(enc + ct).digest()).digest()


def encrypt(value: str, context: Context, public_key: ec.EllipticCurvePublicKey) -> tuple[str, str]:
    if not value or len(value.encode("utf-8")) > MAX_SECRET:
        raise ProtocolError("Invalid credential size.")
    plaintext = canonical({"value": value})
    if len(plaintext) > 12000:
        raise ProtocolError("Encoded credential is too large.")
    ciphertext = SUITE.encrypt(plaintext, public_key, info=INFO + canonical(context.model_dump()))
    length = KEM.P256.enc_length()
    return encode(ciphertext[:length]), encode(ciphertext[length:])


def _server(rp_id: str, origin: str) -> Fido2Server:
    parts = urlsplit(origin)
    if (parts.scheme != "https" or parts.hostname != rp_id or parts.username or parts.password
            or parts.path or parts.query or parts.fragment or parts.port not in (None, 443)):
        raise ProtocolError("Invalid relying party configuration.")
    return Fido2Server({"id": rp_id, "name": "Sleeptalk private setup"}, verify_origin=lambda candidate: candidate == origin)


def assertion_options(credential: bytes, challenge: bytes, origin: str) -> dict:
    options, _ = _server(urlsplit(origin).hostname, origin).authenticate_begin(
        [AttestedCredentialData(credential)], user_verification="required", challenge=challenge)
    return json.loads(json.dumps(dict(options)))["publicKey"]


def verify_assertion(assertion: dict, *, credential: bytes, challenge: bytes, origin: str) -> None:
    try:
        credential = AttestedCredentialData(credential)
        server = _server(urlsplit(origin).hostname, origin)
        _, state = server.authenticate_begin([credential], user_verification="required", challenge=challenge)
        response = AuthenticationResponse.from_dict(assertion)
        client = parse(bytes(response.response.client_data))
        if client.get("crossOrigin", False) is not False or "topOrigin" in client:
            raise ValueError
        server.authenticate_complete(state, [credential], response)
    except Exception:
        raise ProtocolError("Operator assertion failed.") from None


def decrypt_authorized(raw: bytes, *, expected: Context, private_key: ec.EllipticCurvePrivateKey,
                       enrolled_credential: bytes, rp_id: str, origin: str) -> str:
    """Verify an already paired operator before decrypting one ledger-bound value.

    Replays deliberately cannot be detected here: only the caller's durable
    ledger can distinguish an exact lost-ack retry from another mutation.
    """
    try:
        envelope = Envelope.model_validate(parse(raw))
        if envelope.context != expected:
            raise ValueError
        enc, ct = decode(envelope.enc), decode(envelope.ct)
        if len(enc) != KEM.P256.enc_length() or not 16 <= len(ct) <= MAX_SECRET * 2:
            raise ValueError
        credential = AttestedCredentialData(enrolled_credential)
        server = _server(rp_id, origin)
        _, state = server.authenticate_begin([credential], user_verification="required",
            challenge=authorization_challenge(expected, enc, ct))
        assertion = AuthenticationResponse.from_dict(envelope.assertion)
        client = parse(bytes(assertion.response.client_data))
        if client.get("crossOrigin", False) is not False or "topOrigin" in client:
            raise ValueError
        server.authenticate_complete(state, [credential], assertion)
        plaintext = SUITE.decrypt(enc + ct, private_key, info=INFO + canonical(expected.model_dump()))
        secret = parse(plaintext)
        if set(secret) != {"value"} or not isinstance(secret["value"], str):
            raise ValueError
        value = secret["value"]
        if not value or len(value.encode("utf-8")) > MAX_SECRET:
            raise ValueError
        return value
    except Exception:
        raise ProtocolError("Remote credential authorization failed.") from None


def sign(document: dict, key: ed25519.Ed25519PrivateKey, *, purpose: Literal["invitation", "receipt", "enrollment"]) -> str:
    return encode(key.sign(_domain(purpose) + canonical(document)))


def verify(document: dict, signature: str, pinned_key: ed25519.Ed25519PublicKey, *,
           purpose: Literal["invitation", "receipt", "enrollment"]) -> None:
    try:
        pinned_key.verify(decode(signature), _domain(purpose) + canonical(document))
    except Exception:
        raise ProtocolError("Host signature verification failed.") from None


def _domain(purpose: str) -> bytes:
    if purpose not in {"invitation", "receipt", "enrollment"}:
        raise ProtocolError("Invalid signature purpose.")
    return f"sleeptalk/setup/{purpose}/v1\0".encode()
