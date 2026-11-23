"""Local file intelligence: search, read, spreadsheets, safe writes, calculations, memory."""
from __future__ import annotations

import ast
import csv
import datetime as dt
import operator
import os
import shutil
import time
from pathlib import Path
from typing import Any

from pydantic import Field

from ..core import redact
from ..policy import DOC_EXT, TEXT_EXT, PolicyError, Risk, is_blocked
from .base import Args, Registry, Tool

MAX_FILE_BYTES = 40 * 1024 * 1024


# ------------------------------------------------------------------ text extraction
def extract_text(p: Path, limit: int = 200_000) -> str:
    ext = p.suffix.lower()
    if p.stat().st_size > MAX_FILE_BYTES:
        raise PolicyError("File too large (>40MB)")
    if ext in TEXT_EXT:
        return p.read_bytes()[: limit * 2].decode("utf-8", errors="replace")[:limit]
    if ext == ".pdf":
        from pypdf import PdfReader

        out, n = [], 0
        for i, page in enumerate(PdfReader(str(p)).pages):
            t = page.extract_text() or ""
            out.append(f"[page {i + 1}]\n{t}")
            n += len(t)
            if n > limit:
                break
        return "\n".join(out)[:limit]
    if ext == ".docx":
        import docx

        d = docx.Document(str(p))
        parts = [para.text for para in d.paragraphs]
        for tb in d.tables:
            for row in tb.rows:
                parts.append("\t".join(c.text for c in row.cells))
        return "\n".join(parts)[:limit]
    if ext in (".xlsx", ".xlsm"):
        import openpyxl

        wb = openpyxl.load_workbook(str(p), read_only=True, data_only=True)
        out, n = [], 0
        for ws in wb.worksheets:
            out.append(f"[sheet {ws.title}]")
            for row in ws.iter_rows(values_only=True):
                line = "\t".join("" if v is None else str(v) for v in row)
                out.append(line)
                n += len(line)
                if n > limit:
                    break
            if n > limit:
                break
        wb.close()
        return "\n".join(out)[:limit]
    if ext == ".pptx":
        from pptx import Presentation

        out = []
        for i, s in enumerate(Presentation(str(p)).slides, 1):
            out.append(f"[slide {i}]")
            for sh in s.shapes:
                if sh.has_text_frame:
                    out.append(sh.text_frame.text)
        return "\n".join(out)[:limit]
    raise PolicyError(f"Unsupported file type {ext}")


# ------------------------------------------------------------------ args
class ListRootsArgs(Args):
    pass


