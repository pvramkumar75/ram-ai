"""Terminal + Python execution + installs, all behind exact-action approval and a hard safety floor.

- run_command: PowerShell / cmd, non-interactive, time-limited, output captured.
- run_python: scripts run in RAM AI's own isolated workspace venv (cannot break RAM AI itself), working folder <output>/_scratch.
- install_package: pip (into the workspace venv) or winget (Windows apps; the user approves the publisher licence on the card).
A short list of system-destroying / security-disabling / credential-stealing commands is refused even with authorisation."""
from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Literal

from pydantic import Field

from ..core import data_dir, redact
from ..policy import PolicyError, Risk
from .base import Args, Registry, Tool

MAX_OUT = 20000
NO_WINDOW = 0x08000000

# Refused even with "authorization granted" (destroys Windows/data, disables protection, steals secrets, runs untrusted downloads)
HARD_BLOCK = [
    (r"\bformat(\.com)?\s+[a-z]:", "formatting a drive"),
    (r"\bdiskpart\b", "disk partitioning"),
    (r"\bbcdedit\b", "changing the Windows boot configuration"),
    (r"\b(vssadmin|wbadmin)\b.*\bdelete\b", "deleting system backups / shadow copies"),
    (r"\bcipher\s+/w", "wiping free disk space"),
    (r"(Set|Add)-MpPreference|\bDisableRealtimeMonitoring\b|Uninstall-WindowsDefender", "turning off or weakening Windows Defender"),
    (r"netsh\s+advfirewall\s+set\b.*\boff\b|Set-NetFirewallProfile\b.*-Enabled\s+(False|0)", "switching off the firewall"),
    (r"\breg(\.exe)?\s+(add|delete|import)\s+HKLM", "changing machine-wide registry settings"),
    (r"Set-ItemProperty\s+[^|;]*HKLM:", "changing machine-wide registry settings"),
    (r"\b(mimikatz|procdump|lsass)\b|reg(\.exe)?\s+save\s+hklm\\(sam|security|system)", "dumping credentials"),
    (r"(iwr|Invoke-WebRequest|curl|wget|Invoke-RestMethod|irm)\b[^|;\n]*\|\s*(iex|Invoke-Expression)\b", "downloading and running code in one step"),
    (r"\b(Stop-Computer|Restart-Computer|shutdown(\.exe)?\s+/[srp])\b", "shutting down / restarting the PC"),
    (r"\b(takeown|icacls)\b[^|;\n]*\\Windows\b", "taking over Windows system files"),
    (r"Remove-Item\b[^|;\n]*(C:\\(Windows|Program Files|ProgramData)|[A-Z]:\\\s*$|[A-Z]:\\\*)", "deleting system folders or a whole drive"),
    (r"\brd\s+/s[^|;\n]*[A-Z]:\\(Windows|Program Files)?\s*$", "deleting system folders or a whole drive"),
    (r"\b(del|erase)\s+/[sfq].*[A-Z]:\\\*", "deleting a whole drive"),
    (r"RAM_AI\\(secrets\.dat|session\.dat|ramai\.db)|secrets\.dat", "reading RAM AI's own key store"),
    (r"\bSet-ExecutionPolicy\b[^|;\n]*-Scope\s+LocalMachine", "changing machine-wide script policy"),
    (r"\b(net\s+user|net\s+localgroup\s+administrators)\b.*\/add", "creating accounts / granting admin rights"),
]


def check_command(text: str) -> None:
    for pat, why in HARD_BLOCK:
        if re.search(pat, text, re.I | re.M):
            raise PolicyError(f"Refused by RAM AI's safety floor: this looks like {why}. That is never done automatically, even with "
                              "authorisation. If you really need it, run it yourself in an admin terminal.")


def workspace() -> Path:
    return data_dir() / "workspace"


def ws_python() -> Path:
    return workspace() / ".venv" / "Scripts" / "python.exe"


def scratch(app) -> Path:
    p = Path(app.settings.get("output_dir")) / "_scratch"
    (p / "scripts").mkdir(parents=True, exist_ok=True)
    return p


