"""
jobs.py - background jobs for the web studio.

Every long task the studio starts (make a reel, auto-edit, channel analysis,
style extract, render, plan compile, script, template preview) runs as its
own process, `python -m vstudio <command> ...`, never inside the web server.
A job lives in its project folder:

    <studio>/projects/<project>/jobs/<job>/
        job.json     kind, validated args, the command(s), status, times, outputs
        log.txt      everything the command printed (stdout and stderr)
        exit.json    written by the job wrapper when the command finishes
        out/         the job's output files

Lifecycle: queued -> running -> done | failed | cancelled.

How it runs. The runner starts a small wrapper, `python -m vstudio.jobs exec
<job dir>`, in its own process group. The wrapper runs the job's steps one
after the other with their output going straight into log.txt, then writes
exit.json. Because nothing flows through the web server, a job keeps
running if the studio is closed (on macOS/Linux; closing the console window
on Windows stops it), and when the studio starts again it reads every
job.json: queued jobs are queued again, running ones are picked up again if
their process is still alive, and finished in the meantime if exit.json is
there; otherwise they are marked failed ("interrupted").

Concurrency: heavy jobs (renders, reels, edits, analysis) run `max_parallel`
at a time (default 1) and the rest wait in order; light jobs (plan compile,
script writing) start immediately. Cancel terminates the whole process tree
(process group on macOS/Linux, `taskkill /T` on Windows).

Arguments come from the browser, so each kind has a whitelist of keys, every
value is type-checked, files are project-relative paths resolved inside the
project folder (no absolute paths, no '..', symlinks followed and checked),
URLs must be http(s), and commands are argument lists (never a shell).

    python -m vstudio.jobs exec <job dir>          the wrapper (started by the runner)
    python -m vstudio.jobs script --topic "..." --out-dir DIR [--seconds 35] [--wpm 170]
        writes a script with the script-forge method through Claude Code
        (vstudio.llm) and scores it with script-forge's scriptcheck.py

Limits: progress is read from what a command prints (render's "12/90
frames", "PROGRESS 40%" lines, "[3/12]" counters, yt-dlp download
percentages); a command that prints nothing shows no progress bar. The
script job runs the writing and scoring steps of script-forge; the
simulated viewer panel needs the agent (/script-forge in Claude Code).
"""

import argparse
import collections
import json
import math
import os
import re
import secrets
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time

from . import paths

KINDS = ("reel", "autoedit", "channel", "extract", "render", "script", "plan", "template")
LIGHT_KINDS = frozenset({"plan", "script"})     # never wait for a render slot
STATUSES = ("queued", "running", "done", "failed", "cancelled")
FINAL = frozenset({"done", "failed", "cancelled"})

ASPECTS = ("9:16", "1:1", "4:5", "16:9")
VIDEO_EXT = (".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi")
IMAGE_EXT = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp")
AUDIO_EXT = (".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac", ".opus")
CAPTION_EXT = (".srt", ".vtt", ".json", ".json3")

PROJECT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
JOB_ID_RE = re.compile(r"^\d{8}-\d{6}-[0-9a-f]{4,8}$")
URL_RE = re.compile(r"^https?://[^\s/?#]+[^\s]*$", re.I)
TEMPLATE_RE = re.compile(r"^[a-z0-9_]{1,40}$")
HEX_RE = re.compile(r"^#[0-9A-Fa-f]{6}([0-9A-Fa-f]{2})?$")
BARE_DOMAIN_RE = re.compile(r"^(www\.)?[\w-]+(\.[\w-]+)*\.(com|be|tv|net|org|io|co|me|app|ly|gg|fm|us|uk|in)(/|$)", re.I)

# Template preview sizes per aspect: (still, clip). Stills are larger because
# they are looked at closely; clips stay small so they render in seconds.
TEMPLATE_SIZES = {"9:16": ("540x960", "360x640"), "1:1": ("720x720", "480x480"),
                  "4:5": ("648x810", "432x540"), "16:9": ("960x540", "640x360")}

# The file a job page shows first, per kind (relative to out/).
PRIMARY = {"reel": ["reel.mp4", "spec.json", "BRIEF.md"], "autoedit": ["edited.mp4", "edit.json", "EDIT_REPORT.md"],
           "channel": ["CHANNEL_DNA.md"], "extract": ["STYLE_DNA.md"], "render": [], "script": ["script.md"],
           "plan": ["spec.json"], "template": []}

LOG_TAIL_BYTES = 16384
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


class JobError(ValueError):
    """A job request that cannot run as given. The message is written for the user."""


class PathError(ValueError):
    """A path that is absolute, climbs out with '..', names a drive, or leaves its folder."""


JobContext = collections.namedtuple("JobContext", "project_dir job_dir out_dir notes")


# --------------------------------------------------------------------------- small helpers

def now():
    return round(time.time(), 3)


def new_job_id():
    return time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(3)


def read_json(path, default=None):
    """Parse a JSON file; `default` when it is missing or unreadable (a half-written file, say)."""
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def write_json(path, data):
    """Write JSON atomically (temp file + rename), so readers never see half a file."""
    tmp = "%s.%s.tmp" % (path, secrets.token_hex(3))
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    for attempt in range(20):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:      # Windows: a reader has the file open for a moment
            time.sleep(0.05 * (attempt + 1))
    os.replace(tmp, path)


def resolve_in(root, rel, must_exist=True):
    """Absolute path of the relative path `rel` inside `root`.

    Raises PathError for anything that is not a plain relative path inside
    root: absolute paths, drive letters, '..' segments, ':' (Windows drives
    and alternate data streams), NUL bytes, and symlinks that point outside.
    Backslashes count as separators, as on Windows. With must_exist, a
    missing file raises PathError too.
    """
    if not isinstance(rel, str) or not rel.strip() or "\x00" in rel:
        raise PathError("empty path")
    rel = rel.strip().replace("\\", "/")
    if rel.startswith("/") or re.match(r"^[A-Za-z]:", rel):
        raise PathError("absolute paths are not allowed: %s" % rel)
    parts = [p for p in rel.split("/") if p not in ("", ".")]
    if not parts or any(p == ".." or ":" in p for p in parts):
        raise PathError("path leaves the project folder: %s" % rel)
    base = os.path.realpath(root)
    full = os.path.realpath(os.path.join(base, *parts))
    try:
        inside = os.path.commonpath([base, full]) == base and full != base
    except ValueError:               # different drives on Windows
        inside = False
    if not inside:
        raise PathError("path leaves the project folder: %s" % rel)
    if must_exist and not os.path.exists(full):
        raise PathError("not found in the project: %s" % rel)
    return full


def rel_to(root, path):
    """`path` relative to `root`, with forward slashes (what the browser sees)."""
    return os.path.relpath(path, root).replace(os.sep, "/")


def display_command(argv):
    """A copy-pasteable rendering of an argument list for the log (python for the interpreter path)."""
    shown = ["python" if a == sys.executable else a for a in argv]
    if os.name == "nt":
        return subprocess.list2cmdline(shown)
    return shlex.join(shown)


def looks_like_url(v):
    """True for links ('https://...', 'youtube.com/@x', '@handle'); project files are plain relative paths."""
    v = v.strip()
    return "://" in v or v.startswith(("@", "www.")) or bool(BARE_DOMAIN_RE.match(v))


def _num(x):
    """30.0 -> '30', 12.5 -> '12.5' (argument text without float noise)."""
    return ("%.3f" % x).rstrip("0").rstrip(".") if isinstance(x, float) else str(x)


def _vstudio(*args):
    return [sys.executable, "-m", "vstudio"] + [str(a) for a in args]


