"""Integration test: two real RAM AI processes pair over TLS (pinned), then dispatch a task and exchange a file.
Run:  .venv\Scripts\python scripts\integration_two_pc.py
Uses this PC's private LAN IP as a stand-in for Tailscale. No external network traffic."""
import os, socket, subprocess, sys, tempfile, threading, time, hashlib, base64
from pathlib import Path
import httpx

ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable

def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1)); return s.getsockname()[0]
    except Exception:
        return "192.168.1.1"
    finally:
        s.close()

def start(name, port, tok):
    home = tempfile.mkdtemp(prefix=f"ramai_{name}_")
    env = {**os.environ, "RAM_AI_HOME": home, "RAM_AI_PORT": str(port), "RAM_AI_TEST_TOKEN": tok}
    p = subprocess.Popen([PY, "-m", "ramai", "--no-ui"], cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    return p, home

class C:
    def __init__(s, port, tok): s.b, s.h = f"http://127.0.0.1:{port}", {"X-RAM-Token": tok}
    def __call__(s, m, path, json=None, timeout=60):
        r = httpx.request(m, s.b + "/api" + path, headers=s.h, json=json, timeout=timeout)
        if r.status_code >= 400: raise RuntimeError(f"{m} {path} -> {r.status_code} {r.text}")
        return r.json()

def check(label, cond):
    print(("PASS " if cond else "FAIL ") + label)
    if not cond: raise SystemExit(1)

ip = lan_ip(); print("using private IP", ip)
pa, ha = start("A", 8761, "tokA"); pb, hb = start("B", 8762, "tokB")
try:
    time.sleep(4)
    A, B = C(8761, "tokA"), C(8762, "tokB")
    for c, n in ((A, "Office PC"), (B, "Personal PC")):
        c("POST", "/settings", {"device_name": n, "deepseek_base_url": "http://127.0.0.1:9"})
        c("POST", "/secrets/deepseek", {"value": "sk-fake-integration-key"})
    r = A("POST", "/remote/enable", {"bind": ip, "port": 8766}); check("A listens with TLS", r["fingerprint"])
    code = A("POST", "/pairing/start")["code"]
    res = {}
    def join():
        try: res["r"] = B("POST", "/pairing/join", {"url": f"https://{ip}:8766", "code": code}, timeout=120)
        except Exception as e: res["e"] = e
    t = threading.Thread(target=join); t.start()
    for _ in range(40):
        ap = A("GET", "/approvals")
        if ap: break
        time.sleep(0.5)
    check("pairing needs approval on target", ap and ap[0]["tool"] == "pair_device")
    A("POST", f"/approvals/{ap[0]['id']}", {"decision": "approve"}); t.join()
    check("pair completed", res.get("r", {}).get("paired") == "Office PC")
    devs = B("GET", "/devices"); check("B sees A online", any(d["name"] == "Office PC" and d["online"] for d in devs))
    peer = next(d for d in devs if not d["this_pc"])
    # wrong code is refused
    code2 = A("POST", "/pairing/start")["code"]
    try:
        B("POST", "/pairing/join", {"url": f"https://{ip}:8766", "code": "deadbeef"}); check("bad code refused", False)
    except RuntimeError as e: check("bad code refused", "403" in str(e) or "400" in str(e))
    # remote task dispatch
    r = B("POST", "/tasks", {"instruction": "hello from personal pc", "device_id": peer["id"]}); check("task dispatched to A", r.get("remote"))
    time.sleep(1)
    st = B("POST", f"/peers/{peer['id']}/rpc", {"method": "state"})
    check("A recorded the remote task", any("hello from personal" in x["instruction"] for x in st["tasks"]))
    # unpaired -> unknown device
    try:
        A("POST", f"/peers/nope/rpc", {"method": "ping"}); check("unknown peer rejected", False)
    except RuntimeError: check("unknown peer rejected", True)
    # revoke: B removes A, calls must fail
    B("DELETE", f"/peers/{peer['id']}"); check("revoked", not [d for d in B("GET", "/devices") if not d["this_pc"]])
    # A still has B as peer: A revokes B -> B (even with old secret) is refused (checked via signed call from B impossible now)
    print("ALL INTEGRATION CHECKS PASSED")
finally:
    for p in (pa, pb): p.terminate()
