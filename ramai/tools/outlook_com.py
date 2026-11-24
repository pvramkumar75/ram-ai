"""Classic Outlook (desktop) via COM automation: fast, no cloud app registration, resolves people by NAME.
All functions are synchronous and must run in a worker thread (they CoInitialize themselves) and return only primitives."""
from __future__ import annotations

import hashlib
import html
import json
import os
import re
import time
from contextlib import contextmanager
from pathlib import Path

from ..policy import BLOCKED_FILE, PolicyError

PR_SMTP = "http://schemas.microsoft.com/mapi/proptag/0x39FE001E"
PR_SENDER_SMTP = "http://schemas.microsoft.com/mapi/proptag/0x5D01001F"
EMAIL = re.compile(r"[^@\s;,]+@[^@\s;,]+\.[^@\s;,]+")


@contextmanager
def com():
    import pythoncom
    import win32com.client

    pythoncom.CoInitialize()
    try:
        try:
            ol = win32com.client.Dispatch("Outlook.Application")
        except Exception as e:
            raise PolicyError(f"Classic Outlook automation is unavailable ({type(e).__name__}). Use the Graph sign-in or Outlook web.")
        yield ol  # errors inside the caller's block propagate unchanged
    finally:
        pythoncom.CoUninitialize()


def available() -> bool:
    return os.path.exists(r"C:\Program Files\Microsoft Office\root\Office16\OUTLOOK.EXE") or \
        os.path.exists(r"C:\Program Files (x86)\Microsoft Office\root\Office16\OUTLOOK.EXE")


def _smtp_of_recipient(r) -> str:
    try:
        v = r.PropertyAccessor.GetProperty(PR_SMTP)
        if v:
            return v
    except Exception:
        pass
    try:
        a = r.Address or ""
        return a if "@" in a else ""
    except Exception:
        return ""


def _sender_smtp(item) -> str:
    try:
        v = item.PropertyAccessor.GetProperty(PR_SENDER_SMTP)
        if v:
            return v
    except Exception:
        pass
    try:
        a = item.SenderEmailAddress or ""
        return a if "@" in a else ""
    except Exception:
        return ""


def _tokens(q: str) -> list[str]:
    return [t for t in re.split(r"\s+", q.lower().strip()) if t]


# ------------------------------------------------------------------ people
def find_people(name: str, limit: int = 10, budget: float = 4.0) -> list[dict]:
    if not is_trusted():
        return _find_people_safe(name, limit)
    toks = _tokens(name)
    found: dict[str, dict] = {}
    t0 = time.time()

    from ..people import match as _short_match

    def add(display: str, email: str, source: str):
        if not email or not (all(t in (display + " " + email).lower() for t in toks) or _short_match(name, display, email)[0] >= 0.8):
            return
        k = email.lower()
        if k in found:
            found[k]["seen"] += 1
            return
        found[k] = {"name": display, "email": email, "source": source, "seen": 1}

    with com() as ol:
        ns = ol.GetNamespace("MAPI")
        # 1) saved contacts
        try:
            items = ns.GetDefaultFolder(10).Items
            for i, c in enumerate(items):
                if time.time() - t0 > budget / 2 or i > 3000:
                    break
                try:
                    for slot in ("Email1Address", "Email2Address"):
                        addr = getattr(c, slot, "") or ""
                        if "@" in addr:
                            add(c.FullName or "", addr, "contact")
                except Exception:
                    continue
        except Exception:
            pass
        # 2) people you recently mailed / got mail from
        for fid, kind in ((5, "sent"), (6, "inbox")):
            try:
                items = ns.GetDefaultFolder(fid).Items
                items.Sort("[SentOn]" if fid == 5 else "[ReceivedTime]", True)
                for i, m in enumerate(items):
                    if time.time() - t0 > budget or i >= 400:
                        break
                    try:
                        if fid == 5:
                            for r in m.Recipients:
                                add(r.Name or "", _smtp_of_recipient(r), "recent")
                        else:
                            add(m.SenderName or "", _sender_smtp(m), "recent")
                    except Exception:
                        continue
            except Exception:
                pass
        # 3) directory (GAL) resolution by name
        try:
            r = ns.CreateRecipient(name)
            if r.Resolve():
                ae = r.AddressEntry
                eu = ae.GetExchangeUser()
                add(ae.Name, (eu.PrimarySmtpAddress if eu else r.Address) or "", "directory")
        except Exception:
            pass
    res = sorted(found.values(), key=lambda x: (x["name"].lower() != name.lower(), -x["seen"]))
    return res[:limit]


