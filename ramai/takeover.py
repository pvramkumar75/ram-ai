"""Hand-over: take the mouse/keyboard in the middle of a task to correct something, and RAM AI carries on.

While a task runs, low-level input hooks record only PHYSICAL input (Windows marks RAM AI's own simulated input as
'injected', so it is ignored). Clicks, wheel and keys outside the RAM AI window mean "the user is correcting something":
before its next screen action the agent waits until you have been idle for a moment, then looks at the screen again
instead of acting on what it saw before. 'Take control' (button or Ctrl+Alt+P) pauses it until you press Resume."""
from __future__ import annotations

import asyncio
import ctypes
import threading
import time
from ctypes import wintypes

IDLE_SECONDS = 3.0        # how long you must leave mouse/keyboard alone before the agent continues
MOVE_SETTLE = 1.2         # ...and the pointer must have been still this long (e.g. not mid-drag)
INPUT_TOOLS = {           # tools that drive the screen besides the 'control' ones
    "whatsapp_send_message", "whatsapp_send_prepared", "whatsapp_prepare_message", "whatsapp_read_chat", "whatsapp_find_chat",
    "send_email", "outlook_create_draft", "launch_app", "focus_window", "ask_chatgpt", "copy_window_text", "scroll_to_text"}

WH_KEYBOARD_LL, WH_MOUSE_LL = 13, 14
WM_MOUSEMOVE = 0x0200
LLMHF_INJECTED, LLKHF_INJECTED = 0x01, 0x10
ULONG_PTR = ctypes.c_size_t
LRESULT = ctypes.c_ssize_t
HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)


class MSLL(ctypes.Structure):
    _fields_ = [("pt", wintypes.POINT), ("mouseData", wintypes.DWORD), ("flags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("extra", ULONG_PTR)]


class KBDLL(ctypes.Structure):
    _fields_ = [("vk", wintypes.DWORD), ("scan", wintypes.DWORD), ("flags", wintypes.DWORD), ("time", wintypes.DWORD), ("extra", ULONG_PTR)]


class Takeover:
    def __init__(self, app=None):
        self.app = app
        self.last_action = 0.0      # last physical click/wheel/key outside RAM AI (monotonic seconds)
        self.last_move = 0.0        # last physical pointer movement
        self.mark_t = time.monotonic()
        self.paused = False         # 'Take control' pressed
        self.waiting: dict | None = None   # shown in the UI while the agent waits for you
        self.resume_now = threading.Event()
        self._thread = None
        self._tid = 0
        self._procs = []
        self._users = 0
        self._lock = threading.Lock()

    # ------------------------------------------------------------ hooks (only while tasks run)
    def _is_ramai(self, h) -> bool:
        try:
            from . import uia

            return bool(h) and uia.is_ramai_window(h)
        except Exception:
            return False

    def _run(self):
        u, k = ctypes.windll.user32, ctypes.windll.kernel32
        u.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, wintypes.HINSTANCE, wintypes.DWORD]
        u.SetWindowsHookExW.restype = ctypes.c_void_p
        u.CallNextHookEx.argtypes = [ctypes.c_void_p, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
        u.CallNextHookEx.restype = LRESULT
        u.UnhookWindowsHookEx.argtypes = [ctypes.c_void_p]
        u.WindowFromPoint.argtypes = [wintypes.POINT]
        u.WindowFromPoint.restype = ctypes.c_void_p
        u.GetAncestor.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        u.GetAncestor.restype = ctypes.c_void_p

        def mouse(n, w, l):
            if n == 0:
                m = ctypes.cast(l, ctypes.POINTER(MSLL)).contents
                if not m.flags & LLMHF_INJECTED:
                    now = time.monotonic()
                    if w == WM_MOUSEMOVE:
                        self.last_move = now
                    else:  # button / wheel: ignore it when it lands on the RAM AI window (e.g. clicking Approve)
                        top = u.GetAncestor(u.WindowFromPoint(m.pt), 2)
                        if not self._is_ramai(top):
                            self.last_action = now
            return u.CallNextHookEx(None, n, w, l)

        def keyboard(n, w, l):
            if n == 0:
                kb = ctypes.cast(l, ctypes.POINTER(KBDLL)).contents
                if not kb.flags & LLKHF_INJECTED and not self._is_ramai(u.GetForegroundWindow()):
                    self.last_action = time.monotonic()
            return u.CallNextHookEx(None, n, w, l)

        self._procs = [HOOKPROC(mouse), HOOKPROC(keyboard)]
        hm = u.SetWindowsHookExW(WH_MOUSE_LL, self._procs[0], k.GetModuleHandleW(None), 0)
        hk = u.SetWindowsHookExW(WH_KEYBOARD_LL, self._procs[1], k.GetModuleHandleW(None), 0)
        self._tid = k.GetCurrentThreadId()
        msg = wintypes.MSG()
        while u.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            pass
        for h in (hm, hk):
            if h:
                u.UnhookWindowsHookEx(h)

    def arm(self) -> None:
        """A task started: start watching for the user's hands (idempotent per task)."""
        with self._lock:
            self._users += 1
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._run, name="ramai-takeover", daemon=True)
                self._thread.start()
        self.mark()

    def disarm(self) -> None:
        with self._lock:
            self._users = max(0, self._users - 1)
            if self._users == 0 and self._thread and self._thread.is_alive() and self._tid:
                ctypes.windll.user32.PostThreadMessageW(self._tid, 0x0012, 0, 0)  # WM_QUIT -> unhook
                self._thread = None
        self.paused = False
        self.waiting = None

    # ------------------------------------------------------------ the agent's side
    def mark(self) -> None:
        """Called after each agent step: anything physical after this moment is the user."""
        self.mark_t = time.monotonic()

    def user_intervened(self) -> bool:
        return self.paused or self.last_action > self.mark_t

    def set_paused(self, on: bool) -> None:
        self.paused = bool(on)
        if not on:
            self.resume_now.set()

    async def gate(self, ctx, tool_name: str) -> bool:
        """Before a screen action: if you took over, wait until you are done. Returns True if you intervened."""
        if not self.user_intervened():
            return False
        self.resume_now.clear()
        why = "you pressed Take control" if self.paused else "you are using the mouse/keyboard"
        self.waiting = {"task_id": ctx.task_id, "since": time.time(), "reason": why, "paused": self.paused}
        self._say(ctx, f"Paused because {why}. I'll continue {'when you press Resume (or Ctrl+Alt+P)' if self.paused else f'{IDLE_SECONDS:.0f} s after you stop'} "
                       "and look at the screen again first.")
        try:
            while True:
                if ctx.stopped():
                    break
                if self.resume_now.is_set():
                    self.paused = False
                    break
                now = time.monotonic()
                if not self.paused and now - self.last_action >= IDLE_SECONDS and now - self.last_move >= MOVE_SETTLE:
                    break
                self.waiting["paused"] = self.paused
                await asyncio.sleep(0.25)
        finally:
            self.waiting = None
            self.mark()
        self._say(ctx, "You're done - continuing. Checking the screen first, since it may have changed.")
        return True

    def _say(self, ctx, text: str) -> None:
        try:
            self.app.db.audit(ctx.task_id, ctx.device, "thought", None, {"text": text})
        except Exception:
            pass


RECHECK = ("NOT DONE YET - the user took over the mouse/keyboard to correct something and has now handed back control. "
           "The screen may have changed: first look again (read_screen_text, screenshot, ui_elements or the app's own tools), "
           "then continue the task from where things are now. If this step is still needed, call it again. "
           "Do not undo what the user changed.")
