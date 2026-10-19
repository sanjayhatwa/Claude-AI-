"""Viral Studio reel director: llm.py (CLI wrapper), planner.py (plan -> timeline, offline planner), director.py.

Offline and fast: the Claude CLI is replaced by an injected runner or a fake client; media is generated with
ffmpeg at small sizes (those tests skip when ffmpeg is missing).
"""

import importlib.util
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

from vstudio import director, llm, paths, planner, templates, timeline  # noqa: E402

HAVE_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
HAVE_OCR = importlib.util.find_spec("rapidocr_onnxruntime") is not None     # optional: reads on-screen text
T = {}

SCRIPT = """I lost $300 on one setting.
Most creators never check it.
First, turn off auto captions.
Second, export at 1080p, not 4K.
Third, keep your hook under 2 seconds.
Why does this matter?
Because 73% of viewers decide in the first 3 seconds.
Follow for part 2."""


def _ff(*args):
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"] + list(args), check=True)


def setUpModule():
    T["tmp"] = tempfile.mkdtemp(prefix="vs_director_")
    T["cache"] = paths.CACHE
    paths.CACHE = os.path.join(T["tmp"], "cache")      # glows and sfx are cached here, not in the workspace
    os.environ["VSTUDIO_NO_WHISPER"] = "1"
    from PIL import Image
    Image.new("RGB", (64, 48), (200, 80, 40)).save(os.path.join(T["tmp"], "photo.png"))
    if not HAVE_FFMPEG:
        return
    d = T["tmp"]
    _ff("-f", "lavfi", "-i", "testsrc2=size=320x180:rate=10:duration=3", "-c:v", "libx264", "-pix_fmt", "yuv420p",
        os.path.join(d, "broll.mp4"))
    # a reference: two shots with burned-in captions at 68 % down, and its transcript next to it
    from vstudio import fonts
    font = fonts.resolve("Inter Black").replace("\\", "/").replace(":", "\\:")
    cap = ("drawtext=fontfile='%s':text='%s':fontsize=14:fontcolor=white:borderw=2:bordercolor=black:"
           "x=(w-tw)/2:y=h*0.68-th/2:enable='between(t,%s,%s)'")
    vf = ",".join([cap % (font, "STOP SCROLLING", 0.2, 1.5), cap % (font, "WATCH THIS", 1.5, 2.9)])
    _ff("-f", "lavfi", "-i", "color=c=0x1e2a44:s=180x320:r=10:d=1.5", "-f", "lavfi", "-i",
        "color=c=0x6a2c2c:s=180x320:r=10:d=1.5", "-filter_complex", "[0][1]concat=n=2:v=1:a=0," + vf + ",format=yuv420p",
        "-c:v", "libx264", os.path.join(d, "ref.mp4"))
    with open(os.path.join(d, "ref.srt"), "w", encoding="utf-8") as f:
        f.write("1\n00:00:00,200 --> 00:00:01,500\nStop scrolling.\n\n2\n00:00:01,500 --> 00:00:02,900\nWatch this.\n")


def tearDownModule():
    paths.CACHE = T["cache"]
    os.environ.pop("VSTUDIO_NO_WHISPER", None)
    shutil.rmtree(T["tmp"], ignore_errors=True)


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def _envelope(result="", structured=None, is_error=False):
    env = {"type": "result", "subtype": "success", "is_error": is_error, "result": result}
    if structured is not None:
        env["structured_output"] = structured
    return json.dumps(env)


class Runner:
    """A fake `claude` process: returns the queued outputs in order and records each call."""

    def __init__(self, *outputs):
        self.outputs = list(outputs)
        self.calls = []

    def __call__(self, argv, stdin, timeout, cwd, env):
        self.calls.append({"argv": list(argv), "stdin": stdin, "env": env, "cwd": cwd})
        out = self.outputs.pop(0)
        return out if isinstance(out, tuple) else (0, out, "")


class LLMTest(unittest.TestCase):
    def tearDown(self):
        llm.set_runner(None)
        os.environ.pop(llm.ENV_MODEL, None)

    def test_extract_json_fences_and_prose(self):
        self.assertEqual(llm.extract_json('```json\n{"a": 1}\n```'), {"a": 1})
        self.assertEqual(llm.extract_json('Here you go:\n```\n{"a": [1, 2]}\n```\nEnjoy.'), {"a": [1, 2]})
        self.assertEqual(llm.extract_json('Sure! {"plan": {"x": "y"}} hope it helps'), {"plan": {"x": "y"}})
        self.assertEqual(llm.extract_json('﻿  [1, 2] '), [1, 2])
        with self.assertRaises(ValueError):
            llm.extract_json("no json here")

    def test_structured_output_wins_and_flags(self):
        r = Runner(_envelope(result="ignored prose", structured={"ok": True}))
        llm.set_runner(r)
        self.assertTrue(llm.available())
        os.environ[llm.ENV_MODEL] = "some-model"
        data = llm.complete_json("plan it", schema={"type": "object", "required": ["ok"]}, system="be brief")
        self.assertEqual(data, {"ok": True})
        argv = r.calls[0]["argv"]
        self.assertEqual(argv[1:4], ["-p", "--output-format", "json"])
        self.assertIn("--json-schema", argv)
        self.assertEqual(argv[argv.index("--tools") + 1], "")            # no tools for planning
        self.assertEqual(argv[argv.index("--append-system-prompt") + 1], "be brief")
        self.assertEqual(argv[argv.index("--model") + 1], "some-model")
        self.assertIn("plan it", r.calls[0]["stdin"])                    # the prompt goes on stdin
        self.assertNotIn("CLAUDECODE", r.calls[0]["env"])

    def test_no_model_flag_by_default_and_fenced_result(self):
        r = Runner(_envelope(result='```json\n{"title": "x"}\n```'))
        llm.set_runner(r)
        self.assertEqual(llm.complete_json("q"), {"title": "x"})
        self.assertNotIn("--model", r.calls[0]["argv"])
        r.outputs.append(_envelope(result="hello"))
        self.assertEqual(llm.complete("q2"), "hello")

    def test_retry_feeds_problems_back(self):
        r = Runner(_envelope(result='{"n": 1}'), _envelope(result="not json at all"), _envelope(result='{"n": 3}'))
        llm.set_runner(r)
        data = llm.complete_json("count", check=lambda d: [] if d.get("n") == 3 else ["n must be 3"], retries=2)
        self.assertEqual(data, {"n": 3})
        self.assertIn("n must be 3", r.calls[1]["stdin"])               # the problem and the old answer go back
        self.assertIn('"n": 1', r.calls[1]["stdin"])
        self.assertIn("not valid JSON", r.calls[2]["stdin"])

    def test_gives_up_with_last_answer(self):
        llm.set_runner(Runner(_envelope(result='{"n": 1}'), _envelope(result='{"n": 2}')))
        with self.assertRaises(llm.LLMError) as cm:
            llm.complete_json("count", check=lambda d: ["always wrong"], retries=1)
        self.assertEqual(cm.exception.data, {"n": 2})
        self.assertEqual(cm.exception.errors, ["always wrong"])

    def test_schema_is_checked_locally(self):
        schema = {"type": "object", "required": ["beats"], "properties": {"beats": {"type": "array", "minItems": 1}}}
        self.assertEqual(llm.schema_errors({"beats": [1]}, schema), [])
        self.assertTrue(llm.schema_errors({"beats": []}, schema))
        self.assertTrue(llm.schema_errors({}, schema))
        self.assertTrue(llm.schema_errors({"beats": "x"}, schema))

    def test_login_error_is_unavailable(self):
        llm.set_runner(Runner((1, _envelope(result="Invalid API key · Please run /login", is_error=True), "")))
        with self.assertRaises(llm.LLMUnavailable):
            llm.complete("hi")

    def test_unknown_option_is_dropped(self):
        r = Runner((1, "", "error: unknown option '--tools'"), _envelope(result="ok"))
        llm.set_runner(r)
        self.assertEqual(llm.complete("hi"), "ok")
        self.assertNotIn("--tools", r.calls[1]["argv"])
        self.assertIn("--disallowedTools", r.calls[1]["argv"])

    def test_missing_cli(self):
        old = llm.claude_path
        llm.claude_path = lambda: None
        try:
            self.assertFalse(llm.available())
            with self.assertRaises(llm.LLMUnavailable):
                llm.complete("hi")
        finally:
            llm.claude_path = old