class SearchArgs(Args):
    query: str = Field(max_length=300, description="Space-separated terms; all must match (case-insensitive)")
    root: str | None = Field(None, description="Restrict to this authorised folder")
    extensions: list[str] = Field(default_factory=list, max_length=12, description="e.g. ['.pdf','.xlsx']")
    modified_after: str | None = Field(None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    modified_before: str | None = Field(None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    search_content: bool = False
    max_results: int = Field(40, ge=1, le=200)


class ReadArgs(Args):
    path: str
    max_chars: int = Field(20000, ge=100, le=100000)
    offset: int = Field(0, ge=0)


class SheetArgs(Args):
    path: str
    sheet: str | None = None
    max_rows: int = Field(200, ge=1, le=2000)
    start_row: int = Field(1, ge=1)


class WriteTextArgs(Args):
    path: str
    content: str = Field(max_length=500_000)
    overwrite: bool = False


class WorkbookArgs(Args):
    path: str = Field(description="Must end with .xlsx")
    sheets: dict[str, list[list[Any]]] = Field(description="sheet name -> rows (list of cell values)")
    overwrite: bool = False


class MoveArgs(Args):
    src: str
    dst: str


class DeleteArgs(Args):
    path: str


class CopyArgs(Args):
    src: str
    dst: str
    overwrite: bool = False


class CalcArgs(Args):
    expression: str = Field(max_length=500, description="Arithmetic only, e.g. '(100*5.2+50*5.5)/150'. Functions: sum,min,max,round,abs,len")


class WAvgArgs(Args):
    values: list[float] = Field(min_length=1, max_length=5000)
    weights: list[float] = Field(min_length=1, max_length=5000)


class RememberArgs(Args):
    key: str = Field(max_length=80, pattern=r"^[\w .,&/()'\-]+$")
    value: str = Field(max_length=1000)


class RecallArgs(Args):
    pass


class ForgetArgs(Args):
    key: str = Field(max_length=80)


# ------------------------------------------------------------------ implementations
def _roots(ctx):
    return ctx.app.policy.roots()


def t_list_roots(a, ctx):
    return {"roots": _roots(ctx), "output_dir": ctx.app.settings.get("output_dir")}


def t_search(a: SearchArgs, ctx):
    pol = ctx.app.policy
    terms = [t.lower() for t in a.query.split() if t]
    exts = {e.lower() if e.startswith(".") else "." + e.lower() for e in a.extensions}
    after = dt.datetime.fromisoformat(a.modified_after).timestamp() if a.modified_after else None
    before = (dt.datetime.fromisoformat(a.modified_before).timestamp() + 86400) if a.modified_before else None
    if a.root:
        starts = [str(pol.resolve(a.root))]
    else:
        from ..policy import ELEVATED

        starts = [str(Path.home())] if ELEVATED.get() else [r["path"] for r in _roots(ctx)]
    deadline = time.time() + 25
    results, scanned, truncated = [], 0, False
    for start in starts:
        for dirpath, dirnames, filenames in os.walk(start):
            if ctx.stopped() or time.time() > deadline:
                truncated = True
                break
            dirnames[:] = [d for d in dirnames if not is_blocked(Path(dirpath) / d)]
            for fn in filenames:
                p = Path(dirpath) / fn
                if is_blocked(p):
                    continue
                ext = p.suffix.lower()
                if exts and ext not in exts:
                    continue
                if not exts and a.search_content and ext not in DOC_EXT:
                    continue
                try:
                    st = p.stat()
                except OSError:
                    continue
                if (after and st.st_mtime < after) or (before and st.st_mtime > before):
                    continue
                scanned += 1
                name_l = fn.lower()
                snippet = None
                if all(t in name_l or t in str(p.parent).lower() for t in terms):
                    hit = True
                elif a.search_content and ext in DOC_EXT and st.st_size < 15 * 1024 * 1024:
                    try:
                        text = extract_text(p, 100_000)
                    except Exception:
                        continue
                    tl = text.lower()
                    hit = all(t in tl for t in terms)
                    if hit and terms:
                        i = tl.find(terms[0])
                        snippet = text[max(0, i - 80): i + 160].replace("\n", " ")
                else:
                    hit = False
                if hit:
                    results.append({"path": str(p), "size": st.st_size,
                                    "modified": dt.datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d"),
                                    **({"snippet": snippet} if snippet else {})})
                    if len(results) >= a.max_results:
                        truncated = True
                        break
            if len(results) >= a.max_results:
                break
    return {"count": len(results), "scanned": scanned, "truncated": truncated, "results": results}


def t_read(a: ReadArgs, ctx):
    p = ctx.app.policy.resolve(a.path, must_exist=True)
    text = extract_text(p, a.offset + a.max_chars)
    return {"path": str(p), "total_chars_read": len(text), "content": text[a.offset: a.offset + a.max_chars]}


def t_sheet(a: SheetArgs, ctx):
    p = ctx.app.policy.resolve(a.path, must_exist=True)
    ext = p.suffix.lower()
    if ext in (".csv", ".tsv"):
        with open(p, newline="", encoding="utf-8", errors="replace") as f:
            rows = list(csv.reader(f, delimiter="\t" if ext == ".tsv" else ","))
        sel = rows[a.start_row - 1: a.start_row - 1 + a.max_rows]
        return {"path": str(p), "total_rows": len(rows), "rows": sel}
    import openpyxl

    wb = openpyxl.load_workbook(str(p), read_only=True, data_only=True)
    names = wb.sheetnames
    ws = wb[a.sheet] if a.sheet and a.sheet in names else wb.worksheets[0]
    rows = []
    for i, row in enumerate(ws.iter_rows(values_only=True), 1):
        if i < a.start_row:
            continue
        if len(rows) >= a.max_rows:
            break
        rows.append([None if v is None else (v.isoformat() if hasattr(v, "isoformat") else v) for v in row])
    wb.close()
    return {"path": str(p), "sheets": names, "sheet": ws.title, "rows": rows}


def _write_risk(arg_path_attr: str = "path"):
    def f(a):  # decide by existence only; policy.resolve at run time enforces roots
        try:
            return Risk.HIGH if Path(getattr(a, arg_path_attr)).exists() else Risk.LOW
        except OSError:
            return Risk.HIGH

    return f


def _check_write(attr="path"):
    def f(a, ctx):
        ctx.app.policy.resolve(getattr(a, attr), write=True)

    return f


def t_write_text(a: WriteTextArgs, ctx):
    p = ctx.app.policy.resolve(a.path, write=True)
    if p.exists() and not a.overwrite:
        raise PolicyError("File exists; set overwrite=true (requires approval)")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(a.content, encoding="utf-8")
    return {"written": str(p), "chars": len(a.content)}


def t_workbook(a: WorkbookArgs, ctx):
    if not a.path.lower().endswith(".xlsx"):
        raise PolicyError("Path must end with .xlsx")
    p = ctx.app.policy.resolve(a.path, write=True)
    if p.exists() and not a.overwrite:
        raise PolicyError("File exists; set overwrite=true (requires approval)")
    import openpyxl

    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    for name, rows in a.sheets.items():
        ws = wb.create_sheet(title=str(name)[:31] or "Sheet")
        for r in rows[:100000]:
            ws.append([c if isinstance(c, (int, float, str, type(None), bool)) else str(c) for c in r])
    p.parent.mkdir(parents=True, exist_ok=True)
    wb.save(str(p))
    return {"written": str(p), "sheets": list(a.sheets)}


def t_copy(a: CopyArgs, ctx):
    s = ctx.app.policy.resolve(a.src, must_exist=True)
    d = ctx.app.policy.resolve(a.dst, write=True)
    if d.exists() and not a.overwrite:
        raise PolicyError("Destination exists; set overwrite=true (requires approval)")
    d.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(s, d)
    return {"copied": str(d)}


def t_move(a: MoveArgs, ctx):
    s = ctx.app.policy.resolve(a.src, write=True, must_exist=True)
    d = ctx.app.policy.resolve(a.dst, write=True)
    if d.exists():
        raise PolicyError("Destination already exists")
    d.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(s), str(d))
    return {"moved_to": str(d)}


def t_delete(a: DeleteArgs, ctx):
    from send2trash import send2trash

    p = ctx.app.policy.resolve(a.path, write=True, must_exist=True)
    if p.is_dir():
        raise PolicyError("Deleting folders is not supported")
    send2trash(str(p))  # recoverable: goes to the Recycle Bin
    return {"moved_to_recycle_bin": str(p)}


_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
        ast.Pow: operator.pow, ast.Mod: operator.mod, ast.USub: operator.neg, ast.UAdd: operator.pos}