# ------------------------------------------------------------------ mail
def _like(field: str, tok: str) -> str:
    return f'"{field}" LIKE \'%{tok.replace(chr(39), chr(39) * 2)}%\''


def _eq(field: str, val: str) -> str:
    return f'"{field}" = \'{val.replace(chr(39), chr(39) * 2)}\''


def _phrase(field: str, tok: str) -> str:
    return f'"{field}" ci_phrasematch \'{tok.replace(chr(39), chr(39) * 2)}\''


def _collect(items, flt, top, fid, out, deadline, safe=False):
    try:
        res = items.Restrict(flt)
    except Exception:
        return
    for i, m in enumerate(res):
        if i >= top or time.time() > deadline:
            break
        try:
            if any(o["id"] == m.EntryID for o in out):
                continue
            if safe:
                out.append(_safe_summary(m, {6: "inbox", 5: "sent", 16: "drafts"}[fid]))
                continue
            out.append({"id": m.EntryID, "subject": m.Subject, "from": m.SenderName, "from_email": _sender_smtp(m),
                        "received": local_time(m.ReceivedTime) if fid != 5 else local_time(m.SentOn), "has_attachments": m.Attachments.Count > 0,
                        "folder": {6: "inbox", 5: "sent", 16: "drafts"}[fid],
                        "preview": (m.Body or "")[:200].replace("\r", " ").replace("\n", " ")})
        except Exception:
            continue


def search(query: str, folder: str = "inbox", top: int = 10, body: bool = False, days: int = 365) -> list[dict]:
    """Stage 1: Windows Search index (ci_phrasematch) - ~0.2 s even on 30k+ mailboxes.
    Stage 2 (only if stage 1 finds nothing): substring LIKE limited to the last `days` days."""
    import datetime as dt

    toks = _tokens(query)
    if not toks:
        return []
    fields = ["urn:schemas:httpmail:subject", "urn:schemas:httpmail:fromname", "urn:schemas:httpmail:displayto"]
    if body:
        fields.append("urn:schemas:httpmail:textdescription")
    fast = "@SQL=" + " AND ".join("(" + " OR ".join(_phrase(f, t) for f in fields) + ")" for t in toks)
    since = (dt.datetime.now() - dt.timedelta(days=days)).strftime("%m/%d/%Y")
    slow_fields = ["urn:schemas:httpmail:subject", "urn:schemas:httpmail:fromname"] + (["urn:schemas:httpmail:textdescription"] if body else [])
    slow = (f'@SQL="urn:schemas:httpmail:datereceived" >= \'{since}\' AND '
            + " AND ".join("(" + " OR ".join(_like(f, t) for f in slow_fields) + ")" for t in toks))
    out: list[dict] = []
    deadline = time.time() + 25
    with com() as ol:
        ns = ol.GetNamespace("MAPI")
        fids = {"inbox": [6], "sent": [5], "drafts": [16], "all": [6, 5]}.get(folder, [6])
        safe = not is_trusted(ol)
        for flt in (fast, slow):
            for fid in fids:
                items = ns.GetDefaultFolder(fid).Items
                items.Sort("[ReceivedTime]" if fid != 5 else "[SentOn]", True)
                _collect(items, flt, top, fid, out, deadline, safe)
            if out:
                break
    try:  # brand-new mail may not be indexed yet: check the last 2 days directly and merge
        recent = list_messages("2d", folders="inbox_and_subfolders" if folder != "sent" else "sent", limit=400, preview=200)["messages"]
        have = {o["id"] for o in out}
        for r in recent:
            hay = f"{r['subject']} {r.get('from', '')} {r.get('from_email', '')}".lower() + (" " + r.get("preview", "").lower() if body else "")
            if r["id"] not in have and all(t in hay for t in toks):
                out.append(r)
    except Exception:
        pass
    out.sort(key=lambda x: x["received"], reverse=True)
    return out[:top]


