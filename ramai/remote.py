"""Secure two-PC connectivity: pairing (code + explicit approval), HMAC-signed RPC with replay protection,
self-signed TLS with certificate pinning, per-peer revocation. Intended to run over Tailscale/private VPN only."""
from __future__ import annotations

import asyncio
import base64
import datetime as dt
import hashlib
import hmac
import ipaddress
import json
import secrets as pysecrets
import ssl
import threading
import time
import uuid
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request
from pydantic import Field

from .core import VERSION, data_dir
from .engine import ToolContext, args_hash
from .policy import PolicyError, Risk
from .tools.base import Args, Registry, Tool

MAX_FILE = 25 * 1024 * 1024
SKEW = 60


def _hmac(key: str | bytes, msg: str) -> str:
    k = key.encode() if isinstance(key, str) else key
    return hmac.new(k, msg.encode(), hashlib.sha256).hexdigest()


def sign_headers(device_id: str, secret: str, body: bytes) -> dict:
    ts, nonce = str(int(time.time())), pysecrets.token_hex(12)
    return {"X-Device-Id": device_id, "X-Timestamp": ts, "X-Nonce": nonce,
            "X-Signature": _hmac(secret, f"{ts}\n{nonce}\n{hashlib.sha256(body).hexdigest()}")}


