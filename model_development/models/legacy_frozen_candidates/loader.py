"""Load a named local frozen candidate after checking its entire release identity."""
import hashlib
import importlib
from importlib import metadata
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[2]


def verify_release():
    manifest=json.loads(Path(__file__).with_name('release.json').read_text(encoding='utf-8'))
    if manifest['schema']!='local_frozen_models_v1':raise ValueError('Unknown frozen-model release schema')
    if list(sys.version_info[:3])!=manifest['python']:raise RuntimeError('Frozen candidate Python version differs; use the recorded project environment')
    for name,version in manifest['packages'].items():
        if metadata.version(name)!=version:raise RuntimeError(f'Frozen dependency version differs: {name}')
    for name,expected in manifest['files'].items():
        path=(ROOT/name).resolve()
        if not path.is_relative_to(ROOT.resolve()):raise ValueError('Frozen release path escapes project')
        if hashlib.sha256(path.read_bytes()).hexdigest()!=expected:raise RuntimeError(f'Frozen release source changed: {name}')
    return manifest


def load_model(name):
    manifest=verify_release()
    if name not in manifest['models']:raise ValueError(f'Unknown frozen model: {name}')
    model=importlib.import_module(f'{__name__}.{name}')
    expected=ROOT/'live_trading/live_model'/f'{name}.py'
    if Path(model.__file__).resolve()!=expected.resolve():raise ImportError('Another checkout supplied the frozen model')
    for module in tuple(sys.modules.values()):
        module_name=getattr(module,'__name__','')
        if module_name.startswith(('live_trading.execution.frozen_v1.','live_trading.state.frozen_v1.','live_trading.collectors.frozen_v1.')):
            path=getattr(module,'__file__',None)
            expected=ROOT/Path(*module_name.split('.')).with_suffix('.py')
            if path is None or Path(path).resolve()!=expected.resolve():raise ImportError('Another checkout supplied frozen shared infrastructure')
    return model
