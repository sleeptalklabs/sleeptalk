"""Private credentials and lossless profile edits for the human setup wizard."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import tomllib

from sleeptalk.credential_store import CredentialError, CredentialStore


class InteractiveProfile:
    def __init__(self, path: Path, answers, *, store: CredentialStore | None = None):
        from sleeptalk.setup_core import profile_toml

        self.path = path
        self.store = store if store is not None else CredentialStore()
        self.original = tomllib.loads(path.read_text()) if path.is_file() else {}
        self.baseline = tomllib.loads(profile_toml(answers))
        if self.original:
            from sleeptalk.config import resolve_config_document

            self.effective = resolve_config_document(path).raw
        else:
            self.effective = {}
        self.refs = dict(self.effective.get("credentials", {}).get("refs", {}))

    def restore(self, name: str, provider: str) -> str:
        reference = self.refs.get(name)
        if reference is None:
            reference = self.store.default_reference(provider)
        if reference is None:
            return ""
        try:
            if self.store.status(reference).provider != provider:
                raise CredentialError("Credential reference belongs to another provider.")
            value = self.store.get(reference)
        except CredentialError:
            # An unavailable saved key needs replacement, never an ambient fallback.
            self.refs.pop(name, None)
            return ""
        self.refs[name] = reference
        return value

    def save_key(self, name: str, provider: str, value: str) -> None:
        reference = self.refs.get(name)
        if reference is None or self.store.get(reference) != value:
            reference = self.store.put_validated(provider, value)
        self.refs[name] = reference
        self.store.set_default_reference(provider, reference)

    def write(self, answers, *, env_path: Path) -> Path:
        from sleeptalk.agent_setup_options import toml_value
        from sleeptalk.config import (
            SleeptalkConfig, ensure_shared_root, project_root_for_profiles,
            shared_root_for_project, _merge_toml,
        )
        from sleeptalk.private_input import atomic_write
        from sleeptalk.runtime_binding import bind
        from sleeptalk.setup_core import env_entries, merge_env_entries, profile_toml

        # Keep Discord connection details available when the preferred call is local.
        previous_transport = self.effective.get("transport", {})
        if previous_transport.get("type") == "discord" and answers.surface != "discord":
            answers = replace(answers, surface="discord")
        generated = tomllib.loads(profile_toml(answers))
        document = deepcopy(self.original) if self.original else deepcopy(generated)
        if self.original:
            for section in self.baseline.keys() | generated.keys():
                before, after = self.baseline.get(section, {}), generated.get(section, {})
                target = document.setdefault(section, {})
                for key in before.keys() | after.keys():
                    if before.get(key) != after.get(key):
                        if key in after:
                            target[key] = after[key]
                        else:
                            target.pop(key, None)
            for section, discriminator in (("agent", "type"), ("transport", "type"), ("route", "type"), ("tts", "provider")):
                if self.baseline.get(section, {}).get(discriminator) != generated.get(section, {}).get(discriminator):
                    document[section] = generated[section]
            # A section the original never wrote (a hand-written profile with
            # no [route], say) must not survive as an empty table: a
            # discriminated section without its type fails validation.
            for section in list(document):
                if not document[section] and generated.get(section):
                    document[section] = deepcopy(generated[section])
            if generated["route"]["type"] == "hybrid":
                document.pop("stt", None)
        # New reference-backed speech keys do not enter a project .env.
        needed = env_entries(answers)
        entries = {key: value for key, value in needed.items() if key not in self.refs}
        # A provider the profile no longer uses takes its key reference with it.
        self.refs = {name: ref for name, ref in self.refs.items() if name in needed}
        document["credentials"] = {"refs": self.refs}
        document.pop("binding", None)
        root = project_root_for_profiles(self.path.parent)
        composed = document
        if root is not None:
            root.mkdir(parents=True, exist_ok=True)
            ensure_shared_root(root)
            base = tomllib.loads(shared_root_for_project(root).read_text())
            composed = _merge_toml(base, document)
        config = SleeptalkConfig.model_validate(composed)
        document["binding"] = bind(config.agent).model_dump()
        text = "\n\n".join(
            f"[{json.dumps(section)}]\n" + "\n".join(
                f"{json.dumps(key)} = {toml_value(value)}" for key, value in fields.items()
            ) for section, fields in document.items()
        ) + "\n"
        SleeptalkConfig.model_validate(tomllib.loads(text))
        if entries:
            merge_env_entries(entries, env_path)
        generations = {ref: self.store.status(ref).generation for ref in self.refs.values()}
        with self.store.bind_profile_generation(generations, str(self.path.resolve())):
            atomic_write(self.path, text.encode())
        for ref in set(self.original.get("credentials", {}).get("refs", {}).values()) - set(self.refs.values()):
            self.store.unbind_profile(ref, str(self.path.resolve()))
        return self.path
