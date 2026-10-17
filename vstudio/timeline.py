"""
timeline.py - the TIMELINE SPEC (version 1): defaults, normalization and
validation. See ARCHITECTURE.md section 1 for the format.

    spec = load("project/spec.json")    # read + normalize (paths resolved against the file's folder)
    errors = validate(spec)             # [] when the spec can be rendered
    duration(spec)                      # seconds

normalize() returns a new dict where every field the renderer reads exists:
defaults filled in, "50%" coordinates turned into pixels, relative paths
made absolute against base_dir, keyframes sorted, enter/exit shorthands
("pop") expanded to {"type", "duration", "delay"}, highlight words given as
strings turned into word indices. It never raises on bad input; it leaves
what it does not understand for validate() to report. normalize() is
idempotent: normalizing a normalized spec changes nothing.

validate() reports every problem it can find in one pass, as sentences that
name the place (`layers[3] "title" (text): ...`) and suggest the closest
valid name for typos.

Durations that are not given are measured: a video or audio file without an
`end`/`duration` lasts as long as the file (ffprobe), captions as long as
their last word. If nothing has a length the spec is 5 s long.
"""

import copy
import difflib
import json
import math
import os
import re
import subprocess

from . import anim, sfx

SCHEMA_VERSION = 1
DEFAULT_DURATION = 5.0

LAYER_TYPES = ["video", "image", "text", "shape", "progress", "counter"]
ANCHORS = {
    "center": (0.5, 0.5), "top-left": (0.0, 0.0), "top": (0.5, 0.0), "top-right": (1.0, 0.0),
    "left": (0.0, 0.5), "right": (1.0, 0.5), "bottom-left": (0.0, 1.0), "bottom": (0.5, 1.0),
    "bottom-right": (1.0, 1.0),
}
BLENDS = ["normal", "add", "screen", "multiply"]
FITS = ["cover", "contain", "fill"]
CASES = ["none", "upper", "lower", "title"]
ALIGNS = ["left", "center", "right"]
SHAPES = ["rect", "circle", "ellipse", "line", "polygon", "ring"]
ANIM_PROPS = ["x", "y", "opacity", "scale", "rotation", "blur"]
CAMERA_PROPS = ["zoom", "x", "y", "rotation"]
EFFECT_TYPES = ["flash", "shake", "glitch", "zoom-blur", "vignette", "grade", "grain", "letterbox"]
TIMED_EFFECTS = ["flash", "shake", "glitch", "zoom-blur"]   # these need a start
AUDIO_ROLES = ["music", "voice", "sfx", "clip"]
CAPTION_STYLE_KEYS = ["mode", "font_size_pct", "center_x_pct", "center_y_pct", "max_width_pct", "case", "fill",
                      "highlight", "outline", "outline_pct", "box", "words_per_card", "lines_per_card", "align",
                      "font_candidates", "font_look"]

COMMON_DEFAULTS = {
    "id": None, "start": 0.0, "end": None, "x": None, "y": None, "anchor": "center", "opacity": 1.0,
    "scale": 1.0, "rotation": 0.0, "animate": {}, "enter": None, "exit": None, "fixed": False,
    "blend": "normal", "shadow": None,
}
TEXT_DEFAULTS = {
    "text": "", "font": "Montserrat Black", "size": 96, "color": "#FFFFFF", "case": "none", "stroke": None,
    "box": None, "highlight": None, "max_width": None, "align": "center", "line_spacing": 1.1,
    "letter_spacing": 0, "word_times": None, "gradient": None,
}
TYPE_DEFAULTS = {
    "video": {"src": None, "in": 0.0, "speed": 1.0, "fit": "cover", "focus": [0.5, 0.5], "w": None, "h": None,
              "volume": 1.0, "kenburns": None, "loop": False},
    "image": {"src": None, "fit": "contain", "focus": [0.5, 0.5], "w": None, "h": None, "kenburns": None,
              "radius": 0},
    "text": TEXT_DEFAULTS,
    "shape": {"shape": "rect", "w": 100, "h": 100, "color": "#FFFFFF", "radius": 0, "stroke": None,
              "points": None, "gradient": None, "draw": None},
    "progress": {"w": None, "h": 10, "color": "#FFE500", "track": "#FFFFFF33", "from": 0.0, "to": 1.0,
                 "radius": 0},
    "counter": dict(TEXT_DEFAULTS, **{"from": 0, "to": 100, "decimals": 0, "prefix": "", "suffix": "",
                                      "separator": ",", "count_duration": 1.2, "ease": "out-expo"}),
}
# progress bars are HUD elements: top-left anchored at (0, 0) and fixed unless told otherwise
PROGRESS_COMMON = {"x": 0, "y": 0, "anchor": "top-left", "fixed": True}

SHADOW_DEFAULT = {"color": "#00000099", "dx": 0, "dy": 8, "blur": 16}
BOX_DEFAULT = {"color": "#000000", "pad": [24, 14], "radius": 18, "mode": "block"}
HIGHLIGHT_BOX_DEFAULT = {"color": "#FFE500", "pad": [12, 6], "radius": 12}
EFFECT_DEFAULTS = {
    "flash": {"duration": 0.15, "color": "#FFFFFF", "opacity": 0.85},
    "shake": {"duration": 0.3, "intensity": 18},
    "glitch": {"duration": 0.25, "intensity": 1.0},
    "zoom-blur": {"duration": 0.2, "intensity": 1.0},
    "vignette": {"strength": 0.35},
    "grade": {"contrast": 1.0, "saturation": 1.0, "brightness": 0.0, "warmth": 0.0},
    "grain": {"strength": 0.04},
    "letterbox": {"start": 0.0, "duration": None, "ratio": 2.35},
}
AUDIO_DEFAULTS = {"src": None, "sfx": None, "start": 0.0, "in": 0.0, "duration": None, "volume": 1.0,
                  "fade_in": 0.0, "fade_out": 0.0, "role": None, "duck": None, "loop": False}

