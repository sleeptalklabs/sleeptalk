#!/usr/bin/env python3
"""Compare wheel and source-archive contents with tracked distribution inputs."""

from __future__ import annotations

import argparse
import configparser
import hashlib
from pathlib import Path, PurePosixPath
import subprocess
import stat
import tarfile
import tomllib
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def tracked_inputs(root: Path) -> dict[str, bytes]:
    paths = subprocess.check_output(
        ['git', 'ls-files', '-z', '--', 'sleeptalk', 'tests', 'README.md', 'LICENSE', 'pyproject.toml'],
        cwd=root,
    ).decode().split('\0')
    return {path: (root / path).read_bytes() for path in paths if path and (root / path).is_file()}


def members(path: Path) -> dict[str, bytes]:
    result = {}
    if path.suffix == '.whl':
        with zipfile.ZipFile(path) as archive:
            if any(stat.S_ISLNK(item.external_attr >> 16) for item in archive.infolist()):
                raise ValueError('wheel contains a symbolic link')
            entries = [(item.filename, archive.read(item)) for item in archive.infolist() if not item.is_dir()]
    else:
        with tarfile.open(path, 'r:gz') as archive:
            files = [item for item in archive.getmembers() if not item.isdir()]
            if any(not item.isfile() for item in files):
                raise ValueError('source archive contains a link or special file')
            entries = [(item.name, archive.extractfile(item).read()) for item in files]
    for name, data in entries:
        if PurePosixPath(name).is_absolute() or '..' in PurePosixPath(name).parts or '\\' in name:
            raise ValueError(f'unsafe archive path: {name}')
        if name in result:
            raise ValueError(f'duplicate archive member: {name}')
        result[name] = data
    return result


def audit(path: Path, sources: dict[str, bytes], scripts: dict[str, str]) -> dict:
    actual = members(path)
    package = {name: data for name, data in sources.items() if name.startswith('sleeptalk/')}
    forbidden = {'calls', 'logs', 'recordings', 'credentials', '__pycache__', '.git', '.venv', 'config'}
    for name in package:
        parts = PurePosixPath(name).parts
        if forbidden.intersection(parts) or any(part.startswith('.env') for part in parts) or name.endswith(('.pyc', '.log', '.key', '.pem')):
            raise ValueError(f'operator state or build debris in package inputs: {name}')
    if path.suffix == '.whl':
        roots = {name.split('/')[0] for name in actual if '.dist-info/' in name}
        if len(roots) != 1:
            raise ValueError('wheel must contain one metadata directory')
        metadata = roots.pop()
        generated = {f'{metadata}/{name}' for name in ('METADATA', 'WHEEL', 'entry_points.txt', 'RECORD')}
        expected = {**package, f'{metadata}/licenses/LICENSE': sources['LICENSE']}
        entrypoints = configparser.ConfigParser()
        entrypoints.read_string(actual.get(f'{metadata}/entry_points.txt', b'').decode())
        if dict(entrypoints['console_scripts']) != scripts:
            raise ValueError('wheel console scripts differ from pyproject.toml')
    else:
        roots = {name.split('/')[0] for name in actual}
        if len(roots) != 1:
            raise ValueError('source archive must contain one root directory')
        prefix = roots.pop() + '/'
        actual = {name.removeprefix(prefix): data for name, data in actual.items()}
        expected = {
            name: data for name, data in sources.items()
            if name.startswith(('sleeptalk/', 'tests/')) or name in ('README.md', 'LICENSE', 'pyproject.toml')
        }
        # Hatch includes repository ignore rules and generated package metadata.
        generated = {'PKG-INFO', '.gitignore'}
    missing = (expected.keys() | generated) - actual.keys()
    unexpected = actual.keys() - expected.keys() - generated
    changed = {name for name, data in expected.items() if name in actual and actual[name] != data}
    if missing or unexpected or changed:
        raise ValueError(f'missing={sorted(missing)}, unexpected={sorted(unexpected)}, changed={sorted(changed)}')
    return {
        'artifact': path.name,
        'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
        'members': len(actual),
        'source_files_verified': len(expected),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('artifacts', nargs='+', type=Path)
    args = parser.parse_args()
    sources = tracked_inputs(ROOT)
    project = tomllib.loads(sources['pyproject.toml'].decode())['project']
    for path in args.artifacts:
        result = audit(path, sources, project['scripts'])
        print(f"{result['artifact']}: {result['members']} members, {result['source_files_verified']} source files verified, sha256={result['sha256']}")


if __name__ == '__main__':
    main()
