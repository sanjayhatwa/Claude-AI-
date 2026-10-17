"""
spoken.py - shared text tools for scripts that are meant to be SAID.

A script that reads well and a script that sounds human out loud are
different things. These helpers measure the second: how long the sentences
are when spoken, how often a real person would contract, start a sentence
with "and" or "so", give a number, or say "you". voiceprint.py and
scriptcheck.py both use them.
"""

import collections
import json
import os
import re
import statistics

HERE = os.path.dirname(os.path.abspath(__file__))
SKILLS = os.path.dirname(os.path.dirname(HERE))
SLOP = os.path.join(SKILLS, "ig-human", "slop.json")

WORD_RE = re.compile(r"[A-Za-z0-9$%']+(?:[.,][0-9]+)?")
SENT_RE = re.compile(r"[^.!?\n]+[.!?]*")
CONTRACTION_RE = re.compile(r"\b\w+'(?:s|t|re|ve|ll|d|m)\b", re.I)
NUMBER_RE = re.compile(r"\$?\b\d[\d,.]*(?:%|k|K|x)?\b")
CUE_RE = re.compile(r"^\s*(?:\[.*\]|\(.*\)|#|>|[-*]?\s*(?:on[- ]screen|text|b[- ]?roll|shot|cut|sfx|music|"
                    r"caption|visual|camera|note|beat|scene)\s*[:\-])", re.I)
SPEAKER_RE = re.compile(r"^\s*(?:vo|voice ?over|v\.o\.|me|creator|host|[A-Z][A-Za-z]{0,14})\s*:\s*")
TIME_RE = re.compile(r"^\s*\[?\(?\d{1,2}:\d{2}(?:\s*[-–]\s*\d{1,2}:\d{2})?\)?\]?\s*[-:]?\s*")

FILLERS = {"um", "uh", "like", "literally", "basically", "actually", "honestly", "okay", "ok",
           "right", "yeah", "so", "anyway", "kinda", "gonna", "wanna", "lowkey", "ngl"}

# Words and moves that live on the page, not in the mouth.
WRITTEN_ISMS = [
    "however", "moreover", "furthermore", "additionally", "thus", "therefore", "hence",
    "in order to", "utilize", "utilise", "individuals", "numerous", "a plethora of", "one must",
    "it is important to note", "it's important to note", "it is worth noting", "it's worth noting",
    "in conclusion", "to summarize", "in summary", "whom", "whereby", "upon", "regarding",
    "nevertheless", "consequently", "subsequently", "commence", "endeavor", "endeavour",
    "in today's video", "in this video", "without further ado", "a myriad of", "plays a crucial role",
    "it goes without saying", "the fact of the matter", "first and foremost", "last but not least",
]

# Lines a scroller has heard ten thousand times. They signal "content", not a person.
DEAD_OPENERS = [
    "hey guys", "hi guys", "what's up guys", "whats up guys", "welcome back", "in this video",
    "today i'm going to", "today we're going to", "today i want to", "so today", "let's talk about",
    "have you ever wondered", "did you know that", "are you tired of", "you won't believe",
    "stop scrolling", "wait for it", "watch till the end", "watch until the end", "before we start",
    "don't forget to", "make sure to like", "i'm going to show you", "in today's",
]

# Patterns that read as model-written when spoken aloud.
SPOKEN_AI_TELLS = [
    ("not-x-its-y", r"(?i)\b(?:it'?s|this is|that'?s) not (?:just |only |about )?[^.!?\n]{2,50}[,.;] (?:it'?s|this is|that'?s)\b",
     "Say the real thing straight. The 'not X, it's Y' turn is the most recognisable AI move."),
    ("colon-reveal", r"(?i)\b(?:here'?s the (?:thing|truth|kicker|secret|catch)|the (?:truth|secret|answer|reason|kicker) (?:is)?)\s*[:?]",
     "Drop the drumroll. Just say the truth."),
    ("q-then-a", r"(?i)(?:^|[.!]\s+)(?:the (?:result|answer|best part|catch|twist)|why|how)\?\s+\w",
     "A one-word question answered by yourself is a model's reveal. Say it in one sentence."),
    ("triad", r"(?i)\b\w+(?: \w+)?, \w+(?: \w+)?,? and \w+(?: \w+)?[.!]",
     "Lists of exactly three are the model's default rhythm. Use two, or one concrete example."),
    ("cliche", r"(?i)\b(?:game[- ]changer|unlock(?:s|ing)? (?:your|the)|elevate your|level up your|take (?:it|your \w+) to the next level|"
     r"secret sauce|buckle up|spoiler alert|let'?s dive in|dive into|deep dive|journey|in the realm of|tapestry|"
     r"landscape of|navigate the|embark|unleash|supercharge|revolutionize|cutting[- ]edge|seamless(?:ly)?|"
     r"harness the power|it'?s that simple|and that'?s the magic|mind[- ]blowing|hidden gem|life[- ]changing)\b",
     "Stock phrase. Replace it with the specific thing that happened."),
    ("moral-close", r"(?i)(?:^|[.!?]\s+)(?:remember,|so remember|at the end of the day|the lesson here|the takeaway|"
     r"never forget|always remember|trust the process)",
     "No moral at the end. Stop on the payoff or the loop."),
    ("trust-me", r"(?i)\b(?:trust me|believe me|i promise you|i guarantee)\b",
     "Asking for trust signals you have not shown proof. Show the number instead."),
    ("em-dash", r"[—]", "Nobody says an em dash. Use a full stop or a comma."),
    ("semicolon", r";", "Nobody speaks a semicolon. Split the sentence."),
]

