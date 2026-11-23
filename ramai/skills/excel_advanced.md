---
name: excel_advanced
description: Professional Excel workbooks - formulas, tables, number formats, conditional formatting, charts, pivot-style summaries, freeze panes; editing existing files safely.
---
# Advanced Excel (run_python)

Use `run_python`. Preinstalled: pandas, openpyxl, xlsxwriter. Save to `os.environ["OUTPUT_DIR"]`.

## Choose the library
- **New report workbook** → pandas + **xlsxwriter** (best formatting & charts).
- **Edit an existing workbook / keep its formatting** → **openpyxl** (`load_workbook`). Copy the file first (or use `edit_workbook` for simple cell edits).
- Reading data: `pd.read_excel(path, sheet_name=None)` returns all sheets.

## Report template (xlsxwriter)
```python
import os, pandas as pd
out = os.path.join(os.environ["OUTPUT_DIR"], "Supplier_Comparison.xlsx")
df = pd.DataFrame(rows)  # build from your data
with pd.ExcelWriter(out, engine="xlsxwriter") as xw:
    df.to_excel(xw, sheet_name="Comparison", index=False, startrow=2)
    wb, ws = xw.book, xw.sheets["Comparison"]
    title = wb.add_format({"bold": True, "font_size": 14})
    hdr = wb.add_format({"bold": True, "bg_color": "#1F4E78", "font_color": "white", "border": 1, "text_wrap": True, "valign": "vcenter"})
    money = wb.add_format({"num_format": "#,##0.00", "border": 1})
    pct = wb.add_format({"num_format": "0.0%", "border": 1})
    ws.write(0, 0, "Supplier comparison - ETFE quotations 2026", title)
    for c, name in enumerate(df.columns):
        ws.write(2, c, name, hdr)
    ws.set_column(0, 0, 28); ws.set_column(1, len(df.columns) - 1, 14, money)
    ws.freeze_panes(3, 1)
    ws.autofilter(2, 0, 2 + len(df), len(df.columns) - 1)
    # live formula row (Excel recalculates)
    r = 3 + len(df)
    ws.write(r, 0, "Average", hdr)
    ws.write_formula(r, 1, f"=AVERAGE(B4:B{r})", money)
    # highlight lowest price per column
    ws.conditional_format(3, 1, r - 1, 1, {"type": "bottom", "value": 1, "format": wb.add_format({"bg_color": "#C6EFCE"})})
    # chart
    ch = wb.add_chart({"type": "column"})
    ch.add_series({"name": "Price", "categories": ["Comparison", 3, 0, r - 1, 0], "values": ["Comparison", 3, 1, r - 1, 1]})
    ch.set_title({"name": "Price by supplier"}); ws.insert_chart(2, len(df.columns) + 1, ch, {"x_scale": 1.3, "y_scale": 1.2})
print("saved", out)
```

## Useful patterns
- Pivot-style summary: `pd.pivot_table(df, index="Supplier", columns="Month", values="Qty", aggfunc="sum", margins=True)` → write to its own sheet.
- Weighted average price: `(df.Price*df.Qty).sum()/df.Qty.sum()` - also write it as an Excel formula `=SUMPRODUCT(B:B,C:C)/SUM(C:C)` so the user sees the logic.
- Excel table object (xlsxwriter): `ws.add_table(first_row, first_col, last_row, last_col, {"columns": [{"header": h} for h in df.columns], "style": "Table Style Medium 2"})`.
- Data validation dropdown: `ws.data_validation("D4:D200", {"validate": "list", "source": ["Open", "Closed"]})`.
- Dates: write real datetimes with a date format (`{"num_format": "dd-mmm-yyyy"}`), never text.
- Indian number format: `"#,##,##0.00"`.

## Editing an existing workbook (openpyxl)
```python
from openpyxl import load_workbook
wb = load_workbook(path)           # keep_vba=True for .xlsm
ws = wb["Sheet1"]; ws["E2"] = "=C2*D2"; ws.append([...])
wb.save(new_path)                  # save to a NEW name unless the user asked to overwrite
```
openpyxl does not evaluate formulas; if you need computed values, compute them in pandas too.

## Checks before you finish
- Re-open the file (`pd.read_excel`) and print the first rows to prove it is correct.
- Report: file path, sheets, what each contains.
