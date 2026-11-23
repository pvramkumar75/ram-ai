"""Shortcut/autostart launches use pythonw, where sys.stdout and sys.stderr are None. Start-up must survive that."""
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_server_config_builds_without_console():
    home = tempfile.mkdtemp(prefix="ramai_pyw_")
    code = (
        "import sys; sys.stdout = None; sys.stderr = None\n"
        "import ramai.__main__ as m, uvicorn\n"
        "uvicorn.Config(lambda *a: None, host=m.HOST, port=m.PORT, log_level='warning')\n"
        "sys.__stdout__.write('OK')\n"
    )
    env = dict(os.environ, RAM_AI_HOME=home)
    out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert out.stdout.endswith("OK")
