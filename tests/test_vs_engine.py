"""Viral Studio engine, part 1: timeline, anim, sfx, fonts, textrender (no ffmpeg, no network)."""

import contextlib
import io
import os
import shutil
import sys
import tempfile
import unittest
import wave

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np  # noqa: E402

from vstudio import anim, fonts, paths, sfx, textrender, timeline  # noqa: E402

_saved = {}


def setUpModule():
    # caches (sfx wavs, font index) go to a scratch folder, not the user's workspace
    _saved["cache"] = paths.CACHE
    _saved["tmp"] = tempfile.mkdtemp(prefix="vs_engine_")
    paths.CACHE = os.path.join(_saved["tmp"], "cache")


def tearDownModule():
    paths.CACHE = _saved["cache"]
    shutil.rmtree(_saved["tmp"], ignore_errors=True)


def spec(**kw):
    base = {"width": 180, "height": 320, "fps": 10, "layers": []}
    base.update(kw)
    return base


def text_layer(**kw):
    L = {"type": "text", "text": "HELLO WORLD", "font": "DejaVu Sans Bold", "size": 40}
    L.update(kw)
    return timeline.normalize(spec(width=1080, height=1920, layers=[L]))["layers"][0]


# --------------------------------------------------------------------------- timeline

class TimelineValidate(unittest.TestCase):
    def errors(self, **kw):
        return timeline.validate(spec(**kw), base_dir=_saved["tmp"])

    def assertError(self, errors, *needles):
        joined = "\n".join(errors)
        for n in needles:
            self.assertIn(n, joined)

    def test_minimal_spec_is_valid(self):
        self.assertEqual(self.errors(layers=[{"type": "text", "text": "hi"}]), [])

    def test_unknown_layer_type_suggests(self):
        self.assertError(self.errors(layers=[{"type": "txt", "text": "x"}]), "unknown layer type 'txt'", "'text'")

    def test_bad_colour(self):
        self.assertError(self.errors(layers=[{"type": "shape", "color": "red"}]), "color 'red' is not a colour")
        self.assertError(self.errors(background="#12345"), "background")

    def test_end_before_start(self):
        self.assertError(self.errors(layers=[{"type": "shape", "start": 2, "end": 1}]),
                         "end (1) must be after start (2)")

    def test_missing_source_file(self):
        self.assertError(self.errors(layers=[{"type": "video", "src": "nope.mp4"}]), "src file not found")
        self.assertError(self.errors(audio=[{"src": "nope.wav"}]), "audio[0]: src file not found")
        self.assertError(self.errors(layers=[{"type": "image", "src": "https://x.com/a.png"}]), "is a URL")

    def test_unknown_names(self):
        errs = self.errors(
            layers=[{"type": "text", "text": "a", "enter": "pops", "exit": {"type": "pop"},
                     "animate": {"scale": [[0, 1], [1, 2, "out-cubc"]]}}],
            effects=[{"type": "shaek", "start": 1}], audio=[{"sfx": "whosh"}])
        self.assertError(errs, "unknown enter type 'pops' (did you mean 'pop'?)", "unknown exit type 'pop'",
                         "unknown easing 'out-cubc' (did you mean 'out-cubic'?)", "unknown effect 'shaek'",
                         "unknown sfx 'whosh' (did you mean 'whoosh'?)")

    def test_keyframe_shape(self):
        self.assertError(self.errors(layers=[{"type": "shape", "animate": {"x": [[0, 1], 5]}}]), "animate.x[1] must be")
        self.assertError(self.errors(layers=[{"type": "shape", "animate": {"wobble": []}}]), "not animatable")
        self.assertError(self.errors(camera={"pan": []}), "camera: unknown property 'pan'")

    def test_typo_in_field_name(self):
        self.assertError(self.errors(layers=[{"type": "shape", "animte": {}}]),
                         "unknown field 'animte' (did you mean 'animate'?)")

    def test_canvas_and_misc(self):
        self.assertError(timeline.validate({"width": 181, "height": 320, "layers": []}), "width must be even")
        self.assertError(self.errors(effects=[{"type": "flash"}]), "needs a start time")
        self.assertError(self.errors(captions={"words": [{"w": "a", "s": 1, "e": 0.5}]}), "ends (0.5) before it starts")
        self.assertError(self.errors(layers=[{"type": "shape", "shape": "polygon"}]), "polygon needs points")
        self.assertError(self.errors(layers=[{"type": "text", "text": "a b", "highlight": {"words": [5]}}]),
                         "out of range")
        self.assertEqual(timeline.validate([]), ["the spec must be a JSON object, got list"])