FONT_EXT = (".ttf", ".otf", ".ttc", ".otc")
COLOR_RE = re.compile(r"^#(?:[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$")
WORD_STRIP = re.compile(r"[^\w$%]+", re.UNICODE)


# --------------------------------------------------------------------------- small helpers

def _isnum(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _pct(v, total):
    """'50%' -> 0.5 * total; numeric strings -> float; anything else unchanged."""
    if isinstance(v, str):
        s = v.strip()
        try:
            return float(s[:-1]) / 100.0 * total if s.endswith("%") else float(s)
        except ValueError:
            return v
    return v


def _color(c):
    """Expand #RGB / #RGBA shorthands; leave everything else for validate()."""
    if isinstance(c, str) and re.match(r"^#[0-9a-fA-F]{3,4}$", c):
        return "#" + "".join(ch * 2 for ch in c[1:])
    return c


def parse_color(c, default=(255, 255, 255, 255)):
    """'#RRGGBB' / '#RRGGBBAA' -> (r, g, b, a) ints 0..255."""
    c = _color(c)
    if not isinstance(c, str) or not COLOR_RE.match(c):
        return default
    h = c[1:]
    vals = [int(h[i:i + 2], 16) for i in range(0, len(h), 2)]
    return tuple(vals) if len(vals) == 4 else tuple(vals) + (255,)


def _luma(c):
    r, g, b, _ = parse_color(c)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _resolve(p, base_dir):
    if not isinstance(p, str) or not p or re.match(r"^[a-z][a-z0-9+.-]*://", p, re.I):
        return p
    p = os.path.expanduser(p)
    if not os.path.isabs(p):
        p = os.path.join(base_dir or os.getcwd(), p)
    return os.path.normpath(os.path.abspath(p))


def _looks_like_font_file(name):
    return isinstance(name, str) and (name.lower().endswith(FONT_EXT) or "/" in name or "\\" in name)


def _suggest(name, choices):
    m = difflib.get_close_matches(str(name), list(choices), n=1, cutoff=0.6)
    return " (did you mean %r?)" % m[0] if m else " (use one of: %s)" % ", ".join(choices)


def text_words(layer):
    """The words of a text layer after its case transform (what highlight indices count)."""
    from_case = str(layer.get("text") or "")
    case = layer.get("case")
    if case == "upper":
        from_case = from_case.upper()
    elif case == "lower":
        from_case = from_case.lower()
    elif case == "title":
        from_case = " ".join(w[:1].upper() + w[1:] for w in from_case.split(" "))
    return from_case.split()


def _norm_word(w):
    return WORD_STRIP.sub("", str(w)).lower()


# --------------------------------------------------------------------------- media probing

_PROBE_CACHE = {}


def probe(path):
    """ffprobe facts about a media file: {duration, width, height, fps, has_audio, has_video}.

    Returns None when the file or ffprobe is missing or unreadable. Cached by
    path + modification time. Width/height account for phone rotation tags.
    """
    try:
        st = os.stat(path)
    except (OSError, TypeError):
        return None
    key = (path, st.st_mtime, st.st_size)
    if key in _PROBE_CACHE:
        return _PROBE_CACHE[key]
    info = None
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                            "format=duration:stream=codec_type,width,height,avg_frame_rate,r_frame_rate,duration:"
                            "stream_tags=rotate:stream_side_data=rotation", "-of", "json", path],
                           capture_output=True, text=True, timeout=60)
        if r.returncode == 0:
            data = json.loads(r.stdout or "{}")
            streams = data.get("streams") or []
            vs = next((s for s in streams if s.get("codec_type") == "video"), None)
            dur = (data.get("format") or {}).get("duration")
            info = {"duration": float(dur) if dur not in (None, "N/A") else None,
                    "has_audio": any(s.get("codec_type") == "audio" for s in streams),
                    "has_video": vs is not None, "width": None, "height": None, "fps": None}
            if vs:
                w, h = vs.get("width"), vs.get("height")
                rot = 0
                for sd in vs.get("side_data_list") or []:
                    if "rotation" in sd:
                        rot = int(float(sd["rotation"]))
                rot = rot or int(float((vs.get("tags") or {}).get("rotate", 0) or 0))
                if rot % 180:
                    w, h = h, w
                info["width"], info["height"] = w, h
                for k in ("avg_frame_rate", "r_frame_rate"):
                    num, _, den = str(vs.get(k) or "0/0").partition("/")
                    if den and float(den) > 0 and float(num) > 0:
                        info["fps"] = float(num) / float(den)
                        break
                if info["duration"] is None and vs.get("duration") not in (None, "N/A"):
                    info["duration"] = float(vs["duration"])
    except (OSError, ValueError, subprocess.SubprocessError):
        info = None
    _PROBE_CACHE[key] = info
    return info


def _media_len(path):
    info = probe(path)
    return info.get("duration") if info else None


# --------------------------------------------------------------------------- duration

