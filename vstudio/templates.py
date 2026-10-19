"""
templates.py - the motion-graphics library: named templates that turn a few
parameters into timeline layers (pure data, see ARCHITECTURE.md section 1).

    from vstudio import templates
    layers = templates.build("hook_title", {"text": "I LOST $300", "highlight": ["$300"]},
                             start=0.0, end=2.4, canvas={"width": 1080, "height": 1920, "fps": 30},
                             style={"accent": "#FFE500", "energy": "high"})

    python -m vstudio templates [list]               the catalog as a table
    python -m vstudio templates --json               the catalog as JSON (for the web UI / planner)
    python -m vstudio templates doc                  the markdown the planner gives Claude
    python -m vstudio templates build stat --params '{"value": "73%"}'      print the layers
    python -m vstudio templates preview cta --out cta.mp4 [--aspect 9:16] [--params JSON]
        [--energy high|medium|calm] [--style JSON] [--size 216x384] [--duration 3]
        [--background clip.mp4|#hex] [--preview] [--spec spec.json]
        (an --out ending in .png writes one still of the settled design instead; --still SECONDS
        picks the moment)
    python -m vstudio templates <command> --help     each command's options

How the designs are made

  * Everything is designed on a 1080x1920 grid and scaled by
    min(W/1080, H/1920) (a little larger on square and landscape frames,
    where text would otherwise read small), so one template works at 9:16,
    4:5, 1:1 and 16:9. Layouts are placed inside the platform safe zone:
    on 9:16 nothing readable sits in the top 11 % or bottom 20 % (where the
    app's own buttons and caption sit) and side margins are 6 %.
  * Text is measured (with the real font through fonts.py when it is
    available, otherwise with a conservative width table), shrunk until it
    fits its box and balanced so lines break evenly instead of leaving one
    orphan word.
  * Timing always fits inside [start, end]: entrances are staggered, exits
    finish at `end`, and short windows animate proportionally faster.
  * Style keys: font, font_secondary, text, accent, bg, bg2, energy.
    energy high = snappy overshoot, stamps, drift and pulses; medium =
    smooth slides with a light overshoot; calm = fades and soft blur-ins,
    no overshoot and no idle motion.
  * Cards and lists sit in a centred column (at most ~1100 design px wide)
    so on 16:9 they read as a block instead of stretching edge to edge.
  * Cards made of several layers move as one rigid group: their scale pop
    is written as matching scale + x/y keyframes about one pivot, so parts
    never drift apart mid-animation. Inside templates x/y keyframes are
    offsets; _Ctx.add turns them into the absolute positions the spec uses.
  * Icons are polygons in a 0..1 box (ICONS), not emoji, so they render the
    same on every machine.
  * Sloppy input is safe: params are coerced to their declared types, bad
    values fall back to defaults, blank text never becomes a layer, and a
    template whose essential params are all blank draws nothing (_REQUIRES)
    instead of an empty card or placeholder words.

Limits: text heights follow textrender's cap-to-baseline box but are still
estimates, so stacked layouts leave a little slack; templates know nothing
about the footage (highlight_box, arrow and callout take the target as a
fraction of the frame from the caller); image_card reads the image's
aspect from the file when it can, relative to the current folder.
"""

import argparse
import copy
import json
import math
import os
import random
import re
import sys
import zlib

DEFAULT_STYLE = {"font": "Montserrat Black", "font_secondary": "Inter Bold", "text": "#FFFFFF",
                 "accent": "#FFE500", "bg": "#0E0E12", "bg2": "#1B1B24", "energy": "high"}

CATALOG = {}

ASPECTS = {"9:16": (1080, 1920), "1:1": (1080, 1080), "4:5": (1080, 1350), "16:9": (1920, 1080)}

_HEX = re.compile(r"^#[0-9A-Fa-f]{6}([0-9A-Fa-f]{2})?$")

# Energy profiles. in/out/gap are seconds for a window of 2.4 s or longer
# (shorter windows scale them down); over = amount of overshoot (0 = none);
# drift = slow push-in over a layer's life; pulse = idle pulse amplitude.
_ENERGY = {
    "high": {"in": 0.34, "out": 0.2, "gap": 0.08, "over": 1.0, "drift": 0.035, "pulse": 0.05,
             "title": "stamp", "pop": "pop", "rise": "slide-up", "exit": "pop-out",
             "group_in": "pop", "group_out": "shrink", "count_ease": "out-expo", "word_pace": 0.17},
    "medium": {"in": 0.45, "out": 0.25, "gap": 0.12, "over": 0.5, "drift": 0.018, "pulse": 0.025,
               "title": "slide-up", "pop": "pop", "rise": "slide-up", "exit": "fade",
               "group_in": "rise", "group_out": "fade", "count_ease": "out-cubic", "word_pace": 0.22},
    "calm": {"in": 0.62, "out": 0.35, "gap": 0.18, "over": 0.0, "drift": 0.0, "pulse": 0.0,
             "title": "blur-in", "pop": "fade", "rise": "fade", "exit": "fade",
             "group_in": "rise", "group_out": "fade", "count_ease": "in-out-cubic", "word_pace": 0.3},
}
_ENERGY_ALIASES = {"hype": "high", "intense": "high", "fast": "high", "punchy": "high", "energetic": "high",
                   "aggressive": "high", "normal": "medium", "mid": "medium", "moderate": "medium",
                   "balanced": "medium", "low": "calm", "chill": "calm", "slow": "calm", "soft": "calm",
                   "relaxed": "calm", "minimal": "calm"}



# =========================================================================== colours

def _rgb(c):
    c = c.lstrip("#")
    return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))


def _hex(rgb):
    return "#%02X%02X%02X" % tuple(max(0, min(255, int(round(v)))) for v in rgb)


def _alpha(c, a):
    """Colour with alpha a (0..1), as #RRGGBBAA."""
    return c[:7].upper() + "%02X" % max(0, min(255, int(round(a * 255))))


def _mix(a, b, t):
    """Blend colour a toward b by t (0..1); alpha is dropped."""
    ra, rb = _rgb(a), _rgb(b)
    return _hex([ra[i] + (rb[i] - ra[i]) * t for i in range(3)])


def _lum(c):
    """Relative luminance 0..1 (WCAG)."""
    def ch(v):
        v /= 255.0
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    r, g, b = _rgb(c)
    return 0.2126 * ch(r) + 0.7152 * ch(g) + 0.0722 * ch(b)


def _on(c, dark="#111114", light="#FFFFFF"):
    """Readable text colour on top of colour c."""
    return dark if _lum(c) > 0.36 else light


# =========================================================================== text measurement

_REF = 100.0
_FONT_OBJ = {}
_WIDTH_CACHE = {}


def _font_obj(font):
    """A Pillow font at the reference size through fonts.py, or None (then the width table is used)."""
    if font not in _FONT_OBJ:
        obj = None
        try:
            from . import fonts as _fonts  # engine module; optional while it is being built
            obj = _fonts.load(font, int(_REF))
            if not hasattr(obj, "getlength"):
                obj = None
        except Exception:
            obj = None
        _FONT_OBJ[font] = obj
    return _FONT_OBJ[font]


def _char_em(ch):
    """Conservative advance of one character in em, for heavy display fonts."""
    if ch == " ":
        return 0.28
    if ch in "il.,:;'|!`\u2019":
        return 0.3
    if ch in "jtfI1()[]":
        return 0.42
    if ch == "r":
        return 0.46
    if ch in "mw":
        return 0.92
    if ch in "MW%@\u2014":
        return 1.02
    if ch.isupper():
        return 0.76
    if ch.isdigit():
        return 0.68
    if ch.islower():
        return 0.6
    if ord(ch) > 0x2000:
        return 1.0
    return 0.62


def _ref_vmetrics(text, font):
    """(ink top above the baseline, ink bottom below it) in px at the reference size."""
    key = ("v", font, text)
    if key not in _WIDTH_CACHE:
        obj = _font_obj(font)
        val = None
        if obj is not None and hasattr(obj, "getbbox"):
            try:
                bb = obj.getbbox(text or "H", anchor="ls")
                val = (float(-bb[1]), float(bb[3]))
            except Exception:
                val = None
        if val is None:
            # conservative table: caps 0.74 em, ascenders 0.78, descenders 0.22
            top = 0.78 if re.search(r"[bdfhklt0-9]", text) else 0.74
            val = (top * _REF, 0.22 * _REF if re.search(r"[gjpqy,;]", text) else 0.0)
        _WIDTH_CACHE[key] = val
    return _WIDTH_CACHE[key]


def _ref_width(text, font):
    key = (font, text)
    if key not in _WIDTH_CACHE:
        obj = _font_obj(font)
        if obj is not None:
            try:
                _WIDTH_CACHE[key] = float(obj.getlength(text))
                return _WIDTH_CACHE[key]
            except Exception:
                pass
        _WIDTH_CACHE[key] = sum(_char_em(ch) for ch in text) * _REF
    return _WIDTH_CACHE[key]


def text_width(text, font, size, letter_spacing=0.0):
    """Estimated advance width in px of one line of text at a font size."""
    if not text:
        return 0.0
    return _ref_width(text, font) * size / _REF + letter_spacing * max(0, len(text) - 1)


def _apply_case(text, case):
    if case == "upper":
        return text.upper()
    if case == "lower":
        return text.lower()
    if case == "title":
        return " ".join(w[:1].upper() + w[1:] for w in text.split(" "))
    return text


def _wrap(words, font, size, width, lsp):
    lines, cur = [], []
    for w in words:
        trial = " ".join(cur + [w])
        if cur and text_width(trial, font, size, lsp) > width:
            lines.append(" ".join(cur))
            cur = [w]
        else:
            cur.append(w)
    if cur:
        lines.append(" ".join(cur))
    return lines


class _Block:
    """A fitted piece of text: the size to use, its lines and the box it fills (px).

    `h` follows textrender's layout box, which is what anchors align to: from
    the cap height (or taller ink) of the first line to the baseline of the
    last line (plus its descenders), lines `size * ls` apart.
    """

    def __init__(self, text, font, size, lines, width, mw, case, ls, lsp):
        self.text, self.font, self.size, self.lines = text, font, size, lines
        self.case, self.ls, self.lsp = case, ls, lsp
        self.w = width          # widest line
        self.mw = mw            # max_width handed to the renderer
        n = max(1, len(lines))
        cap = _ref_vmetrics("H", font)[0]
        top = max(cap, _ref_vmetrics(lines[0] if lines else "H", font)[0])
        bottom = _ref_vmetrics(lines[-1] if lines else "H", font)[1]
        self.top = top * size / _REF          # first baseline sits this far below the box top
        self.h = (n - 1) * size * ls + (top + bottom) * size / _REF

    def line_w(self, i=-1):
        return text_width(self.lines[i], self.font, self.size, self.lsp) if self.lines else 0.0


def fit_text(text, font, size, max_width, lines=2, min_size=None, case="none", ls=1.05, lsp=0.0,
             stroke=0.0, balance=True, prefer=None):
    """Largest size <= `size` at which `text` wraps into at most `lines` lines of `max_width` px.

    A single word never ends up wider than the box: below `min_size` the
    text keeps shrinking until the widest word fits. With `balance` the
    lines are broken evenly (as textrender does), so `w` and `lines` match
    what will be drawn. `prefer` = a smaller line count worth up to 20 %
    of the size (two strong lines read better than three).
    """
    if prefer and prefer < lines:
        blk = fit_text(text, font, size, max_width, prefer, size * 0.8, case, ls, lsp, stroke, balance)
        if len(blk.lines) <= prefer and blk.size >= size * 0.8 - 1:
            return blk
    text = str(text or "").strip()
    shown = _apply_case(text, case)
    words = shown.split() or [""]
    min_size = min_size or size * 0.45
    room = max(8.0, max_width * 0.96 - 2 * stroke)
    s = float(size)
    while True:
        out = _wrap(words, font, s, room, lsp)
        widest_word = max(text_width(w, font, s, lsp) for w in words)
        fits_lines = len(out) <= lines
        if widest_word <= room and (fits_lines or s <= min_size):
            break
        if s <= 6:
            break
        s *= 0.93
    s = max(6.0, s)
    width = max(text_width(line, font, s, lsp) for line in out)
    mw = max_width
    if balance and len(out) > 1:
        lo, hi = widest_word, room
        for _ in range(14):
            mid = (lo + hi) / 2
            if len(_wrap(words, font, s, mid, lsp)) <= len(out):
                hi = mid
            else:
                lo = mid
        out = _wrap(words, font, s, hi, lsp)
        width = max(text_width(line, font, s, lsp) for line in out)
    return _Block(text, font, int(round(s)), out, width + 2 * stroke, mw, case, ls, lsp)


# =========================================================================== icons

def _circle_pts(cx, cy, r, n=40, aspect=1.0):
    """Circle of radius r (in box widths) as normalized points of a box with width/height = aspect."""
    return [[cx + r * math.cos(2 * math.pi * i / n), cy + r * aspect * math.sin(2 * math.pi * i / n)]
            for i in range(n)]


def _qbez(p0, p1, p2, n=12):
    return [[(1 - t) ** 2 * p0[0] + 2 * (1 - t) * t * p1[0] + t * t * p2[0],
             (1 - t) ** 2 * p0[1] + 2 * (1 - t) * t * p1[1] + t * t * p2[1]]
            for t in (i / float(n) for i in range(n + 1))]


def _arc(cx, cy, r, a0, a1, n=16):
    """Points on a circle from angle a0 to a1 (degrees, screen coordinates: 90 = down)."""
    return [[cx + r * math.cos(math.radians(a0 + (a1 - a0) * i / n)),
             cy + r * math.sin(math.radians(a0 + (a1 - a0) * i / n))] for i in range(n + 1)]


def _round_rect_pts(x0, y0, x1, y1, r, n=6):
    pts = []
    for cx, cy, a0 in ((x1 - r, y0 + r, -90), (x1 - r, y1 - r, 0), (x0 + r, y1 - r, 90), (x0 + r, y0 + r, 180)):
        pts += _arc(cx, cy, r, a0, a0 + 90, n)
    return pts


def _stroke_path(pts, width):
    """Polygon outline of a polyline drawn with a given width (averaged normals at joins)."""
    left, right = [], []
    for i, (x, y) in enumerate(pts):
        nx = ny = 0.0
        for a, b in ((i - 1, i), (i, i + 1)):
            if 0 <= a and b < len(pts):
                dx, dy = pts[b][0] - pts[a][0], pts[b][1] - pts[a][1]
                ln = math.hypot(dx, dy) or 1.0
                nx, ny = nx - dy / ln, ny + dx / ln
        ln = math.hypot(nx, ny) or 1.0
        nx, ny = nx / ln * width / 2, ny / ln * width / 2
        left.append([x + nx, y + ny])
        right.append([x - nx, y - ny])
    return left + right[::-1]


def _arrow_path(pts, width, head_len, head_w):
    """A thick polyline ending in a triangular arrow head (one simple polygon).

    The shaft is cut where the path comes within `head_len` of the tip, so
    it ends exactly at the head's base: on a curve (whose last segments are
    much shorter than the head) the outline never runs past the base and
    folds back over itself. A path shorter than the head is all head.
    """
    bx, by = pts[-1]
    cut = None
    for i in range(len(pts) - 2, -1, -1):
        (px, py), (qx, qy) = pts[i], pts[i + 1]
        if math.hypot(px - bx, py - by) >= head_len:
            # the point on segment p->q at distance head_len from the tip (q is closer than head_len)
            dx, dy, fx, fy = qx - px, qy - py, px - bx, py - by
            a, b, c = dx * dx + dy * dy, 2 * (fx * dx + fy * dy), fx * fx + fy * fy - head_len * head_len
            t = 0.0 if a <= 1e-12 else (-b - math.sqrt(max(0.0, b * b - 4 * a * c))) / (2 * a)
            t = min(1.0, max(0.0, t))
            cut = (i, [px + dx * t, py + dy * t])
            break
    if cut is None:                          # shorter than the head: a plain triangle from the first point
        base = list(pts[0])
        shaft = None
    else:
        base = cut[1]
        shaft = pts[:cut[0] + 1] + [base]
        if math.hypot(shaft[-1][0] - shaft[-2][0], shaft[-1][1] - shaft[-2][1]) < 1e-9:
            shaft = shaft[:-1]
    ln = math.hypot(bx - base[0], by - base[1]) or 1.0
    ux, uy = (bx - base[0]) / ln, (by - base[1]) / ln
    nx, ny = -uy, ux
    head = [[base[0] + nx * head_w / 2, base[1] + ny * head_w / 2], [bx, by],
            [base[0] - nx * head_w / 2, base[1] - ny * head_w / 2]]
    if not shaft or len(shaft) < 2:
        return head
    outline = _stroke_path(shaft, width)    # left side forward, then right side backward
    n = len(shaft)
    return outline[:n] + head + outline[n:]


def _rotate_pts(pts, deg, cx=0.5, cy=0.5):
    a = math.radians(deg)
    ca, sa = math.cos(a), math.sin(a)
    return [[cx + (x - cx) * ca - (y - cy) * sa, cy + (x - cx) * sa + (y - cy) * ca] for x, y in pts]


def _heart():
    raw = []
    for i in range(56):
        t = 2 * math.pi * i / 56
        raw.append((16 * math.sin(t) ** 3,
                    -(13 * math.cos(t) - 5 * math.cos(2 * t) - 2 * math.cos(3 * t) - math.cos(4 * t))))
    return _fit_unit(raw, 0.05)


def _fit_unit(raw, margin):
    """Scale points into the 0..1 box with a margin, keeping proportions and centring."""
    xs, ys = [p[0] for p in raw], [p[1] for p in raw]
    w, h = max(xs) - min(xs), max(ys) - min(ys)
    k = (1 - 2 * margin) / max(w, h)
    ox = (1 - w * k) / 2 - min(xs) * k
    oy = (1 - h * k) / 2 - min(ys) * k
    return [[x * k + ox, y * k + oy] for x, y in raw]


def _star_pts(cx=0.5, cy=0.53, ro=0.49, ri=0.2):
    return [[cx + (ro if k % 2 == 0 else ri) * math.cos(math.radians(-90 + 36 * k)),
             cy + (ro if k % 2 == 0 else ri) * math.sin(math.radians(-90 + 36 * k))] for k in range(10)]


def _half_left(pts, xcut=0.5):
    """Clip a polygon to x <= xcut (Sutherland-Hodgman against one edge)."""
    out = []
    for i in range(len(pts)):
        a, b = pts[i - 1], pts[i]
        ina, inb = a[0] <= xcut, b[0] <= xcut
        if ina != inb:
            t = (xcut - a[0]) / (b[0] - a[0])
            out.append([xcut, a[1] + (b[1] - a[1]) * t])
        if inb:
            out.append(list(b))
    return out


def _bell():
    left = []
    for i in range(13):
        y = 0.2 + 0.44 * i / 12
        left.append([0.5 - (0.12 + 0.2 * ((y - 0.2) / 0.44) ** 0.7), y])
    left += [[0.15, 0.7], [0.1, 0.76]]                 # top -> bottom, flaring out at the rim
    dome = _arc(0.5, 0.2, 0.12, 180, 360, 10)[1:-1]
    right = [[1 - x, y] for x, y in left]
    body = list(reversed(left)) + dome + right
    return [{"points": body}, {"points": _circle_pts(0.5, 0.075, 0.05)},
            {"points": _circle_pts(0.5, 0.84, 0.075)}]


def _flame():
    outer = (_arc(0.5, 0.66, 0.29, 90, -12, 14)
             + _qbez([0.5 + 0.29 * math.cos(math.radians(-12)), 0.66 + 0.29 * math.sin(math.radians(-12))],
                     [0.8, 0.3], [0.56, 0.04])[1:]
             + _qbez([0.56, 0.04], [0.46, 0.28], [0.32, 0.3])[1:]
             + _qbez([0.32, 0.3], [0.2, 0.42], [0.21, 0.6])[1:]
             + _arc(0.5, 0.66, 0.29, 192, 90, 12)[1:])
    inner = (_arc(0.5, 0.76, 0.13, 90, -20, 8)
             + _qbez([0.5 + 0.13 * math.cos(math.radians(-20)), 0.76 + 0.13 * math.sin(math.radians(-20))],
                     [0.6, 0.56], [0.5, 0.46])[1:]
             + _qbez([0.5, 0.46], [0.36, 0.6], [0.5 - 0.13 * math.cos(math.radians(20)),
                                                0.76 - 0.13 * math.sin(math.radians(20))])[1:]
             + _arc(0.5, 0.76, 0.13, 200, 90, 8)[1:])
    return [{"points": outer}, {"points": inner, "role": "cut"}]


def _eye():
    top = _qbez([0.03, 0.5], [0.5, 0.0], [0.97, 0.5], 16)
    bottom = _qbez([0.97, 0.5], [0.5, 1.0], [0.03, 0.5], 16)[1:-1]
    return [{"points": top + bottom}, {"points": _circle_pts(0.5, 0.5, 0.2), "role": "cut"},
            {"points": _circle_pts(0.5, 0.5, 0.1)}]


def _comment():
    pts = _round_rect_pts(0.06, 0.1, 0.94, 0.74, 0.18)
    # insert the tail on the bottom edge (that edge runs right -> left between corners 2 and 3)
    k = 14  # index where the bottom-left corner arc begins
    tail = [[0.44, 0.74], [0.17, 0.93], [0.27, 0.74]]
    body = pts[:k] + tail + pts[k:]
    dots = [{"points": _circle_pts(x, 0.42, 0.06), "role": "cut"} for x in (0.3, 0.5, 0.7)]
    return [{"points": body}] + dots


def _warning():
    tri = _round_rect_like_triangle()
    return [{"points": tri},
            {"points": [[0.45, 0.34], [0.55, 0.34], [0.535, 0.64], [0.465, 0.64]], "role": "cut"},
            {"points": _circle_pts(0.5, 0.76, 0.055), "role": "cut"}]


def _round_rect_like_triangle():
    # triangle with softened corners
    corners = [(0.5, 0.06), (0.96, 0.9), (0.04, 0.9)]
    pts = []
    for i, (x, y) in enumerate(corners):
        px, py = corners[i - 1]
        nx, ny = corners[(i + 1) % 3]
        pts.append([x + (px - x) * 0.07, y + (py - y) * 0.07])
        pts.append([x + (nx - x) * 0.07, y + (ny - y) * 0.07])
    return pts


def _search():
    handle = _stroke_path([[0.6, 0.6], [0.9, 0.9]], 0.14)
    return [{"points": _circle_pts(0.42, 0.42, 0.33)}, {"points": _circle_pts(0.42, 0.42, 0.2), "role": "cut"},
            {"points": handle}]


def _pin():
    # a circle with two tangents meeting at the tip: tangent points sit acos(r / d) either side of "down"
    cx, cy, r, tip = 0.5, 0.37, 0.31, 0.96
    side = math.degrees(math.acos(r / (tip - cy)))
    body = _arc(cx, cy, r, 90 + side, 450 - side, 28) + [[cx, tip]]
    return [{"points": body}, {"points": _circle_pts(cx, cy, 0.13), "role": "cut"}]


def _user():
    shoulders = _arc(0.5, 0.98, 0.4, 180, 360, 16)
    return [{"points": _circle_pts(0.5, 0.34, 0.2)}, {"points": shoulders}]


_PLUS = [[0.39, 0.1], [0.61, 0.1], [0.61, 0.39], [0.9, 0.39], [0.9, 0.61], [0.61, 0.61],
         [0.61, 0.9], [0.39, 0.9], [0.39, 0.61], [0.1, 0.61], [0.1, 0.39], [0.39, 0.39]]

# name -> parts; each part is {"points": [[x, y], ...] in 0..1, "role": "fill" | "cut"}.
# "fill" parts take the icon colour, "cut" parts the colour behind the icon
# (that is how holes and details are drawn without polygon holes).
ICONS = {
    "heart": [{"points": _heart()}],
    "star": [{"points": _star_pts()}],
    "check": [{"points": [[0.08, 0.54], [0.22, 0.4], [0.4, 0.58], [0.78, 0.18], [0.92, 0.32], [0.4, 0.86]]}],
    "play": [{"points": [[0.28, 0.14], [0.86, 0.5], [0.28, 0.86]]}],
    "bell": _bell(),
    "arrow": [{"points": [[0.06, 0.39], [0.54, 0.39], [0.54, 0.16], [0.94, 0.5], [0.54, 0.84],
                          [0.54, 0.61], [0.06, 0.61]]}],
    "flame": _flame(),
    "lightning": [{"points": [[0.6, 0.04], [0.2, 0.56], [0.46, 0.56], [0.38, 0.96], [0.8, 0.42],
                              [0.54, 0.42], [0.66, 0.04]]}],
    "eye": _eye(),
    "comment": _comment(),
    "share": [{"points": [[0.04, 0.46], [0.96, 0.06], [0.6, 0.94], [0.46, 0.58]]}],
    "bookmark": [{"points": [[0.22, 0.06], [0.78, 0.06], [0.78, 0.94], [0.5, 0.72], [0.22, 0.94]]}],
    "plus": [{"points": _PLUS}],
    "cross": [{"points": _rotate_pts(_PLUS, 45)}],
    "warning": _warning(),
    "search": _search(),
    "crown": [{"points": [[0.06, 0.26], [0.3, 0.5], [0.5, 0.14], [0.7, 0.5], [0.94, 0.26], [0.84, 0.8],
                          [0.16, 0.8]]}],
    "trend": [{"points": _arrow_path([[0.06, 0.8], [0.36, 0.5], [0.54, 0.66], [0.9, 0.28]], 0.11, 0.2, 0.28)}],
    "document": [{"points": [[0.18, 0.05], [0.62, 0.05], [0.84, 0.27], [0.84, 0.95], [0.18, 0.95]]},
                 {"points": [[0.62, 0.05], [0.62, 0.27], [0.84, 0.27]], "role": "cut"}] +
                [{"points": [[0.3, y], [0.72, y], [0.72, y + 0.06], [0.3, y + 0.06]], "role": "cut"}
                 for y in (0.46, 0.6, 0.74)],
    "cursor": [{"points": [[0.12, 0.04], [0.12, 0.8], [0.3, 0.63], [0.44, 0.94], [0.57, 0.88],
                           [0.43, 0.58], [0.68, 0.58]]}],
    "user": _user(),
    "pin": _pin(),
}

