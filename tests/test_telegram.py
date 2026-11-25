import json
import time

import httpx
import pytest

from ramai.telegram import Telegram, chunks

TOKEN = "123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw1"
OWNER = 5550001


class FakeTelegram:
    """Stands in for api.telegram.org: records every call, hands out scripted updates."""

    def __init__(self):
        self.calls, self.updates = [], []

    def handler(self, request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content) if request.content and request.headers["content-type"].startswith("application/json") else {}
        self.calls.append((method, body))
        if method == "getMe":
            return httpx.Response(200, json={"ok": True, "result": {"id": 77, "username": "ram_test_bot"}})
        if method == "getUpdates":
            ups, self.updates = self.updates, []
            return httpx.Response(200, json={"ok": True, "result": ups})
        if method == "sendMessage":
            return httpx.Response(200, json={"ok": True, "result": {"message_id": len(self.calls) + 100}})
        return httpx.Response(200, json={"ok": True, "result": True})

    def sent(self, method="sendMessage"):
        return [b for m, b in self.calls if m == method]

    def texts(self):
        return [b["text"] for b in self.sent()]


def msg(text, uid=OWNER, chat_type="private", age=0, reply_to=None, uid_n=[0]):
    uid_n[0] += 1
    m = {"message_id": uid_n[0], "from": {"id": uid, "first_name": "Ram"}, "chat": {"id": uid, "type": chat_type},
         "date": int(time.time() - age), "text": text}
    if reply_to:
        m["reply_to_message"] = {"message_id": reply_to}
    return {"update_id": 1000 + uid_n[0], "message": m}


@pytest.fixture()
def tg(app):
    fake = FakeTelegram()
    t = Telegram(app, httpx.AsyncClient(transport=httpx.MockTransport(fake.handler)))
    t.fake = fake
    return t


async def paired(tg, app):
    await tg.connect(TOKEN)
    tg.stop()
    tg.save(chat_id=OWNER, who="Ram")
    tg.fake.calls.clear()
    return tg


async def test_connect_validates_token_and_keeps_it_encrypted(tg, app):
    with pytest.raises(Exception, match="bot token"):
        await tg.connect("not a token")
    st = await tg.connect(TOKEN)
    tg.stop()
    assert st["enabled"] and st["bot"] == "ram_test_bot" and not st["paired"]
    assert app.secrets.get("telegram_bot") == TOKEN
    assert TOKEN not in json.dumps(app.settings.data)          # never written to settings.json


async def test_pairing_needs_the_code_and_only_works_once(tg, app):
    await tg.connect(TOKEN)
    tg.stop()
    await tg.handle(msg("hello"))                                # nobody paired, no code requested: silent
    assert tg.fake.texts() == []
    code = tg.start_pairing()["code"]
    await tg.handle(msg("WRONGCODE", uid=999))
    assert "not right" in tg.fake.texts()[-1] and not tg.status()["paired"]
    await tg.handle(msg(f"/start {code.lower()}", uid=OWNER))   # deep link form, any case
    assert tg.status()["paired"] and tg.cfg()["chat_id"] == OWNER and "Paired" in tg.fake.texts()[-1]
    n = len(tg.fake.calls)
    await tg.handle(msg(code, uid=4242))                         # stranger after pairing: ignored, code is spent
    assert len(tg.fake.calls) == n and tg.cfg()["chat_id"] == OWNER


async def test_pairing_locks_after_five_wrong_tries(tg, app):
    await tg.connect(TOKEN)
    tg.stop()
    code = tg.start_pairing()["code"]
    for _ in range(6):
        await tg.handle(msg("AAAAAAAA", uid=999))
    await tg.handle(msg(code, uid=999))
    assert not tg.status()["paired"] and tg.pairing is None


async def test_strangers_groups_and_old_messages_never_run(tg, app, monkeypatch):
    await paired(tg, app)
    made = []
    monkeypatch.setattr(app.orchestrator, "create", lambda *a, **k: made.append(a) or "t1")
    await tg.handle(msg("delete everything", uid=666))                       # another person
    await tg.handle(msg("hi", chat_type="group"))                            # a group
    await tg.handle(msg("send the report", age=3600))                        # sent while RAM AI was off
    assert made == [] and "minutes old" in tg.fake.texts()[-1]
    assert any(r["event"] == "telegram_rejected" for r in app.db.query("SELECT event FROM audit"))


