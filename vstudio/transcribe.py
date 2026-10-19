"""
transcribe.py - word timings for a recording, and moving them onto the edited
timeline after cuts.

    words = get_words(media="raw.mp4", captions="raw.srt")    # [{"w", "s", "e"}, ...] or None
    words, source, notes = load_words(media="raw.mp4")         # also says where the words came from
    out = remap(words, [{"in": 2.0, "out": 5.5, "t0": 0.0}, {"in": 7.1, "out": 9.0, "t0": 3.5}])

Where the words come from, best first:
    1. words_json   a words file: [{"w","s","e"}] (extract.py's transcript.json),
                    [{"word","start","end"}], {"words": [...]}, whisper-style
                    {"segments": [{"words": [...]}]} or {"segments": [{"start","end","text"}]},
                    or YouTube json3
    2. captions     an SRT / VTT / json3 captions file (SRT and json3 parsed by
                    extract.py, WebVTT by parse_vtt here: cue settings, tags,
                    [Music] and YouTube's rolling repeated lines are not words,
                    and auto-subs' inline <00:00:01.280> stamps time each word);
                    otherwise cue text is spread evenly over the cue, so word
                    edges are approximate (autoedit refines them against the audio)
    3. a sidecar    with neither given: <video>.srt / .vtt / .json3 / .words.json
                    next to the video, the way CapCut and Premiere export them
    4. whisper      local faster-whisper on the media (extract.whisper_words),
                    when it is installed and its model can be loaded
    5. None         autoedit then cuts on silence only

Every source is cleaned the same way: blank tokens dropped, multi-word tokens
split, times sorted, made non-overlapping and clipped to be non-negative.

remap() maps source-time words to output time: a word is kept when most of it
(at least half, or 0.12 s) survives inside one kept segment, clipped to that
segment; words that fell in a cut disappear. Extra keys on the words are kept.

Limits: SRT files and VTT files without inline stamps only time whole cues,
so word timing inside a cue is an estimate; whisper needs `pip install
faster-whisper` and downloads its model (about 500 MB for "small") on first
use.
"""

import bisect
import html
import json
import os
import re
import sys

from . import paths

SIDECAR_EXT = (".words.json", ".json3", ".srt", ".vtt", ".en.srt", ".en.vtt", ".en.json3")
MIN_WORD = 0.02   # seconds: shortest word kept after cleaning


# --------------------------------------------------------------------------- cleaning

def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f and abs(f) != float("inf") else None


def _pick(d, *keys):
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return None


def clean(words):
    """Normalized copies of words: [{"w", "s", "e", ...}] sorted, non-overlapping, no blanks.

    Accepts "w"/"word"/"text" and "s"/"start" / "e"/"end" keys. A token holding
    several words ("so yeah") is split evenly over its time. A missing end
    becomes the next word's start (at most 1 s later).
    """
    rows = []
    for w in words or []:
        if not isinstance(w, dict):
            continue
        text = _pick(w, "w", "word", "text")
        s = _num(_pick(w, "s", "start"))
        if text is None or s is None or not str(text).strip():
            continue
        e = _num(_pick(w, "e", "end"))
        extra = {k: v for k, v in w.items() if k not in ("w", "word", "text", "s", "start", "e", "end")}
        rows.append((max(0.0, s), e, str(text).strip(), extra))
    rows.sort(key=lambda r: r[0])
    # missing ends: up to the next start, at most 1 s
    filled = []
    for i, (s, e, text, extra) in enumerate(rows):
        if e is None:
            nxt = rows[i + 1][0] if i + 1 < len(rows) else s + 0.4
            e = min(max(nxt, s + 0.1), s + 1.0)
        filled.append((s, max(e, s), text, extra))
    out = []
    for s, e, text, extra in filled:
        toks = text.split()
        if not toks:
            continue
        step = (e - s) / len(toks)
        for k, tok in enumerate(toks):
            d = dict(extra)
            d.update({"w": tok, "s": s + k * step, "e": s + (k + 1) * step})
            out.append(d)
    # no overlaps: a word ends where the next one starts
    for i in range(len(out) - 1):
        if out[i]["e"] > out[i + 1]["s"]:
            out[i]["e"] = max(out[i]["s"], out[i + 1]["s"])
    res = []
    for d in out:
        d["s"], d["e"] = round(d["s"], 3), round(max(d["e"], d["s"] + MIN_WORD), 3)
        res.append(d)
    for i in range(len(res) - 1):    # the minimum length must not create an overlap either
        if res[i]["e"] > res[i + 1]["s"]:
            res[i + 1]["s"] = res[i]["e"]
            res[i + 1]["e"] = round(max(res[i + 1]["e"], res[i + 1]["s"] + MIN_WORD), 3)
    return res


