"""Viral Studio auto-editor: transcripts, cut decisions, reframing, graphics, captions and a real preview render.

Offline: the "talking" video is synthesized with ffmpeg (testsrc2 picture, tone bursts where the words are),
the face model is only used when it is already on disk (set VSTUDIO_NET_TESTS=1 to test its download).
"""

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import wave

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np  # noqa: E402

from vstudio import autoedit, paths, reframe, render, timeline, transcribe  # noqa: E402

HAVE_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
T = {}
DUR = 10.0
# the script: a hook, an "um", a false start, a sentence with a number, a long pause, a question, a CTA
SCRIPT = [("Here's", .30, .62), ("the", .64, .84), ("secret.", .86, 1.20), ("um", 1.50, 1.75),
          ("The", 2.00, 2.20), ("best", 2.22, 2.42), ("way", 2.44, 2.64), ("to", 2.66, 2.86),
          ("The", 3.36, 3.56), ("best", 3.58, 3.78), ("way", 3.80, 4.00), ("to", 4.02, 4.22), ("grow", 4.24, 4.54),
          ("is", 4.56, 4.76), ("73%", 4.78, 5.18), ("of", 5.20, 5.40), ("consistency.", 5.42, 5.90),
          ("Do", 7.20, 7.36), ("you", 7.38, 7.52), ("post", 7.54, 7.80), ("every", 7.82, 8.05), ("day?", 8.07, 8.40),
          ("Follow", 8.60, 8.85), ("for", 8.87, 9.05), ("more.", 9.07, 9.40)]
SMALL = (216, 384)


def W(text, t0=0.0, step=0.3, gaps=None):
    """Evenly timed words; gaps = {word index: extra pause before it}."""
    out, t = [], t0
    for i, w in enumerate(text.split()):
        t += (gaps or {}).get(i, 0.0)
        out.append({"w": w, "s": round(t, 3), "e": round(t + step - 0.04, 3)})
        t += step
    return out


def _ff(*args):
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"] + list(args), check=True)


