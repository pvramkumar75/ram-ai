"""Outlook tools. Backend is chosen automatically: Microsoft Graph (if signed in) -> classic Outlook desktop (COM) -> error.
Both resolve people by NAME where possible and share the same approval/digest/duplicate-send protections."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
from pathlib import Path

from pydantic import Field, field_validator

from ..signoff import strip_ai_signoff, strip_closing_block

from ..policy import BLOCKED_FILE, PolicyError, Risk
from . import outlook as _ol
from . import outlook_com as oc
from .base import Args, Registry, Tool

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def backend(app) -> str:
    pref = app.settings.get("outlook_backend", "auto")
    if pref in ("graph", "com"):
        return pref
    if app.settings.get("ms_client_id") and app.outlook.account():
        return "graph"
    if oc.available():
        return "com"
    if app.settings.get("ms_client_id"):
        return "graph"
    raise PolicyError("No Outlook access: install classic Outlook, or add a Microsoft app ID and sign in (Settings), "
                      "or use Outlook on the web (launch_app 'outlook' with prefer_web).")


def _open_outlook_window(ctx):
    try:
        from .apps import launch

        launch(ctx.app, "Outlook")  # the user sees the app; COM then works against the running instance
    except Exception:
        pass


# ------------------------------------------------------------------ args
class FindPersonArgs(Args):
    name: str = Field(max_length=100, description="Person's name or part of it (NOT the full email). Returns candidates with emails.")
    limit: int = Field(10, ge=1, le=25)


class SearchArgs(Args):
    query: str = Field(max_length=200, description="Words; ALL must appear in subject or sender name/email (or body if search_body). e.g. 'supplier copper price'")
    folder: str = Field("inbox", pattern="^(inbox|sent|drafts|all)$")
    top: int = Field(10, ge=1, le=25)
    search_body: bool = False
    days: int = Field(365, ge=1, le=3650, description="Only used by the slower fallback search")


class ReadArgs(Args):
    message_id: str
    max_chars: int = Field(8000, ge=200, le=30000)


class DownloadArgs(Args):
    message_id: str
    attachment_id: str = Field(description="Attachment id from outlook_read (the index number with desktop Outlook)")
    dest_dir: str = Field(description="Write-enabled authorised folder")


class DraftArgs(Args):
    to: list[str] = Field(default_factory=list, max_length=20, description="Emails or NAMES (desktop Outlook resolves names; ambiguous names return an error)")
    cc: list[str] = Field(default_factory=list, max_length=20)
    subject: str = Field("", max_length=300)
    body: str = Field(max_length=50000)
    reply_to_message_id: str | None = None
    reply_all: bool = False
    forward_message_id: str | None = None
    attachment_paths: list[str] = Field(default_factory=list, max_length=5)

    @field_validator("body")
    @classmethod
    def _no_ai_signoff(cls, v: str) -> str:  # never 'Regards, RAM AI (on behalf of ...)'
        return strip_ai_signoff(v)


class SendArgs(Args):
    draft_id: str


def _recips(addrs):
    for a in addrs:
        if not EMAIL_RE.match(a):
            raise PolicyError(f"Invalid email address: {a} (the Graph path needs full addresses; use outlook_find_person first)")
    return [{"emailAddress": {"address": a}} for a in addrs]


# ------------------------------------------------------------------ short names -> the right person
async def resolve_one(ctx, q: str) -> tuple[str | None, str]:
    """'KVR', 'Suresh', 'Srini' -> one email address. Ties are broken by how much you mail each person."""
    from ..people import TIE, explain, rank

    ppl = ctx.app.people
    cands = ppl.candidates(q, "mail")
    if backend(ctx.app) == "com" and not (cands and cands[0]["score"] >= 1.0):
        try:
            for f in await asyncio.to_thread(oc.find_people, q, 8):
                ppl.note(f.get("name") or "", f.get("email") or "", source="outlook")
        except Exception:
            pass
        cands = ppl.candidates(q, "mail")
    cands = [c for c in cands if c["score"] >= 0.8]
    if not cands:
        return None, f"'{q}': no known person matched, so Outlook resolves the name itself (Check Names)"
    counts = {}
    tied = [c for c in cands if c["score"] >= cands[0]["score"] - TIE]
    if backend(ctx.app) == "com" and len(tied) > 1:
        try:
            counts = await asyncio.to_thread(oc.interaction_counts, tied)
        except Exception:
            counts = {}
    ranked = rank(cands, counts)
    ctx.scratch.setdefault("people_map", {})[q] = ranked[0]["key"]
    return ranked[0]["email"], explain(q, ranked, lambda c: f"{c['name']} <{c['email']}>")


async def resolve_names(ctx, names: list[str]) -> tuple[list[str], list[str]]:
    out, notes = [], []
    for raw in names:
        q = (raw or "").strip()
        if not q or "@" in q:
            out.append(q)
            continue
        email, note = await resolve_one(ctx, q)
        out.append(email or q)
        if note:
            notes.append(note)
    return out, notes


def _remember_sent(ctx, asked: list[str]) -> None:
    ppl, m = ctx.app.people, ctx.scratch.get("people_map", {})
    for q in asked:
        q = (q or "").strip()
        if "@" in q:
            ppl.note(email=q, source="mail", sent="mail")
        elif q in m:
            ppl.note(email=m[q], source="mail", sent="mail")
            ppl.learn(q, "mail", m[q])


def _learn_from_messages(ctx, msgs) -> None:
    try:
        for m in msgs or []:
            if not isinstance(m, dict):
                continue
            if m.get("from_email"):
                ctx.app.people.note(m.get("from") or "", m["from_email"], source="mail")
            for f in ("from", "to", "cc"):
                if isinstance(m.get(f), str) and "@" in m[f]:
                    ctx.app.people.note_from_line(m[f], "mail")
    except Exception:
        pass


# ------------------------------------------------------------------ tools
async def t_find_person(a: FindPersonArgs, ctx):
    if backend(ctx.app) == "com":
        res = await asyncio.to_thread(oc.find_people, a.name, a.limit)
    else:
        toks = a.name.lower().split()
        d = await _ol.graph(ctx, "GET", "/me/people", params={"$search": '"' + a.name.replace('"', " ") + '"', "$top": a.limit})
        res = []
        for p in d.get("value", []):
            for em in p.get("scoredEmailAddresses", [])[:1]:
                res.append({"name": p.get("displayName"), "email": em.get("address"), "source": "people", "seen": 0})
        res = [r for r in res if r["email"] and all(t in (r["name"] + r["email"]).lower() for t in toks)]
    for r in res:
        ctx.app.people.note(r.get("name") or "", r.get("email") or "", source="outlook")
    email, note = await resolve_one(ctx, a.name)
    if not res and not email:
        return {"candidates": [], "next": "No match. Ask the user for the full email address or another spelling."}
    return {"candidates": res, "picked": email, "why": note,
            "next": "Use the picked address (send_email does this automatically for names and short forms). "
                    "The approval card shows who was picked, so the user can correct it."}


async def t_search(a: SearchArgs, ctx):
    if a.query.strip() in ("", "*", "all", "received:today"):  # not a keyword search: list by date instead
        return await t_list(ListArgs(since="today"), ctx)
    if backend(ctx.app) == "com":
        await asyncio.to_thread(_open_outlook_window, ctx)
        return {"messages": await asyncio.to_thread(oc.search, a.query, a.folder, a.top, a.search_body, a.days)}
    q = " ".join(a.query.replace('"', " ").split())
    d = await _ol.graph(ctx, "GET", "/me/messages", params={
        "$search": f'"{q}"', "$top": a.top, "$select": "id,subject,from,receivedDateTime,hasAttachments,bodyPreview"},
        headers={"ConsistencyLevel": "eventual"})
    return {"messages": [{"id": m["id"], "subject": m.get("subject"), "from": (m.get("from") or {}).get("emailAddress"),
                          "received": m.get("receivedDateTime"), "has_attachments": m.get("hasAttachments"),
                          "preview": (m.get("bodyPreview") or "")[:200]} for m in d.get("value", [])]}


async def t_read(a: ReadArgs, ctx):
    mid = _ol._id(a.message_id)
    if backend(ctx.app) == "com":
        return await asyncio.to_thread(oc.read, mid, a.max_chars)
    m = await _ol.graph(ctx, "GET", f"/me/messages/{mid}", params={"$select": "id,subject,from,toRecipients,receivedDateTime,body,hasAttachments,conversationId"},
                        headers={"Prefer": 'outlook.body-content-type="text"'})
    atts = []
    if m.get("hasAttachments"):
        at = await _ol.graph(ctx, "GET", f"/me/messages/{mid}/attachments", params={"$select": "id,name,size,contentType"})
        atts = [{"id": x["id"], "name": x.get("name"), "size": x.get("size"), "type": x.get("contentType")} for x in at.get("value", [])]
    return {"id": mid, "subject": m.get("subject"), "from": (m.get("from") or {}).get("emailAddress"),
            "to": [r["emailAddress"] for r in m.get("toRecipients", [])], "received": m.get("receivedDateTime"),
            "body": (m.get("body") or {}).get("content", "")[: a.max_chars], "attachments": atts}


def _check_dest(a: DownloadArgs, ctx):
    ctx.app.policy.resolve(a.dest_dir, write=True)


async def t_download(a: DownloadArgs, ctx):
    dest = ctx.app.policy.resolve(a.dest_dir, write=True)
    if backend(ctx.app) == "com":
        if not a.attachment_id.isdigit():
            raise PolicyError("With desktop Outlook, attachment_id is the index number from outlook_read")
        return await asyncio.to_thread(oc.download, _ol._id(a.message_id), int(a.attachment_id), dest)
    at = await _ol.graph(ctx, "GET", f"/me/messages/{_ol._id(a.message_id)}/attachments/{_ol._id(a.attachment_id)}")
    if "contentBytes" not in at:
        raise PolicyError("Only file attachments can be downloaded")
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", at.get("name") or "attachment")[:150]
    if BLOCKED_FILE.search(name):
        raise PolicyError("Attachment type is blocked by policy")
    dest.mkdir(parents=True, exist_ok=True)
    p = dest / name
    i = 1
    while p.exists():  # never overwrite
        p = dest / f"{Path(name).stem} ({i}){Path(name).suffix}"
        i += 1
    p.write_bytes(base64.b64decode(at["contentBytes"]))
    return {"saved": str(p), "size": p.stat().st_size}


async def t_draft(a: DraftArgs, ctx):
    if any("@" not in x for x in a.to + a.cc):
        a = a.model_copy(update={"to": (await resolve_names(ctx, a.to))[0], "cc": (await resolve_names(ctx, a.cc))[0]})
    if _window_mode(ctx):
        snap = await _compose_window(a, ctx)
        return {"draft": "open in an Outlook window (not sent)", "to": snap["to"], "cc": snap["cc"], "subject": snap["subject"],
                "attachments": snap["attachments"], "note": "Use send_email to send in one step next time; the user can also press Send."}
    files = [ctx.app.policy.resolve(fp, must_exist=True) for fp in a.attachment_paths]
    if backend(ctx.app) == "com":
        res = await asyncio.to_thread(
            oc.create_draft, a.to, a.cc, a.subject, a.body,
            _ol._id(a.reply_to_message_id) if a.reply_to_message_id else None, a.reply_all,
            _ol._id(a.forward_message_id) if a.forward_message_id else None, files)
        return {**res, "note": "Draft is open in Outlook and saved in Drafts. NOT sent. Use send_email_draft after the user approves."}
    for p in files:
        if p.stat().st_size > _ol.MAX_ATTACH:
            raise PolicyError(f"{p.name} exceeds the 3 MB attachment limit of the Graph path")
    body = {"contentType": "Text", "content": a.body}
    if a.reply_to_message_id or a.forward_message_id:
        src = _ol._id(a.reply_to_message_id or a.forward_message_id)
        action = "createReplyAll" if a.reply_all and a.reply_to_message_id else "createReply" if a.reply_to_message_id else "createForward"
        d = await _ol.graph(ctx, "POST", f"/me/messages/{src}/{action}")
        patch = {"body": body}
        if a.to:
            patch["toRecipients"] = _recips(a.to)
        if a.cc:
            patch["ccRecipients"] = _recips(a.cc)
        if a.forward_message_id and not a.to:
            raise PolicyError("Forward needs recipients (to)")
        await _ol.graph(ctx, "PATCH", f"/me/messages/{d['id']}", json=patch)
    else:
        if not a.to:
            raise PolicyError("At least one recipient is required")
        d = await _ol.graph(ctx, "POST", "/me/messages", json={"subject": a.subject, "body": body,
                                                               "toRecipients": _recips(a.to), "ccRecipients": _recips(a.cc)})
    for p in files:
        await _ol.graph(ctx, "POST", f"/me/messages/{d['id']}/attachments", json={
            "@odata.type": "#microsoft.graph.fileAttachment", "name": p.name,
            "contentBytes": base64.b64encode(p.read_bytes()).decode()})
    return {"draft_id": d["id"], "note": "Draft saved in Outlook Drafts. NOT sent. Use send_email_draft after the user approves."}


async def _fetch_draft(ctx, did):
    if backend(ctx.app) == "com":
        return await asyncio.to_thread(oc.fetch_draft, did)
    m = await _ol.graph(ctx, "GET", f"/me/messages/{did}", params={"$select": "id,subject,toRecipients,ccRecipients,bccRecipients,body,hasAttachments,isDraft"},
                        headers={"Prefer": 'outlook.body-content-type="text"'})
    if not m.get("isDraft"):
        raise PolicyError("That message is not a draft")
    atts = []
    if m.get("hasAttachments"):
        at = await _ol.graph(ctx, "GET", f"/me/messages/{did}/attachments", params={"$select": "name,size"})
        atts = [f"{x['name']} ({x.get('size', 0) // 1024} KB)" for x in at.get("value", [])]
    to = [r["emailAddress"]["address"] for r in m.get("toRecipients", [])]
    cc = [r["emailAddress"]["address"] for r in m.get("ccRecipients", [])]
    bcc = [r["emailAddress"]["address"] for r in m.get("bccRecipients", [])]
    body = (m.get("body") or {}).get("content", "")
    digest = hashlib.sha256(json.dumps([to, cc, bcc, m.get("subject"), body, atts]).encode()).hexdigest()
    return to, cc, bcc, m.get("subject"), body, atts, digest


async def send_preview(a: SendArgs, ctx):
    to, cc, bcc, subj, body, atts, digest = await _fetch_draft(ctx, _ol._id(a.draft_id))
    if not to:
        raise PolicyError("Draft has no recipients")
    text = (f"SEND EMAIL from {ctx.app.outlook.account() or 'your Outlook account'}\nTo: {', '.join(to)}\n"
            f"Cc: {', '.join(cc) or '-'}\nBcc: {', '.join(bcc) or '-'}\nSubject: {subj}\n"
            f"Attachments: {', '.join(atts) or 'none'}\n--- message ---\n{body[:3000]}")
    return {"text": text, "digest": digest}


async def t_send(a: SendArgs, ctx):
    did = _ol._id(a.draft_id)
    key = f"outlook:{did}"
    if ctx.app.db.one("SELECT 1 FROM sent WHERE key=?", (key,)):
        raise PolicyError("This draft was already sent. Not sending again.")
    *_, digest = await _fetch_draft(ctx, did)
    if digest != ctx.approved_digest:
        raise PolicyError("The draft changed after you approved it. Nothing was sent; request a new approval.")
    ctx.app.db.exec("INSERT INTO sent(key,ts) VALUES(?,strftime('%s','now'))", (key,))  # mark first: no duplicate on uncertain failure
    if backend(ctx.app) == "com":
        await asyncio.to_thread(oc.send, did)
        return {"sent": True, "note": "Outlook accepted the send (message moved to Outbox/Sent Items)."}
    await _ol.graph(ctx, "POST", f"/me/messages/{did}/send")
    return {"sent": True, "note": "Graph accepted the send request (202)."}


class SendEmailArgs(DraftArgs):
    pass


def _window_mode(ctx) -> bool:
    return backend(ctx.app) == "com" and not oc.is_trusted()


async def _compose_window(a, ctx) -> dict:
    from . import outlook_window as ow

    files = [ctx.app.policy.resolve(fp, must_exist=True) for fp in a.attachment_paths]
    body = strip_closing_block(a.body)  # Outlook adds the user's own signature below the text: no second 'Regards, name'
    return await asyncio.to_thread(ow.compose, a.to, a.cc, a.subject, body, files, ctx,
                                   _ol._id(a.reply_to_message_id) if a.reply_to_message_id else None, a.reply_all,
                                   _ol._id(a.forward_message_id) if a.forward_message_id else None)


def _window_text(snap: dict, ctx) -> str:
    return (f"SEND EMAIL (Outlook window on {ctx.device})\nTo: {snap['to'] or '-'}\nCc: {snap['cc'] or '-'}\nSubject: {snap['subject']}\n"
            f"Attachments: {', '.join(snap['attachments']) or 'none'}\n--- message (as typed in Outlook, above your signature) ---\n"
            f"{snap['body'][:2500]}")


async def quick_send_preview(a: SendEmailArgs, ctx):
    to, n1 = await resolve_names(ctx, a.to)
    cc, n2 = await resolve_names(ctx, a.cc)
    notes = n1 + n2
    a = a.model_copy(update={"to": to, "cc": cc})
    pv = await _quick_send_preview(a, ctx)
    if notes:
        pv["text"] = "Recipients: " + " | ".join(notes) + "\n\n" + pv["text"]
    return pv


async def _quick_send_preview(a: SendEmailArgs, ctx):
    if _window_mode(ctx):
        snap = await _compose_window(a, ctx)
        if not snap["to"].strip() and not a.reply_to_message_id:
            raise PolicyError("The To field is empty in Outlook's window; nothing will be sent. Check the recipient.")
        ctx.scratch["email_window"] = snap
        asked = ", ".join(a.to + a.cc)
        text = _window_text(snap, ctx)
        if asked:
            text = text.replace("\nCc:", f"  (you asked for: {asked})\nCc:", 1)
        return {"text": text, "digest": snap["digest"]}
    res = await t_draft(a, ctx)
    ctx.scratch["email_draft"] = res["draft_id"]
    return await send_preview(SendArgs(draft_id=res["draft_id"]), ctx)


async def t_quick_send(a: SendEmailArgs, ctx):
    snap = ctx.scratch.pop("email_window", None)
    if snap is not None:
        from . import outlook_window as ow

        key = f"outlook-win:{snap['digest']}:{ctx.task_id}"
        if ctx.app.db.one("SELECT 1 FROM sent WHERE key=?", (key,)):
            raise PolicyError("This email was already sent in this task. Not sending again.")
        now = await asyncio.to_thread(ow.snapshot, snap["hwnd"], snap["attachments"])
        if now["digest"] != ctx.approved_digest:
            raise PolicyError("The email in Outlook's window changed after you approved it. Nothing was sent; it is still open for you.")
        ctx.app.db.exec("INSERT INTO sent(key,ts) VALUES(?,strftime('%s','now'))", (key,))
        res = await asyncio.to_thread(ow.press_send, snap["hwnd"])
        _remember_sent(ctx, a.to + a.cc)
        return res
    did = ctx.scratch.pop("email_draft", None)
    if not did:
        raise PolicyError("No prepared draft; nothing sent")
    res = await t_send(SendArgs(draft_id=did), ctx)
    _remember_sent(ctx, a.to + a.cc)
    return res


class ListArgs(Args):
    since: str = Field("today", description="'today', 'yesterday', '3d' (last 3 days), '12h', 'this week' or a date 'YYYY-MM-DD'")
    until: str | None = Field(None, description="Optional end date 'YYYY-MM-DD' (inclusive) or 'today'")
    folders: str = Field("inbox_and_subfolders", pattern="^(inbox|inbox_and_subfolders|sent)$",
                         description="Received mail lives in the Inbox and its rule subfolders; Focused/Other are both in Inbox")
    unread_only: bool = False
    sender: str | None = Field(None, max_length=100, description="Only mail whose sender name/email contains this")
    limit: int = Field(200, ge=1, le=500)


class ReadManyArgs(Args):
    ids: list[str] = Field(min_length=1, max_length=25, description="Message ids from outlook_list_messages / outlook_search (max 25 per call)")
    max_chars: int = Field(1200, ge=100, le=6000, description="Body characters per mail (quoted reply chains are removed)")


async def t_list(a: ListArgs, ctx):
    if backend(ctx.app) == "com":
        res = await asyncio.to_thread(oc.list_messages, a.since, a.until, a.folders, a.unread_only, a.sender, a.limit)
        _learn_from_messages(ctx, res.get("messages"))
        return res
    start, end = oc.parse_since(a.since, a.until)
    folder = "sentitems" if a.folders == "sent" else "inbox"
    flt = f"receivedDateTime ge {start.astimezone().isoformat()}" + (f" and receivedDateTime lt {end.astimezone().isoformat()}" if end else "")
    if a.unread_only:
        flt += " and isRead eq false"
    d = await _ol.graph(ctx, "GET", f"/me/mailFolders/{folder}/messages", params={
        "$filter": flt, "$orderby": "receivedDateTime desc", "$top": min(a.limit, 100),
        "$select": "id,subject,from,toRecipients,ccRecipients,receivedDateTime,isRead,hasAttachments,bodyPreview"})
    msgs = []
    for m in d.get("value", []):
        fr = (m.get("from") or {}).get("emailAddress", {})
        if a.sender and a.sender.lower() not in f"{fr.get('name', '')} {fr.get('address', '')}".lower():
            continue
        msgs.append({"id": m["id"], "received": m.get("receivedDateTime"), "from": fr.get("name"), "from_email": fr.get("address"),
                     "to": "; ".join(r["emailAddress"].get("name", "") for r in m.get("toRecipients", [])),
                     "subject": m.get("subject"), "unread": not m.get("isRead"), "preview": (m.get("bodyPreview") or "")[:300]})
    return {"count": len(msgs), "messages": msgs}


async def t_read_many(a: ReadManyArgs, ctx):
    ids = [_ol._id(i) for i in a.ids]
    if backend(ctx.app) == "com":
        msgs = await asyncio.to_thread(oc.read_many, ids, a.max_chars)
        _learn_from_messages(ctx, msgs)
        return {"messages": msgs}
    out = []
    for i in ids:
        try:
            out.append(await t_read(ReadArgs(message_id=i, max_chars=a.max_chars), ctx))
        except PolicyError as e:
            out.append({"id": i, "error": str(e)[:150]})
    return {"messages": out}


def register(reg: Registry) -> None:
    R = Risk
    reg.register(Tool("send_email", "FASTEST way to email (also called mail) in ONE call: recipients may be NAMES or SHORT FORMS (KVR, Suresh, Srini) - each is resolved to the right person (if several match equally, the one you mail most) and shown on the approval card. Creates the draft, gets approval, sends. Use reply_to_message_id to reply.", SendEmailArgs, t_quick_send, R.HIGH, 150, preview=quick_send_preview, reversible=False))
    reg.register(Tool("outlook_list_messages", "List mail by DATE (complete and current, no keywords needed): e.g. since='today'. Use for 'mails received today/yesterday/this week', unread mail, or all mail from a sender in a period.", ListArgs, t_list, R.READ, 120, untrusted=True))
    reg.register(Tool("outlook_read_many", "Read up to 25 emails in ONE call (sender, To, Cc, cleaned body). Use after outlook_list_messages/outlook_search.", ReadManyArgs, t_read_many, R.READ, 240, untrusted=True))
    reg.register(Tool("outlook_find_person", "Find people by NAME or SHORT FORM (initials like KVR, first name, nickname) in contacts, recent mail and directory. Returns the picked person (best match; if tied, the one you mail most) and the alternatives.", FindPersonArgs, t_find_person, R.READ, 40, untrusted=True))
    reg.register(Tool("outlook_search", "Search the mailbox. Every word must match subject/sender (or body). Works by person NAME too.", SearchArgs, t_search, R.READ, 60, untrusted=True))
    reg.register(Tool("outlook_read", "Read an email (body as text) and list its attachments.", ReadArgs, t_read, R.READ, 60, untrusted=True))
    reg.register(Tool("outlook_download_attachment", "Download an attachment into a write-enabled authorised folder (never overwrites).", DownloadArgs, t_download, R.LOW, 90, check=_check_dest))
    reg.register(Tool("outlook_create_draft", "Create a draft new email/reply/forward; recipients may be NAMES with desktop Outlook. Does NOT send.", DraftArgs, t_draft, R.LOW, 120))
    reg.register(Tool("send_email_draft", "Send an existing draft. The user sees recipients, subject, body, attachments and must approve.", SendArgs, t_send, R.HIGH, 90, preview=send_preview, reversible=False))