def read(entry_id: str, max_chars: int) -> dict:
    if not is_trusted():
        return read_many([entry_id], max_chars)[0]
    with com() as ol:
        m = ol.GetNamespace("MAPI").GetItemFromID(entry_id)
        return {"id": entry_id, "subject": m.Subject, "from": m.SenderName, "from_email": _sender_smtp(m),
                "to": [{"name": r.Name, "email": _smtp_of_recipient(r)} for r in m.Recipients][:30],
                "received": local_time(getattr(m, "ReceivedTime", "")), "body": (m.Body or "")[:max_chars],
                "attachments": [{"index": i, "name": m.Attachments.Item(i).FileName, "size": m.Attachments.Item(i).Size}
                                for i in range(1, m.Attachments.Count + 1)]}


def download(entry_id: str, index: int, dest: Path) -> dict:
    with com() as ol:
        m = ol.GetNamespace("MAPI").GetItemFromID(entry_id)
        if not (1 <= index <= m.Attachments.Count):
            raise PolicyError("Attachment index out of range")
        att = m.Attachments.Item(index)
        name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", att.FileName or "attachment")[:150]
        if BLOCKED_FILE.search(name):
            raise PolicyError("Attachment type is blocked by policy")
        dest.mkdir(parents=True, exist_ok=True)
        p = dest / name
        i = 1
        while p.exists():
            p = dest / f"{Path(name).stem} ({i}){Path(name).suffix}"
            i += 1
        att.SaveAsFile(str(p))
        return {"saved": str(p), "size": p.stat().st_size}


def _html_body(text: str) -> str:
    return "<div style='font-family:Calibri,Arial;font-size:11pt'>" + html.escape(text).replace("\n", "<br>") + "</div><br>"


def create_draft(to: list[str], cc: list[str], subject: str, body: str, reply_to: str | None, reply_all: bool,
                 forward: str | None, attachments: list[Path]) -> dict:
    if not is_trusted():
        raise PolicyError("UNTRUSTED_OUTLOOK: Outlook on this PC blocks programs from using recipients/sending (it would show a "
                          "security prompt). Draft it in Outlook's own window instead: launch_app('Outlook'), hotkey ctrl+n, type the "
                          "recipient, Tab, subject, Tab, body; then press Alt+S only after the user approves.")
    with com() as ol:
        ns = ol.GetNamespace("MAPI")
        if reply_to:
            m = ns.GetItemFromID(reply_to)
            mail = m.ReplyAll() if reply_all else m.Reply()
        elif forward:
            mail = ns.GetItemFromID(forward).Forward()
        else:
            mail = ol.CreateItem(0)
            mail.Subject = subject
        unresolved, ambiguous = [], []
        for lst, kind in ((to, 1), (cc, 2)):
            for who in lst:
                r = mail.Recipients.Add(who)
                r.Type = kind
        if to or cc:
            mail.Recipients.ResolveAll()
            for r in mail.Recipients:
                if not r.Resolved:
                    unresolved.append(r.Name)
        if unresolved:
            raise PolicyError(f"Could not resolve recipient(s) {unresolved} to a single address. Use outlook_find_person to list candidates and ask the user which one.")
        if forward is None and not reply_to and not to:
            raise PolicyError("At least one recipient is required")
        for p in attachments:
            mail.Attachments.Add(str(p))
        mail.Display(False)  # show the draft window so the user sees it (does not send)
        try:
            mail.HTMLBody = _html_body(body) + (mail.HTMLBody or "")
        except Exception:
            mail.Body = body + "\r\n\r\n" + (mail.Body or "")
        mail.Save()
        return {"draft_id": mail.EntryID, "to": [{"name": r.Name, "email": _smtp_of_recipient(r)} for r in mail.Recipients]}


