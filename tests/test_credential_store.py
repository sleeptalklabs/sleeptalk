from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from sleeptalk.credential_store import CredentialError, CredentialImpactError, CredentialStore

SECRET = 'sentinel-private-provider-value'


@pytest.fixture
def store(tmp_path):
    return CredentialStore(tmp_path / 'private', backend='file')


def test_independent_durable_credentials_and_value_free_metadata(store):
    reference = store.put_validated('deepgram', SECRET)
    other = store.put_validated('cartesia', 'other-sentinel')
    resumed = CredentialStore(store.root)
    assert resumed.get(reference) == SECRET
    assert resumed.get(other) == 'other-sentinel'
    assert SECRET not in repr(resumed)
    assert SECRET not in repr(asdict(resumed.status(reference)))
    assert resumed.status(reference).available
    assert store.root.stat().st_mode & 0o777 == 0o700
    assert (store.root / 'store.sqlite3').stat().st_mode & 0o777 == 0o600


def test_shared_key_replacement_requires_current_exact_impact(store):
    reference = store.put_validated('deepgram', SECRET, profile_id='first')
    store.bind_profile(reference, 'second')
    store.bind_profile(reference, 'second')
    with pytest.raises(CredentialImpactError) as error:
        store.replace_validated(reference, 'replacement', affected_profiles=['first'])
    assert error.value.profiles == ('first', 'second')
    assert store.get(reference) == SECRET
    store.replace_validated(reference, 'replacement', affected_profiles=['first', 'second'])
    assert store.get(reference) == 'replacement'
    assert store.status(reference).profiles == ('first', 'second')
    with pytest.raises(CredentialImpactError):
        store.remove(reference, affected_profiles=[])
    store.unbind_profile(reference, 'first')
    store.remove(reference, affected_profiles=['second'])
    with pytest.raises(CredentialError, match='not found'):
        store.get(reference)


def test_concurrent_profile_registration_is_not_lost(store):
    reference = store.put_validated('deepgram', SECRET)
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda i: CredentialStore(store.root).bind_profile(reference, f'profile-{i}'), range(40)))
    assert len(store.status(reference).profiles) == 40


def test_concurrent_independent_provider_writes(store):
    with ThreadPoolExecutor(max_workers=8) as pool:
        refs = list(pool.map(lambda i: CredentialStore(store.root, backend='file').put_validated('deepgram', f'value-{i}'), range(20)))
    assert len(set(refs)) == 20
    assert [store.get(ref) for ref in refs] == [f'value-{i}' for i in range(20)]


@pytest.mark.parametrize('operation', ['put', 'replace'])
def test_process_death_before_commit_recovers_without_partial_secret(store, operation):
    reference = store.put_validated('deepgram', SECRET)
    script = '''
import os, sys
from sleeptalk.credential_store import CredentialStore
s=CredentialStore(sys.argv[1], backend='file')
s._value=lambda row: os._exit(29)
if sys.argv[3] == 'replace':
    s.replace_validated(sys.argv[2], 'uncommitted-sentinel', affected_profiles=[])
else:
    s.put_validated('cartesia', 'uncommitted-sentinel', profile_id='incomplete')
'''
    result = subprocess.run([sys.executable, '-c', script, str(store.root), reference, operation])
    assert result.returncode == 29
    assert CredentialStore(store.root).get(reference) == SECRET
    db = sqlite3.connect(store.root / 'store.sqlite3')
    assert db.execute('SELECT COUNT(*) FROM credentials').fetchone()[0] == 1
    assert db.execute('SELECT COUNT(*) FROM profiles').fetchone()[0] == 0
    db.close()


def test_failure_during_file_verification_rolls_back(store, monkeypatch):
    reference = store.put_validated('deepgram', SECRET)
    monkeypatch.setattr(store, '_value', lambda row: 'wrong-value')
    with pytest.raises(CredentialError, match='verification'):
        store.replace_validated(reference, 'new-secret', affected_profiles=[])
    assert CredentialStore(store.root).get(reference) == SECRET


class FakeKeyring:
    def __init__(self):
        self.values = {}
        self.locked = False
        self.interrupt = False

    def get_password(self, service, slot):
        if self.locked:
            raise RuntimeError(SECRET)
        return self.values.get(slot)

    def set_password(self, service, slot, value):
        if self.locked:
            raise RuntimeError(value)
        self.values[slot] = value
        if self.interrupt:
            raise KeyboardInterrupt()

    def delete_password(self, service, slot):
        if self.locked:
            raise RuntimeError(SECRET)
        del self.values[slot]


@pytest.fixture
def keyring_store(tmp_path, monkeypatch):
    backend = FakeKeyring()
    monkeypatch.setattr('sleeptalk.credential_store._keyring_backend', lambda: backend)
    return CredentialStore(tmp_path / 'private', backend='keyring'), backend