class TimelineNormalize(unittest.TestCase):
    def test_defaults_and_percent(self):
        s = timeline.normalize(spec(layers=[{"type": "text", "text": "hi", "x": "25%", "y": "50%",
                                             "animate": {"x": [[1, "100%"], [0, "0%"]]}}]))
        L = s["layers"][0]
        self.assertEqual((L["x"], L["y"]), (45.0, 160.0))
        self.assertEqual(L["animate"]["x"], [[0, 0.0], [1, 180.0]])  # sorted and converted
        self.assertEqual(L["anchor"], "center")
        self.assertEqual(L["end"], s["duration"])
        self.assertEqual(s["duration"], timeline.DEFAULT_DURATION)
        self.assertEqual(s["camera"], {"zoom": [], "x": [], "y": [], "rotation": []})

    def test_enter_shorthand_and_progress_defaults(self):
        s = timeline.normalize(spec(layers=[{"type": "shape", "enter": "pop"}, {"type": "progress"}]))
        self.assertEqual(s["layers"][0]["enter"], {"type": "pop", "duration": 0.35, "delay": 0.0})
        p = s["layers"][1]
        self.assertTrue(p["fixed"])
        self.assertEqual((p["x"], p["y"], p["anchor"], p["w"]), (0, 0, "top-left", 180))

    def test_paths_resolve_against_base_dir(self):
        base = os.path.join(_saved["tmp"], "proj")
        s = timeline.normalize(spec(layers=[{"type": "video", "src": "clips/a.mp4"}],
                                    audio=[{"src": "m.mp3"}], captions={"words": [], "style_dna": "dna.json"}), base)
        self.assertEqual(s["layers"][0]["src"], os.path.join(base, "clips", "a.mp4"))
        self.assertEqual(s["audio"][0]["src"], os.path.join(base, "m.mp3"))
        self.assertEqual(s["captions"]["style_dna"], os.path.join(base, "dna.json"))
        self.assertEqual(s["audio"][0]["role"], "music")
        self.assertTrue(s["audio"][0]["duck"])

    def test_highlight_words_as_text(self):
        L = text_layer(text="I lost $300 today", highlight={"words": ["$300"], "box": True})
        self.assertEqual(L["highlight"]["words"], [2])
        self.assertEqual(L["highlight"]["color"], "#000000")  # readable on the yellow default pill

    def test_duration_from_content(self):
        self.assertEqual(timeline.duration(spec(layers=[{"type": "shape", "end": 3.5}])), 3.5)
        self.assertEqual(timeline.duration(spec(captions={"words": [{"w": "a", "s": 0, "e": 4.25}]})), 4.25)
        self.assertEqual(timeline.duration(spec(duration=2, layers=[{"type": "shape", "end": 9}])), 2.0)

    def test_idempotent(self):
        raw = spec(layers=[text_layer(enter="typewriter", highlight=["WORLD"]),
                           {"type": "shape", "shape": "ring", "w": 50, "h": 50, "draw": {}}],
                   effects=[{"type": "grade"}], audio=[{"sfx": "pop", "start": 1}])
        once = timeline.normalize(raw)
        self.assertEqual(timeline.normalize(once), once)

    def test_load_save_roundtrip(self):
        folder = os.path.join(_saved["tmp"], "rt")
        path = os.path.join(folder, "spec.json")
        timeline.save(spec(layers=[{"type": "image", "src": "logo.png"}]), path)
        s = timeline.load(path)
        self.assertEqual(s["layers"][0]["src"], os.path.join(folder, "logo.png"))


# --------------------------------------------------------------------------- anim

