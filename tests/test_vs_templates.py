"""Tests for vstudio.templates: every template builds layers that follow the timeline
spec exactly (a strict local mirror of ARCHITECTURE.md section 1), stay inside their
time window and the canvas, keep text in the platform safe zone, and render."""

import contextlib
import importlib.util
import io
import json
import math
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from vstudio import templates  # noqa: E402

try:
    from vstudio import timeline
except ImportError:
    timeline = None

HAVE_RENDER = importlib.util.find_spec("vstudio.render") is not None and shutil.which("ffmpeg") is not None

# ----------------------------------------------------------------------------- the contract, spelled out

LAYER_TYPES = {"video", "image", "text", "shape", "progress", "counter"}
COMMON = {"type", "id", "start", "end", "x", "y", "anchor", "opacity", "scale", "rotation", "animate", "enter",
          "exit", "fixed", "blend", "shadow"}
TEXT = {"text", "font", "size", "color", "case", "stroke", "box", "highlight", "max_width", "align", "line_spacing",
        "letter_spacing", "word_times", "gradient"}
FIELDS = {
    "video": {"src", "in", "speed", "fit", "focus", "w", "h", "volume", "kenburns", "loop"},
    "image": {"src", "fit", "w", "h", "kenburns", "radius"},
    "text": TEXT,
    "shape": {"shape", "w", "h", "color", "radius", "stroke", "points", "gradient", "draw"},
    "progress": {"w", "h", "color", "track", "from", "to"},
    "counter": (TEXT - {"text"}) | {"from", "to", "decimals", "prefix", "suffix", "separator", "count_duration",
                                     "ease"},
}
ANCHORS = {"center", "top-left", "top", "top-right", "left", "right", "bottom-left", "bottom", "bottom-right"}
ANCHOR_FX = {"center": 0.5, "top": 0.5, "bottom": 0.5, "top-left": 0, "left": 0, "bottom-left": 0,
             "top-right": 1, "right": 1, "bottom-right": 1}
ENTERS = {"none", "fade", "pop", "slide-up", "slide-down", "slide-left", "slide-right", "zoom-in", "zoom-out", "drop",
          "bounce", "stamp", "blur-in", "wipe-right", "wipe-left", "wipe-up", "typewriter", "words", "crossfade",
          "whip-left", "whip-right", "glitch"}
EXITS = {"none", "fade", "pop-out", "slide-up", "slide-down", "slide-left", "slide-right", "zoom-in", "zoom-out",
         "blur-out", "wipe-right", "wipe-left", "whip-left", "whip-right", "glitch"}
EASINGS = {"linear", "in-quad", "out-quad", "in-out-quad", "in-cubic", "out-cubic", "in-out-cubic", "in-quart",
           "out-quart", "in-out-quart", "in-expo", "out-expo", "in-out-expo", "out-back", "in-back", "in-out-back",
           "out-elastic", "out-bounce", "step"}
ANIM_PROPS = {"x", "y", "opacity", "scale", "rotation", "blur"}
SHAPES = {"rect", "circle", "ellipse", "line", "polygon", "ring"}
CASES = {"none", "upper", "lower", "title"}
ALIGNS = {"left", "center", "right"}
BLENDS = {"normal", "add", "screen", "multiply"}
FITS = {"cover", "contain", "fill"}

CANVASES = {"9:16": (1080, 1920), "16:9": (1920, 1080), "1:1": (1080, 1080), "4:5": (1080, 1350)}
# decorative layers allowed to reach outside the safe zone (they carry no reading text)
SAFE_EXEMPT = {"big_number_bg"}
EPS = 1e-6


def is_num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def is_color(c):
    import re
    return isinstance(c, str) and re.match(r"^#(?:[0-9A-Fa-f]{6}|[0-9A-Fa-f]{8})$", c) is not None


def check_keys(errs, where, keys, life, prop):
    if not isinstance(keys, list) or not keys:
        errs.append("%s: animate.%s must be a non-empty list" % (where, prop))
        return
    last = -1.0
    for k in keys:
        if not (isinstance(k, list) and len(k) in (2, 3) and is_num(k[0]) and is_num(k[1])):
            errs.append("%s: bad key %r in animate.%s" % (where, k, prop))
            continue
        if len(k) == 3 and k[2] not in EASINGS:
            errs.append("%s: unknown easing %r" % (where, k[2]))
        if k[0] < last - EPS:
            errs.append("%s: animate.%s keys not sorted" % (where, prop))
        if k[0] < -EPS or k[0] > life + 1e-3:
            errs.append("%s: animate.%s key time %s outside the layer life %s" % (where, prop, k[0], life))
        if prop == "opacity" and not -EPS <= k[1] <= 1 + EPS:
            errs.append("%s: opacity key %s outside 0..1" % (where, k[1]))
        if prop == "scale" and k[1] <= 0:
            errs.append("%s: scale key %s must be > 0" % (where, k[1]))
        last = k[0]


def text_words(layer):
    return templates._apply_case(str(layer.get("text", "")), layer.get("case", "none")).split()


def shown_width(layer):
    """Upper bound of the drawn width of a text/counter layer (the longest it could be on one line)."""
    font, size, lsp = layer["font"], layer["size"], layer.get("letter_spacing", 0)
    if layer["type"] == "counter":
        dec, sep = layer.get("decimals", 0), layer.get("separator", ",")
        longest = max((templates._fmt_number(v, dec, sep) for v in (layer["from"], layer["to"])), key=len)
        one = layer.get("prefix", "") + longest + layer.get("suffix", "")
    else:
        one = " ".join(text_words(layer))
    est = templates.text_width(one, font, size, lsp)
    return min(layer["max_width"], est) if layer.get("max_width") else est


