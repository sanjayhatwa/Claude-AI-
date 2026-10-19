"""The skill docs, the agents and vstudio/ARCHITECTURE.md against what the tools really do.

Also the skill-script fixes those docs rely on: spoken.sentences (decimals and abbreviations),
radar's per-source reachability, captions.py --ass-only without a video and its fonts, and
auto-edit's cut_moment.py. Offline; the ffmpeg parts skip without ffmpeg.
"""

import contextlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from _load import ROOT, SKILLS_DIR, load

spoken = load("script-forge/scripts", "spoken")
radar = load("trend-hunt/scripts", "radar")
captions = load("viral-extract/scripts", "captions")
cut_moment = load("auto-edit/scripts", "cut_moment")

ARCH = os.path.join(ROOT, "vstudio", "ARCHITECTURE.md")
HAVE_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def vstudio(name):
    """A vstudio module (the repo root on sys.path)."""
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)
    import importlib
    return importlib.import_module("vstudio." + name)


def doc_files():
    out = [os.path.join(ROOT, "CLAUDE.md"), ARCH]
    for base in (SKILLS_DIR, os.path.join(ROOT, ".claude", "agents")):
        for root, _, names in os.walk(base):
            out += [os.path.join(root, n) for n in names if n.endswith(".md")]
    return out


_help = {}


def cli_flags(cmd):
    """Every --option of `python -m vstudio <cmd>`, read from its --help."""
    if cmd not in _help:
        r = subprocess.run([sys.executable, "-m", "vstudio", cmd, "--help"], cwd=ROOT, capture_output=True,
                           text=True, timeout=60)
        assert r.returncode == 0, r.stderr
        _help[cmd] = set(re.findall(r"(?<![\w-])--[a-z][a-z0-9-]*", r.stdout)) - {"--help"}
    return _help[cmd]


CMD_RE = re.compile(r"vstudio (reel|autoedit|serve|render|channel)\b")


def doc_commands(text):
    """(command, flags) for every `vstudio <command> ...` written in a doc, continuation lines joined."""
    lines = text.splitlines()
    out, i = [], 0
    while i < len(lines):
        line = lines[i]
        m = CMD_RE.search(line)
        if not m:
            i += 1
            continue
        arch_style = line.lstrip().startswith("# vstudio")
        cmd = line[m.start():]
        if line[:m.start()].count("`") % 2:          # inline code: up to the closing backtick
            cmd = cmd.split("`", 1)[0]
        while cmd.rstrip().endswith("\\") and i + 1 < len(lines):
            i += 1
            cmd = cmd.rstrip()[:-1] + " " + lines[i]
        while arch_style and i + 1 < len(lines) and re.match(r"#\s{3,}\S", lines[i + 1]):
            i += 1
            cmd += " " + lines[i].lstrip("# ")
        cmd = re.sub(r"\s#\s.*$", "", cmd)           # a shell comment after the command
        out.append((m.group(1), set(re.findall(r"(?<![\w-])--[a-z][a-z0-9-]*", cmd))))
        i += 1
    return out


class TestSpokenSentences(unittest.TestCase):
    """spoken.sentences is what scriptcheck and voiceprint count sentences with."""

    def test_decimals_prices_and_abbreviations_stay_in_their_sentence(self):
        self.assertEqual(spoken.sentences("It costs $4.99 a month. Dr. Smith said hi."),
                         ["It costs $4.99 a month.", "Dr. Smith said hi."])
        line = "Second, every cut came every 1.5 seconds or faster."
        self.assertEqual(spoken.sentences(line), [line])
        self.assertEqual(spoken.text_stats(line)["sentences"], 1)
        self.assertEqual(spoken.sentences("Use e.g. CapCut. Then post it. iPhone vs. Android wins."),
                         ["Use e.g. CapCut.", "Then post it.", "iPhone vs. Android wins."])
        self.assertEqual(spoken.sentences("No. 1 rule: post daily. We moved to the U.S. Then it grew."),
                         ["No. 1 rule: post daily.", "We moved to the U.S.", "Then it grew."])

    def test_still_splits_real_sentence_ends(self):
        self.assertEqual(spoken.sentences("so i did it. then it broke! why? because.\nNo full stop here"),
                         ["so i did it.", "then it broke!", "why?", "because.", "No full stop here"])
        self.assertEqual(spoken.sentences("I made 5. Then I quit."), ["I made 5.", "Then I quit."])
        self.assertEqual(spoken.sentences(""), [])
        self.assertEqual(spoken.sentences("... !!"), [])


