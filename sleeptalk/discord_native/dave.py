from __future__ import annotations

from typing import Any


class NativeDaveError(RuntimeError):
    pass


class NativeDaveDecryptor:
    def __init__(self, voice_client: Any) -> None:
        self.voice_client = voice_client

    def decrypt_opus(self, user_id: int, opus_frame: bytes) -> bytes:
        dave_session = getattr(getattr(self.voice_client, "_connection", None), "dave_session", None)
        dave_protocol_version = getattr(
            getattr(self.voice_client, "_connection", None),
            "dave_protocol_version",
            0,
        )
        if not dave_session or dave_protocol_version == 0:
            return opus_frame
        if not getattr(dave_session, "ready", False):
            raise NativeDaveError("DAVE session is not ready.")

        try:
            import davey
        except ImportError as exc:
            raise NativeDaveError("Missing davey; install Discord DAVE support.") from exc

        decrypted = dave_session.decrypt(user_id, davey.MediaType.audio, opus_frame)
        if not isinstance(decrypted, bytes):
            raise NativeDaveError("DAVE decrypt returned a non-bytes payload.")
        return decrypted


def has_native_dave() -> bool:
    try:
        import davey  # noqa: F401
    except ImportError:
        return False
    return True
