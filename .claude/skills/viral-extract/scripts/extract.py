#!/usr/bin/env python3
"""
extract.py - pull the full Style DNA out of a viral video so it can be
recreated: the edit, the on-screen text (font look, size, position, colours,
outline, box, line spacing, words per card, highlight colour, timing), and the
dialogue (pace, pauses, hook, sentence rhythm, re-hooks, CTA).

    python3 extract.py <file-or-url> --out workspace/dna/<slug>

Writes into --out:
    analysis/           ReelMimic's edit analysis (cuts, beats, camera, look, contact sheets)
    transcript.json     words with start/end seconds (when a transcript could be had)
    text_cards.json     every on-screen text card with timing and measured style
    text_crops/         a crop of the main text cards, for identifying the font by eye
    fonts_sheet.jpg     those crops tiled on one sheet: open it and name the font
    style_dna.json      everything, machine-readable (captions.py reads this)
    STYLE_DNA.md        the recreate spec, human-readable

Transcript sources, best first: --captions FILE (json3, SRT, VTT, or a words
JSON: transcript.json, {"words": [...]} or whisper {"segments": [...]}), the
YouTube captions for a YouTube URL (the original-language speech track, never
the machine translation), local faster-whisper if installed (on the GPU when it
can, else the CPU). None of them is required; without one the dialogue section
says so.

What this cannot do: name a font with certainty from pixels. It measures the
font's weight, width, case and colour and proposes free look-alikes; the
agent then looks at fonts_sheet.jpg and makes the call.
"""

import argparse
import collections
import json
import math
import os
import re
import shutil
import statistics
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SKILLS = os.path.dirname(os.path.dirname(HERE))
ANALYZE = os.path.join(SKILLS, "reel-mimic", "scripts", "analyze.py")
FONTS_REF = os.path.join(os.path.dirname(HERE), "references", "fonts.json")

FILLERS = {"um", "uh", "like", "literally", "basically", "actually", "honestly", "okay", "ok",
           "right", "yeah", "so", "anyway", "kinda", "gonna", "wanna"}
PIVOTS = {"but", "so", "now", "wait", "except", "then", "turns", "here's", "heres", "plot",
          "and", "because", "until", "however", "instead", "suddenly"}
CTA_WORDS = {"follow", "comment", "save", "share", "subscribe", "link", "bio", "part", "dm", "like"}
WORD_RE = re.compile(r"[A-Za-z0-9$%']+")
CONTRACTION_RE = re.compile(r"\b\w+'(?:s|t|re|ve|ll|d|m)\b", re.I)


# --------------------------------------------------------------------------- transcript

def parse_json3(data):
    """Words from YouTube json3 captions.

    Auto-captions time every word (one seg per word, with tOffsetMs). Uploaded captions hold a
    whole line in one seg, so that line's words are spread evenly from the seg's start to the next
    seg, the end of the event (dDurationMs) or the next event, whichever comes first, the way
    parse_srt_vtt spreads a cue; giving them all the line's start time would read as words spoken
    at once followed by a pause."""
    events = [ev for ev in data.get("events") or []
              if any((s.get("utf8") or "").strip() for s in ev.get("segs") or [])]
    words = []
    for k, ev in enumerate(events):
        start = ev.get("tStartMs") or 0
        end = start + (ev.get("dDurationMs") or 0)
        nxt_ev = (events[k + 1].get("tStartMs") or 0) if k + 1 < len(events) else None
        if nxt_ev is not None and nxt_ev > start:
            end = min(end, nxt_ev) if end > start else nxt_ev
        segs = [s for s in ev.get("segs") or [] if (s.get("utf8") or "").strip()]
        offs = [start + (s.get("tOffsetMs") or 0) for s in segs]
        for i, seg in enumerate(segs):
            toks = seg["utf8"].split()
            a = offs[i]
            z = offs[i + 1] if i + 1 < len(segs) else end
            if len(toks) == 1:
                words.append({"w": toks[0], "s": round(a / 1000.0, 3), "e": None})
                continue
            if z <= a:   # no length known: a nominal 0.35 s per word (about 170 wpm)
                z = a + 350 * len(toks)
            step = (z - a) / float(len(toks))
            for j, w in enumerate(toks):
                words.append({"w": w, "s": round((a + j * step) / 1000.0, 3),
                              "e": round((a + (j + 1) * step) / 1000.0, 3)})
    for i, w in enumerate(words):
        if w["e"] is None:
            nxt = words[i + 1]["s"] if i + 1 < len(words) else w["s"] + 0.4
            w["e"] = round(min(max(nxt, w["s"]), w["s"] + 1.0), 3)
    return words


def _ts(t):
    t = t.replace(",", ".")
    parts = [float(p) for p in t.split(":")]
    while len(parts) < 3:
        parts.insert(0, 0.0)
    return parts[0] * 3600 + parts[1] * 60 + parts[2]


def parse_srt_vtt(text):
    words = []
    blocks = re.split(r"\n\s*\n", text.replace("\r", ""))
    for b in blocks:
        m = re.search(r"([\d:.,]+)\s*-->\s*([\d:.,]+)", b)
        if not m:
            continue
        a, z = _ts(m.group(1)), _ts(m.group(2).split()[0])
        body = b[m.end():].strip()
        body = re.sub(r"<[^>]+>", "", body)
        toks = body.split()
        if not toks or z <= a:
            continue
        step = (z - a) / len(toks)
        for i, w in enumerate(toks):
            words.append({"w": w, "s": round(a + i * step, 3), "e": round(a + (i + 1) * step, 3)})
    return words


def _segment_words(segments):
    """Whisper-style segments -> words (their own word lists, or the text spread over the segment)."""
    out = []
    for seg in segments or []:
        if not isinstance(seg, dict):
            continue
        if isinstance(seg.get("words"), list):
            out.extend(seg["words"])
            continue
        s, e, toks = seg.get("start"), seg.get("end"), str(seg.get("text") or "").split()
        if not isinstance(s, (int, float)) or not isinstance(e, (int, float)) or not toks or e <= s:
            continue
        step = (e - s) / float(len(toks))
        out.extend({"w": t, "s": s + i * step, "e": s + (i + 1) * step} for i, t in enumerate(toks))
    return out


