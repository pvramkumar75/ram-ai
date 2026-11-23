@echo off
setlocal
title RAM AI setup
cd /d "%~dp0"

rem ---- 1. Refuse to run from inside the ZIP (Windows runs it from a temp copy without the other files)
if not exist "%~dp0ramai\__main__.py" goto :notextracted
echo %~dp0 | findstr /i /c:"\Temp\" /c:".zip\" >nul && (
  if not exist "%~dp0requirements.txt" goto :notextracted
)

rem ---- 2. Remove the "downloaded from the internet" block from our own files
powershell -NoProfile -Command "Get-ChildItem -LiteralPath '%~dp0' -Recurse -File | Unblock-File" >nul 2>&1

rem ---- 3. Run the installer in a way company script policies do not block
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop'; & ([scriptblock]::Create((Get-Content -Raw -LiteralPath '%~dp0Install_RAM_AI.ps1')))"
set RC=%ERRORLEVEL%
echo.
if not "%RC%"=="0" (
  echo ============================================================
  echo  Setup did not finish. A log was saved here:
  echo    %~dp0install_log.txt
  echo  Please send that file to whoever shared RAM AI with you.
  echo ============================================================
)
pause
exit /b %RC%

:notextracted
echo ============================================================
echo  Please EXTRACT the ZIP first:
echo    right-click the ZIP file  -^>  "Extract All..."  -^>  choose e.g. Documents
echo  then open the extracted RAM_AI folder and double-click Install_RAM_AI.bat
echo ============================================================
pause
exit /b 1
