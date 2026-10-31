"""Small, targeted edits to a saved profile.

The ``[tts]`` voice (from a running call) and ``[transport.options]``
delivery (from ``host ensure --tailnet``) are edited here. The file is
rewritten atomically and every other line is preserved byte for byte, so
bindings, references, and comments survive. Both key spellings the wizard
and the settings editor use (``key = "…"`` and ``"key" = "…"``) are handled.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import tomllib
from pathlib import Path

_SECTION = re.compile(r"^\s*\[(?P<name>[^\]]+)\]\s*$")
_VOICE_LINE = re.compile(r'^(?P<indent>\s*)(?P<key>"?voice_id"?)\s*=\s*.*$')


def set_tts_voice(profile_path: str | Path, voice_id: str) -> bool:
    """Set ``[tts] voice_id`` in the profile; returns whether the file changed."""
    path = Path(profile_path)
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    value = json.dumps(voice_id)
    out: list[str] = []
    section = ""
    replaced = False
    inserted = False
    tts_seen = False
    for line in lines:
        header = _SECTION.match(line)
        if header:
            if section == "tts" and not replaced and not inserted:
                # Keep the blank line(s) that separate sections after the new key.
                trailing: list[str] = []
                while out and not out[-1].strip():
                    trailing.append(out.pop())
                out.append(f'voice_id = {value}\n')
                out.extend(reversed(trailing))
                inserted = True
            section = header.group("name").strip().strip('"')
            if section == "tts":
                tts_seen = True
            out.append(line)
            continue
        if section == "tts" and not replaced:
            match = _VOICE_LINE.match(line)
            if match:
                out.append(f'{match.group("indent")}{match.group("key")} = {value}\n')
                replaced = True
                continue
        out.append(line)
    if section == "tts" and not replaced and not inserted:
        if out and not out[-1].endswith("\n"):
            out[-1] += "\n"
        out.append(f'voice_id = {value}\n')
        inserted = True
    if not tts_seen:
        if out and not out[-1].endswith("\n"):
            out[-1] += "\n"
        out.append(f'\n[tts]\nvoice_id = {value}\n')
    new_text = "".join(out)
    if new_text == text:
        return False
    # Never leave a profile that won't load: the edited file must parse and
    # carry exactly this voice, or nothing is written.
    try:
        parsed = tomllib.loads(new_text)
    except tomllib.TOMLDecodeError as exc:
        raise ValueError("The voice ID can't be stored in the profile.") from exc
    if parsed.get("tts", {}).get("voice_id") != voice_id:
        raise ValueError("The voice ID can't be stored in the profile.")
    descriptor, temporary = tempfile.mkstemp(prefix=".sleeptalk-profile-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(new_text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return True


def set_delivery(profile_path: str | Path, delivery: str) -> bool:
    """Set ``[transport.options] delivery``; returns whether the file changed."""
    return set_string_key(profile_path, "transport.options", "delivery", delivery)


def set_string_key(profile_path: str | Path, section: str, key: str, value: str) -> bool:
    """Set one string key in one section, adding the section at the end if missing."""
    path = Path(profile_path)
    text = path.read_text(encoding="utf-8")
    inline = _set_in_inline_table(text, section, key, value)
    if inline is not None:
        return _write_checked(path, text, inline, section, key, value)
    lines = text.splitlines(keepends=True)
    encoded = json.dumps(value)
    line_pattern = re.compile(rf'^(?P<indent>\s*)(?P<key>"?{re.escape(key)}"?)\s*=\s*.*$')
    out: list[str] = []
    current = ""
    replaced = inserted = seen = False
    for line in lines:
        header = _SECTION.match(line)
        if header:
            if current == section and not replaced and not inserted:
                trailing: list[str] = []
                while out and not out[-1].strip():
                    trailing.append(out.pop())
                out.append(f"{key} = {encoded}\n")
                out.extend(reversed(trailing))
                inserted = True
            current = header.group("name").strip().replace('"', "")
            if current == section:
                seen = True
            out.append(line)
            continue
        if current == section and not replaced:
            match = line_pattern.match(line)
            if match:
                out.append(f'{match.group("indent")}{match.group("key")} = {encoded}\n')
                replaced = True
                continue
        out.append(line)
    if current == section and not replaced and not inserted:
        if out and not out[-1].endswith("\n"):
            out[-1] += "\n"
        out.append(f"{key} = {encoded}\n")
        inserted = True
    if not seen:
        if out and not out[-1].endswith("\n"):
            out[-1] += "\n"
        out.append(f"\n[{section}]\n{key} = {encoded}\n")
    return _write_checked(path, text, "".join(out), section, key, value)


def _set_in_inline_table(text: str, section: str, key: str, value: str) -> str | None:
    """The profile with ``key`` set inside ``section`` written as an inline table, or None.

    A profile rewritten by the terminal setup holds key references as
    ``"refs" = { ... }`` under ``[credentials]``; adding a ``[credentials.refs]``
    header next to it would define the table twice and nothing would load.
    """
    if "." not in section:
        return None
    parent, _, name = section.rpartition(".")
    try:
        current = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return None
    node: object = current
    for part in parent.split("."):
        node = node.get(part) if isinstance(node, dict) else None
    if not isinstance(node, dict) or not isinstance(node.get(name), dict):
        return None
    entry = re.compile(rf'^(?P<indent>\s*)(?P<key>"?{re.escape(name)}"?)\s*=\s*\{{.*\}}\s*$')
    lines = text.splitlines(keepends=True)
    in_parent = False
    for index, line in enumerate(lines):
        header = _SECTION.match(line)
        if header:
            in_parent = header.group("name").strip().replace('"', "") == parent
            continue
        match = entry.match(line) if in_parent else None
        if match:
            table = {**node[name], key: value}
            lines[index] = f'{match.group("indent")}{match.group("key")} = {_toml_inline(table)}\n'
            return "".join(lines)
    return None


def _write_checked(path: Path, text: str, new_text: str, section: str, key: str, value: str) -> bool:
    """Write ``new_text`` only if it loads and carries exactly this value."""
    if new_text == text:
        return False
    try:
        parsed = tomllib.loads(new_text)
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"The {key} value can't be stored in the profile.") from exc
    node: object = parsed
    for part in section.split("."):
        node = node.get(part) if isinstance(node, dict) else None
    if not isinstance(node, dict) or node.get(key) != value:
        raise ValueError(f"The {key} value can't be stored in the profile.")
    descriptor, temporary = tempfile.mkstemp(prefix=".sleeptalk-profile-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(new_text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return True


def set_table(profile_path: str | Path, section: str, values: dict[str, str | bool]) -> bool:
    """Replace one whole table (not its sub-tables), adding it at the end if missing.

    For a table whose keys depend on one discriminator: ``[route]`` for
    hybrid carries keys a cascade ``[route]`` must not have.
    """
    path = Path(profile_path)
    text = path.read_text(encoding="utf-8")
    body = f"[{section}]\n" + "".join(f"{key} = {json.dumps(value)}\n" for key, value in values.items())
    out: list[str] = []
    replaced = False
    for name, piece in toml_tables(text):
        if name == section and not replaced:
            # Keep the blank line(s) that separate it from the next table.
            blank = len(piece) - len(piece.rstrip("\n")) - 1
            out.append(body + "\n" * max(blank, 0))
            replaced = True
        else:
            out.append(piece)
    if not replaced:
        if out and not out[-1].endswith("\n"):
            out[-1] += "\n"
        out.append("\n" + body)
    new_text = "".join(out)
    if new_text == text:
        return False
    try:
        parsed = tomllib.loads(new_text)
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"The {section} settings can't be stored in the profile.") from exc
    node: object = parsed
    for part in section.split("."):
        node = node.get(part) if isinstance(node, dict) else None
    if not isinstance(node, dict) or {k: v for k, v in node.items() if not isinstance(v, dict)} != values:
        raise ValueError(f"The {section} settings can't be stored in the profile.")
    descriptor, temporary = tempfile.mkstemp(prefix=".sleeptalk-profile-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(new_text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return True


_TABLE_HEADER = re.compile(r"^\s*\[\[?\s*([^\]]+?)\s*\]\]?\s*(#.*)?$")


def toml_tables(text: str) -> list[tuple[str, str]]:
    """A TOML file as (table name, text) pieces in order; the preamble has name ""."""
    pieces: list[tuple[str, str]] = []
    name, lines = "", []
    for line in text.splitlines(keepends=True):
        header = _TABLE_HEADER.match(line)
        if header:
            pieces.append((name, "".join(lines)))
            name, lines = header.group(1).strip().strip('"'), []
        lines.append(line)
    pieces.append((name, "".join(lines)))
    return [piece for piece in pieces if piece[1]]


def keep_unmodelled_policy(new_text: str, old_text: str) -> str:
    """Carry [policy] settings the wizard doesn't write from the old profile.

    The wizard regenerates a profile from its answers, which don't include
    every policy knob (turn taking set on the settings page, a hand-added
    [policy.interrupt]). Those are kept; everything the wizard does write,
    and every other section, is its to decide.
    """
    original = new_text
    try:
        old = tomllib.loads(old_text)
        new = tomllib.loads(new_text)
    except tomllib.TOMLDecodeError:
        return new_text
    old_policy = old.get("policy") or {}
    new_policy = new.get("policy") or {}
    scalars = [f"{key} = {json.dumps(value)}" for key, value in old_policy.items()
               if key not in new_policy and not isinstance(value, dict)
               and (isinstance(value, (str, bool, int, float)) or (isinstance(value, list) and all(isinstance(v, str) for v in value)))]
    if scalars:
        if re.search(r"^\[policy\]\s*$", new_text, re.M):
            new_text = re.sub(r"^\[policy\]\s*$", lambda m: m.group(0) + "\n" + "\n".join(scalars), new_text, count=1, flags=re.M)
        else:
            new_text = new_text.rstrip("\n") + "\n\n[policy]\n" + "\n".join(scalars) + "\n"
    missing = {name for name, value in old_policy.items() if isinstance(value, dict) and name not in new_policy}
    if missing:
        extra = "".join(piece for name, piece in toml_tables(old_text)
                        if name.startswith("policy.") and name.split(".")[1].strip('"') in missing)
        if extra:
            new_text = new_text.rstrip("\n") + "\n\n" + extra.rstrip("\n") + "\n"
    try:
        tomllib.loads(new_text)
    except tomllib.TOMLDecodeError:
        return original  # never trade a loadable profile for a kept setting
    return new_text


# What the wizard writes itself, per table. Anything else in these tables was
# set elsewhere (the settings page, `host`, a hand edit) and survives a save.
_WIZARD_KEYS: dict[str, frozenset[str]] = {
    "route": frozenset({"type", "provider", "model", "voice", "speech_model", "transcription_model", "language"}),
    "transport": frozenset({"type", "guild_id", "client_id", "token_env", "authorization_required",
                            "operator_ids", "allowed_channel_ids", "capture_mode"}),
    "transport.options": frozenset({"delivery"}),
    "stt": frozenset({"provider", "model", "language"}),
    "tts": frozenset({"provider", "model", "voice_id", "language", "speed"}),
    "log": frozenset({"latency"}),
}
# Tables the wizard (or the credential writer, or the runtime binding) owns outright.
_WIZARD_OWNED = ("agent", "credentials", "binding", "policy")


def _toml_inline(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_inline(item) for item in value) + "]"
    if isinstance(value, dict):
        return "{" + ", ".join(f"{json.dumps(str(key))} = {_toml_inline(item)}" for key, item in value.items()) + "}"
    raise TypeError(f"can't write {type(value).__name__} back to TOML")


def _dig(document: dict, dotted: str) -> object:
    node: object = document
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def _same_owner(name: str, old: dict, new: dict) -> bool:
    """Does this table still describe the same thing it did before the save?

    Provider options belong to their provider, Discord settings to a Discord
    transport, Realtime options to the Realtime route. Switching one of those
    leaves the old table's extras behind. Switching to Realtime keeps the
    listening and speaking settings (as the settings page does), so switching
    back finds them.
    """
    top = name.split(".", 1)[0]
    old_route = _dig(old, "route.type") or "cascade"
    new_route = _dig(new, "route.type") or "cascade"
    if top in ("stt", "tts"):
        if new_route == "hybrid":
            return True
        return _dig(old, f"{top}.provider") == _dig(new, f"{top}.provider")
    if top == "transport":
        return (_dig(old, "transport.type") or "discord") == (_dig(new, "transport.type") or "discord")
    if top == "route":
        return old_route == new_route
    return True


def keep_unmodelled_settings(new_text: str, old_text: str) -> str:
    """Carry every setting the wizard doesn't write from the old profile.

    The wizard regenerates a profile from its answers. Settings made on the
    settings page or by hand that its answers don't carry (Realtime's
    specialist tool, Discord speaker lists, a provider's options, turn
    taking) are kept, table by table, while the table still belongs to the
    same route, transport, or provider.
    """
    new_text = keep_unmodelled_policy(new_text, old_text)
    original = new_text
    try:
        old = tomllib.loads(old_text)
        new = tomllib.loads(new_text)
    except tomllib.TOMLDecodeError:
        return new_text
    appended: list[str] = []
    for name, _piece in toml_tables(old_text):
        if not name or name.split(".", 1)[0] in _WIZARD_OWNED or not _same_owner(name, old, new):
            continue
        old_table = _dig(old, name)
        if not isinstance(old_table, dict):
            continue
        new_table = _dig(new, name)
        modelled = _WIZARD_KEYS.get(name, frozenset())
        if name in ("stt", "tts") and _dig(new, "route.type") == "hybrid":
            # Realtime writes only the speed; the listening and speaking
            # settings stay, so switching back finds them.
            modelled = frozenset({"speed"})
        missing = {key: value for key, value in old_table.items()
                   if not isinstance(value, dict) and key not in modelled
                   and not (isinstance(new_table, dict) and key in new_table)}
        if not missing:
            continue
        try:
            lines = "\n".join(f"{json.dumps(key)} = {_toml_inline(value)}" for key, value in missing.items())
        except TypeError:
            continue
        if isinstance(new_table, dict):
            header = re.compile(r"^\s*\[\s*" + re.escape(name) + r"\s*\]\s*$", re.M)
            if header.search(new_text):
                new_text = header.sub(lambda m: m.group(0) + "\n" + lines, new_text, count=1)
                continue
        appended.append(f"[{name}]\n{lines}\n")
    if appended:
        new_text = new_text.rstrip("\n") + "\n\n" + "\n".join(appended)
    try:
        tomllib.loads(new_text)
    except tomllib.TOMLDecodeError:
        return original  # never trade a loadable profile for a kept setting
    return new_text