def words_from_json(data, name="words json"):
    """[{"w", "s", "e"}] from any words-JSON shape: YouTube json3 ({"events": ...}), a list of
    {"w"|"word"|"text", "s"|"start", "e"|"end"} (transcript.json), {"words": [...]}, or whisper-style
    {"segments": [{"words": [...]}]} / {"segments": [{"start", "end", "text"}]}. A token holding several
    words is spread over its time. Raises ValueError for a shape it does not know."""
    if isinstance(data, dict):
        if "events" in data:
            return parse_json3(data)
        if isinstance(data.get("words"), list):
            data = data["words"]
        elif isinstance(data.get("segments"), list):
            data = _segment_words(data["segments"])
        else:
            raise ValueError("%s: no words found (expected a list of {w, s, e}, {\"words\": [...]}, "
                             "{\"segments\": [...]} or YouTube json3)" % name)
    if not isinstance(data, list):
        raise ValueError("%s: not a words file" % name)
    if data and isinstance(data[0], dict) and isinstance(data[0].get("words"), list) and "w" not in data[0]:
        data = _segment_words(data)
    pick = lambda d, *ks: next((d[k] for k in ks if d.get(k) is not None), None)
    rows = []
    for d in data:
        if not isinstance(d, dict):
            continue
        text, s, e = pick(d, "w", "word", "text"), pick(d, "s", "start"), pick(d, "e", "end")
        if text is None or not isinstance(s, (int, float)) or not str(text).strip():
            continue
        rows.append((float(s), float(e) if isinstance(e, (int, float)) else None, str(text).split()))
    rows.sort(key=lambda r: r[0])
    words = []
    for i, (s, e, toks) in enumerate(rows):
        if e is None or e < s:
            nxt = rows[i + 1][0] if i + 1 < len(rows) else s + 0.4
            e = min(max(nxt, s), s + 1.0)
        step = (e - s) / float(len(toks))
        words.extend({"w": t, "s": round(s + k * step, 3), "e": round(s + (k + 1) * step, 3)}
                     for k, t in enumerate(toks))
    return words


CAPTION_EXTS = (".json3", ".vtt", ".srt")


def load_captions(path):
    """Words from a captions or words file: json3, SRT, VTT or any words-JSON shape (words_from_json)."""
    with open(path, encoding="utf-8", errors="replace") as f:
        raw = f.read()
    low = path.lower()
    if low.endswith((".json3", ".json")) or raw.lstrip().startswith(("{", "[")):
        return words_from_json(json.loads(raw), os.path.basename(path))
    return parse_srt_vtt(raw)


def caption_lang(name):
    """The language code of a yt-dlp captions file ("subs.hi-orig.json3" -> "hi-orig")."""
    stem = os.path.basename(name)
    for ext in CAPTION_EXTS:
        if stem.lower().endswith(ext):
            stem = stem[:-len(ext)]
    return stem.split(".", 1)[1] if "." in stem else ""


def pick_captions(files, lang=None):
    """The captions file that holds what was actually said, best timed, among yt-dlp's
    "subs.<lang>.<ext>" downloads; None when there is none.

    Order: YouTube's original-language speech track ("<lang>-orig": auto captions with a time per
    word), then a track in the video's own language (`lang`, from its metadata), then English, then
    any other track; json3 before VTT before SRT. When the video's language is known and is not
    English, English tracks are left out: they are YouTube's machine translation, not their words."""
    base = lambda code: code.lower().replace("_", "-").split("-")[0]
    want = base(lang) if lang else None
    ranked = []
    for f in files or []:
        ext = os.path.splitext(f)[1].lower()
        if ext not in CAPTION_EXTS:
            continue
        code = caption_lang(f)
        b = base(code)
        english = b in ("en", "eng")
        if code.lower().endswith("-orig"):
            rank = 0 if want is None or b == want else 1
        elif want and b == want:
            rank = 2
        elif english and want not in (None, "en", "eng"):
            continue
        elif english:
            rank = 3
        else:
            rank = 4
        ranked.append((rank, CAPTION_EXTS.index(ext), len(code), f))
    return min(ranked)[3] if ranked else None


def youtube_captions(url, tmp):
    out = os.path.join(tmp, "subs")
    subprocess.run([sys.executable, "-m", "yt_dlp", "--quiet", "--no-warnings", "--skip-download",
                    "--write-subs", "--write-auto-subs", "--sub-langs", "en.*,en,.*-orig",
                    "--sub-format", "json3/vtt/srt", "-o", out, url], capture_output=True, timeout=180)
    best = pick_captions([f for f in os.listdir(tmp) if f.startswith("subs.")])
    return load_captions(os.path.join(tmp, best)) if best else None


GPU_ERROR_RE = re.compile(r"cuda|cublas|cudnn|nvrtc|libcu|gpu|nvidia|device", re.I)


def whisper_words(media, model="small", lang=None):
    """Words with times from local faster-whisper, or None when it is not installed.

    device="auto" picks CUDA whenever an NVIDIA driver is present, but pip's faster-whisper ships
    without cuBLAS / cuDNN, so on many Windows gaming laptops the first segment raises ("Library
    cublas64_12.dll is not found"). Such a GPU failure is retried once on the CPU, which always works."""
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        return None

    def run(device):
        m = WhisperModel(model, device=device, compute_type="int8")
        segs, _ = m.transcribe(media, word_timestamps=True, language=lang, vad_filter=True)
        words = []
        for seg in segs:   # a generator: GPU library errors surface here, so it is read inside the try
            for w in seg.words or []:
                words.append({"w": w.word.strip(), "s": round(w.start, 3), "e": round(w.end, 3)})
        return words

    try:
        return run("auto")
    except (RuntimeError, OSError, ValueError) as e:
        if not GPU_ERROR_RE.search(str(e)):
            raise
        print("whisper: the GPU could not run it (%s); transcribing on the CPU instead" % str(e)[:160],
              file=sys.stderr)
        return run("cpu")


