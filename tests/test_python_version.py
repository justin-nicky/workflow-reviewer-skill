import shutil
import subprocess
import sys

import pytest

from conftest import SCRIPT


def older_python():
    for name in ("/usr/bin/python3", "python3.9", "python3.10", "python3"):
        exe = shutil.which(name) or name
        try:
            out = subprocess.run([exe, "-c", "import sys; print(sys.version_info[:2] < (3, 11))"],
                                 capture_output=True, text=True).stdout.strip()
        except OSError:
            continue
        if out == "True":
            return exe
    return None


def test_old_python_gets_a_clear_message_and_its_own_exit_code(tmp_path):
    exe = older_python()
    if exe is None:
        pytest.skip("no Python older than 3.11 on this machine")
    proc = subprocess.run([exe, str(SCRIPT), "--format", "json"], capture_output=True, text=True,
                          cwd=str(tmp_path))
    assert proc.returncode == 5
    assert "needs Python 3.11+" in proc.stderr
    assert "ImportError" not in proc.stderr
