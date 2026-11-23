"""Core services: data dir, settings, DPAPI secret store, redaction, SQLite database."""
from __future__ import annotations

import base64
import copy
import ctypes
import json
import os
import re
import sqlite3
import threading
import time
import uuid
from ctypes import wintypes
from pathlib import Path
from typing import Any

APP_NAME = "RAM AI"
VERSION = "0.2.5"


def data_dir() -> Path:
    base = os.environ.get("RAM_AI_HOME") or str(Path(os.environ.get("APPDATA", str(Path.home()))) / "RAM_AI")
    p = Path(base)
    p.mkdir(parents=True, exist_ok=True)
    return p


# --------------------------------------------------------------------------- redaction
_known_secrets: set[str] = set()
_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-~+/=]{8,}"),
    re.compile(r"eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]*"),
    re.compile(
        r"(?i)(\"?(?:api[_-]?key|password|passwd|token|secret|authorization|access_token|refresh_token)\"?\s*[:=]\s*)\"?[^\s\",}]{4,}\"?"
    ),
]


def register_secret_value(v: str | None) -> None:
    if v and len(v) >= 6:
        _known_secrets.add(v)


def redact(text: Any) -> str:
    s = text if isinstance(text, str) else json.dumps(text, default=str, ensure_ascii=False)
    for v in _known_secrets:
        s = s.replace(v, "[REDACTED]")
    s = _PATTERNS[0].sub("[REDACTED]", s)
    s = _PATTERNS[1].sub("Bearer [REDACTED]", s)
    s = _PATTERNS[2].sub("[REDACTED_JWT]", s)
    s = _PATTERNS[3].sub(lambda m: m.group(1) + "[REDACTED]", s)
    return s


# --------------------------------------------------------------------------- DPAPI
class _BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.c_void_p)]


def _mk_blob(data: bytes):
    buf = ctypes.create_string_buffer(data, len(data))
    return _BLOB(len(data), ctypes.cast(buf, ctypes.c_void_p)), buf


def _dpapi(data: bytes, protect: bool) -> bytes:
    inb, keep = _mk_blob(data)
    out = _BLOB()
    fn = ctypes.windll.crypt32.CryptProtectData if protect else ctypes.windll.crypt32.CryptUnprotectData
    ok = fn(ctypes.byref(inb), None, None, None, None, 0, ctypes.byref(out))
    if not ok:
        raise ctypes.WinError()
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        lf = ctypes.windll.kernel32.LocalFree
        lf.argtypes, lf.restype = [ctypes.c_void_p], ctypes.c_void_p
        lf(out.pbData)
        del keep


def dpapi_protect(data: bytes) -> bytes:
    return _dpapi(data, True)


def dpapi_unprotect(data: bytes) -> bytes:
    return _dpapi(data, False)


class SecretStore:
    """Per-Windows-user DPAPI-encrypted secrets. Values never leave this class except via get()."""

    def __init__(self, path: Path | None = None):
        self.path = path or (data_dir() / "secrets.dat")
        self._lock = threading.RLock()

    def _load(self) -> dict[str, str]:
        if not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text("utf-8"))
        except Exception:
            return {}

    def set(self, name: str, value: str) -> None:
        with self._lock:
            d = self._load()
            d[name] = base64.b64encode(dpapi_protect(value.encode("utf-8"))).decode()
            self.path.write_text(json.dumps(d), "utf-8")
            register_secret_value(value)

    def get(self, name: str) -> str | None:
        with self._lock:
            raw = self._load().get(name)
        if raw is None:
            return None
        try:
            v = dpapi_unprotect(base64.b64decode(raw)).decode("utf-8")
        except Exception:
            return None
        register_secret_value(v)
        return v

    def has(self, name: str) -> bool:
        return name in self._load()

    def delete(self, name: str) -> None:
        with self._lock:
            d = self._load()
            d.pop(name, None)
            self.path.write_text(json.dumps(d), "utf-8")


# --------------------------------------------------------------------------- settings
DEFAULT_EXCLUDED_APPS = ["password", "keepass", "bitwarden", "1password", "lastpass", "credential", "netbanking", "bank", "payment"]

DEFAULTS: dict[str, Any] = {
    "device_name": "",
    "user_name": "",
    "device_id": "",
    "setup_done": False,
    "deepseek_base_url": "https://api.deepseek.com",
    "deepseek_model": "deepseek-chat",
    "llm_provider": "deepseek",  # deepseek | openai | local | custom
    "openai_base_url": "https://api.openai.com/v1",
    "openai_model": "",
    "sarvam_base_url": "https://api.sarvam.ai/v2",
    "sarvam_model": "deepseekv4.1-flash-1017",
    "custom_label": "Other provider",
    "custom_base_url": "",
    "custom_model": "",
    "local_base_url": "http://localhost:11434/v1",
    "local_model": "",
    "vision": {"enabled": False, "consent": False, "base_url": "", "model": ""},
    "roots": [],  # [{"path": str, "write": bool}]
    "output_dir": "",
    "excluded_apps": DEFAULT_EXCLUDED_APPS,
    "ms_client_id": "",
    "ms_tenant": "common",
    "peers": [],  # [{"id","name","url","fingerprint"}]
    "remote": {"enabled": False, "bind": "", "port": 8766, "allow_screenshot": False},
    "max_steps": 40,
    "web_access": True,
    "outlook_backend": "auto",  # auto | com | graph
    "outlook_variant": "classic",  # classic | new
    "whatsapp_backend": "auto",  # auto | desktop | web
    "retention_days": 90,
    "screenshot_preview": False,
}