def _natural_duration(spec):
    ends = []
    for L in spec.get("layers") or []:
        if not isinstance(L, dict):
            continue
        start = L.get("start") if _isnum(L.get("start")) else 0.0
        if _isnum(L.get("end")):
            ends.append(L["end"])
        elif L.get("type") == "video" and not L.get("loop") and isinstance(L.get("src"), str):
            n = _media_len(L["src"])
            speed = L.get("speed") if _isnum(L.get("speed")) and L["speed"] > 0 else 1.0
            src_in = L.get("in") if _isnum(L.get("in")) else 0.0
            if n:
                ends.append(start + max(0.0, n - src_in) / speed)
    for a in spec.get("audio") or []:
        if not isinstance(a, dict):
            continue
        start = a.get("start") if _isnum(a.get("start")) else 0.0
        if _isnum(a.get("duration")):
            ends.append(start + a["duration"])
        elif a.get("sfx") in sfx.NAMES:
            ends.append(start + sfx.duration(a["sfx"]))
        elif isinstance(a.get("src"), str) and not a.get("loop"):
            n = _media_len(a["src"])
            if n:
                ends.append(start + max(0.0, n - (a.get("in") if _isnum(a.get("in")) else 0.0)))
    cap = spec.get("captions")
    if isinstance(cap, dict):
        es = [w.get("e") for w in cap.get("words") or [] if isinstance(w, dict) and _isnum(w.get("e"))]
        if es:
            ends.append(max(es))
    ends = [e for e in ends if _isnum(e) and e > 0]
    return float(max(ends)) if ends else DEFAULT_DURATION


def duration(spec):
    """Timeline length in seconds: `duration` if given, else the natural length of its content."""
    d = spec.get("duration") if isinstance(spec, dict) else None
    if _isnum(d) and d > 0:
        return float(d)
    return _natural_duration(spec if isinstance(spec, dict) else {})


# --------------------------------------------------------------------------- normalize

def _norm_keys(keys, total=None):
    """Keyframes -> sorted [[t, v], [t, v, easing]] lists; '%' values converted when total is given."""
    if not isinstance(keys, list):
        return keys
    out = []
    for k in keys:
        if isinstance(k, (list, tuple)) and len(k) in (2, 3):
            k = list(k)
            if total is not None:
                k[1] = _pct(k[1], total)
            if len(k) == 3 and k[2] is None:
                k = k[:2]
            out.append(k)
        else:
            out.append(k)
    try:
        out.sort(key=lambda k: k[0])
    except (TypeError, IndexError, KeyError):
        pass  # malformed keys: validate() reports them
    return out


def _norm_motion(m, layer, which):
    if m in (None, False, "none"):
        return None
    if isinstance(m, str):
        m = {"type": m}
    if not isinstance(m, dict):
        return m
    m = dict(m)
    m.setdefault("type", "fade")
    if m.get("duration") is None:
        known = anim.ENTER_TYPES if which == "enter" else anim.EXIT_TYPES
        m["duration"] = anim.default_duration(m["type"], layer, which) if m["type"] in known else 0.3
    if which == "enter":
        m.setdefault("delay", 0.0)
    return m


def _norm_pad(p):
    if _isnum(p):
        return [p, p]
    if isinstance(p, (list, tuple)) and len(p) == 2:
        return list(p)
    return p


def _norm_text(L, W):
    for k in ("color",):
        L[k] = _color(L.get(k))
    if L.get("max_width") is None:
        L["max_width"] = round(W * 0.88)
    else:
        L["max_width"] = _pct(L["max_width"], W)
    if isinstance(L.get("stroke"), dict):
        L["stroke"] = dict({"color": "#000000", "width": 0}, **L["stroke"])
        L["stroke"]["color"] = _color(L["stroke"]["color"])
    if L.get("box") is True:
        L["box"] = dict(BOX_DEFAULT)
    if isinstance(L.get("box"), dict):
        L["box"] = dict(BOX_DEFAULT, **L["box"])
        L["box"]["pad"] = _norm_pad(L["box"]["pad"])
        L["box"]["color"] = _color(L["box"]["color"])
    hl = L.get("highlight")
    if isinstance(hl, list):
        hl = {"words": hl}
    if isinstance(hl, dict):
        hl = dict(hl)
        box = hl.get("box")
        if box is True:
            box = {}
        if isinstance(box, dict):
            box = dict(HIGHLIGHT_BOX_DEFAULT, **box)
            box["pad"] = _norm_pad(box["pad"])
            box["color"] = _color(box["color"])
            if hl.get("color") is None:  # readable text on the pill by default
                hl["color"] = "#000000" if _luma(box["color"]) > 140 else "#FFFFFF"
        hl["box"] = box
        hl.setdefault("color", "#FFE500")
        hl["color"] = _color(hl["color"])
        words = hl.get("words")
        if isinstance(words, (str, int)):
            words = [words]
        if isinstance(words, list):
            tw = [_norm_word(w) for w in text_words(L)]
            conv = []
            for w in words:
                if isinstance(w, str) and not w.lstrip("-").isdigit():
                    # a word given as text: every occurrence (each word of a phrase) is highlighted
                    for part in w.split():
                        hits = [i for i, x in enumerate(tw) if x and x == _norm_word(part)]
                        conv.extend(hits if hits else [w])
                elif isinstance(w, str):
                    conv.append(int(w))
                else:
                    conv.append(w)
            seen = []
            for w in conv:
                if w not in seen:
                    seen.append(w)
            hl["words"] = seen
        else:
            hl["words"] = [] if words is None else words
        L["highlight"] = hl
    if isinstance(L.get("gradient"), list):
        L["gradient"] = [_color(c) for c in L["gradient"]]