for _parts in ICONS.values():
    for _part in _parts:
        _part.setdefault("role", "fill")
        _part["points"] = [[round(min(1.0, max(0.0, x)), 4), round(min(1.0, max(0.0, y)), 4)]
                           for x, y in _part["points"]]


# =========================================================================== frame, clock, context

class _Frame:
    """Canvas geometry: design scale and the platform safe zone (px)."""

    def __init__(self, canvas):
        canvas = canvas or {}
        self.W = int(canvas.get("width") or 1080)
        self.H = int(canvas.get("height") or 1920)
        self.fps = float(canvas.get("fps") or 30)
        ratio = self.H / float(self.W)
        u = min(self.W / 1080.0, self.H / 1920.0)
        if ratio < 1.5:
            # square / 4:5 / landscape: the 9:16 grid would make text small, and
            # these frames have proportionally more vertical room per design px
            u = min(self.W / 1080.0, u * 1.4)
        self.u = u
        self.portrait = ratio >= 1.5
        self.wide = ratio < 0.95
        if self.portrait:
            top, bottom, side = 0.11, 0.20, 0.06
        elif self.wide:
            top, bottom, side = 0.08, 0.12, 0.05
        else:
            top, bottom, side = 0.07, 0.12, 0.06
        self.L, self.R = self.W * side, self.W * (1 - side)
        self.T, self.B = self.H * top, self.H * (1 - bottom)
        self.SW, self.SH = self.R - self.L, self.B - self.T
        self.CX, self.CY = (self.L + self.R) / 2, (self.T + self.B) / 2

    def d(self, v):
        """Design px (1080x1920 grid) -> canvas px."""
        return v * self.u

    def column(self, width=1100):
        """A copy whose safe zone is a centred column at most `width` design px wide.

        Cards and lists read as a column; on wide frames they would otherwise
        stretch edge to edge with the text lost in the middle.
        """
        col = copy.copy(self)
        w = min(self.SW, self.d(width))
        col.L, col.R, col.SW = self.CX - w / 2, self.CX + w / 2, w
        return col

    def place(self, position, total, margin=0.05):
        """Top y of a block of height `total` at 'top' / 'center' / 'bottom' of the safe zone."""
        if position == "top":
            y = self.T + self.SH * margin
        elif position == "bottom":
            y = self.B - self.SH * margin - total
        else:
            y = self.CY - total / 2
        return max(self.T, min(y, self.B - total)) if total <= self.SH else self.T

    def clamp_x(self, x, half):
        return max(self.L + half, min(self.R - half, x)) if 2 * half <= self.SW else self.CX

    def clamp_y(self, y, half):
        return max(self.T + half, min(self.B - half, y)) if 2 * half <= self.SH else self.CY


class _Clock:
    """Timing inside one template window; short windows animate proportionally faster."""

    def __init__(self, start, end, prof):
        self.t0, self.t1 = float(start), float(end)
        self.dur = self.t1 - self.t0
        k = min(1.0, max(0.3, self.dur / 2.4))
        self.din = prof["in"] * k
        self.dout = prof["out"] * k
        self.gap = prof["gap"] * k
        self.min_life = min(0.3, self.dur * 0.5)

    def stagger(self, n, share=0.5, per=None):
        """Seconds between n staggered entrances, so the last one starts within `share` of the window."""
        if n <= 1:
            return 0.0
        return min(per if per is not None else self.gap, self.dur * share / (n - 1))

    def span(self, at, until, min_life=None):
        """(start offset, end offset from the window end) clamped so the life is at least min_life."""
        ml = self.min_life if min_life is None else min(min_life, self.dur)
        at = max(0.0, min(float(at), self.dur - ml))
        until = max(0.0, min(float(until), self.dur - at - ml))
        return at, until


def _r(v, nd=1):
    v = round(float(v), nd)
    return int(v) if v == int(v) else v


def _keys(pairs):
    """[[t, v(, ease)], ...] with rounded numbers."""
    out = []
    for k in pairs:
        key = [round(float(k[0]), 3), round(float(k[1]), 4)]
        if len(k) > 2 and k[2]:
            key.append(k[2])
        out.append(key)
    return out


def _clip_keys(keys, life):
    """Drop keyframes after the layer ends; a key at `life` keeps the motion continuous up to the cut."""
    if not keys or keys[-1][0] <= life + 1e-9:
        return keys
    inside = [k for k in keys if k[0] <= life + 1e-9]
    if not inside:
        return [[0.0, keys[0][1]]]
    nxt = keys[len(inside)]
    try:
        from .anim import value_at
        v = value_at(keys, life, keys[0][1])
    except ImportError:   # linear fallback while the engine is missing
        a = inside[-1]
        v = a[1] + (nxt[1] - a[1]) * (life - a[0]) / max(1e-9, nxt[0] - a[0])
    if life - inside[-1][0] > 1e-3:
        inside.append([round(life, 3), round(v, 4)] + list(nxt[2:]))
    return inside


def _key_value(keys, t, default=1.0):
    """A keyframe curve's value at t (the engine's interpolation; linear while the engine is missing)."""
    try:
        from .anim import value_at
    except ImportError:
        value_at = None
    if value_at is not None:
        return value_at(keys, t, default)
    if not keys:
        return default
    if t <= keys[0][0]:
        return float(keys[0][1])
    for a, b in zip(keys, keys[1:]):
        if t < b[0]:
            if len(b) > 2 and b[2] == "step":
                return float(a[1])
            return a[1] + (b[1] - a[1]) * (t - a[0]) / max(1e-9, b[0] - a[0])
    return float(keys[-1][1])


def _mul_keys(a, b, dt=1.0 / 30):
    """Keyframes of the product of two curves (a member's own opacity x its group's opacity).

    Exact wherever at most one curve changes between two neighbouring key
    times (that segment keeps its easing, scaled); a stretch where both change,
    or where one steps in the middle of the other's eased segment, is sampled
    every `dt` seconds.
    """
    if not a or not b:
        return [list(k) for k in (a or b or [])]

    def seg(keys, t0, t1):
        """(start key, end key) of the segment of `keys` covering [t0, t1], or None outside the keys."""
        if t1 <= keys[0][0] + 1e-9 or t0 >= keys[-1][0] - 1e-9:
            return None
        for k0, k1 in zip(keys, keys[1:]):
            if k0[0] <= t0 + 1e-9 and t1 <= k1[0] + 1e-9:
                return k0, k1
        return None

    def ease_of(k):
        return k[2] if len(k) > 2 and k[2] else None

    def flat(s, t1):
        """The curve holds one value on [t0, t1) (it may still step to a new value exactly at t1)."""
        return s is None or abs(s[0][1] - s[1][1]) < 1e-9 or (ease_of(s[1]) == "step")

    def jumps(s, t1):
        return s is not None and ease_of(s[1]) == "step" and abs(s[1][0] - t1) < 1e-9 and \
            abs(s[0][1] - s[1][1]) >= 1e-9

    def prod(t):
        return _key_value(a, t) * _key_value(b, t)

    times = sorted({round(float(k[0]), 4) for k in list(a) + list(b)})
    out = [[times[0], prod(times[0])]]
    for t0, t1 in zip(times, times[1:]):
        sa, sb = seg(a, t0, t1), seg(b, t0, t1)
        fa, fb = flat(sa, t1), flat(sb, t1)
        v1 = prod(t1)
        held_zero = (fa and abs(_key_value(a, t0)) < 1e-9) or (fb and abs(_key_value(b, t0)) < 1e-9)
        if (fa and fb) or held_zero:     # nothing changes before t1 (a curve held at 0 zeroes the product)
            out.append([t1, v1, "step"] if abs(v1 - out[-1][1]) >= 1e-9 else [t1, v1])
            continue
        ramp, other = (sa, sb) if not fa else (sb, sa)
        if (fa or fb) and abs(ramp[0][0] - t0) < 1e-9 and abs(ramp[1][0] - t1) < 1e-9 and not jumps(other, t1):
            out.append([t1, v1] + ([ease_of(ramp[1])] if ease_of(ramp[1]) else []))
            continue
        k = math.floor(t0 / dt + 1e-6) + 1      # samples on the frame grid of the layer
        while k * dt < t1 - 1e-3:
            out.append([k * dt, prod(k * dt)])
            k += 1
        out.append([t1, v1, "step"] if (jumps(sa, t1) or jumps(sb, t1)) else [t1, v1])
    # drop keys inside a run of one value (the curve is constant across them whatever their easing)
    keep = [k for i, k in enumerate(out)
            if i in (0, len(out) - 1) or not (abs(out[i - 1][1] - k[1]) < 1e-9 and abs(out[i + 1][1] - k[1]) < 1e-9)]
    return _keys(keep)


def _enter(spec, default_d):
    if not spec:
        return None
    if isinstance(spec, str):
        return {"type": spec, "duration": round(default_d, 3), "delay": 0}
    kind, d = spec[0], spec[1]
    delay = spec[2] if len(spec) > 2 else 0
    return {"type": kind, "duration": round(max(0.05, d), 3), "delay": round(max(0.0, delay), 3)}


def _exit(spec, default_d):
    if not spec:
        return None
    if isinstance(spec, str):
        return {"type": spec, "duration": round(default_d, 3)}
    return {"type": spec[0], "duration": round(max(0.05, spec[1]), 3)}


class _Ctx:
    """Everything a template function needs: frame, clock, style, energy and a layer list."""

    def __init__(self, name, frame, clock, style, prof, energy):
        self.name, self.f, self.clk, self.s, self.prof, self.energy = name, frame, clock, style, prof, energy
        self.layers = []
        self.fixed = True
        self._ids = {}

    # ----- colours and text
    def color(self, value, key="accent"):
        """A colour param: '' -> the style colour `key`; a style key name -> that colour; else the hex."""
        if not value:
            return self.s[key]
        if value in ("accent", "text", "bg", "bg2"):
            return self.s[value]
        return value

    def fit(self, text, size, max_width, lines=2, min_size=None, font=None, **kw):
        return fit_text(text, font or self.s["font"], size, max_width, lines, min_size, **kw)

    def body(self, text, size, max_width, lines=2, min_size=None, **kw):
        kw.setdefault("ls", 1.15)
        return fit_text(text, self.s["font_secondary"], size, max_width, lines, min_size, **kw)

    def text_shadow(self, strength=1.0):
        f = self.f
        return {"color": _alpha("#000000", 0.62 * strength), "dx": 0, "dy": _r(f.d(5)), "blur": _r(f.d(16))}

    def card_shadow(self):
        f = self.f
        return {"color": _alpha("#000000", 0.45), "dx": 0, "dy": _r(f.d(14)), "blur": _r(f.d(34))}

    # ----- layers
    def add(self, kind, part, fields, at=0.0, until=0.0, enter=None, exit="auto", drift=False, group=None,
            min_life=None):
        """Append one layer living from `at` seconds after the window start to `until` before its end."""
        if group is not None:
            at, until, exit = group.at, group.until, None
        at, until = self.clk.span(at, until, min_life)
        start, end = round(self.clk.t0 + at, 3), round(self.clk.t1 - until, 3)
        life = end - start
        n = self._ids.get(part, 0)
        self._ids[part] = n + 1
        lay = {"type": kind, "id": "%s.%s%s" % (self.name, part, "" if n == 0 else n + 1),
               "start": start, "end": end}
        for k, v in fields.items():
            if v is None:
                continue
            if k == "animate":   # callers may share one dict between layers: never modify theirs
                v = {prop: [list(key) for key in keys] for prop, keys in v.items() if keys}
            if k in ("x", "y", "w", "h", "size", "max_width"):
                v = _r(v, 0)
                if k in ("w", "h", "size", "max_width"):
                    v = max(1, v)
            lay[k] = v
        lay.setdefault("anchor", "center")
        lay["fixed"] = self.fixed
        en = _enter(enter, self.clk.din)
        ex = _exit(self.prof["exit"] if exit == "auto" else exit, self.clk.dout)
        # entrance + exit must fit inside the life with a moment of rest
        need = (en["duration"] + en["delay"] if en else 0) + (ex["duration"] if ex else 0)
        if need > life * 0.92:
            k = life * 0.92 / need
            if en:
                en["duration"], en["delay"] = round(en["duration"] * k, 3), round(en["delay"] * k, 3)
            if ex:
                ex["duration"] = round(ex["duration"] * k, 3)
        if en:
            lay["enter"] = en
        if ex:
            lay["exit"] = ex
        if drift and self.prof["drift"]:
            lay.setdefault("animate", {})["scale"] = _keys([[0, 1.0], [life, 1 + self.prof["drift"]]])
        if group is not None:
            anim = group.animate_for(lay.get("x", self.f.W / 2), lay.get("y", self.f.H / 2))
            own = (lay.get("animate") or {}).get("opacity")
            if own and anim.get("opacity"):     # a member's own fades/blinks ride on the group's fades
                anim["opacity"] = _mul_keys(own, anim["opacity"], 1.0 / self.f.fps)
            if anim:
                lay.setdefault("animate", {}).update(anim)
        # templates write x/y keyframes as offsets from the layer's position; the spec wants positions
        for axis, base in (("x", lay.get("x", self.f.W / 2)), ("y", lay.get("y", self.f.H / 2))):
            keys = (lay.get("animate") or {}).get(axis)
            if keys:
                lay["animate"][axis] = [[k[0], round(base + k[1], 2)] + list(k[2:]) for k in keys]
        for prop, keys in list((lay.get("animate") or {}).items()):
            lay["animate"][prop] = _clip_keys(keys, life)
        self.layers.append(lay)
        return lay

    def text(self, part, blk, x, y, anchor="center", color=None, font=None, align="center", **kw):
        """A text layer from a fitted block (nothing at all when the text is blank)."""
        if not blk.text.strip():
            return None
        fields = {"text": blk.text, "font": font or blk.font, "size": blk.size,
                  "color": color or self.s["text"], "case": blk.case, "max_width": blk.mw,
                  "align": align, "line_spacing": blk.ls, "x": x, "y": y, "anchor": anchor}
        if blk.lsp:
            fields["letter_spacing"] = _r(blk.lsp)
        for k in ("stroke", "box", "highlight", "word_times", "gradient", "shadow", "opacity", "rotation",
                  "scale", "animate", "blend"):
            if k in kw:
                fields[k] = kw.pop(k)
        sh = fields.get("shadow")
        if sh and not fields.get("box") and _lum(fields["color"]) < 0.2 and _lum(sh["color"]) < 0.2:
            # dark ink (light themes): a soft light halo separates it from footage, a black shadow smudges it
            fields["shadow"] = dict(sh, color=_alpha("#FFFFFF", 0.55), dy=0)
        return self.add("text", part, fields, **kw)

    def shape(self, part, shape, x, y, w, h, color, radius=None, anchor="center", **kw):
        fields = {"shape": shape, "x": x, "y": y, "w": w, "h": h, "color": color, "anchor": anchor}
        if radius:
            fields["radius"] = _r(radius)
        for k in ("stroke", "points", "gradient", "draw", "shadow", "opacity", "rotation", "scale",
                  "animate", "blend"):
            if k in kw:
                fields[k] = kw.pop(k)
        return self.add("shape", part, fields, **kw)

    def rect(self, part, x, y, w, h, color, radius=None, **kw):
        return self.shape(part, "rect", x, y, w, h, color, radius, **kw)

    def circle(self, part, x, y, dia, color, **kw):
        return self.shape(part, "circle", x, y, dia, dia, color, **kw)

    def ring(self, part, x, y, dia, color, width, **kw):
        kw["stroke"] = {"color": color, "width": _r(width)}
        return self.shape(part, "ring", x, y, dia, dia, color, **kw)

    def poly(self, part, x, y, w, h, points, color, **kw):
        kw["points"] = points
        return self.shape(part, "polygon", x, y, w, h, color, **kw)

    def icon(self, part, name, x, y, size, color, cut, **kw):
        """An icon from ICONS: every part shares one box, so they animate as one piece."""
        out = []
        for i, piece in enumerate(ICONS.get(name) or ICONS["star"]):
            col = color if piece["role"] == "fill" else cut
            out.append(self.poly("%s%d" % (part, i), x, y, size, size, piece["points"], col, **dict(kw)))
        return out

    def counter(self, part, blk, x, y, frm, to, decimals=0, prefix="", suffix="", separator=",",
                count_duration=1.2, ease="out-expo", anchor="center", color=None, **kw):
        fields = {"font": blk.font, "size": blk.size, "color": color or self.s["text"], "case": "none",
                  "max_width": max(blk.mw, blk.w * 1.25), "align": "center", "x": x, "y": y, "anchor": anchor,
                  "from": frm, "to": to, "decimals": int(decimals), "prefix": prefix, "suffix": suffix,
                  "separator": separator, "count_duration": round(count_duration, 3), "ease": ease}
        for k in ("stroke", "box", "shadow", "animate", "gradient", "opacity"):
            if k in kw:
                fields[k] = kw.pop(k)
        return self.add("counter", part, fields, **kw)

    def group(self, pivot, intro=None, outro=None, at=0.0, until=0.0, drift=False, dist=None, dim=None):
        """A set of layers that enter and leave as one rigid piece around `pivot`.

        dim=(t, level) fades the whole group to `level` opacity at window time t.
        """
        return _Group(self, pivot, intro or self.prof["group_in"], outro or self.prof["group_out"],
                      at, until, drift, dist, dim)

    # ----- motion helpers
    def pop_keys(self, life, start_scale=0.45):
        """Scale keyframes for a single layer: punch in with overshoot, then drift until the exit."""
        o = self.prof["over"]
        d = self.clk.din
        if o <= 0:
            keys = [[0, 0.94], [d, 1.0, "out-quart"]]
        else:
            keys = [[0, start_scale], [d * 0.62, 1 + 0.1 * o, "out-cubic"], [d, 1.0, "in-out-quad"]]
        if self.prof["drift"] and life - self.clk.dout > d + 0.05:
            keys.append([life - self.clk.dout, 1 + self.prof["drift"], "linear"])
        return _keys(keys)

    def pulse_keys(self, t_from, t_to, period=0.9, amp=None):
        """Scale keys: a gentle heartbeat between t_from and t_to (None when the energy has no idle motion)."""
        amp = self.prof["pulse"] if amp is None else amp
        if amp <= 0 or t_to - t_from < period:
            return None
        keys, t = [[t_from, 1.0]], t_from
        while t + period <= t_to:
            keys.append([t + period * 0.35, 1 + amp, "out-quad"])
            keys.append([t + period, 1.0, "in-out-quad"])
            t += period
        return _keys(keys)

    def wiggle_keys(self, t, amp=12.0):
        """Rotation keys: a quick decaying wiggle starting at t (bells, badges); None on calm energy."""
        if self.prof["over"] <= 0:
            return None
        a = amp * self.prof["over"]
        return _keys([[t, 0], [t + 0.07, a, "out-quad"], [t + 0.16, -a * 0.7, "in-out-quad"],
                      [t + 0.25, a * 0.4, "in-out-quad"], [t + 0.34, 0, "in-out-quad"]])


class _Group:
    """Shared timing and motion for layers that must move as one piece.

    The group's scale is written into each member as a scale key plus x/y
    keys that move the member's anchor along with the scale, so the whole
    group scales about one pivot (members scale about their own anchor).
    """

    def __init__(self, ctx, pivot, intro, outro, at, until, drift, dist, dim):
        self.c = ctx
        self.at, self.until = ctx.clk.span(at, until)
        self.px, self.py = pivot
        self.life = ctx.clk.dur - self.at - self.until
        self.keys = _group_keys(intro, outro, self.life, ctx.clk.din * 1.1, ctx.clk.dout, ctx.prof, ctx.f,
                                drift, dist)
        if dim:
            self.keys = _dim_keys(self.keys, dim[0] - self.at, dim[1])

    def animate_for(self, ax, ay):
        """Group motion for a member anchored at (ax, ay): scale keys plus x/y offsets about the pivot."""
        props = {"scale": [], "x": [], "y": [], "opacity": [], "blur": []}
        for t, s, dx, dy, o, b, e in self.keys:
            vals = {"scale": s, "x": dx + (s - 1) * (ax - self.px), "y": dy + (s - 1) * (ay - self.py),
                    "opacity": o, "blur": b}
            for name, v in vals.items():
                key = [round(t, 3), round(v, 4 if name in ("scale", "opacity") else 2)]
                if e:
                    key.append(e)
                props[name].append(key)
        ident = {"scale": 1.0, "x": 0.0, "y": 0.0, "opacity": 1.0, "blur": 0.0}
        return {n: ks for n, ks in props.items() if any(abs(k[1] - ident[n]) > 1e-6 for k in ks)}

    # same creators as the context, bound to this group
    def __getattr__(self, name):
        method = getattr(self.c, name)

        def bound(*a, **kw):
            kw["group"] = self
            return method(*a, **kw)
        return bound


def _dim_keys(keys, t, level, fade=0.3):
    """Insert a fade to `level` opacity at group time t into group keys (only while the group rests)."""
    rest = [k for k in keys if k[4] == 1]
    if not rest or not (rest[0][0] < t and t + fade < keys[-1][0] - 0.02):
        return keys
    out, done = [], False
    for k in keys:
        if not done and k[0] > t:
            prev = out[-1]
            span = k[0] - prev[0]
            s_at = prev[1] + (k[1] - prev[1]) * ((t - prev[0]) / span if span > 0 else 1.0)
            out.append((t, s_at, 0, 0, 1, 0, "linear"))
            out.append((t + fade, s_at, 0, 0, level, 0, "out-quad"))
            done = True
        if done and k[0] > t + fade:
            k = k[:4] + (k[4] * level,) + k[5:]
        if not done or k[0] > t + fade:
            out.append(k)
    return out


def _group_keys(intro, outro, life, din, dout, prof, f, drift, dist):
    """Group motion as (t, scale, dx, dy, opacity, blur, ease) keys over the group's life."""
    o = prof["over"]
    dist = dist or f.d(120)
    din = min(din, life * 0.45)
    dout = min(dout, life * 0.3)
    dirs = {"rise": (0, 1), "drop": (0, -1), "left": (-1, 0), "right": (1, 0),
            "sink": (0, 1), "lift": (0, -1)}
    k = []
    if intro == "pop":
        if o > 0:
            k += [(0, 0.55, 0, 0, 0, 0, None), (din * 0.62, 1 + 0.07 * o, 0, 0, 1, 0, "out-cubic"),
                  (din, 1, 0, 0, 1, 0, "in-out-quad")]
        else:
            k += [(0, 0.92, 0, 0, 0, f.d(8), None), (din, 1, 0, 0, 1, 0, "out-quart")]
    elif intro in dirs:
        vx, vy = dirs[intro]
        if o > 0:
            k += [(0, 1, vx * dist, vy * dist, 0, 0, None),
                  (din * 0.7, 1, -vx * dist * 0.07 * o, -vy * dist * 0.07 * o, 1, 0, "out-cubic"),
                  (din, 1, 0, 0, 1, 0, "in-out-quad")]
        else:
            k += [(0, 1, vx * dist * 0.4, vy * dist * 0.4, 0, f.d(6), None), (din, 1, 0, 0, 1, 0, "out-quart")]
    elif intro == "fade":
        k += [(0, 1, 0, 0, 0, 0, None), (din, 1, 0, 0, 1, 0, "out-quad")]
    else:
        k += [(0, 1, 0, 0, 1, 0, None)]
        din = 0.0
    rest = 1 + (prof["drift"] if drift else 0.0)
    if outro == "none":
        dout = 0.0
    if life - dout > din + 0.02:
        k.append((life - dout, rest, 0, 0, 1, 0, "linear"))
    if outro == "fade":
        k.append((life, rest, 0, 0, 0, 0, "in-quad"))
    elif outro == "shrink":
        k.append((life, rest * 0.8, 0, 0, 0, 0, "in-cubic"))
    elif outro in dirs:
        vx, vy = dirs[outro]
        k.append((life, rest, vx * dist * 0.6, vy * dist * 0.6, 0, 0, "in-cubic"))
    return k


# =========================================================================== registry and params

def _p(type_, default, doc, choices=None):
    d = {"type": type_, "default": default, "doc": doc}
    if choices:
        d["choices"] = list(choices)
    return d


def _template(tname, category, description, **params):
    """Register a template builder fn(ctx, params) under `tname` (params may include 'name')."""
    def deco(fn):
        CATALOG[tname] = {"description": description, "category": category, "params": params, "fn": fn}
        return fn
    return deco


_ICON_NAMES = list(ICONS)


def _coerce(spec, value):
    """Convert a param value to its declared type; None or garbage -> the default."""
    t, default = spec["type"], spec["default"]
    if value is None:
        return default
    try:
        if t == "str":
            return str(value)
        if t == "int":
            v = float(value)
            return int(round(v)) if math.isfinite(v) else default
        if t == "float":
            v = float(value)
            return default if math.isnan(v) or math.isinf(v) else v
        if t == "bool":
            if isinstance(value, str):
                return value.strip().lower() in ("1", "true", "yes", "on", "y")
            return bool(value)
        if t == "list[str]":
            if isinstance(value, str):
                parts = re.split(r"\s*(?:\||\n)\s*", value.strip())
                return [x for x in parts if x]
            return [str(x) for x in value if str(x).strip()]
        if t == "list[float]":
            if isinstance(value, str):
                value = [x for x in re.split(r"[,|\s]+", value.strip()) if x]
            out = [float(x) for x in value]
            return out if all(math.isfinite(x) for x in out) else default
        if t == "highlight":
            if isinstance(value, (str, int)):
                value = [value] if isinstance(value, int) else [x for x in re.split(r"\s*[,|]\s*", value) if x]
            return [x if isinstance(x, int) else str(x) for x in value]
        if t == "color":
            v = str(value).strip()
            return v if (_HEX.match(v) or v in ("", "accent", "text", "bg", "bg2")) else default
        if t == "choice":
            v = str(value).strip().lower()
            return v if v in spec["choices"] else default
    except (TypeError, ValueError, OverflowError):
        return default
    return value


