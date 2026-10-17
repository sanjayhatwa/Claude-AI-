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

PY=$(command -v python3 || command -v python || true)
[ -z "$PY" ] && { echo "Python 3.10+ is required: https://www.python.org/downloads/"; exit 1; }

echo "==> Python packages"
"$PY" -m pip install --user -q -r requirements.txt || "$PY" -m pip install -q -r requirements.txt

command -v ffmpeg >/dev/null || echo "!!  ffmpeg not found. The studio and reel-mimic need it: https://ffmpeg.org/download.html"

echo "==> Studio assets: free caption fonts (Google Fonts, OFL) and the face-tracking model"
"$PY" -m vstudio fonts install "Montserrat" "Anton" "Bebas Neue" "Poppins" "Archivo Black" "Bangers" \
  "Playfair Display" "Space Grotesk" "Permanent Marker" "Courier Prime" || echo "!!  font download failed (offline?); system fonts will be used"
"$PY" -c "from vstudio import reframe; print('    face model:', reframe.ensure_model())" || echo "!!  face model download failed; reframing falls back to centre crops"

mkdir -p workspace
touch workspace/.gitkeep

if [ "$TOOLS" = 1 ]; then
  echo "==> Fetching tool repos into tools/ (pinned versions)"
  mkdir -p tools
  while read -r name url rev; do
    [ -z "$name" ] && continue
    if [ -d "tools/$name/.git" ]; then echo "    $name: already there"; continue; fi
    git clone -q --filter=blob:none "$url" "tools/$name"
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
echo "Done."
echo "  Local studio (web app):   ./studio.sh        -> http://127.0.0.1:4320"
echo "  Claude Code agent:        open Claude Code here and say:  make me go viral"