def check_layer(L, W, H, t0, t1):
    """Every way a layer can break the contract, as readable strings."""
    errs = []
    where = "%s (%s)" % (L.get("id"), L.get("type"))
    t = L.get("type")
    if t not in LAYER_TYPES:
        return ["%s: unknown type" % where]
    extra = set(L) - COMMON - FIELDS[t]
    if extra:
        errs.append("%s: fields outside the contract: %s" % (where, sorted(extra)))
    if not isinstance(L.get("id"), str) or not L["id"]:
        errs.append("%s: id should be a non-empty label" % where)
    s, e = L.get("start"), L.get("end")
    if not (is_num(s) and is_num(e)):
        return errs + ["%s: start/end must be numbers" % where]
    if s < t0 - 1e-3 or e > t1 + 1e-3 or e <= s:
        errs.append("%s: [%s, %s] not inside the window [%s, %s]" % (where, s, e, t0, t1))
    life = e - s
    for k in ("x", "y"):
        if not is_num(L.get(k)):
            errs.append("%s: %s must be a number" % (where, k))
    if is_num(L.get("x")) and is_num(L.get("y")):
        if not (-1 <= L["x"] <= W + 1 and -1 <= L["y"] <= H + 1):
            errs.append("%s: anchor (%s, %s) outside the %sx%s canvas" % (where, L["x"], L["y"], W, H))
    if L.get("anchor", "center") not in ANCHORS:
        errs.append("%s: bad anchor %r" % (where, L.get("anchor")))
    if "opacity" in L and not (is_num(L["opacity"]) and 0 <= L["opacity"] <= 1):
        errs.append("%s: opacity must be 0..1" % where)
    if "scale" in L and not (is_num(L["scale"]) and L["scale"] > 0):
        errs.append("%s: scale must be > 0" % where)
    if "rotation" in L and not is_num(L["rotation"]):
        errs.append("%s: rotation must be a number" % where)
    if "fixed" in L and not isinstance(L["fixed"], bool):
        errs.append("%s: fixed must be a bool" % where)
    if L.get("blend", "normal") not in BLENDS:
        errs.append("%s: bad blend" % where)
    en, ex = L.get("enter"), L.get("exit")
    used = 0.0
    if en is not None:
        if not (isinstance(en, dict) and set(en) <= {"type", "duration", "delay"} and en.get("type") in ENTERS):
            errs.append("%s: bad enter %r" % (where, en))
        else:
            if not (is_num(en.get("duration")) and en["duration"] > 0 and is_num(en.get("delay", 0))
                    and en.get("delay", 0) >= 0):
                errs.append("%s: enter needs duration > 0 and delay >= 0" % where)
            else:
                used += en["duration"] + en.get("delay", 0)
            if en["type"] in ("typewriter", "words") and t not in ("text", "counter"):
                errs.append("%s: %s only applies to text" % (where, en["type"]))
    if ex is not None:
        if not (isinstance(ex, dict) and set(ex) <= {"type", "duration"} and ex.get("type") in EXITS):
            errs.append("%s: bad exit %r" % (where, ex))
        elif not (is_num(ex.get("duration")) and ex["duration"] > 0):
            errs.append("%s: exit needs duration > 0" % where)
        else:
            used += ex["duration"]
    if used > life + 1e-3:
        errs.append("%s: enter + exit (%.3f s) longer than the layer (%.3f s)" % (where, used, life))
    an = L.get("animate", {})
    if not isinstance(an, dict) or not set(an) <= ANIM_PROPS:
        errs.append("%s: animate props must be among %s" % (where, sorted(ANIM_PROPS)))
    else:
        for prop, keys in an.items():
            check_keys(errs, where, keys, life, prop)
    sh = L.get("shadow")
    if sh is not None and not (isinstance(sh, dict) and set(sh) <= {"color", "dx", "dy", "blur"}
                               and is_color(sh.get("color")) and sh.get("blur", 0) >= 0):
        errs.append("%s: bad shadow %r" % (where, sh))

    if t in ("text", "counter"):
        if t == "text" and not (isinstance(L.get("text"), str) and L["text"].strip()):
            errs.append("%s: empty text" % where)
        if not (isinstance(L.get("font"), str) and L["font"]):
            errs.append("%s: font missing" % where)
        if not (is_num(L.get("size")) and L["size"] > 0):
            errs.append("%s: size must be > 0" % where)
        if not is_color(L.get("color")):
            errs.append("%s: bad color %r" % (where, L.get("color")))
        if L.get("case", "none") not in CASES or L.get("align", "center") not in ALIGNS:
            errs.append("%s: bad case/align" % where)
        if "max_width" in L and not (is_num(L["max_width"]) and L["max_width"] > 0):
            errs.append("%s: max_width must be > 0" % where)
        for k in ("line_spacing", "letter_spacing"):
            if k in L and not is_num(L[k]):
                errs.append("%s: %s must be a number" % (where, k))
        st = L.get("stroke")
        if st is not None and not (isinstance(st, dict) and set(st) == {"color", "width"} and is_color(st["color"])
                                   and is_num(st["width"]) and st["width"] >= 0):
            errs.append("%s: bad stroke %r" % (where, st))
        boxes = [("box", L.get("box"))]
        hl = L.get("highlight")
        if hl is not None:
            if not (isinstance(hl, dict) and set(hl) <= {"words", "color", "box"} and is_color(hl.get("color"))):
                errs.append("%s: bad highlight %r" % (where, hl))
            else:
                n = len(text_words(L))
                for w in hl.get("words", []):
                    if not (isinstance(w, int) and 0 <= w < n):
                        errs.append("%s: highlight index %r outside the %d words" % (where, w, n))
                boxes.append(("highlight.box", hl.get("box")))
        for name, box in boxes:
            if box is not None and not (isinstance(box, dict) and set(box) == {"color", "pad", "radius"}
                                        and is_color(box["color"]) and isinstance(box["pad"], list)
                                        and len(box["pad"]) == 2 and all(is_num(v) and v >= 0 for v in box["pad"])
                                        and is_num(box["radius"]) and box["radius"] >= 0):
                errs.append("%s: bad %s %r" % (where, name, box))
        wt = L.get("word_times")
        if wt is not None:
            n = len(text_words(L))
            if not (isinstance(wt, list) and len(wt) == n and all(is_num(v) and -EPS <= v <= life for v in wt)
                    and wt == sorted(wt)):
                errs.append("%s: word_times must be %d sorted times inside the life" % (where, n))
        g = L.get("gradient")
        if g is not None and not (isinstance(g, list) and len(g) >= 2 and all(is_color(c) for c in g)):
            errs.append("%s: bad gradient" % where)
        if t == "counter":
            if not (is_num(L.get("from")) and is_num(L.get("to"))):
                errs.append("%s: counter from/to must be numbers" % where)
            if not (isinstance(L.get("decimals"), int) and 0 <= L["decimals"] <= 6):
                errs.append("%s: decimals must be 0..6" % where)
            if not (is_num(L.get("count_duration")) and 0 < L["count_duration"] <= life + 1e-3):
                errs.append("%s: count_duration must fit the life" % where)
            if L.get("ease") not in EASINGS:
                errs.append("%s: bad counter ease" % where)
            for k in ("prefix", "suffix", "separator"):
                if not isinstance(L.get(k), str):
                    errs.append("%s: %s must be text" % (where, k))
    elif t == "shape":
        if L.get("shape") not in SHAPES:
            errs.append("%s: bad shape %r" % (where, L.get("shape")))
        if not (is_num(L.get("w")) and is_num(L.get("h")) and L["w"] > 0 and L["h"] > 0):
            errs.append("%s: w/h must be > 0" % where)
        if not is_color(L.get("color")):
            errs.append("%s: bad color" % where)
        if "radius" in L and not (is_num(L["radius"]) and L["radius"] >= 0):
            errs.append("%s: bad radius" % where)
        st = L.get("stroke")
        if st is not None and not (isinstance(st, dict) and set(st) == {"color", "width"} and is_color(st["color"])):
            errs.append("%s: bad stroke" % where)
        if L.get("shape") == "ring" and not (st and st["width"] > 0):
            errs.append("%s: a ring needs stroke.width" % where)
        if L.get("shape") == "polygon":
            pts = L.get("points")
            if not (isinstance(pts, list) and len(pts) >= 3 and all(
                    isinstance(p, list) and len(p) == 2 and all(is_num(v) and -EPS <= v <= 1 + EPS for v in p)
                    for p in pts)):
                errs.append("%s: polygon points must be >= 3 pairs in 0..1" % where)
        elif "points" in L:
            errs.append("%s: points only belong on polygons" % where)
        d = L.get("draw")
        if d is not None and not (isinstance(d, dict) and set(d) == {"from", "to", "ease"} and d["ease"] in EASINGS
                                  and 0 <= d["from"] <= 1 and 0 <= d["to"] <= 1):
            errs.append("%s: bad draw %r" % (where, d))
        g = L.get("gradient")
        if g is not None and not (isinstance(g, list) and len(g) >= 2 and all(is_color(c) for c in g)):
            errs.append("%s: bad gradient" % where)
    elif t == "progress":
        if not (is_num(L.get("w")) and is_num(L.get("h")) and L["w"] > 0 and L["h"] > 0):
            errs.append("%s: w/h must be > 0" % where)
        if not (is_color(L.get("color")) and is_color(L.get("track"))):
            errs.append("%s: bad colors" % where)
        if not (0 <= L.get("from", -1) <= 1 and 0 <= L.get("to", -1) <= 1):
            errs.append("%s: from/to must be 0..1" % where)
    elif t == "image":
        if not isinstance(L.get("src"), str) or L.get("fit") not in FITS:
            errs.append("%s: image needs src and fit" % where)
    return errs