def _cli_supports(module, flag):
    """True when vstudio/<module>.py declares `flag`.

    Used for options that go beyond the architecture contract (for example
    autoedit --no-captions): they are passed only when the command knows
    them, so a missing option becomes a note in the log, not a failed job.
    """
    try:
        with open(os.path.join(paths.PKG, module + ".py"), encoding="utf-8") as f:
            return ('"%s"' % flag) in f.read()
    except OSError:
        return False


# --------------------------------------------------------------------------- argument checking

LABELS = {"prompt": "a prompt (what the reel is about)", "video": "a video to edit", "urls": "at least one link",
          "src": "a video file or link", "spec": "a timeline spec (.json)", "plan": "a plan (.json)",
          "topic": "a topic", "name": "a template name"}


class Args:
    """Typed, whitelisted access to one job's args dict (raises JobError with a readable message)."""

    def __init__(self, kind, args, ctx, allowed):
        if args is None:
            args = {}
        if not isinstance(args, dict):
            raise JobError("args must be an object")
        unknown = sorted(k for k in args if k not in allowed)
        if unknown:
            raise JobError("%s does not take: %s (it takes %s)" % (kind, ", ".join(unknown), ", ".join(sorted(allowed))))
        self.kind, self.a, self.ctx = kind, args, ctx

    def _blank(self, key):
        v = self.a.get(key)
        return v is None or (isinstance(v, str) and not v.strip()) or (isinstance(v, (list, dict)) and not v)

    def _missing(self, key):
        raise JobError("%s needs %s" % (self.kind, LABELS.get(key, key)))

    def text(self, key, required=False, max_len=4000):
        if self._blank(key):
            if required:
                self._missing(key)
            return None
        v = self.a[key]
        if not isinstance(v, str):
            raise JobError("%s must be text" % key)
        v = v.replace("\x00", "").strip()
        if len(v) > max_len:
            raise JobError("%s is too long (%d characters; at most %d)" % (key, len(v), max_len))
        return v

    def number(self, key, lo, hi, default=None, integer=False):
        if self._blank(key):
            return default
        v = self.a[key]
        if isinstance(v, bool):
            raise JobError("%s must be a number" % key)
        try:
            x = float(v)
        except (TypeError, ValueError):
            raise JobError("%s must be a number, got %r" % (key, v)) from None
        if not math.isfinite(x) or x < lo or x > hi:
            raise JobError("%s must be between %s and %s" % (key, _num(float(lo)), _num(float(hi))))
        return int(round(x)) if integer else x

    def choice(self, key, choices, default):
        if self._blank(key):
            return default
        v = self.a[key]
        if v not in choices:
            raise JobError("%s must be one of %s" % (key, ", ".join(choices)))
        return v

    def flag(self, key, default):
        v = self.a.get(key)
        if v is None or v == "":
            return default
        if isinstance(v, bool):
            return v
        if isinstance(v, (int, float)) and v in (0, 1):
            return bool(v)
        if isinstance(v, str) and v.lower() in ("true", "false", "yes", "no", "on", "off", "1", "0"):
            return v.lower() in ("true", "yes", "on", "1")
        raise JobError("%s must be true or false" % key)

    def _file(self, key, rel, exts):
        if not isinstance(rel, str):
            raise JobError("%s must be a file name from the project" % key)
        try:
            path = resolve_in(self.ctx.project_dir, rel)
        except PathError as e:
            raise JobError("%s: %s" % (key, e)) from None
        if not os.path.isfile(path):
            raise JobError("%s: %s is not a file" % (key, rel))
        if exts and not path.lower().endswith(tuple(exts)):
            raise JobError("%s: %s is not a %s file" % (key, rel, "/".join(e.lstrip(".") for e in exts)))
        return path

    def file(self, key, required=False, exts=None):
        if self._blank(key):
            if required:
                self._missing(key)
            return None
        return self._file(key, self.a[key], exts)

    def _list(self, key, max_n):
        v = self.a.get(key)
        if v is None or v == "":
            return []
        if isinstance(v, str):
            v = [s for s in re.split(r"[\r\n]+", v)]
        if not isinstance(v, list):
            raise JobError("%s must be a list" % key)
        v = [x.strip() if isinstance(x, str) else x for x in v]
        v = [x for x in v if x not in (None, "")]
        if len(v) > max_n:
            raise JobError("%s: at most %d items" % (key, max_n))
        return v

    def files(self, key, exts=None, max_n=50):
        return [self._file(key, rel, exts) for rel in self._list(key, max_n)]

    def _url(self, key, v):
        if not isinstance(v, str):
            raise JobError("%s must be a link or a project file" % key)
        v = v.strip()
        if v.startswith("@") and re.match(r"^@[\w.-]{2,100}$", v):     # a bare YouTube handle
            v = "https://www.youtube.com/" + v
        elif "://" not in v and BARE_DOMAIN_RE.match(v):
            v = "https://" + v                                        # pasted without the scheme
        if len(v) > 2048 or not URL_RE.match(v):
            raise JobError("%s: not a web link: %s" % (key, v[:120]))
        return v

    def _source(self, key, v):
        """A link (http/https) or a project file."""
        if isinstance(v, str) and looks_like_url(v):
            return self._url(key, v)
        return self._file(key, v, None)

    def source(self, key, required=False):
        if self._blank(key):
            if required:
                self._missing(key)
            return None
        return self._source(key, self.a[key])

    def sources(self, key, max_n=20):
        return [self._source(key, v) for v in self._list(key, max_n)]

    def urls(self, key, required=False, max_n=20):
        items = []
        for v in self._list(key, max_n):
            items += re.split(r"[\s,]+", v.strip()) if isinstance(v, str) else [v]
        items = [v for v in items if v != ""]
        if len(items) > max_n:
            raise JobError("%s: at most %d links" % (key, max_n))
        out = [self._url(key, v) for v in items]
        if required and not out:
            self._missing(key)
        return out


# --------------------------------------------------------------------------- commands per kind

def _write_text(ctx, name, text):
    path = os.path.join(ctx.job_dir, name)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return path


def _link_files(files, folder):
    """Put files into one folder (hard links when possible, else copies) for commands that take a DIR."""
    os.makedirs(folder, exist_ok=True)
    for i, src in enumerate(files):
        dst = os.path.join(folder, "%02d_%s" % (i + 1, os.path.basename(src)))
        try:
            os.link(src, dst)
        except OSError:
            shutil.copy2(src, dst)
    return folder


def _reel(r, ctx):
    plan = r.file("plan", exts=(".json",))
    prompt = r.text("prompt", required=plan is None, max_len=4000)
    argv = _vstudio("reel", "--out-dir", ctx.out_dir)
    if prompt:
        argv.append("--prompt=" + prompt)
    if plan:
        argv += ["--plan", plan]
    argv += ["--ref=" + s for s in r.sources("refs")]
    argv += ["--competitor=" + s for s in r.sources("competitors")]
    argv += ["--asset=" + f for f in r.files("assets", max_n=100)]
    script, script_text = r.file("script", exts=(".txt", ".md")), r.text("script_text", max_len=20000)
    if script and script_text:
        raise JobError("give the script as a file or as text, not both")
    if script_text:
        script = _write_text(ctx, "script.txt", script_text + "\n")
    if script:
        argv += ["--script", script]
    voice = r.file("voice", exts=AUDIO_EXT + VIDEO_EXT)
    if voice:
        argv += ["--voice", voice]
    music = r.file("music", exts=AUDIO_EXT)
    if music:
        argv += ["--music", music]
    argv += ["--aspect", r.choice("aspect", ASPECTS, "9:16")]
    argv += ["--duration", _num(r.number("duration", 3, 180, 30.0))]
    argv += ["--llm", r.choice("llm", ("auto", "claude", "none"), "auto")]
    if not r.flag("render", True):
        argv.append("--no-render")
    if r.flag("preview", False):
        argv.append("--preview")
    if not r.flag("qa", True):
        argv.append("--no-qa")  # skips the re-measure pass (about 40 s on a short preview)
    return [argv]