class Easing(unittest.TestCase):
    def test_endpoints(self):
        for name in anim.EASINGS:
            self.assertAlmostEqual(anim.ease(name, 0), 0.0, places=6, msg=name)
            self.assertAlmostEqual(anim.ease(name, 1), 1.0, places=6, msg=name)

    def test_known_values(self):
        self.assertAlmostEqual(anim.ease("in-quad", 0.5), 0.25)
        self.assertAlmostEqual(anim.ease("out-cubic", 0.5), 0.875)
        self.assertAlmostEqual(anim.ease("in-out-expo", 0.5), 0.5)
        self.assertAlmostEqual(anim.ease("out-bounce", 0.5), 0.765625)
        self.assertGreater(max(anim.ease("out-back", x / 100.0) for x in range(101)), 1.09)  # overshoot
        self.assertLess(min(anim.ease("in-back", x / 100.0) for x in range(101)), -0.09)     # anticipation
        self.assertEqual(anim.ease("step", 0.99), 0.0)
        self.assertEqual(anim.ease("linear", 2.0), 1.0)  # clamped
        with self.assertRaises(ValueError):
            anim.ease("wobble", 0.5)

    def test_value_at(self):
        keys = [[0, 0.0], [1, 10.0], [2, 20.0, "out-quad"], [3, 30.0, "step"]]
        self.assertEqual(anim.value_at(keys, -1, 99), 0.0)
        self.assertEqual(anim.value_at(keys, 5, 99), 30.0)
        self.assertAlmostEqual(anim.value_at(keys, 0.5, 99), 5.0)
        self.assertAlmostEqual(anim.value_at(keys, 1.5, 99), 17.5)  # easing of the arriving key
        self.assertEqual(anim.value_at(keys, 2.9, 99), 20.0)        # step holds until its key
        self.assertEqual(anim.value_at([], 1, 7.0), 7.0)


class Presets(unittest.TestCase):
    layer = {"id": "t", "enter": {"type": "pop", "duration": 0.3}, "exit": {"type": "fade", "duration": 0.3}}

    def test_every_preset_rests_at_identity(self):
        for kind in anim.ENTER_TYPES:
            L = {"id": "a", "text": "a b c", "enter": {"type": kind, "duration": 0.3}}
            self.assertEqual(anim.enter_state(kind, 1.0, L), anim.IDENTITY, kind)
            st = anim.enter_state(kind, 0.4, L, canvas=(180, 320), fps=10, frame=3)
            self.assertTrue(set(anim.IDENTITY) <= set(st), kind)
        for kind in anim.EXIT_TYPES:
            L = {"id": "a", "exit": {"type": kind, "duration": 0.3}}
            self.assertEqual(anim.exit_state(kind, 0.0, L), anim.IDENTITY, kind)
            anim.exit_state(kind, 0.6, L, canvas=(180, 320), fps=10, frame=3)
        with self.assertRaises(ValueError):
            anim.enter_state("teleport", 0.5)

    def test_pop_overshoot(self):
        scales = [anim.enter_state("pop", i / 200.0)["scale"] for i in range(201)]
        self.assertAlmostEqual(scales[0], 0.5)
        self.assertAlmostEqual(max(scales), 1.08, places=3)
        self.assertEqual(anim.enter_state("pop", 0)["opacity"], 0.0)

    def test_stamp_whip_glitch_wipe(self):
        L = {"id": "s", "enter": {"type": "stamp", "duration": 0.3}}
        self.assertAlmostEqual(anim.enter_state("stamp", 0, L)["scale"], 2.2)
        self.assertNotEqual(anim.enter_state("stamp", 0.56, L, fps=30)["dx"], 0.0)   # impact shake
        self.assertEqual(anim.enter_state("stamp", 0.95, L, fps=30)["dx"], 0.0)
        W = {"enter": {"type": "whip-left", "duration": 0.25}}
        mid = anim.enter_state("whip-left", 0.3, W)
        self.assertGreater(mid["dx"], 0)
        self.assertGreater(mid["mblur"], 1)
        G = {"id": "g"}
        a = anim.enter_state("glitch", 0.3, G, frame=4)
        self.assertEqual(a, anim.enter_state("glitch", 0.3, G, frame=4))  # deterministic
        self.assertGreater(a["rgb_split"], 0)
        splits = [anim.enter_state("glitch", 0.3, G, frame=f)["rgb_split"] for f in range(5)]
        self.assertGreater(len(set(splits)), 1)  # changes from frame to frame
        self.assertEqual(anim.enter_state("wipe-right", 0.5)["wipe"][:2], (0.0, anim.ease("out-quart", 0.5)))
        self.assertEqual(anim.exit_state("wipe-left", 0.0)["wipe"], None)

    def test_state_at_combines(self):
        L = timeline.normalize(spec(layers=[{"type": "shape", "x": 50, "scale": 2, "end": 2,
                                             "animate": {"x": [[0, 50], [1, 150]]},
                                             "enter": {"type": "slide-right", "duration": 0.5, "distance": 40},
                                             "exit": {"type": "fade", "duration": 0.5}}]))["layers"][0]
        st = anim.state_at(L, 0.0)
        self.assertAlmostEqual(st["x"], 50 - 40)       # keyframe + enter offset
        self.assertTrue(st["moving"])
        st = anim.state_at(L, 1.0)
        self.assertAlmostEqual(st["x"], 150)
        self.assertAlmostEqual(st["scale"], 2.0)
        self.assertAlmostEqual(anim.state_at(L, 1.75)["opacity"], 1 - anim.ease("in-quad", 0.5))
        self.assertFalse(anim.state_at(L, 1.2)["moving"])


