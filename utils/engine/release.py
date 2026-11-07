"""Verify a model's explicit shared-engine release without changing its state."""
import hashlib
import json
from importlib import metadata
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]


def verify_release(path, *, expected_sha256):
    """The model contract pins the manifest hash, which pins sources/environment."""
    if not isinstance(expected_sha256, str) or len(expected_sha256) != 64:
        raise ValueError('An explicit engine manifest SHA-256 is required')
    manifest_path = Path(path).resolve()
    raw = manifest_path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise ValueError('Engine release manifest differs from the model contract')
    manifest = json.loads(raw)
    if manifest.get('schema') != 'shared_engine_release_v1':
        raise ValueError('Unsupported engine release schema')
    if list(sys.version_info[:3]) != manifest['python']:
        raise RuntimeError('Engine Python version differs from its release')
    for package, version in manifest['packages'].items():
        if metadata.version(package) != version:
            raise RuntimeError(f'Engine dependency version differs: {package}')
    if not manifest.get('files'):
        raise ValueError('Engine release has no pinned sources')
    for name, expected in manifest['files'].items():
        source = (ROOT / name).resolve()
        if not source.is_relative_to(ROOT):
            raise ValueError('Engine release source escapes project root')
        if hashlib.sha256(source.read_bytes()).hexdigest() != expected:
            raise RuntimeError(f'Engine release source changed: {name}')
    return manifest
