"""
director.py - the reel director: a prompt, reference videos (the style to
copy), competitor videos (the bar to beat) and the creator's own assets in;
a brief, a plan, a timeline, a rendered reel and a QA report out.

    python -m vstudio reel --prompt "3 settings costing you views" --ref ref.mp4 \
        --competitor https://youtube.com/shorts/... --asset broll.mp4 --duration 30 --preview
    python -m vstudio reel --plan DIR/plan.json --out-dir DIR        (agent mode: you wrote the plan)

    make_reel(prompt, refs=[...], competitors=[...], assets=[...], out_dir="workspace/reels/x")

The five stages (each prints one status line, as PROGRESS n% for the studio):

 1. Analyze every input. A link is downloaded with yt-dlp (one video, mp4,
    plus its captions when the platform has them) into DIR/inputs, with the
    link recorded next to it (a re-run with the same link reuses the file,
    a new link replaces it); a file is used in place (a caption file next
    to it with the same name, .srt, .vtt, .json3 or .words.json, is its
    transcript); a folder or a style_dna.json from an earlier analysis is
    reused as it is. viral-extract (extract.py, run as a subprocess)
    measures each one into DIR/analysis/<ref|competitor>N/: on-screen text
    (caption font look, size, position, colours, words per card) and
    dialogue (pace, hook, re-hooks); ReelMimic's analyze.py, run
    alongside, gives the edit (cuts, shots, camera, BPM, look), read here
    by edit_from_report().
 2. BRIEF.md: style targets merged from the references (cut rate, shot
    length, caption style -> plan.style.caption_style, font, palette from
    the footage's dominant colours and the caption colours, words per
    minute, hook timing, text density, energy), a competitor summary (their
    hooks, structure, length) with what to beat, and the scriptcheck score
    of the creator's script when there is one. DIR/analysis.json keeps the
    numbers for later runs.
 3. Plan. With Claude (llm "claude", or "auto" when Claude Code is
    installed): one JSON call with the director's rules, the PLAN schema, the
    template catalog, the brief, the reference and competitor transcripts
    (truncated), the prompt, script and asset list; the answer must pass
    validate_plan, compile_plan and timeline.validate, keep the creator's
    script, use no number the creator did not give and copy no competitor
    line, or it goes back with the problems listed. Otherwise (or after
    repeated failures in auto mode) the offline heuristic_plan. With --plan
    the given plan is used as it is. The exact planning prompt is kept in
    DIR/plan_prompt.md.
 4. Render DIR/reel.mp4 (preview = half size and frame rate).
 5. QA.md: the reel is re-measured with extract.py and compared with the
    reference targets (duration, hook timing, cuts per 10 s, shot length,
    caption size, position, words per card, case and colours, words per
    minute, text inside the safe zone) as pass / warn, plus a contact sheet
    of stills (render.still at each beat) in DIR/qa/ for a visual review.

References and competitors are only measured: their footage, audio and
words never go into the reel (backgrounds may only be the given assets).
An asset that cannot be decoded (a damaged, unfinished or renamed file) is
skipped with a note instead of failing the render. --music and --voice may
be audio or video files (their sound is used); a voiceover's words come
from a captions or words file next to it (transcribe.load_words: SRT, VTT,
json3, {w,s,e} / {word,start,end} lists, whisper segments) or faster-whisper.

Limits: measuring needs ffmpeg (and OCR for text); downloads need yt-dlp and
a platform that serves the video without a login; a transcript needs a
caption file, platform captions or faster-whisper. Whatever cannot be
measured is reported as such in BRIEF.md, never guessed.
"""

import argparse
import collections
import colorsys
import concurrent.futures
import importlib.util
import json
import math
import os
import re
import shutil
import statistics
import subprocess
import sys
import threading
import time

from . import paths, planner, sfx, templates, timeline
from . import llm as llm_module

URL_RE = re.compile(r"^https?://", re.I)
CAPTION_SIDECARS = (".words.json", ".en.json3", ".json3", ".en.srt", ".srt", ".en.vtt", ".vtt")
TRANSCRIPT_CHARS = 1600          # per input, in the planning prompt
ANALYSIS_VERSION = 2


class DirectorError(RuntimeError):
    """A reel could not be made (bad input, nothing to plan from, render failure)."""


def _say(log, msg, pct=None):
    if log:
        log(("PROGRESS %d%% " % pct if pct is not None else "") + msg)


def _slug(text, n=40):
    s = re.sub(r"[^a-z0-9]+", "-", str(text or "").lower()).strip("-")
    return (s[:n].rstrip("-") or "reel")


def _read_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _write_json(path, data):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".part"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, path)


def _write_text(path, text):
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def _num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) else None


def _median(vals):
    vals = [v for v in (_num(x) for x in vals) if v is not None]
    return round(statistics.median(vals), 2) if vals else None


def _fmt(v, unit="", nd=1):
    if v is None:
        return "not measured"
    if isinstance(v, float):
        v = round(v, nd)
    return "%s%s" % (v, unit)


# =========================================================================== 1. inputs

def _sidecar(video):
    """A transcript file next to a video (same name), or None."""
    stem = os.path.splitext(video)[0]
    for ext in CAPTION_SIDECARS:
        if os.path.isfile(stem + ext):
            return stem + ext
    return None


def _have_ytdlp():
    return importlib.util.find_spec("yt_dlp") is not None


def download(url, dest_stem, log=None, timeout=900):
    """One video from a link as mp4 (yt-dlp), plus its captions when available: (video, captions or None).

    The files are kept as <dest_stem>.mp4 (and .vtt/.srt/.json3) with the
    link in <dest_stem>.source.json, so a re-run with the same link reuses
    them (captions are fetched once) and a different link replaces them.
    """
    folder = os.path.dirname(dest_stem)
    os.makedirs(folder, exist_ok=True)
    base = os.path.basename(dest_stem)
    source_file = dest_stem + ".source.json"

    def found(exts):
        hits = [os.path.join(folder, f) for f in sorted(os.listdir(folder))
                if f.startswith(base + ".") and f.lower().endswith(exts) and not f.endswith(".part")]
        return hits[0] if hits else None

    source = _read_json(source_file, {}) or {}
    if not isinstance(source, dict) or source.get("url") != url:
        # files from another link (or from a version that did not record it): never measure those for this one
        for f in os.listdir(folder):
            if f.startswith(base + "."):
                try:
                    os.remove(os.path.join(folder, f))
                except OSError:
                    pass
        source = {"url": url}
    video = found(planner.VIDEO_EXT)
    caps = found((".json3", ".vtt", ".srt"))
    if video and (caps or source.get("captions_tried") or not _have_ytdlp()):
        return video, caps
    if not _have_ytdlp():
        raise DirectorError("downloading links needs yt-dlp (pip install yt-dlp); or download the video yourself "
                            "and pass the file")
    if not video:
        _say(log, "  downloading %s" % url)
        cmd = [sys.executable, "-m", "yt_dlp", "--no-playlist", "--no-warnings", "--quiet", "-f",
               "bv*[height<=1080][ext=mp4]+ba[ext=m4a]/b[height<=1080][ext=mp4]/bv*[height<=1080]+ba/b",
               "--merge-output-format", "mp4", "-o", dest_stem + ".%(ext)s", url]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
        except subprocess.TimeoutExpired:
            raise DirectorError("download timed out: %s" % url)
        video = found(planner.VIDEO_EXT)
        if r.returncode != 0 or not video:
            msg = (r.stderr or r.stdout or "").strip().splitlines()
            raise DirectorError("could not download %s: %s" % (url, msg[-1] if msg else "no file"))
        _write_json(source_file, source)
    if not caps:
        cmd = [sys.executable, "-m", "yt_dlp", "--no-playlist", "--no-warnings", "--quiet", "--skip-download",
               "--write-subs", "--write-auto-subs", "--sub-langs", "en.*,en", "--sub-format", "json3/vtt/srt/best",
               "-o", dest_stem + ".%(ext)s", url]
        try:
            subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180)
            source["captions_tried"] = True     # a video without captions is not asked again on every run
        except subprocess.TimeoutExpired:
            pass
        caps = found((".json3", ".vtt", ".srt"))
        _write_json(source_file, source)
    return video, caps


