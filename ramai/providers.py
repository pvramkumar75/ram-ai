"""DeepSeek reasoning provider and a separate, provider-neutral vision provider."""
from __future__ import annotations

import asyncio
import base64
import io
import re
from typing import Any

import httpx

from .core import SecretStore, Settings, redact


class ProviderError(Exception):
    pass


async def _post_with_retry(client: httpx.AsyncClient, url: str, headers: dict, payload: dict, retries: int = 3) -> dict:
    delay = 1.5
    last = "unknown error"
    for attempt in range(retries + 1):
        try:
            r = await client.post(url, headers=headers, json=payload)
            if r.status_code == 401 or (r.status_code == 403 and "api_key" in r.text.lower()):
                raise ProviderError(f"Invalid API key ({r.status_code}). Re-enter the key in Settings.")
            if r.status_code == 402:
                raise ProviderError("The AI provider says your account has no credits left (402). Top up your balance.")
            if r.status_code in (429, 500, 502, 503, 504):
                last = f"HTTP {r.status_code}"
                ra = r.headers.get("retry-after")
                await asyncio.sleep(min(float(ra), 30) if ra and ra.replace(".", "").isdigit() else delay)
                delay *= 2
                continue
            if r.status_code >= 400:
                raise ProviderError(f"HTTP {r.status_code}: {redact(r.text[:400])}")
            return r.json()
        except (httpx.TimeoutException, httpx.TransportError) as e:
            last = f"network error: {type(e).__name__} (is the server at {url.split('/chat')[0]} running?)"
            await asyncio.sleep(delay)
            delay *= 2
    raise ProviderError(f"Provider unavailable after retries ({last})")


# Every reasoning provider speaks the OpenAI-style /chat/completions + tools protocol. Add a new one by adding a row.
PROVIDERS: dict[str, dict] = {
    "deepseek": {"label": "DeepSeek", "url": "deepseek_base_url", "model": "deepseek_model", "secret": "deepseek", "needs_key": True},
    "openai": {"label": "OpenAI (GPT, GPT Luna)", "url": "openai_base_url", "model": "openai_model", "secret": "openai", "needs_key": True},
    "local": {"label": "Local model", "url": "local_base_url", "model": "local_model", "secret": "local_llm", "needs_key": False},
    "sarvam": {"label": "Sarvam AI", "url": "sarvam_base_url", "model": "sarvam_model", "secret": "sarvam", "needs_key": True,
               "auth": "sarvam", "max_tokens": 8192},
    "custom": {"label": "Other (OpenAI-compatible)", "url": "custom_base_url", "model": "custom_model", "secret": "custom_llm", "needs_key": False},
}
# Sarvam v2 has no model-list endpoint: its documented chat models (those marked * support tool calling, which the agent needs)
SARVAM_MODELS = ["deepseekv4.1-flash-1017", "deepseekv4.1-flash-1017-interactive", "glm5.3", "glm5.3-flash", "gemma4",
                 "deepseekv4-pro", "kimi-k3.1-pro", "qwen38-27b", "sarvam-105b", "sarvam-105b-conversations"]
SARVAM_NO_TOOLS = {"sarvam-105b", "sarvam-105b-conversations"}
LLM_SECRETS = {p["secret"] for p in PROVIDERS.values()}


