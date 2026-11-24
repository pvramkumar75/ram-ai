"""Skills library: markdown playbooks the agent loads when relevant. Built-in skills ship with RAM AI; new skills (written by
the user or proposed by the agent) live in %APPDATA%\\RAM_AI\\skills and are saved only after the user approves them."""
from __future__ import annotations

import re
from pathlib import Path

from pydantic import Field

from ..core import data_dir
from ..policy import PolicyError, Risk
from .base import Args, Registry, Tool

BUILTIN = Path(__file__).resolve().parents[1] / "skills"
NAME_RE = re.compile(r"^[a-z0-9_]{3,40}$")


def user_dir() -> Path:
    d = data_dir() / "skills"
    d.mkdir(exist_ok=True)
    return d


def _meta(p: Path) -> dict:
    txt = p.read_text(encoding="utf-8", errors="replace")
    m = re.search(r"^---\s*\n(.*?)\n---\s*\n", txt, re.S)
    meta = {"name": p.stem, "description": ""}
    if m:
        for line in m.group(1).splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                meta[k.strip()] = v.strip()
    meta["source"] = "built-in" if p.parent == BUILTIN else "yours"
    meta["path"] = str(p)
    return meta


def all_skills() -> list[dict]:
    found: dict[str, dict] = {}
    for d in (BUILTIN, user_dir()):
        for p in sorted(d.glob("*.md")):
            found[p.stem] = _meta(p)  # user skills override built-ins with the same name
    return list(found.values())


def skills_brief() -> str:
    return "\n".join(f"- {s['name']}: {s['description']}" + (" (TAUGHT by the user doing it)" if s.get("taught") else "")
                     for s in all_skills()) or "(none)"


class LoadArgs(Args):
    name: str = Field(max_length=40)


class SaveArgs(Args):
    name: str = Field(pattern=NAME_RE.pattern, description="lowercase_with_underscores")
    description: str = Field(min_length=10, max_length=200)
    content: str = Field(min_length=50, max_length=30000, description="Markdown playbook: when to use, steps, code templates, pitfalls")


class NoArgs(Args):
    pass


def t_list(a, ctx):
    return {"skills": [{k: s[k] for k in ("name", "description", "source")} for s in all_skills()]}


def t_load(a: LoadArgs, ctx):
    s = next((x for x in all_skills() if x["name"] == a.name), None)
    if not s:
        raise PolicyError(f"No skill named '{a.name}'. Available: {[x['name'] for x in all_skills()]}")
    return {"name": a.name, "content": Path(s["path"]).read_text(encoding="utf-8", errors="replace")[:30000]}


def _save_preview(a: SaveArgs, ctx):
    async def _():
        return {"text": f"SAVE NEW SKILL '{a.name}' (it will guide future tasks)\nDescription: {a.description}\n--- content ---\n{a.content[:5000]}",
                "digest": None}
    return _()


def t_save(a: SaveArgs, ctx):
    p = user_dir() / f"{a.name}.md"
    p.write_text(f"---\nname: {a.name}\ndescription: {a.description}\n---\n{a.content}", encoding="utf-8")
    ctx.app.db.audit(ctx.task_id, ctx.device, "skill_saved", None, {"name": a.name})
    return {"saved": a.name, "path": str(p)}


def register(reg: Registry) -> None:
    reg.register(Tool("list_skills", "List available skills (playbooks for advanced Excel/Word/PowerPoint/Power BI and any user-added skills).", NoArgs, t_list, Risk.READ, 5))
    reg.register(Tool("load_skill", "Load a skill's full playbook (steps + code templates) before doing that kind of task.", LoadArgs, t_load, Risk.READ, 5))
    reg.register(Tool("save_skill", "Save a reusable skill/playbook for future tasks (the user approves the content).", SaveArgs, t_save, Risk.HIGH, 10,
                      preview=_save_preview, reversible=True))