def _autoedit(r, ctx):
    video = r.file("video", required=True, exts=VIDEO_EXT)
    steps = []
    argv = _vstudio("autoedit", video, "--out-dir", ctx.out_dir)
    style = r.source("style")
    if style and style.lower().endswith(".json"):
        argv += ["--style", style]
    elif style:
        # A reference video or link: extract its Style DNA first, then edit with it.
        ref_dir = os.path.join(ctx.out_dir, "style_ref")
        steps.append([sys.executable, paths.skill_path("extract"), style, "--out", ref_dir])
        argv += ["--style", os.path.join(ref_dir, "style_dna.json")]
    words = r.file("words", exts=(".json",))
    if words:
        argv += ["--words", words]
    captions_file = r.file("captions_file", exts=CAPTION_EXT)
    if captions_file:
        argv += ["--captions", captions_file]
    argv += ["--aspect", r.choice("aspect", ASPECTS + ("source",), "9:16")]
    # auto (autoedit's own default) picks high/medium/calm from how fast the speaker talks
    argv += ["--energy", r.choice("energy", ("auto", "high", "medium", "calm"), "auto")]
    broll = r.files("broll", exts=VIDEO_EXT + IMAGE_EXT, max_n=100)
    if broll:
        argv += ["--broll", _link_files(broll, os.path.join(ctx.job_dir, "broll"))]
    music = r.file("music", exts=AUDIO_EXT)
    if music:
        argv += ["--music", music]
    max_duration = r.number("max_duration", 3, 3600)
    if max_duration:
        argv += ["--max-duration", _num(max_duration)]
    hook = r.text("hook", max_len=200)
    if hook:
        argv.append("--hook=" + hook)
    for key in ("zoom", "graphics", "sfx"):
        if not r.flag(key, True):
            argv.append("--no-" + key)
    if not r.flag("captions", True):
        if _cli_supports("autoedit", "--no-captions"):
            argv.append("--no-captions")
        else:
            ctx.notes.append("this version of autoedit has no --no-captions option; captions stay on")
    if r.flag("render", True):
        argv.append("--render")
    if r.flag("preview", False):
        argv.append("--preview")
    steps.append(argv)
    return steps


def _channel(r, ctx):
    urls = r.urls("urls", required=True, max_n=20)
    argv = _vstudio("channel", *urls)
    argv += ["--out-dir", ctx.out_dir,
             "--max-videos", str(r.number("max_videos", 1, 500, 30, integer=True)),
             "--deep", str(r.number("deep", 0, 30, 4, integer=True))]
    if not r.flag("download", True):
        argv.append("--no-download")
    return [argv]


def _extract(r, ctx):
    src = r.source("src", required=True)
    argv = [sys.executable, paths.skill_path("extract"), src, "--out", ctx.out_dir]
    captions = r.file("captions", exts=CAPTION_EXT)
    if captions:
        argv += ["--captions", captions]
    fps = r.number("fps", 0.5, 10)
    if fps:
        argv += ["--fps", _num(fps)]
    if r.flag("skip_edit", False):
        argv.append("--skip-edit")
    if not r.flag("whisper", True):
        argv.append("--no-whisper")
    return [argv]


def _render(r, ctx):
    spec = r.file("spec", required=True, exts=(".json",))
    still = r.number("still", 0, 36000)
    preview = r.flag("preview", False)
    if still is not None:
        out = os.path.join(ctx.out_dir, "still_%s.png" % _num(round(still, 2)).replace(".", "_"))
        argv = _vstudio("render", spec, "--out", out, "--still", _num(still))
    else:
        out = os.path.join(ctx.out_dir, "preview.mp4" if preview else "final.mp4")
        argv = _vstudio("render", spec, "--out", out)
    if preview:
        argv.append("--preview")
    return [argv]


def _plan(r, ctx):
    plan = r.file("plan", required=True, exts=(".json",))
    return [_vstudio("plan", "compile", plan, "--out", os.path.join(ctx.out_dir, "spec.json"))]


def _template(r, ctx):
    name = r.text("name", required=True, max_len=40)
    if not TEMPLATE_RE.match(name):
        raise JobError("template names are lowercase letters, digits and _")
    aspect = r.choice("aspect", ASPECTS, "9:16")
    still = r.flag("still", True)
    params = r.a.get("params") or {}
    if not isinstance(params, dict):
        raise JobError("params must be an object")
    params_json = json.dumps(params, ensure_ascii=False)
    if len(params_json) > 8000:          # it travels on the command line (Windows caps it at 32k)
        raise JobError("params are too large")
    out = os.path.join(ctx.out_dir, name + (".png" if still else ".mp4"))
    argv = _vstudio("templates", "preview", name, "--out", out, "--aspect", aspect,
                    "--size", TEMPLATE_SIZES[aspect][0 if still else 1],
                    "--duration", _num(r.number("duration", 1, 15, 3.0)))
    if params:
        argv += ["--params", params_json]
    energy = r.choice("energy", ("high", "medium", "calm"), None)
    if energy:
        argv += ["--energy", energy]
    background = r.a.get("background")
    if isinstance(background, str) and HEX_RE.match(background.strip()):
        argv += ["--background", background.strip()]
    elif not r._blank("background"):
        argv += ["--background", r.file("background", exts=VIDEO_EXT + IMAGE_EXT)]
    return [argv]


def _script(r, ctx):
    topic = r.text("topic", required=True, max_len=2000)
    argv = [sys.executable, "-m", "vstudio.jobs", "script", "--topic=" + topic, "--out-dir", ctx.out_dir,
            "--seconds", str(r.number("seconds", 8, 180, 35, integer=True)),
            "--wpm", str(r.number("wpm", 100, 280, 170, integer=True)),
            "--angles", str(r.number("angles", 1, 3, 3, integer=True))]
    notes = r.text("notes", max_len=6000)
    if notes:
        argv.append("--notes=" + notes)
    fmt = r.text("format", max_len=40)
    if fmt:
        if not re.match(r"^[a-z][a-z-]{0,39}$", fmt):
            raise JobError("format must be a format name such as storytime or tutorial")
        argv.append("--format=" + fmt)
    for key in ("style", "voice"):
        f = r.file(key, exts=(".json",))
        if f:
            argv += ["--" + key, f]
    return [argv]


ALLOWED = {
    "reel": {"prompt", "refs", "competitors", "assets", "script", "script_text", "voice", "music", "aspect",
             "duration", "llm", "plan", "render", "preview", "qa"},
    "autoedit": {"video", "style", "words", "captions_file", "aspect", "energy", "broll", "music", "max_duration",
                 "hook", "zoom", "graphics", "sfx", "captions", "render", "preview"},
    "channel": {"urls", "max_videos", "deep", "download"},
    "extract": {"src", "captions", "fps", "skip_edit", "whisper"},
    "render": {"spec", "preview", "still"},
    "plan": {"plan"},
    "template": {"name", "params", "aspect", "still", "duration", "energy", "background"},
    "script": {"topic", "seconds", "wpm", "angles", "notes", "format", "style", "voice"},
}
BUILDERS = {"reel": _reel, "autoedit": _autoedit, "channel": _channel, "extract": _extract, "render": _render,
            "plan": _plan, "template": _template, "script": _script}


