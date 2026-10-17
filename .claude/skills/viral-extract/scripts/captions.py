#!/usr/bin/env python3
"""
captions.py - put captions on YOUR video in the exact style a viral video
used: same font look, size, position, colours, outline or box, words per
card, highlight on the spoken word, and timing.

    python3 captions.py --style workspace/dna/<slug>/style_dna.json \
        --video my_take.mp4 --out my_take_captioned.mp4

Where the words and their timing come from, best first:
    --words words.json     [{"w": "Nobody", "s": 0.0, "e": 0.32}, ...] (extract.py's transcript.json works)
    --srt file.srt         an SRT/VTT you already have (CapCut and Premiere export these)
    (nothing)              local faster-whisper transcribes the video, if installed
    --script script.txt    no recording yet: timed at the reference's pace, for a preview

Options
    --font "Montserrat Black"   override the font (default: the DNA's first candidate)
    --fonts-dir fonts/          folder of .ttf/.otf files to use (otherwise system fonts)
    --pop                       scale-pop each card in (the common 'bounce' caption)
    --ass-only                  write the .ass subtitle file and stop (import it into an editor)

The .ass file is always written next to --out. CapCut, Premiere and DaVinci
can import it, or ffmpeg burns it in (this script does that by default).
"""

import argparse
import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import extract  # noqa: E402

DEFAULT_CAPTION = {
    "mode": "word-by-word", "words_per_card": {"median": 3, "max": 4}, "lines_per_card": 1,
    "case": "upper", "font_size_pct": 4.6, "center_x_pct": 50.0, "center_y_pct": 68.0,
    "max_width_pct": 84.0, "align": "center", "fill": "#FFFFFF", "highlight": "#FFE500",
    "outline": "#000000", "outline_pct": 0.35, "box": None, "font_candidates": ["Montserrat Black"],
}

# libass sizes text by the font's full line height, not the em box; this
# factor maps the extracted em size to the ASS Fontsize. Calibrated with a
# round trip through extract.py (see tests/test_captions.py).
ASS_SIZE_K = 1.2


def ass_color(hexcol, alpha=0):
    hexcol = (hexcol or "#FFFFFF").lstrip("#")
    r, g, b = hexcol[0:2], hexcol[2:4], hexcol[4:6]
    return "&H%02X%s%s%s" % (alpha, b, g, r)