USER_EDITABLE = set(DEFAULTS) - {"device_id", "peers", "roots"}


class Settings:
    def __init__(self, path: Path | None = None):
        self.path = path or (data_dir() / "settings.json")
        self._lock = threading.RLock()
        self.data: dict[str, Any] = copy.deepcopy(DEFAULTS)
        if self.path.exists():
            try:
                self.data.update(json.loads(self.path.read_text("utf-8")))
            except Exception:
                pass
        if not self.data.get("device_id"):
            self.data["device_id"] = uuid.uuid4().hex
        if not self.data.get("output_dir"):
            self.data["output_dir"] = str(Path.home() / "Documents" / "RAM_AI_Output")
        self.save()

    def save(self) -> None:
        with self._lock:
            self.path.write_text(json.dumps(self.data, indent=2), "utf-8")

    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            return copy.deepcopy(self.data.get(key, default))

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            self.data[key] = value
            self.save()

    def update_user(self, patch: dict[str, Any]) -> list[str]:
        """Apply user-editable settings only. Returns the keys applied."""
        applied = []
        with self._lock:
            for k, v in patch.items():
                if k in USER_EDITABLE:
                    if isinstance(DEFAULTS[k], dict) and isinstance(v, dict):
                        merged = dict(self.data.get(k, {}))
                        merged.update({kk: vv for kk, vv in v.items() if kk in DEFAULTS[k]})
                        v = merged
                    self.data[k] = v
                    applied.append(k)
            self.save()
        return applied


# --------------------------------------------------------------------------- database
SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks(
  id TEXT PRIMARY KEY, instruction TEXT, device TEXT, state TEXT, plan TEXT,
  result TEXT, error TEXT, created REAL, updated REAL);
CREATE TABLE IF NOT EXISTS audit(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, task_id TEXT, device TEXT,
  event TEXT, tool TEXT, data TEXT);
CREATE TABLE IF NOT EXISTS approvals(
  id TEXT PRIMARY KEY, task_id TEXT, kind TEXT, tool TEXT, args_hash TEXT, summary TEXT,
  risk TEXT, status TEXT, answer TEXT, created REAL, decided REAL);
CREATE TABLE IF NOT EXISTS memory(key TEXT PRIMARY KEY, value TEXT, updated REAL);
CREATE TABLE IF NOT EXISTS sent(key TEXT PRIMARY KEY, ts REAL);
CREATE INDEX IF NOT EXISTS audit_task ON audit(task_id);
"""


class DB:
    def __init__(self, path: Path | None = None):
        self.path = path or (data_dir() / "ramai.db")
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        with self._lock:
            self.conn.executescript(SCHEMA)
            self.conn.commit()

    def exec(self, sql: str, params: tuple = ()) -> None:
        with self._lock:
            self.conn.execute(sql, params)
            self.conn.commit()

    def query(self, sql: str, params: tuple = ()) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    def one(self, sql: str, params: tuple = ()) -> dict | None:
        r = self.query(sql, params)
        return r[0] if r else None

    def audit(self, task_id: str | None, device: str, event: str, tool: str | None = None, data: Any = None) -> None:
        payload = redact(data) if data is not None else None
        if payload and len(payload) > 4000:  # keep the log entry valid JSON (the UI reads ms/args from it)
            keep = {k: v for k, v in data.items() if k in ("ms", "risk", "id", "secs") or (k == "args" and len(json.dumps(v, default=str)) < 1500)}                 if isinstance(data, dict) else {}
            keep["truncated"] = payload[:3000]
            payload = json.dumps(keep, default=str, ensure_ascii=False)
        self.exec(
            "INSERT INTO audit(ts,task_id,device,event,tool,data) VALUES(?,?,?,?,?,?)",
            (time.time(), task_id, device, event, tool, payload),
        )

    def purge_older_than(self, days: int) -> None:
        cutoff = time.time() - days * 86400
        self.exec("DELETE FROM audit WHERE ts<?", (cutoff,))
        self.exec("DELETE FROM tasks WHERE updated<?", (cutoff,))

    def clear_history(self) -> None:
        for t in ("audit", "tasks", "approvals"):
            self.exec(f"DELETE FROM {t}")