# --------------------------------------------------------------------------- dialogue DNA

def _clean(w):
    return re.sub(r"[^a-z0-9$%']", "", w.lower().replace("’", "'"))


def sentences_from_words(words, pause=0.45):
    """Punctuation when the transcript has it; otherwise a pause ends a sentence."""
    has_punct = sum(1 for w in words if re.search(r"[.!?]$", w["w"])) >= max(2, len(words) // 40)
    out, cur = [], []
    for i, w in enumerate(words):
        cur.append(w)
        gap = words[i + 1]["s"] - w["e"] if i + 1 < len(words) else 9
        end = re.search(r"[.!?]$", w["w"]) if has_punct else gap >= pause
        if end or gap >= 1.2:
            out.append(cur)
            cur = []
    if cur:
        out.append(cur)
    return out


def dialogue_dna(words, duration=None):
    if not words:
        return None
    toks = [_clean(w["w"]) for w in words]
    n = len(words)
    start, end = words[0]["s"], words[-1]["e"]
    speech = max(0.1, end - start)
    sents = sentences_from_words(words)
    lens = [len(s) for s in sents]
    gaps = [round(words[i + 1]["s"] - words[i]["e"], 3) for i in range(n - 1)]
    pauses = [g for g in gaps if g >= 0.3]
    per100 = lambda c: round(100.0 * c / n, 1)

    hook_words = [w for w in words if w["s"] < start + 3.0]
    first10 = [w for w in words if w["s"] < start + 10.0]

    openers = collections.Counter(_clean(s[0]["w"]) for s in sents if s)
    pivots = [round(s[0]["s"], 2) for s in sents[1:] if _clean(s[0]["w"]) in PIVOTS]
    pivot_gaps = [round(b - a, 2) for a, b in zip(pivots, pivots[1:])]

    grams = collections.Counter()
    for k in (2, 3):
        for i in range(n - k + 1):
            g = " ".join(toks[i:i + k])
            if all(toks[i:i + k]):
                grams[g] += 1
    signature = [g for g, c in grams.most_common(40) if c >= 2][:12]

    tail = [w for w in words if w["s"] >= end - 5.0]
    tail_txt = {_clean(w["w"]) for w in tail}
    first_sent = {_clean(w["w"]) for w in sents[0]} if sents else set()
    last_sent = {_clean(w["w"]) for w in sents[-1]} if sents else set()
    content = lambda s: {x for x in s if len(x) > 3}
    loop_overlap = len(content(first_sent) & content(last_sent))

    text = " ".join(w["w"] for w in words)
    return {
        "words": n,
        "time_to_first_word_s": round(start, 2),
        "speech_seconds": round(speech, 1),
        "speech_coverage": round(speech / duration, 2) if duration else None,
        "wpm": round(n / speech * 60),
        "wpm_first_10s": round(len(first10) / max(0.1, min(10.0, speech)) * 60),
        "hook": {"text": " ".join(w["w"] for w in hook_words), "words": len(hook_words),
                 "first_sentence": " ".join(w["w"] for w in sents[0]) if sents else ""},
        "sentences": {"count": len(sents), "mean_words": round(statistics.mean(lens), 1),
                      "stdev_words": round(statistics.pstdev(lens), 1), "max_words": max(lens),
                      "short_share": round(sum(1 for x in lens if x <= 5) / len(lens), 2)},
        "pauses": {"count": len(pauses), "per_min": round(len(pauses) / speech * 60, 1),
                   "mean_s": round(statistics.mean(pauses), 2) if pauses else 0,
                   "longest_s": max(pauses) if pauses else 0},
        "per_100_words": {
            "you": per100(sum(1 for t in toks if t in ("you", "your", "you're", "yours"))),
            "i": per100(sum(1 for t in toks if t in ("i", "me", "my", "i'm", "i've", "mine"))),
            "we": per100(sum(1 for t in toks if t in ("we", "us", "our", "we're"))),
            "numbers": per100(sum(1 for t in toks if re.search(r"\d", t))),
            "fillers": per100(sum(1 for t in toks if t in FILLERS)),
            "contractions": per100(len(CONTRACTION_RE.findall(text))),
            "questions": per100(sum(1 for s in sents if s[-1]["w"].endswith("?"))),
        },
        "openers": [o for o, _ in openers.most_common(8)],
        "rehooks": {"times_s": pivots, "mean_gap_s": round(statistics.mean(pivot_gaps), 1) if pivot_gaps else None},
        "signature_phrases": signature,
        "cta": {"text": " ".join(w["w"] for w in tail),
                "asks": sorted(tail_txt & CTA_WORDS)},
        "loop_ending": loop_overlap >= 2,
    }


# --------------------------------------------------------------------------- on-screen text

def _ocr_engine():
    try:
        from rapidocr_onnxruntime import RapidOCR
    except ImportError:
        return None
    try:  # keep the OCR stack from phoning home; everything here runs offline
        import onnxruntime
        onnxruntime.disable_telemetry_events()
    except Exception:
        pass
    return RapidOCR()


def ocr_frame(engine, img, min_conf=0.6):
    res, _ = engine(img)
    boxes = []
    for pts, text, conf in res or []:
        conf = float(conf)
        text = text.strip()
        if conf < min_conf or not text:
            continue
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        boxes.append({"text": text, "conf": round(conf, 3),
                      "x0": min(xs), "y0": min(ys), "x1": max(xs), "y1": max(ys)})
    return boxes


def group_lines(boxes):
    """Stack lines that sit on top of each other into one block (one card)."""
    boxes = sorted(boxes, key=lambda b: b["y0"])
    blocks = []
    for b in boxes:
        h = b["y1"] - b["y0"]
        placed = False
        for blk in blocks:
            last = blk[-1]
            lh = last["y1"] - last["y0"]
            gap = b["y0"] - last["y1"]
            overlap = min(b["x1"], last["x1"]) - max(b["x0"], last["x0"])
            if -0.3 * lh <= gap <= 0.9 * max(h, lh) and overlap > -0.2 * (b["x1"] - b["x0"]) \
                    and 0.6 < h / max(1, lh) < 1.6:
                blk.append(b)
                placed = True
                break
        if not placed:
            blocks.append([b])
    return blocks


def _lum(c):
    return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]