def _beat(s, e, **kw):
    b = {"start": s, "end": e, "say": None, "visual": None, "background": None, "transition": "cut", "sfx": None,
         "camera": "none"}
    b.update(kw)
    return b


class PlannerTest(unittest.TestCase):
    def assert_valid(self, plan, base_dir=None):
        self.assertEqual(planner.validate_plan(plan, base_dir=base_dir), [])
        spec = planner.compile_plan(plan, base_dir=base_dir)
        self.assertEqual(timeline.validate(spec), [])
        return spec

    def test_heuristic_plans_compile(self):
        cases = [("3 settings costing you views", SCRIPT, "high"),
                 ("Make a reel about 3 habits that doubled my focus: waking up early, no phone, and a long walk",
                  None, "medium"),
                 ("", "Wait. My channel grew from 200 to 12,000 subscribers in 90 days. iPhone vs Android? "
                      "\"Make the thing you wish existed\" said Ada. Stop guessing. Comment GUIDE.", "calm")]
        for prompt, script, energy in cases:
            plan = planner.heuristic_plan(prompt, script, {"energy": energy}, 20)
            spec = self.assert_valid(plan)
            beats = plan["beats"]
            self.assertEqual(beats[0]["start"], 0.0)
            self.assertEqual(beats[0]["visual"]["template"], "hook_title")
            for a, b in zip(beats, beats[1:]):
                self.assertAlmostEqual(a["end"], b["start"], places=3)
            self.assertIn(beats[-1]["visual"]["template"], ("cta",))
            self.assertAlmostEqual(spec["duration"], beats[-1]["end"], places=2)

    def test_heuristic_reads_the_content(self):
        plan = planner.heuristic_plan("", SCRIPT, {"energy": "high"}, 30)
        names = [(b["visual"] or {}).get("template") for b in plan["beats"]]
        self.assertEqual(names.count("list_item"), 3)
        self.assertIn("question", names)
        stat = next(b["visual"]["params"] for b in plan["beats"] if (b["visual"] or {}).get("template") == "stat")
        self.assertEqual(stat["value"], "73%")
        self.assertTrue(stat["label"].startswith("of viewers"))
        grow = planner.heuristic_plan("", "Look at this. We grew from 200 to 12,000 subscribers in 90 days.", None, 10)
        counter = [b["visual"]["params"] for b in grow["beats"] if (b["visual"] or {}).get("template") == "counter_reveal"]
        self.assertEqual((counter[0]["value"], counter[0]["from"]), (12000.0, 200.0))
        # numbers only come from the text
        calm = planner.heuristic_plan("", "Slow down. Breathe in. Notice the room around you. Let it go.", None, 12)
        self.assertFalse([b for b in calm["beats"] if (b["visual"] or {}).get("template") in
                          ("stat", "counter_reveal", "percent_ring")])

    def test_script_opening_on_item_one_keeps_every_item(self):
        script = ("Mistake one: everything is lit evenly. Mistake two: no shadows move. "
                  "Mistake three: the exit is always bright. Fix these and players will scream.")
        plan = planner.heuristic_plan("3 lighting mistakes in horror levels", script, None, 14)
        calls = [b["visual"] or {} for b in plan["beats"]]
        self.assertEqual(calls[0].get("template"), "hook_title")          # the prompt opens the reel
        self.assertEqual([c["params"]["number"] for c in calls if c.get("template") == "list_item"], [1, 2, 3])
        # a line that only the small words could match is still recognised as already on screen
        self.assertEqual(planner._overlap_share("Here is why.", [{"params": {"word": "HERE IS WHY"}}]), 1.0)
        # every spoken word is kept for QA's pace check, also where a graphic shows the line instead of captions
        spec = planner.compile_plan(plan)
        spoken = spec["meta"]["spoken_words"]
        self.assertEqual(len(spoken), sum(len(planner.clean_say(b.get("say") or "").split()) for b in plan["beats"]))
        self.assertLess(len((spec.get("captions") or {}).get("words") or []), len(spoken))

    def test_assets_alternate_and_voice_words_drive_beats(self):
        img = os.path.join(T["tmp"], "photo.png")
        plan = planner.heuristic_plan("", SCRIPT, None, 20, assets=[img])
        self.assertTrue(all(b["background"]["asset"] == img for b in plan["beats"]))
        text = "This changed everything. Here is the trick. Nobody ever talks about this part. Follow for more."
        words = [{"w": w, "s": i * 0.4, "e": i * 0.4 + 0.35} for i, w in enumerate(text.split())]
        vplan = planner.heuristic_plan("", None, None, 30, assets=[img], words=words)
        self.assertEqual([round(b["start"], 2) for b in vplan["beats"]], [0.0, 1.2, 2.8, 5.2])
        spec = self.assert_valid(vplan)
        caps = spec["captions"]["words"]
        self.assertTrue(caps and all(w in words for w in caps))       # the voice's own words and timing
        for b in vplan["beats"]:
            mine = [w for w in caps if b["start"] <= w["s"] < b["end"]]
            if b["visual"] is None:
                self.assertTrue(mine)                                  # a plain beat is carried by its captions
            elif b["visual"]["template"] == "kinetic_text":
                self.assertFalse(mine)                                 # kinetic text is the caption on its beat

    def test_compile_every_option(self):
        img = os.path.join(T["tmp"], "photo.png")
        bgs = [{"color": "#203040"}, {"gradient": ["#101820", "#FF3366"], "angle": 30}, {"keep": True},
               {"asset": img, "zoom": [1.0, 1.1], "focus": [0.4, 0.5]}, None, {"color": "#FFE500"},
               {"asset": img}, {"keep": True}]
        trs = [None, "flash", "whip", "zoom", "fade", "glitch", "slide", "cut"]
        cams = ["punch", "shake", "drift", "push", "none", "punch", "drift", "push"]
        beats = []
        for i in range(8):
            beats.append(_beat(i * 1.5, (i + 1) * 1.5, say="Line number %s here." % ["one", "two", "three", "four",
                                                                                     "five", "six", "seven", "eight"][i],
                               background=bgs[i], transition=trs[i], camera=cams[i],
                               sfx=["boom", "hit", "whoosh", "whoosh", None, "glitch", "swoosh", "riser"][i]))
        beats[0]["visual"] = {"template": "hook_title", "params": {"text": "Every option", "highlight": ["option"]}}
        beats[5]["visual"] = [{"template": "big_number_bg", "params": {"number": "6"}},
                              {"template": "stat", "params": {"value": "6X", "label": "faster", "position": "bottom"}}]
        plan = {"title": "t", "aspect": "9:16", "duration": 12, "beats": beats,
                "style": {"energy": "high", "grade": {"contrast": 1.05}, "grain": 0.03},
                "cta": {"template": "cta", "params": {"text": "Follow"}}}
        spec = self.assert_valid(plan, base_dir=T["tmp"])
        L = {l["id"]: l for l in spec["layers"]}
        fx = [(e["type"], e.get("start")) for e in spec["effects"]]
        self.assertIn(("flash", 1.48), fx)
        self.assertIn("zoom-blur", [t for t, _ in fx])
        self.assertIn("glitch", [t for t, _ in fx])
        self.assertIn("shake", [t for t, _ in fx])
        self.assertIn("grade", [t for t, _ in fx])
        self.assertIn("vignette", [t for t, _ in fx])
        self.assertEqual(L["bg.1"]["rotation"], -60.0)                     # a 30 degree gradient: the rect turned
        # kept through beat 3, then overlapping beat 4's zoom transition while it zooms out of frame
        self.assertAlmostEqual(L["bg.1"]["end"], 4.5 + L["bg.3"]["enter"]["duration"], places=3)
        self.assertEqual(L["bg.1"]["exit"]["type"], "zoom-in")
        self.assertEqual(L["bg.3"]["enter"]["type"], "zoom-out")           # zoom into beat 4
        self.assertEqual(L["bg.3"]["kenburns"], [1.0, 1.1])
        self.assertEqual(L["bg.3"]["exit"]["type"], "fade")                # beat 5 has no background: fade to it
        self.assertEqual(L["bg.6"]["enter"]["type"], "slide-left")
        self.assertEqual(L["bg.6"]["enter"]["distance"], 1080)
        self.assertEqual(L["bg.6"]["end"], 12.0)                           # kept to the end
        self.assertIn("zoom", spec["camera"])
        steps = [k for k in spec["camera"]["zoom"] if len(k) == 3 and k[2] == "step"]
        self.assertTrue(steps)                                             # hard cuts snap the camera
        tpl = [l for l in spec["layers"] if l["id"][:1] == "b" and l["id"][1:3].isdigit()]
        self.assertTrue(tpl and all(l["fixed"] for l in tpl))           # graphics are HUD: the camera moves footage
        sfx_starts = {a["sfx"]: a["start"] for a in spec["audio"] if a.get("sfx")}
        self.assertAlmostEqual(sfx_starts["swoosh"], 9.0 - planner.SFX_LEAD["swoosh"], places=3)
        self.assertAlmostEqual(sfx_starts["riser"], 10.5 - 2.0, places=3)  # a riser ends on its cut
        last = [l for l in spec["layers"] if l["id"].startswith("b08.cta")]
        self.assertTrue(last)
        self.assertTrue(all(l["exit"] is None for l in last if l["end"] >= 12.0 - 1e-3))   # the reel ends on it held
        self.assertTrue([l for l in spec["layers"] if l["id"].startswith("ambient.")])
        self.assertTrue([l for l in spec["layers"] if l["id"].startswith("scrim.")])

    @unittest.skipUnless(HAVE_FFMPEG, "ffmpeg missing")
    def test_video_backgrounds_whip_fade_and_cut_every(self):
        clip = os.path.join(T["tmp"], "broll.mp4")
        beats = [_beat(0, 2.0, background={"asset": clip, "in": 0.5}, camera="punch"),
                 _beat(2.0, 3.2, background={"asset": clip}, transition="whip"),
                 _beat(3.2, 4.4, background={"asset": clip, "in": 2.9}, transition="fade")]
        plan = {"aspect": "16:9", "duration": 4.4, "beats": beats, "style": {"cut_every": 0.8, "energy": "medium"}}
        spec = self.assert_valid(plan)
        vids = [l for l in spec["layers"] if l["type"] == "video"]
        first = [l for l in vids if l["id"].startswith("bg.0")]
        self.assertEqual(len(first), 2)                                    # 2 s at one cut per 0.8 s: two shots
        self.assertGreater(first[1]["in"], first[0]["in"])                 # a jump forward in the clip
        whip_in = next(l for l in vids if l["id"] == "bg.1")
        self.assertEqual(whip_in["enter"]["type"], "whip-left")
        self.assertEqual(first[-1]["exit"]["type"], "whip-left")
        self.assertGreater(first[-1]["end"], 2.0)                          # overlaps the incoming shot
        fade_in = next(l for l in vids if l["id"] == "bg.2")
        self.assertEqual(fade_in["enter"]["type"], "crossfade")
        self.assertLessEqual(fade_in["in"] + 1.2, 3.0 + 1e-6)              # an in point past the end is pulled back
        self.assertTrue(all(l["volume"] == 0 for l in vids))               # b-roll is muted unless asked

    def test_samples_never_ship_and_numbers_are_required(self):
        plan = {"aspect": "9:16", "duration": 4, "beats": [
            _beat(0, 2, visual={"template": "lower_third", "params": {}}),
            _beat(2, 4, visual={"template": "counter_reveal", "params": {"label": "views"}})]}
        errs = planner.validate_plan(plan)
        self.assertTrue(any("lower_third" in e and "draw nothing" in e for e in errs))
        self.assertTrue(any("counter_reveal" in e and "params.value" in e for e in errs))
        spec = planner.compile_plan(plan)
        self.assertEqual(timeline.validate(spec), [])
        texts = json.dumps(spec)
        self.assertNotIn("Alex Rivera", texts)
        self.assertNotIn("12,500", texts)
        self.assertIn("counter_reveal dropped", spec["meta"]["notes"])

    def test_validate_reports_plan_problems(self):
        plan = {"aspect": "3:2", "duration": 5, "beats": [
            _beat(0.5, 2, visual={"template": "hook_titel", "params": {}}),
            _beat(3, 5, visual={"template": "stat", "params": {"value": "9", "lable": "x"}}, transition="wipe",
                  sfx="whooosh", camera="orbit", background={"asset": "missing.mp4"})]}
        errs = "\n".join(planner.validate_plan(plan, assets=[]))
        for want in ("aspect must be one of", "beats[0] must start at 0", "did you mean 'hook_title'",
                     "gap after beats[0]", "unknown param 'lable'", "did you mean 'label'", "transition must be one of",
                     "did you mean 'whoosh'", "camera must be one of", "file not found"):
            self.assertIn(want, errs)

    def test_captions_timed_inside_beats_and_kept_off_graphics(self):
        plan = {"aspect": "9:16", "duration": 6, "style": {"caption_style": {"center_y_pct": 70, "font_size_pct": 4.6}},
                "beats": [_beat(0, 2, say="This is a very long line that will never fit in a short beat at all."),
                          _beat(2, 4, say="Ranking time.", visual={"template": "ranking", "params": {"items": ["A", "B"]}}),
                          _beat(4, 6, say="Short one here.",
                                visual={"template": "stat", "params": {"value": "9X", "label": "x", "position": "bottom"}})],
                "voiceover": {"src": None, "words": None}}
        spec = self.assert_valid(plan)
        words = spec["captions"]["words"]
        self.assertTrue(all(0 <= w["s"] < w["e"] <= 2.0 for w in words if w["s"] < 2))     # squeezed into beat 1
        self.assertFalse([w for w in words if 2.0 <= w["s"] < 4.0])                       # ranking hides captions
        self.assertTrue([w for w in words if w["s"] >= 4.0])
        self.assertEqual(spec["captions"]["style"]["center_y_pct"], 70)
        stat = templates.build("stat", {"value": "9X", "label": "x", "position": "center"}, 4, 6,
                               {"width": 1080, "height": 1920, "fps": 30})
        moved = [l for l in spec["layers"] if l["id"].startswith("b03.stat")]
        self.assertEqual([round(l["y"]) for l in moved], [round(l["y"]) for l in stat])  # bottom -> centre
        # captions are pulled into the safe zone and narrowed to it
        cs = planner._caption_style_for({"caption_style": {"center_y_pct": 92, "max_width_pct": 97}}, 1920, 1080)
        self.assertLessEqual(cs["center_y_pct"] + 2.9, 80.0)
        self.assertEqual(cs["max_width_pct"], 88.0)

    def test_plan_cli(self):
        d = tempfile.mkdtemp(dir=T["tmp"])
        plan = planner.heuristic_plan("", SCRIPT, None, 15)
        p = os.path.join(d, "plan.json")
        with open(p, "w", encoding="utf-8") as f:
            json.dump(plan, f)
        out = os.path.join(d, "spec.json")
        self.assertEqual(planner.main(["compile", p, "--out", out]), 0)
        self.assertEqual(timeline.validate(timeline.load(out)), [])
        self.assertEqual(planner.main(["validate", p]), 0)
        self.assertEqual(planner.main(["heuristic", "--prompt", "Coffee: beans, water, time", "--out",
                                       os.path.join(d, "p2.json"), "--spec", os.path.join(d, "s2.json")]), 0)

    def _shown_numbers(self, plan):
        out = []
        for b in plan["beats"]:
            for c in planner.visual_calls(b["visual"]):
                for k, v in (c.get("params") or {}).items():
                    if k in ("text", "value", "label", "word", "highlight"):
                        for x in (v if isinstance(v, list) else [v]):
                            out += re.findall(r"\d[\d,.]*", str(x))
        return [n.rstrip(".,") for n in out]

    def test_decimals_and_abbreviations_do_not_split_sentences(self):
        self.assertEqual(planner._sentences("How I grew 3.5x faster in 30 days. Then it stopped."),
                         ["How I grew 3.5x faster in 30 days.", "Then it stopped."])
        self.assertEqual(planner._sentences("I made $1,250.50 from one reel! Wild."),
                         ["I made $1,250.50 from one reel!", "Wild."])
        self.assertEqual(planner._sentences("iPhone vs. Android, e.g. for video. Dr. Lee agrees. Visit vstudio.app now."),
                         ["iPhone vs. Android, e.g. for video.", "Dr. Lee agrees.", "Visit vstudio.app now."])
        self.assertEqual(planner._sentences("stop scrolling. this changes everything"),
                         ["stop scrolling.", "this changes everything"])            # lowercase scripts still split
        for text, nums in (("How I grew 3.5x faster in 30 days", {"3.5", "30"}),
                           ("I made $1,250.50 from one reel", {"1,250.50"}),
                           ("We hit 2.5M views", {"2.5"})):
            plan = planner.heuristic_plan(text, None, None, 10)
            self.assertEqual(plan["beats"][0]["say"].rstrip("."), text)              # one line, not two halves
            self.assertTrue(set(self._shown_numbers(plan)) <= nums, (text, self._shown_numbers(plan)))
        plan = planner.heuristic_plan("", "Which phone shoots better video? iPhone vs. Android. Here is the answer.",
                                      None, 12)
        self.assertIn("versus", [(b["visual"] or {}).get("template") for b in plan["beats"]])
        self.assertEqual(planner.headline("iPhone vs. Android"), "iPhone vs. Android")    # not "IPhone"
        self.assertEqual(planner.headline("stop posting at night"), "Stop posting at night")
        plan = planner.heuristic_plan("iPhone vs. Android is the question. Here is the answer.", None, None, 12)
        self.assertEqual(plan["beats"][0]["say"], "iPhone vs. Android is the question.")  # the spoken line too
        plan = planner.heuristic_plan("Best phones: iPhone, Pixel and eBay deals", None, None, 12)
        self.assertIn("iPhone.", " ".join(b["say"] or "" for b in plan["beats"]))
        words = [{"w": w, "s": i * 0.4, "e": i * 0.4 + 0.3} for i, w in enumerate("We hit 2.5M views. Dr. Lee said so.".split())]
        self.assertEqual([len(s) for s in planner.sentences_from_words(words)], [4, 4])

    def test_headline_never_ends_on_a_dangling_word(self):
        for line, want in (("Follow for part 2 where I break down the hooks.", "Follow for part 2"),
                           ("Comment GUIDE and I will send you the checklist.", "Comment GUIDE"),
                           ("Follow me if you want more tips like this one.", "Follow me"),
                           ("So if this helped, share this with a friend who needs it.", "Share this with a friend")):
            plan = planner.heuristic_plan("", "Stop guessing your hooks. Most creators never test them. " + line, None, 12)
            cta = plan["beats"][-1]["visual"]
            self.assertEqual(cta["template"], "cta")
            self.assertEqual(cta["params"]["text"], want)
        dangling = {"and", "or", "but", "where", "when", "if", "to", "for", "before", "that", "which", "with", "of",
                    "the", "a", "my", "your"}
        for line in ("Most creators quit right before it starts working for them every single time.",
                     "The one setting that nobody checks and that costs you half of your views.",
                     "Follow for part 2 where I break down the hooks."):
            for n in (4, 5, 7):
                h = planner.headline(line, n)
                self.assertNotIn(h.split()[-1].lower(), dangling, (line, n, h))
                self.assertLessEqual(len(h.split()), n + 2)

    def test_long_line_without_a_comma_splits_between_phrases(self):
        # no comma or pivot word: the beat break goes before "to", not after "my" (mid-phrase, with a whip and a whoosh)
        line = "I rebuilt the first room of my own game 12 times to find out why."
        self.assertEqual(planner._split_long(line), ["I rebuilt the first room of my own game 12 times", "to find out why."])
        for line in ("The quick brown fox jumps over the lazy dog every single morning near the old red barn.",
                     "one two three four five six seven eight nine ten eleven twelve thirteen fourteen"):
            parts = planner._split_long(line)
            self.assertEqual(" ".join(parts), line)
            self.assertTrue(all(3 <= len(p.split()) <= 13 for p in parts), parts)
            self.assertNotIn(re.sub(r"\W", "", parts[0].split()[-1].lower()), planner._DANGLING, parts)
        # a short tail after the number is its label (the captions carry the rest); a short line keeps its lead-in
        for text, want in (("I rebuilt the first room of my own game 12 times", "times"), ("I lost $300 today", "I lost today"),
                           ("73% of viewers decide in the first second", "of viewers decide in the first second")):
            self.assertEqual(planner._label_around(text, planner._number(text)), want)
        # the counter's number counts as shown text, so a caption that only repeats the card is left out
        call = {"template": "counter_reveal", "params": {"value": 12.0, "label": "times to find out why"}}
        self.assertGreaterEqual(planner._overlap_share("own game 12 times to find out why.", [call]), 0.6)
        self.assertLess(planner._overlap_share("own game 12 times to find out why.",
                                               [dict(call, template="kinetic_text")]), 0.6)

    def test_symbol_only_prompt_still_opens_on_a_hook(self):
        for prompt in ("🔥🔥🔥", "!!! ???", "***"):
            plan = planner.heuristic_plan(prompt, None, None, 8)
            self.assertEqual(planner.validate_plan(plan), [], prompt)
            self.assertTrue(plan["beats"][0]["visual"]["params"]["text"].strip())
            spec = planner.compile_plan(plan)
            self.assertTrue([l for l in spec["layers"] if l["id"].startswith("b01.") and l["type"] == "text"])
        # a script without words falls back to the prompt
        plan = planner.heuristic_plan("Three habits that fixed my sleep", "🔥🔥", None, 8)
        self.assertIn("habits", plan["beats"][0]["say"].lower())

    def test_non_latin_lines_are_timed_like_speech(self):
        en = planner.speak_seconds("This is exactly how I grew my channel in one single month.")
        for line in ("Вот как именно я вырастил свой канал всего за один месяц.",
                     "ठीक इसी तरह मैंने सिर्फ एक महीने में अपना चैनल बढ़ाया।",
                     "这就是我在短短一个月内发展频道的方法。",
                     "これがたった一か月でチャンネルを伸ばした方法です。",
                     "이것이 내가 한 달 만에 채널을 키운 방법입니다."):
            t = planner.speak_seconds(line)
            self.assertTrue(0.5 * en <= t <= 2.0 * en, (line, t, en))
        zh = planner.time_words("这就是我在短短一个月内发展频道的方法。", 0, 6)
        self.assertGreater(len(zh), 3)                                       # not one 18-character caption "word"
        self.assertEqual("".join(w["w"] for w in zh), "这就是我在短短一个月内发展频道的方法。")
        self.assertTrue(all(len(w["w"]) <= 5 for w in zh))
        self.assertEqual(planner._syllables("café"), 2)
        self.assertEqual(planner._syllables("2026"), 4)

    def test_malformed_plans_never_crash_the_checks(self):
        good = _beat(0, 3, say="hello there friend")
        bad_plans = [
            {"aspect": ["9:16"], "beats": [good]},
            {"beats": [_beat(0, 3, visual={"template": "hook_title", "params": ["I LOST $300"]})]},
            {"beats": [_beat(0, 3, visual={"template": "hook_title", "params": "HELLO"})]},
            {"beats": [_beat(0, 3, visual={"template": ["hook_title"], "params": {"text": "x"}})]},
            {"beats": [_beat(0, 3, visual={"template": {"n": 1}, "params": {"text": "x"}})]},
            {"beats": [good], "cta": {"template": "cta", "params": ["Follow"]}},
            {"beats": [good], "cta": {"template": ["cta"], "params": {"text": "Follow"}}},
            {"beats": [good], "music": {"src": ["a.mp3"]}},
            {"beats": [good], "voiceover": {"src": {"x": 1}, "words": None}},
            {"beats": [_beat(0, 3, visual={"template": "list_item", "params": {"text": "x", "number": "1e400"}})]},
            {"beats": [_beat(0, 3, visual={"template": "counter_reveal", "params": {"value": float("inf")}})]},
            {"beats": [_beat(0, 3, visual={"template": "bar_chart", "params": {"labels": ["a"], "values": [1e400]}})]},
            {"beats": [_beat(0, 3, background={"asset": ["x.mp4"]})]},
            {"beats": [_beat(0, 3, background={"color": 7})]},
            {"beats": [_beat(0, 3, background={"gradient": "#000000"})]},
            {"beats": [_beat(0, 3, background={"color": "#101010", "zoom": 2, "focus": "x"})]},
            {"beats": [_beat(0, 3, say=["a", "b"])]},
        ]
        for plan in bad_plans:
            errs = planner.validate_plan(plan)
            self.assertTrue(errs, plan)
            try:
                spec = planner.compile_plan(plan)
            except ValueError:
                continue
            self.assertEqual(timeline.validate(spec), [], plan)
            self.assertNotIn("12,500", json.dumps(spec))                     # a broken number never becomes a sample
            self.assertNotIn("12500", json.dumps([l for l in spec["layers"] if l["type"] == "counter"]))

    def test_music_may_be_a_video_file(self):
        song = os.path.join(T["tmp"], "song.mp4")
        with open(song, "wb") as f:
            f.write(b"\0" * 64)
        plan = {"aspect": "9:16", "duration": 3, "beats": [_beat(0, 3, say="hello")], "music": {"src": song}}
        self.assertEqual(planner.validate_plan(plan), [])
        plan["music"]["src"] = os.path.join(T["tmp"], "photo.png")
        self.assertTrue(any("music.src" in e for e in planner.validate_plan(plan)))

    def test_unreadable_assets_are_skipped(self):
        broken_png = os.path.join(T["tmp"], "broken.png")
        broken_mp4 = os.path.join(T["tmp"], "broken.mp4")
        for p in (broken_png, broken_mp4):
            with open(p, "wb") as f:
                f.write(b"not media at all " * 20)
        skipped = []
        rows = planner.asset_rows([broken_png, os.path.join(T["tmp"], "photo.png")], skipped)
        self.assertEqual([r["path"] for r in rows], [os.path.join(T["tmp"], "photo.png")])
        self.assertEqual([s[0] for s in skipped], [broken_png])
        if HAVE_FFMPEG:
            good = os.path.join(T["tmp"], "broll.mp4")
            skipped = []
            rows = planner.asset_rows([broken_mp4, good], skipped)
            self.assertEqual([r["path"] for r in rows], [good])
            self.assertEqual([s[0] for s in skipped], [broken_mp4])
            plan = planner.heuristic_plan("", SCRIPT, None, 12, assets=[broken_mp4, good])
            self.assertTrue(all(b["background"]["asset"] == good for b in plan["beats"]))
            self.assertIn("could not be read", plan["notes"])