class LLM:
    """The reasoning model. Switchable at any time between providers/models in Settings or the header switcher."""

    def __init__(self, settings: Settings, secrets: SecretStore):
        self.settings, self.secrets = settings, secrets
        self.usage = {"prompt_tokens": 0, "completion_tokens": 0, "calls": 0}
        self._no_temperature: set[str] = set()  # models that reject a custom temperature (e.g. newer OpenAI models)

    def provider(self, pid: str | None = None) -> str:
        p = pid or self.settings.get("llm_provider") or "deepseek"
        return p if p in PROVIDERS else "deepseek"

    def label(self, pid: str | None = None) -> str:
        p = self.provider(pid)
        return self.settings.get("custom_label") or PROVIDERS[p]["label"] if p == "custom" else PROVIDERS[p]["label"]

    def is_local(self) -> bool:
        return self.provider() == "local"

    def _cfg(self, pid=None) -> dict:
        return PROVIDERS[self.provider(pid)]

    def _key(self, pid=None) -> str:
        c = self._cfg(pid)
        k = self.secrets.get(c["secret"])
        if not k:
            if c["needs_key"]:
                raise ProviderError(f"{self.label(pid)} API key is not set. Open Settings → Reasoning provider and enter it.")
            return "none"
        return k

    def _base(self, pid=None) -> str:
        return (self.settings.get(self._cfg(pid)["url"]) or "").rstrip("/")

    def headers(self, pid=None) -> dict:
        key = self._key(pid)
        if self._cfg(pid).get("auth") == "sarvam":  # Sarvam documents both headers
            return {"api-subscription-key": key, "Authorization": f"Bearer {key}"}
        return {"Authorization": f"Bearer {key}"}

    def models(self, pid=None) -> list[str]:
        """The model box may list several models ('deepseek-flash, deepseek-v4-pro'): the FIRST is used, the others
        can be picked from the selector at the top. One name is ever sent to the provider."""
        out: list[str] = []
        for m in re.split(r"[,;\s]+", str(self.settings.get(self._cfg(pid)["model"]) or "")):
            if m and m not in out:
                out.append(m)
        return out

    def model(self, pid=None) -> str:
        ms = self.models(pid)
        return ms[0] if ms else ""

    def use_model(self, pid: str, model: str) -> None:
        """Make `model` the one in use (kept first); the other listed models stay available."""
        new = [m for m in re.split(r"[,;\s]+", model or "") if m]
        if len(new) == 1:
            new += [m for m in self.models(pid) if m != new[0]]
        if new:
            self.settings.set(self._cfg(pid)["model"], ", ".join(new)[:400])

    def ready(self, pid=None) -> bool:
        c = self._cfg(pid)
        if not self._base(pid):
            return False
        if c["needs_key"] and not self.secrets.has(c["secret"]):
            return False
        return bool(self.model(pid)) or c["needs_key"]  # cloud providers pick a model on first test

    def providers_status(self) -> list[dict]:
        return [{"id": pid, "label": self.label(pid), "model": self.model(pid), "models": self.models(pid),
                 "models_text": ", ".join(self.models(pid)), "base_url": self._base(pid),
                 "key_set": self.secrets.has(c["secret"]), "needs_key": c["needs_key"], "ready": self.ready(pid),
                 "active": pid == self.provider()} for pid, c in PROVIDERS.items()]

    async def chat(self, messages: list[dict], tools: list[dict] | None = None, temperature: float = 0.2) -> dict:
        """One model call. If the model in use keeps failing (overloaded, down, retired) and other models are listed in the
        settings box, the next one answers instead, so a task is not lost to one model's bad hour."""
        models = self.models()
        if not models:
            raise ProviderError(f"No model selected for {self.label()}. Open Settings, press Test & list models and choose one.")
        last: ProviderError | None = None
        for model in models:
            try:
                return await self._chat_with(model, messages, tools, temperature)
            except ProviderError as e:
                s = str(e)
                if "API key" in s or "credits" in s or "does not support tool" in s:   # another model will not fix these
                    raise
                last = e
        raise last or ProviderError("The AI provider did not answer")

    async def _chat_with(self, model: str, messages: list[dict], tools: list[dict] | None, temperature: float) -> dict:
        payload: dict[str, Any] = {"model": model, "messages": messages}
        if model not in self._no_temperature:
            payload["temperature"] = temperature
        if tools:
            payload["tools"] = tools
        cfg = self._cfg()
        if cfg.get("max_tokens"):  # e.g. Sarvam defaults to 2048 tokens INCLUDING reasoning, which cuts answers short
            payload["max_tokens"] = cfg["max_tokens"]
        if tools and cfg.get("auth") == "sarvam" and model in SARVAM_NO_TOOLS:
            raise ProviderError(f"Sarvam model '{model}' does not support tool calling, which RAM AI needs. "
                                "Choose deepseekv4.1-flash-1017, glm5.3, gemma4, kimi-k3.1-pro or qwen38-27b in Settings.")
        url, hdr = f"{self._base()}/chat/completions", self.headers()
        async with httpx.AsyncClient(timeout=httpx.Timeout(180, connect=15)) as c:
            try:
                data = await _post_with_retry(c, url, hdr, payload)
            except ProviderError as e:
                if "temperature" in str(e).lower() and "temperature" in payload:  # model only accepts its default
                    self._no_temperature.add(model)
                    payload.pop("temperature")
                    data = await _post_with_retry(c, url, hdr, payload)
                else:
                    raise
        u = data.get("usage") or {}
        self.usage["prompt_tokens"] += u.get("prompt_tokens", 0)
        self.usage["completion_tokens"] += u.get("completion_tokens", 0)
        self.usage["calls"] += 1
        try:
            return data["choices"][0]["message"]
        except (KeyError, IndexError):
            raise ProviderError(f"Unexpected response shape from {self.label()}")

    async def list_models(self, pid=None) -> list[str]:
        if self._cfg(pid).get("auth") == "sarvam":
            return [m for m in SARVAM_MODELS if m not in SARVAM_NO_TOOLS]
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.get(f"{self._base(pid)}/models", headers=self.headers(pid))
        if r.status_code == 401:
            raise ProviderError("Invalid API key (401)")
        if r.status_code >= 400:
            raise ProviderError(f"HTTP {r.status_code}")
        return sorted(m["id"] for m in r.json().get("data", []))

    async def test(self, pid=None) -> dict:
        p = self.provider(pid)
        if not self._base(p):
            raise ProviderError(f"Set the base URL for {self.label(p)} first")
        models = await self.list_models(p)
        if self._cfg(p).get("auth") == "sarvam":  # no list endpoint: prove the key works with a tiny real request
            m = self.model(p) or models[0]
            async with httpx.AsyncClient(timeout=httpx.Timeout(60, connect=15)) as c:
                await _post_with_retry(c, f"{self._base(p)}/chat/completions", self.headers(p),
                                       {"model": m, "messages": [{"role": "user", "content": "Reply with OK"}], "max_tokens": 64}, retries=1)
        return {"ok": True, "provider": p, "label": self.label(p), "models": models,
                "selected": self.model(p), "selected_available": self.model(p) in models}


