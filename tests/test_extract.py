import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))   # also when run as tests/test_extract.py
from _load import load, SKILLS_DIR  # noqa: E402

extract = load("viral-extract/scripts", "extract")
sys.path.insert(0, os.path.join(SKILLS_DIR, "viral-extract", "scripts"))
captions = load("viral-extract/scripts", "captions")

SRT = """1
00:00:00,000 --> 00:00:01,600
Nobody tells you this.

2
00:00:01,900 --> 00:00:04,000
Your first ten reels will flop.

3
00:00:04,300 --> 00:00:05,600
And that is fine.
"""


class TestTranscripts(unittest.TestCase):
    def test_srt_words_are_timed_inside_cues(self):
        w = extract.parse_srt_vtt(SRT)
        self.assertEqual(len(w), 14)
        self.assertEqual(w[0]["w"], "Nobody")
        self.assertAlmostEqual(w[4]["s"], 1.9, places=2)

    def test_json3(self):
        data = {"events": [{"tStartMs": 0, "segs": [{"utf8": "hey"}, {"utf8": " you", "tOffsetMs": 300}]}]}
        self.assertEqual([x["w"] for x in extract.parse_json3(data)], ["hey", "you"])

    def test_json3_uploaded_lines_are_spread_over_their_time(self):
        """Uploaded captions: one seg per line, no per-word offsets. 33 words over 10 s of speech."""
        lines = ["Nobody tells you this about sourdough starters", "the first week is supposed to smell bad",
                 "and that is exactly when most people quit", "so here is what to do instead of tossing it"]
        starts, durs = [0, 2500, 5100, 7500], [2500, 2600, 2400, 2500]
        data = {"events": [{"tStartMs": a, "dDurationMs": d, "segs": [{"utf8": t}]} for a, d, t in zip(starts, durs, lines)]}
        w = extract.parse_json3(data)
        self.assertEqual(len(w), 33)
        self.assertEqual(len({x["s"] for x in w}), 33)                 # every word has its own start
        self.assertAlmostEqual(w[7]["s"], 2.5, places=2)                # line 2 starts on time
        self.assertLessEqual(max(x["e"] for x in w), 10.0)
        d = extract.dialogue_dna(w, 10.0)
        self.assertAlmostEqual(d["wpm"], 198, delta=8)                  # 33 words in 10 s, not 251
        self.assertEqual(d["pauses"]["count"], 0)                       # continuous speech: no invented pauses
        gaps = sorted(b["s"] - a["s"] for a, b in zip(w, w[1:]))
        self.assertGreater(gaps[len(gaps) // 2], 0.25)                  # median onset gap ~0.3 s, not 0

    def test_json3_mixed_events_keep_word_offsets(self):
        data = {"events": [{"tStartMs": 1000, "dDurationMs": 4000,
                            "segs": [{"utf8": "so"}, {"utf8": " here", "tOffsetMs": 400}, {"utf8": " we go", "tOffsetMs": 800}]},
                           {"tStartMs": 3000, "segs": [{"utf8": "\n"}]},
                           {"tStartMs": 3000, "dDurationMs": 1000, "segs": [{"utf8": "again"}]}]}
        w = extract.parse_json3(data)
        self.assertEqual([x["w"] for x in w], ["so", "here", "we", "go", "again"])
        self.assertEqual([x["s"] for x in w[:3]], [1.0, 1.4, 1.8])
        self.assertAlmostEqual(w[3]["s"], 2.4, places=2)                # "we go" spread up to the next event
        self.assertEqual(w[4]["s"], 3.0)

    def test_pick_captions_prefers_what_was_said(self):
        pick = extract.pick_captions
        # a Hindi video: "en" is YouTube's machine translation, "hi-orig" the speech track
        self.assertEqual(pick(["subs.en.json3", "subs.hi-orig.json3"]), "subs.hi-orig.json3")
        self.assertEqual(pick(["subs.en.json3", "subs.en-orig.json3"]), "subs.en-orig.json3")
        self.assertEqual(pick(["subs.en.json3", "subs.hi.vtt"], "hi"), "subs.hi.vtt")
        self.assertIsNone(pick(["subs.en.json3", "subs.en-US.vtt"], "hi"))   # translations only
        self.assertEqual(pick(["subs.en.vtt", "subs.en.json3"]), "subs.en.json3")
        self.assertEqual(pick(["subs.eng-US.vtt"]), "subs.eng-US.vtt")      # TikTok: VTT only
        self.assertEqual(pick(["subs.es.srt", "subs.info.json"]), "subs.es.srt")
        self.assertIsNone(pick([]))

    def test_words_json_shapes(self):
        words = [{"w": "nobody", "s": 0.0, "e": 0.3}, {"w": "tells", "s": 0.3, "e": 0.6}, {"w": "you", "s": 0.6, "e": 0.8}]
        with tempfile.TemporaryDirectory() as tmp:
            shapes = {"list.json": words, "dict.json": {"words": words},
                      "whisper.json": {"segments": [{"start": 0.0, "end": 0.8, "text": "nobody tells you"}]},
                      "segwords.json": {"segments": [{"words": [{"word": " nobody", "start": 0.0, "end": 0.3},
                                                                {"word": " tells", "start": 0.3, "end": 0.6},
                                                                {"word": " you", "start": 0.6, "end": 0.8}]}]}}
            for name, data in shapes.items():
                p = os.path.join(tmp, name)
                with open(p, "w", encoding="utf-8") as f:
                    json.dump(data, f)
                got = extract.load_captions(p)
                self.assertEqual([x["w"] for x in got], ["nobody", "tells", "you"], name)
                self.assertEqual(got[0]["s"], 0.0, name)
            bad = os.path.join(tmp, "bad.json")
            with open(bad, "w", encoding="utf-8") as f:
                json.dump({"text": "no timings here"}, f)
            with self.assertRaises(ValueError):
                extract.load_captions(bad)


class TestWhisperDevice(unittest.TestCase):
    def test_gpu_without_cuda_libraries_falls_back_to_cpu(self):
        """device="auto" picks CUDA on any NVIDIA laptop; without cuBLAS the first segment raises."""
        import types
        from unittest import mock
        used = []

        class Word:
            def __init__(self, w, s, e):
                self.word, self.start, self.end = w, s, e

        class Model:
            def __init__(self, name, device="auto", compute_type="int8"):
                self.device = device
                used.append(device)

            def transcribe(self, media, **kw):
                def segs():
                    if self.device != "cpu":
                        raise RuntimeError("Library cublas64_12.dll is not found or cannot be loaded")
                    yield types.SimpleNamespace(words=[Word(" hello", 0.0, 0.4), Word(" there", 0.4, 0.8)])
                return segs(), None

        fake = types.ModuleType("faster_whisper")
        fake.WhisperModel = Model
        with mock.patch.dict(sys.modules, {"faster_whisper": fake}):
            words = extract.whisper_words("clip.mp4")
        self.assertEqual([w["w"] for w in words], ["hello", "there"])
        self.assertEqual(used, ["auto", "cpu"])

    def test_other_errors_are_not_hidden(self):
        import types
        from unittest import mock

        class Model:
            def __init__(self, *a, **kw):
                raise RuntimeError("HTTP Error 403: Forbidden (model download)")

        fake = types.ModuleType("faster_whisper")
        fake.WhisperModel = Model
        with mock.patch.dict(sys.modules, {"faster_whisper": fake}):
            with self.assertRaises(RuntimeError):
                extract.whisper_words("clip.mp4")


class TestDialogue(unittest.TestCase):
    def test_dna(self):
        d = extract.dialogue_dna(extract.parse_srt_vtt(SRT), duration=6.0)
        self.assertEqual(d["words"], 14)
        self.assertEqual(d["sentences"]["count"], 3)
        self.assertIn("Nobody", d["hook"]["text"])
        self.assertEqual(d["rehooks"]["times_s"], [4.3])
        self.assertGreater(d["wpm"], 100)


class TestTextHelpers(unittest.TestCase):
    def test_word_break_uses_spoken_words(self):
        vocab = {"your", "first", "ten", "reels"}
        self.assertEqual(extract.word_break("YOURFIRST", vocab), ["your", "first"])
        self.assertEqual(extract.count_words("TENREELS WILL", vocab), 3)

    def test_case(self):
        self.assertEqual(extract.case_style("NOBODY TELLS"), "upper")
        self.assertEqual(extract.case_style("nobody tells"), "lower")
        self.assertEqual(extract.case_style("Nobody Tells You"), "title")

    def test_font_look_normalises_case(self):
        upper = extract.font_look({"case": "upper", "ink_ratio": 0.55, "width_ratio": 0.89, "outline": None, "box": None})
        lower = extract.font_look({"case": "lower", "ink_ratio": 0.32, "width_ratio": 0.47, "outline": None, "box": None})
        self.assertEqual(upper["weight"], "black")
        self.assertEqual(lower["weight"], "black")
        self.assertEqual(upper["width"], "normal")

    def test_candidates_from_reference(self):
        c = extract.font_candidates({"weight": "black", "width": "condensed", "case": "upper"})
        self.assertIn("Anton", c)


class TestImageWritesAnyPath(unittest.TestCase):
    def test_sheets_do_not_depend_on_cv2_imwrite(self):
        """On Windows cv2.imwrite fails (returns False) for a folder with non-ASCII letters."""
        import numpy as np
        import cv2
        from unittest import mock
        analyze = load("reel-mimic/scripts", "analyze")
        tile = np.full((40, 120, 3), 200, "uint8")
        with tempfile.TemporaryDirectory() as tmp:
            folder = os.path.join(tmp, "\u0930\u0947\u0938\u093f\u092a\u0940 caf\u00e9")
            os.makedirs(folder)
            with mock.patch.object(cv2, "imwrite", lambda *a, **k: False):
                extract._sheet([tile, tile], os.path.join(folder, "fonts_sheet.jpg"))
                self.assertTrue(analyze.imwrite(os.path.join(folder, "sheet_scenes.jpg"), tile,
                                                [cv2.IMWRITE_JPEG_QUALITY, 85]))
            for name in ("fonts_sheet.jpg", "sheet_scenes.jpg"):
                img = cv2.imdecode(np.fromfile(os.path.join(folder, name), dtype="uint8"), cv2.IMREAD_COLOR)
                self.assertIsNotNone(img, name)


class TestCaptions(unittest.TestCase):
    def test_chunking_respects_card_size_and_punctuation(self):
        words = extract.parse_srt_vtt(SRT)
        cards = captions.chunk(words, per_card=3)
        self.assertTrue(all(len(c) <= 4 for c in cards))
        self.assertFalse(any(len(c) == 1 and c[0]["w"].endswith(".") for c in cards))
        self.assertEqual(cards[0][-1]["w"], "this.")

    def test_ass_has_style_and_highlight(self):
        cap = dict(captions.DEFAULT_CAPTION, highlight="#FFE500")
        ass, n = captions.build_ass(extract.parse_srt_vtt(SRT), cap, 1080, 1920, "Montserrat Black")
        self.assertIn("Style: Cap,Montserrat Black", ass)
        self.assertIn("\\c&H0000E5FF", ass)
        self.assertGreater(n, 3)
        self.assertIn("NOBODY", ass)

    def test_script_timing(self):
        w = captions.words_from_script("one two three. four", wpm=120)
        self.assertEqual(len(w), 4)
        self.assertGreater(w[3]["s"] - w[2]["s"], w[1]["s"] - w[0]["s"])


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "needs ffmpeg")
class TestCaptionsCli(unittest.TestCase):
    """captions.py end to end: words in any JSON shape, burned in under awkward folder names."""

    def test_burn_in_with_drive_colon_and_apostrophe_paths(self):
        script = os.path.join(SKILLS_DIR, "viral-extract", "scripts", "captions.py")
        with tempfile.TemporaryDirectory() as tmp:
            plain = os.path.join(tmp, "plain.mp4")
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=blue:s=320x240:d=1",
                            "-pix_fmt", "yuv420p", plain], check=True)
            words = os.path.join(tmp, "words.json")
            with open(words, "w", encoding="utf-8") as f:
                json.dump({"words": [{"w": "hello", "s": 0.0, "e": 0.4}, {"w": "there", "s": 0.4, "e": 0.9}]}, f)
            fonts_dir = os.path.join(tmp, "my font's")
            os.makedirs(fonts_dir)
            # "C:" stands in for a Windows drive letter: the colon must survive both ffmpeg parsers
            for sub in ("C:", "it's", "a,b [c]"):
                out = os.path.join(tmp, sub, "out.mp4")
                r = subprocess.run([sys.executable, script, "--video", plain, "--out", out, "--words", words,
                                    "--fonts-dir", fonts_dir], capture_output=True, text=True)
                self.assertEqual(r.returncode, 0, (sub, r.stderr[-400:]))
                self.assertTrue(os.path.isfile(out), sub)
            bad = os.path.join(tmp, "bad.json")
            with open(bad, "w", encoding="utf-8") as f:
                json.dump({"text": "no timings"}, f)
            r = subprocess.run([sys.executable, script, "--video", plain, "--out", os.path.join(tmp, "x.mp4"),
                                "--words", bad], capture_output=True, text=True)
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("no words found", r.stderr)
            self.assertNotIn("Traceback", r.stderr)


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "needs ffmpeg")
class TestWindowsPipedStdout(unittest.TestCase):
    """Windows writes piped stdout as cp1252: the scripts must not crash on what they print."""

    def setUp(self):
        self.env = dict(os.environ, PYTHONIOENCODING="cp1252")
        self.env.pop("PYTHONUTF8", None)
        self.tmp = tempfile.mkdtemp(prefix="cp1252_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.clip = os.path.join(self.tmp, "clip.mp4")
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=red:s=160x120:d=3",
                        "-f", "lavfi", "-i", "sine=f=440:d=3", "-shortest", "-pix_fmt", "yuv420p", self.clip], check=True)

    @unittest.skipUnless(importlib.util.find_spec("librosa"), "analyze.py needs librosa (requirements.txt)")
    def test_analyze_survives_cp1252(self):
        """It prints "build -> hit" for every clip with sound; a non-zero exit made callers drop the report."""
        analyze = os.path.join(SKILLS_DIR, "reel-mimic", "scripts", "analyze.py")
        r = subprocess.run([sys.executable, analyze, self.clip, "--out", os.path.join(self.tmp, "an"), "--no-cache"],
                           capture_output=True, env=self.env)
        self.assertEqual(r.returncode, 0, r.stderr.decode("cp1252", "replace")[-400:])
        self.assertTrue(os.path.isfile(os.path.join(self.tmp, "an", "report.json")))

    def test_extract_survives_cp1252(self):
        script = os.path.join(SKILLS_DIR, "viral-extract", "scripts", "extract.py")
        srt = os.path.join(self.tmp, "hi.srt")   # a Hindi transcript, an arrow and an emoji: none of them in cp1252
        with open(srt, "w", encoding="utf-8") as f:
            f.write("1\n00:00:00,000 --> 00:00:02,000\n\u0906\u0938\u093e\u0928 \u091a\u093f\u0915\u0928 "
                    "\u0915\u0930\u0940 \u2192 \U0001F525\n")
        r = subprocess.run([sys.executable, script, self.clip, "--out", os.path.join(self.tmp, "dna"), "--skip-edit",
                            "--no-whisper", "--captions", srt], capture_output=True, env=self.env)
        self.assertEqual(r.returncode, 0, r.stderr.decode("cp1252", "replace")[-400:])
        self.assertTrue(os.path.isfile(os.path.join(self.tmp, "dna", "style_dna.json")))