def fetch_draft(entry_id: str) -> tuple[list[str], list[str], list[str], str, str, list[str], str]:
    if not is_trusted():
        raise PolicyError("UNTRUSTED_OUTLOOK: Outlook on this PC blocks programs from using recipients/sending (it would show a "
                          "security prompt). Draft it in Outlook's own window instead: launch_app('Outlook'), hotkey ctrl+n, type the "
                          "recipient, Tab, subject, Tab, body; then press Alt+S only after the user approves.")
    with com() as ol:
        m = ol.GetNamespace("MAPI").GetItemFromID(entry_id)
        if getattr(m, "Sent", True):
            raise PolicyError("That message is not an unsent draft")
        to, cc, bcc = [], [], []
        for r in m.Recipients:
            lab = f"{r.Name} <{_smtp_of_recipient(r)}>"
            {1: to, 2: cc, 3: bcc}.get(r.Type, to).append(lab)
        atts = [f"{m.Attachments.Item(i).FileName} ({m.Attachments.Item(i).Size // 1024} KB)" for i in range(1, m.Attachments.Count + 1)]
        body = m.Body or ""
        digest = hashlib.sha256(json.dumps([to, cc, bcc, m.Subject, body, atts]).encode()).hexdigest()
        return to, cc, bcc, m.Subject or "", body, atts, digest


def send(entry_id: str) -> None:
    if not is_trusted():
        raise PolicyError("UNTRUSTED_OUTLOOK: Outlook on this PC blocks programs from using recipients/sending (it would show a "
                          "security prompt). Draft it in Outlook's own window instead: launch_app('Outlook'), hotkey ctrl+n, type the "
                          "recipient, Tab, subject, Tab, body; then press Alt+S only after the user approves.")
    with com() as ol:
        ol.GetNamespace("MAPI").GetItemFromID(entry_id).Send()


# ------------------------------------------------------------------ listing by date (no search index: always complete & current)
import datetime as _dt

_QUOTE_START = re.compile(
    r"^\s*(-{2,}\s*Original Message\s*-{2,}|From:\s.+|On .{6,120} wrote:|_{8,}|Sent from my |Get Outlook for |"
    r"-{2,}\s*Forwarded message\s*-{2,})", re.I | re.M)


def local_time(v) -> str:
    """Outlook COM returns local wall-clock times tagged as UTC; show them as plain local times."""
    try:
        return v.replace(tzinfo=None).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return str(v)


def _naive(v):
    try:
        return v.replace(tzinfo=None)
    except Exception:
        return None


def clean_body(text: str, limit: int = 1500) -> str:
    t = (text or "").replace("\r", "").replace("\x07", " | ").replace("\x0b", "\n")
    t = re.sub(r"\[cid:[^\]]+\]", "", t)
    m = _QUOTE_START.search(t, 40)  # cut the quoted reply chain (keep at least the first lines)
    if m:
        t = t[: m.start()]
    t = re.sub(r"[ \t ]+", " ", t)
    t = re.sub(r"\n\s*\n+", "\n", t).strip()
    return t[:limit] + ("…" if len(t) > limit else "")


def parse_since(since: str, until: str | None = None):
    now = _dt.datetime.now()
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    s = (since or "today").strip().lower()
    end = None
    if s == "today":
        start = today
    elif s == "yesterday":
        start, end = today - _dt.timedelta(days=1), today
    elif re.fullmatch(r"\d+\s*h", s):
        start = now - _dt.timedelta(hours=int(s[:-1]))
    elif re.fullmatch(r"\d+\s*d", s):
        start = today - _dt.timedelta(days=int(s[:-1]) - 1)
    elif s in ("this week", "week"):
        start = today - _dt.timedelta(days=today.weekday())
    else:
        start = _dt.datetime.fromisoformat(s)
    if until:
        u = until.strip().lower()
        end = today if u == "today" else _dt.datetime.fromisoformat(u) + (_dt.timedelta(days=1) if len(u) == 10 else _dt.timedelta())
    return start, end


def _folders(ns, which: str):
    inbox = ns.GetDefaultFolder(6)
    if which == "sent":
        return [ns.GetDefaultFolder(5)]
    out = [inbox]
    if which in ("inbox_and_subfolders", "all"):
        stack = [inbox]
        while stack:
            f = stack.pop()
            try:
                for sf in f.Folders:
                    out.append(sf)
                    stack.append(sf)
            except Exception:
                pass
    return out


