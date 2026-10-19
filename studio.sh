#!/usr/bin/env bash
# Start Viral Studio locally: http://127.0.0.1:4320  (Ctrl+C to stop)
cd "$(dirname "$0")"
# The Python that setup.sh installed the packages for: the project environment in .venv (setup.sh makes
# it for Homebrew's and Linux distributions' Pythons, which refuse pip installs), else the first Python
# 3.10+ on PATH that has them, else the first Python 3.10+ (the health check then says what is missing).
HAS_PACKAGES='import sys, importlib.util as u
sys.exit(0 if sys.version_info >= (3, 10) and u.find_spec("numpy") and u.find_spec("cv2") else 1)'
NEW_ENOUGH='import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'
PY=""
if [ -x .venv/bin/python ] && .venv/bin/python -c "$NEW_ENOUGH" >/dev/null 2>&1; then
  PY="$PWD/.venv/bin/python"
fi
for probe in "$HAS_PACKAGES" "$NEW_ENOUGH"; do
  [ -n "$PY" ] && break
  for c in python3 python; do
    exe=$(command -v "$c" 2>/dev/null) || continue
    if "$exe" -c "$probe" >/dev/null 2>&1; then PY=$exe; break; fi
  done
done
if [ -z "$PY" ]; then
  echo "Python 3.10+ was not found. Run ./setup.sh first."
  exit 1
fi
exec "$PY" -m vstudio serve --open "$@"
