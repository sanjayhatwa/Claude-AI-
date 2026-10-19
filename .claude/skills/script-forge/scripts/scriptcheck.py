#!/usr/bin/env python3
"""
scriptcheck.py - score a short-form script the way a viewer hears it.

Eight checks, each 0-100 (higher is better), plus line-by-line flags with
the fix. Only spoken lines are scored: lines that start with [B-ROLL],
ON-SCREEN:, SHOT:, TEXT:, SFX:, (notes) and timestamps are skipped, and a
speaker label like "VO:" is removed.

  HOOK         the first sentence: short, concrete, no throat-clearing
  RHYTHM       sentence-length variety the way people talk (fragments + runs)
  SPOKEN       contractions and plain words; nothing that only exists on paper
  AI TELLS     the patterns that make a script sound model-written out loud
  SPECIFIC     numbers, names, moments; abstract scripts don't hold attention
  RETENTION    open loop early, re-hooks through the middle, clean ending
  LENGTH       spoken duration against the target
  VOICE        distance from the creator's voiceprint (only with --voice)

    python3 scriptcheck.py script.md --seconds 35 --wpm 175 --voice workspace/voice.json
    python3 scriptcheck.py script.md --json
    python3 scriptcheck.py a.md b.md          # compare drafts, best first

Exit code 0 always; the verdict line says SHIP (80+, no red line), FIX or REWRITE.
Heuristics, not a model: they catch what makes scripts sound written. The
viewer-panel swipe test in SKILL.md is the judgment layer on top.
"""

import argparse
import json
import os
import re
import statistics
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import spoken  # noqa: E402


def clamp(x):
    return max(0.0, min(100.0, x))


def scale(v, good, bad):
    if good == bad:
        return 50.0
    return clamp((v - bad) / (good - bad) * 100)


def check_hook(lines, sents):
    flags = []
    if not sents:
        return 0, "no spoken lines", flags
    first = sents[0]
    n = len(spoken.words(first))
    low = " ".join(sents[:2]).lower()
    score = 100.0
    if n > 14:
        score -= min(50, (n - 14) * 6)
        flags.append(("hook", first, "First sentence is %d words. Get the claim out in 12 or fewer." % n))
    dead = [d for d in spoken.DEAD_OPENERS if low.startswith(d) or (" " + d) in low[:60]]
    if dead:
        score -= 45
        flags.append(("hook", first, "Throat-clearing opener (%s). Start on the claim, the conflict or the result." % dead[0]))
    concrete = bool(spoken.NUMBER_RE.search(first)) or bool(re.search(r"(?i)\b(you|your|i|my)\b", first))
    tension = bool(spoken.OPEN_LOOP_RE.search(first))
    if not concrete:
        score -= 15
        flags.append(("hook", first, "No 'you', no 'I', no number in the first line. Make it about someone or something specific."))
    if not tension:
        score -= 15
        flags.append(("hook", first, "No tension in the first line: no question, contrast, mistake or promise of a payoff."))
    return clamp(score), "%d words, concrete=%s, tension=%s" % (n, concrete, tension), flags


def check_rhythm(sents):
    lens = [len(spoken.words(s)) for s in sents]
    flags = []
    if len(lens) < 4:
        return 60.0, "too short to judge", flags
    mean = statistics.mean(lens)
    cv = statistics.pstdev(lens) / mean if mean else 0
    short = sum(1 for x in lens if x <= 5) / len(lens)
    score = scale(cv, 0.65, 0.2) * 0.6 + (100 - abs(short - 0.4) * 180) * 0.4
    for i in range(len(lens) - 2):
        a, b, c = lens[i:i + 3]
        if max(a, b, c) - min(a, b, c) <= 2 and min(a, b, c) >= 6:
            flags.append(("rhythm", sents[i + 1], "Three sentences in a row at ~%d words. Break one into a fragment or run two together." % b))
            score -= 6
            break
    for s, n in zip(sents, lens):
        if n >= 26:
            flags.append(("rhythm", s, "%d words in one breath. Split it where you would breathe." % n))
            score -= 5
    return clamp(score), "variation %.2f, %d%% short sentences" % (cv, short * 100), flags