def build_steps(kind, args, ctx):
    """The command(s) for a job: a list of argument lists, run in order.

    `args` is the untrusted dict from the browser; it is checked against the
    kind's whitelist and every file it names must be inside ctx.project_dir.
    Raises JobError with a message meant for the user.
    """
    if kind not in BUILDERS:
        raise JobError("unknown job kind %r (one of %s)" % (kind, ", ".join(KINDS)))
    return BUILDERS[kind](Args(kind, args, ctx, ALLOWED[kind]), ctx)


def job_title(kind, args):
    """A short human label for the jobs list."""
    a = args if isinstance(args, dict) else {}

    def short(v, n=64):
        v = " ".join(str(v).split())
        return v if len(v) <= n else v[:n - 1].rstrip() + "..."

    def base(v):
        return os.path.basename(str(v).replace("\\", "/")) if v else ""

    if kind == "reel":
        return short(a.get("prompt") or base(a.get("plan")) or "reel")
    if kind == "autoedit":
        return "Edit " + base(a.get("video"))
    if kind == "channel":
        urls = a.get("urls") or []
        if isinstance(urls, str):
            urls = urls.split()
        first = short(urls[0], 48) if urls else ""
        return "Channel " + first + (" +%d" % (len(urls) - 1) if len(urls) > 1 else "")
    if kind == "extract":
        return "Style of " + short(base(a.get("src")) if "://" not in str(a.get("src")) else a.get("src"), 48)
    if kind == "render":
        what = base(a.get("spec"))
        if a.get("still") not in (None, ""):
            return "Still %ss of %s" % (a.get("still"), what)
        return ("Preview " if a.get("preview") else "Render ") + what
    if kind == "plan":
        return "Compile " + base(a.get("plan"))
    if kind == "template":
        return "Template " + str(a.get("name") or "")
    if kind == "script":
        return "Script: " + short(a.get("topic") or "", 56)
    return kind


# --------------------------------------------------------------------------- log reading

FRAMES_RE = re.compile(r"(\d+)\s*/\s*(\d+)\s+frames")
PROGRESS_RE = re.compile(r"\bPROGRESS\s+(\d+(?:\.\d+)?)\s*(%?)", re.I)
COUNTER_RE = re.compile(r"^\s*\[(\d+)\s*/\s*(\d+)\]")
DOWNLOAD_RE = re.compile(r"^\[download\]\s+(\d+(?:\.\d+)?)%")


def log_lines(text):
    """Lines as a terminal would show them: carriage-return updates collapse to their last state."""
    out = []
    for line in ANSI_RE.sub("", text).split("\n"):
        out.append(line.split("\r")[-1] if "\r" in line.rstrip("\r") else line.rstrip("\r"))
    return out


def read_tail(path, nbytes=LOG_TAIL_BYTES):
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - nbytes))
            data = f.read()
    except OSError:
        return ""
    text = data.decode("utf-8", errors="replace")
    if size > nbytes and "\n" in text:
        text = text.split("\n", 1)[1]            # drop the partial first line
    return text


def parse_progress(text):
    """(fraction or None, stage line) from the end of a log."""
    lines = [l for l in log_lines(text) if l.strip()]
    frac = None
    for line in reversed(lines[-60:]):
        m = FRAMES_RE.search(line)
        if m and int(m.group(2)) > 0:
            frac = int(m.group(1)) / int(m.group(2))
            break
        m = PROGRESS_RE.search(line)
        if m:
            v = float(m.group(1))
            frac = v / 100.0 if (m.group(2) or v > 1) else v
            break
        m = COUNTER_RE.match(line) or None
        if m and int(m.group(2)) > 0:
            frac = int(m.group(1)) / int(m.group(2))
            break
        m = DOWNLOAD_RE.match(line)
        if m:
            frac = float(m.group(1)) / 100.0
            break
    stage = ""
    for line in reversed(lines):
        s = line.strip()
        if s and not s.startswith("== "):
            stage = s
            break
    if len(stage) > 160:
        stage = stage[:157] + "..."
    return (None if frac is None else max(0.0, min(1.0, frac))), stage


def failure_reason(text):
    """The most useful line of a failed job's log: an error line if there is one, else the last line."""
    lines = [l.strip() for l in log_lines(text) if l.strip() and not l.startswith("== ")]
    for line in reversed(lines[-40:]):
        if re.search(r"(error|failed|not found|missing|cannot|can't|unable|denied|traceback)", line, re.I) \
                and not line.lower().startswith("traceback"):
            return line[:300]
    return lines[-1][:300] if lines else "the command exited without output"


def list_outputs(project_dir, out_dir, limit=400):
    """Files under out/ as [{path, name, size, type}], the ones people open first (video, reports) first."""
    found = []
    for folder, dirs, files in os.walk(out_dir):
        dirs.sort()
        for name in sorted(files):
            if name.endswith((".tmp", ".part")) or name.startswith("."):
                continue
            full = os.path.join(folder, name)
            try:
                st = os.stat(full)
            except OSError:
                continue
            found.append({"path": rel_to(project_dir, full), "name": name, "size": st.st_size,
                          "mtime": round(st.st_mtime, 3), "type": file_type(name),
                          "depth": rel_to(out_dir, full).count("/")})
            if len(found) >= limit:
                break
        if len(found) >= limit:
            break
    rank = {"video": 0, "markdown": 1, "image": 2, "json": 3, "audio": 4, "text": 5, "other": 6}
    found.sort(key=lambda f: (f["depth"], rank.get(f["type"], 9), f["name"].lower()))
    for f in found:
        del f["depth"]
    return found


def file_type(name):
    ext = os.path.splitext(name)[1].lower()
    if ext in VIDEO_EXT:
        return "video"
    if ext in IMAGE_EXT:
        return "image"
    if ext in AUDIO_EXT:
        return "audio"
    if ext in (".md", ".markdown"):
        return "markdown"
    if ext == ".json":
        return "json"
    if ext in (".txt", ".srt", ".vtt", ".ass", ".log", ".csv"):
        return "text"
    return "other"


# --------------------------------------------------------------------------- processes