def check_safe(name, L, W, H):
    """Reading text keeps to the safe zone (anchor) and its drawn width stays inside the side margins."""
    if name in SAFE_EXEMPT or L["type"] not in ("text", "counter"):
        return []
    fr = templates._Frame({"width": W, "height": H})
    tol = 2.0
    errs = []
    width = shown_width(L)
    fx = ANCHOR_FX[L.get("anchor", "center")]
    left = L["x"] - fx * width
    right = left + width
    if left < fr.L - tol or right > fr.R + tol:
        errs.append("%s: text spans x %.0f..%.0f outside the safe zone %.0f..%.0f" % (L["id"], left, right, fr.L, fr.R))
    if not fr.T - tol <= L["y"] <= fr.B + tol:
        errs.append("%s: text anchor y %.0f outside the safe zone %.0f..%.0f" % (L["id"], L["y"], fr.T, fr.B))
    return errs


def build_all(canvas_key="9:16", start=1.0, end=4.0, energy="high", params=None):
    W, H = CANVASES[canvas_key]
    for name in templates.CATALOG:
        layers = templates.build(name, (params or {}).get(name, {}), start, end,
                                 {"width": W, "height": H, "fps": 30}, {"energy": energy})
        yield name, layers, W, H


class TestCatalog(unittest.TestCase):
    REQUIRED = ["hook_title", "kinetic_text", "stat", "counter_reveal", "list_item", "checklist", "quote",
                "lower_third", "cta", "progress_bar", "versus", "before_after", "highlight_box", "arrow", "countdown",
                "title_card", "emphasis_word", "icon_badge", "subscribe", "timeline_steps", "comment_bubble",
                "ranking", "question", "source_card"]

    def test_required_templates_exist(self):
        missing = [n for n in self.REQUIRED if n not in templates.CATALOG]
        self.assertEqual(missing, [])
        self.assertGreaterEqual(len(templates.CATALOG), 40)

    def test_entries_are_documented(self):
        for name, e in templates.CATALOG.items():
            self.assertTrue(len(e["description"]) > 40, name)
            self.assertTrue(callable(e["fn"]), name)
            for pn, ps in e["params"].items():
                self.assertTrue({"type", "default", "doc"} <= set(ps), (name, pn))
                self.assertTrue(ps["doc"], (name, pn))
                if "choices" in ps:
                    self.assertIn(ps["default"], ps["choices"], (name, pn))

    def test_default_style_matches_contract(self):
        self.assertEqual(set(templates.DEFAULT_STYLE),
                         {"font", "font_secondary", "text", "accent", "bg", "bg2", "energy"})

    def test_catalog_doc_mentions_every_template_and_param(self):
        doc = templates.catalog_doc()
        for name, e in templates.CATALOG.items():
            self.assertIn("**%s**" % name, doc)
            for pn in e["params"]:
                self.assertIn("`%s`" % pn, doc, (name, pn))

    def test_icons(self):
        for name in ("heart", "star", "check", "play", "bell", "arrow", "flame", "lightning", "eye", "comment",
                     "share", "bookmark", "plus"):
            self.assertIn(name, templates.ICONS)
        for name, parts in templates.ICONS.items():
            self.assertTrue(parts, name)
            for part in parts:
                self.assertIn(part["role"], ("fill", "cut"))
                self.assertGreaterEqual(len(part["points"]), 3)
                for x, y in part["points"]:
                    self.assertTrue(0 <= x <= 1 and 0 <= y <= 1, name)


