"""
textrender.py - text layers to sprites: the typography of every title,
callout and counter in a render.

    img = render_text(layer)                  # PIL RGBA, img.info["box"] = layout box
    arr, box = sprite(layer, scale=0.5)       # premultiplied float32 RGBA for the compositor

What a text layer can have (timeline spec section 1, `text`):
    wrapping to max_width with balanced lines (no lonely last word), align,
    line_spacing (x font size, like CSS line-height), letter_spacing (px),
    case, a stroke drawn outside the glyphs (round joins), a drop shadow
    (Gaussian, CSS-like: sigma = blur / 2), a box behind the whole block or
    behind each line (`box.mode` "block" | "lines"), highlight colour on
    chosen words with an optional pill behind them, and a vertical
    gradient fill.

Partial renders for the typewriter and words reveals keep the layout of
the full text: the sprite has the same size and the glyphs the same place
at every step, so revealed text never shifts.

The layout box (`info["box"]`) is what anchors align to: the text block
from cap height to baseline (descenders included when the text has them),
plus the box padding when there is a box. Stroke, shadow and glyph
overhang live outside it, so nothing is ever clipped.

Sprites are cached (LRU, about 384 MB) keyed by every input that changes
pixels.

Characters the layer's font has no glyph for (Hindi or other scripts in a
Latin font, symbols) are drawn with the closest weight of an installed font
that has them (fonts.glyph_fallbacks: Nirmala UI on Windows, Kohinoor
Devanagari on macOS, Noto Sans Devanagari...), run by run, so the Latin
words keep the layer's font; a one-line notice names the substitution.
Limits: colour emoji fonts are bitmap-only and cannot be scaled here, so an
emoji draws only from a monochrome emoji or symbol font (Segoe UI Emoji on
Windows) and otherwise as a box, with a notice naming it; right-to-left
scripts render only as well as Pillow's layout engine on the machine.
"""

import collections
import json
import math

import cv2
import numpy as np
from PIL import Image, ImageDraw

from . import fonts, timeline

TEXT_KEYS = ("text", "font", "size", "color", "case", "stroke", "box", "highlight", "max_width", "align",
             "line_spacing", "letter_spacing", "gradient", "shadow", "_tabular", "_reserve")
CACHE_BYTES = 384 * 1024 * 1024


# --------------------------------------------------------------------------- shared drawing helpers

def rrect_mask(h, w, x0, y0, x1, y1, r):
    """Anti-aliased coverage (h x w float32, 0..1) of a rounded rectangle, from its signed distance."""
    if x1 <= x0 or y1 <= y0:
        return np.zeros((h, w), np.float32)
    r = max(0.0, min(r, (x1 - x0) / 2.0, (y1 - y0) / 2.0))
    ys = np.arange(h, dtype=np.float32)[:, None] + 0.5
    xs = np.arange(w, dtype=np.float32)[None, :] + 0.5
    cx, cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0
    qx = np.abs(xs - cx) - ((x1 - x0) / 2.0 - r)
    qy = np.abs(ys - cy) - ((y1 - y0) / 2.0 - r)
    outside = np.sqrt(np.maximum(qx, 0) ** 2 + np.maximum(qy, 0) ** 2)
    inside = np.minimum(np.maximum(qx, qy), 0)
    return np.clip(0.5 - (outside + inside - r), 0, 1).astype(np.float32)


def rgba01(color):
    """'#RRGGBB[AA]' -> (r, g, b, a) floats 0..1."""
    return tuple(v / 255.0 for v in timeline.parse_color(color))


def over(dst, rgb, alpha):
    """Composite a flat colour with a coverage mask over a premultiplied RGBA float image, in place."""
    a = alpha * rgb[3]
    inv = 1.0 - a
    for c in range(3):
        dst[..., c] = rgb[c] * a + dst[..., c] * inv
    dst[..., 3] = a + dst[..., 3] * inv
    return dst


