"""Scheduled tasks: run an instruction once or on a recurring timetable.

Safety: a schedule carries a *standing approval* the user gave when creating it: the exact recipients it may send to
(email addresses / names / WhatsApp chats). At run time only sends to those recipients are auto-approved; every other
sensitive action still waits for the user. Runs are normal tasks (visible in the timeline and History)."""
from __future__ import annotations

import asyncio
import calendar
import ctypes
import datetime as dt
import json
import time
import uuid

DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
CATCH_UP = dt.timedelta(hours=2)      # run a missed schedule if we are at most this late
LOCKED_RETRY = dt.timedelta(minutes=5)
LOCKED_GIVE_UP = dt.timedelta(hours=2)

SCHEMA = """
CREATE TABLE IF NOT EXISTS schedules(
  id TEXT PRIMARY KEY, title TEXT, instruction TEXT, spec TEXT, allowed TEXT, enabled INTEGER,
  next_run REAL, last_run REAL, last_task TEXT, last_status TEXT, created REAL);
"""


def _hm(s: str) -> tuple[int, int]:
    h, m = (s or "09:00").strip().split(":")[:2]
    return int(h), int(m)


def next_run(spec: dict, after: dt.datetime) -> dt.datetime | None:
    """Next local time strictly after `after` matching the spec, or None (one-off already past)."""
    kind = spec.get("kind", "once")
    if kind == "once":
        t = dt.datetime.fromisoformat(f"{spec['date']} {spec.get('time', '09:00')}")
        return t if t > after else None
    if kind == "interval":
        mins = max(15, int(spec.get("every_minutes", 60)))
        start = dt.datetime.fromisoformat(spec["start"]) if spec.get("start") else after
        if start > after:
            return start
        n = int((after - start).total_seconds() // (mins * 60)) + 1
        return start + dt.timedelta(minutes=mins * n)
    h, m = _hm(spec.get("time"))
    day = after.replace(hour=h, minute=m, second=0, microsecond=0)
    for i in range(0, 400):
        cand = day + dt.timedelta(days=i)
        if cand <= after:
            continue
        wd = DAYS[cand.weekday()]
        if kind == "daily":
            return cand
        if kind == "weekdays" and cand.weekday() < 5:
            return cand
        if kind == "weekly" and wd in [d.lower()[:3] for d in spec.get("days", ["mon"])]:
            return cand
        if kind == "monthly":
            dom = int(spec.get("day_of_month", 1))
            last = calendar.monthrange(cand.year, cand.month)[1]
            if cand.day == min(dom, last):
                return cand
    return None


def describe(spec: dict) -> str:
    k = spec.get("kind")
    t = spec.get("time", "")
    if k == "once":
        return f"once on {spec.get('date')} at {t}"
    if k == "daily":
        return f"every day at {t}"
    if k == "weekdays":
        return f"every weekday (Mon-Fri) at {t}"
    if k == "weekly":
        return f"every {', '.join(d.capitalize() for d in spec.get('days', []))} at {t}"
    if k == "monthly":
        return f"monthly on day {spec.get('day_of_month')} at {t}"
    if k == "interval":
        mins = int(spec.get("every_minutes", 60))
        return f"every {mins // 60} h" if mins % 60 == 0 else f"every {mins} min"
    return k or "?"


def session_locked() -> bool:
    """True when the Windows session is locked (apps cannot be driven then)."""
    u = ctypes.windll.user32
    h = u.OpenInputDesktop(0, False, 0x0100)  # DESKTOP_SWITCHDESKTOP
    if not h:
        return True
    try:
        return not u.SwitchDesktop(h)
    finally:
        u.CloseDesktop(h)


class Scheduler:
    def __init__(self, app):
        self.app = app
        self.app.db.conn.executescript(SCHEMA)
        self.app.db.conn.commit()
        self._task: asyncio.Task | None = None
        self._locked_since: dict[str, float] = {}

    # ---------------- CRUD
    def create(self, title: str, instruction: str, spec: dict, allowed: list[str], enabled: bool = True) -> dict:
        nr = next_run(spec, dt.datetime.now())
        if nr is None:
            raise ValueError("That time is already in the past")
        sid = uuid.uuid4().hex[:8]
        self.app.db.exec("INSERT INTO schedules(id,title,instruction,spec,allowed,enabled,next_run,created) VALUES(?,?,?,?,?,?,?,?)",
                         (sid, title[:80], instruction, json.dumps(spec), json.dumps(allowed), 1 if enabled else 0, nr.timestamp(), time.time()))
        self.app.db.audit(None, self.app.device_name, "schedule_created", None, {"id": sid, "title": title, "when": describe(spec), "allowed": allowed})
        return self.get(sid)

    def get(self, sid: str) -> dict | None:
        r = self.app.db.one("SELECT * FROM schedules WHERE id=?", (sid,))
        return self._fmt(r) if r else None

    def list(self) -> list[dict]:
        return [self._fmt(r) for r in self.app.db.query("SELECT * FROM schedules ORDER BY enabled DESC, next_run")]

    def _fmt(self, r: dict) -> dict:
        spec = json.loads(r["spec"])
        return {**r, "spec": spec, "allowed": json.loads(r["allowed"] or "[]"), "when": describe(spec),
                "next_run_text": dt.datetime.fromtimestamp(r["next_run"]).strftime("%a %d %b %Y, %H:%M") if r["next_run"] else "—",
                "last_run_text": dt.datetime.fromtimestamp(r["last_run"]).strftime("%a %d %b, %H:%M") if r["last_run"] else "never"}

    def set_enabled(self, sid: str, on: bool) -> None:
        r = self.app.db.one("SELECT spec FROM schedules WHERE id=?", (sid,))
        nr = next_run(json.loads(r["spec"]), dt.datetime.now()) if (r and on) else None
        self.app.db.exec("UPDATE schedules SET enabled=?, next_run=COALESCE(?, next_run) WHERE id=?", (1 if on else 0, nr.timestamp() if nr else None, sid))

    def delete(self, sid: str) -> None:
        self.app.db.exec("DELETE FROM schedules WHERE id=?", (sid,))
        self.app.db.audit(None, self.app.device_name, "schedule_deleted", None, {"id": sid})

    # ---------------- running
    def run_now(self, sid: str, reason: str = "manual") -> str:
        s = self.get(sid)
        if not s:
            raise ValueError("Unknown schedule")
        tid = self.app.orchestrator.create(
            f"[Scheduled: {s['title']} — {s['when']}]\n{s['instruction']}", self.app.device_name,
            pre_approve=False, schedule={"id": sid, "allowed": s["allowed"], "title": s["title"]})
        self.app.db.exec("UPDATE schedules SET last_run=?, last_task=?, last_status=? WHERE id=?", (time.time(), tid, f"started ({reason})", sid))
        return tid

    def _advance(self, s: dict, now: dt.datetime) -> None:
        nr = next_run(s["spec"], now)
        if nr is None:  # one-off finished
            self.app.db.exec("UPDATE schedules SET enabled=0, next_run=NULL WHERE id=?", (s["id"],))
        else:
            self.app.db.exec("UPDATE schedules SET next_run=? WHERE id=?", (nr.timestamp(), s["id"]))

    def tick(self) -> None:
        now = dt.datetime.now()
        for s in self.list():
            if not s["enabled"] or not s["next_run"] or s["next_run"] > now.timestamp():
                continue
            due = dt.datetime.fromtimestamp(s["next_run"])
            if now - due > CATCH_UP:  # PC was off / RAM AI not running for too long: skip this occurrence
                self.app.db.exec("UPDATE schedules SET last_status=? WHERE id=?", (f"missed {due:%d %b %H:%M} (RAM AI was not running)", s["id"]))
                self.app.db.audit(None, self.app.device_name, "schedule_missed", None, {"id": s["id"], "due": due.isoformat()})
                self._advance(s, now)
                continue
            if session_locked():  # apps cannot be driven on a locked screen: wait and retry
                first = self._locked_since.setdefault(s["id"], time.time())
                if time.time() - first > LOCKED_GIVE_UP.total_seconds():
                    self.app.db.exec("UPDATE schedules SET last_status=? WHERE id=?", ("skipped: PC stayed locked", s["id"]))
                    self._locked_since.pop(s["id"], None)
                    self._advance(s, now)
                else:
                    self.app.db.exec("UPDATE schedules SET last_status=? WHERE id=?", ("waiting: PC is locked, will retry", s["id"]))
                continue
            self._locked_since.pop(s["id"], None)
            self._advance(s, now)
            try:
                self.run_now(s["id"], "on schedule")
            except Exception as e:
                self.app.db.exec("UPDATE schedules SET last_status=? WHERE id=?", (f"error: {e}"[:200], s["id"]))

    async def loop(self) -> None:
        while True:
            try:
                self.tick()
            except Exception:
                pass
            await asyncio.sleep(20)

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.get_running_loop().create_task(self.loop())