def setUpModule():
    T["tmp"] = tempfile.mkdtemp(prefix="vs_autoedit_")
    T["cache"], T["models"] = paths.CACHE, paths.MODELS
    paths.CACHE = os.path.join(T["tmp"], "cache")
    T["words"] = [{"w": w, "s": s, "e": e} for w, s, e in SCRIPT]
    T["words_json"] = os.path.join(T["tmp"], "words.json")
    with open(T["words_json"], "w", encoding="utf-8") as f:
        json.dump(T["words"], f)
    if not HAVE_FFMPEG:
        return
    sr = 48000
    t = np.arange(int(DUR * sr)) / sr
    x = np.zeros_like(t)
    for i, (w, s, e) in enumerate(SCRIPT):
        m = (t >= s) & (t < e)
        ramp = np.minimum(1, np.minimum((t[m] - s) / 0.01, (e - t[m]) / 0.01))
        x[m] = (0.5 if w == "73%" else 0.25) * ramp * np.sin(2 * np.pi * (180 + 40 * (i % 5)) * t[m])
    voice = os.path.join(T["tmp"], "voice.wav")
    with wave.open(voice, "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(sr)
        f.writeframes((x * 32767).astype("<i2").tobytes())
    T["video"] = os.path.join(T["tmp"], "raw.mp4")
    _ff("-f", "lavfi", "-i", "testsrc2=s=320x180:r=15:d=%g" % DUR, "-i", voice, "-c:v", "libx264",
        "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", T["video"])


def tearDownModule():
    paths.CACHE, paths.MODELS = T["cache"], T["models"]
    shutil.rmtree(T["tmp"], ignore_errors=True)


def plan(words="script", **opts):
    o = {"size": SMALL, "fps": 15, "download_model": False}
    o.update(opts)
    return autoedit.plan_edit(T["video"], T["words"] if words == "script" else words, o)


def clips(spec):
    return [L for L in spec["layers"] if L["type"] == "video" and str(L.get("id", "")).startswith("clip")]


# =========================================================================== transcribe

class TestTranscribe(unittest.TestCase):
    def test_clean_splits_sorts_and_fills(self):
        w = transcribe.clean([{"word": " so yeah ", "start": 1, "end": 1.6}, {"w": "hi", "s": 0.2, "e": 0.5},
                              {"w": "", "s": 3, "e": 4}, {"w": "x", "s": 1.5, "e": None}])
        self.assertEqual([x["w"] for x in w], ["hi", "so", "yeah", "x"])
        self.assertEqual((w[1]["s"], w[1]["e"], w[2]["s"]), (1.0, 1.3, 1.3))
        self.assertEqual(w[2]["e"], 1.5, "an overlapping word ends where the next starts")
        self.assertAlmostEqual(w[3]["e"], 1.9, places=3)
        for a, b in zip(w, w[1:]):
            self.assertLessEqual(a["e"], b["s"])

    def test_words_json_shapes(self):
        d = T["tmp"]
        shapes = {
            "list.json": [{"w": "a", "s": 0, "e": 0.2}],
            "dict.json": {"words": [{"word": "a", "start": 0, "end": 0.2}]},
            "whisper.json": {"segments": [{"start": 0, "end": 1, "text": "a b", "words": [
                {"word": " a", "start": 0, "end": 0.2}, {"word": " b", "start": 0.3, "end": 0.5}]}]},
            "segments.json": {"segments": [{"start": 0, "end": 1, "text": "one two"}]},
            "yt.json3": {"events": [{"tStartMs": 0, "segs": [{"utf8": "hi"}, {"utf8": " there", "tOffsetMs": 400}]}]},
        }
        for name, data in shapes.items():
            path = os.path.join(d, name)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f)
            words = transcribe.load_words_json(path) if name.endswith(".json") else transcribe.load_captions(path)
            self.assertTrue(words, name)
            self.assertTrue(all(set(("w", "s", "e")) <= set(x) for x in words), name)
        self.assertEqual([x["w"] for x in transcribe.load_words_json(os.path.join(d, "segments.json"))],
                         ["one", "two"])

    def test_srt_and_precedence(self):
        d = T["tmp"]
        srt = os.path.join(d, "talk.srt")
        with open(srt, "w", encoding="utf-8") as f:
            f.write("1\n00:00:01,000 --> 00:00:02,000\nhello there\n\n2\n00:00:03,000 --> 00:00:03,500\nbye\n")
        words = transcribe.load_captions(srt)
        self.assertEqual([x["w"] for x in words], ["hello", "there", "bye"])
        self.assertEqual((words[0]["s"], words[1]["e"]), (1.0, 2.0))
        w, src, _ = transcribe.load_words(None, captions=srt, words_json=T["words_json"])
        self.assertTrue(src.startswith("words file"), "a words file wins over captions")
        self.assertEqual(len(w), len(SCRIPT))
        # a captions file next to the media is found by name
        media = os.path.join(d, "talk.mp4")
        open(media, "wb").close()
        w, src, _ = transcribe.load_words(media)
        self.assertIn("next to the video", src)
        self.assertEqual(len(w), 3)

    def test_nothing_available(self):
        words, src, notes = transcribe.load_words(None)
        self.assertIsNone(words)
        self.assertIsNone(src)
        self.assertIsNone(transcribe.get_words(None))

    def test_remap(self):
        words = [{"w": "a", "s": 0.0, "e": 0.4}, {"w": "b", "s": 1.0, "e": 1.4}, {"w": "c", "s": 2.0, "e": 2.4},
                 {"w": "d", "s": 3.0, "e": 3.4, "i": 7}]
        segs = [{"in": 0.0, "out": 0.5, "t0": 0.0}, {"in": 2.3, "out": 3.6, "t0": 0.5}]
        out = transcribe.remap(words, segs)
        self.assertEqual([x["w"] for x in out], ["a", "d"], "b falls in a cut; only 0.1 s of c survives")
        self.assertEqual((out[1]["s"], out[1]["e"]), (1.2, 1.6))
        self.assertEqual(out[1]["i"], 7, "extra keys are kept")
        out = transcribe.remap([{"w": "c", "s": 2.0, "e": 2.4}], [{"in": 2.15, "out": 3.0, "t0": 1.0}])
        self.assertEqual((out[0]["s"], out[0]["e"]), (1.0, 1.25), "a mostly kept word is clipped to its segment")


# =========================================================================== cut decisions (pure)

