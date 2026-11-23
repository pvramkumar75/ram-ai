# RAM AI — a desktop agent for Windows 11

RAM AI runs on your own PC. You tell it what you need in plain words, an AI model plans the steps, and typed tools carry them out: Outlook, WhatsApp, files, Excel/Word/PowerPoint, the web, PowerShell and Python, and mouse/keyboard control of any app. **Anything outward-facing or destructive (sending, deleting, running commands) waits for your approval**, and every action is written to an audit log.

## Install (one line)

Open **PowerShell** (Start → type `PowerShell` → Enter) and paste:

```powershell
irm https://raw.githubusercontent.com/pvramkumar75/ram-ai/main/get.ps1 | iex
```

What this does: downloads the latest release from this repository, **checks its SHA-256 checksum**, installs it to `%LOCALAPPDATA%\RAM_AI\app` (no administrator rights), installs Python 3.12 for your user if needed, creates the Desktop/Start-menu shortcuts and adds the `ramai` command. It takes 2–5 minutes the first time.

Prefer a ZIP? Download `RAM_AI_v*.zip` from [Releases](https://github.com/pvramkumar75/ram-ai/releases), right-click → **Extract All**, then double-click **`Install_RAM_AI.bat`** in the extracted folder.

On first start RAM AI asks for your name, a name for the computer, and an **AI key**: DeepSeek, OpenAI, Sarvam, or a local model (Ollama / LM Studio). Each person uses their own key. Keys are encrypted with Windows DPAPI and never leave your PC except to the provider you chose.

## Use it from the terminal

```powershell
ramai                                   # open the app
ramai "summarise today's unread mail"   # run a task here; approvals are asked in the terminal
ramai run --pre-approve "..."           # approve the whole task up front
ramai status                            # is it running? which model?
ramai history -n 10                     # recent tasks
ramai schedules                         # list scheduled tasks (run / pause / resume / delete <id>)
ramai doctor                            # health check: Python, packages, Outlook, WhatsApp, model
ramai update                            # install the newest release
ramai stop
```

Open a **new** terminal after installing so `ramai` is on your PATH.

## What it can do

- **Outlook** (desktop app, else the web): find people by name, search and summarise mail, write and send email with attachments (approval first).
- **WhatsApp** (desktop app, else WhatsApp Web): find a chat by name, asks when names are ambiguous, sends formatted messages.
- **Scheduling**: "every weekday at 9:00 ask ChatGPT for copper prices and WhatsApp them to me". Runs while RAM AI is open; it can start with Windows.
- **Documents**: create and edit Excel, Word and PowerPoint; Power BI guidance; read PDF/DOCX/XLSX/CSV/PPTX.
- **Code**: PowerShell/cmd and Python in an isolated workspace; installs packages when a task needs them.
- **Any app**: screenshots, OCR, UI Automation, mouse and keyboard, with a live view of what the agent is doing.
- **Two PCs**: pair an office and a personal PC over pinned TLS.

Emergency stop: **Ctrl+Alt+Q** or the red STOP button.

## Security in short

- Loopback-only server with a per-launch token; nothing listens on your network unless you pair a second PC.
- Folders are inaccessible until you add them in Settings → Folders; credential stores and system folders are always blocked.
- Approvals are bound to the exact action (a changed recipient or file needs a new approval).
- Text from emails, web pages and chats is treated as data, never as instructions.

More: [`docs/THREAT_MODEL.md`](docs/THREAT_MODEL.md) and [`docs/GUIDES.md`](docs/GUIDES.md) (providers, Outlook, WhatsApp, pairing, troubleshooting).

## Uninstall

Run `Uninstall_RAM_AI.bat` in the install folder (`%LOCALAPPDATA%\RAM_AI\app`). Your settings and history are kept unless you choose to delete them.

## Develop

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt -r requirements-dev.txt
.venv\Scripts\python -m pytest -q
.venv\Scripts\python -m ramai
```

`ramai/` holds `core` (settings, DPAPI secrets, SQLite audit), `policy`, `tools/*` (typed tool registry), `engine` (planner, approvals, executor), `providers` (DeepSeek / OpenAI / Sarvam / local), `scheduler`, `remote` (two-PC), `server` (loopback API), `cli` and `ui/`.

Releasing: bump `VERSION` in `ramai/core.py`, then `python scripts/publish_release.py` (builds the ZIP, scans it for personal data, writes `SHA256SUMS.txt` and creates the GitHub release that `get.ps1` installs).