async def test_a_message_becomes_a_task_and_the_result_comes_back(tg, app, monkeypatch):
    await paired(tg, app)
    app.settings.set("device_name", "Test PC")
    made = {}

    def fake_create(ins, device=None, *a, **k):
        made["ins"] = ins
        app.db.exec("INSERT INTO tasks(id,instruction,device,state,created,updated) VALUES('tsk1',?,?,?,?,?)", (ins, device, "Running", time.time(), time.time()))
        return "tsk1"

    monkeypatch.setattr(app.orchestrator, "create", fake_create)
    await tg.handle(msg("Send the copper rate to Ravi"))
    assert made["ins"] == "Send the copper rate to Ravi" and "tsk1".startswith("tsk") and "On it" in tg.fake.texts()[-1]
    await tg.watch_once()
    assert "tsk1" in tg.tracked                                              # still running: nothing sent yet
    app.db.exec("UPDATE tasks SET state='Completed',result='Copper is 912.5' WHERE id='tsk1'")
    await tg.watch_once()
    assert tg.fake.texts()[-1].endswith("Copper is 912.5") and "tsk1" not in tg.tracked


async def test_a_failed_send_keeps_the_result_until_it_gets_through(tg, app):
    await paired(tg, app)
    app.db.exec("INSERT INTO tasks(id,instruction,device,state,error,created,updated) VALUES('tsk2','x','d','Failed','Outlook closed',1,1)")
    tg.tracked["tsk2"] = time.time()
    good = tg.client._transport.handler
    tg.client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(502, text="bad gateway")))
    with pytest.raises(Exception):
        await tg.watch_once()
    assert "tsk2" in tg.tracked
    tg.client = httpx.AsyncClient(transport=httpx.MockTransport(good))
    await tg.watch_once()
    assert "Outlook closed" in tg.fake.texts()[-1] and "tsk2" not in tg.tracked


async def seed_approval(app, aid="ap1", kind="approval", tool="send_email", risk="high"):
    app.db.exec("INSERT INTO approvals(id,task_id,kind,tool,args_hash,summary,risk,status,created) VALUES(?,?,?,?,?,?,?,?,?)",
                (aid, "tsk", kind, tool, "h", "SEND EMAIL to ravi@x.com", risk, "pending", time.time()))


async def test_approval_buttons_decide_the_real_approval(tg, app, monkeypatch):
    await paired(tg, app)
    await seed_approval(app)
    decided = []
    monkeypatch.setattr(app.approvals, "decide", lambda aid, d, a=None, s="once": decided.append((aid, d, s)) or {"status": d})
    await tg.watch_once()
    sent = tg.fake.sent()[-1]
    buttons = [b["callback_data"] for row in sent["reply_markup"]["inline_keyboard"] for b in row]
    assert "SEND EMAIL" in sent["text"] and buttons == ["ay:ap1", "an:ap1", "at:ap1"]
    await tg.watch_once()
    assert len(tg.fake.sent()) == 1                                           # not asked twice
    await tg.handle({"update_id": 1, "callback_query": {"id": "c1", "from": {"id": 666}, "data": "ay:ap1"}})
    assert decided == []                                                      # a stranger's tap does nothing
    await tg.handle({"update_id": 2, "callback_query": {"id": "c2", "from": {"id": OWNER}, "data": "ay:ap1"}})
    assert decided == [("ap1", "approve", "once")]
    assert "✅ Approved" in tg.fake.sent("editMessageText")[-1]["text"] and "ap1" not in tg.shown


async def test_high_risk_can_be_kept_for_the_pc_and_pairing_never_goes_to_the_phone(tg, app, monkeypatch):
    await paired(tg, app)
    tg.save(phone_high_risk=False)
    await seed_approval(app)
    await seed_approval(app, "ap2", tool="pair_device", risk="high")
    await seed_approval(app, "ap3", tool="list_windows", risk="low")
    decided = []
    monkeypatch.setattr(app.approvals, "decide", lambda aid, d, a=None, s="once": decided.append(aid) or {"status": d})
    await tg.watch_once()
    by = {b["text"].split("\n")[2][:10]: b for b in tg.fake.sent()}
    assert all("reply_markup" not in b for b in tg.fake.sent() if "SEND EMAIL" in b["text"] and "PC" in b["text"])
    low = [b for b in tg.fake.sent() if "reply_markup" in b]
    assert len(low) == 1                                                      # only the low-risk one has buttons
    await tg.handle({"update_id": 3, "callback_query": {"id": "c", "from": {"id": OWNER}, "data": "ay:ap1"}})
    await tg.handle({"update_id": 4, "callback_query": {"id": "c", "from": {"id": OWNER}, "data": "ay:ap2"}})
    assert decided == []                                                      # even a forged tap is refused


