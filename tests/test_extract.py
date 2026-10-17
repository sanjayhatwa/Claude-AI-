import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

from _load import load, SKILLS_DIR

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