def _run(cmd, timeout, cwd):
    """(ok, error tail) of a measuring subprocess."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
                           cwd=cwd)
    except subprocess.TimeoutExpired:
        return False, "timed out after %d s" % timeout
    return r.returncode == 0, (r.stderr or r.stdout or "").strip()[-600:]


def edit_from_report(report):
    """The edit DNA (cuts, shots, camera, BPM, look) from ReelMimic's analysis report.json.

    Same fields as extract.py's edit section; reads both report layouts
    (`shots` as a list of [start, end] with `pacing`, or as a summary dict).
    """
    if not isinstance(report, dict) or not report:
        return None
    dur = _num((report.get("video") or {}).get("duration")) or _num(report.get("duration"))
    shots = report.get("shots")
    pacing = report.get("pacing") or {}
    details = report.get("shot_details") or []
    if isinstance(shots, list):
        spans = [s for s in shots if isinstance(s, (list, tuple)) and len(s) == 2]
        count = len(spans) or (1 if dur else None)
        cuts = [float(s[0]) for s in spans[1:]]
        mean = _num(pacing.get("mean_shot_s")) or (round(dur / count, 2) if dur and count else None)
    else:
        shots = shots if isinstance(shots, dict) else {}
        count, mean = shots.get("count"), shots.get("mean_shot_s")
        cuts = [float(d["start"]) for d in details[1:] if _num(d.get("start")) is not None]
    beats = (report.get("audio") or {}).get("beats") or []
    on_beat = None
    if cuts and beats:
        on_beat = round(sum(1 for c in cuts if min(abs(c - b) for b in beats) <= 0.08) / len(cuts), 2)
    moves = collections.Counter(k for d in details for k in ((d.get("camera") or {}).get("kind") or []))
    return {"duration_s": dur, "shots": count, "mean_shot_s": mean,
            "cuts_per_10s": round(len(cuts) / dur * 10, 1) if dur else None,
            "first_cut_s": round(cuts[0], 2) if cuts else None, "cuts_on_beat": on_beat,
            "camera_moves": dict(moves), "bpm": (report.get("audio") or {}).get("bpm"),
            "look": report.get("look_summary"), "flashes": len(report.get("flashes") or [])}


def run_extract(video, dna_dir, captions=None, log=None, timeout=1800):
    """Measure one video into dna_dir with viral-extract; returns the style_dna.json path or raises DirectorError.

    The text and dialogue come from extract.py; the edit (cuts, camera, BPM,
    look) from ReelMimic's analyze.py, run here directly so a failure there
    (or a report layout extract.py does not expect) never costs the rest. A
    soundtrack the beat tracker cannot read is retried on a muted copy, and a
    whisper failure on a run without captions is retried without whisper.
    """
    os.makedirs(dna_dir, exist_ok=True)
    dur = planner.media_duration(video) or 60.0
    fps = "4" if dur <= 90 else "2" if dur <= 300 else "1"
    base = [sys.executable, paths.skill_path("extract"), video, "--out", dna_dir, "--fps", fps, "--skip-edit"]
    if captions:
        base += ["--captions", captions]
    attempts = [base + ["--no-whisper"]] if captions or os.environ.get("VSTUDIO_NO_WHISPER") else \
        [base, base + ["--no-whisper"]]      # whisper can fail (no model download): measure without it
    out, err = os.path.join(dna_dir, "style_dna.json"), ""
    for cmd in attempts:
        ok, err = _run(cmd, timeout, dna_dir)
        if ok and os.path.isfile(out):
            break
    else:
        raise DirectorError("could not analyze %s: %s" % (video, err or "extract.py failed"))
    # the edit
    adir = os.path.join(dna_dir, "analysis")
    analyze = os.path.join(paths.SKILLS, "reel-mimic", "scripts", "analyze.py")
    report = None
    if os.path.isfile(analyze):
        ok, err = _run([sys.executable, analyze, video, "--out", adir], timeout, dna_dir)
        if not ok and shutil.which("ffmpeg"):
            muted = os.path.join(dna_dir, "_muted" + os.path.splitext(video)[1])
            if _run(["ffmpeg", "-v", "error", "-y", "-i", video, "-an", "-c:v", "copy", muted], 300, dna_dir)[0]:
                ok, err = _run([sys.executable, analyze, muted, "--out", adir], timeout, dna_dir)
                try:
                    os.remove(muted)
                except OSError:
                    pass
        report = _read_json(os.path.join(adir, "report.json")) if ok else None
    dna = _read_json(out, {}) or {}
    dna["edit"] = edit_from_report(report)
    if not dna["edit"]:
        _say(log, "  edit analysis unavailable for %s: %s" % (os.path.basename(video), (err or "")[-160:]))
    _write_json(out, dna)
    return out


def _load_dna_dir(d):
    dna = _read_json(os.path.join(d, "style_dna.json"), {}) or {}
    words = _read_json(os.path.join(d, "transcript.json"))
    report = _read_json(os.path.join(d, "analysis", "report.json"), {}) or {}
    if not dna.get("edit") and report:
        dna["edit"] = edit_from_report(report)      # an older analysis whose edit section did not come through
    video = None
    for name in ("source.mp4", "proxy.mp4"):
        p = os.path.join(d, "analysis", name)
        if os.path.isfile(p):
            video = p
            break
    return dna, words if isinstance(words, list) else None, report, video


def analyze_input(src, role, index, out_dir, log=None):
    """Measure one reference or competitor: {role, src, name, video, dna_dir, dna, words, report, error}."""
    name = "%s%d" % (role, index)
    item = {"role": role, "src": src, "name": name, "video": None, "dna_dir": None, "dna": None, "words": None,
            "report": {}, "error": None}
    try:
        if os.path.isdir(src) and os.path.isfile(os.path.join(src, "style_dna.json")):
            d = src                                    # an earlier analysis: reuse it
        elif os.path.isfile(src) and os.path.basename(src).lower() == "style_dna.json":
            d = os.path.dirname(os.path.abspath(src))
        else:
            d = os.path.join(out_dir, "analysis", name)
            stamp = {"src": src, "version": ANALYSIS_VERSION}
            if URL_RE.match(src):
                video, caps = download(src, os.path.join(out_dir, "inputs", name), log)
            elif os.path.isfile(src):
                video, caps = os.path.abspath(src), _sidecar(src)
                st = os.stat(video)
                stamp.update({"size": st.st_size, "mtime": round(st.st_mtime, 3)})
            else:
                raise DirectorError("not a file, folder or link: %s" % src)
            if _read_json(os.path.join(d, "source.json")) != stamp or not os.path.isfile(os.path.join(d, "style_dna.json")):
                _say(log, "  measuring %s (%s)" % (name, os.path.basename(video)))
                run_extract(video, d, caps, log)
                _write_json(os.path.join(d, "source.json"), stamp)
            item["video"] = video
        dna, words, report, vid = _load_dna_dir(d)
        item.update({"dna_dir": d, "dna": dna, "words": words, "report": report, "video": item["video"] or vid})
    except DirectorError as e:
        item["error"] = str(e)
    return item


def analyze_inputs(refs, competitors, out_dir, log=None, workers=3):
    """Measure every input (a few at a time); returns (refs, competitors) lists of analyze_input items."""
    jobs = [(s, "ref", i + 1) for i, s in enumerate(refs)] + [(s, "competitor", i + 1) for i, s in enumerate(competitors)]
    if not jobs:
        return [], []
    results = {}
    lock = threading.Lock()

    def safe_log(msg):              # one whole line at a time from the worker threads
        if log:
            with lock:
                log(msg)

    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, min(workers, len(jobs)))) as ex:
        futs = {ex.submit(analyze_input, s, role, i, out_dir, safe_log): (role, i) for s, role, i in jobs}
        for f in concurrent.futures.as_completed(futs):
            results[futs[f]] = f.result()
    return ([results[("ref", i + 1)] for i in range(len(refs))],
            [results[("competitor", i + 1)] for i in range(len(competitors))])


# =========================================================================== 2. targets and brief

def _hex_rgb(c):
    c = str(c).lstrip("#")
    return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))


def _lum(c):
    r, g, b = (v / 255.0 for v in _hex_rgb(c))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _hsv(c):
    return colorsys.rgb_to_hsv(*(v / 255.0 for v in _hex_rgb(c)))


def _dist(a, b):
    return math.sqrt(sum((x - y) ** 2 for x, y in zip(_hex_rgb(a), _hex_rgb(b))))


def _shade(c, k):
    """Colour c scaled toward black (k < 1) or white (k > 1)."""
    rgb = _hex_rgb(c)
    if k <= 1:
        out = [v * k for v in rgb]
    else:
        out = [v + (255 - v) * (k - 1) for v in rgb]
    return "#%02X%02X%02X" % tuple(int(max(0, min(255, round(v)))) for v in out)


def palette(refs):
    """Accent, backgrounds and text colour from the references' caption colours and footage, or {}."""
    weights = collections.Counter()
    for r in refs:
        for shot in (r.get("report") or {}).get("shot_details") or []:
            dur = max(0.1, (_num(shot.get("end")) or 0) - (_num(shot.get("start")) or 0))
            for k, c in enumerate(((shot.get("look") or {}).get("dominant") or [])[:4]):
                if isinstance(c, str) and re.match(r"^#[0-9A-Fa-f]{6}$", c):
                    weights[c.upper()] += dur * (0.4, 0.3, 0.2, 0.1)[k]
    cap = caption_track(refs) or {}
    out, why = {}, {}
    accents = []
    for key in ("highlight", "box"):
        c = cap.get(key)
        if isinstance(c, str) and re.match(r"^#[0-9A-Fa-f]{6}$", c):
            h, s, v = _hsv(c)
            if s >= 0.35 and v >= 0.35:
                accents.append((c.upper(), "the reference's caption %s colour" % key))
    vivid = [(c, w) for c, w in weights.most_common() if _hsv(c)[1] >= 0.45 and _hsv(c)[2] >= 0.45]
    if vivid:
        accents.append((vivid[0][0], "the most vivid recurring colour in the reference footage"))
    dark = [c for c, _ in weights.most_common() if _lum(c) < 0.16]
    if dark:
        bg = _shade(dark[0], 0.85)
        out["bg"], why["bg"] = bg, "the darkest recurring colour in the reference footage"
        out["bg2"] = _shade(bg, 1.12) if _lum(bg) < 0.05 else _shade(bg, 0.6)
        why["bg2"] = "a step off the background"
    if accents:
        bg = out.get("bg") or templates.DEFAULT_STYLE["bg"]
        for c, reason in accents:
            if _dist(c, bg) > 120:
                out["accent"], why["accent"] = c, reason
                break
    fill = cap.get("fill")
    if isinstance(fill, str) and re.match(r"^#[0-9A-Fa-f]{6}$", fill) and _lum(fill) > 0.6:
        out["text"], why["text"] = fill.upper(), "the reference's caption fill"
    if out:
        out["_why"] = why
        out["_dominant"] = [c for c, _ in weights.most_common(6)]
    return out


