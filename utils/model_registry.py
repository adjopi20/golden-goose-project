"""Verify portable model packages without opening accounts or starting services."""
import hashlib
import json
from pathlib import Path
from utils.engine.release import verify_release

ROOT = Path(__file__).resolve().parents[1]


def verify_model(source):
    source = Path(source).resolve()
    contract = json.loads(source.with_name('contract.json').read_text(encoding='utf-8'))
    if contract.get('schema') != 'operational_model_package_v1':
        raise ValueError('Unknown model package contract')
    if source.name != 'strategy.py' or sha256(source) != contract['strategy_sha256']:
        raise RuntimeError('Model strategy source changed')
    for name, expected in contract['dependencies'].items():
        path = (ROOT / name).resolve()
        if not path.is_relative_to(ROOT) or sha256(path) != expected:
            raise RuntimeError(f'Model dependency source changed: {name}')
    for name, expected in contract.get('model_files', {}).items():
        path = (source.parent / name).resolve()
        if not path.is_relative_to(source.parent) or sha256(path) != expected:
            raise RuntimeError(f'Model configuration changed: {name}')
    release = contract['engine_release']
    verify_release(ROOT / release['path'], expected_sha256=release['sha256'])
    return contract


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()