def _dist(a, b):
    return math.sqrt(sum((x - y) ** 2 for x, y in zip(a, b)))


def _hex(c):
    return "#%02X%02X%02X" % tuple(int(max(0, min(255, v))) for v in c)


def measure_style(img, blk):
    """Colours, outline, box, weight and width of one text block (img is RGB)."""
    import numpy as np
    import cv2
    H, W = img.shape[:2]
    x0 = int(min(b["x0"] for b in blk)); x1 = int(max(b["x1"] for b in blk))
    y0 = int(min(b["y0"] for b in blk)); y1 = int(max(b["y1"] for b in blk))
    line_h = statistics.median(b["y1"] - b["y0"] for b in blk)
    pad = max(4, int(line_h * 0.35))
    X0, Y0, X1, Y1 = max(0, x0 - pad), max(0, y0 - pad), min(W, x1 + pad), min(H, y1 + pad)
    crop = img[Y0:Y1, X0:X1]
    inner = img[max(0, y0):y1, max(0, x0):x1]
    if inner.size == 0 or crop.size == 0:
        return {}
    gray = cv2.cvtColor(inner, cv2.COLOR_RGB2GRAY)
    _, th = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    masks = [th > 0, th == 0]

    def border_share(m):
        edge = np.concatenate([m[0, :], m[-1, :], m[:, 0], m[:, -1]])
        return edge.mean() if edge.size else 1

    text_mask = min(masks, key=border_share)
    if text_mask.mean() > 0.7:
        text_mask = ~text_mask
    ink = float(text_mask.mean())

    core = cv2.erode(text_mask.astype("uint8"), np.ones((2, 2), "uint8")).astype(bool)
    core = core if core.sum() > 20 else text_mask
    px = inner[core].reshape(-1, 3).astype("float32")
    if len(px) < 10:
        return {}
    fill = np.median(px, axis=0)
    highlight = None
    if len(px) > 60:
        crit = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 20, 1.0)
        _, lab, cen = cv2.kmeans(px, 2, None, crit, 3, cv2.KMEANS_PP_CENTERS)
        share = np.bincount(lab.ravel(), minlength=2) / len(lab)
        if _dist(cen[0], cen[1]) > 90 and share.min() > 0.18:
            major, minor = (0, 1) if share[0] >= share[1] else (1, 0)
            fill, highlight = cen[major], cen[minor]

    # rings around the glyphs, inside the padded crop
    full = np.zeros(crop.shape[:2], "uint8")
    full[max(0, y0) - Y0:y1 - Y0, max(0, x0) - X0:x1 - X0] = text_mask.astype("uint8")
    k = lambda r: np.ones((2 * r + 1, 2 * r + 1), "uint8")
    r1 = max(1, int(line_h * 0.05))
    d1 = cv2.dilate(full, k(r1))
    d2 = cv2.dilate(full, k(r1 * 3))
    d3 = cv2.dilate(full, k(r1 * 6))
    ring1 = crop[(d1 > 0) & (full == 0)].reshape(-1, 3).astype("float32")
    ring2 = crop[(d3 > 0) & (d2 == 0)].reshape(-1, 3).astype("float32")
    bg = np.median(ring2, axis=0) if len(ring2) else None
    if highlight is not None and len(ring2) > 30:
        # a second colour that also lives in the background is leak, not a highlight
        crit = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 20, 1.0)
        kk = min(4, len(ring2))
        _, _, bgc = cv2.kmeans(ring2, kk, None, crit, 2, cv2.KMEANS_PP_CENTERS)
        if min(_dist(highlight, c) for c in bgc) < 70:
            fill, highlight = np.median(px, axis=0), None
    outline = None
    outline_px = 0
    if len(ring1) and bg is not None:
        r1c = np.median(ring1, axis=0)
        if _dist(r1c, fill) > 80 and _dist(r1c, bg) > 45:
            outline = r1c
            # grow outward one pixel at a time while the ring is still outline-coloured
            prev = full
            for r in range(1, max(2, int(line_h * 0.25))):
                cur = cv2.dilate(full, k(r))
                ring = crop[(cur > 0) & (prev == 0)].reshape(-1, 3).astype("float32")
                if not len(ring):
                    break
                rc = np.median(ring, axis=0)
                if _dist(rc, r1c) >= _dist(rc, bg):
                    break
                outline_px = r
                prev = cur
    box = None
    if len(ring2) > 30:
        sd = float(np.mean(np.std(ring2, axis=0)))
        if sd < 14 and _dist(bg, fill) > 60:
            # a box is a patch that differs from what lies around it; a flat
            # background behind plain text is not a box
            o = 2 * pad
            OX0, OY0, OX1, OY1 = max(0, X0 - o), max(0, Y0 - o), min(W, X1 + o), min(H, Y1 + o)
            outer_mask = np.ones((OY1 - OY0, OX1 - OX0), bool)
            outer_mask[Y0 - OY0:Y1 - OY0, X0 - OX0:X1 - OX0] = False
            outer = img[OY0:OY1, OX0:OX1][outer_mask].reshape(-1, 3).astype("float32")
            near = float(np.mean(np.linalg.norm(outer - bg, axis=1) < 35)) if len(outer) else 1.0
            if near < 0.5:
                box, outline, outline_px = bg, None, 0

    lines = sorted(blk, key=lambda b: b["y0"])
    gaps = [lines[i + 1]["y0"] - lines[i]["y1"] for i in range(len(lines) - 1)]
    chars = sum(len(b["text"].replace(" ", "")) for b in blk)
    width_ratio = sum(b["x1"] - b["x0"] for b in blk) / max(1, chars) / max(1, line_h)
    centers = [(b["x0"] + b["x1"]) / 2 for b in lines]
    lefts = [b["x0"] for b in lines]
    align = "center"
    if len(lines) > 1:
        align = "left" if statistics.pstdev(lefts) < statistics.pstdev(centers) * 0.5 else "center"
    elif abs((x0 + x1) / 2 - W / 2) > W * 0.12:
        align = "left" if (x0 + x1) / 2 < W / 2 else "right"
    return {
        "line_height_pct": round(line_h / H * 100, 2),
        "outline_pct": round(outline_px / H * 100, 2),
        "line_gap_ratio": round(statistics.mean(gaps) / line_h, 2) if gaps else None,
        "center_x_pct": round((x0 + x1) / 2 / W * 100, 1),
        "center_y_pct": round((y0 + y1) / 2 / H * 100, 1),
        "width_pct": round((x1 - x0) / W * 100, 1),
        "align": align,
        "fill": _hex(fill),
        "highlight": _hex(highlight) if highlight is not None else None,
        "outline": _hex(outline) if outline is not None else None,
        "box": _hex(box) if box is not None else None,
        "ink_ratio": round(ink, 3),
        "width_ratio": round(width_ratio, 3),
        "_bbox": [X0, Y0, X1, Y1],
    }


