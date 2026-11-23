import hashlib
import json
import time

import pytest
from fastapi.testclient import TestClient

from ramai.remote import _hmac, build_remote_app, sign_headers
from ramai.server import create_api


@pytest.fixture()
def client(app):
    api = create_api(app, "TESTTOKEN", 8765)
    with TestClient(api, base_url="http://127.0.0.1:8765") as c:
        yield c


def test_api_requires_token(client):
    assert client.get("/api/status").status_code == 401
    assert client.get("/api/status", headers={"X-RAM-Token": "wrong"}).status_code == 401
    r = client.get("/api/status", headers={"X-RAM-Token": "TESTTOKEN"})
    assert r.status_code == 200 and r.json()["configured"] is True
    assert "sk-test" not in r.text  # secrets never returned


def test_host_header_rebinding_blocked(client):
    r = client.get("/api/status", headers={"X-RAM-Token": "TESTTOKEN", "Host": "evil.example.com"})
    assert r.status_code == 403
    r = client.get("/api/status", headers={"X-RAM-Token": "TESTTOKEN", "Origin": "http://evil.example.com"})
    assert r.status_code == 403


def test_ui_served_without_secrets(client):
    r = client.get("/")
    assert r.status_code == 200 and "RAM AI" in r.text and "TESTTOKEN" not in r.text


def test_settings_whitelist(client, app):
    h = {"X-RAM-Token": "TESTTOKEN"}
    r = client.post("/api/settings", headers=h, json={"device_id": "hacked", "roots": [{"path": "C:/", "write": True}], "max_steps": 5})
    assert r.json()["applied"] == ["max_steps"]
    assert app.settings.get("device_id") != "hacked" and app.settings.get("roots") == []


def test_secret_endpoint_never_echoes(client):
    h = {"X-RAM-Token": "TESTTOKEN"}
    r = client.post("/api/secrets/deepseek", headers=h, json={"value": "sk-newkey-123456789"})
    assert r.status_code == 200 and "newkey" not in r.text
    assert client.post("/api/secrets/other", headers=h, json={"value": "x" * 20}).status_code == 404


def test_roots_api_blocks_system(client):
    h = {"X-RAM-Token": "TESTTOKEN"}
    assert client.post("/api/roots", headers=h, json={"path": "C:\\Windows"}).status_code == 400


# ------------------------------------------------------------ remote (two-PC) auth
@pytest.fixture()
def remote(app):
    app.add_peer({"id": "peer1", "name": "Other PC", "url": "", "fingerprint": ""}, "SECRET" * 8)
    return TestClient(build_remote_app(app), base_url="https://127.0.0.1:8766")


def post(c, body, secret="SECRET" * 8, dev="peer1", mutate=None):
    raw = json.dumps(body).encode()
    h = sign_headers(dev, secret, raw)
    if mutate:
        mutate(h)
    return c.post("/remote/rpc", content=raw, headers=h)


def test_remote_unauthenticated_rejected(remote):
    assert remote.post("/remote/rpc", json={"method": "ping"}).status_code == 401


def test_remote_good_and_replay(remote):
    raw = json.dumps({"method": "ping"}).encode()
    h = sign_headers("peer1", "SECRET" * 8, raw)
    assert remote.post("/remote/rpc", content=raw, headers=h).status_code == 200
    assert remote.post("/remote/rpc", content=raw, headers=h).status_code == 401  # replay


def test_remote_bad_signature_unknown_device_stale(remote):
    assert post(remote, {"method": "ping"}, secret="WRONG" * 8).status_code == 401
    assert post(remote, {"method": "ping"}, dev="stranger").status_code == 401
    assert post(remote, {"method": "ping"}, mutate=lambda h: h.update({"X-Timestamp": str(int(time.time()) - 3600)})).status_code == 401


def test_remote_tampered_body_rejected(remote):
    raw = json.dumps({"method": "ping"}).encode()
    h = sign_headers("peer1", "SECRET" * 8, raw)
    assert remote.post("/remote/rpc", content=json.dumps({"method": "state"}).encode(), headers=h).status_code == 401


def test_remote_screenshot_off_by_default_and_unknown_method(remote):
    assert post(remote, {"method": "screenshot"}).status_code == 403
    assert post(remote, {"method": "run_shell", "params": {"cmd": "calc"}}).status_code == 400


def test_remote_file_integrity(remote, app):
    import base64

    data = b"hello report"
    ok = post(remote, {"method": "receive_file", "params": {"name": "..\\..\\evil.txt", "sha256": hashlib.sha256(data).hexdigest(), "data_b64": base64.b64encode(data).decode()}})
    assert ok.status_code == 200
    bad = post(remote, {"method": "receive_file", "params": {"name": "a.txt", "sha256": "0" * 64, "data_b64": base64.b64encode(data).decode()}})
    assert bad.status_code == 400
    stored = list((app.data_path / "inbox").rglob("*evil.txt"))
    assert stored and stored[0].parent.parent.name == "inbox"  # path traversal stripped