def _g(m, attr, default=""):
    """Read one property; some items (protected/encrypted/not yet downloaded) refuse individual fields."""
    try:
        v = getattr(m, attr)
        return default if v is None else v
    except Exception:
        return default


def _summary(m, fname: str, preview: int) -> dict:
    try:
        n_att = m.Attachments.Count
    except Exception:
        n_att = 0
    try:
        sender_email = _sender_smtp(m)
    except Exception:
        sender_email = ""
    body = _g(m, "Body", "")
    return {"id": _g(m, "EntryID"), "received": local_time(_g(m, "ReceivedTime", "")), "from": _g(m, "SenderName", "(unknown sender)"),
            "from_email": sender_email, "to": str(_g(m, "To"))[:300], "cc": str(_g(m, "CC"))[:300], "subject": _g(m, "Subject", "(no subject)"),
            "unread": bool(_g(m, "UnRead", False)), "attachments": n_att, "folder": fname,
            "preview": clean_body(body, preview) if body else "(body not available - protected or not downloaded)"}


def list_messages(since: str = "today", until: str | None = None, folders: str = "inbox_and_subfolders",
                  unread_only: bool = False, sender: str | None = None, limit: int = 100, preview: int = 300) -> dict:
    start, end = parse_since(since, until)
    sender_l = (sender or "").lower().strip()
    out: list[dict] = []
    scanned_folders = []
    t0 = time.time()
    with com() as ol:
        ns = ol.GetNamespace("MAPI")
        safe = not is_trusted(ol)
        for f in _folders(ns, folders):
            try:
                items = f.Items
                if sender_l:  # filter by sender inside Outlook's store (no guarded property reads)
                    sq = sender_l.replace("'", "''")
                    items = items.Restrict('@SQL=("urn:schemas:httpmail:fromname" LIKE ' + "'%" + sq + "%'" +
                                           ' OR "urn:schemas:httpmail:fromemail" LIKE ' + "'%" + sq + "%')")
                items.Sort("[ReceivedTime]", True)  # newest first: stop as soon as we pass the start time
                fname = f.Name
            except Exception:
                continue
            scanned_folders.append(fname)
            m = items.GetFirst()
            while m is not None:
                if time.time() - t0 > 40:
                    break
                try:
                    rt = _naive(m.ReceivedTime)
                except Exception:
                    rt = None
                if rt is not None:
                    if rt < start:
                        break
                    ok = (end is None or rt < end) and (not unread_only or _g(m, "UnRead", False))
                    if ok and _g(m, "Class", 43) in (43, 53, 46):  # mail, meeting requests and reports
                        try:
                            out.append(_safe_summary(m, fname) if safe else _summary(m, fname, preview))
                        except Exception:
                            pass
                        if len(out) >= limit:
                            break
                m = items.GetNext()
            if len(out) >= limit:
                break
    out.sort(key=lambda x: x["received"], reverse=True)
    return {"window": f"{start:%Y-%m-%d %H:%M} to {(end or _dt.datetime.now()):%Y-%m-%d %H:%M} (local time)",
            "mode": "prompt-free (subjects/times only; call outlook_read_many for sender, recipients, body)" if safe else "full",
            "count": len(out), "truncated": len(out) >= limit, "folders": scanned_folders[:30], "messages": out}


def read_many(ids: list[str], max_chars: int = 1500) -> list[dict]:
    res = []
    with com() as ol:
        ns = ol.GetNamespace("MAPI")
        if not is_trusted(ol):
            for i in ids:
                try:
                    res.append(_read_via_window(ol, ns, i, max_chars))
                except Exception:
                    # Outlook busy (sync, another program): "RPC server unavailable" kills this connection - wait, reconnect, retry once
                    time.sleep(1.5)
                    try:
                        with com() as ol2:
                            ol, ns = ol2, ol2.GetNamespace("MAPI")
                            res.append(_read_via_window(ol, ns, i, max_chars))
                    except Exception as e2:
                        res.append({"id": i, "error": f"{type(e2).__name__}: {str(e2)[:120]}",
                                    "hint": "Outlook was busy; retry this id in a few seconds with outlook_read_many."})
            return res
        for i in ids:
            try:
                m = ns.GetItemFromID(i)
                d = _summary(m, "", 0)
                try:
                    atts = [m.Attachments.Item(k).FileName for k in range(1, m.Attachments.Count + 1)][:10]
                except Exception:
                    atts = []
                body = _g(m, "Body", "")
                res.append({k: d[k] for k in ("id", "received", "from", "from_email", "to", "cc", "subject")} |
                           {"attachments": atts, "body": clean_body(body, max_chars) if body else "(body not available - protected or not downloaded)"})
            except Exception as e:
                res.append({"id": i, "error": f"{type(e).__name__}"})
    return res


