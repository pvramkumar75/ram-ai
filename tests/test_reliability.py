"""Things that must never go wrong again: tasks reported as done when they were not, the same action twice, the same
failure retried for minutes, two tasks fighting over the screen, a chat read with the speakers swapped."""
import asyncio
import json
import time

import pytest
from PIL import Image, ImageDraw

from ramai.engine import SCREEN_WAIT, ToolContext, compact_messages, family
from ramai.ocr import Line
from ramai.policy import PolicyError, Risk
from ramai.providers import LLM, ProviderError
from ramai.tools import whatsapp as wa
from ramai.tools.base import Args, Tool

from .conftest import FakeDeepSeek, tool_call, wait_for


# ------------------------------------------------------------ WhatsApp: who said what, which chat is open
def test_chat_read_tells_who_said_what_from_the_bubble_colour():
    img = Image.new("RGB", (800, 400), (239, 234, 226))                    # wallpaper
    d = ImageDraw.Draw(img)
    d.rectangle((20, 40, 400, 100), fill=(255, 255, 255))                  # incoming: white
    d.rectangle((420, 130, 780, 190), fill=(217, 253, 211))                # outgoing: green
    d.rectangle((20, 220, 400, 300), fill=(255, 255, 255))
    lines = [Line("Have you tested again?", 40, 50, 200, 20), Line("8:36 am", 330, 76, 60, 14),
             Line("Hi Wan, let me check", 440, 140, 200, 20), Line("6:41 am", 700, 166, 60, 14),
             Line("Today", 380, 200, 40, 14),
             Line("Next we try to improve", 40, 230, 220, 20), Line("the master batch material", 40, 258, 220, 20), Line("8:37 am", 330, 280, 60, 14)]
    msgs = wa.classify_messages(lines, (img, (0, 0, 800, 400)), 10)
    assert [(m["from"], m.get("time")) for m in msgs] == [("them", "8:36 am"), ("you", "6:41 am"), ("system", None), ("them", "8:37 am")]
    assert msgs[1]["text"] == "Hi Wan, let me check"
    assert msgs[3]["text"] == "Next we try to improve\nthe master batch material"      # one bubble, two lines
    assert wa.classify_messages(lines, None, 10)[0]["from"] == "unknown"             # no picture: never guess a speaker


def test_dark_theme_bubbles_too():
    img = Image.new("RGB", (400, 200), (11, 20, 26))
    d = ImageDraw.Draw(img)
    d.rectangle((10, 20, 200, 80), fill=(32, 44, 51))                      # incoming dark grey
    d.rectangle((210, 100, 390, 160), fill=(5, 92, 75))                    # outgoing dark green
    lines = [Line("from him", 30, 40, 100, 20), Line("from me", 230, 120, 100, 20)]
    assert [m["from"] for m in wa.classify_messages(lines, (img, (0, 0, 400, 200)))] == ["them", "you"]


def test_open_chat_is_confirmed_from_windows_own_name_even_when_ocr_sees_nothing():
    d = wa.Desktop(None)
    d.uia_chat_name = lambda: "Wan"
    d.ocr_header = lambda: ""                                           # the screen strip read blank, as in the failed task
    assert d.header_ok("Wan") == "Wan"
    d.uia_chat_name = lambda: "Wanga Gmee Solar"                        # a DIFFERENT chat is open: never matches
    d.ocr_header = lambda: ""
    assert d.header_ok("Wan") is None
    d.uia_chat_name = lambda: None                                      # accessibility unavailable: OCR still works
    d.ocr_header = lambda: "Wan"
    assert d.header_ok("Wan") == "Wan"


# ------------------------------------------------------------ outcomes are honest
async def test_failed_send_is_not_reported_as_done(app):
    """The Wan task: the chat could not be confirmed, so nothing was sent - that is NOT 'Completed'."""
    def boom(a, ctx):
        raise PolicyError("Clicked 'Wan' twice but cannot confirm that chat is open")

    class A(Args):
        text: str

    app.registry.register(Tool("fake_send", "send", A, boom, Risk.HIGH, 5, reversible=False))
    app.deepseek = FakeDeepSeek([tool_call("fake_send", {"text": "hi"}), {"role": "assistant", "content": "I could not send it."}])
    app.loop = asyncio.get_running_loop()
    app.settings.set("device_name", "Test PC")
    tid = app.orchestrator.create("send hi", pre_approve=True)
    await wait_for(lambda: app.db.one("SELECT state FROM tasks WHERE id=?", (tid,))["state"] in ("Completed", "Incomplete"))
    assert app.db.one("SELECT state FROM tasks WHERE id=?", (tid,))["state"] == "Incomplete"


async def test_a_retry_that_works_clears_the_warning(app):
    ctx = ToolContext(app, "t", "d")
    app.executor._note_blocked(ctx, "whatsapp_send_message", "chat not confirmed")
    assert ctx.scratch["blocked"]
    app.executor._note_blocked(ctx, "whatsapp_send_prepared", None)      # same family, it really went out
    assert not ctx.scratch["blocked"] and family("whatsapp_send_prepared") == family("whatsapp_open")


