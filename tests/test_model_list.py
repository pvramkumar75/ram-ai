def test_several_models_first_is_used_and_switchable(app):
    llm = app.deepseek
    app.settings.set("llm_provider", "deepseek")
    app.settings.set("deepseek_model", "deepseek-flash, deepseek-v4-pro")
    assert llm.model() == "deepseek-flash"                       # only ONE name is ever sent to the provider
    assert llm.models() == ["deepseek-flash", "deepseek-v4-pro"]
    llm.use_model("deepseek", "deepseek-v4-pro")                  # picked at the top: becomes first, the other stays
    assert llm.model() == "deepseek-v4-pro" and llm.models() == ["deepseek-v4-pro", "deepseek-flash"]
    app.settings.set("deepseek_model", " deepseek-flash ;deepseek-flash  deepseek-v4-pro,")
    assert llm.models() == ["deepseek-flash", "deepseek-v4-pro"]
    st = next(p for p in llm.providers_status() if p["id"] == "deepseek")
    assert st["model"] == "deepseek-flash" and st["models_text"] == "deepseek-flash, deepseek-v4-pro"


def test_select_endpoint_reorders(app):
    from fastapi.testclient import TestClient

    from ramai.server import create_api

    app.settings.set("deepseek_model", "deepseek-flash, deepseek-v4-pro")
    with TestClient(create_api(app, "T", 8765), base_url="http://127.0.0.1:8765") as c:
        r = c.post("/api/llm/select", headers={"X-RAM-Token": "T"}, json={"provider": "deepseek", "model": "deepseek-v4-pro"})
    assert r.json()["model"] == "deepseek-v4-pro" and app.settings.get("deepseek_model") == "deepseek-v4-pro, deepseek-flash"
