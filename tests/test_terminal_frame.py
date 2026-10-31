import io
import os

from sleeptalk.config import AgentConfig, HybridRouteConfig, SleeptalkConfig, STTConfig, TTSConfig
from sleeptalk.presentation import Line, PresentationState, Word
from sleeptalk.transports.terminal_visual import (
    ENTER_S,
    RevealView,
    apply_frame_cues,
    conversation_rows,
    line_label,
    listening_dots,
    render_live_screen,
    speaker_tag,
    stack_from_config,
    step_reveal,
    TerminalBeing,
    timeline_rows,
    user_speaking_hint,
    visible_len,
)
from sleeptalk.transports.terminal_controls import TerminalControls, TerminalKey


def test_line_label_prints_tool_counts() -> None:
    line = Line(kind="tool", text="edit  serve.py", plus=160, minus=40)
    assert line_label(line) == "edit  serve.py  +160  -40"


def test_timeline_rows_keep_order() -> None:
    state = PresentationState(
        lines=(
            Line(kind="user", text="do the thing"),
            Line(kind="tool", text="read  a.py"),
            Line(kind="assistant", text="Done.", words=(Word("Done."),), spoken_index=1),
        )
    )
    assert timeline_rows(state) == ["do the thing", "read  a.py", "Done."]


def test_speaker_tag_stays_six_or_under() -> None:
    assert speaker_tag("you") == "you"
    assert speaker_tag("maxwell") == "max"
    assert speaker_tag("claude-code") == "cc"
    assert len(speaker_tag("openclaw")) <= 6


def test_stack_from_config_reads_the_running_profile() -> None:
    assert stack_from_config(None) == ()
    assert stack_from_config(SleeptalkConfig()) == ("pi", "deepgram/nova-3", "cartesia")
    hybrid = SleeptalkConfig(
        route=HybridRouteConfig(),
        agent=AgentConfig(type="claude_code", model="claude-sonnet-4-20250514"),
    )
    assert stack_from_config(hybrid) == ("claude-code/sonnet-4", "realtime/2.1")
    named = SleeptalkConfig(
        agent=AgentConfig(type="pi", model="opus"),
        stt=STTConfig(provider="deepgram", model="nova-3"),
        tts=TTSConfig(provider="cartesia", model="sonic-3"),
    )
    assert stack_from_config(named) == ("pi/opus", "deepgram/nova-3", "cartesia/sonic-3")


def test_header_shows_stack_not_the_surface() -> None:
    screen = render_live_screen(
        PresentationState(presence="listening"),
        0.0,
        columns=80,
        rows=16,
        agent="maxwell",
        stack=("pi/opus", "deepgram/nova-3", "cartesia/sonic-3"),
    )
    assert screen[0].startswith("sleeptalk")
    assert "maxwell" in screen[0]
    assert "pi/opus" in screen[0]
    assert "deepgram/nova-3" in screen[0]
    assert "cartesia/sonic-3" in screen[0]
    assert "terminal" not in screen[0]


def test_live_screen_is_an_instrument() -> None:
    state = PresentationState(
        presence="speaking",
        lines=(
            Line(kind="user", text="show me the frame"),
            Line(
                kind="assistant",
                text="Here's the voice frame.",
                words=(Word("Here's"), Word("the"), Word("voice"), Word("frame.")),
                spoken_index=2,
            ),
            Line(kind="tool", text="read  presentation.py"),
            Line(
                kind="assistant",
                text="Words stay dim until speech catches up.",
                words=tuple(Word(token) for token in "Words stay dim until speech catches up.".split()),
                spoken_index=0,
            ),
        ),
    )
    screen = render_live_screen(state, 0.4, columns=80, rows=24, muted=False, agent="maxwell")
    assert len(screen) == 24
    assert all(visible_len(row) == 80 for row in screen)
    assert screen[0].startswith("sleeptalk")
    assert "maxwell" in screen[0]
    assert "terminal" in screen[0]
    assert "live" in screen[0]
    assert set(screen[1].strip()) <= {"─", "-"}
    assert "you" in screen[2]
    assert "show me the frame" in screen[2]
    joined = "\n".join(screen)
    assert "Here's the voice frame." in joined
    assert "read  presentation.py" in joined
    assert "Words stay dim until speech catches up." in joined
    assert "speaking" in screen[-1]
    assert "Space stop" in screen[-1]
    assert "m mute" in screen[-1]
    assert "s settings" in screen[-1]
    assert "q quit" in screen[-1]
    assert "speed: 1x" in screen[-3]
    assert "SLEEP.TALK" not in joined
    assert "END CALL" not in joined