def _norm_layer(L, W, H, dur, base_dir):
    if not isinstance(L, dict):
        return L
    t = L.get("type")
    out = dict(COMMON_DEFAULTS)
    if t == "progress":
        out.update(PROGRESS_COMMON)
    out.update(TYPE_DEFAULTS.get(t, {}))
    out.update({k: v for k, v in L.items() if v is not None or k not in out})
    # keep explicit nulls only where null is meaningful
    for k, v in L.items():
        if v is None and k in ("enter", "exit", "shadow", "stroke", "box", "highlight", "gradient", "draw",
                               "points", "kenburns", "word_times", "end", "id"):
            out[k] = None
    L = out
    L["animate"] = dict(L.get("animate") or {}) if isinstance(L.get("animate"), dict) else L.get("animate")
    if L.get("end") is None:
        L["end"] = dur
    L["x"] = W / 2.0 if L.get("x") is None else _pct(L["x"], W)
    L["y"] = H / 2.0 if L.get("y") is None else _pct(L["y"], H)
    for k in ("start", "end", "opacity", "scale", "rotation"):
        L[k] = _pct(L[k], 1.0) if isinstance(L.get(k), str) else L.get(k)
    if isinstance(L["animate"], dict):
        for prop, keys in list(L["animate"].items()):
            total = W if prop == "x" else H if prop == "y" else None
            L["animate"][prop] = _norm_keys(keys, total)
    L["enter"] = _norm_motion(L.get("enter"), L, "enter")
    L["exit"] = _norm_motion(L.get("exit"), L, "exit")
    if L.get("shadow") is True:
        L["shadow"] = dict(SHADOW_DEFAULT)
    elif isinstance(L.get("shadow"), dict):
        L["shadow"] = dict(SHADOW_DEFAULT, **L["shadow"])
        L["shadow"]["color"] = _color(L["shadow"]["color"])
    for k in ("w", "max_width"):
        if isinstance(L.get(k), str):
            L[k] = _pct(L[k], W)
    if isinstance(L.get("h"), str):
        L["h"] = _pct(L["h"], H)

    if t in ("video", "image"):
        L["src"] = _resolve(L.get("src"), base_dir)
        if t == "video" or L.get("fit") == "cover":
            L["w"] = W if L.get("w") is None else L["w"]
            L["h"] = H if L.get("h") is None else L["h"]
        if isinstance(L.get("focus"), (list, tuple)):
            L["focus"] = list(L["focus"])
        if isinstance(L.get("kenburns"), (list, tuple)):
            L["kenburns"] = list(L["kenburns"])
    elif t in ("text", "counter"):
        if _looks_like_font_file(L.get("font")):
            L["font"] = _resolve(L["font"], base_dir)
        _norm_text(L, W)
    elif t == "shape":
        L["color"] = _color(L.get("color"))
        if isinstance(L.get("stroke"), dict):
            L["stroke"] = dict({"color": "#000000", "width": 0}, **L["stroke"])
            L["stroke"]["color"] = _color(L["stroke"]["color"])
        if L.get("shape") == "ring" and not (isinstance(L.get("stroke"), dict) and L["stroke"].get("width")):
            # a ring needs a thickness: default to 10 % of its size, in the shape's colour
            size = min(L["w"], L["h"]) if _isnum(L.get("w")) and _isnum(L.get("h")) else 100
            L["stroke"] = {"color": L["color"], "width": max(1.0, round(0.1 * size, 2))}
        if isinstance(L.get("draw"), dict):
            L["draw"] = dict({"from": 0.0, "to": 1.0, "ease": "out-cubic"}, **L["draw"])
        if isinstance(L.get("gradient"), list):
            L["gradient"] = [_color(c) for c in L["gradient"]]
    elif t == "progress":
        L["w"] = W if L.get("w") is None else L["w"]
        L["color"], L["track"] = _color(L.get("color")), _color(L.get("track"))
    return L


def _norm_caption_style(style):
    if not isinstance(style, dict):
        return style
    style = dict(style)
    wpc = style.get("words_per_card")
    if _isnum(wpc):
        style["words_per_card"] = {"median": wpc, "max": wpc}
    return style


def normalize(spec, base_dir=None):
    """A complete copy of the spec: defaults filled, '%' coordinates in px, paths absolute."""
    if not isinstance(spec, dict):
        return spec
    s = copy.deepcopy(spec)
    s.setdefault("version", SCHEMA_VERSION)
    s["width"] = s.get("width") if s.get("width") is not None else 1080
    s["height"] = s.get("height") if s.get("height") is not None else 1920
    s["fps"] = s.get("fps") if s.get("fps") is not None else 30
    W = s["width"] if _isnum(s["width"]) else 1080
    H = s["height"] if _isnum(s["height"]) else 1920
    bg = s.get("background")
    if bg is None:
        s["background"] = "#000000"
    elif isinstance(bg, dict):
        bg = dict(bg)
        if isinstance(bg.get("gradient"), list):
            bg["gradient"] = [_color(c) for c in bg["gradient"]]
        bg.setdefault("angle", 90)
        s["background"] = bg
    else:
        s["background"] = _color(bg)

    for a in s.get("audio") or []:
        if isinstance(a, dict) and isinstance(a.get("src"), str):
            a["src"] = _resolve(a["src"], base_dir)
    if not isinstance(s.get("layers"), list):
        s["layers"] = [] if s.get("layers") is None else s["layers"]
    for L in s["layers"] if isinstance(s["layers"], list) else []:
        if isinstance(L, dict) and L.get("type") in ("video", "image") and isinstance(L.get("src"), str):
            L["src"] = _resolve(L["src"], base_dir)
    cap = s.get("captions")
    if isinstance(cap, dict) and isinstance(cap.get("style_dna"), str):
        cap["style_dna"] = _resolve(cap["style_dna"], base_dir)
    # the natural duration needs resolved paths, so it is measured after resolving them;
    # an invalid explicit duration is kept as it is so validate() can report it
    if s.get("duration") is None:
        s["duration"] = duration(s)
    dur = duration(s)

    if isinstance(s["layers"], list):
        s["layers"] = [_norm_layer(L, W, H, dur, base_dir) for L in s["layers"]]

    cam = s.get("camera")
    cam = dict(cam) if isinstance(cam, dict) else ({} if cam is None else cam)
    if isinstance(cam, dict):
        for p in CAMERA_PROPS:
            cam[p] = _norm_keys(cam.get(p) or [], W if p == "x" else H if p == "y" else None)
    s["camera"] = cam

    effects = s.get("effects")
    if effects is None:
        effects = []
    if isinstance(effects, list):
        out = []
        for e in effects:
            if isinstance(e, dict) and e.get("type") in EFFECT_DEFAULTS:
                e = dict(EFFECT_DEFAULTS[e["type"]], **{k: v for k, v in e.items() if v is not None})
                if "color" in e:
                    e["color"] = _color(e["color"])
                if e["type"] == "letterbox" and e.get("duration") is None:
                    e["duration"] = max(0.0, dur - (e["start"] if _isnum(e.get("start")) else 0.0))
            out.append(e)
        effects = out
    s["effects"] = effects

    if isinstance(cap, dict):
        cap = dict({"words": [], "style": None, "style_dna": None, "font": None, "pop": False}, **cap)
        cap["style"] = _norm_caption_style(cap.get("style"))
        if cap.get("words") is None:
            cap["words"] = []
        s["captions"] = cap
    elif not cap:
        s["captions"] = None

    audio = s.get("audio")
    if audio is None:
        audio = []
    if isinstance(audio, list):
        out = []
        for a in audio:
            if isinstance(a, dict):
                a = dict(AUDIO_DEFAULTS, **{k: v for k, v in a.items() if v is not None})
                if a.get("role") is None:
                    a["role"] = "sfx" if a.get("sfx") else "music"
                if a.get("duck") is None:
                    a["duck"] = a["role"] == "music"
            out.append(a)
        audio = out
    s["audio"] = audio
    if not isinstance(s.get("meta"), dict):
        s["meta"] = {} if s.get("meta") is None else s["meta"]
    return s


