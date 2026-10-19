"""
doctor.py - is this computer ready for Viral Studio?

    python -m vstudio doctor            a table of checks with a fix for each problem
    python -m vstudio doctor --json     the same as JSON (what the web studio's Settings page shows)

check() returns [{name, ok, detail, fix, required}]. `required` checks are
the ones nothing works without (Python, ffmpeg with libx264 + libass,
ffprobe); the rest switch features on (downloads, OCR, word timing, face
tracking, Claude planning, 2D remakes). main() returns 1 when a required
check fails, else 0.

The checks are cheap and local: version flags, imports looked up without
importing heavy packages, files on disk. The Claude check runs `claude
--version` and `claude auth status` (both local, no prompt is sent and no
tokens are used). Nothing is downloaded or installed here.
"""

import argparse
import concurrent.futures
import glob
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys

from . import paths

RECOMMENDED_FONTS = ["Montserrat", "Anton", "Bebas Neue", "Poppins", "Archivo Black", "Bangers"]
FONT_INSTALL = 'python -m vstudio fonts install "Montserrat" "Anton" "Bebas Neue" "Poppins" "Archivo Black" "Bangers"'
MIN_FREE_GB = 5.0


def _run(argv, timeout=20):
    """(returncode, stdout+stderr text); (None, reason) when the program cannot run."""
    kwargs = {}
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        r = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=timeout, stdin=subprocess.DEVNULL, **kwargs)
    except FileNotFoundError:
        return None, "not found"
    except subprocess.TimeoutExpired:
        return None, "timed out after %d s" % timeout
    except OSError as e:
        return None, str(e)
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def _item(name, ok, detail, fix="", required=False):
    return {"name": name, "ok": bool(ok), "detail": detail, "fix": "" if ok else fix, "required": required}


def _ffmpeg_install_hint():
    if sys.platform == "darwin":
        return "brew install ffmpeg"
    if os.name == "nt":
        return "winget install Gyan.FFmpeg   (or download from https://www.gyan.dev/ffmpeg/builds/), then reopen the terminal"
    return "sudo apt install ffmpeg   (or your distribution's package)"


def externally_managed():
    """True when pip refuses to install into this Python (PEP 668: Homebrew's and many Linux
    distributions' Pythons, outside a virtual environment)."""
    if sys.prefix != sys.base_prefix:
        return False
    try:
        import sysconfig
        return os.path.isfile(os.path.join(sysconfig.get_path("stdlib"), "EXTERNALLY-MANAGED"))
    except Exception:
        return False


def pip_fix(*packages):
    """The fix that installs `packages` for the Python running the studio.

    Only the missing packages are named: a whole requirements file fails as soon as one optional
    package has no build for this Python (OCR or whisper on a brand-new Python version).
    """
    if externally_managed():
        if os.path.isfile(os.path.join(paths.REPO, ".venv", "bin", "python")):
            return ("this Python takes no pip installs (Homebrew, uv or the system manages it); setup put the packages "
                    "in .venv: start the studio with ./studio.sh, or run `source .venv/bin/activate` first")
        return ("this Python takes no pip installs (Homebrew, uv or the system manages it): run ./setup.sh, which "
                "installs into a project environment in .venv")
    return "python -m pip install " + " ".join(packages)


# --------------------------------------------------------------------------- checks

def check_python():
    v = sys.version_info
    ok = v >= (3, 10)
    return _item("Python", ok, "%d.%d.%d at %s" % (v[0], v[1], v[2], sys.executable),
                 "install Python 3.10 or newer from https://www.python.org/downloads/", required=True)


