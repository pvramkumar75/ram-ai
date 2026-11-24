"""People directory: understands short forms of names and picks the right person.

  'KVR'    -> G. Srinivasa Rao / kvr@company.com      (initials, or the part of an address before @)
  'Suresh'  -> Suresh Babu                                 (first name / start of a name)
  'Srini'  -> Srinivas Reddy                            (common nicknames)
  'PSR sir'-> PSR TPL                                   (honorifics ignored)

It learns from everything RAM AI sees and does (people in mail it read, chats in WhatsApp, every message sent) and
remembers which short form meant whom. When several people match equally well it picks the one you interact with most
(mails and messages exchanged, most recent first) and says so, so the approval card shows exactly who was chosen."""
from __future__ import annotations

import difflib
import json
import re
import time

HONORIFICS = {"mr", "mrs", "ms", "miss", "dr", "sir", "madam", "mam", "maam", "garu", "ji", "saab", "sahab", "sahib", "shri",
              "smt", "prof", "er"}
NICKNAMES = {  # short form -> starts of full names it usually stands for
    "srini": ["srinivas"], "seenu": ["srinivas"], "cheenu": ["srinivas"], "venky": ["venkat"], "venkat": ["venkatesh", "venkata"],
    "subbu": ["subba", "subra"], "bala": ["bala"], "krish": ["krishna"], "kittu": ["krishna"], "raj": ["raj"], "ram": ["ram"],
    "siva": ["siva", "shiva"], "shiva": ["siva", "shiva"], "nagu": ["naga"], "chandu": ["chandra"], "satya": ["satya"],
    "ravi": ["ravi"], "suri": ["suresh"], "pappu": [], "babu": [], "anil": ["anil"], "sunny": ["sunil"], "vijju": ["vijay"],
    "bob": ["robert"], "rob": ["robert"], "bill": ["william"], "will": ["william"], "mike": ["michael"], "jim": ["james"],
    "tom": ["thomas"], "dave": ["david"], "chris": ["christopher", "christine"], "alex": ["alexander", "alexandra"],
    "kate": ["katherine", "catherine"], "liz": ["elizabeth"], "beth": ["elizabeth"], "sam": ["samuel", "samantha"],
    "dan": ["daniel"], "nick": ["nicholas"], "tony": ["anthony"], "joe": ["joseph"], "steve": ["steven", "stephen"],
}
TIE = 0.05  # scores this close count as "equally good" -> the person you interact with most wins
EMAIL_RE = re.compile(r"[\w.+'-]+@[\w-]+(?:\.[\w-]+)+")


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s@.]", " ", (s or "").lower())).strip()


def clean_query(q: str) -> str:
    toks = [t for t in norm(q).replace(".", " ").split() if t not in HONORIFICS]
    return " ".join(toks) or norm(q)


def name_tokens(name: str) -> list[str]:
    n = re.sub(r"\(.*?\)|<.*?>", " ", name or "")
    return [t for t in re.sub(r"[^\w\s]", " ", n.lower()).split() if t and t not in HONORIFICS]


def initials(name: str) -> str:
    return "".join(t[0] for t in name_tokens(name))


def local_part(email: str) -> str:
    return (email or "").split("@")[0].lower()


def _ordered_prefixes(q: list[str], toks: list[str]) -> bool:
    """Every query word starts a different name word, in order: 'srini rao' ~ 'Srinivasa Rao', 'suresh' ~ 'Suresh Babu'."""
    i = 0
    for w in q:
        while i < len(toks) and not toks[i].startswith(w):
            i += 1
        if i == len(toks):
            return False
        i += 1
    return True


