#!/usr/bin/env python3
"""
radar.py - give it a niche, get back every trend worth riding this week.

    python3 radar.py "horror games" --geo IN --subreddits horrorgaming,indiegaming \
        --dir workspace/trends/2026-10-17

What it does, in order:

  1. EXPAND   turns the niche into ~16 searches across formats and intents
              (mistakes, POV, storytime, tier list, vs, explained...) plus what
              people are typing right now (YouTube autocomplete)
  2. COLLECT  YouTube Shorts (newest and top), YouTube long-form, Reddit's top
              posts of the week, Google Trends' daily trending searches
  3. ENRICH   upload date, likes, comments and channel baseline for the best
              candidates: views/day velocity, engagement rate, outlier multiple
  4. CLUSTER  groups videos that share a topic or format into TRENDS, each with
              its creators, median multiple, age and stage (rising / peaking /
              saturated)
  5. DIVERSIFY  ranks trends but caps any one format, so the slate is varied
  6. GAPS     searches people make that few outlier videos answer yet

Writes radar.json (everything), RADAR.md (the readable brief) and
swipe-input.tsv (for ig-viral/swipe.py). A source that could not be reached
(YouTube, autocomplete, Reddit, Google Trends) is named at the top of
RADAR.md and recorded in radar.json's "sources", so missing data never reads
as "nothing trending".

Read, do not scrape: ~20 searches, a few dozen channel and video lookups and
a handful of Reddit and Trends requests, spaced out. No login anywhere.
TikTok and Instagram are checked by hand afterwards (see SKILL.md).
"""

import argparse
import datetime as dt
import json
import math
import os
import re
import statistics
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import trends  # noqa: E402

UA = "Mozilla/5.0 (viral-hyper-agent research; low volume)"
PAUSE = 1.2

# How each source answered this run: "ok", "partial" (some requests failed) or "unreachable" (all failed).
# Written to radar.json as "sources" and named at the top of RADAR.md, because an empty Reddit or Trends
# list otherwise looks the same as "nothing is trending there".
SOURCES = {}
SOURCE_NAMES = {"youtube": "YouTube", "autocomplete": "YouTube autocomplete", "reddit": "Reddit",
                "google_trends": "Google Trends"}


def mark(src, ok):
    """Record one request to a source (see SOURCES)."""
    now = "ok" if ok else "unreachable"
    was = SOURCES.get(src)
    SOURCES[src] = now if was in (None, now) else "partial"

MODIFIERS = ["", "mistakes", "pov", "storytime", "tips", "explained", "vs", "tier list",
             "day in the life", "before and after", "facts", "challenge", "myths", "reaction",
             "beginner", "secret"]

FORMATS = [
    ("pov", r"\bpov\b|\bwhen you\b|\bme when\b|\bthat one\b"),
    ("challenge", r"\bchallenge\b|\bi tried\b|\b(?:spent |survived )?\d+ (?:hours|days)\b|\bfor \d+ days\b"),
    ("storytime", r"\bstory ?time\b|\bstorytime\b|\bi (?:quit|lost|got|tried|spent|made|almost|finally)\b|\bthe time i\b|\bwhat happened\b"),
    ("tutorial", r"\bhow to\b|\btutorial\b|\bstep by step\b|\bguide\b|\blearn\b|\bin \d+ (?:seconds|minutes|steps)\b"),
    ("list", r"\b(?:top )?\d+ (?:[a-z']+ ){0,3}(?:things|ways|tips|reasons|games|signs|mistakes|habits|tools|facts)\b|\btier ?list\b|\branking\b|\branked\b"),
    ("mistakes-myths", r"\bmistakes?\b|\bmyths?\b|\bstop doing\b|\bdon'?t\b|\bwrong\b|\bnever\b|\bavoid\b"),
    ("versus", r"\bvs\.?\b|\bversus\b|\bcompared\b|\bwhich is better\b"),
    ("day-in-life", r"\bday in (?:the|my) life\b|\bditl\b|\bvlog\b|\bmorning routine\b|\bnight routine\b"),
    ("before-after", r"\bbefore (?:and|&|vs) after\b|\btransformation\b|\bglow ?up\b|\bthen vs now\b"),
    ("reaction", r"\breact(?:s|ion|ing)?\b|\bresponding to\b|\bduet\b|\bstitch\b"),
    ("explainer", r"\bexplained\b|\bwhy (?:does|do|is|are|you)\b|\bthe (?:truth|reason|science)\b|\bwhat is\b|\bfacts?\b|\bdid you know\b"),
    ("review", r"\breview\b|\bworth it\b|\bhonest\b|\bunboxing\b|\bfirst impressions?\b"),
    ("hot-take", r"\bunpopular opinion\b|\bhot take\b|\boverrated\b|\bunderrated\b|\bnobody talks about\b|\brant\b"),
    ("skit", r"\bskit\b|\bpart \d+\b|\bep(?:isode)? ?\d+\b|\bseries\b"),
    ("satisfying", r"\bsatisfying\b|\basmr\b|\boddly\b|\brelaxing\b"),
    ("behind-scenes", r"\bbehind the scenes\b|\bbts\b|\bhow i made\b|\bmaking of\b|\bdevlog\b"),
]

