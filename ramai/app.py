"""Application container: wires services together; control indicator and emergency-stop hotkey."""
from __future__ import annotations

import asyncio
import ctypes
import queue
import threading
import time
from ctypes import wintypes

from .core import DB, SecretStore, Settings, data_dir
from .engine import ApprovalManager, Executor, Orchestrator, register_system
from .policy import Policy
from .providers import DeepSeek, Vision
from .tools import apps, chatai, code, computer, docs, files, outlook, outlook_tools, schedule_tools, skills, web, whatsapp
from .tools.base import Registry
from . import remote

TERMINAL = {"Completed", "Failed", "Cancelled"}


class Indicator:
    """Always-on-top banner shown while the agent controls mouse/keyboard."""

    def __init__(self, enabled: bool = True):
        self.q: queue.Queue = queue.Queue()
        self.enabled = enabled
        self.thread = None
        self.visible = False

    def _run(self):
        try:
            import tkinter as tk

            root = tk.Tk()
            root.overrideredirect(True)
            root.attributes("-topmost", True)
            root.configure(bg="#b00020")
            lbl = tk.Label(root, text="", fg="white", bg="#b00020", font=("Segoe UI", 11, "bold"), padx=14, pady=5)
            lbl.pack()
            root.withdraw()

            def poll():
                try:
                    while True:
                        cmd, text = self.q.get_nowait()
                        if cmd == "show":
                            lbl.config(text="● " + text)
                            root.update_idletasks()
                            w = root.winfo_reqwidth()
                            root.geometry(f"+{(root.winfo_screenwidth() - w) // 2}+0")
                            root.deiconify()
                        elif cmd == "hide":
                            root.withdraw()
                        elif cmd == "quit":
                            root.destroy()
                            return
                except queue.Empty:
                    pass
                root.after(120, poll)

            poll()
            root.mainloop()
        except Exception:
            pass

    def show(self, text: str):
        if not self.enabled:
            return
        if not self.thread:
            self.thread = threading.Thread(target=self._run, daemon=True)
            self.thread.start()
        self.visible = True
        self.q.put(("show", text))

    def hide(self):
        if self.thread:
            self.q.put(("hide", ""))
        self.visible = False


def start_hotkey(callback) -> threading.Thread | None:
    """Global emergency stop: Ctrl+Alt+Q."""

    def run():
        u = ctypes.windll.user32
        if not u.RegisterHotKey(None, 1, 0x0001 | 0x0002 | 0x4000, ord("Q")):  # ALT|CTRL|NOREPEAT
            return
        msg = wintypes.MSG()
        while u.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
            if msg.message == 0x0312:
                callback()

    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t


class App:
    def __init__(self, indicator: bool = True):
        self.data_path = data_dir()
        self.settings = Settings()
        self.secrets = SecretStore()
        self.db = DB()
        self.policy = Policy(self.settings)
        self.deepseek = DeepSeek(self.settings, self.secrets)
        self.vision = Vision(self.settings, self.secrets)
        self.global_stop = threading.Event()
        self.screen_state: dict | None = None
        self.last_screenshot: bytes | None = None
        self.screenshot_sharing = True
        self.pairing: dict | None = None
        self.thinking: dict[str, float] = {}  # task_id -> time the model started thinking
        self.loop: asyncio.AbstractEventLoop | None = None
        self.indicator = Indicator(indicator)
        self.approvals = ApprovalManager(self)
        self.registry = Registry()
        self.start_apps = apps.StartApps()
        self.start_apps.start_background()
        from . import uia as _uia

        self.work = _uia.WorkWindowTracker()
        if indicator:
            self.work.start()
        computer.register(self.registry)
        files.register(self.registry)
        apps.register(self.registry)
        apps.register_ocr(self.registry)
        apps.register_copy(self.registry)
        schedule_tools.register(self.registry)
        code.register(self.registry)
        if indicator:
            code.ensure_workspace_background()
        skills.register(self.registry)
        chatai.register(self.registry)
        docs.register(self.registry)
        web.register(self.registry)
        outlook_tools.register(self.registry)
        whatsapp.register(self.registry)
        remote.register(self.registry)
        register_system(self.registry)
        self.executor = Executor(self, self.registry)
        self.orchestrator = Orchestrator(self)
        from .scheduler import Scheduler

        self.scheduler = Scheduler(self)
        self.outlook = outlook.OutlookAuth(self)
        self.whatsapp = whatsapp.WhatsApp(self)
        self.remote_server = remote.RemoteServer(self)
        self.policy.settings.get("output_dir") and self._ensure_output()

    def _ensure_output(self):
        try:
            from pathlib import Path

            Path(self.settings.get("output_dir")).mkdir(parents=True, exist_ok=True)
        except OSError:
            pass

    @property
    def device_name(self) -> str:
        return self.settings.get("device_name") or "This PC"

    def remote_url(self) -> str:
        r = self.settings.get("remote")
        return f"https://{r['bind']}:{r['port']}" if r.get("enabled") and r.get("bind") else ""

    def set_task_state(self, tid: str, state: str) -> None:
        row = self.db.one("SELECT state FROM tasks WHERE id=?", (tid,))
        if row and row["state"] not in TERMINAL:
            self.db.exec("UPDATE tasks SET state=?,updated=? WHERE id=?", (state, time.time(), tid))

    def add_peer(self, peer: dict, secret: str) -> None:
        peers = [p for p in self.settings.get("peers", []) if p["id"] != peer["id"]]
        peers.append(peer)
        self.settings.set("peers", peers)
        self.secrets.set(f"peer:{peer['id']}", secret)

    def remove_peer(self, pid: str) -> None:
        self.settings.set("peers", [p for p in self.settings.get("peers", []) if p["id"] != pid])
        self.secrets.delete(f"peer:{pid}")
        self.db.audit(None, self.device_name, "peer_revoked", None, {"id": pid})

    def configured(self) -> bool:
        return self.deepseek.ready() and bool(self.settings.get("device_name")) and bool(self.settings.get("user_name"))
