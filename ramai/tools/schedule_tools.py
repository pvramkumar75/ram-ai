"""Agent tools to create, list and delete scheduled tasks."""
from __future__ import annotations

import datetime as dt
from typing import Literal

from pydantic import Field

from ..policy import PolicyError, Risk
from .base import Args, Registry, Tool


class CreateArgs(Args):
    title: str = Field(max_length=80, description="Short name, e.g. 'Daily price update to my manager'")
    instruction: str = Field(min_length=10, max_length=4000, description="Complete, self-contained instruction to run each time")
    kind: Literal["once", "daily", "weekdays", "weekly", "monthly", "interval"]
    time: str = Field("09:00", pattern=r"^\d{1,2}:\d{2}$", description="Local time HH:MM (24h)")
    date: str | None = Field(None, pattern=r"^\d{4}-\d{2}-\d{2}$", description="For kind=once: YYYY-MM-DD")
    days: list[Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"]] = Field(default_factory=list, description="For kind=weekly")
    day_of_month: int | None = Field(None, ge=1, le=31, description="For kind=monthly")
    every_minutes: int | None = Field(None, ge=15, le=10080, description="For kind=interval (min 15)")
    allowed_recipients: list[str] = Field(default_factory=list, max_length=20,
                                          description="Emails / WhatsApp chat names this schedule may send to WITHOUT asking each time")


class IdArgs(Args):
    id: str = Field(max_length=20)


class NoArgs(Args):
    pass


def _spec(a: CreateArgs) -> dict:
    spec = {"kind": a.kind, "time": a.time}
    if a.kind == "once":
        if not a.date:
            raise PolicyError("kind=once needs a date (YYYY-MM-DD)")
        spec["date"] = a.date
    if a.kind == "weekly":
        if not a.days:
            raise PolicyError("kind=weekly needs days, e.g. ['mon']")
        spec["days"] = a.days
    if a.kind == "monthly":
        spec["day_of_month"] = a.day_of_month or 1
    if a.kind == "interval":
        spec["every_minutes"] = a.every_minutes or 60
        spec["start"] = dt.datetime.now().replace(second=0, microsecond=0).isoformat()
    return spec


def _preview(a: CreateArgs, ctx):
    async def _():
        from ..scheduler import describe, next_run

        spec = _spec(a)
        nr = next_run(spec, dt.datetime.now())
        if nr is None:
            raise PolicyError("That date/time is already in the past")
        rc = ", ".join(a.allowed_recipients) or "nobody (any send will ask you first)"
        text = (f"CREATE SCHEDULE: {a.title}\nWhen: {describe(spec)}  (first run {nr:%a %d %b %Y, %H:%M})\n"
                f"May send WITHOUT asking to: {rc}\n--- instruction it will run ---\n{a.instruction}")
        return {"text": text, "digest": None}

    return _()


def t_create(a: CreateArgs, ctx):
    s = ctx.app.scheduler.create(a.title, a.instruction, _spec(a), a.allowed_recipients)
    return {"created": s["id"], "title": s["title"], "when": s["when"], "next_run": s["next_run_text"], "allowed_recipients": s["allowed"],
            "note": "RAM AI must be running (and the PC unlocked) at that time. Settings → 'Start RAM AI with Windows' helps."}


def t_list(a, ctx):
    return {"schedules": [{k: s[k] for k in ("id", "title", "when", "next_run_text", "last_run_text", "last_status", "enabled", "allowed")}
                          | {"instruction": s["instruction"][:300]} for s in ctx.app.scheduler.list()]}


def t_delete(a: IdArgs, ctx):
    if not ctx.app.scheduler.get(a.id):
        raise PolicyError("No schedule with that id (use schedule_list)")
    ctx.app.scheduler.delete(a.id)
    return {"deleted": a.id}


def register(reg: Registry) -> None:
    reg.register(Tool("schedule_create", "Schedule a task to run later or repeatedly (once/daily/weekdays/weekly/monthly/interval). The user approves it once, including who it may send to.",
                      CreateArgs, t_create, Risk.HIGH, 20, preview=_preview, reversible=True))
    reg.register(Tool("schedule_list", "List scheduled tasks with next run time and last result.", NoArgs, t_list, Risk.READ, 10))
    reg.register(Tool("schedule_delete", "Delete a scheduled task by id.", IdArgs, t_delete, Risk.LOW, 10))
