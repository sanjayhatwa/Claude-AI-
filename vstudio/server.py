"""
server.py - the local web studio: one page (vstudio/web) and a JSON API.

    python -m vstudio serve [--host 127.0.0.1] [--port 4320] [--workspace workspace/studio] [--open]

Everything runs on this computer: the page talks to this server, the server
starts jobs (`python -m vstudio <cmd>`, see jobs.py) and serves the files
they write. Planning and "Ask Claude" go through the user's own Claude Code
login (vstudio.llm). Standard library only, no build step, no CDN.

API (JSON in and out; errors are {"error": "..."} with a matching status):

    GET  /api/health                         app, version, workspace, job counts
    GET  /api/projects                       projects, newest first
    POST /api/projects                       {"name"} -> new project
    GET  /api/projects/<id>                  project, uploads, timeline specs, jobs, ask history
    PUT  /api/projects/<id>/upload?name=F    raw body -> uploads/F (streamed to disk, 4 GB max)
    DELETE /api/projects/<id>/upload?name=F  remove an upload
    POST /api/projects/<id>/jobs             {"kind", "args"} -> job (kinds in jobs.KINDS)
    GET  /api/projects/<id>/jobs             that project's jobs
    GET  /api/projects/<id>/spec?path=P      a timeline spec, its validation errors, versions, renders
    PUT  /api/projects/<id>/spec?path=P      save a spec [&version=new saves spec.vN.json] [&force=1]
    POST /api/projects/<id>/ask              {"message", "spec_path"} -> Claude edits the spec, saved as a new version
    GET  /api/jobs[?project=<id>]            jobs
    GET  /api/jobs/<id>                      one job with outputs and the end of its log
    GET  /api/jobs/<id>/log?offset=N         the log from byte N (for live tails)
    POST /api/jobs/<id>/cancel               cancel (stops the whole process tree)
    GET  /api/templates                      the motion-graphics catalog
    GET  /api/doctor                         doctor.check() results
    GET  /files/<project>/<path>             a project file (HTTP Range for video seeking; ?download=1)

Security model: it binds 127.0.0.1 unless told otherwise (and warns loudly
when it is). Requests whose Host header is a DNS name other than localhost
are refused (DNS rebinding); requests another website's page makes are
refused (Origin and Sec-Fetch-Site checks), and responses carry
Cross-Origin-Resource-Policy: same-origin. Upload names cannot contain
folders, and every path from a request is resolved (symlinks included) and
must stay inside its project folder. There is no login: anyone who can reach
the port can use the studio, which is why it stays on 127.0.0.1.
"""

import argparse
import email.utils
import errno
import http.server
import ipaddress
import json
import os
import re
import secrets
import shutil
import socket
import sys
import threading
import time
import traceback
import urllib.parse
import urllib.request
import webbrowser

from . import __version__, jobs, paths

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 4320
MAX_UPLOAD = 4 * 1024 ** 3
MAX_JSON = 32 * 1024 ** 2
CHUNK = 1024 * 1024
OUT_CHUNK = 256 * 1024
SPEC_FILE_MAX = 20 * 1024 ** 2

CONTENT_TYPES = {
    ".mp4": "video/mp4", ".m4v": "video/mp4", ".mov": "video/quicktime", ".webm": "video/webm",
    ".mkv": "video/x-matroska", ".avi": "video/x-msvideo",
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp", ".gif": "image/gif",
    ".bmp": "image/bmp", ".svg": "image/svg+xml",
    ".wav": "audio/wav", ".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".aac": "audio/aac", ".ogg": "audio/ogg",
    ".opus": "audio/ogg", ".flac": "audio/flac",
    ".json": "application/json; charset=utf-8", ".md": "text/markdown; charset=utf-8",
    ".srt": "application/x-subrip; charset=utf-8", ".ass": "text/x-ssa; charset=utf-8",
    ".vtt": "text/vtt; charset=utf-8", ".txt": "text/plain; charset=utf-8", ".log": "text/plain; charset=utf-8",
    ".csv": "text/csv; charset=utf-8",
    # Pages and scripts from project folders are shown as text, never run in the studio's origin.
    ".html": "text/plain; charset=utf-8", ".htm": "text/plain; charset=utf-8", ".js": "text/plain; charset=utf-8",
    ".ttf": "font/ttf", ".otf": "font/otf", ".woff2": "font/woff2", ".zip": "application/zip",
}

WEB_FILES = {"/": ("index.html", "text/html; charset=utf-8"), "/index.html": ("index.html", "text/html; charset=utf-8"),
             "/app.js": ("app.js", "text/javascript; charset=utf-8"), "/style.css": ("style.css", "text/css; charset=utf-8")}

PAGE_CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data: blob:; "
            "media-src 'self' blob:; connect-src 'self'; font-src 'self' data:; object-src 'none'; "
            "base-uri 'none'; form-action 'self'; frame-ancestors 'none'")

WINDOWS_RESERVED = {"CON", "PRN", "AUX", "NUL"} | {"COM%d" % i for i in range(1, 10)} | {"LPT%d" % i for i in range(1, 10)}
# Studio bookkeeping; compared in lower case because macOS and Windows disks ignore case (JOB.JSON is job.json).
PROTECTED_JSON = {"project.json", "job.json", "exit.json", "chat.json"}
MAX_NAME_BYTES = 200        # disks allow 255 bytes (UTF-8) per name; the rest is room for " (12)"
WINDOWS_MAX_PATH = 259      # Windows without long-path support
PARTIAL_UPLOAD_RE = re.compile(r"^\.upload-[0-9a-f]+\.part$")
PARTIAL_UPLOAD_MAX_AGE = 600    # seconds without a write before a temp upload counts as abandoned


class ApiError(Exception):
    """An error answered as JSON: {"error": message, ...extra} with an HTTP status."""

    def __init__(self, status, message, **extra):
        super().__init__(message)
        self.status, self.message, self.extra = status, message, extra


# --------------------------------------------------------------------------- helpers

def content_type(path):
    return CONTENT_TYPES.get(os.path.splitext(path)[1].lower(), "application/octet-stream")


def _big_int(digits):
    """int() of an ASCII digit string; past 18 digits just 10**18 (int() refuses more than 4300 digits)."""
    return int(digits) if len(digits) <= 18 else 10 ** 18


def parse_count(raw):
    """A non-negative whole number written in ASCII digits (Content-Length, ?limit=, ?offset=), else None.

    str.isdigit() is not enough: it accepts '²' and other digits int() refuses, and int() refuses
    more than 4300 digits.
    """
    if not isinstance(raw, str):
        return None
    raw = raw.strip()
    return int(raw) if re.fullmatch(r"[0-9]{1,18}", raw) else None


