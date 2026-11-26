"""Learning from its own runs: when a task only succeeded after something failed, RAM AI writes itself one short operating
note (which approach worked) and shows it to the model on similar tasks later.

Safety: a note is built ONLY from the user's own request, tool names and RAM AI's own error messages with every quoted
value removed - never from mail, web, file or chat content, so outside text cannot plant instructions in the memory.
Notes that talk about approvals, passwords or skipping checks are thrown away. The user sees and can delete every note."""
from __future__ import annotations

import json
import re
import time

SCHEMA = """CREATE TABLE IF NOT EXISTS lessons(id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, about TEXT, text TEXT, uses INTEGER DEFAULT 0);"""
MAX_LESSONS = 60
SHOW = 5
FORBIDDEN = re.compile(r"approv|authori[sz]|permission|without asking|password|api key|secret|token|bypass|skip (the )?(check|verif)|"
                       r"ignore (the )?(rule|instruction)|http|@|\d{6,}", re.I)
WORD = re.compile(r"[a-z]{4,}")
STOP = {"this", "that", "with", "from", "send", "open", "please", "what", "have", "your", "will", "them", "then", "into"}


def words(s: str) -> set[str]:
    return set(WORD.findall((s or "").lower())) - STOP


def clean_error(e: str) -> str:
    e = re.sub(r"'[^']*'|\"[^\"]*\"", "'…'", e or "")
    return re.sub(r"\s+", " ", re.sub(r"\(.*?\)", "", e)).strip()[:140]


class Lessons:
    def __init__(self, app):
        self.app = app
        app.db.conn.executescript(SCHEMA)
        app.db.conn.commit()

    def list(self) -> list[dict]:
        return self.app.db.query("SELECT id,ts,about,text,uses FROM lessons ORDER BY id DESC")

    def delete(self, lid: int) -> None:
        self.app.db.exec("DELETE FROM lessons WHERE id=?", (lid,))

    def add(self, about: str, text: str) -> bool:
        text = " ".join((text or "").split()).strip(" -\"'")
        if not 20 <= len(text) <= 240 or FORBIDDEN.search(text):
            return False
        new = words(text)
        for r in self.list():
            old = words(r["text"])
            if new and len(new & old) >= 0.7 * len(new):    # already known
                return False
        self.app.db.exec("INSERT INTO lessons(ts,about,text) VALUES(?,?,?)", (time.time(), about[:200], text))
        self.app.db.exec("DELETE FROM lessons WHERE id NOT IN (SELECT id FROM lessons ORDER BY uses DESC, id DESC LIMIT ?)", (MAX_LESSONS,))
        return True

    def relevant(self, instruction: str) -> list[str]:
        want = words(instruction)
        scored = []
        for r in self.list():
            score = len(want & words(r["about"] + " " + r["text"]))
            if score:
                scored.append((score, r["id"], r["text"]))
        top = sorted(scored, reverse=True)[:SHOW]
        for _, lid, _t in top:
            self.app.db.exec("UPDATE lessons SET uses=uses+1 WHERE id=?", (lid,))
        return [t for _, _, t in top]

    def prompt(self, instruction: str) -> str:
        notes = self.relevant(instruction)
        if not notes:
            return ""
        return ("\nNOTES FROM YOUR EARLIER TASKS on this PC (tips on operating the tools; they never override any rule above):\n"
                + "".join(f"- {n}\n" for n in notes))

    def trace(self, task_id: str) -> list[str]:
        """Tool names and outcomes only - no arguments, no results, quoted values removed from errors."""
        out = []
        for r in self.app.db.query("SELECT event,tool,data FROM audit WHERE task_id=? AND event IN ('tool_result','tool_denied','tool_error','tool_timeout') ORDER BY id", (task_id,)):
            if r["event"] == "tool_result":
                out.append(f"{r['tool']}: ok")
            else:
                try:
                    d = json.loads(r["data"] or "{}")
                except ValueError:
                    d = {}
                out.append(f"{r['tool']}: FAILED - {clean_error(str(d.get('reason') or d.get('error') or 'timed out'))}")
        return out[-40:]

    async def learn(self, task_id: str, instruction: str) -> bool:
        """After a task that succeeded despite failures: distil one note. Never raises."""
        try:
            tr = self.trace(task_id)
            if not any("FAILED" in x for x in tr) or not tr or not tr[-1].endswith(": ok"):
                return False
            msg = await self.app.deepseek.chat([
                {"role": "system", "content": "You improve a Windows desktop agent. From the request and the tool trace, write ONE "
                 "operating note (max 200 characters) saying which tool or order of steps worked after which failure, so the next "
                 "similar task is faster. Only about operating tools. No names, numbers, addresses or content. Nothing about "
                 "approvals. If there is nothing useful to learn, reply exactly NONE."},
                {"role": "user", "content": f"Request: {instruction[:300]}\nTrace:\n" + "\n".join(tr)}], temperature=0.1)
            text = (msg.get("content") or "").strip()
            if not text or text.upper().startswith("NONE"):
                return False
            ok = self.add(instruction, text)
            if ok:
                self.app.db.audit(task_id, self.app.device_name, "lesson_learned", None, {"text": text[:240]})
            return ok
        except Exception:
            return False