def _popen_group_kwargs():
    """Start a process as the leader of its own group, so cancel can stop everything it started."""
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def kill_tree(pid, grace=4.0):
    """Terminate a job's wrapper and everything it started (renders spawn ffmpeg, for example)."""
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return
    try:
        os.killpg(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return
    deadline = time.time() + grace
    while time.time() < deadline:
        try:
            os.killpg(pid, 0)
        except (ProcessLookupError, PermissionError):
            return
        time.sleep(0.1)
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def _windows_alive(pid):
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.restype = wintypes.HANDLE
    handle = k32.OpenProcess(0x1000, False, pid)          # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return False
    try:
        code = wintypes.DWORD()
        if not k32.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value != 259:   # STILL_ACTIVE
            return False
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(1024)
        if k32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return "python" in os.path.basename(buf.value).lower()
        return True
    finally:
        k32.CloseHandle(handle)


def wrapper_alive(pid, job_id):
    """True when `pid` is still this job's wrapper (checked by command line, so a reused pid does not count)."""
    if not pid:
        return False
    if os.name == "nt":
        try:
            return _windows_alive(int(pid))
        except Exception:
            return False
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    cmdline = ""
    try:
        with open("/proc/%d/cmdline" % int(pid), "rb") as f:
            cmdline = f.read().replace(b"\x00", b" ").decode("utf-8", "replace")
    except OSError:
        try:
            cmdline = subprocess.run(["ps", "-p", str(int(pid)), "-o", "command="], capture_output=True,
                                     text=True, timeout=5).stdout
        except (OSError, subprocess.SubprocessError):
            return False
    return "vstudio.jobs" in cmdline and job_id in cmdline


def job_env():
    """Environment for job processes: unbuffered UTF-8 output so the log streams on every platform."""
    env = dict(os.environ)
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    return env


# --------------------------------------------------------------------------- the runner

class JobRunner:
    """Start, queue, watch and cancel jobs for the projects under `root`/projects.

    builder(kind, args, ctx) -> [argv, ...] turns a request into commands;
    tests inject their own to run quick `python -c` jobs.
    """

    def __init__(self, root, builder=None, max_parallel=1, light_kinds=LIGHT_KINDS):
        self.root = os.path.abspath(root)
        self.projects_dir = os.path.join(self.root, "projects")
        os.makedirs(self.projects_dir, exist_ok=True)
        self.builder = builder or build_steps
        self.max_parallel = max(1, int(max_parallel))
        self.light_kinds = frozenset(light_kinds)
        self._lock = threading.RLock()
        self._changed = threading.Condition(self._lock)
        self._jobs = {}        # id -> job record (what job.json holds)
        self._procs = {}       # id -> Popen of the wrapper (jobs started by this runner)
        self._queue = []       # ids waiting for a slot, oldest first
        self._closed = False
        self._load()
        self._pump()

    # ---- folders

    def project_dir(self, project_id):
        if not isinstance(project_id, str) or not PROJECT_ID_RE.match(project_id):
            raise JobError("bad project id")
        path = os.path.join(self.projects_dir, project_id)
        if not os.path.isdir(path):
            raise JobError("no project %s" % project_id)
        return path

    def job_dir(self, job):
        return os.path.join(self.projects_dir, job["project"], "jobs", job["id"])

    def _save(self, job):
        write_json(os.path.join(self.job_dir(job), "job.json"), job)

    # ---- restart recovery

    def _load(self):
        records = []
        for pid in sorted(os.listdir(self.projects_dir)):
            jobs_dir = os.path.join(self.projects_dir, pid, "jobs")
            if not PROJECT_ID_RE.match(pid) or not os.path.isdir(jobs_dir):
                continue
            for jid in os.listdir(jobs_dir):
                job = read_json(os.path.join(jobs_dir, jid, "job.json"))
                if isinstance(job, dict) and job.get("id") == jid and JOB_ID_RE.match(jid):
                    job["project"] = pid
                    records.append(job)
        records.sort(key=lambda j: j.get("created") or 0)
        for job in records:
            self._jobs[job["id"]] = job
            status = job.get("status")
            if status == "queued":
                self._queue.append(job["id"])
            elif status == "running":
                self._recover(job)

    def _recover(self, job):
        exit_info = read_json(os.path.join(self.job_dir(job), "exit.json"))
        if exit_info is not None:
            self._finish(job, exit_info.get("code"), exit_info.get("ended"))
        elif wrapper_alive(job.get("pid"), job["id"]):
            job["detached"] = True
            threading.Thread(target=self._watch_detached, args=(job["id"],), daemon=True).start()
        else:
            self._finish(job, None, None, interrupted=True)

    # ---- public API

    def submit(self, project_id, kind, args=None):
        """Validate and queue a job; returns its record. Raises JobError for a bad request."""
        if self._closed:
            raise JobError("the studio is shutting down")
        pdir = self.project_dir(project_id)
        jid = new_job_id()
        while os.path.exists(os.path.join(pdir, "jobs", jid)):
            jid = new_job_id()
        jdir = os.path.join(pdir, "jobs", jid)
        out = os.path.join(jdir, "out")
        os.makedirs(out)
        ctx = JobContext(pdir, jdir, out, [])
        try:
            steps = self.builder(kind, args if args is not None else {}, ctx)
            if not steps or not all(isinstance(s, list) and s and all(isinstance(a, str) for a in s) for s in steps):
                raise JobError("the job has no command to run")
        except Exception:
            shutil.rmtree(jdir, ignore_errors=True)
            raise
        job = {"id": jid, "project": project_id, "kind": kind, "title": job_title(kind, args), "args": args or {},
               "steps": steps, "notes": ctx.notes, "cwd": paths.REPO, "status": "queued", "exit_code": None,
               "error": None, "created": now(), "started": None, "ended": None, "pid": None,
               "light": kind in self.light_kinds, "cancel_requested": False, "outputs": []}
        with self._lock:
            self._save(job)
            self._jobs[jid] = job
            self._queue.append(jid)
            self._changed.notify_all()
        self._pump()
        return self.get(jid)

    def get(self, job_id, detail=True):
        """A copy of the job record, with live progress, queue position and outputs."""
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return None
            out = json.loads(json.dumps(job))
            queue_pos = self._queue.index(job_id) + 1 if job_id in self._queue else None
        jdir = self.job_dir(out)
        out["queue_position"] = queue_pos
        out["command"] = " && ".join(display_command(s) for s in out.get("steps") or [])
        out["progress"], out["stage"] = None, ""
        if out["status"] == "running":
            out["progress"], out["stage"] = parse_progress(read_tail(os.path.join(jdir, "log.txt")))
        elif out["status"] == "done":
            out["progress"] = 1.0
        if out["status"] == "running" and detail:
            out["outputs"] = list_outputs(os.path.dirname(os.path.dirname(jdir)), os.path.join(jdir, "out"))
        out["primary"] = self._primary(out)
        if out.get("started"):
            out["elapsed"] = round((out.get("ended") or time.time()) - out["started"], 1)
        if not detail:
            for k in ("steps", "outputs", "cwd", "notes", "command"):
                out.pop(k, None)
        return out

    def _primary(self, job):
        outs = job.get("outputs") or []
        prefix = "jobs/%s/out/" % job["id"]
        for name in PRIMARY.get(job["kind"], []):
            for o in outs:
                if o["path"] == prefix + name:
                    return o["path"]
        for kind in ("video", "image", "markdown"):
            for o in outs:
                if o["type"] == kind:
                    return o["path"]
        return outs[0]["path"] if outs else None

    def list(self, project_id=None, limit=200):
        """Job summaries, newest first (optionally for one project)."""
        with self._lock:
            ids = [j["id"] for j in self._jobs.values() if project_id is None or j["project"] == project_id]
        ids.sort(reverse=True)
        return [self.get(i, detail=False) for i in ids[:limit]]

    def counts(self):
        with self._lock:
            c = collections.Counter(j["status"] for j in self._jobs.values())
        return {s: c.get(s, 0) for s in STATUSES}

    def log(self, job_id, offset=0, max_bytes=262144):
        """(text, next_offset, size) of the job's log from byte `offset` (restarts at 0 if the log shrank)."""
        job = self._jobs.get(job_id)
        if job is None:
            return None
        path = os.path.join(self.job_dir(job), "log.txt")
        try:
            size = os.path.getsize(path)
        except OSError:
            return "", 0, 0
        offset = max(0, int(offset or 0))
        if offset > size:
            offset = 0
        start = max(offset, size - max_bytes)
        with open(path, "rb") as f:
            f.seek(start)
            data = f.read(size - start)
        return data.decode("utf-8", errors="replace"), start + len(data), size

    def cancel(self, job_id):
        """Cancel a queued job, or stop a running one and everything it started. Returns the job."""
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return None
            if job["status"] == "queued":
                if job_id in self._queue:
                    self._queue.remove(job_id)
                job.update(status="cancelled", ended=now(), cancel_requested=True)
                self._save(job)
                self._changed.notify_all()
            elif job["status"] == "running" and not job.get("cancel_requested"):
                job["cancel_requested"] = True
                self._save(job)
                pid = job.get("pid")
                if pid and (job_id in self._procs or wrapper_alive(pid, job_id)):
                    threading.Thread(target=kill_tree, args=(pid,), daemon=True).start()
        return self.get(job_id)

    def wait(self, job_id, timeout=None):
        """Block until the job is finished (or the timeout passes); returns the job."""
        deadline = None if timeout is None else time.time() + timeout
        with self._lock:
            while self._jobs.get(job_id, {}).get("status") not in FINAL:
                left = None if deadline is None else deadline - time.time()
                if left is not None and left <= 0:
                    break
                self._changed.wait(0.5 if left is None else min(0.5, left))
        return self.get(job_id)

    def close(self, cancel_running=False):
        """Stop starting jobs. Running jobs keep going (and are picked up next start) unless cancel_running."""
        with self._lock:
            self._closed = True
            running = [j for j in self._jobs.values() if j["status"] == "running"]
        if cancel_running:
            for job in running:
                self.cancel(job["id"])
            for job in running:
                self.wait(job["id"], timeout=10)
        return len(running)

    # ---- scheduling

    def _pump(self):
        """Start queued jobs while slots are free (light jobs always start)."""
        with self._lock:
            if self._closed:
                return
            heavy = sum(1 for j in self._jobs.values() if j["status"] == "running" and not j.get("light"))
            for jid in list(self._queue):
                job = self._jobs[jid]
                if not job.get("light"):
                    if heavy >= self.max_parallel:
                        continue
                    heavy += 1
                self._queue.remove(jid)
                self._start(job)

    def _start(self, job):
        jdir = self.job_dir(job)
        job.update(status="running", started=now())
        try:
            log = open(os.path.join(jdir, "log.txt"), "ab")
            try:
                proc = subprocess.Popen([sys.executable, "-m", "vstudio.jobs", "exec", jdir],
                                        stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                        cwd=paths.REPO, env=job_env(), **_popen_group_kwargs())
            finally:
                log.close()
        except OSError as e:
            job.update(status="failed", ended=now(), error="could not start: %s" % e)
            self._save(job)
            self._changed.notify_all()
            return
        job["pid"] = proc.pid
        self._procs[job["id"]] = proc
        self._save(job)
        self._changed.notify_all()
        threading.Thread(target=self._watch, args=(job["id"], proc), daemon=True).start()

    def _watch(self, job_id, proc):
        proc.wait()
        with self._lock:
            self._procs.pop(job_id, None)
            job = self._jobs[job_id]
            info = read_json(os.path.join(self.job_dir(job), "exit.json"))
            code = info.get("code") if info else proc.returncode
            self._finish(job, code, info.get("ended") if info else None, wrapper_died=info is None)
        self._pump()

    def _watch_detached(self, job_id):
        """Follow a job started by an earlier run of the studio until its wrapper exits."""
        job = self._jobs[job_id]
        exit_path = os.path.join(self.job_dir(job), "exit.json")
        while True:
            info = read_json(exit_path)
            if info is not None or not wrapper_alive(job.get("pid"), job_id):
                time.sleep(0.2)
                info = info or read_json(exit_path)
                with self._lock:
                    if info is not None:
                        self._finish(job, info.get("code"), info.get("ended"))
                    else:
                        self._finish(job, None, None, interrupted=not job.get("cancel_requested"))
                self._pump()
                return
            time.sleep(1.0)

    def _finish(self, job, code, ended, interrupted=False, wrapper_died=False):
        with self._lock:
            jdir = self.job_dir(job)
            job["exit_code"] = code
            job["ended"] = ended or now()
            job.pop("detached", None)
            if code == 0 and not wrapper_died and not interrupted:
                job["status"], job["error"] = "done", None      # finished before a cancel could land
            elif job.get("cancel_requested"):
                job["status"], job["error"] = "cancelled", None
            elif interrupted:
                job["status"] = "failed"
                job["error"] = "interrupted: the studio stopped while this job was running"
            else:
                job["status"] = "failed"
                job["error"] = failure_reason(read_tail(os.path.join(jdir, "log.txt")))
            job["outputs"] = list_outputs(os.path.dirname(os.path.dirname(jdir)), os.path.join(jdir, "out"))
            self._save(job)
            self._changed.notify_all()


# --------------------------------------------------------------------------- the wrapper process

def exec_job(job_dir):
    """Run a job's steps with output into log.txt, then write exit.json. Returns the exit code."""
    job = read_json(os.path.join(job_dir, "job.json"))
    if not isinstance(job, dict) or not job.get("steps"):
        print("job.json is missing or has no steps", flush=True)
        return 2
    t0 = time.time()
    code = 0
    print("== %s job %s started %s" % (job.get("kind"), job.get("id"), time.strftime("%Y-%m-%d %H:%M:%S")), flush=True)
    for note in job.get("notes") or []:
        print("note: " + note, flush=True)
    steps = job["steps"]
    for i, argv in enumerate(steps):
        if len(steps) > 1:
            print("== step %d/%d" % (i + 1, len(steps)), flush=True)
        print("$ " + display_command(argv), flush=True)
        try:
            # The log is handed over explicitly: on Windows a child does not reliably inherit
            # file handles it is not given.
            with open(os.path.join(job_dir, "log.txt"), "ab") as log:
                code = subprocess.call(argv, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                       cwd=job.get("cwd") or paths.REPO)
        except OSError as e:
            print("could not start %s: %s" % (argv[0], e), flush=True)
            code = 127
        if code != 0:
            break
    print("== exit %s after %.1f s" % (code, time.time() - t0), flush=True)
    write_json(os.path.join(job_dir, "exit.json"), {"code": code, "ended": now()})
    return code


# --------------------------------------------------------------------------- script job (script-forge)

SCRIPT_SYSTEM = """You write short-form video scripts the way script-forge does: for the ear, not the page.
The creator says every line into a phone to a friend. Never write from a default assistant voice.
Never invent numbers, clients, results, quotes or events: when the script needs a real number or
moment the brief does not give, write a bracketed placeholder like [YOUR NUMBER] or [THE CLIENT'S NAME]
and list it under "missing". Never copy lines from reference transcripts; borrow rhythm only.
Answer with JSON only."""

SCRIPT_RULES = """Method (script-forge):
1. Find the true story first: the claim (one sentence), the proof (the number, the moment, the
   before/after), the turn (what most people get wrong, or what happened next).
2. Write {angles} angle(s) on the same story, each as dictation: usually contrarian ("stop doing X"),
   story (in medias res, past tense, "I"), proof/demo (result first).
   - Start on the claim, conflict or result. Never "hey guys", "in this video", "did you know",
     "have you ever".
   - At least a third of the sentences under six words; one longer run where the energy rises.
   - Contract everything a person would. "And", "but", "so" can start sentences.
   - One real number (or placeholder) in the first two lines; one proper noun or a time somewhere.
   - Re-hook every 8 to 10 seconds with a turn: "but", "so then", "wait", "except".
   - No moral at the end: end on the payoff, a loop back to line one, or one single ask.
   - No em dashes, no semicolons, no "delve", "game-changer", "unlock", "elevate", "in today's world".
   - Length: about {words} spoken words ({seconds} s at {wpm} wpm).
3. Each beat is one spoken line ("say", exactly as said), the on-screen text for it (2-6 words,
   caption style), the shot or B-roll, and a short delivery note.
4. Give two alternative hooks per angle."""

SCRIPT_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "story": {"type": "object", "properties": {"claim": {"type": "string"}, "proof": {"type": "string"},
                                                    "turn": {"type": "string"}},
                  "required": ["claim", "proof", "turn"]},
        "missing": {"type": "array", "items": {"type": "string"}},
        "drafts": {"type": "array", "minItems": 1, "items": {
            "type": "object",
            "properties": {
                "angle": {"type": "string"},
                "beats": {"type": "array", "minItems": 1, "items": {
                    "type": "object",
                    "properties": {"say": {"type": "string"}, "on_screen": {"type": "string"},
                                   "shot": {"type": "string"}, "delivery": {"type": "string"}},
                    "required": ["say"]}},
                "alt_hooks": {"type": "array", "items": {"type": "string"}}},
            "required": ["angle", "beats"]}},
    },
    "required": ["story", "drafts"],
}