# --------------------------------------------------------------------------- sfx

class Sfx(unittest.TestCase):
    def test_every_effect(self):
        for name in sfx.NAMES:
            x = sfx.synth(name)
            self.assertEqual(x.shape[1], 2, name)
            self.assertFalse(np.isnan(x).any(), name)
            self.assertAlmostEqual(np.abs(x).max(), 10 ** (-3 / 20), places=4, msg=name)
            self.assertEqual(len(x), int(round(sfx.LENGTHS[name] * 48000)), name)
            self.assertLess(abs(float(x.mean())), 1e-3, name)

    def test_cached_wav(self):
        path = sfx.make("pop")
        self.assertTrue(path.startswith(paths.CACHE))
        self.assertEqual(sfx.make("pop"), path)
        with wave.open(path) as w:
            self.assertEqual((w.getframerate(), w.getnchannels(), w.getsampwidth()), (48000, 2, 2))
        self.assertGreater(sfx.duration("riser"), 1.5)
        out = os.path.join(_saved["tmp"], "w.wav")
        self.assertEqual(sfx.main(["render", "whoosh", "--out", out]), 0)
        self.assertTrue(os.path.getsize(out) > 1000)
        self.assertEqual(sfx.main(["render", "nope", "--out", out]), 2)
        with self.assertRaises(ValueError):
            sfx.synth("nope")


# --------------------------------------------------------------------------- fonts

