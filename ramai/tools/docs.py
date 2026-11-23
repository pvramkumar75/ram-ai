"""Office documents: create Word documents, edit existing Excel workbooks (automatic backup before any change)."""
from __future__ import annotations

import shutil
import time
from pathlib import Path
from typing import Any, Literal

from pydantic import Field

from ..policy import PolicyError, Risk
from .base import Args, Registry, Tool


class Block(Args):
    type: Literal["heading", "paragraph", "bullet", "numbered", "table", "page_break"] = "paragraph"
    text: str = Field("", max_length=20000)
    level: int = Field(1, ge=1, le=3, description="Heading level")
    rows: list[list[Any]] = Field(default_factory=list, description="For tables: first row is the header")


class DocArgs(Args):
    path: str = Field(description="Must end with .docx")
    title: str | None = None
    blocks: list[Block] = Field(min_length=1, max_length=400)
    overwrite: bool = False


class CellEdit(Args):
    cell: str = Field(pattern=r"^[A-Za-z]{1,3}[0-9]{1,7}$", description="e.g. B4")
    value: Any = None


class EditBookArgs(Args):
    path: str = Field(description="Existing .xlsx file")
    sheet: str | None = Field(None, description="Sheet name (default: first sheet). Created if it does not exist.")
    cells: list[CellEdit] = Field(default_factory=list, max_length=2000)
    append_rows: list[list[Any]] = Field(default_factory=list, max_length=20000)


def _backup(app, p: Path) -> str:
    bdir = Path(app.settings.get("output_dir")) / "_backups"
    bdir.mkdir(parents=True, exist_ok=True)
    dst = bdir / f"{p.stem}.{time.strftime('%Y%m%d-%H%M%S')}{p.suffix}"
    shutil.copy2(p, dst)
    return str(dst)


def t_create_doc(a: DocArgs, ctx):
    import docx

    if not a.path.lower().endswith(".docx"):
        raise PolicyError("Path must end with .docx")
    p = ctx.app.policy.resolve(a.path, write=True)
    if p.exists() and not a.overwrite:
        raise PolicyError("File exists; set overwrite=true (requires approval)")
    d = docx.Document()
    if a.title:
        d.add_heading(a.title, 0)
    for b in a.blocks:
        if b.type == "heading":
            d.add_heading(b.text, b.level)
        elif b.type == "bullet":
            d.add_paragraph(b.text, style="List Bullet")
        elif b.type == "numbered":
            d.add_paragraph(b.text, style="List Number")
        elif b.type == "page_break":
            d.add_page_break()
        elif b.type == "table" and b.rows:
            cols = max(len(r) for r in b.rows)
            t = d.add_table(rows=0, cols=cols)
            t.style = "Light Grid Accent 1"
            for i, r in enumerate(b.rows):
                cells = t.add_row().cells
                for j in range(cols):
                    cells[j].text = "" if j >= len(r) or r[j] is None else str(r[j])
                    if i == 0:
                        for run in cells[j].paragraphs[0].runs:
                            run.bold = True
        else:
            d.add_paragraph(b.text)
    p.parent.mkdir(parents=True, exist_ok=True)
    d.save(str(p))
    return {"written": str(p), "blocks": len(a.blocks)}


def _check_edit(a: EditBookArgs, ctx):
    p = ctx.app.policy.resolve(a.path, write=True, must_exist=True)
    if p.suffix.lower() not in (".xlsx", ".xlsm"):
        raise PolicyError("Only .xlsx/.xlsm workbooks can be edited")


def _edit_preview(a: EditBookArgs, ctx):
    async def _():
        lines = [f"EDIT WORKBOOK {a.path}" + (f" (sheet {a.sheet})" if a.sheet else ""),
                 "A backup copy is saved first (RAM_AI_Output\\_backups)."]
        lines += [f"  {c.cell} = {str(c.value)[:80]}" for c in a.cells[:40]]
        if len(a.cells) > 40:
            lines.append(f"  … and {len(a.cells) - 40} more cells")
        if a.append_rows:
            lines.append(f"  + append {len(a.append_rows)} row(s), first: {a.append_rows[0][:8]}")
        return {"text": "\n".join(lines), "digest": None}

    return _()


def t_edit_book(a: EditBookArgs, ctx):
    import openpyxl

    p = ctx.app.policy.resolve(a.path, write=True, must_exist=True)
    backup = _backup(ctx.app, p)
    wb = openpyxl.load_workbook(str(p), keep_vba=p.suffix.lower() == ".xlsm")
    if a.sheet:
        ws = wb[a.sheet] if a.sheet in wb.sheetnames else wb.create_sheet(a.sheet[:31])
    else:
        ws = wb.worksheets[0]
    for c in a.cells:
        ws[c.cell.upper()] = c.value
    for r in a.append_rows:
        ws.append([v if isinstance(v, (int, float, str, bool, type(None))) else str(v) for v in r])
    try:
        wb.save(str(p))
    except PermissionError:
        raise PolicyError("The workbook is open in Excel (locked). Close it in Excel and retry.")
    return {"edited": str(p), "sheet": ws.title, "cells_set": len(a.cells), "rows_appended": len(a.append_rows), "backup": backup}


def register(reg: Registry) -> None:
    reg.register(Tool("create_document", "Create a Word .docx (headings, paragraphs, bullets, numbered lists, tables) in a write-enabled folder.",
                      DocArgs, t_create_doc, lambda a: Risk.HIGH if Path(a.path).exists() else Risk.LOW, 60,
                      check=lambda a, ctx: ctx.app.policy.resolve(a.path, write=True), reversible=False))
    reg.register(Tool("edit_workbook", "Change cells and/or append rows in an EXISTING Excel workbook (a backup is saved first; needs approval).",
                      EditBookArgs, t_edit_book, Risk.HIGH, 120, preview=_edit_preview, check=_check_edit, reversible=True))