# --------------------------------------------------------------------------- validate

class _Errors(list):
    def need(self, cond, where, msg):
        if not cond:
            self.append("%s: %s" % (where, msg) if where else msg)
        return cond


def _check_color(errs, where, field, c, allow_none=False):
    if c is None and allow_none:
        return
    errs.need(isinstance(c, str) and COLOR_RE.match(c), where,
              "%s %r is not a colour (use \"#RRGGBB\" or \"#RRGGBBAA\")" % (field, c))


def _check_num(errs, where, field, v, lo=None, hi=None, lo_open=False, allow_none=False):
    if v is None and allow_none:
        return True
    if not errs.need(_isnum(v), where, "%s must be a number, got %r" % (field, v)):
        return False
    if lo is not None:
        bad = v <= lo if lo_open else v < lo
        if bad:
            errs.append("%s: %s must be %s %s, got %r" % (where, field, ">" if lo_open else ">=", lo, v))
            return False
    if hi is not None and v > hi:
        errs.append("%s: %s must be <= %s, got %r" % (where, field, hi, v))
        return False
    return True


def _check_keys(errs, where, field, keys, allow_pct=False):
    if keys is None:
        return
    if not errs.need(isinstance(keys, list), where, "%s must be a list of keyframes [[t, value], ...]" % field):
        return
    for i, k in enumerate(keys):
        ok = isinstance(k, (list, tuple)) and len(k) in (2, 3) and _isnum(k[0]) and _isnum(k[1])
        if not ok:
            errs.append("%s: %s[%d] must be [t, value] or [t, value, \"easing\"] with numbers, got %r"
                        % (where, field, i, k))
            continue
        if len(k) == 3 and k[2] is not None and k[2] not in anim.EASINGS:
            errs.append("%s: %s[%d] has unknown easing %r%s" % (where, field, i, k[2], _suggest(k[2], anim.EASINGS)))


def _check_file(errs, where, field, path):
    if not errs.need(isinstance(path, str) and path, where, "%s is missing" % field):
        return
    if re.match(r"^[a-z][a-z0-9+.-]*://", path, re.I):
        errs.append("%s: %s %r is a URL; download it first and point %s at the local file" % (where, field, path, field))
    elif not os.path.isfile(path):
        errs.append("%s: %s file not found: %s" % (where, field, path))


def _check_unknown_fields(errs, where, obj, known):
    for k in obj:
        if k not in known:
            m = difflib.get_close_matches(k, list(known), n=1, cutoff=0.8)
            if m:
                errs.append("%s: unknown field %r (did you mean %r?)" % (where, k, m[0]))


def _check_motion(errs, where, which, m, kinds):
    if m is None:
        return
    if not errs.need(isinstance(m, dict), where, "%s must be {\"type\": ..., \"duration\": s} or a type name" % which):
        return
    if m.get("type") not in kinds:
        errs.append("%s: unknown %s type %r%s" % (where, which, m.get("type"), _suggest(m.get("type"), kinds)))
    _check_num(errs, where, which + ".duration", m.get("duration"), 0, lo_open=True)
    if which == "enter":
        _check_num(errs, where, "enter.delay", m.get("delay", 0), 0)
    _check_num(errs, where, which + ".distance", m.get("distance"), allow_none=True)