_FUNCS = {"sum": sum, "min": min, "max": max, "round": round, "abs": abs, "len": len}


def safe_eval(expr: str):
    def ev(n):
        if isinstance(n, ast.Expression):
            return ev(n.body)
        if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)):
            return n.value
        if isinstance(n, ast.BinOp) and type(n.op) in _OPS:
            l, r = ev(n.left), ev(n.right)
            if isinstance(n.op, ast.Pow) and abs(r) > 100:
                raise ValueError("exponent too large")
            return _OPS[type(n.op)](l, r)
        if isinstance(n, ast.UnaryOp) and type(n.op) in _OPS:
            return _OPS[type(n.op)](ev(n.operand))
        if isinstance(n, (ast.List, ast.Tuple)):
            return [ev(e) for e in n.elts]
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in _FUNCS and not n.keywords:
            return _FUNCS[n.func.id](*[ev(x) for x in n.args])
        raise ValueError("unsupported expression")

    return ev(ast.parse(expr, mode="eval"))


def t_calc(a: CalcArgs, ctx):
    try:
        return {"result": safe_eval(a.expression)}
    except Exception as e:
        raise PolicyError(f"Cannot evaluate: {e}")


def t_wavg(a: WAvgArgs, ctx):
    if len(a.values) != len(a.weights):
        raise PolicyError("values and weights must have the same length")
    tw = sum(a.weights)
    if tw == 0:
        raise PolicyError("weights sum to zero")
    return {"weighted_average": sum(v * w for v, w in zip(a.values, a.weights)) / tw, "total_weight": tw}