def _resolve_params(entry, params):
    out = {}
    for k, spec in entry["params"].items():
        out[k] = _coerce(spec, params.get(k))
    return out


def _resolve_style(style):
    st = dict(DEFAULT_STYLE)
    for k, v in (style or {}).items():
        if k in ("text", "accent", "bg", "bg2"):
            if isinstance(v, str) and _HEX.match(v.strip()):
                st[k] = v.strip()[:7].upper()
        elif k in ("font", "font_secondary"):
            if v:
                st[k] = str(v)
        elif k == "energy" and v:
            e = str(v).strip().lower()
            st[k] = e if e in _ENERGY else _ENERGY_ALIASES.get(e, "medium")
    return st


def _highlight_idx(text, targets, case):
    """Word indices (after the case transform) for highlight targets: words, phrases or indices."""
    words = _apply_case(str(text), case).split()
    norm = [re.sub(r"[^\w$%#@&+]", "", w.lower()) for w in words]
    out = set()
    for t in targets or []:
        if isinstance(t, int):
            if 0 <= t < len(words):
                out.add(t)
            continue
        parts = [p for p in (re.sub(r"[^\w$%#@&+]", "", x.lower()) for x in str(t).split()) if p]
        for i in range(len(norm) - len(parts) + 1):
            if parts and norm[i:i + len(parts)] == parts:
                out.update(range(i, i + len(parts)))
    return sorted(out)


# The params a template cannot do without: when all of them are blank (or empty
# lists) the template draws nothing rather than an empty card or a placeholder.
_REQUIRES = {
    "hook_title": ["text"], "title_card": ["title"], "chapter_marker": ["number", "title"], "question": ["text"],
    "alert_banner": ["text"], "kinetic_text": ["text"], "emphasis_word": ["word"], "word_stack": ["words"],
    "underline_reveal": ["text"], "quote": ["text"], "stat": ["value"], "bar_chart": ["values"],
    "price_tag": ["price"], "big_number_bg": ["number"], "list_item": ["text"], "checklist": ["items"],
    "ranking": ["items"], "timeline_steps": ["steps"], "step_number": ["text", "label"], "pros_cons": ["pros", "cons"],
    "versus": ["left", "right"], "myth_vs_fact": ["myth", "fact"], "comment_bubble": ["text"],
    "post_card": ["text"], "notification_pop": ["title", "text"], "text_message": ["messages"],
    "search_bar": ["query"], "poll": ["question", "options"], "callout": ["text"], "cta": ["text"],
    "subscribe": ["text"], "lower_third": ["name"], "source_card": ["title"], "location_tag": ["place"],
}


def _blank(v):
    return (isinstance(v, str) and not v.strip()) or (isinstance(v, list) and not any(str(x).strip() for x in v))


def build(name, params=None, start=0.0, end=3.0, canvas=None, style=None):
    """Layer dicts (timeline spec v1) for one template call inside [start, end] on `canvas`.

    `params` are validated against CATALOG[name]["params"] (unknown keys
    ignored, bad values fall back to defaults), `style` is merged over
    DEFAULT_STYLE. Raises KeyError for an unknown template and ValueError
    when end <= start.
    """
    if name not in CATALOG:
        raise KeyError("unknown template %r (known: %s)" % (name, ", ".join(sorted(CATALOG))))
    start, end = float(start), float(end)
    if not end > start:
        raise ValueError("template %s: end (%s) must be after start (%s)" % (name, end, start))
    entry = CATALOG[name]
    st = _resolve_style(style)
    prof = _ENERGY[st["energy"]]
    ctx = _Ctx(name, _Frame(canvas), _Clock(start, end, prof), st, prof, st["energy"])
    p = _resolve_params(entry, params or {})
    if name in _REQUIRES and all(_blank(p[k]) for k in _REQUIRES[name]):
        return []
    entry["fn"](ctx, p)
    return ctx.layers


# =========================================================================== shared looks

_LOOKS = ["shadow", "stroke", "box", "plain"]
_CASES = ["upper", "none", "title", "lower"]


def _look(c, look, hl, hl_style="color"):
    """Text fields for a look: shadow, stroke, box (dark text on a light card) or plain."""
    f, s, acc = c.f, c.s, c.s["accent"]
    out = {"color": s["text"]}
    if look == "box":
        out["color"] = _on(s["text"])
        out["box"] = {"color": s["text"], "pad": [_r(f.d(30)), _r(f.d(16))], "radius": _r(f.d(22))}
        out["shadow"] = c.card_shadow()
        if hl:
            out["highlight"] = {"words": hl, "color": _on(acc),
                                "box": {"color": acc, "pad": [_r(f.d(10)), _r(f.d(4))], "radius": _r(f.d(10))}}
        return out
    if look == "stroke" and not (hl and hl_style == "pill"):    # an outline around a pill word looks broken
        out["stroke"] = {"color": "#000000", "width": _r(f.d(8))}
        out["shadow"] = c.text_shadow(0.6)
    elif look == "stroke":
        out["shadow"] = c.text_shadow()
    elif look == "shadow":
        out["shadow"] = c.text_shadow()
    if hl:
        if hl_style == "pill":
            out["highlight"] = {"words": hl, "color": _on(acc),
                                "box": {"color": acc, "pad": [_r(f.d(14)), _r(f.d(6))], "radius": _r(f.d(14))}}
        else:
            out["highlight"] = {"words": hl, "color": acc}
    return out


def _reveal(c, mode, blk):
    """Enter spec for a headline: auto (by energy), whole, words or typewriter."""
    if mode == "words":
        return ("words", min(c.clk.dur * 0.45, c.prof["word_pace"] * len(blk.text.split()) + 0.1))
    if mode == "typewriter":
        return ("typewriter", min(c.clk.dur * 0.5, 0.04 * len(blk.text) + 0.15))
    if mode == "whole":
        return c.prof["pop"]
    return c.prof["title"]


def _pill(c, blk, pad=(22, 12)):
    """Box padding (design px -> px) for a text pill and the pill's height."""
    f = c.f
    return [_r(f.d(pad[0])), _r(f.d(pad[1]))], blk.h + 2 * f.d(pad[1])


# =========================================================================== hooks and titles

@_template(
    "hook_title", "hooks",
    "The scroll-stopping line on screen in the first 1-3 s (bold claim, question, promise). "
    "Use once, on the opening beat, synced with the first spoken sentence.",
    text=_p("str", "NOBODY TELLS YOU THIS", "the hook; 2-8 words reads best"),
    highlight=_p("highlight", [], "words, phrases or word indices painted in the accent colour"),
    kicker=_p("str", "", "small label above the title, e.g. 'PART 2' or '3 MISTAKES'"),
    position=_p("choice", "top", "where the title sits in the safe zone", ["top", "center", "bottom"]),
    look=_p("choice", "shadow", "shadow, stroke (black outline; not combined with pill highlights), box (dark text "
            "on a light card) or plain", _LOOKS),
    highlight_style=_p("choice", "color", "highlighted words as coloured text or in an accent pill",
                       ["color", "pill"]),
    reveal=_p("choice", "auto", "auto (by energy), whole, words (word by word) or typewriter",
              ["auto", "whole", "words", "typewriter"]),
    case=_p("choice", "upper", "letter case", _CASES),
    size=_p("float", 0, "font size in design px (1080x1920 grid); 0 = fit automatically"),
)
def _hook_title(c, p):
    f, acc = c.f, c.s["accent"]
    stroke = f.d(8) if p["look"] == "stroke" else 0
    blk = c.fit(p["text"], f.d(p["size"] or 128), f.SW, lines=3, min_size=f.d(58), case=p["case"], ls=1.0,
                stroke=stroke, prefer=2)
    look = _look(c, p["look"], _highlight_idx(p["text"], p["highlight"], p["case"]), p["highlight_style"])
    kick = None
    if p["kicker"]:
        kick = c.body(p["kicker"], f.d(38), f.SW * 0.8, lines=1, case="upper", lsp=f.d(3), ls=1.0)
        pad, head_h = _pill(c, kick, (24, 16))
    else:
        head_h = f.d(12)
    gap = f.d(30)
    top = f.place(p["position"], head_h + gap + blk.h)
    if kick:
        c.text("kicker", kick, f.CX, top, anchor="top", color=_on(acc),
               box={"color": acc, "pad": pad, "radius": _r(f.d(12))}, enter=c.prof["pop"])
    else:
        c.rect("bar", f.CX, top, f.d(150), f.d(12), acc, radius=f.d(6), anchor="top",
               enter=("wipe-right", c.clk.din * 1.3))
    c.text("title", blk, f.CX, top + head_h + gap, anchor="top", enter=_reveal(c, p["reveal"], blk),
           at=c.clk.gap, drift=True, **look)


@_template(
    "title_card", "hooks",
    "Full-screen section title: video intros, chapter openers, the reveal before a list. "
    "Covers the footage (full=true) or sits on it (full=false).",
    title=_p("str", "THE SETUP", "main title"),
    kicker=_p("str", "", "small spaced-out line above the title, e.g. 'CHAPTER 1'"),
    sub=_p("str", "", "one supporting line under the title"),
    number=_p("str", "", "big number above the title, e.g. '01'"),
    full=_p("bool", True, "cover the frame with the style background gradient"),
    align=_p("choice", "center", "text alignment", ["center", "left"]),
)
def _title_card(c, p):
    f, s, acc = c.f, c.s, c.s["accent"]
    left = p["align"] == "left"
    x = f.L if left else f.CX
    top_anchor = "top-left" if left else "top"
    align = "left" if left else "center"
    title = c.fit(p["title"], f.d(124), f.SW, lines=3, min_size=f.d(60), case="upper", ls=1.0)
    num = c.fit(p["number"], f.d(210), f.SW, lines=1, min_size=f.d(100)) if p["number"] else None
    kick = c.body(p["kicker"], f.d(36), f.SW, lines=1, case="upper", lsp=f.d(9), ls=1.0) if p["kicker"] else None
    sub = c.body(p["sub"], f.d(46), f.SW * 0.9, lines=2) if p["sub"] else None
    gap, line_h = f.d(30), f.d(10)
    total = title.h + gap * 1.3 + line_h
    num_gap = gap * 0.9  # breathing room under a big numeral before the small kicker
    total += (num.h + num_gap) if num else 0
    total += (kick.h + gap) if kick else 0
    total += (gap + sub.h) if sub else 0
    y = f.place("center", total)
    g = c.clk.stagger(5, 0.35)
    if p["full"]:
        c.rect("panel", f.W / 2, f.H / 2, f.W, f.H, s["bg"], gradient=[s["bg"], s["bg2"]],
               enter=("fade", c.clk.din * 0.6), exit=("fade", c.clk.dout))
    content_until = c.clk.dout * 0.6 if p["full"] else 0.0
    if num:
        c.text("number", num, x, y, anchor=top_anchor, align=align, color=acc, enter=c.prof["rise"], at=g,
               until=content_until)
        y += num.h + num_gap
    if kick:
        c.text("kicker", kick, x, y, anchor=top_anchor, align=align, color=acc, enter=("wipe-right", c.clk.din * 1.4),
               at=g, until=content_until)
        y += kick.h + gap
    c.text("title", title, x, y, anchor=top_anchor, align=align, enter=c.prof["title"], at=2 * g, drift=True,
           until=content_until, shadow=None if p["full"] else c.text_shadow())
    y += title.h + gap * 1.3
    c.rect("line", x, y, f.d(170), line_h, acc, radius=line_h / 2, anchor=top_anchor,
           enter=("wipe-right", c.clk.din * 1.5), at=3 * g, until=content_until)
    y += line_h + gap
    if sub:
        c.text("sub", sub, x, y, anchor=top_anchor, align=align, color=_alpha(s["text"], 0.8), enter=c.prof["rise"],
               at=4 * g, until=content_until)


@_template(
    "chapter_marker", "hooks",
    "Small chip in a top corner naming the current section ('02 The edit'). Use on the first beat of each "
    "section of a longer video or a multi-part reel; stays out of the way of the footage.",
    number=_p("str", "01", "section number"),
    title=_p("str", "The setup", "section name"),
    corner=_p("choice", "top-left", "where the chip sits", ["top-left", "top-right", "top"]),
)
def _chapter_marker(c, p):
    f, s, acc = c.f, c.s, c.s["accent"]
    num = c.fit(p["number"], f.d(46), f.SW * 0.3, lines=1)
    title = c.body(p["title"], f.d(42), f.SW * 0.7, lines=1, ls=1.0)
    npad, nh = _pill(c, num, (18, 14))
    tpad, th = _pill(c, title, (22, 14))
    tpad[1] = _r(max(0, (nh - title.h) / 2))
    nw = num.w + 2 * npad[0] if num.text else 0.0
    tw = title.w + 2 * tpad[0] if title.text else 0.0
    gap = f.d(10) if nw and tw else 0.0
    total = nw + gap + tw
    if p["corner"] == "top-right":
        x0 = f.R - total
    elif p["corner"] == "top":
        x0 = f.CX - total / 2
    else:
        x0 = f.L
    y = f.T + f.d(14) + nh / 2
    c.text("number", num, x0, y, anchor="left", color=_on(acc), box={"color": acc, "pad": npad, "radius": _r(f.d(10))},
           enter=c.prof["pop"], exit="fade")
    c.text("title", title, x0 + nw + gap, y, anchor="left", font=s["font_secondary"],
           box={"color": _alpha(s["bg"], 0.82), "pad": tpad, "radius": _r(f.d(10))},
           enter=("wipe-right", c.clk.din * 1.4), at=c.clk.gap * 1.5, exit=("wipe-left", c.clk.dout))


@_template(
    "question", "hooks",
    "A big question to the viewer with an optional set of answers: 'Would you rather', quizzes, "
    "comment-bait openers and the setup before a reveal.",
    text=_p("str", "WOULD YOU POST THIS?", "the question"),
    options=_p("list[str]", [], "optional answers shown as pills (2-4)"),
    sub=_p("str", "", "small line under the question, e.g. 'Answer in the comments'"),
    position=_p("choice", "center", "vertical placement", ["top", "center", "bottom"]),
)
def _question(c, p):
    c.f = c.f.column(1250)
    f, s, acc = c.f, c.s, c.s["accent"]
    D = f.d(150)
    q = c.fit(p["text"], f.d(88), f.SW, lines=3, min_size=f.d(52), case="upper", ls=1.0)
    opts = [c.body(o, f.d(46), f.SW * 0.8, lines=1) for o in p["options"][:4]]
    sub = c.body(p["sub"], f.d(38), f.SW * 0.85, lines=2) if p["sub"] else None
    gap = f.d(34)
    opad, oh = _pill(c, opts[0], (40, 22)) if opts else ([0, 0], 0)
    total = D + gap + q.h + sum(gap * 0.5 + oh for _ in opts) + ((gap * 0.8 + sub.h) if sub else 0)
    if opts:
        total += gap * 0.5
    y = f.place(p["position"], total)
    cy = y + D / 2
    land = c.clk.din
    wig = c.wiggle_keys(land, 14)
    extra = {"animate": {"rotation": wig}} if wig else {}
    c.circle("badge", f.CX, cy, D, acc, enter="bounce" if c.prof["over"] > 0.6 else c.prof["pop"],
             shadow=c.card_shadow(), **extra)
    mark = fit_text("?", c.s["font"], f.d(112), D, 1)
    c.text("mark", mark, f.CX, cy, color=_on(acc), enter="bounce" if c.prof["over"] > 0.6 else c.prof["pop"],
           **dict(extra))
    y += D + gap
    g = c.clk.stagger(len(opts) + 3, 0.45)
    c.text("question", q, f.CX, y, anchor="top", shadow=c.text_shadow(), enter=c.prof["rise"], at=g, drift=True)
    y += q.h + gap
    for i, o in enumerate(opts):
        y += gap * 0.5 if i == 0 else 0
        c.text("option", o, f.CX, y, anchor="top", font=s["font_secondary"],
               box={"color": _alpha(s["bg2"], 0.92), "pad": opad, "radius": _r(oh / 2)},
               enter=c.prof["pop"], at=g * (2 + i))
        y += oh + gap * 0.5
    if sub:
        c.text("sub", sub, f.CX, y + gap * 0.3, anchor="top", font=s["font_secondary"], color=_alpha(s["text"], 0.8),
               shadow=c.text_shadow(0.7), enter="fade", at=g * (2 + len(opts)))


@_template(
    "alert_banner", "hooks",
    "A tilted full-width warning band ('STOP DOING THIS', 'BIG MISTAKE'). Use for pattern interrupts, "
    "warnings and the 'don't do this' beat.",
    text=_p("str", "STOP DOING THIS", "banner text, short"),
    color=_p("color", "#FF2D46", "band colour"),
    tilt=_p("float", -4.0, "rotation in degrees (negative = rising to the right)"),
    y=_p("float", 0.32, "vertical centre as a fraction of the safe zone (0 = top, 1 = bottom)"),
)
def _alert_banner(c, p):
    f = c.f
    col = c.color(p["color"])
    tilt = max(-12.0, min(12.0, p["tilt"]))
    blk = c.fit(p["text"], f.d(96), f.SW * 0.9, lines=2, min_size=f.d(52), case="upper", ls=1.0)
    h = max(f.d(160), blk.h + f.d(56))
    # a tilted band is taller than h: keep its visible extent in the safe zone
    reach = h / 2 + abs(math.sin(math.radians(tilt))) * f.W * 0.5
    y = f.clamp_y(f.T + f.SH * min(1.0, max(0.0, p["y"])), min(reach, f.SH / 2))
    over = c.prof["over"] > 0
    c.rect("shade", f.W / 2, y, f.W * 1.3, h, _alpha(_mix(col, "#000000", 0.55), 0.85), rotation=tilt + 2.5,
           enter="whip-left" if over else ("wipe-left", c.clk.din), at=c.clk.gap, exit="fade")
    c.rect("band", f.W / 2, y, f.W * 1.3, h, col, rotation=tilt, shadow=c.card_shadow(),
           enter="whip-right" if over else ("wipe-right", c.clk.din), exit="fade")
    c.text("text", blk, f.W / 2, y, color=_on(col), rotation=tilt,
           enter="glitch" if over else c.prof["rise"], at=c.clk.gap * 2.5, exit="fade", drift=True)


# =========================================================================== text emphasis

@_template(
    "kinetic_text", "text",
    "Spoken words appearing on beat: build = the sentence assembles word by word, punch = one or two huge "
    "words at a time replacing each other. Pass `times` (voiceover word starts) for exact sync.",
    text=_p("str", "THIS ONE SETTING DOUBLED MY VIEWS", "the line, as spoken"),
    highlight=_p("highlight", [], "words, phrases or word indices painted in the accent colour"),
    times=_p("list[float]", [], "timeline seconds at which each word appears; empty = paced evenly"),
    mode=_p("choice", "build", "build (sentence assembles) or punch (1-2 big words at a time)", ["build", "punch"]),
    position=_p("choice", "center", "vertical placement", ["top", "center", "bottom"]),
    look=_p("choice", "shadow", "text look", _LOOKS),
    case=_p("choice", "upper", "letter case", _CASES),
    size=_p("float", 0, "font size in design px; 0 = fit automatically"),
)
def _kinetic_text(c, p):
    c.f = c.f.column(1250)
    f, acc = c.f, c.s["accent"]
    words = p["text"].split()
    if not words:
        return
    n = len(words)
    last_ok = c.clk.dur - 0.25
    if len(p["times"]) == n:
        rel = [min(last_ok, max(0.0, t - c.clk.t0)) for t in p["times"]]
        for i in range(1, n):
            rel[i] = max(rel[i], rel[i - 1])
    else:
        span = min(c.clk.dur * 0.6, c.prof["word_pace"] * n)
        rel = [i * span / n for i in range(n)]
    hl = _highlight_idx(p["text"], p["highlight"], p["case"])
    stroke = f.d(8) if p["look"] == "stroke" else 0
    if p["mode"] == "build":
        blk = c.fit(p["text"], f.d(p["size"] or 116), f.SW, lines=4, min_size=f.d(56), case=p["case"], ls=1.0,
                    stroke=stroke, prefer=3 if f.portrait else 2)
        top = f.place(p["position"], blk.h, margin=0.08)
        c.text("text", blk, f.CX, top, anchor="top", word_times=[round(t, 3) for t in rel],
               enter=("words", max(0.1, rel[-1] + 0.12)), drift=True, **_look(c, p["look"], hl))
        return
    # punch: chunks of one or two short words, each replacing the last (hard cuts, like an editor would)
    chunks, i = [], 0
    while i < n:
        j = i + 1
        if j < n and len(words[i]) + len(words[j]) <= 9 and (i in hl) == (j in hl):
            j += 1
        chunks.append((i, j))
        i = j
    yc = {"top": f.T + f.SH * 0.22, "bottom": f.B - f.SH * 0.22}.get(p["position"], f.CY)
    for k, (i, j) in enumerate(chunks):
        t_in = rel[i]
        t_out = rel[chunks[k + 1][0]] if k + 1 < len(chunks) else c.clk.dur
        blk = c.fit(" ".join(words[i:j]), f.d(p["size"] or 176), f.SW, lines=2, min_size=f.d(70), case=p["case"],
                    ls=1.0, stroke=stroke)
        look = _look(c, p["look"], [])
        if any(x in hl for x in range(i, j)):
            if p["look"] == "box":
                look["box"]["color"] = acc
                look["color"] = _on(acc)
            else:
                look["color"] = acc
        last = k == len(chunks) - 1
        pop = min(0.16, max(0.06, (t_out - t_in) * 0.5))
        c.text("chunk", blk, f.CX, yc, enter=(c.prof["pop"], pop), at=t_in, min_life=0.06,
               until=0.0 if last else c.clk.dur - t_out, exit="auto" if last else None, **look)


@_template(
    "emphasis_word", "text",
    "One huge word slammed onto the screen ('INSANE', 'FREE', '10X') on the stressed word of a sentence. "
    "Pair with a camera punch or flash for the strongest beat of the video.",
    word=_p("str", "INSANE", "the word (or two)"),
    color=_p("color", "", "word colour ('' = accent)"),
    look=_p("choice", "fill", "fill = coloured word, box = dark word on a colour block, stroke = outlined",
            ["fill", "box", "stroke"]),
    y=_p("float", 0.45, "vertical centre as a fraction of the safe zone"),
    tilt=_p("float", -3.0, "resting rotation in degrees"),
    echo=_p("bool", True, "a ghost copy that expands and fades on impact"),
)
def _emphasis_word(c, p):
    f, s = c.f, c.s
    col = c.color(p["color"])
    tilt = max(-15.0, min(15.0, p["tilt"]))
    blk = c.fit(p["word"], f.d(250), f.SW * 0.94, lines=2, min_size=f.d(90), case="upper", ls=0.95,
                stroke=f.d(10) if p["look"] == "stroke" else 0)
    y = f.clamp_y(f.T + f.SH * min(1.0, max(0.0, p["y"])), blk.h / 2 + f.d(20))
    fields = {"color": col, "shadow": c.text_shadow()}
    if p["look"] == "box":
        fields = {"color": _on(col), "box": {"color": col, "pad": [_r(f.d(34)), _r(f.d(14))], "radius": _r(f.d(18))},
                  "shadow": c.card_shadow()}
    elif p["look"] == "stroke":
        fields = {"color": s["text"], "stroke": {"color": "#000000", "width": _r(f.d(10))},
                  "shadow": c.text_shadow(0.6)}
    life = c.clk.dur
    o = c.prof["over"]
    if o > 0:
        hit = min(0.18, c.clk.din * 0.6)
        anim = {"scale": _keys([[0, 2.3], [hit, 0.93, "in-quad"], [hit + 0.14, 1.0, "out-back"],
                                [life - c.clk.dout, 1 + c.prof["drift"], "linear"]]),
                "blur": _keys([[0, f.d(16)], [hit, 0, "in-quad"]]),
                "rotation": _keys([[0, tilt - 9 * o], [hit + 0.18, tilt, "out-back"]])}
        c.text("word", blk, f.CX, y, enter=("fade", hit * 0.6), animate=anim, **fields)
        if p["echo"]:
            ghost = dict(fields)
            ghost.pop("shadow", None)
            c.text("echo", blk, f.CX, y, rotation=tilt,
                   animate={"scale": _keys([[hit, 1.0], [hit + 0.55, 1.45, "out-cubic"]]),
                            "opacity": _keys([[0, 0], [hit, 0], [hit + 0.02, 0.4], [hit + 0.5, 0, "out-quad"]])},
                   enter=None, exit=None, until=max(0.0, life - hit - 0.6), **ghost)
    else:
        c.text("word", blk, f.CX, y, rotation=tilt, enter=c.prof["title"], drift=True, **fields)


@_template(
    "word_stack", "text",
    "A justified stack of words, each line scaled to the same width (kinetic poster type). Use for "
    "manifesto lines, rules and punchy 3-6 word statements.",
    words=_p("list[str]", ["STOP", "WASTING", "YOUR", "TIME"], "one entry per line"),
    highlight=_p("highlight", [], "lines (words or indices) painted in the accent colour"),
    align=_p("choice", "center", "stack alignment", ["center", "left"]),
    case=_p("choice", "upper", "letter case", _CASES),
)
def _word_stack(c, p):
    f, s, acc = c.f, c.s, c.s["accent"]
    lines = [w for w in p["words"] if w.strip()][:7]
    width = f.SW * 0.92
    hl = set(_highlight_idx(" ".join(x.replace(" ", "_") for x in lines),
                            [h if isinstance(h, int) else str(h).replace(" ", "_") for h in p["highlight"]], p["case"]))
    k = 1.0
    for _ in range(4):
        blks = [c.fit(w, f.d(300) * k, width, lines=1, min_size=f.d(50) * k, case=p["case"], ls=1.0, balance=False)
                for w in lines]
        adv = [b.size * 0.92 for b in blks]
        if sum(adv) <= f.SH * 0.86:
            break
        k *= f.SH * 0.86 / sum(adv) * 0.98
    y = f.CY - sum(adv) / 2
    left = p["align"] == "left"
    g = c.clk.stagger(len(lines), 0.4, per=c.clk.gap * 1.6)
    for i, b in enumerate(blks):
        yc = y + adv[i] / 2
        enter = c.prof["title"] if (i in hl and c.prof["over"] > 0) else c.prof["pop"]
        c.text("line", b, f.L if left else f.CX, yc, anchor="left" if left else "center",
               align="left" if left else "center", color=acc if i in hl else s["text"], shadow=c.text_shadow(),
               enter=enter, at=i * g, drift=True)
        y += adv[i]


