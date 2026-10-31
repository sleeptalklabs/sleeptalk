"""Route implementations behind the RealtimeSession contract (ADR 0003)."""

from sleeptalk.routes.audio_output import (
    AudioOutput,
    AudioOutputAccounting,
    InFlightTurn,
)
from sleeptalk.routes.base import (
    InterruptionGranularity,
    RecoveryOutcome,
    RecoveryResult,
    Route,
    RouteCapabilities,
    RouteCapabilityError,
    RouteError,
    RouteInput,
    RouteRuntimeError,
    RouteStateError,
    RouteType,
    TurnContext,
    TurnOutcome,
)

__all__ = [
    "AudioOutput",
    "AudioOutputAccounting",
    "InFlightTurn",
    "InterruptionGranularity",
    "RecoveryOutcome",
    "RecoveryResult",
    "Route",
    "RouteCapabilities",
    "RouteCapabilityError",
    "RouteError",
    "RouteInput",
    "RouteRuntimeError",
    "RouteStateError",
    "RouteType",
    "TurnContext",
    "TurnOutcome",
]
