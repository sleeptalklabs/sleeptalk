"""Shared, non-interactive setup operation for wizard and host callers."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from sleeptalk import setup_core
from sleeptalk.setup_core import SetupAnswers

SetupStatus = Literal["ready", "needs_input", "failed"]


@dataclass(frozen=True, slots=True)
class SetupResult:
    """Private operation result. ``answers`` may contain secrets; never print it."""

    status: SetupStatus
    answers: SetupAnswers
    phase: str = ""
    messages: tuple[str, ...] = ()
    written: tuple[str, ...] = ()

    @property
    def ready(self) -> bool:
        return self.status == "ready"


def ensure_dependencies(surface: str, *, repair: bool) -> tuple[str, ...]:
    """Converge Python and native dependencies for one invocation surface."""
    problems: list[str] = []
    extra = setup_core.missing_runtime_extra(surface)
    if extra:
        if repair:
            extra = setup_core.ensure_runtime_extra(surface)
        if extra:
            problems.append(extra)

    from sleeptalk import sysdeps

    problems.extend(sysdeps.ensure_required_for_surface(surface, repair=repair))
    return tuple(problems)


async def ensure_setup(
    answers: SetupAnswers,
    *,
    profiles_dir: Path,
    env_path: Path,
    live: bool = True,
    bind_runtime: bool = False,
) -> SetupResult:
    """Validate and converge one profile plus its local secret store.

    All validation completes before the profile or secret file is changed.
    File writing is delegated to the same idempotent writer used by the human
    wizard, so repeated calls with identical inputs do not rewrite either file.
    """
    missing = tuple(setup_core.missing_answer_fields(answers))
    if missing:
        return SetupResult(
            status="needs_input",
            answers=answers,
            phase="missing",
            messages=missing,
        )

    if live and "local" in (answers.stt_provider, answers.tts_provider):
        from sleeptalk import sysdeps

        extra = sysdeps.missing_local_extra()
        if extra:
            return SetupResult(status="needs_input", answers=answers, phase="dependencies", messages=(extra,))
        import asyncio

        from sleeptalk.speech import local_models

        try:
            if answers.stt_provider == "local":
                await asyncio.to_thread(local_models.ensure_model, local_models.get_model(answers.stt_model or local_models.DEFAULT_STT_MODEL, "stt"))
            if answers.tts_provider == "local":
                await asyncio.to_thread(local_models.ensure_model, local_models.resolve_tts_model(answers.tts_model, answers.tts_voice_id))
        except local_models.LocalModelError as exc:
            return SetupResult(status="needs_input", answers=answers, phase="dependencies", messages=(str(exc),))
    problems = tuple(await setup_core.validate_answers(answers, live=live))
    if problems:
        return SetupResult(
            status="needs_input",
            answers=answers,
            phase="invalid",
            messages=problems,
        )

    dependency_problems = ensure_dependencies(answers.surface, repair=live)
    if dependency_problems:
        return SetupResult(
            status="needs_input",
            answers=answers,
            phase="dependencies",
            messages=dependency_problems,
        )

    try:
        if answers.agent_type == "model" and (answers.model_key or not answers.model_credential_ref):
            from sleeptalk.credential_store import CredentialStore
            from sleeptalk.model_only_setup import auth_choice, store_key

            # The key lives only in the private store; the profile carries a reference.
            answers.model_credential_ref = store_key(
                CredentialStore(), auth_choice(answers.model_vendor, answers.model_base_url), answers.model_key
            )
            answers.model_key = ""
        written = tuple(
            setup_core.write_files(
                answers,
                profiles_dir=profiles_dir,
                env_path=env_path,
                bind_runtime=bind_runtime,
            )
        )
    except Exception:
        return SetupResult(
            status="failed",
            answers=answers,
            phase="write",
            messages=("local configuration could not be written",),
        )
    return SetupResult(
        status="ready",
        answers=answers,
        phase="complete",
        written=written,
    )