def _mem_preview(a: RememberArgs, ctx):
    async def _():
        return {"text": f"Save to memory:\n  {a.key} = {a.value}", "digest": None}

    return _()


def t_remember(a: RememberArgs, ctx):
    if redact(a.value) != a.value or redact(a.key) != a.key:
        raise PolicyError("Looks like a secret; secrets are never stored in memory")
    ctx.app.db.exec("INSERT OR REPLACE INTO memory(key,value,updated) VALUES(?,?,?)", (a.key, a.value, time.time()))
    return {"saved": a.key}


def t_recall(a, ctx):
    return {"memory": ctx.app.db.query("SELECT key,value FROM memory ORDER BY key")}


def t_forget(a: ForgetArgs, ctx):
    ctx.app.db.exec("DELETE FROM memory WHERE key=?", (a.key,))
    return {"forgotten": a.key}


class ListFolderArgs(Args):
    path: str = Field(description="Absolute folder path, e.g. C:\\Users\\<you>\\Desktop")
    max_items: int = Field(300, ge=1, le=2000)


class CreateFolderArgs(Args):
    path: str


class MoveItem(Args):
    src: str
    dst: str = Field(description="Full destination path (folder + file name)")


class MoveManyArgs(Args):
    moves: list[MoveItem] = Field(min_length=1, max_length=500)