# ------------------------------------------------------------------ prompt-free mode (Outlook's "a program is trying to access..." guard)
# When Outlook does not trust automation (no healthy antivirus reported), reading SenderName/To/CC/Body/Recipients makes
# Outlook pop up a security prompt. In that case RAM AI never touches those properties: it reads only unguarded fields
# (subject, time, unread) and reads sender/recipients/body from the message's own Outlook window via UI Automation.
_trust_cache = {"v": None, "t": 0.0}


def is_trusted(ol=None) -> bool:
    if _trust_cache["v"] is not None and time.time() - _trust_cache["t"] < 60:
        return _trust_cache["v"]
    try:
        if ol is None:
            with com() as o:
                v = bool(o.IsTrusted)
        else:
            v = bool(ol.IsTrusted)
    except Exception:
        v = False  # unknown -> assume untrusted: never risk the prompt
    _trust_cache.update(v=v, t=time.time())
    return v


def _local_uia():
    import comtypes
    import comtypes.client

    comtypes.client.GetModule("UIAutomationCore.dll")
    from comtypes.gen import UIAutomationClient as U

    try:  # worker threads (scheduled/agent runs) have no COM apartment yet
        comtypes.CoInitialize()
    except OSError:
        pass
    return U, comtypes.CoCreateInstance(U.CUIAutomation._reg_clsid_, interface=U.IUIAutomation, clsctx=comtypes.CLSCTX_INPROC_SERVER)


def _read_via_window(ol, ns, entry_id: str, max_chars: int) -> dict:
    from .. import uia as wu

    U, ua = _local_uia()
    m = ns.GetItemFromID(entry_id)
    was_unread, subject, received = bool(m.UnRead), m.Subject, local_time(m.ReceivedTime)
    insp = None
    try:
        m.Display(False)
        insp = ol.ActiveInspector()
        cap = insp.Caption
        h = wu.wait_for(lambda: next((x for x, t in wu.windows() if t == cap), None), 6, 0.08)
        if not h:
            raise PolicyError("Message window did not open")
        root = ua.ElementFromHandle(h)

        def first(name=None, ctype=None):
            conds = []
            if name is not None:
                conds.append(ua.CreatePropertyCondition(U.UIA_NamePropertyId, name))
            if ctype is not None:
                conds.append(ua.CreatePropertyCondition(U.UIA_ControlTypePropertyId, ctype))
            c = conds[0] if len(conds) == 1 else ua.CreateAndCondition(conds[0], conds[1])
            try:
                return root.FindFirst(U.TreeScope_Descendants, c)
            except Exception:
                return None

        def value(el):
            if el is None:
                return ""
            try:
                return el.GetCurrentPattern(U.UIA_ValuePatternId).QueryInterface(U.IUIAutomationValuePattern).CurrentValue or ""
            except Exception:
                return ""

        doc = wu.wait_for(lambda: first(None, U.UIA_DocumentControlTypeId), 5, 0.08)
        body = ""
        end = time.time() + 2.5
        while doc is not None and time.time() < end:
            try:
                body = doc.GetCurrentPattern(U.UIA_TextPatternId).QueryInterface(U.IUIAutomationTextPattern).DocumentRange.GetText(max(4000, max_chars * 3))
            except Exception:
                body = ""
            if body.strip():
                break
            time.sleep(0.1)
        fields = {k: value(first(k, U.UIA_EditControlTypeId)) for k in ("From", "To", "Cc", "Sent")}
        return {"id": entry_id, "received": received, "subject": subject, "from": fields["From"], "to": fields["To"], "cc": fields["Cc"],
                "body": clean_body(body.replace("\r", "\n"), max_chars) or "(empty body)", "read_via": "Outlook message window (prompt-free)"}
    finally:
        try:
            if insp is not None:
                insp.Close(1)  # olDiscard: nothing is changed or saved in the message
        except Exception:
            pass
        if was_unread:  # opening marks it read: put the unread flag back exactly as it was
            try:
                f = ns.GetItemFromID(entry_id)
                f.UnRead = True
                f.Save()
            except Exception:
                pass