def _segments_words(segments):
    """Whisper-style segments -> words (their own word lists, or the text spread over the segment)."""
    words = []
    for seg in segments or []:
        if not isinstance(seg, dict):
            continue
        if seg.get("words"):
            words.extend(seg["words"])
            continue
        s, e = _num(seg.get("start")), _num(seg.get("end"))
        toks = str(seg.get("text") or "").split()
        if s is None or e is None or not toks or e <= s:
            continue
        step = (e - s) / len(toks)
        words.extend({"w": t, "s": s + i * step, "e": s + (i + 1) * step} for i, t in enumerate(toks))
    return words


def load_words_json(path):
    """Words from a JSON file in any of the shapes listed in the module docstring (cleaned)."""
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, dict):
        if "events" in data:
            return clean(paths.load_skill("extract").parse_json3(data))
        if isinstance(data.get("words"), list):
            return clean(data["words"])
        if isinstance(data.get("segments"), list):
            return clean(_segments_words(data["segments"]))
        raise ValueError("%s: no words found (expected a list of {w, s, e}, {\"words\": [...]} or "
                         "{\"segments\": [...]})" % path)
    if isinstance(data, list):
        if data and isinstance(data[0], dict) and "words" in data[0] and "w" not in data[0]:
            return clean(_segments_words(data))
        return clean(data)
    raise ValueError("%s: not a words file" % path)


_VTT_TIME = re.compile(r"^\s*((?:\d+:)?\d{1,2}:\d{2}[.,]\d{1,3})\s*-->\s*((?:\d+:)?\d{1,2}:\d{2}[.,]\d{1,3})")
_VTT_STAMP = re.compile(r"<((?:\d+:)?\d{1,2}:\d{2}[.,]\d{1,3})>")


def _vtt_seconds(t):
    parts = [float(p) for p in t.replace(",", ".").split(":")]
    return sum(p * 60 ** k for k, p in enumerate(reversed(parts)))


def _vtt_line(line):
    """[(start or None, text)] chunks of a cue line: inline <hh:mm:ss.mmm> stamps start chunks, tags dropped."""
    chunks, t = [], None
    for k, part in enumerate(_VTT_STAMP.split(line)):
        if k % 2:
            t = _vtt_seconds(part)
            continue
        text = html.unescape(re.sub(r"<[^>]*>", "", part))
        text = re.sub(r"\[[^\]]*\]", " ", text)                 # [Music], [Applause]: sounds, not speech
        if text.strip():
            chunks.append((t, text))
    if chunks:   # ">>" (a new speaker) and a dialogue dash at the start of a line are not words
        t0, text = chunks[0]
        chunks[0] = (t0, re.sub(r"^\s*(?:>>\s*|-\s+)", "", text))
    return [(t, text) for t, text in chunks if text.strip()]


def _norm(text):
    return " ".join(text.lower().split())


def parse_vtt(text):
    """Words [{"w", "s", "e"}] of a WebVTT file, including YouTube's auto-subs.

    Cue settings ("align:start position:0%") and tags are not words; a line
    that repeats the previous cue's last line is the rolling display of
    auto-subs, not new speech (and a cue under 0.15 s holding only such a
    line is a display transition); inline <hh:mm:ss.mmm> stamps time the
    words after them. Other words are spread evenly up to the next timed
    word or the cue end.
    """
    lines = text.replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff").split("\n")
    words, prev_last, k = [], None, 0
    while k < len(lines):
        m = _VTT_TIME.match(lines[k])
        k += 1
        if not m:
            continue
        a, z = _vtt_seconds(m.group(1)), _vtt_seconds(m.group(2))
        body = []
        while k < len(lines) and lines[k] and not lines[k].strip():
            k += 1                                   # auto-subs put a " " line above the new words
        if k + 1 < len(lines) and _VTT_TIME.match(lines[k + 1]):
            continue                                 # an empty cue: that line is the next cue's id
        while k < len(lines) and lines[k].strip() and not _VTT_TIME.match(lines[k]):
            body.append(lines[k])
            k += 1
        plain = [_norm(" ".join(t for _, t in _vtt_line(b))) for b in body]
        keep = list(zip(body, plain))
        if prev_last is not None:
            while len(keep) > 1 and keep[0][1] == prev_last:
                keep.pop(0)                          # the rolling line above the new words
            if len(keep) == 1 and keep[0][1] == prev_last and z - a < 0.15:
                keep = []
        if any(p for _, p in keep):
            prev_last = [p for _, p in keep if p][-1]
        if z <= a:
            continue
        # runs of words that start at one time: the cue start, then each inline stamp
        runs = []
        for b, _ in keep:
            for t, chunk in _vtt_line(b):
                if t is not None or not runs:
                    start = min(max(a if t is None else t, runs[-1][0] if runs else a), z)
                    runs.append((start, []))
                runs[-1][1].extend(chunk.split())
        for n, (s, toks) in enumerate(runs):
            e = runs[n + 1][0] if n + 1 < len(runs) else z
            step = (e - s) / len(toks)
            for q, w in enumerate(toks):
                words.append({"w": w, "s": round(s + q * step, 3), "e": round(s + (q + 1) * step, 3)})
    return words


