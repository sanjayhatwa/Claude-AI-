#!/usr/bin/env python3
"""
trends.py - find what is breaking out in a niche right now, from public
YouTube Shorts data, and rank it by how far each video beat its own channel.

Short-form trends move across platforms together: the hook, format or sound
that is breaking out on Reels and TikTok is almost always on Shorts the same
week, and Shorts is the one place where view counts are public and no login
is involved. So this is the cheap, legal radar. Instagram and TikTok are then
checked by hand (see SKILL.md) to confirm.

Three steps, each one usable on its own:

  search   search a few niche queries, keep recent short videos
  rank     fetch each channel's recent median and rank by outlier multiple
  hooks    pull the spoken first ~3 seconds of the top videos from captions

Usage
  python3 trends.py search "horror game" "indie game devlog" --out cand.json
  python3 trends.py rank cand.json --out ranked.json --top 25
  python3 trends.py hooks ranked.json --top 10 --out ranked.json
  python3 trends.py all "horror game" "indie game devlog" --dir workspace/trends

Output of `rank` is also valid input for yt-viral's swipe.py, and
`--tsv` writes the account/followers/median/views/hook file that
ig-viral's swipe.py reads.

Read, do not scrape: a handful of queries, a few dozen channels, at a polite
pace. No login, no cookies, no account of the user's is touched.
"""

import argparse
import json
import os
import statistics
import subprocess
import sys
import time

SHORT_MAX_SECONDS = 180
MIN_CHANNEL_SAMPLES = 4
PAUSE_SECONDS = 1.5


def ytdlp(args, timeout=120):
    cmd = [sys.executable, "-m", "yt_dlp", "--quiet", "--no-warnings"] + args
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           encoding="utf-8", errors="replace")
    except FileNotFoundError:
        sys.exit("yt-dlp is not installed: python3 -m pip install yt-dlp")
    except subprocess.TimeoutExpired:
        return None
    if r.returncode != 0 or not r.stdout.strip():
        msg = (r.stderr or "").strip().splitlines()
        if msg:
            print("  yt-dlp: " + msg[-1][:200], file=sys.stderr)
        return None
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        return None


def entry_row(e, query=None):
    vid = e.get("id")
    return {
        "id": vid,
        "title": e.get("title") or "",
        "views": e.get("view_count"),
        "duration": e.get("duration"),
        "channel": e.get("channel") or e.get("uploader") or "",
        "channel_id": e.get("channel_id") or "",
        "channel_url": e.get("channel_url") or e.get("uploader_url") or "",
        "url": "https://www.youtube.com/shorts/%s" % vid if vid else e.get("url"),
        "query": query,
    }


def search(queries, per_query=40, newest=True):
    rows, seen = [], set()
    for q in queries:
        # ytsearchdate = newest first, which is what "trending now" means
        prefix = "ytsearchdate" if newest else "ytsearch"
        print("search: %s" % q, file=sys.stderr)
        d = ytdlp(["--flat-playlist", "-J", "%s%d:%s #shorts" % (prefix, per_query, q)])
        for e in (d or {}).get("entries") or []:
            row = entry_row(e, q)
            if not row["id"] or row["id"] in seen:
                continue
            if row["duration"] and row["duration"] > SHORT_MAX_SECONDS:
                continue
            seen.add(row["id"])
            rows.append(row)
        time.sleep(PAUSE_SECONDS)
    return rows


def channel_median(channel_url, sample=20):
    if not channel_url:
        return None, 0
    d = ytdlp(["--flat-playlist", "-J", "--playlist-end", str(sample),
               channel_url.rstrip("/") + "/shorts"])
    views = [e.get("view_count") for e in (d or {}).get("entries") or []]
    views = [v for v in views if isinstance(v, int)]
    if len(views) < MIN_CHANNEL_SAMPLES:
        return None, len(views)
    return statistics.median(views), len(views)


def rank(rows, medians=None, top=None, min_views=1000):
    """Attach `median` and `multiple` to each row and sort by multiple.

    `medians` maps channel_url -> (median, samples); missing ones are fetched.
    Rows whose channel has too few shorts for a median are kept but unranked,
    because a guess at a baseline is worse than saying there is none.
    """
    medians = dict(medians or {})
    for row in rows:
        key = row.get("channel_url")
        if key not in medians:
            print("median: %s" % (row.get("channel") or key), file=sys.stderr)
            medians[key] = channel_median(key)
            time.sleep(PAUSE_SECONDS)
        med, n = medians[key]
        row["median"] = med
        row["samples"] = n
        views = row.get("views")
        row["multiple"] = round(views / med, 2) if med and views else None
    ranked = [r for r in rows if r["multiple"] is not None and (r["views"] or 0) >= min_views]
    ranked.sort(key=lambda r: r["multiple"], reverse=True)
    unranked = [r for r in rows if r not in ranked]
    if top:
        ranked = ranked[:top]
    return ranked, unranked


