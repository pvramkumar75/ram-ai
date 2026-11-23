"""Policy engine: file-root permissions, blocked locations, risk classes."""
from __future__ import annotations

import os
import re
import contextvars
from enum import Enum
from pathlib import Path

from .core import Settings, data_dir


class PolicyError(Exception):
    pass


class Risk(str, Enum):
    READ = "read"
    LOW = "low"
    HIGH = "high"


BLOCKED_DIR_NAMES = {
    "windows", "program files", "program files (x86)", "programdata", "$recycle.bin",
    "system volume information", ".ssh", ".aws", ".gnupg", ".azure", "credentials",
}
BLOCKED_SUBSTR = [
    r"\appdata\local\google\chrome", r"\appdata\local\microsoft\edge", r"\appdata\local\microsoft\credentials",
    r"\appdata\roaming\mozilla", r"\appdata\roaming\microsoft\credentials", r"\appdata\roaming\microsoft\protect",
    r"\appdata\local\bravesoftware", r"\appdata\roaming\opera software",
]
BLOCKED_FILE = re.compile(
    r"(\.kdbx?$|\.pem$|\.pfx$|\.p12$|\.ppk$|^id_rsa|^id_ed25519|^login data|^cookies$|^web data$|"
    r"^key[34]\.db$|^logins\.json$|^\.env(\..*)?$|^secrets\.dat$|^credentials\.json$)",
    re.I,
)
TEXT_EXT = {".txt", ".md", ".csv", ".log", ".json", ".xml", ".yaml", ".yml", ".ini", ".tsv"}
DOC_EXT = {".pdf", ".docx", ".xlsx", ".xlsm", ".pptx"} | TEXT_EXT


def _norm(p: str | Path) -> str:
    return os.path.normcase(os.path.abspath(str(p)))


def _inside(child: str, parent: str) -> bool:
    try:
        return os.path.commonpath([child, parent]) == parent
    except ValueError:  # different drives
        return False


# Set by the executor while a tool runs inside a task the USER explicitly authorised in their own words.
ELEVATED: contextvars.ContextVar[bool] = contextvars.ContextVar("ramai_elevated", default=False)
SYSTEM_DIRS = {"windows", "program files", "program files (x86)", "programdata"}


def hard_block(path: str | Path, write: bool) -> str | None:
    """The only limits that stay even after the user grants authorisation: password/cookie/key stores and RAM AI's
    own secret store are never read (protects you if a document tries to trick the agent), and Windows system
    folders are never written."""
    p = Path(path)
    low = _norm(p)
    if _inside(low, _norm(data_dir())):
        return "RAM AI's own secret store is protected"
    for s in BLOCKED_SUBSTR:
        if s in low:
            return "browser password/cookie store is protected"
    parts = [x.lower() for x in p.parts]
    if any(x in (".ssh", ".aws", ".gnupg", ".azure") for x in parts[1:]):
        return "key/credential folder is protected"
    if p.name and BLOCKED_FILE.search(p.name):
        return "credential/secret file types are protected"
    if write and any(x in SYSTEM_DIRS for x in parts[1:2]):
        return "Windows system folders are never modified"
    return None


def is_blocked(path: str | Path) -> str | None:
    """Return a reason string when the path is in a protected location, else None."""
    p = Path(path)
    low = _norm(p)
    own = _norm(data_dir())
    if _inside(low, own):
        return "RAM AI's own data folder is protected"
    parts = [x.lower() for x in p.parts]
    for part in parts[1:]:
        if part in BLOCKED_DIR_NAMES:
            return f"'{part}' is a protected system/credential location"
    for s in BLOCKED_SUBSTR:
        if s in low:
            return "browser/credential store is protected"
    if p.name and BLOCKED_FILE.search(p.name):
        return "credential/secret file types are protected"
    return None


class Policy:
    def __init__(self, settings: Settings):
        self.settings = settings

    def roots(self) -> list[dict]:
        roots = [dict(r) for r in self.settings.get("roots", [])]
        out = self.settings.get("output_dir")
        if out:
            roots.append({"path": out, "write": True})
        return roots

    def add_root(self, path: str, write: bool = False) -> dict:
        p = Path(os.path.expandvars(path)).expanduser()
        if not p.exists() or not p.is_dir():
            raise PolicyError("Folder does not exist")
        real = str(p.resolve())
        reason = is_blocked(real)
        if reason:
            raise PolicyError(f"Cannot authorise this folder: {reason}")
        if real.startswith("\\\\"):
            # network shares must be explicitly authorised: allowed only through this explicit call
            pass
        roots = self.settings.get("roots", [])
        roots = [r for r in roots if _norm(r["path"]) != _norm(real)]
        entry = {"path": real, "write": bool(write)}
        roots.append(entry)
        self.settings.set("roots", roots)
        return entry

    def remove_root(self, path: str) -> None:
        roots = [r for r in self.settings.get("roots", []) if _norm(r["path"]) != _norm(path)]
        self.settings.set("roots", roots)

    def resolve(self, path: str, write: bool = False, must_exist: bool = False) -> Path:
        """Resolve to a real path that is inside an authorised root. Defeats ../ and symlink escapes."""
        if not path or "\x00" in path:
            raise PolicyError("Invalid path")
        raw = Path(os.path.expandvars(path)).expanduser()
        if not raw.is_absolute():
            raise PolicyError("Absolute path required")
        real = raw.resolve(strict=False)
        if ELEVATED.get():  # user granted authorisation for this task: any folder, only the hard floor applies
            reason = hard_block(real, write)
            if reason:
                raise PolicyError(f"Access denied even with authorisation: {reason}")
            if must_exist and not real.exists():
                raise PolicyError("File not found")
            return real
        n = _norm(real)
        matched = None
        for r in self.roots():
            try:
                rn = _norm(Path(r["path"]).resolve(strict=False))
            except OSError:
                continue
            if _inside(n, rn):
                if write and not r.get("write"):
                    continue
                matched = r
                break
        if matched is None:
            if write and any(_inside(n, _norm(Path(r["path"]).resolve())) for r in self.roots()):
                raise PolicyError("This folder is authorised read-only; write permission is required")
            raise PolicyError("Path is outside the folders you have authorised")
        reason = is_blocked(real)
        if reason:
            raise PolicyError(f"Access denied: {reason}")
        if must_exist and not real.exists():
            raise PolicyError("File not found")
        return real
