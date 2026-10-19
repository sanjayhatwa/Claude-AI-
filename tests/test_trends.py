import os
import subprocess
import sys
import tempfile
import unittest

from _load import load, SKILLS_DIR

trends = load("trend-hunt/scripts", "trends")


def row(vid, channel, views):
    return {"id": vid, "title": "title " + vid, "views": views, "channel": channel,
            "channel_url": "https://www.youtube.com/@" + channel}


class TestRank(unittest.TestCase):
    def setUp(self):
        self.rows = [row("a", "big", 400000), row("b", "small", 400000),
                     row("c", "small", 9000), row("d", "thin", 50000)]
        self.medians = {"https://www.youtube.com/@big": (2000000, 20),
                        "https://www.youtube.com/@small": (10000, 20),
                        "https://www.youtube.com/@thin": (None, 2)}

    def test_ranks_by_multiple_not_raw_views(self):
        ranked, _ = trends.rank(self.rows, self.medians)
        self.assertEqual([r["id"] for r in ranked], ["b", "c", "a"])
        self.assertEqual(ranked[0]["multiple"], 40.0)

    def test_channel_without_baseline_is_unranked(self):
        ranked, unranked = trends.rank(self.rows, self.medians)
        self.assertNotIn("d", [r["id"] for r in ranked])
        self.assertEqual([r["id"] for r in unranked], ["d"])

    def test_min_views_and_top(self):
        ranked, _ = trends.rank(self.rows, self.medians, top=1, min_views=10000)
        self.assertEqual([r["id"] for r in ranked], ["b"])


class TestHooks(unittest.TestCase):
    def test_hook_is_first_three_seconds(self):
        data = {"events": [
            {"tStartMs": 0, "segs": [{"utf8": "nobody"}, {"utf8": " tells", "tOffsetMs": 400}]},
            {"tStartMs": 1200, "segs": [{"utf8": "you", "tOffsetMs": 0}]},
            {"tStartMs": 3500, "segs": [{"utf8": "later"}]},
        ]}
        self.assertEqual(trends.hook_from_json3(data), "nobody tells you")

    def test_no_captions(self):
        self.assertIsNone(trends.hook_from_json3({"events": []}))


class TestTsvFeedsIgViral(unittest.TestCase):
    def test_ig_viral_swipe_reads_our_tsv(self):
        rows = [dict(row("b", "small", 400000), median=10000, hook="nobody tells you your first reels flop"),
                dict(row("a", "big", 400000), median=2000000, hook="stop doing this")]
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "in.tsv")
            with open(path, "w", encoding="utf-8") as f:
                f.write(trends.to_tsv(rows))
            swipe = os.path.join(SKILLS_DIR, "ig-viral", "swipe.py")
            r = subprocess.run([sys.executable, swipe, path], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("40", r.stdout)


class TestWindowsPipedStdout(unittest.TestCase):
    def test_titles_in_any_script_print_through_a_cp1252_pipe(self):
        """radar.py and trends.py print titles (emoji, Hindi); Windows pipes stdout as cp1252, which cannot
        encode them, so utf8_stdio() switches stdout to UTF-8 (what Claude Code reads) before printing."""
        scripts = os.path.join(SKILLS_DIR, "trend-hunt", "scripts")
        env = dict(os.environ, PYTHONIOENCODING="cp1252")
        env.pop("PYTHONUTF8", None)
        code = ("import sys; sys.path.insert(0, %r); import trends; trends.utf8_stdio(); "
                "trends.save([{'title': '\\U0001F525 \\u0928\\u092e\\u0938\\u094d\\u0924\\u0947'}], None)" % scripts)
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, env=env, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr.decode("utf-8", "replace")[-400:])
        self.assertIn("\U0001F525 नमस्ते", r.stdout.decode("utf-8"))
        radar = open(os.path.join(scripts, "radar.py"), encoding="utf-8").read()
        self.assertIn("trends.utf8_stdio()", radar.split("def main():", 1)[1].split("\n", 3)[1])


if __name__ == "__main__":
    unittest.main()
