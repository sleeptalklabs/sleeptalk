from types import SimpleNamespace

from sleeptalk.policy import VoicePolicy
from sleeptalk.presentation import PresentationState
from sleeptalk.transports.terminal import TerminalTransport
from sleeptalk.transports.terminal_controls import (
    GlobalControl,
    SetNarrationStyle,
    SetReplyLength,
    SetSpeechSpeed,
    TerminalControls,
    TerminalKey,
)


def test_settings_menu_browses_without_changing_and_applies_on_enter() -> None:
    state = PresentationState(
        narration_style="narrated",
        reply_length="summarized",
        tts_speed=1.25,
    )
    controls = TerminalControls()

    assert controls.handle(TerminalKey.SETTINGS, state) is None
    root = controls.view(state)
    assert root is not None
    assert root.title == "Voice settings"
    assert [(row.label, row.value) for row in root.rows] == [
        ("Tool narration", "Narrated"),
        ("Response length", "Brief"),
        ("Speech speed", "1.25x"),
    ]

    controls.handle(TerminalKey.DOWN, state)
    controls.handle(TerminalKey.ENTER, state)
    choices = controls.view(state)
    assert choices is not None
    assert choices.title == "Voice settings / Response length"
    assert [(row.label, row.focused, row.current) for row in choices.rows] == [
        ("Full", False, False),
        ("Brief", True, True),
        ("Concise speech", False, False),
    ]

    controls.handle(TerminalKey.UP, state)
    assert controls.handle(TerminalKey.ENTER, state) == SetReplyLength("full")
    assert controls.view(state).title == "Voice settings"


def test_settings_menu_escape_backs_out_without_applying() -> None:
    state = PresentationState()
    controls = TerminalControls()
    controls.handle(TerminalKey.SETTINGS, state)
    controls.handle(TerminalKey.ENTER, state)

    assert controls.handle(TerminalKey.DOWN, state) is None
    assert controls.handle(TerminalKey.ESCAPE, state) is None
    assert controls.view(state).title == "Voice settings"
    assert controls.handle(TerminalKey.ESCAPE, state) is None
    assert controls.view(state) is None


def test_global_controls_work_without_closing_the_menu() -> None:
    state = PresentationState()
    controls = TerminalControls()
    controls.handle(TerminalKey.SETTINGS, state)

    assert controls.handle(TerminalKey.STOP, state) is GlobalControl.STOP
    assert controls.handle(TerminalKey.MUTE, state) is GlobalControl.TOGGLE_MUTE
    assert controls.handle(TerminalKey.QUIT, state) is GlobalControl.QUIT
    assert controls.view(state) is not None


def test_every_settings_page_emits_an_absolute_typed_change() -> None:
    state = PresentationState()
    controls = TerminalControls()

    controls.handle(TerminalKey.SETTINGS, state)
    controls.handle(TerminalKey.ENTER, state)
    controls.handle(TerminalKey.DOWN, state)
    assert controls.handle(TerminalKey.ENTER, state) == SetNarrationStyle("silence")

    controls.handle(TerminalKey.DOWN, state)
    controls.handle(TerminalKey.ENTER, state)
    controls.handle(TerminalKey.DOWN, state)
    assert controls.handle(TerminalKey.ENTER, state) == SetReplyLength("summarized")

    controls.handle(TerminalKey.DOWN, state)
    controls.handle(TerminalKey.ENTER, state)
    controls.handle(TerminalKey.DOWN, state)
    assert controls.handle(TerminalKey.ENTER, state) == SetSpeechSpeed(1.25)


class FakeSession:
    def __init__(self) -> None:
        self.policy = VoicePolicy()
        self.speed = 1.0

    def current_tts_speed(self) -> float:
        return self.speed

    def set_narration_style(self, value: object) -> str:
        self.policy.narration_style = str(value)
        return self.policy.narration_style

    def set_reply_length(self, value: object) -> str:
        self.policy.reply_length = str(value)
        return self.policy.reply_length

    def set_tts_speed(self, value: float | None) -> float:
        self.speed = float(value or 1.0)
        return self.speed


def test_transport_applies_menu_choices_through_the_session_before_prepare() -> None:
    session = FakeSession()
    config = SimpleNamespace(
        transport=SimpleNamespace(input_device="", output_device="", capture_mode=""),
        policy=session.policy,
    )
    transport = TerminalTransport(config, {})
    assert transport._session is None

    for key in (
        TerminalKey.SETTINGS,
        TerminalKey.ENTER,
        TerminalKey.DOWN,
        TerminalKey.DOWN,
        TerminalKey.ENTER,
    ):
        transport._handle_terminal_key(session, key)

    assert session.policy.narration_style == "keyboard"
    assert transport._presentation.narration_style == "keyboard"
    assert transport.events[-1] == {"event": "narration_style", "style": "keyboard"}


def test_transport_global_stop_and_mute_stay_live_inside_settings() -> None:
    session = FakeSession()
    config = SimpleNamespace(
        transport=SimpleNamespace(input_device="", output_device="", capture_mode=""),
        policy=session.policy,
    )
    transport = TerminalTransport(config, {})
    transport._handle_terminal_key(session, TerminalKey.SETTINGS)

    transport._handle_terminal_key(session, TerminalKey.STOP)
    transport._handle_terminal_key(session, TerminalKey.MUTE)

    assert transport._cut_in.is_set()
    assert transport._muted is True
    assert transport._controls.view(transport._presentation) is not None


def test_concise_speech_can_be_browsed_and_selected():
    state = PresentationState(reply_length='concise')
    controls=TerminalControls()
    controls.handle(TerminalKey.SETTINGS,state)
    controls.handle(TerminalKey.DOWN,state)
    controls.handle(TerminalKey.ENTER,state)
    view=controls.view(state)
    assert view is not None
    assert any(row.label=='Concise speech' and row.current for row in view.rows)
    assert controls.handle(TerminalKey.ENTER,state)==SetReplyLength('concise')