@_template(
    "underline_reveal", "text",
    "A line of text with an accent underline or highlighter stroke drawing across it. Use for the key "
    "takeaway, a definition or the sentence you want screenshotted.",
    text=_p("str", "THIS CHANGES EVERYTHING", "the line"),
    highlight=_p("highlight", [], "words painted in the accent colour"),
    mode=_p("choice", "underline", "underline (bar under the last line) or marker (highlighter behind it)",
            ["underline", "marker"]),
    position=_p("choice", "center", "vertical placement", ["top", "center", "bottom"]),
    case=_p("choice", "upper", "letter case", _CASES),
)
def _underline_reveal(c, p):
    f, acc = c.f, c.s["accent"]
    blk = c.fit(p["text"], f.d(104), f.SW, lines=3, min_size=f.d(54), case=p["case"], ls=1.0)
    bar_h = f.d(16) if p["mode"] == "underline" else blk.size * 0.42
    total = blk.h + (f.d(10) + bar_h if p["mode"] == "underline" else 0)
    top = f.place(p["position"], total)
    bottom = top + blk.h
    lw = blk.line_w(-1) * 1.04 + f.d(12)
    draw_at = c.clk.din + c.clk.gap
    look = _look(c, "shadow" if p["mode"] == "underline" else "plain", _highlight_idx(p["text"], p["highlight"],
                                                                                      p["case"]))
    if p["mode"] == "marker":
        # the highlighter sits on the lower half of the last line, drawn behind the text
        y_mark = bottom - blk.size * 0.2
        c.rect("marker", f.CX, y_mark, lw, bar_h, _alpha(acc, 0.9), radius=f.d(6),
               enter=("wipe-right", c.clk.din * 1.6), at=draw_at, exit="fade")
        if look.get("highlight"):
            look["highlight"]["color"] = c.s["text"]
    c.text("text", blk, f.CX, top, anchor="top", enter=c.prof["rise"], drift=False, **look)
    if p["mode"] == "underline":
        c.rect("underline", f.CX, bottom + f.d(10), lw, bar_h, acc, radius=bar_h / 2, anchor="top",
               enter=("wipe-right", c.clk.din * 1.6), at=draw_at, exit="fade")


@_template(
    "quote", "text",
    "A quotation with a big accent quote mark and attribution. Use for expert quotes, a viewer's words, "
    "or the creator's own one-line philosophy.",
    text=_p("str", "Make the thing you wish existed.", "the quote, without quote marks"),
    author=_p("str", "", "who said it"),
    role=_p("str", "", "their title or context, under the name"),
    highlight=_p("highlight", [], "words painted in the accent colour"),
    position=_p("choice", "center", "vertical placement", ["top", "center", "bottom"]),
)
def _quote(c, p):
    c.f = c.f.column(1100)
    f, s, acc = c.f, c.s, c.s["accent"]
    x = f.L + f.d(10)
    width = f.SW - f.d(20)
    q = c.body(p["text"].strip().strip("\"\u201c\u201d"), f.d(82), width, lines=6, min_size=f.d(40), ls=1.18)
    author = c.body("\u2014 " + p["author"], f.d(44), width, lines=1, ls=1.0) if p["author"] else None
    role = c.body(p["role"], f.d(34), width, lines=1, ls=1.0) if p["role"] else None
    mark = fit_text("\u201c", s["font"], f.d(340), f.SW, 1)
    mark_h = f.d(170)
    total = mark_h + q.h + ((f.d(40) + author.h) if author else 0) + ((f.d(8) + role.h) if role else 0)
    y = f.place(p["position"], total)
    g = c.clk.stagger(4, 0.4)
    c.text("mark", mark, x - f.d(6), y - f.d(30), anchor="top-left", align="left", color=acc, enter=c.prof["pop"])
    y += mark_h
    words = len(q.text.split())
    reveal = ("words", min(c.clk.dur * 0.45, 0.12 * words + 0.2)) if c.prof["over"] > 0 else c.prof["rise"]
    lag = reveal[1] * 0.6 if isinstance(reveal, tuple) else 0.0     # attribution waits for the words
    c.text("text", q, x, y, anchor="top-left", align="left", shadow=c.text_shadow(),
           highlight=({"words": _highlight_idx(q.text, p["highlight"], "none"), "color": acc}
                      if p["highlight"] else None),
           enter=reveal, at=g)
    y += q.h
    if author:
        y += f.d(40)
        c.text("author", author, x, y, anchor="top-left", align="left", color=acc, shadow=c.text_shadow(0.6),
               enter=c.prof["rise"], at=g * 2 + lag)
        y += author.h
    if role:
        c.text("role", role, x, y + f.d(8), anchor="top-left", align="left", color=_alpha(s["text"], 0.72),
               shadow=c.text_shadow(0.6), enter="fade", at=g * 3 + lag)


# =========================================================================== numbers and data

def _fmt_number(v, decimals, sep):
    s = "%.*f" % (max(0, decimals), abs(v))
    whole, _, frac = s.partition(".")
    if sep:
        whole = "{:,}".format(int(whole)).replace(",", sep)
    return ("-" if v < 0 else "") + whole + ("." + frac if frac else "")


@_template(
    "stat", "numbers",
    "One big number with a label ('73%' / 'of viewers decide in 3 seconds'). Use when the script states a "
    "fact or result; the number lands with a punch, the label follows.",
    value=_p("str", "73%", "the number exactly as it should read, e.g. '73%', '$4.2M', '3x'"),
    label=_p("str", "of viewers decide in the first 3 seconds", "what the number means"),
    source=_p("str", "", "small source line, e.g. 'Source: platform data, 2025'"),
    color=_p("color", "", "number colour ('' = accent)"),
    position=_p("choice", "center", "vertical placement", ["top", "center", "bottom"]),
)
def _stat(c, p):
    c.f = c.f.column(1100)
    f, s = c.f, c.s
    col = c.color(p["color"])
    val = c.fit(p["value"], f.d(300), f.SW * 0.96, lines=1, min_size=f.d(110))
    lab = c.body(p["label"], f.d(54), f.SW * 0.88, lines=3, min_size=f.d(36)) if p["label"] else None
    src = c.body(p["source"], f.d(30), f.SW * 0.85, lines=2, min_size=f.d(24)) if p["source"] else None
    bar_h, gap = f.d(10), f.d(30)
    total = val.h + gap + bar_h + (gap + lab.h if lab else 0) + (gap + src.h if src else 0)
    y = f.place(p["position"], total) + val.h
    g = c.clk.stagger(4, 0.4)
    life = c.clk.dur
    if c.prof["over"] > 0:
        c.text("value", val, f.CX, y, anchor="bottom", color=col, shadow=c.text_shadow(),
               enter=("fade", c.clk.din * 0.3), animate={"scale": c.pop_keys(life, 0.3)})
    else:
        c.text("value", val, f.CX, y, anchor="bottom", color=col, shadow=c.text_shadow(), enter=c.prof["title"])
    y += gap
    c.rect("bar", f.CX, y, f.d(150), bar_h, c.s["accent"], radius=bar_h / 2, anchor="top",
           enter=("wipe-right", c.clk.din * 1.3), at=g)
    y += bar_h
    if lab:
        c.text("label", lab, f.CX, y + gap, anchor="top", shadow=c.text_shadow(0.8), enter=c.prof["rise"], at=g * 2)
        y += gap + lab.h
    if src:
        c.text("source", src, f.CX, y + gap, anchor="top", color=_alpha(s["text"], 0.62), enter="fade", at=g * 3)


@_template(
    "counter_reveal", "numbers",
    "A number that counts up (0 -> 12,500 views) with a bar filling in sync and a punch when it lands. "
    "Use for growth, money, views, followers: any number whose size is the point.",
    value=_p("float", 12500, "final number"),
    **{"from": _p("float", 0, "starting number")},
    prefix=_p("str", "", "text before the number, e.g. '$'"),
    suffix=_p("str", "", "text after the number, e.g. '%' or ' views'"),
    decimals=_p("int", 0, "digits after the decimal point"),
    separator=_p("str", ",", "thousands separator ('' for none)"),
    label=_p("str", "views in 24 hours", "what the number means"),
    duration=_p("float", 0, "seconds the count takes; 0 = by energy"),
    color=_p("color", "", "number colour ('' = text colour)"),
    position=_p("choice", "center", "vertical placement", ["top", "center", "bottom"]),
)
def _counter_reveal(c, p):
    c.f = c.f.column(1100)
    f, s, acc = c.f, c.s, c.s["accent"]
    col = c.color(p["color"], "text")
    dec = max(0, min(6, p["decimals"]))
    longest = max((_fmt_number(v, dec, p["separator"]) for v in (p["value"], p["from"])), key=len)
    num = c.fit(p["prefix"] + longest + p["suffix"], f.d(230), f.SW * 0.94, lines=1, min_size=f.d(80))
    lab = c.body(p["label"], f.d(50), f.SW * 0.86, lines=2, min_size=f.d(34)) if p["label"] else None
    life = c.clk.dur
    cd = p["duration"] if p["duration"] > 0 else {"high": 1.3, "medium": 1.7, "calm": 2.2}[c.energy]
    cd = max(min(0.3, life * 0.5), min(cd, life * 0.62))
    bar_w, bar_h, gap = min(f.d(560), f.SW * 0.7), f.d(14), f.d(34)
    total = num.h + gap + bar_h + ((gap + lab.h) if lab else 0)
    y = f.place(p["position"], total) + num.h
    ease = c.prof["count_ease"]
    keys = [[0, 0.85], [c.clk.din, 1.0, "out-cubic"]]
    if c.prof["over"] > 0 and cd > c.clk.din and cd + 0.35 < life - c.clk.dout:   # a punch when the count lands
        keys += [[cd, 1.0], [cd + 0.09, 1.1, "out-quad"], [cd + 0.32, 1.0, "out-back"]]
    c.counter("number", num, f.CX, y, p["from"], p["value"], dec, p["prefix"], p["suffix"], p["separator"], cd, ease,
              anchor="bottom", color=col, shadow=c.text_shadow(), enter=("fade", c.clk.din * 0.5),
              animate={"scale": _keys(keys)})
    y += gap
    c.rect("track", f.CX, y, bar_w, bar_h, _alpha(s["text"], 0.16), radius=bar_h / 2, anchor="top", enter="fade")
    c.rect("fill", f.CX - bar_w / 2, y, bar_w, bar_h, acc, radius=bar_h / 2, anchor="top-left",
           enter=("wipe-right", cd))
    if lab:
        c.text("label", lab, f.CX, y + bar_h + gap, anchor="top", shadow=c.text_shadow(0.8), enter=c.prof["rise"],
               at=c.clk.gap * 2)


@_template(
    "percent_ring", "numbers",
    "A ring that fills to a percentage while the number counts up in its centre. Use for completion rates, "
    "retention, survey results: any share of a whole.",
    value=_p("float", 87, "percentage 0-100"),
    label=_p("str", "of viewers watched to the end", "what the percentage means"),
    suffix=_p("str", "%", "text after the number"),
    decimals=_p("int", 0, "digits after the decimal point"),
    color=_p("color", "", "ring colour ('' = accent)"),
)
def _percent_ring(c, p):
    f, s = c.f, c.s
    col = c.color(p["color"])
    value = max(0.0, min(100.0, p["value"]))
    dec = max(0, min(6, p["decimals"]))
    lab = c.body(p["label"], f.d(48), f.SW * 0.86, lines=2, min_size=f.d(32)) if p["label"] else None
    gap = f.d(48)
    D = min(f.d(500), f.SW * 0.8, (f.SH - (gap + lab.h if lab else 0)) * 0.9)
    width = D * 0.075
    total = D + ((gap + lab.h) if lab else 0)
    top = f.place("center", total)
    cy = top + D / 2
    life = c.clk.dur
    cd = max(0.4, min({"high": 1.2, "medium": 1.6, "calm": 2.0}[c.energy], life * 0.6))
    ease = c.prof["count_ease"]
    grp = c.group((f.CX, cy), intro="pop" if c.prof["over"] > 0 else "fade", outro=c.prof["group_out"])
    grp.ring("track", f.CX, cy, D, _alpha(s["text"], 0.14), width)
    grp.ring("arc", f.CX, cy, D, col, width, draw={"from": 0.0, "to": round(value / 100.0, 4), "ease": ease},
             enter=("none", cd))
    num = c.fit(_fmt_number(value, dec, "") + p["suffix"], D * 0.3, D * 0.62, lines=1)
    grp.counter("number", num, f.CX, cy, 0, round(value, 6), dec, "", p["suffix"], "", cd, ease)
    if lab:
        c.text("label", lab, f.CX, top + D + gap, anchor="top", shadow=c.text_shadow(0.8), enter=c.prof["rise"],
               at=c.clk.gap * 2)


@_template(
    "bar_chart", "numbers",
    "An animated bar chart (2-7 bars) with counting values: growth over time, us vs them, before/after "
    "numbers. Horizontal bars on vertical video, columns on landscape.",
    labels=_p("list[str]", ["2023", "2024", "2025"], "bar labels"),
    values=_p("list[float]", [12, 30, 85], "bar values (same order as labels)"),
    title=_p("str", "", "chart title"),
    prefix=_p("str", "", "text before each value"),
    suffix=_p("str", "", "text after each value, e.g. 'K'"),
    decimals=_p("int", 0, "digits after the decimal point"),
)
def _bar_chart(c, p):
    f, s, acc = c.f, c.s, c.s["accent"]
    vals = [max(0.0, v) for v in p["values"]][:7]
    labels = (p["labels"] + [""] * len(vals))[:len(vals)]
    n = len(vals)
    vmax = max(vals) or 1.0
    dec = max(0, min(6, p["decimals"]))
    title = c.fit(p["title"], f.d(72), f.SW, lines=2, min_size=f.d(44), case="upper", ls=1.0) if p["title"] else None
    g = c.clk.stagger(n, 0.4, per=0.16)
    grow = max(0.35, min(0.9, c.clk.dur * 0.35))
    other = _alpha(s["text"], 0.85)
    if f.wide:
        cw = min(f.d(150), f.SW / (n * 1.7))
        base = f.B - f.d(80)
        top = f.T + ((title.h + f.d(40)) if title else 0) + f.d(90)
        hmax = base - top
        pitch = f.SW / n
        if title:
            c.text("title", title, f.CX, f.T, anchor="top", shadow=c.text_shadow(), enter=c.prof["rise"])
        c.rect("axis", f.CX, base, f.SW, f.d(4), _alpha(s["text"], 0.35), anchor="top", enter=("wipe-right", grow))
        for i, v in enumerate(vals):
            x = f.L + pitch * (i + 0.5)
            h = max(f.d(8), hmax * v / vmax)
            col = acc if v == vmax else other
            c.rect("bar", x, base, cw, h, col, radius=f.d(10), anchor="bottom", enter=("wipe-up", grow), at=g * i)
            num = c.fit(p["prefix"] + _fmt_number(v, dec, ",") + p["suffix"], f.d(54), pitch * 0.95, lines=1)
            c.counter("value", num, x, base - h - f.d(14), 0, v, dec, p["prefix"], p["suffix"], ",", grow,
                      c.prof["count_ease"], anchor="bottom", enter="fade", at=g * i)
            lab = c.body(labels[i], f.d(38), pitch * 0.95, lines=1)
            c.text("label", lab, x, base + f.d(18), anchor="top", color=_alpha(s["text"], 0.85), enter="fade",
                   at=g * i)
        return
    lab_size, num_size, bar_h = f.d(44), f.d(56), f.d(42)
    row_h = num_size * 0.74 + f.d(22) + bar_h
    gap = f.d(44)
    needed = (title.h + f.d(50) if title else 0) + n * row_h + (n - 1) * gap
    k = min(1.0, f.SH * 0.9 / needed)
    row_h, gap, bar_h = row_h * k, gap * k, bar_h * k
    y = f.place("center", needed * k)
    if title:
        c.text("title", title, f.CX, y, anchor="top", shadow=c.text_shadow(), enter=c.prof["rise"])
        y += title.h + f.d(50) * k
    for i, v in enumerate(vals):
        col = acc if v == vmax else other
        lab = c.body(labels[i], lab_size * k, f.SW * 0.6, lines=1, ls=1.0)
        num = c.fit(p["prefix"] + _fmt_number(v, dec, ",") + p["suffix"], num_size * k, f.SW * 0.38, lines=1)
        row = c.group((f.L, y + row_h / 2), intro="left" if c.prof["over"] > 0 else "fade", outro="fade",
                      at=g * i)
        mid = y + num.size * 0.37
        row.text("label", lab, f.L, mid, anchor="left", align="left", color=_alpha(s["text"], 0.88))
        row.counter("value", num, f.R, mid, 0, v, dec, p["prefix"], p["suffix"], ",", grow, c.prof["count_ease"],
                    anchor="right", color=col if v == vmax else s["text"])
        by = y + row_h - bar_h
        row.rect("track", f.L, by, f.SW, bar_h, _alpha(s["text"], 0.12), radius=bar_h / 2, anchor="top-left")
        row.rect("bar", f.L, by, max(bar_h, f.SW * v / vmax), bar_h, col, radius=bar_h / 2, anchor="top-left",
                 enter=("wipe-right", grow))
        y += row_h + gap


@_template(
    "rating", "numbers",
    "A row of stars filling to a score (4.5 / 5) with the number. Use for reviews, verdicts, tier "
    "ratings and 'I rated every X' formats.",
    value=_p("float", 4.5, "score; halves are drawn as half stars"),
    max=_p("int", 5, "number of stars (3-10)"),
    label=_p("str", "", "what is being rated, shown above the stars"),
    show_number=_p("bool", True, "show '4.5/5' under the stars"),
    color=_p("color", "", "star colour ('' = accent)"),
)
def _rating(c, p):
    f, s = c.f, c.s
    col = c.color(p["color"])
    n = max(3, min(10, p["max"]))
    value = max(0.0, min(float(n), p["value"]))
    S = min(f.d(150), f.SW * 0.94 / (n * 1.12))
    pitch = S * 1.12
    lab = c.fit(p["label"], f.d(72), f.SW, lines=2, min_size=f.d(44), case="upper", ls=1.0) if p["label"] else None
    num = c.fit("%s/%d" % (_fmt_number(value, 1 if value % 1 else 0, ""), n), f.d(110), f.SW * 0.6, lines=1)
    gap = f.d(40)
    total = S + ((lab.h + gap) if lab else 0) + ((gap + num.h) if p["show_number"] else 0)
    y = f.place("center", total)
    if lab:
        c.text("label", lab, f.CX, y, anchor="top", shadow=c.text_shadow(), enter=c.prof["rise"])
        y += lab.h + gap
    cy = y + S / 2
    g = c.clk.stagger(n, 0.35, per=0.09)
    star = ICONS["star"][0]["points"]
    for i in range(n):
        x = f.CX + (i - (n - 1) / 2) * pitch
        c.poly("empty", x, cy, S, S, star, _alpha(s["text"], 0.22), enter="fade", at=g * i)
        fill = value - i
        if fill >= 0.75:
            c.poly("star", x, cy, S, S, star, col, enter=c.prof["pop"], at=g * i + c.clk.gap)
        elif fill >= 0.25:
            c.poly("half", x, cy, S, S, _half_left(star), col, enter=c.prof["pop"], at=g * i + c.clk.gap)
    if p["show_number"]:
        c.text("number", num, f.CX, y + S + gap, anchor="top", shadow=c.text_shadow(), enter=c.prof["rise"],
               at=g * n + c.clk.gap)


@_template(
    "price_tag", "numbers",
    "A swinging price tag with an optional crossed-out old price. Use for product prices, deals, "
    "'this cost me $X', cost comparisons.",
    price=_p("str", "$49", "the price as it should read"),
    old_price=_p("str", "", "previous price, shown crossed out"),
    label=_p("str", "", "small pill above, e.g. 'TODAY ONLY'"),
    note=_p("str", "", "line under the tag"),
    color=_p("color", "", "tag colour ('' = accent)"),
)
def _price_tag(c, p):
    f, s = c.f, c.s
    col = c.color(p["color"])
    price = c.fit(p["price"], f.d(190), f.SW * 0.62, lines=1, min_size=f.d(70))
    h = price.size * 1.45
    body_w = price.w + h * 0.7
    # the tag body is centred in its box (the point sits in the left margin) so text and
    # tag rotate about the same centre
    w = body_w / 0.68
    tag = [[0.0, 0.5], [0.16, 0.06], [0.81, 0.06], [0.84, 0.13], [0.84, 0.87], [0.81, 0.94], [0.16, 0.94]]
    hole = _circle_pts(0.1, 0.5, 0.06 * h / w, 24, w / h)
    old = c.body(p["old_price"], f.d(64), f.SW * 0.6, lines=1, ls=1.0) if p["old_price"] else None
    lab = c.body(p["label"], f.d(34), f.SW * 0.7, lines=1, case="upper", lsp=f.d(4), ls=1.0) if p["label"] else None
    note = c.body(p["note"], f.d(44), f.SW * 0.86, lines=2) if p["note"] else None
    gap = f.d(30)
    lpad, lh = _pill(c, lab, (22, 14)) if lab else ([0, 0], 0)
    total = h + ((lh + gap) if lab else 0) + ((old.h + gap * 0.5) if old else 0) + ((gap + note.h) if note else 0)
    y = f.place("center", total)
    g = c.clk.stagger(4, 0.4)
    if lab:
        c.text("label", lab, f.CX, y, anchor="top", color=s["text"],
               box={"color": _alpha(s["bg2"], 0.95), "pad": lpad, "radius": _r(lh / 2)}, enter=c.prof["pop"])
        y += lh + gap
    if old:
        c.text("old", old, f.CX, y, anchor="top", color=_alpha(s["text"], 0.6), enter="fade", at=g)
        c.rect("strike", f.CX, y + old.h * 0.52, old.w * 1.08, f.d(7), "#FF3B4E", radius=f.d(3),
               rotation=-6, enter=("wipe-right", c.clk.din), at=g * 2)
        y += old.h + gap * 0.5
    cy = y + h / 2
    cx = f.CX + 0.04 * w       # centre the whole tag (point + body), not just the body
    swing = None
    if c.prof["over"] > 0:
        o = c.prof["over"]
        swing = {"rotation": _keys([[0, -22 * o], [0.32, 9 * o, "out-cubic"], [0.6, -4 * o, "in-out-quad"],
                                    [0.85, 1.5 * o, "in-out-quad"], [1.05, 0, "in-out-quad"]])}
    for part, pts, colr in (("tag", tag, col), ("hole", hole, _mix(col, "#000000", 0.35))):
        c.poly(part, cx, cy, w, h, pts, colr, enter=c.prof["pop"], at=g,
               shadow=c.card_shadow() if part == "tag" else None, **({"animate": dict(swing)} if swing else {}))
    c.text("price", price, cx, cy, color=_on(col), enter=c.prof["pop"], at=g,
           **({"animate": dict(swing)} if swing else {}))
    if note:
        c.text("note", note, f.CX, y + h + gap, anchor="top", shadow=c.text_shadow(0.8), enter=c.prof["rise"],
               at=g * 3)


@_template(
    "countdown", "numbers",
    "3-2-1 countdown with a ring per second and an optional 'GO!'. Use before a reveal, a challenge "
    "start or a transformation.",
    **{"from": _p("int", 3, "first number (2-10)")},
    go=_p("str", "GO!", "word after the last number ('' for none)"),
    ring=_p("bool", True, "a ring drawing around each number"),
    color=_p("color", "", "ring and GO colour ('' = accent)"),
)
def _countdown(c, p):
    f, s = c.f, c.s
    col = c.color(p["color"])
    n = max(1, min(10, p["from"]))
    slots = n + (1 if p["go"] else 0)
    while slots > 1 and c.clk.dur / slots < 0.35:      # too short for every number: drop the first ones
        n -= 1
        slots -= 1
    slot = c.clk.dur / slots
    D = min(f.d(560), f.SW * 0.85, f.SH * 0.6)
    cy = f.CY
    if p["ring"]:
        c.ring("track", f.CX, cy, D, _alpha(s["text"], 0.14), D * 0.05, enter="fade", exit="fade")
    out_d = min(0.12, slot * 0.2)
    for i in range(n):
        # a number (and its full arc) stays until the next one has landed: its exit runs over the next
        # entrance instead of ending on the frame where the next one starts invisible
        last = i == slots - 1
        at = i * slot
        until = max(0.0, c.clk.dur - (i + 1) * slot - (0.0 if last else out_d))
        num = fit_text(str(n - i), s["font"], D * 0.62, D * 0.8, 1)
        if p["ring"]:
            c.ring("arc", f.CX, cy, D, col, D * 0.05, draw={"from": 0.0, "to": 1.0, "ease": "linear"},
                   enter=("none", slot * 0.95), exit=("fade", out_d), at=at, until=until, min_life=0.1)
        c.text("number", num, f.CX, cy, shadow=c.text_shadow(), enter=(c.prof["title"], min(0.3, slot * 0.45)),
               exit=(c.prof["exit"], out_d), at=at, until=until, min_life=0.1)
    if p["go"]:
        go = c.fit(p["go"], D * 0.42, f.SW * 0.9, lines=1, case="upper")
        gpad, _ = _pill(c, go, (44, 26))
        c.text("go", go, f.CX, cy, color=_on(col), box={"color": col, "pad": gpad, "radius": _r(f.d(24))},
               enter=(c.prof["title"], min(0.3, slot * 0.45)), at=n * slot, min_life=0.1, shadow=c.card_shadow())


