"""Tests for vstudio.channel and vstudio.thumbs, offline.

A fake `fetch` serves a fake cooking channel (tests/fixtures/channel/kitchenlab.json: 20 long-form
videos, 20 Shorts, 3 lives with metadata), a TikTok profile (tiktok.json), and an Instagram profile
that refuses logged-out readers. Thumbnails are drawn with Pillow (the outliers get big bold text on
warm colours, the rest are dark and text-free) and the deep-dive clip is made with ffmpeg (drawtext
captions over two shots, a modulated tone as the soundtrack) with hook.srt as its captions.
"""

import collections
import contextlib
import importlib.util
import io
import json
import os
import re
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from vstudio import channel, thumbs  # noqa: E402

FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "channel")
HAVE_FFMPEG = shutil.which("ffmpeg") is not None
HAVE_OCR = importlib.util.find_spec("rapidocr_onnxruntime") is not None

YT = "https://www.youtube.com/@kitchenlab"
IG = "https://www.instagram.com/kitchenlab/"
TT = "https://www.tiktok.com/@kitchenlab"
FONT = None


def font(size):
    from PIL import ImageFont
    global FONT
    if FONT is None:
        from vstudio import fonts
        FONT = fonts.resolve("Inter Black") or fonts.resolve(None)
    return ImageFont.truetype(FONT, size) if FONT else ImageFont.load_default()


def draw_thumb(path, title, winner):
    """320x180 thumbnail: winners = warm background + 2-3 words of big outlined text; the rest = dark,
    textured, no text."""
    import numpy as np
    from PIL import Image, ImageDraw
    if winner:
        im = Image.new("RGB", (320, 180), (245, 196, 28))
        d = ImageDraw.Draw(im)
        d.ellipse((205, 60, 300, 175), fill=(226, 170, 140))
        words = [w for w in re.findall(r"[A-Za-z0-9$]+", title) if len(w) > 1][:2]
        d.text((14, 16), " ".join(words).upper(), font=font(34), fill="white", stroke_width=3, stroke_fill="black")
    else:
        rng = np.random.default_rng(abs(hash(title)) % 1000)
        base = np.linspace(30, 80, 320)[None, :, None] * np.array([0.4, 0.5, 1.0])[None, None, :]
        arr = np.clip(base + rng.normal(0, 18, (180, 320, 3)), 0, 255).astype("uint8")
        im = Image.fromarray(arr)
    im.save(path, "JPEG", quality=90)


def make_clip(path):
    """3 s, 216x384, 10 fps: two shots (cut at 1.5 s) with word-by-word drawtext captions and a tone."""
    import subprocess
    from vstudio import fonts
    f = fonts.resolve("Inter Black").replace("\\", "/").replace(":", "\\:")
    dt = "drawtext=fontfile='%s':fontsize=34:fontcolor=white:borderw=3:bordercolor=black:x=(w-text_w)/2:y=h*0.62" % f
    caps = [("NOBODY", 0, 0.6), ("TELLS YOU", 0.6, 1.4), ("THIS PART", 1.4, 2.2), ("WATCH", 2.2, 3.0)]
    chain = ",".join("%s:text='%s':enable='between(t,%s,%s)'" % (dt, t, a, b) for t, a, b in caps)
    tone = "aevalsrc='0.5*sin(2*PI*(180+60*sin(2*PI*3*t))*t)*(0.55+0.45*sin(2*PI*4*t))':s=22050:d=3"
    subprocess.run(["ffmpeg", "-v", "error", "-y",
                    "-f", "lavfi", "-i", "color=c=0x2a4d8f:s=216x384:r=10:d=1.5",
                    "-f", "lavfi", "-i", "color=c=0xd9822b:s=216x384:r=10:d=1.5",
                    "-f", "lavfi", "-i", tone,
                    "-filter_complex", "[0:v]drawbox=x=40:y=60:w=136:h=120:color=0xf2d0a9:t=fill[a];"
                                       "[1:v]drawbox=x=20:y=90:w=90:h=90:color=0x333333:t=fill[b];"
                                       "[a][b]concat=n=2:v=1:a=0,%s[v]" % chain,
                    "-map", "[v]", "-map", "2:a", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
                    "-shortest", path], check=True, capture_output=True)
    return path


