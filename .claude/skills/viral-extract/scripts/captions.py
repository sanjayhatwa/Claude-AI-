#!/usr/bin/env python3
"""
captions.py - put captions on YOUR video in the exact style a viral video
used: same font look, size, position, colours, outline or box, words per
card, highlight on the spoken word, and timing.

    python3 captions.py --style workspace/dna/<slug>/style_dna.json \
        --video my_take.mp4 --out my_take_captioned.mp4
    python3 captions.py --style workspace/dna/<slug>/style_dna.json \
        --script script.txt --ass-only --out captions/1-hook.ass      (nothing filmed yet)

Where the words and their timing come from, best first:
    --words words.json     [{"w": "Nobody", "s": 0.0, "e": 0.32}, ...] (extract.py's transcript.json),
                           {"words": [...]}, whisper {"segments": [...]} or YouTube json3
    --srt file.srt         an SRT/VTT you already have (CapCut and Premiere export these)
    (nothing)              local faster-whisper transcribes the video, if installed
    --script script.txt    no recording yet: timed at the reference's pace, for a preview

Options
    --font "Montserrat Black"   override the font (default: the DNA's first candidate)
    --fonts-dir fonts/          another folder of .ttf/.otf files (subfolders too)
    --pop                       scale-pop each card in (the common 'bounce' caption)
    --ass-only                  write the .ass subtitle file and stop (import it into an editor);
                                --video is optional then: the .ass is laid out for --size
    --size 1080x1920            frame size for the .ass when there is no --video (default 1080x1920;
                                positions and sizes are fractions of the frame, so any 9:16 video fits)

The .ass file is always written next to --out. CapCut, Premiere and DaVinci
can import it (the computer that opens it needs the font installed), or
ffmpeg burns it in (this script does that by default).

Fonts when burning: the font is found the way Viral Studio's renderer finds
it (--fonts-dir, workspace/fonts from `python -m vstudio fonts install`, then
the system's fonts) and handed to libass as a file. libass cannot choose a
weight inside a variable font (Montserrat[wght].ttf), so a static instance of
the asked weight is made (needs fontTools); a font that is not installed is
reported with the one drawn instead. The last line names the font file used.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(HERE))))
sys.path.insert(0, HERE)
import extract  # noqa: E402

DEFAULT_SIZE = (1080, 1920)

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


def filter_path(path):
    """A file path as an option value inside an ffmpeg -vf filter graph.

    Two parsers read it, so it is escaped twice: first for the filter's option parser
    (\\ ' :), then for the graph parser (\\ ' [ ] , ;). Escaping once breaks on Windows drive
    letters (C:/...) and on any apostrophe. Backslashes become forward slashes first, which
    ffmpeg accepts on Windows too. (The same rule as vstudio.render.ffmpeg_filter_path.)"""
    p = os.path.abspath(path).replace("\\", "/")
    for ch in ("\\", "'", ":"):
        p = p.replace(ch, "\\" + ch)
    for ch in ("\\", "'", "[", "]", ",", ";"):
        p = p.replace(ch, "\\" + ch)
    return p


def probe_size(video):
    """(width, height) of the video's first picture stream; exits with a plain message when there is none."""
    if not os.path.isfile(video):
        sys.exit("no such video: %s" % video)
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                            "stream=width,height", "-of", "json", video], capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
    except OSError:
        sys.exit("ffprobe was not found: install ffmpeg (it includes ffprobe), or write only the .ass "
                 "with --ass-only --size WxH")
    try:
        s = json.loads(r.stdout or "{}")["streams"][0]
        return int(s["width"]), int(s["height"])
    except (ValueError, KeyError, IndexError, TypeError):
        why = (r.stderr or "").strip().splitlines()
        sys.exit("no video stream in %s%s" % (video, ": " + why[-1] if why else ""))


def frame_size(text):
    m = re.fullmatch(r"\s*(\d{2,5})\s*[xX:]\s*(\d{2,5})\s*", text or "")
    if not m:
        raise argparse.ArgumentTypeError("give the size as WIDTHxHEIGHT, e.g. 1080x1920")
    return int(m.group(1)), int(m.group(2))


