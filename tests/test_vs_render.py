"""Viral Studio engine, part 2: frames, video decoding, full renders, audio mix (needs ffmpeg; offline)."""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
import wave

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cv2  # noqa: E402
import numpy as np  # noqa: E402
from PIL import Image  # noqa: E402

from vstudio import anim, audio, paths, render, timeline  # noqa: E402

HAVE_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
T = {}


def _ff(*args):
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"] + list(args), check=True)


def _wav(path, x, sr=48000):
    pcm = (np.clip(x, -1, 1) * 32767).astype("<i2")
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())


def setUpModule():
    T["tmp"] = tempfile.mkdtemp(prefix="vs_render_")
    T["cache"] = paths.CACHE
    paths.CACHE = os.path.join(T["tmp"], "cache")
    if not HAVE_FFMPEG:
        return
    d = T["tmp"]
    # a small 16:9 clip with sound
    _ff("-f", "lavfi", "-i", "testsrc2=size=320x180:rate=10:duration=3", "-f", "lavfi", "-i",
        "sine=frequency=440:sample_rate=48000:duration=3", "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-shortest", os.path.join(d, "clip.mp4"))
    # lossless clip whose grey level is 8 x frame number: decoding can be checked exactly
    _ff("-f", "lavfi", "-i", "nullsrc=s=32x32:r=10:d=3,format=gray,geq=lum='N*8'", "-c:v", "ffv1",
        os.path.join(d, "counter.mkv"))
    sr = 48000
    t = np.arange(4 * sr) / sr
    _wav(os.path.join(d, "music.wav"), 0.3 * np.sin(2 * np.pi * 200 * t))
    voice = 0.3 * np.sin(2 * np.pi * 2000 * t) * ((t >= 1.0) & (t < 2.0))
    _wav(os.path.join(d, "voice.wav"), voice)
    logo = np.zeros((40, 40, 4), np.uint8)
    logo[8:32, 8:32] = (0, 255, 0, 255)
    Image.fromarray(logo, "RGBA").save(os.path.join(d, "logo.png"))


def tearDownModule():
    paths.CACHE = T["cache"]
    shutil.rmtree(T["tmp"], ignore_errors=True)


def S(**kw):
    s = {"width": 180, "height": 320, "fps": 10, "duration": 2, "background": "#000000", "layers": []}
    s.update(kw)
    return s


def rect(**kw):
    L = {"type": "shape", "shape": "rect", "w": 60, "h": 30, "x": 20, "y": 40, "anchor": "top-left", "color": "#FF0000"}
    L.update(kw)
    return L


def probe_streams(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                        "stream=codec_type,codec_name,width,height,r_frame_rate,sample_rate,channels:format=duration",
                        "-of", "json", path], capture_output=True, text=True, check=True)
    return json.loads(r.stdout)