STOP = set("""a an the and or but so of to in on for with at by from is are was were be been it its this that
these those i me my you your we our they their he she his her them us as if then than not no yes do does did
how what why when who which just get got make made into out up down over more most very can will would should
could about after before all any some one two new best top vs part video shorts short reel reels tiktok youtube
amp""".split())


# --------------------------------------------------------------------------- 1. expand

def expand(niche, extra=None, autocomplete=None):
    qs = []
    for m in MODIFIERS:
        q = ("%s %s" % (niche, m)).strip()
        if q not in qs:
            qs.append(q)
    for q in (extra or []) + (autocomplete or []):
        if q and q not in qs:
            qs.append(q)
    return qs


def http_json(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def http_text(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def parse_autocomplete(payload):
    if isinstance(payload, list) and len(payload) > 1 and isinstance(payload[1], list):
        return [s if isinstance(s, str) else s[0] for s in payload[1]]
    return []


def autocomplete(niche, stems=("", "how to", "why", "best", "is"), limit=20):
    out = []
    for stem in stems:
        q = ("%s %s" % (stem, niche)).strip() if stem else niche
        url = "https://suggestqueries.google.com/complete/search?client=firefox&ds=yt&q=" + urllib.parse.quote(q)
        try:
            for s in parse_autocomplete(http_json(url)):
                if s not in out:
                    out.append(s)
            mark("autocomplete", True)
        except Exception as e:
            warn("autocomplete", e)
            mark("autocomplete", False)
            break
        time.sleep(PAUSE)
    return out[:limit]


# --------------------------------------------------------------------------- 2. collect

def parse_reddit(payload):
    rows = []
    for ch in ((payload or {}).get("data") or {}).get("children") or []:
        d = ch.get("data") or {}
        rows.append({"title": d.get("title", ""), "score": d.get("score", 0), "comments": d.get("num_comments", 0),
                     "sub": d.get("subreddit", ""), "url": "https://www.reddit.com" + d.get("permalink", ""),
                     "created": d.get("created_utc")})
    return rows


def reddit(niche, subs, limit=25):
    urls = ["https://www.reddit.com/search.json?sort=top&t=week&limit=%d&q=%s" % (limit, urllib.parse.quote(niche))]
    urls += ["https://www.reddit.com/r/%s/top.json?t=week&limit=%d" % (s.strip(), limit) for s in subs if s.strip()]
    rows = []
    for u in urls:
        try:
            rows += parse_reddit(http_json(u))
            mark("reddit", True)
        except Exception as e:
            warn("reddit", e)
            mark("reddit", False)
        time.sleep(PAUSE * 1.5)
    seen, out = set(), []
    for r in sorted(rows, key=lambda r: -(r["score"] + 2 * r["comments"])):
        if r["url"] not in seen:
            seen.add(r["url"])
            out.append(r)
    return out[:40]


def parse_trends_rss(xml_text):
    out = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return out
    ns = {"ht": "https://trends.google.com/trending/rss"}
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        traffic = item.findtext("ht:approx_traffic", default="", namespaces=ns) or ""
        news = [n.findtext("ht:news_item_title", default="", namespaces=ns) for n in item.findall("ht:news_item", ns)]
        if title:
            out.append({"query": title, "traffic": traffic.strip(), "news": [n for n in news if n][:2]})
    return out


def google_trends(geo="US"):
    try:
        out = parse_trends_rss(http_text("https://trends.google.com/trending/rss?geo=%s" % geo))
    except Exception as e:
        warn("google trends", e)
        mark("google_trends", False)
        return []
    mark("google_trends", True)
    return out


def youtube_longform(queries, per_query=15):
    rows = []
    for q in queries:
        d = trends.ytdlp(["--flat-playlist", "-J", "ytsearch%d:%s" % (per_query, q)])
        for e in (d or {}).get("entries") or []:
            r = trends.entry_row(e, q)
            if r["id"] and (r["duration"] or 0) > trends.SHORT_MAX_SECONDS:
                r["url"] = "https://www.youtube.com/watch?v=%s" % r["id"]
                r["format_len"] = "long"
                rows.append(r)
        time.sleep(PAUSE)
    return rows


# --------------------------------------------------------------------------- 3. enrich

def enrich(rows, top=30):
    """Upload date, likes, comments for the most promising rows (one lookup each)."""
    for r in sorted(rows, key=lambda r: -(r.get("multiple") or 0))[:top]:
        url = r["url"] if r["url"].startswith("http") else "https://www.youtube.com/watch?v=%s" % r["id"]
        d = trends.ytdlp(["-j", "--skip-download", "--no-playlist", url], timeout=90)
        if d:
            r["upload_date"] = d.get("upload_date")
            r["likes"] = d.get("like_count")
            r["comments"] = d.get("comment_count")
            r["followers"] = d.get("channel_follower_count")
            r["tags"] = (d.get("tags") or [])[:12]
            r["views"] = d.get("view_count") or r.get("views")
        time.sleep(PAUSE)
    return rows


def age_days(upload_date, today=None):
    if not upload_date:
        return None
    try:
        d = dt.datetime.strptime(str(upload_date), "%Y%m%d").date()
    except ValueError:
        return None
    today = today or dt.date.today()
    return max(0.5, (today - d).days or 0.5)


def add_metrics(r, today=None):
    a = age_days(r.get("upload_date"), today)
    r["age_days"] = a
    v = r.get("views") or 0
    r["velocity"] = round(v / a) if a else None
    if v and (r.get("likes") is not None or r.get("comments") is not None):
        r["engagement"] = round(((r.get("likes") or 0) + 2 * (r.get("comments") or 0)) / v * 100, 2)
    return r


# --------------------------------------------------------------------------- 4. cluster

def classify_format(text):
    t = (text or "").lower()
    hits = [name for name, rx in FORMATS if re.search(rx, t)]
    return hits[0] if hits else "other"


def keywords(text):
    toks = re.findall(r"[a-z0-9']+", (text or "").lower())
    stem = lambda t: t[:-1] if len(t) > 4 and t.endswith("s") and not t.endswith("ss") else t
    return {stem(t) for t in toks if t not in STOP and len(t) > 2 and not t.isdigit()}


def jaccard(a, b):
    return len(a & b) / len(a | b) if a and b else 0.0


def cluster(rows, niche_words=(), threshold=0.25):
    """Greedy single-pass clustering on topic keywords, split by format."""
    niche = set(niche_words)
    clusters = []
    for r in rows:
        kw = keywords("%s %s" % (r.get("title", ""), r.get("hook") or "")) - niche
        r["format"] = r.get("format") or classify_format("%s %s" % (r.get("title", ""), r.get("hook") or ""))
        best, best_s = None, 0
        for c in clusters:
            s = jaccard(kw, c["_kw"])
            if c["format"] == r["format"]:
                s += 0.12
            if s > best_s:
                best, best_s = c, s
        if best and best_s >= threshold:
            best["items"].append(r)
            best["_kw"] |= kw
            best["_count"].update(kw)
        else:
            import collections
            clusters.append({"format": r["format"], "items": [r], "_kw": set(kw), "_count": collections.Counter(kw)})
    out = []
    for c in clusters:
        out.append(summarize_cluster(c))
    return out


def summarize_cluster(c):
    items = c["items"]
    mults = [i["multiple"] for i in items if i.get("multiple")]
    ages = [i["age_days"] for i in items if i.get("age_days")]
    vel = [i["velocity"] for i in items if i.get("velocity")]
    chans = {i.get("channel") for i in items if i.get("channel")}
    med_m = statistics.median(mults) if mults else None
    med_age = statistics.median(ages) if ages else None
    label_words = [w for w, _ in c["_count"].most_common(4)]
    trend = {
        "label": " ".join(label_words) or c["format"],
        "format": c["format"],
        "videos": len(items),
        "creators": len(chans),
        "median_multiple": round(med_m, 1) if med_m else None,
        "best_multiple": round(max(mults), 1) if mults else None,
        "median_age_days": round(med_age, 1) if med_age else None,
        "median_velocity": round(statistics.median(vel)) if vel else None,
        "examples": sorted(items, key=lambda i: -(i.get("multiple") or 0))[:4],
    }
    trend["stage"] = stage(trend)
    trend["score"] = trend_score(trend)
    return trend


def stage(t):
    m, age, n = t["median_multiple"] or 0, t["median_age_days"], t["creators"]
    if n >= 6 and m < 2:
        return "saturated"
    if age is not None and age <= 14 and m >= 2:
        return "rising"
    if m >= 2:
        return "peaking"
    return "weak"


def trend_score(t):
    m = t["median_multiple"] or 0
    if m <= 0:
        return 0.0
    spread = min(1.0, t["creators"] / 4.0)
    age = t["median_age_days"]
    fresh = 1.0 if age is None else 1.0 if age <= 7 else 0.8 if age <= 14 else 0.5 if age <= 30 else 0.25
    stage_k = {"rising": 1.2, "peaking": 1.0, "weak": 0.5, "saturated": 0.4}[t["stage"]]
    return round(math.log2(1 + m) * (0.4 + 0.6 * spread) * fresh * stage_k, 3)


# --------------------------------------------------------------------------- 5. diversify, 6. gaps

def diversify(clusters, n=10, per_format=2):
    picked, count = [], {}
    for c in sorted(clusters, key=lambda c: -c["score"]):
        if c["score"] <= 0:
            continue
        if count.get(c["format"], 0) >= per_format:
            continue
        picked.append(c)
        count[c["format"]] = count.get(c["format"], 0) + 1
        if len(picked) >= n:
            break
    return picked


def gaps(searches, rows, min_hits=1):
    """Searches people type that few outlier videos answer."""
    out = []
    hot = [keywords(r.get("title", "")) for r in rows if (r.get("multiple") or 0) >= 2]
    for q in searches:
        kw = keywords(q)
        if not kw:
            continue
        hits = sum(1 for h in hot if len(kw & h) >= max(1, len(kw) - 1))
        if hits < min_hits + 1:
            out.append({"search": q, "outlier_videos": hits})
    return out[:12]


def warn(src, e):
    print("  %s unavailable: %s" % (src, str(e)[:120]), file=sys.stderr)


# --------------------------------------------------------------------------- report

def render(niche, picked, all_clusters, ac, red, gt, gp, n_rows, reachable, sources=None):
    L = ["# Trend radar: %s" % niche, "", "Generated %s. %d videos ranked into %d clusters." % (
        dt.date.today().isoformat(), n_rows, len(all_clusters)), ""]
    if not reachable:
        L += ["**YouTube was not reachable from this machine, so there is no ranked video evidence below.**", ""]
    others = [(SOURCE_NAMES[k], (sources or {}).get(k)) for k in SOURCE_NAMES if k != "youtube"]
    down = [name for name, v in others if v == "unreachable"]
    part = [name for name, v in others if v == "partial"]
    if down:
        L += ["**Not reachable from this machine: %s.** Nothing was read from %s, so a missing section below "
              "means no data, not that nothing is trending there." % (", ".join(down), "it" if len(down) == 1 else "them"), ""]
    if part:
        L += ["**Only partly read: %s** (some requests failed; the run's messages say which)." % ", ".join(part), ""]
    L += ["## Trends to ride (varied by format)", ""]
    if not picked:
        L += ["None with enough evidence. Widen the niche, add accounts, or run the manual TikTok/Instagram pass.", ""]
    for i, t in enumerate(picked, 1):
        L += ["### %d. %s  [%s, %s]" % (i, t["label"], t["format"], t["stage"].upper()), "",
              "- Evidence: %d videos from %d creators, median %sx their own baseline (best %sx), median age %s days, %s views/day" % (
                  t["videos"], t["creators"], t["median_multiple"], t["best_multiple"], t["median_age_days"], t["median_velocity"]),
              "- Examples:"]
        for e in t["examples"][:3]:
            L.append("  - %sx  %s  (%s)  %s%s" % (e.get("multiple"), e.get("title", "")[:80], e.get("channel", ""), e.get("url", ""),
                                               "  hook: \"%s\"" % e["hook"] if e.get("hook") else ""))
        L += [""]
    mix = {}
    for c in all_clusters:
        if c["score"] > 0:
            mix[c["format"]] = mix.get(c["format"], 0) + c["videos"]
    if mix:
        L += ["## Format mix among outliers", ""]
        L += ["- %s: %d videos" % (k, v) for k, v in sorted(mix.items(), key=lambda x: -x[1])]
        L += [""]
    dying = [c for c in all_clusters if c["stage"] == "saturated"]
    if dying:
        L += ["## Saturated (everyone is doing it, it no longer over-performs)", ""]
        L += ["- %s [%s]: %d creators, median %sx" % (c["label"], c["format"], c["creators"], c["median_multiple"]) for c in dying[:6]]
        L += [""]
    if gp:
        L += ["## Content gaps (people search it, few outlier videos answer it)", ""]
        L += ["- \"%s\" (%d outlier videos)" % (g["search"], g["outlier_videos"]) for g in gp]
        L += [""]
    if ac:
        L += ["## What people are typing (YouTube autocomplete)", "", ", ".join(ac[:20]), ""]
    if red:
        L += ["## What the community is talking about (Reddit, top this week)", ""]
        L += ["- %s  (r/%s, %d upvotes, %d comments)" % (r["title"][:100], r["sub"], r["score"], r["comments"]) for r in red[:10]]
        L += [""]
    if gt:
        L += ["## Trending searches today (Google Trends) - newsjack only if it fits", ""]
        L += ["- %s %s" % (g["query"], "(%s)" % g["traffic"] if g["traffic"] else "") for g in gt[:12]]
        L += [""]
    L += ["## Not covered by this run", "",
          "TikTok and Instagram view counts need a person's browser: run the manual pass in trend-hunt/SKILL.md step 2.", ""]
    return "\n".join(L)


def main():
    trends.utf8_stdio()   # RADAR.md is printed: emoji and Hindi titles must not stop it on Windows
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("niche")
    ap.add_argument("--dir", default="workspace/trends/%s" % dt.date.today().isoformat())
    ap.add_argument("--queries", help="extra search phrases, comma separated")
    ap.add_argument("--subreddits", default="", help="comma separated, without r/")
    ap.add_argument("--geo", default="US", help="country code for Google Trends")
    ap.add_argument("--per-query", type=int, default=25)
    ap.add_argument("--enrich", type=int, default=30, help="how many top videos to look up in detail")
    ap.add_argument("--hooks", type=int, default=12, help="how many top videos to pull spoken hooks for")
    ap.add_argument("--no-long", action="store_true", help="skip long-form YouTube")
    a = ap.parse_args()
    os.makedirs(a.dir, exist_ok=True)
    SOURCES.clear()

    print("1/6 expand", file=sys.stderr)
    ac = autocomplete(a.niche)
    extra = [q.strip() for q in (a.queries or "").split(",") if q.strip()]
    queries = expand(a.niche, extra, ac[:6])

    print("2/6 collect: %d searches" % len(queries), file=sys.stderr)
    shorts = trends.search(queries, per_query=a.per_query, newest=True)
    shorts += [r for r in trends.search(queries[:6], per_query=a.per_query, newest=False)
               if r["id"] not in {s["id"] for s in shorts}]
    long_rows = [] if a.no_long else youtube_longform(queries[:5])
    red = reddit(a.niche, a.subreddits.split(","))
    gt = google_trends(a.geo)
    rows = shorts + long_rows
    reachable = bool(rows)
    SOURCES["youtube"] = "ok" if reachable else "unreachable"

    print("3/6 enrich: baselines for %d videos" % len(rows), file=sys.stderr)
    ranked, _ = trends.rank(rows, top=None) if rows else ([], [])
    enrich(ranked, top=a.enrich)
    for r in ranked:
        add_metrics(r)
    for r in ranked[:a.hooks]:
        r["hook"] = trends.spoken_hook(r["id"]) or r.get("hook")
        time.sleep(PAUSE)

    print("4/6 cluster", file=sys.stderr)
    clusters = cluster(ranked, keywords(a.niche))
    print("5/6 diversify", file=sys.stderr)
    picked = diversify(clusters)
    print("6/6 gaps", file=sys.stderr)
    gp = gaps(ac + [r["title"] for r in red[:15]], ranked)

    data = {"niche": a.niche, "date": dt.date.today().isoformat(), "queries": queries, "autocomplete": ac,
            "reddit": red, "google_trends": gt, "videos": ranked, "trends": picked,
            "all_clusters": [dict(c, examples=[e.get("id") for e in c["examples"]]) for c in clusters],
            "gaps": gp, "youtube_reachable": reachable, "sources": dict(SOURCES)}
    trends.save(data, os.path.join(a.dir, "radar.json"))
    trends.save(trends.to_tsv(ranked[:60]), os.path.join(a.dir, "swipe-input.tsv"))
    md = render(a.niche, picked, clusters, ac, red, gt, gp, len(ranked), reachable, SOURCES)
    trends.save(md + "\n", os.path.join(a.dir, "RADAR.md"))
    print(md)


if __name__ == "__main__":
    main()