def over_gradient(dst, colors, mask, top, height):
    """Composite a vertical gradient (colours spread from row `top` over `height`) through a mask, in place."""
    cols = np.array([rgba01(c) for c in colors], np.float32)
    u = np.clip((np.arange(dst.shape[0], dtype=np.float32) + 0.5 - top) / max(height, 1.0), 0, 1) * (len(cols) - 1)
    i0 = np.minimum(np.floor(u).astype(int), len(cols) - 2)
    f = (u - i0)[:, None]
    rows = cols[i0] * (1 - f) + cols[i0 + 1] * f  # one colour per row
    a = mask * rows[:, 3][:, None]
    inv = 1.0 - a
    for c in range(3):
        dst[..., c] = rows[:, c][:, None] * a + dst[..., c] * inv
    dst[..., 3] = a + dst[..., 3] * inv
    return dst


def add_shadow(arr, shadow, scale):
    """Premultiplied RGBA with a drop shadow of its own alpha underneath (the canvas must have room)."""
    color = rgba01(shadow.get("color", "#00000099"))
    sigma = max(0.0, float(shadow.get("blur", 0)) / 2.0 * scale)
    dx, dy = float(shadow.get("dx", 0)) * scale, float(shadow.get("dy", 0)) * scale
    a = arr[..., 3]
    if sigma > 0.3:
        a = cv2.GaussianBlur(a, (0, 0), sigma)
    m = np.float32([[1, 0, dx], [0, 1, dy]])
    a = cv2.warpAffine(a, m, (arr.shape[1], arr.shape[0]), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT)
    sh = np.zeros_like(arr)
    over(sh, color, a)
    inv = 1.0 - arr[..., 3:4]
    return arr + sh * inv


def shadow_margin(shadow, scale):
    """Extra px a shadow needs on each side: (left, top, right, bottom)."""
    if not shadow:
        return 0, 0, 0, 0
    reach = float(shadow.get("blur", 0)) * 1.5 * scale
    dx, dy = float(shadow.get("dx", 0)) * scale, float(shadow.get("dy", 0)) * scale
    return (reach + max(0.0, -dx), reach + max(0.0, -dy), reach + max(0.0, dx), reach + max(0.0, dy))


# --------------------------------------------------------------------------- layout

def _apply_case(text, case):
    if case == "upper":
        return text.upper()
    if case == "lower":
        return text.lower()
    if case == "title":
        return " ".join(w[:1].upper() + w[1:] for w in text.split(" "))
    return text


class _FontRuns:
    """The layer's font plus fallback faces for the characters it has no glyph for (Hindi in a Latin
    font, symbols): text is split into runs, each measured, inked and drawn with its own face, so a
    Devanagari word is shaped as a whole and the Latin words keep the layer's font. Offers the parts of
    the Pillow font API the layout uses (getlength, getbbox)."""

    def __init__(self, primary, fallbacks=None):
        self.primary = primary
        self.fb = fallbacks or {}   # char -> Pillow font

    def runs(self, s):
        """[(start, text, font)] covering s; spaces, marks and joiners stay with the run before them."""
        if not self.fb:
            return [(0, s, self.primary)] if s else []
        out = []
        for i, ch in enumerate(s):
            f = self.fb.get(ch)
            if f is None:
                f = out[-1][2] if out and not fonts.needs_glyph(ch) else self.primary
            if out and out[-1][2] is f:
                out[-1][1] += ch
            else:
                out.append([i, ch, f])
        return [tuple(r) for r in out]

    def font_at(self, s, i):
        for start, text, f in self.runs(s):
            if start <= i < start + len(text):
                return f
        return self.primary

    def getlength(self, s):
        if not self.fb:
            return self.primary.getlength(s)
        return sum(f.getlength(t) for _, t, f in self.runs(s))

    def getbbox(self, s, anchor="ls"):
        if not self.fb:
            return self.primary.getbbox(s, anchor=anchor)
        box, x = None, 0.0
        for _, t, f in self.runs(s):
            b = f.getbbox(t, anchor=anchor)
            b = (b[0] + x, b[1], b[2] + x, b[3])
            box = b if box is None else (min(box[0], b[0]), min(box[1], b[1]), max(box[2], b[2]), max(box[3], b[3]))
            x += f.getlength(t)
        return box or self.primary.getbbox(s, anchor=anchor)

    def draw(self, draw, xy, s, offsets, start, **kw):
        """Draw s, which begins at char `start` of a line whose char x positions are offsets (from xy[0])."""
        for j, t, f in self.runs(s):
            draw.text((xy[0] + offsets[start + j], xy[1]), t, font=f, anchor="ls", **kw)