def check_ffmpeg():
    exe = shutil.which("ffmpeg")
    if not exe:
        return _item("ffmpeg", False, "not found on PATH", _ffmpeg_install_hint(), required=True)
    code, out = _run([exe, "-hide_banner", "-version"])
    m = re.search(r"ffmpeg version (\S+)", out or "")
    version = m.group(1) if m else "unknown version"
    _, enc = _run([exe, "-hide_banner", "-encoders"])
    _, flt = _run([exe, "-hide_banner", "-filters"])
    have = {"libx264": bool(re.search(r"\blibx264\b", enc or "")),
            "aac": bool(re.search(r"\baac\b", enc or "")),
            "libass": bool(re.search(r"^\s*\S+\s+ass\s", flt or "", re.M))}
    # the audio mix (audio.py): music ducking, loudness, and amix's normalize option (ffmpeg 5.0+)
    mix = {f: bool(re.search(r"^\s*\S+\s+%s\s" % f, flt or "", re.M)) for f in ("sidechaincompress", "loudnorm", "amix")}
    if mix["amix"]:
        _, amix_help = _run([exe, "-hide_banner", "-h", "filter=amix"])
        mix["amix"] = "normalize" in (amix_help or "")
    missing = [k for k, v in have.items() if not v] + ["%s filter" % k for k, v in mix.items() if not v]
    detail = "%s with %s" % (version, ", ".join(k for k, v in have.items() if v) or "no usable encoders")
    if missing:
        detail += "; missing " + ", ".join(missing)
    # HDR phone footage (iPhone records HLG by default) is tone-mapped with zscale + tonemap (render.color_filters);
    # without them it renders washed out. Not required: everything else works, so it is said, not failed
    hdr = [f for f in ("zscale", "tonemap") if not re.search(r"^\s*\S+\s+%s\s" % f, flt or "", re.M)]
    if hdr and flt:
        detail += ("; no %s filter, so HDR phone clips (iPhone) will look washed out: a full build has it (%s)"
                   % (" / ".join(hdr), _ffmpeg_install_hint()))
    fix = ("this ffmpeg build lacks %s (needed for H.264 video, AAC audio, burned-in captions and the audio mix); "
           "install a full build, version 5.0 or newer: %s" % (", ".join(missing), _ffmpeg_install_hint()))
    return _item("ffmpeg", code == 0 and not missing, detail, fix, required=True)


def check_ffprobe():
    exe = shutil.which("ffprobe")
    if not exe:
        return _item("ffprobe", False, "not found on PATH", "it ships with ffmpeg: " + _ffmpeg_install_hint(),
                     required=True)
    code, out = _run([exe, "-hide_banner", "-version"])
    m = re.search(r"ffprobe version (\S+)", out or "")
    return _item("ffprobe", code == 0, m.group(1) if m else "found", "reinstall ffmpeg: " + _ffmpeg_install_hint(),
                 required=True)


def check_ytdlp():
    fix = pip_fix("-U", "yt-dlp")
    if importlib.util.find_spec("yt_dlp") is not None:
        try:
            from yt_dlp import version as ytv
            return _item("yt-dlp", True, "%s (Python module)" % ytv.__version__)
        except Exception as e:
            return _item("yt-dlp", False, "installed but broken: %s" % e, fix)
    exe = shutil.which("yt-dlp")
    if exe:
        code, out = _run([exe, "--version"])
        return _item("yt-dlp", False, "the yt-dlp program is installed (%s) but not the Python module this "
                     "project calls" % (out or "").strip()[:20], fix)
    return _item("yt-dlp", False, "not installed (needed for links: references, competitors, channels)", fix)


def check_rapidocr():
    ok = importlib.util.find_spec("rapidocr_onnxruntime") is not None
    return _item("RapidOCR", ok, "installed (reads on-screen text locally)" if ok else
                 "not installed (style extraction cannot read on-screen text)",
                 pip_fix("rapidocr-onnxruntime"))


def _hf_cache_dirs():
    dirs = []
    for env in ("HF_HUB_CACHE", "HUGGINGFACE_HUB_CACHE"):
        if os.environ.get(env):
            dirs.append(os.environ[env])
    if os.environ.get("HF_HOME"):
        dirs.append(os.path.join(os.environ["HF_HOME"], "hub"))
    dirs.append(os.path.join(os.path.expanduser("~"), ".cache", "huggingface", "hub"))
    dirs.append(os.path.join(paths.MODELS, "whisper"))
    dirs.append(paths.MODELS)
    return dirs


def whisper_models():
    """Names of faster-whisper models already on disk (no download needed)."""
    found = set()
    for d in _hf_cache_dirs():
        for path in glob.glob(os.path.join(d, "models--*faster-whisper*")) + glob.glob(os.path.join(d, "*faster-whisper*")):
            if os.path.isdir(path):
                name = os.path.basename(path).split("faster-whisper-")[-1]
                found.add(name.replace("models--", ""))
    return sorted(found)


def check_whisper():
    if importlib.util.find_spec("faster_whisper") is None:
        return _item("faster-whisper", False, "not installed (word timing from speech; captions files and YouTube "
                     "captions still work)", pip_fix("faster-whisper"))
    models = whisper_models()
    if models:
        return _item("faster-whisper", True, "installed; model(s) on disk: " + ", ".join(models))
    return _item("faster-whisper", True, "installed; no model downloaded yet (the first transcription downloads "
                 "about 500 MB)")


def face_model():
    """Path of the YuNet face model in workspace/models, or None."""
    hits = sorted(glob.glob(os.path.join(paths.MODELS, "*yunet*.onnx")))
    return hits[0] if hits else None


