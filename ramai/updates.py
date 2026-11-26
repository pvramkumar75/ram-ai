"""Tells the user when a newer RAM AI release exists (so friends know to update). One anonymous request to the public
releases page every few hours - nothing about the user or this PC is sent. Setting: update_check."""
from __future__ import annotations

import asyncio
import re
import subprocess
import sys
from pathlib import Path

import httpx

from .core import VERSION

LATEST = "https://api.github.com/repos/pvramkumar75/ram-ai/releases/latest"
EVERY = 6 * 3600
ROOT = Path(__file__).resolve().parents[1]


def parts(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", v or "")[:3]) or (0,)


def newer(tag: str, current: str = VERSION) -> bool:
    return parts(tag) > parts(current)


class Updates:
    def __init__(self, app, client: httpx.AsyncClient | None = None):
        self.app = app
        self.client = client           # tests inject a fake GitHub here
        self.available: dict | None = None
        self.task: asyncio.Task | None = None

    async def check(self) -> dict | None:
        c = self.client or httpx.AsyncClient(timeout=20)
        try:
            r = await c.get(LATEST, headers={"User-Agent": "ramai-update-check", "Accept": "application/vnd.github+json"})
            if r.status_code != 200:
                return self.available
            d = r.json()
            tag = str(d.get("tag_name") or "")
            self.available = ({"version": tag.lstrip("v"), "url": str(d.get("html_url") or "")} if newer(tag) else None)
        except (httpx.HTTPError, ValueError):
            pass
        finally:
            if not self.client:
                await c.aclose()
        if self.available:
            await self.announce(self.available["version"])
        return self.available

    async def announce(self, version: str) -> None:
        """Once per new version: also say so on the paired phone."""
        tg = getattr(self.app, "telegram", None)
        if tg and tg.cfg().get("notified") != version:
            if await tg.alert(f"⬆ RAM AI {version} is available (you have {VERSION}). Open RAM AI on the PC and press Update now, "
                              "or run:  ramai update"):
                tg.save(notified=version)

    async def loop(self) -> None:
        await asyncio.sleep(20)
        while True:
            if self.app.settings.get("update_check", True):
                try:
                    await self.check()
                except Exception:
                    pass
            await asyncio.sleep(EVERY)

    def start(self) -> None:
        if self.task is None:
            self.task = asyncio.get_running_loop().create_task(self.loop())

    def run_update(self) -> None:
        """Start the normal updater in its own visible window (the user pressed the button)."""
        exe = Path(sys.executable)
        python = exe.with_name("python.exe") if exe.name.lower() == "pythonw.exe" else exe
        subprocess.Popen([str(python), "-c", "from ramai.cli import cmd_update; cmd_update(None); input('Press Enter to close')"],
                         cwd=str(ROOT), creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0))