@_template(
    "big_number_bg", "numbers",
    "A giant, faint number behind everything (list position '3', a year, a percentage). Put it first in the "
    "beat's visual list so other templates draw over it.",
    number=_p("str", "3", "the number or short word"),
    color=_p("color", "", "colour ('' = text colour)"),
    opacity=_p("float", 0.12, "0.05-0.4; keep it faint"),
)
def _big_number_bg(c, p):
    f = c.f
    c.fixed = False
    col = c.color(p["color"], "text")
    size = min(f.H * 0.78, f.W * 1.4)
    blk = c.fit(p["number"], size, f.W * 0.94, lines=1, min_size=size * 0.3, ls=0.9)
    op = max(0.03, min(0.5, p["opacity"]))
    life = c.clk.dur
    c.text("number", blk, f.W / 2, f.H / 2, color=col, opacity=op, enter=("fade", c.clk.din * 1.5), exit="fade",
           animate={"scale": _keys([[0, 1.08], [life, 1.0, "out-cubic"]])})


# =========================================================================== lists and structure

_GOOD, _BAD = "#22C55E", "#FF3B4E"


def _clamp01(v):
    return min(1.0, max(0.0, v))


def _fit_stack(build_rows, avail, tries=4):
    """Call build_rows(k) -> (rows, needed_height) with a shrinking k until the stack fits `avail`."""
    k = 1.0
    for _ in range(tries):
        rows, needed = build_rows(k)
        if needed <= avail:
            break
        k *= avail / needed * 0.98
    return rows, needed, k


@_template(
    "list_item", "lists",
    "One numbered point of a list video ('1  Hook in the first second') on a card with a number badge. "
    "Use one per list beat; set `total` to show progress (2/5).",
    number=_p("int", 1, "the item number"),
    text=_p("str", "Hook in the first second", "the item"),
    sub=_p("str", "", "one supporting line"),
    total=_p("int", 0, "items in the list; > 0 shows '2/5' on the card"),
    y=_p("float", 0.5, "vertical centre as a fraction of the safe zone (0 = top, 1 = bottom)"),
)
def _list_item(c, p):
    c.f = c.f.column(1100)
    f, s, acc = c.f, c.s, c.s["accent"]
    pad, D, gap = f.d(34), f.d(124), f.d(34)
    tx = f.L + pad + D + gap
    tw = f.R - pad - tx - (f.d(90) if p["total"] > 0 else 0)
    main = c.fit(p["text"], f.d(62), tw, lines=3, min_size=f.d(40), ls=1.05)
    sub = c.body(p["sub"], f.d(40), tw, lines=2, min_size=f.d(30)) if p["sub"] else None
    th = main.h + ((f.d(10) + sub.h) if sub else 0)
    card_h = max(D + 2 * pad, th + 2 * pad)
    cy = f.clamp_y(f.T + f.SH * _clamp01(p["y"]), card_h / 2)
    grp = c.group((f.L, cy), intro="left" if c.prof["over"] > 0 else "fade")
    grp.rect("card", f.CX, cy, f.SW, card_h, _alpha(s["bg2"], 0.94), radius=f.d(36), shadow=c.card_shadow())
    bx = f.L + pad + D / 2
    num = fit_text(str(p["number"]), s["font"], D * 0.54, D * 0.8, 1)
    pop = (c.prof["pop"], c.clk.din, c.clk.gap * 1.5)
    grp.circle("badge", bx, cy, D, acc, enter=pop)
    grp.text("number", num, bx, cy, color=_on(acc), enter=pop)
    ty = cy - th / 2
    grp.text("text", main, tx, ty, anchor="top-left", align="left", enter=("fade", c.clk.din, c.clk.gap * 2.5))
    if sub:
        grp.text("sub", sub, tx, ty + main.h + f.d(10), anchor="top-left", align="left",
                 color=_alpha(s["text"], 0.72), enter=("fade", c.clk.din, c.clk.gap * 3.5))
    if p["total"] > 0:
        cnt = c.body("%d/%d" % (p["number"], p["total"]), f.d(30), f.d(200), lines=1, ls=1.0)
        grp.text("count", cnt, f.R - f.d(24), cy - card_h / 2 + f.d(20), anchor="top-right",
                 color=_alpha(s["text"], 0.55), enter=("fade", c.clk.din, c.clk.gap * 3))


@_template(
    "checklist", "lists",
    "Items appearing one by one, each ticked (or crossed) as it lands. Use for requirements, routines, "
    "'what you need', or a recap of the tips in the video.",
    items=_p("list[str]", ["Hook in the first second", "Captions on", "Loop the ending"], "the items (up to 7)"),
    title=_p("str", "", "heading above the list"),
    mark=_p("choice", "check", "check (ticked), cross (crossed off) or none (empty boxes)", ["check", "cross", "none"]),
    position=_p("choice", "center", "vertical placement", ["top", "center", "bottom"]),
)
def _checklist(c, p):
    c.f = c.f.column(1100)
    f, s, acc = c.f, c.s, c.s["accent"]
    items = p["items"][:7]
    n = len(items)

    def rows(k):
        B, gap = f.d(78) * k, f.d(34) * k
        tx = f.L + B + f.d(34) * k
        blks = [c.fit(it, f.d(56) * k, f.R - tx, lines=2, min_size=f.d(34) * k, ls=1.05) for it in items]
        title = c.fit(p["title"], f.d(76) * k, f.SW, lines=2, min_size=f.d(44) * k, case="upper",
                      ls=1.0) if p["title"] else None
        heights = [max(B, b.h) for b in blks]
        needed = (title.h + gap * 1.2 if title else 0) + sum(heights) + gap * (n - 1)
        return (B, gap, tx, blks, title, heights), needed

    (B, gap, tx, blks, title, heights), needed, k = _fit_stack(rows, f.SH * 0.9)
    y = f.place(p["position"], needed)
    per = {"high": 0.45, "medium": 0.6, "calm": 0.8}[c.energy]
    step = c.clk.stagger(n + (1 if title else 0), 0.55, per=per)
    t = 0.0
    if title:
        c.text("title", title, f.L, y, anchor="top-left", align="left", color=acc, shadow=c.text_shadow(),
               enter=c.prof["rise"])
        y += title.h + gap * 1.2
        t = step
    over = c.prof["over"] > 0
    colr = acc if p["mark"] == "check" else _BAD
    for i, b in enumerate(blks):
        cy = y + heights[i] / 2
        row = c.group((f.L, cy), intro="left" if over else "fade", outro="fade", at=t + i * step, dist=f.d(90))
        bx = f.L + B / 2
        edge = f.d(6) * k     # an outlined box: a light square with a dark one inside
        row.rect("box", bx, cy, B, B, s["text"], radius=B * 0.22)
        row.rect("box_inner", bx, cy, B - 2 * edge, B - 2 * edge, _mix(s["bg"], s["bg2"], 0.5),
                 radius=max(0.0, B * 0.22 - edge))
        if p["mark"] != "none":
            tick = (c.prof["pop"], c.clk.din * 0.8, c.clk.din * 0.9)
            row.rect("fill", bx, cy, B, B, colr, radius=B * 0.22, enter=tick)
            row.icon("mark", p["mark"], bx, cy, B * 0.72, _on(colr), colr, enter=tick)
        row.text("item", b, tx, cy, anchor="left", align="left", shadow=c.text_shadow(0.7),
                 enter=("fade", c.clk.din * 0.8, c.clk.gap))
        y += heights[i] + gap


@_template(
    "ranking", "lists",
    "A top-N leaderboard (up to 6) revealed from last place to #1, the winner on an accent card with a crown. "
    "Use for tier lists, 'top 5', best-to-worst rankings.",
    items=_p("list[str]", ["Hooks", "Pacing", "Captions"], "best first (the first item is #1)"),
    title=_p("str", "TOP 3", "heading"),
    order=_p("choice", "countdown", "countdown reveals the last place first and #1 last; top-down starts at #1",
             ["countdown", "top-down"]),
)
def _ranking(c, p):
    c.f = c.f.column(1100)
    f, s, acc = c.f, c.s, c.s["accent"]
    items = p["items"][:6]
    n = len(items)

    def rows(k):
        rh0, gap = f.d(132) * k, f.d(22) * k
        pad, rank_w = f.d(30) * k, f.d(150) * k
        title = c.fit(p["title"], f.d(84) * k, f.SW, lines=2, min_size=f.d(48) * k, case="upper",
                      ls=1.0) if p["title"] else None
        # one text size for every row (the longest name sets it; it may wrap to two lines rather
        # than shrink far), and one row height that holds the tallest name
        room = [f.SW - pad * 2 - rank_w - (rh0 * 0.56 if i == 0 else 0) for i in range(n)]
        size = min(c.fit(items[i], f.d(58) * k, room[i], lines=2, min_size=f.d(34) * k, case="upper",
                         ls=1.0, prefer=1).size for i in range(n))
        names = [c.fit(items[i], size, room[i], lines=2, min_size=size, case="upper", ls=1.0) for i in range(n)]
        rh = max(rh0, max(b.h for b in names) + f.d(44) * k)
        needed = (title.h + gap * 2 if title else 0) + n * rh + (n - 1) * gap
        return (rh0, rh, gap, title, names), needed

    (rh0, rh, gap, title, names), needed, k = _fit_stack(rows, f.SH * 0.92)
    y = f.place("center", needed)
    per = {"high": 0.42, "medium": 0.55, "calm": 0.7}[c.energy]
    step = c.clk.stagger(n + (1 if title else 0), 0.6, per=per)
    t0 = 0.0
    if title:
        c.text("title", title, f.CX, y, anchor="top", shadow=c.text_shadow(), enter=c.prof["title"])
        y += title.h + gap * 2
        t0 = step
    order = list(range(n))[::-1] if p["order"] == "countdown" else list(range(n))
    pad = f.d(30) * k
    rank_w = f.d(150) * k
    over = c.prof["over"] > 0
    for i in range(n):
        cy = y + i * (rh + gap) + rh / 2
        top = i == 0
        card = acc if top else _alpha(s["bg2"], 0.94)
        ink = _on(acc) if top else s["text"]
        intro = ("pop" if top else "right") if over else "fade"
        row = c.group((f.CX, cy), intro=intro, outro="fade", at=t0 + order.index(i) * step, dist=f.d(160))
        row.rect("card", f.CX, cy, f.SW, rh, card, radius=f.d(28) * k, shadow=c.card_shadow())
        rank = fit_text("#%d" % (i + 1), s["font"], rh0 * 0.46, rank_w, 1)
        row.text("rank", rank, f.L + pad, cy, anchor="left", align="left", color=ink if top else acc)
        crown = rh0 * 0.56 if top else 0
        row.text("name", names[i], f.L + pad + rank_w, cy, anchor="left", align="left", color=ink)
        if top:
            row.icon("crown", "crown", f.R - pad - crown / 2, cy, crown, ink, acc,
                     enter=(c.prof["pop"], c.clk.din, c.clk.din))


@_template(
    "timeline_steps", "lists",
    "Steps on a line that draws from one to the next: a process, a roadmap, a story's timeline "
    "('0 to 100K in 4 steps'). Vertical on tall frames, horizontal on wide ones.",
    steps=_p("list[str]", ["Idea", "Script", "Shoot", "Edit", "Post"], "step labels (2-6)"),
    title=_p("str", "", "heading"),
    active=_p("int", -1, "index of the step to emphasise; -1 = none"),
    layout=_p("choice", "auto", "auto (vertical on tall frames), vertical or horizontal",
              ["auto", "vertical", "horizontal"]),
)
def _timeline_steps(c, p):
    f, s, acc = c.f, c.s, c.s["accent"]
    steps = p["steps"][:6]
    n = len(steps)
    vertical = p["layout"] == "vertical" or (p["layout"] == "auto" and not f.wide)
    title = c.fit(p["title"], f.d(76), f.SW, lines=2, min_size=f.d(44), case="upper", ls=1.0) if p["title"] else None
    gap = f.d(50)
    dot, thick = f.d(46), f.d(8)
    per = {"high": 0.38, "medium": 0.5, "calm": 0.65}[c.energy]
    step_t = c.clk.stagger(n, 0.6, per=per)
    t0 = c.clk.gap * 2 if title else 0.0
    head = (title.h + gap) if title else 0.0
    track = _alpha(s["text"], 0.2)
    if vertical:
        pitch = min(f.d(210), (f.SH * 0.9 - head - dot) / max(1, n - 1))
        total = head + pitch * (n - 1) + dot
        y = f.place("center", total)
        x_line = f.L + dot / 2 + f.d(8)
        if title:
            c.text("title", title, f.L, y, anchor="top-left", align="left", shadow=c.text_shadow(),
                   enter=c.prof["rise"])
        y0 = y + head + dot / 2
        span = pitch * (n - 1)
        if n > 1:
            c.shape("track", "line", x_line, y0 + span / 2, span, thick, track, rotation=90, enter="fade")
        for i, label in enumerate(steps):
            yi = y0 + i * pitch
            at = t0 + i * step_t
            if i < n - 1:
                c.shape("segment", "line", x_line, yi + pitch / 2, pitch, thick, acc, rotation=90,
                        draw={"from": 0.0, "to": 1.0, "ease": "in-out-quad"}, enter=("none", step_t), at=at)
            on = i == p["active"]
            col = acc if (on or p["active"] < 0) else _mix(acc, s["bg2"], 0.55)
            c.circle("dot", x_line, yi, dot * (1.35 if on else 1.0), col, enter=c.prof["pop"], at=at,
                     shadow=c.card_shadow())
            lab = c.fit(label, f.d(58), f.R - x_line - dot - f.d(30), lines=2, min_size=f.d(36), ls=1.0)
            c.text("label", lab, x_line + dot / 2 + f.d(34), yi, anchor="left", align="left",
                   color=acc if on else s["text"], shadow=c.text_shadow(0.7), enter=c.prof["rise"],
                   at=at + c.clk.gap)
        return
    pitch = f.SW / n
    lab_blks = [c.fit(x, f.d(48), pitch * 0.9, lines=2, min_size=f.d(30), ls=1.0) for x in steps]
    num_size = f.d(34)
    total = head + num_size * 0.74 + f.d(18) + dot + f.d(24) + max(b.h for b in lab_blks)
    y = f.place("center", total)
    if title:
        c.text("title", title, f.CX, y, anchor="top", shadow=c.text_shadow(), enter=c.prof["rise"])
    y_line = y + head + num_size * 0.74 + f.d(18) + dot / 2
    x0 = f.L + pitch / 2
    if n > 1:
        c.rect("track", x0, y_line, pitch * (n - 1), thick, track, anchor="left", enter="fade")
    for i, b in enumerate(lab_blks):
        xi = x0 + i * pitch
        at = t0 + i * step_t
        if i < n - 1:
            c.rect("segment", xi, y_line, pitch, thick, acc, anchor="left", enter=("wipe-right", step_t), at=at)
        on = i == p["active"]
        col = acc if (on or p["active"] < 0) else _mix(acc, s["bg2"], 0.55)
        c.circle("dot", xi, y_line, dot * (1.35 if on else 1.0), col, enter=c.prof["pop"], at=at)
        num = c.body("%02d" % (i + 1), num_size, pitch * 0.8, lines=1, ls=1.0)
        c.text("number", num, xi, y_line - dot / 2 - f.d(18), anchor="bottom", color=acc, enter="fade", at=at)
        c.text("label", b, xi, y_line + dot / 2 + f.d(24), anchor="top", color=acc if on else s["text"],
               shadow=c.text_shadow(0.7), enter=c.prof["rise"], at=at + c.clk.gap)


@_template(
    "step_number", "lists",
    "A big 'STEP 01' marker with the step's instruction, left-aligned. Use at the start of each step "
    "in tutorials and how-to videos.",
    number=_p("int", 1, "the step number"),
    text=_p("str", "Find your hook", "what this step is"),
    label=_p("str", "STEP", "word before the number"),
    total=_p("int", 0, "number of steps; > 0 shows 'STEP 1 OF 5'"),
    position=_p("choice", "top", "vertical placement", ["top", "center", "bottom"]),
)
def _step_number(c, p):
    f, acc = c.f, c.s["accent"]
    kick_text = p["label"] + (" %d OF %d" % (p["number"], p["total"]) if p["total"] > 0 else "")
    kick = c.body(kick_text, f.d(38), f.SW, lines=1, case="upper", lsp=f.d(8), ls=1.0)
    num = c.fit("%02d" % p["number"], f.d(250), f.SW, lines=1, ls=0.9)
    txt = c.fit(p["text"], f.d(76), f.SW, lines=3, min_size=f.d(46), case="upper", ls=1.0) if p["text"] else None
    gap = f.d(16)
    total = kick.h + gap + num.h + ((gap * 2 + txt.h) if txt else 0)
    y = f.place(p["position"], total)
    g = c.clk.stagger(3, 0.35)
    c.text("label", kick, f.L, y, anchor="top-left", align="left", color=acc, shadow=c.text_shadow(0.6),
           enter=("wipe-right", c.clk.din * 1.3))
    y += kick.h + gap
    c.text("number", num, f.L, y, anchor="top-left", align="left", shadow=c.text_shadow(),
           enter=c.prof["rise"], at=g, drift=True)
    y += num.h + gap * 2
    if txt:
        c.text("text", txt, f.L, y, anchor="top-left", align="left", shadow=c.text_shadow(), enter=c.prof["rise"],
               at=g * 2)


@_template(
    "pros_cons", "lists",
    "Two lists, the good (ticks) and the bad (crosses). Use for reviews, 'is it worth it', tool and "
    "product comparisons, decisions.",
    pros=_p("list[str]", ["Free", "Fast to learn"], "good points (up to 4)"),
    cons=_p("list[str]", ["Ads everywhere"], "bad points (up to 4)"),
    title=_p("str", "", "heading"),
    pro_label=_p("str", "PROS", "label of the good list"),
    con_label=_p("str", "CONS", "label of the bad list"),
)
def _pros_cons(c, p):
    f = c.f
    sections = [(label, items, col, icon) for label, items, col, icon in
                ((p["pro_label"], p["pros"][:4], _GOOD, "check"), (p["con_label"], p["cons"][:4], _BAD, "cross"))
                if items]           # a side with nothing to say is left out, not shown empty
    side = f.wide and len(sections) == 2
    col_w = (f.SW - f.d(60)) / 2 if side else f.SW

    def rows(k):
        I, gap = f.d(68) * k, f.d(26) * k
        title = c.fit(p["title"], f.d(76) * k, f.SW, lines=2, min_size=f.d(44) * k, case="upper",
                      ls=1.0) if p["title"] else None
        secs, heights = [], []
        for label, items, col, icon in sections:
            head = c.body(label, f.d(40) * k, col_w, lines=1, case="upper", lsp=f.d(6) * k, ls=1.0)
            hpad, hh = _pill(c, head, (22, 12))
            blks = [c.fit(it, f.d(58) * k, col_w - I - f.d(28) * k, lines=2, min_size=f.d(32) * k, ls=1.05)
                    for it in items]
            rh = [max(I, b.h) for b in blks]
            secs.append((head, hpad, hh, blks, rh, col, icon))
            heights.append(hh + gap + sum(rh) + gap * max(0, len(blks) - 1))
        body_h = max(heights) if side else sum(heights) + gap * 2.5
        needed = (title.h + gap * 2 if title else 0) + body_h
        return (I, gap, title, secs, heights), needed

    (I, gap, title, secs, heights), needed, k = _fit_stack(rows, f.SH * 0.92)
    y = f.place("center", needed)
    if title:
        c.text("title", title, f.CX, y, anchor="top", shadow=c.text_shadow(), enter=c.prof["rise"])
        y += title.h + gap * 2
    count = sum(len(sec[3]) + 1 for sec in secs)
    step = c.clk.stagger(count, 0.55, per={"high": 0.3, "medium": 0.4, "calm": 0.55}[c.energy])
    t = 0.0
    for si, (head, hpad, hh, blks, rh, col, icon) in enumerate(secs):
        x = f.L + (si * (col_w + f.d(60)) if side else 0)
        yy = y
        c.text("head", head, x, yy, anchor="top-left", align="left", color=_on(col),
               box={"color": col, "pad": hpad, "radius": _r(f.d(12))}, enter=c.prof["pop"], at=t)
        t += step
        yy += hh + gap
        for i, b in enumerate(blks):
            cy = yy + rh[i] / 2
            row = c.group((x, cy), intro="left" if c.prof["over"] > 0 else "fade", outro="fade", at=t,
                          dist=f.d(80))
            row.circle("dot", x + I / 2, cy, I, col)
            row.icon("icon", icon, x + I / 2, cy, I * 0.62, _on(col), col)
            row.text("item", b, x + I + f.d(28) * k, cy, anchor="left", align="left", shadow=c.text_shadow(0.7))
            yy += rh[i] + gap
            t += step
        if not side:
            y = yy + gap * 1.5


# =========================================================================== comparisons

def _initial(name):
    m = re.search(r"[A-Za-z0-9]", name or "")
    return m.group(0).upper() if m else "?"


@_template(
    "versus", "compare",
    "Two contenders on coloured panels with a VS badge slammed between them. Use for 'X vs Y', "
    "comparisons, debates, this-or-that hooks.",
    left=_p("str", "IPHONE", "first contender"),
    right=_p("str", "ANDROID", "second contender"),
    left_sub=_p("str", "", "line under the first name"),
    right_sub=_p("str", "", "line under the second name"),
    left_color=_p("color", "", "first panel colour ('' = accent)"),
    right_color=_p("color", "#3D7BFF", "second panel colour"),
    vs=_p("str", "VS", "badge text"),
)
def _versus(c, p):
    f, s = c.f, c.s
    side = f.H / float(f.W) < 1.2
    gap = f.d(26)
    if side:
        cw, ch = (f.SW - gap) / 2, min(f.SH * 0.72, f.d(900))
        centers = [(f.L + cw / 2, f.CY), (f.R - cw / 2, f.CY)]
        intros = ["left", "right"]
    else:
        cw, ch = f.SW, min((f.SH * 0.86 - gap) / 2, f.d(520))
        centers = [(f.CX, f.CY - (ch + gap) / 2), (f.CX, f.CY + (ch + gap) / 2)]
        intros = ["drop", "rise"]
    over = c.prof["over"] > 0
    g = c.clk.stagger(3, 0.35)
    D = f.d(176)
    texts = []      # (x0, y0, x1, y1) of every name and sub line, to keep the badge's entrance off them
    for i, (name, sub, colp) in enumerate(((p["left"], p["left_sub"], p["left_color"]),
                                           (p["right"], p["right_sub"], p["right_color"]))):
        col = c.color(colp)
        cx, cy = centers[i]
        # side by side, the VS badge covers the inner edges: keep the text clear of it
        room = cw - D * 0.5 - f.d(70) if side else cw * 0.84
        tx = cx + (D / 4 if i else -D / 4) if side else cx
        lab = c.fit(name, f.d(112), room, lines=2, min_size=f.d(44), case="upper", ls=1.0)
        sb = c.body(sub, f.d(42), room, lines=2, min_size=f.d(30)) if sub else None
        block = lab.h + ((f.d(14) + sb.h) if sb else 0)
        grp = c.group((cx, cy), intro=intros[i] if over else "fade", outro="fade", at=i * g,
                      dist=f.d(260) if over else None)
        grp.rect("panel", cx, cy, cw, ch, col, radius=f.d(40),
                 gradient=[_alpha(col, 0.95), _alpha(_mix(col, "#000000", 0.38), 0.95)], shadow=c.card_shadow())
        ink = _on(col)
        grp.text("name", lab, tx, cy - block / 2, anchor="top", color=ink)
        texts.append((tx - lab.w / 2, cy - block / 2, tx + lab.w / 2, cy - block / 2 + lab.h))
        if sb:
            grp.text("sub", sb, tx, cy - block / 2 + lab.h + f.d(14), anchor="top", color=_alpha(ink, 0.85))
            texts.append((tx - sb.w / 2, cy - block / 2 + lab.h + f.d(14), tx + sb.w / 2,
                          cy - block / 2 + lab.h + f.d(14) + sb.h))
    vcx, vcy = f.CX, f.CY
    vs = fit_text(p["vs"], s["font"], D * 0.42, D * 0.8, 1, case="upper")
    anim = {}
    enter, land = c.prof["title"], c.clk.din
    if enter == "stamp":
        # a stamp starts at 2.2x and would cover both names right as they are read: slam the badge in
        # from no bigger than the room around it allows (at most 1.35x)
        clear = min([math.hypot(max(x0 - vcx, 0, vcx - x1), max(y0 - vcy, 0, vcy - y1))
                     for x0, y0, x1, y1 in texts] or [D])
        peak = max(1.0, min(1.35, (clear - f.d(14)) / (D / 2)))
        land = c.clk.din * 0.55
        anim["scale"] = _keys([[0, peak], [land, 0.96, "in-quad"], [c.clk.din * 0.8, 1.0, "out-quad"]])
        enter = ("fade", c.clk.din * 0.3)
    wig = c.wiggle_keys(land, 10)       # layer time: the badge's own clock starts at its entrance
    if wig:
        anim["rotation"] = wig
    extra = {"animate": anim} if anim else {}
    c.circle("badge", vcx, vcy, D, s["text"], enter=enter, at=2 * g, shadow=c.card_shadow(), **extra)
    c.text("vs", vs, vcx, vcy, color=_on(s["text"]), enter=enter, at=2 * g, **dict(extra))


