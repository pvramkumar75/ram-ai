"""`ramai` - terminal command for RAM AI.

  ramai                         open the RAM AI window (starts RAM AI if needed)
  ramai "do something"          run a task, show its steps live, approve/answer right here
  ramai run "..." [--pre-approve]
  ramai status | stop | history [-n 10] | doctor | version | update
  ramai schedules [list | run ID | pause ID | resume ID | delete ID]
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx

from .core import VERSION, data_dir, dpapi_unprotect
from .textfmt import render

PORT = int(os.environ.get("RAM_AI_PORT", "8765"))
BASE = f"http://127.0.0.1:{PORT}/api"
ROOT = Path(__file__).resolve().parents[1]
REPO = "pvramkumar75/ram-ai"
COMMANDS = {"run", "status", "stop", "history", "doctor", "version", "update", "schedules", "open", "help"}

for s in (sys.stdout, sys.stderr):
    try:
        s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

def _enable_vt() -> bool:
    """Windows consoles (classic cmd included) show colours/bold only after virtual-terminal mode is switched on."""
    if os.name != "nt":
        return True
    try:
        import ctypes

        k = ctypes.windll.kernel32
        h = k.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if not k.GetConsoleMode(h, ctypes.byref(mode)):
            return False
        return bool(mode.value & 4 or k.SetConsoleMode(h, mode.value | 4))   # ENABLE_VIRTUAL_TERMINAL_PROCESSING
    except Exception:
        return False


C = {"dim": "\033[2m", "b": "\033[1m", "g": "\033[32m", "r": "\033[31m", "y": "\033[33m", "c": "\033[36m", "m": "\033[35m", "x": "\033[0m"}
if os.environ.get("NO_COLOR") or not sys.stdout.isatty() or not _enable_vt():
    C = {k: "" for k in C}


def say(text, col=""):
    print(f"{C.get(col, '')}{text}{C['x'] if col else ''}", flush=True)


# ------------------------------------------------------------------ connection
def _token() -> str | None:
    try:
        d = json.loads(dpapi_unprotect((data_dir() / "session.dat").read_bytes()).decode())
        return d["token"] if d.get("port") == PORT else None
    except Exception:
        return None


def _alive() -> bool:
    try:
        return httpx.get(f"http://127.0.0.1:{PORT}/", timeout=2).status_code == 200
    except Exception:
        return False


def connect(start: bool = True) -> dict:
    if not _alive():
        if not start:
            raise SystemExit("RAM AI is not running.")
        say("Starting RAM AI…", "dim")
        pyw = Path(sys.executable).with_name("pythonw.exe")
        subprocess.Popen([str(pyw if pyw.exists() else sys.executable), "-m", "ramai", "--no-ui"], cwd=str(ROOT),
                         creationflags=0x08000000 | 0x00000008)  # no window, detached
        for _ in range(60):
            time.sleep(0.5)
            if _alive() and _token():
                break
        else:
            raise SystemExit("RAM AI did not start. Try: ramai doctor")
    tok = _token()
    if not tok:
        raise SystemExit("Could not read the RAM AI session (is it running as another Windows user?).")
    return {"X-RAM-Token": tok}


def api(h, method, path, body=None, timeout=30):
    r = httpx.request(method, BASE + path, headers=h, json=body, timeout=timeout)
    if r.status_code >= 400:
        try:
            msg = r.json().get("detail")
        except Exception:
            msg = r.text[:200]
        raise SystemExit(f"Error: {msg}")
    return r.json()


# ------------------------------------------------------------------ helpers
def label(tool: str, a: dict) -> str:
    a = a or {}
    for k in ("name", "query", "contact", "path", "url", "command", "title", "text", "prompt", "expression"):
        if a.get(k):
            v = str(a[k]).replace("\n", " ")
            return f"{tool.replace('_', ' ')} · {v[:70]}{'…' if len(v) > 70 else ''}"
    if a.get("to"):
        return f"{tool.replace('_', ' ')} · to {', '.join(a['to'])}"
    return tool.replace("_", " ")


def _parse(d):
    try:
        return json.loads(d) if d else {}
    except Exception:
        return {}


def handle_approval(h, ap) -> None:
    say("\n" + ("❓ QUESTION" if ap["kind"] == "question" else "⚠  APPROVAL NEEDED") + f"  ({ap['tool']})", "y")
    say(ap["summary"])
    if ap["kind"] == "question":
        ans = input(f"{C['b']}Your answer (empty = skip): {C['x']}").strip()
        body = {"decision": "approve", "answer": ans} if ans else {"decision": "reject"}
    else:
        c = input(f"{C['b']}[a]pprove once / approve [all] for this task / [r]eject: {C['x']}").strip().lower()
        body = {"decision": "approve", "scope": "task"} if c in ("all", "t") else {"decision": "approve"} if c in ("a", "y", "yes") \
            else {"decision": "reject"}
    api(h, "POST", f"/approvals/{ap['id']}", body)


def follow(h, tid: str) -> int:
    after, seen_ap, thinking_shown = 0, set(), False
    while True:
        r = api(h, "GET", f"/tasks/{tid}/events?after={after}")
        for e in r["events"]:
            after = e["id"]
            d = _parse(e["data"])
            ev = e["event"]
            if ev == "thought" and d.get("text"):
                say(f"  ✦ {d['text'][:240]}{'…' if len(d['text']) > 240 else ''}", "m")
            elif ev == "tool_call":
                say(f"  ▸ {label(e['tool'], d.get('args'))}", "c")
            elif ev == "tool_result":
                say(f"    ✓ {d.get('ms', 0)} ms", "dim")
            elif ev in ("tool_denied", "tool_error", "tool_timeout"):
                say(f"    ✗ {(d.get('reason') or d.get('error') or 'failed')[:200]}", "r")
            elif ev == "auto_approved":
                say(f"    ✓ auto-approved ({d.get('by', '')})", "g")
        if r.get("thinking_since") and not thinking_shown:
            say("  … thinking", "dim")
            thinking_shown = True
        if not r.get("thinking_since"):
            thinking_shown = False
        for ap in api(h, "GET", "/approvals"):
            if ap.get("task_id") == tid and ap["id"] not in seen_ap:
                seen_ap.add(ap["id"])
                handle_approval(h, ap)
        if r["state"] in ("Completed", "Failed", "Cancelled"):
            print()
            if r["state"] == "Completed":
                say("◆ RAM AI", "b")
                print(render(r.get("result") or "", color=bool(C["b"])), flush=True)
                return 0
            say(f"{r['state']}: {r.get('error') or ''}", "r")
            return 1
        time.sleep(0.6)


# ------------------------------------------------------------------ commands
def cmd_run(args) -> int:
    h = connect()
    st = api(h, "GET", "/status")
    if not st.get("configured"):
        say("RAM AI is not set up yet. Opening the window for the first-time setup…", "y")
        return cmd_open(args)
    r = api(h, "POST", "/tasks", {"instruction": args.text, "pre_approve": bool(args.pre_approve)})
    say(f"Task {r['task_id']} on {r['device']} · AI: {st['llm']['label']} {st['llm']['model']}  (Ctrl+C stops it)", "dim")
    try:
        return follow(h, r["task_id"])
    except KeyboardInterrupt:
        api(h, "POST", f"/tasks/{r['task_id']}/cancel")
        say("\nStopped.", "r")
        return 130


def cmd_open(args) -> int:
    connect()
    from .__main__ import open_ui

    open_ui(f"http://127.0.0.1:{PORT}/#token={_token()}")
    return 0


def cmd_status(args) -> int:
    if not _alive():
        say("RAM AI is not running.  Start it with:  ramai open", "y")
        return 1
    h = connect(False)
    s = api(h, "GET", "/status")
    se = s["settings"]
    say(f"RAM AI {s['version']} · running · {s['device']} · user {se.get('user_name') or '-'}", "b")
    say(f"AI model: {s['llm']['label']} · {s['llm']['model']}   (set up: {'yes' if s['configured'] else 'NO - run: ramai open'})")
    say(f"Folders it may read: {len(se.get('roots', []))} · web: {'on' if se.get('web_access', True) else 'off'} · tools: {len(s['tools'])}")
    act = [t for t in api(h, "GET", "/tasks")[:10] if t["state"] not in ("Completed", "Failed", "Cancelled")]
    say(f"Running tasks: {len(act)}" + "".join(f"\n  - {t['id']} {t['state']}: {t['instruction'][:70]}" for t in act))
    return 0


def cmd_stop(args) -> int:
    h = connect(False)
    api(h, "POST", "/stop")
    say("Emergency stop sent - all tasks stopped.", "r")
    return 0


def cmd_history(args) -> int:
    h = connect(False)
    for t in api(h, "GET", "/tasks")[: args.n]:
        col = {"Completed": "g", "Failed": "r", "Cancelled": "y"}.get(t["state"], "c")
        say(f"{time.strftime('%d %b %H:%M', time.localtime(t['created']))}  {t['state']:<10}  {t['instruction'][:80]}", col)
    return 0


def cmd_schedules(args) -> int:
    h = connect()
    act, sid = (args.action or "list"), args.id
    if act == "list":
        rows = api(h, "GET", "/schedules")
        if not rows:
            say('No schedules. Create one in the window or: ramai "every weekday at 9:00 …"')
        for s in rows:
            say(f"{s['id']}  {'active' if s['enabled'] else 'paused':<7} {s['when']:<30} next: {s['next_run_text']:<24} {s['title']}",
                "g" if s["enabled"] else "dim")
        return 0
    if not sid:
        raise SystemExit("Give the schedule id, e.g.  ramai schedules run 1a2b3c4d")
    if act == "run":
        tid = api(h, "POST", f"/schedules/{sid}/run")["task_id"]
        return follow(h, tid)
    if act in ("pause", "resume"):
        api(h, "POST", f"/schedules/{sid}/enabled", {"enabled": act == "resume"})
    elif act == "delete":
        api(h, "DELETE", f"/schedules/{sid}")
    say("Done.")
    return 0


def cmd_doctor(args) -> int:
    ok = True

    def check(name, good, hint=""):
        nonlocal ok
        ok &= bool(good)
        say(f"  {'✓' if good else '✗'} {name}" + ("" if good else f"   → {hint}"), "g" if good else "r")

    say(f"RAM AI {VERSION} health check", "b")
    check("Python " + sys.version.split()[0], sys.version_info[:2] >= (3, 11), "install Python 3.12 and re-run the installer")
    check("RAM AI running", _alive(), "start it: ramai open")
    if _alive():
        s = api(connect(False), "GET", "/status")
        check("Set up (name + AI key)", s["configured"], "ramai open  → finish the setup wizard")
    ws = data_dir() / "workspace" / ".venv" / "Scripts" / "python.exe"
    check("Agent Python workspace", ws.exists(), "it is rebuilt automatically on next start; or re-run the installer")
    try:
        import win32com.client  # noqa: F401
        check("Windows automation (pywin32)", True)
    except Exception:
        check("Windows automation (pywin32)", False, "re-run the installer")
    try:
        import winrt.windows.media.ocr as o
        check("Windows text recognition (OCR)", o.OcrEngine.try_create_from_user_profile_languages() is not None, "add an OCR language in Windows settings")
    except Exception:
        check("Windows text recognition (OCR)", False, "re-run the installer")
    from .tools.apps import StartApps

    sa = StartApps(); sa.load()
    check("Outlook installed", sa.has("outlook"), "optional - needed for email tasks")
    check("WhatsApp desktop installed", sa.has("whatsapp"), "optional - install WhatsApp from the Microsoft Store")
    say("All good." if ok else "Some checks failed - see the hints above.", "g" if ok else "y")
    return 0 if ok else 1


def cmd_update(args) -> int:
    say("Downloading and installing the latest RAM AI release…", "c")
    url = f"https://raw.githubusercontent.com/{REPO}/main/get.ps1"
    ps = f"$env:RAMAI_INSTALL_DIR='{ROOT}'; irm '{url}' | iex"
    return subprocess.call(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps])


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        return cmd_open(None)
    if argv[0] in ("-h", "--help", "help"):
        print(__doc__)
        return 0
    if argv[0] in ("-v", "--version", "version"):
        print(f"RAM AI {VERSION}")
        return 0
    if argv[0] not in COMMANDS:  # ramai "do something"
        argv = ["run", *argv]
    p = argparse.ArgumentParser(prog="ramai", description="RAM AI desktop agent")
    sub = p.add_subparsers(dest="cmd")
    r = sub.add_parser("run"); r.add_argument("text", nargs="+"); r.add_argument("--pre-approve", action="store_true")
    for n in ("status", "stop", "doctor", "update", "open"):
        sub.add_parser(n)
    hi = sub.add_parser("history"); hi.add_argument("-n", type=int, default=10)
    sc = sub.add_parser("schedules"); sc.add_argument("action", nargs="?", choices=["list", "run", "pause", "resume", "delete"]); sc.add_argument("id", nargs="?")
    a = p.parse_args(argv)
    if a.cmd == "run":
        a.text = " ".join(a.text)
    return {"run": cmd_run, "status": cmd_status, "stop": cmd_stop, "history": cmd_history, "schedules": cmd_schedules,
            "doctor": cmd_doctor, "update": cmd_update, "open": cmd_open}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
