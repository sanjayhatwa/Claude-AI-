#!/usr/bin/env bash
# Viral Hyper Agent setup (macOS / Linux / Git Bash).
#   ./setup.sh            Python tools + fetch the full tool repos into tools/
#   ./setup.sh --global   also copy the agents and skills into ~/.claude so they work in every project
#   ./setup.sh --no-tools skip fetching tools/ (the skills work without them)
set -euo pipefail
cd "$(dirname "$0")"

GLOBAL=0; TOOLS=1
for a in "$@"; do
  case "$a" in
    --global) GLOBAL=1 ;;
    --no-tools) TOOLS=0 ;;
    *) echo "unknown option: $a"; exit 1 ;;
  esac
done

echo "==> Checking what this computer has"
PY=""
for c in python3 python; do
  if command -v "$c" >/dev/null && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
    PY=$(command -v "$c"); break
  fi
done
if [ -z "$PY" ]; then
  echo "!!  Python 3.10+ is required: https://www.python.org/downloads/  (macOS: brew install python)"
  exit 1
fi
echo "    Python:  $("$PY" --version)"
if [ "$(uname)" = "Darwin" ]; then FF_HINT="brew install ffmpeg"; else FF_HINT="sudo apt install ffmpeg  (or your distro's package manager)"; fi
if command -v ffmpeg >/dev/null; then echo "    FFmpeg:  found"; else echo "!!  FFmpeg not found (needed to read and render video): $FF_HINT"; fi
if command -v claude >/dev/null; then echo "    Claude Code: found"; else
  echo "!!  Claude Code not found. The studio still renders without it, but planning, scripts and"
  echo "    'Ask Claude' need it. Install it from https://code.claude.com and run 'claude' once to log in."
fi

echo "==> Python packages"
# Packages the studio cannot start without; the others switch features on (downloads, beats, shots,
# OCR, word timing, exact variable fonts) and only cost those features when they cannot be installed.
CORE_PACKAGES="numpy opencv-python-headless pillow"
NEWEST_WHEELS=14    # newest Python 3.x that every package (OCR's onnxruntime, whisper's ctranslate2) has builds for

PYV=$("$PY" -c 'import sys; print(sys.version_info[1])')
if [ "$PYV" -gt "$NEWEST_WHEELS" ]; then
  echo "    note: Python 3.$PYV is newer than some packages' builds; OCR or whisper may not install yet"
  echo "          (everything else works; Python 3.$NEWEST_WHEELS gets them all)"
fi

# Homebrew's, uv's and many Linux distributions' Pythons refuse pip installs outside a virtual environment
# (PEP 668, "externally-managed-environment"). For those the packages go into a project environment
# in .venv, which studio.sh then uses. A .venv from an earlier run is used again.
in_venv() { "$1" -c 'import sys; sys.exit(0 if sys.prefix != sys.base_prefix else 1)' 2>/dev/null; }
externally_managed() {
  "$1" -c 'import os, sys, sysconfig
sys.exit(0 if os.path.isfile(os.path.join(sysconfig.get_path("stdlib"), "EXTERNALLY-MANAGED")) else 1)' 2>/dev/null
}
VENV_NOTE=0
if [ -x .venv/bin/python ] && .venv/bin/python -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
  PY="$PWD/.venv/bin/python"; VENV_NOTE=1
  echo "    using the project environment in .venv"
elif ! in_venv "$PY" && externally_managed "$PY"; then
  echo "    $PY is managed by Homebrew, uv or the system (pip may not install into it),"
  echo "    so the packages go into a project environment in .venv"
  if ! "$PY" -m venv .venv; then
    echo "!!  could not create .venv. On Debian/Ubuntu: sudo apt install python3-venv (or python3.$PYV-venv),"
    echo "    then run ./setup.sh again."
    exit 1
  fi
  PY="$PWD/.venv/bin/python"; VENV_NOTE=1