class TestRadarSources(unittest.TestCase):
    """A source that cannot be reached is named in RADAR.md and radar.json, not left as an empty list."""

    def setUp(self):
        radar.SOURCES.clear()
        self.addCleanup(radar.SOURCES.clear)

    def _quiet(self):
        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch.object(radar.time, "sleep", lambda s: None))
        stack.enter_context(contextlib.redirect_stderr(io.StringIO()))
        return stack

    def test_failed_requests_mark_their_source(self):
        def down(url, timeout=20):
            raise OSError("Tunnel connection failed: 403 Forbidden")
        with self._quiet(), mock.patch.object(radar, "http_json", down), mock.patch.object(radar, "http_text", down):
            self.assertEqual(radar.autocomplete("horror games"), [])
            self.assertEqual(radar.reddit("horror games", ["horrorgaming"]), [])
            self.assertEqual(radar.google_trends("US"), [])
        self.assertEqual(radar.SOURCES, {"autocomplete": "unreachable", "reddit": "unreachable",
                                         "google_trends": "unreachable"})
        md = radar.render("horror games", [], [], [], [], [], [], 0, True, radar.SOURCES)
        self.assertIn("Not reachable from this machine: YouTube autocomplete, Reddit, Google Trends.", md)
        self.assertNotIn("YouTube was not reachable", md)

    def test_partly_failed_source_is_partial(self):
        good = {"data": {"children": [{"data": {"title": "t", "score": 5, "num_comments": 2, "subreddit": "s",
                                                "permalink": "/r/s/1"}}]}}

        def some(url, timeout=20):
            if "/r/nosuchsub/" in url:
                raise OSError("HTTP Error 404: Not Found")
            return good
        with self._quiet(), mock.patch.object(radar, "http_json", some):
            self.assertEqual(len(radar.reddit("horror games", ["nosuchsub"])), 1)
        self.assertEqual(radar.SOURCES["reddit"], "partial")
        md = radar.render("x", [], [], [], [], [], [], 0, True, {"reddit": "partial", "google_trends": "ok"})
        self.assertIn("Only partly read: Reddit", md)
        self.assertNotIn("Not reachable", md)
        # everything answered: no warning line at all
        md = radar.render("x", [], [], [], [], [], [], 0, True, {k: "ok" for k in radar.SOURCE_NAMES})
        self.assertNotIn("reachable", md.lower())

    def test_run_writes_sources_into_radar_json_and_the_top_of_radar_md(self):
        def down(url, timeout=20):
            raise OSError("blocked")
        with tempfile.TemporaryDirectory() as tmp, self._quiet(), \
                mock.patch.object(radar, "http_json", down), mock.patch.object(radar, "http_text", down), \
                mock.patch.object(radar.trends, "search", lambda *a, **k: []), \
                mock.patch.object(radar, "youtube_longform", lambda *a, **k: []), \
                mock.patch.object(sys, "argv", ["radar.py", "horror games", "--dir", tmp]), \
                contextlib.redirect_stdout(io.StringIO()):
            radar.main()
            data = json.loads(read(os.path.join(tmp, "radar.json")))
            md = read(os.path.join(tmp, "RADAR.md"))
        self.assertEqual(data["sources"], {"autocomplete": "unreachable", "reddit": "unreachable",
                                           "google_trends": "unreachable", "youtube": "unreachable"})
        head = md.split("## Trends to ride")[0]
        self.assertIn("YouTube was not reachable", head)
        self.assertIn("Not reachable from this machine: YouTube autocomplete, Reddit, Google Trends.", head)