def test_muted_screen_shows_muted_state() -> None:
    screen = render_live_screen(PresentationState(), 0.0, columns=40, rows=16, muted=True)
    assert "muted" in screen[-1]


def test_footer_shows_current_tts_speed() -> None:
    screen = render_live_screen(
        PresentationState(tts_speed=1.25),
        0.0,
        columns=80,
        rows=16,
    )
    assert "speed: 1.25x" in screen[-3]


def test_live_screen_shows_labeled_settings_and_settings_key() -> None:
    screen = render_live_screen(
        PresentationState(
            narration_style="keyboard",
            reply_length="summarized",
            tts_speed=1.25,
        ),
        0.0,
        columns=80,
        rows=16,
    )

    assert "tools: keyboard" in screen[-3]
    assert "response: brief" in screen[-3]
    assert "speed: 1.25x" in screen[-3]
    assert "s settings" in screen[-1]
    assert "Space stop" in screen[-1]

    narrow = render_live_screen(PresentationState(), 0.0, columns=40, rows=16)
    assert "s settings" in narrow[-1]


def test_settings_menu_replaces_only_the_conversation_body() -> None:
    state = PresentationState(
        presence="listening",
        lines=(Line(kind="assistant", text="conversation stays behind the menu"),),
        reply_length="summarized",
    )
    controls = TerminalControls()
    controls.handle(TerminalKey.SETTINGS, state)
    menu = controls.view(state)
    screen = render_live_screen(
        state,
        0.0,
        columns=80,
        rows=24,
        settings=menu,
    )
    joined = "\n".join(screen)

    assert "Voice settings" in joined
    assert "Tool narration" in joined
    assert "Response length" in joined
    assert "Speech speed" in joined
    assert "conversation stays behind the menu" not in joined
    assert "listening" in screen[-1]
    assert all(visible_len(row) == 80 for row in screen)


def test_settings_choice_page_distinguishes_focus_from_current_value() -> None:
    state = PresentationState(narration_style="narrated")
    controls = TerminalControls()
    controls.handle(TerminalKey.SETTINGS, state)
    controls.handle(TerminalKey.ENTER, state)
    controls.handle(TerminalKey.DOWN, state)

    screen = render_live_screen(
        state,
        0.0,
        columns=40,
        rows=16,
        settings=controls.view(state),
    )
    joined = "\n".join(screen)

    assert "> Silent" in joined
    assert "Narrated" in joined and "current" in joined
    assert "Esc back" in joined
    assert all(visible_len(row) == 40 for row in screen)


def test_assistant_sentences_wrap_as_one_paragraph() -> None:
    first = (
        "A few that actually break things, roughly in order of how often they bite: "
        "**Symbol pronunciation.** Ask me something that forces me to say a file path, "
        "a version number, and a URL in TTS."
    )
    second = (
        "Numbers and units. \"1000 ms\", \"$2M\", \"52.8 percent\", \"72 hours\"."
    )
    state = PresentationState(
        lines=(
            Line(kind="user", text="what breaks TTS"),
            Line(kind="assistant", text=first),
            Line(kind="assistant", text=second),
        )
    )
    rows = conversation_rows(state, 48)
    kinds = [row.kind for row in rows]
    assert kinds[0] == "user"
    assert kinds[1] == ""
    assert all(kind == "assistant" for kind in kinds[2:])
    assert " ".join(row.text for row in rows if row.kind == "assistant").startswith(
        "A few that actually break things"
    )
    screen = render_live_screen(state, 0.0, columns=80, rows=24)
    assert all(visible_len(row) == 80 for row in screen)
    joined = " ".join(row.strip() for row in screen if row.strip())
    assert "URL in TTS." in joined
    assert "$2M" in joined
    assert "how often they bite:" in joined


