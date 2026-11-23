---
name: pptx_advanced
description: Professional PowerPoint decks with python-pptx - title/section slides, bullet hierarchies, tables, native charts, images, consistent theme, speaker notes.
---
# Advanced PowerPoint (run_python, python-pptx)

Save to `os.environ["OUTPUT_DIR"]`. 16:9 widescreen. Keep slides clean: max ~6 bullets, 1 idea per slide.

## Deck skeleton
```python
import os
from pptx import Presentation
from pptx.util import Inches, Pt, Emu
from pptx.dml.color import RGBColor
from pptx.chart.data import CategoryChartData
from pptx.enum.chart import XL_CHART_TYPE, XL_LEGEND_POSITION
from pptx.enum.text import PP_ALIGN

NAVY, ACCENT, GREY = RGBColor(0x1F, 0x4E, 0x78), RGBColor(0xC0, 0x39, 0x2B), RGBColor(0x59, 0x59, 0x59)
prs = Presentation(); prs.slide_width, prs.slide_height = Inches(13.333), Inches(7.5)

def title_slide(t, sub):
    s = prs.slides.add_slide(prs.slide_layouts[0]); s.shapes.title.text = t; s.placeholders[1].text = sub
    s.shapes.title.text_frame.paragraphs[0].runs[0].font.color.rgb = NAVY
    return s

def bullets(t, items, notes=None):          # items: ["text", ("sub text", 1), ...]
    s = prs.slides.add_slide(prs.slide_layouts[1]); s.shapes.title.text = t
    tf = s.placeholders[1].text_frame; tf.clear()
    for i, it in enumerate(items):
        text, lvl = (it, 0) if isinstance(it, str) else it
        p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        p.text, p.level = text, lvl; p.font.size = Pt(22 if lvl == 0 else 18)
    if notes: s.notes_slide.notes_text_frame.text = notes
    return s

def table(t, header, rows, col_w=None):
    s = prs.slides.add_slide(prs.slide_layouts[5]); s.shapes.title.text = t
    shp = s.shapes.add_table(len(rows) + 1, len(header), Inches(0.6), Inches(1.5), Inches(12.1), Inches(0.4) * (len(rows) + 1))
    tb = shp.table
    for c, h in enumerate(header):
        cell = tb.cell(0, c); cell.text = str(h); cell.fill.solid(); cell.fill.fore_color.rgb = NAVY
        cell.text_frame.paragraphs[0].runs[0].font.color.rgb = RGBColor(255, 255, 255); cell.text_frame.paragraphs[0].runs[0].font.bold = True
    for r, row in enumerate(rows, 1):
        for c, v in enumerate(row):
            tb.cell(r, c).text = f"{v:,.2f}" if isinstance(v, float) else str(v)
            tb.cell(r, c).text_frame.paragraphs[0].runs[0].font.size = Pt(14)
    return s

def chart(t, cats, series: dict, kind=XL_CHART_TYPE.COLUMN_CLUSTERED):
    s = prs.slides.add_slide(prs.slide_layouts[5]); s.shapes.title.text = t
    cd = CategoryChartData(); cd.categories = cats
    for name, vals in series.items(): cd.add_series(name, vals)
    ch = s.shapes.add_chart(kind, Inches(0.8), Inches(1.5), Inches(11.7), Inches(5.5), cd).chart
    ch.has_legend = len(series) > 1; ch.legend.position = XL_LEGEND_POSITION.BOTTOM if ch.has_legend else None
    return s

title_slide("Raw Material Price Update", "Purchase department · week 48")
bullets("Key takeaways", ["Copper down 2.7% w/w", ("LME cash USD 12,230/t", 1), "Buy-now: Aluminium"], notes="Talk track…")
table("Prices", ["Metal", "USD/t", "Change"], [["Cu", 12230.5, "-2.7%"], ["Al", 2580.0, "+0.4%"]])
chart("Copper trend", ["Mon", "Tue", "Wed"], {"Cu": (12570, 12400, 12230)}, XL_CHART_TYPE.LINE_MARKERS)
out = os.path.join(os.environ["OUTPUT_DIR"], "Price_Update.pptx"); prs.save(out); print("saved", out)
```

## Tips
- Images: `s.shapes.add_picture(path, Inches(x), Inches(y), width=Inches(w))`; make charts with matplotlib (`plt.savefig(png, dpi=200, bbox_inches="tight")`) when python-pptx native charts are not enough.
- To use the company template: `Presentation(r"path\to\template.pptx")` and inspect `prs.slide_layouts` names first.
- Converting to PDF needs PowerPoint installed: `run_command` with PowerShell COM:
  `$p=New-Object -ComObject PowerPoint.Application; $d=$p.Presentations.Open('in.pptx',$true,$false,$false); $d.SaveAs('out.pdf',32); $d.Close(); $p.Quit()`
- Finish by listing the slides you produced (re-open with Presentation and print titles).