def make_clip(path, seconds=3, size="180x320", gop=None):
    cmd = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=%s:r=15:d=%g" % (size, seconds),
           "-f", "lavfi", "-i", "sine=frequency=220:sample_rate=48000:duration=%g" % seconds,
           "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest"]
    if gop:
        cmd += ["-g", str(gop), "-keyint_min", str(gop), "-sc_threshold", "0"]
    subprocess.run(cmd + [path], check=True)


def probe(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,start_time,duration",
                        "-of", "json", path], capture_output=True, text=True, check=True)
    return {s["codec_type"]: s for s in json.loads(r.stdout)["streams"]}


WORDS = [{"w": "Nobody", "s": 0.2, "e": 0.5}, {"w": "tells", "s": 0.55, "e": 0.8}, {"w": "you", "s": 0.85, "e": 1.0},
         {"w": "this.", "s": 1.05, "e": 1.4}, {"w": "Every", "s": 1.8, "e": 2.0}, {"w": "1.5", "s": 2.05, "e": 2.4},
         {"w": "seconds.", "s": 2.45, "e": 2.9}]


class TestCaptionsCli(unittest.TestCase):
    SCRIPT = os.path.join(SKILLS_DIR, "viral-extract", "scripts", "captions.py")

    def run_captions(self, *args):
        return subprocess.run([sys.executable, self.SCRIPT] + list(args), capture_output=True, text=True,
                              timeout=180)

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="vs docs ")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.script = os.path.join(self.tmp, "script.txt")
        with open(self.script, "w", encoding="utf-8") as f:
            f.write("I lost three hundred dollars on one setting.\nAnd nobody warned me.\n")

    def test_ass_only_needs_no_video(self):
        """The /viral pack writes caption files before anything is filmed."""
        out = os.path.join(self.tmp, "pack", "captions", "1-hook.ass")
        r = self.run_captions("--script", self.script, "--ass-only", "--out", out)
        self.assertEqual(r.returncode, 0, r.stderr)
        ass = read(out)
        self.assertIn("PlayResX: 1080", ass)
        self.assertIn("PlayResY: 1920", ass)
        self.assertIn("Dialogue:", ass)
        r = self.run_captions("--script", self.script, "--ass-only", "--size", "720x1280", "--out", out)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("PlayResY: 1280", read(out))

    def test_missing_or_bad_inputs_say_so_without_a_traceback(self):
        out = os.path.join(self.tmp, "o.mp4")
        r = self.run_captions("--script", self.script, "--out", out)
        self.assertEqual(r.returncode, 2)
        self.assertIn("--video is needed", r.stderr)
        r = self.run_captions("--ass-only", "--out", os.path.join(self.tmp, "o.ass"))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("No words to caption", r.stderr)
        r = self.run_captions("--script", self.script, "--ass-only", "--size", "big", "--out", out)
        self.assertEqual(r.returncode, 2)
        self.assertIn("WIDTHxHEIGHT", r.stderr)
        r = self.run_captions("--video", os.path.join(self.tmp, "none.mp4"), "--script", self.script, "--out", out)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("no such video", r.stderr)
        self.assertNotIn("Traceback", r.stderr)
        if HAVE_FFMPEG:
            r = self.run_captions("--video", self.script, "--script", self.script, "--out", out)
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("no video stream", r.stderr)
            self.assertNotIn("Traceback", r.stderr)

    @unittest.skipUnless(HAVE_FFMPEG, "needs ffmpeg")
    def test_font_from_a_family_subfolder_is_the_one_burned_in(self):
        """`vstudio fonts install` lays fonts out as workspace/fonts/<Family>/<file>; libass does not look
        into subfolders, so the font used to be swapped for some other bold font without a word."""
        try:
            from fontTools.ttLib import TTFont
        except ImportError:
            self.skipTest("fontTools is not installed")
        fonts = vstudio("fonts")
        src = next((e["path"] for e in fonts.installed() if not e["variable"] and not e["italic"]
                    and e["weight"] == 700 and e["path"].lower().endswith((".ttf", ".otf"))), None)
        if not src:
            self.skipTest("no static bold .ttf/.otf font installed")
        font = TTFont(src)
        for rec in list(font["name"].names):
            value = {1: "Zebra Test", 16: "Zebra Test", 2: "Bold", 17: "Bold", 4: "Zebra Test Bold",
                     6: "ZebraTest-Bold", 3: "ZebraTest-Bold"}.get(rec.nameID)
            if value:
                font["name"].setName(value, rec.nameID, rec.platformID, rec.platEncID, rec.langID)
        fam_dir = os.path.join(self.tmp, "fonts", "ZebraTest")
        os.makedirs(fam_dir)
        font_file = os.path.join(fam_dir, "ZebraTest-Bold" + os.path.splitext(src)[1])
        font.save(font_file)
        video = os.path.join(self.tmp, "take.mp4")
        make_clip(video)
        words = os.path.join(self.tmp, "words.json")
        with open(words, "w", encoding="utf-8") as f:
            json.dump(WORDS, f)
        out = os.path.join(self.tmp, "out", "take_captioned.mp4")
        r = self.run_captions("--video", video, "--words", words, "--font", "Zebra Test Bold",
                              "--fonts-dir", os.path.join(self.tmp, "fonts"), "--out", out)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("font Zebra Test Bold from %s" % os.path.basename(font_file), r.stderr)
        self.assertTrue(os.path.getsize(out) > 0)
        ass = os.path.join(self.tmp, "out", "take_captioned.ass")
        self.assertIn("Style: Cap,Zebra Test Bold,", read(ass))
        # and libass, given the folder captions.py hands it, draws that very file
        dbg = subprocess.run(["ffmpeg", "-loglevel", "debug", "-i", video, "-t", "1", "-vf",
                              "ass=filename=%s:fontsdir=%s" % (captions.filter_path(ass), captions.filter_path(fam_dir)),
                              "-f", "null", "-"], capture_output=True, text=True)
        picked = [l for l in dbg.stderr.splitlines() if "fontselect" in l]
        self.assertTrue(picked and "ZebraTest-Bold" in picked[0], picked)