def case_style(text):
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return "none"
    up = sum(c.isupper() for c in letters) / len(letters)
    if up > 0.9:
        return "upper"
    if up < 0.05:
        return "lower"
    words = [w for w in text.split() if w[:1].isalpha()]
    if words and sum(w[0].isupper() for w in words) / len(words) > 0.8:
        return "title"
    return "sentence"


def norm_text(t):
    return re.sub(r"[^a-z0-9]", "", t.lower())


def track_cards(samples, H):
    """samples: [(t, [block...])] -> cards that persist across consecutive samples."""
    open_cards, cards = [], []
    for t, blocks in samples:
        still = []
        for blk in blocks:
            key = norm_text(" ".join(b["text"] for b in sorted(blk["lines"], key=lambda b: b["y0"])))
            if not key:
                continue
            cy = blk["style"].get("center_y_pct", 0)
            match = None
            for c in open_cards:
                if abs(c["_cy"] - cy) < 8 and (c["_key"] == key or _similar(c["_key"], key) > 0.8):
                    match = c
                    break
            if match:
                match["end"] = t
                match["_n"] += 1
                if len(key) > len(match["_key"]):
                    match["_key"], match["text"] = key, blk["text"]
                still.append(match)
            else:
                c = {"text": blk["text"], "start": t, "end": t, "style": blk["style"],
                     "_key": key, "_cy": cy, "_n": 1, "_frame": blk["frame"]}
                cards.append(c)
                still.append(c)
        open_cards = still
    return cards


def _similar(a, b):
    if not a or not b:
        return 0.0
    sa, sb = set(a[i:i + 2] for i in range(len(a) - 1)), set(b[i:i + 2] for i in range(len(b) - 1))
    return len(sa & sb) / max(1, len(sa | sb))


def word_break(token, vocab, max_len=20):
    """Split 'YOURFIRST' into ['your', 'first'] using words actually spoken."""
    t = token.lower()
    if not t or t in vocab or not vocab:
        return [token]
    best = [None] * (len(t) + 1)
    best[0] = []
    for i in range(1, len(t) + 1):
        for j in range(max(0, i - max_len), i):
            if best[j] is not None and t[j:i] in vocab:
                cand = best[j] + [t[j:i]]
                if best[i] is None or len(cand) < len(best[i]):
                    best[i] = cand
    return best[-1] or [token]


def count_words(text, vocab):
    n = 0
    for tok in WORD_RE.findall(text):
        n += len(word_break(tok, vocab))
    return n


def text_dna(video, out, fps=4.0, max_seconds=180, vocab=None):
    import cv2
    engine = _ocr_engine()
    if engine is None:
        return None, []
    cap = cv2.VideoCapture(video)
    vfps = cap.get(cv2.CAP_PROP_FPS) or 30
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    W, H = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    duration = total / vfps if total else max_seconds
    scale = min(1.0, 960.0 / max(W, H))
    step = max(1, int(round(vfps / fps)))
    samples, frames = [], {}
    idx = 0
    while idx < total and idx / vfps <= max_seconds:
        cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
        ok, bgr = cap.read()
        if not ok:
            break
        if scale < 1:
            bgr = cv2.resize(bgr, (int(W * scale), int(H * scale)), interpolation=cv2.INTER_AREA)
        rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
        t = round(idx / vfps, 2)
        blocks = []
        for blk in group_lines(ocr_frame(engine, rgb)):
            style = measure_style(rgb, blk)
            if not style:
                continue
            text = " / ".join(b["text"] for b in sorted(blk, key=lambda b: b["y0"]))
            blocks.append({"lines": blk, "style": style, "text": text, "frame": t})
        if blocks:
            frames[t] = rgb
        samples.append((t, blocks))
        idx += step
    cap.release()

    n_samples = max(1, len(samples))
    cards = track_cards(samples, H)
    pinned_ids = {id(c) for c in cards if c["_n"] >= 0.8 * n_samples and n_samples > 4}
    persistent = [{"text": c["text"], "center_y_pct": c["style"]["center_y_pct"],
                   "line_height_pct": c["style"]["line_height_pct"], "fill": c["style"]["fill"],
                   "box": c["style"]["box"], "outline": c["style"]["outline"],
                   "case": case_style(c["text"])} for c in cards if id(c) in pinned_ids]
    cards = [c for c in cards if id(c) not in pinned_ids]
    short = {id(c) for c in cards if c["_n"] == 1 and any(
        o is not c and o["_n"] > 1 and abs(o["start"] - c["start"]) <= 1.5 / fps and abs(o["_cy"] - c["_cy"]) < 8
        for o in cards)}
    cards = [c for c in cards if id(c) not in short]
    dt = 1.0 / fps
    for c in cards:
        c["end"] = round(c["end"] + dt, 2)
        c["duration"] = round(c["end"] - c["start"], 2)
        c["words"] = count_words(c["text"], vocab or set())
        c["case"] = case_style(c["text"])
        c["lines"] = c["text"].count(" / ") + 1

    # crops of the cards that matter most, for the font call
    crops_dir = os.path.join(out, "text_crops")
    os.makedirs(crops_dir, exist_ok=True)
    chosen = sorted(cards, key=lambda c: (-c["duration"], c["start"]))[:12]
    tiles = []
    for i, c in enumerate(chosen):
        img = frames.get(c["_frame"])
        if img is None:
            continue
        X0, Y0, X1, Y1 = c["style"]["_bbox"]
        crop = cv2.cvtColor(img[Y0:Y1, X0:X1], cv2.COLOR_RGB2BGR)
        if crop.size == 0:
            continue
        path = os.path.join(crops_dir, "card_%02d.png" % (i + 1))
        if imwrite(path, crop):
            c["crop"] = os.path.relpath(path, out)
        tiles.append(crop)
    if tiles:
        _sheet(tiles, os.path.join(out, "fonts_sheet.jpg"))

    for c in cards:
        for k in [k for k in c if k.startswith("_")]:
            c.pop(k)
        c["style"].pop("_bbox", None)

    covered = sum(1 for _, b in samples if b) / n_samples
    return summarize_text(cards, covered, duration, persistent, vocab), cards