def _check_text(errs, where, L):
    if L["type"] == "text":
        errs.need(isinstance(L.get("text"), str) and L["text"].strip(), where, "text is empty")
    _check_num(errs, where, "size", L.get("size"), 0, lo_open=True)
    errs.need(isinstance(L.get("font"), str) and L["font"].strip(), where, "font must be a family name or a font file")
    if _looks_like_font_file(L.get("font")) and not os.path.isfile(L["font"]):
        errs.append("%s: font file not found: %s" % (where, L["font"]))
    _check_color(errs, where, "color", L.get("color"))
    if L.get("case") not in CASES:
        errs.append("%s: unknown case %r%s" % (where, L.get("case"), _suggest(L.get("case"), CASES)))
    if L.get("align") not in ALIGNS:
        errs.append("%s: unknown align %r%s" % (where, L.get("align"), _suggest(L.get("align"), ALIGNS)))
    _check_num(errs, where, "max_width", L.get("max_width"), 0, lo_open=True)
    _check_num(errs, where, "line_spacing", L.get("line_spacing"), 0, lo_open=True)
    _check_num(errs, where, "letter_spacing", L.get("letter_spacing"))
    st = L.get("stroke")
    if st is not None and errs.need(isinstance(st, dict), where, "stroke must be {\"color\", \"width\"}"):
        _check_color(errs, where, "stroke.color", st.get("color"))
        _check_num(errs, where, "stroke.width", st.get("width"), 0)
    for name, box in (("box", L.get("box")), ("highlight.box", (L.get("highlight") or {}).get("box")
                                              if isinstance(L.get("highlight"), dict) else None)):
        if box is None:
            continue
        if not errs.need(isinstance(box, dict), where, "%s must be {\"color\", \"pad\", \"radius\"}" % name):
            continue
        _check_color(errs, where, name + ".color", box.get("color"))
        pad = box.get("pad")
        errs.need(isinstance(pad, list) and len(pad) == 2 and all(_isnum(p) and p >= 0 for p in pad), where,
                  "%s.pad must be a number or [x, y], got %r" % (name, pad))
        _check_num(errs, where, name + ".radius", box.get("radius"), 0)
        if name == "box" and box.get("mode") not in ("block", "lines"):
            errs.append("%s: box.mode must be \"block\" or \"lines\", got %r" % (where, box.get("mode")))
    hl = L.get("highlight")
    if hl is not None and errs.need(isinstance(hl, dict), where, "highlight must be {\"words\": [...], \"color\"}"):
        _check_color(errs, where, "highlight.color", hl.get("color"))
        words = hl.get("words")
        if errs.need(isinstance(words, list), where, "highlight.words must be a list of word indices"):
            n = len(text_words(L))
            for w in words:
                if isinstance(w, str):
                    errs.append("%s: highlight word %r is not in the text" % (where, w))
                elif not (isinstance(w, int) and not isinstance(w, bool)):
                    errs.append("%s: highlight.words must hold word indices, got %r" % (where, w))
                elif L["type"] == "text" and not -n <= w < n:
                    errs.append("%s: highlight word index %d is out of range (the text has %d words)" % (where, w, n))
    g = L.get("gradient")
    if g is not None and errs.need(isinstance(g, list) and len(g) >= 2, where, "gradient must be [\"#hex\", \"#hex\"]"):
        for c in g:
            _check_color(errs, where, "gradient colour", c)
    wt = L.get("word_times")
    if wt is not None:
        errs.need(isinstance(wt, list) and all(_isnum(x) for x in wt), where, "word_times must be a list of seconds")