class TestLayersFollowTheContract(unittest.TestCase):
    def assert_clean(self, name, layers, W, H, t0, t1, safe=True):
        self.assertIsInstance(layers, list, name)
        self.assertTrue(layers, "%s built no layers" % name)
        json.dumps(layers)  # plain JSON data
        errs = []
        for L in layers:
            errs += check_layer(L, W, H, t0, t1)
            if safe:
                errs += check_safe(name, L, W, H)
        self.assertEqual(errs, [], "%s at %dx%d:\n  %s" % (name, W, H, "\n  ".join(errs)))

    def test_defaults_on_every_aspect(self):
        for key in CANVASES:
            for name, layers, W, H in build_all(key):
                with self.subTest(template=name, aspect=key):
                    self.assert_clean(name, layers, W, H, 1.0, 4.0)

    def test_every_energy(self):
        for energy in ("high", "medium", "calm"):
            for name, layers, W, H in build_all("9:16", energy=energy):
                with self.subTest(template=name, energy=energy):
                    self.assert_clean(name, layers, W, H, 1.0, 4.0)

    def test_short_and_long_windows(self):
        for t0, t1 in ((2.0, 2.6), (0.0, 1.2), (3.0, 13.0)):
            for key in ("9:16", "16:9"):
                for name, layers, W, H in build_all(key, t0, t1):
                    with self.subTest(template=name, window=(t0, t1), aspect=key):
                        self.assert_clean(name, layers, W, H, t0, t1)

    def test_small_draft_canvas(self):
        W, H = 216, 384
        for name in templates.CATALOG:
            with self.subTest(template=name):
                layers = templates.build(name, {}, 0, 3, {"width": W, "height": H, "fps": 10})
                self.assert_clean(name, layers, W, H, 0, 3)

    def test_long_text_still_fits(self):
        long = "this is a deliberately long line of text that a planner might send to see what happens"
        params = {"hook_title": {"text": long}, "kinetic_text": {"text": long}, "quote": {"text": long * 2},
                  "comment_bubble": {"text": long}, "post_card": {"text": long * 2},
                  "list_item": {"text": long, "sub": long}, "emphasis_word": {"word": "UNBELIEVABLEMENTE"},
                  "checklist": {"items": [long] * 7, "title": long}, "ranking": {"items": [long] * 6},
                  "text_message": {"messages": ["them: " + long, "me: " + long] * 4},
                  "stat": {"value": "$1,234,567,890", "label": long}, "word_stack": {"words": long.split()[:7]},
                  "title_card": {"title": long, "kicker": long, "sub": long, "number": "01"},
                  "notification_pop": {"text": long}, "search_bar": {"query": long, "suggestions": [long] * 4},
                  "poll": {"question": long, "options": [long] * 4, "results": [1, 2, 3, 4]},
                  "pros_cons": {"pros": [long] * 4, "cons": [long] * 4, "title": long},
                  "myth_vs_fact": {"myth": long, "fact": long}, "versus": {"left": long, "right": long},
                  "cta": {"text": long, "sub": long}, "lower_third": {"name": long, "title": long},
                  "source_card": {"title": long, "detail": long}, "timeline_steps": {"steps": [long] * 6},
                  "bar_chart": {"labels": [long] * 7, "values": [1, 5, 9, 2, 3, 4, 8]},
                  "question": {"text": long, "options": [long] * 4, "sub": long}}
        for key in ("9:16", "16:9", "1:1"):
            for name, layers, W, H in build_all(key, 0, 6, params=params):
                with self.subTest(template=name, aspect=key):
                    self.assert_clean(name, layers, W, H, 0, 6)

    @unittest.skipUnless(timeline is not None and hasattr(timeline, "validate"), "timeline.validate not available")
    def test_timeline_validate_accepts_every_template(self):
        for key in ("9:16", "16:9", "1:1"):
            W, H = CANVASES[key]
            for name in templates.CATALOG:
                with self.subTest(template=name, aspect=key):
                    spec = templates.preview_spec(name, {}, aspect=key, duration=3.0)
                    self.assertEqual(timeline.validate(spec), [])

    def test_text_layers_use_style_fonts_and_colours(self):
        style = {"font": "Anton", "font_secondary": "Inter Medium", "text": "#101010", "accent": "#FF3366",
                 "bg": "#FAFAFA", "bg2": "#EEEEEE", "energy": "medium"}
        layers = templates.build("hook_title", {"text": "MONEY TALK", "highlight": ["money"]}, 0, 3, None, style)
        title = [L for L in layers if L["id"] == "hook_title.title"][0]
        self.assertEqual(title["font"], "Anton")
        self.assertEqual(title["color"], "#101010")
        self.assertEqual(title["highlight"]["words"], [0])
        self.assertEqual(title["highlight"]["color"], "#FF3366")
        body = templates.build("stat", {}, 0, 3, None, style)
        self.assertIn("Inter Medium", [L.get("font") for L in body])