class DirectorChecksTest(unittest.TestCase):
    def test_invented_numbers(self):
        plan = {"beats": [_beat(0, 2, say="We hit 47% retention and 3 tips.",
                                visual={"template": "stat", "params": {"value": "1,200", "label": "views"}})]}
        errs = director.invented_numbers(plan, "my reel about retention, 47 percent")
        self.assertEqual(len(errs), 1)
        self.assertIn("1,200", errs[0])
        self.assertEqual(director.invented_numbers(plan, "47% and 1200 views"), [])

    def test_invented_numbers_ignores_layout_params(self):
        layout = [{"template": "emphasis_word", "params": {"word": "LUNCH", "y": 0.3, "tilt": -3.5}},
                  {"template": "highlight_box", "params": {"x": 0.42, "y": 0.61, "w": 0.55, "h": 0.18, "thickness": 12,
                                                           "color": "#1E2A44"}},
                  {"template": "arrow", "params": {"x": 0.3, "y": 0.7, "length": 420}},
                  {"template": "icon_badge", "params": {"size": 260, "y": 0.35}},
                  {"template": "kinetic_text", "params": {"text": "post at lunch", "times": [0.12, 0.55, 0.93]}},
                  {"template": "counter_reveal", "params": {"value": 47, "label": "retention", "duration": 1.35}},
                  {"template": "list_item", "params": {"number": 2, "total": 3, "text": "cut the intro", "y": 0.62}}]
        plan = {"beats": [_beat(0, 2, say="Post at lunch.", visual=layout)],
                "cta": {"template": "cta", "params": {"text": "Follow", "color": "#FF3366"}}}
        self.assertEqual(director.invented_numbers(plan, "post at lunch, 47% retention"), [])
        # numbers a template shows are still checked, in number and in text params
        plan["beats"][0]["visual"] = [{"template": "counter_reveal", "params": {"value": 340, "from": 120}},
                                      {"template": "stat", "params": {"value": "340%", "label": "in 90 days"}},
                                      {"template": "hook_title", "params": {"text": "I made 12k", "size": 120}}]
        errs = "\n".join(director.invented_numbers(plan, "post at lunch"))
        for want in ("counter_reveal.value", "counter_reveal.from", "stat.value", "stat.label", "hook_title.text"):
            self.assertIn(want, errs)
        self.assertNotIn("size", errs)

    def test_copied_lines_and_script_coverage(self):
        plan = {"beats": [_beat(0, 2, say="nobody tells you this about the algorithm and your first hundred videos")]}
        theirs = ["so nobody tells you this about the algorithm and your first hundred videos ok"]
        self.assertTrue(director.copied_lines(plan, theirs))
        self.assertFalse(director.copied_lines(plan, theirs, own_text=plan["beats"][0]["say"]))
        self.assertGreater(director.script_coverage({"beats": [_beat(0, 1, say=SCRIPT)]}, SCRIPT), 0.99)
        self.assertLess(director.script_coverage({"beats": [_beat(0, 1, say="something else")]}, SCRIPT), 0.2)

    def test_edit_from_report_layouts(self):
        rep = {"video": {"duration": 10.0}, "shots": [[0, 2.5], [2.5, 5], [5, 10]], "pacing": {"mean_shot_s": 3.33},
               "audio": {"bpm": 120, "beats": [2.5, 5.0]}, "shot_details": [{"camera": {"kind": ["push_in"]}}]}
        e = director.edit_from_report(rep)
        self.assertEqual((e["shots"], e["cuts_per_10s"], e["first_cut_s"], e["cuts_on_beat"]), (3, 2.0, 2.5, 1.0))
        self.assertEqual(e["camera_moves"], {"push_in": 1})
        self.assertIsNone(director.edit_from_report({}))

    def test_palette_prefers_caption_highlight(self):
        refs = [{"dna": {"text": {"caption_track": {"cards": 4, "highlight": "#00E5FF", "fill": "#FFFFFF"}}},
                 "report": {"shot_details": [{"start": 0, "end": 2, "look": {"dominant": ["#0A0A10", "#FF2244"]}}]}}]
        pal = director.palette(refs)
        self.assertEqual(pal["accent"], "#00E5FF")
        self.assertEqual(pal["text"], "#FFFFFF")
        self.assertLess(director._lum(pal["bg"]), 0.1)