def check_spoken(text, sents):
    flags = []
    ws = spoken.words(text)
    if not ws:
        return 0, "empty", flags
    per100 = 100.0 / len(ws)
    contr = len(spoken.CONTRACTION_RE.findall(text.replace("’", "'"))) * per100
    low = text.lower()
    hits = []
    for w in spoken.WRITTEN_ISMS:
        for m in re.finditer(r"\b" + re.escape(w) + r"\b", low):
            hits.append(w)
    uncontracted = re.findall(r"(?i)\b(?:it is|do not|does not|did not|i am|you are|we are|they are|that is|"
                              r"there is|can not|cannot|will not|would not|should not|is not|are not|i have|you have|i will)\b", text)
    score = scale(contr, 4.0, 0.0) * 0.5 + clamp(100 - len(hits) * 18) * 0.3 + clamp(100 - len(uncontracted) * 10) * 0.2
    for s in sents:
        sl = s.lower()
        for w in spoken.WRITTEN_ISMS:
            if re.search(r"\b" + re.escape(w) + r"\b", sl):
                flags.append(("spoken", s, "'%s' is written English. Say it the way you'd say it to a friend." % w))
                break
        else:
            m = re.search(r"(?i)\b(it is|do not|does not|i am|you are|that is|there is|will not|is not|are not)\b", s)
            if m:
                flags.append(("spoken", s, "'%s' sounds read. Contract it." % m.group(1)))
    return clamp(score), "%.1f contractions/100 words, %d written-isms, %d uncontracted" % (contr, len(hits), len(uncontracted)), flags


def check_ai(text, sents, lex):
    flags = []
    tells = 0
    for tid, rx, fix in spoken.SPOKEN_AI_TELLS:
        for s in sents:
            if re.search(rx, s):
                flags.append(("ai", s, fix))
                tells += 1
    for st in lex.get("structures", []):
        try:
            rx = re.compile(st["regex"], re.M)
        except re.error:
            continue
        for s in sents:
            if rx.search(s) and not any(f[1] == s and f[0] == "ai" for f in flags):
                flags.append(("ai", s, st.get("fix", st.get("name", "structural tell"))))
                tells += 1
    stock = []
    for e in lex.get("words", []) + lex.get("phrases", []):
        if re.search(r"\b" + re.escape(e["find"]).replace(r"\ ", r"\s+") + r"\b", text, re.I):
            stock.append(e["find"])
    tells += len(stock)
    if stock:
        flags.append(("ai", "", "Stock words: %s. Use the plain word or the specific thing." % ", ".join(sorted(set(stock))[:6])))
    return clamp(100 - tells * 14), "%d tell(s)" % tells, flags


def check_specific(text, sents):
    ws = spoken.words(text)
    if len(ws) < 20:
        return 60.0, "too short to judge", []
    per100 = 100.0 / len(ws)
    nums = len(spoken.NUMBER_RE.findall(text))
    names = len(set(re.findall(r"(?<![.!?]\s)(?<!^)\b[A-Z][a-z]{2,}\b", text, re.M)))
    moments = len(re.findall(r"(?i)\b(yesterday|last (?:week|month|year|night)|this morning|tonight|"
                             r"\d{1,2}(?::\d{2})?\s?(?:am|pm)|monday|tuesday|wednesday|thursday|friday|saturday|sunday|"
                             r"\d+ (?:days|weeks|months|years|hours|minutes) (?:ago|later|in))\b", text))
    density = (nums + names + moments) * per100
    flags = []
    if nums == 0:
        flags.append(("specific", "", "No numbers anywhere. One real number (a price, a count, a time) makes it believable."))
    abstract = [s for s in sents if len(spoken.words(s)) >= 8 and not spoken.NUMBER_RE.search(s)
                and not re.search(r"\b[A-Z][a-z]{2,}\b", s[1:]) and re.search(
                    r"(?i)\b(success|growth|mindset|potential|value|content|strategy|results|journey|goals|things|stuff)\b", s)]
    for s in abstract[:2]:
        flags.append(("specific", s, "Abstract. Replace with the moment, the number or the object."))
    return clamp(scale(density, 5.0, 0.5) - len(abstract) * 6), "%d numbers, %d names, %d moments (%.1f per 100 words)" % (
        nums, names, moments, density), flags