def _progress(pct, msg):
    print("PROGRESS %d%% %s" % (pct, msg), flush=True)


def _clip(text, n):
    text = text or ""
    return text if len(text) <= n else text[:n] + "\n[...truncated]"


def _format_section(fmt):
    """The playbook section for one format from script-forge's references/formats.md ('' if unknown)."""
    path = os.path.join(paths.SKILLS, "script-forge", "references", "formats.md")
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return ""
    if not fmt:
        return _clip(text, 7000)
    m = re.search(r"^## %s\s*$(.*?)(?=^## |\Z)" % re.escape(fmt.strip().lower()), text, re.M | re.S)
    return ("## %s%s" % (fmt, m.group(1))).strip() if m else ""


def _style_summary(style_path):
    """Pace and hook facts from a style_dna.json (viral-extract), for the prompt."""
    dna = read_json(style_path) if style_path else None
    if not isinstance(dna, dict):
        return "", None
    dialogue = dna.get("dialogue") or {}
    # Numbers only: the reference's own words (hook text, CTA text, phrases) stay out of the prompt,
    # so nothing can be copied from it.
    keep = {k: dialogue[k] for k in ("wpm", "words", "time_to_first_word_s", "speech_coverage", "sentences",
                                     "pauses", "loop_ending") if k in dialogue}
    if isinstance(dialogue.get("hook"), dict):
        keep["hook_words"] = dialogue["hook"].get("words")
    if isinstance(dialogue.get("rehooks"), dict):
        keep["rehook_mean_gap_s"] = dialogue["rehooks"].get("mean_gap_s")
    wpm = dialogue.get("wpm")
    text = json.dumps(keep, ensure_ascii=False, indent=1) if keep else ""
    return _clip(text, 3000), (wpm if isinstance(wpm, (int, float)) else None)