class TestCutDecisions(unittest.TestCase):
    def test_fillers(self):
        ws = W("so um I think like you know this works", gaps={1: 0.0, 5: 0.4, 6: 0.0, 7: 0.4})
        drop = autoedit.mark_fillers(ws)
        self.assertIn(1, drop, "um is always a filler")
        self.assertIn(0, drop, "'so' next to an 'um' is part of the hesitation")
        self.assertNotIn(4, drop, "'like' inside running speech stays")
        self.assertIn(5, drop)
        self.assertIn(6, drop, "'you know' between pauses goes")
        ws = W("I I think the the plan works")
        drop = autoedit.mark_fillers(ws)
        self.assertEqual(sorted(drop), [0, 3])
        self.assertIn("stutter", drop[0][1])

    def test_retakes(self):
        ws = W("the best way to the best way to grow is focus", gaps={4: 0.5})
        drop = autoedit.mark_retakes(ws, {})
        self.assertEqual(sorted(drop), [0, 1, 2, 3])
        self.assertIn("the best way to", drop[0][1])
        # an unfinished phrase restarted word for word, back to back, is a flub even without a pause
        self.assertEqual(sorted(autoedit.mark_retakes(W("the best way to the best way to grow"), {})), [0, 1, 2, 3])
        # no hesitation and not back to back: the repeated opening stays
        self.assertEqual(autoedit.mark_retakes(W("the best way to win is the best way to grow"), {}), {})
        # a complete sentence said twice in a row is deliberate emphasis and stays
        self.assertEqual(autoedit.mark_retakes(W("Stop doing this now. Stop doing this now."), {}), {})
        # a complete different sentence with the same opening (anaphora) stays
        ws = W("We will fight on the beaches. We will fight on the landing grounds.", gaps={6: 0.5})
        self.assertEqual(autoedit.mark_retakes(ws, {}), {})
        # the same sentence said twice: the first take goes
        ws = W("This is the best camera. This is the best camera for beginners.", gaps={5: 0.6})
        self.assertEqual(sorted(autoedit.mark_retakes(ws, {})), [0, 1, 2, 3, 4])
        # three takes keep the last
        ws = W("my first tip my first tip my first tip is hooks", gaps={3: 0.5, 6: 0.5})
        self.assertEqual(sorted(autoedit.mark_retakes(ws, {})), list(range(6)))

    def test_keep_ranges(self):
        E = autoedit.ENERGY["high"]
        ws = W("one two three four", t0=0.5, gaps={2: 1.0})
        drop = {}
        ranges, between, head, tail = autoedit.keep_ranges(ws, drop, 3.5, E)
        self.assertEqual(head["out"], 0.4)
        self.assertEqual(len(between), 1)
        cut = between[0]
        self.assertEqual(cut["kind"], "DEAD")
        self.assertAlmostEqual(cut["in"], ws[1]["e"] + E["tail"], places=3)
        self.assertAlmostEqual(cut["out"], ws[2]["s"] - E["lead"], places=3)
        self.assertEqual(ranges[0][1], cut["in"])
        self.assertEqual(ranges[1][0], cut["out"])
        self.assertAlmostEqual(ranges[-1][1], ws[-1]["e"] + autoedit.END_TAIL, places=3)
        # a filler packed between words is cut at its own edges
        ws = W("one um two", step=0.25)
        ranges, between, _, _ = autoedit.keep_ranges(ws, {1: ("FILLER", '"um"')}, 1.0, E)
        self.assertEqual(between[0]["kind"], "FILLER")
        self.assertLessEqual(between[0]["in"], ws[1]["s"] + 1e-6)
        self.assertGreaterEqual(between[0]["out"], ws[1]["e"] - 1e-6)

    def test_refine_words_against_audio(self):
        # a captions cue spreads three words evenly, but the speaker paused from 1.0 s to 1.62 s
        db = np.full(200, -80.0, np.float32)
        db[0:100] = -20.0
        db[162:200] = -20.0
        env = {"db": db, "hop": 0.01, "thr": -50.0, "contrast": 60.0}
        ws = [{"w": "one", "s": 0.0, "e": 0.7}, {"w": "two", "s": 0.7, "e": 1.4}, {"w": "three", "s": 1.4, "e": 2.0}]
        out, moved = autoedit.refine_words(ws, env)
        self.assertEqual(moved, 2)
        self.assertAlmostEqual(out[1]["e"], 1.06, places=2)
        self.assertAlmostEqual(out[2]["s"], 1.58, places=2)
        self.assertEqual(out[0], ws[0])
        ranges, between, _, _ = autoedit.keep_ranges(out, {}, 2.0, autoedit.ENERGY["high"], env)
        self.assertEqual(len(between), 1, "the hidden pause becomes a cut")
        self.assertEqual(autoedit.refine_words(ws, None), (ws, 0), "no audio: words stay as given")

    def test_merge_short(self):
        ranges = [[0, 2], [2.5, 2.7], [3.0, 5.0]]
        between = [{"kind": "DEAD", "in": 2, "out": 2.5}, {"kind": "FILLER", "in": 2.7, "out": 3.0}]
        r, b = autoedit.merge_short(ranges, between)
        self.assertEqual(r, [[0, 2.7], [3.0, 5.0]], "the 0.2 s piece joins its neighbour across the pause cut")
        self.assertEqual([c["kind"] for c in b], ["FILLER"])

    def test_numbers(self):
        def spans(text):
            return [(s["display"], s["kind"]) for s in autoedit.number_spans(W(text))]
        self.assertEqual(spans("I made $4.2M in 2024"), [("$4.2M", "money"), ("2024", "year")])
        self.assertEqual(spans("seventy three percent of people"), [("73%", "percent")])
        self.assertEqual(spans("it cost ten thousand dollars"), [("$10,000", "money")])
        self.assertEqual(spans("this is 3x faster"), [("3x", "multiplier")])
        self.assertEqual(spans("1.5 million people"), [("1,500,000", "count")])
        self.assertEqual(spans("two hundred and fifty followers."), [("250", "count")])
        self.assertEqual(spans("a million views"), [("1,000,000", "count")])
        self.assertEqual(spans("the 2nd step"), [("2nd", "ordinal")])
        self.assertEqual(spans("no numbers here"), [])