def _yunet_problem(cv2, model):
    """Why this OpenCV cannot run the YuNet model (one detection on a blank image), or None when it can.

    OpenCV 4.5.4 to 4.7 load face_detection_yunet_2023mar.onnx and then fail on every detect()."""
    try:
        import numpy as np
        from . import reframe
        reframe._cv_log_level(cv2, reframe.LOG_SILENT)   # (keep OpenCV's own error dump off the table)
        try:
            det = cv2.FaceDetectorYN.create(model, "", (64, 64), 0.7, 0.3, 5000)
            det.setInputSize((64, 64))
            det.detect(np.zeros((64, 64, 3), np.uint8))
        finally:
            reframe._cv_log_level(cv2, reframe.LOG_ERROR)
    except Exception as e:  # cv2.error, or a model file OpenCV cannot read
        lines = [ln.strip() for ln in str(e).strip().splitlines() if ln.strip()]
        return (lines[-1] if lines else type(e).__name__)[:160]
    return None


def check_face():
    try:
        import cv2
    except ImportError:
        return _item("Face tracking", False, "OpenCV is not installed", pip_fix("opencv-python-headless"))
    if not hasattr(cv2, "FaceDetectorYN"):
        return _item("Face tracking", False, "OpenCV %s has no FaceDetectorYN (reframing falls back to centre crops)"
                     % cv2.__version__, pip_fix("-U", "opencv-python-headless"))
    model = face_model()
    if model:
        why = _yunet_problem(cv2, model)
        if why:
            return _item("Face tracking", False, "OpenCV %s cannot run the YuNet face model (%s); reframing and "
                         "thumbnail faces fall back to an older detector or centre crops" % (cv2.__version__, why),
                         pip_fix("-U", "opencv-python-headless"))
        return _item("Face tracking", True, "OpenCV %s, YuNet model %s" % (cv2.__version__, os.path.basename(model)))
    return _item("Face tracking", False, "OpenCV %s; the YuNet face model is not downloaded (16:9 -> 9:16 "
                 "reframing uses centre crops until it is)" % cv2.__version__,
                 'python -c "from vstudio import reframe; print(reframe.ensure_model())"')


def check_fonts():
    try:
        from . import fonts
        faces = fonts.installed()
    except Exception as e:  # fonts.py missing or broken: say so instead of failing the whole doctor
        return _item("Fonts", False, "could not list fonts: %s" % e, FONT_INSTALL)
    families = {str(f.get("family") or "").lower() for f in faces}
    have = [f for f in RECOMMENDED_FONTS if f.lower() in families]
    missing = [f for f in RECOMMENDED_FONTS if f.lower() not in families]
    detail = "%d font faces; caption fonts: %s" % (len(faces), ", ".join(have) if have else "none of the recommended")
    if missing:
        detail += "; missing " + ", ".join(missing)
    if importlib.util.find_spec("fontTools") is None:
        # libass ignores variable fonts (Google ships Montserrat as one): without fontTools captions use the
        # closest static font instead of the exact one
        detail += "; for exact variable-font captions: python -m pip install fonttools"
    fix = ('%s   (free Google Fonts, OFL; or download from https://fonts.google.com and put the .ttf files in '
           'workspace/fonts)' % FONT_INSTALL)
    return _item("Fonts", len(have) >= 2, detail, fix)


def claude_logged_in(exe):
    """(True/False/None, how we know). `claude auth status` is local; older versions fall back to config files."""
    code, out = _run([exe, "auth", "status"], timeout=15)
    if code is not None:
        try:
            data = json.loads(out[out.index("{"):out.rindex("}") + 1])
            return bool(data.get("loggedIn")), "auth status"
        except ValueError:
            pass
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
        return True, "token in the environment"
    cfg = os.path.join(os.path.expanduser("~"), ".claude.json")
    try:
        with open(cfg, encoding="utf-8") as f:
            if "oauthAccount" in json.load(f):
                return True, "~/.claude.json"
    except (OSError, ValueError):
        pass
    if os.path.isfile(os.path.join(os.path.expanduser("~"), ".claude", ".credentials.json")):
        return True, "credentials file"
    return None, "unknown"


def _claude_exe():
    """The same executable the planner will run (llm.claude_path also looks outside PATH)."""
    try:
        from . import llm
        return llm.claude_path()
    except Exception:  # llm.py missing or broken: PATH only
        return shutil.which("claude")


