"""Temporary process boundary preserves legacy imports, argv and working directory."""
from pathlib import Path
import os
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def launch(target, arguments, *, module=False):
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1')
    previous = env.get('PYTHONPATH', '')
    env['PYTHONPATH'] = str(ROOT) + (os.pathsep + previous if previous else '')
    command = [sys.executable, '-B']
    command += ['-m', target] if module else [str(ROOT / target)]
    return subprocess.call([*command, *arguments], env=env)