class Frames(unittest.TestCase):
    """Pixel-exact checks of the compositor (no ffmpeg needed for these)."""

    def test_red_rect_is_where_it_should_be(self):
        f = render.render_frame(S(layers=[rect()]), 0.5)
        self.assertEqual(f.shape, (320, 180, 3))
        self.assertEqual(tuple(f[55, 50]), (255, 0, 0))
        self.assertEqual(tuple(f[40, 20]), (255, 0, 0))   # top-left corner pixel
        self.assertEqual(tuple(f[69, 79]), (255, 0, 0))   # bottom-right corner pixel
        self.assertEqual(tuple(f[70, 80]), (0, 0, 0))     # crisp edge, no bleed
        self.assertEqual(tuple(f[10, 10]), (0, 0, 0))

    def test_anchor_rotation_scale(self):
        f = render.render_frame(S(layers=[rect(w=40, h=10, x=90, y=160, anchor="center", rotation=90)]), 0)
        self.assertEqual(tuple(f[175, 90]), (255, 0, 0))
        self.assertEqual(tuple(f[160, 105]), (0, 0, 0))
        f = render.render_frame(S(layers=[rect(w=20, h=20, x=90, y=160, anchor="center", scale=2)]), 0)
        self.assertEqual(tuple(f[160, 107]), (255, 0, 0))
        f = render.render_frame(S(layers=[rect(w=20, h=20, x=180, y=320, anchor="bottom-right")]), 0)
        self.assertEqual(tuple(f[315, 175]), (255, 0, 0))

    def test_camera_moves_scene_not_fixed_layers(self):
        layers = [rect(w=20, h=20, x=130, y=160, anchor="center"),
                  rect(w=10, h=10, x=130, y=40, anchor="center", color="#0000FF", fixed=True)]
        f = render.render_frame(S(layers=layers, camera={"zoom": [[0, 2.0]]}), 0)
        self.assertEqual(tuple(f[160, 170]), (255, 0, 0))   # 90 + (130 - 90) * 2
        self.assertEqual(tuple(f[160, 130]), (0, 0, 0))
        self.assertEqual(tuple(f[40, 130]), (0, 0, 255))    # fixed: unmoved
        f = render.render_frame(S(layers=layers, camera={"x": [[0, 30]]}), 0)
        self.assertEqual(tuple(f[160, 160]), (255, 0, 0))

    def test_blend_modes(self):
        def px(mode):
            L = rect(w=180, h=320, x=0, y=0, color="#808080", blend=mode)
            return int(render.render_frame(S(background="#808080", layers=[L]), 0)[100, 100, 0])
        self.assertEqual(px("normal"), 128)
        self.assertEqual(px("add"), 255)
        self.assertAlmostEqual(px("multiply"), 64, delta=1)
        self.assertAlmostEqual(px("screen"), 192, delta=1)

    def test_composite_matches_reference_math(self):
        # every blend mode, RGBA and opaque sprites, snapped and resampled placement, with and without opacity,
        # against the textbook formulas on premultiplied colour (the fast paths skip unused temporaries)
        rng = np.random.default_rng(3)
        base = rng.random((40, 50, 3)).astype(np.float32)
        a = rng.random((20, 30, 1)).astype(np.float32)
        rgba = np.concatenate([rng.random((20, 30, 3)).astype(np.float32) * a, a], axis=2)
        opaque = rng.random((20, 30, 3)).astype(np.float32)

        def ref(d, s, al, mode):
            return {"normal": s + d * (1 - al), "add": d + s, "screen": s + d - s * d,
                    "multiply": d * (s + 1 - al)}[mode]

        for spr in (rgba, opaque):
            for mode in ("normal", "add", "screen", "multiply"):
                for op in (1.0, 0.4):
                    for split in (None, "cached"):
                        canvas = base.copy()
                        M = np.array([[1.0, 0, 7], [0, 1.0, 5], [0, 0, 1]])  # whole pixels: no resampling
                        sp = render.split_alpha(spr) if split and spr.shape[2] == 4 else None
                        render.composite(canvas, spr, M, mode, op, split=(lambda: sp) if sp else None)
                        s = spr[..., :3] * op
                        al = (spr[..., 3:] if spr.shape[2] == 4 else np.ones_like(spr[..., :1])) * op
                        want = base.copy()
                        want[5:25, 7:37] = ref(base[5:25, 7:37], s, al, mode)
                        np.testing.assert_allclose(canvas, want, atol=2e-5, err_msg="%s %s %s" % (
                            spr.shape[2], mode, op))
                # a resampled placement (scale 1.5, half-pixel offset) agrees with an explicit warp
                M = np.array([[1.5, 0, 3.5], [0, 1.5, 2.5], [0, 0, 1]])
                for mode in ("normal", "add", "screen", "multiply"):
                    canvas = base.copy()
                    render.composite(canvas, spr, M, mode, 0.7)
                    src = spr if spr.shape[2] == 4 else np.concatenate([spr, np.ones_like(spr[..., :1])], 2)
                    w = cv2.warpAffine(src, render._cv_affine(M), (50, 40), flags=cv2.INTER_LINEAR,
                                       borderMode=cv2.BORDER_CONSTANT, borderValue=(0, 0, 0, 0))
                    want = ref(base, w[..., :3] * 0.7, w[..., 3:] * 0.7, mode)
                    inside = (slice(4, 30), slice(5, 47))  # away from the opaque path's replicated border
                    np.testing.assert_allclose(canvas[inside], want[inside], atol=2e-4,
                                               err_msg="warp %s %s" % (spr.shape[2], mode))

    def test_stroke_with_transparent_fill_is_an_outline(self):
        for shape in ("rect", "circle", "polygon"):
            L = {"type": "shape", "shape": shape, "w": 120, "h": 120, "x": 90, "y": 160, "color": "#00000000",
                 "stroke": {"color": "#FF0000", "width": 8}, "points": [[0, 0], [1, 0], [1, 1], [0, 1]]}
            f = render.render_frame(S(layers=[L]), 0)
            self.assertEqual(tuple(f[160, 90]), (0, 0, 0), shape)        # the inside stays see-through
            self.assertEqual(tuple(f[160, 33]), (255, 0, 0), shape)      # the band is drawn
            f = render.render_frame(S(layers=[dict(L, color="#00FF00")]), 0)
            self.assertEqual(tuple(f[160, 90]), (0, 255, 0), shape)      # an opaque body still fills it
            self.assertEqual(tuple(f[160, 33]), (255, 0, 0), shape)

    def test_opacity_and_gradient_background(self):
        f = render.render_frame(S(layers=[rect(opacity=0.5)]), 0)
        self.assertAlmostEqual(int(f[55, 50, 0]), 128, delta=1)
        g = render.render_frame(S(background={"gradient": ["#000000", "#FFFFFF"], "angle": 90}), 0)
        self.assertLess(g[5].mean(), 20)
        self.assertGreater(g[-5].mean(), 235)

    def test_shapes_image_progress(self):
        layers = [
            {"type": "shape", "shape": "circle", "w": 60, "h": 60, "x": 50, "y": 50, "color": "#00FF00"},
            {"type": "shape", "shape": "ring", "w": 60, "h": 60, "x": 130, "y": 50, "color": "#0000FF",
             "stroke": {"width": 8}},
            {"type": "shape", "shape": "polygon", "w": 60, "h": 60, "x": 50, "y": 130,
             "points": [[0, 0], [1, 0], [0, 1]], "color": "#FF0000"},
            {"type": "image", "src": "logo.png", "x": 130, "y": 130},
            {"type": "progress", "y": 300, "h": 10, "color": "#FFFFFF", "track": "#FF000080", "end": 2},
        ]
        f = render.render_frame(S(layers=layers), 1.0, base_dir=T["tmp"])
        self.assertEqual(tuple(f[50, 50]), (0, 255, 0))       # circle centre
        self.assertEqual(tuple(f[50, 130]), (0, 0, 0))        # ring hole
        self.assertEqual(tuple(f[50, 104]), (0, 0, 255))      # ring band
        self.assertEqual(tuple(f[110, 30]), (255, 0, 0))      # triangle top-left
        self.assertEqual(tuple(f[150, 70]), (0, 0, 0))        # outside the hypotenuse
        self.assertEqual(tuple(f[130, 130]), (0, 255, 0))     # logo
        self.assertEqual(tuple(f[112, 112]), (0, 0, 0))       # its transparent border
        self.assertEqual(tuple(f[305, 40]), (255, 255, 255))  # progress half full
        self.assertAlmostEqual(int(f[305, 140, 0]), 128, delta=2)

    def test_shape_draw_on(self):
        L = rect(w=100, h=10, x=0, y=100, color="#FFFFFF", draw={"from": 0, "to": 1, "ease": "linear"},
                 enter={"type": "none", "duration": 1.0})
        f = render.render_frame(S(layers=[L]), 0.5)
        self.assertEqual(int(f[105, 30, 0]), 255)
        self.assertEqual(int(f[105, 70, 0]), 0)

    def test_deterministic(self):
        spec = S(layers=[rect(), {"type": "text", "text": "GLITCH", "font": "DejaVu Sans Bold", "size": 40,
                                  "enter": {"type": "glitch", "duration": 1.5}}],
                 effects=[{"type": "glitch", "start": 0, "duration": 2}, {"type": "grain", "strength": 0.05},
                          {"type": "shake", "start": 0, "duration": 2, "intensity": 10}])
        a = render.render_frame(spec, 0.7)
        b = render.render_frame(spec, 0.7)
        self.assertTrue(np.array_equal(a, b))
        self.assertFalse(np.array_equal(a, render.render_frame(spec, 0.8)))

    def test_every_enter_and_exit_renders(self):
        layers = []
        for i, kind in enumerate(anim.ENTER_TYPES):
            text = kind in ("typewriter", "words")
            L = {"type": "text", "text": "HI THERE", "font": "DejaVu Sans Bold", "size": 30} if text or i % 2 else \
                {"type": "shape", "w": 40, "h": 20, "color": "#FFFFFF", "shadow": True}
            L.update({"id": "e%d" % i, "end": 1.0, "enter": {"type": kind, "duration": 0.6}})
            layers.append(L)
        for i, kind in enumerate(anim.EXIT_TYPES):
            L = {"type": "text", "text": "BYE", "font": "DejaVu Sans Bold", "size": 30} if i % 2 else \
                {"type": "image", "src": "logo.png"}
            L.update({"id": "x%d" % i, "start": 1.0, "end": 2.0, "exit": {"type": kind, "duration": 0.6}})
            layers.append(L)
        s = render._prepare(S(layers=layers), T["tmp"])
        comp = render.Compositor(s, 1.0, 10)
        try:
            frames = [comp.frame_u8(i / 10.0, i) for i in range(20)]
        finally:
            comp.close()
        self.assertFalse(np.array_equal(frames[0], frames[9]))
        self.assertFalse(np.array_equal(frames[12], frames[19]))

    def test_words_reveal_with_word_times(self):
        L = {"type": "text", "text": "ONE TWO THREE", "font": "DejaVu Sans Bold", "size": 30,
             "enter": {"type": "words", "duration": 1}, "word_times": [0.0, 0.5, 1.0]}
        lit = [int((render.render_frame(S(layers=[L]), t) > 0).sum()) for t in (0.1, 0.6, 1.1)]
        self.assertTrue(lit[0] < lit[1] < lit[2])

    def test_counter_text(self):
        s = render._prepare(S(layers=[{"type": "counter", "from": 0, "to": 1234.5, "decimals": 1, "prefix": "$",
                                       "count_duration": 1.0, "font": "DejaVu Sans Bold"}]))
        comp = render.Compositor(s)
        L = s["layers"][0]
        self.assertEqual(comp._counter_text(L, 0)["text"], "$0.0")
        self.assertEqual(comp._counter_text(L, 1.5)["text"], "$1,234.5")
        self.assertEqual(comp._counter_text(L, 0.5)["_reserve"], "$1,234.5")

    def test_bad_spec_raises(self):
        with self.assertRaises(render.RenderError) as cm:
            render.render_frame(S(layers=[{"type": "txt"}]), 0)
        self.assertIn("unknown layer type", str(cm.exception))


