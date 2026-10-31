"""Distribution auditing must reject extra, missing, and altered payloads."""

import io
from pathlib import Path
import tarfile
import zipfile

import pytest

from audit_release_contents import audit

SOURCES = {
    'sleeptalk/__init__.py': b'"""Example package."""',
    'sleeptalk/voice_loop/data/model.onnx': b'model bytes',
    'tests/test_example.py': b'assert True',
    'LICENSE': b'MIT',
    'README.md': b'Example package',
    'pyproject.toml': b'[project]\nname="example"',
    'qa-notes.md': b'Local operator notes excluded from both artifacts',
}
SCRIPTS = {'sleeptalk': 'sleeptalk.cli:main'}


def write_archive(path: Path, contents: dict[str, bytes]) -> None:
    if path.suffix == '.whl':
        with zipfile.ZipFile(path, 'w') as archive:
            for name, data in contents.items():
                archive.writestr(name, data)
    else:
        with tarfile.open(path, 'w:gz') as archive:
            for name, data in contents.items():
                entry = tarfile.TarInfo('example-1.0/' + name)
                entry.size = len(data)
                archive.addfile(entry, io.BytesIO(data))


@pytest.fixture(params=['.whl', '.tar.gz'])
def artifact(request, tmp_path):
    path = tmp_path / ('example-1.0' + request.param)
    if request.param == '.whl':
        contents = {name: data for name, data in SOURCES.items() if name.startswith('sleeptalk/')}
        contents.update({
            'example-1.0.dist-info/METADATA': b'Name: example',
            'example-1.0.dist-info/WHEEL': b'Wheel-Version: 1.0',
            'example-1.0.dist-info/RECORD': b'',
            'example-1.0.dist-info/entry_points.txt': b'[console_scripts]\nsleeptalk = sleeptalk.cli:main\n',
            'example-1.0.dist-info/licenses/LICENSE': b'MIT',
        })
    else:
        contents = {name: data for name, data in SOURCES.items() if name != 'qa-notes.md'}
        contents.update({'PKG-INFO': b'Name: example', '.gitignore': b'.env'})
    return path, contents


def test_expected_members_pass(artifact):
    path, contents = artifact
    write_archive(path, contents)
    assert audit(path, SOURCES, SCRIPTS)['members'] == len(contents)


@pytest.mark.parametrize('extra', ['sleeptalk/.env', 'calls/private.wav', 'other/app.py'])
def test_local_or_server_files_fail(artifact, extra):
    path, contents = artifact
    contents[extra] = b'sentinel, not a credential'
    write_archive(path, contents)
    with pytest.raises(ValueError, match='unexpected='):
        audit(path, SOURCES, SCRIPTS)


def test_missing_model_fails(artifact):
    path, contents = artifact
    del contents['sleeptalk/voice_loop/data/model.onnx']
    write_archive(path, contents)
    with pytest.raises(ValueError, match='missing=.*model.onnx'):
        audit(path, SOURCES, SCRIPTS)


def test_stale_source_bytes_fail(artifact):
    path, contents = artifact
    contents['sleeptalk/__init__.py'] = b'old source'
    write_archive(path, contents)
    with pytest.raises(ValueError, match='changed=.*__init__.py'):
        audit(path, SOURCES, SCRIPTS)


def test_even_tracked_operator_state_fails(artifact):
    path, contents = artifact
    write_archive(path, contents)
    with pytest.raises(ValueError, match='operator state'):
        audit(path, {**SOURCES, 'sleeptalk/.env': b'sentinel'}, SCRIPTS)


def test_source_archive_symlink_fails(tmp_path):
    path = tmp_path / 'bad.tar.gz'
    with tarfile.open(path, 'w:gz') as archive:
        entry = tarfile.TarInfo('example-1.0/link')
        entry.type = tarfile.SYMTYPE
        entry.linkname = '/tmp/operator-state'
        archive.addfile(entry)
    with pytest.raises(ValueError, match='link or special file'):
        audit(path, SOURCES, SCRIPTS)