def _split(c, p, labels, colors, notes, handle):
    """Shared by before_after and split_screen: divider, labels per half, optional handle."""
    f, s = c.f, c.s
    vertical = p["layout"] == "vertical" or (p["layout"] == "auto" and f.H > f.W)
    g = c.clk.stagger(3, 0.35)
    line = f.d(8)
    D = f.d(92)
    if vertical:
        c.rect("divider", f.W / 2, f.H / 2, f.W, line, s["text"], enter=("wipe-right", c.clk.din * 1.4),
               shadow=c.card_shadow())
        # the second label hangs under the divider, its pill clear of the slider handle centred on it
        # (a label's y is its text top; the pill reaches 16 design px above that)
        spots = [(f.CX, f.T + f.d(16)), (f.CX, f.H / 2 + (D / 2 + f.d(24 + 16) if handle else f.d(40)))]
        half_w = f.SW
    else:
        c.rect("divider", f.W / 2, f.H / 2, line, f.H, s["text"], enter=("wipe-up", c.clk.din * 1.4),
               shadow=c.card_shadow())
        spots = [(f.W * 0.25, f.T + f.d(16)), (f.W * 0.75, f.T + f.d(16))]
        half_w = f.W / 2 - f.L * 1.5
    if handle:
        c.circle("handle", f.W / 2, f.H / 2, D, s["text"], enter=c.prof["pop"], at=g, shadow=c.card_shadow())
        for tri in ([[0.5, 0.12], [0.8, 0.42], [0.2, 0.42]], [[0.5, 0.88], [0.8, 0.58], [0.2, 0.58]]):
            pts = tri if vertical else [[y, x] for x, y in tri]     # up/down arrows, or left/right
            c.poly("handle_arrow", f.W / 2, f.H / 2, D * 0.62, D * 0.62, pts, _on(s["text"]), enter=c.prof["pop"],
                   at=g)
    for i in range(2):
        lab = c.body(labels[i], f.d(40), half_w, lines=1, case="upper", lsp=f.d(4), ls=1.0)
        pad, ph = _pill(c, lab, (28, 16))
        x, y = spots[i]
        c.text("label", lab, x, y, anchor="top", color=_on(colors[i]),
               box={"color": colors[i], "pad": pad, "radius": _r(ph / 2)}, shadow=c.card_shadow(),
               enter=c.prof["pop"], at=(i + 1) * g)
        if notes[i]:
            nb = c.body(notes[i], f.d(38), half_w, lines=2, min_size=f.d(28))
            c.text("note", nb, x, y + ph + f.d(14), anchor="top", shadow=c.text_shadow(), enter="fade",
                   at=(i + 1) * g + c.clk.gap)


@_template(
    "before_after", "compare",
    "BEFORE / AFTER labels with a divider and slider handle over a split frame (top/bottom on vertical video). "
    "Put the two clips or images in the halves; use for transformations, edits, results.",
    before=_p("str", "BEFORE", "label of the first half"),
    after=_p("str", "AFTER", "label of the second half"),
    before_note=_p("str", "", "line under the first label"),
    after_note=_p("str", "", "line under the second label"),
    layout=_p("choice", "auto", "auto (top/bottom on tall frames), vertical or horizontal",
              ["auto", "vertical", "horizontal"]),
)
def _before_after(c, p):
    _split(c, p, [p["before"], p["after"]], [_alpha("#000000", 0.72), c.s["accent"]],
           [p["before_note"], p["after_note"]], handle=True)


@_template(
    "split_screen", "compare",
    "Labels and a divider for two clips shown at once (me vs everyone, reaction + original, two takes). "
    "The planner places the two videos; this adds the labels.",
    a=_p("str", "ME", "label of the first half"),
    b=_p("str", "EVERYONE ELSE", "label of the second half"),
    a_color=_p("color", "", "first label colour ('' = accent)"),
    b_color=_p("color", "#3D7BFF", "second label colour"),
    layout=_p("choice", "auto", "auto (top/bottom on tall frames), vertical or horizontal",
              ["auto", "vertical", "horizontal"]),
)
def _split_screen(c, p):
    _split(c, p, [p["a"], p["b"]], [c.color(p["a_color"]), c.color(p["b_color"])], ["", ""], handle=False)


@_template(
    "myth_vs_fact", "compare",
    "A MYTH card, then the FACT card that corrects it (the myth dims when the fact lands). Use for "
    "busting misconceptions, 'stop believing this', correcting bad advice.",
    myth=_p("str", "You need to post every day", "the misconception"),
    fact=_p("str", "Consistency beats frequency", "the correction"),
    reveal=_p("float", 0.45, "when the fact appears, as a fraction of the window"),
    myth_label=_p("str", "MYTH", "label of the first card"),
    fact_label=_p("str", "FACT", "label of the second card"),
)
def _myth_vs_fact(c, p):
    f, s = c.f, c.s
    side = f.wide
    gap = f.d(30)
    cw = (f.SW - gap) / 2 if side else f.SW
    pad = f.d(36)
    I = f.d(76)
    cards = []
    for label, text, col, icon in ((p["myth_label"], p["myth"], _BAD, "cross"),
                                   (p["fact_label"], p["fact"], _GOOD, "check")):
        head = c.body(label, f.d(36), cw * 0.6, lines=1, case="upper", lsp=f.d(6), ls=1.0)
        hpad, hh = _pill(c, head, (22, 12))
        body = c.fit(text, f.d(64), cw - pad * 2, lines=4, min_size=f.d(38), ls=1.05)
        ch = pad + max(I, hh) + f.d(24) + body.h + pad
        cards.append((head, hpad, hh, body, ch, col, icon))
    if side:
        H = max(cd[4] for cd in cards)
        boxes = [(f.L + cw / 2, f.CY, H), (f.R - cw / 2, f.CY, H)]
    else:
        total = cards[0][4] + gap + cards[1][4]
        top = f.place("center", total)
        boxes = [(f.CX, top + cards[0][4] / 2, cards[0][4]), (f.CX, top + cards[0][4] + gap + cards[1][4] / 2,
                                                               cards[1][4])]
    t_fact = max(c.clk.din * 2, min(c.clk.dur * 0.8, c.clk.dur * _clamp01(p["reveal"])))
    over = c.prof["over"] > 0
    for i, ((head, hpad, hh, body, ch, col, icon), (cx, cy, H)) in enumerate(zip(cards, boxes)):
        at = 0.0 if i == 0 else t_fact
        # the myth dims once the fact is on screen
        grp = c.group((cx, cy), intro=("left" if i == 0 else "right") if over else "fade", outro="fade", at=at,
                      dist=f.d(200), dim=(t_fact, 0.45) if i == 0 else None)
        x0, y0 = cx - cw / 2, cy - H / 2
        grp.rect("card", cx, cy, cw, H, _alpha(s["bg2"], 0.95), radius=f.d(36), shadow=c.card_shadow(),
                 stroke={"color": _alpha(col, 0.9), "width": _r(f.d(4))})
        grp.text("label", head, x0 + pad, y0 + pad + max(I, hh) / 2, anchor="left", align="left", color="#FFFFFF",
                 box={"color": col, "pad": hpad, "radius": _r(f.d(10))})
        grp.circle("badge", x0 + cw - pad - I / 2, y0 + pad + I / 2, I, col,
                   enter=(c.prof["pop"], c.clk.din, c.clk.din * 0.8))
        grp.icon("icon", icon, x0 + cw - pad - I / 2, y0 + pad + I / 2, I * 0.6, "#FFFFFF", col,
                 enter=(c.prof["pop"], c.clk.din, c.clk.din * 0.8))
        grp.text("text", body, x0 + pad, y0 + pad + max(I, hh) + f.d(24), anchor="top-left", align="left")


# =========================================================================== social and UI mock-ups

_INK, _GREY = "#111114", "#8A8A8E"


def _avatar(grp, part, x, y, D, name, color):
    """Round avatar with the name's initial."""
    grp.circle(part, x, y, D, color, gradient=[color, _mix(color, "#000000", 0.3)])
    ini = fit_text(_initial(name), grp.c.s["font"], D * 0.46, D * 0.8, 1)
    grp.text(part + "_initial", ini, x, y, color=_on(color))


@_template(
    "comment_bubble", "social",
    "A viewer comment on screen: reply = the white 'Reply to @user's comment' sticker used to answer "
    "comments in a video, card = a dark comment row with likes. Use to open reply videos or show social proof.",
    user=_p("str", "@viewer", "commenter's handle"),
    text=_p("str", "How do you get so many views?", "the comment"),
    likes=_p("str", "", "like count shown on the card look, e.g. '2.4K'"),
    look=_p("choice", "reply", "reply sticker or comment card", ["reply", "card"]),
    position=_p("choice", "top", "vertical placement", ["top", "center", "bottom"]),
)
def _comment_bubble(c, p):
    c.f = c.f.column(1100)
    f, s, acc = c.f, c.s, c.s["accent"]
    user = p["user"] if p["user"].startswith("@") or not p["user"] else "@" + p["user"]
    if p["look"] == "reply":
        width = min(f.SW, f.d(860))
        pad, av = f.d(32), f.d(48)
        head = c.body("Reply to %s's comment" % user, f.d(34), width - pad * 2 - av - f.d(14), lines=1, ls=1.0)
        txt = c.body(p["text"], f.d(56), width - pad * 2, lines=4, min_size=f.d(32), ls=1.15)
        card_h = pad + av + f.d(16) + txt.h + pad
        tail_h = f.d(36)
        top = f.place(p["position"], card_h + tail_h, margin=0.06)
        x0 = f.CX - width / 2
        tip = (x0 + f.d(70), top + card_h + tail_h)
        grp = c.group(tip, intro="pop" if c.prof["over"] > 0 else "rise", drift=True)
        grp.rect("card", f.CX, top + card_h / 2, width, card_h, "#FFFFFF", radius=f.d(30), shadow=c.card_shadow())
        grp.poly("tail", x0 + f.d(52), top + card_h - f.d(2), f.d(60), tail_h + f.d(2),
                 [[0.0, 0.0], [1.0, 0.0], [0.3, 1.0]], "#FFFFFF", anchor="top-left")
        _avatar(grp, "avatar", x0 + pad + av / 2, top + pad + av / 2, av, user, _mix(acc, "#6E6E73", 0.4))
        grp.text("head", head, x0 + pad + av + f.d(14), top + pad + av / 2, anchor="left", align="left",
                 color=_GREY)
        grp.text("comment", txt, x0 + pad, top + pad + av + f.d(16), anchor="top-left", align="left", color=_INK)
        return
    width, pad, av = f.SW, f.d(30), f.d(84)
    side_w = f.d(90) if p["likes"] else 0
    tw = width - pad * 2 - av - f.d(22) - side_w
    name = c.body(user, f.d(34), tw, lines=1, ls=1.0)
    txt = c.body(p["text"], f.d(50), tw, lines=4, min_size=f.d(30), ls=1.18)
    card_h = pad * 2 + max(av, name.h + f.d(8) + txt.h)
    top = f.place(p["position"], card_h, margin=0.06)
    x0 = f.L
    grp = c.group((f.CX, top + card_h / 2), drift=True)
    grp.rect("card", f.CX, top + card_h / 2, width, card_h, _alpha(s["bg2"], 0.95), radius=f.d(30),
             shadow=c.card_shadow())
    _avatar(grp, "avatar", x0 + pad + av / 2, top + pad + av / 2, av, user, _mix(acc, "#6E6E73", 0.4))
    tx = x0 + pad + av + f.d(22)
    grp.text("name", name, tx, top + pad, anchor="top-left", align="left", color=_alpha(s["text"], 0.62))
    grp.text("comment", txt, tx, top + pad + name.h + f.d(8), anchor="top-left", align="left")
    if p["likes"]:
        hx = f.R - pad - side_w / 2
        grp.icon("heart", "heart", hx, top + pad + f.d(26), f.d(46), "#FF3B5C", s["bg2"],
                 enter=(c.prof["pop"], c.clk.din, c.clk.din))
        lk = c.body(p["likes"], f.d(28), side_w, lines=1, ls=1.0)
        grp.text("likes", lk, hx, top + pad + f.d(60), anchor="top", color=_alpha(s["text"], 0.62))


@_template(
    "post_card", "social",
    "A text post shown as a social-media card (avatar, name, handle, text, likes); no platform logos. "
    "Use to quote a viral post, a hot take or a screenshot-worthy statement.",
    name=_p("str", "Alex Rivera", "display name"),
    handle=_p("str", "@alexmakes", "handle"),
    text=_p("str", "Nobody cares about your first 100 videos. That is the freedom.", "post text"),
    likes=_p("str", "12.4K", "like count ('' to hide the stats row)"),
    comments=_p("str", "318", "comment count"),
    verified=_p("bool", True, "show a verified tick after the name"),
    theme=_p("choice", "light", "card colours", ["light", "dark"]),
)
def _post_card(c, p):
    f, acc = c.f, c.s["accent"]
    light = p["theme"] == "light"
    card, ink, grey = ("#FFFFFF", _INK, "#6E6E73") if light else ("#16181C", "#F5F5F7", "#8B8B92")
    width = min(f.SW, f.d(900))
    pad, av = f.d(44), f.d(96)
    x0 = f.CX - width / 2
    name = c.body(p["name"], f.d(40), width - pad * 2 - av - f.d(80), lines=1, ls=1.0)
    handle = c.body(p["handle"], f.d(34), width - pad * 2 - av - f.d(22), lines=1, ls=1.0)
    txt = c.body(p["text"], f.d(50), width - pad * 2, lines=7, min_size=f.d(32), ls=1.22)
    stats = bool(p["likes"] or p["comments"])
    row_h = f.d(50)
    card_h = pad + av + f.d(28) + txt.h + ((f.d(30) + f.d(2) + f.d(26) + row_h) if stats else 0) + pad
    top = f.place("center", card_h)
    grp = c.group((f.CX, top + card_h / 2), drift=True)
    grp.rect("card", f.CX, top + card_h / 2, width, card_h, card, radius=f.d(36), shadow=c.card_shadow())
    _avatar(grp, "avatar", x0 + pad + av / 2, top + pad + av / 2, av, p["name"], _mix(acc, "#3D7BFF", 0.5))
    nx = x0 + pad + av + f.d(22)
    ny = top + pad + av * 0.3
    grp.text("name", name, nx, ny, anchor="left", align="left", color=ink)
    if p["verified"]:
        vb = f.d(38)
        vx = nx + name.w + f.d(12) + vb / 2
        grp.circle("verified", vx, ny, vb, "#1D9BF0")
        grp.icon("verified_tick", "check", vx, ny, vb * 0.62, "#FFFFFF", "#1D9BF0")
    grp.text("handle", handle, nx, top + pad + av * 0.74, anchor="left", align="left", color=grey)
    y = top + pad + av + f.d(28)
    grp.text("text", txt, x0 + pad, y, anchor="top-left", align="left", color=ink)
    if stats:
        y += txt.h + f.d(30)
        grp.rect("divider", f.CX, y, width - pad * 2, f.d(2), _alpha(grey, 0.35), anchor="top")
        y += f.d(2) + f.d(26) + row_h / 2
        x = x0 + pad
        for icon, val, col in (("comment", p["comments"], grey), ("heart", p["likes"], "#F91880")):
            if not val:
                continue
            grp.icon("stat_" + icon, icon, x + f.d(21), y, f.d(42), col, card,
                     enter=(c.prof["pop"], c.clk.din, c.clk.din) if icon == "heart" else None)
            vb_ = c.body(val, f.d(32), f.d(220), lines=1, ls=1.0)
            grp.text("stat_" + icon + "_n", vb_, x + f.d(56), y, anchor="left", align="left", color=grey)
            x += f.d(56) + vb_.w + f.d(56)


@_template(
    "notification_pop", "social",
    "A phone notification banner dropping in at the top ('Mom: did you quit your job??'). Use for story "
    "hooks, plot twists, sales pings ('You made a sale'), reminders.",
    app=_p("str", "MESSAGES", "app name line (generic; avoid real brand names)"),
    title=_p("str", "Mom", "sender or title"),
    text=_p("str", "Did you really quit your job??", "message preview"),
    time=_p("str", "now", "time label"),
    icon=_p("choice", "comment", "app icon", _ICON_NAMES),
    color=_p("color", "#34C759", "app icon tile colour"),
)
def _notification_pop(c, p):
    c.f = c.f.column(1100)
    f = c.f
    col = c.color(p["color"])
    pad, tile = f.d(34), f.d(66)
    width = f.SW
    app = c.body(p["app"], f.d(30), width * 0.6, lines=1, case="upper", lsp=f.d(2), ls=1.0)
    tm = c.body(p["time"], f.d(30), width * 0.25, lines=1, ls=1.0)
    title = c.body(p["title"], f.d(46), width - pad * 2, lines=1, ls=1.0)
    txt = c.body(p["text"], f.d(42), width - pad * 2, lines=2, min_size=f.d(30), ls=1.18)
    card_h = pad + tile + f.d(16) + title.h + f.d(4) + txt.h + pad
    top = f.T + f.d(12)
    x0 = f.L
    over = c.prof["over"] > 0
    grp = c.group((f.CX, top), intro="drop" if over else "fade", outro="lift" if over else "fade",
                  dist=card_h + f.d(60))
    grp.rect("card", f.CX, top + card_h / 2, width, card_h, _alpha("#F2F2F7", 0.97), radius=f.d(44),
             shadow=c.card_shadow())
    ty = top + pad + tile / 2
    grp.rect("tile", x0 + pad + tile / 2, ty, tile, tile, col, radius=f.d(15))
    grp.icon("icon", p["icon"], x0 + pad + tile / 2, ty, tile * 0.62, _on(col), col)
    grp.text("app", app, x0 + pad + tile + f.d(18), ty, anchor="left", align="left", color="#6E6E73")
    grp.text("time", tm, f.R - pad, ty, anchor="right", align="right", color="#6E6E73")
    y = top + pad + tile + f.d(16)
    grp.text("title", title, x0 + pad, y, anchor="top-left", align="left", color=_INK)
    grp.text("text", txt, x0 + pad, y + title.h + f.d(4), anchor="top-left", align="left", color="#3A3A3C")


@_template(
    "text_message", "social",
    "A chat conversation that plays out bubble by bubble, with typing dots before replies. Prefix lines "
    "with 'me:' (right, blue) or 'them:' (left, grey). Use for storytime, skits, 'what my client sent me'.",
    messages=_p("list[str]", ["them: you up?", "me: editing a reel", "them: at 3am??", "me: it's going viral"],
                "lines in order, each starting 'me:' or 'them:'"),
    name=_p("str", "", "contact name shown above the chat"),
    theme=_p("choice", "dark", "bubble colours for the other person", ["dark", "light"]),
)
def _text_message(c, p):
    c.f = c.f.column(1100)
    f, s = c.f, c.s
    msgs = []
    for line in p["messages"][:8]:
        m = re.match(r"^\s*(me|them|you|i)\s*:\s*(.*)$", line, re.I)
        mine = bool(m) and m.group(1).lower() in ("me", "i")
        body = (m.group(2) if m else line).strip()
        if body:
            msgs.append((mine, body))
    them_col, them_ink = ("#2C2C2E", "#FFFFFF") if p["theme"] == "dark" else ("#E9E9EB", _INK)
    n = len(msgs)

    def rows(k):
        pad = [_r(f.d(34) * k), _r(f.d(22) * k)]
        blks = [c.body(t, f.d(54) * k, f.SW * 0.74 - pad[0] * 2, lines=6, min_size=f.d(30) * k, ls=1.2)
                for _, t in msgs]
        hs = [b.h + pad[1] * 2 for b in blks]
        gaps = [0] + [f.d(14) * k if msgs[i][0] == msgs[i - 1][0] else f.d(30) * k for i in range(1, n)]
        head = (f.d(90) + f.d(16) + f.d(32) * 0.8 + f.d(30)) * k if p["name"] else 0
        return (pad, blks, hs, gaps, head), head + sum(hs) + sum(gaps)

    (pad, blks, hs, gaps, head), needed, k = _fit_stack(rows, f.SH * 0.9)
    y = f.place("top", needed, margin=0.03)
    if p["name"]:
        av = f.d(90) * k
        grp = c.group((f.CX, y + av / 2))
        _avatar(grp, "contact", f.CX, y + av / 2, av, p["name"], "#8E8E93")
        nm = c.body(p["name"], f.d(32) * k, f.SW * 0.6, lines=1, ls=1.0)
        grp.text("contact_name", nm, f.CX, y + av + f.d(16) * k, anchor="top", color=_alpha(s["text"], 0.8))
        y += head
    step = c.clk.stagger(n, 0.75, per={"high": 0.75, "medium": 0.9, "calm": 1.1}[c.energy])
    t = c.clk.din if p["name"] else 0.0
    radius = _r(f.d(40) * k)
    for i, ((mine, _), b) in enumerate(zip(msgs, blks)):
        y += gaps[i]
        typing = (not mine) and step >= 0.45 and i > 0
        at = t + i * step
        if typing:
            # typing dots in a small grey bubble until the message arrives
            ty0 = at - step * 0.55
            bw, bh = f.d(132) * k, f.d(84) * k
            until = c.clk.dur - at
            c.rect("typing", f.L, y, bw, bh, them_col, radius=bh / 2, anchor="top-left", enter="fade", exit=None,
                   at=ty0, until=until, min_life=0.1)
            for j in range(3):
                bob = _loop_keys(step, 0.6, [(0, 0, None), (0.25, -f.d(9) * k, "out-quad"),
                                             (0.5, 0, "in-quad")], start=j * 0.12)
                c.circle("dot", f.L + f.d(36) * k + j * f.d(30) * k, y + bh / 2, f.d(18) * k, _alpha(them_ink, 0.6),
                         enter=None, exit=None, at=ty0, until=until, min_life=0.1,
                         **({"animate": {"y": bob}} if bob else {}))
        col, ink = ("#0A84FF", "#FFFFFF") if mine else (them_col, them_ink)
        c.text("bubble", b, f.R if mine else f.L, y, anchor="top-right" if mine else "top-left", align="left",
               font=s["font_secondary"], color=ink, box={"color": col, "pad": list(pad), "radius": radius},
               enter=(c.prof["pop"], min(0.3, c.clk.din)), at=at, exit="fade")
        y += hs[i]


@_template(
    "search_bar", "social",
    "A search box typing a query, then dropping suggestions. Use for 'everyone is searching for X', "
    "relatable 'how to...' hooks, research beats. Generic design, no search-engine branding.",
    query=_p("str", "how to go viral on reels", "the text that gets typed"),
    suggestions=_p("list[str]", [], "suggestion rows shown after typing (up to 4)"),
    position=_p("choice", "top", "vertical placement", ["top", "center"]),
)
def _search_bar(c, p):
    c.f = c.f.column(1100)
    f = c.f
    bar_h = f.d(140)
    width = f.SW
    q = c.body(p["query"], f.d(52), width - f.d(180), lines=1, min_size=f.d(28), ls=1.0)
    sugg = [c.body(x, f.d(44), width - f.d(150), lines=1, min_size=f.d(28), ls=1.0) for x in p["suggestions"][:4]]
    row_h = f.d(100)
    total = bar_h + ((f.d(16) + row_h * len(sugg) + f.d(20)) if sugg else 0)
    top = f.place(p["position"], total, margin=0.12)
    cy = top + bar_h / 2
    grp = c.group((f.CX, cy), intro="pop" if c.prof["over"] > 0 else "fade", drift=False)
    grp.rect("bar", f.CX, cy, width, bar_h, "#FFFFFF", radius=bar_h / 2, shadow=c.card_shadow())
    grp.icon("icon", "search", f.L + f.d(68), cy, f.d(56), "#5F6368", "#FFFFFF")
    tx = f.L + f.d(122)
    type_at = c.clk.din + 0.1
    type_d = max(0.3, min(c.clk.dur * 0.42, 0.055 * len(p["query"]) + 0.15))
    grp.text("query", q, tx, cy, anchor="left", align="left", color="#202124",
             enter=("typewriter", type_d, type_at))
    cur_w, cur_h = max(2.0, f.d(4)), f.d(58)
    end_x = tx + q.w + f.d(6)

    def blink(t_on, t_off):
        """Caret opacity: off, then blinking (0.45 s on / 0.45 s off) from t_on until t_off."""
        keys, t, v = [[0, 0], [t_on, 1, "step"]], t_on, 1
        while t + 0.45 < min(t_off, c.clk.dur):
            t, v = t + 0.45, 1 - v
            keys.append([t, v, "step"])
        if t_off < c.clk.dur and v:
            keys.append([t_off, 0, "step"])
        return _keys(keys)

    # the caret is part of the bar (it pops, scales and leaves with it): before the typing at the start
    # of the box, after it at the end of the query
    grp.rect("cursor", tx, cy, cur_w, cur_h, "#1A73E8", animate={"opacity": blink(type_at * 0.5, type_at)})
    grp.rect("cursor_end", end_x, cy, cur_w, cur_h, "#1A73E8",
             animate={"opacity": blink(type_at + type_d, c.clk.dur)})
    if sugg:
        card_top = top + bar_h + f.d(16)
        card_h = row_h * len(sugg) + f.d(20)
        at = type_at + type_d + 0.1
        drop = c.group((f.CX, card_top), intro="drop" if c.prof["over"] > 0 else "fade", at=at, dist=f.d(30))
        drop.rect("list", f.CX, card_top + card_h / 2, width, card_h, "#FFFFFF", radius=f.d(32),
                  shadow=c.card_shadow())
        for i, b in enumerate(sugg):
            ry = card_top + f.d(10) + row_h * (i + 0.5)
            drop.icon("row_icon", "search", f.L + f.d(56), ry, f.d(36), "#9AA0A6", "#FFFFFF")
            drop.text("row", b, f.L + f.d(100), ry, anchor="left", align="left", color="#202124")