def _sheet(tiles, path, width=900):
    import numpy as np
    import cv2
    rows = []
    for t in tiles:
        h, w = t.shape[:2]
        s = min(1.0, width / w)
        t = cv2.resize(t, (max(1, int(w * s)), max(1, int(h * s))))
        canvas = np.full((t.shape[0] + 12, width, 3), 40, "uint8")
        canvas[6:6 + t.shape[0], 0:t.shape[1]] = t
        rows.append(canvas)
    return imwrite(path, np.vstack(rows))


def imwrite(path, img, params=()):
    """cv2.imwrite for any path: on Windows OpenCV opens files in the ANSI code page, so a folder name
    with Hindi or accented letters fails there, silently. Encoding in memory and writing with Python
    works everywhere. Returns True when the file was written."""
    import cv2
    ok, buf = cv2.imencode(os.path.splitext(path)[1] or ".png", img, list(params))
    if not ok:
        print("could not encode %s" % os.path.basename(path), file=sys.stderr)
        return False
    with open(path, "wb") as f:
        f.write(buf.tobytes())
    return True


def _majority(vals, share=0.4):
    """The most common non-empty value, only if it is on at least `share` of cards."""
    present = [v for v in vals if v is not None]
    if not vals or len(present) < share * len(vals):
        return None
    return collections.Counter(present).most_common(1)[0][0]


def _mode(vals):
    vals = [v for v in vals if v is not None]
    return collections.Counter(vals).most_common(1)[0][0] if vals else None


def spoken_share(text, vocab):
    toks = [t.lower() for t in WORD_RE.findall(text)]
    if not toks or not vocab:
        return 0.0
    parts = [p for t in toks for p in word_break(t, vocab)]
    return sum(1 for p in parts if p.lower() in vocab) / len(parts)


