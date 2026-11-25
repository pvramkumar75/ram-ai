"""Phone control through your own private Telegram bot.

RAM AI only ever makes OUTGOING requests to Telegram (long polling), so nothing is opened on your PC or router.
The bot answers one person only: the Telegram account that paired with a one-time code. Everyone else is ignored.
Approvals work as on the PC: sensitive actions wait for an Approve / Decline tap."""
from __future__ import annotations

import asyncio
import hmac
import io
import re
import secrets as pysecrets
import time
from typing import Any

import httpx

from .core import redact
from .policy import PolicyError

API = "https://api.telegram.org"
TERMINAL = {"Completed", "Failed", "Cancelled"}
STALE = 600                       # a message older than this was sent while RAM AI was off: never run it late
CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
TOKEN_RE = re.compile(r"^\d{5,}:[A-Za-z0-9_-]{30,}$")
NO_PHONE_ALL = {"pair_device", "remember"}   # never offered as "approve everything in this task"
PHONE_NEVER = {"pair_device"}                # pairing another PC must be approved on the PC itself
HELP = ("RAM AI on your phone. Just type what you want done, for example:\n"
        "  Send the copper rate report to the sales team\n\n"
        "/status  what is running now\n"
        "/tasks  your last tasks\n"
        "/cancel  stop the running task(s)\n"
        "/stop  emergency stop of everything\n"
        "/screenshot  see the PC screen (if you turned it on)\n"
        "/unpair  disconnect this phone\n\n"
        "Anything that sends, deletes or overwrites asks you here first: tap Approve or Decline.")


def chunks(text: str, n: int = 3900, limit: int = 3) -> list[str]:
    text = (text or "").strip() or "(empty)"
    out = [text[i:i + n] for i in range(0, len(text), n)]
    if len(out) > limit:
        out = out[:limit]
        out[-1] = out[-1][: n - 30] + "\n… (trimmed; full text is on the PC)"
    return out


def shrink(png: bytes, width: int = 1600) -> bytes:
    from PIL import Image

    im = Image.open(io.BytesIO(png)).convert("RGB")
    if im.width > width:
        im = im.resize((width, round(im.height * width / im.width)))
    b = io.BytesIO()
    im.save(b, "JPEG", quality=80)
    return b.getvalue()