def test_feed_caps_at_eighty_in_a_wide_terminal() -> None:
    text = " ".join(["word"] * 40)
    state = PresentationState(lines=(Line(kind="assistant", text=text),))
    screen = render_live_screen(state, 0.0, columns=120, rows=24, agent="mx")
    assert all(visible_len(row) == 120 for row in screen)
    body = next(row for row in screen[2:] if "word" in row)
    assert body[88:].strip() == "" or visible_len(body[:88].rstrip()) <= 88


def test_live_screen_uses_instrument_ink() -> None:
    state = PresentationState(
        presence="listening",
        partial="and ping me when it's gr",
        latency_ms=38,
        lines=(
            Line(kind="user", text="show me the frame"),
            Line(kind="assistant", text="Here's the voice frame."),
            Line(kind="tool", text="edit  serve.py", plus=160, minus=40),
        ),
    )
    screen = render_live_screen(state, 0.0, columns=80, rows=24, color=True, agent="maxwell")
    assert "\033[2mshow me the frame\033[0m" in "\n".join(screen)
    assert "Here's the voice frame." in "\n".join(screen)
    assert "\033[3;33mand ping me when it's gr|\033[0m" in "\n".join(screen)
    assert "\033[33m● live\033[0m" in screen[0]
    assert "38ms" in screen[0]
    assert "\033[33m● listening\033[0m" in screen[-1]
    assert "+160" in "\n".join(screen)
    assert "\033[32m" not in "\n".join(screen)
    assert all(visible_len(row) == 80 for row in screen)


def test_user_speaking_hint_policy() -> None:
    assert user_speaking_hint(partial="", hearing=True).kind == "dots"
    assert user_speaking_hint(partial="hi", hearing=True).kind == "partial"
    assert user_speaking_hint(partial="", hearing=False).kind == "none"


def test_hearing_without_partial_paints_you_row_dots() -> None:
    state = PresentationState(presence="listening", partial="")
    phase = 0.4
    screen = render_live_screen(state, phase, columns=80, rows=24, hearing=True, color=True)
    rows = conversation_rows(state, 48)
    assert all(row.kind != "user" or not row.live for row in rows)
    joined = "\n".join(screen)
    assert "you" in joined
    assert listening_dots(phase) in joined
    assert "|" not in joined
    assert "\033[32m" not in joined


def test_partial_beats_hearing() -> None:
    state = PresentationState(presence="listening", partial="hello")
    assert user_speaking_hint(partial=state.partial, hearing=True).kind == "partial"
    screen = render_live_screen(state, 0.0, columns=80, rows=24, hearing=True, color=True)
    joined = "\n".join(screen)
    assert "hello|" in joined
    assert listening_dots(0.0) not in joined
    assert "\033[32m" not in joined


def test_listening_presence_is_not_dots() -> None:
    state = PresentationState(presence="listening", partial="")
    assert user_speaking_hint(partial="", hearing=False).kind == "none"
    screen = render_live_screen(state, 0.0, columns=80, rows=24, hearing=False)
    assert listening_dots(0.0) not in "\n".join(screen)


def test_muted_hearing_is_not_dots() -> None:
    state = PresentationState(presence="listening", partial="")
    screen = render_live_screen(
        state, 0.0, columns=80, rows=24, muted=True, hearing=True, color=True
    )
    assert listening_dots(0.0) not in "\n".join(screen)
    assert "\033[32m" not in "\n".join(screen)


def test_first_sight_assistant_has_no_enter_dim() -> None:
    state = PresentationState(lines=(Line(kind="assistant", text="Hello world"),))
    reveal = step_reveal(RevealView(), state, now=1.00)
    screen = render_live_screen(state, 0.0, columns=80, rows=24, color=True, reveal=reveal)
    joined = "\n".join(screen)
    assert "Hello world" in joined
    assert "\033[2mHello" not in joined
    assert "\033[2mworld" not in joined


def test_assistant_growth_enters_only_the_new_suffix() -> None:
    first = PresentationState(lines=(Line(kind="assistant", text="Hello world"),))
    grown = PresentationState(lines=(Line(kind="assistant", text="Hello world again"),))
    reveal = step_reveal(RevealView(), first, now=1.00)
    reveal = step_reveal(reveal, grown, now=1.01)
    screen = render_live_screen(
        grown, 0.0, columns=80, rows=24, color=True, reveal=reveal
    )
    joined = "\n".join(screen)
    assert "Hello world" in joined
    assert "\033[2magain\033[0m" in joined
    assert "\033[3;33m" not in joined
    assert "\033[32m" not in joined

    settled = step_reveal(reveal, grown, now=1.01 + ENTER_S)
    later = render_live_screen(grown, 0.0, columns=80, rows=24, color=True, reveal=settled)
    assert "\033[2magain\033[0m" not in "\n".join(later)