def _safe_summary(m, fname: str) -> dict:
    return {"id": _g(m, "EntryID"), "received": local_time(_g(m, "ReceivedTime", "")), "subject": _g(m, "Subject", "(no subject)"),
            "unread": bool(_g(m, "UnRead", False)), "folder": fname,
            "from": "(not read yet: use outlook_read_many for sender, recipients and body)"}


SMTP_TAG = "http://schemas.microsoft.com/mapi/proptag/0x5D01001F"  # sender SMTP address (works for Exchange senders too)


def interaction_counts(people: list[dict], days: int = 180, budget: float = 6.0) -> dict:
    """How much you exchange mail with each person over `days`: received from them + 2 x sent to them.
    Uses only search filters (no protected properties) so Outlook never shows a security prompt."""
    import datetime as dt

    since = (dt.datetime.now() - dt.timedelta(days=days)).strftime("%m/%d/%Y")
    out, t0 = {}, time.time()
    with com() as ol:
        ns = ol.GetNamespace("MAPI")
        # one date filter per folder, then a simple filter per person on top (an OR of filters is ~100x slower)
        inbox = ns.GetDefaultFolder(6).Items.Restrict(f"[ReceivedTime] >= '{since}'")
        sent = ns.GetDefaultFolder(5).Items.Restrict(f"[SentOn] >= '{since}'")
        for p in people[:8]:
            if time.time() - t0 > budget:
                break
            em, nm = (p.get("email") or ""), (p.get("name") or "").strip()
            n = 0
            try:
                if em:
                    n += inbox.Restrict("@SQL=" + _eq(SMTP_TAG, em)).Count
                elif len(nm) > 2:
                    n += inbox.Restrict("@SQL=" + _eq("urn:schemas:httpmail:fromname", nm)).Count
                if len(nm) > 2 and nm.lower() != em:
                    n += 2 * sent.Restrict("@SQL=" + _like("urn:schemas:httpmail:displayto", nm)).Count
            except Exception:
                pass
            out[p.get("key") or p.get("email")] = n
    return out


def _find_people_safe(name: str, limit: int) -> list[dict]:
    """Untrusted Outlook: find recent mail FROM that name inside the store, then read the From line in its window."""
    q = name.replace("'", "''")
    with com() as ol:
        ns = ol.GetNamespace("MAPI")
        lp = re.sub(r"[^\w.-]", "", name.split()[0] if name.split() else "").replace("'", "")
        flt = ('@SQL="urn:schemas:httpmail:fromname" LIKE ' + "'%" + q + "%'"
               + (f" OR \"urn:schemas:httpmail:fromemail\" LIKE '{lp}%' OR \"{SMTP_TAG}\" LIKE '{lp}%'" if len(lp) >= 2 else ""))
        items = ns.GetDefaultFolder(6).Items.Restrict(flt)
        items.Sort("[ReceivedTime]", True)
        ids, m = [], items.GetFirst()
        while m is not None and len(ids) < 6:
            ids.append(m.EntryID)
            m = items.GetNext()
    seen = {}
    for r in read_many(ids, 50):
        frm = r.get("from") or ""
        em = (EMAIL.findall(frm) or [""])[0]
        nm = re.sub(r"\s*<[^>]*>", "", frm).strip() or em
        if em and em.lower() not in seen:
            seen[em.lower()] = {"name": nm, "email": em, "source": "recent mail", "seen": 1}
        elif em:
            seen[em.lower()]["seen"] += 1
    return list(seen.values())[:limit]