PIVOTS = {"but", "so", "now", "wait", "except", "then", "until", "because", "and", "turns", "plot",
          "here's", "heres", "which", "meanwhile", "instead", "suddenly", "anyway"}

OPEN_LOOP_RE = re.compile(r"(?i)(\?|\bbut\b|\buntil\b|\bbefore\b|\bonly\b|\binstead\b|\bthan\b|\blost\b|\bfailed\b|"
                          r"\bquit\b|\balmost\b|\bfinally\b|\bwhy\b|\bhow (?:i|to|you)\b|\bhere'?s (?:why|how|what)\b|\bthe reason\b|\bwait\b|"
                          r"\bnobody\b|\bmost people\b|\bthe (?:one|only) thing\b|\bby the end\b|\bwhat happened\b|"
                          r"\bturns out\b|\bsecret\b|\bmistake\b|\bwrong\b|\bstop\b|\bnever\b)")
CTA_RE = re.compile(r"(?i)\b(follow|comment|save|share|subscribe|link in (?:my )?bio|part 2|dm me|send this)\b")


def load_slop():
    try:
        return json.load(open(SLOP, encoding="utf-8"))
    except Exception:
        return {"words": [], "phrases": [], "structures": []}


LABEL_RE = re.compile(r"(?i)^\s*(?:#+\s*)?(?:hook|intro|setup|body|payoff|outro|cta|turn|twist|ending|loop|"
                      r"beat|act|part|scene|section|step|re-?hook)\b[^.!?]*$")


def is_label(line):
    """'HOOK (0:00-0:03)', 'BEAT 2', '## Payoff': structure labels, not speech."""
    letters = [c for c in line if c.isalpha()]
    if LABEL_RE.match(line) and len(line.split()) <= 6:
        return True
    return bool(letters) and all(c.isupper() for c in letters) and len(line.split()) <= 5 and not re.search(r"[.!?]$", line)


def spoken_lines(script):
    """Keep only what is said out loud: drop cue lines, labels, timestamps and speaker labels."""
    out = []
    for raw in script.replace("\r", "").split("\n"):
        line = raw.strip()
        if not line or CUE_RE.match(line) or is_label(line):
            continue
        line = TIME_RE.sub("", line)
        line = SPEAKER_RE.sub("", line) if re.match(r"^\s*[A-Za-z .]{1,16}:\s", line) else line
        line = re.sub(r"\[[^\]]*\]|\([^)]*\)|\{[^}]*\}", "", line).strip()
        line = line.strip("\"'")
        if line:
            out.append(line)
    return out


def sentences(text):
    return [s.strip() for s in SENT_RE.findall(text) if WORD_RE.search(s)]


def words(text):
    return WORD_RE.findall(text)


def text_stats(text):
    """Speech-relevant statistics from plain text (no timing needed)."""
    sents = sentences(text)
    ws = words(text)
    n = max(1, len(ws))
    lens = [len(words(s)) for s in sents] or [0]
    low = [w.lower().replace("’", "'") for w in ws]
    openers = collections.Counter(words(s)[0].lower() for s in sents if words(s))
    per100 = lambda c: round(100.0 * c / n, 1)
    grams = collections.Counter()
    for k in (2, 3):
        for i in range(len(low) - k + 1):
            grams[" ".join(low[i:i + k])] += 1
    stop = {"of the", "in the", "to the", "and the", "on the", "it is", "is a", "to be", "of a", "and i",
            "i was", "it was", "in a", "at the", "for the"}
    signature = [g for g, c in grams.most_common(60) if c >= 2 and g not in stop][:15]
    mean = statistics.mean(lens)
    return {
        "words": len(ws),
        "sentences": len(sents),
        "mean_words": round(mean, 1),
        "stdev_words": round(statistics.pstdev(lens), 1),
        "burstiness": round(statistics.pstdev(lens) / mean, 2) if mean else 0,
        "short_share": round(sum(1 for x in lens if x <= 5) / len(lens), 2),
        "long_share": round(sum(1 for x in lens if x >= 20) / len(lens), 2),
        "per_100_words": {
            "contractions": per100(len(CONTRACTION_RE.findall(text.replace("’", "'")))),
            "you": per100(sum(1 for t in low if t in ("you", "your", "you're", "yours", "you've"))),
            "i": per100(sum(1 for t in low if t in ("i", "me", "my", "i'm", "i've", "mine", "i'd"))),
            "numbers": per100(len(NUMBER_RE.findall(text))),
            "fillers": per100(sum(1 for t in low if t in FILLERS)),
            "questions": per100(sum(1 for s in sents if s.rstrip().endswith("?"))),
            "and_but_so_openers": per100(sum(openers[o] for o in ("and", "but", "so"))),
        },
        "openers": [o for o, _ in openers.most_common(10)],
        "signature_phrases": signature,
    }


def duration_s(text, wpm):
    return round(len(words(text)) / max(60.0, wpm) * 60.0, 1)
