import asyncio
import time

import ramai.takeover as tk_mod
from ramai.takeover import Takeover


class Ctx:
    task_id, device = "t1", "PC"

    def __init__(self):
        self.stop = False

    def stopped(self):
        return self.stop


def test_no_user_input_means_no_pause():
    tk = Takeover()
    tk.mark()
    assert asyncio.run(tk.gate(Ctx(), "click")) is False


def test_waits_until_the_user_is_idle(monkeypatch):
    monkeypatch.setattr(tk_mod, "IDLE_SECONDS", 0.4)
    monkeypatch.setattr(tk_mod, "MOVE_SETTLE", 0.1)
    tk = Takeover()
    tk.mark()
    tk.last_action = time.monotonic() + 0.01       # the user clicked after the agent's last step
    t0 = time.monotonic()
    assert asyncio.run(tk.gate(Ctx(), "click")) is True
    assert time.monotonic() - t0 >= 0.35           # waited for the idle period
    assert not tk.user_intervened()                # marked: the same click does not pause again


def test_take_control_waits_for_resume():
    tk = Takeover()
    tk.set_paused(True)

    async def run():
        async def resume_later():
            await asyncio.sleep(0.3)
            tk.set_paused(False)
        asyncio.get_running_loop().create_task(resume_later())
        return await tk.gate(Ctx(), "type_text")

    t0 = time.monotonic()
    assert asyncio.run(run()) is True and time.monotonic() - t0 >= 0.25 and not tk.paused


def test_stop_ends_the_wait():
    tk = Takeover()
    tk.set_paused(True)
    c = Ctx()
    c.stop = True
    assert asyncio.run(tk.gate(c, "click")) is True