def _check_layer(errs, i, L, spec):
    if not isinstance(L, dict):
        errs.append("layers[%d]: a layer must be an object, got %r" % (i, L))
        return
    t = L.get("type")
    where = "layers[%d]%s (%s)" % (i, " %r" % L["id"] if L.get("id") else "", t)
    if t not in LAYER_TYPES:
        errs.append("%s: unknown layer type %r%s" % (where, t, _suggest(t, LAYER_TYPES)))
        return
    known = set(COMMON_DEFAULTS) | set(TYPE_DEFAULTS[t]) | {"type"}
    _check_unknown_fields(errs, where, L, known)
    ok_s = _check_num(errs, where, "start", L.get("start"), 0)
    ok_e = _check_num(errs, where, "end", L.get("end"))
    if ok_s and ok_e and L["end"] <= L["start"]:
        errs.append("%s: end (%g) must be after start (%g)" % (where, L["end"], L["start"]))
    _check_num(errs, where, "x", L.get("x"))
    _check_num(errs, where, "y", L.get("y"))
    if L.get("anchor") not in ANCHORS:
        errs.append("%s: unknown anchor %r%s" % (where, L.get("anchor"), _suggest(L.get("anchor"), ANCHORS)))
    _check_num(errs, where, "opacity", L.get("opacity"), 0, 1)
    _check_num(errs, where, "scale", L.get("scale"), 0, lo_open=True)
    _check_num(errs, where, "rotation", L.get("rotation"))
    if L.get("blend") not in BLENDS:
        errs.append("%s: unknown blend %r%s" % (where, L.get("blend"), _suggest(L.get("blend"), BLENDS)))
    errs.need(isinstance(L.get("fixed"), bool), where, "fixed must be true or false")
    an = L.get("animate")
    if errs.need(isinstance(an, dict), where, "animate must be {prop: keyframes}"):
        for prop, keys in an.items():
            if prop not in ANIM_PROPS:
                errs.append("%s: animate.%s is not animatable%s" % (where, prop, _suggest(prop, ANIM_PROPS)))
            else:
                _check_keys(errs, where, "animate." + prop, keys)
    _check_motion(errs, where, "enter", L.get("enter"), anim.ENTER_TYPES)
    _check_motion(errs, where, "exit", L.get("exit"), anim.EXIT_TYPES)
    en = L.get("enter")
    if isinstance(en, dict) and en.get("type") in ("typewriter", "words") and t not in ("text", "counter"):
        errs.append("%s: enter %r only works on text and counter layers" % (where, en["type"]))
    sh = L.get("shadow")
    if sh is not None and errs.need(isinstance(sh, dict), where, "shadow must be {\"color\", \"dx\", \"dy\", \"blur\"}"):
        _check_color(errs, where, "shadow.color", sh.get("color"))
        for k in ("dx", "dy"):
            _check_num(errs, where, "shadow." + k, sh.get(k))
        _check_num(errs, where, "shadow.blur", sh.get("blur"), 0)

    if t in ("video", "image"):
        _check_file(errs, where, "src", L.get("src"))
        if L.get("fit") not in FITS:
            errs.append("%s: unknown fit %r%s" % (where, L.get("fit"), _suggest(L.get("fit"), FITS)))
        f = L.get("focus")
        errs.need(isinstance(f, list) and len(f) == 2 and all(_isnum(v) and 0 <= v <= 1 for v in f), where,
                  "focus must be [fx, fy] between 0 and 1, got %r" % (f,))
        _check_num(errs, where, "w", L.get("w"), 0, lo_open=True, allow_none=True)
        _check_num(errs, where, "h", L.get("h"), 0, lo_open=True, allow_none=True)
        kb = L.get("kenburns")
        if kb is not None:
            errs.need(isinstance(kb, list) and len(kb) == 2 and all(_isnum(v) and v > 0 for v in kb), where,
                      "kenburns must be [scale_from, scale_to], got %r" % (kb,))
        if t == "video":
            _check_num(errs, where, "in", L.get("in"), 0)
            _check_num(errs, where, "speed", L.get("speed"), 0, lo_open=True)
            _check_num(errs, where, "volume", L.get("volume"), 0)
            errs.need(isinstance(L.get("loop"), bool), where, "loop must be true or false")
        else:
            _check_num(errs, where, "radius", L.get("radius"), 0)
    elif t in ("text", "counter"):
        _check_text(errs, where, L)
        if t == "counter":
            _check_num(errs, where, "from", L.get("from"))
            _check_num(errs, where, "to", L.get("to"))
            errs.need(isinstance(L.get("decimals"), int) and 0 <= L["decimals"] <= 6, where,
                      "decimals must be a whole number 0..6")
            _check_num(errs, where, "count_duration", L.get("count_duration"), 0, lo_open=True)
            if L.get("ease") not in anim.EASINGS:
                errs.append("%s: unknown ease %r%s" % (where, L.get("ease"), _suggest(L.get("ease"), anim.EASINGS)))
            for k in ("prefix", "suffix", "separator"):
                errs.need(isinstance(L.get(k), str), where, "%s must be text" % k)
    elif t == "shape":
        if L.get("shape") not in SHAPES:
            errs.append("%s: unknown shape %r%s" % (where, L.get("shape"), _suggest(L.get("shape"), SHAPES)))
        _check_num(errs, where, "w", L.get("w"), 0, lo_open=True)
        _check_num(errs, where, "h", L.get("h"), 0, lo_open=True)
        _check_color(errs, where, "color", L.get("color"))
        _check_num(errs, where, "radius", L.get("radius"), 0)
        st = L.get("stroke")
        if st is not None and errs.need(isinstance(st, dict), where, "stroke must be {\"color\", \"width\"}"):
            _check_color(errs, where, "stroke.color", st.get("color"))
            _check_num(errs, where, "stroke.width", st.get("width"), 0)
        if L.get("shape") == "polygon":
            pts = L.get("points")
            errs.need(isinstance(pts, list) and len(pts) >= 3 and all(
                isinstance(p, (list, tuple)) and len(p) == 2 and all(_isnum(v) for v in p) for p in pts), where,
                "a polygon needs points [[x, y], ...] (at least 3, in 0..1 of the w x h box)")
        g = L.get("gradient")
        if g is not None and errs.need(isinstance(g, list) and len(g) >= 2, where, "gradient must be [\"#hex\", \"#hex\"]"):
            for c in g:
                _check_color(errs, where, "gradient colour", c)
        d = L.get("draw")
        if d is not None and errs.need(isinstance(d, dict), where, "draw must be {\"from\", \"to\", \"ease\"}"):
            _check_num(errs, where, "draw.from", d.get("from"), 0, 1)
            _check_num(errs, where, "draw.to", d.get("to"), 0, 1)
            if d.get("ease") not in anim.EASINGS:
                errs.append("%s: unknown draw.ease %r%s" % (where, d.get("ease"), _suggest(d.get("ease"), anim.EASINGS)))
    elif t == "progress":
        _check_num(errs, where, "w", L.get("w"), 0, lo_open=True)
        _check_num(errs, where, "h", L.get("h"), 0, lo_open=True)
        _check_color(errs, where, "color", L.get("color"))
        _check_color(errs, where, "track", L.get("track"), allow_none=True)
        _check_num(errs, where, "from", L.get("from"), 0, 1)
        _check_num(errs, where, "to", L.get("to"), 0, 1)
        _check_num(errs, where, "radius", L.get("radius"), 0)


def _check_effect(errs, i, e):
    where = "effects[%d]" % i
    if not errs.need(isinstance(e, dict), where, "an effect must be an object"):
        return
    t = e.get("type")
    where += " (%s)" % t
    if t not in EFFECT_TYPES:
        errs.append("%s: unknown effect %r%s" % (where, t, _suggest(t, EFFECT_TYPES)))
        return
    if t in TIMED_EFFECTS:
        errs.need("start" in e, where, "a %s effect needs a start time" % t)
    if "start" in e:
        _check_num(errs, where, "start", e.get("start"), 0)
        _check_num(errs, where, "duration", e.get("duration"), 0, lo_open=True)
    if t == "flash":
        _check_color(errs, where, "color", e.get("color"))
        _check_num(errs, where, "opacity", e.get("opacity"), 0, 1)
    elif t in ("shake", "glitch", "zoom-blur"):
        _check_num(errs, where, "intensity", e.get("intensity"), 0)
    elif t in ("vignette", "grain"):
        _check_num(errs, where, "strength", e.get("strength"), 0, 1)
    elif t == "grade":
        _check_num(errs, where, "contrast", e.get("contrast"), 0)
        _check_num(errs, where, "saturation", e.get("saturation"), 0)
        _check_num(errs, where, "brightness", e.get("brightness"), -1, 1)
        _check_num(errs, where, "warmth", e.get("warmth"), -1, 1)
    elif t == "letterbox":
        _check_num(errs, where, "ratio", e.get("ratio"), 0, lo_open=True)