def check_retention(sents, seconds):
    flags = []
    if len(sents) < 3:
        return 50.0, "too short to judge", flags
    n = len(sents)
    early = sents[:max(1, n // 5 + 1)]
    loop = any(spoken.OPEN_LOOP_RE.search(s) for s in early)
    pivots = [i for i, s in enumerate(sents[1:], 1) if spoken.words(s) and spoken.words(s)[0].lower() in spoken.PIVOTS]
    want = max(1, int((seconds or 30) / 10))
    gaps = [b - a for a, b in zip([0] + pivots, pivots + [n])]
    longest = max(gaps) if gaps else n
    last = sents[-1]
    cta = bool(spoken.CTA_RE.search(" ".join(sents[-2:])))
    score = 100.0
    if not loop:
        score -= 30
        flags.append(("retention", sents[0], "No open loop in the first fifth. Promise something the viewer only gets by staying."))
    if len(pivots) < want:
        score -= 20
        flags.append(("retention", "", "Only %d re-hook(s) (a sentence opening on but/so/then/wait). Want about one every 8-10 seconds: %d." % (len(pivots), want)))
    if longest > 6:
        score -= 10
        flags.append(("retention", sents[min(n - 1, sum(gaps[:gaps.index(longest)]) + 3)],
                      "A stretch of %d sentences with no turn. Viewers leave in the flat middle." % longest))
    asks = len(spoken.CTA_RE.findall(" ".join(sents)))
    if asks > 1:
        score -= 10
        flags.append(("retention", last, "%d asks in one video. Keep one." % asks))
    return clamp(score), "open loop=%s, re-hooks=%d, longest flat run=%d sentences, CTA=%s" % (loop, len(pivots), longest, cta), flags


def check_length(text, seconds, wpm):
    dur = spoken.duration_s(text, wpm)
    if not seconds:
        return None, "%.0fs at %d wpm" % (dur, wpm), []
    off = abs(dur - seconds) / seconds
    flags = []
    if off > 0.15:
        flags.append(("length", "", "Reads at %.0fs against a %ds target (%d wpm). %s about %d words." % (
            dur, seconds, wpm, "Cut" if dur > seconds else "Add", abs(dur - seconds) * wpm / 60)))
    return clamp(100 - max(0, off - 0.1) * 250), "%.0fs at %d wpm (target %ds)" % (dur, wpm, seconds), flags


def check_voice(text, voice):
    if not voice:
        return None, "no voiceprint", []
    st = spoken.text_stats(text)
    p, q = st["per_100_words"], voice["per_100_words"]
    flags = []
    d_len = abs(st["mean_words"] - voice["mean_words"]) / max(4, voice["mean_words"])
    d_con = abs(p["contractions"] - q["contractions"]) / max(2, q["contractions"])
    d_you = abs(p["you"] - q["you"]) / max(2, q["you"])
    d_i = abs(p["i"] - q["i"]) / max(2, q["i"])
    d_open = abs(p["and_but_so_openers"] - q["and_but_so_openers"]) / max(1.5, q["and_but_so_openers"])
    score = 100 - (min(1, d_len) * 30 + min(1, d_con) * 20 + min(1, d_you) * 15 + min(1, d_i) * 15 + min(1, d_open) * 10)
    sig = [g for g in voice.get("signature_phrases", []) if g in text.lower()]
    if voice.get("signature_phrases") and not sig:
        score -= 10
        flags.append(("voice", "", "None of their own phrases appear (%s). Use one, naturally." % ", ".join(voice["signature_phrases"][:4])))
    if d_len > 0.35:
        flags.append(("voice", "", "Sentences average %s words; they talk in %s." % (st["mean_words"], voice["mean_words"])))
    if d_con > 0.5:
        flags.append(("voice", "", "%s contractions per 100 words; they use %s." % (p["contractions"], q["contractions"])))
    if d_you > 0.6 or d_i > 0.6:
        flags.append(("voice", "", "Person is off: you %s / I %s here vs you %s / I %s in their voice." % (p["you"], p["i"], q["you"], q["i"])))
    return clamp(score), "sentence len %s vs %s, contractions %s vs %s" % (st["mean_words"], voice["mean_words"], p["contractions"], q["contractions"]), flags


WEIGHTS = {"HOOK": 0.22, "RHYTHM": 0.12, "SPOKEN": 0.14, "AI TELLS": 0.16, "SPECIFIC": 0.1,
           "RETENTION": 0.14, "LENGTH": 0.04, "VOICE": 0.08}


def score_script(script, seconds=None, wpm=170, voice=None):
    lines = spoken.spoken_lines(script)
    text = " ".join(lines)
    sents = spoken.sentences(text)
    lex = spoken.load_slop()
    checks = {
        "HOOK": check_hook(lines, sents),
        "RHYTHM": check_rhythm(sents),
        "SPOKEN": check_spoken(text, sents),
        "AI TELLS": check_ai(text, sents, lex),
        "SPECIFIC": check_specific(text, sents),
        "RETENTION": check_retention(sents, seconds),
        "LENGTH": check_length(text, seconds, wpm),
        "VOICE": check_voice(text, voice),
    }
    used = {k: v for k, v in checks.items() if v[0] is not None}
    total_w = sum(WEIGHTS[k] for k in used)
    overall = sum(v[0] * WEIGHTS[k] for k, v in used.items()) / total_w
    red = [k for k, v in used.items() if v[0] < 50]
    verdict = "SHIP" if overall >= 80 and not red else "FIX" if overall >= 60 else "REWRITE"
    flags = [f for v in checks.values() for f in v[2]]
    return {
        "overall": round(overall), "verdict": verdict, "red": red,
        "seconds": spoken.duration_s(text, wpm), "words": len(spoken.words(text)),
        "checks": {k: {"score": None if v[0] is None else round(v[0]), "detail": v[1]} for k, v in checks.items()},
        "flags": [{"check": c, "line": l, "fix": f} for c, l, f in flags],
    }


def bar(score, width=20):
    if score is None:
        return " " * width
    n = int(round(score / 100 * width))
    return "#" * n + "." * (width - n)


def render(r, label=None):
    out = ["SCRIPT CHECK%s  ·  %d words  ·  ~%ss spoken" % ("  ·  " + label if label else "", r["words"], r["seconds"]), ""]
    for k, v in r["checks"].items():
        s = "--" if v["score"] is None else "%3d" % v["score"]
        out.append("  %-10s %s  %s  %s" % (k, s, bar(v["score"]), v["detail"]))
    out += ["", "  OVERALL %d  ->  %s%s" % (r["overall"], r["verdict"], "  (red: %s)" % ", ".join(r["red"]) if r["red"] else ""), ""]
    if r["flags"]:
        out.append("FIX THESE")
        for f in r["flags"]:
            line = (" \"%s\"" % (f["line"][:70] + ("..." if len(f["line"]) > 70 else ""))) if f["line"] else ""
            out.append("  [%s]%s\n      -> %s" % (f["check"], line, f["fix"]))
    return "\n".join(out)


def main():
    spoken.utf8_stdio()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("scripts", nargs="+", help="script file(s), or - for stdin")
    ap.add_argument("--seconds", type=int, help="target length")
    ap.add_argument("--wpm", type=int, default=170, help="speaking pace (use the reference's from style_dna)")
    ap.add_argument("--voice", help="voice.json from voiceprint.py")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    voice = json.load(open(a.voice, encoding="utf-8")) if a.voice else None
    results = []
    for p in a.scripts:
        text = sys.stdin.read() if p == "-" else open(p, encoding="utf-8").read()
        results.append((p, score_script(text, a.seconds, a.wpm, voice)))
    if len(results) > 1:
        results.sort(key=lambda x: -x[1]["overall"])
    if a.json:
        print(json.dumps({p: r for p, r in results} if len(results) > 1 else results[0][1], indent=2))
        return
    for p, r in results:
        print(render(r, os.path.basename(p) if len(results) > 1 else None))
        print()


if __name__ == "__main__":
    main()