@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg not installed")
class Video(unittest.TestCase):
    def grey(self, layer, t):
        L = dict({"type": "video", "src": "counter.mkv", "volume": 0}, **layer)
        return int(render.render_frame(S(duration=6, layers=[L]), t, base_dir=T["tmp"])[160, 90, 0])

    def test_in_speed_hold_loop(self):
        self.assertEqual(self.grey({}, 0.0), 0)
        self.assertEqual(self.grey({}, 1.2), 12 * 8)
        self.assertEqual(self.grey({"in": 0.5, "speed": 2.0}, 0.4), 13 * 8)    # 0.5 + 0.4 * 2 = 1.3 s
        self.assertEqual(self.grey({"start": 1.0}, 1.5), 5 * 8)                # layer-relative
        self.assertEqual(self.grey({}, 4.5), 29 * 8)                           # ran out: hold the last frame
        self.assertEqual(self.grey({"loop": True}, 4.5), 15 * 8)               # wraps: 4.5 - 3.0

    def test_sequential_equals_random_access(self):
        spec = S(layers=[{"type": "video", "src": "clip.mp4", "kenburns": [1.0, 1.2], "in": 0.5}])
        s = render._prepare(spec, T["tmp"])
        comp = render.Compositor(s, 1.0, 10)
        try:
            seq = [comp.frame_u8(i / 10.0, i) for i in range(12)]
        finally:
            comp.close()
        for i in (0, 7, 11):
            self.assertTrue(np.array_equal(seq[i], render.render_frame(s, i / 10.0)), i)