def test_wrapped_suffix_enter_marks_only_the_last_row() -> None:
    first = PresentationState(
        lines=(Line(kind="assistant", text="Hello world this is a longer line"),)
    )
    grown = PresentationState(
        lines=(
            Line(
                kind="assistant",
                text="Hello world this is a longer line extra",
            ),
        )
    )
    reveal = step_reveal(RevealView(), first, now=1.00)
    reveal = step_reveal(reveal, grown, now=1.01)
    rows = conversation_rows(grown, 18)
    marked = apply_frame_cues(
        rows,
        hint=user_speaking_hint(partial="", hearing=False),
        reveal=reveal,
        phase=0.0,
    )
    assistant = [row for row in marked if row.kind == "assistant"]
    assert assistant[-1].enter == 1
    assert all(row.enter == 0 for row in assistant[:-1])


def test_pinned_being_hearing_paints_dots(monkeypatch) -> None:
    class TTY(io.StringIO):
        def isatty(self) -> bool:
            return True

    monkeypatch.setattr(
        "sleeptalk.transports.terminal_visual.shutil.get_terminal_size",
        lambda fallback: os.terminal_size((80, 24)),
    )
    stream = TTY()
    being = TerminalBeing(stream=stream, fps=1)
    being.pin_top()
    being.set_hearing(True)
    being.close()
    text = stream.getvalue()
    assert "you" in text
    assert any(frame in text for frame in ("●··", "·●·", "··●", ".  ", " . ", "  ."))


def test_spoken_index_change_does_not_step_reveal() -> None:
    words = (Word("Hello"), Word("world"))
    first = PresentationState(
        lines=(Line(kind="assistant", text="Hello world", words=words, spoken_index=0),)
    )
    ticked = PresentationState(
        lines=(Line(kind="assistant", text="Hello world", words=words, spoken_index=2),)
    )
    reveal = step_reveal(RevealView(), first, now=1.00)
    assert step_reveal(reveal, ticked, now=1.01) == reveal


def test_a_placeholder_agent_name_shows_the_profile_name() -> None:
    from types import SimpleNamespace

    from sleeptalk.transports.terminal_visual import agent_display_name

    assert agent_display_name(SimpleNamespace(name="default", slug="kanye")) == "kanye"
    assert agent_display_name(SimpleNamespace(name="Ye", slug="kanye")) == "Ye"
    assert agent_display_name(SimpleNamespace(name="", slug="kanye")) == "kanye"
    assert agent_display_name(SimpleNamespace(name="sleeptalk", slug="sleeptalk")) == "sleeptalk"
    assert speaker_tag(agent_display_name(SimpleNamespace(name="default", slug="kanye"))) == "kanye"


def test_several_jobs_are_titled_two_at_most_and_the_rest_counted() -> None:
    from sleeptalk.presentation import WorkerEntry, WorkerJob

    def job(job_id: str, title: str, phase: str = "running", text: str = "") -> WorkerJob:
        entries = (WorkerEntry(kind="step", text=text),) if text else ()
        return WorkerJob(job_id=job_id, title=title, phase=phase, steps=12, entries=entries)

    state = PresentationState(jobs=(
        job("1", "todo app", text="writing the tests"),
        job("2", "latency run", text="analyze.py · reading the run"),
        job("3", "valuation", text="web_search · Six Flags"),
        job("4", "summary bug", phase="queued"),
    ))
    texts = [row.text for row in conversation_rows(state, 120) if row.text]
    assert any(text.startswith("todo app · writing the tests") for text in texts)
    assert any(text.startswith("latency run · analyze.py") for text in texts)
    assert not any("Six Flags" in text for text in texts)
    assert texts[-1] == "+2 more"
    # One job alone keeps today's untitled line.
    alone = PresentationState(jobs=(job("1", "todo app", text="writing the tests"),))
    assert [row.text for row in conversation_rows(alone, 120) if row.text] == ["writing the tests (12 steps)"]
