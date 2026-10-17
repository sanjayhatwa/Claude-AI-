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


if __name__ == "__main__":
    unittest.main()