def _run(args: list[str], cwd: Path, timeout: int, ctx, env=None) -> dict:
    t0 = time.time()
    p = subprocess.Popen(args, cwd=str(cwd), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         creationflags=NO_WINDOW, env=env)
    out = []
    try:
        while True:
            if ctx.stopped():
                p.kill()
                raise PolicyError("Stopped by user")
            if time.time() - t0 > timeout:
                p.kill()
                return {"exit_code": None, "timed_out": True, "seconds": round(time.time() - t0, 1),
                        "output": redact(b"".join(out).decode("utf-8", "replace"))[-MAX_OUT:]}
            try:
                o, _ = p.communicate(timeout=0.5)
                out.append(o or b"")
                break
            except subprocess.TimeoutExpired:
                continue
    finally:
        if p.poll() is None:
            p.kill()
    text = redact(b"".join(out).decode("utf-8", "replace"))
    return {"exit_code": p.returncode, "timed_out": False, "seconds": round(time.time() - t0, 1),
            "output": (text[:MAX_OUT // 2] + "\n…[middle truncated]…\n" + text[-MAX_OUT // 2:]) if len(text) > MAX_OUT else text}


def _files_in(d: Path) -> dict:
    try:
        return {str(p): p.stat().st_mtime for p in d.rglob("*") if p.is_file() and "scripts" not in p.parts}
    except Exception:
        return {}


# ------------------------------------------------------------------ args
class CmdArgs(Args):
    shell: Literal["powershell", "cmd"] = "powershell"
    command: str = Field(min_length=1, max_length=8000)
    cwd: str | None = Field(None, description="Working folder (default: the output _scratch folder)")
    timeout_seconds: int = Field(120, ge=5, le=1800)


class PyArgs(Args):
    code: str = Field(min_length=1, max_length=60000, description="Complete Python 3 script. Print results; save files to OUTPUT_DIR (env var).")
    packages: list[str] = Field(default_factory=list, max_length=15, description="Extra pip packages to install first (pandas, openpyxl, xlsxwriter, python-docx, python-pptx, matplotlib are preinstalled)")
    timeout_seconds: int = Field(300, ge=5, le=1800)


class InstallArgs(Args):
    kind: Literal["python", "winget"]
    name: str = Field(min_length=2, max_length=120, pattern=r"^[A-Za-z0-9._\-+=<>\[\], ]+$",
                      description="pip package spec (e.g. 'plotly' or 'pandas>=2') or winget package id (e.g. 'Microsoft.PowerBI')")


# ------------------------------------------------------------------ previews (exact action on the approval card)
def _cmd_preview(a: CmdArgs, ctx):
    async def _():
        return {"text": f"RUN {a.shell.upper()} COMMAND on {ctx.device}\nFolder: {a.cwd or scratch(ctx.app)}\nTime limit: {a.timeout_seconds}s\n"
                        f"--- command ---\n{a.command}", "digest": None}
    return _()


def _py_preview(a: PyArgs, ctx):
    async def _():
        extra = f"Installs first: {', '.join(a.packages)}\n" if a.packages else ""
        return {"text": f"RUN PYTHON SCRIPT (isolated workspace) on {ctx.device}\nFolder: {scratch(ctx.app)}\n{extra}--- code ---\n{a.code[:6000]}",
                "digest": None}
    return _()


def _install_preview(a: InstallArgs, ctx):
    async def _():
        if a.kind == "python":
            t = f"INSTALL PYTHON PACKAGE into RAM AI's workspace: {a.name}\n(from pypi.org; does not change your system Python)"
        else:
            t = (f"INSTALL WINDOWS APP with winget: {a.name}\nThis downloads from the winget catalogue and ACCEPTS the publisher's licence "
                 "agreement. Windows may ask you to confirm (admin prompt) - RAM AI never bypasses that.")
        return {"text": t, "digest": None}
    return _()


def _check_cmd(a: CmdArgs, ctx):
    check_command(a.command)
    if a.cwd:
        ctx.app.policy.resolve(a.cwd, must_exist=True)


def _check_py(a: PyArgs, ctx):
    check_command(a.code)
    if not ws_python().exists():
        raise PolicyError("The Python workspace is still being set up. Try again in a minute.")


# ------------------------------------------------------------------ tools
def t_cmd(a: CmdArgs, ctx):
    cwd = ctx.app.policy.resolve(a.cwd, must_exist=True) if a.cwd else scratch(ctx.app)
    if a.shell == "powershell":
        args = ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command",
                "[Console]::OutputEncoding=[Text.Encoding]::UTF8; " + a.command]
    else:
        args = ["cmd.exe", "/d", "/s", "/c", "chcp 65001>nul & " + a.command]
    before = _files_in(scratch(ctx.app))
    res = _run(args, cwd, a.timeout_seconds, ctx)
    after = _files_in(scratch(ctx.app))
    res["new_or_changed_files"] = [f for f, m in after.items() if before.get(f) != m][:50]
    return res


def _pip(spec: list[str], ctx) -> dict:
    return _run([str(ws_python()), "-m", "pip", "install", "--disable-pip-version-check", "-q", *spec], workspace(), 900, ctx)


def t_py(a: PyArgs, ctx):
    sc = scratch(ctx.app)
    if a.packages:
        r = _pip(a.packages, ctx)
        if r["exit_code"] != 0:
            raise PolicyError(f"Installing {a.packages} failed: {r['output'][-800:]}")
    f = sc / "scripts" / f"script_{time.strftime('%Y%m%d_%H%M%S')}.py"
    f.write_text(a.code, encoding="utf-8")
    env = {**os.environ, "OUTPUT_DIR": str(Path(ctx.app.settings.get("output_dir"))), "SCRATCH_DIR": str(sc), "PYTHONIOENCODING": "utf-8"}
    env.pop("RAM_AI_HOME", None)
    before = _files_in(Path(ctx.app.settings.get("output_dir")))
    res = _run([str(ws_python()), "-X", "utf8", str(f)], sc, a.timeout_seconds, ctx, env=env)
    after = _files_in(Path(ctx.app.settings.get("output_dir")))
    res["script"] = str(f)
    res["new_or_changed_files"] = [p for p, m in after.items() if before.get(p) != m][:50]
    return res


def t_install(a: InstallArgs, ctx):
    if a.kind == "python":
        r = _pip(a.name.split(), ctx)
    else:
        r = _run(["winget", "install", "--id", a.name, "-e", "--accept-source-agreements", "--accept-package-agreements",
                  "--disable-interactivity"], Path.home(), 1800, ctx)
    r["installed"] = a.name if r.get("exit_code") == 0 else None
    return r


def register(reg: Registry) -> None:
    reg.register(Tool("run_command", "Run a PowerShell or cmd command (non-interactive). Use for system info, file batch jobs, git, office automation, "
                      "anything a terminal can do. Shown to the user for approval.", CmdArgs, t_cmd, Risk.HIGH, 1900,
                      preview=_cmd_preview, check=_check_cmd, reversible=False))
    reg.register(Tool("run_python", "Run a Python script in RAM AI's isolated workspace (pandas, openpyxl, xlsxwriter, python-docx, python-pptx, "
                      "matplotlib preinstalled). Best for advanced Excel/Word/PowerPoint/charts/data analysis. Save outputs to os.environ['OUTPUT_DIR'].",
                      PyArgs, t_py, Risk.HIGH, 1900, preview=_py_preview, check=_check_py, reversible=False))
    reg.register(Tool("install_package", "Install a Python package into the workspace (kind='python') or a Windows app via winget (kind='winget').",
                      InstallArgs, t_install, Risk.HIGH, 1900, preview=_install_preview, reversible=False))


def ensure_workspace_background() -> None:
    """Create the agent's Python workspace if missing (e.g. installer skipped it); runs in a background thread."""
    import threading

    if ws_python().exists():
        return

    def build():
        try:
            base = getattr(sys, "_base_executable", sys.executable)
            workspace().mkdir(parents=True, exist_ok=True)
            subprocess.run([base, "-m", "venv", str(workspace() / ".venv")], creationflags=NO_WINDOW, check=True)
            req = Path(__file__).resolve().parents[2] / "requirements-workspace.txt"
            pkgs = ["-r", str(req)] if req.exists() else ["pandas", "openpyxl", "xlsxwriter", "python-docx", "python-pptx", "matplotlib"]
            subprocess.run([str(ws_python()), "-m", "pip", "install", "-q", "--disable-pip-version-check", *pkgs], creationflags=NO_WINDOW)
        except Exception:
            pass

    threading.Thread(target=build, daemon=True).start()
