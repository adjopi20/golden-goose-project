"""Exercise the real capture shell script with Docker replaced by a small fake."""
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest


BASH = (str(Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "Git/bin/bash.exe")
        if os.name == "nt" else shutil.which("bash"))
pytestmark = pytest.mark.skipif(not BASH or not Path(BASH).exists(), reason="Bash required")
SCRIPT = Path(__file__).resolve().parents[1] / "infra/ops/capture_phase2.sh"


def capture(tmp_path, mode, health="present"):
    script = tmp_path / "capture.sh"
    script.write_text(SCRIPT.read_text(), encoding="utf-8", newline="\n")
    wrapper = tmp_path / "fake.sh"
    wrapper.write_text(r'''
set -euo pipefail
git() {
  if [[ "$*" == "rev-parse --show-toplevel" ]]; then pwd; else echo test-commit; fi
}
docker() {
  printf '%s\n' "$*" >> "$CALLS"
  if [[ "$1" == compose ]]; then
    shift 3
    case "$1" in
      ps) echo fake-container ;;
      exec) echo '{"status":"ok"}' ;;
      logs) echo logs ;;
      *) echo "Unexpected compose command: $*" >&2; return 3 ;;
    esac
  elif [[ "$1" == inspect ]]; then
    if [[ "$3" == '{{.Image}}' ]]; then echo sha256:old;
    else
      # Docker's strict template lookup errors on .State.Health if absent.
      if [[ "$HEALTH" == absent && "$3" == *'.State.Health'* ]]; then
        echo 'template parsing error: map has no entry for key "Health"' >&2
        return 1
      fi
      if [[ "$HEALTH" == absent ]]; then health=null; else health='"healthy"'; fi
      printf '{"container_id":"fake-container","image_id":"sha256:old","health":%s}\n' "$health"
    fi
  elif [[ "$1 $2" == 'image inspect' ]]; then
    case "$MODE" in
      available) echo '["repo@sha256:old"]' ;;
      missing) echo 'Error response from daemon: No such image: sha256:old' >&2; return 1 ;;
      broken) echo 'Cannot connect to Docker daemon' >&2; return 1 ;;
    esac
  elif [[ "$1" == cp ]]; then
    touch "$3"
  elif [[ "$1" == stats ]]; then
    echo resources
  else
    echo "Unexpected Docker command: $*" >&2; return 3
  fi
}
source "$SCRIPT" "$OUTPUT"
''', encoding="utf-8")
    env = dict(os.environ, MODE=mode, HEALTH=health, SCRIPT="./capture.sh", OUTPUT="./out", CALLS="./calls")
    result = subprocess.run([BASH, wrapper.as_posix()], env=env, cwd=tmp_path,
                            capture_output=True, text=True, timeout=30)
    assert (tmp_path / "out").exists(), result.stderr
    # Archive and directory names share a prefix; pick the capture directory.
    out = next(p for p in (tmp_path / "out").iterdir() if p.is_dir())
    calls = (tmp_path / "calls").read_text()
    assert not set(calls.split()) & {"warmup", "restart", "up", "down", "build"}
    return result, out


@pytest.mark.parametrize("mode", ["available", "missing"])
@pytest.mark.parametrize("health", ["present", "absent"])
def test_capture_continues_only_for_missing_image_metadata(tmp_path, mode, health):
    result, out = capture(tmp_path, mode, health)
    assert result.returncode == 0, result.stderr
    assert (out / "CAPTURE_COMPLETE.txt").exists()
    assert Path(str(out) + ".tar.gz").exists()
    assert (out / "binance.sqlite").exists()
    assert (out / "lighter.sqlite").exists()
    assert (out / "shadow-status.json").exists()
    recorded = json.loads((out / "containers.jsonl").read_text())
    assert recorded["image_id"] == "sha256:old"
    assert recorded["health"] == ("healthy" if health == "present" else None)
    metadata = json.loads((out / "images/fake-container.json").read_text())
    if mode == "missing":
        assert metadata["status"] == "unavailable"
        assert metadata["image_id"] == "sha256:old"
        assert "WARNING" in (out / "warnings.txt").read_text()
    else:
        assert metadata == ["repo@sha256:old"]
        assert not (out / "warnings.txt").exists()


def test_capture_does_not_hide_daemon_errors(tmp_path):
    result, out = capture(tmp_path, "broken")
    assert result.returncode != 0
    assert "Cannot connect" in result.stderr
    assert not (out / "CAPTURE_COMPLETE.txt").exists()
    assert not Path(str(out) + ".tar.gz").exists()