def spoken_hook(video_id, seconds=3.0):
    """First ~3 s of auto-captions: the hook as said, not as written."""
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, "hook")
        subprocess.run([sys.executable, "-m", "yt_dlp", "--quiet", "--no-warnings",
                        "--skip-download", "--write-auto-subs", "--sub-langs", "en.*",
                        "--sub-format", "json3", "-o", out,
                        "https://www.youtube.com/watch?v=%s" % video_id],
                       capture_output=True, timeout=120)
        files = [f for f in os.listdir(tmp) if f.endswith(".json3")]
        if not files:
            return None
        data = json.load(open(os.path.join(tmp, files[0]), encoding="utf-8"))
    return hook_from_json3(data, seconds)


def hook_from_json3(data, seconds=3.0):
    words = []
    for ev in data.get("events") or []:
        start = ev.get("tStartMs", 0)
        for seg in ev.get("segs") or []:
            t = (start + seg.get("tOffsetMs", 0)) / 1000.0
            w = (seg.get("utf8") or "").strip()
            if w and t < seconds:
                words.append(w)
    return " ".join(words) or None


def to_tsv(rows):
    lines = ["account\tfollowers\tmedian\tviews\thook"]
    for r in rows:
        hook = (r.get("hook") or r.get("title") or "").replace("\t", " ").replace("\n", " ")
        lines.append("%s\t\t%s\t%s\t%s" % (r.get("channel") or "", int(r["median"]) if r.get("median") else "",
                                          r.get("views") or "", hook))
    return "\n".join(lines) + "\n"


def report(ranked, unranked):
    out = ["TRENDS  ·  %d ranked  ·  %d without a baseline" % (len(ranked), len(unranked)), ""]
    hot = [r for r in ranked if r["multiple"] >= 3]
    out.append("OUTLIERS (3x or more over the channel's own median): %d" % len(hot))
    for r in ranked:
        out.append("  %6.1fx  %9s views  %-24s  %s" % (
            r["multiple"], "{:,}".format(r["views"]), (r["channel"] or "")[:24], r.get("hook") or r["title"]))
    if len(ranked) < 15:
        out += ["", "Small sample (%d). Treat this as a lead, not a finding." % len(ranked)]
    return "\n".join(out)


def save(obj, path):
    if not path:
        print(json.dumps(obj, indent=2, ensure_ascii=False))
        return
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        if isinstance(obj, str):
            f.write(obj)
        else:
            json.dump(obj, f, indent=2, ensure_ascii=False)
    print("wrote %s" % path, file=sys.stderr)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("search")
    s.add_argument("queries", nargs="+")
    s.add_argument("--per-query", type=int, default=40)
    s.add_argument("--relevance", action="store_true", help="relevance order instead of newest first")
    s.add_argument("--out")

    r = sub.add_parser("rank")
    r.add_argument("candidates")
    r.add_argument("--top", type=int, default=25)
    r.add_argument("--min-views", type=int, default=1000)
    r.add_argument("--out")
    r.add_argument("--tsv", help="also write ig-viral swipe.py input here")

    h = sub.add_parser("hooks")
    h.add_argument("ranked")
    h.add_argument("--top", type=int, default=10)
    h.add_argument("--out")

    a = sub.add_parser("all")
    a.add_argument("queries", nargs="+")
    a.add_argument("--dir", default="workspace/trends")
    a.add_argument("--top", type=int, default=25)
    a.add_argument("--hooks", type=int, default=10)

    args = ap.parse_args()

    if args.cmd == "search":
        save(search(args.queries, args.per_query, not args.relevance), args.out)

    elif args.cmd == "rank":
        rows = json.load(open(args.candidates, encoding="utf-8"))
        ranked, unranked = rank(rows, top=args.top, min_views=args.min_views)
        save(ranked, args.out)
        if args.tsv:
            save(to_tsv(ranked), args.tsv)
        print(report(ranked, unranked), file=sys.stderr)

    elif args.cmd == "hooks":
        rows = json.load(open(args.ranked, encoding="utf-8"))
        for row in rows[:args.top]:
            row["hook"] = spoken_hook(row["id"]) or row.get("hook")
            time.sleep(PAUSE_SECONDS)
        save(rows, args.out or args.ranked)

    elif args.cmd == "all":
        os.makedirs(args.dir, exist_ok=True)
        rows = search(args.queries)
        save(rows, os.path.join(args.dir, "candidates.json"))
        if not rows:
            sys.exit("No results. Is YouTube reachable from this machine?")
        ranked, unranked = rank(rows, top=args.top)
        for row in ranked[:args.hooks]:
            row["hook"] = spoken_hook(row["id"])
            time.sleep(PAUSE_SECONDS)
        save(ranked, os.path.join(args.dir, "ranked.json"))
        save(to_tsv(ranked), os.path.join(args.dir, "swipe-input.tsv"))
        text = report(ranked, unranked)
        save(text + "\n", os.path.join(args.dir, "report.txt"))
        print(text)


if __name__ == "__main__":
    main()