def _check_audio(errs, i, a):
    where = "audio[%d]" % i
    if not errs.need(isinstance(a, dict), where, "an audio entry must be an object"):
        return
    has_src, has_sfx = bool(a.get("src")), bool(a.get("sfx"))
    if not errs.need(has_src != has_sfx, where, "give either \"src\" (a file) or \"sfx\" (a built-in effect name)"):
        return
    if has_sfx:
        if a["sfx"] not in sfx.NAMES:
            errs.append("%s: unknown sfx %r%s" % (where, a["sfx"], _suggest(a["sfx"], sfx.NAMES)))
    else:
        _check_file(errs, where, "src", a.get("src"))
    if a.get("role") not in AUDIO_ROLES:
        errs.append("%s: unknown role %r%s" % (where, a.get("role"), _suggest(a.get("role"), AUDIO_ROLES)))
    for k in ("start", "in", "volume", "fade_in", "fade_out"):
        _check_num(errs, where, k, a.get(k), 0)
    _check_num(errs, where, "duration", a.get("duration"), 0, lo_open=True, allow_none=True)


def validate(spec, base_dir=None):
    """Every problem with a spec, as readable sentences; [] means it can be rendered."""
    if not isinstance(spec, dict):
        return ["the spec must be a JSON object, got %s" % type(spec).__name__]
    try:
        s = normalize(spec, base_dir)
    except Exception as e:  # normalize is tolerant; this only guards against the unexpected
        return ["could not read the spec: %s" % e]
    errs = _Errors()
    errs.need(s.get("version") == SCHEMA_VERSION, "", "version must be %d, got %r" % (SCHEMA_VERSION, s.get("version")))
    for k in ("width", "height"):
        v = s.get(k)
        if errs.need(isinstance(v, int) and not isinstance(v, bool) and 16 <= v <= 8192, "",
                     "%s must be a whole number of pixels (16..8192), got %r" % (k, v)):
            errs.need(v % 2 == 0, "", "%s must be even (H.264 needs even sizes), got %d" % (k, v))
    _check_num(errs, "", "fps", s.get("fps"), 1, 120)
    _check_num(errs, "", "duration", s.get("duration"), 0, lo_open=True)
    bg = s.get("background")
    if isinstance(bg, dict):
        g = bg.get("gradient")
        if errs.need(isinstance(g, list) and len(g) >= 2, "background", "gradient must be [\"#hex\", \"#hex\"]"):
            for c in g:
                _check_color(errs, "background", "gradient colour", c)
        _check_num(errs, "background", "angle", bg.get("angle"))
    else:
        _check_color(errs, "", "background", bg)
    if errs.need(isinstance(s.get("layers"), list), "", "layers must be a list"):
        for i, L in enumerate(s["layers"]):
            _check_layer(errs, i, L, s)
    cam = s.get("camera")
    if errs.need(isinstance(cam, dict), "camera", "camera must be {\"zoom\": [...], \"x\": [...], ...}"):
        for k, keys in cam.items():
            if k not in CAMERA_PROPS:
                errs.append("camera: unknown property %r%s" % (k, _suggest(k, CAMERA_PROPS)))
            else:
                _check_keys(errs, "camera", k, keys)
        for i, k in enumerate(cam.get("zoom") or []):
            if isinstance(k, (list, tuple)) and len(k) > 1 and _isnum(k[1]) and k[1] <= 0:
                errs.append("camera: zoom[%d] must be > 0, got %r" % (i, k[1]))
    if errs.need(isinstance(s.get("effects"), list), "", "effects must be a list"):
        for i, e in enumerate(s["effects"]):
            _check_effect(errs, i, e)
    cap = s.get("captions")
    if cap is not None and errs.need(isinstance(cap, dict), "captions", "captions must be an object"):
        words = cap.get("words")
        if errs.need(isinstance(words, list), "captions", "words must be a list of {\"w\", \"s\", \"e\"}"):
            for i, w in enumerate(words):
                ok = isinstance(w, dict) and isinstance(w.get("w"), str) and _isnum(w.get("s")) and _isnum(w.get("e"))
                if not ok:
                    errs.append("captions: words[%d] must be {\"w\": text, \"s\": seconds, \"e\": seconds}, got %r" % (i, w))
                elif w["e"] < w["s"]:
                    errs.append("captions: words[%d] %r ends (%g) before it starts (%g)" % (i, w["w"], w["e"], w["s"]))
        st = cap.get("style")
        if st is not None and errs.need(isinstance(st, dict), "captions", "style must be an object of captions.py keys"):
            _check_unknown_fields(errs, "captions.style", st, CAPTION_STYLE_KEYS)
        if cap.get("style_dna") is not None:
            _check_file(errs, "captions", "style_dna", cap["style_dna"])
        errs.need(cap.get("font") is None or isinstance(cap["font"], str), "captions", "font must be a font name")
    if errs.need(isinstance(s.get("audio"), list), "", "audio must be a list"):
        for i, a in enumerate(s["audio"]):
            _check_audio(errs, i, a)
    return list(errs)


# --------------------------------------------------------------------------- files

def load(path_or_dict, base_dir=None):
    """Read a spec (path to JSON, JSON text or a dict) and normalize it.

    Relative paths resolve against base_dir, which defaults to the spec file's folder.
    """
    if isinstance(path_or_dict, dict):
        return normalize(path_or_dict, base_dir)
    text = str(path_or_dict)
    if text.lstrip().startswith("{"):
        return normalize(json.loads(text), base_dir)
    with open(text, encoding="utf-8") as f:
        spec = json.load(f)
    return normalize(spec, base_dir or os.path.dirname(os.path.abspath(text)))


def save(spec, path):
    """Write a spec as pretty JSON (folders created as needed)."""
    folder = os.path.dirname(os.path.abspath(path))
    os.makedirs(folder, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(spec, f, indent=2, ensure_ascii=False)
        f.write("\n")
