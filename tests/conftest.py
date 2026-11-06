import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "skills" / "workflow-reviewer" / "scripts" / "transcript_metrics.py"
sys.path.insert(0, str(Path(__file__).resolve().parent))


class Cli:
    """Black-box driver for transcript_metrics.py in an isolated HOME."""

    def __init__(self, tmp_path):
        self.home = tmp_path / "home"
        self.home.mkdir()
        self.repo = tmp_path / "repo"
        self.repo.mkdir()
        self.env_extra = {}

    def env(self):
        env = {k: v for k, v in os.environ.items()
               if k not in ("CLAUDE_CONFIG_DIR", "CODEX_HOME", "CODEX_THREAD_ID")}
        env.update(HOME=str(self.home), USERPROFILE=str(self.home),
                   PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8")
        env.update(self.env_extra)
        return env

    def run(self, *args, stdin=None, fmt="json", cwd=None):
        cmd = [sys.executable, str(SCRIPT), *map(str, args)]
        if fmt and "--format" not in args:
            cmd += ["--format", fmt]
        return subprocess.run(cmd, input=stdin, capture_output=True, text=True,
                              env=self.env(), cwd=str(cwd or self.repo))

    def json(self, *args, **kw):
        proc = self.run(*args, **kw)
        try:
            return proc.returncode, json.loads(proc.stdout)
        except ValueError:
            raise AssertionError(f"not JSON (exit {proc.returncode}):\n{proc.stdout}\n{proc.stderr}")


@pytest.fixture
def cli(tmp_path):
    return Cli(tmp_path)