class _Measure:
    """Character positions for a font with letter spacing and optional tabular (fixed-width) digits."""

    def __init__(self, font, letter_spacing, tabular):
        self.font = font
        self.ls = letter_spacing
        self.tab = max(font.getlength(d) for d in "0123456789") if tabular else None

    def offsets(self, s):
        """x of every character start, plus the end: len(s) + 1 values."""
        if self.tab is None:
            # start of char i = width through it minus its own advance: keeps the kern pair before it
            gl, at = self.font.getlength, self.font.font_at
            out = [gl(s[:i + 1]) - at(s, i).getlength(s[i]) + self.ls * i for i in range(len(s))]
            return out + [gl(s) + self.ls * len(s)]
        out, x = [0.0], 0.0
        for i, ch in enumerate(s):
            x += (self.tab if ch.isdigit() else self.font.font_at(s, i).getlength(ch)) + self.ls
            out.append(x)
        return out

    def width(self, s):
        if not s:
            return 0.0
        if self.tab is None:
            return self.font.getlength(s) + self.ls * max(0, len(s) - 1)
        return self.offsets(s)[-1] - self.ls


def _greedy(words, widths, space, max_w):
    lines, cur, cur_w = [], [], 0.0
    for i, w in enumerate(words):
        add = widths[i] if not cur else cur_w + space + widths[i]
        if cur and add > max_w:
            lines.append(cur)
            cur, cur_w = [i], widths[i]
        else:
            cur.append(i)
            cur_w = add
    if cur:
        lines.append(cur)
    return lines


def _wrap(words, extra, measure, max_w):
    """Word indices per line: greedy fit, then balanced to the narrowest width giving as many lines.

    extra[i] is room word i needs besides its glyphs (space for a highlight pill).
    """
    if not words:
        return []
    widths = [measure.width(w) + e for w, e in zip(words, extra)]
    space = measure.width(" ") + measure.ls
    lines = _greedy(words, widths, space, max_w)
    if len(lines) > 1:
        lo, hi = max(widths), max_w
        for _ in range(18):  # binary search the width
            mid = (lo + hi) / 2
            if len(_greedy(words, widths, space, mid)) <= len(lines):
                hi = mid
            else:
                lo = mid
        lines = _greedy(words, widths, space, hi + 0.01)
    return lines


