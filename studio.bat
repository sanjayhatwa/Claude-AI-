@echo off
rem Start Viral Studio locally: http://127.0.0.1:4320  (close this window to stop)
cd /d "%~dp0"
where python >nul 2>nul || (echo Python 3.10+ is required: https://www.python.org/downloads/ & pause & exit /b 1)
python -m vstudio serve --open %*
pause