def test_pairing_requires_code_and_approval(remote, app):
    r = remote.post("/remote/pair", json={"device_id": "x", "name": "n", "nonce": "n", "proof": "p"})
    assert r.status_code == 403  # no active code
    app.pairing = {"code": "c0de", "expires": time.time() + 60, "attempts": 0}
    r = remote.post("/remote/pair", json={"device_id": "x", "name": "n", "nonce": "n", "proof": "wrong"})
    assert r.status_code == 403


def test_bootstrap_only_same_origin_browser_fetch(client):
    assert client.get("/bootstrap").status_code == 403  # plain/cross-site request: refused
    assert client.get("/bootstrap", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
    r = client.get("/bootstrap", headers={"Sec-Fetch-Site": "same-origin"})
    assert r.status_code == 200 and r.json()["token"] == "TESTTOKEN"
    assert client.get("/bootstrap", headers={"Sec-Fetch-Site": "same-origin", "Host": "evil.com"}).status_code == 403


# ------------------------------------------------------------ switchable AI providers
def test_provider_switch_and_keys(client, app):
    h = {"X-RAM-Token": "TESTTOKEN"}
    assert client.post("/api/secrets/openai", headers=h, json={"value": "sk-openai-test-123456"}).status_code == 200
    r = client.post("/api/llm/select", headers=h, json={"provider": "openai", "model": "gpt-6-luna"}).json()
    assert r == {"provider": "openai", "model": "gpt-6-luna", "ready": True}
    st = client.get("/api/status", headers=h).json()
    assert st["llm"] == {"provider": "openai", "label": "OpenAI (GPT, GPT Luna)", "model": "gpt-6-luna"}
    assert "sk-openai" not in str(st)
    assert app.deepseek._base() == "https://api.openai.com/v1"
    assert client.post("/api/llm/select", headers=h, json={"provider": "evil"}).status_code == 400
    client.post("/api/llm/select", headers=h, json={"provider": "deepseek"})
    assert app.deepseek.model() == app.settings.get("deepseek_model")


@pytest.mark.asyncio
async def test_temperature_fallback(app, monkeypatch):
    from ramai import providers

    sent = []

    async def fake_post(c, url, headers, payload, retries=3):
        sent.append(dict(payload))
        if "temperature" in payload:
            raise providers.ProviderError("HTTP 400: Unsupported value: 'temperature' does not support 0.2")
        return {"choices": [{"message": {"content": "hi"}}]}

    monkeypatch.setattr(providers, "_post_with_retry", fake_post)
    app.secrets.set("openai", "sk-openai-test-123456")
    app.settings.update_user({"llm_provider": "openai", "openai_model": "gpt-6-luna"})
    assert (await app.deepseek.chat([{"role": "user", "content": "x"}]))["content"] == "hi"
    assert "temperature" in sent[0] and "temperature" not in sent[1]
    await app.deepseek.chat([{"role": "user", "content": "y"}])
    assert "temperature" not in sent[2]  # remembered for this model


@pytest.mark.asyncio
async def test_sarvam_provider(app, monkeypatch):
    import httpx

    from ramai import providers

    app.secrets.set("sarvam", "sk_sarvam_test_123456")
    app.settings.update_user({"llm_provider": "sarvam"})
    assert app.deepseek.ready() and app.deepseek._base() == "https://api.sarvam.ai/v2"
    h = app.deepseek.headers()
    assert h["api-subscription-key"] == "sk_sarvam_test_123456" and h["Authorization"].startswith("Bearer ")
    models = await app.deepseek.list_models()
    assert "deepseekv4.1-flash-1017" in models and "sarvam-105b" not in models  # only tool-capable models offered
    sent = []

    async def fake_post(c, url, headers, payload, retries=3):
        sent.append((url, headers, payload))
        return {"choices": [{"message": {"content": "ok"}}]}

    monkeypatch.setattr(providers, "_post_with_retry", fake_post)
    await app.deepseek.chat([{"role": "user", "content": "x"}], tools=[{"type": "function", "function": {"name": "a", "parameters": {}}}])
    url, headers, payload = sent[-1]
    assert url == "https://api.sarvam.ai/v2/chat/completions" and payload["max_tokens"] == 8192 and "api-subscription-key" in headers
    app.settings.update_user({"sarvam_model": "sarvam-105b"})
    with pytest.raises(providers.ProviderError, match="tool calling"):
        await app.deepseek.chat([{"role": "user", "content": "x"}], tools=[{"type": "function", "function": {"name": "a", "parameters": {}}}])


@pytest.mark.asyncio
async def test_403_invalid_key_message():
    import httpx

    from ramai.providers import ProviderError, _post_with_retry

    class C:
        async def post(self, *a, **k):
            return httpx.Response(403, text='{"error":{"code":"invalid_api_key_error"}}')

    with pytest.raises(ProviderError, match="Invalid API key"):
        await _post_with_retry(C(), "u", {}, {})
