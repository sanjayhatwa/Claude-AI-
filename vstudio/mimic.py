"""
mimic.py - start the full ReelMimic studio (2D animated remakes of a reference video).

ReelMimic (MIT, github.com/edenfunf/reelmimic) is fetched by setup into
tools/reelmimic at a pinned commit. It needs Node 22.18+, Python 3.10+, FFmpeg
and Chrome, and runs its agents on the user's own Claude Code login, like the
rest of this project.

    python -m vstudio mimic2d status     # is it installed, which commit, is it running
    python -m vstudio mimic2d install    # its own installer (npm install, Python deps)
    python -m vstudio mimic2d start      # start it and open http://localhost:4318
    python -m vstudio mimic2d fetch      # clone it into tools/reelmimic if setup was run with --no-tools
"""

import argparse
import os
import shutil
import subprocess
import sys
import urllib.request

from . import paths

DIR = os.path.join(paths.REPO, "tools", "reelmimic")
URL = "http://localhost:4318"


def _lock():
    with open(os.path.join(paths.REPO, "tools.lock"), encoding="utf-8") as f:
        for line in f:
            parts = line.split()
            if parts and parts[0] == "reelmimic":
                return parts[1], parts[2]
    raise SystemExit("reelmimic is not listed in tools.lock")


def installed():
    return os.path.isdir(os.path.join(DIR, ".claude", "skills", "video-clone"))


def running():
    try:
        with urllib.request.urlopen(URL + "/api/health", timeout=2) as r:
            return r.status == 200
    except Exception:
        return False


def status():
    if not installed():
        return {"installed": False, "running": False, "dir": DIR}
    rev = subprocess.run(["git", "-C", DIR, "rev-parse", "--short", "HEAD"], capture_output=True, text=True).stdout.strip()
    return {"installed": True, "running": running(), "dir": DIR, "commit": rev,
            "node": shutil.which("node") is not None, "chrome": find_chrome() or False,
            "built": os.path.isdir(os.path.join(DIR, "app", "dist"))}


def fetch():
    if installed():
        return DIR
    url, rev = _lock()
    os.makedirs(os.path.dirname(DIR), exist_ok=True)
    subprocess.run(["git", "clone", "-q", "--filter=blob:none", url, DIR], check=True)
    subprocess.run(["git", "-C", DIR, "checkout", "-q", rev], check=True)
    return DIR


def find_chrome():
    """ReelMimic renders frames with Chrome; find one so the user doesn't have to set CHROME_PATH."""
    if os.environ.get("CHROME_PATH") and os.path.exists(os.environ["CHROME_PATH"]):
        return os.environ["CHROME_PATH"]
    for name in ("google-chrome", "google-chrome-stable", "chrome", "chromium", "chromium-browser", "msedge"):
        path = shutil.which(name)
        if path:
            return path
    candidates = [
        os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%LocalAppData%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"),
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
    ]
    pw = os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or "/opt/pw-browsers"
    if os.path.isdir(pw):
        for d in sorted(os.listdir(pw), reverse=True):
            if d.startswith("chromium"):
                candidates.append(os.path.join(pw, d, "chrome-linux", "chrome"))
    return next((c for c in candidates if c and os.path.exists(c)), None)


def _script(name):
    if os.name == "nt":
        return [os.path.join(DIR, name + ".bat")]
    return ["bash", os.path.join(DIR, name + ".sh")]


def main(argv=None):
    ap = argparse.ArgumentParser(prog="vstudio mimic2d", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("action", choices=["status", "fetch", "install", "start"], nargs="?", default="status")
    a = ap.parse_args(argv)
    if a.action == "status":
        for k, v in status().items():
            print("  %-10s %s" % (k, v))
        if not installed():
            print("\n  Not installed. Run: python -m vstudio mimic2d fetch")
        return 0
    if a.action == "fetch":
        print("ReelMimic in", fetch())
        return 0
    if not installed():
        print("ReelMimic is not in tools/reelmimic. Run: python -m vstudio mimic2d fetch", file=sys.stderr)
        return 1
    if not shutil.which("node"):
        print("ReelMimic needs Node.js 22.18+: https://nodejs.org", file=sys.stderr)
        return 1
    if a.action == "install":
        return subprocess.run(_script("install"), cwd=DIR).returncode
    if running():
        print("ReelMimic is already running at", URL)
        return 0
    env = dict(os.environ)
    chrome = find_chrome()
    if chrome:
        env["CHROME_PATH"] = chrome
    else:
        print("Chrome was not found; ReelMimic needs it to render frames: https://www.google.com/chrome", file=sys.stderr)
    print("Starting ReelMimic at %s (Ctrl+C to stop)" % URL)
    return subprocess.run(_script("start"), cwd=DIR, env=env).returncode


if __name__ == "__main__":
    sys.exit(main())
