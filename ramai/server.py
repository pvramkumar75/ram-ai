"""Local control API (loopback only, per-launch token) + static UI."""
from __future__ import annotations

import asyncio
import hmac
import json
import re
import secrets as pysecrets
import threading
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from .app import App, start_hotkey
from .core import VERSION, redact
from .policy import PolicyError
from .providers import ProviderError
from .remote import join_peer, peer_call

UI_DIR = Path(__file__).parent / "ui"
PEER_METHODS = {"ping", "state", "submit_task", "cancel_task", "decide_approval", "screenshot"}


def create_api(app: App, token: str | None = None, port: int = 8765) -> FastAPI:
    token = token or pysecrets.token_urlsafe(32)
    api = FastAPI(title="RAM AI local agent", docs_url=None, redoc_url=None, openapi_url=None)
    api.state.token = token
    started = __import__("time").time()
    hosts = {f"127.0.0.1:{port}", f"localhost:{port}", "testserver", "127.0.0.1", "localhost"}

    @api.on_event("startup")
    async def _startup():
        app.loop = asyncio.get_running_loop()
        app.orchestrator.recover()
        app.scheduler.start()
        app.db.purge_older_than(app.settings.get("retention_days", 90))

    @api.middleware("http")
    async def guard(request: Request, call_next):
        if request.headers.get("host", "") not in hosts:  # DNS-rebinding defence
            return JSONResponse({"detail": "bad host"}, 403)
        origin = request.headers.get("origin")
        if origin and origin.split("://", 1)[-1] not in hosts:
            return JSONResponse({"detail": "bad origin"}, 403)
        if request.url.path.startswith("/api/"):
            if not hmac.compare_digest(request.headers.get("x-ram-token", ""), token):
                return JSONResponse({"detail": "unauthorised"}, 401)
        resp = await call_next(request)
        if not request.url.path.startswith("/api/"):
            resp.headers["Cache-Control"] = "no-cache, must-revalidate"  # always load the current UI after updates
        return resp

    @api.get("/bootstrap")
    async def bootstrap(request: Request):
        """Lets the app window recover the session token when Edge re-uses an old window (token missing from URL).
        Only same-origin browser fetches (Sec-Fetch-Site is set by the browser and cannot be forged by web pages),
        and only during the first 3 minutes after the agent starts."""
        import time

        if request.headers.get("sec-fetch-site") != "same-origin" or time.time() - started > 180:
            raise HTTPException(403, "bootstrap unavailable; re-open RAM AI from its launcher")
        return JSONResponse({"token": token}, headers={"Cache-Control": "no-store"})

    def err(e: Exception, code=400):
        raise HTTPException(code, redact(str(e)))

    # ---------------------------------------------------------------- status & settings
    def public_settings() -> dict:
        s = dict(app.settings.data)
        s["roots"] = app.policy.roots()
        s["secrets_set"] = {"deepseek": app.secrets.has("deepseek"), "vision": app.secrets.has("vision")}
        s["llm_providers"] = app.deepseek.providers_status()
        s["peers"] = [{k: v for k, v in p.items() if k != "fingerprint"} for p in s.get("peers", [])]
        return s

    @api.get("/api/status")
    async def status():
        return {"version": VERSION, "configured": app.configured(), "device": app.device_name,
                "control_active": app.indicator.visible, "settings": public_settings(),
                "usage": app.deepseek.usage, "llm_provider": app.settings.get("llm_provider"),
                "llm": {"provider": app.deepseek.provider(), "label": app.deepseek.label(), "model": app.deepseek.model()}, "outlook": {"account": app.outlook.account() if app.settings.get("ms_client_id") else None,
                                                         **app.outlook.login_state},
                "vision_ready": app.vision.ready()[0], "screenshot_sharing": app.screenshot_sharing,
                "tools": app.registry.names()}

    @api.post("/api/settings")
    async def set_settings(body: dict = Body(...)):
        applied = app.settings.update_user(body)
        if "device_name" in applied:
            app.settings.set("device_name", str(body["device_name"])[:40])
        return {"applied": applied}

    @api.post("/api/secrets/{name}")
    async def set_secret(name: str, body: dict = Body(...)):
        from .providers import LLM_SECRETS

        if name not in LLM_SECRETS | {"vision"}:
            raise HTTPException(404)
        v = str(body.get("value", "")).strip()
        if len(v) < 4:
            raise HTTPException(400, "Key looks too short")
        app.secrets.set(name, v)
        app.db.audit(None, app.device_name, "secret_set", name)
        return {"ok": True}

    @api.delete("/api/secrets/{name}")
    async def del_secret(name: str):
        from .providers import LLM_SECRETS

        if name not in LLM_SECRETS | {"vision"}:
            raise HTTPException(404)
        app.secrets.delete(name)
        return {"ok": True}

    @api.post("/api/llm/test")
    async def test_llm(body: dict = Body(default={})):
        try:
            r = await app.deepseek.test(body.get("provider"))
            if not r["selected_available"] and r["models"]:
                r["hint"] = "Selected model not in this account's list; choose one from 'models'."
            return r
        except (ProviderError, Exception) as e:
            return {"ok": False, "error": redact(str(e)) or type(e).__name__}

    @api.post("/api/llm/select")
    async def llm_select(body: dict = Body(...)):
        from .providers import PROVIDERS

        pid = str(body.get("provider", ""))
        if pid not in PROVIDERS:
            raise HTTPException(400, "Unknown provider")
        if body.get("model"):
            app.deepseek.use_model(pid, str(body["model"])[:400])
        app.settings.set("llm_provider", pid)
        app.db.audit(None, app.device_name, "llm_selected", None, {"provider": pid, "model": app.deepseek.model(pid)})
        return {"provider": pid, "model": app.deepseek.model(pid), "ready": app.deepseek.ready(pid)}

    @api.post("/api/deepseek/test")
    async def test_deepseek():
        try:
            r = await app.deepseek.test()
            if not r["selected_available"] and r["models"]:
                r["hint"] = "Selected model not in your account's list; choose one from 'models'."
            return r
        except (ProviderError, Exception) as e:
            return {"ok": False, "error": redact(str(e)) or type(e).__name__}

    @api.post("/api/vision/test")
    async def test_vision():
        try:
            return await app.vision.test()
        except ProviderError as e:
            return {"ok": False, "error": redact(str(e))}

    @api.post("/api/screenshot_sharing")
    async def sharing(body: dict = Body(...)):
        app.screenshot_sharing = bool(body.get("enabled"))
        return {"enabled": app.screenshot_sharing}

    # ---------------------------------------------------------------- folders
    @api.post("/api/roots")
    async def add_root(body: dict = Body(...)):
        try:
            e = app.policy.add_root(str(body.get("path", "")), bool(body.get("write", False)))
        except PolicyError as ex:
            err(ex)
        app.db.audit(None, app.device_name, "root_added", None, e)
        return e

    @api.post("/api/roots/quick")
    async def quick_roots(body: dict = Body(default={})):
        """Grant read-only access to the user's everyday folders (Desktop, Documents, Downloads, OneDrive)."""
        import os

        home = Path.home()
        cands = [home / "Desktop", home / "Documents", home / "Downloads"]
        for env in ("OneDriveCommercial", "OneDrive", "OneDriveConsumer"):
            if os.environ.get(env):
                cands.append(Path(os.environ[env]))
        added = []
        for c in dict.fromkeys(cands):
            if c.exists():
                try:
                    app.policy.add_root(str(c), bool(body.get("write", False)))
                    added.append(str(c))
                except PolicyError:
                    pass
        app.db.audit(None, app.device_name, "roots_quick_added", None, {"roots": added, "write": bool(body.get("write"))})
        return {"added": added}

    @api.delete("/api/roots")
    async def del_root(body: dict = Body(...)):
        app.policy.remove_root(str(body.get("path", "")))
        app.db.audit(None, app.device_name, "root_removed", None, body)
        return {"ok": True}

    @api.post("/api/roots/pick")
    async def pick():
        def run():
            import tkinter as tk
            from tkinter import filedialog

            r = tk.Tk()
            r.withdraw()
            r.attributes("-topmost", True)
            p = filedialog.askdirectory(title="Choose a folder RAM AI may access")
            r.destroy()
            return p

        return {"path": await asyncio.to_thread(run)}

    # ---------------------------------------------------------------- tasks
    @api.post("/api/tasks")
    async def create_task(body: dict = Body(...)):
        ins = str(body.get("instruction", "")).strip()
        if not ins:
            raise HTTPException(400, "Empty instruction")
        if not app.configured():
            raise HTTPException(400, "Finish setup first (device name and a DeepSeek key or local model)")
        peer_id = body.get("device_id")
        if peer_id and peer_id != app.settings.get("device_id"):
            peer = next((p for p in app.settings.get("peers", []) if p["id"] == peer_id), None)
            if not peer:
                raise HTTPException(404, "Unknown device")
            try:
                r = await peer_call(app, peer, "submit_task", {"instruction": ins, "pre_approve": bool(body.get("pre_approve"))})
            except PolicyError as e:
                err(e, 502)
            app.db.audit(r["task_id"], peer["name"], "task_dispatched_remote", None, {"instruction": ins})
            return {"task_id": r["task_id"], "device": peer["name"], "remote": True}
        return {"task_id": app.orchestrator.create(ins, pre_approve=bool(body.get("pre_approve"))), "device": app.device_name}

    @api.get("/api/tasks")
    async def tasks():
        return app.db.query("SELECT id,instruction,device,state,result,error,created,updated FROM tasks ORDER BY created DESC LIMIT 50")

    @api.get("/api/tasks/{tid}")
    async def task(tid: str):
        t = app.db.one("SELECT * FROM tasks WHERE id=?", (tid,))
        if not t:
            raise HTTPException(404)
        t["plan"] = json.loads(t["plan"]) if t.get("plan") else []
        t["events"] = app.db.query("SELECT ts,event,tool,data FROM audit WHERE task_id=? ORDER BY id", (tid,))
        return t

    @api.get("/api/tasks/{tid}/events")
    async def task_events(tid: str, after: int = 0):
        t = app.db.one("SELECT id,instruction,device,state,plan,result,error,created,updated FROM tasks WHERE id=?", (tid,))
        if not t:
            raise HTTPException(404)
        t["plan"] = json.loads(t["plan"]) if t.get("plan") else []
        t["thinking_since"] = app.thinking.get(tid)
        t["now"] = __import__("time").time()
        t["events"] = app.db.query("SELECT id,ts,event,tool,data FROM audit WHERE task_id=? AND id>? ORDER BY id LIMIT 500", (tid, after))
        return t

    @api.post("/api/tasks/{tid}/cancel")
    async def cancel(tid: str):
        return {"cancelled": app.orchestrator.cancel(tid)}

    @api.post("/api/stop")
    async def stop():
        app.orchestrator.emergency_stop()
        return {"stopped": True}

    # ---------------------------------------------------------------- schedules
    @api.get("/api/schedules")
    async def schedules():
        return app.scheduler.list()

    @api.post("/api/schedules")
    async def schedule_add(body: dict = Body(...)):
        try:
            spec = {"kind": body.get("kind", "daily"), "time": body.get("time", "09:00")}
            for k in ("date", "days", "day_of_month", "every_minutes"):
                if body.get(k) not in (None, "", []):
                    spec[k] = body[k]
            if spec["kind"] == "interval":
                spec["start"] = __import__("datetime").datetime.now().replace(second=0, microsecond=0).isoformat()
            allowed = [x.strip() for x in str(body.get("allowed", "")).replace(";", ",").split(",") if x.strip()]
            return app.scheduler.create(str(body.get("title") or body["instruction"][:60]), str(body["instruction"]), spec, allowed)
        except (KeyError, ValueError) as e:
            err(e)

    @api.post("/api/schedules/{sid}/enabled")
    async def schedule_enable(sid: str, body: dict = Body(...)):
        app.scheduler.set_enabled(sid, bool(body.get("enabled")))
        return app.scheduler.get(sid)

    @api.post("/api/schedules/{sid}/run")
    async def schedule_run(sid: str):
        try:
            return {"task_id": app.scheduler.run_now(sid, "run now")}
        except ValueError as e:
            err(e)

    @api.delete("/api/schedules/{sid}")
    async def schedule_del(sid: str):
        app.scheduler.delete(sid)
        return {"ok": True}

    def _startup_file():
        import os

        return Path(os.environ["APPDATA"]) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / "RAM AI.cmd"

    @api.get("/api/takeover")
    async def takeover_get():
        tk = app.takeover
        return {"paused": tk.paused, "waiting": tk.waiting}

    @api.post("/api/takeover")
    async def takeover_set(body: dict = Body(...)):
        """on=true: 'Take control' - the agent pauses before its next screen action until on=false (Resume)."""
        app.takeover.set_paused(bool(body.get("on")))
        return {"paused": app.takeover.paused}

    # ------------------------------------------------------------ Teach mode: record the user doing a task -> skill
    @api.get("/api/teach")
    async def teach_status():
        return app.teach.status()

    @api.post("/api/teach/start")
    async def teach_start():
        import asyncio as _asyncio

        try:
            return await _asyncio.to_thread(app.teach.start)
        except RuntimeError as e:
            raise HTTPException(409, str(e))

    @api.post("/api/teach/stop")
    async def teach_stop():
        import asyncio as _asyncio

        await _asyncio.to_thread(app.teach.stop)
        return {"steps": app.teach.review, "stopped_reason": app.teach.stopped_reason}

    @api.get("/api/teach/review")
    async def teach_review():
        """The finished recording waiting for review (so a page refresh does not lose it)."""
        return {"steps": app.teach.review}

    @api.post("/api/teach/restore")
    async def teach_restore(body: dict = Body(...)):
        """Put a reviewed recording back (e.g. after RAM AI restarted). Text lines only."""
        from .teach import clean_review

        if app.teach.recording:
            raise HTTPException(409, "Stop the current recording first.")
        app.teach.steps, app.teach.review = [], clean_review(body.get("steps"))
        return {"steps": len(app.teach.review)}

    @api.post("/api/teach/discard")
    async def teach_discard():
        app.teach.discard()
        return {"ok": True}

    @api.post("/api/teach/draft")
    async def teach_draft(body: dict = Body(...)):
        """Send ONLY the steps the user kept to the AI, which writes the skill for the user to check."""
        from .teach import clean_draft, draft_prompt, review_text, slug

        title = str(body.get("title") or "").strip()[:80]
        if len(title) < 3:
            raise HTTPException(400, "Give the task a name first (e.g. 'MB51 stock report').")
        steps = app.teach.review
        if not steps:
            raise HTTPException(409, "The recording is no longer in RAM AI (it was restarted). Press Teach and record the task again.")
        keep = {int(i) for i in body.get("keep") or [] if str(i).isdigit() and int(i) < len(steps)}
        if not keep:
            raise HTTPException(400, "No steps kept. Record the task again, or tick the steps to use.")
        desc = str(body.get("description") or "").strip()[:600]
        text = review_text(steps, keep)
        try:
            msg = await app.deepseek.chat(draft_prompt(title, desc, text), temperature=0.2)
        except ProviderError as e:
            raise HTTPException(502, str(e))
        content = clean_draft(msg.get("content") or "")
        if len(content) < 40:
            raise HTTPException(502, "The AI returned an empty skill. Try again.")
        return {"name": slug(title), "description": (desc or title)[:200], "content": content, "recording": text}

    @api.post("/api/teach/save")
    async def teach_save(body: dict = Body(...)):
        from .teach import slug
        from .tools.skills import user_dir

        name = slug(str(body.get("name") or ""))
        desc = re.sub(r"\s+", " ", str(body.get("description") or "")).strip()[:200]
        content = str(body.get("content") or "").strip()[:30000]
        recording = str(body.get("recording") or "").strip()[:20000]
        if len(desc) < 3 or len(content) < 40:
            raise HTTPException(400, "Name, description and skill text are needed.")
        p = user_dir() / f"{name}.md"
        if p.exists() and not body.get("overwrite"):
            raise HTTPException(409, f"A skill called '{name}' already exists. Tick 'Replace' or choose another name.")
        rec = f"\n\n## Recorded demonstration (for reference)\n```\n{recording}\n```\n" if recording else "\n"
        p.write_text(f"---\nname: {name}\ndescription: {desc}\ntaught: yes\n---\n{content}{rec}", encoding="utf-8")
        app.db.audit(None, app.device_name, "skill_saved", None, {"name": name, "taught": True})
        app.teach.steps, app.teach.review = [], []
        return {"saved": name, "path": str(p)}

    @api.post("/api/voice/listen")
    async def voice_listen(body: dict = Body(default={})):
        """Speak once into the default microphone (Windows speech); returns the words. Used by `ramai voice`."""
        import asyncio as _asyncio

        from . import voice

        lang = str(body.get("lang") or app.settings.get("voice_lang") or "en-IN")[:12]
        try:
            text = await _asyncio.to_thread(voice.listen, lang)
        except voice.VoiceError as e:
            raise HTTPException(409, str(e))
        return {"text": text}

    @api.get("/api/autostart")
    async def autostart_get():
        return {"enabled": _startup_file().exists()}

    @api.post("/api/autostart")
    async def autostart_set(body: dict = Body(...)):
        """Start RAM AI when you sign in to Windows (a shortcut in YOUR Startup folder; no admin, no system change)."""
        f = _startup_file()
        if body.get("enabled"):
            root = Path(__file__).resolve().parents[1]
            f.write_text(f'@echo off\r\ncd /d "{root}"\r\nstart "" /min "{root}\\.venv\\Scripts\\pythonw.exe" -m ramai\r\n', encoding="utf-8")
        elif f.exists():
            f.unlink()
        app.db.audit(None, app.device_name, "autostart", None, {"enabled": bool(body.get("enabled"))})
        return {"enabled": f.exists()}

    @api.get("/api/approvals")
    async def approvals():
        return app.approvals.pending()

    @api.post("/api/approvals/{aid}")
    async def decide(aid: str, body: dict = Body(...)):
        try:
            r = app.approvals.decide(aid, str(body.get("decision")), body.get("answer"), str(body.get("scope", "once")))
        except ValueError as e:
            err(e, 409)
        return {"status": r["status"]}

    @api.get("/api/audit")
    async def audit(limit: int = 200, task_id: str | None = None):
        if task_id:
            return app.db.query("SELECT * FROM audit WHERE task_id=? ORDER BY id DESC LIMIT ?", (task_id, limit))
        return app.db.query("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (min(limit, 1000),))

    @api.get("/api/screenshot")
    async def screenshot(fresh: bool = True, view: str = "agent"):
        from .tools.computer import grab, is_excluded

        h0 = app.work.current()
        if is_excluded(app, __import__("ramai.uia", fromlist=["x"]).window_title(h0) if h0 else None):
            raise HTTPException(403, "The window in front is on the exclusion list")
        def small():
            import ctypes
            import io

            from PIL import Image

            from . import uia as _uia
            from .capture import capture_window
            from .tools.computer import foreground_title

            if view == "agent":  # only the window the agent works in: no mirror of RAM AI itself
                h = app.work.current()
                cap = capture_window(h) if h else None
                if cap:
                    im, (l, t, r, b) = cap
                    im.thumbnail((1400, 1400))
                    buf = io.BytesIO()
                    im.save(buf, "JPEG", quality=70)
                    pt = ctypes.wintypes.POINT()
                    ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
                    return buf.getvalue(), {"width": r - l, "height": b - t, "left": l, "top": t}, pt.x - l, pt.y - t, _uia.window_title(h)
            png, info = grab(1)
            im = Image.open(io.BytesIO(png)).convert("RGB")
            from PIL import ImageDraw

            dr = ImageDraw.Draw(im)  # black out RAM AI's own window: no mirror even in full-screen mode
            for (l, t, r, b) in _uia.ramai_window_rects():
                box = (l - info["left"], t - info["top"], r - info["left"], b - info["top"])
                dr.rectangle(box, fill=(17, 19, 23))
                dr.text((box[0] + 24, box[1] + 24), "RAM AI (hidden in this view)", fill=(110, 118, 129))
            im.thumbnail((1400, 1400))
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=62)
            pt = ctypes.wintypes.POINT()
            ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
            return buf.getvalue(), info, pt.x - info["left"], pt.y - info["top"], foreground_title()

        import urllib.parse

        jpg, info, cx, cy, title = await asyncio.to_thread(small)
        return Response(jpg, media_type="image/jpeg", headers={
            "Cache-Control": "no-store", "X-Screen-W": str(info["width"]), "X-Screen-H": str(info["height"]),
            "X-Cursor-X": str(cx), "X-Cursor-Y": str(cy), "X-Window": urllib.parse.quote(title or ""),
            "X-Control": "1" if app.indicator.visible else "0"})

    # ---------------------------------------------------------------- memory / history / export
    @api.get("/api/memory")
    async def memory():
        return app.db.query("SELECT key,value FROM memory ORDER BY key")

    @api.put("/api/memory")
    async def memory_put(body: dict = Body(...)):
        v = str(body.get("value", ""))
        if redact(v) != v:
            raise HTTPException(400, "Looks like a secret; not stored")
        app.db.exec("INSERT OR REPLACE INTO memory(key,value,updated) VALUES(?,?,strftime('%s','now'))", (str(body["key"])[:80], v[:500]))
        return {"ok": True}

    @api.delete("/api/memory/{key}")
    async def memory_del(key: str):
        app.db.exec("DELETE FROM memory WHERE key=?", (key,))
        return {"ok": True}

    @api.delete("/api/history")
    async def clear_history():
        app.db.clear_history()
        return {"ok": True}

    @api.get("/api/export")
    async def export():
        s = public_settings()
        return {"settings": s, "memory": app.db.query("SELECT key,value FROM memory"),
                "tasks": app.db.query("SELECT * FROM tasks"), "audit": app.db.query("SELECT * FROM audit")}

    # ---------------------------------------------------------------- Outlook / WhatsApp
    @api.post("/api/outlook/login")
    async def ol_login():
        if not app.settings.get("ms_client_id"):
            raise HTTPException(400, "Enter your Microsoft app (client) ID in settings first")
        threading.Thread(target=app.outlook.login, daemon=True).start()
        return {"started": True}

    @api.post("/api/outlook/logout")
    async def ol_logout():
        app.outlook.logout()
        return {"ok": True}

    @api.post("/api/whatsapp/open")
    async def wa_open():
        from .engine import ToolContext

        try:
            return {"status": await app.whatsapp.status()}
        except Exception as e:
            return {"status": "error", "error": redact(f"{type(e).__name__}: {e}")[:300]}

    # ---------------------------------------------------------------- devices
    @api.get("/api/devices")
    async def devices():
        out = [{"id": app.settings.get("device_id"), "name": app.device_name, "this_pc": True, "online": True}]
        async def one(p):
            try:
                await peer_call(app, p, "ping", timeout=4)
                return {"id": p["id"], "name": p["name"], "this_pc": False, "online": True}
            except PolicyError as e:
                return {"id": p["id"], "name": p["name"], "this_pc": False, "online": False, "reason": str(e)[:80]}
        out += await asyncio.gather(*[one(p) for p in app.settings.get("peers", [])])
        return out

    @api.post("/api/remote/enable")
    async def remote_enable(body: dict = Body(...)):
        r = app.settings.get("remote")
        r.update({"bind": str(body.get("bind", "")).strip(), "port": int(body.get("port", 8766)),
                  "allow_screenshot": bool(body.get("allow_screenshot", False)), "enabled": True})
        try:
            app.settings.set("remote", r)
            fp = app.remote_server.start()
        except PolicyError as e:
            r["enabled"] = False
            app.settings.set("remote", r)
            err(e)
        except OSError as e:
            r["enabled"] = False
            app.settings.set("remote", r)
            err(e)
        return {"listening": app.remote_url(), "fingerprint": fp}

    @api.post("/api/remote/disable")
    async def remote_disable():
        app.remote_server.stop()
        r = app.settings.get("remote")
        r["enabled"] = False
        app.settings.set("remote", r)
        return {"ok": True}

    @api.post("/api/pairing/start")
    async def pairing_start():
        import time

        code = pysecrets.token_hex(10)
        app.pairing = {"code": code, "expires": time.time() + 600, "attempts": 0}
        return {"code": code, "expires_in": 600, "url": app.remote_url()}

    @api.post("/api/pairing/join")
    async def pairing_join(body: dict = Body(...)):
        try:
            p = await join_peer(app, str(body["url"]), str(body["code"]).strip())
        except (PolicyError, KeyError) as e:
            err(e)
        return {"paired": p["name"]}

    @api.delete("/api/peers/{pid}")
    async def peer_del(pid: str):
        app.remove_peer(pid)
        return {"ok": True}

    @api.post("/api/peers/{pid}/rpc")
    async def peer_rpc(pid: str, body: dict = Body(...)):
        if body.get("method") not in PEER_METHODS:
            raise HTTPException(400, "method not allowed")
        peer = next((p for p in app.settings.get("peers", []) if p["id"] == pid), None)
        if not peer:
            raise HTTPException(404)
        try:
            return await peer_call(app, peer, body["method"], body.get("params"))
        except PolicyError as e:
            err(e, 502)

    api.mount("/", StaticFiles(directory=str(UI_DIR), html=True), name="ui")
    return api