def _layout(layer, scale):
    """Positions of every word at this scale (independent of reveal state)."""
    size = max(1.0, float(layer.get("size", 96)) * scale)
    text = _apply_case(str(layer.get("text") or ""), layer.get("case"))
    found, _ = fonts.glyph_fallbacks(layer.get("font"), text + str(layer.get("_reserve") or ""))
    faces = {}
    for ch, e in found.items():  # one Pillow font per fallback face, shared by its characters
        key = (e["path"], e.get("index", 0))
        faces[key] = faces.get(key) or fonts.load_face(e, size)
        found[ch] = faces[key]
    font = _FontRuns(fonts.load(layer.get("font"), size), found)
    ls = float(layer.get("letter_spacing") or 0) * scale
    measure = _Measure(font, ls, bool(layer.get("_tabular")))
    max_w = float(layer.get("max_width") or 1e9) * scale
    paras = [p.split() for p in text.split("\n")]
    n_words = sum(len(p) for p in paras)
    hl_set = _hl_indices(layer, n_words)
    hl_box = (layer.get("highlight") or {}).get("box")
    gap = _pad(hl_box, scale)[0] if hl_box and hl_set else 0.0  # pills get room, so they never touch neighbours

    lines = []  # each: {"words": [(global index, word)], "text": str}
    gi = 0
    for pw in paras:
        if not pw:
            lines.append({"words": [], "text": ""})
            continue
        extra = [2 * gap if gi + i in hl_set else 0.0 for i in range(len(pw))]
        for idxs in _wrap(pw, extra, measure, max_w):
            ws = [(gi + i, pw[i]) for i in idxs]
            lines.append({"words": ws, "text": " ".join(w for _, w in ws)})
        gi += len(pw)

    cap_h = -font.primary.getbbox("H", anchor="ls")[1]
    adv = size * float(layer.get("line_spacing") or 1.1)
    reserve = measure.width(_apply_case(str(layer["_reserve"]), layer.get("case"))) if layer.get("_reserve") else 0.0
    for ln in lines:
        shifts, sh, ws = [], 0.0, ln["words"]
        for k, (g, _) in enumerate(ws):
            # a pill gets its padding as extra space, at line edges too, so alignment includes it
            if g in hl_set and (k == 0 or ws[k - 1][0] not in hl_set):
                sh += gap
            shifts.append(sh)
            if g in hl_set and (k + 1 == len(ws) or ws[k + 1][0] not in hl_set):
                sh += gap
        ln["shifts"] = shifts
        ln["w"] = measure.width(ln["text"]) + sh
        ln["offsets"] = measure.offsets(ln["text"])
    block_w = max([ln["w"] for ln in lines] + [reserve, 1.0])
    align = layer.get("align", "center")
    ink_top, ink_bottom = -cap_h, 0.0
    for i, ln in enumerate(lines):
        ln["x"] = 0.0 if align == "left" else (block_w - ln["w"]) if align == "right" else (block_w - ln["w"]) / 2
        ln["base"] = i * adv
        if ln["text"]:
            bb = font.getbbox(ln["text"], anchor="ls")
            ln["ink"] = (bb[1], bb[3])
            # horizontal ink beyond the advance box (italic swashes, overhanging f / j)
            ln["ink_x"] = (min(0.0, bb[0]), max(0.0, bb[2] - font.getlength(ln["text"])))
            if i == 0:
                ink_top = min(ink_top, bb[1])
            if i == len(lines) - 1:
                ink_bottom = max(ink_bottom, bb[3])
        else:
            ln["ink"] = (-cap_h, 0.0)
            ln["ink_x"] = (0.0, 0.0)
        # where each word starts in the line (char offset of the word)
        pos, starts = 0, []
        for _, w in ln["words"]:
            pos = ln["text"].index(w, pos)
            starts.append(pos)
            pos += len(w)
        ln["starts"] = starts
    top = ink_top
    bottom = (len(lines) - 1) * adv + ink_bottom
    return {"font": font, "measure": measure, "lines": lines, "size": size, "cap_h": cap_h, "adv": adv,
            "block": (0.0, top, block_w, bottom), "n_words": n_words, "hl_set": hl_set}


# --------------------------------------------------------------------------- drawing

def _hl_indices(layer, n_words):
    hl = layer.get("highlight") or {}
    out = set()
    for w in hl.get("words") or []:
        if isinstance(w, int) and not isinstance(w, bool) and -n_words <= w < n_words:
            out.add(w % n_words)
    return out


def _pad(box, scale):
    pad = box.get("pad", [0, 0])
    if not isinstance(pad, (list, tuple)):
        pad = [pad, pad]
    return float(pad[0]) * scale, float(pad[1]) * scale


def _draw_run(draw, measure, x, y, ln, start, s, per_char, **kw):
    """Draw s (which begins at char `start` of the line; x includes the word's shift) at baseline y."""
    if not per_char:
        measure.font.draw(draw, (x, y), s, ln["offsets"], start, **kw)
        return
    for k, ch in enumerate(s):
        if not ch.isspace():
            cx = x + ln["offsets"][start + k]
            f = measure.font.font_at(ln["text"], start + k)
            if measure.tab is not None and ch.isdigit():
                cx += (measure.tab - f.getlength(ch)) / 2  # centre digits in their cell
            draw.text((cx, y), ch, font=f, anchor="ls", **kw)


