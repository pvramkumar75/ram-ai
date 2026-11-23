"""Live read-only check against your real apps. Usage: python scripts/live_check.py <name>
Runs: launch WhatsApp, find chat by name, find Outlook person by name, search mail by name. Sends/drafts nothing."""
import asyncio, json, os, sys, tempfile, time
sys.path.insert(0, ".")
os.environ.setdefault("RAM_AI_HOME", tempfile.mkdtemp(prefix="ramai_live_"))
from ramai.app import App
from ramai.engine import ToolContext

name = sys.argv[1] if len(sys.argv) > 1 else "Asha"


async def main():
    app = App(indicator=False)
    app.settings.set("device_name", "Live")
    app.loop = asyncio.get_running_loop()
    ctx = ToolContext(app, "live", "Live", approve_all=True)
    ctx.control_approved = True
    for tool, args in [("launch_app", {"name": "WhatsApp"}), ("whatsapp_find_chat", {"query": name}),
                       ("outlook_find_person", {"name": name}), ("outlook_search", {"query": name, "top": 3})]:
        t = time.time()
        out, ok = await app.executor.execute(ctx, tool, json.dumps(args))
        print(f"\n== {tool} ok={ok} {time.time() - t:.2f}s\n{out[:1200]}")

asyncio.run(main())
