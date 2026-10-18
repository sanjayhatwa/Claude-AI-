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
"$PY" -m pip install --user -q -r requirements.txt || "$PY" -m pip install -q -r requirements.txt


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
echo "  Anything marked fail/warn above shows its fix next to it."
