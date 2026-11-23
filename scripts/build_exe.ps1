# NOT YET RUN/VERIFIED. Builds a one-folder Windows app with PyInstaller (then wrap with Inno Setup for an installer).
.\.venv\Scripts\python -m pip install pyinstaller
.\.venv\Scripts\pyinstaller --noconfirm --name RAM_AI --add-data "ramai\ui;ramai\ui" --collect-all pywinauto --collect-all playwright -w ramai\__main__.py
Write-Host "Output: dist\RAM_AI\RAM_AI.exe (test on a clean PC; sign the exe before distributing)"
