---
name: word_advanced
description: Professional Word documents with python-docx - styles, headings, numbered/bulleted lists, formatted tables, headers/footers with page numbers, letterheads, TOC field, PDF export.
---
# Advanced Word (run_python, python-docx)

Simple documents: use the `create_document` tool. For letters, reports, formatted tables, headers/footers → `run_python`.

```python
import os
from docx import Document
from docx.shared import Pt, Cm, RGBColor
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.oxml import OxmlElement
from docx.oxml.ns import qn

doc = Document()
sec = doc.sections[0]; sec.left_margin = sec.right_margin = Cm(2); sec.top_margin = Cm(2)
st = doc.styles["Normal"]; st.font.name = "Calibri"; st.font.size = Pt(11)

def page_number(paragraph):                       # footer "Page X"
    r = paragraph.add_run(); f1 = OxmlElement("w:fldChar"); f1.set(qn("w:fldCharType"), "begin")
    it = OxmlElement("w:instrText"); it.text = "PAGE"; f2 = OxmlElement("w:fldChar"); f2.set(qn("w:fldCharType"), "end")
    r._r.append(f1); r._r.append(it); r._r.append(f2)

hp = sec.header.paragraphs[0]; hp.text = "Company name · Department"; hp.alignment = WD_ALIGN_PARAGRAPH.RIGHT
fp = sec.footer.paragraphs[0]; fp.text = "Page "; page_number(fp); fp.alignment = WD_ALIGN_PARAGRAPH.CENTER

doc.add_heading("Weekly Purchase Report", 0)
doc.add_paragraph("Prepared on 23 Nov 2026").runs[0].italic = True
doc.add_heading("1. Highlights", 1)
for t in ["Copper down 2.7%", "Two RFQs pending approval"]: doc.add_paragraph(t, style="List Bullet")
doc.add_heading("2. Price table", 1)
rows = [("Copper", "12,230.50", "-2.7%"), ("Aluminium", "2,580.00", "+0.4%")]
tb = doc.add_table(rows=1, cols=3); tb.style = "Light Grid Accent 1"; tb.alignment = WD_TABLE_ALIGNMENT.CENTER
for c, h in zip(tb.rows[0].cells, ["Metal", "USD/t", "Change"]): c.text = h; c.paragraphs[0].runs[0].bold = True
for r in rows:
    cells = tb.add_row().cells
    for c, v in zip(cells, r): c.text = v
out = os.path.join(os.environ["OUTPUT_DIR"], "Weekly_Report.docx"); doc.save(out); print("saved", out)
```

## Tips
- Numbered lists: style "List Number"; second level "List Bullet 2" / "List Number 2".
- Table of contents: insert a TOC field (`instrText = 'TOC \\o "1-3" \\h \\z \\u'`); Word fills it when the user presses F9 / opens.
- Letter: date right-aligned, recipient block, "Subject:" bold, body, "Thanks & Regards" + name/title.
- Edit an existing .docx: `Document(path)`, change paragraphs/tables, save under a NEW name unless the user asked to overwrite.
- PDF export (needs Word): `run_command` PowerShell
  `$w=New-Object -ComObject Word.Application; $d=$w.Documents.Open('C:\\in.docx'); $d.SaveAs([ref]'C:\\out.pdf',[ref]17); $d.Close(); $w.Quit()`
