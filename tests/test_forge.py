import json
import os
import tempfile
import unittest

from _load import load

spoken = load("script-forge/scripts", "spoken")
scriptcheck = load("script-forge/scripts", "scriptcheck")
voiceprint = load("script-forge/scripts", "voiceprint")

FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def fixture(name):
    return open(os.path.join(FIX, name), encoding="utf-8").read()


class TestSpokenLines(unittest.TestCase):
    def test_cues_labels_and_speakers_are_not_speech(self):
        script = "HOOK (0:00-0:03)\nVO: I lost $300.\n[B-ROLL: my desk]\nON-SCREEN: $300\n0:04 - And nobody warned me.\nBEAT 2"
        self.assertEqual(spoken.spoken_lines(script), ["I lost $300.", "And nobody warned me."])


class TestScriptCheck(unittest.TestCase):
    def test_human_script_ships_and_ai_script_does_not(self):
        human = scriptcheck.score_script(fixture("script_human.md"), seconds=38, wpm=175)
        ai = scriptcheck.score_script(fixture("script_ai.md"), seconds=30, wpm=175)
        self.assertEqual(human["verdict"], "SHIP")
        self.assertEqual(ai["verdict"], "REWRITE")
        self.assertGreater(human["overall"] - ai["overall"], 30)

    def test_catches_the_classic_tells(self):
        r = scriptcheck.score_script("Hey guys, welcome back. It's not about posting more, it's about posting smarter. "
                                     "Here's the thing: consistency is key. Remember, success is a journey.")
        fixes = " ".join(f["fix"] for f in r["flags"])
        self.assertIn("Throat-clearing", fixes)
        self.assertIn("not X, it's Y", fixes)
        self.assertIn("drumroll", fixes)
        self.assertIn("No moral", fixes)

    def test_length_against_target(self):
        r = scriptcheck.score_script("word " * 175, seconds=30, wpm=175)
        self.assertEqual(r["seconds"], 60.0)
        self.assertTrue(any(f["check"] == "length" for f in r["flags"]))

    def test_voice_distance(self):
        voice = {"mean_words": 6.0, "per_100_words": {"contractions": 6.0, "you": 1.0, "i": 9.0, "and_but_so_openers": 4.0},
                 "signature_phrases": ["no joke"]}
        r = scriptcheck.score_script(fixture("script_human.md"), voice=voice)
        self.assertIsNotNone(r["checks"]["VOICE"]["score"])
        self.assertTrue(any("their own phrases" in f["fix"] for f in r["flags"]))


class TestVoiceprint(unittest.TestCase):
    def test_profile_from_srt(self):
        srt = ("1\n00:00:00,000 --> 00:00:03,000\nSo I tried this for a week and it's honestly wild.\n\n"
               "2\n00:00:03,200 --> 00:00:06,000\nNo joke. I posted 30 times.\n\n"
               "3\n00:00:06,200 --> 00:00:09,000\nAnd you know what? No joke, it worked.\n")
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "a.srt")
            open(p, "w").write(srt)
            v = voiceprint.build([p])
        self.assertGreater(v["words"], 15)
        self.assertIn("no joke", v["signature_phrases"])
        self.assertGreater(v["per_100_words"]["contractions"], 0)
        self.assertTrue(v["examples"])
        self.assertIn("Voice profile", voiceprint.render(v))


class TestWindowsPipedStdout(unittest.TestCase):
    """Windows writes piped stdout (Claude Code's) as cp1252: the reports must print, as UTF-8."""

    def test_scriptcheck_and_voiceprint_print_any_script(self):
        import subprocess
        import sys
        here = os.path.dirname(os.path.abspath(__file__))
        scripts = os.path.join(os.path.dirname(here), ".claude", "skills", "script-forge", "scripts")
        env = dict(os.environ, PYTHONIOENCODING="cp1252")
        env.pop("PYTHONUTF8", None)
        tmp = tempfile.mkdtemp()
        script = os.path.join(tmp, "hi.txt")
        with open(script, "w", encoding="utf-8") as f:
            f.write("घर पर पास्ता \U0001F525 → easy.\n"
                    "Why does nobody tell you this?\n")
        r = subprocess.run([sys.executable, os.path.join(scripts, "scriptcheck.py"), script], capture_output=True,
                           env=env, timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr.decode("utf-8", "replace")[-400:])
        self.assertIn("\U0001F525", r.stdout.decode("utf-8"))
        srt = os.path.join(tmp, "talk.srt")
        with open(srt, "w", encoding="utf-8") as f:
            f.write("1\n00:00:00,000 --> 00:00:02,000\nSo here’s the thing \U0001F525 nobody says\n")
        r = subprocess.run([sys.executable, os.path.join(scripts, "voiceprint.py"), srt, "--out",
                            os.path.join(tmp, "voice.json")], capture_output=True, env=env, timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr.decode("utf-8", "replace")[-400:])


if __name__ == "__main__":
    unittest.main()
