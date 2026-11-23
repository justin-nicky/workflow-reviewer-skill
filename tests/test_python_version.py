import subprocess
import sys

from conftest import SCRIPT

FAKE_OLD_PYTHON = (
    "import runpy, sys; sys.version_info = (3, 8, 10, 'final', 0); "
    "runpy.run_path(sys.argv[1], run_name='__main__')"
)


def test_python_older_than_3_9_gets_a_clear_message_and_its_own_exit_code(tmp_path):
    proc = subprocess.run([sys.executable, "-c", FAKE_OLD_PYTHON, str(SCRIPT), "--format", "json"], capture_output=True, text=True,
                          cwd=str(tmp_path))
    assert proc.returncode == 5
    assert "needs Python 3.9+" in proc.stderr and "found 3.8" in proc.stderr
    assert "ImportError" not in proc.stderr and "Traceback" not in proc.stderr
