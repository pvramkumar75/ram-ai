@echo off
cd /d "%~dp0"
if not exist .venv\Scripts\pythonw.exe (
  echo RAM AI is not installed yet - running setup...
  call "%~dp0Install_RAM_AI.bat"
  exit /b
)
start "" ".venv\Scripts\pythonw.exe" -m ramai