class TestTimingAndMotion(unittest.TestCase):
    def test_entrances_are_staggered(self):
        layers = templates.build("checklist", {"items": ["a", "b", "c", "d"]}, 0, 4, None)
        items = sorted(L["start"] for L in layers if L["id"].startswith("checklist.item"))
        self.assertEqual(len(items), 4)
        self.assertTrue(all(b > a for a, b in zip(items, items[1:])), items)

    def test_exits_finish_by_the_end(self):
        for name, layers, W, H in build_all("9:16", 0, 3):
            for L in layers:
                self.assertLessEqual(L["end"], 3.0 + 1e-6, (name, L["id"]))

    def test_energy_changes_snappiness(self):
        def first_enter(energy):
            L = templates.build("hook_title", {}, 0, 3, None, {"energy": energy})
            title = [x for x in L if x["id"] == "hook_title.title"][0]
            return title["enter"]
        hi, calm = first_enter("high"), first_enter("calm")
        self.assertEqual(hi["type"], "stamp")
        self.assertEqual(calm["type"], "blur-in")
        self.assertLess(hi["duration"], calm["duration"])
        # calm means no overshoot anywhere: no out-back / bounce keys
        for name, layers, W, H in build_all("9:16", energy="calm"):
            for L in layers:
                for keys in L.get("animate", {}).values():
                    self.assertFalse(any(len(k) > 2 and k[2] in ("out-back", "out-elastic", "out-bounce")
                                         for k in keys), (name, L["id"]))

    def test_group_moves_rigidly(self):
        """Members of a card scale about one pivot: at every key the distances between anchors scale together."""
        layers = templates.build("list_item", {}, 0, 3, None, {"energy": "high"})
        card = [L for L in layers if L["id"] == "list_item.card"][0]
        text = [L for L in layers if L["id"] == "list_item.text"][0]
        for kc, kt, ks in zip(card["animate"]["x"], text["animate"]["x"], card["animate"]["scale"]):
            gap_now = kt[1] - kc[1]
            gap_rest = text["x"] - card["x"]
            self.assertAlmostEqual(gap_now, gap_rest * ks[1], delta=1.0)

    def test_icon_parts_move_together(self):
        """Polygons sharing one icon box (same place, size and timing) must carry identical motion."""
        for icon in ("flame", "bell", "eye", "comment", "warning", "search"):
            params = {"icon_badge": {"icon": icon}, "cta": {"icon": icon}, "notification_pop": {"icon": icon},
                      "like_burst": {"icon": icon}}
            for name, layers, W, H in build_all("9:16", params=params):
                boxes = {}
                for L in layers:
                    if L.get("shape") == "polygon":
                        key = (L["x"], L["y"], L["w"], L["h"], L["start"], L["end"])
                        boxes.setdefault(key, []).append(L)
                for group in boxes.values():
                    first = group[0]
                    for other in group[1:]:
                        self.assertEqual(first.get("animate"), other.get("animate"), (name, icon, other["id"]))
                        self.assertEqual(first.get("enter"), other.get("enter"), (name, icon, other["id"]))

    def test_offsets_become_absolute_positions(self):
        layers = templates.build("icon_badge", {}, 0, 3, None, {"energy": "high"})
        badge = [L for L in layers if L["id"] == "icon_badge.badge"][0]
        ys = [k[1] for k in badge["animate"]["y"]]
        self.assertTrue(all(abs(y - badge["y"]) < 40 for y in ys), ys)

    def test_kinetic_times_sync_to_words(self):
        times = [5.0, 5.3, 5.5, 6.1]
        L = templates.build("kinetic_text", {"text": "one two three four", "times": times}, 5.0, 8.0, None)[0]
        self.assertEqual(L["word_times"], [0.0, 0.3, 0.5, 1.1])
        punch = templates.build("kinetic_text", {"text": "one two three four", "times": times, "mode": "punch"},
                                5.0, 8.0, None)
        starts = [L["start"] for L in punch]
        self.assertEqual(starts[0], 5.0)
        ends = [L["end"] for L in punch]
        self.assertTrue(all(e <= s2 + 1e-6 for e, s2 in zip(ends, starts[1:])), "chunks must not overlap")

    def test_short_window_compresses_animation(self):
        long = templates.build("stat", {}, 0, 3, None)
        short = templates.build("stat", {}, 0, 0.8, None)
        d_long = [L for L in long if L["id"] == "stat.label"][0]["enter"]["duration"]
        d_short = [L for L in short if L["id"] == "stat.label"][0]["enter"]["duration"]
        self.assertLess(d_short, d_long)