class FakeClient:
    """Stands in for llm: walks a list of canned plans through the director's check, like complete_json's retries."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.prompts, self.errors = [], []

    def complete_json(self, prompt, schema=None, system=None, timeout=900, retries=2, check=None):
        self.prompts.append(prompt)
        self.schema, self.system = schema, system
        last = None
        for ans in self.answers[:retries + 1]:
            last = json.loads(json.dumps(ans))
            errs = check(last) if check else []
            self.errors.append(errs)
            if not errs:
                return last
        raise llm.LLMError("still wrong", data=last, errors=self.errors[-1])


class DirectorFlowTest(unittest.TestCase):
    def _plan(self, asset, n_extra=None):
        beats = [_beat(0, 1.4, say="Stop guessing your edits.", visual={"template": "hook_title", "params": {
                     "text": "Stop guessing", "highlight": ["guessing"]}}, background={"asset": asset, "in": 0.2},
                       camera="punch", sfx="boom", transition=None),
                 _beat(1.4, 2.6, say="Copy the rhythm, not the words.", background={"asset": asset, "in": 1.0},
                       transition="whip", sfx="whoosh", camera="drift"),
                 _beat(2.6, 3.6, say="Follow for more.", visual={"template": "cta", "params": {"text": "Follow"}},
                       transition="flash", sfx="ding")]
        if n_extra:
            beats[1]["say"] = "Copy the rhythm, it doubled views by %s." % n_extra
        return {"title": "Rhythm", "aspect": "9:16", "fps": 30, "duration": 3.6, "style": {"energy": "high"},
                "beats": beats, "cta": None}

    @unittest.skipUnless(HAVE_FFMPEG, "ffmpeg missing")
    def test_make_reel_with_fake_llm(self):
        out = os.path.join(T["tmp"], "reel1")
        asset = os.path.join(T["tmp"], "broll.mp4")
        good = self._plan(asset)
        invented = self._plan(asset, n_extra="340%")
        client = FakeClient([invented, good])
        res = director.make_reel("why copying rhythm works", refs=[os.path.join(T["tmp"], "ref.mp4")],
                                 assets=[asset], duration=3.6, out_dir=out, llm="claude", client=client, preview=True,
                                 log=None)
        self.assertEqual(res["planner"], "claude")
        self.assertTrue(any("not in the creator's prompt" in e for e in client.errors[0]))   # 340% was refused
        self.assertEqual(client.errors[-1], [])
        prompt = client.prompts[0]
        self.assertIn("# Motion-graphics templates", prompt)                  # catalog
        self.assertIn("Stop scrolling", prompt)                               # reference transcript (from the SRT)
        self.assertIn(asset, prompt)
        self.assertEqual(client.schema, director.PLAN_SCHEMA)
        for name in ("BRIEF.md", "plan.json", "spec.json", "reel.mp4", "QA.md", "analysis.json"):
            self.assertTrue(os.path.isfile(os.path.join(out, name)), name)
        spec = timeline.load(os.path.join(out, "spec.json"))
        self.assertEqual(timeline.validate(spec), [])
        # the reference's measured caption look went into the plan style (reading on-screen text needs the OCR)
        plan = json.loads(_read(os.path.join(out, "plan.json")))
        if HAVE_OCR:
            self.assertIn("caption_style", plan["style"])
            self.assertAlmostEqual(plan["style"]["caption_style"]["center_y_pct"], 68, delta=4)
        info = timeline.probe(res["video"])
        self.assertEqual((info["width"], info["height"]), (540, 960))
        self.assertAlmostEqual(info["duration"], 3.6, delta=0.2)
        brief = _read(res["brief"])
        self.assertIn("Style targets", brief)
        self.assertIn("Cuts per 10 s", brief)
        qa = _read(res["qa"])
        self.assertIn("| Length |", qa)
        self.assertTrue(os.path.isfile(res["contact_sheet"]))
        # agent mode: the same folder re-rendered from the written plan, targets reused, no render
        res2 = director.make_reel("", plan=os.path.join(out, "plan.json"), out_dir=out, render=False, log=None)
        self.assertEqual(res2["planner"], "file")
        self.assertEqual(res2["plan_errors"], [])

    def test_failing_llm_falls_back_and_cli(self):
        out = os.path.join(T["tmp"], "reel2")
        client = FakeClient([{"title": "x", "beats": []}])
        res = director.make_reel("3 habits: sleep, walk, read", script=None, duration=12, out_dir=out, llm="auto",
                                 client=client, render=False, log=None)
        self.assertEqual(res["planner"], "heuristic")
        self.assertTrue(any("offline planner" in n for n in res["notes"]))
        self.assertEqual(timeline.validate(timeline.load(res["spec"])), [])
        out3 = os.path.join(T["tmp"], "reel3")
        script = os.path.join(T["tmp"], "script.txt")
        with open(script, "w", encoding="utf-8") as f:
            f.write(SCRIPT)
        self.assertEqual(director.main(["--script", script, "--duration", "20", "--out-dir", out3, "--llm", "none",
                                        "--no-render"]), 0)
        brief = _read(os.path.join(out3, "BRIEF.md"))
        self.assertIn("scriptcheck", brief)

    def _simple_plan(self, **kw):
        plan = {"title": "Stop posting at night", "aspect": "9:16", "duration": 6, "style": {}, "cta": None,
                "beats": [_beat(0, 3, say="Stop posting at night.", transition=None, camera="punch", sfx="hit",
                                visual={"template": "hook_title", "params": {"text": "STOP POSTING AT NIGHT"}}),
                          _beat(3, 6, say="Post at lunch instead.",
                                visual={"template": "emphasis_word", "params": {"word": "LUNCH", "y": 0.3}})]}
        plan.update(kw)
        return plan

    def _runner(self, *answers):
        calls = []

        def run(argv, stdin, timeout, cwd, env):
            calls.append(stdin)
            ans = answers[min(len(calls), len(answers)) - 1]
            return 0, _envelope(result=ans if isinstance(ans, str) else json.dumps(ans)), ""
        return run, calls

    def tearDown(self):
        llm.set_runner(None)

    def test_layout_numbers_and_video_music_pass_the_checks(self):
        song = os.path.join(T["tmp"], "track.mp4")
        with open(song, "wb") as f:
            f.write(b"\0" * 64)
        for music in (None, song):
            run, calls = self._runner(self._simple_plan())
            llm.set_runner(run)
            res = director.make_reel("Stop posting at night. Post at lunch instead.", music=music, duration=6,
                                     out_dir=tempfile.mkdtemp(dir=T["tmp"]), llm="claude", render=False, log=None)
            self.assertEqual((res["planner"], len(calls)), ("claude", 1), res["notes"])
            plan = json.loads(_read(res["plan"]))
            self.assertEqual((plan.get("music") or {}).get("src"), music)
        with self.assertRaises(director.DirectorError):
            director.make_reel("x", music=os.path.join(T["tmp"], "photo.png"), duration=6, llm="none", render=False,
                               out_dir=tempfile.mkdtemp(dir=T["tmp"]), log=None)

    def test_malformed_answers_go_back_to_claude(self):
        base = self._simple_plan()
        hook = base["beats"][0]

        def with_hook(**visual):
            p = json.loads(json.dumps(base))
            p["beats"][0]["visual"] = dict(hook["visual"], **visual)
            return p
        cases = {"params_list": with_hook(params=["I LOST $300"]),
                 "params_str": with_hook(params="HELLO"),
                 "template_list": with_hook(template=["hook_title"]),
                 "huge_int": with_hook(template="list_item", params={"text": "post later", "number": "1e400"}),
                 "cta_params_list": dict(json.loads(json.dumps(base)), cta={"template": "cta", "params": ["Follow"]}),
                 "music_src_list": dict(json.loads(json.dumps(base)), music={"src": ["a.mp3"]}),
                 "beats_of_strings": dict(json.loads(json.dumps(base)), beats=["hello", "there"])}
        for name, bad in cases.items():
            run, calls = self._runner(bad, base)
            llm.set_runner(run)
            res = director.make_reel("Stop posting at night. Post at lunch instead.", duration=6, llm="claude",
                                     out_dir=tempfile.mkdtemp(dir=T["tmp"]), render=False, log=None)
            self.assertEqual((res["planner"], len(calls)), ("claude", 2), (name, res["notes"]))
            self.assertIn("# Your previous answer had these problems", calls[1], name)
        # a check that fails on a shape nobody foresaw sends the error back instead of crashing the reel
        old_copied, hits = director.copied_lines, []

        def flaky(*a, **kw):
            hits.append(1)
            if len(hits) == 1:
                raise TypeError("unhashable type: 'list'")
            return old_copied(*a, **kw)
        director.copied_lines = flaky
        try:
            run, calls = self._runner(base, base)
            llm.set_runner(run)
            res = director.make_reel("Stop posting at night. Post at lunch instead.", duration=6, llm="claude",
                                     out_dir=tempfile.mkdtemp(dir=T["tmp"]), render=False, log=None)
        finally:
            director.copied_lines = old_copied
        self.assertEqual((res["planner"], len(calls)), ("claude", 2), res["notes"])
        self.assertIn("could not be checked (TypeError", calls[1])
        # a plan file with the same faults is reported, never a traceback
        for name, bad in cases.items():
            d = tempfile.mkdtemp(dir=T["tmp"])
            p = os.path.join(d, "plan.json")
            with open(p, "w", encoding="utf-8") as f:
                json.dump(bad, f)
            try:
                res = director.make_reel("", plan=p, out_dir=d, render=False, log=None)
            except director.DirectorError:
                continue
            self.assertTrue(res["plan_errors"], name)

    def _wav(self, path, seconds=2.0, sr=16000):
        with wave.open(path, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(sr)
            w.writeframes(b"\0\0" * int(seconds * sr))
        return path

    def test_voice_words_sidecar_in_every_documented_shape(self):
        d = tempfile.mkdtemp(dir=T["tmp"])
        shapes = {"whisperx": [{"word": "Stop", "start": 0.1, "end": 0.4}, {"word": "posting", "start": 0.45, "end": 0.9},
                               {"word": "at", "start": 0.95, "end": 1.05}, {"word": "night.", "start": 1.1, "end": 1.6}],
                  "segments": {"segments": [{"start": 0.1, "end": 1.6, "text": "Stop posting at night."}]},
                  "unsorted": [{"w": "night.", "s": 1.1, "e": 1.6}, {"w": "Stop", "s": 0.1, "e": 0.4},
                               {"w": "posting", "s": 0.45, "e": 0.9}, {"w": "at", "s": 0.95, "e": 1.05}]}
        for name, data in shapes.items():
            voice = self._wav(os.path.join(d, "%s.wav" % name))
            with open(os.path.join(d, "%s.words.json" % name), "w", encoding="utf-8") as f:
                json.dump(data, f)
            words = director._voice_words(voice)
            self.assertEqual([w["w"] for w in words], ["Stop", "posting", "at", "night."], name)
            self.assertTrue(all(set(w) >= {"w", "s", "e"} for w in words))
            self.assertEqual(words, sorted(words, key=lambda w: w["s"]))
            res = director.make_reel("Stop posting at night", voice=voice, duration=10, llm="none", render=False,
                                     out_dir=os.path.join(d, "out_" + name), log=None)
            self.assertAlmostEqual(json.loads(_read(res["plan"]))["duration"], 1.6 + 0.5 + 2.2, delta=0.6)
        # a transcript file that cannot be read: the reel is still made, and the brief says why it was not used
        voice = self._wav(os.path.join(d, "broken.wav"))
        with open(os.path.join(d, "broken.words.json"), "w", encoding="utf-8") as f:
            f.write("{not json")
        self.assertIsNone(director._voice_words(voice))
        res = director.make_reel("Stop posting at night", voice=voice, duration=10, llm="none", render=False,
                                 out_dir=os.path.join(d, "out_broken"), log=None)
        self.assertTrue(any("broken.words.json" in n and "could not be used" in n for n in res["notes"]), res["notes"])

    def test_download_follows_the_link(self):
        d = tempfile.mkdtemp(dir=T["tmp"])
        stem = os.path.join(d, "inputs", "ref1")
        calls = []

        def fake_run(cmd, **kw):
            calls.append(cmd)
            url = cmd[-1]
            if "--skip-download" not in cmd:
                with open(stem + ".mp4", "w", encoding="utf-8") as f:
                    f.write(url)
            return subprocess.CompletedProcess(cmd, 0, "", "")
        old_run, old_have = director.subprocess.run, director._have_ytdlp
        director.subprocess.run, director._have_ytdlp = fake_run, lambda: True
        try:
            v1, _ = director.download("https://www.youtube.com/shorts/AAA", stem)
            self.assertEqual(_read(v1), "https://www.youtube.com/shorts/AAA")
            n = len(calls)
            v1b, _ = director.download("https://www.youtube.com/shorts/AAA", stem)
            self.assertEqual((v1b, len(calls)), (v1, n))                       # same link: the file is reused
            os.makedirs(os.path.dirname(stem), exist_ok=True)
            with open(stem + ".en.vtt", "w", encoding="utf-8") as f:
                f.write("WEBVTT\n")
            v2, caps = director.download("https://www.youtube.com/shorts/COMPLETELY_DIFFERENT", stem)
            self.assertEqual(_read(v2), "https://www.youtube.com/shorts/COMPLETELY_DIFFERENT")
            self.assertIsNone(caps)                                            # the old link's captions are gone
            # a file from before the link was recorded next to it could be any video: it is fetched again
            os.remove(stem + ".source.json")
            with open(stem + ".mp4", "w", encoding="utf-8") as f:
                f.write("an older download")
            v3, _ = director.download("https://www.youtube.com/shorts/BBB", stem)
            self.assertEqual(_read(v3), "https://www.youtube.com/shorts/BBB")
        finally:
            director.subprocess.run, director._have_ytdlp = old_run, old_have

    @unittest.skipUnless(HAVE_FFMPEG, "ffmpeg missing")
    def test_unreadable_asset_is_skipped_not_fatal(self):
        d = tempfile.mkdtemp(dir=T["tmp"])
        broken = os.path.join(d, "broken.mp4")
        with open(broken, "wb") as f:
            f.write(b"not a video at all" * 10)
        good = os.path.join(T["tmp"], "broll.mp4")
        res = director.make_reel("Stop posting at 9pm", assets=[broken, good], duration=5, llm="none", render=False,
                                 out_dir=os.path.join(d, "out"), log=None)
        self.assertTrue(any("broken.mp4" in n and "could not be read" in n for n in res["notes"]), res["notes"])
        plan = json.loads(_read(res["plan"]))
        self.assertTrue(all((b["background"] or {}).get("asset") == good for b in plan["beats"]))

    def test_relative_music_and_voice_paths(self):
        d = tempfile.mkdtemp(dir=T["tmp"])
        self._wav(os.path.join(d, "song.wav"), 3.0)
        self._wav(os.path.join(d, "voice.wav"), 3.0)
        old = os.getcwd()
        os.chdir(d)                          # the CLI passes paths as typed, relative to where it runs
        try:
            res = director.make_reel("Stop posting at night", music="song.wav", voice="voice.wav", duration=5,
                                     llm="none", render=False, out_dir=os.path.join(d, "project"), log=None)
        finally:
            os.chdir(old)
        self.assertEqual(res["plan_errors"], [])
        spec = timeline.load(res["spec"])
        srcs = {a["role"]: os.path.realpath(a["src"]) for a in spec["audio"] if a.get("role") in ("music", "voice")}
        self.assertEqual(srcs, {"music": os.path.realpath(os.path.join(d, "song.wav")),     # macOS: /var -> /private/var
                                "voice": os.path.realpath(os.path.join(d, "voice.wav"))})

    def test_offline_plans_are_validated_too(self):
        res = director.make_reel("🔥🔥🔥", duration=6, llm="none", render=False, out_dir=tempfile.mkdtemp(dir=T["tmp"]),
                                 log=None)
        self.assertEqual(res["plan_errors"], [])
        self.assertTrue(json.loads(_read(res["plan"]))["beats"][0]["visual"]["params"]["text"])

    def test_claude_mode_without_cli_says_how_to_fix(self):
        old = llm.claude_path
        llm.claude_path = lambda: None
        try:
            with self.assertRaises(llm.LLMUnavailable):
                director.make_reel("x", duration=5, out_dir=os.path.join(T["tmp"], "reel4"), llm="claude",
                                   render=False, log=None)
            self.assertEqual(director.main(["--prompt", "x", "--duration", "5", "--llm", "claude", "--no-render",
                                            "--out-dir", os.path.join(T["tmp"], "reel5")]), 3)
        finally:
            llm.claude_path = old


if __name__ == "__main__":
    unittest.main()