def load_captions(path):
    """Words from an SRT / VTT / json3 / words-json captions file (cleaned).

    WebVTT is parsed here (parse_vtt); SRT and json3 by extract.py.
    """
    if path.lower().endswith(".json"):
        return load_words_json(path)
    with open(path, encoding="utf-8-sig", errors="replace") as f:   # read here so the file is always closed
        raw = f.read()
    if path.lower().endswith(".vtt") or raw.lstrip().startswith("WEBVTT"):
        return clean(parse_vtt(raw))
    ex = paths.load_skill("extract")
    if path.lower().endswith(".json3") or raw.lstrip().startswith("{"):
        return clean(ex.parse_json3(json.loads(raw)))
    return clean(ex.parse_srt_vtt(raw))


def find_sidecar(media):
    """A captions or words file saved next to the media with the same name, or None."""
    if not media:
        return None
    stem = os.path.splitext(media)[0]
    for ext in SIDECAR_EXT:
        for cand in (stem + ext, stem + ext.upper()):
            if os.path.isfile(cand):
                return cand
    return None


# --------------------------------------------------------------------------- public API

def load_words(media=None, captions=None, words_json=None, model="small", lang=None, sidecar=True):
    """(words or None, source label or None, notes) - the best transcript available.

    `notes` explains anything that was tried and failed (a broken file,
    faster-whisper missing), so callers can report it.
    """
    notes = []
    if words_json:
        try:
            words = load_words_json(words_json)
            if words:
                return words, "words file %s" % os.path.basename(words_json), notes
            notes.append("the words file %s has no words" % words_json)
        except (OSError, ValueError) as e:
            notes.append("could not read the words file: %s" % e)
    if captions:
        try:
            words = load_captions(captions)
            if words:
                return words, "captions file %s" % os.path.basename(captions), notes
            notes.append("the captions file %s has no cues" % captions)
        except (OSError, ValueError) as e:
            notes.append("could not read the captions file: %s" % e)
    if sidecar and not words_json and not captions:
        side = find_sidecar(media)
        if side:
            try:
                words = load_captions(side)
                if words:
                    return words, "captions file found next to the video (%s)" % os.path.basename(side), notes
            except (OSError, ValueError) as e:
                notes.append("could not read %s: %s" % (side, e))
    if media and os.path.isfile(media):
        try:
            raw = paths.load_skill("extract").whisper_words(media, model=model, lang=lang)
        except Exception as e:   # model download / decode failures inside faster-whisper
            raw = None
            notes.append("faster-whisper could not transcribe the video: %s" % (str(e).strip()[:300] or e))
        else:
            if raw is None:
                notes.append("faster-whisper is not installed (pip install faster-whisper), so there is no "
                             "transcript; pass --captions or --words to get word-level cuts and captions")
        words = clean(raw) if raw else None
        if words:
            return words, "faster-whisper (%s model, on this machine)" % model, notes
        if raw is not None and not words:
            notes.append("faster-whisper heard no speech")
    return None, None, notes


def get_words(media=None, captions=None, words_json=None, model="small", lang=None):
    """Words [{"w", "s", "e"}] from words_json, else captions, else a sidecar file, else whisper; None if none."""
    words, _, notes = load_words(media, captions, words_json, model, lang)
    for n in notes:
        print("transcribe: %s" % n, file=sys.stderr)
    return words


def remap(words, segments):
    """Source-time words -> output-time words after cuts.

    segments: [{"in": source start, "out": source end, "t0": output start}].
    A word survives when at least half of it (or 0.12 s) lies inside one
    segment; it is clipped to that segment. Output is sorted by start and
    non-overlapping. Other keys of each word are copied.
    """
    segs = sorted((float(g["in"]), float(g["out"]), float(g["t0"])) for g in segments
                  if float(g["out"]) > float(g["in"]))
    if not segs:
        return []
    starts = [g[0] for g in segs]
    longest = max(g[1] - g[0] for g in segs)
    out = []
    for w in words or []:
        s, e = float(w["s"]), float(w["e"])
        dur = max(e - s, 1e-6)
        best, best_ov = None, 0.0
        i = bisect.bisect_left(starts, e) - 1
        while i >= 0 and segs[i][0] + longest >= s:   # every segment that can overlap [s, e]
            a, b, _ = segs[i]
            ov = min(e, b) - max(s, a)
            if ov > best_ov:
                best, best_ov = segs[i], ov
            i -= 1
        if best is None or not (best_ov >= 0.5 * dur or best_ov >= 0.12):
            continue
        a, b, t0 = best
        d = dict(w)
        d["s"] = round(t0 + max(s, a) - a, 3)
        d["e"] = round(t0 + min(e, b) - a, 3)
        out.append(d)
    out.sort(key=lambda d: d["s"])
    for i in range(len(out) - 1):
        if out[i]["e"] > out[i + 1]["s"]:
            out[i]["e"] = out[i + 1]["s"]
    return out


def strip(words):
    """Plain {"w", "s", "e"} copies (for the captions track)."""
    return [{"w": w["w"], "s": w["s"], "e": w["e"]} for w in words or []]