def test_keyring_never_duplicates_values_in_database(keyring_store):
    store, backend = keyring_store
    ref = store.put_validated('deepgram', SECRET, profile_id='main')
    assert store.get(ref) == SECRET
    assert SECRET.encode() not in (store.root / 'store.sqlite3').read_bytes()
    assert store.status(ref).backend == 'keyring'
    store.replace_validated(ref, 'replacement', affected_profiles=['main'])
    assert list(backend.values.values()) == ['replacement']
    store.remove(ref, affected_profiles=['main'])
    assert not backend.values


def test_locked_store_fails_closed_without_file_fallback(keyring_store):
    store, backend = keyring_store
    reference = store.put_validated('deepgram', SECRET)
    backend.locked = True
    for action in [lambda: store.get(reference), lambda: store.status(reference), lambda: store.put_validated('cartesia', SECRET)]:
        with pytest.raises(CredentialError) as error:
            action()
        assert SECRET not in str(error.value)
    assert SECRET.encode() not in (store.root / 'store.sqlite3').read_bytes()
    with pytest.raises(CredentialError, match='different backend'):
        CredentialStore(store.root, backend='file').get(reference)


def test_interrupted_external_write_cleaned_on_reopen(keyring_store):
    store, backend = keyring_store
    ref = store.put_validated('deepgram', SECRET)
    backend.interrupt = True
    with pytest.raises(KeyboardInterrupt):
        store.replace_validated(ref, 'incomplete-new-value', affected_profiles=[])
    assert len(backend.values) == 2
    backend.interrupt = False
    assert CredentialStore(store.root).get(ref) == SECRET
    assert list(backend.values.values()) == [SECRET]


def test_missing_os_session_never_changes_existing_backend(keyring_store, monkeypatch):
    store, _ = keyring_store
    ref = store.put_validated('deepgram', SECRET)
    monkeypatch.setattr('sleeptalk.credential_store._keyring_backend', lambda: None)
    with pytest.raises(CredentialError, match='unavailable'):
        CredentialStore(store.root).get(ref)


def test_missing_keyring_entry_has_safe_status(keyring_store):
    store, backend = keyring_store
    ref = store.put_validated('deepgram', SECRET)
    backend.values.clear()
    assert not store.status(ref).available
    with pytest.raises(CredentialError, match='missing'):
        store.get(ref)


def test_symlinked_private_file_is_rejected(store, tmp_path):
    target = tmp_path / 'unrelated'
    target.write_text('leave me alone')
    store.root.mkdir()
    (store.root / 'store.sqlite3').symlink_to(target)
    with pytest.raises(CredentialError):
        store.put_validated('deepgram', SECRET)
    assert target.read_text() == 'leave me alone'


@pytest.mark.parametrize('remove_old', [True, False])
def test_authorized_env_migration_verifies_then_optionally_removes(store, tmp_path, remove_old):
    source = tmp_path / '.env'
    original = f'# local\nDEEPGRAM_API_KEY="{SECRET}"\nOTHER=untouched\n'
    source.write_text(original)
    checked = []
    ref = store.migrate_env_validated(source, 'DEEPGRAM_API_KEY', 'deepgram', validate=checked.append, remove_old=remove_old)
    assert checked == [SECRET]
    assert store.get(ref) == SECRET
    assert source.read_text() == ('# local\nOTHER=untouched\n' if remove_old else original)
    if remove_old:
        assert source.stat().st_mode & 0o777 == 0o600


def test_invalid_migrated_key_is_not_stored(store, tmp_path):
    source = tmp_path / '.env'
    source.write_text(f'DEEPGRAM_API_KEY={SECRET}\n')
    def rejected(value):
        raise ValueError(value)
    with pytest.raises(CredentialError) as error:
        store.migrate_env_validated(source, 'DEEPGRAM_API_KEY', 'deepgram', validate=rejected)
    assert SECRET not in str(error.value)
    assert not store.root.exists()
    assert SECRET in source.read_text()


def test_invalid_refs_and_values_do_not_echo_input(store):
    for action in [lambda: store.get(SECRET), lambda: store.put_validated(SECRET+'!', SECRET), lambda: store.put_validated('deepgram', '\x00'+SECRET)]:
        with pytest.raises(CredentialError) as error:
            action()
        assert SECRET not in str(error.value)


def test_retry_after_completed_write_uses_same_reference(store):
    ref = store.put_validated('deepgram', SECRET, request_id='setup-request-one')
    resumed = CredentialStore(store.root)
    assert resumed.put_validated('deepgram', SECRET, request_id='setup-request-one', profile_id='main') == ref
    assert resumed.status(ref).profiles == ('main',)
    with pytest.raises(CredentialError, match='already completed'):
        resumed.put_validated('deepgram', 'different', request_id='setup-request-one')
    assert resumed.get(ref) == SECRET


def test_concurrent_retry_does_not_duplicate_keyring_secret(keyring_store):
    store, backend = keyring_store
    with ThreadPoolExecutor(max_workers=4) as pool:
        refs = list(pool.map(lambda _: CredentialStore(store.root).put_validated('deepgram', SECRET, request_id='same'), range(8)))
    assert len(set(refs)) == 1
    assert len(backend.values) == 1