def ass_time(t):
    t = max(0.0, t)
    h = int(t // 3600)
    m = int(t % 3600 // 60)
    s = t % 60
    return "%d:%02d:%05.2f" % (h, m, s)


def display_token(word):
    """What a caption shows: trailing . , ; : are dropped (they end cards, nobody reads them);
    decimals like 3.5 and ? ! stay."""
    return re.sub(r"[.,;:]+$", "", word) or word


def apply_case(text, case):
    if case == "upper":
        return text.upper()
    if case == "lower":
        return text.lower()
    if case == "title":
        return " ".join(w[:1].upper() + w[1:] for w in text.split(" "))
    return text


def words_from_script(text, wpm=165, start=0.3):
    toks = text.split()
    sec = 60.0 / max(60, wpm)
    out, t = [], start
    for w in toks:
        dur = sec * (1.6 if re.search(r"[.!?]$", w) else 1.0)
        out.append({"w": w, "s": round(t, 3), "e": round(t + sec, 3)})
        t += dur
    return out


def chunk(words, per_card=3, max_chars=22, pause=0.35):
    """Group words into cards: at most per_card words, break on punctuation and pauses."""
    cards, cur = [], []
    for i, w in enumerate(words):
        cur.append(w)
        text_len = len(" ".join(x["w"] for x in cur))
        gap = words[i + 1]["s"] - w["e"] if i + 1 < len(words) else 9
        nxt_len = text_len + 1 + len(words[i + 1]["w"]) if i + 1 < len(words) else 0
        # don't strand the last word of a sentence alone on its own card
        orphan = (i + 1 < len(words) and re.search(r"[.!?]$", words[i + 1]["w"]) and len(cur) == per_card
                  and nxt_len <= max_chars and words[i + 1]["s"] - w["e"] < pause)
        if (len(cur) >= per_card and not orphan) or len(cur) > per_card or re.search(r"[.!?,;:]$", w["w"]) \
                or gap >= pause or nxt_len > max_chars:
            cards.append(cur)
            cur = []
    if cur:
        cards.append(cur)
    return cards


def caption_style(dna_path):
    if not dna_path:
        return dict(DEFAULT_CAPTION)
    with open(dna_path, encoding="utf-8") as f:
        dna = json.load(f)
    cap = ((dna.get("text") or {}).get("caption_track")) or {}
    merged = dict(DEFAULT_CAPTION)
    merged.update({k: v for k, v in cap.items() if v is not None or k in ("highlight", "outline", "box")})
    if not cap:
        print("no caption track in the DNA; using the default bold caption style", file=sys.stderr)
    return merged


def build_ass(words, cap, W, H, font, pop=False):
    size_px = cap["font_size_pct"] / 100.0 * H * ASS_SIZE_K
    outline_px = (cap.get("outline_pct") or 0) / 100.0 * H if cap.get("outline") else 0
    boxed = bool(cap.get("box"))
    border_style = 3 if boxed else 1
    outline_val = max(outline_px, size_px * 0.18) if boxed else outline_px
    outline_col = cap.get("box") if boxed else (cap.get("outline") or "#000000")
    bold = -1 if (cap.get("font_look") or {}).get("weight", "black") in ("bold", "black") else 0
    per_card = max(1, int(round(cap["words_per_card"]["median"])))
    max_w = cap.get("max_width_pct", 84) / 100.0 * W
    max_chars = max(6, int(max_w / (size_px * 0.62))) * max(1, int(cap.get("lines_per_card") or 1))
    x = cap["center_x_pct"] / 100.0 * W
    y = cap["center_y_pct"] / 100.0 * H
    an = 5
    if cap.get("align") == "left":
        an, x = 4, x - max_w / 2
    elif cap.get("align") == "right":
        an, x = 6, x + max_w / 2

    head = [
        "[Script Info]", "ScriptType: v4.00+", "PlayResX: %d" % W, "PlayResY: %d" % H,
        "WrapStyle: 0", "ScaledBorderAndShadow: yes", "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, "
        "Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        "Style: Cap,%s,%d,%s,%s,%s,&H80000000,%d,0,0,0,100,100,0,0,%d,%.1f,0,%d,%d,%d,0,1" % (
            font, round(size_px), ass_color(cap.get("fill")), ass_color(cap.get("highlight") or cap.get("fill")),
            ass_color(outline_col), bold, border_style, outline_val, an,
            int((W - max_w) / 2), int((W - max_w) / 2)),
        "", "[Events]", "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    events = []
    cards = chunk(words, per_card, max_chars)
    for ci, card in enumerate(cards):
        start = card[0]["s"]
        hold = card[-1]["e"] + 0.4
        if ci + 1 < len(cards):
            nxt = cards[ci + 1][0]["s"]
            # stay up until the next card, unless a real pause follows: then clear
            end = hold if nxt - card[-1]["e"] > 0.6 else nxt
        else:
            end = hold
        end = max(end, start + 0.25)
        tokens = [apply_case(display_token(w["w"]), cap.get("case")) for w in card]
        prefix = "{\\an%d\\pos(%d,%d)%s}" % (
            an, x, y, "\\fscx80\\fscy80\\t(0,90,\\fscx100\\fscy100)" if pop else "")
        if cap.get("highlight") and len(card) > 1:
            hl = ass_color(cap["highlight"])
            for wi, w in enumerate(card):
                ws = start if wi == 0 else w["s"]
                we = card[wi + 1]["s"] if wi + 1 < len(card) else end
                parts = [("{\\c%s}%s{\\r}" % (hl, t)) if j == wi else t for j, t in enumerate(tokens)]
                p = prefix if wi == 0 else "{\\an%d\\pos(%d,%d)}" % (an, x, y)
                events.append("Dialogue: 0,%s,%s,Cap,,0,0,0,,%s%s" % (ass_time(ws), ass_time(we), p, " ".join(parts)))
        else:
            text = " ".join(tokens)
            if cap.get("highlight") and len(card) == 1:
                text = "{\\c%s}%s" % (ass_color(cap["highlight"]), text)
            events.append("Dialogue: 0,%s,%s,Cap,,0,0,0,,%s%s" % (ass_time(start), ass_time(end), prefix, text))
    return "\n".join(head + events) + "\n", len(cards)


def probe_size(video):
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                        "stream=width,height", "-of", "json", video], capture_output=True, text=True)
    s = json.loads(r.stdout)["streams"][0]
    return s["width"], s["height"]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--style", help="style_dna.json from extract.py (omit for the default bold style)")
    ap.add_argument("--video", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--words")
    ap.add_argument("--srt")
    ap.add_argument("--script")
    ap.add_argument("--wpm", type=int)
    ap.add_argument("--font")
    ap.add_argument("--fonts-dir")
    ap.add_argument("--pop", action="store_true")
    ap.add_argument("--ass-only", action="store_true")
    ap.add_argument("--whisper-model", default="small")
    a = ap.parse_args()

    cap = caption_style(a.style)
    W, H = probe_size(a.video)

    if a.words:
        words = json.load(open(a.words, encoding="utf-8"))
    elif a.srt:
        words = extract.parse_srt_vtt(open(a.srt, encoding="utf-8", errors="replace").read())
    elif a.script:
        wpm = a.wpm
        if not wpm and a.style:
            wpm = ((json.load(open(a.style, encoding="utf-8")).get("dialogue") or {}).get("wpm"))
        words = words_from_script(open(a.script, encoding="utf-8").read(), wpm or 165)
    else:
        words = extract.whisper_words(a.video, a.whisper_model)
        if not words:
            sys.exit("No words to caption: pass --words, --srt or --script, or pip install faster-whisper")

    font = a.font or (cap.get("font_candidates") or ["Montserrat Black"])[0]
    ass, n = build_ass(words, cap, W, H, font, a.pop)
    ass_path = os.path.splitext(a.out)[0] + ".ass"
    os.makedirs(os.path.dirname(os.path.abspath(ass_path)), exist_ok=True)
    open(ass_path, "w", encoding="utf-8").write(ass)
    print("wrote %s (%d cards, font %s)" % (ass_path, n, font), file=sys.stderr)
    if a.ass_only:
        return
    flt = "ass=%s" % ass_path.replace("\\", "/").replace(":", "\\:")
    if a.fonts_dir:
        flt += ":fontsdir=%s" % a.fonts_dir.replace("\\", "/").replace(":", "\\:")
    r = subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-i", a.video, "-vf", flt,
                        "-c:a", "copy", "-c:v", "libx264", "-pix_fmt", "yuv420p", a.out])
    if r.returncode != 0:
        sys.exit("ffmpeg failed; the .ass file is still there to import into your editor")
    print("wrote %s" % a.out, file=sys.stderr)


if __name__ == "__main__":
    main()
