import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@pytest.fixture()
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    monkeypatch.setenv("RAM_AI_HOME", str(h))
    return h


@pytest.fixture()
def app(home, tmp_path):
    from ramai.app import App

    a = App(indicator=False)
    out = tmp_path / "out"
    out.mkdir()
    a.settings.set("output_dir", str(out))
    a.settings.set("device_name", "Test PC")
    a.settings.set("user_name", "Test User")
    a.secrets.set("deepseek", "sk-test-0123456789abcdef")
    return a


@pytest.fixture()
def docs(tmp_path, app):
    d = tmp_path / "docs"
    d.mkdir()
    app.policy.add_root(str(d), write=False)
    return d


class FakeDeepSeek:
    """Scripted model: list of assistant messages (dicts)."""

    def __init__(self, script):
        self.script, self.calls, self.usage = list(script), [], {"prompt_tokens": 0, "completion_tokens": 0, "calls": 0}

    async def chat(self, messages, tools=None, temperature=0.2):
        self.calls.append(json.loads(json.dumps(messages)))
        return self.script.pop(0)


def tool_call(name, args, cid="c1"):
    return {"role": "assistant", "content": "", "tool_calls": [
        {"id": cid, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]}


async def wait_for(pred, timeout=5.0):
    end = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < end:
        v = pred()
        if v:
            return v
        await asyncio.sleep(0.02)
    raise AssertionError("timeout waiting for condition")
