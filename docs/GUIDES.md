# RAM AI guides

## 1. DeepSeek
Enter your API key in the first-run wizard (or Settings → DeepSeek). **Test & list models** calls `GET {base}/models`; pick a listed model name in Settings if the default (`deepseek-chat`) is not listed. Base URL default `https://api.deepseek.com`. Tool-calling needs a model that supports function calling. Never paste the key in chat or code.

## 2. Vision provider (optional, separate from DeepSeek)
Settings → Vision. Any OpenAI-compatible `/chat/completions` endpoint that accepts `image_url` (hosted or a local server such as Ollama/vLLM at `http://localhost:11434/v1`). Enter base URL, model, key (any string for local), tick *Enable* and the consent box, press **Test**. A local server keeps screenshots on the PC. Without vision the agent still works through UI Automation and file tools.

## 3. Outlook (Microsoft Graph)
1. portal.azure.com → Microsoft Entra ID → App registrations → New registration. Account type: your org (or "any org + personal"). Redirect URI: platform **Mobile and desktop** → `http://localhost`.
2. API permissions → Microsoft Graph → **Delegated**: `Mail.ReadWrite`, `Mail.Send`, `User.Read`. Org tenants may need admin consent.
3. Copy the *Application (client) ID* (and Directory/tenant ID for single-tenant) into Settings → Outlook → Save → **Sign in with Microsoft**. MFA/Conditional Access are handled by Microsoft's page. Only tokens are stored (DPAPI); sign-out deletes them.
Scopes explained: ReadWrite = search/read mail and create drafts; Send = send an approved draft; User.Read = show which account is signed in.

## 4. WhatsApp Web (experimental)
Settings → Open WhatsApp Web; scan the QR yourself in the Edge window (RAM AI-owned profile, no cookies read). The agent can read visible chat text, type a draft, and send only after you approve the exact recipient + text; it then checks the message appears as outgoing and says "unverified" otherwise. Unofficial automation may violate WhatsApp's terms and breaks when the page changes. No bulk messaging.

## 5. Two PCs with Tailscale
1. Install Tailscale on both PCs, sign in to the same tailnet; note each Tailscale IP (`100.x.y.z`).
2. On the PC to be controlled (target): Settings → Two-PC → enter its Tailscale IP → **Start listening**, then **Generate pairing code**.
3. On the controlling PC: enter `https://<target-ip>:8766` and the code → **Pair**. The target PC shows an approval naming the requesting device; approve it.
4. The device selector now lists both. Pair in both directions if each should control the other. Revoke any time (Settings → revoke).
Windows Firewall: allow inbound TCP 8766 **only on the Tailscale interface/profile**. Never port-forward. Approvals for actions on the target appear in the controller's UI (proxied) and are executed on the target.

## 6. Security & permissions
- Folders default to nothing; read-only unless write ticked; output folder is always writable. System, Program Files, browser/credential stores, key/secret file types are always blocked.
- Approval dialogs show device, exact recipient/message/file/args, and whether the action is reversible. Approval is single-use and bound to the exact arguments.
- Delete = Recycle Bin. Overwrite/move/send/transfer/remember always ask.
- Audit log: Settings → History; retention 90 days by default; Clear history/Export available.

## 7. Troubleshooting
| Problem | Fix |
|---|---|
| UI says "Cannot reach the agent" | Re-open via `Start_RAM_AI.bat` (token changes each launch) |
| "Invalid API key (401)" | Re-enter the key in Settings |
| Model not found | Settings → Test & list models → copy a listed name |
| Click refused: "No recent screenshot" | Expected; the agent must screenshot first |
| Port 8765 busy | set `RAM_AI_PORT` env var |
| Peer "offline" | Check Tailscale up, target "Start listening", firewall rule, same port |
| Pairing refused | Code is single-use, 10 min, 3 attempts — generate a new one |
| WhatsApp "UI not recognised" | WhatsApp changed; do the step manually |

## 8. Known limitations
- No voice input yet.
- Scheduled tasks run only while RAM AI is open (it can start with Windows).
- Screen control works on the main display; very unusual app layouts may need you to finish a step by hand.
