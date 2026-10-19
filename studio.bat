@echo off
rem Start Viral Studio locally: http://127.0.0.1:4320  (close this window to stop)
cd /d "%~dp0"
rem The Python that setup.bat installed the packages for: a project environment in .venv when there is
rem one, else the first Python 3.10+ that has them (on ARM64 Windows that is the x64 Python, which setup
rem picks because OpenCV has no ARM64 builds), else the first Python 3.10+ (the health check says what is missing).
set "PY="
set "NEW_ENOUGH=import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)"
set "HAS_PACKAGES=import sys, importlib.util as u; sys.exit(0 if sys.version_info >= (3, 10) and u.find_spec('numpy') and u.find_spec('cv2') else 1)"
if exist ".venv\Scripts\python.exe" ".venv\Scripts\python.exe" -c "%NEW_ENOUGH%" >nul 2>nul && set "PY=.venv\Scripts\python.exe"
for %%P in (python "py -3" "py -V:3.14" "py -V:3.13" "py -V:3.12" "py -V:3.11" "py -V:3.10") do (
  if not defined PY %%~P -c "%HAS_PACKAGES%" >nul 2>nul && set "PY=%%~P"
)
if not defined PY python -c "%NEW_ENOUGH%" >nul 2>nul && set "PY=python"
if not defined PY py -3 -c "%NEW_ENOUGH%" >nul 2>nul && set "PY=py -3"
if not defined PY (echo Python 3.10+ was not found. Double-click setup.bat first. & pause & exit /b 1)
%PY% -m vstudio serve --open %*
pause