def _have_tools():
    try:
        import rapidocr_onnxruntime  # noqa: F401
    except ImportError:
        return False
    return shutil.which("ffmpeg") is not None and subprocess.run(
        ["fc-match", "-f", "%{file}", "Inter:style=Black"], capture_output=True, text=True).stdout.endswith((".otf", ".ttf"))


@unittest.skipUnless(_have_tools(), "needs ffmpeg, rapidocr and the Inter font")
class TestRoundTrip(unittest.TestCase):
    """Render captions with known settings, read them back, re-apply, read again."""

    def test_extract_then_recreate(self):
        font = subprocess.run(["fc-match", "-f", "%{file}", "Inter:style=Black"], capture_output=True, text=True).stdout
        words = ["NOBODY TELLS", "YOU THIS", "YOUR FIRST", "TEN REELS", "WILL FLOP", "AND THAT", "IS FINE"]
        draw = ",".join("drawtext=fontfile=%s:text='%s':fontsize=64:fontcolor=white:borderw=5:bordercolor=black:"
                        "x=(w-tw)/2:y=h*0.70-th/2:enable='between(t,%.1f,%.1f)'" % (font, t, i * 0.8, i * 0.8 + 0.8)
                        for i, t in enumerate(words))
        with tempfile.TemporaryDirectory() as tmp:
            ref, plain = os.path.join(tmp, "ref.mp4"), os.path.join(tmp, "plain.mp4")
            srt = os.path.join(tmp, "ref.srt")
            open(srt, "w").write(SRT)
            subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "color=c=0x3a5a78:s=720x1280:d=6:r=30",
                            "-vf", draw, "-pix_fmt", "yuv420p", ref], check=True)
            subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "color=c=0x784a3a:s=720x1280:d=6:r=30",
                            "-pix_fmt", "yuv420p", plain], check=True)
            vocab = {extract._clean(w["w"]).replace("'", "") for w in extract.parse_srt_vtt(SRT)}
            text, _ = extract.text_dna(ref, os.path.join(tmp, "dna"), fps=4, vocab=vocab)
            cap = text["caption_track"]
            self.assertAlmostEqual(cap["center_y_pct"], 70, delta=1.5)
            self.assertAlmostEqual(cap["center_x_pct"], 50, delta=1.5)
            self.assertAlmostEqual(cap["font_size_pct"], 5.0, delta=0.5)
            self.assertEqual(cap["case"], "upper")
            self.assertEqual(cap["words_per_card"]["median"], 2)
            self.assertIsNotNone(cap["outline"])
            self.assertEqual(cap["font_look"]["weight"], "black")
            self.assertIn("Inter Black", cap["font_candidates"])

            ass, _ = captions.build_ass(extract.parse_srt_vtt(SRT), cap, 720, 1280, "Inter Black")
            ass_path, out = os.path.join(tmp, "c.ass"), os.path.join(tmp, "out.mp4")
            open(ass_path, "w").write(ass)
            subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-i", plain, "-vf", "ass=" + ass_path,
                            "-pix_fmt", "yuv420p", out], check=True)
            again, _ = extract.text_dna(out, os.path.join(tmp, "dna2"), fps=4, vocab=vocab)
            cap2 = again["caption_track"]
            self.assertAlmostEqual(cap2["center_y_pct"], cap["center_y_pct"], delta=1.5)
            self.assertAlmostEqual(cap2["font_size_pct"], cap["font_size_pct"], delta=0.5)
            self.assertEqual(cap2["case"], "upper")
            self.assertIsNotNone(cap2["outline"])


if __name__ == "__main__":
    unittest.main()
