"""Pairing proofs that produce a pending credential, never an operator binding.

The durable owner enforces expiry, one-use requests/nonces, scope and revocation.
Only a subsequent trusted local comparison and confirmation may enroll the key.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import hmac
import json
import secrets
from typing import Literal
from urllib.parse import urlsplit

from cryptography.hazmat.primitives.asymmetric import ec, ed25519
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from fido2 import cbor
from fido2.server import Fido2Server
from fido2.webauthn import AttestedCredentialData, AuthenticationResponse, RegistrationResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from sleeptalk import remote_crypto as crypto

HPKE_SUITE = "DHKEM-P256-HKDF-SHA256/HKDF-SHA256/AES-128-GCM"
REGISTRATION_INFO = b"sleeptalk/setup/enrollment/registration/v1\0"
POSSESSION_DOMAIN = b"sleeptalk/setup/enrollment/possession/v1\0"
FINGERPRINT_DOMAIN = b"sleeptalk/setup/enrollment/fingerprint/v1\0"
MAX_WEBAUTHN = 16384


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class EnrollmentDescriptor(StrictModel):
    version: Literal[1]
    enrollment_id: crypto.Identifier
    host_id: crypto.Digest
    host_signing_public_key: str = Field(max_length=43)
    hpke_suite: Literal["DHKEM-P256-HKDF-SHA256/HKDF-SHA256/AES-128-GCM"]
    recipient_public_key: str = Field(max_length=87)
    workspace_id: crypto.Identifier
    runtime_binding_digest: crypto.Digest
    display_host: str = Field(min_length=1, max_length=128)
    display_workspace: str = Field(min_length=1, max_length=128)
    setup_origin: str = Field(max_length=255)
    relay_origin: str = Field(max_length=255)
    issued_at: crypto.Counter
    expires_at: crypto.Counter
    capability_hash: crypto.Digest
    registration_challenge: str = Field(max_length=43)

    @field_validator("version", mode="before")
    @classmethod
    def integer_version(cls, value):
        if type(value) is not int:
            raise ValueError("Invalid enrollment version.")
        return value

    @field_validator("enrollment_id", "workspace_id")
    @classmethod
    def identifiers(cls, value):
        if len(crypto.decode(value)) != 16:
            raise ValueError("Invalid enrollment identifier.")
        return value

    @field_validator("setup_origin", "relay_origin")
    @classmethod
    def origins(cls, value):
        parts = urlsplit(value)
        if (parts.scheme != "https" or not parts.hostname or parts.username or parts.password
                or parts.path or parts.query or parts.fragment or parts.port not in (None, 443)
                or not parts.hostname.isascii()):
            raise ValueError("Invalid enrollment origin.")
        return value

    @model_validator(mode="after")
    def bindings(self):
        signing_key = crypto.decode(self.host_signing_public_key)
        if len(signing_key) != 32 or hashlib.sha256(signing_key).hexdigest() != self.host_id:
            raise ValueError("Invalid enrollment host binding.")
        recipient = crypto.decode(self.recipient_public_key)
        if len(recipient) != 65 or recipient[0] != 4:
            raise ValueError("Invalid enrollment recipient key.")
        ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), recipient)
        if len(crypto.decode(self.registration_challenge)) != 32 or self.expires_at - self.issued_at != 600:
            raise ValueError("Invalid enrollment challenge or lifetime.")
        return self


@dataclass(frozen=True)
class Enrollment:
    descriptor: EnrollmentDescriptor
    signature: str
    capability: str = field(repr=False)
    private_key: ec.EllipticCurvePrivateKey = field(repr=False)


@dataclass(frozen=True)
class PendingCredential:
    enrolled_credential: bytes = field(repr=False)
    credential_digest: str
    fingerprint: str


class RegistrationContext(StrictModel):
    enrollment_id: crypto.Identifier
    descriptor_digest: crypto.Digest
    host_id: crypto.Digest


class RegistrationEnvelope(StrictModel):
    context: RegistrationContext
    enc: str = Field(max_length=128)
    ct: str = Field(max_length=24000)


class RegistrationData(StrictModel):
    clientDataJSON: str = Field(min_length=1, max_length=4096)
    attestationObject: str = Field(min_length=1, max_length=12000)
    transports: list[Literal["usb", "nfc", "ble", "internal", "hybrid", "smart-card"]] = Field(default_factory=list, max_length=6)


class CredentialResponse(StrictModel):
    id: str = Field(min_length=1, max_length=1366)
    rawId: str = Field(min_length=1, max_length=1366)
    type: Literal["public-key"]
    authenticatorAttachment: Literal["platform", "cross-platform"] | None = None
    clientExtensionResults: dict = Field(default_factory=dict, max_length=0)

    @model_validator(mode="after")
    def identity(self):
        if self.id != self.rawId or not 1 <= len(crypto.decode(self.rawId)) <= 1024:
            raise ValueError("Invalid enrollment credential identifier.")
        return self


class Registration(CredentialResponse):
    response: RegistrationData


class RegistrationPlaintext(StrictModel):
    capability: str = Field(min_length=43, max_length=43)
    registration: Registration


class AssertionData(StrictModel):
    clientDataJSON: str = Field(min_length=1, max_length=4096)
    authenticatorData: str = Field(min_length=1, max_length=2048)
    signature: str = Field(min_length=1, max_length=2048)
    userHandle: str | None = Field(default=None, max_length=86)


class Assertion(CredentialResponse):
    response: AssertionData


class ClientData(StrictModel):
    model_config = ConfigDict(extra="ignore", strict=True, frozen=True)
    type: Literal["webauthn.create", "webauthn.get"]
    challenge: str = Field(min_length=43, max_length=43)
    origin: str = Field(max_length=255)
    crossOrigin: Literal[False] = False

    @model_validator(mode="before")
    @classmethod
    def top_level_only(cls, value):
        if isinstance(value, dict) and "topOrigin" in value:
            raise ValueError("Cross-origin enrollment is forbidden.")
        return value

    @field_validator("challenge")
    @classmethod
    def challenge_encoding(cls, value):
        if len(crypto.decode(value)) != 32:
            raise ValueError("Invalid enrollment client challenge.")
        return value

    @field_validator("crossOrigin", mode="before")
    @classmethod
    def not_cross_origin(cls, value):
        if value is not False:
            raise ValueError("Cross-origin enrollment is forbidden.")
        return value


def descriptor_digest(descriptor: EnrollmentDescriptor) -> str:
    return hashlib.sha256(crypto.canonical(descriptor.model_dump())).hexdigest()


def registration_context(descriptor: EnrollmentDescriptor) -> RegistrationContext:
    return RegistrationContext(enrollment_id=descriptor.enrollment_id,
        descriptor_digest=descriptor_digest(descriptor), host_id=descriptor.host_id)


def create_enrollment(*, host_signing_key: ed25519.Ed25519PrivateKey, workspace_id: str,
                      runtime_binding_digest: str, setup_origin: str, relay_origin: str,
                      display_host: str, display_workspace: str, now: int) -> Enrollment:
    try:
        capability = crypto.encode(secrets.token_bytes(32))
        private_key = ec.generate_private_key(ec.SECP256R1())
        signing_public_key = host_signing_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        descriptor = EnrollmentDescriptor(version=1, enrollment_id=crypto.encode(secrets.token_bytes(16)),
            host_id=hashlib.sha256(signing_public_key).hexdigest(),
            host_signing_public_key=crypto.encode(signing_public_key), hpke_suite=HPKE_SUITE,
            recipient_public_key=crypto.encode(private_key.public_key().public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)),
            workspace_id=workspace_id, runtime_binding_digest=runtime_binding_digest,
            display_host=display_host, display_workspace=display_workspace,
            setup_origin=setup_origin, relay_origin=relay_origin, issued_at=now, expires_at=now + 600,
            capability_hash=hashlib.sha256(crypto.decode(capability)).hexdigest(),
            registration_challenge=crypto.encode(secrets.token_bytes(32)))
        return Enrollment(descriptor, crypto.sign(descriptor.model_dump(), host_signing_key, purpose="enrollment"), capability, private_key)
    except Exception:
        raise crypto.ProtocolError("Enrollment creation failed.") from None


def _server(descriptor: EnrollmentDescriptor) -> Fido2Server:
    return Fido2Server({"id": urlsplit(descriptor.setup_origin).hostname, "name": "Sleeptalk private setup"},
        attestation="none", verify_origin=lambda origin: origin == descriptor.setup_origin)


def _registration_begin(descriptor: EnrollmentDescriptor):
    server = _server(descriptor)
    options, state = server.register_begin(
        {"id": crypto.decode(descriptor.enrollment_id), "name": "sleeptalk-operator", "displayName": "Sleeptalk operator"},
        user_verification="required", challenge=crypto.decode(descriptor.registration_challenge))
    return server, options, state


def registration_options(descriptor: EnrollmentDescriptor) -> dict:
    """Return the JSON publicKey options, with base64url binary members."""
    return crypto.parse(json.dumps(dict(_registration_begin(descriptor)[1])["publicKey"]).encode())


def pending_credential(enrolled_credential: bytes, enrollment_id: str) -> PendingCredential:
    """Reconstruct persisted, already verified pending data without confirming it."""
    try:
        if not 1 <= len(enrolled_credential) <= 4096 or len(crypto.decode(enrollment_id)) != 16:
            raise ValueError
        credential = AttestedCredentialData(enrolled_credential)
        if not 1 <= len(credential.credential_id) <= 1024:
            raise ValueError
        identity = {"enrollment_id": enrollment_id, "credential_id": crypto.encode(credential.credential_id),
                    "public_key": crypto.encode(cbor.encode(credential.public_key))}
        fingerprint = hashlib.sha256(FINGERPRINT_DOMAIN + crypto.canonical(identity)).digest()[:16].hex()
        return PendingCredential(enrolled_credential, hashlib.sha256(enrolled_credential).hexdigest(), fingerprint)
    except Exception:
        raise crypto.ProtocolError("Invalid pending enrollment credential.") from None


def encrypt_registration(registration: dict, *, descriptor: EnrollmentDescriptor, capability: str) -> bytes:
    """Reference sender for interoperability proofs; browsers use the same wire."""
    try:
        plaintext = crypto.canonical(RegistrationPlaintext(capability=capability,
            registration=Registration.model_validate(registration)).model_dump(exclude_none=True))
        if len(plaintext) > MAX_WEBAUTHN:
            raise ValueError
        public_key = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), crypto.decode(descriptor.recipient_public_key))
        context = registration_context(descriptor)
        ciphertext = crypto.SUITE.encrypt(plaintext, public_key, info=REGISTRATION_INFO + crypto.canonical(context.model_dump()))
        return crypto.canonical({"context": context.model_dump(), "enc": crypto.encode(ciphertext[:65]), "ct": crypto.encode(ciphertext[65:])})
    except Exception:
        raise crypto.ProtocolError("Enrollment encryption failed.") from None


def decrypt_registration(raw: bytes, *, expected: EnrollmentDescriptor,
                         private_key: ec.EllipticCurvePrivateKey) -> PendingCredential:
    """Verify registration for a trusted ledger descriptor, returning pending only."""
    try:
        envelope = RegistrationEnvelope.model_validate(crypto.parse(raw))
        context = registration_context(expected)
        if envelope.context != context:
            raise ValueError
        recipient = private_key.public_key().public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
        if crypto.encode(recipient) != expected.recipient_public_key:
            raise ValueError
        enc, ct = crypto.decode(envelope.enc), crypto.decode(envelope.ct)
        if len(enc) != 65 or not 16 <= len(ct) <= MAX_WEBAUTHN + 16:
            raise ValueError
        plaintext = crypto.SUITE.decrypt(enc + ct, private_key, info=REGISTRATION_INFO + crypto.canonical(context.model_dump()))
        body = RegistrationPlaintext.model_validate(crypto.parse(plaintext))
        capability = crypto.decode(body.capability)
        if len(capability) != 32 or not hmac.compare_digest(hashlib.sha256(capability).hexdigest(), expected.capability_hash):
            raise ValueError
        registration = body.registration.model_dump(exclude_none=True)
        for name in ("clientDataJSON", "attestationObject"):
            crypto.decode(registration["response"][name])
        response = RegistrationResponse.from_dict(registration)
        ClientData.model_validate(crypto.parse(bytes(response.response.client_data)))
        server, _, state = _registration_begin(expected)
        auth_data = server.register_complete(state, response)
        credential = auth_data.credential_data
        if credential is None or response.raw_id != credential.credential_id:
            raise ValueError
        if credential.public_key.get(3) not in {algorithm.alg for algorithm in server.allowed_algorithms}:
            raise ValueError
        return pending_credential(bytes(credential), expected.enrollment_id)
    except Exception:
        raise crypto.ProtocolError("Enrollment registration verification failed.") from None


def possession_challenge(descriptor: EnrollmentDescriptor, pending: PendingCredential, nonce: bytes) -> bytes:
    if len(nonce) != 32:
        raise crypto.ProtocolError("Invalid enrollment proof nonce.")
    context = {"enrollment_id": descriptor.enrollment_id, "descriptor_digest": descriptor_digest(descriptor),
               "credential_digest": pending.credential_digest, "nonce": crypto.encode(nonce)}
    return hashlib.sha256(POSSESSION_DOMAIN + crypto.canonical(context)).digest()


def _proof_begin(descriptor: EnrollmentDescriptor, pending: PendingCredential, nonce: bytes):
    server = _server(descriptor)
    credential = AttestedCredentialData(pending.enrolled_credential)
    options, state = server.authenticate_begin([credential], user_verification="required",
        challenge=possession_challenge(descriptor, pending, nonce))
    return server, credential, options, state


def proof_options(descriptor: EnrollmentDescriptor, pending: PendingCredential, nonce: bytes) -> dict:
    return crypto.parse(json.dumps(dict(_proof_begin(descriptor, pending, nonce)[2])["publicKey"]).encode())


def verify_possession(raw_assertion: bytes, *, descriptor: EnrollmentDescriptor,
                      pending: PendingCredential, nonce: bytes) -> None:
    """Check proof for the caller's fresh nonce. This is not local confirmation."""
    try:
        if len(raw_assertion) > MAX_WEBAUTHN:
            raise ValueError
        assertion = Assertion.model_validate(crypto.parse(raw_assertion)).model_dump(exclude_none=True)
        for value in assertion["response"].values():
            crypto.decode(value)
        response = AuthenticationResponse.from_dict(assertion)
        ClientData.model_validate(crypto.parse(bytes(response.response.client_data)))
        if response.response.user_handle not in (None, crypto.decode(descriptor.enrollment_id)):
            raise ValueError
        server, credential, _, state = _proof_begin(descriptor, pending, nonce)
        server.authenticate_complete(state, [credential], response)
    except Exception:
        raise crypto.ProtocolError("Enrollment possession verification failed.") from None