def _spoken_text(draft):
    return "\n".join((b.get("say") or "").strip() for b in draft.get("beats") or [] if (b.get("say") or "").strip())


def _scriptcheck(files, seconds, wpm, voice):
    """Score draft files with script-forge's scriptcheck.py; returns {path: result}."""
    argv = [sys.executable, paths.skill_path("scriptcheck")] + files + ["--seconds", str(seconds), "--wpm", str(wpm),
                                                                       "--json"]
    if voice:
        argv += ["--voice", voice]
    r = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
    if r.returncode != 0:
        raise RuntimeError("scriptcheck failed: " + (r.stderr.strip().splitlines() or ["no output"])[-1])
    data = json.loads(r.stdout)
    return data if len(files) > 1 else {files[0]: data}


def _detect(path):
    """ig-human's detect.py on the spoken lines (second opinion); None when it is not there."""
    script = os.path.join(paths.SKILLS, "ig-human", "detect.py")
    if not os.path.isfile(script):
        return None
    try:
        r = subprocess.run([sys.executable, script, path, "--json"], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=60)
        return json.loads(r.stdout) if r.returncode == 0 else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def _cell(text):
    return " ".join(str(text or "").split()).replace("|", "\\|")


def _timecode(t):
    return "%d:%02d" % (int(t) // 60, int(t) % 60)


def _sheet(title, topic, draft, score, human, data, wpm, seconds, others):
    """The production sheet (script-forge step 5) as markdown."""
    lines = ["# " + (title or "Script"), "",
             "| | |", "| --- | --- |",
             "| Topic | %s |" % _cell(topic),
             "| Angle | %s |" % _cell(draft.get("angle")),
             "| Length | ~%s s at %s wpm (target %s s) |" % (score.get("seconds"), wpm, seconds),
             "| Score | scriptcheck **%s** (%s)%s |" % (score.get("overall"), score.get("verdict"),
                                                     ", red: " + ", ".join(score["red"]) if score.get("red") else ""),
             ]
    if human:
        lines.append("| Human check | detect.py %s (%s) |" % (human.get("human_score"), human.get("verdict")))
    lines += ["", "| time | spoken (exactly as said) | on-screen text | shot / B-roll | delivery |",
              "| --- | --- | --- | --- | --- |"]
    t = 0.0
    for b in draft.get("beats") or []:
        say = (b.get("say") or "").strip()
        n = len(re.findall(r"[A-Za-z0-9$%']+", re.sub(r"\[[^\]]*\]", "x", say)))
        dt = max(0.8, n / float(wpm) * 60.0)
        lines.append("| %s-%s | %s | %s | %s | %s |" % (_timecode(t), _timecode(t + dt), _cell(say),
                                                       _cell(b.get("on_screen")), _cell(b.get("shot")),
                                                       _cell(b.get("delivery"))))
        t += dt
    story = data.get("story") or {}
    lines += ["", "## The story", "", "- **Claim:** " + _cell(story.get("claim")),
              "- **Proof:** " + _cell(story.get("proof")), "- **Turn:** " + _cell(story.get("turn"))]
    missing = [m for m in data.get("missing") or [] if str(m).strip()]
    if missing:
        lines += ["", "## Fill these in before you shoot", ""]
        lines += ["- " + _cell(m) for m in missing]
        lines += ["", "Bracketed placeholders are facts only you have. Nothing was made up for them."]
    hooks = [h for h in draft.get("alt_hooks") or [] if str(h).strip()]
    if hooks:
        lines += ["", "## Alternative hooks (for an A/B repost)", ""] + ["- " + _cell(h) for h in hooks]
    checks = score.get("checks") or {}
    if checks:
        lines += ["", "## Score", "", "| check | score | detail |", "| --- | --- | --- |"]
        for k, v in checks.items():
            lines.append("| %s | %s | %s |" % (k, "-" if v.get("score") is None else v["score"], _cell(v.get("detail"))))
    flags = score.get("flags") or []
    if flags:
        lines += ["", "## Still worth fixing", ""]
        lines += ["- [%s] %s%s" % (f["check"], ("\"%s\": " % _cell(f["line"])[:90]) if f.get("line") else "",
                                   _cell(f["fix"])) for f in flags[:12]]
    if others:
        lines += ["", "## Other angles", ""]
        for d, s in others:
            lines += ["### %s (scriptcheck %s, %s)" % (_cell(d.get("angle")), s.get("overall"), s.get("verdict")), ""]
            lines += [_cell(b.get("say")) for b in d.get("beats") or []] + [""]
    lines += ["", "## Next", "",
              "- Read it out loud once at %s wpm. Anything you stumble on, rewrite." % wpm,
              "- For the simulated viewer panel (where viewers would swipe), run `/script-forge` in Claude Code "
              "on this file. It is a simulation that finds weak beats, not a forecast of real numbers."]
    return "\n".join(lines) + "\n"


def script_main(argv=None):
    """Write a script with Claude Code (script-forge method), score it with scriptcheck, write script.md."""
    ap = argparse.ArgumentParser(prog="python -m vstudio.jobs script")
    ap.add_argument("--topic", required=True)
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--seconds", type=int, default=35)
    ap.add_argument("--wpm", type=int, default=170)
    ap.add_argument("--angles", type=int, default=3)
    ap.add_argument("--notes", default="")
    ap.add_argument("--format", default="")
    ap.add_argument("--style", help="style_dna.json of a reference (pace, hook, re-hooks)")
    ap.add_argument("--voice", help="voice.json from voiceprint.py (default: workspace/voice.json)")
    ap.add_argument("--rounds", type=int, default=2, help="fix rounds while the score is under 80 (default 2)")
    a = ap.parse_args(argv)
    os.makedirs(a.out_dir, exist_ok=True)
    try:
        import importlib
        llm = importlib.import_module("vstudio.llm")
    except ImportError:
        print("error: script writing needs vstudio/llm.py (the Claude Code wrapper), which is missing from this "
              "copy. Update the repo, or write the script in Claude Code with /script-forge.", flush=True)
        return 3
    if not llm.available():
        print("error: Claude Code is not installed or not logged in. Install it (npm install -g "
              "@anthropic-ai/claude-code), run `claude` once to log in, then try again.", flush=True)
        return 3
    voice = a.voice or os.path.join(paths.WORKSPACE, "voice.json")
    voice = voice if os.path.isfile(voice) else None
    style_text, ref_wpm = _style_summary(a.style)
    wpm = int(round(ref_wpm)) if ref_wpm and 100 <= ref_wpm <= 280 and a.wpm == 170 else a.wpm
    words = int(round(a.seconds * wpm / 60.0))
    brand = ""
    for name in ("brand.md",):
        p = os.path.join(paths.WORKSPACE, name)
        if os.path.isfile(p):
            with open(p, encoding="utf-8", errors="replace") as f:
                brand = _clip(f.read(), 6000)
    parts = ["# Topic", a.topic, "", SCRIPT_RULES.format(angles=a.angles, words=words, seconds=a.seconds, wpm=wpm)]
    if a.notes:
        parts += ["", "# Notes from the creator (facts you may use)", a.notes]
    if brand:
        parts += ["", "# brand.md (audience, what they film, what is off-limits)", brand]
    if voice:
        parts += ["", "# The creator's voiceprint (match sentence length, contractions, signature phrases)",
                  _clip(json.dumps(read_json(voice), ensure_ascii=False), 3000)]
    if style_text:
        parts += ["", "# Reference video dialogue DNA (borrow pace and re-hook spacing, never lines)", style_text]
    fmt = _format_section(a.format)
    if fmt:
        parts += ["", "# Format playbook", fmt]
    parts += ["", "Return JSON: title, story {claim, proof, turn}, missing [facts to fill in], drafts "
                  "[{angle, beats [{say, on_screen, shot, delivery}], alt_hooks}] with %d draft(s)." % a.angles]
    _progress(5, "writing %d angle(s) with Claude" % a.angles)
    try:
        data = llm.complete_json("\n".join(parts), schema=SCRIPT_SCHEMA, system=SCRIPT_SYSTEM, timeout=900)
    except Exception as e:      # LLMUnavailable or a bad answer: say what happened
        print("error: Claude could not write the script: %s" % e, flush=True)
        return 4
    drafts = [d for d in data.get("drafts") or [] if isinstance(d, dict) and _spoken_text(d)]
    if not drafts:
        print("error: Claude returned no usable draft", flush=True)
        return 4
    draft_dir = os.path.join(a.out_dir, "drafts")
    os.makedirs(draft_dir, exist_ok=True)
    files = []
    for i, d in enumerate(drafts):
        path = os.path.join(draft_dir, "%s.md" % "abcdefgh"[i])
        with open(path, "w", encoding="utf-8") as f:
            f.write(_spoken_text(d) + "\n")
        files.append(path)
    _progress(45, "scoring %d draft(s) with scriptcheck" % len(files))
    scores = _scriptcheck(files, a.seconds, wpm, voice)
    ranked = sorted(zip(drafts, files), key=lambda df: -scores[df[1]]["overall"])
    best, best_file = ranked[0]
    best_score = scores[best_file]
    print("best angle: %s, scriptcheck %s (%s)" % (best.get("angle"), best_score["overall"], best_score["verdict"]),
          flush=True)
    revisions = 0
    while revisions < a.rounds and (best_score["overall"] < 80 or best_score["red"]):
        revisions += 1
        _progress(55 + 15 * revisions, "fix round %d: %s" % (revisions, ", ".join(best_score["red"]) or "under 80"))
        fix = ["Rewrite this draft to fix every flag below. Keep the angle, the story and every real fact; keep "
               "placeholders in brackets. Same JSON shape, one draft.", "",
               "# Draft", json.dumps(best, ensure_ascii=False, indent=1), "",
               "# scriptcheck (%s overall, %s)" % (best_score["overall"], best_score["verdict"]),
               json.dumps({"checks": best_score["checks"], "flags": best_score["flags"]}, ensure_ascii=False,
                          indent=1), "", SCRIPT_RULES.format(angles=1, words=words, seconds=a.seconds, wpm=wpm)]
        try:
            fixed = llm.complete_json("\n".join(fix), schema=SCRIPT_SCHEMA, system=SCRIPT_SYSTEM, timeout=900)
        except Exception as e:
            print("fix round failed: %s" % e, flush=True)
            break
        cand = next((d for d in fixed.get("drafts") or [] if isinstance(d, dict) and _spoken_text(d)), None)
        if not cand:
            break
        cand_file = os.path.join(draft_dir, "%s_fix%d.md" % (os.path.splitext(os.path.basename(best_file))[0], revisions))
        with open(cand_file, "w", encoding="utf-8") as f:
            f.write(_spoken_text(cand) + "\n")
        cand_score = _scriptcheck([cand_file], a.seconds, wpm, voice)[cand_file]
        print("fix round %d: scriptcheck %s (%s)" % (revisions, cand_score["overall"], cand_score["verdict"]), flush=True)
        if cand_score["overall"] >= best_score["overall"]:
            cand.setdefault("alt_hooks", best.get("alt_hooks"))
            best, best_file, best_score = cand, cand_file, cand_score
    _progress(92, "writing the production sheet")
    human = _detect(best_file)
    others = [(d, scores[f]) for d, f in ranked[1:]]
    sheet = _sheet(data.get("title"), a.topic, best, best_score, human, data, wpm, a.seconds, others)
    with open(os.path.join(a.out_dir, "script.md"), "w", encoding="utf-8") as f:
        f.write(sheet)
    summary = dict(best_score, angle=best.get("angle"), wpm=wpm, target_seconds=a.seconds, revisions=revisions,
                   human=human, title=data.get("title"), missing=data.get("missing") or [],
                   drafts=[{"angle": d.get("angle"), "overall": s["overall"], "verdict": s["verdict"]}
                           for d, s in [(best, best_score)] + others])
    write_json(os.path.join(a.out_dir, "score.json"), summary)
    write_json(os.path.join(a.out_dir, "script.json"), {"best": best, "story": data.get("story"),
                                                        "drafts": drafts, "missing": data.get("missing") or []})
    _progress(100, "done")
    print("wrote %s (scriptcheck %s, %s)" % (os.path.join(a.out_dir, "script.md"), best_score["overall"],
                                             best_score["verdict"]), flush=True)
    return 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["exec"] and len(argv) == 2:
        return exec_job(argv[1])
    if argv[:1] == ["script"]:
        return script_main(argv[1:])
    print("usage: python -m vstudio.jobs exec <job dir> | script --topic TEXT --out-dir DIR", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
