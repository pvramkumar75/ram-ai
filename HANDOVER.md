# RAM AI — quick start for a new user

RAM AI is a desktop assistant for Windows 10/11. You type what you want ("summarise today's mail",
"make a supplier comparison in Excel", "WhatsApp Ravi that the meeting moved to 11") and it works in your
apps — Outlook, WhatsApp, Excel, Word, PowerPoint, files, web — while showing every step.
Anything that sends, deletes, overwrites or runs code waits for your OK.

## 1. Install (5 minutes, no admin rights)
1. **Extract** the ZIP first (right-click → *Extract All…*) to a permanent place, e.g. `Documents\RAM_AI`.
   Do not run it from inside the ZIP window, and avoid OneDrive-synced folders.
2. Double-click **`Install_RAM_AI.bat`**. If Windows shows *"Windows protected your PC"*, click **More info → Run anyway**.
   If setup fails, it saves **`install_log.txt`** in the same folder — send that file to whoever shared RAM AI.
   - If Python 3.11+ is missing it offers to install it for you (winget, your user only).
   - It installs RAM AI, creates **Desktop + Start-menu shortcuts**, and asks whether to start with Windows
     (say **Yes** if you want scheduled tasks to run).
3. RAM AI opens. The setup asks for:
   - **Your name** and a name for this computer,
   - **Your own AI key**: DeepSeek (platform.deepseek.com → API keys) or OpenAI. Each person uses their own key;
     it is stored encrypted for your Windows user only and never shown again.

## 2. First things to do
- **Settings → Capabilities → Grant read access** so it can read your Desktop/Documents/Downloads/OneDrive.
- Outlook: works automatically with **classic Outlook** on this PC. (Optional, faster and prompt-free:
  Settings → Outlook → Microsoft sign-in — see `docs/GUIDES.md`, may need IT approval.)
- WhatsApp: install and log in to the **WhatsApp desktop app** yourself; RAM AI uses it.
- Try a suggestion on the start screen.

## 3. Everyday use
- Type a request and press Enter. Left side = what it is doing on screen; right side = its thinking, steps and approvals.
- **Approve / Reject** cards appear before sending, deleting, overwriting or running code.
  Tick **Pre-approve** or write **"authorization granted"** to let one task run without prompts.
- **Stop** button or **Ctrl+Alt+Q** stops everything immediately.
- **Schedules** tab: "every weekday at 9:00 do X and email it to Y" — or just ask the agent to schedule it.

## 4. Good to know
- Your data (keys, settings, history, memory, schedules) stays on your PC in `%APPDATA%\RAM_AI`.
- Some actions are always refused (formatting disks, turning off antivirus/firewall, reading saved passwords…).
- If Outlook shows "A program is trying to access email address information", click **Deny** — RAM AI
  does not need it; it reads mail through Outlook's own windows instead. Your IT can make Outlook trust
  automation by keeping the antivirus status healthy.
- Company PCs: ask IT before installing; the tool controls mouse/keyboard and reads mail on your behalf.
- Uninstall: **`Uninstall_RAM_AI.bat`** (asks before deleting your data).

Troubleshooting: see `docs/GUIDES.md`.