class FakeFetch:
    """The `fetch` contract, answered from fixtures. Records what was asked."""

    def __init__(self, thumbs_dir, clip=None):
        with open(os.path.join(FIX, "kitchenlab.json"), encoding="utf-8") as f:
            self.yt = json.load(f)
        with open(os.path.join(FIX, "tiktok.json"), encoding="utf-8") as f:
            self.tt = json.load(f)
        self.thumbs_dir, self.clip = thumbs_dir, clip
        self.last_error = None
        self.calls = collections.Counter()

    def list_channel(self, url, tab, limit):
        self.calls["list"] += 1
        if "instagram.com" in url:
            self.last_error = ("ERROR: [Instagram] kitchenlab: Requested content is not available, rate-limit reached "
                               "or login required. Use --cookies, --cookies-from-browser")
            return None
        if "tiktok.com" in url:
            return dict(self.tt, entries=self.tt["entries"][:limit])
        if "@kitchenlab" not in url:
            self.last_error = "ERROR: [youtube:tab] This channel does not exist."
            return None
        return dict(self.yt["channel"], title="Kitchen Lab - %s" % tab.title(), entries=self.yt["tabs"][tab][:limit])

    def video_meta(self, url):
        self.calls["meta"] += 1
        m = re.search(r"(?:v=|shorts/)([A-Za-z0-9_-]{11})", url)
        return self.yt["meta"].get(m.group(1)) if m else None

    def download_thumb(self, url, path):
        self.calls["thumb"] += 1
        vid = re.search(r"/vi/([^/]+)/", url).group(1)
        src = os.path.join(self.thumbs_dir, vid + ".jpg")
        if not os.path.isfile(src):
            return None
        shutil.copyfile(src, path)
        return path

    def download_clip(self, url, path, seconds):
        self.calls["clip"] += 1
        if not self.clip:
            return None
        shutil.copyfile(self.clip, path)
        return path

    def captions(self, url, dir):
        dst = os.path.join(dir, "hook.srt")
        shutil.copyfile(os.path.join(FIX, "hook.srt"), dst)
        return dst


def thumbs_for_fixture(folder):
    with open(os.path.join(FIX, "kitchenlab.json"), encoding="utf-8") as f:
        data = json.load(f)
    for e in data["tabs"]["videos"]:
        winner = e["view_count"] >= 100000   # the 2x+ outliers of a 50K-median channel
        draw_thumb(os.path.join(folder, e["id"] + ".jpg"), e["title"], winner)


def quiet(fn, *a, **kw):
    with contextlib.redirect_stderr(io.StringIO()):
        return fn(*a, **kw)


# ============================================================================ full flow

