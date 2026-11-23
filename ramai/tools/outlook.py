"""Outlook via Microsoft Graph (delegated OAuth through MSAL; tokens DPAPI-protected). No passwords stored.

Requires the user's own Entra ID app registration (public client, redirect http://localhost) - see docs/GUIDES.md (section 3).
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
import threading
from pathlib import Path

import httpx
from pydantic import Field

from ..policy import PolicyError, Risk
from .base import Args, Registry, Tool

GRAPH = "https://graph.microsoft.com/v1.0"
SCOPES = ["Mail.ReadWrite", "Mail.Send", "User.Read"]  # offline_access is added by MSAL automatically
ID_RE = re.compile(r"^[A-Za-z0-9_\-=]{8,700}$")
MAX_ATTACH = 3 * 1024 * 1024


class OutlookAuth:
    def __init__(self, app):
        self.app = app
        self._msal = None
        self._lock = threading.Lock()
        self.login_state = {"status": "signed_out", "error": None}

    def _client(self):
        import msal

        cid = self.app.settings.get("ms_client_id")
        if not cid:
            raise PolicyError("Microsoft client ID is not configured (Settings → Outlook)")
        with self._lock:
            if self._msal is None or self._msal[0] != cid:
                cache = msal.SerializableTokenCache()
                blob = self.app.secrets.get("msal_cache")
                if blob:
                    cache.deserialize(blob)
                cli = msal.PublicClientApplication(
                    cid, authority=f"https://login.microsoftonline.com/{self.app.settings.get('ms_tenant') or 'common'}",
                    token_cache=cache)
                self._msal = (cid, cli, cache)
            return self._msal[1], self._msal[2]

    def _persist(self, cache) -> None:
        if cache.has_state_changed:
            self.app.secrets.set("msal_cache", cache.serialize())

    def token(self) -> str:
        cli, cache = self._client()
        accts = cli.get_accounts()
        if not accts:
            raise PolicyError("Outlook is not signed in. Open Settings → Outlook → Sign in.")
        r = cli.acquire_token_silent(SCOPES, account=accts[0])
        self._persist(cache)
        if not r or "access_token" not in r:
            raise PolicyError("Outlook session expired. Please sign in again in Settings.")
        return r["access_token"]

    def login(self) -> None:
        """Interactive browser sign-in (official Microsoft flow; handles MFA/Conditional Access). Blocking: run in a thread."""
        try:
            self.login_state = {"status": "waiting", "error": None}
            cli, cache = self._client()
            r = cli.acquire_token_interactive(SCOPES, prompt="select_account")
            self._persist(cache)
            if "access_token" in r:
                self.login_state = {"status": "signed_in", "error": None}
            else:
                self.login_state = {"status": "error", "error": str(r.get("error_description", r.get("error")))[:300]}
        except Exception as e:
            self.login_state = {"status": "error", "error": f"{type(e).__name__}: {e}"[:300]}

    def account(self) -> str | None:
        try:
            cli, _ = self._client()
            a = cli.get_accounts()
            return a[0]["username"] if a else None
        except Exception:
            return None

    def logout(self) -> None:
        try:
            cli, _ = self._client()
            for a in cli.get_accounts():
                cli.remove_account(a)
        except Exception:
            pass
        self.app.secrets.delete("msal_cache")
        self._msal = None
        self.login_state = {"status": "signed_out", "error": None}


async def graph(ctx, method: str, path: str, **kw):
    tok = ctx.app.outlook.token()
    hdrs = {"Authorization": f"Bearer {tok}", **kw.pop("headers", {})}
    async with httpx.AsyncClient(timeout=60) as c:
        r = await c.request(method, GRAPH + path, headers=hdrs, **kw)
    if r.status_code == 429:
        raise PolicyError("Microsoft Graph rate limit; try again shortly")
    if r.status_code >= 400:
        raise PolicyError(f"Graph error {r.status_code}: {r.text[:300]}")
    return r.json() if r.content else {}


def _id(v: str) -> str:
    if not ID_RE.match(v):
        raise PolicyError("Invalid message/attachment id")
    return v