class TestParams(unittest.TestCase):
    def test_coercion_and_fallbacks(self):
        layers = templates.build("checklist", {"items": "one | two | three", "position": "nowhere"}, 0, 3, None)
        items = [L["text"] for L in layers if L["id"].startswith("checklist.item")]
        self.assertEqual(items, ["one", "two", "three"])
        counter = templates.build("counter_reveal", {"value": "2500", "decimals": "1", "prefix": "$"}, 0, 3, None)
        c = [L for L in counter if L["type"] == "counter"][0]
        self.assertEqual((c["to"], c["decimals"], c["prefix"]), (2500.0, 1, "$"))
        bad = templates.build("stat", {"color": "red", "value": None}, 0, 3, None)
        self.assertEqual([L for L in bad if L["id"] == "stat.value"][0]["text"], "73%")

    def test_highlight_by_word_phrase_or_index(self):
        f = templates._highlight_idx
        self.assertEqual(f("I lost $300 today", ["$300"], "upper"), [2])
        self.assertEqual(f("nobody tells you this", ["tells you"], "none"), [1, 2])
        self.assertEqual(f("a b c", [2, 9], "none"), [2])

    def test_blank_params_never_break_or_invent_text(self):
        """Blank strings and empty lists give valid layers (maybe none) and no placeholder words."""
        for name, e in templates.CATALOG.items():
            blank = {}
            for pn, ps in e["params"].items():
                if ps["type"] == "str":
                    blank[pn] = ""
                elif ps["type"].startswith("list") or ps["type"] == "highlight":
                    blank[pn] = []
            for key in ("9:16", "16:9"):
                W, H = CANVASES[key]
                with self.subTest(template=name, aspect=key):
                    layers = templates.build(name, blank, 0, 3, {"width": W, "height": H, "fps": 30})
                    errs = []
                    for L in layers:
                        errs += check_layer(L, W, H, 0, 3)
                        if L["type"] == "text":
                            self.assertNotIn(L["text"].strip().lower(), ("item", "...", "start", "finish", "yes"))
                    self.assertEqual(errs, [], name)
                    if timeline is not None and layers:
                        spec = {"version": 1, "width": W, "height": H, "fps": 30, "duration": 3, "layers": layers}
                        self.assertEqual(timeline.validate(spec), [], name)

    def test_each_param_blank_or_odd(self):
        """One param at a time set to a blank or extreme value still gives valid, on-canvas layers."""
        odd = {"str": ["", "x" * 80], "int": [0, -5, 999], "float": [0.0, -3.0, 1e6], "bool": [False],
               "list[str]": [[], ["a"] * 20], "list[float]": [[], [0, 0, 0]], "highlight": [[], [99]],
               "color": ["", "accent", "#12345678"]}
        for name, e in templates.CATALOG.items():
            for pn, ps in e["params"].items():
                values = odd.get(ps["type"], []) + list(ps.get("choices", []))
                for v in values:
                    for key in ("9:16", "16:9"):
                        W, H = CANVASES[key]
                        with self.subTest(template=name, param=pn, value=str(v)[:20], aspect=key):
                            layers = templates.build(name, {pn: v}, 0, 3, {"width": W, "height": H, "fps": 30})
                            errs = []
                            for L in layers:
                                errs += check_layer(L, W, H, 0, 3)
                            self.assertEqual(errs, [])

    def test_errors(self):
        with self.assertRaises(KeyError):
            templates.build("no_such_template", {}, 0, 1, None)
        with self.assertRaises(ValueError):
            templates.build("stat", {}, 2.0, 2.0, None)

    def test_deterministic(self):
        a = templates.build("like_burst", {}, 0, 3, None)
        b = templates.build("like_burst", {}, 0, 3, None)
        self.assertEqual(a, b)

    def test_works_without_the_font_engine(self):
        """With no font module the conservative width table still yields valid layouts."""
        saved = templates._font_obj
        templates._FONT_OBJ.clear()
        templates._WIDTH_CACHE.clear()
        templates._font_obj = lambda font: None
        try:
            for name, layers, W, H in build_all("9:16"):
                errs = []
                for L in layers:
                    errs += check_layer(L, W, H, 1.0, 4.0) + check_safe(name, L, W, H)
                self.assertEqual(errs, [], name)
        finally:
            templates._font_obj = saved
            templates._WIDTH_CACHE.clear()

    def test_fit_text_keeps_words_inside(self):
        blk = templates.fit_text("SUPERCALIFRAGILISTIC", "Montserrat Black", 200, 400, lines=1)
        self.assertLessEqual(blk.w, 400)
        two = templates.fit_text("NOBODY TELLS YOU THIS", "Montserrat Black", 120, 900, lines=3, prefer=2)
        self.assertEqual(len(two.lines), 2)


# ----------------------------------------------------------------------------- design regressions

def _segments_cross(p1, p2, p3, p4):
    """True when segments p1p2 and p3p4 properly cross (touching ends do not count)."""
    def orient(a, b, c):
        return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])
    d1, d2 = orient(p3, p4, p1), orient(p3, p4, p2)
    d3, d4 = orient(p1, p2, p3), orient(p1, p2, p4)
    return d1 * d2 < -1e-12 and d3 * d4 < -1e-12


def self_intersections(pts):
    """Pairs of non-adjacent edges of a closed polygon that cross each other."""
    n, out = len(pts), []
    for i in range(n):
        for j in range(i + 2, n):
            if i == 0 and j == n - 1:
                continue
            if _segments_cross(pts[i], pts[(i + 1) % n], pts[j], pts[(j + 1) % n]):
                out.append((i, j))
    return out


def layer_state(L, t, W, H, fps=30):
    """anim.state_at for a layer at timeline second t (None while the layer is not on screen)."""
    from vstudio import anim
    if not L["start"] <= t < L["end"]:
        return None
    return anim.state_at(L, t - L["start"], (W, H), fps)


def text_box(L, st=None):
    """(x0, y0, x1, y1) of a text layer's layout box (the fitted lines), at its state when given."""
    blk = templates.fit_text(L["text"], L["font"], L["size"], L["max_width"], 20, L["size"], L.get("case", "none"),
                             L.get("line_spacing", 1.05), L.get("letter_spacing", 0))
    s = st["scale"] if st else 1.0
    x, y = (st["x"], st["y"]) if st else (L["x"], L["y"])
    w, h = blk.w * s, blk.h * s
    fx = ANCHOR_FX[L.get("anchor", "center")]
    a = L.get("anchor", "center")
    fy = 0.0 if a.startswith("top") else 1.0 if a.startswith("bottom") else 0.5
    return (x - fx * w, y - fy * h, x - fx * w + w, y - fy * h + h)