def test_keyring_cleanup_recovers_after_commit_failure(keyring_store, monkeypatch):
    store, backend = keyring_store
    reference = store.put_validated('deepgram', SECRET)
    original_delete = backend.delete_password
    def fail_delete(service, slot):
        raise RuntimeError(SECRET)
    monkeypatch.setattr(backend, 'delete_password', fail_delete)
    with pytest.raises(CredentialError):
        store.replace_validated(reference, 'replacement', affected_profiles=[])
    assert len(backend.values) == 2
    monkeypatch.setattr(backend, 'delete_password', original_delete)
    assert CredentialStore(store.root).get(reference) == 'replacement'
    assert list(backend.values.values()) == ['replacement']


def test_migration_preserves_multiline_unrelated_values(store, tmp_path):
    source = tmp_path / '.env'
    source.write_text(f'export DEEPGRAM_API_KEY="{SECRET}"\nOTHER="first\nsecond"\n# retained\n')
    store.migrate_env_validated(source, 'DEEPGRAM_API_KEY', 'deepgram', validate=lambda _: None, remove_old=True)
    assert source.read_text() == 'OTHER="first\nsecond"\n# retained\n'


def test_os_backend_initialization_error_does_not_fall_back(tmp_path, monkeypatch):
    from types import SimpleNamespace
    def locked():
        raise RuntimeError(SECRET)
    monkeypatch.setitem(sys.modules, 'keyring', SimpleNamespace(get_keyring=locked))
    store = CredentialStore(tmp_path / 'private')
    with pytest.raises(CredentialError, match='could not be opened') as error:
        store.put_validated('deepgram', SECRET)
    assert SECRET not in str(error.value)
    db = sqlite3.connect(store.root / 'store.sqlite3')
    assert db.execute('SELECT COUNT(*) FROM credentials').fetchone()[0] == 0
    assert db.execute('SELECT COUNT(*) FROM settings').fetchone()[0] == 0
    db.close()


def test_migration_retry_reuses_verified_destination(store, tmp_path):
    source = tmp_path / '.env'
    source.write_text(f'DEEPGRAM_API_KEY={SECRET}\n')
    first = store.migrate_env_validated(source, 'DEEPGRAM_API_KEY', 'deepgram', validate=lambda _: None)
    second = store.migrate_env_validated(source, 'DEEPGRAM_API_KEY', 'deepgram', validate=lambda _: None, remove_old=True)
    assert first == second
    assert store.get(second) == SECRET
    assert source.read_text() == ''


def test_a_file_store_moves_to_the_keychain_and_leaves_no_plaintext(tmp_path, monkeypatch):
    import sqlite3
    backend = FakeKeyring()
    monkeypatch.setattr('sleeptalk.credential_store._keyring_backend', lambda: None)
    store = CredentialStore(tmp_path / 'private', backend='file')
    first, second = store.put_validated('cartesia', 'first-' + SECRET), store.put_validated('discord', 'second-' + SECRET)
    monkeypatch.setattr('sleeptalk.credential_store._keyring_backend', lambda: backend)
    assert store.move_to_keyring() == 2
    reopened = CredentialStore(tmp_path / 'private')
    assert reopened.get(first) == 'first-' + SECRET and reopened.get(second) == 'second-' + SECRET
    assert reopened.status(first).backend == 'keyring'
    assert SECRET.encode() not in (tmp_path / 'private' / 'store.sqlite3').read_bytes()
    with sqlite3.connect(tmp_path / 'private' / 'store.sqlite3') as db:
        assert db.execute("SELECT COUNT(*) FROM credentials WHERE value IS NOT NULL").fetchone()[0] == 0
    assert reopened.move_to_keyring() == 0  # already moved


def test_an_interrupted_move_keeps_the_file_store_and_cleans_up_after(tmp_path, monkeypatch):
    backend = FakeKeyring()
    monkeypatch.setattr('sleeptalk.credential_store._keyring_backend', lambda: None)
    store = CredentialStore(tmp_path / 'private', backend='file')
    reference = store.put_validated('cartesia', SECRET)
    store.put_validated('groq', 'other-' + SECRET)
    monkeypatch.setattr('sleeptalk.credential_store._keyring_backend', lambda: backend)
    backend.interrupt = True
    with pytest.raises(KeyboardInterrupt):
        store.move_to_keyring()
    assert CredentialStore(tmp_path / 'private', backend='file').get(reference) == SECRET
    backend.interrupt = False
    assert store.move_to_keyring() == 2
    assert len(backend.values) == 2  # the interrupted slot was removed, not left behind


def test_moving_needs_an_os_store(tmp_path, monkeypatch):
    monkeypatch.setattr('sleeptalk.credential_store._keyring_backend', lambda: None)
    store = CredentialStore(tmp_path / 'private', backend='file')
    store.put_validated('cartesia', SECRET)
    with pytest.raises(CredentialError, match='sleeptalk\\[credentials\\]'):
        store.move_to_keyring()
