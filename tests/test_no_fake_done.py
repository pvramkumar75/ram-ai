"""A task must not report actions (sent, saved, opened...) that it did not perform with tools in that task."""
import asyncio

import pytest

from .conftest import FakeDeepSeek, tool_call, wait_for


@pytest.mark.asyncio
async def test_claim_without_any_tool_call_is_sent_back(app, tmp_path):
    out = tmp_path / "note.txt"
    script = [{"role": "assistant", "content": "Sent to @me at 12:01 pm: Copper 12203.5"},        # copied from an earlier run
              tool_call("write_text_file", {"path": str(out), "content": "x"}, "c1"),
              {"role": "assistant", "content": "Saved the note."}]
    ds = FakeDeepSeek(script)
    app.deepseek, app.loop = ds, asyncio.get_running_loop()
    app.policy.add_root(str(tmp_path), write=True)
    tid = app.orchestrator.create("write the note", pre_approve=True)
    await wait_for(lambda: app.db.one("SELECT state FROM tasks WHERE id=?", (tid,))["state"] == "Completed", 10)
    assert "[RAM AI check]" in ds.calls[1][-1]["content"]            # the fake report was rejected
    assert app.db.one("SELECT result FROM tasks WHERE id=?", (tid,))["result"] == "Saved the note."


@pytest.mark.asyncio
async def test_plain_answer_without_tools_is_fine(app):
    ds = FakeDeepSeek([{"role": "assistant", "content": "MCP is the Model Context Protocol."}])
    app.deepseek, app.loop = ds, asyncio.get_running_loop()
    tid = app.orchestrator.create("what is MCP?")
    await wait_for(lambda: app.db.one("SELECT state FROM tasks WHERE id=?", (tid,))["state"] == "Completed")
    assert len(ds.calls) == 1


@pytest.mark.asyncio
async def test_history_is_background_not_previous_answers(app):
    app.db.exec("INSERT INTO tasks(id,instruction,state,result,created,updated) VALUES('old','send copper rate','Completed','Sent at 11:01',1,1)")
    ds = FakeDeepSeek([{"role": "assistant", "content": "MCP is a protocol."}])
    app.deepseek, app.loop = ds, asyncio.get_running_loop()
    app.orchestrator.create("what is MCP?")
    await wait_for(lambda: ds.calls)
    msgs = ds.calls[0]
    assert not any(m["role"] == "assistant" and "Sent at 11:01" in (m.get("content") or "") for m in msgs)
    assert any("BACKGROUND ONLY" in (m.get("content") or "") for m in msgs)