class TestDesign(unittest.TestCase):
    """Things that built valid layers but looked broken on screen."""

    def test_arrow_is_one_simple_polygon(self):
        """The shaft stops at the head's base: the outline never folds back over itself."""
        dirs = templates.CATALOG["arrow"]["params"]["direction"]["choices"]
        for key in ("9:16", "16:9"):
            W, H = CANVASES[key]
            for d in dirs:
                for curve in (True, False):
                    for length in (80, 340, 900):
                        with self.subTest(aspect=key, direction=d, curve=curve, length=length):
                            layers = templates.build("arrow", {"direction": d, "curve": curve, "length": length},
                                                     0, 3, {"width": W, "height": H, "fps": 30})
                            arrow = [L for L in layers if L["id"] == "arrow.arrow"][0]
                            self.assertEqual(self_intersections(arrow["points"]), [])
        self.assertEqual(self_intersections(templates.ICONS["trend"][0]["points"]), [])
        # a path shorter than the head still gives a clean head
        stub = templates._arrow_path([[0.1, 0.5], [0.2, 0.5]], 0.1, 0.25, 0.34)
        self.assertEqual(self_intersections(stub), [])

    def test_countdown_never_flashes_an_empty_frame(self):
        """From the first number landing to the last exit, a number (or GO) is always clearly on screen."""
        for fps in (24, 25, 30, 60):
            for t0, t1 in ((0.0, 3.0), (0.0, 4.0), (1.0, 3.4), (0.37, 2.97), (0.0, 1.5), (2.0, 8.0)):
                for energy in ("high", "medium", "calm"):
                    for go in ("GO!", ""):
                        layers = templates.build("countdown", {"go": go}, t0, t1,
                                                 {"width": 1080, "height": 1920, "fps": fps}, {"energy": energy})
                        shown = [L for L in layers if L["id"].startswith(("countdown.number", "countdown.go"))]
                        first, last = shown[0], shown[-1]
                        settle = first["start"] + first["enter"]["duration"] + first["enter"].get("delay", 0)
                        leave = last["end"] - (last["exit"]["duration"] if last.get("exit") else 0)
                        k = int(math.ceil(settle * fps))
                        while k / fps < leave - 1e-9:
                            t = k / fps
                            states = [layer_state(L, t, 1080, 1920, fps) for L in shown]
                            total = sum(s["opacity"] for s in states if s)
                            with self.subTest(fps=fps, window=(t0, t1), energy=energy, go=go, t=round(t, 3)):
                                self.assertGreaterEqual(total, 0.6)
                            k += 1

    def test_ranking_rows_share_one_text_size(self):
        items = ["Hooks that open a loop", "Pacing under 2 seconds per shot", "Captions", "Music",
                 "Thumbnails and covers"]
        for key in ("9:16", "16:9", "1:1"):
            W, H = CANVASES[key]
            for its in (items, ["Hooks", "Pacing", "Captions"], ["a much longer winner than the others", "b"]):
                with self.subTest(aspect=key, items=its):
                    layers = templates.build("ranking", {"items": its}, 0, 3, {"width": W, "height": H, "fps": 30})
                    names = [L for L in layers if L["id"].startswith("ranking.name")]
                    self.assertEqual(len(names), len(its))
                    self.assertEqual(len({L["size"] for L in names}), 1, [(L["text"], L["size"]) for L in names])
                    cards = [L for L in layers if L["id"].startswith("ranking.card")]
                    for name, card in zip(names, cards):
                        x0, y0, x1, y1 = text_box(name)
                        self.assertLessEqual(y1 - y0, card["h"] - 2, name["text"])

    def test_poll_parts_move_with_the_card(self):
        """Result fills, counters and labels scale with the card on the way in and out."""
        W, H = CANVASES["9:16"]
        for energy in ("high", "medium", "calm"):
            layers = templates.build("poll", {}, 0, 3, {"width": W, "height": H, "fps": 30}, {"energy": energy})
            card = [L for L in layers if L["id"] == "poll.card"][0]
            parts = [L for L in layers if L["id"].split(".")[1].rstrip("0123456789") in ("fill", "pct", "option")]
            self.assertEqual(len(parts), 6)
            for L in parts:
                for k in range(90):
                    t = k / 30.0
                    sc, sl = layer_state(card, t, W, H), layer_state(L, t, W, H)
                    if not sc or not sl or sl["opacity"] < 0.05:
                        continue
                    with self.subTest(energy=energy, layer=L["id"], t=round(t, 3)):
                        self.assertAlmostEqual(sl["scale"], sc["scale"], delta=0.01)
                        self.assertAlmostEqual(sl["x"] - sc["x"], (L["x"] - card["x"]) * sc["scale"], delta=1.5)
                        self.assertAlmostEqual(sl["y"] - sc["y"], (L["y"] - card["y"]) * sc["scale"], delta=1.5)

    def test_search_bar_cursor_moves_with_the_bar(self):
        W, H = CANVASES["9:16"]
        layers = templates.build("search_bar", {}, 0, 3, {"width": W, "height": H, "fps": 30}, {"energy": "high"})
        bar = [L for L in layers if L["id"] == "search_bar.bar"][0]
        cursors = [L for L in layers if L["id"].startswith("search_bar.cursor")]
        self.assertEqual(len(cursors), 2)
        for L in cursors:
            for k in range(90):
                t = k / 30.0
                sb, sl = layer_state(bar, t, W, H), layer_state(L, t, W, H)
                if not sb or not sl:
                    continue
                with self.subTest(layer=L["id"], t=round(t, 3)):
                    self.assertAlmostEqual(sl["scale"], sb["scale"], delta=0.01)
                    self.assertAlmostEqual(sl["x"] - sb["x"], (L["x"] - bar["x"]) * sb["scale"], delta=1.5)

    def test_source_card_title_leads_the_hierarchy(self):
        long_title = "The 2025 State of the Creator Economy Annual Report"
        detail = "Published by the Creator Research Institute, surveyed 12,000 creators in 40 countries"
        for key in ("9:16", "16:9", "1:1"):
            W, H = CANVASES[key]
            for params in ({}, {"title": long_title, "detail": detail}, {"title": long_title * 2, "detail": detail}):
                with self.subTest(aspect=key, params=params):
                    layers = templates.build("source_card", params, 0, 3, {"width": W, "height": H, "fps": 30})
                    size = {L["id"].split(".")[1]: L["size"] for L in layers if L["type"] == "text"}
                    self.assertGreaterEqual(size["title"], size["detail"], size)
                    self.assertGreaterEqual(size["title"], size["label"], size)
                    card = [L for L in layers if L["id"] == "source_card.card"][0]
                    for L in layers:
                        if L["type"] == "text":
                            x0, y0, x1, y1 = text_box(L)
                            self.assertGreaterEqual(y0, card["y"] - card["h"] / 2 - 1, L["id"])
                            self.assertLessEqual(y1, card["y"] + card["h"] / 2 + 1, L["id"])
        default = templates.build("source_card", {}, 0, 3, {"width": 1080, "height": 1920, "fps": 30})
        title = [L for L in default if L["id"] == "source_card.title"][0]
        self.assertEqual(title["size"], 44)    # short titles keep their full size

    def test_before_after_label_clears_the_handle(self):
        for key in ("9:16", "4:5", "16:9"):
            W, H = CANVASES[key]
            for params in ({}, {"after_note": "52% retention"}, {"layout": "vertical"}, {"layout": "horizontal"}):
                with self.subTest(aspect=key, params=params):
                    layers = templates.build("before_after", params, 0, 3, {"width": W, "height": H, "fps": 30})
                    handle = [L for L in layers if L["id"] == "before_after.handle"][0]
                    hx0, hy0 = handle["x"] - handle["w"] / 2, handle["y"] - handle["h"] / 2
                    hx1, hy1 = hx0 + handle["w"], hy0 + handle["h"]
                    gap = templates._Frame({"width": W, "height": H}).d(12)
                    for L in layers:
                        if L["type"] != "text":
                            continue
                        x0, y0, x1, y1 = text_box(L)
                        pad = L["box"]["pad"] if L.get("box") else [0, 0]
                        x0, y0, x1, y1 = x0 - pad[0], y0 - pad[1], x1 + pad[0], y1 + pad[1]
                        apart = x0 >= hx1 + gap or x1 <= hx0 - gap or y0 >= hy1 + gap or y1 <= hy0 - gap
                        self.assertTrue(apart, "%s (%s) touches the handle" % (L["id"], L["text"]))

    def test_versus_badge_never_covers_the_names(self):
        names = [{}, {"left": "PRODUCTIVITY", "right": "MOTIVATION"},
                 {"left": "Morning routines", "right": "Night routines"}, {"left": "A", "right": "B"}]
        for key in ("9:16", "4:5", "1:1", "16:9"):
            W, H = CANVASES[key]
            for params in names:
                for energy in ("high", "medium", "calm"):
                    layers = templates.build("versus", params, 0, 3, {"width": W, "height": H, "fps": 30},
                                             {"energy": energy})
                    badge = [L for L in layers if L["id"] == "versus.badge"][0]
                    labels = [L for L in layers if L["id"].startswith("versus.name")]
                    for k in range(90):
                        t = k / 30.0
                        sb = layer_state(badge, t, W, H)
                        if not sb or sb["opacity"] < 0.2:
                            continue
                        r = badge["w"] / 2 * sb["scale"]
                        for L in labels:
                            sl = layer_state(L, t, W, H)
                            if not sl or sl["opacity"] < 0.2:
                                continue
                            x0, y0, x1, y1 = text_box(L, sl)
                            dx = max(x0 - sb["x"], 0, sb["x"] - x1)
                            dy = max(y0 - sb["y"], 0, sb["y"] - y1)
                            with self.subTest(aspect=key, params=params, energy=energy, t=round(t, 3), label=L["id"]):
                                self.assertGreater(math.hypot(dx, dy), r)


