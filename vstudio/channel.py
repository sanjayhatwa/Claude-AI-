"""
channel.py - the full DNA of a YouTube channel (and public Instagram / TikTok
profiles): what it posts, what works, why, and how to compete with it.

    python -m vstudio channel https://www.youtube.com/@handle --out-dir workspace/channels/handle
    python -m vstudio channel @one @two https://www.tiktok.com/@three --deep 2
    python -m vstudio channel @handle --no-download        # metadata + thumbnails, no video downloads

Per channel, in order:

  1 inventory    long-form (/videos), Shorts (/shorts) and lives (/streams) as
                 flat lists through yt-dlp, plus the follower count
  2 metadata     upload date and hour, duration, views, likes, comments, tags,
                 categories, description, chapters and thumbnail for the newest
                 N videos of each format plus that format's top performers
  3 performance  median views per format (the baseline), outlier multiple per
                 video (views / its format's median), views per day,
                 engagement, best and worst, the last 10 against the 10 before
  4 cadence      uploads per week per format, weekdays and hours (UTC)
  5 titles       length, caps, numbers, questions, brackets, emoji, person,
                 the format (trend radar) and the hook formula (yt-script), and
                 which patterns over-index among the outliers
  6 topics       every video tagged with 1-3 topics: TF-IDF keyword topics
                 from title + tags + description, merged when they cover the
                 same videos; per topic: count, median multiple, trend
  7 thumbnails   thumbs.analyze_thumb on long-form thumbnails (text, palette,
                 light, busyness, faces, composition), the traits that
                 over-index among outliers, and thumbs_sheet.jpg
  8 deep dive    the top N Shorts and the first 90 s of the top N long-form
                 videos through viral-extract: caption style, dialogue DNA
                 (pace, word spacing, sentence rhythm, hook, re-hooks) and edit
                 DNA (cuts, shot length); an ENERGY score 0-100 and the
                 content type (talking head, voice-over + b-roll, ...)

Writes into --out-dir: CHANNEL_DNA.json (everything), CHANNEL_DNA.md (the
read), thumbs_sheet.jpg (top outliers; one per channel as
thumbs_sheet_<slug>.jpg when several are compared), thumbs/ and
dna/<video_id>/ (each deep-dived video's Style DNA, captions.py reads its
style_dna.json).

Read, do not scrape: flat lists, one metadata lookup per selected video and a
pause of about a second between calls (longer after a rate limit), with no
login and no cookies. Instagram and most TikTok profiles need a logged-in
browser: the report says "not public / needs your browser" and moves on; this
never asks for a password. For TikTok, a link to one of the account's videos
often works where the profile page does not (the account is listed by its id).

Time: the inventory and metadata take about 2 s per looked-up video (the
polite pause included), thumbnails well under a second each, and each deep
dive about a minute (download, cut and beat analysis, OCR every 0.3-0.5 s).

Limits, said plainly: public numbers only (no CTR, retention or revenue);
views of the newest uploads are still growing; topic tags come from words, so
a channel that titles everything vaguely gets vague topics; the content type
and energy score are heuristics with their evidence printed next to them; and
nothing here can say WHY a video travelled, only what the winners share.
"""

import argparse
import collections
import contextlib
import datetime as dt
import importlib.util
import io
import json
import math
import os
import re
import shutil
import statistics
import subprocess
import sys
import time
import urllib.request

from . import paths

FORMATS = (("long", "videos"), ("short", "shorts"), ("live", "streams"))
FORMAT_NAMES = {"long": "long-form", "short": "Shorts", "live": "lives"}
PLATFORM_NAMES = {"youtube": "YouTube", "instagram": "Instagram", "tiktok": "TikTok", "other": "Profile"}
HOOK_SECONDS = 90            # long-form deep dives read the opening only
SHORT_CLIP_SECONDS = 180     # a Short is read whole
OCR_FPS = {"short": 3.0, "long": 2.0, "live": 2.0}
FRAME_FPS = 2.0              # frames per second sampled for the content-type read
PAUSE = 1.0                  # seconds between yt-dlp calls
THUMB_PAUSE = 0.25           # thumbnail images come from a static CDN
MIN_BASELINE = 4             # fewer videos than this in a format = no median, no multiples
OUTLIER = 2.0                # views / format median that counts as an outlier
THUMB_CAP = 60
SHEET_TILES = 12
NEEDS_BROWSER = "not public / needs your browser"
UA = "Mozilla/5.0 (viral-studio channel analysis; low volume)"
WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

# words that say nothing about a topic (on top of trend radar's stop list)
TOPIC_STOP = set("""
subscribe subscribed channel follow instagram twitter tiktok facebook patreon discord merch link links
http https www com watch video videos episode official full today vlog shorts short reel reels like comment
share please check thank thanks want know going really also much every time way thing things need use
used using here there now let lets let's don't can't won't didn't isn't it's that's here's what's i'm i've
you're you've we're they're doesn't aren't wasn't shouldn't couldn't wouldn't gonna wanna gotta yeah okay
sure ever even still back look looks looking try tried trying see seen good great better best really actually
literally basically link bio below above sponsor sponsored affiliate code discount amazon available click
live livestream stream streaming premiere q&a
""".split())
EMOJI_RE = re.compile("[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\uFE0F]")
WORD_RE = re.compile(r"[A-Za-z0-9$%'\u2019]+")


def log(msg):
    print(msg, file=sys.stderr, flush=True)


# --------------------------------------------------------------------------- skills (reused, never copied)

def _skill(name):
    """A skill script as a module (see paths.load_skill), or None when it cannot be loaded."""
    try:
        return paths.load_skill(name)
    except Exception as e:  # a broken skill script costs one section, not the run
        log("  skill %s unavailable: %s" % (name, str(e)[:160]))
        return None


def _yt_hooks():
    """yt-script/hookscore.py: YouTube hook formulas (hooks.json) and the spoken-hook score."""
    # registered here because paths.py has no entry for it yet (requested from its owner)
    paths._SKILL_FILES.setdefault("ythook", ("yt-script", "hookscore.py"))
    return _skill("ythook")


# --------------------------------------------------------------------------- network layer

def _is_rate_limit(msg):
    return bool(re.search(r"\b429\b|too many requests|rate.?limit", msg or "", re.I))


def _is_missing_tab(msg):
    return bool(re.search(r"does not have an? \w+ tab|no (?:videos|shorts|streams)|playlist does not exist",
                          msg or "", re.I))


def classify_error(msg, platform=None):
    """(status, note) for a yt-dlp failure message: needs_browser, rate_limited, not_found,
    missing_tool, unsupported or unreachable."""
    m = (msg or "").lower()
    if "no module named yt_dlp" in m or "yt-dlp is not installed" in m:
        return "missing_tool", "yt-dlp is not installed: python -m pip install yt-dlp"
    if re.search(r"unable to connect|tunnel connection failed|connection (?:refused|reset|aborted)|timed? ?out|"
                 r"name or service not known|getaddrinfo|network is unreachable|no route to host|"
                 r"temporary failure in name resolution|ssl: |certificate verify", m):
        return "unreachable", "not reachable from this machine (network)"
    if re.search(r"log ?in|sign ?in|cookies|private|authenticat|not available|confirm (?:your age|you)|"
                 r"empty media response|unable to extract (?:secondary )?user ?id", m):
        return "needs_browser", NEEDS_BROWSER
    if _is_rate_limit(m):
        return ("needs_browser", NEEDS_BROWSER) if platform in ("instagram", "tiktok") else \
            ("rate_limited", "rate limited: wait an hour and run again")
    if re.search(r"\b404\b|does not exist|not found|unavailable", m):
        return "not_found", "not found (check the link)"
    if "unsupported url" in m:
        return "unsupported", "yt-dlp does not support this link"
    if platform in ("instagram", "tiktok"):
        # these sites refuse logged-out readers in many ways; the honest summary is the same
        return "needs_browser", NEEDS_BROWSER
    return "unreachable", "not reachable from this machine"


class YtDlpFetch:
    """The real network layer (the `fetch` object analyze() takes; tests pass a fake instead).

    list_channel(url, tab, limit) -> yt-dlp flat playlist dict or None
    video_meta(url)               -> yt-dlp info dict or None
    download_thumb(url, path)     -> saved JPEG path or None (maxresdefault falls back to hqdefault)
    download_clip(url, path, s)   -> video path (first s seconds) or None
    captions(url, dir)            -> json3 captions path or None
    last_error                    -> the last failure message (None after a success)

    At least PAUSE seconds between yt-dlp calls; on a rate limit it backs off and retries twice.
    """

    online = True

    def __init__(self, pause=PAUSE):
        self.pause = pause
        self.last_error = None
        self._last = 0.0

    @staticmethod
    def available():
        return importlib.util.find_spec("yt_dlp") is not None

    def _wait(self, pause=None):
        gap = (self.pause if pause is None else pause) - (time.time() - self._last)
        if gap > 0:
            time.sleep(gap)
        self._last = time.time()

    def _json(self, args, timeout):
        trends = paths.load_skill("trends")
        for attempt in range(3):
            self._wait()
            buf = io.StringIO()
            try:
                with contextlib.redirect_stderr(buf):   # keep yt-dlp's reason instead of printing it
                    data = trends.ytdlp(args, timeout=timeout)
            except SystemExit as e:
                self.last_error = str(e)
                return None
            if data is not None:
                self.last_error = None
                return data
            self.last_error = buf.getvalue().replace("yt-dlp:", "").strip() or "no answer (timed out or empty)"
            if _is_rate_limit(self.last_error) and attempt < 2:
                log("  rate limited; waiting %d s" % (20 * (attempt + 1)))
                time.sleep(20 * (attempt + 1))
                continue
            return None
        return None

    def list_channel(self, url, tab, limit):
        target = url.rstrip("/") + "/" + tab if tab else url
        return self._json(["--flat-playlist", "-J", "--playlist-end", str(int(limit)), target], timeout=300)

    def video_meta(self, url):
        return self._json(["-j", "--skip-download", "--no-playlist", url], timeout=120)

    def download_thumb(self, url, path):
        urls = [url]
        if "/maxresdefault." in url:
            urls.append(re.sub(r"/maxresdefault\.\w+", "/hqdefault.jpg", url))
        for u in urls:
            self._wait(THUMB_PAUSE)
            try:
                req = urllib.request.Request(u, headers={"User-Agent": UA})
                with urllib.request.urlopen(req, timeout=20) as r:
                    raw = r.read()
                from PIL import Image
                im = Image.open(io.BytesIO(raw)).convert("RGB")
                if im.width <= 120:   # YouTube's grey "no thumbnail" placeholder
                    continue
                os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
                im.save(path, "JPEG", quality=92)
                self.last_error = None
                return path
            except Exception as e:  # 404 for a missing maxres, network errors: try the next size
                self.last_error = "thumbnail %s: %s" % (u, str(e)[:120])
        return None

    def _run(self, args, timeout):
        self._wait()
        try:
            r = subprocess.run([sys.executable, "-m", "yt_dlp", "--quiet", "--no-warnings"] + args,
                               capture_output=True, text=True, timeout=timeout, encoding="utf-8", errors="replace")
        except subprocess.TimeoutExpired:
            self.last_error = "timed out after %d s" % timeout
            return False
        if r.returncode != 0:
            self.last_error = ((r.stderr or "").strip().splitlines() or ["yt-dlp failed"])[-1][:300]
            return False
        return True

    def download_clip(self, url, path, seconds):
        folder = os.path.dirname(os.path.abspath(path))
        stem = os.path.splitext(os.path.basename(path))[0]
        os.makedirs(folder, exist_ok=True)
        args = ["--no-playlist", "-f", "bv*[height<=720]+ba/b[height<=720]/b", "--merge-output-format", "mp4",
                "-o", os.path.join(folder, stem + ".%(ext)s")]
        if seconds:
            args += ["--download-sections", "*0-%d" % int(seconds)]
        self._run(args + [url], timeout=900)
        made = sorted(f for f in os.listdir(folder) if f.startswith(stem + ".") and not f.endswith((".part", ".ytdl")))
        if not made:
            return None
        self.last_error = None
        return os.path.join(folder, "%s.mp4" % stem if "%s.mp4" % stem in made else made[0])

    def captions(self, url, dir):
        os.makedirs(dir, exist_ok=True)
        self._run(["--skip-download", "--write-subs", "--write-auto-subs", "--sub-langs", "en.*,en,.*-orig",
                   "--sub-format", "json3", "-o", os.path.join(dir, "subs"), url], timeout=180)
        files = sorted(f for f in os.listdir(dir) if f.startswith("subs") and f.endswith(".json3"))
        if not files:
            return None
        best = [f for f in files if f == "subs.en.json3"] or [f for f in files if ".en" in f] or files
        return os.path.join(dir, best[0])


# --------------------------------------------------------------------------- urls

YT_VIDEO_RE = re.compile(r"(?:youtube\.com/(?:watch\?(?:.*&)?v=|shorts/|live/|embed/)|youtu\.be/)([A-Za-z0-9_-]{11})")
YT_BASE_RE = re.compile(r"(https?://www\.youtube\.com/(?:@[^/?#]+|channel/[^/?#]+|c/[^/?#]+|user/[^/?#]+))", re.I)


def normalize_url(u):
    """Tidy a user-typed link: "@handle" and bare handles become YouTube channel URLs, scheme added,
    mobile hosts mapped to www."""
    u = (u or "").strip()
    if u.startswith("@"):
        return "https://www.youtube.com/" + u
    if not re.match(r"https?://", u, re.I):
        if re.match(r"^[A-Za-z0-9._-]{3,}$", u) and "." not in u:
            return "https://www.youtube.com/@" + u
        u = "https://" + u
    u = re.sub(r"^http://", "https://", u, flags=re.I)
    u = re.sub(r"://(?:m\.|music\.)?youtube\.com", "://www.youtube.com", u, flags=re.I)
    u = re.sub(r"://(?:m\.)?instagram\.com", "://www.instagram.com", u, flags=re.I)
    u = re.sub(r"://(?:m\.)?tiktok\.com", "://www.tiktok.com", u, flags=re.I)   # vm.tiktok.com short links stay
    return u


def platform_of(url):
    """youtube / instagram / tiktok / other."""
    host = re.sub(r"^https?://", "", url or "", flags=re.I).split("/")[0].lower()
    if host.endswith("youtube.com") or host.endswith("youtu.be"):
        return "youtube"
    if host.endswith("instagram.com"):
        return "instagram"
    if host.endswith("tiktok.com"):
        return "tiktok"
    return "other"


def youtube_channel_base(url):
    """https://www.youtube.com/@handle (or /channel/<id>, /c/<name>, /user/<name>) without a tab; None for others."""
    m = YT_BASE_RE.match(url or "")
    return m.group(1) if m else None


def slug(text):
    s = re.sub(r"[^A-Za-z0-9]+", "-", (text or "").replace("@", "")).strip("-").lower()
    return s[:48] or "channel"


def _safe_id(vid):
    return re.sub(r"[^A-Za-z0-9_-]", "_", str(vid))[:64] or "video"


# --------------------------------------------------------------------------- small helpers

def _median(xs):
    xs = [x for x in xs if isinstance(x, (int, float))]
    return statistics.median(xs) if xs else None


def _r(v, nd=1):
    return None if v is None else round(v, nd)


def _n(v):
    """1234567 -> 1.2M."""
    if v is None:
        return "n/a"
    v = float(v)
    for div, suf in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(v) >= div:
            s = "%.1f" % (v / div)
            return (s[:-2] if s.endswith(".0") else s) + suf
    return str(int(round(v)))


