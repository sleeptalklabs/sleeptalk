"""Cursor first-party ACP launch profile and extension wire shapes."""

from __future__ import annotations

from typing import Any, Mapping

from sleeptalk.agents.profiles import AcpProfile

# The Cursor installer (curl https://cursor.com/install -fsS | bash) symlinks
# the CLI to ~/.local/bin/agent, with cursor-agent kept as a legacy name.
CURSOR_CLI_INSTALL_COMMAND = "curl https://cursor.com/install -fsS | bash"
CURSOR_CLI_BINARY_FALLBACKS = (
    "~/.local/bin/agent",
    "~/.local/bin/cursor-agent",
)
CURSOR_CLI_INSTALL_HINT = (
    f"install the Cursor CLI: {CURSOR_CLI_INSTALL_COMMAND} "
    "(or set CURSOR_API_KEY in .env to use the Python SDK instead)"
)
CURSOR_CLI_LOGIN_MESSAGE = (
    "Cursor CLI is installed but not logged in. Run `agent login` (it opens a "
    "browser), then start sleeptalk again. Or set CURSOR_API_KEY in .env to use "
    "the Cursor Python SDK instead."
)
CURSOR_CLI_MISSING_MESSAGE = (
    "Cursor CLI (`agent`) not found on PATH or in ~/.local/bin. Install it with "
    f"`{CURSOR_CLI_INSTALL_COMMAND}`, or set CURSOR_API_KEY in .env to use the "
    "Cursor Python SDK instead."
)

CURSOR_PROFILE = AcpProfile(
    name="cursor",
    binary="agent",
    extra_args=("acp",),
    extension_namespaces=frozenset({"cursor"}),
    binary_fallbacks=CURSOR_CLI_BINARY_FALLBACKS,
    install_hint=CURSOR_CLI_MISSING_MESSAGE,
)

CURSOR_PROFILE_NO_EXTENSIONS = AcpProfile(
    name="cursor",
    binary="agent",
    extra_args=("acp",),
    extension_namespaces=frozenset(),
    binary_fallbacks=CURSOR_CLI_BINARY_FALLBACKS,
    install_hint=CURSOR_CLI_MISSING_MESSAGE,
)

ASK_QUESTION = "cursor/ask_question"
CREATE_PLAN = "cursor/create_plan"
UPDATE_TODOS = "cursor/update_todos"
TASK = "cursor/task"
GENERATE_IMAGE = "cursor/generate_image"

BLOCKING_METHODS = frozenset({ASK_QUESTION, CREATE_PLAN})
NOTIFICATION_METHODS = frozenset({UPDATE_TODOS, TASK, GENERATE_IMAGE})
KNOWN_METHODS = BLOCKING_METHODS | NOTIFICATION_METHODS


def nested_outcome(kind: str, **fields: Any) -> dict[str, Any]:
    return {"outcome": {"outcome": kind, **fields}}


def ask_question_answered(
    answers: list[Mapping[str, Any]],
) -> dict[str, Any]:
    return nested_outcome("answered", answers=list(answers))


def ask_question_skipped(reason: str = "") -> dict[str, Any]:
    if reason:
        return nested_outcome("skipped", reason=reason)
    return nested_outcome("skipped")


def create_plan_accepted(plan_uri: str = "") -> dict[str, Any]:
    if plan_uri:
        return nested_outcome("accepted", planUri=plan_uri)
    return nested_outcome("accepted")


def create_plan_rejected(reason: str = "") -> dict[str, Any]:
    if reason:
        return nested_outcome("rejected", reason=reason)
    return nested_outcome("rejected")


def cancelled_outcome() -> dict[str, Any]:
    return nested_outcome("cancelled")


def first_question_answers(params: Mapping[str, Any]) -> list[dict[str, Any]]:
    answers: list[dict[str, Any]] = []
    for question in params.get("questions") or []:
        if not isinstance(question, Mapping):
            continue
        options = question.get("options") or []
        first = options[0] if options else {}
        option_id = ""
        if isinstance(first, Mapping):
            option_id = str(first.get("id") or "")
        answers.append(
            {
                "questionId": str(question.get("id") or ""),
                "selectedOptionIds": [option_id] if option_id else [],
            }
        )
    return answers


def question_prompt(params: Mapping[str, Any]) -> str:
    title = str(params.get("title") or "").strip()
    questions = [
        item for item in (params.get("questions") or []) if isinstance(item, Mapping)
    ]
    if questions:
        first = str(questions[0].get("prompt") or "").strip()
        if first:
            return first
    return title or "Cursor question"


def plan_prompt(params: Mapping[str, Any]) -> str:
    name = str(params.get("name") or "").strip()
    overview = str(params.get("overview") or "").strip()
    if name and overview:
        return f"{name}: {overview}"
    return name or overview or "Approve Cursor plan"


def auth_method_ids(init: Mapping[str, Any]) -> frozenset[str]:
    raw = init.get("authMethods") or init.get("authenticationMethods") or []
    found: set[str] = set()
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, Mapping):
                token = item.get("id") or item.get("methodId")
                if token:
                    found.add(str(token))
            elif isinstance(item, str):
                found.add(item)
    return frozenset(found)