def _render(layer, scale, n_chars, n_words):
    lay = _layout(layer, scale)
    font, measure, lines = lay["font"], lay["measure"], lay["lines"]
    bx0, by0, bx1, by1 = lay["block"]
    stroke = layer.get("stroke") or {}
    sw = float(stroke.get("width") or 0) * scale
    box = layer.get("box")
    hl = layer.get("highlight") or {}
    hl_box = hl.get("box")
    hl_set = lay["hl_set"]

    # the layout box: the text block plus the box padding
    lx0, ly0, lx1, ly1 = bx0, by0, bx1, by1
    if box:
        px, py = _pad(box, scale)
        lx0, ly0, lx1, ly1 = bx0 - px, by0 - py, bx1 + px, by1 + py
    # room for everything drawn outside the layout box: measured glyph ink plus the stroke
    inked = [ln for ln in lines if ln["text"]] or lines
    safe = sw + 2 + 0.02 * lay["size"]
    ex0 = min(lx0, min(ln["x"] + ln["ink_x"][0] for ln in inked) - safe)
    ex1 = max(lx1, max(ln["x"] + ln["w"] + ln["ink_x"][1] for ln in inked) + safe)
    ey0 = min(ly0, min(ln["base"] + ln["ink"][0] for ln in inked) - safe)
    ey1 = max(ly1, max(ln["base"] + ln["ink"][1] for ln in inked) + safe)
    if hl_box:
        hpx, hpy = _pad(hl_box, scale)
        ex0, ey0, ex1, ey1 = ex0 - hpx, ey0 - hpy, ex1 + hpx, ey1 + hpy
    sl, st, sr, sb = shadow_margin(layer.get("shadow"), scale)
    ox, oy = math.floor(ex0 - sl) - 1, math.floor(ey0 - st) - 1
    W = int(math.ceil(ex1 + sr - ox)) + 2
    H = int(math.ceil(ey1 + sb - oy)) + 2
    off_x, off_y = -ox, -oy  # layout coords -> sprite px

    arr = np.zeros((H, W, 4), np.float32)
    desc = max([ln["ink"][1] for ln in lines if ln["text"]] + [0.0])

    if box:
        color = rgba01(box.get("color"))
        r = float(box.get("radius", 0)) * scale
        px, py = _pad(box, scale)
        if box.get("mode") == "lines":
            m = np.zeros((H, W), np.float32)
            for ln in lines:
                if ln["text"]:
                    x0 = off_x + ln["x"] - px
                    m = np.maximum(m, rrect_mask(H, W, x0, off_y + ln["base"] - lay["cap_h"] - py,
                                                 x0 + ln["w"] + 2 * px, off_y + ln["base"] + desc + py, r))
            over(arr, color, m)
        else:
            over(arr, color, rrect_mask(H, W, off_x + lx0, off_y + ly0, off_x + lx1, off_y + ly1, r))

    # visibility of each character (typewriter) or word (words reveal)
    char_budget = n_chars
    vis = []  # per line: list of (word slot, chars visible)
    for ln in lines:
        row = []
        for k, (gi, w) in enumerate(ln["words"]):
            if n_words is not None:
                row.append(len(w) if gi < n_words else 0)
            elif char_budget is None:
                row.append(len(w))
            else:
                c = max(0, min(len(w), char_budget))
                char_budget -= c
                row.append(c)
        vis.append(row)

    if hl_box and hl_set:
        color = rgba01(hl_box.get("color"))
        hpx, hpy = _pad(hl_box, scale)
        r = float(hl_box.get("radius", 0)) * scale
        m = np.zeros((H, W), np.float32)
        for ln, row in zip(lines, vis):
            k = 0
            while k < len(ln["words"]):  # one pill per run of consecutive highlighted words
                if ln["words"][k][0] not in hl_set or row[k] == 0:
                    k += 1
                    continue
                j = k
                while j + 1 < len(ln["words"]) and ln["words"][j + 1][0] in hl_set and row[j + 1] > 0:
                    j += 1
                s0 = ln["starts"][k]
                s1 = ln["starts"][j] + row[j]
                x0 = off_x + ln["x"] + ln["offsets"][s0] + ln["shifts"][k] - hpx
                x1 = off_x + ln["x"] + ln["offsets"][s1] + ln["shifts"][j] - (measure.ls if s1 > s0 else 0) + hpx
                ink = font.getbbox(ln["text"][s0:s1], anchor="ls")  # the run's own height: centred on caps/digits
                m = np.maximum(m, rrect_mask(H, W, x0, off_y + ln["base"] + min(-lay["cap_h"], ink[1]) - hpy, x1,
                                             off_y + ln["base"] + max(0.0, ink[3]) + hpy, r))
                k = j + 1
        over(arr, color, m)

    per_char = measure.ls != 0 or measure.tab is not None
    stroke_m = Image.new("L", (W, H)) if sw > 0 else None
    fill_m = Image.new("L", (W, H))
    hl_m = Image.new("L", (W, H)) if hl_set else None
    d_stroke = ImageDraw.Draw(stroke_m) if stroke_m else None
    d_fill, d_hl = ImageDraw.Draw(fill_m), ImageDraw.Draw(hl_m) if hl_m else None
    for ln, row in zip(lines, vis):
        y = off_y + ln["base"]
        for k, (gi, w) in enumerate(ln["words"]):
            c = row[k]
            if c <= 0:
                continue
            s = w[:c]
            x = off_x + ln["x"] + ln["shifts"][k]
            if d_stroke:
                _draw_run(d_stroke, measure, x, y, ln, ln["starts"][k], s, per_char, fill=255,
                          stroke_width=max(1, int(round(sw))), stroke_fill=255)
            target = d_hl if gi in hl_set else d_fill
            _draw_run(target, measure, x, y, ln, ln["starts"][k], s, per_char, fill=255)

    if stroke_m is not None:
        over(arr, rgba01(stroke.get("color", "#000000")), np.asarray(stroke_m, np.float32) / 255.0)
    fm = np.asarray(fill_m, np.float32) / 255.0
    if layer.get("gradient"):
        over_gradient(arr, layer["gradient"], fm, off_y + by0, by1 - by0)
    else:
        over(arr, rgba01(layer.get("color", "#FFFFFF")), fm)
    if hl_m is not None:
        over(arr, rgba01(hl.get("color", "#FFE500")), np.asarray(hl_m, np.float32) / 255.0)
    if layer.get("shadow"):
        arr = add_shadow(arr, layer["shadow"], scale)
    return arr, (off_x + lx0, off_y + ly0, off_x + lx1, off_y + ly1)