def check_claude():
    exe = _claude_exe()
    fix_install = ("install Claude Code (npm install -g @anthropic-ai/claude-code, or the installer from "
                   "claude.com/claude-code), then run `claude` once and log in with your subscription")
    if not exe:
        return _item("Claude Code", False, "the `claude` command is not on PATH (planning falls back to the "
                     "offline planner; scripts and Ask Claude need it)", fix_install)
    code, out = _run([exe, "--version"], timeout=20)
    if code != 0:
        return _item("Claude Code", False, "`claude --version` failed: %s" % (out or "").strip()[:120], fix_install)
    version = (out or "").strip().splitlines()[0][:60] if out else "installed"
    logged, how = claude_logged_in(exe)
    if logged is False:
        return _item("Claude Code", False, "%s, not logged in" % version, "run `claude` once in a terminal and log in")
    if logged is None:
        return _item("Claude Code", True, "%s (login not confirmed; if planning fails, run `claude` once and log "
                     "in)" % version)
    return _item("Claude Code", True, "%s, logged in" % version)


def check_reelmimic():
    try:
        from . import mimic
        installed = mimic.installed()
        where = mimic.DIR
    except Exception:
        where = os.path.join(paths.REPO, "tools", "reelmimic")
        installed = os.path.isdir(os.path.join(where, ".claude", "skills", "video-clone"))
    if not installed:
        return _item("ReelMimic (2D remakes)", False, "not in tools/reelmimic (optional)",
                     "python -m vstudio mimic2d fetch   (or run setup again without --no-tools)")
    node = shutil.which("node")
    if not node:
        return _item("ReelMimic (2D remakes)", False, "in tools/reelmimic, but Node.js is missing",
                     "install Node.js 22.18+ from https://nodejs.org")
    return _item("ReelMimic (2D remakes)", True, "in tools/reelmimic; start it with python -m vstudio mimic2d start")


def check_disk(workspace=None):
    where = workspace or paths.WORKSPACE
    probe = where
    while probe and not os.path.exists(probe):
        parent = os.path.dirname(probe)
        if parent == probe:
            break
        probe = parent
    try:
        usage = shutil.disk_usage(probe or ".")
    except OSError as e:
        return _item("Disk space", False, "could not read free space: %s" % e, "check the workspace folder")
    free_gb = usage.free / 1e9
    return _item("Disk space", free_gb >= MIN_FREE_GB, "%.1f GB free for %s" % (free_gb, where),
                 "free up space: renders, downloads and caches need %d GB or more" % MIN_FREE_GB)


def check_python_packages():
    missing = [m for m, mod in (("numpy", "numpy"), ("Pillow", "PIL"), ("opencv-python-headless", "cv2"),
                                ("librosa", "librosa"), ("scenedetect", "scenedetect"))
               if importlib.util.find_spec(mod) is None]
    return _item("Python packages", not missing, "numpy, Pillow, OpenCV, librosa, scenedetect" if not missing else
                 "missing: " + ", ".join(missing), pip_fix(*missing),
                 required=bool({"numpy", "Pillow"} & set(missing)))


CHECKS = [check_python, check_python_packages, check_ffmpeg, check_ffprobe, check_ytdlp, check_rapidocr,
          check_whisper, check_face, check_fonts, check_claude, check_reelmimic]


def check(workspace=None):
    """Every check as [{name, ok, detail, fix, required}], in a stable order (run in parallel, a few seconds)."""
    funcs = CHECKS + [lambda: check_disk(workspace)]
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        futures = [pool.submit(f) for f in funcs]
        results = []
        for f, fut in zip(funcs, futures):
            try:
                results.append(fut.result())
            except Exception as e:     # a broken check must not hide the others
                name = getattr(f, "__name__", "check").replace("check_", "")
                results.append(_item(name, False, "check crashed: %s" % e, "report this"))
    return results


def main(argv=None):
    ap = argparse.ArgumentParser(prog="vstudio doctor", description="check that the studio's tools are available")
    ap.add_argument("--json", action="store_true", help="print the checks as JSON")
    ap.add_argument("--workspace", help="folder whose disk space to check (default: workspace/)")
    a = ap.parse_args(argv)
    results = check(a.workspace)
    if a.json:
        print(json.dumps(results, indent=2, ensure_ascii=False))
    else:
        width = max(len(r["name"]) for r in results)
        print("Viral Studio doctor\n")
        for r in results:
            mark = "ok  " if r["ok"] else ("FAIL" if r["required"] else "warn")
            print("  %s  %-*s  %s" % (mark, width, r["name"], r["detail"]))
            if not r["ok"] and r["fix"]:
                print("  %s  %-*s  fix: %s" % ("    ", width, "", r["fix"]))
        bad = [r for r in results if not r["ok"] and r["required"]]
        warn = [r for r in results if not r["ok"] and not r["required"]]
        print()
        if bad:
            print("Not ready: fix the FAIL lines first.")
        elif warn:
            print("Ready. %d optional feature(s) are off (warn lines)." % len(warn))
        else:
            print("Everything is ready.")
    return 1 if any(not r["ok"] and r["required"] for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
