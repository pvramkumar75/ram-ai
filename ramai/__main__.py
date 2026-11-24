"""Entry point: python -m ramai   (runs in the logged-in user's session, loopback only)."""
from __future__ import annotations

import os
import secrets
import shutil
import subprocess
import sys
import threading
import time
import webbrowser


def _console_fallback() -> None:
    """Started from a shortcut/autostart (pythonw) there is no console: sys.stdout/stderr are None and libraries that
    print or check isatty() crash at start. Send that output to %APPDATA%\\RAM_AI\\ramai.log instead (kept small)."""
    if sys.stdout is not None and sys.stderr is not None:
        return
    from .core import data_dir

    log = data_dir() / "ramai.log"
    try:
        if log.exists() and log.stat().st_size > 2_000_000:
            log.replace(log.with_suffix(".log.old"))
        f = open(log, "a", encoding="utf-8", buffering=1, errors="replace")
    except OSError:
        f = open(os.devnull, "w")
    sys.stdout = sys.stdout or f
    sys.stderr = sys.stderr or f


_console_fallback()

import uvicorn  # noqa: E402  (after the console fallback: uvicorn's logging needs a real stdout)

from .app import App, start_hotkey
from .server import create_api

HOST, PORT = "127.0.0.1", int(os.environ.get("RAM_AI_PORT", "8765"))


def open_ui(url: str) -> None:
    for exe in (shutil.which("msedge"), r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
                r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"):
        if exe and os.path.exists(exe):
            subprocess.Popen([exe, f"--app={url}", "--window-size=1280,860"])
            return
    webbrowser.open(url)


def _session_file():
    from .core import data_dir

    return data_dir() / "session.dat"


def _attach_to_running() -> bool:
    """If RAM AI already runs on this PC, open its window (token kept DPAPI-encrypted for this Windows user) instead of
    starting a second copy that would fail to bind the port."""
    import json
    import socket

    from .core import dpapi_unprotect

    s = socket.socket()
    s.settimeout(0.5)
    try:
        if s.connect_ex((HOST, PORT)) != 0:
            return False
    finally:
        s.close()
    f = _session_file()
    try:
        d = json.loads(dpapi_unprotect(f.read_bytes()).decode())
        if d.get("port") == PORT and d.get("token"):
            if "--no-ui" not in sys.argv:
                open_ui(f"http://{HOST}:{PORT}/#token={d['token']}")
            print("RAM AI is already running - opened its window.")
            return True
    except Exception:
        pass
    print(f"Port {PORT} is used by another program (or an old RAM AI). Close it, or set RAM_AI_PORT to another port.")
    return True


def main() -> None:
    if _attach_to_running():
        return
    app = App()
    token = os.environ.get("RAM_AI_TEST_TOKEN") or secrets.token_urlsafe(32)  # override is for automated tests only
    api = create_api(app, token, PORT)

    start_hotkey(app.orchestrator.emergency_stop, app.takeover)
    if app.settings.get("remote", {}).get("enabled"):
        try:
            app.remote_server.start()
        except Exception as e:
            print("Remote server not started:", e)
    if "--no-ui" not in sys.argv:
        def open_when_ready():
            import time

            for _ in range(300):
                if getattr(main, "_server", None) is not None and main._server.started:
                    open_ui(f"http://{HOST}:{PORT}/#token={token}")
                    return
                time.sleep(0.1)

        threading.Thread(target=open_when_ready, daemon=True).start()
    print(f"RAM AI listening on http://{HOST}:{PORT} (loopback only). Emergency stop: Ctrl+Alt+Q")
    server = uvicorn.Server(uvicorn.Config(api, host=HOST, port=PORT, log_level="warning"))

    def save_session_when_listening():
        # only record the token once THIS process really owns the port (a losing second copy must not overwrite it)
        import json
        import time

        from .core import dpapi_protect

        for _ in range(300):
            if server.started:
                try:
                    _session_file().write_bytes(dpapi_protect(json.dumps({"port": PORT, "token": token, "pid": os.getpid()}).encode()))
                except Exception:
                    pass
                return
            time.sleep(0.1)

    main._server = server
    threading.Thread(target=save_session_when_listening, daemon=True).start()
    server.run()


if __name__ == "__main__":
    main()