@_template(
    "poll", "social",
    "A poll card whose result bars fill in with counting percentages. Use for 'which one would you pick', "
    "audience questions, revealing what most people get wrong.",
    question=_p("str", "Which hook works better?", "the question"),
    options=_p("list[str]", ["A question", "A bold claim"], "2-4 answers"),
    results=_p("list[float]", [38, 62], "share of votes per option (normalised to 100)"),
    show_results=_p("bool", True, "fill the bars with the results halfway through"),
)
def _poll(c, p):
    f, acc = c.f, c.s["accent"]
    opts = p["options"][:4]
    n = len(opts)
    res = [max(0.0, v) for v in (p["results"] + [0.0] * n)[:n]]
    tot = sum(res) or 1.0
    pct = [round(100.0 * v / tot) for v in res]
    best = pct.index(max(pct)) if pct else -1
    width = min(f.SW, f.d(840))
    pad, row_h, gap = f.d(38), f.d(116), f.d(18)
    q = c.fit(p["question"], f.d(62), width - pad * 2, lines=3, min_size=f.d(36), ls=1.05)
    card_h = pad + q.h + f.d(30) + n * row_h + (n - 1) * gap + pad
    top = f.place("center", card_h)
    x0 = f.CX - width / 2
    reveal = max(c.clk.din * 2, c.clk.dur * 0.42)
    grow = max(0.3, min(0.8, c.clk.dur - reveal - c.clk.dout - 0.2))
    grp = c.group((f.CX, top + card_h / 2), intro="pop" if c.prof["over"] > 0 else "rise")
    grp.rect("card", f.CX, top + card_h / 2, width, card_h, "#FFFFFF", radius=f.d(40), shadow=c.card_shadow())
    grp.text("question", q, f.CX, top + pad, anchor="top", color=_INK)
    y = top + pad + q.h + f.d(30)
    rw = width - pad * 2
    for i, opt in enumerate(opts):
        cy = y + row_h / 2
        grp.rect("row", f.CX, cy, rw, row_h, "#EFEFF4", radius=f.d(28))
        win = i == best
        # fills, counters and labels belong to the card (they scale and leave with it); their own
        # entrances are delayed to the reveal
        if p["show_results"] and reveal + grow < c.clk.dur:
            fill = max(row_h, rw * pct[i] / 100.0)
            grp.rect("fill", x0 + pad, cy, fill, row_h, acc if win else "#D1D1D6", radius=f.d(28), anchor="left",
                     enter=("wipe-right", grow, reveal))
            num = c.fit("100%", f.d(50), f.d(200), lines=1)
            grp.counter("pct", num, x0 + pad + rw - f.d(28), cy, 0, pct[i], 0, "", "%", "", grow, "out-cubic",
                        anchor="right", color=_INK, enter=("fade", c.clk.din, reveal))
        lab = c.body(opt, f.d(48), rw * 0.66, lines=1, min_size=f.d(30), ls=1.0)
        grp.text("option", lab, x0 + pad + f.d(30), cy, anchor="left", align="left", color=_INK,
                 enter=("fade", c.clk.din, c.clk.din * 0.6))
        y += row_h + gap


@_template(
    "like_burst", "social",
    "Hearts (or any icon) floating up from a point like a live-stream reaction. Use on a punchline, a "
    "win, a 'you'll love this' moment; layer it over other templates.",
    icon=_p("choice", "heart", "particle icon", _ICON_NAMES),
    count=_p("int", 9, "number of particles (3-20)"),
    color=_p("color", "#FF3B5C", "particle colour"),
    x=_p("float", 0.78, "origin x as a fraction of the frame width"),
    y=_p("float", 0.7, "origin y as a fraction of the frame height"),
    size=_p("float", 90, "particle size in design px"),
)
def _like_burst(c, p):
    f = c.f
    col = c.color(p["color"])
    n = max(3, min(20, p["count"]))
    ox = min(f.R, max(f.L, f.W * _clamp01(p["x"])))
    oy = min(f.B, max(f.T + f.SH * 0.35, f.H * _clamp01(p["y"])))
    # seeded from the params: the same call always gives the same burst, on every machine
    rng = random.Random(zlib.crc32(("%s|%s|%d|%.3f|%.3f" % (p["icon"], col, n, p["x"], p["y"])).encode("utf-8")))
    spread = min(c.clk.dur * 0.6, 1.4)
    for i in range(n):
        r_size, r_rise, r_sway, r_tilt, r_drift = (rng.random() for _ in range(5))
        at = spread * i / n
        life = min(1.5, c.clk.dur - at)
        if life < 0.4:
            break
        size = f.d(p["size"]) * (0.7 + 0.55 * r_size)
        rise = min(oy - f.T, f.d(620) * (0.75 + 0.5 * r_rise))
        sway = f.d(70) * (0.5 + r_sway)                       # wobble amplitude
        drift = f.d(260) * (r_drift * 2 - 1)                  # where it ends up sideways: the burst fans out
        drift = min(f.R - sway, max(f.L + sway, ox + drift)) - ox   # but never off the frame
        side = 1 if i % 2 else -1
        tilt = 18 * (r_tilt * 2 - 1)
        anim = {"y": _keys([[0, 0], [life, -rise, "out-cubic"]]),
                "x": _keys([[0, 0], [life * 0.35, drift * 0.35 + side * sway, "in-out-quad"],
                            [life * 0.7, drift * 0.7 - side * sway * 0.6, "in-out-quad"],
                            [life, drift, "in-out-quad"]]),
                "scale": _keys([[0, 0.3], [0.22, 1.08 if c.prof["over"] > 0 else 1.0,
                                            "out-back" if c.prof["over"] > 0 else "out-cubic"], [life, 0.8, "linear"]]),
                "opacity": _keys([[0, 0], [0.08, 1], [life * 0.65, 1], [life, 0, "in-quad"]]),
                "rotation": _keys([[0, -tilt], [life, tilt, "in-out-quad"]])}
        tint = col if i % 3 else _mix(col, "#FFFFFF", 0.3)
        c.icon("particle%d_" % (i + 1), p["icon"], ox, oy, size, tint, _mix(tint, "#000000", 0.3), animate=anim, enter=None,
               exit=None, at=at, until=c.clk.dur - at - life, min_life=0.1)


# =========================================================================== pointers and highlights

def _rigid(scale_keys, ax, ay, px, py):
    """Animate dict that scales a layer anchored at (ax, ay) about the pivot (px, py), in step with others."""
    return {"scale": scale_keys,
            "x": [[k[0], round((k[1] - 1) * (ax - px), 2)] + k[2:] for k in scale_keys],
            "y": [[k[0], round((k[1] - 1) * (ay - py), 2)] + k[2:] for k in scale_keys]}


def _loop_keys(life, period, values, start=0.0):
    """Keys repeating `values` [(fraction of period, value, ease)] every `period` s over a life."""
    keys, t = [], start
    while t < life:
        for frac, v, e in values:
            if t + frac * period <= life:
                keys.append([t + frac * period, v, e])
        t += period
    return _keys(keys) if len(keys) > 1 else None


@_template(
    "highlight_box", "pointers",
    "A box or circle that locks onto something in the footage (a number on screen, a product, a face), "
    "with an optional label. x/y/w/h are fractions of the frame.",
    x=_p("float", 0.5, "centre x of the target, fraction of the frame width"),
    y=_p("float", 0.45, "centre y of the target, fraction of the frame height"),
    w=_p("float", 0.5, "target width, fraction of the frame width"),
    h=_p("float", 0.18, "target height, fraction of the frame height"),
    shape=_p("choice", "rect", "rect (box outline), corners (viewfinder brackets) or circle (drawn on like a pen)",
             ["rect", "corners", "circle"]),
    label=_p("str", "", "short label next to the box"),
    color=_p("color", "", "line colour ('' = accent)"),
    thickness=_p("float", 10, "line width in design px"),
)
def _highlight_box(c, p):
    f = c.f
    c.fixed = False
    col = c.color(p["color"])
    th = max(2.0, min(f.d(40), f.d(p["thickness"])))
    w = min(f.W - th * 2, max(f.d(60), f.W * _clamp01(p["w"])))
    h = min(f.H - th * 2, max(f.d(60), f.H * _clamp01(p["h"])))
    # the whole box stays on the frame even when the target hugs an edge
    cx = min(f.W - w / 2 - th, max(w / 2 + th, f.W * _clamp01(p["x"])))
    cy = min(f.H - h / 2 - th, max(h / 2 + th, f.H * _clamp01(p["y"])))
    life = c.clk.dur
    land = c.clk.din
    pulse = c.pulse_keys(land + 0.2, life - c.clk.dout, period=1.0, amp=c.prof["pulse"] * 0.5)
    if p["shape"] == "circle":
        D = max(w, h) * 1.08
        anim = {"scale": pulse} if pulse else {}
        c.ring("ring", cx, cy, D, col, th, draw={"from": 0.0, "to": 1.0, "ease": "in-out-cubic"},
               enter=("none", land * 1.6), shadow=c.text_shadow(0.7), **({"animate": anim} if anim else {}))
        box_top, box_bottom = cy - D / 2, cy + D / 2
    else:
        # four sides (or four corner brackets) locking on together: the box shrinks onto the target
        keys = [[0, 1.3], [land, 1.0, "out-back" if c.prof["over"] > 0 else "out-cubic"]]
        if pulse:
            keys += [k for k in pulse if k[0] > land]
        keys = _keys(keys)
        if p["shape"] == "corners":
            arm = min(w, h) * 0.32
            t = th / arm
            ell = [[0, 0], [1, 0], [1, t], [t, t], [t, 1], [0, 1]]
            for sx, sy in ((-1, -1), (1, -1), (1, 1), (-1, 1)):
                pts = [[x if sx < 0 else 1 - x, y if sy < 0 else 1 - y] for x, y in ell]
                ax, ay = cx + sx * (w / 2 - arm / 2), cy + sy * (h / 2 - arm / 2)
                c.poly("corner", ax, ay, arm, arm, pts, col, enter=("fade", land * 0.5), shadow=c.text_shadow(0.7),
                       animate=_rigid(keys, ax, ay, cx, cy))
        else:
            for part, ax, ay, sw_, sh_ in (("top", cx, cy - h / 2, w + th, th), ("bottom", cx, cy + h / 2, w + th, th),
                                          ("left", cx - w / 2, cy, th, h + th), ("right", cx + w / 2, cy, th, h + th)):
                c.rect("side_" + part, ax, ay, sw_, sh_, col, radius=th / 2, enter=("fade", land * 0.5),
                       shadow=c.text_shadow(0.7), animate=_rigid(keys, ax, ay, cx, cy))
        box_top, box_bottom = cy - h / 2, cy + h / 2
    if p["label"]:
        lab = c.body(p["label"], f.d(40), f.SW * 0.8, lines=1, case="upper", lsp=f.d(2), ls=1.0)
        pad, ph = _pill(c, lab, (24, 14))
        half = lab.w / 2 + pad[0]
        lx = f.clamp_x(cx, half)
        if box_top - f.d(18) - ph >= f.T:
            ly, anchor = box_top - f.d(18), "bottom"
        else:
            ly, anchor = min(box_bottom + f.d(18), f.B - ph), "top"
        c.text("label", lab, lx, ly, anchor=anchor, color=_on(col),
               box={"color": col, "pad": pad, "radius": _r(ph / 2)}, enter=c.prof["pop"], at=land,
               shadow=c.card_shadow())


@_template(
    "arrow", "pointers",
    "A bold arrow that points at something in the footage and bobs toward it, with an optional label at its "
    "tail. x/y is the target as fractions of the frame.",
    x=_p("float", 0.5, "target x, fraction of the frame width"),
    y=_p("float", 0.5, "target y, fraction of the frame height"),
    direction=_p("choice", "auto", "side the arrow comes from",
                 ["auto", "left", "right", "top", "bottom", "top-left", "top-right", "bottom-left", "bottom-right"]),
    label=_p("str", "", "short label at the arrow's tail"),
    curve=_p("bool", True, "a hand-drawn curve instead of a straight shaft"),
    length=_p("float", 340, "arrow length in design px"),
    color=_p("color", "", "arrow colour ('' = accent)"),
)
def _arrow(c, p):
    f = c.f
    c.fixed = False
    col = c.color(p["color"])
    tx, ty = f.W * _clamp01(p["x"]), f.H * _clamp01(p["y"])
    d = p["direction"]
    if d == "auto":
        d = ("bottom" if ty < f.CY else "top") + "-" + ("left" if tx >= f.CX else "right")
    vx = (1 if "left" in d else -1 if "right" in d else 0)
    vy = (1 if "top" in d else -1 if "bottom" in d else 0)
    ln = math.hypot(vx, vy) or 1.0
    ux, uy = vx / ln, vy / ln                       # unit vector the arrow points along
    L = f.d(max(80.0, min(900.0, p["length"])))
    # path in a square 0..1 box from tail to head, bent sideways when curved
    a, b = [0.5 - 0.42 * ux, 0.5 - 0.42 * uy], [0.5 + 0.42 * ux, 0.5 + 0.42 * uy]
    bend = 0.2 if p["curve"] else 0.0
    ctrl = [0.5 - uy * bend, 0.5 + ux * bend]
    path = _qbez(a, ctrl, b, 16)
    pts = _arrow_path(path, 0.1, 0.25, 0.34)
    gap = f.d(14)
    cx = tx - ux * (0.42 * L + gap)
    cy = ty - uy * (0.42 * L + gap)
    cx = min(f.W - L * 0.1, max(L * 0.1, cx))
    cy = min(f.H - L * 0.1, max(L * 0.1, cy))
    life = c.clk.dur
    bob = f.d(16) * (1 if c.prof["over"] > 0 else 0.5)
    anim = {}
    if c.prof["pulse"] > 0 or c.energy == "medium":
        for axis, u in (("x", ux), ("y", uy)):
            if abs(u) > 1e-6:
                k = _loop_keys(life, 0.7, [(0, 0, "in-out-quad"), (0.5, bob * u, "in-out-quad"),
                                           (1.0, 0, "in-out-quad")], start=c.clk.din)
                if k:
                    anim[axis] = k
    c.poly("arrow", cx, cy, L, L, [[round(x, 4), round(y, 4)] for x, y in pts], col,
           stroke={"color": _alpha("#000000", 0.55), "width": _r(f.d(3))}, enter=c.prof["pop"],
           shadow=c.text_shadow(0.8), **({"animate": anim} if anim else {}))
    if p["label"]:
        lab = c.body(p["label"], f.d(42), f.SW * 0.7, lines=2, case="upper", lsp=f.d(1), ls=1.05)
        pad, ph = _pill(c, lab, (24, 14))
        px_, py_ = cx - ux * (0.45 * L), cy - uy * (0.45 * L)
        if abs(uy) > 0.3:
            anchor = "top" if uy < 0 else "bottom"
            py_ += f.d(10) * (1 if uy < 0 else -1)
        else:
            anchor = "right" if ux > 0 else "left"
            px_ -= f.d(10) * (1 if ux > 0 else -1)
        half = lab.w / 2 + pad[0]
        if anchor in ("top", "bottom"):
            px_ = f.clamp_x(px_, half)
            py_ = min(f.B - ph, py_) if anchor == "top" else max(f.T + ph, py_)
        else:
            px_ = max(f.L + 2 * half, px_) if anchor == "right" else min(f.R - 2 * half, px_)
            py_ = f.clamp_y(py_, ph / 2)
        c.text("label", lab, px_, py_, anchor=anchor, color=_on(col),
               box={"color": col, "pad": pad, "radius": _r(f.d(14))}, enter=c.prof["pop"], at=c.clk.gap * 2,
               shadow=c.card_shadow())


@_template(
    "callout", "pointers",
    "A pulsing dot on a point in the footage with a leader line to a label, like an annotated diagram. "
    "Use to name parts of a product, a setting on screen, a detail in a shot.",
    x=_p("float", 0.62, "point x, fraction of the frame width"),
    y=_p("float", 0.5, "point y, fraction of the frame height"),
    text=_p("str", "This setting", "label text"),
    side=_p("choice", "auto", "which side the label goes", ["auto", "left", "right"]),
    color=_p("color", "", "dot and line colour ('' = accent)"),
)
def _callout(c, p):
    f, s = c.f, c.s
    c.fixed = False
    col = c.color(p["color"])
    px_, py_ = f.W * _clamp01(p["x"]), f.H * _clamp01(p["y"])
    side = p["side"] if p["side"] != "auto" else ("left" if px_ > f.CX else "right")
    sx = 1 if side == "right" else -1
    sy = -1 if py_ > f.T + f.SH * 0.3 else 1          # elbow upward unless the point is near the top
    lab = c.body(p["text"], f.d(48), f.SW * 0.55, lines=2, min_size=f.d(30), ls=1.1)
    pad, ph = _pill(c, lab, (26, 16))
    dx, dy = f.d(110), f.d(110)
    run = f.d(70)
    elbow = (px_ + sx * dx, py_ + sy * dy)
    end = (elbow[0] + sx * run, elbow[1])
    # keep the label inside the safe zone by shortening the run when needed
    lw = lab.w + 2 * pad[0]
    if sx > 0 and end[0] + lw > f.R:
        end = (max(elbow[0], f.R - lw), end[1])
    if sx < 0 and end[0] - lw < f.L:
        end = (min(elbow[0], f.L + lw), end[1])
    end = (end[0], f.clamp_y(end[1], ph / 2))
    elbow = (elbow[0], end[1])
    pts = [(px_, py_), elbow, end]
    x0, x1 = min(q[0] for q in pts), max(q[0] for q in pts)
    y0, y1 = min(q[1] for q in pts), max(q[1] for q in pts)
    th = f.d(6)
    bw, bh = max(x1 - x0, th) + th * 2, max(y1 - y0, th) + th * 2
    # stroke the path in px space, then normalise to the box
    outline = _stroke_path([[q[0] - x0 + th, q[1] - y0 + th] for q in pts], th)
    poly = [[round(min(1, max(0, x / bw)), 4), round(min(1, max(0, y / bh)), 4)] for x, y in outline]
    life = c.clk.dur
    dot = f.d(30)
    c.circle("dot", px_, py_, dot, col, enter=c.prof["pop"], shadow=c.text_shadow(0.6))
    ring_keys = _loop_keys(life, 1.1, [(0, 1.0, None), (0.8, 2.4, "out-cubic"), (1.0, 2.4, None)], start=0.1)
    ring_op = _loop_keys(life, 1.1, [(0, 0.9, None), (0.8, 0.0, "out-quad"), (1.0, 0.0, None)], start=0.1)
    if ring_keys and c.prof["pulse"] > 0:
        c.ring("pulse", px_, py_, dot, col, f.d(4), animate={"scale": ring_keys, "opacity": ring_op}, enter=None)
    c.poly("line", (x0 + x1) / 2, (y0 + y1) / 2, bw, bh, poly, col, enter=("wipe-right" if sx > 0 else "wipe-left",
                                                                          c.clk.din * 1.3), at=c.clk.gap,
           shadow=c.text_shadow(0.6))
    c.text("label", lab, end[0], end[1], anchor="left" if sx > 0 else "right", align="left" if sx > 0 else "right",
           color=_on(s["text"]), box={"color": s["text"], "pad": pad, "radius": _r(f.d(16))},
           enter=c.prof["pop"], at=c.clk.gap + c.clk.din * 0.9, shadow=c.card_shadow())


@_template(
    "icon_badge", "pointers",
    "A round badge with an icon (heart, star, check, bell, flame, lightning, eye...) and an optional label. "
    "Use to tag a beat with an emotion or topic, or as a sticker next to a point.",
    icon=_p("choice", "heart", "which icon", _ICON_NAMES),
    label=_p("str", "", "short label under the badge"),
    color=_p("color", "", "badge colour ('' = accent)"),
    x=_p("float", 0.5, "centre x as a fraction of the safe zone width"),
    y=_p("float", 0.35, "centre y as a fraction of the safe zone height"),
    size=_p("float", 220, "badge diameter in design px"),
)
def _icon_badge(c, p):
    f, s = c.f, c.s
    col = c.color(p["color"])
    D = f.d(max(60.0, min(600.0, p["size"])))
    lab = c.body(p["label"], f.d(42), f.SW * 0.8, lines=1, case="upper", lsp=f.d(3), ls=1.0) if p["label"] else None
    pad, ph = _pill(c, lab, (24, 14)) if lab else ([0, 0], 0)
    cx = f.clamp_x(f.L + f.SW * _clamp01(p["x"]), max(D / 2, (lab.w / 2 + pad[0]) if lab else 0))
    total = D + ((f.d(26) + ph) if lab else 0)
    cy = f.clamp_y(f.T + f.SH * _clamp01(p["y"]), D / 2) if not lab else \
        min(max(f.T + D / 2, f.T + f.SH * _clamp01(p["y"])), f.B - total + D / 2)
    over = c.prof["over"]
    enter = "bounce" if over > 0.6 else c.prof["pop"]
    life = c.clk.dur
    halo = None
    if over > 0:
        halo = {"scale": _keys([[0.15, 1.0], [0.75, 1.55, "out-cubic"]]),
                "opacity": _keys([[0, 0], [0.15, 0.6], [0.75, 0, "out-quad"]])}
        c.ring("halo", cx, cy, D, col, f.d(8), animate=halo, enter=None, exit=None,
               until=max(0.0, life - 0.8))
    wig = c.wiggle_keys(c.clk.din + 0.05, 12)
    float_y = _loop_keys(life, 1.6, [(0, 0, "in-out-quad"), (0.5, -f.d(10), "in-out-quad"), (1.0, 0, "in-out-quad")],
                         start=c.clk.din) if over > 0 else None
    anim = {}
    if float_y:
        anim["y"] = float_y
    c.circle("badge", cx, cy, D, col, gradient=[col, _mix(col, "#000000", 0.18)], enter=enter,
             shadow=c.card_shadow(), **({"animate": dict(anim)} if anim else {}))
    icon_anim = dict(anim)
    if wig:
        icon_anim["rotation"] = wig
    c.icon("icon", p["icon"], cx, cy, D * 0.56, _on(col), col, enter=enter,
           **({"animate": icon_anim} if icon_anim else {}))
    if lab:
        c.text("label", lab, cx, cy + D / 2 + f.d(26), anchor="top", color=_on(s["bg2"]),
               box={"color": _alpha(s["bg2"], 0.94), "pad": pad, "radius": _r(ph / 2)}, enter=c.prof["rise"],
               at=c.clk.gap * 2, shadow=c.card_shadow())


# =========================================================================== calls to action

@_template(
    "cta", "cta",
    "The closing ask: an icon badge and a pill button ('FOLLOW FOR PART 2', 'COMMENT \"GUIDE\"'). Use on "
    "the last beat; keep the text to 2-5 words.",
    text=_p("str", "FOLLOW FOR PART 2", "the ask"),
    sub=_p("str", "", "small line under the button"),
    icon=_p("choice", "plus", "badge icon ('none' hides the badge)", _ICON_NAMES + ["none"]),
    position=_p("choice", "center", "vertical placement", ["top", "center", "bottom"]),
)
def _cta(c, p):
    f, s, acc = c.f, c.s, c.s["accent"]
    D = f.d(150) if p["icon"] != "none" else 0
    btn = c.fit(p["text"], f.d(64), f.SW - f.d(100), lines=2, min_size=f.d(40), case="upper", ls=1.05)
    pad, bh = _pill(c, btn, (48, 30))
    sub = c.body(p["sub"], f.d(40), f.SW * 0.85, lines=2) if p["sub"] else None
    gap = f.d(30)
    total = (D + gap if D else 0) + bh + ((gap + sub.h) if sub else 0)
    y = f.place(p["position"], total)
    life = c.clk.dur
    over = c.prof["over"]
    if D:
        cy = y + D / 2
        enter = "bounce" if over > 0.6 else c.prof["pop"]
        wig = c.wiggle_keys(c.clk.din + 0.05, 14)
        c.circle("badge", f.CX, cy, D, acc, enter=enter, shadow=c.card_shadow())
        c.icon("icon", p["icon"], f.CX, cy, D * 0.54, _on(acc), acc, enter=enter,
               **({"animate": {"rotation": wig}} if wig else {}))
        y += D + gap
    at = c.clk.gap * 1.5
    pulse = c.pulse_keys(c.clk.din + 0.2, life - at - c.clk.dout, period=0.9)
    c.text("button", btn, f.CX, y, anchor="top", color=_on(s["text"]),
           box={"color": s["text"], "pad": pad, "radius": _r(min(bh / 2, f.d(60)))}, shadow=c.card_shadow(),
           enter=c.prof["pop"], at=at, **({"animate": {"scale": pulse}} if pulse else {}))
    if sub:
        c.text("sub", sub, f.CX, y + bh + gap, anchor="top", color=_alpha(s["text"], 0.85), shadow=c.text_shadow(0.8),
               enter="fade", at=at + c.clk.gap)