def parse_range(header, size):
    """A single 'bytes=a-b' range as (start, end) inclusive.

    Returns "ignore" for headers to ignore (malformed or several ranges:
    the whole file is sent) and None when the range cannot be satisfied (416).
    """
    m = re.match(r"^\s*bytes\s*=\s*([0-9]*)\s*-\s*([0-9]*)\s*$", header or "")
    if not m or (m.group(1) == "" and m.group(2) == ""):
        return "ignore"
    first, last = m.group(1), m.group(2)
    if first == "":                                   # suffix: the last N bytes
        n = _big_int(last)
        if n == 0 or size == 0:
            return None
        return max(0, size - n), size - 1
    start = _big_int(first)
    if start >= size:
        return None
    end = size - 1 if last == "" else _big_int(last)
    if end < start:
        return "ignore"
    return start, min(end, size - 1)


def _shorten_name(stem, ext, fits):
    """stem + ext with the stem cut (never the extension) until fits(name) holds; '' when nothing fits."""
    if fits(stem + ext):
        return stem + ext
    while stem and not fits(stem + ext):
        stem = stem[:-1]
    stem = stem.rstrip(" .")                          # a cut can end on a space or dot (Windows drops those)
    return stem + ext if stem else ""


def clean_upload_name(name):
    """A safe file name for uploads/ (ApiError 400 when the name tries to name a folder or climb out).

    Long names are shortened (the extension is kept) to 150 characters and MAX_NAME_BYTES bytes of
    UTF-8: disks count bytes, and Devanagari, CJK or emoji take 3-4 bytes a character.
    """
    if not isinstance(name, str) or not name.strip():
        raise ApiError(400, "?name= is required")
    name = name.strip()
    if "/" in name or "\\" in name or name in (".", "..") or "\x00" in name:
        raise ApiError(400, "file names cannot contain folders or '..': %r" % name[:80])
    if any(ord(c) < 32 for c in name):
        raise ApiError(400, "file names cannot contain control characters")
    name = re.sub(r'[<>:"|?*]', "_", name).rstrip(" .").lstrip(". ")
    if not name:
        raise ApiError(400, "that file name is empty once cleaned")
    stem, ext = os.path.splitext(name)
    if stem.upper().split(".")[0] in WINDOWS_RESERVED:
        stem = "_" + stem
    if len(ext) > 20 or len(ext.encode("utf-8")) > 40:
        stem, ext = stem + ext, ""                    # not a real extension: shorten it like the rest
    short = _shorten_name(stem, ext, lambda n: len(n) <= 150 and len(n.encode("utf-8")) <= MAX_NAME_BYTES)
    if not short:
        raise ApiError(400, "that file name cannot be shortened to fit; rename the file and try again")
    return short


def fit_upload_name(folder, name, max_path="auto"):
    """`name` shortened so folder/name, with room for " (99)", stays within max_path characters.

    max_path "auto" is WINDOWS_MAX_PATH on Windows (where long paths are often switched off) and no
    limit elsewhere. ApiError 400 when even a short name cannot fit.
    """
    if max_path == "auto":
        max_path = WINDOWS_MAX_PATH if os.name == "nt" else None
    if not max_path:
        return name
    room = max_path - len(os.path.join(folder, "")) - len(" (99)")
    stem, ext = os.path.splitext(name)
    short = _shorten_name(stem, ext, lambda n: len(n) <= room)
    if not short:
        raise ApiError(400, "the project folder's path is too long for Windows to add files to it (%d characters); "
                            "move the studio workspace to a shorter path such as C:\\studio" % len(folder))
    return short


def save_error(e, name):
    """An ApiError the user can act on for an OSError hit while writing an upload or a spec."""
    if e.errno in (errno.ENOSPC, getattr(errno, "EDQUOT", -1)) or getattr(e, "winerror", None) in (39, 112):
        return ApiError(507, "the disk filled up while saving %s; free some space and try again" % name)
    if e.errno == errno.ENAMETOOLONG or getattr(e, "winerror", None) == 206:
        return ApiError(400, "the name or path of %s is too long for this disk; rename it and try again" % name)
    return ApiError(500, "could not save %s: %s" % (name, e.strerror or e))


def slugify(text, n=40):
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s[:n].strip("-") or "project"


def host_allowed(host_header, extra=()):
    """True when the Host header names this machine safely: localhost, *.localhost, an IP literal, or `extra`.

    DNS rebinding needs an attacker-controlled DNS name, so any other name is refused.
    """
    if not host_header:
        return True                                   # HTTP/1.0 clients; browsers always send Host
    host = host_header.strip().lower()
    if host.startswith("["):
        hostname = host[1:host.find("]")] if "]" in host else host
    else:
        hostname = host.rsplit(":", 1)[0] if host.count(":") == 1 else host
    if hostname in ("localhost",) or hostname.endswith(".localhost") or hostname in extra:
        return True
    try:
        ipaddress.ip_address(hostname)
        return True
    except ValueError:
        return False


def _architecture_section(start, stop):
    """One section of ARCHITECTURE.md (the timeline format) for prompts; '' when the file is missing."""
    try:
        with open(os.path.join(paths.PKG, "ARCHITECTURE.md"), encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return ""
    i = text.find(start)
    j = text.find(stop, i + 1) if i >= 0 else -1
    return text[i:j if j > 0 else None].strip() if i >= 0 else ""


def _load_llm():
    """vstudio.llm, imported only when needed (it may not exist in every copy)."""
    import importlib
    try:
        return importlib.import_module("vstudio.llm")
    except ImportError:
        return None


# --------------------------------------------------------------------------- state

ASK_SYSTEM = """You are the editor inside Viral Studio, a local motion-graphics video renderer.
You change a timeline spec (JSON) exactly the way the creator asks and keep everything else identical.
Answer with JSON only."""

ASK_RULES = """Rules:
- Return the COMPLETE updated spec in "spec", not a diff. Keep every layer, caption word, effect and
  audio item you were not asked to change, with the same values and in the same order.
- Use only media files that already appear in the spec or are listed under "Project files", written
  exactly as listed (paths are relative to the spec's folder).
- Times are seconds on the timeline; end comes after start; stay inside the duration unless asked.
- 9:16 safe zone: no text in the top 11% or bottom 20% of the frame; 6% side margins. 2-6 words per
  text card. Never cover the speaker's face.
- Entrances ease out (out-cubic, out-back); exits ease in and finish before the next beat.
- Colours are "#RRGGBB" or "#RRGGBBAA".
- For a designed motion graphic (title, stat, list, CTA, lower third...) prefer "add_templates":
  [{"template": name, "params": {...}, "start": s, "end": s}]: the studio builds those layers and
  puts them on top. Hand-write layers only for small changes.
- "summary": one or two plain sentences saying what you changed."""

ASK_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "spec": {"type": "object"},
        "add_templates": {"type": "array", "items": {
            "type": "object",
            "properties": {"template": {"type": "string"}, "params": {"type": "object"},
                           "start": {"type": "number"}, "end": {"type": "number"}},
            "required": ["template", "start", "end"]}},
    },
    "required": ["summary", "spec"],
}


