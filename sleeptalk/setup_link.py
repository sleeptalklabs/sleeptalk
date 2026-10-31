"""Phone links: the sealed channel between a phone and a host with no browser.

An agent on a remote host (a VPS reached through a chat app) sends the user a
short-lived, single-use link to the hosted call service. The link's fragment
carries this process's public key, which never reaches a server. The phone
opens a channel with it (``SetupChannel``) and the settings page, answering
the agent's request or just open for changes, runs through that channel
(``remote_settings``); the call service relays ciphertext it cannot read.

Trust: whoever can open the link before it is used or expires can set this
profile's keys. The agent's chat already controls this host, so the link grants
no authority beyond that chat. Never open the link from an agent tool.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import logging
import re
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.hpke import AEAD, KDF, KEM, Suite

from sleeptalk.call_provider import CallProviderError, CallTicket, P2PCallProvider
from sleeptalk.private_input import InputSession, atomic_json, lease

SUITE = Suite(KEM.P256, KDF.HKDF_SHA256, AEAD.AES_128_GCM)
LINK_SECONDS = 10 * 60


class LinkError(ValueError):
    """A submission this host can't accept; carries a fixed, value-free code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


def unb64url(value: Any, *, limit: int) -> bytes:
    if not isinstance(value, str) or not value or len(value) > limit or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise LinkError("invalid_submission")
    try:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except ValueError:
        raise LinkError("invalid_submission") from None


def public_key_text(private_key: ec.EllipticCurvePrivateKey) -> str:
    """The 65-byte uncompressed P-256 point, base64url, as carried in the link."""
    return b64url(private_key.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint))


def link_url(ticket: CallTicket, private_key: ec.EllipticCurvePrivateKey) -> str:
    return f"{ticket.join_url}/{public_key_text(private_key)}"


CHANNEL_INFO = b"sleeptalk/setup-channel/v1\0"
# What travels sealed once the page opens the channel: the settings page's
# requests and the host's answers.
SEALED_TYPES = frozenset({"settings.call", "settings.reply"})


class SetupChannel:
    """Both directions of one setup room, sealed with a key only the page and this host hold.

    The page picks a random AES-256-GCM key and seals it to this host's link
    key (HPKE, like a provider key), so the relay, which never sees the
    link's fragment, can neither read the channel nor open one of its own.
    Each message's additional data binds its direction, room, and sequence
    number, so nothing in a channel can be replayed, reordered, or reflected
    back; the room refuses a hello it has already seen (``remote_settings``),
    so a recorded channel can't be reopened either.
    """

    def __init__(self, key: bytes, routing_id: str) -> None:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        if len(key) != 32:
            raise LinkError("invalid_channel")
        self._aead = AESGCM(key)
        self.routing_id = routing_id
        self._sent = 0
        self._received = 0

    @classmethod
    def accept(cls, private_key: ec.EllipticCurvePrivateKey, payload: dict, *, routing_id: str) -> "SetupChannel":
        """Open the page's setup.hello: the channel key sealed to this host."""
        if not isinstance(payload, dict) or set(payload) != {"enc", "ct"}:
            raise LinkError("invalid_channel")
        enc = unb64url(payload["enc"], limit=128)
        ct = unb64url(payload["ct"], limit=512)
        if len(enc) != KEM.P256.enc_length():
            raise LinkError("invalid_channel")
        try:
            plaintext = SUITE.decrypt(enc + ct, private_key, info=CHANNEL_INFO + json.dumps(
                {"routing_id": routing_id}, sort_keys=True, separators=(",", ":")).encode())
            secret = json.loads(plaintext)
        except Exception:
            raise LinkError("invalid_channel") from None
        if not isinstance(secret, dict) or set(secret) != {"key"}:
            raise LinkError("invalid_channel")
        return cls(unb64url(secret["key"], limit=64), routing_id)

    def _aad(self, direction: str, seq: int) -> bytes:
        return CHANNEL_INFO + json.dumps({"direction": direction, "routing_id": self.routing_id, "seq": seq},
                                         sort_keys=True, separators=(",", ":")).encode()

    def seal(self, kind: str, payload: dict) -> dict:
        import os

        self._sent += 1
        iv = os.urandom(12)
        ct = self._aead.encrypt(iv, json.dumps({"type": kind, "payload": payload}).encode(), self._aad("host", self._sent))
        return {"seq": self._sent, "iv": b64url(iv), "ct": b64url(ct)}

    def open(self, payload: dict) -> tuple[str, dict]:
        """(type, payload) of one sealed page message, or LinkError. A replayed
        or older sequence number is refused."""
        if not isinstance(payload, dict) or set(payload) != {"seq", "iv", "ct"}:
            raise LinkError("invalid_channel")
        seq = payload["seq"]
        if not isinstance(seq, int) or isinstance(seq, bool) or not self._received < seq < 2 ** 31:
            raise LinkError("invalid_channel")
        iv = unb64url(payload["iv"], limit=32)
        ct = unb64url(payload["ct"], limit=96 * 1024)
        try:
            inner = json.loads(self._aead.decrypt(iv, ct, self._aad("page", seq)))
        except Exception:
            raise LinkError("invalid_channel") from None
        if (not isinstance(inner, dict) or set(inner) != {"type", "payload"} or inner["type"] not in SEALED_TYPES
                or not isinstance(inner["payload"], dict)):
            raise LinkError("invalid_channel")
        self._received = seq
        return inner["type"], inner["payload"]


def _envelope(kind: str, payload: dict) -> str:
    return json.dumps({"v": 1, "id": str(uuid4()), "type": kind, "payload": payload})


async def serve(directory: Path, *, provider: P2PCallProvider | None = None) -> None:
    """Answer one agent request on the phone: the settings page, opened on that request."""
    from sleeptalk.private_input import _serve_until_done
    from sleeptalk.remote_settings import SettingsRoom, settings_url
    from sleeptalk.settings_page import SettingsPage
    from sleeptalk.setup_core import resolved_rendezvous_url

    session = InputSession(directory)
    lock_name = hashlib.sha256(str(session.profile_path).encode()).hexdigest()
    with lease(directory.parent / (lock_name + ".lock")), lease(directory / "running.lock"):
        session.active()
        session.restore()
        if session.state["status"] == "complete":
            return
        private_key = ec.generate_private_key(ec.SECP256R1())
        provider = provider or P2PCallProvider(resolved_rendezvous_url())
        ticket = await provider.mint_setup(ttl_seconds=min(LINK_SECONDS, max(1, int(session.state["expires_at"] - time.time()))))
        atomic_json(directory / "invitation.json", {"entry": "link", "setup_url": settings_url(ticket, private_key),
                                                   "setup_url_expires_at": ticket.expires_at})
        page = SettingsPage(session.profile_path, profiles_dir=session.profile_path.parent, request=session)
        stop = asyncio.Event()
        room = asyncio.create_task(SettingsRoom(page, ticket, private_key).run(until=stop))
        watch = asyncio.create_task(_serve_until_done(session, directory, stop=stop))
        try:
            await asyncio.wait({room, watch}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            stop.set()
            await asyncio.gather(room, watch, return_exceptions=True)
            (directory / "invitation.json").unlink(missing_ok=True)
            del private_key


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--directory", type=Path, required=True)
    args = parser.parse_args()
    logging.disable(logging.CRITICAL)
    try:
        asyncio.run(serve(args.directory))
    except CallProviderError as exc:
        atomic_json(args.directory / "failure.json", {"code": "call_service", "message": str(exc)})
        return 1
    except Exception:
        atomic_json(args.directory / "failure.json", {"code": "setup_unavailable"})
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