# --------------------------------------------------------------------------- cache + API

class _LRU:
    """Least-recently-used cache with a byte budget."""

    def __init__(self, budget):
        self.budget, self.used, self.data = budget, 0, collections.OrderedDict()

    def get(self, key):
        v = self.data.get(key)
        if v is not None:
            self.data.move_to_end(key)
        return v

    def put(self, key, value, nbytes):
        if key in self.data:
            return
        self.data[key] = (value, nbytes)
        self.used += nbytes
        while self.used > self.budget and len(self.data) > 1:
            _, (_, n) = self.data.popitem(last=False)
            self.used -= n


_cache = _LRU(CACHE_BYTES)


def _visible_counts(layer, reveal, words_visible):
    """Reveal state as exact counts, so frames showing the same text share one cache entry."""
    if words_visible is not None:
        return None, max(0, int(words_visible))
    if reveal >= 1:
        return None, None
    text = _apply_case(str(layer.get("text") or ""), layer.get("case"))
    total = sum(1 for ch in text if not ch.isspace())
    return int(math.floor(max(0.0, reveal) * total + 1e-9)), None


def sprite(layer, scale=1.0, reveal=1.0, words_visible=None):
    """(premultiplied float32 RGBA array, layout box (x0, y0, x1, y1) in its px). Cached; do not modify."""
    n_chars, n_words = _visible_counts(layer, reveal, words_visible)
    key = (json.dumps({k: layer.get(k) for k in TEXT_KEYS}, sort_keys=True, default=str),
           round(float(scale), 5), n_chars, n_words)
    hit = _cache.get(key)
    if hit is not None:
        return hit[0]
    value = _render(layer, float(scale), n_chars, n_words)
    _cache.put(key, value, value[0].nbytes)
    return value


def render_text(layer, scale=1.0, reveal=1.0, words_visible=None):
    """A text layer as a tight PIL RGBA image; img.info["box"] is the layout box anchors align to.

    reveal (0..1) shows that fraction of the characters (typewriter);
    words_visible shows the first N words. Both keep the full layout.
    """
    arr, box = sprite(layer, scale, reveal, words_visible)
    a = arr[..., 3:4]
    rgb = np.where(a > 1e-6, arr[..., :3] / np.maximum(a, 1e-6), 0)
    out = np.concatenate([rgb, a], axis=2)
    img = Image.fromarray(np.clip(out * 255 + 0.5, 0, 255).astype(np.uint8), "RGBA")
    img.info["box"] = box
    return img