def caption_track(refs):
    """The reference caption track with the most cards (extract.py's text.caption_track), or None."""
    best = None
    for r in refs:
        cap = (((r.get("dna") or {}).get("text") or {}).get("caption_track"))
        if isinstance(cap, dict) and (best is None or (cap.get("cards") or 0) > (best.get("cards") or 0)):
            best = cap
    return best


def _font_installed(name):
    try:
        from . import fonts
        info = fonts.info(name)
        fam = fonts.parse_name(name)[0]
    except Exception:
        return False
    key = lambda s: re.sub(r"[^a-z0-9]", "", str(s).lower())
    return bool(info) and key(info.get("family")) == key(fam)


def choose_font(candidates, install=True, log=None):
    """The first candidate font that is installed (installing the top one from Google Fonts if needed), or None."""
    for c in candidates or []:
        if _font_installed(c):
            return c
    if install and candidates:
        try:
            from . import fonts
            fam = fonts.parse_name(candidates[0])[0]
            _say(log, "  installing font %s (Google Fonts, open licence)" % fam)
            fonts.install([fam])
            if _font_installed(candidates[0]):
                return candidates[0]
        except Exception as e:
            _say(log, "  could not install %s: %s" % (candidates[0], str(e).splitlines()[0][:120]))
    return None


def style_targets(refs, log=None, install_fonts=True):
    """What the reel should match, merged from the measured references: numbers plus a plan style."""
    ok = [r for r in refs if r.get("dna")]
    edits = [(r["dna"].get("edit") or {}) for r in ok]
    dias = [(r["dna"].get("dialogue") or {}) for r in ok]
    texts = [(r["dna"].get("text") or {}) for r in ok]
    looks = [((r.get("report") or {}).get("look_summary") or {}) for r in ok]
    t = {
        "refs": len(ok),
        "duration_s": _median(e.get("duration_s") for e in edits),
        "cuts_per_10s": _median(e.get("cuts_per_10s") for e in edits),
        "mean_shot_s": _median(e.get("mean_shot_s") for e in edits),
        "first_cut_s": _median(e.get("first_cut_s") for e in edits),
        "bpm": _median(e.get("bpm") for e in edits),
        "flashes": _median(e.get("flashes") for e in edits),
        "camera_moving_share": _median(l.get("moving_camera_shots") for l in looks),
        "wpm": _median(d.get("wpm") for d in dias),
        "time_to_first_word_s": _median(d.get("time_to_first_word_s") for d in dias),
        "rehook_gap_s": _median((d.get("rehooks") or {}).get("mean_gap_s") for d in dias),
        "sentence_words": _median((d.get("sentences") or {}).get("mean_words") for d in dias),
        "first_text_s": _median(x.get("first_text_s") for x in texts),
        "text_coverage": _median(x.get("coverage") for x in texts),
        "hooks": [((d.get("hook") or {}).get("first_sentence") or "") for d in dias if d],
    }
    cap = caption_track(ok)
    style = {}
    if cap:
        cs = {k: cap.get(k) for k in timeline.CAPTION_STYLE_KEYS if k in cap}
        style["caption_style"] = cs
        t["caption"] = {k: cap.get(k) for k in ("mode", "words_per_card", "font_size_pct", "center_y_pct", "case",
                                                "fill", "highlight", "outline", "box", "font_candidates", "font_look")}
        font = choose_font(cap.get("font_candidates") or [], install_fonts, log)
        if font:
            style["font"] = font
            style["caption_font"] = font
        t["font"] = font
    pal = palette(ok)
    for k in ("accent", "bg", "bg2", "text"):
        if pal.get(k):
            style[k] = pal[k]
    t["palette"] = {k: v for k, v in pal.items()}
    if t["mean_shot_s"]:
        style["cut_every"] = round(max(0.5, min(8.0, t["mean_shot_s"])), 2)
    if t["wpm"] and 100 <= t["wpm"] <= 280:
        style["wpm"] = int(round(t["wpm"]))
    # energy: how fast it cuts and talks, and how much the camera moves
    score, n = 0.0, 0
    if t["cuts_per_10s"] is not None:
        score += min(1.0, t["cuts_per_10s"] / 5.0)
        n += 1
    if t["wpm"] is not None:
        score += min(1.0, max(0.0, (t["wpm"] - 120) / 90.0))
        n += 1
    if t["camera_moving_share"] is not None:
        score += t["camera_moving_share"]
        n += 1
    if n:
        e = score / n
        style["energy"] = "high" if e >= 0.55 else "medium" if e >= 0.3 else "calm"
        t["energy_score"] = round(e, 2)
    t["style"] = style
    return t


def competitor_summary(comps):
    """Per competitor: length, hook, pace, cuts, CTA; plus 'what to beat' lines drawn from the numbers."""
    rows, beat = [], []
    for c in comps:
        dna = c.get("dna") or {}
        d, e, x = dna.get("dialogue") or {}, dna.get("edit") or {}, dna.get("text") or {}
        rows.append({
            "name": c["name"], "src": c["src"], "error": c.get("error"),
            "duration_s": e.get("duration_s"), "cuts_per_10s": e.get("cuts_per_10s"), "mean_shot_s": e.get("mean_shot_s"),
            "first_cut_s": e.get("first_cut_s"), "wpm": d.get("wpm"), "first_word_s": d.get("time_to_first_word_s"),
            "first_text_s": x.get("first_text_s"), "hook": (d.get("hook") or {}).get("first_sentence") or "",
            "sentences": (d.get("sentences") or {}).get("count"), "cta": (d.get("cta") or {}).get("asks"),
            "loop": d.get("loop_ending"), "text_coverage": x.get("coverage"),
            "caption_mode": (x.get("caption_track") or {}).get("mode"),
        })
    ok = [r for r in rows if not r["error"]]
    if not ok:
        return rows, beat
    waits = [v for v in (r["first_word_s"] for r in ok) if _num(v) is not None] + \
        [v for v in (r["first_text_s"] for r in ok) if _num(v) is not None]
    if waits and min(waits) > 0.4:
        beat.append("They make viewers wait %.1f s or more for the first word or text: put the hook on frame 1."
                    % min(waits))
    hooks = [len(r["hook"].split()) for r in ok if r["hook"]]
    if hooks:
        beat.append("Their opening sentence runs %d words: land yours in %d or fewer." % (min(hooks), max(4, min(hooks) - 2)))
    w = _median(r["wpm"] for r in ok)
    if w:
        beat.append("They talk at about %d wpm: match it or go slightly faster, never slower." % w)
    cr = _median(r["cuts_per_10s"] for r in ok)
    if cr is not None:
        beat.append("They cut %.1f times per 10 s: keep every shot as short or shorter." % cr)
    du = _median(r["duration_s"] for r in ok)
    if du:
        beat.append("They run about %.0f s: say the same in that time or less." % du)
    if any(not r["cta"] for r in ok):
        beat.append("At least one never asks for anything at the end: close with one clear ask.")
    if any(r["loop"] is False for r in ok):
        beat.append("Their endings do not loop back to the hook: make the last line lead into the first.")
    return rows, beat


def score_script(text, seconds, wpm):
    """scriptcheck's score of a script (dict), or None when the skill cannot load."""
    try:
        sc = paths.load_skill("scriptcheck")
        return sc.score_script(text, seconds, wpm, None)
    except Exception:
        return None