class Fonts(unittest.TestCase):
    def test_parse_name(self):
        self.assertEqual(fonts.parse_name("Montserrat Black"), ("Montserrat", 900, False))
        self.assertEqual(fonts.parse_name("Montserrat Extra Bold Italic"), ("Montserrat", 800, True))
        self.assertEqual(fonts.parse_name("Inter SemiBold"), ("Inter", 600, False))
        self.assertEqual(fonts.parse_name("Poppins-BlackItalic"), ("Poppins", 900, True))
        self.assertEqual(fonts.parse_name("Bebas Neue"), ("Bebas Neue", None, False))
        self.assertEqual(fonts.parse_name("Open Sans Light"), ("Open Sans", 300, False))

    def test_resolve_never_raises(self):
        for name in ("Montserrat Black", "Totally Missing Family 123", "", None, "DejaVu Sans Bold"):
            p = fonts.resolve(name)
            self.assertIsInstance(p, str)
            if p:
                self.assertTrue(os.path.isfile(p), p)
        f = fonts.load("Totally Missing Family 123", 40)
        self.assertGreater(f.getlength("Hello"), 10)

    def test_resolve_path_and_weights(self):
        faces = [e for e in fonts.installed() if not e["variable"]]
        if not faces:
            self.skipTest("no static fonts on this machine")
        e = faces[0]
        self.assertEqual(fonts.resolve(e["path"]), e["path"])
        fam = e["family"]
        weights = {x["weight"]: x for x in faces if x["family"] == fam and not x["italic"]}
        if len(weights) > 1:
            heavy = max(weights)
            self.assertEqual(fonts.info("%s %s" % (fam, fonts.WEIGHT_NAMES.get(heavy, "")))["weight"], heavy)

    def test_variable_font_weight(self):
        var = [e for e in fonts.installed() if e["variable"] and e.get("wght") and not e["italic"]]
        if not var:
            self.skipTest("no variable font installed (python -m vstudio fonts install Montserrat)")
        fam = var[0]["family"]
        thin, black = fonts.load(fam + " Thin", 80), fonts.load(fam + " Black", 80)

        def ink(f):
            from PIL import Image, ImageDraw
            im = Image.new("L", (600, 120))
            ImageDraw.Draw(im).text((10, 100), "HELLO", font=f, fill=255, anchor="ls")
            return np.asarray(im, np.float32).sum()
        self.assertGreater(ink(black), ink(thin) * 1.5)

    def test_install_offline(self):
        # serve a fake google/fonts tree: METADATA.pb lists the files to fetch
        src = next((e["path"] for e in fonts.installed() if e["path"].lower().endswith(".ttf")), None)
        if not src:
            self.skipTest("no .ttf font to serve")
        with open(src, "rb") as f:
            data = f.read()
        meta = ('name: "Fake Sans"\nfonts {\n  filename: "FakeSans[wght].ttf"\n}\n'
                'fonts {\n  filename: "FakeSans-Italic.ttf"\n}\n')
        calls = []

        def fake_get(url, timeout=60):
            calls.append(url)
            if "/ofl/fakesans/METADATA.pb" in url:
                return meta.encode()
            if "/ofl/fakesans/" in url:
                return b"license" if url.endswith("OFL.txt") else data
            import urllib.error
            raise urllib.error.HTTPError(url, 404, "nf", None, None)

        saved_get, saved_fonts = fonts._get, paths.FONTS
        fonts._get, paths.FONTS = fake_get, os.path.join(_saved["tmp"], "fonts")
        try:
            files = fonts.install(["Fake Sans"])
            self.assertEqual(sorted(os.path.basename(f) for f in files), ["FakeSans-Italic.ttf", "FakeSans[wght].ttf"])
            self.assertTrue(any("FakeSans%5Bwght%5D.ttf" in c for c in calls))  # brackets URL-encoded
            self.assertTrue(os.path.isfile(os.path.join(paths.FONTS, "FakeSans", "OFL.txt")))
            with self.assertRaises(RuntimeError) as cm:
                fonts.install(["No Such Family"])
            self.assertIn("No Such Family", str(cm.exception))
        finally:
            fonts._get, paths.FONTS = saved_get, saved_fonts
            fonts.refresh()

    def test_ass_font(self):
        out = os.path.join(_saved["tmp"], "assfonts")
        name, weight = fonts.ass_font("DejaVu Sans Bold", out)
        self.assertTrue(name)
        self.assertGreaterEqual(weight, 100)
        if fonts.resolve("DejaVu Sans Bold"):
            self.assertTrue(os.listdir(out))

    def test_cli(self):
        self.assertEqual(fonts.main(["resolve", "DejaVu", "Sans"]), 0 if fonts.resolve("DejaVu Sans") else 1)

    def test_resolve_cli_takes_several_names(self):
        # "fonts resolve 'Montserrat Black' Anton 'Inter Bold'" used to look up one font of all the words
        saved = fonts.installed_name
        try:
            fonts.installed_name = lambda n: n in ("DejaVu Sans Bold", "Anton", "Inter")
            self.assertEqual(fonts._resolve_args(["Montserrat Black", "Anton", "Inter Bold"]),
                             ["Montserrat Black", "Anton", "Inter Bold"])          # quoted: one font each
            self.assertEqual(fonts._resolve_args(["DejaVu", "Sans", "Bold"]), ["DejaVu Sans Bold"])
            self.assertEqual(fonts._resolve_args(["Anton", "Inter"]), ["Anton", "Inter"])
            self.assertEqual(fonts._resolve_args(["Bebas", "Neue"]), ["Bebas Neue"])  # not installed: one name
        finally:
            fonts.installed_name = saved
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            fonts.main(["resolve", "DejaVu Sans Bold", "DejaVu Sans"])
        self.assertIn("DejaVu Sans Bold: ", out.getvalue())
        self.assertIn("DejaVu Sans: ", out.getvalue())