def _g(v, nd=1):
    """A number without a trailing .0 ("n/a" for None)."""
    return "n/a" if v is None else ("%%.%df" % nd % v).rstrip("0").rstrip(".") if isinstance(v, float) else str(v)


def _x(m):
    return "n/a" if m is None else "%.1fx" % m


def _pct(v):
    return "n/a" if v is None else "%d%%" % round(100 * v)


def _cell(text, n=70):
    t = re.sub(r"\s+", " ", str(text or "")).replace("|", "/").strip()
    return t if len(t) <= n else t[:n - 1].rstrip() + "..."


def _date(upload_date):
    try:
        return dt.datetime.strptime(str(upload_date), "%Y%m%d").date()
    except (TypeError, ValueError):
        return None


def _fn(ch, fmt):
    """Display name of a format on this channel's platform (TikTok and Instagram have no "Shorts")."""
    if fmt == "short" and ch.get("platform") in ("tiktok", "instagram", "other"):
        return {"tiktok": "TikToks", "instagram": "Reels", "other": "videos"}[ch["platform"]]
    return FORMAT_NAMES[fmt]


def _cap(text):
    return text[:1].upper() + text[1:]


def _handle_from_url(url):
    """"@name" from a profile or channel URL (its last path part), or the host."""
    parts = [p for p in re.sub(r"^https?://", "", url or "").split("?")[0].split("/") if p]
    tail = parts[-1] if len(parts) > 1 else (parts[0] if parts else "")
    if tail in ("videos", "shorts", "streams", "featured", "reels") and len(parts) > 2:
        tail = parts[-2]
    return tail if tail.startswith("@") or len(parts) < 2 else "@" + tail


def _today():
    return dt.date.today()


def _top(values, n):
    """[{"name", "count"}] for the n most common non-empty values."""
    return [{"name": k, "count": c} for k, c in collections.Counter(v for v in values if v).most_common(n)]


def _counts(items, sep=", "):
    return sep.join("%s (%d)" % (x["name"], x["count"]) for x in items) or "n/a"


# --------------------------------------------------------------------------- 1. inventory

def _entries(data):
    """Flat-playlist entries, one nested level of playlists flattened (some tabs list sub-playlists)."""
    out = []
    for e in (data or {}).get("entries") or []:
        if not isinstance(e, dict):
            continue
        if e.get("_type") == "playlist" and e.get("entries"):
            out += [x for x in e["entries"] if isinstance(x, dict)]
        else:
            out.append(e)
    return out


def _record(e, fmt, idx, n, platform):
    vid = str(e.get("id") or "")
    url = e.get("url") if str(e.get("url") or "").startswith("http") else e.get("webpage_url")
    if platform == "youtube":
        url = ("https://www.youtube.com/shorts/%s" if fmt == "short" else "https://www.youtube.com/watch?v=%s") % vid
    views = e.get("view_count")
    rec = {
        "id": vid, "platform": platform, "format": fmt, "url": url,
        "title": (e.get("title") or e.get("description") or "").strip()[:300],
        "views": int(views) if isinstance(views, (int, float)) else None,
        "duration": e.get("duration"),
        "rank": idx, "recency": round(idx / float(max(1, n - 1)), 3),
        "likes": e.get("like_count"), "comments": e.get("comment_count"),
        "timestamp": e.get("timestamp"), "upload_date": e.get("upload_date"),
        "meta": False, "sample": None,
    }
    if not rec["upload_date"] and rec["timestamp"]:
        rec["upload_date"] = dt.datetime.fromtimestamp(rec["timestamp"], dt.timezone.utc).strftime("%Y%m%d")
    thumbs = e.get("thumbnails") or []
    rec["thumbnail"] = e.get("thumbnail") or (thumbs[-1].get("url") if thumbs and isinstance(thumbs[-1], dict) else None)
    if platform != "youtube" and e.get("description") and e.get("title") and e["title"] != e["description"]:
        rec["description"] = e["description"][:1000]
    return rec


def _merge_info(ch, data):
    title = re.sub(r"\s+-\s+(Videos|Shorts|Live|Streams)$", "", data.get("title") or "")
    ch["name"] = ch.get("name") or data.get("channel") or data.get("uploader") or title or None
    ch["channel_id"] = ch.get("channel_id") or data.get("channel_id") or data.get("uploader_id")
    ch["handle"] = ch.get("handle") or data.get("uploader_id")
    ch["url"] = ch.get("url") or data.get("channel_url") or data.get("uploader_url")
    if ch.get("followers") is None and isinstance(data.get("channel_follower_count"), int):
        ch["followers"] = data["channel_follower_count"]
    ch["description"] = ch.get("description") or (data.get("description") or "")[:600] or None
    ch["keywords"] = ch.get("keywords") or (data.get("tags") or [])[:20]


def inventory(ch, base, tabs, fetch, limit):
    """Flat-list every tab; returns the video records (newest first within each format)."""
    recs, seen = [], set()
    # a video listed under two tabs counts once, as the most specific format
    for fmt, tab in sorted(tabs, key=lambda t: {"live": 0, "short": 1, "long": 2}[t[0]]):
        data = fetch.list_channel(base, tab, limit)
        if not data:
            err = getattr(fetch, "last_error", None) or "no answer"
            if tab and _is_missing_tab(err):
                ch["inventory"][fmt] = {"listed": 0, "capped": False}
            else:
                ch["inventory"][fmt] = {"listed": 0, "capped": False, "error": err[:300]}
            continue
        _merge_info(ch, data)
        entries = _entries(data)
        kept = 0
        for i, e in enumerate(entries):
            vid = str(e.get("id") or "")
            if not vid or vid in seen or e.get("live_status") in ("is_upcoming", "is_live"):
                continue
            seen.add(vid)
            recs.append(_record(e, fmt, i, len(entries), ch["platform"]))
            kept += 1
        ch["inventory"][fmt] = {"listed": kept, "capped": len(entries) >= limit}
    return recs


# --------------------------------------------------------------------------- 2. metadata

def _clean_desc(text):
    t = re.sub(r"https?://\S+", " ", text or "")
    return re.sub(r"[ \t]+", " ", t).strip()


def apply_meta(r, m, ch):
    """Merge one yt-dlp info dict into a record."""
    r["meta"] = True
    r["title"] = (m.get("title") or r["title"] or "").strip()[:300]
    if isinstance(m.get("view_count"), (int, float)):
        r["views"] = int(m["view_count"])
    for src, dst in (("like_count", "likes"), ("comment_count", "comments"), ("duration", "duration"),
                     ("language", "language"), ("live_status", "live_status")):
        if m.get(src) is not None:
            r[dst] = m[src]
    r["timestamp"] = m.get("timestamp") or m.get("release_timestamp") or r.get("timestamp")
    r["upload_date"] = m.get("upload_date") or r.get("upload_date")
    if not r["upload_date"] and r["timestamp"]:
        r["upload_date"] = dt.datetime.fromtimestamp(r["timestamp"], dt.timezone.utc).strftime("%Y%m%d")
    r["tags"] = [str(t) for t in (m.get("tags") or [])][:30]
    r["categories"] = m.get("categories") or []
    r["description"] = _clean_desc(m.get("description"))[:1000]
    r["chapters"] = [{"title": c.get("title"), "start": _r(c.get("start_time"), 1)}
                     for c in (m.get("chapters") or []) if isinstance(c, dict)]
    r["thumbnail"] = m.get("thumbnail") or r.get("thumbnail")
    if m.get("width") and m.get("height"):
        r["vertical"] = m["height"] > m["width"]
    if ch.get("followers") is None and isinstance(m.get("channel_follower_count"), int):
        ch["followers"] = m["channel_follower_count"]
    ch["name"] = ch.get("name") or m.get("channel") or m.get("uploader")