def summarize_text(cards, covered, duration, persistent, vocab=None):
    if not cards:
        return {"cards": 0, "coverage": round(covered, 2), "pinned": persistent}
    main = [c for c in cards if c["duration"] >= 0.2] or cards
    # the caption track is the most common vertical zone weighted by time on screen
    zone = lambda c: int(c["style"]["center_y_pct"] // 10)
    # captions are big, sit in one band, and say what is being said
    weight = collections.Counter()
    for c in main:
        weight[zone(c)] += c["duration"] * c["style"]["line_height_pct"] * (1 + 2 * spoken_share(c["text"], vocab))
    top_zone = weight.most_common(1)[0][0]
    track = [c for c in main if abs(zone(c) - top_zone) <= 1]
    others = [c for c in main if c not in track]
    wpc = [c["words"] for c in track]
    durs = [c["duration"] for c in track]
    med_words = statistics.median(wpc)
    mode = "word-by-word" if med_words <= 3 and statistics.median(durs) < 1.5 else \
        "phrase" if med_words <= 8 else "text-card"
    st = [c["style"] for c in track]
    caption = {
        "mode": mode,
        "cards": len(track),
        "words_per_card": {"median": med_words, "max": max(wpc)},
        "seconds_per_card": round(statistics.median(durs), 2),
        "lines_per_card": _mode([c["lines"] for c in track]),
        "case": _mode([c["case"] for c in track]),
        "line_height_pct": round(statistics.median(s["line_height_pct"] for s in st), 2),
        "outline_pct": round(statistics.median(s.get("outline_pct", 0) for s in st), 2),
        "line_gap_ratio": _mode([s["line_gap_ratio"] for s in st]),
        "center_x_pct": round(statistics.median(s["center_x_pct"] for s in st), 1),
        "center_y_pct": round(statistics.median(s["center_y_pct"] for s in st), 1),
        "max_width_pct": round(max(s["width_pct"] for s in st), 1),
        "align": _mode([s["align"] for s in st]),
        "fill": _mode([s["fill"] for s in st]),
        "highlight": _majority([s["highlight"] for s in st]),
        "outline": _majority([s["outline"] for s in st]),
        "box": _majority([s["box"] for s in st]),
        "ink_ratio": round(statistics.median(s["ink_ratio"] for s in st), 3),
        "width_ratio": round(statistics.median(s["width_ratio"] for s in st), 3),
    }
    glyph_h = caption["line_height_pct"] - (2 * caption["outline_pct"] if caption["outline"] else 0)
    caption["font_size_pct"] = font_size_pct(glyph_h, caption["case"])
    caption["font_look"] = font_look(caption)
    caption["font_candidates"] = font_candidates(caption["font_look"])
    return {
        "cards": len(cards),
        "coverage": round(covered, 2),
        "first_text_s": min(c["start"] for c in cards),
        "caption_track": caption,
        "other_text": [{"text": c["text"], "start": c["start"], "duration": c["duration"],
                        "center_y_pct": c["style"]["center_y_pct"],
                        "line_height_pct": c["style"]["line_height_pct"]} for c in others][:20],
        "pinned": persistent,
    }


# OCR boxes hug capitals tightly but lowercase boxes include ascenders and
# descenders, so ink, width and size readings are normalised per case.
# Calibrated on Inter Thin..Black rendered and read back through the OCR.
CASE_NORM = {"upper": (1.0, 1.0, 0.73), "title": (1.6, 1.7, 1.08),
             "sentence": (1.75, 1.85, 1.10), "lower": (1.75, 1.85, 1.10), "none": (1.0, 1.0, 0.9)}


def font_look(cap):
    ink_k, width_k, _ = CASE_NORM.get(cap["case"] or "sentence", CASE_NORM["sentence"])
    ink, wr = cap["ink_ratio"] * ink_k, cap["width_ratio"] * width_k
    weight = "black" if ink > 0.46 else "bold" if ink > 0.34 else "regular" if ink > 0.22 else "light"
    width = "condensed" if wr < 0.62 else "wide" if wr > 1.0 else "normal"
    return {"weight": weight, "width": width, "case": cap["case"] or "sentence",
            "outlined": bool(cap["outline"]), "boxed": bool(cap["box"])}


def font_size_pct(line_height_pct, case):
    """Estimated font size (em) as % of frame height, from the OCR line box."""
    return round(line_height_pct / CASE_NORM.get(case or "sentence", CASE_NORM["sentence"])[2], 2)


def font_candidates(look, path=FONTS_REF, n=4):
    try:
        with open(path, encoding="utf-8") as f:
            ref = json.load(f)["fonts"]
    except Exception:
        return []
    def score(f):
        s = 0
        s += 2 if look["weight"] in f["weights"] else 0
        s += 2 if f["width"] == look["width"] else 0
        s += 1 if look["case"] in f.get("cases", ["upper", "title", "sentence", "lower"]) else 0
        return s
    ranked = sorted(ref, key=lambda f: (-score(f), f["rank"]))
    return [f["name"] for f in ranked[:n]]


# --------------------------------------------------------------------------- edit DNA

def edit_dna(report, words=None):
    # analyze.py keeps the shot list in "shots" and the counts in "pacing";
    # older reports had the counts in "shots"
    shots = report.get("pacing") or report.get("shots") or {}
    if not isinstance(shots, dict):
        shots = {}
    details = report.get("shot_details") or []
    dur = (report.get("video") or {}).get("duration") or report.get("duration")
    cuts = [d["start"] for d in details[1:]] if details else []
    beats = (report.get("audio") or {}).get("beats") or []
    on_beat = None
    if cuts and beats:
        hit = sum(1 for c in cuts if min(abs(c - b) for b in beats) <= 0.08)
        on_beat = round(hit / len(cuts), 2)
    moves = collections.Counter()
    for d in details:
        for k in d.get("camera", {}).get("kind", []):
            moves[k] += 1
    return {
        "duration_s": dur,
        "shots": shots.get("count"),
        "mean_shot_s": shots.get("mean_shot_s"),
        "cuts_per_10s": round(len(cuts) / dur * 10, 1) if dur else None,
        "first_cut_s": cuts[0] if cuts else None,
        "cuts_on_beat": on_beat,
        "camera_moves": dict(moves),
        "bpm": (report.get("audio") or {}).get("bpm"),
        "look": report.get("look_summary"),
        "flashes": len(report.get("flashes") or []),
    }


# --------------------------------------------------------------------------- report

def render_md(dna):
    L = ["# Style DNA", "", "Source: %s" % dna["source"], ""]
    e = dna.get("edit") or {}
    L += ["## Edit", "",
          "- Length %ss, %s shots, mean shot %ss, %s cuts per 10 s, first cut at %ss" % (
              e.get("duration_s"), e.get("shots"), e.get("mean_shot_s"), e.get("cuts_per_10s"), e.get("first_cut_s")),
          "- Cuts on the beat: %s   BPM: %s   flashes: %s" % (e.get("cuts_on_beat"), e.get("bpm"), e.get("flashes")),
          "- Camera moves: %s" % (e.get("camera_moves") or "n/a"), ""]
    t = dna.get("text") or {}
    cap = t.get("caption_track")
    L += ["## On-screen text", ""]
    if not cap:
        L += ["No on-screen text found (or OCR unavailable).", ""]
    else:
        L += ["- Text on screen %d%% of the video; first text at %ss" % (t["coverage"] * 100, t["first_text_s"]),
              "- Caption mode: **%s**, %s words per card (max %s), %ss per card, %s line(s)" % (
                  cap["mode"], cap["words_per_card"]["median"], cap["words_per_card"]["max"],
                  cap["seconds_per_card"], cap["lines_per_card"]),
              "- Position: centre at %s%% across, %s%% down; align %s; max width %s%% of frame" % (
                  cap["center_x_pct"], cap["center_y_pct"], cap["align"], cap["max_width_pct"]),
              "- Size: font size about %s%% of frame height (%dpx on a 1080x1920 canvas); line gap %s x line height" % (
                  cap["font_size_pct"], round(cap["font_size_pct"] * 19.2), cap["line_gap_ratio"]),
              "- Colour: fill %s, highlight %s, outline %s (%s%% of frame height thick), box %s" % (
                  cap["fill"], cap["highlight"], cap["outline"], cap["outline_pct"], cap["box"]),
              "- Case: %s.  Font look: %s weight, %s width" % (
                  cap["case"], cap["font_look"]["weight"], cap["font_look"]["width"]),
              "- Free look-alike candidates: %s  (confirm against fonts_sheet.jpg)" % ", ".join(cap["font_candidates"]),
              ""]
        for p in t.get("pinned") or []:
            L += ["- Pinned all video: \"%s\" at %s%% down, line height %s%%, fill %s, box %s, outline %s, %s case" % (
                p["text"], p["center_y_pct"], p["line_height_pct"], p["fill"], p["box"], p["outline"], p["case"])]
        L += [""]
        if t.get("other_text"):
            L += ["Other text (titles, labels):", ""]
            L += ["- %ss for %ss: \"%s\"" % (o["start"], o["duration"], o["text"]) for o in t["other_text"][:8]]
            L += [""]
    d = dna.get("dialogue")
    L += ["## Dialogue", ""]
    if not d:
        L += ["No transcript available. Pass --captions, use a YouTube URL, or install faster-whisper.", ""]
    else:
        s = d["sentences"]
        p = d["per_100_words"]
        L += ["- Hook (first 3 s, %d words): \"%s\"" % (d["hook"]["words"], d["hook"]["text"]),
              "- First word at %ss. Pace %s wpm (first 10 s: %s wpm)" % (
                  d["time_to_first_word_s"], d["wpm"], d["wpm_first_10s"]),
              "- Sentences: %s, mean %s words (stdev %s, max %s), %d%% are 5 words or fewer" % (
                  s["count"], s["mean_words"], s["stdev_words"], s["max_words"], s["short_share"] * 100),
              "- Pauses: %s per minute, mean %ss, longest %ss" % (
                  d["pauses"]["per_min"], d["pauses"]["mean_s"], d["pauses"]["longest_s"]),
              "- Per 100 words: you %s, I %s, numbers %s, contractions %s, fillers %s, questions %s" % (
                  p["you"], p["i"], p["numbers"], p["contractions"], p["fillers"], p["questions"]),
              "- Sentence openers: %s" % ", ".join(d["openers"]),
              "- Re-hooks (pivot words opening a sentence) at: %s s%s" % (
                  d["rehooks"]["times_s"] or "none",
                  ", one every %ss" % d["rehooks"]["mean_gap_s"] if d["rehooks"]["mean_gap_s"] else ""),
              "- Signature phrases: %s" % (", ".join(d["signature_phrases"]) or "none repeated"),
              "- Ending: \"%s\" asks=%s loop=%s" % (d["cta"]["text"], d["cta"]["asks"], d["loop_ending"]), ""]
    L += ["## Recreate it", "",
          "Captions: `python3 .claude/skills/viral-extract/scripts/captions.py --style style_dna.json --video YOUR.mp4 --out out.mp4`",
          "Script: `/script-forge` with this file as the style reference.", ""]
    return "\n".join(L)


def _safe_stdio():
    """Never die on printing: Windows writes piped stdout as cp1252, which cannot encode most
    transcripts that are not English; a character it cannot show becomes '?' instead of a crash
    after every file is written."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass


def main():
    _safe_stdio()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("src")
    ap.add_argument("--out", required=True)
    ap.add_argument("--fps", type=float, default=4.0, help="OCR samples per second (default 4)")
    ap.add_argument("--captions", help="transcript file: json3, srt, vtt, or words json")
    ap.add_argument("--whisper-model", default="small")
    ap.add_argument("--lang")
    ap.add_argument("--no-whisper", action="store_true")
    ap.add_argument("--skip-edit", action="store_true", help="skip the ReelMimic edit analysis")
    a = ap.parse_args()

    os.makedirs(a.out, exist_ok=True)
    is_url = re.match(r"https?://", a.src) is not None
    adir = os.path.join(a.out, "analysis")
    report = {}
    video = a.src
    if not a.skip_edit:
        print("edit: analysing cuts, beats, camera, look", file=sys.stderr)
        r = subprocess.run([sys.executable, ANALYZE, a.src, "--out", adir], capture_output=True, text=True)
        if r.returncode != 0:
            print(r.stderr[-800:], file=sys.stderr)
        rp = os.path.join(adir, "report.json")
        if os.path.exists(rp):
            report = json.load(open(rp, encoding="utf-8"))
        for name in ("proxy.mp4", "source.mp4"):
            if os.path.exists(os.path.join(adir, name)):
                video = os.path.join(adir, name)
                break
    elif is_url:
        os.makedirs(adir, exist_ok=True)
        video = os.path.join(adir, "source.mp4")
        subprocess.run([sys.executable, "-m", "yt_dlp", "-q", "-f", "mp4/best", "-o", video, a.src])
    if not os.path.exists(video):
        sys.exit("could not get the video: %s" % a.src)

    print("transcript", file=sys.stderr)
    words, source = None, None
    if a.captions:
        try:
            words, source = load_captions(a.captions), "captions file"
        except ValueError as e:
            sys.exit(str(e))
        if not words:
            print("  no words in %s" % a.captions, file=sys.stderr)
    elif is_url and re.search(r"youtu\.?be", a.src):
        words, source = youtube_captions(a.src, a.out), "youtube captions"
    if not words and not a.no_whisper:
        audio = os.path.join(adir, "audio.wav")
        words = whisper_words(audio if os.path.exists(audio) else video, a.whisper_model, a.lang)
        source = "faster-whisper" if words else None
    if words:
        json.dump(words, open(os.path.join(a.out, "transcript.json"), "w", encoding="utf-8"), indent=1)

    print("on-screen text: OCR at %s fps" % a.fps, file=sys.stderr)
    vocab = {_clean(w["w"]).replace("'", "") for w in words or []} - {""}
    text, cards = text_dna(video, a.out, fps=a.fps, vocab=vocab)
    if text is None:
        print("  OCR not installed: pip install rapidocr-onnxruntime", file=sys.stderr)
    json.dump(cards, open(os.path.join(a.out, "text_cards.json"), "w", encoding="utf-8"), indent=1, ensure_ascii=False)

    dur = (report.get("video") or {}).get("duration")
    dna = {
        "source": a.src,
        "edit": edit_dna(report) if report else None,
        "text": text,
        "dialogue": dialogue_dna(words, dur),
        "transcript_source": source,
    }
    json.dump(dna, open(os.path.join(a.out, "style_dna.json"), "w", encoding="utf-8"), indent=2, ensure_ascii=False)
    md = render_md(dna)
    open(os.path.join(a.out, "STYLE_DNA.md"), "w", encoding="utf-8").write(md)
    print(md)


if __name__ == "__main__":
    main()
