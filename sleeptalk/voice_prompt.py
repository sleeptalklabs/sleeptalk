"""Shared voice-turn prompt shaping: coworker contract plus spoken skills."""

from __future__ import annotations

VOICE_SPEECH_CONTRACT = (
    "You are speaking out loud to a coworker. Use short spoken sentences. "
    "Do not use markdown, headings, bullet lists, or commit hashes. "
    "Say what changed and where. Offer more detail if they ask."
)


# Reply length "summarized". Trimming an answer after the fact can only ever
# remove sentences; asking for a short answer up front is what actually makes
# one — it is how a realtime voice model stays short, and it costs no latency,
# no tokens, and no fidelity, because the agent never writes the sprawl.
BRIEF_SPEECH_CONTRACT = (
    "Answer out loud in at most three short sentences. Lead with the outcome. "
    "Leave out the steps you took, the file names, and the line numbers. "
    "Do not list. If there is more worth saying, say they can ask for it."
)

CONCISE_SPEECH_CONTRACT = (
    "This is a voice conversation with a complete written result. "
    "Lead with a short, natural summary of the outcome, then provide the full "
    "answer, evidence, links, and details the caller needs in the same response. "
    "The opening will be spoken and the complete response will remain in text. "
    "Do not omit important information just to shorten the spoken part."
)


def prepare_voice_prompt(
    user_text: str,
    *,
    cwd: str = "",
    invoke: str = "slash",
    contract: bool = False,
    brief: bool = False,
    concise: bool = False,
    preface: str = "",
) -> str:
    from sleeptalk.voice_skills import discover_skills, rewrite_spoken_skill

    catalog = discover_skills(cwd)
    rewritten = rewrite_spoken_skill(user_text, catalog, invoke=invoke)
    # Call-level context (background jobs) leads, ahead of the reply contract.
    lead = f"{preface.strip()}\n\n" if preface.strip() else ""
    if brief:
        return f"{lead}{BRIEF_SPEECH_CONTRACT}\n\n{rewritten}"
    if concise:
        return f"{lead}{CONCISE_SPEECH_CONTRACT}\n\n{rewritten}"
    if contract:
        return f"{lead}{VOICE_SPEECH_CONTRACT}\n\n{rewritten}"
    return f"{lead}{rewritten}"