def t_list_folder(a: ListFolderArgs, ctx):
    p = ctx.app.policy.resolve(a.path, must_exist=True)
    if not p.is_dir():
        raise PolicyError("Not a folder")
    out = []
    for e in sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
        try:
            st = e.stat()
        except OSError:
            continue
        out.append({"name": e.name, "type": "folder" if e.is_dir() else (e.suffix.lower() or "file"),
                    "size": None if e.is_dir() else st.st_size,
                    "modified": dt.datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d")})
        if len(out) >= a.max_items:
            break
    return {"folder": str(p), "count": len(out), "items": out}


def t_create_folder(a: CreateFolderArgs, ctx):
    p = ctx.app.policy.resolve(a.path, write=True)
    existed = p.exists()
    p.mkdir(parents=True, exist_ok=True)
    return {"folder": str(p), "already_existed": existed}


def _moves_preview(a: MoveManyArgs, ctx):
    async def _():
        lines = [f"MOVE {len(a.moves)} item(s):"] + [f"  {m.src}  ->  {m.dst}" for m in a.moves[:60]]
        if len(a.moves) > 60:
            lines.append(f"  … and {len(a.moves) - 60} more")
        return {"text": "\n".join(lines), "digest": None}

    return _()


def _check_moves(a: MoveManyArgs, ctx):
    for m in a.moves:
        ctx.app.policy.resolve(m.src, write=True, must_exist=True)
        ctx.app.policy.resolve(m.dst, write=True)


def t_move_many(a: MoveManyArgs, ctx):
    done, failed = [], []
    for m in a.moves:
        if ctx.stopped():
            failed.append({"src": m.src, "error": "stopped"})
            continue
        try:
            s = ctx.app.policy.resolve(m.src, write=True, must_exist=True)
            d = ctx.app.policy.resolve(m.dst, write=True)
            if d.exists():
                raise PolicyError("destination exists (not overwritten)")
            d.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(s), str(d))
            done.append(str(d))
        except Exception as e:
            failed.append({"src": m.src, "error": str(e)[:150]})
    return {"moved": len(done), "failed": failed, "destinations": done[:100]}


def register(reg: Registry) -> None:
    R = Risk
    reg.register(Tool("list_folder", "List files and sub-folders of a folder (name, type, size, modified). Use this instead of File Explorer.", ListFolderArgs, t_list_folder, R.READ, 20, untrusted=True))
    reg.register(Tool("create_folder", "Create a folder (and parents).", CreateFolderArgs, t_create_folder, R.LOW, 10, check=lambda a, ctx: ctx.app.policy.resolve(a.path, write=True)))
    reg.register(Tool("move_files", "Move/rename MANY files in one call (e.g. organise a folder). Never overwrites. Needs one approval for the whole list.", MoveManyArgs, t_move_many, R.HIGH, 120, preview=_moves_preview, check=_check_moves, reversible=True))
    reg.register(Tool("list_approved_folders", "List authorised folders and the output folder.", ListRootsArgs, t_list_roots, R.READ, 10))
    reg.register(Tool("search_files", "Search authorised folders by file name (and optionally content). Returns source paths.", SearchArgs, t_search, R.READ, 40, untrusted=True))
    reg.register(Tool("read_file", "Read text from a PDF/DOCX/XLSX/PPTX/CSV/TXT/MD file in an authorised folder.", ReadArgs, t_read, R.READ, 40, untrusted=True))
    reg.register(Tool("read_sheet", "Read rows from an XLSX/CSV file as arrays.", SheetArgs, t_sheet, R.READ, 40, untrusted=True))
    reg.register(Tool("write_text_file", "Create a text file in a write-enabled folder. Overwriting needs approval.", WriteTextArgs, t_write_text, _write_risk(), 30, check=_check_write(), reversible=False))
    reg.register(Tool("create_workbook", "Create an .xlsx workbook in a write-enabled folder. Overwriting needs approval.", WorkbookArgs, t_workbook, _write_risk(), 60, check=_check_write(), reversible=False))
    reg.register(Tool("copy_file", "Copy a file into a write-enabled folder. Overwriting needs approval.", CopyArgs, t_copy, _write_risk("dst"), 60, check=_check_write("dst"), reversible=False))
    reg.register(Tool("move_file", "Move/rename a file (always needs approval).", MoveArgs, t_move, R.HIGH, 60, check=_check_write("dst"), reversible=False))
    reg.register(Tool("delete_file", "Send a file to the Recycle Bin (always needs approval).", DeleteArgs, t_delete, R.HIGH, 30, check=_check_write(), reversible=True))
    reg.register(Tool("calculate", "Evaluate an arithmetic expression exactly. Use for all numeric calculations.", CalcArgs, t_calc, R.READ, 5))
    reg.register(Tool("weighted_average", "Compute a weighted average of values by weights.", WAvgArgs, t_wavg, R.READ, 5))
    reg.register(Tool("remember", "Save a durable fact about the user's work (people, clients, suppliers, preferences, formats, ongoing projects). Shown in Settings → Memory; never secrets.", RememberArgs, t_remember, R.LOW, 5))
    reg.register(Tool("recall", "List saved memory entries.", RecallArgs, t_recall, R.READ, 5))
    reg.register(Tool("forget", "Delete a memory entry.", ForgetArgs, t_forget, R.LOW, 5))