async def test_questions_are_answered_by_replying(tg, app, monkeypatch):
    await paired(tg, app)
    await seed_approval(app, "q1", kind="question", tool="ask_user", risk="low")
    got = []
    monkeypatch.setattr(app.approvals, "decide", lambda aid, d, a=None, s="once": got.append((aid, d, a)) or {"status": d})
    await tg.watch_once()
    mid = tg.shown["q1"]["mid"]
    assert tg.fake.sent()[-1]["reply_markup"]["force_reply"]
    await tg.handle(msg("Rs 912", reply_to=mid))
    assert got == [("q1", "approve", "Rs 912")]


async def test_approval_handled_on_the_pc_removes_the_buttons(tg, app):
    await paired(tg, app)
    await seed_approval(app)
    await tg.watch_once()
    app.db.exec("UPDATE approvals SET status='approved' WHERE id='ap1'")
    await tg.watch_once()
    assert "handled on the PC" in tg.fake.sent("editMessageText")[-1]["text"]


async def test_commands_and_emergency_stop(tg, app, monkeypatch):
    await paired(tg, app)
    stopped = []
    monkeypatch.setattr(app.orchestrator, "emergency_stop", lambda: stopped.append(1))
    await tg.handle(msg("/stop"))
    await tg.handle(msg("/status"))
    await tg.handle(msg("/screenshot"))
    await tg.handle(msg("/nonsense"))
    t = tg.fake.texts()
    assert stopped and "Stopped" in t[0] and "Nothing is running" in t[1] and "switched off" in t[2] and "don't know" in t[3]
    await tg.handle(msg("/unpair"))
    assert not tg.status()["paired"]
    await tg.handle(msg("anything"))                                          # no longer the owner
    assert "anything" not in " ".join(tg.fake.texts())


async def test_polling_remembers_its_position_and_never_replays(tg, app, monkeypatch):
    await paired(tg, app)
    made = []
    monkeypatch.setattr(app.orchestrator, "create", lambda *a, **k: made.append(a[0]) or "t9")
    tg.fake.updates = [msg("do the thing")]
    off = tg.fake.updates[0]["update_id"] + 1
    assert await tg.poll_once() == 1 and made == ["do the thing"] and tg.cfg()["offset"] == off
    await tg.poll_once()
    assert tg.fake.sent("getUpdates")[-1]["offset"] == off                    # asks only for newer updates


def test_chunking_and_secret_store_isolation(app):
    assert chunks("a" * 9000, 4000, 3)[0] == "a" * 4000 and len(chunks("a" * 99999)) == 3
    assert "trimmed" in chunks("a" * 99999)[-1]
    assert "telegram" not in __import__("ramai.core", fromlist=["x"]).USER_EDITABLE
    assert app.settings.update_user({"telegram": {"chat_id": 1}}) == []      # the settings API cannot add an owner


def test_endpoints(app, monkeypatch):
    from fastapi.testclient import TestClient

    from ramai.server import create_api

    fake = FakeTelegram()
    app.telegram.client = httpx.AsyncClient(transport=httpx.MockTransport(fake.handler))
    monkeypatch.setattr(app.telegram, "start", lambda: None)                 # no background polling in this test
    h ={"X-RAM-Token": "T"}
    with TestClient(create_api(app, "T", 8765), base_url="http://127.0.0.1:8765") as c:
        assert c.get("/api/telegram", headers=h).json()["enabled"] is False
        assert c.post("/api/telegram/pair", headers=h).status_code == 400
        assert c.post("/api/telegram/connect", headers=h, json={"token": "bad"}).status_code == 400
        r = c.post("/api/telegram/connect", headers=h, json={"token": TOKEN})
        assert r.status_code == 200 and r.json()["bot"] == "ram_test_bot"
        p = c.post("/api/telegram/pair", headers=h).json()
        assert p["link"] == f"https://t.me/ram_test_bot?start={p['code']}"
        assert c.post("/api/telegram/test", headers=h).status_code == 409
        assert c.post("/api/telegram/options", headers=h, json={"allow_screenshot": True}).json()["allow_screenshot"] is True
        assert c.post("/api/telegram/disconnect", headers=h).json()["enabled"] is False
        assert app.secrets.get("telegram_bot") is None