@unittest.skipUnless(HAVE_FFMPEG and HAVE_OCR, "needs ffmpeg and rapidocr for the full flow")
class TestAnalyzeFullFlow(unittest.TestCase):
    """One analyze() over YouTube + Instagram + TikTok, deep=1, shared by the tests below."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="vs_channel_")
        tdir = os.path.join(cls.tmp, "src_thumbs")
        os.makedirs(tdir)
        thumbs_for_fixture(tdir)
        clip = make_clip(os.path.join(cls.tmp, "clip.mp4"))
        cls.fetch = FakeFetch(tdir, clip)
        cls.out = os.path.join(cls.tmp, "out")
        cls.data = quiet(channel.analyze, [YT, IG, TT], cls.out, max_videos=12, deep=1, fetch=cls.fetch)
        cls.yt, cls.ig, cls.tt = cls.data["channels"]
        with open(os.path.join(cls.out, "CHANNEL_DNA.md"), encoding="utf-8") as f:
            cls.md = f.read()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_files_written(self):
        for name in ("CHANNEL_DNA.json", "CHANNEL_DNA.md", "thumbs_sheet.jpg", "thumbs_sheet_kitchen-lab.jpg"):
            self.assertTrue(os.path.isfile(os.path.join(self.out, name)), name)
        with open(os.path.join(self.out, "CHANNEL_DNA.json"), encoding="utf-8") as f:
            on_disk = json.load(f)
        self.assertEqual(on_disk["channels"][0]["name"], "Kitchen Lab")
        self.assertEqual(len(on_disk["channels"]), 3)

    def test_inventory_and_channel_info(self):
        y = self.yt
        self.assertEqual(y["status"], "ok")
        self.assertEqual(y["followers"], 412000)
        self.assertEqual({k: v["listed"] for k, v in y["inventory"].items()}, {"long": 20, "short": 20, "live": 3})
        # newest 12 per format + top 4 of the older ones, lives all
        self.assertEqual(y["metadata"]["looked_up"], 12 + 4 + 12 + 4 + 3)

    def test_performance_and_multiples(self):
        f = self.yt["formats"]
        self.assertEqual(f["long"]["median_views"], 50000)
        self.assertEqual(f["short"]["median_views"], 215000)
        self.assertIsNone(f["live"]["median_views"])        # 3 lives: below the 4-video baseline
        self.assertEqual(f["long"]["outliers"], 6)
        self.assertEqual(f["long"]["best"][0]["title"], "The $20 Air Fryer vs the $300 One")
        self.assertAlmostEqual(f["long"]["best"][0]["multiple"], 6.2)
        self.assertIsNotNone(f["long"]["trend"])
        self.assertIsNotNone(f["long"]["median_engagement"])
        vids = {v["id"]: v for v in self.yt["videos"]}
        self.assertIsNone(vids["KLl00000001"]["multiple"])
        self.assertGreater(vids["KLv00000001"]["velocity"], 0)

    def test_cadence(self):
        c = self.yt["cadence"]
        self.assertEqual(set(c["top_days"]), {"Tue", "Thu"})
        self.assertAlmostEqual(c["per_format"]["long"]["per_week"], 2.0, delta=0.25)
        self.assertEqual({h["hour"] for h in c["hours_utc"][:2]}, {15, 18})   # long-form 15:00, Shorts 18:00 UTC

    def test_every_video_has_1_to_3_topics(self):
        for v in self.yt["videos"] + self.tt["videos"]:
            self.assertTrue(1 <= len(v["topics"]) <= 3, (v["title"], v["topics"]))
        labels = {t["label"] for t in self.yt["topics"]}
        self.assertTrue(any("air fryer" in l.lower() for l in labels), labels)
        self.assertTrue(any("sourdough" in l.lower() for l in labels), labels)
        # the channel-wide tags ("cooking", "kitchen lab") are on every video, so they are not topics
        self.assertFalse({"cooking", "kitchen lab"} & {l.lower() for l in labels})

    def test_winning_topic_is_air_fryer(self):
        ranked = [t for t in self.yt["topics"] if t["label"] != "other" and t["videos"] >= 3
                  and t["median_multiple"] is not None]
        best = max(ranked, key=lambda t: t["median_multiple"])
        self.assertIn("air fryer", best["label"].lower())
        knife = [t for t in ranked if "knife" in t["label"].lower()]
        self.assertTrue(knife and knife[0]["median_multiple"] < 1.0)

    def test_title_patterns_over_index(self):
        over = self.yt["titles"]["over_index"]
        num = [t for t in over["traits"] if t["trait"] == "has_number"][0]
        self.assertGreater(num["lift"], 1.5)
        self.assertGreater(self.yt["titles"]["per_format"]["long"]["median_words"], 3)
        v = [x for x in self.yt["videos"] if x["id"] == "KLv00000016"][0]
        self.assertTrue(v["title_features"]["money"] and v["title_features"]["versus"])

    def test_thumbnail_traits(self):
        th = self.yt["thumbnails"]
        self.assertGreaterEqual(th["analysed"], 16)
        text = [t for t in th["over_index"]["traits"] if t["trait"] == "has_text"][0]
        self.assertGreater(text["winners"], 0.8)
        self.assertLess(text["rest"], 0.2)
        warm = [t for t in th["over_index"]["traits"] if t["trait"] == "warm"][0]
        self.assertGreater(warm["lift"], 2)
        v = [x for x in self.yt["videos"] if x["id"] == "KLv00000016"][0]
        self.assertTrue(v["thumb"]["text"]["words"] >= 1)
        self.assertTrue(os.path.isfile(os.path.join(self.out, v["thumb"]["path"])))

    def test_deep_dive(self):
        ok = [d for d in self.yt["deep"] if d["status"] == "ok"]
        self.assertEqual(sorted(d["format"] for d in ok), ["long", "short"])
        d = ok[0]
        self.assertTrue(os.path.isfile(os.path.join(self.out, d["style_dna"])))
        self.assertTrue(os.path.isfile(os.path.join(self.out, d["dir"], "STYLE_DNA.md")))
        self.assertGreater(d["dialogue"]["wpm"], 150)
        self.assertIsNotNone(d["word_spacing"])
        self.assertEqual(d["edit"]["shots"], 2)
        self.assertEqual(d["captions"]["mode"], "word-by-word")
        self.assertTrue(0 <= d["energy"]["score"] <= 100)
        self.assertIn(d["energy"]["label"], ("calm", "medium", "high"))
        self.assertIn(d["content_type"]["type"], list(channel.CONTENT_TYPES) + ["unclear"])
        self.assertTrue(d["hook"]["text"].startswith("Nobody tells you"))
        self.assertIn("short", self.yt["deep_summary"])

    def test_instagram_needs_browser(self):
        self.assertEqual(self.ig["status"], "needs_browser")
        self.assertEqual(self.ig["status_note"], channel.NEEDS_BROWSER)
        self.assertIn("not public / needs your browser", self.md)
        self.assertNotIn("password:", self.md.lower())

    def test_tiktok_profile(self):
        t = self.tt
        self.assertEqual(t["status"], "ok")
        self.assertEqual(t["inventory"]["short"]["listed"], 10)
        self.assertEqual(t["formats"]["short"]["best"][0]["views"], 2100000)
        self.assertIsNotNone(t["cadence"]["per_format"]["short"]["per_week"])
        self.assertEqual(len([d for d in t["deep"] if d["status"] == "ok"]), 1)

    def test_markdown_sections(self):
        for heading in ("## Comparison", "## Kitchen Lab (YouTube)", "### Executive summary",
                        "### Formats and cadence", "### Topics", "### Title patterns", "### Thumbnail patterns",
                        "### Dialogue and energy", "### Caption and edit style",
                        "### How to compete with / replicate this channel", "### Data gaps"):
            self.assertIn(heading, self.md)
        self.assertIn("thumbs_sheet_kitchen-lab.jpg", self.md)
        summary = self.yt["summary"]
        self.assertEqual(len(summary), 5)
        self.assertTrue(all(re.search(r"\d", b) for b in summary), summary)
        self.assertTrue(self.yt["playbook"])
        self.assertIn("python -m vstudio autoedit", self.md)
        self.assertEqual(len(self.data["comparison"]), 3)

    def test_polite_lookups(self):
        # one list per tab, one meta per selected video, nothing more
        self.assertLessEqual(self.fetch.calls["meta"], 12 + 4 + 12 + 4 + 3)
        self.assertEqual(self.fetch.calls["clip"], 3)


# ============================================================================ pieces

class TestUrls(unittest.TestCase):
    def test_normalize(self):
        self.assertEqual(channel.normalize_url("@mkbhd"), "https://www.youtube.com/@mkbhd")
        self.assertEqual(channel.normalize_url("mkbhd"), "https://www.youtube.com/@mkbhd")
        self.assertEqual(channel.normalize_url("m.youtube.com/@x/videos"), "https://www.youtube.com/@x/videos")
        self.assertEqual(channel.youtube_channel_base("https://www.youtube.com/@x/shorts?view=0"),
                         "https://www.youtube.com/@x")
        self.assertEqual(channel.youtube_channel_base("https://www.youtube.com/channel/UC123/videos"),
                         "https://www.youtube.com/channel/UC123")
        self.assertIsNone(channel.youtube_channel_base("https://www.youtube.com/watch?v=abcdefghijk"))
        self.assertEqual(channel.platform_of("https://www.instagram.com/x/"), "instagram")
        self.assertEqual(channel.platform_of("https://vm.tiktok.com/x"), "tiktok")
        self.assertEqual(channel.platform_of("https://youtu.be/abcdefghijk"), "youtube")

    def test_classify_error(self):
        self.assertEqual(channel.classify_error("Requested content is not available, login required", "instagram")[0],
                         "needs_browser")
        self.assertEqual(channel.classify_error("HTTP Error 429: Too Many Requests", "youtube")[0], "rate_limited")
        self.assertEqual(channel.classify_error("This channel does not exist.", "youtube")[0], "not_found")
        self.assertEqual(channel.classify_error("Unable to extract webpage", "tiktok")[0], "needs_browser")
        self.assertEqual(channel.classify_error("/usr/bin/python3: No module named yt_dlp")[0], "missing_tool")


class TestTitlesAndTopics(unittest.TestCase):
    def test_title_features(self):
        f = channel.title_features("I Tried 7 $1 Meals (HONEST) \U0001F525?")
        self.assertTrue(f["has_number"] and f["money"] and f["brackets"] and f["emoji"] and f["question"])
        self.assertTrue(f["first_person"])
        self.assertEqual(f["caps_words"], 1)
        self.assertFalse(channel.title_features("why bread fails")["has_number"])

    def test_topic_model_tags_every_video(self):
        titles = ["Air fryer wings", "Air fryer fries test", "Best air fryer recipes", "Sourdough starter guide",
                  "Sourdough crumb secrets", "My sourdough bake", "Knife skills basics", "Sharpen your knife",
                  "Knife skills for onions", "A totally random vlog about my week"]
        recs = [{"id": str(i), "title": t, "tags": [], "description": "", "format": "long", "recency": i / 9.0}
                for i, t in enumerate(titles)]
        topics = quiet(channel.topic_model, recs)
        labels = [t["label"].lower() for t in topics]
        self.assertTrue(any("air fryer" in l for l in labels), labels)
        self.assertTrue(any("sourdough" in l for l in labels), labels)
        self.assertTrue(any("knife" in l for l in labels), labels)
        for r in recs:
            self.assertTrue(1 <= len(r["topics"]) <= 3)
        self.assertEqual(recs[-1]["topics"], ["other"])

    def test_over_index(self):
        recs = [{"multiple": m, "flag": m > 2} for m in (5, 4, 3, 1, 1, 0.9, 0.8, 0.5, 0.4, 0.3)]
        res = channel.over_index(recs, [("flag", "flag", lambda r: r["flag"])])
        self.assertEqual(res["winners"], 3)
        self.assertEqual(res["traits"][0]["winners"], 1.0)
        self.assertGreater(res["traits"][0]["lift"], 3)

    def test_cadence_from_dates(self):
        recs = [{"format": "long", "upload_date": d, "timestamp": None, "sample": "recent"}
                for d in ("20260929", "20260922", "20260915", "20260908")]
        c = channel.cadence(recs, today=__import__("datetime").date(2026, 10, 1))
        self.assertAlmostEqual(c["per_format"]["long"]["per_week"], 1.0)
        self.assertEqual(c["top_days"], ["Tue"])
        self.assertEqual(c["per_format"]["long"]["last_upload_days_ago"], 2)


class TestEnergyAndType(unittest.TestCase):
    def test_energy_bounds_and_missing(self):
        hi = channel.energy_score({"wpm": 230}, {"cuts_per_10s": 7}, {"loudness_std_db": 12, "centroid_cv": 0.9},
                                  {"caption_track": {"mode": "word-by-word", "seconds_per_card": 0.4}})
        lo = channel.energy_score({"wpm": 100}, {"cuts_per_10s": 0.2}, {"loudness_std_db": 2, "centroid_cv": 0.1},
                                  {"caption_track": None})
        self.assertEqual(hi["score"], 100)
        self.assertEqual(hi["label"], "high")
        self.assertEqual(lo["score"], 0)
        part = channel.energy_score({"wpm": 160}, None, None, None)
        self.assertEqual(part["missing"], ["cut_rate", "caption_motion", "loudness_dynamics", "brightness_variation"])
        self.assertEqual(part["score"], 50)
        self.assertIsNone(channel.energy_score())

    def test_content_type_rules(self):
        screen = {"face_frames": 0.0, "face_size": None, "face_x_spread": None, "motion": 0.01, "static_share": 0.3,
                  "uniform_share": 0.7, "colorfulness": 8.0, "edge_density": 0.08}
        self.assertEqual(channel.content_type(screen, {"cuts_per_10s": 0.5}, None, {"coverage": 0.9})["type"],
                         "screen_recording")
        head = {"face_frames": 0.95, "face_size": 0.09, "face_x_spread": 0.02, "motion": 0.01, "static_share": 0.2,
                "uniform_share": 0.1, "colorfulness": 30.0, "edge_density": 0.05}
        self.assertEqual(channel.content_type(head, {"cuts_per_10s": 1.0}, {"words": 200}, {"coverage": 0.8})["type"],
                         "talking_head")
        game = {"face_frames": 0.2, "face_size": 0.01, "face_x_spread": 0.0, "motion": 0.08, "static_share": 0.0,
                "uniform_share": 0.05, "colorfulness": 70.0, "edge_density": 0.2}
        self.assertEqual(channel.content_type(game, {"cuts_per_10s": 0.5}, {"words": 300}, {"coverage": 0.3,
                                                                                            "pinned": [1]})["type"],
                         "gameplay")
        broll = dict(head, face_frames=None)
        res = channel.content_type(dict(broll, motion=0.03, uniform_share=0.1), {"cuts_per_10s": 4}, {"words": 300}, {})
        self.assertIn("voice-over", res["label"])

    def test_word_spacing(self):
        words = [{"w": "w%d" % i, "s": i * 0.3, "e": None} for i in range(20)]
        words[10]["s"] += 1.5
        for w in words[11:]:
            w["s"] += 1.5
        ws = channel.word_spacing(words)
        self.assertAlmostEqual(ws["median_onset_gap_s"], 0.3, places=2)
        self.assertAlmostEqual(ws["longest_pause_s"], 1.8, places=2)
        self.assertGreater(ws["pauses_per_min"], 0)

    @unittest.skipUnless(HAVE_FFMPEG, "needs ffmpeg")
    def test_audio_features(self):
        tmp = tempfile.mkdtemp(prefix="vs_channel_audio_")
        try:
            clip = make_clip(os.path.join(tmp, "c.mp4"))
            a = channel.audio_features(clip)
            self.assertFalse(a["silent"])
            self.assertGreater(a["loudness_std_db"], 0.5)
            self.assertGreater(a["centroid_cv"], 0)
            fr = channel.frame_features(clip)
            self.assertIsNone(fr["face_frames"])
            self.assertEqual(fr["samples"], 6)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestThumbs(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="vs_thumbs_")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_measures(self):
        p = os.path.join(self.tmp, "w.jpg")
        draw_thumb(p, "The $20 Air Fryer", True)
        t = thumbs.analyze_thumb(p, faces=False)
        self.assertEqual(t["width"], 640)
        self.assertEqual(t["dominant_hue"], "yellow")
        self.assertEqual(t["palette"][0]["family"], "yellow")
        self.assertGreater(t["brightness"], 0.5)
        self.assertGreater(t["colorfulness"], 60)
        self.assertIsNone(t["faces"])
        self.assertIn(t["composition"]["weight"], ("left", "centre", "right"))
        if HAVE_OCR:
            self.assertGreaterEqual(t["text"]["words"], 1)
            self.assertTrue(t["text"]["position"].startswith("top"))
            self.assertGreater(int(t["text"]["style"]["fill"][1:3], 16), 200)   # white fill, black stroke
        d = os.path.join(self.tmp, "d.jpg")
        draw_thumb(d, "dark", False)
        dark = thumbs.analyze_thumb(d, ocr=False, faces=False)
        self.assertLess(dark["brightness"], 0.35)
        self.assertGreater(dark["busyness"], t["busyness"])

    def test_letterbox_trim(self):
        from PIL import Image, ImageDraw
        im = Image.new("RGB", (480, 360), (0, 0, 0))
        ImageDraw.Draw(im).rectangle((0, 45, 479, 314), fill=(200, 60, 60))
        p = os.path.join(self.tmp, "hq.jpg")
        im.save(p, quality=95)
        t = thumbs.analyze_thumb(p, ocr=False, faces=False)
        self.assertAlmostEqual(t["aspect"], 480 / 270.0, delta=0.05)
        self.assertEqual(t["dominant_hue"], "red")

    def test_text_colors_outline(self):
        if not HAVE_OCR:
            self.skipTest("needs rapidocr")
        from PIL import Image, ImageDraw
        im = Image.new("RGB", (640, 360), (40, 90, 200))
        ImageDraw.Draw(im).text((30, 40), "STOP", font=font(120), fill="white", stroke_width=8, stroke_fill="black")
        p = os.path.join(self.tmp, "o.png")
        im.save(p)
        st = thumbs.analyze_thumb(p, faces=False)["text"]["style"]
        self.assertGreater(int(st["fill"][1:3], 16), 200)        # white fill
        self.assertLess(int(st["outline"][1:3], 16), 60)         # black outline

    def test_sheet(self):
        ps = []
        for i in range(5):
            p = os.path.join(self.tmp, "s%d.jpg" % i)
            draw_thumb(p, "Title %d" % i, i % 2 == 0)
            ps.append(p)
        out = thumbs.sheet(ps + [os.path.join(self.tmp, "missing.jpg")], os.path.join(self.tmp, "sheet.jpg"),
                           labels=["%dx\nA title" % i for i in range(6)], cols=3, title="Top")
        from PIL import Image
        with Image.open(out) as im:
            self.assertEqual(im.width, 10 + 3 * 330)
            self.assertGreater(im.height, 2 * 180)
        self.assertEqual(thumbs.sheet([], os.path.join(self.tmp, "none.jpg")), "")

    def test_faces_with_a_detector(self):
        """The face path, with a stand-in for cv2.FaceDetectorYN (same setInputSize/detect interface)."""
        import numpy as np

        class Det:
            def setInputSize(self, size):
                self.w, self.h = size

            def detect(self, img):
                row = [self.w * 0.55, self.h * 0.2, self.w * 0.3, self.h * 0.5] + [0] * 10 + [0.93]
                return 1, np.array([row], dtype="float32")

        p = os.path.join(self.tmp, "f.jpg")
        draw_thumb(p, "FACE", True)
        t = thumbs.analyze_thumb(p, ocr=False, detector=Det())
        self.assertEqual(t["faces"]["count"], 1)
        self.assertAlmostEqual(t["faces"]["largest_share"], 0.15, places=2)
        self.assertEqual(t["composition"]["subject"], "right")
        if HAVE_FFMPEG:
            clip = make_clip(os.path.join(self.tmp, "f.mp4"))
            fr = channel.frame_features(clip, Det())
            self.assertEqual(fr["face_frames"], 1.0)
            self.assertAlmostEqual(fr["face_size"], 0.15, places=2)
            ct = channel.content_type(fr, {"cuts_per_10s": 0.5}, {"words": 100}, {"coverage": 0.3})
            self.assertIn("talking_head", ct["scores"])

    def test_face_model_absent_is_none(self):
        old = os.environ.pop("VSTUDIO_FACE_MODEL", None)
        try:
            if thumbs.face_model_path() is None:
                self.assertIsNone(thumbs.face_detector())
        finally:
            if old:
                os.environ["VSTUDIO_FACE_MODEL"] = old


class TestTikTokFallback(unittest.TestCase):
    def test_video_link_lists_the_account_by_id(self):
        class Fetch(FakeFetch):
            def list_channel(self, url, tab, limit):
                if url.startswith("tiktokuser:"):
                    return dict(self.tt, entries=self.tt["entries"][:limit])
                self.last_error = "ERROR: [tiktok:user] kitchenlab: Unable to extract secondary user ID"
                return None

            def video_meta(self, url):
                return {"channel_id": "MS4wLjABAAAAkitchenlab"} if "tiktok.com" in url else None

        tmp = tempfile.mkdtemp(prefix="vs_channel_tt_")
        try:
            data = quiet(channel.analyze, [TT + "/video/7400000000000000000", TT], tmp, max_videos=5, deep=0,
                         fetch=Fetch(tmp))
            by_video, by_profile = data["channels"]
            self.assertEqual(by_video["status"], "ok")
            self.assertEqual(by_video["url"], TT)
            self.assertEqual(by_video["inventory"]["short"]["listed"], 10)
            self.assertEqual(by_profile["status"], "needs_browser")
            with open(os.path.join(tmp, "CHANNEL_DNA.md"), encoding="utf-8") as f:
                self.assertIn("passing one of their video links", f.read())
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


@unittest.skipUnless(HAVE_OCR, "needs rapidocr")
class TestCli(unittest.TestCase):
    def test_main_no_download_on_a_profile(self):
        tmp = tempfile.mkdtemp(prefix="vs_channel_cli_")
        fake = FakeFetch(tmp)
        real = channel.YtDlpFetch
        channel.YtDlpFetch = lambda: fake
        try:
            out = io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                rc = channel.main([TT, "--out-dir", os.path.join(tmp, "o"), "--max-videos", "5", "--no-download"])
            self.assertEqual(rc, 0)
            self.assertTrue(os.path.isfile(os.path.join(tmp, "o", "CHANNEL_DNA.md")))
            self.assertEqual(fake.calls["clip"], 0)
            self.assertIn("CHANNEL_DNA.md", out.getvalue())
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(channel.main([IG, "--out-dir", os.path.join(tmp, "ig")]), 1)
        finally:
            channel.YtDlpFetch = real
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
