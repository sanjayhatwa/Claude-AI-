#!/usr/bin/env python3
"""
voiceprint.py - learn how a real person talks, from things they actually said.

Feed it transcripts of the creator's own videos (best), their captions, or
voice notes transcribed. It measures sentence length and rhythm, how often
they contract, start with "and"/"so", say "you", use numbers and fillers,
their openers and the phrases they repeat, and pulls out real example lines.
script-forge writes TO this profile and scriptcheck.py scores against it.

Point it at viral creators' transcripts instead, and you get the register
that is working in the niche, which is the next best thing when the user has
no recordings yet.

    python3 voiceprint.py my_videos/*.txt --out workspace/voice.json
    python3 voiceprint.py workspace/dna/*/transcript.json --out workspace/niche_voice.json

Accepts .txt, .srt, .vtt, .json3 and extract.py's transcript.json.
"""

import argparse
import glob
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import spoken  # noqa: E402

EXTRACT = os.path.join(os.path.dirname(os.path.dirname(HERE)), "viral-extract", "scripts")
sys.path.insert(0, EXTRACT)


def read_any(path):
    """Return (plain text, words-with-timing or None)."""
    raw = open(path, encoding="utf-8", errors="replace").read()
    timed = None
    try:
        import extract
        if path.endswith(".json3") or (path.endswith(".json") and raw.lstrip().startswith("{")):
            timed = extract.parse_json3(json.loads(raw))
        elif path.endswith(".json"):
            timed = json.loads(raw)
        elif path.endswith((".srt", ".vtt")):
            timed = extract.parse_srt_vtt(raw)
    except Exception:
        timed = None
    if timed:
        return " ".join(w["w"] for w in timed), timed
    return raw, None


def punctuate_from_pauses(timed, pause=0.45):
    """Auto-captions have no full stops; a pause is where a sentence ended."""
    if any(re.search(r"[.!?]$", w["w"]) for w in timed[: max(10, len(timed) // 10)]):
        return " ".join(w["w"] for w in timed)
    out = []
    for i, w in enumerate(timed):
        gap = timed[i + 1]["s"] - w["e"] if i + 1 < len(timed) else 9
        out.append(w["w"] + ("." if gap >= pause else ""))
    return " ".join(out)


def example_lines(text, signature, n=12):
    sents = spoken.sentences(text)
    scored = []
    for s in sents:
        ws = spoken.words(s)
        if not 3 <= len(ws) <= 22:
            continue
        low = s.lower()
        score = 0
        score += 2 * sum(1 for g in signature if g in low)
        score += 1 if spoken.CONTRACTION_RE.search(s) else 0
        score += 1 if spoken.NUMBER_RE.search(s) else 0
        score += 1 if ws[0].lower() in ("and", "but", "so", "like", "okay") else 0
        score += 1 if len(ws) <= 7 else 0
        scored.append((score, s.strip()))
    seen, out = set(), []
    for _, s in sorted(scored, key=lambda x: -x[0]):
        k = s.lower()[:40]
        if k not in seen:
            seen.add(k)
            out.append(s)
        if len(out) >= n:
            break
    return out


def build(paths):
    texts, wpms = [], []
    for p in paths:
        text, timed = read_any(p)
        if timed:
            text = punctuate_from_pauses(timed)
            span = timed[-1]["e"] - timed[0]["s"]
            if span > 5:
                wpms.append(len(timed) / span * 60)
        texts.append(text)
    corpus = "\n".join(texts)
    stats = spoken.text_stats(corpus)
    stats["wpm"] = round(sum(wpms) / len(wpms)) if wpms else None
    stats["sources"] = [os.path.basename(p) for p in paths]
    stats["examples"] = example_lines(corpus, stats["signature_phrases"])
    return stats


def render(v):
    p = v["per_100_words"]
    L = ["# Voice profile", "",
         "From %d words across %d source(s)." % (v["words"], len(v["sources"])), "",
         "## How this person talks", "",
         "- Sentences average %s words (spread %s, burstiness %s); %d%% are 5 words or fewer, %d%% are 20+" % (
             v["mean_words"], v["stdev_words"], v["burstiness"], v["short_share"] * 100, v["long_share"] * 100),
         "- Pace: %s words per minute" % (v["wpm"] or "unknown (no timing in sources)"),
         "- Per 100 words: %s contractions, %s 'you', %s 'I', %s numbers, %s fillers, %s questions, %s and/but/so openers" % (
             p["contractions"], p["you"], p["i"], p["numbers"], p["fillers"], p["questions"], p["and_but_so_openers"]),
         "- Usual sentence openers: %s" % ", ".join(v["openers"]),
         "- Phrases they repeat: %s" % (", ".join(v["signature_phrases"]) or "none yet (add more sources)"), "",
         "## Real lines (write like these, never copy them into a script)", ""]
    L += ["- %s" % e for e in v["examples"]]
    L += ["", "## Writing rules derived from this profile", "",
          "- Aim for sentences around %s words, but mix in fragments: at least %d%% under 6 words." % (
              v["mean_words"], max(25, int(v["short_share"] * 100))),
          "- Contract like they do: about %s per 100 words." % p["contractions"],
          "- %s" % ("They talk TO the viewer ('you' %s vs 'I' %s): keep second person." % (p["you"], p["i"])
                    if p["you"] >= p["i"] else "They tell their OWN story ('I' %s vs 'you' %s): keep first person." % (p["i"], p["you"])),
          "- Start sentences the way they do: %s." % ", ".join(v["openers"][:5]), ""]
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("sources", nargs="+")
    ap.add_argument("--out", required=True, help="voice.json path; a readable .md of the same name is written next to it")
    a = ap.parse_args()
    paths = [p for s in a.sources for p in (glob.glob(s) or [s]) if os.path.isfile(p)]
    if not paths:
        sys.exit("no readable sources")
    v = build(paths)
    if v["words"] < 150:
        print("warning: only %d words; a voice profile wants 600+ (3 or more videos)" % v["words"], file=sys.stderr)
    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    json.dump(v, open(a.out, "w", encoding="utf-8"), indent=2, ensure_ascii=False)
    md = render(v)
    open(os.path.splitext(a.out)[0] + ".md", "w", encoding="utf-8").write(md)
    print(md)


if __name__ == "__main__":
    main()