fi
if ! "$PY" -m pip --version >/dev/null 2>&1; then
  echo "!!  pip is missing for $PY. Install it (Debian/Ubuntu: sudo apt install python3-pip), then run ./setup.sh again."
  exit 1
fi

# per-user install outside a virtual environment, as before; inside one (.venv or the user's own) a plain one
pip_install() {
  if ! in_venv "$PY" && "$PY" -m pip install --user -q "$@"; then return 0; fi
  "$PY" -m pip install -q "$@"
}

# pip installs a requirements file all or nothing: one package without a build for this computer (OCR or
# whisper on a brand-new Python, say) would leave every package out. Then each is installed on its own.
if ! pip_install -r requirements.txt; then
  echo "    some packages could not be installed together; installing them one by one"
  MISSING=""; CORE_MISSING=""
  while IFS= read -r req; do
    if ! pip_install "$req"; then
      MISSING="$MISSING $req"
      name=$(printf '%s' "$req" | sed -e 's/[[:space:]]*[<>=!~;[].*//' -e 's/[_.]/-/g' | tr '[:upper:]' '[:lower:]')
      case " $CORE_PACKAGES " in *" $name "*) CORE_MISSING="$CORE_MISSING $req" ;; esac
    fi
  done < <(sed -e 's/#.*//' -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' requirements.txt | awk 'NF')
  if [ -n "$CORE_MISSING" ]; then
    echo "!!  could not install:$CORE_MISSING (the studio needs it). See pip's messages above;"
    echo "    a Python version that has builds for it (3.10 to 3.$NEWEST_WHEELS) fixes most cases."
    exit 1
  fi
  if [ -n "$MISSING" ]; then
    echo "!!  not installed:$MISSING. Everything else works; the health check below says what is missing."
  fi
fi


echo "==> Studio assets: free caption fonts (Google Fonts, OFL) and the face-tracking model"
"$PY" -m vstudio fonts install "Montserrat" "Anton" "Bebas Neue" "Poppins" "Archivo Black" "Bangers" \
  "Playfair Display" "Space Grotesk" "Permanent Marker" "Courier Prime" || echo "!!  font download failed (offline?); system fonts will be used"
"$PY" -c "from vstudio import reframe; print('    face model:', reframe.ensure_model())" || echo "!!  face model download failed; reframing falls back to centre crops"

mkdir -p workspace
touch workspace/.gitkeep

if [ "$TOOLS" = 1 ] && ! command -v git >/dev/null; then
  echo "==> Skipping tools/ (no git). Install git and run setup again to add ReelMimic and the others."
  TOOLS=0
fi
if [ "$TOOLS" = 1 ]; then
  echo "==> Fetching tool repos into tools/ (pinned versions)"
  mkdir -p tools
  while read -r name url rev; do
    [ -z "$name" ] && continue
    if [ -d "tools/$name/.git" ]; then echo "    $name: already there"; continue; fi
    if ! git clone -q --filter=blob:none "$url" "tools/$name"; then echo "!!  could not clone $name; skipping"; continue; fi
    git -C "tools/$name" checkout -q "$rev"
    echo "    $name @ ${rev:0:7}"
  done < tools.lock
fi

if [ "$GLOBAL" = 1 ]; then
  echo "==> Copying agents and skills into ~/.claude"
  mkdir -p ~/.claude/agents ~/.claude/skills
  cp -R .claude/agents/. ~/.claude/agents/
  cp -R .claude/skills/. ~/.claude/skills/
fi

echo
echo "==> Health check"
"$PY" -m vstudio doctor || true

echo
echo "Done."
echo "  Local studio (web app):   ./studio.sh        -> http://127.0.0.1:4320"
echo "  Claude Code agent:        open Claude Code in this folder and say:  make me go viral"
if [ "$VENV_NOTE" = 1 ]; then
  echo "                            (the packages are in .venv: run  source .venv/bin/activate  in the"
  echo "                            terminal first, so the agent's python commands find them)"
fi
echo "  Anything marked fail/warn above shows its fix next to it."