def match(query: str, name: str = "", email: str = "") -> tuple[float, str]:
    """How well a short form fits a person. Returns (score 0..1, why)."""
    q = clean_query(query)
    if not q:
        return 0.0, ""
    qn = q.replace(" ", "")
    nm, toks, lp = norm(name), name_tokens(name), local_part(email)
    if email and q == email.lower():
        return 1.0, "exact address"
    if nm and (q == nm or qn == nm.replace(" ", "")):
        return 1.0, "exact name"
    if lp and qn == re.sub(r"[^a-z0-9]", "", lp):
        return 0.95, "address starts with it"
    ini = initials(name)
    if len(qn) >= 2 and qn.isalpha() and " " not in q:
        if ini == qn:
            return 0.92, "initials"
        if any(t == qn for t in toks):                   # 'KVR' is literally a word in the chat/contact name
            return 0.93, "name contains it"
        if len(qn) >= 3 and ini.startswith(qn):
            return 0.82, "initials"
        if len(qn) <= 4 and lp and (lp.startswith(qn) or re.sub(r"[^a-z]", "", lp) == qn):
            return 0.85, "address starts with it"
    qt = q.split()
    if toks and _ordered_prefixes(qt, toks):
        full_words = sum(1 for w in qt if w in toks)
        return (0.9 if full_words == len(qt) else 0.84), "start of the name"
    for w in qt:
        for full in NICKNAMES.get(w, []):
            if any(t.startswith(full) for t in toks) and all(any(t.startswith(x) for t in toks) for x in qt if x != w):
                return 0.8, f"'{w}' is short for {full.title()}"
    if len(qn) >= 3 and (qn in nm.replace(" ", "") or (lp and qn in lp)):
        return 0.6, "part of the name"
    if len(qn) >= 4:
        best = max((difflib.SequenceMatcher(None, qn, t).ratio() for t in toks + ([lp] if lp else [])), default=0)
        if best >= 0.84:
            return 0.62, "similar spelling"
    return 0.0, ""


# ------------------------------------------------------------------ the directory
SCHEMA = """
CREATE TABLE IF NOT EXISTS people(key TEXT PRIMARY KEY, name TEXT, email TEXT, wa_title TEXT, sources TEXT,
  seen INTEGER DEFAULT 0, mailed INTEGER DEFAULT 0, messaged INTEGER DEFAULT 0, last REAL);
CREATE TABLE IF NOT EXISTS aliases(alias TEXT, channel TEXT, key TEXT, uses INTEGER, updated REAL, PRIMARY KEY(alias, channel));
"""