class TestSpeechGraphics(unittest.TestCase):
    def sents(self, *texts):
        out, t = [], 0.0
        for x in texts:
            ws = W(x, t)
            out.append({"words": ws, "s": ws[0]["s"], "e": ws[-1]["e"]})
            t = ws[-1]["e"] + 0.5
        return out

    def test_callouts(self):
        S = self.sents("Here are three mistakes.", "First, you post without a hook.", "Second, your captions are tiny.",
                       "Do you post every day?", "iPhone versus Android", 'He said "never post without a plan" ok.',
                       "I grew 300% in a month.", "It made me $10,000 last month.", "Step 2 is editing the hook.")
        got = {}
        for c in autoedit.find_callouts(S):
            got.setdefault(c["template"], []).append(c["params"])
        self.assertEqual([p["number"] for p in got["list_item"]], [1, 2])
        self.assertEqual(got["list_item"][0]["text"], "You post without a hook")
        self.assertEqual(got["list_item"][0]["total"], 2)
        self.assertEqual(got["question"][0]["text"], "Do you post every day?")
        self.assertEqual(got["versus"][0], {"left": "iPhone", "right": "Android"})
        self.assertEqual(got["quote"][0]["text"], "never post without a plan")
        self.assertEqual(got["stat"][0]["value"], "300%")
        self.assertEqual(got["stat"][0]["label"], "In a month")
        self.assertEqual((got["counter_reveal"][0]["value"], got["counter_reveal"][0]["prefix"]), (10000.0, "$"))
        self.assertEqual(got["step_number"][0]["number"], 2)
        self.assertNotIn("total", got["step_number"][0], "a single step is not a numbered list")

    def test_list_points_hand_over_in_every_preset(self):
        S = self.sents("Hook line here.", "Here are three mistakes.", "First, you post without a hook.",
                       "Second, your captions are tiny.", "Third, you never post consistently.", "That is it.")
        for name, E in autoedit.ENERGY.items():
            plan, busy, hook_end = autoedit._schedule(S, [], None, 20.0, E, autoedit._opts({}))
            pts = [g for g in plan if g["template"] == "list_item"]
            self.assertEqual([g["params"]["number"] for g in pts], [1, 2, 3], name)
            for a, b in zip(pts, pts[1:]):
                self.assertLessEqual(a["t1"], b["t0"], "points never overlap")
                self.assertGreaterEqual(a["t1"] - a["t0"], 1.2)
            self.assertEqual(plan[0]["template"], "hook_title")
            self.assertGreaterEqual(pts[0]["t0"], hook_end)

    def test_single_ordinal_is_not_a_list(self):
        S = self.sents("First, let me say thanks.", "This works.")
        self.assertFalse([c for c in autoedit.find_callouts(S) if c["template"] == "list_item"])

    def test_hook_and_cta_text(self):
        text, hl = autoedit.hook_phrase(W("So okay guys today I want to show you the biggest mistake beginners "
                                          "make on Instagram"))
        self.assertIn("biggest mistake", text)
        self.assertEqual(hl, ["biggest"])
        self.assertEqual(autoedit.hook_phrase(W("I lost $300 on one setting.")), ("I lost $300 on one setting",
                                                                                   ["$300"]))
        ws = W("so follow for part 2 and comment GUIDE")
        self.assertEqual(autoedit.cta_text(ws, 1, "follow"), "follow for part 2")
        self.assertEqual(autoedit.cta_text(ws, 6, "comment"), 'COMMENT "GUIDE"')
        words = W("Thanks for watching. So follow for more.")
        sents = autoedit.sentences(words)
        self.assertEqual(len(sents), 2)
        self.assertEqual(autoedit.detect_cta(words, sents)[2:], ("follow", "plus"))
        self.assertIsNone(autoedit.detect_cta(W("I like this camera."), [[0, 1, 2, 3]]))
        for text in ("If you follow these steps you win.", "You should save money first."):
            ws = W(text)
            self.assertIsNone(autoedit.detect_cta(ws, [list(range(len(ws)))]), text)
        ws = W("Save this for later.")
        self.assertEqual(autoedit.detect_cta(ws, [list(range(len(ws)))])[2:], ("save", "bookmark"))


