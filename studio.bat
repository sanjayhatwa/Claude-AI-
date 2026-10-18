@echo off
rem Start Viral Studio locally: http://127.0.0.1:4320  (close this window to stop)
cd /d "%~dp0"
set "PY="
python -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul && set "PY=python"
if not defined PY py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul && set "PY=py -3"
if not defined PY (echo Python 3.10+ was not found. Double-click setup.bat first. & pause & exit /b 1)
%PY% -m vstudio serve --open %*
pause
