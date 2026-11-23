---
name: powerbi
description: Power BI - prepare clean data models (star schema CSV/XLSX), write DAX measures and Power Query (M), open Power BI Desktop, and what can/cannot be automated.
---
# Power BI

## What RAM AI can do well
1. **Prepare the data** (run_python + pandas): clean, de-duplicate, type columns, build a star schema:
   - `Fact_Purchases.csv` (Date, SupplierID, MaterialID, Qty, Rate, Amount)
   - `Dim_Supplier.csv`, `Dim_Material.csv`, `Dim_Date.csv` (Date, Year, Month, MonthName, Quarter, FY (Apr-Mar))
   Save them in a folder like `OUTPUT_DIR\PowerBI\<project>` and tell the user the folder (Get Data → Folder/Text-CSV).
2. **Write DAX measures** for the user to paste (Modeling → New measure), e.g.
   - `Total Spend = SUM(Fact_Purchases[Amount])`
   - `Weighted Avg Rate = DIVIDE(SUMX(Fact_Purchases, Fact_Purchases[Qty]*Fact_Purchases[Rate]), SUM(Fact_Purchases[Qty]))`
   - `Spend YoY % = VAR prev = CALCULATE([Total Spend], SAMEPERIODLASTYEAR(Dim_Date[Date])) RETURN DIVIDE([Total Spend]-prev, prev)`
   - `Spend FYTD = TOTALYTD([Total Spend], Dim_Date[Date], "31/3")`
3. **Write Power Query (M)** steps for cleaning when the user prefers to refresh from source.
4. **Open Power BI Desktop**: `launch_app('Power BI Desktop')` or open a file: `run_command` → `Start-Process 'C:\\path\\report.pbix'`.
   If not installed: `install_package(kind='winget', name='Microsoft.PowerBI')` (user approves; licence accepted on the card).
5. **Refresh data in an open report**: keyboard in Power BI Desktop: Home → Refresh (`click_text('Refresh')`), then wait.

## Limits (be honest with the user)
- Building visuals inside Power BI Desktop has no automation API; RAM AI can only click through the UI slowly. Prefer: deliver the
  data model + DAX + a short layout spec ("Page 1: card Total Spend, clustered bar Spend by Supplier, line Weighted Avg Rate by Month").
- Publishing to the Power BI Service / scheduled refresh needs the user's Microsoft account and admin-approved API access; do not attempt
  to sign in for the user.
- Alternative when a quick visual is enough: build the dashboard in Excel (charts, slicers) or a PowerPoint chart deck.