class TestPlacement(unittest.TestCase):
    def test_bands_avoid_face_and_captions(self):
        Wd, Hd = 1080, 1920
        face = [340, 500, 740, 1000]
        cap = (1280, 1400)
        bands = autoedit.free_bands(Wd, Hd, face, cap, wide=False)
        names = [b[0] for b in bands]
        self.assertEqual(names, ["top", "middle", "low"])
        for name, x0, y0, x1, y1 in bands:
            self.assertTrue(y1 <= face[1] or y0 >= face[3], name)
            self.assertTrue(y1 <= cap[0] or y0 >= cap[1], name)
            self.assertGreaterEqual(y0, 0.11 * Hd - 1e-6)
            self.assertLessEqual(y1, 0.80 * Hd + 1e-6)

    def test_captions_move_off_the_face(self):
        E = autoedit.ENERGY["medium"]
        seg = {"k": 1.0, "F": [540.0, 700.0], "face": [390.0, 520.0, 690.0, 880.0]}
        style = dict(autoedit.default_captions(1080, 1920, "medium"), center_y_pct=40.0)   # on the eyes
        moved, note = autoedit.fit_captions(style, [seg], 1080, 1920, E)
        top, bottom = autoedit.caption_band(moved, 1920)
        self.assertGreater(top, 880.0, "moved under the chin (with the zoom margin)")
        self.assertLessEqual(bottom, 0.80 * 1920)
        self.assertIn("40 %", note)
        self.assertEqual(moved["font_size_pct"], style["font_size_pct"], "only the position changes")
        low = dict(seg, face=[390.0, 900.0, 690.0, 1450.0], F=[540.0, 1175.0])   # a face low in the frame
        moved, note = autoedit.fit_captions(style, [low], 1080, 1920, E)
        self.assertLess(autoedit.caption_band(moved, 1920)[1], 900.0, "no room below: above the head")
        clear = dict(style, center_y_pct=70.0)
        self.assertEqual(autoedit.fit_captions(clear, [seg], 1080, 1920, E), (clear, None))
        self.assertEqual(autoedit.fit_captions(style, [dict(seg, face=None)], 1080, 1920, E), (style, None))

    def test_place_template_fits_band(self):
        canvas = {"width": 1080, "height": 1920, "fps": 30}
        band = ("middle", 64.8, 1040.0, 1015.2, 1250.0)
        lay, info = autoedit.place_template("stat", {"value": "73%", "label": "of viewers decide in 3 seconds"},
                                            1.0, 3.0, canvas, None, [band], compact={"value": "73%", "label": ""})
        self.assertIsNotNone(lay)
        x0, y0, x1, y1 = autoedit.extent(lay, 1080, 1920)
        self.assertGreaterEqual(y0, band[2] - 1)
        self.assertLessEqual(y1, band[4] + 1)
        self.assertGreaterEqual(x0, band[1] - 1)
        self.assertLessEqual(x1, band[3] + 1)
        self.assertGreaterEqual(info["scale"], 0.64)
        lay, why = autoedit.place_template("stat", {"value": "73%"}, 1, 3, canvas, None, [("top", 0, 0, 1080, 80)])
        self.assertIsNone(lay)
        self.assertIn("no room", why)

    def test_narrow_side_band_never_beats_a_readable_full_width_one(self):
        # 16:9: a 170 px strip beside the face fits the stat "at 100 %" of its own little canvas, but the
        # text would be a quarter of the size the full-width band above gives it
        canvas = {"width": 1920, "height": 1080, "fps": 30}
        top, side = ("top", 96.0, 86.4, 1824.0, 520.0), ("right", 1650.0, 86.4, 1820.0, 950.0)
        params = {"value": "73%", "label": "of players quit early"}
        lay, info = autoedit.place_template("stat", params, 1.0, 3.0, canvas, None, [side, top])
        self.assertEqual(info["band"], "top")
        side_only, side_info = autoedit.place_template("stat", params, 1.0, 3.0, canvas, None, [side])
        self.assertLess(side_info["scale"], 0.5)                     # reported as the size it reads at
        self.assertGreater(autoedit._text_px(lay), 2 * autoedit._text_px(side_only))


# =========================================================================== reframe

class TestReframe(unittest.TestCase):
    def test_smooth_outlier_gap_and_shot_change(self):
        times = [i * 0.5 for i in range(12)]
        raw = [(0.3, 0.4, 0.1, 0.2)] * 5 + [None] + [(0.3, 0.4, 0.1, 0.2)] * 2 + [(0.75, 0.4, 0.1, 0.2)] * 4
        raw[2] = (0.9, 0.9, 0.1, 0.2)   # a one-off false detection
        values, runs = reframe.smooth(raw, times)
        self.assertAlmostEqual(values[2][0], 0.3, places=2, msg="the median removes the outlier")
        self.assertAlmostEqual(values[5][0], 0.3, places=2, msg="a missing sample takes its run's track")
        self.assertAlmostEqual(values[-1][0], 0.75, places=2)
        self.assertNotEqual(runs[0], runs[-1], "a jump starts a new run, so smoothing does not blend shots")
        self.assertIsNone(reframe.smooth([None, None], [0, 1]))

    def test_focus_for(self):
        self.assertEqual(reframe.focus_for(0.3, 0.4, (1920, 1080), (9, 16)), [0.3, 0.5])
        fx, fy = reframe.focus_for(0.5, 0.3, (1080, 1920), (16, 9))
        self.assertEqual(fx, 0.5)
        self.assertGreater(fy, 0.3, "headroom: the crop centre sits below the face")
        self.assertEqual(reframe.focus_for(0.2, 0.2, (1080, 1920), (9, 16)), [0.5, 0.5])

    def test_detect_faces_on_synthetic_frames(self):
        blank = np.zeros((180, 320, 3), np.uint8)
        self.assertEqual(reframe.detect_faces(blank), [])
        self.assertEqual(reframe.detect_faces(None), [])
        rng = np.random.default_rng(1)
        noise = rng.integers(0, 255, (720, 1280, 3), dtype=np.uint8)
        faces = reframe.detect_faces(noise)   # runs whichever detector exists; may find nothing
        for f in faces:
            self.assertEqual(len(f), 5)

    def test_model_download_and_detect(self):
        """With VSTUDIO_NET_TESTS=1 the YuNet model is downloaded into a temporary models folder."""
        net = bool(os.environ.get("VSTUDIO_NET_TESTS"))
        if net:
            paths.MODELS = os.path.join(T["tmp"], "models")
            reframe._STATE["yunet"] = None
        try:
            path = reframe.ensure_model(download=net)
            if not path:
                self.skipTest("YuNet model not on disk (set VSTUDIO_NET_TESTS=1 to download it)")
            self.assertTrue(reframe._valid(path))
            self.assertEqual(reframe.method(), "yunet")
            self.assertEqual(reframe.detect_faces(np.full((360, 640, 3), 127, np.uint8)), [])
        finally:
            paths.MODELS = T["models"]
            reframe._STATE["yunet"] = None

    @unittest.skipUnless(HAVE_FFMPEG, "ffmpeg not installed")
    def test_track_without_face_falls_back_to_centre(self):
        tr = reframe.track(T["video"], [0.5, 2.0, 4.0, 8.0], aspect=(9, 16), download=False)
        self.assertEqual(tr["method"], "centre")
        self.assertEqual(tr["found"], 0)
        self.assertEqual([p["focus"] for p in tr["points"]], [[0.5, 0.5]] * 4)
        self.assertTrue(all(p["face"] is None for p in tr["points"]))
        frames = reframe.sample_frames(T["video"], [0.5, 2.0, 9.0])
        self.assertEqual(sorted(frames), [0.5, 2.0, 9.0])
        self.assertEqual(frames[0.5].shape, (180, 320, 3))


