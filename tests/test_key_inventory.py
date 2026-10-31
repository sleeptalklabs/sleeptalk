"""Replaced and orphaned keys leave the store; shared keys stay."""
from __future__ import annotations

import json
import tomllib

import pytest

from sleeptalk.credential_store import CredentialNotFoundError, CredentialStore
from sleeptalk.key_inventory import forget_unused, inventory

PROFILE = (
    '[agent]\ntype = "pi"\nworkspace_dir = "/w"\n\n[transport]\ntype = "web"\n\n'
    '[stt]\nprovider = "cartesia"\nlanguage = "en-US"\n\n[tts]\nprovider = "cartesia"\nlanguage = "en-US"\n\n'
    '[credentials.refs]\nCARTESIA_API_KEY = "{ref}"\n'
)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr("sleeptalk.key_inventory.profile_dirs", lambda extra=(), env=None: [p.resolve() for p in extra])
    store = CredentialStore(tmp_path / "store", backend="file")
    profiles = tmp_path / "profiles"
    profiles.mkdir()
    return store, profiles


def _profile(profiles, name, ref):
    path = profiles / f"{name}.toml"
    path.write_text(PROFILE.replace("{ref}", ref))
    return path


async def test_replacing_a_key_removes_the_old_one_unless_another_profile_uses_it(setup, monkeypatch):
    from sleeptalk import settings_page

    store, profiles = setup
    old = store.put_validated("cartesia", "old-key-value")
    path = _profile(profiles, "default", old)

    calls = []

    async def prove(*args, **kwargs):
        calls.append(1)
        return {"stt_model": "ink-2", "voice_id": "v"}, f"req-{len(calls)}"

    monkeypatch.setattr(settings_page, "prove_speech_key", prove)
    shared = _profile(profiles, "discord", old)
    await settings_page.switch_speech_service(path, "cartesia", "new-key-value", "", store=store)
    new = tomllib.loads(path.read_text())["credentials"]["refs"]["CARTESIA_API_KEY"]
    assert new != old and store.get(new) == "new-key-value"
    assert store.get(old) == "old-key-value"  # the discord profile still uses it

    shared.unlink()
    await settings_page.switch_speech_service(path, "cartesia", "newest-key-value", "", store=store)
    newest = tomllib.loads(path.read_text())["credentials"]["refs"]["CARTESIA_API_KEY"]
    assert store.get(newest) == "newest-key-value"
    with pytest.raises(CredentialNotFoundError):
        store.get(new)
    # A profile file removed by hand leaves its key for prune, not for this save.
    assert store.get(old) == "old-key-value"
    assert forget_unused([old], store=store, extra_dirs=[profiles]) == [old]


def test_deleting_a_profile_removes_only_keys_nobody_else_uses(setup, monkeypatch):
    from sleeptalk import settings_page

    store, profiles = setup
    shared = store.put_validated("cartesia", "shared-value")
    own = store.put_validated("discord", "bot-token-value")
    doomed = _profile(profiles, "doomed", shared)
    doomed.write_text(doomed.read_text() + f'DISCORD_BOT_TOKEN = "{own}"\n')
    _profile(profiles, "keeper", shared)
    removed = settings_page.delete_profile(doomed, store=store)
    assert removed == [own]
    assert store.get(shared) == "shared-value"


def test_inventory_lists_every_key_with_its_users_and_prune_clears_the_rest(setup):
    store, profiles = setup
    used = store.put_validated("cartesia", "used-value")
    orphan = store.put_validated("openrouter", "orphan-value")
    _profile(profiles, "default", used)
    items = inventory(store=store, extra_dirs=[profiles])
    assert {i["reference"]: (i["provider"], i["used_by"], i["unused"]) for i in items} == {
        used: ("cartesia", ["default"], False), orphan: ("openrouter", [], True)}
    assert "value" not in json.dumps(items)
    assert forget_unused([used, orphan, "not-a-reference"], store=store, extra_dirs=[profiles]) == [orphan]
    assert [i["reference"] for i in inventory(store=store, extra_dirs=[profiles])] == [used]


@pytest.mark.parametrize("shared", [False, True])
def test_shared_setup_drops_replaced_keys_only_when_unused(setup, monkeypatch, tmp_path, shared):
    from sleeptalk import setup_core

    store, profiles = setup
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sleeptalk.credential_store.default_root", lambda: store.root)
    old = store.put_validated("cartesia", "old-key-value")
    path = _profile(profiles, "default", old)
    if shared:
        _profile(profiles, "keeper", old)
    answers = setup_core.SetupAnswers(
        name="Default", slug="default", surface="web", workspace_dir=str(tmp_path),
        stt_provider="cartesia", stt_model="ink-2", stt_key="new-key-value",
        tts_provider="cartesia", tts_voice_id="v", tts_key="new-key-value",
    )
    setup_core.write_files(answers, profiles_dir=profiles, env_path=tmp_path / ".env")

    reference = tomllib.loads(path.read_text())["credentials"]["refs"]["CARTESIA_API_KEY"]
    assert reference != old and store.get(reference) == "new-key-value"
    if shared:
        assert store.get(old) == "old-key-value"
    else:
        with pytest.raises(CredentialNotFoundError):
            store.get(old)


def test_automatic_cleanup_keeps_a_key_another_profile_is_bound_to(setup):
    from sleeptalk.key_inventory import forget_dropped, profile_ids

    store, profiles = setup
    old = store.put_validated("cartesia", "old-value")
    path = _profile(profiles, "default", old)
    store.bind_profile(old, "elsewhere@0123456789ab")  # a profile this scan can't see
    path.write_text(path.read_text().replace(old, store.put_validated("cartesia", "new-value")))
    assert forget_dropped({old}, path, store=store) == []
    assert store.get(old) == "old-value"
    # Bound to this profile alone: it goes.
    lone = store.put_validated("cartesia", "lone-value")
    for pid in list(profile_ids(path))[:1]:
        store.bind_profile(lone, pid)
    assert forget_dropped({lone}, path, store=store) == [lone]


def test_a_key_an_open_request_saved_is_never_pruned(setup, tmp_path, monkeypatch):
    import json as _json

    store, profiles = setup
    pending = store.put_validated("cartesia", "pending-value")
    root = tmp_path / "home" / ".local" / "share" / "sleeptalk" / "setup" / "req-1"
    root.mkdir(parents=True)
    (root / "state.json").write_text(_json.dumps({"status": "waiting", "refs": {"cartesia": pending}}))
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path / "home")
    assert forget_unused([pending], store=store, extra_dirs=[profiles]) == []
    assert store.get(pending) == "pending-value"