@unittest.skipUnless(HAVE_FFMPEG, "needs ffmpeg")
class TestCutMoment(unittest.TestCase):
    def test_timecodes(self):
        self.assertEqual(cut_moment.seconds("754.2"), 754.2)
        self.assertAlmostEqual(cut_moment.seconds("12:34.2"), 754.2)
        self.assertEqual(cut_moment.seconds("1:02:03"), 3723.0)
        with self.assertRaises(Exception):
            cut_moment.seconds("soon")

    def test_clip_starts_on_the_frame_with_its_words_beside_it(self):
        with tempfile.TemporaryDirectory(prefix="vs docs ") as tmp:
            long = os.path.join(tmp, "long.mp4")
            make_clip(long, seconds=6, gop=90)          # one keyframe: a stream copy would start late
            words = os.path.join(tmp, "long.words.json")
            shifted = [dict(w, s=w["s"] + 2.0, e=w["e"] + 2.0) for w in WORDS]
            with open(words, "w", encoding="utf-8") as f:
                json.dump(shifted, f)
            out = os.path.join(tmp, "moments", "1.mp4")
            r = subprocess.run([sys.executable, os.path.join(SKILLS_DIR, "auto-edit", "scripts", "cut_moment.py"),
                                long, "--start", "2.5", "--end", "4.5", "--transcript", words, "--out", out],
                               capture_output=True, text=True, timeout=120)
            self.assertEqual(r.returncode, 0, r.stderr)
            streams = probe(out)
            self.assertAlmostEqual(float(streams["video"]["start_time"]), 0.0, delta=0.05)
            self.assertAlmostEqual(float(streams["video"]["duration"]), 2.0, delta=0.15)
            self.assertIn("audio", streams)
            got = json.loads(read(os.path.join(tmp, "moments", "1.words.json")))
            # words whose middle is inside 2.5-4.5 s of the long video, on the clip's clock
            self.assertEqual([w["w"] for w in got], ["tells", "you", "this.", "Every", "1.5"])
            self.assertEqual((got[0]["s"], got[0]["e"]), (0.05, 0.3))
            self.assertEqual((got[-1]["s"], got[-1]["e"]), (1.55, 1.9))
            # autoedit finds the words next to the clip by itself
            self.assertEqual(vstudio("transcribe").find_sidecar(out), os.path.join(tmp, "moments", "1.words.json"))

    def test_end_before_start_is_refused(self):
        with self.assertRaises(SystemExit):
            with contextlib.redirect_stderr(io.StringIO()):
                cut_moment.main(["x.mp4", "--start", "5", "--end", "4", "--out", "y.mp4"])


