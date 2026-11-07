"""Load declared live model packages; imports never activate services."""
import importlib
import json
from pathlib import Path
from utils.model_registry import verify_model, sha256

ROOT = Path(__file__).resolve().parents[2]


def verify_release():
    manifest = json.loads(Path(__file__).with_name('release.json').read_text(encoding='utf-8'))
    if manifest.get('schema') != 'live_model_packages_v1':
        raise ValueError('Unknown live model registry')
    for name, expected in manifest['files'].items():
        path = (ROOT / name).resolve()
        if not path.is_relative_to(ROOT) or sha256(path) != expected:
            raise RuntimeError(f'Live package source changed: {name}')
    for path in set(manifest['models'].values()):
        verify_model(ROOT / path / 'strategy.py')
    return manifest


def load_model(name):
    manifest = verify_release()
    if name not in manifest['models']:
        raise ValueError(f'Not a declared live submodel: {name}')
    path = manifest['models'][name]
    module = importlib.import_module(path.replace('/', '.') + '.strategy')
    if Path(module.__file__).resolve() != (ROOT / path / 'strategy.py').resolve():
        raise ImportError('Another checkout supplied this live model')
    return module