@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg not installed")
class FullRender(unittest.TestCase):
    def spec(self):
        words = [{"w": w, "s": 0.1 + 0.3 * i, "e": 0.35 + 0.3 * i} for i, w in enumerate("this is the hook ok".split())]
        return {
            "width": 216, "height": 384, "fps": 10, "duration": 2.0,
            "background": {"gradient": ["#101018", "#000000"], "angle": 90},
            "layers": [
                {"type": "video", "src": "clip.mp4", "kenburns": [1.0, 1.1], "volume": 0.8},
                {"type": "shape", "w": 190, "h": 60, "y": 90, "color": "#000000AA", "radius": 12,
                 "enter": "wipe-right"},
                {"type": "text", "text": "THE HOOK", "font": "Montserrat Black", "size": 40, "y": 90,
                 "stroke": {"color": "#000000", "width": 3}, "highlight": {"words": [1]}, "enter": "pop",
                 "exit": "whip-left", "shadow": True},
                {"type": "counter", "from": 0, "to": 900, "suffix": "K", "y": 200, "size": 44, "enter": "slide-up",
                 "count_duration": 1.0},
                {"type": "image", "src": "logo.png", "x": 180, "y": 330, "enter": "bounce"},
                {"type": "progress", "h": 4},
            ],
            "camera": {"zoom": [[0.8, 1.0], [1.0, 1.15, "out-cubic"], [1.4, 1.0, "in-out-cubic"]]},
            "effects": [{"type": "flash", "start": 1.0, "duration": 0.2},
                        {"type": "shake", "start": 1.0, "duration": 0.3},
                        {"type": "glitch", "start": 1.5, "duration": 0.2},
                        {"type": "zoom-blur", "start": 0.8, "duration": 0.3},
                        {"type": "vignette"}, {"type": "grade", "contrast": 1.1, "saturation": 1.2, "warmth": 0.1},
                        {"type": "grain", "strength": 0.03}, {"type": "letterbox", "start": 1.6, "duration": 0.4}],
            "captions": {"words": words, "pop": True},
            "audio": [{"src": "music.wav", "role": "music", "volume": 0.4, "loop": True, "fade_out": 0.5},
                      {"sfx": "whoosh", "start": 0.0}, {"sfx": "hit", "start": 1.0}],
        }

    def test_render_mp4(self):
        out = os.path.join(T["tmp"], "full.mp4")
        calls = []
        path = render.render(self.spec(), out, progress=lambda d, n: calls.append((d, n)), base_dir=T["tmp"])
        self.assertEqual(path, out)
        info = probe_streams(out)
        v = [s for s in info["streams"] if s["codec_type"] == "video"][0]
        a = [s for s in info["streams"] if s["codec_type"] == "audio"]
        self.assertEqual((v["codec_name"], v["width"], v["height"], v["r_frame_rate"]), ("h264", 216, 384, "10/1"))
        self.assertAlmostEqual(float(info["format"]["duration"]), 2.0, delta=0.06)
        self.assertEqual(len(a), 1)
        self.assertEqual((a[0]["codec_name"], a[0]["sample_rate"], a[0]["channels"]), ("aac", "48000", 2))
        self.assertEqual(len(calls), 20)
        self.assertEqual(calls[-1], (20, 20))

    def test_preview_and_still(self):
        out = os.path.join(T["tmp"], "prev.mp4")
        render.render(self.spec(), out, preview=True, base_dir=T["tmp"])
        v = probe_streams(out)["streams"][0]
        self.assertEqual((v["width"], v["height"], v["r_frame_rate"]), (108, 192, "5/1"))
        png = render.still(self.spec(), 0.5, os.path.join(T["tmp"], "still.png"), base_dir=T["tmp"])
        with Image.open(png) as im:
            with_caps = np.asarray(im.convert("RGB"))
        bare = render.render_frame(self.spec(), 0.5, base_dir=T["tmp"])
        self.assertEqual(with_caps.shape, bare.shape)
        band = slice(int(384 * 0.6), int(384 * 0.76))     # the default caption line sits at 68 %
        self.assertGreater(np.abs(with_caps[band].astype(int) - bare[band]).mean(), 1.0)

    def test_cli(self):
        spec_path = os.path.join(T["tmp"], "cli_spec.json")
        timeline.save(self.spec(), spec_path)
        png = os.path.join(T["tmp"], "cli.png")
        self.assertEqual(render.main([spec_path, "--still", "1.0", "--out", png, "--preview"]), 0)
        with Image.open(png) as im:
            self.assertEqual(im.size, (108, 192))
        bad = os.path.join(T["tmp"], "bad.json")
        timeline.save({"width": 100, "height": 100, "layers": [{"type": "shape", "color": "nope"}]}, bad)
        self.assertEqual(render.main([bad, "--out", os.path.join(T["tmp"], "bad.mp4"), "--quiet"]), 1)

    def test_filter_path_escaping(self):
        weird = os.path.join(T["tmp"], "we:ird [dir], it's; ok")
        os.makedirs(weird, exist_ok=True)
        flt = render.build_captions(timeline.normalize(S(captions={"words": [{"w": "hi", "s": 0, "e": 1}]})),
                                    180, 320, weird)
        r = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                            "color=black:s=180x320:d=0.5", "-vf", flt, "-frames:v", "1",
                            os.path.join(T["tmp"], "esc.png")], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        with Image.open(os.path.join(T["tmp"], "esc.png")) as im:
            self.assertGreater(np.asarray(im.convert("L")).max(), 200)
        self.assertEqual(render.ffmpeg_filter_path("/a:b/c'd,[e];f.ass"), r"/a\\:b/c\\\'d\,\[e\]\;f.ass")

    def test_caption_cards_end_at_silences_and_drop_trailing_punctuation(self):
        # a line, 10 s with no words (the line is on screen as a graphic), then more words
        words = [{"w": "Here", "s": 2.0, "e": 2.3}, {"w": "is", "s": 2.3, "e": 2.6}, {"w": "why.", "s": 2.6, "e": 3.0},
                 {"w": "Wait,", "s": 13.0, "e": 13.3}, {"w": "3.5", "s": 13.3, "e": 13.6},
                 {"w": "really?", "s": 13.6, "e": 14.0}]
        d = tempfile.mkdtemp(dir=T["tmp"])
        render.build_captions(timeline.normalize(S(captions={"words": words})), 180, 320, d)
        with open(os.path.join(d, "captions.ass"), encoding="utf-8") as f:
            events = [l for l in f.read().splitlines() if l.startswith("Dialogue:")]

        def secs(ts):
            h, m, s = ts.split(":")
            return int(h) * 3600 + int(m) * 60 + float(s)

        spans = [(secs(e.split(",")[1]), secs(e.split(",")[2])) for e in events]
        self.assertTrue(all(end <= 3.0 + 0.41 for start, end in spans if start < 5))   # no stale card in the gap
        self.assertTrue(any(start >= 13.0 for start, _ in spans))
        text = " ".join(re.sub(r"\{[^}]*\}", "", e.split(",,0,0,0,,", 1)[1]) for e in events)
        self.assertIn("WHY", text)
        self.assertNotIn("WHY.", text)
        self.assertNotIn("WAIT,", text)
        self.assertIn("3.5", text)          # decimals survive
        self.assertIn("REALLY?", text)      # tone marks stay