async def test_empty_final_answer_is_asked_for_again(app):
    ds = FakeDeepSeek([{"role": "assistant", "content": ""}, {"role": "assistant", "content": "All 3 files are in the folder."}])
    app.deepseek, app.loop = ds, asyncio.get_running_loop()
    tid = app.orchestrator.create("check the folder")
    await wait_for(lambda: app.db.one("SELECT state FROM tasks WHERE id=?", (tid,))["state"] == "Completed")
    assert app.db.one("SELECT result FROM tasks WHERE id=?", (tid,))["result"] == "All 3 files are in the folder."


async def test_decisions_record_where_they_were_made(app):
    app.deepseek, app.loop = FakeDeepSeek([tool_call("delete_file", {"path": str(app.policy.settings.get("output_dir")) + "/nothing.txt"})]), asyncio.get_running_loop()
    app.orchestrator.create("delete it")
    ap = await wait_for(lambda: app.approvals.pending())
    app.approvals.decide(ap[0]["id"], "reject", None, "once", "phone")
    row = app.db.one("SELECT data FROM audit WHERE event='approval_rejected'")
    assert json.loads(row["data"])["via"] == "phone"


# ------------------------------------------------------------ no repeats, no loops
async def test_same_request_twice_runs_once(app):
    import pathlib

    f = pathlib.Path(app.settings.get("output_dir")) / "keep.txt"
    f.write_text("x")
    app.deepseek, app.loop = FakeDeepSeek([tool_call("delete_file", {"path": str(f)}, "c1")]), asyncio.get_running_loop()
    a = app.orchestrator.create("send the copper rate to Ravi")
    await wait_for(lambda: app.approvals.pending())                       # still working, waiting for approval
    b = app.orchestrator.create("  Send the copper RATE to Ravi ")
    assert a == b and len(app.db.query("SELECT id FROM tasks")) == 1
    app.orchestrator.emergency_stop()


async def test_the_same_failure_is_not_retried_forever(app):
    calls = []

    def bad(a, ctx):
        calls.append(1)
        raise PolicyError("Clicked 'Wan' but the open chat header shows ''. Nothing typed.")

    class A(Args):
        n: int = 0

    app.registry.register(Tool("whatsapp_fake", "x", A, bad, Risk.READ, 5))
    ctx = ToolContext(app, "t", "d")
    outs = [await app.executor.execute(ctx, "whatsapp_fake", json.dumps({"n": i})) for i in range(5)]
    assert len(calls) == 3                                                 # the 4th and 5th never ran
    assert "same error happened twice" in outs[1][0] and "switched off" in outs[2][0] and "switched off" in outs[3][0]
    other = await app.executor.execute(ctx, "calculate", json.dumps({"expression": "1+1"}))
    assert other[1] is True                                                # other tools are unaffected


def test_long_history_is_shortened_but_the_newest_results_stay_whole():
    msgs = [{"role": "system", "content": "s"}] + [{"role": "tool", "content": "x" * 30000} for _ in range(20)]
    assert compact_messages(msgs) == 6
    assert len(msgs[1]["content"]) < 800 and len(msgs[-1]["content"]) == 30000 and len(msgs[-14]["content"]) == 30000
    small = [{"role": "tool", "content": "y" * 500} for _ in range(30)]
    assert compact_messages(small) == 0


async def test_one_task_at_a_time_on_the_screen(app):
    tool = Tool("ui_click", "x", Args, lambda a, c: None)
    c1, c2 = ToolContext(app, "t1", "d"), ToolContext(app, "t2", "d")
    assert await app.executor._screen(c1, tool, "ui_click") is None and c1.scratch["screen"]
    second = asyncio.create_task(app.executor._screen(c2, tool, "ui_click"))
    await asyncio.sleep(0.1)
    assert not second.done()                                               # t2 waits while t1 has the screen
    app.executor.release_screen(c1)
    assert await asyncio.wait_for(second, 2) is None and c2.scratch["screen"]
    app.executor.release_screen(c2)
    assert not app.orchestrator.screen_lock.locked()
    assert await app.executor._screen(ToolContext(app, "t3", "d"), Tool("calculate", "x", Args, lambda a, c: None), "calculate") is None  # no screen needed
    assert SCREEN_WAIT >= 60


# ------------------------------------------------------------ the AI model
async def test_a_failing_model_falls_back_to_the_next_listed_one(app, monkeypatch):
    llm = app.deepseek
    app.settings.set("deepseek_model", "model-a, model-b")
    tried = []

    async def fake(self, model, messages, tools, temperature):
        tried.append(model)
        if model == "model-a":
            raise ProviderError("Provider unavailable after retries (HTTP 503)")
        return {"role": "assistant", "content": "ok from " + model}

    monkeypatch.setattr(LLM, "_chat_with", fake)
    assert (await llm.chat([{"role": "user", "content": "hi"}]))["content"] == "ok from model-b" and tried == ["model-a", "model-b"]

    tried.clear()

    async def bad_key(self, model, messages, tools, temperature):
        tried.append(model)
        raise ProviderError("Invalid API key (401). Re-enter the key in Settings.")

    monkeypatch.setattr(LLM, "_chat_with", bad_key)
    with pytest.raises(ProviderError):
        await llm.chat([{"role": "user", "content": "hi"}])
    assert tried == ["model-a"]                                            # another model cannot fix a bad key