class TestDocsMatchTheTools(unittest.TestCase):
    def test_every_documented_vstudio_flag_exists(self):
        bad = []
        for path in doc_files():
            for cmd, flags in doc_commands(read(path)):
                missing = flags - cli_flags(cmd)
                if missing:
                    bad.append("%s: vstudio %s %s" % (os.path.relpath(path, ROOT), cmd, sorted(missing)))
        self.assertEqual(bad, [])

    def test_architecture_documents_every_cli_option(self):
        arch = read(ARCH)
        for cmd in ("reel", "autoedit", "serve", "render", "channel"):
            missing = sorted(f for f in cli_flags(cmd) if not re.search(re.escape(f) + r"(?![\w-])", arch))
            self.assertEqual(missing, [], "vstudio %s options missing from ARCHITECTURE.md" % cmd)

    def test_auto_edit_skill_documents_every_autoedit_option(self):
        doc = read(os.path.join(SKILLS_DIR, "auto-edit", "SKILL.md"))
        missing = sorted(f for f in cli_flags("autoedit") if not re.search(re.escape(f) + r"(?![\w-])", doc))
        self.assertEqual(missing, [])
        self.assertIn("auto|high|medium|calm", doc)

    def test_architecture_lists_every_job_kind_and_route(self):
        jobs, server = vstudio("jobs"), vstudio("server")
        arch = read(ARCH)
        kinds = re.search(r"`reel, autoedit[^`]*`", arch)
        self.assertTrue(kinds)
        self.assertEqual(set(kinds.group(0).strip("`").split(", ")), set(jobs.KINDS))
        documented = {re.sub(r"<[^>]*>", "<>", p) for p in re.findall(r"(/(?:api|files)/?[^\s`?\[,)]*)", arch)}
        for method, rx, _ in server.ROUTES:
            path = re.sub(r"\(\[\^/\]\+\)|\(\.\+\)", "<>", rx.pattern.strip("^$"))
            self.assertIn(path, documented, "%s %s is not in ARCHITECTURE.md" % (method, path))

    def test_script_paths_in_docs_exist(self):
        for path in doc_files():
            for rel in re.findall(r"python3? (\.claude/skills/[\w./-]+\.py)", read(path)):
                self.assertTrue(os.path.isfile(os.path.join(ROOT, rel)), "%s names %s" % (path, rel))

    def test_claude_md_says_which_python_runs_the_commands(self):
        doc = read(os.path.join(ROOT, "CLAUDE.md"))
        section = doc.split("## Running Python", 1)[1].split("\n## ", 1)[0]
        for must in (".venv/bin/python", ".venv/Scripts/python.exe", "python3", "py -3", "Microsoft Store"):
            self.assertIn(must, section)

    def test_motion_designer_bar_matches_what_qa_passes(self):
        director = read(os.path.join(ROOT, "vstudio", "director.py"))
        cut_rel = re.search(r'\("Cuts per 10 s", "cuts_per_10s", ([0-9.]+)', director)
        size_rel = re.search(r'\("Caption size", "font_size_pct", ([0-9.]+)', director)
        self.assertTrue(cut_rel and size_rel)
        agent = read(os.path.join(ROOT, ".claude", "agents", "motion-designer.md"))
        skill = read(os.path.join(SKILLS_DIR, "reel-factory", "SKILL.md"))
        for doc in (agent, skill):
            self.assertIn("within %d%% of the" % round(float(cut_rel.group(1)) * 100), doc)
            self.assertIn("caption size within %d%%" % round(float(size_rel.group(1)) * 100), doc)


if __name__ == "__main__":
    unittest.main()