@_template(
    "subscribe", "cta",
    "The subscribe / follow button animation: a cursor clicks it, it flips to 'SUBSCRIBED' and a bell pops "
    "and rings. Use near the end or after the hook pays off.",
    text=_p("str", "SUBSCRIBE", "button text before the click"),
    done=_p("str", "SUBSCRIBED", "button text after the click"),
    handle=_p("str", "", "channel name above the button"),
    color=_p("color", "#FF0033", "button colour before the click"),
    bell=_p("bool", True, "bell icon after the click"),
)
def _subscribe(c, p):
    f = c.f
    col = c.color(p["color"])
    life = c.clk.dur
    a = c.body(p["text"], f.d(66), f.SW * 0.7, lines=1, case="upper", lsp=f.d(1), ls=1.0)
    b = c.body(p["done"], f.d(66), f.SW * 0.6, lines=1, case="upper", lsp=f.d(1), ls=1.0)
    pad, bh = _pill(c, a, (60, 36))
    cy = f.T + f.SH * 0.6
    click = max(c.clk.din + 0.3, min(life * 0.5, life - 0.9))
    swap = click + 0.06
    over = c.prof["over"]
    if p["handle"]:
        hb = c.body(p["handle"], f.d(44), f.SW * 0.8, lines=1, ls=1.0)
        c.text("handle", hb, f.CX, cy - bh / 2 - f.d(40), anchor="bottom", shadow=c.text_shadow(),
               enter=c.prof["rise"])
    squash = _keys([[0, 1.0], [click - 0.02, 1.0], [click + 0.06, 0.9, "out-quad"]])
    c.text("button", a, f.CX, cy, color=_on(col), box={"color": col, "pad": pad, "radius": _r(f.d(16))},
           shadow=c.card_shadow(), enter=c.prof["pop"], exit=None, until=life - swap, animate={"scale": squash})
    bell_D = f.d(118) if p["bell"] else 0
    bw = b.w + 2 * pad[0]
    bx = f.clamp_x(f.CX, (bw + (bell_D + f.d(24) if bell_D else 0)) / 2)
    done_x = bx - ((bell_D + f.d(24)) / 2 if bell_D else 0)
    settle = _keys([[0, 0.9], [0.22, 1.0, "out-back" if over > 0 else "out-cubic"]])
    c.text("done", b, done_x, cy, color="#0F0F0F", box={"color": "#ECECEC", "pad": pad, "radius": _r(f.d(16))},
           shadow=c.card_shadow(), at=swap, enter=("fade", 0.08), animate={"scale": settle})
    if bell_D:
        bxx = done_x + bw / 2 + f.d(24) + bell_D / 2
        wig = c.wiggle_keys(0.2, 22)
        c.circle("bell_bg", bxx, cy, bell_D, "#ECECEC", at=swap + 0.12, enter=c.prof["pop"], shadow=c.card_shadow())
        c.icon("bell", "bell", bxx, cy, bell_D * 0.58, "#0F0F0F", "#ECECEC", at=swap + 0.12, enter=c.prof["pop"],
               **({"animate": {"rotation": wig}} if wig else {}))
    # the cursor glides in, clicks (the button squashes) and leaves
    cs = f.d(100)
    tip_x, tip_y = f.CX + f.d(30), cy + f.d(8)
    arrive = click - 0.04
    t0 = max(0.0, arrive - 0.55)
    gone = click - t0 + 0.55        # glides away and fades after the click
    cur_anim = {"x": _keys([[0, f.d(300)], [arrive - t0, 0, "out-cubic"], [click - t0 + 0.2, 0],
                            [gone, f.d(170), "in-cubic"]]),
                "y": _keys([[0, f.d(330)], [arrive - t0, 0, "out-cubic"], [click - t0 + 0.2, 0],
                            [gone, f.d(210), "in-cubic"]]),
                "scale": _keys([[arrive - t0, 1.0], [click - t0 + 0.05, 0.82, "out-quad"],
                                [click - t0 + 0.18, 1.0, "out-quad"]]),
                "opacity": _keys([[click - t0 + 0.3, 1.0], [gone, 0.0, "in-quad"]])}
    c.icon("cursor", "cursor", tip_x, tip_y, cs, "#FFFFFF", "#FFFFFF", anchor="top-left", at=t0,
           enter=("fade", 0.12), exit=None, until=max(0.0, life - t0 - gone), animate=cur_anim,
           stroke={"color": "#000000", "width": _r(f.d(4))}, shadow=c.text_shadow(0.7))
    ripple = {"scale": _keys([[0, 0.3], [0.45, 1.6, "out-cubic"]]), "opacity": _keys([[0, 0.8], [0.45, 0, "out-quad"]])}
    c.ring("ripple", tip_x + cs * 0.12, tip_y + cs * 0.04, f.d(110), _alpha("#FFFFFF", 0.9), f.d(5), at=click,
           enter=None, exit=None, until=max(0.0, life - click - 0.5), animate=ripple, min_life=0.1)


# =========================================================================== utility

@_template(
    "progress_bar", "utility",
    "A thin bar filling across the top (or bottom) edge over the window: tells viewers how much is left and "
    "keeps them watching. Usually one call spanning the whole video.",
    position=_p("choice", "top", "frame edge", ["top", "bottom"]),
    color=_p("color", "", "fill colour ('' = accent)"),
    track=_p("color", "#FFFFFF33", "unfilled colour"),
    thickness=_p("float", 12, "height in design px"),
    **{"from": _p("float", 0.0, "fill at the start (0-1)"), "to": _p("float", 1.0, "fill at the end (0-1)")},
)
def _progress_bar(c, p):
    f = c.f
    h = max(2.0, f.d(p["thickness"]))
    top = p["position"] == "top"
    c.add("progress", "bar", {"x": 0, "y": 0 if top else f.H, "anchor": "top-left" if top else "bottom-left",
                              "w": f.W, "h": h, "color": c.color(p["color"]), "track": c.color(p["track"]) if p["track"] else "#FFFFFF33",
                              "from": _clamp01(p["from"]), "to": _clamp01(p["to"])},
          enter=("fade", min(0.3, c.clk.din)), exit=None)


@_template(
    "lower_third", "utility",
    "Name and title bar in the lower part of the safe zone, the classic interview/speaker ID. Use the first "
    "time a person appears, or to credit a source clip.",
    name=_p("str", "Alex Rivera", "the name"),
    title=_p("str", "Founder, Studio North", "role or context"),
    side=_p("choice", "left", "which side it hangs from", ["left", "right"]),
    color=_p("color", "", "accent bar colour ('' = accent)"),
)
def _lower_third(c, p):
    f, s = c.f, c.s
    col = c.color(p["color"])
    right = p["side"] == "right"
    name = c.fit(p["name"], f.d(70), f.SW * 0.8, lines=1, min_size=f.d(40), ls=1.0)
    title = c.body(p["title"], f.d(42), f.SW * 0.8, lines=1, min_size=f.d(28), ls=1.0) if p["title"] else None
    pad, gap, bar_w = f.d(30), f.d(16), f.d(12)
    block = name.h + ((gap + title.h) if title else 0)
    ph = block + pad * 2
    pw = max(name.w, title.w if title else 0) + pad * 2
    bottom = f.B - f.d(30)
    cy = bottom - ph / 2
    edge = f.R if right else f.L
    sx = -1 if right else 1
    a_l, a_r = ("right", "left") if right else ("left", "right")
    g = c.clk.stagger(4, 0.35)
    c.rect("bar", edge, cy, bar_w, ph, col, anchor=a_l, enter=("wipe-up", c.clk.din), exit="fade")
    px_ = edge + sx * (bar_w + f.d(4))
    c.rect("panel", px_, cy, pw, ph, _alpha(s["bg"], 0.82), radius=f.d(8), anchor=a_l,
           enter=("wipe-left" if right else "wipe-right", c.clk.din * 1.2), at=g,
           exit=("wipe-right" if right else "wipe-left", c.clk.dout * 1.4))
    tx = px_ + sx * pad
    ty = cy - block / 2
    c.text("name", name, tx, ty, anchor="top-" + a_l, align=a_l, enter=c.prof["rise"], at=2 * g, exit="fade")
    if title:
        c.text("title", title, tx, ty + name.h + gap, anchor="top-" + a_l, align=a_l, color=_alpha(s["text"], 0.75),
               enter="fade", at=3 * g, exit="fade")


@_template(
    "source_card", "utility",
    "A compact citation card (document icon or thumbnail, source name, detail). Use whenever the script "
    "cites a study, article, statistic or screenshot, to earn trust.",
    title=_p("str", "Platform Creator Report", "where it comes from"),
    detail=_p("str", "Survey of 2,000 creators, 2025", "what exactly (study, date, page)"),
    label=_p("str", "SOURCE", "small label above the title"),
    image=_p("str", "", "optional thumbnail image file (shown instead of the icon)"),
    position=_p("choice", "bottom", "vertical placement", ["top", "center", "bottom"]),
)
def _source_card(c, p):
    f, s, acc = c.f, c.s, c.s["accent"]
    width = min(f.SW, f.d(920))
    pad, th = f.d(28), f.d(118)
    tw = width - pad * 3 - th
    # the title leads: it wraps to a second line before it shrinks, and the label and the detail are
    # sized under it, so a long title never ends up the smallest text on the card
    title = c.body(p["title"], f.d(44), tw, lines=2, min_size=f.d(30), ls=1.05, prefer=1)
    lab = c.body(p["label"], min(f.d(28), title.size * 0.8), tw, lines=1, case="upper", lsp=f.d(4),
                 ls=1.0) if p["label"] else None
    dsize = min(f.d(34), title.size * 0.8)
    det = c.body(p["detail"], dsize, tw, lines=2, min_size=min(f.d(24), dsize * 0.8), ls=1.15) \
        if p["detail"] else None
    block = (lab.h + f.d(12) if lab else 0) + title.h + ((f.d(12) + det.h) if det else 0)
    ch = max(th, block) + pad * 2
    top = f.place(p["position"], ch, margin=0.03)
    cy = top + ch / 2
    x0 = f.CX - width / 2
    over = c.prof["over"] > 0
    grp = c.group((f.CX, cy), intro="rise" if over else "fade", outro="sink" if over else "fade", dist=f.d(70))
    grp.rect("card", f.CX, cy, width, ch, _alpha(s["bg2"], 0.96), radius=f.d(26), shadow=c.card_shadow(),
             stroke={"color": _alpha(s["text"], 0.08), "width": _r(f.d(2))})
    ix = x0 + pad + th / 2
    if p["image"]:
        grp.add("image", "thumb", {"src": p["image"], "fit": "cover", "w": th, "h": th, "radius": _r(f.d(16)),
                                   "x": ix, "y": cy})
    else:
        grp.rect("tile", ix, cy, th, th, _alpha(acc, 0.16), radius=f.d(18))
        grp.icon("icon", "document", ix, cy, th * 0.6, acc, _mix(acc, s["bg2"], 0.84))
    tx = x0 + pad * 2 + th
    y = cy - block / 2
    if lab:
        grp.text("label", lab, tx, y, anchor="top-left", align="left", color=acc)
        y += lab.h + f.d(12)
    grp.text("title", title, tx, y, anchor="top-left", align="left")
    if det:
        grp.text("detail", det, tx, y + title.h + f.d(12), anchor="top-left", align="left",
                 color=_alpha(s["text"], 0.66))


@_template(
    "location_tag", "utility",
    "A map-pin tag naming the place ('Kyoto, Japan' / 'Day 3'). Use when a vlog, travel, food or "
    "event video changes location, usually on the first shot there.",
    place=_p("str", "Kyoto, Japan", "the place"),
    sub=_p("str", "", "second line, e.g. a date or 'Day 3'"),
    corner=_p("choice", "top-left", "where the tag sits", ["top-left", "top-right", "bottom-left", "center"]),
    color=_p("color", "", "pin colour ('' = accent)"),
)
def _location_tag(c, p):
    f, s = c.f, c.s
    col = c.color(p["color"])
    D = f.d(92)
    pad = f.d(22)
    place = c.fit(p["place"], f.d(54), f.SW - D - pad * 4, lines=1, min_size=f.d(32), ls=1.0)
    sub = c.body(p["sub"], f.d(34), f.SW - D - pad * 4, lines=1, ls=1.0) if p["sub"] else None
    block = place.h + ((f.d(12) + sub.h) if sub else 0)
    h = max(D, block) + pad * 2
    w = pad + D + pad * 1.2 + max(place.w, sub.w if sub else 0) + pad * 2
    corner = p["corner"]
    x0 = f.R - w if corner == "top-right" else f.CX - w / 2 if corner == "center" else f.L
    y0 = f.B - h if corner == "bottom-left" else f.CY - h / 2 if corner == "center" else f.T + f.d(14)
    cy = y0 + h / 2
    over = c.prof["over"] > 0
    grp = c.group((x0 + pad + D / 2, cy), intro="pop" if over else "fade")
    grp.rect("card", x0 + w / 2, cy, w, h, _alpha(s["bg"], 0.86), radius=h / 2, shadow=c.card_shadow())
    px = x0 + pad + D / 2
    drop = (c.prof["pop"], c.clk.din, c.clk.gap)
    grp.circle("pin_bg", px, cy, D, col, enter=drop)
    grp.icon("pin", "pin", px, cy, D * 0.6, _on(col), col, enter=drop)
    tx = px + D / 2 + pad * 1.2
    grp.text("place", place, tx, cy - block / 2, anchor="top-left", align="left",
             enter=("wipe-right", c.clk.din * 1.3, c.clk.gap * 2))
    if sub:
        grp.text("sub", sub, tx, cy - block / 2 + place.h + f.d(12), anchor="top-left", align="left",
                 color=_alpha(s["text"], 0.7), enter=("fade", c.clk.din, c.clk.gap * 3))


def _image_size(path):
    """(width, height) of an image file without decoding it, or None."""
    try:
        from PIL import Image
        with Image.open(path) as im:
            return im.size
    except Exception:
        return None


def _image_see_through(path):
    """True when an image file has pixels that are not fully opaque (a cut-out, a logo); False for
    opaque or unreadable files (an RGBA screenshot whose alpha is all 255 counts as opaque)."""
    try:
        from PIL import Image
        with Image.open(path) as im:
            if im.mode in ("RGBA", "LA", "PA"):
                return im.getchannel("A").getextrema()[0] < 250
            if "transparency" in im.info:
                return im.convert("RGBA").getchannel("A").getextrema()[0] < 250
    except Exception:
        pass
    return False


@_template(
    "image_card", "utility",
    "A user asset (screenshot, product photo, chart) popped in as a rounded card with a shadow and a slight "
    "tilt, with an optional caption. Without an image it shows a placeholder card.",
    src=_p("str", "", "image file (png/jpg/webp); relative paths resolve against the spec folder"),
    caption=_p("str", "", "line under the image"),
    width=_p("float", 0.82, "card width as a fraction of the safe zone width"),
    y=_p("float", 0.45, "centre y as a fraction of the safe zone height"),
    tilt=_p("float", -2.0, "resting rotation in degrees"),
    aspect=_p("float", 0, "image width / height; 0 = read it from the file (4:3 when unreadable)"),
)
def _image_card(c, p):
    f, s = c.f, c.s
    w = f.SW * max(0.2, min(1.0, p["width"]))
    size = _image_size(p["src"]) if (p["src"] and not p["aspect"]) else None
    aspect = 1.0 / p["aspect"] if p["aspect"] > 0 else (size[1] / float(size[0])) if size and size[0] else 0.75
    cap = c.body(p["caption"], f.d(44), f.SW * 0.9, lines=2) if p["caption"] else None
    cap_h = (f.d(30) + cap.h) if cap else 0
    h = w * aspect
    max_h = f.SH * 0.86 - cap_h
    if h > max_h:
        h, w = max_h, max_h / aspect
    cy = f.clamp_y(f.T + f.SH * _clamp01(p["y"]), (h + cap_h) / 2) - cap_h / 2
    tilt = max(-10.0, min(10.0, p["tilt"]))
    over = c.prof["over"]
    rot = {"rotation": _keys([[0, tilt - 8 * over], [c.clk.din * 1.2, tilt, "out-back" if over else "out-cubic"]])}
    border, rad = f.d(10), f.d(22)
    # Frame and photo fade and pop together. Two layers fading on top of each other show the lower one
    # through the upper one half-way (a white frame under a photo turns the photo milky), so for an
    # opaque photo (and the placeholder) the frame is an outline drawn over the photo, overlapping its
    # edge by a hair so no seam shows, and the photo carries the card shadow: every pixel of the card
    # belongs to one layer. A see-through image (a cut-out, a logo) needs the white card behind it.
    backed = bool(p["src"]) and _image_see_through(p["src"])
    shadow = c.card_shadow()
    if backed:
        c.rect("frame", f.CX, cy, w + border * 2, h + border * 2, "#FFFFFF", radius=rad + border, rotation=tilt,
               shadow=shadow, enter=c.prof["pop"], drift=True, animate=dict(rot))
    else:
        shadow = dict(shadow, dy=_r(shadow["dy"] + border))
    if p["src"]:
        c.add("image", "image", {"src": p["src"], "fit": "cover", "w": w, "h": h, "radius": _r(rad),
                                 "x": f.CX, "y": cy, "rotation": tilt, "animate": dict(rot),
                                 "shadow": None if backed else shadow},
              enter=c.prof["pop"], drift=True)
    else:
        c.rect("placeholder", f.CX, cy, w, h, s["bg2"], radius=rad, rotation=tilt, enter=c.prof["pop"],
               drift=True, animate=dict(rot), shadow=shadow)
    if not backed:
        tuck = max(1.5, f.d(3))
        c.rect("frame", f.CX, cy, w + border * 2, h + border * 2, "#FFFFFF00", radius=rad + border,
               stroke={"color": "#FFFFFF", "width": _r(border + tuck)}, rotation=tilt, enter=c.prof["pop"],
               drift=True, animate=dict(rot))
    if not p["src"]:
        c.icon("placeholder_icon", "play", f.CX, cy, min(w, h) * 0.3, _alpha(s["text"], 0.5), s["bg2"], rotation=tilt,
               enter=c.prof["pop"], drift=True, animate=dict(rot))
    if cap:
        c.text("caption", cap, f.CX, cy + h / 2 + border + f.d(30), anchor="top", shadow=c.text_shadow(),
               enter=c.prof["rise"], at=c.clk.gap * 2)


# =========================================================================== docs and CLI

_CATEGORY_TITLES = [
    ("hooks", "Hooks and titles"), ("text", "Text emphasis"), ("numbers", "Numbers and data"),
    ("lists", "Lists and structure"), ("compare", "Comparisons"), ("social", "Social and UI mock-ups"),
    ("pointers", "Pointers and highlights"), ("cta", "Calls to action"), ("utility", "Utility"),
]


def _fmt_default(v):
    return json.dumps(v, ensure_ascii=False)


def catalog_doc():
    """Markdown catalog for the planning prompt: when to use each template and its params."""
    out = ["# Motion-graphics templates",
           "",
           "Use as a beat's `visual`: `{\"template\": name, \"params\": {...}}` (or a list of them).",
           "Every template fits its beat window, keeps text inside the platform safe zone and follows",
           "the plan style (font, font_secondary, text, accent, bg, bg2, energy high|medium|calm).",
           "Colour params accept #RRGGBB, '' (= style default) or a style key (accent, text, bg, bg2).",
           "List params accept a JSON list or a string split on '|'. Icons: %s." % ", ".join(_ICON_NAMES),
           ""]
    for cat, title in _CATEGORY_TITLES:
        names = [n for n, e in CATALOG.items() if e["category"] == cat]
        if not names:
            continue
        out += ["## " + title, ""]
        for n in names:
            e = CATALOG[n]
            out.append("**%s** - %s" % (n, e["description"]))
            for pn, ps in e["params"].items():
                choices = " (%s)" % "|".join(ps["choices"]) if ps.get("choices") else ""
                out.append("  - `%s` %s%s = %s: %s" % (pn, ps["type"], choices, _fmt_default(ps["default"]),
                                                       ps["doc"]))
            out.append("")
    return "\n".join(out).rstrip() + "\n"


def catalog_json():
    """The catalog without the builder functions (JSON-serializable)."""
    return {n: {"description": e["description"], "category": e["category"], "params": e["params"]}
            for n, e in CATALOG.items()}


def _table():
    lines = ["%-18s %-9s %s" % ("TEMPLATE", "KIND", "PARAMS")]
    for n, e in CATALOG.items():
        lines.append("%-18s %-9s %s" % (n, e["category"], ", ".join(e["params"])))
    lines.append("")
    lines.append("%d templates. `vstudio templates doc` explains when to use each; "
                 "`vstudio templates preview <name> --out f.mp4` renders one." % len(CATALOG))
    return "\n".join(lines)


def _canvas_for(aspect, size, fps):
    if size:
        m = re.match(r"^\s*(\d+)\s*[xX]\s*(\d+)\s*$", size)
        if not m:
            raise ValueError("--size must look like 1080x1920")
        w, h = int(m.group(1)), int(m.group(2))
    else:
        if aspect not in ASPECTS:
            raise ValueError("--aspect must be one of %s" % ", ".join(ASPECTS))
        w, h = ASPECTS[aspect]
    return {"width": w, "height": h, "fps": fps}


def preview_spec(name, params=None, aspect="9:16", size=None, fps=30, duration=3.0, style=None, background=None):
    """A complete timeline spec that shows one template for `duration` seconds."""
    canvas = _canvas_for(aspect, size, fps)
    st = _resolve_style(style)
    layers = build(name, params or {}, 0.0, duration, canvas, st)
    spec = {"version": 1, "width": canvas["width"], "height": canvas["height"], "fps": fps,
            "duration": duration, "background": {"gradient": [st["bg"], st["bg2"]], "angle": 90},
            "layers": layers,
            "meta": {"title": "template preview: " + name, "source": "manual",
                     "notes": "vstudio templates preview %s" % name}}
    if background:
        if _HEX.match(background):
            spec["background"] = background
        else:
            src = os.path.abspath(background)
            ext = os.path.splitext(src)[1].lower()
            kind = "image" if ext in (".png", ".jpg", ".jpeg", ".webp", ".bmp") else "video"
            bg = {"type": kind, "id": "preview.background", "src": src, "fit": "cover",
                  "start": 0, "end": duration, "w": canvas["width"], "h": canvas["height"]}
            if kind == "video":
                bg.update({"in": 0, "volume": 0, "loop": True})
            spec["layers"].insert(0, bg)
    return spec


def _json_arg(text, what):
    if not text:
        return {}
    if os.path.isfile(text):
        with open(text, encoding="utf-8") as fh:
            return json.load(fh)
    try:
        return json.loads(text)
    except ValueError as e:
        raise SystemExit("%s is not valid JSON: %s" % (what, e))


def _parser():
    """`vstudio templates` with its subcommands (each prints its own usage under --help)."""
    ap = argparse.ArgumentParser(
        prog="vstudio templates", formatter_class=argparse.RawDescriptionHelpFormatter,
        description="The motion-graphics templates: list them, read when to use each, print the layers one "
                    "makes, or render a preview.",
        epilog="examples:\n"
               "  vstudio templates                      the catalog as a table\n"
               "  vstudio templates --json               the catalog as JSON\n"
               "  vstudio templates doc                  when to use each template, with every param\n"
               "  vstudio templates build stat --params '{\"value\": \"73%\"}'\n"
               "  vstudio templates preview cta --out cta.mp4 [--aspect 16:9] [--energy calm]\n"
               "  vstudio templates preview cta --out cta.png --still 1.5\n\n"
               "`vstudio templates <command> --help` shows a command's options.")
    ap.add_argument("--json", dest="json_top", action="store_true", help="list: print the catalog as JSON")
    sub = ap.add_subparsers(dest="cmd", metavar="{list,doc,build,preview}",
                            title="commands (default: list)")
    ls = sub.add_parser("list", help="the catalog as a table (names, kinds, params)",
                        description="The template catalog as a table, or as JSON with --json.")
    ls.add_argument("--json", action="store_true", help="print the catalog as JSON")
    sub.add_parser("doc", help="when to use each template and what every param does (markdown)",
                   description="The markdown catalog the planner gives Claude: when to use each template, "
                               "and every param with its type, choices and default.")
    one = argparse.ArgumentParser(add_help=False)
    one.add_argument("name", help="template name (see `vstudio templates`)")
    one.add_argument("--params", help="JSON object (or a .json file) of template params")
    one.add_argument("--style", help="JSON object (or a .json file) of style keys")
    one.add_argument("--energy", choices=sorted(_ENERGY), help="override style.energy")
    one.add_argument("--aspect", default="9:16", help="9:16 (default), 1:1, 4:5 or 16:9")
    one.add_argument("--size", help="exact canvas size, e.g. 216x384 for a fast draft")
    one.add_argument("--fps", type=int, default=30)
    one.add_argument("--duration", type=float, default=3.0, help="seconds the template is shown (default 3)")
    b = sub.add_parser("build", parents=[one], help="print the timeline layers a template makes (JSON)",
                       description="Print the timeline layers one template makes, as JSON.")
    b.add_argument("--start", type=float, default=0.0, help="timeline second the template starts at")
    pv = sub.add_parser("preview", parents=[one], help="render a template to an MP4 (or one PNG still)",
                        description="Render one template over a plain background (or your clip) to an MP4, "
                                    "or to one PNG still when --out ends in .png.")
    pv.add_argument("--out", required=True, help="output .mp4 (or .png for one still)")
    pv.add_argument("--background", help="#hex colour or a video/image file to put behind the template")
    pv.add_argument("--still", type=float, help="time of the still when --out is .png (default: settled)")
    pv.add_argument("--preview", action="store_true", help="half resolution and fps (fast draft)")
    pv.add_argument("--spec", help="also write the timeline spec JSON here")
    return ap


def main(argv=None):
    """vstudio templates [list] [--json] | doc | build <name> | preview <name> --out f.mp4"""
    argv = list(sys.argv[1:] if argv is None else argv)
    args = _parser().parse_args(argv)
    cmd = args.cmd or "list"
    if cmd == "list":
        as_json = args.json_top or getattr(args, "json", False)
        print(json.dumps(catalog_json(), indent=2, ensure_ascii=False) if as_json else _table())
        return 0
    if cmd == "doc":
        sys.stdout.write(catalog_doc())
        return 0
    if args.name not in CATALOG:
        print("unknown template %r; known: %s" % (args.name, ", ".join(CATALOG)), file=sys.stderr)
        return 2
    style = _json_arg(args.style, "--style")
    if args.energy:
        style["energy"] = args.energy
    params = _json_arg(args.params, "--params")
    try:
        canvas = _canvas_for(args.aspect, args.size, args.fps)
    except ValueError as e:
        print(e, file=sys.stderr)
        return 2
    if cmd == "build":
        layers = build(args.name, params, args.start, args.start + args.duration, canvas, style)
        print(json.dumps(layers, indent=2, ensure_ascii=False))
        return 0
    spec = preview_spec(args.name, params, args.aspect, args.size, args.fps, args.duration, style,
                        args.background)
    out = os.path.abspath(args.out)
    spec_path = args.spec
    try:
        from . import render
    except ImportError as e:
        spec_path = spec_path or os.path.splitext(out)[0] + ".json"
        render = None
        reason = str(e)
    if spec_path:
        os.makedirs(os.path.dirname(os.path.abspath(spec_path)) or ".", exist_ok=True)
        with open(spec_path, "w", encoding="utf-8") as fh:
            json.dump(spec, fh, indent=2, ensure_ascii=False)
    if render is None:
        print("render engine not available (%s); wrote the timeline spec to %s" % (reason, spec_path),
              file=sys.stderr)
        return 1
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    if out.lower().endswith(".png"):
        t = args.still if args.still is not None else min(args.duration * 0.62, args.duration - 0.4)
        render.still(spec, t, out)
    else:
        render.render(spec, out, preview=args.preview)
    print(out)
    return 0