class Telegram:
    def __init__(self, app, client: httpx.AsyncClient | None = None):
        self.app = app
        self.client = client            # tests inject a fake Telegram here
        self.task: asyncio.Task | None = None
        self.error = ""
        self.connected = False
        self.pairing: dict | None = None
        self.tracked: dict[str, float] = {}      # task id -> time started (tasks that came from the phone)
        self.shown: dict[str, dict] = {}         # approval id -> {"mid", "text", "kind"} messages still waiting on the phone
        self._typing = 0.0
        self._rejected_at = 0.0

    # ------------------------------------------------------------------ plumbing
    def cfg(self) -> dict:
        return self.app.settings.get("telegram") or {}

    def save(self, **kw) -> None:
        c = self.cfg()
        c.update(kw)
        self.app.settings.set("telegram", c)

    def token(self) -> str | None:
        return self.app.secrets.get("telegram_bot")

    async def _call(self, method: str, params: dict | None = None, files: dict | None = None, timeout: float = 30,
                    token: str | None = None) -> Any:
        tok = token or self.token()
        if not tok:
            raise PolicyError("No Telegram bot token saved")
        if self.client is None:
            self.client = httpx.AsyncClient(timeout=40)
        try:
            kw: dict[str, Any] = {"timeout": timeout}
            if files:
                kw["data"], kw["files"] = {k: str(v) for k, v in (params or {}).items()}, files
            else:
                kw["json"] = params or {}
            r = await self.client.post(f"{API}/bot{tok}/{method}", **kw)
            d = r.json()
        except httpx.HTTPError as e:
            raise PolicyError(f"Cannot reach Telegram ({type(e).__name__})") from None
        except ValueError:
            raise PolicyError("Telegram sent an unreadable reply") from None
        if not d.get("ok"):
            raise PolicyError(f"Telegram: {d.get('description') or r.status_code}")
        return d["result"]

    async def send(self, chat: int, text: str, markup: dict | None = None, **extra) -> int | None:
        mid = None
        parts = chunks(text)
        for i, part in enumerate(parts):
            p: dict[str, Any] = {"chat_id": chat, "text": part, "disable_web_page_preview": True, **extra}
            if markup and i == len(parts) - 1:
                p["reply_markup"] = markup
            mid = (await self._call("sendMessage", p))["message_id"]
        return mid

    def audit(self, event: str, data: dict | None = None, task_id: str | None = None) -> None:
        self.app.db.audit(task_id, self.app.device_name, event, None, data or {})

    # ------------------------------------------------------------------ setup (called from the settings page)
    def status(self) -> dict:
        c = self.cfg()
        live = bool(self.pairing and time.time() < self.pairing["expires"])
        return {"enabled": bool(c.get("enabled") and self.token()), "connected": self.connected, "bot": c.get("bot", ""),
                "paired": bool(c.get("chat_id")), "who": c.get("who", ""), "allow_screenshot": bool(c.get("allow_screenshot")),
                "phone_high_risk": c.get("phone_high_risk", True) is not False, "error": self.error, "pairing": live,
                "token_set": bool(self.token())}

    async def connect(self, token: str) -> dict:
        token = (token or "").strip()
        if not TOKEN_RE.match(token):
            raise PolicyError("That does not look like a bot token. It looks like 123456789:AAH… (from BotFather).")
        me = await self._call("getMe", token=token)
        c = self.cfg()
        if c.get("bot_id") != me["id"]:       # a different bot: forget the old owner and update position
            c.update({"chat_id": 0, "who": "", "offset": 0})
        c.update({"enabled": True, "bot": me.get("username", ""), "bot_id": me["id"]})
        self.app.secrets.set("telegram_bot", token)
        self.app.settings.set("telegram", c)
        try:
            await self._call("deleteWebhook", {"drop_pending_updates": False})
        except PolicyError:
            pass
        self.audit("telegram_connected", {"bot": me.get("username")})
        self.stop()
        self.start()
        return self.status()

    def start_pairing(self) -> dict:
        if not self.token():
            raise PolicyError("Connect your bot first")
        code = "".join(pysecrets.choice(CODE_ALPHABET) for _ in range(8))
        self.pairing = {"code": code, "expires": time.time() + 600, "attempts": 0}
        bot = self.cfg().get("bot", "")
        return {"code": code, "expires_in": 600, "bot": bot, "link": f"https://t.me/{bot}?start={code}" if bot else ""}

    def unpair(self) -> None:
        self.save(chat_id=0, who="")
        self.pairing = None
        self.tracked.clear()
        self.audit("telegram_unpaired")

    def disconnect(self) -> None:
        self.stop()
        self.app.secrets.delete("telegram_bot")
        self.save(enabled=False, chat_id=0, who="", bot="", bot_id=0, offset=0)
        self.pairing, self.connected, self.error = None, False, ""
        self.tracked.clear()
        self.shown.clear()
        self.audit("telegram_disconnected")

    def start(self) -> None:
        c = self.cfg()
        if not (c.get("enabled") and self.token()) or (self.task and not self.task.done()):
            return
        self.task = asyncio.get_running_loop().create_task(self._main())

    def stop(self) -> None:
        if self.task:
            self.task.cancel()
            self.task = None
        self.connected = False

    # ------------------------------------------------------------------ loops
    async def _main(self) -> None:
        await asyncio.gather(self._poll_loop(), self._watch_loop())

    async def _poll_loop(self) -> None:
        backoff = 2
        while True:
            try:
                await self.poll_once(25)
                self.connected, self.error, backoff = True, "", 2
                await asyncio.sleep(0.3)      # Telegram holds an empty poll for ~25 s; this only stops a runaway loop
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.connected, self.error = False, redact(str(e))[:200]
                await asyncio.sleep(backoff)
                backoff = min(60, backoff * 2)

    async def _watch_loop(self) -> None:
        while True:
            try:
                await self.watch_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.error = redact(str(e))[:200]
            await asyncio.sleep(2)

    async def poll_once(self, wait: int = 0) -> int:
        off = int(self.cfg().get("offset") or 0)
        params: dict[str, Any] = {"timeout": wait, "allowed_updates": ["message", "callback_query"]}
        if off:
            params["offset"] = off
        updates = await self._call("getUpdates", params, timeout=wait + 15)
        for u in updates:
            self.save(offset=u["update_id"] + 1)     # saved BEFORE acting: an update is never run twice, even after a crash
            try:
                await self.handle(u)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.error = redact(str(e))[:200]
        return len(updates)

    # ------------------------------------------------------------------ incoming
    async def handle(self, u: dict) -> None:
        owner = int(self.cfg().get("chat_id") or 0)
        cb = u.get("callback_query")
        if cb:
            if owner and (cb.get("from") or {}).get("id") == owner:
                await self._callback(cb, owner)
            return
        m = u.get("message")
        if not m or (m.get("chat") or {}).get("type") != "private":
            return
        uid = (m.get("from") or {}).get("id")
        if not owner:
            await self._stranger(m)
        elif uid != owner or m["chat"]["id"] != owner:
            if time.time() - self._rejected_at > 60:       # ignored silently; logged at most once a minute
                self._rejected_at = time.time()
                self.audit("telegram_rejected", {"user": uid})
        else:
            await self._owner(m, owner)

    async def _stranger(self, m: dict) -> None:
        """Nobody is paired yet: the only thing that works is the one-time code from RAM AI's settings."""
        p = self.pairing
        chat = m["chat"]["id"]
        if not p or time.time() > p["expires"]:
            return
        text = (m.get("text") or "").strip()
        if text.lower().startswith("/start"):
            text = text[6:].strip()
        text = text.upper().replace(" ", "").replace("-", "")
        p["attempts"] += 1
        if p["attempts"] > 5:
            self.pairing = None
            return
        if not text or not hmac.compare_digest(text, p["code"]):
            await self.send(chat, "That code is not right. Open RAM AI on your PC, Settings, Phone, and press 'Pair my phone'.")
            return
        who = " ".join(x for x in [(m.get("from") or {}).get("first_name"), (m.get("from") or {}).get("last_name")] if x)[:60]
        self.pairing = None
        self.save(chat_id=chat, who=who or "your phone")
        self.audit("telegram_paired", {"who": who})
        await self.send(chat, "✅ Paired. This phone can now control RAM AI on " + self.app.device_name + ".\n\n" + HELP)

    async def _owner(self, m: dict, chat: int) -> None:
        text = (m.get("text") or "").strip()
        if not text:
            await self.send(chat, "I understand typed messages only for now.")
            return
        age = time.time() - float(m.get("date") or time.time())
        if age > STALE:
            await self.send(chat, f"That message is {int(age // 60)} minutes old (RAM AI was off), so I did not run it. Send it again if you still want it.")
            return
        rt = m.get("reply_to_message")
        if rt:
            aid = next((a for a, s in self.shown.items() if s["mid"] == rt.get("message_id") and s["kind"] == "question"), None)
            if aid:
                try:
                    self.app.approvals.decide(aid, "approve", text[:2000], "once")
                    self.shown.pop(aid, None)
                    await self.send(chat, "👍 Answer sent.")
                except ValueError:
                    self.shown.pop(aid, None)
                    await self.send(chat, "That question is no longer waiting (it was answered or timed out).")
                return
        cmd = text.split()[0].lower().split("@")[0] if text.startswith("/") else ""
        app = self.app
        if cmd in ("/start", "/help"):
            await self.send(chat, HELP)
        elif cmd == "/status":
            await self.send(chat, self._status_text())
        elif cmd == "/tasks":
            await self.send(chat, self._tasks_text())
        elif cmd == "/stop":
            app.orchestrator.emergency_stop()
            self.audit("telegram_emergency_stop")
            await self.send(chat, "⏹ Stopped everything.")
        elif cmd == "/cancel":
            ids = [t for t in self.tracked if t in app.orchestrator.running] or list(app.orchestrator.running)
            n = sum(1 for t in ids if app.orchestrator.cancel(t))
            await self.send(chat, f"⏹ Cancelled {n} task(s)." if n else "Nothing is running.")
        elif cmd == "/screenshot":
            await self._screenshot(chat)
        elif cmd == "/unpair":
            await self.send(chat, "Phone disconnected. You can pair again from RAM AI's settings on the PC.")
            self.unpair()
        elif cmd:
            await self.send(chat, "I don't know that command.\n\n" + HELP)
        else:
            await self._new_task(chat, text)

    async def _new_task(self, chat: int, text: str) -> None:
        app = self.app
        if not app.configured():
            await self.send(chat, "RAM AI is not fully set up on the PC yet (it needs a device name and an AI key).")
            return
        tid = app.orchestrator.create(text[:4000], app.device_name)
        self.tracked[tid] = time.time()
        self.audit("task_from_telegram", {"instruction": text[:300]}, tid)
        await self.send(chat, f"▶ On it ({tid[:4]}). I'll message you when it is done or when I need your OK.")

    async def _callback(self, cb: dict, chat: int) -> None:
        data = str(cb.get("data") or "")
        kind, _, aid = data.partition(":")
        row = self.app.db.one("SELECT kind,tool,risk,status FROM approvals WHERE id=?", (aid,))
        note = ""
        if kind in ("ay", "at", "an") and row and row["status"] == "pending":
            if kind != "an" and (row["tool"] in PHONE_NEVER or not self._phone_may(row)):
                note = "Approve this one on the PC."
            elif kind == "at" and (row["kind"] != "approval" or row["tool"] in NO_PHONE_ALL):
                note = "Not allowed for this action."
            else:
                try:
                    self.app.approvals.decide(aid, "reject" if kind == "an" else "approve", None, "task" if kind == "at" else "once")
                    note = "Declined" if kind == "an" else ("Approved for this task" if kind == "at" else "Approved")
                except ValueError:
                    note = "No longer waiting"
        else:
            note = "No longer waiting"
        try:
            await self._call("answerCallbackQuery", {"callback_query_id": cb.get("id"), "text": note})
        except PolicyError:
            pass
        sh = self.shown.get(aid)
        if sh and note in ("Declined", "Approved", "Approved for this task", "No longer waiting"):
            await self._finish_msg(chat, aid, ("✅ " if note.startswith("Approved") else "🚫 " if note == "Declined" else "") + note)

    def _phone_may(self, row: dict) -> bool:
        return row.get("risk") != "high" or self.cfg().get("phone_high_risk", True) is not False

    async def _screenshot(self, chat: int) -> None:
        if not self.cfg().get("allow_screenshot"):
            await self.send(chat, "Screenshots on the phone are switched off. Turn them on in RAM AI, Settings, Phone.")
            return
        from . import uia
        from .tools.computer import grab, is_excluded

        h0 = self.app.work.current()
        if is_excluded(self.app, uia.window_title(h0) if h0 else None):
            await self.send(chat, "The window in front is on the exclusion list, so I did not capture it.")
            return
        png, _ = await asyncio.to_thread(grab, 1)
        await self._call("sendPhoto", {"chat_id": chat}, files={"photo": ("screen.jpg", shrink(png), "image/jpeg")}, timeout=60)
        self.audit("telegram_screenshot")

    # ------------------------------------------------------------------ outgoing
    def _status_text(self) -> str:
        app = self.app
        run = [t for t in app.orchestrator.running]
        rows = app.db.query("SELECT instruction,state FROM tasks WHERE id IN (%s)" % ",".join("?" * len(run)), tuple(run)) if run else []
        lines = [f"{app.device_name} · RAM AI is on · {app.deepseek.label()} / {app.deepseek.model()}"]
        lines += [f"▶ {r['state']}: {r['instruction'][:80]}" for r in rows] or ["Nothing is running."]
        n = len(app.approvals.pending())
        if n:
            lines.append(f"⚠ {n} waiting for approval")
        return "\n".join(lines)

    def _tasks_text(self) -> str:
        rows = self.app.db.query("SELECT instruction,state FROM tasks ORDER BY created DESC LIMIT 6")
        icon = {"Completed": "✅", "Failed": "❌", "Cancelled": "⏹"}
        return "\n".join(f"{icon.get(r['state'], '▶')} {r['state']}: {r['instruction'][:70]}" for r in rows) or "No tasks yet."

    async def watch_once(self) -> None:
        chat = int(self.cfg().get("chat_id") or 0)
        if not chat:
            return
        db = self.app.db
        for tid in list(self.tracked):
            row = db.one("SELECT state,result,error FROM tasks WHERE id=?", (tid,))
            if not row:
                self.tracked.pop(tid, None)
            elif row["state"] in TERMINAL:
                body = {"Completed": "✅ Done.\n\n" + (row["result"] or ""), "Failed": "❌ Could not finish.\n\n" + (row["error"] or ""),
                        "Cancelled": "⏹ Cancelled."}[row["state"]]
                await self.send(chat, body.strip())           # only forgotten once it really went out
                self.tracked.pop(tid, None)
        pend = self.app.approvals.pending()
        ids = {a["id"] for a in pend}
        for a in pend:
            if a["id"] not in self.shown:
                await self._ask(chat, a)
        for aid in [x for x in self.shown if x not in ids]:   # decided on the PC (or timed out): take the buttons away
            await self._finish_msg(chat, aid, "(handled on the PC)")
        if self.tracked and time.time() - self._typing > 4.5:
            self._typing = time.time()
            try:
                await self._call("sendChatAction", {"chat_id": chat, "action": "typing"})
            except PolicyError:
                pass

    async def _ask(self, chat: int, a: dict) -> None:
        if a["kind"] == "question":
            text = "❓ " + a["summary"] + "\n\n(Reply to this message to answer.)"
            mid = await self.send(chat, text, None, reply_markup={"force_reply": True, "input_field_placeholder": "Your answer"})
        else:
            on_pc = a["tool"] in PHONE_NEVER or not self._phone_may(a)
            text = f"⚠ APPROVAL NEEDED ({a['risk']} risk)\n\n{a['summary']}"
            if on_pc:
                mid = await self.send(chat, text + "\n\nApprove or decline this one on the PC.")
            else:
                rows = [[{"text": "✅ Approve", "callback_data": f"ay:{a['id']}"}, {"text": "🚫 Decline", "callback_data": f"an:{a['id']}"}]]
                if a["tool"] not in NO_PHONE_ALL:
                    rows.append([{"text": "✅ Approve all for this task", "callback_data": f"at:{a['id']}"}])
                mid = await self.send(chat, text, {"inline_keyboard": rows})
        self.shown[a["id"]] = {"mid": mid, "text": text, "kind": a["kind"]}

    async def _finish_msg(self, chat: int, aid: str, note: str) -> None:
        sh = self.shown.pop(aid, None)
        if not sh or not sh.get("mid"):
            return
        try:
            await self._call("editMessageText", {"chat_id": chat, "message_id": sh["mid"], "text": (sh["text"] + "\n\n" + note)[:4000]})
        except PolicyError:
            pass