# ------------------------------------------------------------------ TLS
def ensure_cert(bind: str) -> tuple[Path, Path, str]:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    d = data_dir() / "tls"
    d.mkdir(exist_ok=True)
    crt, key = d / "cert.pem", d / "key.pem"
    if not crt.exists() or not key.exists():
        k = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "ramai-device")])
        san = [x509.DNSName("localhost")]
        try:
            san.append(x509.IPAddress(ipaddress.ip_address(bind)))
        except ValueError:
            pass
        now = dt.datetime.now(dt.timezone.utc)
        cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(k.public_key())
                .serial_number(x509.random_serial_number()).not_valid_before(now - dt.timedelta(days=1))
                .not_valid_after(now + dt.timedelta(days=3650)).add_extension(x509.SubjectAlternativeName(san), False)
                .sign(k, hashes.SHA256()))
        key.write_bytes(k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        crt.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    der = ssl.PEM_cert_to_DER_cert(crt.read_text())
    return crt, key, hashlib.sha256(der).hexdigest()


# ------------------------------------------------------------------ client
async def peer_call(app, peer: dict, method: str, params: dict | None = None, timeout: float = 20) -> dict:
    secret = app.secrets.get(f"peer:{peer['id']}")
    if not secret:
        raise PolicyError("Peer has no stored credentials (re-pair)")
    body = json.dumps({"method": method, "params": params or {}}).encode()
    hdrs = {**sign_headers(app.settings.get("device_id"), secret, body), "Content-Type": "application/json"}
    async with httpx.AsyncClient(verify=False, timeout=timeout) as c:
        try:
            r = await c.post(peer["url"].rstrip("/") + "/remote/rpc", content=body, headers=hdrs)
        except httpx.HTTPError as e:
            raise PolicyError(f"Device '{peer['name']}' is offline or unreachable ({type(e).__name__})")
        stream = r.extensions.get("network_stream")
        sslobj = stream.get_extra_info("ssl_object") if stream else None
        der = sslobj.getpeercert(binary_form=True) if sslobj else None
        if not der or hashlib.sha256(der).hexdigest() != peer.get("fingerprint"):
            raise PolicyError("Peer TLS certificate does not match the pinned fingerprint. Refusing (possible impersonation).")
    if r.status_code != 200:
        raise PolicyError(f"Peer rejected request ({r.status_code}): {r.text[:200]}")
    return r.json()


async def join_peer(app, url: str, code: str) -> dict:
    """Controller side of pairing: prove knowledge of the code; the target's user must approve."""
    my_id, nonce = app.settings.get("device_id"), pysecrets.token_hex(12)
    payload = {"device_id": my_id, "name": app.device_name, "nonce": nonce, "proof": _hmac(code, f"pair|{my_id}|{nonce}"),
               "callback_url": app.remote_url()}
    async with httpx.AsyncClient(verify=False, timeout=150) as c:
        try:
            r = await c.post(url.rstrip("/") + "/remote/pair", json=payload)
        except httpx.HTTPError as e:
            raise PolicyError(f"Cannot reach {url}: {type(e).__name__}")
    if r.status_code != 200:
        raise PolicyError(f"Pairing refused: {r.json().get('detail', r.status_code) if r.headers.get('content-type','').startswith('application/json') else r.status_code}")
    d = r.json()
    secret = _hmac(code, f"secret|{nonce}|{d['device_id']}|{my_id}")
    if not hmac.compare_digest(d["mac"], _hmac(secret, "fp|" + d["fingerprint"])):
        raise PolicyError("Pairing response failed authentication")
    peer = {"id": d["device_id"], "name": d["name"], "url": url.rstrip("/"), "fingerprint": d["fingerprint"]}
    app.add_peer(peer, secret)
    return peer


# ------------------------------------------------------------------ server
def on_main(app, fn):
    async def w():
        return fn()

    return asyncio.run_coroutine_threadsafe(w(), app.loop).result(30)


def build_remote_app(app) -> FastAPI:
    api = FastAPI(title="RAM AI remote", docs_url=None, redoc_url=None, openapi_url=None)
    nonces: dict[str, float] = {}
    hits: dict[str, list[float]] = {}
    lock = threading.Lock()

    @api.post("/remote/pair")
    async def pair(req: Request):
        d = await req.json()
        p = app.pairing
        now = time.time()
        if not p or now > p["expires"] or p["attempts"] >= 3:
            raise HTTPException(403, "No active pairing code")
        p["attempts"] += 1
        try:
            ok = hmac.compare_digest(d["proof"], _hmac(p["code"], f"pair|{d['device_id']}|{d['nonce']}"))
        except Exception:
            ok = False
        if not ok:
            raise HTTPException(403, "Invalid pairing code")
        code = p["code"]
        app.pairing = None  # single use
        ctx = ToolContext(app, "pairing", app.device_name)
        summary = (f"PAIR DEVICE: '{d['name']}' (id {d['device_id'][:8]}…) wants to control THIS PC ({app.device_name}).\n"
                   "Approve only if you started this on your other computer.")
        res = await app.approvals.request(ctx, "approval", "pair_device", args_hash("pair", {"id": d["device_id"]}), summary, "high")
        if res["status"] != "approved":
            raise HTTPException(403, "Pairing was declined")
        _, _, fp = ensure_cert(app.settings.get("remote")["bind"])
        secret = _hmac(code, f"secret|{d['nonce']}|{app.settings.get('device_id')}|{d['device_id']}")
        app.add_peer({"id": d["device_id"], "name": d["name"][:60], "url": d.get("callback_url") or "", "fingerprint": ""}, secret)
        app.db.audit(None, app.device_name, "peer_paired", None, {"peer": d["name"]})
        return {"device_id": app.settings.get("device_id"), "name": app.device_name, "fingerprint": fp, "mac": _hmac(secret, "fp|" + fp)}

    def verify(req: Request, body: bytes) -> dict:
        h = req.headers
        peer = next((p for p in app.settings.get("peers", []) if p["id"] == h.get("x-device-id")), None)
        secret = app.secrets.get(f"peer:{peer['id']}") if peer else None
        if not peer or not secret:
            raise HTTPException(401, "Unknown device")
        try:
            ts = int(h.get("x-timestamp", "0"))
        except ValueError:
            raise HTTPException(401, "Bad timestamp")
        nonce = h.get("x-nonce", "")
        if abs(time.time() - ts) > SKEW or len(nonce) < 16:
            raise HTTPException(401, "Stale or malformed request")
        expect = _hmac(secret, f"{ts}\n{nonce}\n{hashlib.sha256(body).hexdigest()}")
        if not hmac.compare_digest(expect, h.get("x-signature", "")):
            raise HTTPException(401, "Bad signature")
        with lock:
            now = time.time()
            for k in [k for k, v in nonces.items() if v < now]:
                del nonces[k]
            if nonce in nonces:
                raise HTTPException(401, "Replay detected")
            nonces[nonce] = now + SKEW * 2
            w = [t for t in hits.get(peer["id"], []) if t > now - 60]
            if len(w) >= 120:
                raise HTTPException(429, "Rate limit")
            w.append(now)
            hits[peer["id"]] = w
        return peer

    @api.post("/remote/rpc")
    async def rpc(req: Request):
        body = await req.body()
        peer = verify(req, body)
        d = json.loads(body)
        m, p = d.get("method"), d.get("params") or {}
        db = app.db
        db.audit(None, app.device_name, "remote_rpc", m, {"peer": peer["name"]})
        if m == "ping":
            return {"device_id": app.settings.get("device_id"), "name": app.device_name, "version": VERSION}
        if m == "state":
            return {"tasks": db.query("SELECT id,instruction,state,plan,result,error,updated FROM tasks ORDER BY created DESC LIMIT 8"),
                    "approvals": db.query("SELECT id,task_id,kind,tool,summary,risk,created FROM approvals WHERE status='pending'")}
        if m == "submit_task":
            ins = str(p.get("instruction", ""))[:4000]
            if not ins:
                raise HTTPException(400, "instruction required")
            tid = on_main(app, lambda: app.orchestrator.create(ins, app.device_name, bool(p.get("pre_approve"))))
            db.audit(tid, app.device_name, "task_from_peer", None, {"peer": peer["name"]})
            return {"task_id": tid}
        if m == "cancel_task":
            return {"cancelled": on_main(app, lambda: app.orchestrator.cancel(str(p.get("id"))))}
        if m == "decide_approval":
            try:
                return app.approvals.decide(str(p.get("id")), str(p.get("decision")), p.get("answer"), str(p.get("scope", "once")))
            except ValueError as e:
                raise HTTPException(409, str(e))
        if m == "screenshot":
            if not app.settings.get("remote", {}).get("allow_screenshot"):
                raise HTTPException(403, "Remote screenshots are disabled on this PC")
            from .tools.computer import grab

            png, info = grab(1)
            return {"png_b64": base64.b64encode(png).decode(), **info}
        if m == "receive_file":
            raw = base64.b64decode(p.get("data_b64", ""), validate=True)
            if len(raw) > MAX_FILE or hashlib.sha256(raw).hexdigest() != p.get("sha256"):
                raise HTTPException(400, "File too large or checksum mismatch")
            name = "".join(c for c in Path(str(p.get("name", "file"))).name if c not in '\\/:*?"<>|')[:120] or "file"
            inbox = data_dir() / "inbox" / peer["name"].replace("/", "_")[:40]
            inbox.mkdir(parents=True, exist_ok=True)
            dest = inbox / f"{int(time.time())}_{name}"
            dest.write_bytes(raw)
            db.audit(None, app.device_name, "file_received", None, {"from": peer["name"], "name": name, "bytes": len(raw), "sha256": p["sha256"]})
            return {"stored": True, "sha256": p["sha256"], "bytes": len(raw)}
        raise HTTPException(400, "Unknown method")

    return api


class RemoteServer:
    def __init__(self, app):
        self.app, self.server, self.thread = app, None, None

    def start(self) -> str:
        import uvicorn

        r = self.app.settings.get("remote")
        bind = r.get("bind", "")
        try:
            ip = ipaddress.ip_address(bind)
        except ValueError:
            raise PolicyError("Set the remote bind address to this PC's Tailscale/private IP first")
        if ip.is_unspecified or ip.is_loopback:
            raise PolicyError("Remote bind must be a specific private/VPN address (not 0.0.0.0 or loopback)")
        if not (ip.is_private or str(ip) .startswith("100.")):  # RFC1918 or Tailscale CGNAT
            raise PolicyError("Bind address must be a private/VPN address (RFC1918 or Tailscale 100.x.y.z)")
        crt, key, fp = ensure_cert(bind)
        cfg = uvicorn.Config(build_remote_app(self.app), host=bind, port=int(r.get("port", 8766)),
                             ssl_certfile=str(crt), ssl_keyfile=str(key), log_level="warning")
        self.server = uvicorn.Server(cfg)
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        return fp

    def stop(self):
        if self.server:
            self.server.should_exit = True
            self.server = None


# ------------------------------------------------------------------ model-facing tools
class NoArgs(Args):
    pass


class TransferArgs(Args):
    peer: str = Field(max_length=60, description="Name of a paired device")
    path: str


def _peer(app, name):
    p = next((p for p in app.settings.get("peers", []) if p["name"].lower() == name.lower()), None)
    if not p:
        raise PolicyError(f"No paired device named '{name}'")
    return p


async def t_devices(a, ctx):
    out = [{"name": ctx.app.device_name, "this_pc": True}]
    for p in ctx.app.settings.get("peers", []):
        try:
            await peer_call(ctx.app, p, "ping", timeout=5)
            st = "online"
        except PolicyError as e:
            st = f"offline ({str(e)[:60]})"
        out.append({"name": p["name"], "status": st})
    return {"devices": out}


async def transfer_preview(a: TransferArgs, ctx):
    _peer(ctx.app, a.peer)
    p = ctx.app.policy.resolve(a.path, must_exist=True)
    h = hashlib.sha256(p.read_bytes()).hexdigest()
    return {"text": f"TRANSFER FILE\nFrom: {ctx.app.device_name}\nTo: {a.peer}\nFile: {p}\nSize: {p.stat().st_size} bytes\nSHA-256: {h}", "digest": h}


async def t_transfer(a: TransferArgs, ctx):
    peer = _peer(ctx.app, a.peer)
    p = ctx.app.policy.resolve(a.path, must_exist=True)
    raw = p.read_bytes()
    if len(raw) > MAX_FILE:
        raise PolicyError("File exceeds 25 MB limit")
    h = hashlib.sha256(raw).hexdigest()
    if h != ctx.approved_digest:
        raise PolicyError("File changed after approval; nothing sent")
    r = await peer_call(ctx.app, peer, "receive_file", {"name": p.name, "sha256": h, "data_b64": base64.b64encode(raw).decode()}, timeout=120)
    if not r.get("stored") or r.get("sha256") != h:
        raise PolicyError("Receiver did not confirm integrity")
    return {"transferred": p.name, "to": peer["name"], "sha256_verified": True}


def register(reg: Registry) -> None:
    reg.register(Tool("list_devices", "List this PC and paired devices with online status.", NoArgs, t_devices, Risk.READ, 60))
    reg.register(Tool("transfer_file", "Send a file to a paired device (needs approval; integrity verified).", TransferArgs, t_transfer, Risk.HIGH, 150, preview=transfer_preview))