def brief_md(prompt, targets, comps, beat_lines, inputs, script_score, assets, duration, aspect, notes):
    """BRIEF.md text."""
    L = ["# Brief", "", "**Prompt:** %s" % (prompt or "(none: plan given)"), "",
         "**Format:** %s, %s s target" % (aspect, _fmt(duration, "", 1)), ""]
    L += ["## Inputs", ""]
    if not inputs:
        L += ["No reference or competitor videos: the reel follows the default style.", ""]
    for it in inputs:
        status = "error: %s" % it["error"] if it.get("error") else "measured in `%s`" % it.get("dna_dir")
        L.append("- %s `%s`: %s" % (it["name"], it["src"], status))
    if assets:
        L += ["", "Assets (backgrounds the plan may use):", ""]
        L += ["- `%s` (%s%s)" % (a["path"], a["kind"], ", %.1f s" % a["duration"] if a.get("duration") else "")
              for a in assets]
    L.append("")
    st = targets.get("style") or {}
    L += ["## Style targets (from the references)", ""]
    if not targets.get("refs"):
        L += ["No reference could be measured, so there is nothing to match: the plan uses the house style.", ""]
    else:
        rows = [("Length", _fmt(targets.get("duration_s"), " s")),
                ("Cuts per 10 s", _fmt(targets.get("cuts_per_10s"))),
                ("Mean shot", _fmt(targets.get("mean_shot_s"), " s")),
                ("First cut", _fmt(targets.get("first_cut_s"), " s")),
                ("Music tempo", _fmt(targets.get("bpm"), " BPM", 0)),
                ("Camera moving", "%d%% of shots" % round(targets["camera_moving_share"] * 100)
                 if targets.get("camera_moving_share") is not None else "not measured"),
                ("Pace", _fmt(targets.get("wpm"), " wpm", 0)),
                ("First word", _fmt(targets.get("time_to_first_word_s"), " s")),
                ("First on-screen text", _fmt(targets.get("first_text_s"), " s")),
                ("Text on screen", "%d%% of the video" % round(targets["text_coverage"] * 100)
                 if targets.get("text_coverage") is not None else "not measured"),
                ("Re-hook every", _fmt(targets.get("rehook_gap_s"), " s")),
                ("Words per sentence", _fmt(targets.get("sentence_words"))),
                ("Energy", "%s (score %s)" % (st.get("energy"), targets.get("energy_score"))
                 if st.get("energy") else "not measured")]
        L += ["| Metric | Target |", "| --- | --- |"] + ["| %s | %s |" % r for r in rows] + [""]
        cap = targets.get("caption")
        if cap:
            wpc = cap.get("words_per_card")
            wpc = wpc.get("median") if isinstance(wpc, dict) else wpc
            L += ["**Captions:** %s, %s words per card, font size %s%% of the height, centred %s%% down, %s case; "
                  "fill %s, highlight %s, outline %s, box %s. Font look %s; candidates %s; using **%s**." % (
                      cap.get("mode"), wpc, cap.get("font_size_pct"), cap.get("center_y_pct"), cap.get("case"),
                      cap.get("fill"), cap.get("highlight"), cap.get("outline"), cap.get("box"),
                      json.dumps(cap.get("font_look")), ", ".join(cap.get("font_candidates") or []) or "none",
                      targets.get("font") or "the default font (no candidate installed)"), ""]
        else:
            L += ["**Captions:** no caption track measured (no text found, or OCR is not installed): default style.", ""]
        pal = targets.get("palette") or {}
        if pal.get("_why"):
            L += ["**Palette:** " + "; ".join("%s %s (%s)" % (k, pal[k], why) for k, why in pal["_why"].items()),
                  "Dominant colours: " + ", ".join(pal.get("_dominant") or []), ""]
        hooks = [h for h in targets.get("hooks") or [] if h]
        if hooks:
            L += ["**Reference hooks (rhythm only):** " + " / ".join('"%s"' % h for h in hooks[:3]), ""]
        L += ["Plan style from these: `%s`" % json.dumps({k: v for k, v in st.items() if k != "caption_style"}), ""]
    L += ["## Competitors (the bar to beat; nothing of theirs is reused)", ""]
    if not comps:
        L += ["None given.", ""]
    else:
        L += ["| | Length | Cuts/10 s | Pace | First word | Hook | CTA |", "| --- | --- | --- | --- | --- | --- | --- |"]
        for r in comps:
            if r.get("error"):
                L.append("| %s | could not be measured: %s | | | | | |" % (r["name"], r["error"][:80]))
                continue
            L.append("| %s | %s | %s | %s | %s | %s | %s |" % (
                r["name"], _fmt(r["duration_s"], " s"), _fmt(r["cuts_per_10s"]), _fmt(r["wpm"], " wpm", 0),
                _fmt(r["first_word_s"], " s"), (r["hook"] or "no transcript")[:70].replace("|", "/"),
                ", ".join(r["cta"] or []) or "none"))
        L.append("")
        if beat_lines:
            L += ["What to beat:", ""] + ["- " + b for b in beat_lines] + [""]
    if script_score:
        L += ["## Script score (scriptcheck)", "",
              "%s / 100, %s%s" % (script_score["overall"], script_score["verdict"],
                                  " (red: %s)" % ", ".join(script_score["red"]) if script_score.get("red") else ""), ""]
        for k, v in script_score["checks"].items():
            L.append("- %s: %s (%s)" % (k, v["score"] if v["score"] is not None else "n/a", v["detail"]))
        if script_score["overall"] < 80:
            L += ["", "Under 80: run `/script-forge` on it before the final render."]
        L.append("")
    if notes:
        L += ["## Notes", ""] + ["- " + n for n in notes] + [""]
    return "\n".join(L)


# =========================================================================== 3. planning

_CALL = {"type": "object", "required": ["template", "params"],
         "properties": {"template": {"type": "string"}, "params": {"type": "object"},
                        "start": {"type": "number"}, "end": {"type": "number"}}}
PLAN_SCHEMA = {
    "type": "object",
    "required": ["title", "aspect", "duration", "style", "beats"],
    "properties": {
        "title": {"type": "string"},
        "aspect": {"type": "string", "enum": list(planner.ASPECTS)},
        "fps": {"type": "number"},
        "duration": {"type": "number"},
        "style": {"type": "object", "properties": {
            "font": {"type": "string"}, "font_secondary": {"type": "string"}, "text": {"type": "string"},
            "accent": {"type": "string"}, "bg": {"type": "string"}, "bg2": {"type": "string"},
            "energy": {"type": "string", "enum": planner.ENERGIES}, "cut_every": {"type": ["number", "null"]},
            "caption_style": {"type": ["object", "null"]}, "captions": {"type": "boolean"},
            "progress_bar": {"type": "boolean"}, "wpm": {"type": "number"}, "grade": {"type": ["object", "null"]}}},
        "beats": {"type": "array", "minItems": 1, "items": {
            "type": "object", "required": ["start", "end", "say", "visual", "background", "transition", "sfx", "camera"],
            "properties": {
                "start": {"type": "number"}, "end": {"type": "number"}, "say": {"type": ["string", "null"]},
                "visual": {"type": ["object", "array", "null"], "required": ["template", "params"],
                           "properties": _CALL["properties"], "items": _CALL},
                "background": {"type": ["object", "null"]},
                "transition": {"type": ["string", "null"], "enum": planner.TRANSITIONS + [None]},
                "sfx": {"type": ["string", "null"], "enum": list(sfx.NAMES) + [None]},
                "camera": {"type": ["string", "null"], "enum": planner.CAMERAS + [None]}}}},
        "cta": {"type": ["object", "null"], "required": ["template", "params"], "properties": _CALL["properties"]},
        "notes": {"type": "string"},
    },
}

SYSTEM = """You are an elite short-form video director and motion designer: the person top creators hire to turn an idea into a reel that stops the scroll in the first second and holds attention to the last frame. You plan; a renderer draws. You write a PLAN as JSON (beats: what is said, which motion graphic shows, which footage plays, how each cut lands), never pixels.

Rules you never break:
1. Hook in the first 1.5 s. beats[0] starts at 0 with a hook template (usually hook_title) whose text is the sharpest form of the promise in 2-7 words with one accent word highlighted, a punch or push camera and an impact sfx.
2. Re-hook every 6-10 s with a pattern interrupt: a new template family, an emphasis_word, a flash or glitch cut, a question, a number reveal.
3. One idea per beat. A beat lasts about as long as its line takes to say at style.wpm (words / wpm * 60 s, plus a breath), close to the reference's shot length (style.cut_every). Never cram, never let a beat sag past ~5 s.
4. Template text is short and punchy (2-7 words), not a copy of the spoken line (kinetic_text is the exception: it is the line). `say` is the spoken line (on a silent reel it becomes the captions): natural spoken English, contractions, no corporate filler, no hashtags or emoji.
5. Match the reference: copy the measured caption_style object exactly into style.caption_style and use the measured palette, font, energy, wpm and cut_every. The reel must feel cut by the same editor.
6. Competitors are the bar to beat: hook faster and clearer than they do. Never reuse their wording, lines or structure beat for beat.
7. Truth: never invent statistics, prices, results, names or quotes. A number may appear only if the creator's prompt or script gives it. Without facts, make the point with words, not fake data. Number templates (counter_reveal, percent_ring, bar_chart, rating, poll) need real numbers.
8. Use only templates from the catalog, with exactly the params they list; use only the listed asset files (exact paths) as backgrounds, and vary the in points so a clip never repeats the same moment.
9. Timing: beats are contiguous. beats[0].start = 0, every beat starts where the previous ends, the last ends exactly at the duration.
10. Sound follows motion: every visible transition gets a fitting sfx (whoosh or swoosh for whip, slide and zoom; hit or boom for impacts; pop or click for list items; glitch for glitch; a riser before a big reveal; ding for the CTA). Not every beat needs one. 'cut' should be the most common transition.
11. Captions and layout: each beat's `say` line is shown as captions at caption_style.center_y_pct of the frame height, except while a full-screen graphic is up or when the beat's template already shows the same words (then the caption is dropped as a duplicate). When the reference relies on captions (its text covers most of the video), keep captions the main text layer: give templates a different, shorter accent (a number, a 1-3 word punch, a step marker) rather than the line itself. Keep template text out of the caption band with the templates' position / y params; on a beat without captions, centre the main graphic (a top-heavy frame with an empty lower half looks unfinished).
12. Close with one clear ask (the cta template) in the last 2-3 s; when you can, make the last line lead back into the hook so the reel loops.

Return only the JSON plan."""

PLAN_FORMAT = """# PLAN format

{"title": str, "aspect": "9:16"|"1:1"|"4:5"|"16:9", "fps": 30, "duration": seconds,
 "style": {"font", "font_secondary", "text", "accent", "bg", "bg2" (#RRGGBB), "energy": "high"|"medium"|"calm",
           "cut_every": seconds per shot on footage, "caption_style": {...measured, copy as given...},
           "captions": true, "progress_bar": true, "wpm": words per minute, "grade": {"contrast", "saturation"} or null},
 "beats": [{"start": s, "end": s, "say": "spoken line" or null,
            "visual": {"template": name, "params": {...}} or a list of them (drawn in order) or null,
            "background": {"asset": "<exact listed path>", "in": s, "zoom": [1.0, 1.08], "focus": [0.5, 0.4]}
                          or {"color": "#hex"} or {"gradient": ["#hex", "#hex"], "angle": deg} or {"keep": true} or null,
            "transition": "cut"|"flash"|"whip"|"zoom"|"fade"|"glitch"|"slide" (into this beat; null on beat 1),
            "sfx": sound name or null, "camera": "none"|"punch"|"shake"|"drift"|"push"}],
 "cta": {"template": "cta", "params": {"text": "..."}} or null,
 "notes": "one line on the creative idea"}

Background null shows the style background with a soft animated glow; {"keep": true} lets the previous shot run on
(use it to hold one shot across two short beats). A template call may carry "start"/"end" (seconds from the beat start)
to appear later in its beat. Sounds: whoosh, swoosh, pop, click, tick, hit, boom, riser, ding, glitch, typing, camera.
"""