DeepSeek = LLM  # backwards-compatible name


class Vision:
    """OpenAI-compatible image chat endpoint. Independent of DeepSeek; user supplies base URL/model/key."""

    def __init__(self, settings: Settings, secrets: SecretStore):
        self.settings, self.secrets = settings, secrets

    def ready(self) -> tuple[bool, str]:
        v = self.settings.get("vision")
        if not v.get("enabled"):
            return False, "Vision provider is disabled in Settings"
        if not v.get("consent"):
            return False, "You have not consented to sending screenshots to the vision provider"
        if not (v.get("base_url") and v.get("model") and self.secrets.has("vision")):
            return False, "Vision provider is not fully configured (base URL, model, API key)"
        return True, ""

    @staticmethod
    def encode(png_bytes: bytes, max_side: int = 1568) -> str:
        from PIL import Image

        im = Image.open(io.BytesIO(png_bytes)).convert("RGB")
        im.thumbnail((max_side, max_side))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=80)
        return base64.b64encode(buf.getvalue()).decode()

    async def describe(self, png_bytes: bytes, question: str) -> str:
        ok, why = self.ready()
        if not ok:
            raise ProviderError(why)
        v = self.settings.get("vision")
        b64 = self.encode(png_bytes)
        payload = {
            "model": v["model"],
            "max_tokens": 1200,
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": "Describe this Windows screenshot for an automation agent. List visible windows, "
                 "buttons, fields and their approximate pixel positions (x,y of centre in the image as shown, original "
                 "scale if known). Report text exactly. " + question},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
            ]}],
        }
        async with httpx.AsyncClient(timeout=httpx.Timeout(90, connect=15)) as c:
            data = await _post_with_retry(c, v["base_url"].rstrip("/") + "/chat/completions",
                                          {"Authorization": f"Bearer {self.secrets.get('vision')}"}, payload)
        try:
            return data["choices"][0]["message"]["content"]
        except (KeyError, IndexError):
            raise ProviderError("Unexpected vision response")

    async def test(self) -> dict:
        from PIL import Image

        buf = io.BytesIO()
        Image.new("RGB", (64, 64), (200, 30, 30)).save(buf, "PNG")
        text = await self.describe(buf.getvalue(), "What colour is this image?")
        return {"ok": True, "reply": text[:200]}