@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg not installed")
class AudioMix(unittest.TestCase):
    def mix(self, spec, dur=3.0, name="mix.wav"):
        out = os.path.join(T["tmp"], name)
        return audio.mixdown(S(duration=dur, **spec), dur, out, base_dir=T["tmp"]), out

    def read(self, path):
        with wave.open(path) as w:
            sr, ch, n = w.getframerate(), w.getnchannels(), w.getnframes()
            x = np.frombuffer(w.readframes(n), "<i2").reshape(-1, ch).astype(np.float32) / 32768
        return sr, ch, x

    def test_validate_checks_streams(self):
        errs = timeline.validate(S(layers=[{"type": "video", "src": "music.wav"}], audio=[{"src": "counter.mkv"}]),
                                 T["tmp"])
        self.assertTrue(any("has no video stream" in e for e in errs), errs)
        self.assertTrue(any("has no audio stream" in e for e in errs), errs)

    def test_nothing_audible(self):
        res, _ = self.mix({"layers": [{"type": "video", "src": "counter.mkv"}], "audio": [{"sfx": "pop", "volume": 0}]})
        self.assertIsNone(res)

    def test_ducking_length_and_loudness(self):
        res, out = self.mix({"audio": [{"src": "music.wav", "role": "music", "volume": 0.5, "duck": True},
                                       {"src": "voice.wav", "role": "voice"}]})
        self.assertEqual(res, out)
        sr, ch, x = self.read(out)
        self.assertEqual((sr, ch, len(x)), (48000, 2, 3 * 48000))

        def level(a, b, hz):
            seg = x[int(a * sr):int(b * sr), 0]
            spec = np.abs(np.fft.rfft(seg * np.hanning(len(seg))))
            f = np.fft.rfftfreq(len(seg), 1 / sr)
            return spec[(f > hz * 0.9) & (f < hz * 1.1)].max()
        before, during = level(0.2, 0.8, 200), level(1.4, 1.9, 200)
        self.assertLess(during, before * 0.6)          # music ducked under the voice (> 4.4 dB)
        self.assertGreater(level(1.4, 1.9, 2000), level(0.2, 0.8, 2000) * 10)
        r = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-i", out, "-af", "ebur128=framelog=quiet",
                            "-f", "null", "-"], capture_output=True, text=True)
        integrated = float(r.stderr.rsplit("I:", 1)[1].split("LUFS")[0])
        self.assertAlmostEqual(integrated, -14.0, delta=1.5)

    def test_clip_audio_sfx_and_speed(self):
        spec = timeline.normalize(S(duration=2, audio=[{"sfx": "ding", "start": 0.5}],
                                    layers=[{"type": "video", "src": "clip.mp4", "speed": 4.0, "volume": 1}]),
                                  T["tmp"])
        srcs = audio.sources(spec, 2.0)
        self.assertEqual(sorted(s["role"] for s in srcs), ["clip", "sfx"])
        clip = [s for s in srcs if s["role"] == "clip"][0]
        self.assertAlmostEqual(clip["dur"], 0.75)       # 3 s of source at 4x speed
        _, graph = audio.build_graph(srcs, 2.0)
        self.assertIn("atempo=2.000000,atempo=2.000000", graph)
        self.assertIn("adelay=500|500", graph)
        self.assertEqual(audio._atempo(0.25), ["atempo=0.500000", "atempo=0.500000"])
        res, out = self.mix({"layers": [{"type": "video", "src": "clip.mp4", "speed": 4.0}],
                             "audio": [{"sfx": "ding", "start": 0.5}]}, dur=2.0, name="clip.wav")
        self.assertEqual(self.read(out)[2].shape, (2 * 48000, 2))

    def test_jump_cut_has_no_click_and_a_split_take_no_dip(self):
        # a jump cut lands mid-waveform (440 Hz sine, phase jump at 1.0 s): the edge fades keep the step small
        jump = [{"type": "video", "src": "clip.mp4", "start": 0, "end": 1.0, "in": 0.0, "volume": 1},
                {"type": "video", "src": "clip.mp4", "start": 1.0, "end": 2.0, "in": 1.2731, "volume": 1}]
        _, out = self.mix({"layers": jump}, dur=2.0, name="jump.wav")
        sr, _, x = self.read(out)
        d = np.abs(np.diff(x[:, 0]))
        steady = d[int(0.4 * sr):int(0.9 * sr)].max()   # the sine's own largest sample-to-sample change
        self.assertLess(d[int(0.99 * sr):int(1.01 * sr)].max(), steady * 1.5)
        # the same take split in two (nothing removed): no fades at the seam, so no dip in level
        split = [{"type": "video", "src": "clip.mp4", "start": 0, "end": 1.0, "in": 0.0, "volume": 1},
                 {"type": "video", "src": "clip.mp4", "start": 1.0, "end": 2.0, "in": 1.0, "volume": 1}]
        srcs = audio.sources(timeline.normalize(S(duration=2.0, layers=split), T["tmp"]), 2.0)
        first, second = sorted(srcs, key=lambda s: s["start"])
        self.assertEqual((first["fade_out"], second["fade_in"]), (0.0, 0.0))
        self.assertGreater(first["fade_in"], 0)
        self.assertGreater(second["fade_out"], 0)


if __name__ == "__main__":
    unittest.main()