class TestCLI(unittest.TestCase):
    def run_main(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = templates.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_table_json_doc_build(self):
        code, out, _ = self.run_main([])
        self.assertEqual(code, 0)
        for name in templates.CATALOG:
            self.assertIn(name, out)
        code, out, _ = self.run_main(["--json"])
        self.assertEqual(set(json.loads(out)), set(templates.CATALOG))
        code, out, _ = self.run_main(["doc"])
        self.assertIn("hook_title", out)
        code, out, _ = self.run_main(["build", "stat", "--params", '{"value": "5x"}', "--aspect", "16:9"])
        self.assertEqual(code, 0)
        self.assertEqual([L for L in json.loads(out) if L["id"] == "stat.value"][0]["text"], "5x")
        code, _, err = self.run_main(["build", "nope"])
        self.assertEqual(code, 2)

    def test_preview_without_render_writes_the_spec(self):
        import vstudio
        saved = sys.modules.get("vstudio.render", "missing")
        saved_attr = vstudio.__dict__.pop("render", None)   # an imported submodule is found here first
        sys.modules["vstudio.render"] = None        # makes `from . import render` raise ImportError
        try:
            with tempfile.TemporaryDirectory() as d:
                out = os.path.join(d, "cta.mp4")
                code, _, err = self.run_main(["preview", "cta", "--out", out, "--size", "216x384"])
                self.assertEqual(code, 1)
                self.assertIn("wrote the timeline spec", err)
                with open(os.path.join(d, "cta.json")) as fh:
                    spec = json.load(fh)
                self.assertEqual((spec["width"], spec["height"]), (216, 384))
        finally:
            if saved == "missing":
                del sys.modules["vstudio.render"]
            else:
                sys.modules["vstudio.render"] = saved
            if saved_attr is not None:
                vstudio.render = saved_attr


@unittest.skipUnless(HAVE_RENDER, "render engine or ffmpeg not available")
class TestRender(unittest.TestCase):
    def preview(self, argv):
        with contextlib.redirect_stdout(io.StringIO()):
            return templates.main(["preview"] + argv)

    def test_stills_draw_inside_the_frame(self):
        import numpy as np
        from PIL import Image
        with tempfile.TemporaryDirectory() as d:
            for name in ("hook_title", "list_item", "percent_ring", "comment_bubble", "versus", "highlight_box"):
                out = os.path.join(d, name + ".png")
                code = self.preview([name, "--out", out, "--size", "216x384", "--fps", "10"])
                self.assertEqual(code, 0, name)
                img = np.asarray(Image.open(out).convert("RGB")).astype(int)
                bg = img[2, 2]
                diff = np.abs(img - bg).sum(axis=2) > 60
                self.assertGreater(diff.mean(), 0.004, "%s drew almost nothing" % name)

    def test_mp4_preview(self):
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "stat.mp4")
            code = self.preview(["stat", "--out", out, "--size", "180x320", "--fps", "10", "--duration", "1.2"])
            self.assertEqual(code, 0)
            self.assertGreater(os.path.getsize(out), 1000)


if __name__ == "__main__":
    unittest.main()
