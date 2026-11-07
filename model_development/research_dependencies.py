"""Bind the canonical research child namespace without replacing legacy indicators."""
import importlib
import importlib.util
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]


def bind_indicator_namespace(namespace):
    if namespace!='indicator.research':
        raise ValueError(f'Unsupported shared research indicator namespace: {namespace}')
    path=ROOT/'indicator/research/__init__.py'
    existing=sys.modules.get(namespace)
    if existing is not None:
        if Path(existing.__file__).resolve()!=path.resolve():
            raise ImportError('Shared research indicators are shadowed by another checkout')
        return existing
    parent=importlib.import_module('indicator')
    spec=importlib.util.spec_from_file_location(namespace,path,submodule_search_locations=[str(path.parent)])
    module=importlib.util.module_from_spec(spec)
    sys.modules[namespace]=module
    spec.loader.exec_module(module)
    setattr(parent,'research',module)
    return module


def clock_source(core):
    """Resolve the declared clock layout; never substitute a different checkout."""
    core=Path(core).resolve()
    for relative in ('utils/closed_candle_context.py','trading_core/candles.py'):
        path=core/relative
        if path.is_file():return path
    raise FileNotFoundError(f'Missing closed-candle context in declared core: {core}')


def _selected_module(name,path,*,package=False):
    path=Path(path).resolve()
    existing=sys.modules.get(name)
    if existing is not None:
        if Path(existing.__file__).resolve()!=path:
            raise ImportError(f'Selected dependency shadowed by another checkout: {name}')
        return existing
    parent_name,_,child=name.rpartition('.')
    parent=importlib.import_module(parent_name) if parent_name else None
    spec=importlib.util.spec_from_file_location(name,path,
        submodule_search_locations=[str(path.parent)] if package else None)
    if spec is None or spec.loader is None:raise ImportError(f'Cannot load {path}')
    module=importlib.util.module_from_spec(spec)
    sys.modules[name]=module
    try:spec.loader.exec_module(module)
    except BaseException:
        del sys.modules[name]
        raise
    if parent is not None:setattr(parent,child,module)
    return module


def bind_clock(core):
    """Expose the explicitly selected clock through the maintained utility API."""
    core=Path(core).resolve();path=clock_source(core)
    name='utils.closed_candle_context'
    parent=_selected_module('utils',ROOT/'utils/__init__.py',package=True)
    existing=sys.modules.get(name)
    if existing is not None:
        if Path(existing.__file__).resolve()!=path:
            raise ImportError('Research clock shadowed by another checkout')
        return existing
    if path==core/'trading_core/candles.py':
        # Original module name preserves its relative contracts import. Only an
        # explicitly selected external core may supply this historical namespace.
        _selected_module('trading_core',core/'trading_core/__init__.py',package=True)
        _selected_module('indicator.cvd',core/'indicator/cvd.py')
        module=_selected_module('trading_core.candles',path)
        sys.modules[name]=module
        setattr(parent,'closed_candle_context',module)
        return module
    return _selected_module(name,path)