def metadata(ch, recs, fetch, max_videos):
    """Full metadata for the newest `max_videos` of each format plus that format's top performers.

    Profiles whose flat list already carries views and dates (TikTok usually does) need no lookups.
    """
    top_n = max(3, max_videos // 3)
    todo = []
    for fmt in ("long", "short", "live"):
        fr = sorted((r for r in recs if r["format"] == fmt), key=lambda r: r["rank"])
        for r in fr[:max_videos]:
            r["sample"] = "recent"
            todo.append(r)
        older = sorted((r for r in fr[max_videos:] if r.get("views") is not None), key=lambda r: -r["views"])
        for r in older[:top_n]:
            r["sample"] = "top"
            todo.append(r)
    todo = [r for r in todo if r["platform"] == "youtube" or r.get("views") is None or not r.get("upload_date")]
    fails = done = 0
    for i, r in enumerate(todo, 1):
        if i == 1 or i % 10 == 0:
            log("  metadata %d/%d" % (i, len(todo)))
        m = fetch.video_meta(r["url"])
        if not m:
            fails += 1
            if fails >= 5:
                err = getattr(fetch, "last_error", None) or "no answer"
                ch["gaps"].append("Metadata lookups kept failing (%s); stopped after %d of %d to stay polite: "
                                  "dates, likes and tags are missing for the rest." % (_cell(err, 120), i, len(todo)))
                break
            continue
        fails = 0
        done += 1
        apply_meta(r, m, ch)
    return done, len(todo)


# --------------------------------------------------------------------------- 3. performance

def assign_multiples(recs):
    """views / the median views of the video's own format; None when the format has < MIN_BASELINE videos."""
    baselines = {}
    for fmt in ("long", "short", "live"):
        rs = [r for r in recs if r["format"] == fmt]
        views = [r["views"] for r in rs if r.get("views") is not None]
        base = statistics.median(views) if len(views) >= MIN_BASELINE else None
        baselines[fmt] = base
        for r in rs:
            r["multiple"] = round(r["views"] / base, 2) if base and r.get("views") is not None else None
    radar = _skill("radar")
    for r in recs:
        if r.get("views") and r.get("upload_date") and radar:
            radar.add_metrics(r)   # age_days, velocity (views/day), engagement ((likes + 2 x comments) / views)
        if r.get("views"):
            if r.get("likes") is not None:
                r["like_rate"] = round(100.0 * r["likes"] / r["views"], 2)
            if r.get("comments") is not None:
                r["comments_per_1k"] = round(1000.0 * r["comments"] / r["views"], 2)
    return baselines


def _brief(r):
    return {"id": r["id"], "title": r.get("title"), "views": r.get("views"), "multiple": r.get("multiple"),
            "url": r.get("url"), "topics": r.get("topics") or []}


def format_stats(recs, fmt, base):
    """Baseline, outliers, engagement, velocity, best/worst and momentum for one format."""
    rs = [r for r in recs if r["format"] == fmt]
    views = sorted(r["views"] for r in rs if r.get("views") is not None)
    ranked = sorted((r for r in rs if r.get("multiple") is not None), key=lambda r: -r["multiple"])
    newest = sorted((r for r in rs if r.get("views") is not None), key=lambda r: r["rank"])
    last, prev = newest[:10], newest[10:20]
    trend = None
    if len(last) >= 5 and len(prev) >= 5:
        lm, pm = statistics.median(r["views"] for r in last), statistics.median(r["views"] for r in prev)
        if pm > 0:
            ratio = lm / pm
            trend = {"last_n": len(last), "prev_n": len(prev), "last_median": round(lm), "prev_median": round(pm),
                     "ratio": round(ratio, 2),
                     "direction": "up" if ratio >= 1.25 else "down" if ratio <= 0.8 else "flat"}
    meta = [r for r in rs if r.get("meta")]
    durs = [r["duration"] for r in rs if r.get("duration")]
    # profiles (TikTok) carry likes and dates in their flat list, so read rates from every video that has them
    rated_eng = [r for r in rs if r.get("engagement") is not None or r.get("velocity") is not None]
    outl = [r for r in ranked if r["multiple"] >= OUTLIER]
    return {
        "listed": len(rs),
        "with_views": len(views),
        "median_views": round(base) if base else None,
        "baseline_note": None if base else "fewer than %d videos with views: no baseline" % MIN_BASELINE,
        "mean_views": round(statistics.mean(views)) if views else None,
        "p90_views": views[min(len(views) - 1, int(0.9 * len(views)))] if views else None,
        "max_views": views[-1] if views else None,
        "outliers": len(outl),
        "outlier_share": round(len(outl) / float(len(ranked)), 2) if ranked else None,
        "median_duration_s": _r(_median(durs), 0),
        "with_metadata": len(meta),
        "median_engagement": _r(_median([r.get("engagement") for r in rated_eng]), 2),
        "median_like_rate": _r(_median([r.get("like_rate") for r in rated_eng]), 2),
        "median_comments_per_1k": _r(_median([r.get("comments_per_1k") for r in rated_eng]), 2),
        "median_velocity": _r(_median([r.get("velocity") for r in rated_eng]), 0),
        "chapters_share": round(sum(1 for r in meta if r.get("chapters")) / float(len(meta)), 2) if meta else None,
        "best": [_brief(r) for r in ranked[:5]],
        "worst": [_brief(r) for r in ranked[-3:][::-1]] if len(ranked) >= 6 else [],
        "trend": trend,
    }


# --------------------------------------------------------------------------- 4. cadence

def cadence(recs, today=None):
    """Uploads per week per format, gaps, weekdays and hours (UTC) from the newest dated videos."""
    today = today or _today()
    per, allr = {}, []
    for fmt in ("long", "short", "live"):
        rs = [r for r in recs if r["format"] == fmt and r.get("sample") != "top" and _date(r.get("upload_date"))]
        if not rs:
            continue
        allr += rs
        dates = sorted((_date(r["upload_date"]) for r in rs), reverse=True)
        span = (dates[0] - dates[-1]).days
        gaps = [(a - b).days for a, b in zip(dates, dates[1:])]
        per[fmt] = {
            "dated": len(dates), "span_days": span,
            "per_week": round((len(dates) - 1) * 7.0 / span, 2) if len(dates) >= 2 and span > 0 else None,
            "median_gap_days": _r(_median(gaps), 1), "longest_gap_days": max(gaps) if gaps else None,
            "last_upload_days_ago": (today - dates[0]).days,
            "last_30_days": sum(1 for d in dates if (today - d).days <= 30),
        }
    wd = collections.Counter(WEEKDAYS[_date(r["upload_date"]).weekday()] for r in allr)
    hours = collections.Counter(dt.datetime.fromtimestamp(r["timestamp"], dt.timezone.utc).hour
                                for r in allr if isinstance(r.get("timestamp"), (int, float)))
    n, nh = sum(wd.values()), sum(hours.values())
    return {
        "per_format": per,
        "per_week_total": round(sum(p["per_week"] or 0 for p in per.values()), 2) if per else None,
        "weekdays": [{"day": d, "count": wd.get(d, 0), "share": round(wd.get(d, 0) / float(n), 2) if n else 0}
                     for d in WEEKDAYS],
        "top_days": [d for d, _ in wd.most_common(2)],
        "hours_utc": [{"hour": h, "count": c, "share": round(c / float(nh), 2)} for h, c in hours.most_common(4)],
        "dated": n,
    }


# --------------------------------------------------------------------------- 5. titles

def title_features(title):
    """Countable properties of one title."""
    t = title or ""
    low = t.lower().replace("\u2019", "'")
    letters = [c for c in t if c.isalpha()]
    words = WORD_RE.findall(t)
    return {
        "chars": len(t),
        "words": len(words),
        "caps_ratio": round(sum(c.isupper() for c in letters) / float(len(letters)), 2) if letters else 0.0,
        "caps_words": sum(1 for w in words if len(w) > 1 and w.isupper() and any(c.isalpha() for c in w)),
        "has_number": bool(re.search(r"\d", t)),
        "starts_number": bool(re.match(r"\s*[#$]?\d", t)),
        "money": bool(re.search(r"[$\u20AC\u00A3\u20B9]\s?\d|\b\d[\d,.]*\s?(?:k|m)?\s?(?:dollars|usd|bucks|rupees|euros)\b", low)),
        "question": "?" in t,
        "exclaim": "!" in t,
        "brackets": bool(re.search(r"\[[^\]]+\]|\([^)]+\)", t)),
        "emoji": bool(EMOJI_RE.search(t)),
        "first_person": bool(re.search(r"\b(?:i|i'm|i've|i'd|my|me|we|our|us)\b", low)),
        "second_person": bool(re.search(r"\b(?:you|your|you're|yours|yourself)\b", low)),
        "superlative": bool(re.search(r"\b(?:best|worst|most|least|ever|only|fastest|cheapest|biggest|easiest|"
                                      r"hardest|ultimate)\b", low)),
        "negative": bool(re.search(r"\b(?:never|don't|dont|stop|mistakes?|wrong|worst|avoid|fail(?:ed|ing)?|"
                                   r"quit|bad|ruin(?:ed|ing|s)?)\b", low)),
        "time_frame": bool(re.search(r"\b\d+\s*(?:seconds?|secs?|minutes?|mins?|hours?|hrs?|days?|weeks?|"
                                     r"months?|years?)\b", low)),
        "versus": bool(re.search(r"\bvs\.?\b|\bversus\b", low)),
        "split": bool(re.search(r"[:|]|\s[-\u2013\u2014]\s", t)),
        "hashtag": "#" in t,
    }


TITLE_TRAITS = [
    ("has_number", "a number"), ("starts_number", "opens with a number"), ("money", "a money amount"),
    ("question", "a question mark"), ("exclaim", "an exclamation mark"), ("brackets", "[brackets] or (parentheses)"),
    ("emoji", "an emoji"), ("first_person", "first person (I / my / we)"), ("second_person", "second person (you)"),
    ("caps", "an ALL-CAPS word"), ("superlative", "a superlative (best, worst, ever...)"),
    ("negative", "a negative / warning word (never, stop, mistake...)"), ("time_frame", "a time frame (7 days, 10 minutes)"),
    ("versus", "a versus"), ("split", "a colon / dash / pipe split"), ("short40", "40 characters or fewer"),
    ("long60", "over 60 characters"),
]


def _title_trait(r, key):
    f = r.get("title_features")
    if not f:
        return None
    if key == "caps":
        return f["caps_words"] > 0
    if key == "short40":
        return f["chars"] <= 40
    if key == "long60":
        return f["chars"] > 60
    return bool(f[key])


def split_winners(rated):
    """(winners, rest, rule): outliers at 2x+ when there are at least 3, else the top quarter by multiple."""
    rated = sorted(rated, key=lambda r: -r["multiple"])
    w = [r for r in rated if r["multiple"] >= OUTLIER]
    if len(w) >= 3 and len(rated) - len(w) >= 3:
        return w, rated[len(w):], "outliers (2x+ the format median)"
    if len(rated) >= 8:
        k = max(2, len(rated) // 4)
        return rated[:k], rated[k:], "top 25%% by multiple (only %d video(s) reached 2x)" % len(w)
    return [], rated, "too few videos with a baseline to compare"


def over_index(recs, traits, min_count=2):
    """Each trait's share among winners vs the rest, with a smoothed lift (>1 = the winners have it more)."""
    rated = [r for r in recs if r.get("multiple") is not None]
    winners, rest, rule = split_winners(rated)
    rows = []
    if winners and rest:
        for key, label, fn in traits:
            w = [v for v in (fn(r) for r in winners) if v is not None]
            o = [v for v in (fn(r) for r in rest) if v is not None]
            if not w or not o or sum(w) + sum(o) < min_count:
                continue
            lift = ((sum(w) + 0.5) / (len(w) + 1.0)) / ((sum(o) + 0.5) / (len(o) + 1.0))
            rows.append({"trait": key, "label": label, "winners": round(sum(w) / float(len(w)), 2),
                         "rest": round(sum(o) / float(len(o)), 2), "winners_n": len(w), "rest_n": len(o),
                         "lift": round(lift, 2)})
    rows.sort(key=lambda x: -abs(x["winners"] - x["rest"]))   # share difference: robust where a share is 0
    return {"rule": rule, "winners": len(winners), "rest": len(rest), "traits": rows}


def strong_traits(over, positive=True, min_share=0.3):
    """Traits that clearly over-index (lift >= 1.5, on >= 30% of winners) or under-index (lift <= 0.67),
    biggest share difference first."""
    out = []
    for t in over.get("traits") or []:
        if positive and t["lift"] >= 1.5 and t["winners"] >= min_share:
            out.append(t)
        elif not positive and t["lift"] <= 0.67 and t["rest"] >= min_share:
            out.append(t)
    return sorted(out, key=lambda t: -abs(t["winners"] - t["rest"]))


def titles(recs):
    """Title features per video, stats per format, over-indexing patterns, formats and hook formulas."""
    radar, hooks = _skill("radar"), _yt_hooks()
    rs = [r for r in recs if r.get("title")]
    for r in rs:
        r["title_features"] = title_features(r["title"])
        r["title_format"] = radar.classify_format(r["title"]) if radar else None
        r["title_formula"] = hooks.classify(r["title"])[0] if hooks else None
    per = {}
    for fmt in ("long", "short", "live"):
        fr = [r for r in rs if r["format"] == fmt]
        if not fr:
            continue
        per[fmt] = {
            "n": len(fr),
            "median_chars": _r(_median([r["title_features"]["chars"] for r in fr]), 0),
            "median_words": _r(_median([r["title_features"]["words"] for r in fr]), 0),
            "median_caps_ratio": _r(_median([r["title_features"]["caps_ratio"] for r in fr]), 2),
        }
    shares = {key: round(sum(1 for r in rs if _title_trait(r, key)) / float(len(rs)), 2) if rs else None
              for key, _ in TITLE_TRAITS}
    over = over_index(rs, [(k, label, (lambda r, k=k: _title_trait(r, k))) for k, label in TITLE_TRAITS])
    rated = [r for r in rs if r.get("multiple") is not None]
    winners, _, _ = split_winners(rated)
    wchars = sorted(r["title_features"]["chars"] for r in winners)
    sweet = [wchars[len(wchars) // 4], wchars[(3 * len(wchars)) // 4]] if len(wchars) >= 4 else None

    def counts(group, key):
        c = collections.Counter(r.get(key) for r in group if r.get(key))
        return [{"name": k, "count": v, "share": round(v / float(len(group)), 2)} for k, v in c.most_common(6)]

    return {
        "per_format": per, "shares": shares, "over_index": over, "winner_chars_range": sweet,
        "formats_all": counts(rs, "title_format"), "formats_winners": counts(winners, "title_format"),
        "formulas_all": counts(rs, "title_formula"), "formulas_winners": counts(winners, "title_formula"),
        "examples": [_brief(r) for r in sorted(winners, key=lambda r: -r["multiple"])[:6]],
    }


# --------------------------------------------------------------------------- 6. topics

def _stem(t):
    return t[:-1] if len(t) > 4 and t.endswith("s") and not t.endswith("ss") else t


def _tokens(text, stop, surface):
    """Lower-case content tokens with None at every break (stop word, number, punctuation run)."""
    text = (text or "").lower().replace("\u2019", "'")
    text = re.sub(r"https?://\S+|www\.\S+|\S+@\S+", " . ", text)
    text = re.sub(r"\b\d{1,2}:\d{2}(?::\d{2})?\b", " . ", text)
    out = []
    for raw in re.findall(r"[a-z0-9][a-z0-9'+&]*|[.,!?;:()\[\]|/\-\u2013\u2014#]", text):
        if not raw[0].isalnum():
            out.append(None)
            continue
        t = raw.strip("'")
        if t.endswith("'s"):
            t = t[:-2]
        word = t
        t = _stem(t)
        if len(t) < 3 or t.isdigit() or t in stop or raw in stop:
            out.append(None)
            continue
        surface[t][word] += 1
        out.append((t, word))
    return out


def _doc_terms(r, stop, surface):
    """Weighted term counts for one video: title x3 and tags x2 (unigrams + bigrams), description x1
    (unigrams). Returns (counts, strong, titled): strong = terms in the title or tags, titled = in the title."""
    counts, strong, titled = collections.Counter(), set(), set()
    fields = [(r.get("title") or "", 3.0, True)] + [(t, 2.0, True) for t in (r.get("tags") or [])]
    fields.append(((r.get("description") or "")[:600], 1.0, False))
    for k, (text, w, is_strong) in enumerate(fields):
        toks = _tokens(text, stop, surface)
        for i, tok in enumerate(toks):
            if tok is None:
                continue
            t = tok[0]
            found = [t]
            nxt = toks[i + 1] if i + 1 < len(toks) else None
            if is_strong and nxt is not None and nxt[0] != t:
                found.append(t + " " + nxt[0])
                surface[found[-1]][tok[1] + " " + nxt[1]] += 1
            for term in found:
                counts[term] += w
                if is_strong:
                    strong.add(term)
                if k == 0:
                    titled.add(term)
    return counts, strong, titled


def _label(term, surface):
    """The most common written form of a (stemmed) term."""
    if surface.get(term):
        return surface[term].most_common(1)[0][0]
    return " ".join(surface[p].most_common(1)[0][0] if surface.get(p) else p for p in term.split(" "))


def _cos(a, b):
    num = sum(v * b.get(k, 0.0) for k, v in a.items())
    da = math.sqrt(sum(v * v for v in a.values()))
    db = math.sqrt(sum(v * v for v in b.values()))
    return num / (da * db) if da and db else 0.0


def _split_hashtags(recs):
    """Copies of the records' text with "#mealprep" read as "meal prep", using words the channel
    writes elsewhere (viral-extract's word_break); a hashtag that does not split stays one word."""
    extract = _skill("extract")
    texts = [" ".join([r.get("title") or "", r.get("description") or ""] + list(r.get("tags") or [])) for r in recs]
    vocab = {w for t in texts for w in re.findall(r"(?<![#\w])[a-z]{2,}", t.lower())}

    def split(tag):
        parts = extract.word_break(tag, vocab)
        if len(parts) == 1 and tag.lower().endswith("s"):   # "#budgetmeals" when only "meal" is written out
            parts = extract.word_break(tag[:-1], vocab)
            parts = parts[:-1] + [parts[-1] + "s"] if len(parts) > 1 else [tag]
        return " ".join(parts)

    def fix(text):
        if not text or "#" not in text or extract is None:
            return text
        return re.sub(r"#(\w+)", lambda m: split(m.group(1)), text)

    return [{"title": fix(r.get("title")), "tags": [fix(t) for t in r.get("tags") or []],
             "description": fix(r.get("description"))} for r in recs]


def topic_model(recs, max_topics=None):
    """Tag every video with 1-3 topics; returns the topics as [{"label", "keywords"}].

    Candidates are words and two-word phrases in the title or tags of at least two videos but in
    no more than half of them (a word on every video is the niche, not a topic), ranked by summed
    TF-IDF. A word whose videos split between two different phrases ("meal" in "meal prep" and
    "budget meals") is ambiguous and dropped in favour of the phrases. Candidates covering the
    same videos (Jaccard >= 0.5) merge into one topic, labelled by the term most used in titles;
    a new topic must bring at least a third new videos. Each video takes the topics its title and
    tags score highest on (up to 3, each at least 35% of its best); with no title/tag match it
    falls back to its description, then the nearest topic centroid, then "other". Topics left with
    fewer than two videos are dropped and their videos re-tagged. Sets r["topics"] on every record.
    """
    radar = _skill("radar")
    stop = (getattr(radar, "STOP", set()) if radar else set()) | TOPIC_STOP
    jaccard = radar.jaccard if radar else (lambda a, b: len(a & b) / float(len(a | b)) if a and b else 0.0)
    surface = collections.defaultdict(collections.Counter)
    docs = [_doc_terms(r, stop, surface) for r in _split_hashtags(recs)]
    n = len(docs)
    if not n:
        return []
    df, sdocs, tdf = collections.Counter(), collections.defaultdict(set), collections.Counter()
    for i, (c, strong, titled) in enumerate(docs):
        df.update(c.keys())
        tdf.update(titled)
        for t in strong:
            sdocs[t].add(i)
    min_df = max(2, int(math.ceil(0.02 * n)))
    max_df = max(min_df, int(0.5 * n))
    idf = {t: math.log((1.0 + n) / (1.0 + df[t])) + 1.0 for t in df}
    # terms on more than half the videos (the niche, channel boilerplate) carry no topic signal
    vecs = [{t: (1.0 + math.log(w)) * idf[t] for t, w in c.items() if df[t] <= max_df} for c, _, _ in docs]
    score = {t: sum(vecs[i].get(t, 0.0) for i in d) * (1.15 if " " in t else 1.0)
             for t, d in sdocs.items() if len(d) >= min_df and df[t] <= max_df}
    for u in [t for t in score if " " not in t]:
        phrases = sorted((b for b in score if " " in b and u in b.split(" ")), key=lambda b: -len(sdocs[b]))[:5]
        split = any(len(sdocs[a] & sdocs[b]) < 0.3 * min(len(sdocs[a]), len(sdocs[b]))
                    for i, a in enumerate(phrases) for b in phrases[i + 1:])
        held = set().union(*(sdocs[b] for b in phrases)) & sdocs[u] if phrases else set()
        if split and len(held) >= 0.7 * len(sdocs[u]):
            del score[u]
    k_max = max_topics or max(3, min(12, int(round(math.sqrt(n))) + 2))
    topics, covered = [], set()
    for t in sorted(score, key=lambda t: (-score[t], t)):
        d = sdocs[t]
        best = max(topics, key=lambda tp: jaccard(d, tp["docs"]), default=None)
        if best is not None and jaccard(d, best["docs"]) >= 0.5:
            best["terms"].append(t)
            continue
        if len(topics) >= k_max or len(d - covered) < len(d) / 3.0:
            continue
        topics.append({"terms": [t], "docs": set(d)})
        covered |= d
    for tp in topics:
        tp["label"] = _label(max(tp["terms"], key=lambda t: (tdf[t], " " in t, score[t])), surface)

    def ranked(i, strong_only):
        out = []
        for k, tp in enumerate(topics):
            sc = sum(vecs[i].get(t, 0.0) for t in tp["terms"] if not strong_only or t in docs[i][1])
            if sc > 0:
                out.append((sc, k))
        return sorted(out, reverse=True)

    def assign():
        got = [[] for _ in recs]
        for i in range(n):
            sc = ranked(i, True)
            if sc:
                got[i] = [k for v, k in sc if v >= 0.35 * sc[0][0]][:3]
        cents = []   # centroids of the title/tag-tagged videos, for the ones without a match
        for k in range(len(topics)):
            cen = collections.Counter()
            for i, a in enumerate(got):
                if k in a:
                    cen.update(vecs[i])
            cents.append(dict(cen))
        for i in range(n):
            if got[i]:
                continue
            sc = ranked(i, False)
            if sc:
                got[i] = [sc[0][1]]
                continue
            sims = sorted(((_cos(vecs[i], cen), k) for k, cen in enumerate(cents)), reverse=True)
            if sims and sims[0][0] >= 0.1:
                got[i] = [sims[0][1]]
        return got

    assigned = assign()
    size = collections.Counter(k for a in assigned for k in a)
    if any(size[k] < 2 for k in range(len(topics))):
        topics = [tp for k, tp in enumerate(topics) if size[k] >= 2]
        assigned = assign()
    for r, a in zip(recs, assigned):
        r["topics"] = [topics[k]["label"] for k in a] or ["other"]
    return [{"label": tp["label"], "keywords": [_label(t, surface) for t in tp["terms"][:6]]} for tp in topics]


def topic_stats(recs, topics):
    """Per topic: videos, share, formats, median views and multiple, recent vs older trend, best video."""
    rows = []
    recent_all = [r for r in recs if r["recency"] <= 0.34]
    older_all = [r for r in recs if r["recency"] > 0.34]
    for tp in topics + [{"label": "other", "keywords": []}]:
        members = [r for r in recs if tp["label"] in (r.get("topics") or [])]
        if not members:
            continue
        mults = [r["multiple"] for r in members if r.get("multiple") is not None]
        rec_n = sum(1 for r in members if r["recency"] <= 0.34)
        old_n = len(members) - rec_n
        trend = None
        if recent_all and older_all:
            rs = (rec_n + 0.5) / (len(recent_all) + 1.0)
            os_ = (old_n + 0.5) / (len(older_all) + 1.0)
            ratio = rs / os_
            trend = "rising" if ratio >= 1.5 else "fading" if ratio <= 0.67 else "steady"
        rec_m = _median([r.get("multiple") for r in members if r["recency"] <= 0.34])
        old_m = _median([r.get("multiple") for r in members if r["recency"] > 0.34])
        best = max((r for r in members if r.get("multiple") is not None), key=lambda r: r["multiple"], default=None)
        rows.append({
            "label": tp["label"], "keywords": tp["keywords"], "videos": len(members),
            "share": round(len(members) / float(len(recs)), 2),
            "formats": dict(collections.Counter(r["format"] for r in members)),
            "median_multiple": _r(_median(mults), 2), "median_views": _r(_median([r.get("views") for r in members]), 0),
            "outliers": sum(1 for m in mults if m >= OUTLIER),
            "recent_videos": rec_n, "trend": trend,
            "recent_median_multiple": _r(rec_m, 2), "older_median_multiple": _r(old_m, 2),
            "best": _brief(best) if best else None,
        })
    rows.sort(key=lambda t: (t["label"] == "other", -(t["median_multiple"] or 0), -t["videos"]))
    return rows


# --------------------------------------------------------------------------- 7. thumbnails

def thumb_url(r):
    if r["platform"] == "youtube":
        return "https://i.ytimg.com/vi/%s/maxresdefault.jpg" % r["id"]
    return r.get("thumbnail")


THUMB_TRAITS = [
    ("has_text", "text on the thumbnail", lambda t, m: None if t.get("text") is None else t["text"]["words"] > 0),
    ("few_words", "1-3 words of text", lambda t, m: None if t.get("text") is None else 1 <= t["text"]["words"] <= 3),
    ("many_words", "6+ words of text", lambda t, m: None if t.get("text") is None else t["text"]["words"] >= 6),
    ("big_text", "big text (a line >= 12% of the height)",
     lambda t, m: None if t.get("text") is None else t["text"]["max_line_pct"] >= 12),
    ("face", "a face", lambda t, m: None if t.get("faces") is None else t["faces"]["count"] > 0),
    ("big_face", "a big face (>= 8% of the frame)",
     lambda t, m: None if t.get("faces") is None else t["faces"]["largest_share"] >= 0.08),
    ("bright", "brighter than this channel's median", lambda t, m: t["brightness"] >= m["brightness"]),
    ("contrast", "more contrast than the median", lambda t, m: t["contrast"] >= m["contrast"]),
    ("saturated", "more saturated than the median", lambda t, m: t["saturation"] >= m["saturation"]),
    ("vivid", "vivid colour (colourfulness >= 60)", lambda t, m: t["colorfulness"] >= 60),
    ("clean", "clean (busyness <= 40)", lambda t, m: t["busyness"] <= 40),
    ("busy", "busy (busyness >= 60)", lambda t, m: t["busyness"] >= 60),
    ("dark", "dark (brightness < 0.35)", lambda t, m: t["brightness"] < 0.35),
    ("warm", "warm dominant colour (red / orange / yellow)",
     lambda t, m: t.get("dominant_hue") in ("red", "orange", "yellow")),
    ("cool", "cool dominant colour (green / cyan / blue / purple)",
     lambda t, m: t.get("dominant_hue") in ("green", "cyan", "blue", "purple")),
    ("subject_left", "subject on the left", lambda t, m: t["composition"]["subject"] == "left"),
    ("subject_right", "subject on the right", lambda t, m: t["composition"]["subject"] == "right"),
    ("subject_centre", "subject centred", lambda t, m: t["composition"]["subject"] == "centre"),
    ("text_top", "text in the top third",
     lambda t, m: None if not (t.get("text") or {}).get("words") else t["text"]["position"].startswith("top")),
]


def thumbnails(ch, recs, fetch, out_dir, detector):
    """Download and measure long-form thumbnails; traits that over-index among outliers; contact sheet."""
    from . import thumbs
    longs = [r for r in recs if r["format"] == "long"]
    if not longs:
        return None
    rated = sorted((r for r in longs if r.get("multiple") is not None), key=lambda r: -r["multiple"])
    pick, seen = [], set()
    for r in [r for r in longs if r.get("meta")] + rated[:10] + rated[-10:]:
        if r["id"] not in seen and len(pick) < THUMB_CAP:
            seen.add(r["id"])
            pick.append(r)
    tdir = os.path.join(out_dir, "thumbs")
    os.makedirs(tdir, exist_ok=True)
    failed = 0
    for i, r in enumerate(pick, 1):
        if i == 1 or i % 10 == 0:
            log("  thumbnails %d/%d" % (i, len(pick)))
        path = os.path.join(tdir, _safe_id(r["id"]) + ".jpg")
        if not os.path.isfile(path):
            url = thumb_url(r)
            got = fetch.download_thumb(url, path) if url else None
            if not got or not os.path.isfile(got):
                failed += 1
                continue
            path = got
        try:
            t = thumbs.analyze_thumb(path, detector=detector, faces=detector is not None)
        except Exception as e:  # an unreadable image skips one thumbnail
            log("  thumbnail %s unreadable: %s" % (r["id"], str(e)[:100]))
            failed += 1
            continue
        t["path"] = os.path.relpath(path, out_dir).replace(os.sep, "/")
        r["thumb"] = t
    done = [r for r in pick if r.get("thumb")]
    if failed:
        ch["gaps"].append("%d of %d thumbnails could not be downloaded or read." % (failed, len(pick)))
    if not done:
        return {"analysed": 0}
    T = [r["thumb"] for r in done]
    med = {k: _median([t[k] for t in T]) for k in ("brightness", "contrast", "saturation", "colorfulness", "busyness")}
    traits = [(k, label, (lambda r, fn=fn: fn(r["thumb"], med) if r.get("thumb") else None))
              for k, label, fn in THUMB_TRAITS]
    over = over_index(done, traits)
    winners, _, rule = split_winners([r for r in done if r.get("multiple") is not None])
    have_text = [t for t in T if t.get("text") is not None]
    have_faces = [t for t in T if t.get("faces") is not None]
    summary = {"analysed": len(done), "ocr": bool(have_text), "faces_measured": bool(have_faces),
               "all": _thumb_profile(T), "winners": _thumb_profile([r["thumb"] for r in winners]) if winners else None,
               "winners_rule": rule, "over_index": over}
    if not have_faces:
        ch["gaps"].append("Faces on thumbnails were not measured: no face model (python -m vstudio autoedit or "
                          "reframe.ensure_model() downloads YuNet into workspace/models).")
    if not have_text:
        ch["gaps"].append("Thumbnail text was not read: OCR is not installed (pip install rapidocr-onnxruntime).")
    return summary


def _colour_name(hexstr):
    from . import thumbs
    h = (hexstr or "").lstrip("#")
    return thumbs.hue_family([int(h[i:i + 2], 16) for i in (0, 2, 4)]) if len(h) == 6 else None


def _text_style(st):
    """"white text, black outline, on yellow" from a thumbs text style."""
    out = "%s text" % _colour_name(st.get("fill"))
    if st.get("outline"):
        out += ", %s outline" % _colour_name(st["outline"])
    return out + (", on %s" % _colour_name(st["behind"]) if st.get("behind") else "")


def _thumb_profile(T):
    """Medians and counts over a group of thumbnail measurements."""
    have_text = [t for t in T if t.get("text") is not None]
    with_text = [t for t in have_text if t["text"]["words"]]
    have_faces = [t for t in T if t.get("faces") is not None]
    with_face = [t for t in have_faces if t["faces"]["count"]]
    return {
        "n": len(T),
        "median": {k: _r(_median([t[k] for t in T]), 3)
                   for k in ("brightness", "contrast", "saturation", "colorfulness", "busyness")},
        "text_share": round(len(with_text) / float(len(have_text)), 2) if have_text else None,
        "median_text_words": _r(_median([t["text"]["words"] for t in with_text]), 0),
        "median_text_area": _r(_median([t["text"]["area_share"] for t in with_text]), 3),
        "median_text_line_pct": _r(_median([t["text"]["max_line_pct"] for t in with_text]), 1),
        "text_positions": _top((t["text"]["position"] for t in with_text), 4),
        "text_styles": _top((_text_style(t["text"]["style"]) for t in with_text if t["text"].get("style")), 3),
        "face_share": round(len(with_face) / float(len(have_faces)), 2) if have_faces else None,
        "median_face_size": _r(_median([t["faces"]["largest_share"] for t in with_face]), 3),
        "dominant_hues": _top((t.get("dominant_hue") for t in T), 4),
        "subjects": _top((t["composition"]["subject"] for t in T), 3),
        "palette": [p["hex"] for t in T[:5] for p in t["palette"][:2]],
    }


def channel_sheet(ch, recs, out_path, root):
    """Contact sheet of the top outliers' thumbnails, labelled with views and multiple."""
    from . import thumbs
    top = sorted((r for r in recs if r.get("thumb") and r.get("multiple") is not None),
                 key=lambda r: -r["multiple"])[:SHEET_TILES]
    if not top:
        return None
    files = [os.path.join(root, r["thumb"]["path"]) for r in top]
    labels = ["%s  %s views" % (_x(r["multiple"]), _n(r.get("views"))) + "\n" + (r.get("title") or "") for r in top]
    title = "%s: long-form thumbnails ranked by multiple of the channel median" % (ch.get("name") or "channel")
    return thumbs.sheet(files, out_path, labels, title=title) or None


# --------------------------------------------------------------------------- 8. deep dive

def audio_features(media):
    """Loudness dynamics and brightness variation of the soundtrack (ffmpeg decode, numpy maths).

    50 ms frames, 25 ms hop; frames within 35 dB of the loudest count as active. loudness_std_db is
    the spread of active-frame loudness (the "RMS variance" in dB: flat delivery ~3, punchy ~10);
    centroid_cv is the spectral centroid's coefficient of variation (how much the voice or mix
    brightens and darkens). None without ffmpeg or audio.
    """
    import numpy as np
    if not shutil.which("ffmpeg"):
        return None
    sr = 16000
    try:
        r = subprocess.run(["ffmpeg", "-v", "error", "-i", media, "-vn", "-ac", "1", "-ar", str(sr), "-f", "f32le", "-"],
                           capture_output=True, timeout=300)
    except subprocess.TimeoutExpired:
        return None
    y = np.frombuffer(r.stdout, dtype=np.float32)
    if len(y) < sr // 2:
        return None
    win, hop = 800, 400
    n = 1 + (len(y) - win) // hop
    frames = np.lib.stride_tricks.as_strided(y, shape=(n, win), strides=(y.strides[0] * hop, y.strides[0]))
    rms = np.sqrt(np.mean(frames.astype("float64") ** 2, axis=1))
    db = 20 * np.log10(rms + 1e-9)
    if db.max() < -50:
        return {"seconds": round(len(y) / float(sr), 1), "silent": True}
    active = db > max(db.max() - 35.0, -60.0)
    spec = np.abs(np.fft.rfft(frames[active] * np.hanning(win), axis=1))
    freqs = np.fft.rfftfreq(win, 1.0 / sr)
    cen = (spec * freqs).sum(axis=1) / (spec.sum(axis=1) + 1e-9)
    return {
        "seconds": round(len(y) / float(sr), 1), "silent": False,
        "active_share": round(float(active.mean()), 2),
        "loudness_std_db": round(float(db[active].std()), 2),
        "loudness_range_db": round(float(np.percentile(db[active], 95) - np.percentile(db[active], 10)), 1),
        "centroid_mean_hz": round(float(cen.mean())),
        "centroid_cv": round(float(cen.std() / (cen.mean() + 1e-9)), 3),
    }


def frame_features(video, detector=None, fps=FRAME_FPS, max_seconds=SHORT_CLIP_SECONDS, width=256):
    """Picture statistics at `fps` samples per second for the content-type read.

    face_frames (share of samples with a face; None without a detector), face_size (median largest
    face share), face_x_spread, motion (median mean absolute change between samples within a shot),
    static_share, uniform_share (flat 8x8 blocks: UI and slides are mostly flat), colorfulness,
    edge_density.
    """
    import numpy as np
    import cv2
    from . import thumbs
    cap = cv2.VideoCapture(video)
    vfps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    step = max(1, int(round(vfps / fps)))
    idx, prev = 0, None
    faces, sizes, xs, diffs, uniform, colour, edges = [], [], [], [], [], [], []
    while idx / vfps <= max_seconds:
        ok = cap.grab()
        if not ok:
            break
        if idx % step == 0:
            ok, bgr = cap.retrieve()
            if not ok:
                break
            h, w = bgr.shape[:2]
            small = cv2.resize(bgr, (width, max(1, int(round(h * width / float(w))))), interpolation=cv2.INTER_AREA)
            gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
            rgb = cv2.cvtColor(small, cv2.COLOR_BGR2RGB)
            colour.append(thumbs.colorfulness(rgb))
            edges.append(thumbs.edge_density(gray))
            gh, gw = gray.shape[0] // 8 * 8, gray.shape[1] // 8 * 8
            blocks = gray[:gh, :gw].reshape(gh // 8, 8, gw // 8, 8).std(axis=(1, 3))
            uniform.append(float((blocks < 4).mean()))
            if prev is not None:
                diffs.append(float(np.abs(gray.astype("int16") - prev).mean()) / 255.0)
            prev = gray.astype("int16")
            if detector is not None:
                found = thumbs.detect_faces(small, detector)
                faces.append(bool(found))
                if found:
                    x, y, fw, fh, _ = found[0]
                    sizes.append(fw * fh / float(small.shape[0] * small.shape[1]))
                    xs.append((x + fw / 2.0) / small.shape[1])
        idx += 1
    cap.release()
    if not colour:
        return None
    within = [d for d in diffs if d < 0.18]   # bigger jumps are cuts, not motion
    return {
        "samples": len(colour),
        "face_frames": round(sum(faces) / float(len(faces)), 2) if detector is not None and faces else None,
        "face_size": _r(_median(sizes), 3),
        "face_x_spread": round(float(np.std(xs)), 3) if len(xs) >= 3 else None,
        "motion": round(float(np.median(within)), 4) if within else 0.0,
        "static_share": round(sum(1 for d in within if d < 0.004) / float(len(within)), 2) if within else 1.0,
        "uniform_share": round(float(np.median(uniform)), 2),
        "colorfulness": round(float(np.median(colour)), 1),
        "edge_density": round(float(np.median(edges)), 4),
    }


def word_spacing(words):
    """Word spacing from word START times (robust to captions whose end times are guesses):
    median and p90 seconds between word onsets, pauses (onset gaps >= max(0.6 s, 2.5 x median)) per minute."""
    starts = [w["s"] for w in words or [] if isinstance(w.get("s"), (int, float))]
    if len(starts) < 6:
        return None
    gaps = sorted(b - a for a, b in zip(starts, starts[1:]) if b >= a)
    if not gaps:
        return None
    med = statistics.median(gaps)
    limit = max(0.6, 2.5 * med)
    pauses = [g for g in gaps if g >= limit]
    span = max(1.0, starts[-1] - starts[0])
    return {"median_onset_gap_s": round(med, 3), "p90_onset_gap_s": round(gaps[int(0.9 * (len(gaps) - 1))], 3),
            "pause_threshold_s": round(limit, 2), "pauses_per_min": round(len(pauses) / span * 60, 1),
            "longest_pause_s": round(max(gaps), 2)}


def _ramp(x, lo, hi):
    return max(0.0, min(1.0, (x - lo) / float(hi - lo)))


ENERGY_WEIGHTS = {"speech_rate": 0.25, "cut_rate": 0.25, "caption_motion": 0.20,
                  "loudness_dynamics": 0.15, "brightness_variation": 0.15}


def energy_score(dialogue=None, edit=None, audio=None, text=None):
    """ENERGY 0-100 from what was measured, each part ramped to 0..1 then weighted:

    speech_rate 25% (110 wpm -> 0, 210 -> 1), cut_rate 25% (0.5 cuts per 10 s -> 0, 5 -> 1),
    caption_motion 20% (word-by-word 1, phrase 0.6, text cards 0.3, none 0; averaged with caption
    cards per 10 s, 20 -> 1), loudness_dynamics 15% (loudness spread 3 dB -> 0, 10 dB -> 1),
    brightness_variation 15% (spectral-centroid CV 0.2 -> 0, 0.7 -> 1). Parts that were not
    measured are left out and the weights renormalised; `missing` lists them.
    Label: calm < 40 <= medium < 65 <= high (the same words autoedit's --energy takes).
    """
    parts = {}
    if dialogue and dialogue.get("wpm"):
        parts["speech_rate"] = _ramp(dialogue["wpm"], 110, 210)
    if edit and edit.get("cuts_per_10s") is not None:
        parts["cut_rate"] = _ramp(edit["cuts_per_10s"], 0.5, 5.0)
    if text is not None:
        cap = text.get("caption_track")
        if not cap:
            parts["caption_motion"] = 0.0
        else:
            mode = {"word-by-word": 1.0, "phrase": 0.6, "text-card": 0.3}.get(cap.get("mode"), 0.3)
            spc = cap.get("seconds_per_card") or 0
            parts["caption_motion"] = 0.5 * mode + 0.5 * (_ramp(10.0 / spc, 0, 20) if spc else 0.0)
    if audio and not audio.get("silent"):
        parts["loudness_dynamics"] = _ramp(audio["loudness_std_db"], 3, 10)
        parts["brightness_variation"] = _ramp(audio["centroid_cv"], 0.2, 0.7)
    if not parts:
        return None
    wsum = sum(ENERGY_WEIGHTS[k] for k in parts)
    score = int(round(100 * sum(ENERGY_WEIGHTS[k] * v for k, v in parts.items()) / wsum))
    return {"score": score, "label": "high" if score >= 65 else "medium" if score >= 40 else "calm",
            "components": {k: round(v, 2) for k, v in parts.items()},
            "missing": [k for k in ENERGY_WEIGHTS if k not in parts]}


CONTENT_TYPES = {
    "talking_head": "talking head", "skit": "skit / acted scenes", "voiceover_broll": "voice-over + b-roll",
    "screen_recording": "screen recording", "gameplay": "gameplay", "slideshow": "slideshow / still images",
}


def content_type(frames, edit=None, dialogue=None, text=None):
    """Best guess at the format of the video from the picture and edit statistics.

    Returns {"type", "label", "confidence", "scores", "evidence"}. Talking head and skit need the
    face detector; without it they are not scored and "talking head or voice-over" is said
    instead of guessing.
    """
    if not frames:
        return None
    F, A, X = frames.get("face_frames"), frames.get("face_size") or 0.0, frames.get("face_x_spread") or 0.0
    M, S, U = frames["motion"], frames["static_share"], frames["uniform_share"]
    C, E = frames["colorfulness"], frames["edge_density"]
    K = (edit or {}).get("cuts_per_10s") or 0.0
    speech = 1.0 if dialogue and dialogue.get("words", 0) >= 10 else 0.0
    T = (text or {}).get("coverage") or 0.0
    hud = 1.25 if (text or {}).get("pinned") else 1.0
    faces_known = F is not None
    f = F if faces_known else 0.0
    sc = {
        "screen_recording": _ramp(U, 0.2, 0.55) * (1 - _ramp(C, 25, 70)) * (0.4 + 0.6 * _ramp(T, 0.1, 0.6))
        * (1 - 0.6 * _ramp(M, 0.02, 0.08)) * (1 - _ramp(f * A, 0.01, 0.05)),
        "gameplay": min(1.0, _ramp(C, 20, 55) * _ramp(M, 0.01, 0.05) * _ramp(E, 0.04, 0.12)
                        * (1 - _ramp(U, 0.3, 0.6)) * (1 - 0.7 * _ramp(f * A, 0.01, 0.06)) * hud),
        "slideshow": S * (1 - _ramp(M, 0.004, 0.03)) * (0.5 + 0.5 * _ramp(T, 0.1, 0.6)),
        "voiceover_broll": speech * (1 - f) * _ramp(K, 0.8, 3.0) * (1 - 0.5 * _ramp(U, 0.3, 0.6))
        * (0.5 + 0.5 * _ramp(M, 0.004, 0.03)),
    }
    if faces_known:
        sc["talking_head"] = F * _ramp(A, 0.01, 0.05) * (1.0 if speech else 0.6) * (1 - 0.5 * _ramp(K, 2, 6))
        sc["skit"] = F * _ramp(K, 1.0, 3.5) * _ramp(X, 0.05, 0.15) * (1.0 if speech else 0.7)
    best = max(sc, key=sc.get)
    total = sum(sc.values())
    conf = round(sc[best] / total, 2) if total > 0 else 0.0
    label = CONTENT_TYPES[best]
    if not faces_known and best == "voiceover_broll":
        label = "talking head or voice-over + b-roll (no face detector to tell them apart)"
    if total <= 0.02:
        label, conf = "unclear", 0.0
    ev = {"cuts_per_10s": K, "face_frames": F, "face_size": frames.get("face_size"), "motion": M,
          "static_share": S, "uniform_share": U, "colorfulness": C, "text_coverage": T, "speech": bool(speech)}
    return {"type": best if label != "unclear" else "unclear", "label": label, "confidence": conf,
            "scores": {k: round(v, 3) for k, v in sc.items()}, "evidence": ev}


def _edit_report(clip, adir, extract):
    """ReelMimic's analyze.py on the clip (subprocess; it is a CLI). Returns its report dict or None."""
    try:
        r = subprocess.run([sys.executable, extract.ANALYZE, clip, "--out", adir], capture_output=True, text=True,
                           timeout=900, encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        return None, "edit analysis timed out"
    rp = os.path.join(adir, "report.json")
    if os.path.isfile(rp):
        with open(rp, encoding="utf-8") as f:
            return json.load(f), None
    return None, ((r.stderr or r.stdout or "").strip().splitlines() or ["edit analysis failed"])[-1][:200]


def _edit_dna(extract, report):
    # analyze.py keeps the shot list in "shots" and the counts in "pacing"; extract.edit_dna reads the
    # counts from "shots", so hand it the pacing dict there (requested upstream as a fix)
    rep = dict(report)
    if isinstance(rep.get("shots"), list):
        rep["shots"] = rep.get("pacing") or {}
    return extract.edit_dna(rep)


def _hook(r, dialogue, words):
    """Hook text and its score: ig-reel's hookscore for Shorts, yt-script's (first 15 s) for long-form."""
    if not dialogue:
        return None
    first = dialogue["hook"]["first_sentence"] or dialogue["hook"]["text"]
    if r["format"] == "short":
        hs = _skill("hookscore")
        if not hs or not first:
            return {"text": first}
        _, overall, verdict, flags = hs.run(first)
        return {"text": first, "score": round(overall), "verdict": verdict, "scorer": "ig-reel hookscore",
                "flags": flags}
    yt = _yt_hooks()
    t0 = words[0]["s"] if words else 0
    fifteen = " ".join(w["w"] for w in words if w["s"] < t0 + 15.0)
    if not yt or not fifteen:
        return {"text": first}
    _, verdict, name, _ = yt.score(fifteen)
    return {"text": first, "first_15s": fifteen, "score": verdict, "verdict": yt.band(verdict),
            "formula": name, "scorer": "yt-script hookscore"}


def deep_dive(r, fetch, out_dir, detector):
    """Download one video (a Short whole, a long-form video's first 90 s) and read its Style DNA,
    dialogue, energy and content type into dna/<video_id>/."""
    extract = _skill("extract")
    vdir = os.path.join(out_dir, "dna", _safe_id(r["id"]))
    os.makedirs(vdir, exist_ok=True)
    seconds = SHORT_CLIP_SECONDS if r["format"] == "short" else HOOK_SECONDS
    rel = lambda p: os.path.relpath(p, out_dir).replace(os.sep, "/")
    out = {"id": r["id"], "format": r["format"], "title": r.get("title"), "url": r.get("url"),
           "views": r.get("views"), "multiple": r.get("multiple"), "dir": rel(vdir), "seconds_read": seconds,
           "status": "ok", "notes": []}
    if extract is None:
        out["status"] = "failed"
        out["notes"].append("viral-extract could not be loaded")
        return out
    clip = fetch.download_clip(r["url"], os.path.join(vdir, "clip.mp4"), seconds)
    if not clip or not os.path.isfile(clip):
        out["status"] = "no_clip"
        out["notes"].append("download failed: %s" % _cell(getattr(fetch, "last_error", None) or "no file", 160))
        return out

    # edit: ReelMimic's analysis (it copies the clip to analysis/source.mp4, so the download is dropped)
    report, err = _edit_report(clip, os.path.join(vdir, "analysis"), extract)
    video = clip
    if report:
        for name in ("proxy.mp4", "source.mp4"):
            p = os.path.join(vdir, "analysis", name)
            if os.path.isfile(p):
                video = p
                break
        src = os.path.join(vdir, "analysis", "source.mp4")
        if os.path.isfile(src) and os.path.abspath(src) != os.path.abspath(clip):
            os.remove(clip)
            clip = src
    else:
        out["notes"].append("edit analysis unavailable: %s" % err)
    duration = (report or {}).get("video", {}).get("duration") or _probe_seconds(clip)
    out["clip"] = rel(clip)

    # words: the platform's captions, else local faster-whisper when installed
    words, source = None, None
    cap = fetch.captions(r["url"], vdir)
    if isinstance(cap, list):
        words, source = cap, "captions"
    elif cap and os.path.isfile(cap):
        words, source = extract.load_captions(cap), "captions file (%s)" % os.path.basename(cap)
    if words and duration:
        words = [w for w in words if w["s"] < duration]
    whisper = importlib.util.find_spec("faster_whisper") is not None
    if not words and whisper:
        try:
            words, source = extract.whisper_words(clip), "faster-whisper"
        except Exception as e:  # model download or decode failure: no transcript, said in the notes
            out["notes"].append("whisper failed: %s" % str(e)[:120])
    if words:
        with open(os.path.join(vdir, "transcript.json"), "w", encoding="utf-8") as f:
            json.dump(words, f, indent=1, ensure_ascii=False)
    else:
        out["notes"].append("no transcript (no captions; %s): dialogue not measured" % (
            "whisper heard no speech" if whisper else "faster-whisper is not installed"))

    # on-screen text (captions style)
    vocab = {re.sub(r"[^a-z0-9$%]", "", w["w"].lower()) for w in words or []} - {""}
    try:
        text, cards = extract.text_dna(video, vdir, fps=OCR_FPS.get(r["format"], 2.0), max_seconds=seconds,
                                       vocab=vocab)
    except Exception as e:  # OCR crash on an odd video: keep the rest of the read
        text, cards = None, []
        out["notes"].append("on-screen text read failed: %s" % str(e)[:120])
    else:
        if text is None:
            out["notes"].append("on-screen text not read (OCR unavailable)")
    with open(os.path.join(vdir, "text_cards.json"), "w", encoding="utf-8") as f:
        json.dump(cards, f, indent=1, ensure_ascii=False)

    edit = _edit_dna(extract, report) if report else None
    dialogue = extract.dialogue_dna(words, duration) if words else None
    dna = {"source": r.get("url"), "edit": edit, "text": text, "dialogue": dialogue, "transcript_source": source}
    with open(os.path.join(vdir, "style_dna.json"), "w", encoding="utf-8") as f:
        json.dump(dna, f, indent=2, ensure_ascii=False)
    with open(os.path.join(vdir, "STYLE_DNA.md"), "w", encoding="utf-8") as f:
        f.write(extract.render_md(dna) + "\n")

    audio = audio_features(clip)
    frames = frame_features(video, detector, max_seconds=seconds)
    out.update({
        "duration_read_s": _r(duration, 1),
        "transcript_source": source,
        "dialogue": _dialogue_brief(dialogue),
        "word_spacing": word_spacing(words),
        "hook": _hook(r, dialogue, words or []),
        "edit": edit,
        "captions": (text or {}).get("caption_track"),
        "text_coverage": (text or {}).get("coverage"),
        "audio": audio,
        "frames": frames,
        "energy": energy_score(dialogue, edit, audio, text),
        "content_type": content_type(frames, edit, dialogue, text),
        "style_dna": rel(os.path.join(vdir, "style_dna.json")),
    })
    if os.path.isfile(os.path.join(vdir, "fonts_sheet.jpg")):
        out["fonts_sheet"] = rel(os.path.join(vdir, "fonts_sheet.jpg"))
    if os.path.isfile(os.path.join(vdir, "analysis", "sheet_1fps.jpg")):
        out["frames_sheet"] = rel(os.path.join(vdir, "analysis", "sheet_1fps.jpg"))
    return out


def _probe_seconds(path):
    try:
        import cv2
        cap = cv2.VideoCapture(path)
        n, fps = cap.get(cv2.CAP_PROP_FRAME_COUNT), cap.get(cv2.CAP_PROP_FPS) or 0
        cap.release()
        return round(n / fps, 2) if fps else None
    except Exception:
        return None


def _caption_colours(c):
    return "fill %s%s%s" % (c.get("fill"), ", outline %s" % c["outline"] if c.get("outline") else "",
                            ", box %s" % c["box"] if c.get("box") else "")


def _dialogue_brief(d):
    if not d:
        return None
    keep = ("words", "time_to_first_word_s", "speech_seconds", "speech_coverage", "wpm", "wpm_first_10s", "hook",
            "sentences", "pauses", "per_100_words", "openers", "rehooks", "signature_phrases", "cta", "loop_ending")
    return {k: d.get(k) for k in keep}


def deep_summary(deep):
    """Medians per format across the deep-dived videos, plus the most common caption look and type."""
    out = {}
    for fmt in ("short", "long", "live"):
        ds = [d for d in deep if d["format"] == fmt and d["status"] == "ok"]
        if not ds:
            continue
        dl = [d["dialogue"] for d in ds if d.get("dialogue")]
        ed = [d["edit"] for d in ds if d.get("edit")]
        ws = [d["word_spacing"] for d in ds if d.get("word_spacing")]
        caps = [d["captions"] for d in ds if d.get("captions")]
        en = [d["energy"]["score"] for d in ds if d.get("energy")]
        out[fmt] = {
            "videos": len(ds),
            "wpm": _r(_median([x["wpm"] for x in dl]), 0),
            "wpm_first_10s": _r(_median([x["wpm_first_10s"] for x in dl]), 0),
            "time_to_first_word_s": _r(_median([x["time_to_first_word_s"] for x in dl]), 2),
            "sentence_words": _r(_median([x["sentences"]["mean_words"] for x in dl]), 1),
            "short_sentence_share": _r(_median([x["sentences"]["short_share"] for x in dl]), 2),
            "rehook_gap_s": _r(_median([x["rehooks"]["mean_gap_s"] for x in dl]), 1),
            "median_onset_gap_s": _r(_median([x["median_onset_gap_s"] for x in ws]), 3),
            "pauses_per_min": _r(_median([x["pauses_per_min"] for x in ws]), 1),
            "you_per_100": _r(_median([x["per_100_words"]["you"] for x in dl]), 1),
            "cuts_per_10s": _r(_median([x.get("cuts_per_10s") for x in ed]), 1),
            "mean_shot_s": _r(_median([x.get("mean_shot_s") for x in ed]), 2),
            "first_cut_s": _r(_median([x.get("first_cut_s") for x in ed]), 2),
            "energy": _r(_median(en), 0),
            "caption_modes": _top((c.get("mode") for c in caps), 3),
            "caption_y_pct": _r(_median([c.get("center_y_pct") for c in caps]), 1),
            "words_per_card": _r(_median([(c.get("words_per_card") or {}).get("median") for c in caps]), 1),
            "font_looks": _top(("%(weight)s %(width)s %(case)s" % c["font_look"] for c in caps if c.get("font_look")), 2),
            "font_candidates": (caps[0].get("font_candidates") or [])[:4] if caps else [],
            "caption_colours": _top((_caption_colours(c) for c in caps), 2),
            "content_types": _top((d["content_type"]["label"] for d in ds if d.get("content_type")), 3),
            "hook_formulas": _top((d["hook"].get("formula") for d in ds if (d.get("hook") or {}).get("formula")), 3),
        }
    return out


# --------------------------------------------------------------------------- one channel

def _new_channel(url, platform):
    return {"input": url, "platform": platform, "status": "ok", "status_note": None, "name": None, "handle": None,
            "channel_id": None, "url": None, "followers": None, "description": None, "keywords": [],
            "inventory": {}, "formats": {}, "cadence": None, "titles": None, "topics": [], "thumbnails": None,
            "deep": [], "deep_summary": {}, "summary": [], "playbook": [], "gaps": [], "videos": []}


def analyze_one(url, out_dir, fetch, max_videos=30, deep=4, detector=None):
    """Analyse one channel or profile URL. Returns the channel dict (status says how far it got)."""
    url = normalize_url(url)
    plat = platform_of(url)
    ch = _new_channel(url, plat)
    if plat == "youtube":
        base = youtube_channel_base(url)
        if base is None and YT_VIDEO_RE.search(url):
            m = fetch.video_meta(url)   # a video link: analyse the channel that posted it
            base = youtube_channel_base(normalize_url((m or {}).get("channel_url") or (m or {}).get("uploader_url") or ""))
        if base is None:
            ch["status"], ch["status_note"] = classify_error(getattr(fetch, "last_error", None) or "not found", plat)
            if ch["status"] == "unreachable":
                ch["status_note"] = "could not work out the channel from this link"
            return ch
        tabs = list(FORMATS)
    else:
        base, tabs = url, [("short", None)]
        tt = re.match(r"(https://www\.tiktok\.com/@[^/?#]+)/video/\d+", url)
        if tt:
            # yt-dlp often cannot open a TikTok profile page, but a video names its account id
            m = fetch.video_meta(url) or {}
            cid = m.get("channel_id") or m.get("uploader_id")
            if cid:
                base = "tiktokuser:%s" % cid
            ch["url"] = tt.group(1)
    ch["url"] = ch.get("url") or base

    listed = max(60, 4 * max_videos)
    log("  inventory")
    recs = inventory(ch, base, tabs, fetch, listed)
    if not recs:
        errs = [ch["inventory"][f]["error"] for f in ("long", "short", "live")
                if (ch["inventory"].get(f) or {}).get("error")]
        ch["status"], ch["status_note"] = classify_error(errs[0] if errs else "no videos listed", plat)
        if not errs and ch["status"] == "unreachable":
            ch["status_note"] = "no public videos listed"
        ch["error"] = errs[0] if errs else None
        return ch
    for fmt, inv in ch["inventory"].items():
        if inv.get("error"):
            ch["gaps"].append("The %s tab could not be read: %s" % (_fn(ch, fmt), _cell(inv["error"], 140)))
    ch["name"] = ch["name"] or _handle_from_url(base)
    log("  %s: %s" % (ch["name"], ", ".join("%d %s" % (v["listed"], _fn(ch, k)) for k, v in ch["inventory"].items())))

    got, wanted = metadata(ch, recs, fetch, max_videos)
    ch["metadata"] = {"looked_up": got, "selected": wanted}
    if wanted and got < wanted:
        ch["status"] = "partial"
    baselines = assign_multiples(recs)
    log("  titles and topics")
    ch["titles"] = titles(recs)
    ch["topic_list"] = topic_model(recs)
    ch["topics"] = topic_stats(recs, ch["topic_list"])
    for fmt in ("long", "short", "live"):
        if any(r["format"] == fmt for r in recs):
            ch["formats"][fmt] = format_stats(recs, fmt, baselines.get(fmt))
    ch["cadence"] = cadence(recs)

    if plat == "youtube":
        ch["thumbnails"] = thumbnails(ch, recs, fetch, out_dir, detector)

    if deep:
        picks = []
        for fmt in ("short", "long"):
            # by multiple; a format too small for a baseline falls back to raw views
            fr = sorted((r for r in recs if r["format"] == fmt and r.get("views") is not None),
                        key=lambda r: (r.get("multiple") is not None, r.get("multiple") or 0, r["views"]), reverse=True)
            picks += fr[:deep]
        for i, r in enumerate(picks, 1):
            log("  deep %d/%d: %s" % (i, len(picks), _cell(r.get("title"), 60)))
            try:
                d = deep_dive(r, fetch, out_dir, detector)
            except Exception as e:  # one broken video never stops the others
                d = {"id": r["id"], "format": r["format"], "title": r.get("title"), "status": "failed",
                     "notes": ["deep dive failed: %s" % str(e)[:160]]}
            r["deep"] = d.get("dir")
            ch["deep"].append(d)
        ch["deep_summary"] = deep_summary(ch["deep"])

    _channel_gaps(ch, recs)
    ch["videos"] = sorted(recs, key=lambda r: ({"long": 0, "short": 1, "live": 2}[r["format"]], r["rank"]))
    ch["summary"] = executive_summary(ch)
    ch["playbook"] = playbook(ch, out_dir)
    return ch


def _channel_gaps(ch, recs):
    meta = [r for r in recs if r.get("meta")]
    if meta:
        hidden = sum(1 for r in meta if r.get("likes") is None)
        if hidden:
            ch["gaps"].append("Likes hidden or missing on %d of %d videos looked up." % (hidden, len(meta)))
    for fmt, st in ch["formats"].items():
        if st.get("baseline_note") and st["listed"]:
            ch["gaps"].append("%s: %s, so no multiples or outliers for them." % (_cap(_fn(ch, fmt)), st["baseline_note"]))
    young = [r for r in recs if (r.get("age_days") or 99) < 7]
    if young:
        ch["gaps"].append("%d video(s) are under 7 days old: their views (and multiples) are still growing." % len(young))
    for fmt, inv in ch["inventory"].items():
        if inv.get("capped"):
            ch["gaps"].append("Only the newest %d %s were listed; older videos are not in the baselines."
                              % (inv["listed"], _fn(ch, fmt)))
    for d in ch["deep"]:
        for note in d.get("notes") or []:
            ch["gaps"].append("Deep dive %s: %s" % (d["id"], note))
    if ch["platform"] == "youtube":
        ch["gaps"].append("Public numbers only: CTR, retention, traffic sources and revenue live in YouTube Studio "
                          "(the creator's own export: /yt-retention).")


# --------------------------------------------------------------------------- summary and playbook

def _rated_topics(ch, min_videos=2):
    return [t for t in ch["topics"] if t["label"] != "other" and t["videos"] >= min_videos
            and t["median_multiple"] is not None]


def executive_summary(ch):
    """Five bullets with numbers on what makes the channel work (only from measured data)."""
    fm = ch["formats"]
    out = []
    based = [(f, fm[f]) for f in ("long", "short", "live") if fm.get(f, {}).get("median_views")]
    fol = "; %s followers" % _n(ch["followers"]) if ch.get("followers") else ""
    if len(based) >= 2:
        based.sort(key=lambda x: -x[1]["median_views"])
        (fa, a), (fb, b) = based[0], based[1]
        out.append("Reach comes from %s: median %s views each vs %s for %s (%.1fx)%s." % (
            _fn(ch, fa), _n(a["median_views"]), _n(b["median_views"]), _fn(ch, fb),
            a["median_views"] / float(max(1, b["median_views"])), fol))
    elif based:
        f, a = based[0]
        out.append("%s carry the channel: median %s views across %d listed videos%s." % (
            _cap(_fn(ch, f)), _n(a["median_views"]), a["listed"], fol))
    rated = [(f, s) for f, s in fm.items() if s.get("outliers") is not None and s.get("best")]
    if rated:
        f, s = max(rated, key=lambda x: (x[1]["outliers"], x[1]["best"][0]["multiple"] or 0))
        top = s["best"][0]
        out.append("%d of %d %s (%s) beat their median by 2x or more; the top one, \"%s\", did %s (%s views)." % (
            s["outliers"], s["with_views"], _fn(ch, f), _pct(s["outlier_share"]), _cell(top["title"], 60),
            _x(top["multiple"]), _n(top["views"])))
    tops = _rated_topics(ch, 3 if len(ch["videos"]) >= 30 else 2)
    if tops:
        best = max(tops, key=lambda t: t["median_multiple"])
        rest = _median([r["multiple"] for r in ch["videos"] if r.get("multiple") is not None
                        and best["label"] not in r.get("topics", [])])
        line = "Topic \"%s\" wins: %d videos at a median %s vs %s for everything else" % (
            best["label"], best["videos"], _x(best["median_multiple"]), _x(rest))
        worst = min(tops, key=lambda t: t["median_multiple"])
        if worst is not best and worst["median_multiple"] < 0.8:
            line += "; \"%s\" trails at %s" % (worst["label"], _x(worst["median_multiple"]))
        out.append(line + ".")
    pk = []
    tt = strong_traits((ch.get("titles") or {}).get("over_index") or {})
    if tt:
        pk.append("%s in %s of outlier titles vs %s of the rest" % (tt[0]["label"], _pct(tt[0]["winners"]), _pct(tt[0]["rest"])))
    th = strong_traits(((ch.get("thumbnails") or {}).get("over_index")) or {})
    if th:
        pk.append("%s on %s of outlier thumbnails vs %s" % (th[0]["label"], _pct(th[0]["winners"]), _pct(th[0]["rest"])))
    if pk:
        out.append("Packaging of the winners: " + "; ".join(pk) + ".")
    cad = ch.get("cadence") or {}
    bits = []
    if cad.get("per_week_total"):
        per = ", ".join("%s %s" % (_g(p["per_week"]), _fn(ch, f)) for f, p in cad["per_format"].items() if p.get("per_week"))
        bits.append("posts %s times a week (%s)%s" % (_g(cad["per_week_total"]), per,
                                                       ", mostly %s" % "/".join(cad["top_days"]) if cad.get("top_days") else ""))
        if cad.get("hours_utc"):
            bits[-1] += " around %02d:00 UTC" % cad["hours_utc"][0]["hour"]
    ds = ch.get("deep_summary") or {}
    lead = ds.get("short") or ds.get("long")
    if lead and (lead.get("wpm") or lead.get("cuts_per_10s")):
        s = "the top videos talk at %s wpm" % lead["wpm"] if lead.get("wpm") else "the top videos"
        if lead.get("cuts_per_10s"):
            s += " with %s cuts per 10 s" % lead["cuts_per_10s"]
        if lead.get("energy") is not None:
            s += " (energy %d/100)" % lead["energy"]
        bits.append(s)
    if bits:
        out.append((bits[0][0].upper() + bits[0][1:]) + ("; " + "; ".join(bits[1:]) if len(bits) > 1 else "") + ".")
    if len(out) < 5:
        for f, s in fm.items():
            t = s.get("trend")
            if t and len(out) < 5:
                out.append("Momentum on %s: the last %d median %s views, %.2fx the %d before (%s)." % (
                    _fn(ch, f), t["last_n"], _n(t["last_median"]), t["ratio"], t["prev_n"], t["direction"]))
    return out[:5]


def playbook(ch, out_dir=""):
    """Concrete steps to compete with or replicate the channel, each tied to a measured number.
    Commands name files under out_dir with full paths so they can be pasted from anywhere."""
    P = []
    tops = _rated_topics(ch)
    win = sorted([t for t in tops if t["median_multiple"] >= 1.2], key=lambda t: -t["median_multiple"])
    under = [t for t in win if t["videos"] <= 3]
    weak = [t for t in tops if t["median_multiple"] < 0.8 and t["videos"] >= 3]
    if win:
        s = "**Topics.** Make %s." % ", ".join("\"%s\" (%s over %d videos)" % (t["label"], _x(t["median_multiple"]), t["videos"])
                                             for t in win[:4])
        if under:
            s += " Under-made winners: %s - the audience pays off more than the channel posts them." % ", ".join(
                "\"%s\"" % t["label"] for t in under[:3])
        if weak:
            s += " Skip %s (below their median)." % ", ".join("\"%s\" (%s)" % (t["label"], _x(t["median_multiple"])) for t in weak[:3])
        P.append(s)
    fm, cad = ch["formats"], ch.get("cadence") or {}
    fl = []
    for f, s in fm.items():
        t, per = s.get("trend"), (cad.get("per_format") or {}).get(f, {})
        bit = _fn(ch, f) + (": median %s views" % _n(s["median_views"]) if s.get("median_views") else "")
        if per.get("per_week"):
            bit += "%s %s a week" % (":" if bit == _fn(ch, f) else ",", _g(per["per_week"]))
        if t:
            bit += ", momentum %s (%.2fx)" % (t["direction"], t["ratio"])
        fl.append(bit)
    if fl:
        days = cad.get("top_days") or []
        quiet = [w["day"] for w in cad.get("weekdays") or [] if w["count"] == 0] if cad.get("dated", 0) >= 7 else []
        s = "**Formats and cadence.** " + "; ".join(fl) + "."
        if days:
            s += " They post most on %s" % "/".join(days)
            s += ("; %s get nothing from them - an open slot to test if you share their audience." % "/".join(quiet)
                  if quiet else ".")
        P.append(s)
    ti = ch.get("titles") or {}
    if ti:
        pos = strong_traits(ti.get("over_index") or {})
        neg = strong_traits(ti.get("over_index") or {}, positive=False)
        s = "**Titles.**"
        bare = s
        if ti.get("winner_chars_range"):
            s += " %d-%d characters (the middle half of their outliers)." % tuple(ti["winner_chars_range"])
        if pos:
            s += " Use %s." % ", ".join("%s (%s of outliers vs %s)" % (t["label"], _pct(t["winners"]), _pct(t["rest"])) for t in pos[:3])
        if neg:
            s += " Their outliers avoid %s." % ", ".join(t["label"] for t in neg[:2])
        fw = [f["name"] for f in ti.get("formulas_winners") or [] if f["name"] != "Unclassified"][:2]
        fo = [f["name"] for f in ti.get("formats_winners") or [] if f["name"] != "other"][:2]
        if fw or fo:
            s += " Shapes that win: %s. Write your own in those shapes; never reuse their titles." % ", ".join(fw + fo)
        if s != bare:
            P.append(s)
    th = ch.get("thumbnails") or {}
    if th.get("analysed"):
        pos = strong_traits(th.get("over_index") or {})
        w, a = th.get("winners") or th["all"], th["all"]
        who = "Their outliers use" if th.get("winners") else "They use"
        s = "**Thumbnails.**"
        if w.get("median_text_words"):
            s += " %s %d word(s) of text, a line about %s%% of the height tall%s" % (
                who, w["median_text_words"], _g(w["median_text_line_pct"]),
                " at the %s" % w["text_positions"][0]["name"] if w.get("text_positions") else "")
            s += (", %s." % w["text_styles"][0]["name"]) if w.get("text_styles") else "."
        elif w.get("text_share") == 0:
            s += " %s no text on the thumbnail." % who
        if w.get("face_share") is not None:
            s += " Faces on %s%s." % (_pct(w["face_share"]), ", median size %s of the frame" % _pct(w["median_face_size"])
                                      if w.get("median_face_size") else "")
        if pos:
            s += " Biggest differences from their other thumbnails: %s." % ", ".join(
                "%s (%s vs %s)" % (t["label"], _pct(t["winners"]), _pct(t["rest"])) for t in pos[:3])
        if w.get("dominant_hues"):
            s += " Dominant colour%s: %s%s; take the contrast, not their exact colours." % (
                " of the outliers" if th.get("winners") else "",
                w["dominant_hues"][0]["name"],
                " (whole channel: %s)" % a["dominant_hues"][0]["name"] if a.get("dominant_hues") and
                a["dominant_hues"][0]["name"] != w["dominant_hues"][0]["name"] else "")
        P.append(s)
    ds = ch.get("deep_summary") or {}
    for f in ("short", "long"):
        d = ds.get(f)
        if not d:
            continue
        s = "**Hooks and pacing (%s, %d video%s read).**" % (_fn(ch, f), d["videos"], "s" if d["videos"] > 1 else "")
        if d.get("wpm"):
            s += " First word by %s s, %s wpm (%s in the first 10 s), sentences of about %s words" % (
                _g(d["time_to_first_word_s"]), d["wpm"], d["wpm_first_10s"], _g(d["sentence_words"]))
            s += (", a re-hook every %s s." % _g(d["rehook_gap_s"])) if d.get("rehook_gap_s") else "."
        if d.get("cuts_per_10s"):
            s += " A cut every %s s (%s per 10 s)%s." % (
                _g(10.0 / d["cuts_per_10s"]), _g(d["cuts_per_10s"]),
                ", first cut at %s s" % _g(d["first_cut_s"]) if d.get("first_cut_s") is not None else "")
        if d.get("caption_modes"):
            s += " Captions %s, %s word%s per card, centred %s%% down the frame" % (
                d["caption_modes"][0]["name"], _g(d["words_per_card"]), "" if d["words_per_card"] == 1 else "s",
                _g(d["caption_y_pct"]))
            if d.get("font_looks"):
                s += ", a %s font" % d["font_looks"][0]["name"]
                if d.get("font_candidates"):
                    s += " (free look-alikes: %s)" % ", ".join(d["font_candidates"][:3])
            if d.get("caption_colours"):
                s += ", %s" % d["caption_colours"][0]["name"]
            s += "."
        if d.get("energy") is not None:
            lab = "high" if d["energy"] >= 65 else "medium" if d["energy"] >= 40 else "calm"
            s += " Energy %d/100: edit at `--energy %s`." % (d["energy"], lab)
        if d.get("hook_formulas"):
            s += " Hook shapes: %s." % ", ".join(x["name"] for x in d["hook_formulas"])
        P.append(s)
    ok = [d for d in ch.get("deep") or [] if d.get("status") == "ok"]
    if ok:
        ref = max(ok, key=lambda d: d.get("multiple") or 0)
        energy = (ref.get("energy") or {}).get("label", "high")
        P.append("**Make it with Viral Studio.** Style reference = their best deep-dived video (%s, %s): "
                 "`python -m vstudio reel --prompt \"<your topic>\" --ref %s --competitor %s`; your own footage: "
                 "`python -m vstudio autoedit raw.mp4 --style %s --energy %s`; script: `/script-forge` at the measured "
                 "pace; packaging: `/yt-package` with the title and thumbnail patterns above. Copy the formula, never "
                 "their footage, words or face." % (_cell(ref.get("title"), 40), _x(ref.get("multiple")),
                                                    _arg(out_dir, ref.get("clip") or ref["dir"]), ref.get("url"),
                                                    _arg(out_dir, ref.get("style_dna") or ref["dir"]), energy))
    weak_bits = []
    for f, s in fm.items():
        if (s.get("trend") or {}).get("direction") == "down":
            weak_bits.append("%s are sliding (last 10 at %.2fx the 10 before)" % (_fn(ch, f), s["trend"]["ratio"]))
    fading = [t for t in ch["topics"] if t.get("trend") == "fading" and (t.get("median_multiple") or 0) >= 1.2]
    if fading:
        weak_bits.append("they post less of %s although it still beats their median - take it over"
                         % ", ".join("\"%s\"" % t["label"] for t in fading[:2]))
    for f, p in ((cad or {}).get("per_format") or {}).items():
        if p.get("longest_gap_days") and p.get("median_gap_days") and p["longest_gap_days"] >= 3 * max(1, p["median_gap_days"]) \
                and p["longest_gap_days"] >= 14:
            weak_bits.append("%s went quiet for %d days at one point" % (_fn(ch, f), p["longest_gap_days"]))
    if weak_bits:
        P.append("**Where they are beatable.** " + "; ".join(weak_bits) + ".")
    return P


def _arg(root, rel):
    """A command-line argument for a file under root: full path, quoted when it has spaces."""
    p = os.path.normpath(os.path.join(root, rel)) if root else rel
    return '"%s"' % p if " " in p else p


# --------------------------------------------------------------------------- comparison

def comparison(channels):
    """One row per channel for the side-by-side table."""
    rows = []
    for ch in channels:
        fm, cad, ds = ch.get("formats") or {}, ch.get("cadence") or {}, ch.get("deep_summary") or {}
        tops = _rated_topics(ch)
        best = max(tops, key=lambda t: t["median_multiple"]) if tops else None
        lead = ds.get("short") or ds.get("long") or {}
        rows.append({
            "name": ch.get("name") or ch["input"], "platform": ch["platform"], "status": ch["status"],
            "followers": ch.get("followers"),
            "long_listed": (ch["inventory"].get("long") or {}).get("listed"),
            "short_listed": (ch["inventory"].get("short") or {}).get("listed"),
            "long_median": (fm.get("long") or {}).get("median_views"),
            "short_median": (fm.get("short") or {}).get("median_views"),
            "per_week": cad.get("per_week_total"),
            "top_topic": "%s (%s)" % (best["label"], _x(best["median_multiple"])) if best else None,
            "title_chars": ((ch.get("titles") or {}).get("per_format", {}).get("long") or
                            (ch.get("titles") or {}).get("per_format", {}).get("short") or {}).get("median_chars"),
            "thumb_text_share": ((ch.get("thumbnails") or {}).get("all") or {}).get("text_share"),
            "wpm": lead.get("wpm"), "cuts_per_10s": lead.get("cuts_per_10s"), "energy": lead.get("energy"),
        })
    return rows


# --------------------------------------------------------------------------- markdown

def render_md(data):
    """CHANNEL_DNA.md from the analysis dict."""
    chs = data["channels"]
    names = [c.get("name") or c["input"] for c in chs]
    L = ["# Channel DNA: %s" % " vs ".join(names), "",
         "Generated %s by `python -m vstudio channel` from public data (numbers as of that day). Settings: "
         "newest %d videos per format looked up in full, deep dive on the top %d per format%s." % (
             data["generated"][:10], data["settings"]["max_videos"], data["settings"]["deep"],
             "" if data["settings"]["deep"] else " (off: no video downloads)"), ""]
    multi = len(chs) > 1
    if multi:
        L += ["## Comparison", "",
              "| channel | platform | status | followers | long-form | Shorts | median long | median Short | uploads/wk "
              "| best topic | title chars | thumb text | wpm | cuts/10s | energy |",
              "| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- | ---: | ---: | ---: | ---: | ---: |"]
        for r in data["comparison"] or []:
            L.append("| %s | %s | %s | %s | %s | %s | %s | %s | %s | %s | %s | %s | %s | %s | %s |" % (
                _cell(r["name"], 30), PLATFORM_NAMES.get(r["platform"], r["platform"]), r["status"], _n(r["followers"]),
                r["long_listed"] or 0, r["short_listed"] or 0, _n(r["long_median"]), _n(r["short_median"]),
                _g(r["per_week"]), _cell(r["top_topic"] or "n/a", 30), _g(r["title_chars"]), _pct(r["thumb_text_share"]),
                r["wpm"] or "n/a", r["cuts_per_10s"] if r["cuts_per_10s"] is not None else "n/a",
                r["energy"] if r["energy"] is not None else "n/a"))
        L.append("")
        if data["files"].get("sheet"):
            L += ["![top outliers across channels](%s)" % data["files"]["sheet"], ""]
    for ch in chs:
        L += _render_channel(ch, "###" if multi else "##", data, multi)
    if data.get("gaps"):
        L += ["## Run notes", ""] + ["- %s" % g for g in data["gaps"]] + [""]
    return "\n".join(L)


def _render_channel(ch, h, data, multi):
    L = []
    title = ch.get("name") or ch["input"]
    if multi:
        L += ["## %s (%s)" % (title, PLATFORM_NAMES.get(ch["platform"], ch["platform"])), ""]
    L += ["%s: %s%s" % (PLATFORM_NAMES.get(ch["platform"], ch["platform"]), ch.get("url") or ch["input"],
                        "  |  %s followers" % _n(ch["followers"]) if ch.get("followers") else ""), ""]
    if ch["status"] not in ("ok", "partial"):
        L += ["**%s.** %s" % (ch["status_note"] or ch["status"], _status_help(ch)), ""]
        if ch.get("error"):
            L += ["yt-dlp said: `%s`" % _cell(ch["error"], 200), ""]
        L += _render_gaps(ch, h)
        return L
    L += ["%s Executive summary" % h, ""]
    L += ["- %s" % b for b in ch["summary"]] or ["- Not enough data for a summary."]
    L += [""]
    L += _render_formats(ch, h)
    L += _render_topics(ch, h)
    L += _render_titles(ch, h)
    L += _render_thumbs(ch, h, data)
    L += _render_dialogue(ch, h)
    L += _render_style(ch, h)
    L += ["%s How to compete with / replicate this channel" % h, ""]
    L += ["%d. %s" % (i, p) for i, p in enumerate(ch["playbook"], 1)] or ["Not enough data for a playbook."]
    L += [""]
    L += _render_gaps(ch, h)
    return L


def _status_help(ch):
    if ch["status"] == "needs_browser":
        tip = ("This profile only opens for a logged-in browser. Open it in your own browser (Claude in Chrome, "
               "with you present, at human speed) or paste the numbers; never share a password.")
        if ch["platform"] == "tiktok" and "/video/" not in ch["input"]:
            tip += (" For TikTok, passing one of their video links (https://www.tiktok.com/@name/video/...) "
                    "often works: the account is then listed through that video.")
        return tip
    if ch["status"] == "missing_tool":
        return "Install it and run again."
    if ch["status"] == "rate_limited":
        return "The platform asked us to slow down. Nothing was retried aggressively; run again later."
    return "Check the link, or run on a machine where the site is reachable."


def _render_formats(ch, h):
    L = ["%s Formats and cadence" % h, "",
         "| format | listed | median views | outliers 2x+ | best | engagement | views/day | median length "
         "| uploads/wk | last 10 vs previous 10 |",
         "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |"]
    cad = (ch.get("cadence") or {}).get("per_format") or {}
    for f, s in ch["formats"].items():
        t = s.get("trend")
        dur = s.get("median_duration_s")
        dur_s = "n/a" if dur is None else ("%d:%02d" % divmod(int(dur), 60))
        L.append("| %s | %d%s | %s | %s | %s | %s | %s | %s | %s | %s |" % (
            _fn(ch, f), s["listed"], "+" if (ch["inventory"].get(f) or {}).get("capped") else "",
            _n(s["median_views"]), "%d (%s)" % (s["outliers"], _pct(s["outlier_share"])) if s["median_views"] else "n/a",
            _x(s["best"][0]["multiple"]) if s["best"] else "n/a",
            "%s%%" % s["median_engagement"] if s.get("median_engagement") is not None else "n/a",
            _n(s.get("median_velocity")), dur_s, (cad.get(f) or {}).get("per_week") or "n/a",
            "%s %.2fx (%s vs %s)" % (t["direction"], t["ratio"], _n(t["last_median"]), _n(t["prev_median"])) if t else "n/a"))
    L += ["", "Engagement = (likes + 2 x comments) / views, as in the trend radar. A multiple is a video's views "
              "over the median of its own format on this channel.", ""]
    c = ch.get("cadence") or {}
    if c.get("dated"):
        days = ", ".join("%s %s" % (w["day"], _pct(w["share"])) for w in c["weekdays"] if w["count"])
        L.append("- Upload days (%d dated uploads): %s" % (c["dated"], days))
        if c.get("hours_utc"):
            L.append("- Upload hours (UTC): %s" % ", ".join("%02d:00 (%s)" % (x["hour"], _pct(x["share"])) for x in c["hours_utc"]))
        for f, p in cad.items():
            L.append("- %s: last upload %d days ago, %d in the last 30 days, median gap %s days, longest %s days" % (
                _fn(ch, f), p["last_upload_days_ago"], p["last_30_days"], _g(p["median_gap_days"]),
                _g(p["longest_gap_days"])))
    for f, s in ch["formats"].items():
        if s["best"]:
            L.append("- Best %s: %s" % (_fn(ch, f), "; ".join(
                "\"%s\" %s (%s views)" % (_cell(b["title"], 55), _x(b["multiple"]), _n(b["views"])) for b in s["best"][:3])))
        if s["worst"]:
            L.append("- Weakest %s: %s" % (_fn(ch, f), "; ".join(
                "\"%s\" %s" % (_cell(b["title"], 55), _x(b["multiple"])) for b in s["worst"])))
    L.append("")
    return L


def _render_topics(ch, h):
    L = ["%s Topics" % h, "",
         "Every video carries 1-3 topic tags (`videos[].topics` in CHANNEL_DNA.json).", "",
         "| topic | videos | share | formats | median multiple | median views | outliers | trend (recent third) | best video |",
         "| --- | ---: | ---: | --- | ---: | ---: | ---: | --- | --- |"]
    for t in ch["topics"]:
        fmts = ", ".join("%d %s" % (v, _fn(ch, k)) for k, v in sorted(t["formats"].items()))
        best = "\"%s\" %s" % (_cell(t["best"]["title"], 45), _x(t["best"]["multiple"])) if t.get("best") else "n/a"
        L.append("| %s | %d | %s | %s | %s | %s | %d | %s | %s |" % (
            _cell(t["label"], 30), t["videos"], _pct(t["share"]), fmts, _x(t["median_multiple"]),
            _n(t["median_views"]), t["outliers"], "%s (%d recent)" % (t["trend"], t["recent_videos"]) if t["trend"] else "n/a",
            best))
    L.append("")
    return L


def _render_titles(ch, h):
    ti = ch.get("titles") or {}
    L = ["%s Title patterns" % h, ""]
    for f, s in (ti.get("per_format") or {}).items():
        L.append("- %s: median %s characters, %s words, caps ratio %s" % (
            _fn(ch, f), _g(s["median_chars"]), _g(s["median_words"]), _g(s["median_caps_ratio"], 2)))
    if ti.get("winner_chars_range"):
        L.append("- Outlier titles run %d-%d characters (middle half)" % tuple(ti["winner_chars_range"]))
    if ti.get("formats_winners"):
        fmt_share = lambda xs: ", ".join("%s %s" % ("no named format" if x["name"] == "other" else x["name"],
                                                    _pct(x["share"])) for x in xs[:4])
        L.append("- Formats among outliers (trend radar's names): %s (all videos: %s)" % (
            fmt_share(ti["formats_winners"]), fmt_share(ti["formats_all"])))
    if ti.get("formulas_winners"):
        L.append("- Hook formulas among outliers: %s" % ", ".join(
            "%s %s" % (x["name"], _pct(x["share"])) for x in ti["formulas_winners"][:4]))
    over = ti.get("over_index") or {}
    L += [""]
    if over.get("traits"):
        L += ["Compared: %s (%d) vs the rest (%d).%s" % (over["rule"], over["winners"], over["rest"],
                                                        " Lift > 1 = more common among the winners." ), "",
              "| title has | winners | rest | lift |", "| --- | ---: | ---: | ---: |"]
        for t in over["traits"][:12]:
            L.append("| %s | %s | %s | %.2f |" % (t["label"], _pct(t["winners"]), _pct(t["rest"]), t["lift"]))
        L.append("")
    else:
        L += ["Too few videos with a baseline to compare winners and the rest.", ""]
    if ti.get("examples"):
        L += ["Top outlier titles (study the shape, write your own):", ""]
        L += ["- %s \"%s\" (%s views)" % (_x(e["multiple"]), _cell(e["title"], 90), _n(e["views"])) for e in ti["examples"]]
        L.append("")
    return L


def _render_thumbs(ch, h, data):
    th = ch.get("thumbnails")
    L = ["%s Thumbnail patterns" % h, ""]
    if not th or not th.get("analysed"):
        L += ["No long-form thumbnails analysed%s." % ("" if ch["platform"] == "youtube" else
                                                       " (thumbnails are read for YouTube long-form only)"), ""]
        return L
    sheet = ch.get("sheet")
    if sheet:
        L += ["![top outliers](%s)" % sheet, ""]
    w, a = th.get("winners"), th["all"]

    def row(name, fn):
        L.append("| %s | %s | %s |" % (name, fn(w) if w else "n/a", fn(a)))

    L += ["%d thumbnails measured; outliers = %s." % (th["analysed"], th["winners_rule"]), "",
          "| | outliers | all measured |", "| --- | --- | --- |"]
    row("thumbnails", lambda x: x["n"])
    for k, name, nd in (("brightness", "brightness (0-1)", 2), ("contrast", "contrast (0-1)", 2),
                        ("saturation", "saturation (0-1)", 2), ("colorfulness", "colourfulness (60+ vivid)", 0),
                        ("busyness", "busyness (0-100)", 0)):
        row(name, lambda x, k=k, nd=nd: _g(x["median"][k], nd))
    if th.get("ocr"):
        row("with text", lambda x: _pct(x["text_share"]))
        row("words of text", lambda x: _g(x["median_text_words"]))
        row("tallest text line", lambda x: "%s%% of height" % _g(x["median_text_line_pct"]) if x["median_text_line_pct"] else "n/a")
        row("text area", lambda x: _pct(x["median_text_area"]))
        row("text position", lambda x: _counts(x["text_positions"]))
        row("text colours (approx.)", lambda x: _counts(x["text_styles"], "; "))
    row("faces", lambda x: "%s, median size %s" % (_pct(x["face_share"]), _pct(x["median_face_size"]))
        if x["face_share"] is not None else "not measured (no face model)")
    row("dominant colour", lambda x: _counts(x["dominant_hues"]))
    row("subject sits", lambda x: _counts(x["subjects"]))
    row("palette", lambda x: " ".join("`%s`" % c for c in x["palette"][:6]))
    over = th.get("over_index") or {}
    if over.get("traits"):
        L += ["", "| thumbnail has | winners | rest | lift |", "| --- | ---: | ---: | ---: |"]
        for t in over["traits"][:12]:
            L.append("| %s | %s | %s | %.2f |" % (t["label"], _pct(t["winners"]), _pct(t["rest"]), t["lift"]))
    L += ["", "Pixels cannot judge expression, arrows, before/after or curiosity: add a short visual read after "
              "looking at the sheet.", ""]
    return L


def _render_dialogue(ch, h):
    L = ["%s Dialogue and energy" % h, ""]
    ok = [d for d in ch.get("deep") or [] if d.get("status") == "ok"]
    if not ok:
        L += ["No deep dive%s." % ("" if ch.get("deep") else " (run with --deep N and without --no-download)"), ""]
        return L
    L += ["| video | format | multiple | wpm (first 10 s) | word gap | pauses/min | words/sentence | re-hook every "
          "| energy | content type |",
          "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |"]
    for d in ok:
        dl, ws, en, ct = d.get("dialogue") or {}, d.get("word_spacing") or {}, d.get("energy") or {}, d.get("content_type") or {}
        L.append("| %s | %s | %s | %s | %s | %s | %s | %s | %s | %s |" % (
            _cell(d["title"], 40), _fn(ch, d["format"]), _x(d.get("multiple")),
            "%s (%s)" % (dl["wpm"], dl["wpm_first_10s"]) if dl else "n/a",
            "%ss" % ws["median_onset_gap_s"] if ws else "n/a", ws.get("pauses_per_min", "n/a") if ws else "n/a",
            (dl.get("sentences") or {}).get("mean_words", "n/a") if dl else "n/a",
            "%ss" % dl["rehooks"]["mean_gap_s"] if dl and dl["rehooks"]["mean_gap_s"] else "n/a",
            "%s (%s)" % (en["score"], en["label"]) if en else "n/a",
            "%s (%s)" % (ct.get("label"), _pct(ct.get("confidence"))) if ct else "n/a"))
    L += ["", "Word gap = median time between word starts; energy blends speech rate, cut rate, caption motion, "
              "loudness dynamics and brightness variation (components in the JSON).", ""]
    hooks = [d for d in ok if (d.get("hook") or {}).get("text")]
    if hooks:
        L += ["Hooks of the top videos (their words, for the shape only):", ""]
        for d in hooks:
            hk = d["hook"]
            extra = []
            if hk.get("formula"):
                extra.append(hk["formula"])
            if hk.get("score") is not None:
                extra.append("hook score %s %s" % (hk["score"], hk.get("verdict", "")))
            L.append("- %s: \"%s\"%s" % (_cell(d["title"], 40), _cell(hk["text"], 140),
                                         " (%s)" % ", ".join(extra) if extra else ""))
        L.append("")
    return L


def _render_style(ch, h):
    L = ["%s Caption and edit style" % h, ""]
    ok = [d for d in ch.get("deep") or [] if d.get("status") == "ok"]
    if not ok:
        L += ["Needs the deep dive.", ""]
        return L
    L += ["| video | captions | words/card | position | font look (look-alikes) | colours | cuts/10s | mean shot | camera |",
          "| --- | --- | ---: | --- | --- | --- | ---: | ---: | --- |"]
    for d in ok:
        c, e = d.get("captions") or {}, d.get("edit") or {}
        look = c.get("font_look") or {}
        L.append("| %s | %s | %s | %s | %s | %s | %s | %s | %s |" % (
            _cell(d["title"], 36), c.get("mode", "none"), (c.get("words_per_card") or {}).get("median", "n/a"),
            "%s%% down, %s" % (c["center_y_pct"], c.get("align")) if c else "n/a",
            "%s %s %s (%s)" % (look.get("weight"), look.get("width"), look.get("case"),
                               ", ".join((c.get("font_candidates") or [])[:3])) if look else "n/a",
            _caption_colours(c) if c else "n/a",
            e.get("cuts_per_10s", "n/a"), e.get("mean_shot_s", "n/a"),
            ", ".join(sorted((e.get("camera_moves") or {}).keys())) or "n/a"))
    L += [""]
    for d in ok:
        files = [d.get("style_dna"), d.get("fonts_sheet"), d.get("frames_sheet")]
        L.append("- %s: %s" % (_cell(d["title"], 40), ", ".join("`%s`" % f for f in files if f)))
    L += ["", "Each `style_dna.json` feeds captions.py and `vstudio autoedit --style`; the clip next to it "
              "(`analysis/source.mp4`) works as a `vstudio reel --ref` style reference.", ""]
    return L


def _render_gaps(ch, h):
    L = ["%s Data gaps" % h, ""]
    L += ["- %s" % g for g in ch["gaps"]] or ["- None noted."]
    L.append("")
    return L


# --------------------------------------------------------------------------- run

def _jsonable(o):
    if isinstance(o, dict):
        return {k: _jsonable(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_jsonable(v) for v in o]
    if isinstance(o, float) and (math.isnan(o) or math.isinf(o)):
        return None
    if hasattr(o, "item") and not isinstance(o, (str, bytes)):   # numpy scalars
        return o.item()
    return o


def analyze(urls, out_dir, max_videos=30, deep=4, fetch=None):
    """Analyse channels / profiles and write CHANNEL_DNA.json, CHANNEL_DNA.md, thumbs_sheet.jpg,
    thumbs/ and dna/<video_id>/ into out_dir. Returns the analysis dict (what the JSON holds).

    `fetch` is the network layer (see YtDlpFetch for its methods); None = the real one. A fetch
    object with `online = True` may also download the face model once.
    """
    out_dir = os.path.abspath(out_dir)
    os.makedirs(out_dir, exist_ok=True)
    fetch = fetch if fetch is not None else YtDlpFetch()
    online = bool(getattr(fetch, "online", False))
    run_gaps = []
    if online and hasattr(fetch, "available") and not fetch.available():
        run_gaps.append("yt-dlp is not installed (python -m pip install yt-dlp): nothing could be fetched.")
    from . import thumbs
    detector = thumbs.face_detector(allow_download=online)
    if detector is None:
        run_gaps.append("No face model: faces on thumbnails and in videos were not measured.")
    if thumbs.ocr_engine() is None:
        run_gaps.append("OCR is not installed (pip install rapidocr-onnxruntime): text on thumbnails and in videos was not read.")
    channels, sheet_names = [], set()
    for i, url in enumerate(urls, 1):
        log("channel %d/%d: %s" % (i, len(urls), url))
        try:
            ch = analyze_one(url, out_dir, fetch, max_videos=max_videos, deep=deep, detector=detector)
        except Exception as e:  # one bad link never sinks the others
            ch = _new_channel(url, platform_of(normalize_url(url)))
            ch["status"], ch["status_note"] = "failed", "analysis failed: %s" % str(e)[:200]
        ch["name"] = ch.get("name") or _handle_from_url(ch["input"])
        if ch["status"] in ("ok", "partial") and ch.get("thumbnails"):
            name = "thumbs_sheet.jpg" if len(urls) == 1 else "thumbs_sheet_%s.jpg" % slug(ch.get("name") or url)
            if name in sheet_names:   # two channels with the same name
                name = name.replace(".jpg", "_%d.jpg" % i)
            sheet_names.add(name)
            sheet = channel_sheet(ch, ch["videos"], os.path.join(out_dir, name), out_dir)
            ch["sheet"] = name if sheet else None
        channels.append(ch)
    sheet = None
    if len(urls) == 1:
        sheet = channels[0].get("sheet")
    else:
        sheet = _combined_sheet(channels, out_dir)
    data = {
        "version": 1,
        "generated": dt.datetime.now().isoformat(timespec="seconds"),
        "tool": "python -m vstudio channel",
        "inputs": list(urls),
        "settings": {"max_videos": max_videos, "deep": deep},
        "channels": channels,
        "comparison": comparison(channels) if len(channels) > 1 else None,
        "gaps": run_gaps,
        "files": {"json": "CHANNEL_DNA.json", "md": "CHANNEL_DNA.md", "sheet": sheet},
    }
    data = _jsonable(data)
    with open(os.path.join(out_dir, "CHANNEL_DNA.json"), "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1, ensure_ascii=False)
    with open(os.path.join(out_dir, "CHANNEL_DNA.md"), "w", encoding="utf-8") as f:
        f.write(render_md(data))
    return data


def _combined_sheet(channels, out_dir):
    """thumbs_sheet.jpg for several channels: the top outliers across all of them, labelled by channel."""
    from . import thumbs
    rows = []
    for ch in channels:
        for r in ch.get("videos") or []:
            if r.get("thumb") and r.get("multiple") is not None:
                rows.append((ch.get("name") or "", r))
    rows.sort(key=lambda x: -x[1]["multiple"])
    rows = rows[:SHEET_TILES]
    if not rows:
        return None
    files = [os.path.join(out_dir, r["thumb"]["path"]) for _, r in rows]
    labels = ["%s  %s views  %s" % (_x(r["multiple"]), _n(r.get("views")), name) + "\n" + (r.get("title") or "")
              for name, r in rows]
    return "thumbs_sheet.jpg" if thumbs.sheet(files, os.path.join(out_dir, "thumbs_sheet.jpg"), labels,
                                              title="Long-form thumbnails across channels, ranked by multiple") else None


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m vstudio channel", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("urls", nargs="+", help="YouTube channel (URL, @handle or a video link), Instagram or TikTok profile")
    ap.add_argument("--out-dir", help="output folder (default workspace/channels/<first channel>)")
    ap.add_argument("--max-videos", type=int, default=30, help="newest videos per format to look up in full (default 30)")
    ap.add_argument("--deep", type=int, default=4, help="top N Shorts and long-form videos to download and read (default 4)")
    ap.add_argument("--no-download", action="store_true", help="metadata and thumbnails only: no video downloads")
    a = ap.parse_args(argv)
    if a.max_videos < 1 or a.deep < 0:
        ap.error("--max-videos must be >= 1 and --deep >= 0")
    name = slug(_handle_from_url(normalize_url(a.urls[0]))) + ("-compare" if len(a.urls) > 1 else "")
    out_dir = a.out_dir or os.path.join(paths.WORKSPACE, "channels", name)
    deep = 0 if a.no_download else a.deep
    data = analyze(a.urls, out_dir, max_videos=a.max_videos, deep=deep)
    print(os.path.join(out_dir, "CHANNEL_DNA.md"))
    for ch in data["channels"]:
        print("\n%s [%s]" % (ch.get("name") or ch["input"], ch["status"]))
        if ch["status"] not in ("ok", "partial"):
            print("  %s" % ch.get("status_note"))
        for b in ch.get("summary") or []:
            print("  - %s" % b)
    return 0 if any(ch["status"] in ("ok", "partial") for ch in data["channels"]) else 1