# --------------------------------------------------------------------------- textrender

def alpha(img):
    return np.asarray(img, np.float32)[..., 3]


class TextRender(unittest.TestCase):
    def test_wrap_and_box(self):
        one = textrender.render_text(text_layer(text="ONE TWO THREE FOUR", max_width=2000))
        many = textrender.render_text(text_layer(text="ONE TWO THREE FOUR", max_width=200))
        self.assertGreater(many.height, one.height * 2)
        b = many.info["box"]
        self.assertLessEqual(b[2] - b[0], 200 + 1)
        self.assertGreater(alpha(many).max(), 250)

    def test_balanced_lines(self):
        L = text_layer(text="this is a long title that wraps", max_width=500)
        lay = textrender._layout(L, 1.0)
        widths = [ln["w"] for ln in lay["lines"]]
        self.assertGreater(len(widths), 1)
        self.assertGreater(min(widths) / max(widths), 0.6)  # no lonely last word

    def test_highlight_stroke_box_colours(self):
        img = textrender.render_text(text_layer(text="AAA BBB", color="#FFFFFF",
                                                highlight={"words": [1], "color": "#FF0000"},
                                                stroke={"color": "#0000FF", "width": 4}))
        px = np.asarray(img).reshape(-1, 4)
        solid = px[px[:, 3] == 255][:, :3].astype(int)
        for colour in ([255, 255, 255], [255, 0, 0], [0, 0, 255]):   # fill, highlight, stroke
            self.assertTrue((np.abs(solid - colour).sum(axis=1) < 30).any(), colour)
        boxed = textrender.render_text(text_layer(text="HI", box={"color": "#00FF00", "pad": [30, 20], "radius": 10}))
        x0, y0, x1, y1 = boxed.info["box"]
        self.assertEqual(boxed.getpixel((int(x0) + 15, int((y0 + y1) / 2)))[:3], (0, 255, 0))
        pill = textrender.render_text(text_layer(text="AB CD", highlight={"words": [1], "box": {"color": "#FF00FF"}}))
        self.assertTrue((np.abs(np.asarray(pill)[..., :3].astype(int) - [255, 0, 255]).sum(axis=2) < 20).any())

    def test_no_stroke_means_no_outline(self):
        img = textrender.render_text(text_layer(text="AAA", color="#FFFFFF"))
        px = np.asarray(img).reshape(-1, 4)
        self.assertFalse(((px[:, 3] == 255) & (px[:, :3].sum(axis=1) < 100)).any())

    def test_gradient(self):
        img = np.asarray(textrender.render_text(text_layer(text="IIII", size=120, gradient=["#FF0000", "#0000FF"])))
        ys = np.nonzero(img[..., 3] == 255)[0]
        top = img[ys.min() + 2][img[ys.min() + 2][:, 3] == 255][:, :3].mean(axis=0)
        bot = img[ys.max() - 2][img[ys.max() - 2][:, 3] == 255][:, :3].mean(axis=0)
        self.assertGreater(top[0], top[2])
        self.assertGreater(bot[2], bot[0])

    def test_reveal_keeps_layout(self):
        L = text_layer(text="type this out slowly", box={"color": "#000000"})
        full = textrender.render_text(L)
        part = textrender.render_text(L, reveal=0.3)
        words = textrender.render_text(L, words_visible=1)
        self.assertEqual(full.size, part.size)
        self.assertEqual(full.size, words.size)
        self.assertEqual(full.info["box"], part.info["box"])
        L2 = text_layer(text="type this out slowly")
        sums = [alpha(textrender.render_text(L2, reveal=r)).sum() for r in (0.0, 0.3, 0.7, 1.0)]
        self.assertEqual(sums[0], 0)
        self.assertTrue(sums[0] < sums[1] < sums[2] < sums[3])
        self.assertLess(alpha(textrender.render_text(L2, words_visible=1)).sum(), sums[3])

    def test_shadow_scale_and_cache(self):
        L = text_layer(shadow={"color": "#000000FF", "dx": 0, "dy": 10, "blur": 8})
        a = textrender.sprite(L, 1.0)
        self.assertIs(textrender.sprite(L, 1.0), a)  # cached
        plain = textrender.render_text(text_layer())
        shadowed = textrender.render_text(L)
        self.assertGreater(shadowed.height, plain.height)
        half = textrender.render_text(text_layer(), scale=0.5)
        self.assertAlmostEqual(half.info["box"][2] - half.info["box"][0],
                               (plain.info["box"][2] - plain.info["box"][0]) / 2, delta=4)

    def test_no_clipped_glyphs(self):
        # accents, descenders and italic overhangs must all fit inside the sprite
        italics = sorted({e["family"] for e in fonts.installed() if e["italic"]})[:3]
        for name in ["DejaVu Sans Bold"] + [f + " Italic" for f in italics]:
            for extra in ({}, {"stroke": {"color": "#000000", "width": 10}, "letter_spacing": -4}):
                a = alpha(textrender.render_text(text_layer(text="ÉÀÇ fjord Wf\nQjgy|", font=name, size=90, **extra)))
                self.assertEqual(max(a[0].max(), a[-1].max(), a[:, 0].max(), a[:, -1].max()), 0, (name, extra))

    def test_missing_glyphs_fall_back_run_by_run(self):
        # a Hindi hook title in a Latin font used to draw as a row of .notdef boxes
        text = "HELLO नमस्ते"
        found, _ = fonts.glyph_fallbacks("DejaVu Sans Bold", text)
        if "न" not in found:
            self.skipTest("no installed font draws Devanagari")
        for ch in "नमसत":
            self.assertTrue(fonts.has_glyph(fonts._probe(found[ch]), ch), ch)
        self.assertNotIn("H", found)                       # the Latin word keeps the layer's font
        lay = textrender._layout(text_layer(text=text), 1.0)
        runs = lay["font"].runs(text)
        self.assertEqual([r[1] for r in runs], ["HELLO ", "नमस्ते"])
        self.assertIs(runs[0][2], lay["font"].primary)
        # the Devanagari is drawn exactly as its own font draws it (shaped as one word), not as boxes
        fam = found["न"]["family"] + " " + fonts.WEIGHT_NAMES.get(found["न"]["weight"], "")

        def ink(L):
            a = alpha(textrender.render_text(L))
            ys, xs = np.nonzero(a > 0)
            return a[ys.min():ys.max() + 1, xs.min():xs.max() + 1]
        mixed, direct = ink(text_layer(text="नमस्ते")), ink(text_layer(text="नमस्ते", font=fam))
        self.assertEqual(mixed.shape, direct.shape)      # boxes: (35, 140) against (38, 94)
        self.assertLess(np.abs(mixed - direct).mean(), 2.0)

    def test_glyphs_no_font_has_are_named(self):
        saved = fonts._index
        dejavu = [e for e in fonts.installed() if e["family"] == "DejaVu Sans"]
        if not dejavu:
            self.skipTest("DejaVu Sans not installed")
        fonts._index, err = dejavu, io.StringIO()
        fonts._glyph_choice.clear()
        try:
            with contextlib.redirect_stderr(err):
                found, nobody = fonts.glyph_fallbacks("DejaVu Sans Bold", "OK न")
        finally:
            fonts._index = saved
            fonts._glyph_choice.clear()
        self.assertEqual((found, nobody), ({}, ["न"]))
        self.assertIn("U+0928", err.getvalue())
        self.assertFalse(any(fonts.needs_glyph(c) for c in " ्‍️\n"))  # space, virama, ZWJ, VS16
        self.assertTrue(all(fonts.needs_glyph(c) for c in "Aन🔥★"))

    def test_letter_spacing_and_tabular(self):
        tight = text_layer(text="ABCD")
        wide = text_layer(text="ABCD", letter_spacing=20)

        def w(L):
            box = textrender.render_text(L).info["box"]
            return box[2] - box[0]
        self.assertAlmostEqual(w(wide) - w(tight), 60, delta=2)
        a = text_layer(text="1111", _tabular=True)
        b = text_layer(text="0000", _tabular=True)
        self.assertAlmostEqual(w(a), w(b), delta=1)


if __name__ == "__main__":
    unittest.main()