def _transcript(item, n=TRANSCRIPT_CHARS):
    words = item.get("words") or []
    text = " ".join(str(w.get("w", "")) for w in words if isinstance(w, dict)).strip()
    if not text:
        return ""
    return text[:n] + (" ..." if len(text) > n else "")


def _norm_tokens(text):
    return [t for t in re.sub(r"[^a-z0-9$%' ]+", " ", str(text).lower()).split() if t]


def _ngrams(tokens, n=7):
    return {" ".join(tokens[i:i + n]) for i in range(len(tokens) - n + 1)}


_NUMBER_TOKEN = re.compile(r"\d[\d,]*(?:\.\d+)?")
# number-typed template params that put a number on screen; every other number param is layout or timing
# (x, y, w, h, size, tilt, times, duration, decimals...) and says nothing about the creator's results
_NUMERIC_PARAMS = {"value", "from", "values", "results", "price", "old_price", "number", "likes", "comments"}
_LAYOUT_EXCEPTIONS = {("progress_bar", "from"), ("progress_bar", "to")}     # fill fractions, never drawn as text
_PATH_PARAMS = {"image", "src"}                                             # file names, not on-screen text


def _shows_content(template, key):
    """True when a template param is something the viewer reads (text, or a number shown as a fact)."""
    spec = templates.CATALOG.get(template, {}).get("params", {}).get(key) if isinstance(template, str) else None
    if spec is None:
        return False                    # unknown template or param: never drawn, and validate_plan reports it
    t = spec["type"]
    if t in ("str", "list[str]", "highlight"):
        return key not in _PATH_PARAMS
    if t in ("int", "float", "list[float]"):
        return key in _NUMERIC_PARAMS and (template, key) not in _LAYOUT_EXCEPTIONS
    return False                        # colours, choices, switches


def _plan_texts(plan):
    """(where, text) of every spoken line and every on-screen template value in a plan.

    Layout and style params (positions, sizes, word times, colours, choices)
    are left out: they are not content, so they are never "invented numbers".
    Malformed parts (params that are not an object) are skipped: validate_plan reports them.
    """
    out = []

    def call_texts(prefix, c):
        if not isinstance(c, dict) or not isinstance(c.get("params"), dict):
            return
        name = c.get("template")
        for k, v in c["params"].items():
            if not _shows_content(name, k):
                continue
            for x in (v if isinstance(v, list) else [v]):
                if isinstance(x, (str, int, float)) and not isinstance(x, bool):
                    out.append(("%s %s.%s" % (prefix, name, k), str(x)))

    beats = plan.get("beats") if isinstance(plan, dict) else None
    for i, b in enumerate(beats if isinstance(beats, list) else []):
        if not isinstance(b, dict):
            continue
        if isinstance(b.get("say"), str):
            out.append(("beats[%d].say" % i, b["say"]))
        for c in planner.visual_calls(b.get("visual")):
            call_texts("beats[%d].visual" % i, c)
    if isinstance(plan, dict):
        call_texts("cta", plan.get("cta"))
    return out


def invented_numbers(plan, sources):
    """Numbers the plan shows or says that the creator's material does not contain (small counts are fine)."""
    have = {n.replace(",", "").rstrip(".") for n in _NUMBER_TOKEN.findall(sources or "")}
    have |= {str(int(float(x))) for x in have if re.fullmatch(r"\d+(\.0+)?", x)}
    errs = []
    for where, text in _plan_texts(plan):
        for tok in _NUMBER_TOKEN.findall(text):
            t = tok.replace(",", "").rstrip(".")
            try:
                val = float(t)
            except ValueError:
                continue
            if val <= 10 and float(val).is_integer():
                continue                 # list numbers, "part 2", "3 tips"
            if t in have or (float(val).is_integer() and str(int(val)) in have):
                continue
            errs.append("%s says %r: that number is not in the creator's prompt or script (never invent numbers; "
                        "make the point without it)" % (where, tok))
    return errs


def copied_lines(plan, others, own_text=""):
    """Lines that repeat 7+ words in a row from a reference or competitor transcript."""
    own = _ngrams(_norm_tokens(own_text))
    theirs = set()
    for t in others:
        theirs |= _ngrams(_norm_tokens(t))
    theirs -= own
    errs = []
    if not theirs:
        return errs
    for where, text in _plan_texts(plan):
        hit = _ngrams(_norm_tokens(text)) & theirs
        if hit:
            errs.append("%s copies a reference/competitor line (\"%s ...\"): write it in your own words"
                        % (where, sorted(hit)[0]))
    return errs


def script_coverage(plan, script):
    """Share of the script's words that the plan's spoken lines keep (0..1)."""
    want = [w for w in _norm_tokens(script) if len(w) > 2]
    if not want:
        return 1.0
    said = collections.Counter(w for b in plan.get("beats") or [] if isinstance(b, dict)
                               for w in _norm_tokens(b.get("say") or ""))
    hit = 0
    for w in want:
        if said[w] > 0:
            said[w] -= 1
            hit += 1
    return hit / float(len(want))


