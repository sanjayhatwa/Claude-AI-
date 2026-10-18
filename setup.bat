@echo off
rem Double-click to set up Viral Hyper Agent on Windows (runs setup.ps1 past the PowerShell script block).
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup.ps1" %*
echo.
pause