# =========================================================================== the plan on a real file

@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg not installed")
class TestPlanEdit(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.spec = plan()
        cls.rep = cls.spec["meta"]["edit"]

    def test_valid_spec(self):
        self.assertEqual(timeline.validate(self.spec), [])
        self.assertEqual((self.spec["width"], self.spec["height"], self.spec["fps"]), (216, 384, 15.0))

    def test_cut_list(self):
        cuts = self.rep["cuts"]
        self.assertEqual([c["kind"] for c in cuts], ["DEAD", "RETAKE", "DEAD"])
        head, retake, pause = cuts
        self.assertEqual((head["in"], head["out"]), (0.0, 0.2))
        self.assertIn('first take of "The best way to"', retake["why"])
        self.assertIn('"um"', retake["why"], "the merged cut names the filler it also removed")
        E = autoedit.ENERGY["high"]
        # breath kept: tail after the last kept word, lead before the next (cut points snap within 30 ms)
        self.assertAlmostEqual(retake["in"], 1.20 + E["tail"], delta=0.031)
        self.assertAlmostEqual(retake["out"], 3.36 - E["lead"], delta=0.031)
        self.assertAlmostEqual(pause["in"], 5.90 + E["tail"], delta=0.031)
        self.assertAlmostEqual(pause["out"], 7.20 - E["lead"], delta=0.031)
        self.assertIn("1.30 s pause", pause["why"])

    def test_layers_are_consecutive_jump_cuts(self):
        cl = clips(self.spec)
        self.assertEqual(len(cl), 3)
        self.assertEqual(cl[0]["start"], 0.0)
        for a, b in zip(cl, cl[1:]):
            self.assertEqual(a["end"], b["start"])
        self.assertEqual(cl[-1]["end"], self.spec["duration"])
        cuts = self.rep["cuts"]
        self.assertEqual([L["in"] for L in cl], [cuts[0]["out"], cuts[1]["out"], cuts[2]["out"]])
        self.assertTrue(all(L["volume"] == 1.0 and L["fit"] == "cover" for L in cl))
        fps = self.spec["fps"]
        for L in cl:   # whole frames, boundaries never after the frame they belong to
            n = (L["end"] - L["start"]) * fps
            self.assertLess(abs(n - round(n)), 0.05)
        # punch-in alternates: the second shot is scaled up, the others are not
        self.assertNotIn("w", cl[0])
        self.assertGreater(cl[1]["w"], self.spec["width"])
        self.assertGreater(cl[1]["h"], self.spec["height"])
        self.assertNotIn("w", cl[2])
        self.assertLess(self.spec["duration"], DUR - 3.0)

    def test_words_remapped(self):
        words = self.spec["captions"]["words"]
        texts = [w["w"] for w in words]
        self.assertEqual(len(words), 20)
        self.assertNotIn("um", texts)
        self.assertEqual(texts[:5], ["Here's", "the", "secret.", "The", "best"])
        cl = clips(self.spec)
        w73 = words[texts.index("73%")]
        self.assertAlmostEqual(w73["s"], cl[1]["start"] + 4.78 - cl[1]["in"], places=2)
        self.assertAlmostEqual(words[0]["s"], 0.1, places=2)
        for a, b in zip(words, words[1:]):
            self.assertLessEqual(a["e"], b["s"] + 1e-6)

    def test_graphics(self):
        names = [g["template"] for g in self.rep["graphics"]]
        self.assertEqual(names, ["hook_title", "stat", "cta"])
        hook, stat, cta = self.rep["graphics"]
        self.assertEqual(hook["text"], "Here's the secret")
        self.assertEqual(hook["start"], 0.0)
        self.assertEqual(stat["text"], "73%")
        self.assertEqual(cta["text"], "Follow for more")
        self.assertAlmostEqual(cta["end"], self.spec["duration"], places=2)
        fixed = [L for L in self.spec["layers"] if L["type"] in ("text", "shape", "counter", "progress")]
        self.assertTrue(fixed and all(L["fixed"] for L in fixed))
        ids = [L["id"] for L in self.spec["layers"]]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertTrue(any(L["type"] == "progress" for L in self.spec["layers"]), "short-form gets a progress bar")
        # inside the safe zone and clear of the captions
        H = self.spec["height"]
        band = autoedit.caption_band(self.spec["captions"]["style"], H)
        for g in self.rep["graphics"]:
            x0, y0, x1, y1 = g["box"]
            self.assertGreaterEqual(y0, 0.11 * H - 1)
            self.assertLessEqual(y1, 0.80 * H + 1)
            self.assertTrue(y1 <= band[0] or y0 >= band[1], g)
        # at most one callout every few seconds
        starts = [g["start"] for g in self.rep["graphics"] if g["template"] not in ("hook_title", "cta")]
        for a, b in zip(starts, starts[1:]):
            self.assertGreaterEqual(b - a, autoedit.ENERGY["high"]["graphic_spacing"])

    def test_camera_and_sound(self):
        cam = self.spec["camera"]
        ts = [k[0] for k in cam["zoom"]]
        self.assertEqual(ts, sorted(ts))
        self.assertTrue(all(k[1] >= 1.0 for k in cam["zoom"]))
        self.assertEqual(len(cam["x"]), len(cam["zoom"]))
        words = [z["word"] for z in self.rep["zooms"]]
        self.assertIn("73%", words, "the number gets an emphasis punch")
        for z in self.rep["zooms"]:
            self.assertLessEqual(z["zoom"], autoedit.ENERGY["high"]["zoom"])
        sfx = [a["sfx"] for a in self.spec["audio"]]
        self.assertIn("hit", sfx)
        self.assertIn("pop", sfx)
        self.assertIn("whoosh", sfx)
        starts = [a["start"] for a in self.spec["audio"]]
        self.assertEqual(starts, sorted(starts))
        self.assertTrue(all(0 <= s < self.spec["duration"] for s in starts))

    def test_report(self):
        md = autoedit.report_md(self.spec, T["tmp"])
        for needle in ("10.00 s", "RETAKE", '"um"', "hook_title", "## Cuts (3", "Reframe", "centre crop"):
            self.assertIn(needle, md)
        self.assertEqual(self.rep["focus"]["method"], "centre")

    def test_preview_render_is_shorter_and_keeps_the_words(self):
        spec = plan(sfx=False)
        out = os.path.join(T["tmp"], "edited.mp4")
        render.render(spec, out, preview=True)
        info = timeline.probe(out)
        self.assertTrue(info["has_audio"] and info["has_video"])
        self.assertEqual((info["width"], info["height"]), (SMALL[0] // 2, SMALL[1] // 2))
        self.assertAlmostEqual(info["duration"], spec["duration"], delta=0.2)
        self.assertLess(info["duration"], DUR - 3.0)
        # the sound follows the cuts: loud inside kept words, quiet in the breath between two shots
        r = subprocess.run(["ffmpeg", "-v", "error", "-i", out, "-ac", "1", "-ar", "16000", "-f", "f32le", "-"],
                           capture_output=True, check=True)
        x = np.frombuffer(r.stdout, np.float32)

        def rms(a, b):
            seg = x[int(a * 16000):int(b * 16000)]
            return float(np.sqrt(np.mean(seg ** 2))) if len(seg) else 0.0

        words = spec["captions"]["words"]
        loud = [rms(w["s"] + 0.05, w["e"] - 0.05) for w in words]
        self.assertGreater(min(loud), 0.02, "every kept word is audible in the edit")
        cut = clips(spec)[2]["start"]       # "consistency." ends 0.08 s before this cut, "Do" starts 0.06 s after
        self.assertLess(rms(cut - 0.05, cut - 0.01), 0.1 * float(np.median(loud)))


@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg not installed")
class TestModes(unittest.TestCase):
    def test_silence_only_fallback(self):
        spec = plan(words=None)
        rep = spec["meta"]["edit"]
        self.assertEqual(timeline.validate(spec), [])
        self.assertIsNone(spec["captions"])
        self.assertFalse(rep["graphics"])
        self.assertFalse([L for L in spec["layers"] if L["type"] in ("text", "counter")])
        self.assertTrue(any("no transcript" in n for n in rep["not_done"]))
        kinds = [(c["kind"], round(c["in"], 1)) for c in rep["cuts"]]
        self.assertTrue(len(kinds) >= 2 and all(k == "DEAD" for k, _ in kinds), kinds)
        self.assertTrue(any(5.8 < c["in"] < 6.1 and 7.0 < c["out"] < 7.2 for c in rep["cuts"]), rep["cuts"])
        self.assertLess(spec["duration"], DUR - 1.0)
        self.assertIn("silence-based", autoedit.report_md(spec))

    def test_max_duration_trims_the_middle(self):
        spec = plan(max_duration=5.5)
        rep = spec["meta"]["edit"]
        self.assertLessEqual(spec["duration"], 5.5)
        trim = [c for c in rep["cuts"] if c["kind"] == "TRIM"]
        self.assertEqual(len(trim), 1)
        self.assertIn("Do you post every day", trim[0]["why"])
        texts = [w["w"] for w in spec["captions"]["words"]]
        self.assertIn("secret.", texts, "the hook stays")
        self.assertIn("more.", texts, "the ending stays")
        self.assertNotIn("post", texts)

    def test_options_off_and_square(self):
        spec = plan(zoom=False, graphics=False, sfx=False, captions=False, aspect="1:1", size=None, fps=None)
        self.assertEqual((spec["width"], spec["height"], spec["fps"]), (1080, 1080, 30.0))
        self.assertTrue(all("w" not in L for L in clips(spec)), "no punch-ins")
        self.assertEqual(spec["camera"]["zoom"], [[0, 1.0]])
        self.assertIsNone(spec["captions"])
        self.assertEqual(spec["audio"], [])
        self.assertFalse([L for L in spec["layers"] if L["type"] in ("text", "shape", "counter", "progress")])
        self.assertEqual(timeline.validate(spec), [])

    def test_broll_music_and_style(self):
        d = os.path.join(T["tmp"], "broll")
        os.makedirs(d, exist_ok=True)
        from PIL import Image
        Image.new("RGB", (64, 48), (200, 120, 40)).save(os.path.join(d, "consistency-chart.jpg"))
        _ff("-f", "lavfi", "-i", "testsrc=s=160x90:r=10:d=3", "-c:v", "libx264", "-pix_fmt", "yuv420p",
            os.path.join(d, "unrelated.mp4"))
        dna = os.path.join(T["tmp"], "style_dna.json")
        with open(dna, "w", encoding="utf-8") as f:
            json.dump({"text": {"caption_track": {"font_size_pct": 6.0, "center_y_pct": 55.0, "highlight": "#00E5FF",
                                                  "case": "upper", "samples": 12}}}, f)
        spec = plan(broll=d, music=os.path.join(T["tmp"], "voice.wav"), style=dna, graphics=False)
        rep = spec["meta"]["edit"]
        self.assertEqual(timeline.validate(spec), [])
        b = [L for L in spec["layers"] if str(L.get("id", "")).startswith("broll")]
        self.assertEqual(len(b), 1)
        self.assertTrue(b[0]["src"].endswith("consistency-chart.jpg"))
        self.assertEqual(b[0]["type"], "image")
        self.assertTrue(b[0]["kenburns"])
        word = next(w for w in spec["captions"]["words"] if w["w"].startswith("consistency"))
        self.assertAlmostEqual(b[0]["start"], word["s"] - 0.15, places=2)
        self.assertTrue(1.99 <= b[0]["end"] - b[0]["start"] <= 3.01, b[0])
        self.assertEqual(len(clips(spec)), 3, "the talking layers stay under the cutaway (their sound continues)")
        self.assertIn("unrelated.mp4", rep["broll_unused"])
        music = [a for a in spec["audio"] if a.get("role") == "music"]
        self.assertTrue(music and music[0]["duck"] and music[0]["loop"])
        st = spec["captions"]["style"]
        self.assertEqual((st["font_size_pct"], st["center_y_pct"], st["highlight"]), (6.0, 55.0, "#00E5FF"))
        self.assertNotIn("samples", st, "only keys captions.py reads")

    def test_cli(self):
        out = os.path.join(T["tmp"], "cli")
        log = io.StringIO()
        with contextlib.redirect_stdout(log), contextlib.redirect_stderr(io.StringIO()):
            code = autoedit.main([T["video"], "--words", T["words_json"], "--out-dir", out, "--size", "216x384",
                                  "--fps", "12", "--energy", "medium", "--hook", "THE ONE SECRET", "--render",
                                  "--preview"])
            missing = autoedit.main([os.path.join(T["tmp"], "missing.mp4")])
        self.assertEqual(code, 0, log.getvalue())
        self.assertIn("cuts: 3", log.getvalue())
        self.assertEqual(missing, 1)
        for name in ("edit.json", "EDIT_REPORT.md", "edited.mp4"):
            self.assertTrue(os.path.isfile(os.path.join(out, name)), name)
        spec = timeline.load(os.path.join(out, "edit.json"))
        self.assertEqual(timeline.validate(spec), [])
        self.assertEqual(spec["meta"]["edit"]["energy"]["preset"], "medium")
        self.assertEqual(spec["meta"]["edit"]["graphics"][0]["text"], "THE ONE SECRET")
        info = timeline.probe(os.path.join(out, "edited.mp4"))
        self.assertLess(info["duration"], DUR - 3.0)


if __name__ == "__main__":
    unittest.main()