class People:
    def __init__(self, db):
        self.db = db
        with db._lock:
            db.conn.executescript(SCHEMA)
            db.conn.commit()

    # ---- learning
    def note(self, name: str = "", email: str = "", wa_title: str = "", source: str = "", sent: str | None = None) -> str | None:
        """Remember a person (from mail read, a chat seen, a message sent). sent='mail'|'whatsapp' counts an interaction."""
        name, email, wa_title = (name or "").strip()[:120], (email or "").strip().lower()[:200], (wa_title or "").strip()[:120]
        if email:  # accept 'Name <a@b.com>' / '<a@b.com>' forms as Outlook shows them
            m = EMAIL_RE.search(email)
            email = m.group(0).lower() if m else ""
        if not (email or wa_title):
            return None
        key = email or "wa:" + norm(wa_title)
        row = self.db.one("SELECT * FROM people WHERE key=?", (key,))
        now = time.time()
        if row:
            srcs = set(filter(None, (row["sources"] or "").split(","))) | ({source} if source else set())
            self.db.exec("UPDATE people SET name=COALESCE(NULLIF(?,''),name), wa_title=COALESCE(NULLIF(?,''),wa_title), sources=?, "
                         "seen=seen+1, mailed=mailed+?, messaged=messaged+?, last=? WHERE key=?",
                         (name, wa_title, ",".join(sorted(srcs)), int(sent == "mail"), int(sent == "whatsapp"), now, key))
        else:
            self.db.exec("INSERT INTO people(key,name,email,wa_title,sources,seen,mailed,messaged,last) VALUES(?,?,?,?,?,?,?,?,?)",
                         (key, name or wa_title, email, wa_title, source, 1, int(sent == "mail"), int(sent == "whatsapp"), now))
        return key

    def note_from_line(self, line: str, source: str) -> None:
        """'Ravi Kumar <ravi@x.com>; Asha <asha@y.com>' -> remembered."""
        for part in re.split(r"[;,]\s*(?=[^<>]*(?:<|$))", line or ""):
            em = EMAIL_RE.search(part)
            if em:
                self.note(re.sub(r"<.*?>|[\"']", "", part).strip(" ;,") or em.group(0), em.group(0), source=source)

    def learn(self, alias: str, channel: str, key: str) -> None:
        a = clean_query(alias)
        if not a or not key or a == norm(key.removeprefix("wa:")):
            return
        self.db.exec("INSERT INTO aliases(alias,channel,key,uses,updated) VALUES(?,?,?,1,?) "
                     "ON CONFLICT(alias,channel) DO UPDATE SET key=excluded.key, uses=CASE WHEN aliases.key=excluded.key "
                     "THEN aliases.uses+1 ELSE 1 END, updated=excluded.updated", (a, channel, key, time.time()))

    def forget_alias(self, alias: str, channel: str) -> None:
        self.db.exec("DELETE FROM aliases WHERE alias=? AND channel=?", (clean_query(alias), channel))

    def bootstrap_from_audit(self) -> int:
        """First run: learn from past sends recorded in the audit log (recipients and WhatsApp chats)."""
        if self.db.one("SELECT 1 FROM memory WHERE key='people_bootstrapped'"):
            return 0
        n = 0
        for r in self.db.query("SELECT tool, data FROM audit WHERE event='tool_result' AND tool IN "
                               "('whatsapp_send_message','whatsapp_send_prepared','send_email','send_email_draft')"):
            try:
                d = json.loads(r["data"] or "{}").get("result") or {}
            except Exception:
                continue
            sent_ok = d.get("sent") is True or d.get("sent_verified") is True
            if r["tool"].startswith("whatsapp") and sent_ok and d.get("chat"):
                self.note(wa_title=d["chat"], source="whatsapp", sent="whatsapp")
                n += 1
        for r in self.db.query("SELECT data FROM audit WHERE event='tool_call' AND tool='send_email'"):
            for em in EMAIL_RE.findall(r["data"] or ""):
                self.note(email=em, source="mail", sent="mail")
                n += 1
        self.db.exec("INSERT OR REPLACE INTO memory(key,value,updated) VALUES('people_bootstrapped','1',?)", (time.time(),))
        return n

    # ---- finding
    def candidates(self, query: str, channel: str, limit: int = 8) -> list[dict]:
        """People who fit the short form, best first. channel: 'mail' (needs an address) or 'whatsapp' (needs a chat)."""
        q = clean_query(query)
        out = []
        al = self.db.one("SELECT * FROM aliases WHERE alias=? AND channel=?", (q, channel))
        for p in self.db.query("SELECT * FROM people"):
            if channel == "mail" and not p["email"]:
                continue
            label = p["wa_title"] if channel == "whatsapp" and p["wa_title"] else p["name"]
            s, why = max(match(q, label, p["email"]), match(q, p["name"], p["email"]))
            if al and al["key"] == p["key"]:
                s, why = 1.01, f"you used '{q}' for this person before"
            if s > 0:
                out.append({**p, "score": round(s, 2), "why": why,
                            "interactions": (p["mailed"] * 3 + p["messaged"] * 3 + p["seen"])})
        out.sort(key=lambda x: (-x["score"], -x["interactions"], -(x["last"] or 0)))
        return out[:limit]


def rank(cands: list[dict], counts: dict | None = None) -> list[dict]:
    """Best match first; among equally good matches (within TIE), the person you interact with most."""
    counts = counts or {}
    for c in cands:
        c["interactions"] = c.get("interactions", 0) + counts.get(c.get("key") or c.get("email") or c.get("title"), 0)
    if not cands:
        return []
    top = max(c["score"] for c in cands)
    tier = [c for c in cands if c["score"] >= top - TIE]
    rest = [c for c in cands if c["score"] < top - TIE]
    tier.sort(key=lambda c: (-c["interactions"], -c["score"], c.get("order", 0)))
    rest.sort(key=lambda c: (-c["score"], -c["interactions"], c.get("order", 0)))
    return tier + rest


def explain(query: str, ranked: list[dict], label=lambda c: c.get("name")) -> str:
    """One line for the approval card: who was picked for the short form and why, and who else matched."""
    if not ranked:
        return ""
    p = ranked[0]
    tied = [c for c in ranked[1:] if c["score"] >= p["score"] - TIE]
    why = p.get("why") or "best match"
    if tied:
        why += f"; picked as the one you interact with most ({p['interactions']} vs {tied[0]['interactions']})"
    others = ", ".join(label(c) for c in ranked[1:4])
    return f"'{query}' -> {label(p)} ({why})" + (f". Also matched: {others}" if others else "")