class Studio:
    """Projects on disk, the job runner, and the cached catalog/doctor answers the handlers share."""

    def __init__(self, workspace=None, runner=None, max_upload=MAX_UPLOAD, allowed_hosts=(), max_parallel=1,
                 builder=None, ask_timeout=600, quiet=False):
        self.workspace = os.path.abspath(workspace or paths.STUDIO)
        os.makedirs(os.path.join(self.workspace, "projects"), exist_ok=True)
        # The real path (symlinks resolved): request paths are resolved with jobs.resolve_in, which
        # follows symlinks, so the folder they are made relative to must be spelled the same way
        # (macOS /var -> /private/var, a symlinked home folder or drive, a Windows junction).
        self.projects_dir = os.path.realpath(os.path.join(self.workspace, "projects"))
        self.runner = runner or jobs.JobRunner(self.workspace, builder=builder, max_parallel=max_parallel)
        self.max_upload = int(max_upload)
        self.allowed_hosts = tuple(h.lower() for h in allowed_hosts)
        self.ask_timeout = ask_timeout
        self.host, self.port = DEFAULT_HOST, DEFAULT_PORT
        self.verbose = False
        self.quiet = quiet                 # no request log at all (tests)
        self._lock = threading.Lock()
        self._doctor = (0.0, None)
        self._templates = None
        self._inflight = set()             # temp files of uploads being received
        self.clean_partial_uploads()

    # ---- projects

    def project_dir(self, pid):
        """The project's folder as a real path (a project folder may itself be a link to another drive)."""
        if not isinstance(pid, str) or not jobs.PROJECT_ID_RE.match(pid):
            raise ApiError(404, "no such project")
        path = os.path.join(self.projects_dir, pid)
        if not os.path.isfile(os.path.join(path, "project.json")):
            raise ApiError(404, "no such project: %s" % pid)
        return os.path.realpath(path)

    def create_project(self, name):
        name = " ".join(str(name or "").split())[:80] or "Untitled project"
        with self._lock:
            while True:
                pid = "%s-%s" % (slugify(name), secrets.token_hex(2))
                path = os.path.join(self.projects_dir, pid)
                if not os.path.exists(path):
                    break
            os.makedirs(os.path.join(path, "uploads"))
            os.makedirs(os.path.join(path, "jobs"))
            t = jobs.now()
            jobs.write_json(os.path.join(path, "project.json"), {"id": pid, "name": name, "created": t, "updated": t})
        return self.project_summary(pid)

    def _meta(self, pid):
        meta = jobs.read_json(os.path.join(self.projects_dir, pid, "project.json"), {}) or {}
        meta["id"] = pid
        return meta

    def touch(self, pid):
        path = os.path.join(self.projects_dir, pid, "project.json")
        with self._lock:
            meta = jobs.read_json(path, {}) or {}
            meta["updated"] = jobs.now()
            jobs.write_json(path, meta)

    def uploads(self, pid):
        folder = os.path.join(self.projects_dir, pid, "uploads")
        out = []
        try:
            names = sorted(os.listdir(folder), key=str.lower)
        except OSError:
            return out
        for name in names:
            full = os.path.join(folder, name)
            if name.startswith(".") or not os.path.isfile(full):
                continue
            st = os.stat(full)
            out.append({"name": name, "path": "uploads/" + name, "size": st.st_size, "mtime": round(st.st_mtime, 3),
                        "type": jobs.file_type(name)})
        return out

    def project_summary(self, pid):
        meta = self._meta(pid)
        job_list = self.runner.list(pid, limit=50)
        cover = None
        for j in job_list[:8]:
            if j["status"] == "done" and j.get("primary") and jobs.file_type(j["primary"]) == "image":
                cover = j["primary"]
                break
        meta.update(uploads=len(self.uploads(pid)), jobs=len(job_list), cover=cover,
                    last_job=job_list[0] if job_list else None)
        return meta

    def list_projects(self):
        out = []
        for pid in os.listdir(self.projects_dir):
            if jobs.PROJECT_ID_RE.match(pid) and os.path.isfile(os.path.join(self.projects_dir, pid, "project.json")):
                out.append(self.project_summary(pid))
        out.sort(key=lambda p: -(p.get("updated") or p.get("created") or 0))
        return out

    def project_detail(self, pid):
        self.project_dir(pid)
        detail = self.project_summary(pid)
        detail["uploads"] = self.uploads(pid)
        detail["upload_count"] = len(detail["uploads"])
        detail["jobs"] = self.runner.list(pid)
        detail["specs"] = self.find_specs(pid)
        chat = jobs.read_json(os.path.join(self.projects_dir, pid, "chat.json"), []) or []
        detail["asks"] = chat[-100:]
        return detail

    def find_specs(self, pid):
        """Timeline specs and plans in a project: uploads/*.json and JSON under jobs/*/out (two levels deep)."""
        pdir = os.path.join(self.projects_dir, pid)
        candidates = []
        up = os.path.join(pdir, "uploads")
        if os.path.isdir(up):
            candidates += [os.path.join(up, n) for n in os.listdir(up)]
        jobs_dir = os.path.join(pdir, "jobs")
        if os.path.isdir(jobs_dir):
            for jid in os.listdir(jobs_dir):
                out = os.path.join(jobs_dir, jid, "out")
                if not os.path.isdir(out):
                    continue
                for name in os.listdir(out):
                    full = os.path.join(out, name)
                    if os.path.isdir(full) and name not in ("dna", "analysis", "drafts", "text_crops"):
                        candidates += [os.path.join(full, n) for n in os.listdir(full)]
                    else:
                        candidates.append(full)
        found = []
        for full in candidates:
            if not full.lower().endswith(".json") or os.path.basename(full).lower() in PROTECTED_JSON:
                continue
            try:
                st = os.stat(full)
                if st.st_size > SPEC_FILE_MAX:
                    continue
                with open(full, "rb") as f:
                    head = f.read(65536)
            except OSError:
                continue
            kind = "spec" if b'"layers"' in head else "plan" if b'"beats"' in head else None
            if kind:
                rel = jobs.rel_to(pdir, full)
                job = rel.split("/")[1] if rel.startswith("jobs/") else None
                found.append({"path": rel, "kind": kind, "mtime": round(st.st_mtime, 3), "size": st.st_size,
                              "job": job, "name": os.path.basename(full)})
        found.sort(key=lambda s: -s["mtime"])
        return found

    # ---- uploads

    def save_upload(self, pid, name, stream, length, overwrite=False):
        """Stream `length` bytes from `stream` into uploads/<name> in chunks. Returns the saved file's info.

        The name is checked (and shortened to fit the disk) before a byte is read; disk errors while
        writing come back as ApiErrors (507 disk full, 400 name too long), not as internal errors.
        """
        pdir = self.project_dir(pid)
        folder = os.path.join(pdir, "uploads")
        os.makedirs(folder, exist_ok=True)
        name = fit_upload_name(folder, name)
        free = shutil.disk_usage(folder).free
        if length + 200 * 1024 ** 2 > free:
            raise ApiError(507, "not enough disk space for this file (%.1f GB free)" % (free / 1e9))
        tmp = os.path.join(folder, ".upload-%s.part" % secrets.token_hex(6))
        got = 0
        self._inflight.add(tmp)
        try:
            with open(tmp, "wb") as f:
                while got < length:
                    chunk = stream.read(min(CHUNK, length - got))
                    if not chunk:
                        break
                    f.write(chunk)
                    got += len(chunk)
            if got != length:
                raise ApiError(400, "upload interrupted: got %d of %d bytes" % (got, length))
            with self._lock:
                final = os.path.join(folder, name)
                if not overwrite:
                    stem, ext = os.path.splitext(name)
                    n = 2
                    while os.path.exists(final):
                        final = os.path.join(folder, "%s (%d)%s" % (stem, n, ext))
                        n += 1
                jobs.replace_file(tmp, final)
        except (ConnectionError, TimeoutError):
            raise                                     # the browser went away: nothing to tell it
        except OSError as e:
            raise save_error(e, name) from None
        finally:
            self._inflight.discard(tmp)
            jobs.remove_file(tmp)
        self.touch(pid)
        base = os.path.basename(final)
        return {"name": base, "path": "uploads/" + base, "size": got, "type": jobs.file_type(base)}

    def clean_partial_uploads(self, max_age=PARTIAL_UPLOAD_MAX_AGE):
        """Remove upload temp files nobody has written to for max_age seconds (left by a studio that was
        stopped mid-upload: closing the window or Ctrl+C does not run the upload's own clean-up)."""
        removed = 0
        cutoff = time.time() - max_age
        try:
            pids = os.listdir(self.projects_dir)
        except OSError:
            return 0
        for pid in pids:
            folder = os.path.join(self.projects_dir, pid, "uploads")
            try:
                names = os.listdir(folder)
            except OSError:
                continue
            for name in names:
                full = os.path.join(folder, name)
                try:
                    if PARTIAL_UPLOAD_RE.match(name) and os.path.getmtime(full) < cutoff and full not in self._inflight:
                        os.remove(full)
                        removed += 1
                except OSError:
                    pass
        return removed

    def drop_inflight_uploads(self):
        """At shutdown: remove the temp files of uploads still being received (their threads die with the process)."""
        for tmp in list(self._inflight):
            jobs.remove_file(tmp, attempts=1)

    def delete_upload(self, pid, name):
        pdir = self.project_dir(pid)
        clean = clean_upload_name(name)
        if clean != name.strip():
            raise ApiError(400, "no upload named %r" % name[:80])
        full = os.path.join(pdir, "uploads", clean)
        if not os.path.isfile(full):
            raise ApiError(404, "no upload named %r" % clean)
        os.remove(full)
        self.touch(pid)
        return {"deleted": "uploads/" + clean}

    # ---- specs

    def spec_path(self, pid, rel, must_exist=True, writable=False):
        """(project folder, spec file) as real paths. With writable, the file must be somewhere specs
        are saved: uploads/ or a job's out/ folder (never next to job.json, whose commands a job runs)."""
        pdir = self.project_dir(pid)
        if not isinstance(rel, str) or not rel.lower().endswith(".json"):
            raise ApiError(400, "path must be a .json file in the project")
        try:
            full = jobs.resolve_in(pdir, rel, must_exist=False)
        except jobs.PathError as e:
            raise ApiError(403, str(e)) from None
        if os.path.basename(full).lower() in PROTECTED_JSON:
            raise ApiError(403, "%s is studio bookkeeping, not a spec" % os.path.basename(full))
        if writable:
            parts = jobs.rel_to(pdir, full).lower().split("/")
            if not ((len(parts) >= 2 and parts[0] == "uploads") or
                    (len(parts) >= 4 and parts[0] == "jobs" and parts[2] == "out")):
                raise ApiError(403, "specs are saved in uploads/ or in a job's out/ folder, not at %s" % rel)
        if must_exist and not os.path.isfile(full):
            raise ApiError(404, "no file %s in the project" % rel)
        return pdir, full

    @staticmethod
    def _family(full):
        """(folder, stem) where spec.v3.json and spec.json share stem 'spec'."""
        name = os.path.basename(full)[:-5]
        return os.path.dirname(full), re.sub(r"\.v[0-9]+$", "", name)

    def versions(self, pdir, full):
        folder, stem = self._family(full)
        out = []
        for name in os.listdir(folder):
            m = re.match(r"^%s(?:\.v([0-9]{1,9}))?\.json$" % re.escape(stem), name)
            if m:
                p = os.path.join(folder, name)
                out.append({"path": jobs.rel_to(pdir, p), "version": int(m.group(1) or 1),
                            "mtime": round(os.path.getmtime(p), 3)})
        out.sort(key=lambda v: v["version"])
        return out

    def next_version(self, full):
        folder, stem = self._family(full)
        top = 1
        for name in os.listdir(folder):
            m = re.match(r"^%s\.v([0-9]{1,9})\.json$" % re.escape(stem), name)
            if m:
                top = max(top, int(m.group(1)))
        return os.path.join(folder, "%s.v%d.json" % (stem, top + 1)), top + 1

    @staticmethod
    def validate_spec(spec, base_dir):
        """(errors, duration, validated): timeline.validate when the engine is installed."""
        try:
            from . import timeline
        except ImportError:
            return [], None, False
        errors = timeline.validate(spec, base_dir=base_dir)
        dur = None
        if not errors:
            try:
                dur = round(float(timeline.duration(timeline.normalize(spec, base_dir))), 3)
            except Exception:
                dur = None
        return errors, dur, True

    def read_spec(self, pid, rel):
        pdir, full = self.spec_path(pid, rel)
        try:
            with open(full, encoding="utf-8") as f:
                spec = json.load(f)
        except ValueError as e:
            raise ApiError(422, "%s is not valid JSON: %s" % (rel, e)) from None
        errors, dur, validated = self.validate_spec(spec, os.path.dirname(full)) if "layers" in spec else ([], None, False)
        rel_norm = jobs.rel_to(pdir, full)
        folder = os.path.dirname(full)
        media = []
        for name in sorted(os.listdir(folder)):
            if jobs.file_type(name) == "video":
                p = os.path.join(folder, name)
                media.append({"path": jobs.rel_to(pdir, p), "mtime": round(os.path.getmtime(p), 3), "job": None})
        for j in self.runner.list(pid):
            if j["kind"] == "render" and j["status"] == "done" and (j.get("args") or {}).get("spec") == rel_norm \
                    and j.get("primary"):
                media.append({"path": j["primary"], "mtime": j.get("ended") or 0, "job": j["id"],
                              "preview": bool((j.get("args") or {}).get("preview"))})
        media.sort(key=lambda m: -(m["mtime"] or 0))
        return {"path": rel_norm, "spec": spec, "errors": errors, "validated": validated, "duration": dur,
                "kind": "spec" if "layers" in spec else "plan" if "beats" in spec else "json",
                "versions": self.versions(pdir, full), "renders": media}

    def write_spec(self, pid, rel, spec, new_version=False, force=False):
        if not isinstance(spec, dict):
            raise ApiError(400, "the body must be a JSON object (the spec)")
        pdir, full = self.spec_path(pid, rel, must_exist=False, writable=True)
        if not os.path.isdir(os.path.dirname(full)):
            raise ApiError(404, "folder for %s does not exist" % rel)
        errors, dur, validated = self.validate_spec(spec, os.path.dirname(full)) if "layers" in spec else ([], None, False)
        if errors and not force:
            raise ApiError(422, "the spec has %d problem(s); fix them or save with force" % len(errors), errors=errors)
        with self._lock:
            version = None
            if new_version:
                full, version = self.next_version(full)
            try:
                jobs.write_json(full, spec)       # temp file + replace, retried while Windows holds the file
            except OSError as e:
                raise save_error(e, os.path.basename(full)) from None
        self.touch(pid)
        return {"path": jobs.rel_to(pdir, full), "saved": True, "version": version, "errors": errors,
                "validated": validated, "duration": dur}

    # ---- ask Claude

    def _project_files_for_prompt(self, pdir, spec_dir):
        lines = []
        for u in self.uploads(os.path.basename(pdir))[:200]:
            rel = os.path.relpath(os.path.join(pdir, u["path"]), spec_dir).replace(os.sep, "/")
            lines.append("- %s (%s, %d KB)" % (rel, u["type"], u["size"] // 1024))
        return "\n".join(lines) or "(none)"

    @staticmethod
    def _font_file(value):
        """True for a font value the renderer reads as a file (as timeline._looks_like_font_file decides)."""
        return isinstance(value, str) and (value.lower().endswith((".ttf", ".otf", ".ttc", ".otc"))
                                           or "/" in value or "\\" in value)

    @classmethod
    def _media_paths(cls, spec):
        """Every file a spec makes the renderer read, as (how, value) pairs.

        how is "spec" for values resolved against the spec's folder (layer and audio sources, the
        captions style DNA, layer font files) and "cwd" for caption fonts, which the font lookup
        opens as given (relative to the job's working folder).
        """
        found = set()
        if not isinstance(spec, dict):
            return found
        layers = spec.get("layers") if isinstance(spec.get("layers"), list) else []
        for L in layers:
            if not isinstance(L, dict):
                continue
            if isinstance(L.get("src"), str):
                found.add(("spec", L["src"]))
            if cls._font_file(L.get("font")):
                found.add(("spec", L["font"]))
        audio = spec.get("audio") if isinstance(spec.get("audio"), list) else []
        for a in audio:
            if isinstance(a, dict) and isinstance(a.get("src"), str):
                found.add(("spec", a["src"]))
        cap = spec.get("captions")
        if isinstance(cap, dict):
            if isinstance(cap.get("style_dna"), str):
                found.add(("spec", cap["style_dna"]))
            style = cap.get("style") if isinstance(cap.get("style"), dict) else {}
            fonts = [cap.get("font")] + (style.get("font_candidates") if isinstance(style.get("font_candidates"), list)
                                         else [])
            found.update(("cwd", f) for f in fonts if cls._font_file(f))
        return found

    def _foreign_media(self, old, new, spec_dir, pdir):
        """Problems for files the new spec reads that the old one did not and that lie outside the project.

        Claude may only use what is already in the spec or in the project folder, so an answer
        steered by text inside analysed videos cannot point the renderer at other files. Paths are
        resolved the way the renderer resolves them: '~' is the home folder, relative paths start at
        the spec's folder (caption fonts: at the job's working folder), symlinks are followed.
        """
        problems = []
        base = os.path.realpath(pdir)
        for how, src in sorted(self._media_paths(new) - self._media_paths(old)):
            # (URLs are checked as paths too: validation refuses them, and on Windows "c://x" is a drive)
            start = spec_dir if how == "spec" else paths.REPO
            full = os.path.realpath(os.path.join(start, os.path.expanduser(src)))
            try:
                inside = os.path.commonpath([base, full]) == base
            except ValueError:
                inside = False
            if not inside:
                problems.append("%s is outside the project; use only files listed under Project files" % src)
        return problems

    def _expand_templates(self, spec, calls):
        """Build add_templates calls into layers (on top). Returns a list of problems."""
        if not calls:
            return []
        try:
            from . import templates
        except ImportError:
            return ["add_templates cannot be used here (the templates module is missing); write layers instead"]
        problems = []
        canvas = {"width": spec.get("width") or 1080, "height": spec.get("height") or 1920, "fps": spec.get("fps") or 30}
        meta = spec.get("meta") if isinstance(spec.get("meta"), dict) else {}
        style = meta.get("style") if isinstance(meta.get("style"), dict) else None
        for call in calls[:20]:
            name = call.get("template") if isinstance(call, dict) else None
            if name not in templates.CATALOG:
                problems.append("add_templates: unknown template %r" % name)
                continue
            params = dict(call.get("params") or {}) if isinstance(call.get("params"), dict) else {}
            try:
                from . import planner
                # the planner's rule: a catalog default that is sample content never ships, and a template
                # that shows a number as fact needs the real number
                missing = [k for k in planner.REQUIRED_NUMBERS.get(name, []) if params.get(k) in (None, "", [])]
                if missing:
                    problems.append("add_templates: %s needs %s (sample numbers are never used)"
                                    % (name, ", ".join(missing)))
                    continue
                kinds = templates.CATALOG[name]["params"]
                for k in planner.SAMPLE_PARAMS.get(name, []):
                    if params.get(k) is None and k in kinds:
                        params[k] = [] if str(kinds[k].get("type", "")).startswith("list") else ""
            except ImportError:
                pass
            try:
                start, end = float(call.get("start")), float(call.get("end"))
                layers = templates.build(name, params, start, end, canvas, style)
            except Exception as e:
                problems.append("add_templates: %s failed: %s" % (name, e))
                continue
            spec.setdefault("layers", []).extend(layers)
        return problems

    def ask(self, pid, message, spec_rel):
        """Send the request, the spec and the template catalog to Claude; save the answer as a new version."""
        if not isinstance(message, str) or not message.strip():
            raise ApiError(400, "message is required")
        if len(message) > 4000:
            raise ApiError(400, "message is too long (4000 characters at most)")
        pdir, full = self.spec_path(pid, spec_rel, writable=True)     # the answer is saved next to it
        with open(full, encoding="utf-8") as f:
            try:
                spec = json.load(f)
            except ValueError as e:
                raise ApiError(422, "the spec is not valid JSON: %s" % e) from None
        llm = _load_llm()
        if llm is None:
            raise ApiError(503, "Ask Claude needs vstudio/llm.py (the Claude Code wrapper), which is missing from "
                                "this copy of the studio.")
        unavailable = getattr(llm, "LLMUnavailable", RuntimeError)
        try:
            ok = llm.available()
        except Exception:
            ok = False
        if not ok:
            raise ApiError(503, "Claude Code is not available. Install it (npm install -g @anthropic-ai/claude-code), "
                                "run `claude` once to log in with your subscription, then try again.")
        try:
            from . import templates
            catalog = templates.catalog_doc()
        except Exception:
            catalog = ""
        spec_dir = os.path.dirname(full)
        base = ["# What the creator wants", message.strip(), "",
                "# Current spec (%s)" % jobs.rel_to(pdir, full), "```json", json.dumps(spec, ensure_ascii=False), "```",
                "", "# Project files (paths relative to the spec's folder)", self._project_files_for_prompt(pdir, spec_dir),
                "", ASK_RULES, "", _architecture_section("## 1. TIMELINE SPEC", "## 2. PLAN")]
        if catalog:
            base += ["", catalog]
        prompt = "\n".join(base)
        new_spec, summary, errors, validated = None, "", [], False
        for _ in range(2):                            # one retry with the problems listed
            try:
                answer = llm.complete_json(prompt, schema=ASK_SCHEMA, system=ASK_SYSTEM, timeout=self.ask_timeout)
            except unavailable as e:
                raise ApiError(503, "Claude Code is not available: %s" % e) from None
            except Exception as e:
                raise ApiError(502, "Claude did not return a usable answer: %s" % e) from None
            answer = answer if isinstance(answer, dict) else {}
            new_spec = answer.get("spec")
            summary = str(answer.get("summary") or "").strip()
            if not isinstance(new_spec, dict):
                errors = ["the answer had no spec object"]
                new_spec = None
            else:
                errors = self._expand_templates(new_spec, answer.get("add_templates") or [])
                errors += self._foreign_media(spec, new_spec, spec_dir, pdir)
                v_errors, _, validated = self.validate_spec(new_spec, spec_dir)
                errors += v_errors
            if not errors:
                break
            prompt = "\n".join(base + ["", "# Your previous answer had these problems; fix every one",
                                       "\n".join("- " + e for e in errors[:40]), "",
                                       "# Your previous spec", "```json",
                                       json.dumps(new_spec, ensure_ascii=False) if new_spec else "(none)", "```"])
        record = {"t": jobs.now(), "spec_path": jobs.rel_to(pdir, full), "message": message.strip(),
                  "summary": summary, "ok": not errors}
        if errors:
            record["errors"] = errors[:20]
            self._log_ask(pid, record)
            raise ApiError(422, "Claude's change did not validate, so nothing was saved", errors=errors[:40],
                           spec=new_spec, summary=summary, saved=False)
        with self._lock:
            new_full, version = self.next_version(full)
            try:
                jobs.write_json(new_full, new_spec)
            except OSError as e:
                raise save_error(e, os.path.basename(new_full)) from None
        record["new_path"] = jobs.rel_to(pdir, new_full)
        self._log_ask(pid, record)
        self.touch(pid)
        return {"path": record["new_path"], "version": version, "summary": summary, "spec": new_spec, "errors": [],
                "saved": True, "validated": validated}

    def _log_ask(self, pid, record):
        path = os.path.join(self.projects_dir, pid, "chat.json")
        with self._lock:
            chat = jobs.read_json(path, []) or []
            chat.append(record)
            jobs.write_json(path, chat[-500:])

    # ---- catalog and doctor

    def templates(self):
        if self._templates is None:
            try:
                from . import templates
                self._templates = templates.catalog_json()
            except Exception as e:
                raise ApiError(503, "the templates catalog is not available: %s" % e) from None
        return self._templates

    def doctor(self, refresh=False):
        t, cached = self._doctor
        if cached is None or refresh or time.time() - t > 30:
            from . import doctor
            cached = doctor.check(self.workspace)
            self._doctor = (time.time(), cached)
        return cached

    def health(self):
        return {"ok": True, "app": "viral-studio", "version": __version__, "workspace": self.workspace,
                "repo": paths.REPO, "host": self.host, "port": self.port, "jobs": self.runner.counts(),
                "claude": shutil.which("claude") is not None, "max_upload": self.max_upload, "time": jobs.now()}


# --------------------------------------------------------------------------- HTTP

ROUTES = []


def route(method, pattern):
    def deco(fn):
        ROUTES.append((method, re.compile("^" + pattern + "$"), fn))
        return fn
    return deco


class Handler(http.server.BaseHTTPRequestHandler):
    """One request. Routing in ROUTES; every error is JSON."""

    server_version = "ViralStudio/" + __version__
    protocol_version = "HTTP/1.1"

    @property
    def studio(self):
        return self.server.studio

    # ---- plumbing

    def log_request(self, code="-", size="-"):
        try:
            code_int = int(code)
        except (TypeError, ValueError):
            code_int = 0
        if self.studio.quiet:
            return
        if self.studio.verbose or code_int >= 500 or (code_int >= 400 and code_int != 404):
            sys.stderr.write("  %s %s -> %s\n" % (self.command, self.path.split("?")[0][:120], code))

    def log_message(self, fmt, *args):
        if self.studio.verbose:
            sys.stderr.write("  " + (fmt % args) + "\n")

    def handle_expect_100(self):
        length = parse_count(self.headers.get("Content-Length"))
        if length is not None and length > self.studio.max_upload:
            self._body_read = True
            self.close_connection = True
            self._send_json(413, {"error": "file too large (limit %.1f GB)" % (self.studio.max_upload / 1024 ** 3)})
            return False
        return super().handle_expect_100()

    def _headers_common(self, ctype, length):
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(length))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")

    def _send_json(self, status, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self._headers_common("application/json; charset=utf-8", len(body))
        self.send_header("Cache-Control", "no-store")
        if status == 416 and isinstance(obj, dict) and "size" in obj:
            self.send_header("Content-Range", "bytes */%d" % obj["size"])
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _content_length(self):
        if "chunked" in (self.headers.get("Transfer-Encoding") or "").lower():
            self.close_connection = True
            raise ApiError(411, "send a Content-Length (chunked uploads are not supported)")
        length = parse_count(self.headers.get("Content-Length"))
        if length is None:                         # (_fail closes the connection when one was sent unreadable)
            raise ApiError(411, "a Content-Length (a whole number of bytes) is required")
        return length

    def _read_json(self):
        length = self._content_length()
        if length > MAX_JSON:
            self.close_connection = True
            raise ApiError(413, "request body too large")
        data = self.rfile.read(length) if length else b""
        self._body_read = True
        if not data.strip():
            return {}
        try:
            return json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as e:
            raise ApiError(400, "the body is not valid JSON: %s" % e) from None

    def _drain_body(self):
        """Read (or give up on) a body the route did not use, so the next request on this connection parses."""
        if self._body_read:
            return
        raw = self.headers.get("Content-Length")
        length = parse_count(raw)
        if length is None and raw is not None:
            self.close_connection = True           # a length we cannot read: do not guess where the body ends
            length = 0
        if length and length <= 1024 * 1024:
            self.rfile.read(length)
        elif length:
            self.close_connection = True
        self._body_read = True

    # ---- guards

    def _check_origin(self, method, path):
        if not host_allowed(self.headers.get("Host"), self.studio.allowed_hosts):
            raise ApiError(403, "this address is not allowed; open the studio at http://127.0.0.1:%d" % self.studio.port)
        site = (self.headers.get("Sec-Fetch-Site") or "").lower()
        mode = (self.headers.get("Sec-Fetch-Mode") or "").lower()
        if site in ("cross-site", "same-site"):
            if not (method in ("GET", "HEAD") and mode == "navigate" and path in ("/", "/index.html")):
                raise ApiError(403, "requests from other websites are not allowed")
        if method not in ("GET", "HEAD"):
            origin = self.headers.get("Origin")
            if origin is not None:
                netloc = urllib.parse.urlsplit(origin).netloc.lower() if origin != "null" else ""
                if netloc != (self.headers.get("Host") or "").strip().lower():
                    raise ApiError(403, "requests from other websites are not allowed")

    # ---- dispatch

    def do_GET(self):
        self._dispatch("GET")

    def do_HEAD(self):
        self._dispatch("HEAD")

    def do_POST(self):
        self._dispatch("POST")

    def do_PUT(self):
        self._dispatch("PUT")

    def do_DELETE(self):
        self._dispatch("DELETE")

    def do_OPTIONS(self):
        self._dispatch("OPTIONS")

    def _dispatch(self, method):
        self._body_read = False
        try:
            url = urllib.parse.urlsplit(self.path)
            path = url.path
            query = {k: v[-1] for k, v in urllib.parse.parse_qs(url.query, keep_blank_values=True).items()}
            self._check_origin(method, path)
            if path in WEB_FILES and method in ("GET", "HEAD"):
                return self._web(path)
            allowed = []
            for m, rx, fn in ROUTES:
                match = rx.match(path)
                if not match:
                    continue
                if m == method or (m == "GET" and method == "HEAD"):
                    result = fn(self, query, *match.groups())
                    if result is not None:
                        status, obj = result if isinstance(result, tuple) else (200, result)
                        self._drain_body()
                        self._send_json(status, obj)
                    return
                allowed.append(m)
            if allowed:
                raise ApiError(405, "%s is not allowed here (use %s)" % (method, ", ".join(sorted(set(allowed)))))
            raise ApiError(404, "not found: %s" % path)
        except ApiError as e:
            self._fail(e.status, dict({"error": e.message}, **e.extra))
        except jobs.JobError as e:
            self._fail(400, {"error": str(e)})
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            self.close_connection = True
        except Exception as e:
            traceback.print_exc()
            self._fail(500, {"error": "internal error: %s: %s" % (type(e).__name__, e)})

    def _fail(self, status, obj):
        """Answer an error. Never raises: it runs inside the request's exception handler."""
        try:
            if not getattr(self, "_body_read", True):
                raw = self.headers.get("Content-Length")
                if raw is not None and parse_count(raw) != 0:
                    self.close_connection = True   # the unread body must not be parsed as the next request
        except Exception:
            self.close_connection = True
        try:
            self._send_json(status, obj)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            self.close_connection = True

    # ---- static page

    def _web(self, path):
        name, ctype = WEB_FILES[path]
        full = os.path.join(paths.WEB, name)
        try:
            with open(full, "rb") as f:
                body = f.read()
        except OSError:
            raise ApiError(404, "the web app file %s is missing" % name) from None
        self.send_response(200)
        self._headers_common(ctype, len(body))
        self.send_header("Cache-Control", "no-cache")
        if name == "index.html":
            self.send_header("Content-Security-Policy", PAGE_CSP)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    # ---- files with Range

    def send_file(self, full, download=False):
        size = os.path.getsize(full)
        start, end, status = 0, size - 1, 200
        rng = self.headers.get("Range")
        if rng:
            r = parse_range(rng, size)
            if r is None:
                raise ApiError(416, "range not satisfiable", size=size)
            if r != "ignore":
                (start, end), status = r, 206
        length = max(0, end - start + 1)
        self.send_response(status)
        self._headers_common(content_type(full), length)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Last-Modified", email.utils.formatdate(os.path.getmtime(full), usegmt=True))
        self.send_header("Content-Security-Policy", "sandbox; default-src 'none'; img-src 'self'; media-src 'self'")
        if status == 206:
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
        if download:
            quoted = urllib.parse.quote(os.path.basename(full))
            self.send_header("Content-Disposition", "attachment; filename*=UTF-8''%s" % quoted)
        self.end_headers()
        if self.command == "HEAD":
            return
        with open(full, "rb") as f:
            f.seek(start)
            left = length
            while left > 0:
                chunk = f.read(min(OUT_CHUNK, left))
                if not chunk:
                    break
                self.wfile.write(chunk)
                left -= len(chunk)


# --------------------------------------------------------------------------- routes

@route("GET", r"/api/health")
def r_health(h, q):
    return h.studio.health()


@route("GET", r"/api/projects")
def r_projects(h, q):
    return h.studio.list_projects()


@route("POST", r"/api/projects")
def r_project_create(h, q):
    body = h._read_json()
    if not isinstance(body, dict):
        raise ApiError(400, "send {\"name\": \"...\"}")
    return 201, h.studio.create_project(body.get("name"))


@route("GET", r"/api/projects/([^/]+)")
def r_project(h, q, pid):
    return h.studio.project_detail(pid)


@route("PUT", r"/api/projects/([^/]+)/upload")
def r_upload(h, q, pid):
    h.studio.project_dir(pid)
    name = clean_upload_name(q.get("name"))
    length = h._content_length()
    if length > h.studio.max_upload:
        h.close_connection = True
        raise ApiError(413, "file too large (limit %.1f GB)" % (h.studio.max_upload / 1024 ** 3))
    info = h.studio.save_upload(pid, name, h.rfile, length, overwrite=q.get("overwrite") in ("1", "true"))
    h._body_read = True
    return 201, info


@route("DELETE", r"/api/projects/([^/]+)/upload")
def r_upload_delete(h, q, pid):
    return h.studio.delete_upload(pid, q.get("name") or "")


@route("POST", r"/api/projects/([^/]+)/jobs")
def r_job_create(h, q, pid):
    h.studio.project_dir(pid)
    body = h._read_json()
    if not isinstance(body, dict) or not isinstance(body.get("kind"), str):
        raise ApiError(400, "send {\"kind\": \"reel\", \"args\": {...}}")
    job = h.studio.runner.submit(pid, body["kind"], body.get("args") or {})
    h.studio.touch(pid)
    return 201, job


@route("GET", r"/api/projects/([^/]+)/jobs")
def r_project_jobs(h, q, pid):
    h.studio.project_dir(pid)
    return h.studio.runner.list(pid)


@route("GET", r"/api/projects/([^/]+)/spec")
def r_spec_get(h, q, pid):
    return h.studio.read_spec(pid, q.get("path"))


@route("PUT", r"/api/projects/([^/]+)/spec")
def r_spec_put(h, q, pid):
    body = h._read_json()
    if isinstance(body, dict) and "spec" in body and "layers" not in body and "beats" not in body:
        body = body["spec"]
    return h.studio.write_spec(pid, q.get("path"), body, new_version=q.get("version") in ("new", "1", "true"),
                               force=q.get("force") in ("1", "true"))


@route("POST", r"/api/projects/([^/]+)/ask")
def r_ask(h, q, pid):
    body = h._read_json()
    if not isinstance(body, dict):
        raise ApiError(400, "send {\"message\": \"...\", \"spec_path\": \"...\"}")
    return h.studio.ask(pid, body.get("message"), body.get("spec_path"))


@route("GET", r"/api/jobs")
def r_jobs(h, q):
    pid = q.get("project") or None
    if pid:
        h.studio.project_dir(pid)
    limit = parse_count(q.get("limit"))
    return h.studio.runner.list(pid, limit=200 if limit is None else limit)


def _job_or_404(h, jid):
    job = h.studio.runner.get(jid) if jobs.JOB_ID_RE.match(jid) else None
    if job is None:
        raise ApiError(404, "no such job")
    return job


@route("GET", r"/api/jobs/([^/]+)")
def r_job(h, q, jid):
    job = _job_or_404(h, jid)
    text, offset, size = h.studio.runner.log(jid, 0, max_bytes=12288)
    job["log_tail"], job["log_size"] = text, size
    return job


@route("GET", r"/api/jobs/([^/]+)/log")
def r_job_log(h, q, jid):
    _job_or_404(h, jid)
    text, nxt, size = h.studio.runner.log(jid, parse_count(q.get("offset")) or 0)
    return {"text": text, "offset": nxt, "size": size}


@route("POST", r"/api/jobs/([^/]+)/cancel")
def r_job_cancel(h, q, jid):
    _job_or_404(h, jid)
    return h.studio.runner.cancel(jid)


@route("GET", r"/api/templates")
def r_templates(h, q):
    return h.studio.templates()


@route("GET", r"/api/doctor")
def r_doctor(h, q):
    return h.studio.doctor(refresh=q.get("refresh") in ("1", "true"))


@route("GET", r"/files/([^/]+)/(.+)")
def r_files(h, q, pid, rest):
    pdir = h.studio.project_dir(pid)
    rel = urllib.parse.unquote(rest)
    try:
        full = jobs.resolve_in(pdir, rel, must_exist=False)
    except jobs.PathError:
        raise ApiError(403, "that path is outside the project") from None
    if not os.path.isfile(full):
        raise ApiError(404, "no file %s" % rel)
    h.send_file(full, download=q.get("download") in ("1", "true"))
    return None


# --------------------------------------------------------------------------- server

class StudioServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    # On Windows SO_REUSEADDR lets a second server take a port that is in use; only reuse elsewhere.
    allow_reuse_address = os.name != "nt"

    def __init__(self, address, studio):
        if ":" in address[0]:
            self.address_family = socket.AF_INET6
        self.studio = studio
        super().__init__(address, Handler)


def is_loopback(host):
    if host in ("localhost",):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def make_server(host=DEFAULT_HOST, port=DEFAULT_PORT, workspace=None, runner=None, studio=None, **studio_kwargs):
    """A ready-to-run server (port 0 picks a free port); call serve_forever() on it."""
    extra = () if is_loopback(host) or host in ("0.0.0.0", "::") else (host.lower(),)
    studio = studio or Studio(workspace, runner=runner, allowed_hosts=extra, **studio_kwargs)
    httpd = StudioServer((host, port), studio)
    studio.host, studio.port = host, httpd.server_address[1]
    return httpd


def _running_studio(host, port):
    """Health info when a studio already answers on this port, else None."""
    probe = "127.0.0.1" if host in ("0.0.0.0", "::", "") else host
    url = "http://%s:%d/api/health" % ("[%s]" % probe if ":" in probe else probe, port)
    try:
        with urllib.request.urlopen(url, timeout=2) as r:
            data = json.loads(r.read().decode("utf-8"))
            return data if data.get("app") == "viral-studio" else None
    except Exception:
        return None


def _warn_exposed(host):
    bar = "!" * 76
    sys.stderr.write("\n%s\n  WARNING: listening on %s, not only on this computer.\n"
                     "  Anyone who can reach this address can start jobs and read every project file.\n"
                     "  There is no login. Use the default (127.0.0.1) unless you know you need this.\n%s\n\n"
                     % (bar, host, bar))


def main(argv=None):
    ap = argparse.ArgumentParser(prog="vstudio serve", description="open the local web studio")
    ap.add_argument("--host", default=DEFAULT_HOST, help="address to listen on (default 127.0.0.1: this computer only)")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT, help="port (default 4320; 0 = any free port)")
    ap.add_argument("--workspace", default=paths.STUDIO, help="studio folder (default workspace/studio)")
    ap.add_argument("--open", action="store_true", help="open the studio in the browser")
    ap.add_argument("--parallel", type=int, default=1, help="renders that may run at the same time (default 1)")
    ap.add_argument("--max-upload-gb", type=float, default=4.0, help="largest upload in GB (default 4)")
    ap.add_argument("--verbose", action="store_true", help="log every request")
    a = ap.parse_args(argv)
    if not is_loopback(a.host):
        _warn_exposed(a.host)
    httpd = None
    for port in [a.port] + ([a.port + i for i in range(1, 11)] if a.port else []):
        try:
            httpd = make_server(a.host, port, a.workspace, max_upload=int(a.max_upload_gb * 1024 ** 3),
                                max_parallel=a.parallel)
            break
        except OSError as e:
            running = _running_studio(a.host, port)
            if running:
                url = "http://127.0.0.1:%d" % port
                print("Viral Studio is already running at %s (workspace %s)" % (url, running.get("workspace")))
                if a.open:
                    webbrowser.open(url)
                return 0
            print("port %d is busy (%s); trying the next one" % (port, e.strerror or e), file=sys.stderr)
    if httpd is None:
        print("no free port found near %d; pass --port" % a.port, file=sys.stderr)
        return 1
    httpd.studio.verbose = a.verbose
    shown = "127.0.0.1" if a.host in ("0.0.0.0", "::") else a.host
    url = "http://%s:%d" % ("[%s]" % shown if ":" in shown else shown, httpd.server_address[1])
    print("Viral Studio %s\n  open       %s\n  projects   %s\n  Ctrl+C to stop." % (__version__, url, httpd.studio.workspace))
    if a.open:
        threading.Timer(0.6, webbrowser.open, args=(url,)).start()
    try:
        httpd.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        print("\nstopping")
    finally:
        httpd.server_close()
        httpd.studio.drop_inflight_uploads()     # half-received files; the rest is cleaned at the next start
        left = httpd.studio.runner.close()
        if left:
            print("%d job(s) still running; they keep going and show up when the studio starts again." % left)
    return 0


if __name__ == "__main__":
    sys.exit(main())
