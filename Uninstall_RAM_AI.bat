@echo off
title RAM AI uninstall
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -Command "& ([scriptblock]::Create((Get-Content -Raw -LiteralPath '%~dp0Uninstall_RAM_AI.ps1')))"
echo.
pause