def libass_font(font, fonts_dir, dest_dir):
    """(font name for the .ass, its weight or None, the folder to give libass as fontsdir).

    libass only finds fonts installed in the system or lying directly in its fontsdir, and it cannot
    set a variable font's weight, so "Montserrat Black" with workspace/fonts/Montserrat/Montserrat[wght].ttf
    silently came out as some other bold font. vstudio.fonts.ass_font looks the font up the way the
    renderer does (fonts_dir via VSTUDIO_FONTS, workspace/fonts, the system), makes a static instance of
    a variable font, copies the file into dest_dir and says which font it uses when the asked one is
    missing. Without vstudio, libass looks the name up itself in fonts_dir and the system.
    """
    if fonts_dir:
        extra = os.environ.get("VSTUDIO_FONTS")
        os.environ["VSTUDIO_FONTS"] = os.pathsep.join([os.path.abspath(fonts_dir)] + ([extra] if extra else []))
    try:
        if REPO not in sys.path:
            sys.path.append(REPO)
        from vstudio import fonts
        os.makedirs(dest_dir, exist_ok=True)
        name, weight = fonts.ass_font(font, dest_dir)
    except Exception as e:  # vstudio missing or broken: never fail the captions over the font check
        print("could not check the font %r (%s): if it is not installed, libass draws another one"
              % (font, e), file=sys.stderr)
        return font, None, fonts_dir
    return name, weight, dest_dir


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--style", help="style_dna.json from extract.py (omit for the default bold style)")
    ap.add_argument("--video", help="the video to caption (optional with --ass-only)")
    ap.add_argument("--out", required=True)
    ap.add_argument("--size", type=frame_size, help="WxH for the .ass without --video (default 1080x1920)")
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
    if not a.video and not a.ass_only:
        ap.error("--video is needed to burn captions in (or --ass-only to write only the .ass file)")

    cap = caption_style(a.style)
    W, H = a.size or (probe_size(a.video) if a.video else DEFAULT_SIZE)

    if a.words:
        try:   # transcript.json, {"words": [...]}, whisper segments or json3: extract.py reads them all
            words = extract.load_captions(a.words)
        except ValueError as e:
            sys.exit(str(e))
    elif a.srt:
        words = extract.parse_srt_vtt(open(a.srt, encoding="utf-8", errors="replace").read())
    elif a.script:
        wpm = a.wpm
        if not wpm and a.style:
            wpm = ((json.load(open(a.style, encoding="utf-8")).get("dialogue") or {}).get("wpm"))
        words = words_from_script(open(a.script, encoding="utf-8").read(), wpm or 165)
    elif not a.video:
        sys.exit("No words to caption: pass --words, --srt or --script (or --video to transcribe it)")
    else:
        words = extract.whisper_words(a.video, a.whisper_model)
        if not words:
            sys.exit("No words to caption: pass --words, --srt or --script, or pip install faster-whisper")

    if not words:
        sys.exit("No words to caption in %s" % (a.words or a.srt or a.script or a.video))
    font = a.font or (cap.get("font_candidates") or ["Montserrat Black"])[0]
    ass_path = os.path.splitext(a.out)[0] + ".ass"
    os.makedirs(os.path.dirname(os.path.abspath(ass_path)), exist_ok=True)
    if a.ass_only:
        ass, n = build_ass(words, cap, W, H, font, a.pop)
        open(ass_path, "w", encoding="utf-8").write(ass)
        print("wrote %s (%d cards, %dx%d, font %s: install it where the file is opened)" % (ass_path, n, W, H, font),
              file=sys.stderr)
        return
    fonts_tmp = tempfile.mkdtemp(prefix="captions-fonts-")
    try:
        font, weight, fontsdir = libass_font(font, a.fonts_dir, fonts_tmp)
        if weight is not None:   # the file's real weight sets the bold flag: no faux-bold smear on heavy fonts
            cap["font_look"] = dict(cap.get("font_look") or {}, weight="bold" if weight >= 600 else "regular")
        ass, n = build_ass(words, cap, W, H, font, a.pop)
        open(ass_path, "w", encoding="utf-8").write(ass)
        files = sorted(os.listdir(fontsdir)) if fontsdir == fonts_tmp else []
        print("wrote %s (%d cards, font %s%s)" % (ass_path, n, font, " from " + files[0] if files else ""),
              file=sys.stderr)
        flt = "ass=filename=%s" % filter_path(ass_path)
        if fontsdir:
            flt += ":fontsdir=%s" % filter_path(fontsdir)
        r = subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-i", a.video, "-vf", flt,
                            "-c:a", "copy", "-c:v", "libx264", "-pix_fmt", "yuv420p", a.out])
    finally:
        shutil.rmtree(fonts_tmp, ignore_errors=True)
    if r.returncode != 0:
        sys.exit("ffmpeg failed; the .ass file is still there to import into your editor")
    print("wrote %s" % a.out, file=sys.stderr)


if __name__ == "__main__":
    main()