def build_prompt(prompt, script, targets, refs, comps, beat_lines, assets, duration, aspect, voice_words=None,
                 brand=None):
    """The user prompt of the planning call."""
    st = dict(targets.get("style") or {})
    P = ["# The reel to make", "", prompt or "(no prompt: build it from the script)", "",
         "Aspect %s, duration exactly %s s, 30 fps." % (aspect, round(duration, 2)), ""]
    if voice_words:
        sents = planner.sentences_from_words(voice_words)
        P += ["# Voiceover (already recorded; the captions come from it)",
              "Each beat must start where a spoken sentence starts (times below); keep `say` equal to that sentence.", ""]
        P += ["- %.2f-%.2f s: %s" % (s[0]["s"], s[-1]["e"], " ".join(w["w"] for w in s)) for s in sents]
        P.append("")
    elif script:
        P += ["# The creator's script (keep their words: split it across beats in order; you may trim a filler word, "
              "never rewrite or add claims)", "", script.strip(), ""]
    if brand:
        P += ["# brand.md (audience, voice, what is off-limits)", "", brand.strip()[:3000], ""]
    measured = {k: targets.get(k) for k in ("duration_s", "cuts_per_10s", "mean_shot_s", "first_cut_s", "bpm", "wpm",
                                            "time_to_first_word_s", "first_text_s", "text_coverage", "rehook_gap_s",
                                            "sentence_words") if targets.get(k) is not None}
    P += ["# Style targets measured from the reference video(s)", ""]
    if targets.get("refs"):
        P += ["Measured: `%s`" % json.dumps(measured), "",
              "Use this as the plan style (keep caption_style exactly; you may add grade):",
              "```json", json.dumps(st, indent=1, ensure_ascii=False), "```", ""]
    else:
        P += ["No reference was measured: use the house style %s and your judgment." % json.dumps(st or {}), ""]
    for r in refs:
        tx = _transcript(r)
        if tx:
            P += ["## Reference %s transcript (for rhythm and pacing only; do not reuse its words)" % r["name"], tx, ""]
    if comps:
        P += ["# Competitors (beat them; never reuse their words)", ""]
        for c in comps:
            if c.get("error"):
                continue
            d = (c.get("dna") or {}).get("dialogue") or {}
            P.append("- %s: hook \"%s\", %s wpm, %s s long" % (c["name"], (d.get("hook") or {}).get("first_sentence", ""),
                                                             d.get("wpm"), ((c.get("dna") or {}).get("edit") or {}).get("duration_s")))
            tx = _transcript(c, TRANSCRIPT_CHARS // 2)
            if tx:
                P.append("  transcript: " + tx)
        P += [""] + (["What to beat:"] + ["- " + b for b in beat_lines] + [""] if beat_lines else [])
    P += ["# Assets you may use as backgrounds (exact paths)", ""]
    if assets:
        for a in assets:
            info = timeline.probe(a["path"]) or {}
            P.append("- %s: %s%s%s" % (a["path"], a["kind"], ", %.1f s" % a["duration"] if a.get("duration") else "",
                                       ", %sx%s" % (info.get("width"), info.get("height")) if info.get("width") else ""))
    else:
        P.append("None: use null (the animated style background), colours or gradients; let the motion graphics carry it.")
    P += ["", PLAN_FORMAT, "", templates.catalog_doc()]
    return "\n".join(P)


def _llm_client(mode, client=None):
    """The planning client: an injected object with complete_json, the CLI wrapper, or None (offline)."""
    for c in (client, mode):
        if c is not None and not isinstance(c, str) and hasattr(c, "complete_json"):
            return c
    if mode == "none":
        return None
    if mode == "claude":
        if not llm_module.available():
            raise llm_module.LLMUnavailable(llm_module.INSTALL_HELP)
        return llm_module
    return llm_module if llm_module.available() else None


def _apply_targets(plan, targets, aspect, voice=None, voice_words=None, music=None):
    """Settle the plan's frame, the measured style keys it left out, the voice and the music (director-owned)."""
    plan["aspect"] = aspect
    plan.setdefault("fps", 30)
    if not isinstance(plan.get("style"), dict):
        plan["style"] = {}
    st = plan["style"]
    for k, v in (targets.get("style") or {}).items():
        if st.get(k) in (None, "", {}):
            st[k] = v
    if voice:
        plan["voiceover"] = {"src": voice, "words": voice_words or None}
    if music:
        vol = (plan.get("music") if isinstance(plan.get("music"), dict) else {}).get("volume")
        plan["music"] = {"src": music, "volume": vol if _num(vol) is not None and 0 < vol <= 2 else 0.22}
    return plan


# =========================================================================== 5. QA

def _qa_row(metric, target, got, verdict, note=""):
    return {"metric": metric, "target": target, "got": got, "verdict": verdict, "note": note}


def _close(got, want, rel=0.25, absol=0.0):
    if got is None or want is None:
        return None
    return abs(got - want) <= max(abs(want) * rel, absol)


def contact_sheet(spec_path, beats, out_dir, preview=True, base_dir=None, n_max=8):
    """Stills at each beat's settled moment (render.still) tiled into qa/contact_sheet.jpg; returns paths."""
    from PIL import Image, ImageDraw
    from . import render
    times = []
    for b in beats:
        s, e = float(b["start"]), float(b["end"])
        times.append(round(s + min(e - s - 0.05, max(0.5, (e - s) * 0.6)), 2))
    if len(times) > n_max:
        step = len(times) / float(n_max)
        times = [times[int(i * step)] for i in range(n_max)]
    folder = paths.ensure(os.path.join(out_dir, "qa"))
    stills = []
    for k, t in enumerate(times):
        p = os.path.join(folder, "still_%02d_%05.2fs.png" % (k + 1, t))
        render.still(spec_path, t, p, preview=preview, base_dir=base_dir)
        stills.append((t, p))
    if not stills:
        return None, []
    ims = [Image.open(p).convert("RGB") for _, p in stills]
    tw = 270
    th = int(round(ims[0].size[1] * tw / float(ims[0].size[0])))
    cols = min(4 if th > tw else 3, len(ims))
    rows = (len(ims) + cols - 1) // cols
    sheet = Image.new("RGB", (cols * (tw + 8) + 8, rows * (th + 30) + 8), (24, 24, 28))
    d = ImageDraw.Draw(sheet)
    for i, ((t, _), im) in enumerate(zip(stills, ims)):
        x, y = 8 + (i % cols) * (tw + 8), 8 + (i // cols) * (th + 30)
        sheet.paste(im.resize((tw, th), Image.LANCZOS), (x, y + 22))
        d.text((x + 2, y + 4), "%.2f s" % t, fill=(230, 230, 230))
    path = os.path.join(folder, "contact_sheet.jpg")
    sheet.save(path, quality=90)
    return path, [p for _, p in stills]


def qa_report(video, spec, spec_path, plan, targets, out_dir, duration, preview=True, log=None):
    """Re-measure the reel, compare with the targets, write QA.md; returns {"rows", "sheet", "stills", "md"}."""
    qa_dir = paths.ensure(os.path.join(out_dir, "qa"))
    words = (spec.get("captions") or {}).get("words") or []
    # the transcript for the pace and hook checks: every spoken word, not only the captioned ones
    spoken = (spec.get("meta") or {}).get("spoken_words") or words
    cap_file = None
    if spoken:
        cap_file = os.path.join(qa_dir, "spoken_words.json")
        _write_json(cap_file, spoken)
    dna, err = {}, None
    try:
        run_extract(video, os.path.join(qa_dir, "dna"), cap_file, log)
        dna = _read_json(os.path.join(qa_dir, "dna", "style_dna.json"), {}) or {}
    except DirectorError as e:
        err = str(e)
    e, x, d = dna.get("edit") or {}, dna.get("text") or {}, dna.get("dialogue") or {}
    cap = x.get("caption_track") or {}
    rows = []
    info = timeline.probe(video) or {}
    got = info.get("duration")
    want = duration
    rows.append(_qa_row("Length", _fmt(want, " s"), _fmt(got, " s", 2),
                        "pass" if _close(got, want, 0.05, 1.0) else "warn" if got else "n/a"))
    ft = x.get("first_text_s")
    rows.append(_qa_row("Hook text on screen", "by 1.5 s", _fmt(ft, " s"),
                        "pass" if ft is not None and ft <= 1.5 else "warn" if ft is not None else "n/a",
                        "" if ft is not None else "no text was read (OCR missing?)"))
    measured = {"cuts_per_10s": e.get("cuts_per_10s"), "mean_shot_s": e.get("mean_shot_s"),
                "first_cut_s": e.get("first_cut_s"), "wpm": d.get("wpm"), "text_coverage": x.get("coverage")}
    for metric, key, rel, absol, unit in (("Cuts per 10 s", "cuts_per_10s", 0.35, 1.0, ""),
                                          ("Mean shot", "mean_shot_s", 0.35, 0.4, " s"),
                                          ("First cut", "first_cut_s", 0.5, 0.6, " s"),
                                          ("Pace", "wpm", 0.15, 10, " wpm"),
                                          ("Text on screen", "text_coverage", 0.0, 0.25, "")):
        want_v, got_v = _num(targets.get(key)), _num(measured[key])
        ok = _close(got_v, want_v, rel, absol)
        rows.append(_qa_row(metric, _fmt(want_v, unit), _fmt(got_v, unit),
                            "n/a" if ok is None else "pass" if ok else "warn"))
    tc = targets.get("caption") or {}
    # a reel that tells its story in motion graphics has little caption track to compare: the OCR's busiest
    # text band would be template text, so the caption rows are skipped instead of reported as misses
    shown = sum(max(0.0, w["e"] - w["s"]) for w in words if _num(w.get("s")) is not None and _num(w.get("e")) is not None)
    cap_note = "" if shown >= 0.2 * max(1.0, duration) else \
        "captions are on screen only %.0f%% of the time (the words are motion graphics)" % (100 * shown / max(1.0, duration))
    for metric, key, rel, absol, unit in (("Caption size", "font_size_pct", 0.2, 0.4, "% of height"),
                                          ("Caption position", "center_y_pct", 0.0, 6.0, "% down")):
        ok = None if cap_note else _close(_num(cap.get(key)), _num(tc.get(key)), rel, absol)
        rows.append(_qa_row(metric, _fmt(tc.get(key), unit), _fmt(cap.get(key), unit),
                            "n/a" if ok is None else "pass" if ok else "warn", cap_note))
    wpc_t = (tc.get("words_per_card") or {}).get("median") if isinstance(tc.get("words_per_card"), dict) else tc.get("words_per_card")
    wpc_g = (cap.get("words_per_card") or {}).get("median") if isinstance(cap.get("words_per_card"), dict) else None
    ok = None if cap_note else _close(_num(wpc_g), _num(wpc_t), 0.0, 1.0)
    rows.append(_qa_row("Caption words per card", _fmt(wpc_t), _fmt(wpc_g), "n/a" if ok is None else "pass" if ok else "warn"))
    if tc.get("case") and cap.get("case") and not cap_note:
        rows.append(_qa_row("Caption case", tc["case"], cap["case"], "pass" if tc["case"] == cap["case"] else "warn"))
    for key in ("fill", "highlight"):
        a, b = tc.get(key), cap.get(key)
        if isinstance(a, str) and isinstance(b, str) and not cap_note:
            rows.append(_qa_row("Caption %s" % key, a, b, "pass" if _dist(a, b) < 70 else "warn"))
    # safe zone: text the OCR found in the platform UI bands or the side margins
    W, H = spec["width"], spec["height"]
    portrait = H / float(W) >= 1.5
    top, bottom = (11.0, 80.0) if portrait else (7.0, 88.0)
    side = 6.0 if portrait else 5.0
    bad = []
    for c in _read_json(os.path.join(qa_dir, "dna", "text_cards.json"), []) or []:
        if (_num(c.get("duration")) or 0) < 0.5:
            continue          # a passing animation state (an echo, a stamp in flight), not a layout
        s = c.get("style") or {}
        cy, lh, cx, wp = (_num(s.get(k)) for k in ("center_y_pct", "line_height_pct", "center_x_pct", "width_pct"))
        if cy is None or lh is None:
            continue
        lines = int(c.get("lines") or 1)
        y0, y1 = cy - lh * lines / 2.0, cy + lh * lines / 2.0
        x0, x1 = (cx - wp / 2.0, cx + wp / 2.0) if cx is not None and wp is not None else (50, 50)
        # OCR boxes run a little wider than the ink, hence the tolerance
        if y0 < top - 1.0 or y1 > bottom + 1.0 or x0 < side - 1.5 or x1 > 100 - side + 1.5:
            bad.append("%.1f s \"%s\" (%.0f-%.0f%% down, %.0f-%.0f%% across)" % (c.get("start", 0), c.get("text", "")[:30],
                                                                             y0, y1, x0, x1))
    if dna:
        rows.append(_qa_row("Text inside the safe zone", "%g-%g%% down, %g%% sides" % (top, bottom, side),
                            "all inside" if not bad else "%d outside" % len(bad), "pass" if not bad else "warn",
                            "; ".join(bad[:4])))
    sheet, stills = None, []
    try:
        sheet, stills = contact_sheet(spec_path, (spec.get("meta") or {}).get("beats") or plan.get("beats") or [],
                                      out_dir, preview=preview, base_dir=out_dir)
    except Exception as ex:          # stills are a review aid; a failure is reported, not fatal
        err = (err + "; " if err else "") + "stills failed: %s" % ex
    n_warn = sum(1 for r in rows if r["verdict"] == "warn")
    L = ["# QA: the reel against the reference", "",
         "Video: `%s` (%sx%s). %d pass, %d warn, %d not measurable." % (
             video, info.get("width"), info.get("height"), sum(1 for r in rows if r["verdict"] == "pass"), n_warn,
             sum(1 for r in rows if r["verdict"] == "n/a")), ""]
    if not targets.get("refs"):
        L += ["No reference was measured, so only the house rules (length, hook, safe zone) are checked.", ""]
    L += ["| Metric | Target | This reel | Verdict | Note |", "| --- | --- | --- | --- | --- |"]
    L += ["| %s | %s | %s | %s | %s |" % (r["metric"], r["target"], r["got"], r["verdict"].upper() if r["verdict"] == "warn"
                                          else r["verdict"], r["note"].replace("|", "/")) for r in rows]
    L.append("")
    if err:
        L += ["Measuring problems: %s" % err, ""]
    if sheet:
        L += ["## Stills for the visual review", "", "Contact sheet: `%s`" % sheet, ""]
        L += ["- `%s`" % p for p in stills]
        L += ["", "Look for: hook readable in under 1 s; nothing in the top 11% / bottom 20%; one font family; "
              "text never over a face; every visible transition has a sound."]
    if n_warn:
        L += ["", "Fix the WARN rows in plan.json (beat lengths for pace and cuts, style.caption_style for captions) "
              "and run `python -m vstudio reel --plan plan.json --out-dir <this folder>` again."]
    md = "\n".join(L) + "\n"
    _write_text(os.path.join(out_dir, "QA.md"), md)
    return {"rows": rows, "sheet": sheet, "stills": stills, "md": md, "error": err}


# =========================================================================== the whole flow

def _voice_words(voice, log=None, notes=None):
    """Timed words [{"w", "s", "e"}] of a voiceover, or None.

    transcribe.load_words reads them: a captions or words file next to the
    recording (.srt, .vtt, .json3, .words.json in any documented shape:
    {w,s,e}, {word,start,end}, whisper segments), else faster-whisper. The
    words come back cleaned (sorted, non-overlapping, no blanks). What was
    tried and failed goes to the log; a transcript file that is there but
    could not be read also goes to `notes` (a list), for the brief.
    """
    from . import transcribe
    side = transcribe.find_sidecar(voice)
    try:
        words, source, problems = transcribe.load_words(media=voice)
    except Exception as e:               # whisper's own failures are caught inside; this is anything else
        words, source, problems = None, None, ["could not read the voiceover's words: %s" % e]
    for p in problems:
        _say(log, "  voiceover: %s" % p)
        if notes is not None and side and side in p:
            notes.append("the voiceover's transcript could not be used: %s" % p)
    if not words:
        return None
    _say(log, "  voiceover words from %s" % source)
    return transcribe.strip(words)


def _read_script(script):
    if not script:
        return None
    if isinstance(script, str) and len(script) < 1024 and os.path.isfile(script):
        with open(script, encoding="utf-8", errors="replace") as f:
            return f.read()
    return str(script)


def make_reel(prompt, refs=(), competitors=(), assets=(), script=None, voice=None, music=None, aspect="9:16",
              duration=30, out_dir=None, llm="auto", render=True, preview=False, plan=None, qa=True, log=print,
              client=None):
    """Make a reel; returns {"out_dir", "brief", "plan", "spec", "video", "qa", "planner", "notes", "seconds"}.

    llm: "auto" (Claude Code when installed, else offline), "claude" (Claude
    or LLMUnavailable), "none" (offline heuristic), or an object with
    complete_json(prompt, schema=, system=, timeout=, retries=, check=) (tests,
    other front ends; `client=` does the same). plan: a PLAN dict or plan.json
    path used as it is (agent mode). qa=False skips the QA pass.
    """
    t_start = time.time()
    timings = {}
    if aspect not in planner.ASPECTS:
        raise DirectorError("aspect must be one of %s" % ", ".join(planner.ASPECTS))
    plan_path_in = plan if isinstance(plan, str) else None
    if plan_path_in and not os.path.isfile(plan_path_in):
        raise DirectorError("plan not found: %s" % plan_path_in)
    if not out_dir:
        out_dir = os.path.dirname(os.path.abspath(plan_path_in)) if plan_path_in else os.path.join(
            paths.WORKSPACE, "reels", "%s-%s" % (_slug(prompt), time.strftime("%Y%m%d-%H%M%S")))
    out_dir = os.path.abspath(out_dir)
    os.makedirs(out_dir, exist_ok=True)
    notes = []
    script_text = _read_script(script)
    unreadable = []
    asset_rows = planner.asset_rows(assets, unreadable)
    for a in assets or []:
        if not os.path.isfile(a):
            notes.append("asset not found, skipped: %s" % a)
        elif planner.media_kind(a) not in ("video", "image"):
            notes.append("asset %s is not a video or image (pass music with --music), skipped" % a)
    for a, why in unreadable:
        notes.append("asset %s could not be read, skipped: %s" % (a, why))
    # the director owns the music and voice tracks: check them once here, never in Claude's answers; absolute,
    # because the plan's paths resolve against the project folder, not the folder the command ran in
    music = os.path.abspath(music) if music else None
    voice = os.path.abspath(voice) if voice else None
    for what, f in (("music", music), ("voice", voice)):
        if not f:
            continue
        if not os.path.isfile(f):
            raise DirectorError("%s file not found: %s" % (what, f))
        if planner.media_kind(f) not in ("audio", "video"):
            raise DirectorError("%s must be an audio file (or a video with sound), got %s" % (what, f))
    if not (prompt or script_text or plan):
        raise DirectorError("give a prompt, a script or a plan")

    # ----- 1. analyze
    _say(log, "analyzing %d reference(s) and %d competitor(s)" % (len(refs), len(competitors)), 5)
    t0 = time.time()
    ref_items, comp_items = analyze_inputs(list(refs), list(competitors), out_dir, log)
    saved = _read_json(os.path.join(out_dir, "analysis.json"), {}) or {}
    if not ref_items and saved.get("targets") and plan is not None:
        targets = saved["targets"]                 # a re-run on a finished brief keeps its targets
        comp_rows, beat_lines = saved.get("competitors") or [], saved.get("beat") or []
    else:
        targets = style_targets(ref_items, log)
        comp_rows, beat_lines = competitor_summary(comp_items)
    for it in ref_items + comp_items:
        if it.get("error"):
            notes.append("%s could not be measured: %s" % (it["name"], it["error"]))
    timings["analyze_s"] = round(time.time() - t0, 1)

    # ----- voice and length
    voice_words = None
    if voice:
        voice_words = _voice_words(voice, log, notes)
        vlen = planner.media_duration(voice)
        if voice_words:
            duration = max(voice_words[-1]["e"] + 0.5, 1.0)
        elif vlen:
            duration = vlen
            notes.append("no transcript for the voiceover (add a caption file next to it or install faster-whisper): "
                         "captions are timed from the script, not from the voice")

    # ----- 2. brief
    sc = None
    if script_text:
        sc = score_script(script_text, duration, (targets.get("style") or {}).get("wpm") or planner.DEFAULT_WPM)
    inputs = ref_items + comp_items
    brief = brief_md(prompt, targets, comp_rows, beat_lines, inputs, sc, asset_rows, duration, aspect, notes)
    brief_path = os.path.join(out_dir, "BRIEF.md")
    _write_text(brief_path, brief)
    _write_json(os.path.join(out_dir, "analysis.json"), {
        "version": ANALYSIS_VERSION, "prompt": prompt, "targets": targets, "competitors": comp_rows, "beat": beat_lines,
        "inputs": [{k: it.get(k) for k in ("role", "name", "src", "video", "dna_dir", "error")} for it in inputs],
        "assets": asset_rows})
    _say(log, "brief written: %s" % brief_path, 20)

    # ----- 3. plan
    t0 = time.time()
    allowed = [a["path"] for a in asset_rows]
    used = "file"
    plan_errors = []
    if plan is not None:
        the_plan = _read_json(plan) if isinstance(plan, str) else json.loads(json.dumps(plan))
        if not isinstance(the_plan, dict):
            raise DirectorError("the plan is not a JSON object: %s" % plan)
        base = os.path.dirname(os.path.abspath(plan_path_in)) if plan_path_in else out_dir
        planner.resolve_assets(the_plan, allowed or [], base)
        for b in the_plan.get("beats") if isinstance(the_plan.get("beats"), list) else []:
            # make asset paths absolute so plan.json moves safely
            bg = b.get("background") if isinstance(b, dict) else None
            if isinstance(bg, dict) and isinstance(bg.get("asset"), str):
                bg["asset"] = planner.resolve_path(bg["asset"], base)
        if voice or music:
            _apply_targets(the_plan, {}, the_plan.get("aspect") or aspect, voice, voice_words, music)
        plan_errors = planner.validate_plan(the_plan, base_dir=out_dir)
        for e in plan_errors:
            notes.append("plan: " + e)
    else:
        planner_client = _llm_client(llm, client)
        the_plan = None
        if planner_client is not None:
            used = "claude"
            _say(log, "planning with Claude (this can take a minute or two)", 30)
            sources = " ".join(x for x in (prompt, script_text) if x)
            brand_path = os.path.join(paths.WORKSPACE, "brand.md")
            brand = None
            if os.path.isfile(brand_path):
                with open(brand_path, encoding="utf-8", errors="replace") as f:
                    brand = f.read()
                sources += " " + brand
            others = [_transcript(x, 100000) for x in ref_items + comp_items]

            def check(p):
                if not isinstance(p, dict):
                    return ["the answer must be one JSON object (the plan)"]
                try:
                    return plan_problems(p)
                except Exception as e:        # a shape no check foresaw: it goes back to Claude, never a crash
                    return ["the plan could not be checked (%s: %s); follow the PLAN format exactly"
                            % (type(e).__name__, str(e)[:200])]

            def plan_problems(p):
                _apply_targets(p, targets, aspect, voice, voice_words, music)
                planner.resolve_assets(p, allowed, out_dir)
                errs = planner.validate_plan(p, base_dir=out_dir, assets=allowed)
                if _num(p.get("duration")) is None or abs(p["duration"] - duration) > 0.6:
                    errs.append("duration must be %.2f s (got %r)" % (duration, p.get("duration")))
                errs += invented_numbers(p, sources)
                errs += copied_lines(p, others, sources)
                if script_text and not voice_words and script_coverage(p, script_text) < 0.85:
                    errs.append("the spoken lines keep only %d%% of the creator's script: use their script, in order"
                                % round(100 * script_coverage(p, script_text)))
                if not errs:
                    try:
                        errs = ["timeline: " + e for e in timeline.validate(planner.compile_plan(p, out_dir))]
                    except ValueError as e:
                        errs = [str(e)]
                return errs

            user_prompt = build_prompt(prompt, script_text, targets, ref_items, comp_items, beat_lines, asset_rows,
                                       duration, aspect, voice_words, brand)
            _write_text(os.path.join(out_dir, "plan_prompt.md"), "# System\n\n%s\n\n# Prompt\n\n%s\n" % (SYSTEM, user_prompt))
            try:
                the_plan = planner_client.complete_json(user_prompt, schema=PLAN_SCHEMA, system=SYSTEM, timeout=900,
                                                        retries=2, check=check)
                check(the_plan)        # settles style, assets and voice on the accepted answer
            except llm_module.LLMUnavailable:
                if llm == "claude":
                    raise              # asked for Claude explicitly: say how to fix it instead of a silent fallback
                notes.append("Claude Code is not available: used the offline planner")
                the_plan = None
            except llm_module.LLMError as e:
                # a reel still gets made: the offline plan, with the reason in the brief
                notes.append("Claude's plan failed its checks (%s); used the offline planner instead" % e)
                the_plan = None
        if the_plan is None:
            used = "heuristic"
            _say(log, "planning offline (heuristic planner)", 35)
            hstyle = dict(targets.get("style") or {})
            the_plan = planner.heuristic_plan(prompt, script_text, hstyle, duration, allowed, aspect, words=voice_words)
            _apply_targets(the_plan, targets, aspect, voice, voice_words, music)
            plan_errors = planner.validate_plan(the_plan, base_dir=out_dir, assets=allowed)
            for e in plan_errors:
                notes.append("offline plan: " + e)
            # the offline planner never writes content of its own: say so when the reel comes out short
            if "shorter than" in str(the_plan.get("notes") or ""):
                notes.append("%s. For a full-length reel give the spoken lines with --script FILE, or plan with "
                             "Claude Code (--llm auto)" % the_plan["notes"])
    timings["plan_s"] = round(time.time() - t0, 1)
    plan_path = os.path.join(out_dir, "plan.json")
    _write_json(plan_path, the_plan)

    # ----- compile
    try:
        spec = planner.compile_plan(the_plan, base_dir=out_dir)
    except ValueError as e:
        raise DirectorError("the plan could not be compiled: %s" % e)
    except Exception as e:
        if not plan_errors:
            raise                          # a valid plan that does not compile is a bug: keep the traceback
        raise DirectorError("the plan could not be compiled (%s: %s); fix these first:\n- %s"
                            % (type(e).__name__, e, "\n- ".join(plan_errors)))
    spec_errors = timeline.validate(spec)
    spec_path = os.path.join(out_dir, "spec.json")
    timeline.save(spec, spec_path)
    if spec_errors:
        raise DirectorError("the timeline has problems (spec.json written for inspection):\n- " + "\n- ".join(spec_errors))
    if notes:                               # the brief gets the planning notes too
        _write_text(brief_path, brief_md(prompt, targets, comp_rows, beat_lines, inputs, sc, asset_rows, duration,
                                         aspect, notes + ["planner: %s" % used]))
    _say(log, "plan: %d beats, %.1f s (%s); spec written" % (len(the_plan.get("beats") or []), spec["duration"], used), 45)

    # ----- 4. render
    video = None
    qa_result = None
    if render:
        from . import render as render_mod
        video = os.path.join(out_dir, "reel.mp4")
        t0 = time.time()
        last = [-1]

        def progress(done, total):
            pct = int(45 + 40 * done / max(1, total))
            if pct != last[0] and (done == total or pct % 5 == 0):
                last[0] = pct
                _say(log, "rendering %d/%d frames" % (done, total), pct)

        try:
            render_mod.render(spec_path, video, preview=preview, progress=progress, base_dir=out_dir,
                              workdir=os.path.join(out_dir, "render_work"))
        except render_mod.RenderError as e:
            raise DirectorError("render failed: %s" % e)
        finally:
            shutil.rmtree(os.path.join(out_dir, "render_work"), ignore_errors=True)
        timings["render_s"] = round(time.time() - t0, 1)
        _say(log, "rendered %s" % video, 86)
        # ----- 5. QA
        if qa:
            t0 = time.time()
            _say(log, "QA: re-measuring the reel against the reference", 88)
            qa_result = qa_report(video, spec, spec_path, the_plan, targets, out_dir, spec["duration"], preview, log)
            timings["qa_s"] = round(time.time() - t0, 1)
    timings["total_s"] = round(time.time() - t_start, 1)
    _say(log, "done in %.0f s" % timings["total_s"], 100)
    return {"out_dir": out_dir, "brief": brief_path, "plan": plan_path, "spec": spec_path, "video": video,
            "qa": os.path.join(out_dir, "QA.md") if qa_result else None, "qa_rows": (qa_result or {}).get("rows"),
            "contact_sheet": (qa_result or {}).get("sheet"), "planner": used, "notes": notes,
            "plan_errors": plan_errors, "seconds": timings}


def main(argv=None):
    """vstudio reel --prompt "..." [--ref URL|FILE|DIR]... [--competitor ...]... [--asset FILE]... [...]"""
    ap = argparse.ArgumentParser(prog="vstudio reel", description="prompt + reference/competitor videos + assets -> reel")
    ap.add_argument("--prompt", default="", help="what the reel is about (topic, angle, audience, ending)")
    ap.add_argument("--ref", action="append", default=[], help="style reference: link, video file or analysis folder")
    ap.add_argument("--competitor", action="append", default=[], help="competitor video to beat: link, file or folder")
    ap.add_argument("--asset", action="append", default=[], help="your footage or image for backgrounds")
    ap.add_argument("--script", help="script file (your spoken lines)")
    ap.add_argument("--voice", help="voiceover recording (audio or video)")
    ap.add_argument("--music", help="music track (ducked under the voice)")
    ap.add_argument("--aspect", default="9:16", choices=list(planner.ASPECTS))
    ap.add_argument("--duration", type=float, default=30.0, help="target length in seconds (default 30)")
    ap.add_argument("--out-dir", help="project folder (default workspace/reels/<slug>-<time>)")
    ap.add_argument("--llm", default="auto", choices=["auto", "claude", "none"],
                    help="plan with Claude Code (auto when installed), or offline (none)")
    ap.add_argument("--plan", help="use this plan.json as it is (agent mode)")
    ap.add_argument("--no-render", action="store_true", help="stop after brief, plan and spec")
    ap.add_argument("--preview", action="store_true", help="half resolution and frame rate (fast draft)")
    ap.add_argument("--no-qa", action="store_true", help="skip the QA pass after rendering")
    a = ap.parse_args(argv)
    if not (a.prompt or a.script or a.plan):
        ap.error("give --prompt (or --script, or --plan)")
    if not 3 <= a.duration <= 600:
        ap.error("--duration must be 3..600 seconds")

    def log(msg):
        print(msg, flush=True)

    try:
        res = make_reel(a.prompt, a.ref, a.competitor, a.asset, a.script, a.voice, a.music, a.aspect, a.duration,
                        a.out_dir, a.llm, not a.no_render, a.preview, plan=a.plan, qa=not a.no_qa, log=log)
    except llm_module.LLMUnavailable as e:
        print("error: %s" % e, file=sys.stderr, flush=True)
        return 3
    except (DirectorError, OSError, ValueError) as e:
        print("error: %s" % e, file=sys.stderr, flush=True)
        return 1
    for n in res["notes"]:
        print("note: %s" % n)
    print("took   %s" % ", ".join("%s %.0f s" % (k[:-2], v) for k, v in res["seconds"].items()))
    print("brief  %s" % res["brief"])
    print("plan   %s (%s)" % (res["plan"], res["planner"]))
    print("spec   %s" % res["spec"])
    if res["video"]:
        print("video  %s" % res["video"])
    if res["qa"]:
        warns = [r["metric"] for r in res["qa_rows"] or [] if r["verdict"] == "warn"]
        print("qa     %s%s" % (res["qa"], " (warn: %s)" % ", ".join(warns) if warns else ""))
        if res.get("contact_sheet"):
            print("sheet  %s" % res["contact_sheet"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
