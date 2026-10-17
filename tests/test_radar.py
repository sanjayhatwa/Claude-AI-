import datetime as dt
import unittest

from _load import load

radar = load("trend-hunt/scripts", "radar")

TODAY = dt.date(2026, 10, 17)


def rows():
    data = [
        ("a", "POV: you finally beat the hardest horror game boss", "c1", 400000, 40, "20261012"),
        ("b", "pov you hear footsteps in the hardest horror game", "c2", 90000, 9, "20261010"),
        ("c", "5 horror game mistakes beginners make", "c3", 50000, 5, "20261001"),
        ("d", "horror game mistakes you make every time", "c4", 30000, 3, "20260920"),
        ("e", "I spent 100 days in a horror game", "c5", 20000, 1.2, "20260801"),
    ]
    out = []
    for i, t, c, v, m, d in data:
        out.append(radar.add_metrics({"id": i, "title": t, "channel": c, "views": v, "multiple": m, "upload_date": d}, TODAY))
    return out


class TestRadar(unittest.TestCase):
    def test_expand_covers_formats(self):
        q = radar.expand("horror games", ["jump scares"], ["horror games 2026"])
        self.assertIn("horror games pov", q)
        self.assertIn("horror games tier list", q)
        self.assertIn("jump scares", q)
        self.assertGreater(len(q), 15)

    def test_formats(self):
        self.assertEqual(radar.classify_format("POV: you hear footsteps"), "pov")
        self.assertEqual(radar.classify_format("I spent 100 days in a horror game"), "challenge")
        self.assertEqual(radar.classify_format("5 horror game mistakes"), "list")
        self.assertEqual(radar.classify_format("unpopular opinion: jump scares are lazy"), "hot-take")

    def test_velocity_and_age(self):
        r = rows()[0]
        self.assertEqual(r["age_days"], 5)
        self.assertEqual(r["velocity"], 80000)

    def test_clusters_stage_and_variety(self):
        cs = radar.cluster(rows(), radar.keywords("horror games"))
        pov = next(c for c in cs if c["format"] == "pov")
        self.assertEqual(pov["videos"], 2)
        self.assertEqual(pov["creators"], 2)
        self.assertEqual(pov["stage"], "rising")
        picked = radar.diversify(cs, per_format=1)
        self.assertEqual(len({c["format"] for c in picked}), len(picked))
        self.assertEqual(picked[0]["format"], "pov")

    def test_saturated(self):
        t = {"median_multiple": 1.3, "median_age_days": 20, "creators": 8}
        self.assertEqual(radar.stage(t), "saturated")

    def test_gaps(self):
        g = radar.gaps(["horror game mistakes", "scariest horror game ending explained"], rows())
        self.assertEqual([x["search"] for x in g], ["scariest horror game ending explained"])

    def test_parsers(self):
        self.assertEqual(radar.parse_autocomplete(["q", ["a", "b"]]), ["a", "b"])
        rss = ('<rss xmlns:ht="https://trends.google.com/trending/rss"><channel><item><title>diwali</title>'
               '<ht:approx_traffic>500K+</ht:approx_traffic></item></channel></rss>')
        self.assertEqual(radar.parse_trends_rss(rss)[0]["query"], "diwali")
        red = {"data": {"children": [{"data": {"title": "t", "score": 5, "num_comments": 2, "subreddit": "s", "permalink": "/r/s/1"}}]}}
        self.assertEqual(radar.parse_reddit(red)[0]["url"], "https://www.reddit.com/r/s/1")

    def test_render(self):
        cs = radar.cluster(rows(), radar.keywords("horror games"))
        md = radar.render("horror games", radar.diversify(cs), cs, ["horror games 2026"], [], [], [], 5, True)
        self.assertIn("Trends to ride", md)
        self.assertIn("RISING", md)


if __name__ == "__main__":
    unittest.main()
