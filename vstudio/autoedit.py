"""
autoedit.py - raw talking video in, a short edited the way a professional
short-form editor would cut it: a TIMELINE SPEC that render.py turns into the
finished video, plus EDIT_REPORT.md saying what was cut and why.

    python -m vstudio autoedit raw.mp4 --out-dir workspace/edits/take1 --render
    python -m vstudio autoedit raw.mp4 --out-dir DIR --captions raw.srt --style style_dna.json \
        --aspect 9:16 --energy high --broll broll/ --music bed.mp3 --max-duration 60 --hook "TEXT" --render

    spec = plan_edit("raw.mp4", words, {"aspect": "9:16", "energy": "high"})

Writes DIR/edit.json (the timeline: open it in the studio or edit it by
hand), DIR/EDIT_REPORT.md, DIR/transcript.json (the source words, so a
second run needs no transcription) and, with --render, DIR/edited.mp4.

What the edit does, driven by the words (transcribe.py) and the audio:

  Cuts      dead air (pauses over 0.35 s at high energy, 0.42 medium, 0.5
            calm, keeping 0.08-0.12 s of breath on each side), filler words
            (um, uh, er; "like", "so", "you know" only when pauses or an
            "um" isolate them; stutters such as "the the"), retakes (a
            phrase restarted with the same 3+ words after a pause, a cut
            filler or a dash / ellipsis: the last take is kept; a sentence
            or a comma-closed clause counts only when what it said comes
            back, so "if you want views, if you want followers, ..." stays)
            and, with --max-duration, the lowest-value
            sentences from the middle (no keywords, no numbers), never the
            hook or the ending. Word edges are first tightened against the audio, cut
            points snap to the quietest 10 ms nearby, and segments are
            rounded to whole frames so picture and sound cut together.
            Without a transcript it cuts on silence (ffmpeg silencedetect,
            threshold from the recording's own noise floor) and says so.
  Pace      kept stretches become consecutive video layers (jump cuts); a
            take longer than ~5 s (high energy) is split at a sentence
            pause into two shots so the framing can change.
  Reframe   when the output shape differs from the source (16:9 -> 9:16)
            each shot gets a focus point from the speaker's face
            (reframe.py: YuNet, median + zero-phase EMA smoothing, a small
            dead zone so shots of one take frame identically), the centre
            when no face is found.
  Zooms     punch-in scale alternates per cut (1.0 / 1.08-1.15, never the
            same twice in a row) around the face, so jump cuts look meant;
            camera punches land on numbers, superlatives, negations and the
            loudest word of a sentence, hold to the sentence end and snap
            back on the next cut.
  Graphics  a hook title for the first 2-3 s; callouts from the speech:
            numbers, money and percentages (stat / counter_reveal), list
            markers (list_item / step_number), questions, "X vs Y", quotes,
            and a CTA when it ends with follow / subscribe / comment / save.
            At most one every 4-6 s, each placed in the largest free band
            of the frame: never over the face (including its zoom) or the
            captions, inside the platform safe zone, scaled down to fit.
  Captions  the remapped words, styled by --style (a Style DNA's caption
            track) or a strong default sized for the output shape; moved
            just under the chin (or above the head) when that position
            would cover this speaker's face.
  B-roll    --broll DIR: clips and images matched by filename keywords to
            what is being said, else placed at topic changes as 2-3 s
            cutaways (the talking layer's sound continues underneath;
            Ken Burns on stills).
  Sound     a hit on the hook, pops on callouts, a whoosh on about one cut
            in three (with a short zoom blur at high energy), a riser into
            the payoff, and --music ducked under the voice.
  Energy    high / medium / calm presets set all of the above; auto picks
            one from the speaking rate.

Limits: retakes are found by repeated openings, so a retake reworded from
its first word is kept; one focus per shot (no moving crop inside a shot);
the graphics read the words, not the picture; filler detection depends on
the transcript containing the fillers (some captions drop them).
"""

import argparse
import copy
import math
import os
import re
import shutil
import subprocess
import sys
import time

import numpy as np

from . import anim, paths, reframe, templates, textrender, timeline, transcribe

ASPECTS = {"9:16": (1080, 1920), "16:9": (1920, 1080), "1:1": (1080, 1080), "4:5": (1080, 1350)}
VIDEO_EXT = (".mp4", ".mov", ".m4v", ".mkv", ".webm", ".avi")
IMAGE_EXT = (".jpg", ".jpeg", ".png", ".webp", ".bmp")

# Energy presets. gap: pause that gets cut; tail / lead: breath kept after / before speech;
# hold: longest shot before a pace cut (None = never split); punch: punch-in scales that
# alternate with 1.0; zoom*: camera punches on emphasis; graphic_spacing: seconds between callouts;
# whoosh_every: one whoosh per N cuts (0 = none); blur: zoom-blur strength on those cuts.
ENERGY = {
    "high": {"gap": 0.35, "tail": 0.08, "lead": 0.08, "hold": 3.2, "punch": (1.12, 1.08, 1.15),
             "zoom": 1.14, "zoom_spacing": 1.8, "zoom_in": 0.16, "zoom_ease": "out-back", "zoom_release": 0.28,
             "graphic_spacing": 4.0, "callout_len": 2.0, "whoosh_every": 3, "blur": 0.8,
             "broll_in": ("whip-left", 0.22), "broll_out": None, "music": 0.2, "pop": True},
    "medium": {"gap": 0.42, "tail": 0.1, "lead": 0.1, "hold": 4.5, "punch": (1.08, 1.1),
               "zoom": 1.08, "zoom_spacing": 3.5, "zoom_in": 0.26, "zoom_ease": "out-cubic", "zoom_release": 0.4,
               "graphic_spacing": 5.0, "callout_len": 2.4, "whoosh_every": 4, "blur": 0.45,
               "broll_in": ("crossfade", 0.2), "broll_out": None, "music": 0.16, "pop": True},
    "calm": {"gap": 0.5, "tail": 0.12, "lead": 0.12, "hold": None, "punch": (1.06,),
             "zoom": 1.05, "zoom_spacing": 6.0, "zoom_in": 0.6, "zoom_ease": "in-out-cubic", "zoom_release": 0.6,
             "graphic_spacing": 6.5, "callout_len": 3.0, "whoosh_every": 0, "blur": 0.0,
             "broll_in": ("crossfade", 0.35), "broll_out": ("fade", 0.3), "music": 0.12, "pop": False},
}
LEAD_IN = 0.1        # breath kept before the first word
END_TAIL = 0.3       # kept after the last word
MIN_SEGMENT = 0.3    # shorter kept pieces are merged back when a small pause cut allows it
MAX_TOTAL_ZOOM = 1.25

HARD_FILLERS = {"um", "umm", "ummm", "uh", "uhh", "uhhh", "uhm", "er", "erm", "err", "hmm", "hm", "mm", "mmm"}
SOFT_FILLERS = {"like", "so", "ah"}
FILLER_PAIRS = {("you", "know"), ("i", "mean")}
STUTTER = {"i", "i'm", "the", "a", "an", "and", "to", "it", "it's", "is", "we", "you", "my", "in", "of", "but",
           "this", "if", "he", "she", "they", "what", "for", "on", "with", "be", "can", "just", "there"}
CUTOFF = ("-", "\u2014", "\u2013", "...", "\u2026")   # a word cut off mid-phrase ("to-", "the...")
CLAUSE_END = (",", ";", ":")                           # a finished clause ("if you want views,")
# words no phrase ends with: a take that stops on one was cut short, even when the transcript put a comma after it
OPEN_ENDED = {"a", "an", "the", "and", "or", "but", "to", "of", "for", "with", "at", "by", "from", "in", "on",
              "into", "about", "as", "than", "if", "because", "my", "your", "our", "their", "his", "is", "are",
              "was", "were", "i", "we", "they", "he", "she"}

STOP = set("""a an the and or but so of to in on for with at by from is are was were be been being it its it's
this that these those i i'm i've i'd me my mine you you're your yours we we're our they they're their them he she
his her him us as if then than not no do does did done doing just like really very um uh gonna wanna kind sort
thing things know mean actually basically literally okay ok yeah yes right well also there here what which who
whom how when where why all any some can could will would should shall may might must about get got gets have has
had having going go goes went say says said one out up down now into over more most much many lot lots let let's
make made making take took want need see look come came give put way ways time times something anything everything
nothing someone anyone everyone because while still even only ever never always again back off too don't didn't
doesn't isn't wasn't aren't can't won't i'll you'll we'll that's there's here's what's""".split())
# superlatives and intensifiers: the words a speaker leans on
SUPERLATIVES = {"best", "worst", "most", "least", "biggest", "fastest", "easiest", "hardest", "greatest", "highest",
                "lowest", "cheapest", "smartest", "simplest", "only", "top", "ultimate", "perfect",
                "every", "everyone", "everything", "always", "insane", "crazy", "huge", "massive", "secret", "free",
                "instantly", "guaranteed", "double", "triple"}
EST_NOT = {"honest", "interest", "test", "rest", "contest", "invest", "request", "forest", "suggest", "protest",
           "guest", "west", "chest", "nest", "harvest", "modest", "earnest", "digest", "arrest", "manifest", "latest",
           "priest", "quest", "vest", "pest", "crest", "attest", "detest", "unrest"}
NEGATIONS = {"never", "no", "not", "don't", "dont", "can't", "cant", "won't", "wont", "nobody", "nothing", "none",
             "stop", "didn't", "didnt", "isn't", "isnt", "wasn't", "shouldn't", "doesn't", "without", "nowhere"}

UNITS = {"zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
         "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
         "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19}
TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80,
        "ninety": 90}
SCALES = {"hundred": 100, "thousand": 1000, "k": 1000, "million": 1e6, "billion": 1e9}
COUNT_UNITS = {"second", "minute", "hour", "day", "week", "month", "year", "time", "step", "tip", "way", "mistake",
               "thing", "reason", "rule", "people", "person", "view", "follower", "subscriber", "like", "video",
               "post", "client", "customer", "sale", "dollar", "pound", "euro", "kilo", "kg", "mile", "km",
               "calorie", "rep", "book", "hack", "habit", "lesson", "tool", "app", "user", "download",
               "comment", "share", "episode", "account", "job", "country", "language"}
ORDINALS = {"first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6, "seventh": 7, "eighth": 8,
            "ninth": 9, "tenth": 10}
LIST_NOUNS = {"tip", "step", "mistake", "thing", "reason", "way", "rule", "lesson", "hack", "habit", "sign",
              "secret", "trick", "point", "method", "strategy", "tool", "app", "book", "idea", "myth", "question",
              "one", "option", "part"}
QUESTION_STARTS = (("do", "you"), ("did", "you"), ("have", "you"), ("are", "you"), ("would", "you"),
                   ("could", "you"), ("can", "you"), ("ever", "wondered"), ("what", "if"), ("why", "do"),
                   ("why", "does"), ("how", "do"), ("how", "does"), ("what's", "the"), ("whats", "the"))
CTA_VERBS = {"follow": "plus", "subscribe": "bell", "comment": "comment", "save": "bookmark", "share": "share",
             "like": "heart"}
# what may follow a CTA verb ("follow for part 2", "save this"), so "follow these steps" or "save money" is no CTA;
# None: anything ("subscribe", "comment GUIDE")
CTA_NEXT = {"follow": {"for", "me", "us", "along", "now", "my", "the", "and", ""}, "subscribe": None, "comment": None,
            "save": {"this", "it", "the", "for", "and", ""}, "share": {"this", "it", "with", "the", "and", ""},
            "like": {"this", "the", "and", "it", "my"}}
CTA_LEAD = {"so", "and", "to", "please", "just", "go", "make", "sure", "don't", "dont", "forget", "then", "now",
            "you", "should", "also", "definitely", "if", "want", "more", "for"}
DANGLING = {"and", "or", "but", "the", "a", "an", "to", "of", "for", "with", "so", "that", "like", "um", "uh", "my",
            "your", "this", "is", "are", "was", "in", "on", "at", "by", "from", "as", "if", "then", "than", "i",
            "you", "we", "they", "it", "just", "really", "very"}
COMPARATIVES = {"better", "worse", "faster", "slower", "cheaper", "bigger", "smaller", "easier", "harder", "more",
                "less", "best", "worst"}
LEAD_WORDS = {"so", "okay", "ok", "alright", "hey", "guys", "well", "and", "but", "now", "listen", "look", "hi",
              "hello", "yo", "um", "uh", "like", "right"}


# =========================================================================== small helpers

def _tok(w):
    """A word for matching: lower case, letters/digits/apostrophes only."""
    return re.sub(r"[^a-z0-9']", "", str(w).lower().replace("\u2019", "'"))


def _bare(w):
    """A word as shown on screen, without surrounding punctuation or quotes."""
    return re.sub(r"^[\"'\u201c\u2018(\[]+|[\"'\u201d\u2019)\].,!?;:]+$", "", str(w).strip())


def _stem(t):
    """Light stemming so 'tips' meets 'tip' and 'coffee' meets 'coffees'."""
    if len(t) > 4 and t.endswith("ies"):
        return t[:-3] + "y"
    if len(t) > 3 and t.endswith("s") and not t.endswith("ss"):
        return t[:-1]
    return t


def _content(t):
    return bool(t) and t not in STOP and len(t) >= 3 and not t.isdigit()


def keywords(words):
    """Content keywords (stemmed) of a list of word dicts or strings."""
    out = set()
    for w in words:
        t = _tok(w["w"] if isinstance(w, dict) else w)
        if _content(t):
            out.add(_stem(t))
    return out


def _jaccard(a, b):
    return len(a & b) / float(len(a | b)) if a and b else 0.0


def _is_superlative(t):
    return t in SUPERLATIVES or (len(t) >= 6 and t.endswith("est") and t not in EST_NOT)


def _even(v):
    return max(2, int(round(v / 2.0)) * 2)


def _r3(v):
    return round(float(v), 3)


def _floor_ms(v):
    return math.floor(float(v) * 1000 + 1e-6) / 1000.0


def _ffmpeg():
    exe = shutil.which("ffmpeg")
    if not exe:
        raise RuntimeError("ffmpeg is not installed or not on PATH (https://ffmpeg.org/download.html)")
    return exe


def _text(words):
    return " ".join(_bare(w["w"]) for w in words).strip()


# =========================================================================== audio analysis

def audio_env(video, sr=16000, hop=0.01):
    """Loudness of the recording per 10 ms: {"db", "hop", "floor", "speech", "thr", "contrast"}, or None.

    floor / speech are the 10th / 95th percentile levels; thr (the silence
    threshold) sits 30 % of the way from the floor to speech, at least 6 dB
    above the floor and at most 12 dB below speech. Decoded as a stream, so
    long recordings do not need memory for their samples.
    """
    ff = shutil.which("ffmpeg")
    if not ff:
        return None
    n = int(round(sr * hop))
    proc = subprocess.Popen([ff, "-nostdin", "-hide_banner", "-loglevel", "error", "-i", video, "-vn", "-ac", "1",
                             "-ar", str(sr), "-f", "f32le", "-"], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL)
    powers, rest, tail = [], np.zeros(0, np.float32), b""
    try:
        while True:
            buf = proc.stdout.read(n * 4 * 2000)
            if not buf:
                break
            buf = tail + buf
            usable = len(buf) // 4 * 4            # a pipe read can end inside a float
            tail = buf[usable:]
            x = np.concatenate([rest, np.frombuffer(buf[:usable], np.float32)])
            k = len(x) // n
            if k:
                powers.append(np.mean(x[:k * n].reshape(k, n).astype(np.float64) ** 2, axis=1))
            rest = x[k * n:]
    finally:
        proc.stdout.close()
        proc.wait()
    if not powers:
        return None
    p = np.concatenate(powers)
    db = (10 * np.log10(p + 1e-10)).astype(np.float32)
    # 30 ms smoothing, so single quiet frames inside a word are not read as silence
    sm = np.convolve(db, np.ones(3) / 3.0, mode="same").astype(np.float32)
    floor, speech = float(np.percentile(sm, 10)), float(np.percentile(sm, 95))
    thr = floor + max(6.0, 0.3 * (speech - floor))
    thr = max(-70.0, min(thr, speech - 12.0))
    return {"db": sm, "hop": hop, "floor": floor, "speech": speech, "thr": thr, "contrast": speech - floor}


def word_level(env, s, e):
    """Mean level (dB) of the recording between s and e."""
    if env is None:
        return -100.0
    a = max(0, int(s / env["hop"]))
    b = max(a + 1, int(math.ceil(e / env["hop"])))
    seg = env["db"][a:b]
    if not len(seg):
        return -100.0
    return float(10 * np.log10(np.mean(10 ** (seg.astype(np.float64) / 10)) + 1e-10))


def refine_words(words, env):
    """Tighten word edges that sit in silence (captions spread evenly over a cue, padded whisper ends).

    Only shrinks words: a start moves to 40 ms before the first loud frame,
    an end to 60 ms after the last one, when more than 0.2 s of the word is
    silent. Returns (words, number of edges moved).
    """
    if env is None or env["contrast"] < 12:
        return words, 0
    hop, thr, db = env["hop"], env["thr"], env["db"]
    moved = 0
    out = []
    for w in words:
        w = dict(w)
        a, b = int(w["s"] / hop), int(math.ceil(w["e"] / hop))
        seg = db[a:b]
        loud = np.nonzero(seg >= thr)[0]
        if len(loud):
            first, last = a + int(loud[0]), a + int(loud[-1])
            if (first - a) * hop > 0.2:
                w["s"] = _r3(max(w["s"], first * hop - 0.04))
                moved += 1
            if (b - 1 - last) * hop > 0.2:
                w["e"] = _r3(min(w["e"], (last + 1) * hop + 0.06))
                moved += 1
            w["e"] = max(w["e"], _r3(w["s"] + 0.04))
        out.append(w)
    return out, moved


def silences(video, noise_db, min_dur):
    """[(start, end)] of silences from ffmpeg's silencedetect."""
    r = subprocess.run([_ffmpeg(), "-nostdin", "-hide_banner", "-i", video, "-vn", "-af",
                        "silencedetect=noise=%.1fdB:d=%.3f" % (noise_db, min_dur), "-f", "null", "-"],
                       capture_output=True, text=True)
    out, start = [], None
    for line in r.stderr.splitlines():
        m = re.search(r"silence_start:\s*(-?[\d.]+)", line)
        if m:
            start = max(0.0, float(m.group(1)))
            continue
        m = re.search(r"silence_end:\s*([\d.]+)", line)
        if m and start is not None:
            out.append((start, float(m.group(1))))
            start = None
    if start is not None:
        out.append((start, None))
    return out


def _quietest(env, a, b, default):
    """Time of the quietest 10 ms frame in [a, b] (default when there is no audio or no room)."""
    if env is None or b - a < 0.02:
        return default
    hop = env["hop"]
    i0, i1 = int(math.ceil(a / hop)), int(b / hop)
    seg = env["db"][i0:i1 + 1]
    if not len(seg):
        return default
    # prefer the frame nearest the planned point among the quiet ones
    k = np.argmin(seg + 0.05 * np.abs(np.arange(i0, i0 + len(seg)) * hop - default) / hop)
    return (i0 + int(k)) * hop


# =========================================================================== words: sentences, fillers, retakes

_SENT_END = re.compile(r"[.!?\u2026]['\"\u201d\u2019)]*$")   # a word that ends a sentence ("done.", "why?\"")

def sentences(words, idx=None, pause=0.45, max_words=26):
    """Index lists, one per sentence: punctuation when the transcript has it, else pauses; long ones split."""
    idx = list(range(len(words))) if idx is None else list(idx)
    if not idx:
        return []
    punct = sum(1 for i in idx if _SENT_END.search(words[i]["w"])) >= max(2, len(idx) // 40)
    out, cur = [], []
    for k, i in enumerate(idx):
        cur.append(i)
        gap = words[idx[k + 1]]["s"] - words[i]["e"] if k + 1 < len(idx) else 99.0
        if punct:
            end = bool(_SENT_END.search(words[i]["w"]))
        else:
            end = gap >= pause
        if end or gap >= 1.0 or (len(cur) >= max_words and (gap >= 0.2 or words[i]["w"].endswith(","))):
            out.append(cur)
            cur = []
    if cur:
        out.append(cur)
    return out


def mark_fillers(words):
    """{index: ("FILLER", why)} for hesitations, isolated soft fillers and stutters."""
    n = len(words)
    toks = [_tok(w["w"]) for w in words]

    # a hesitation next to a word counts as a pause ("so, um, ..." loses both)
    def pause_before(i):
        return i == 0 or words[i]["s"] - words[i - 1]["e"] >= 0.25 or toks[i - 1] in HARD_FILLERS

    def pause_after(i):
        return i == n - 1 or words[i + 1]["s"] - words[i]["e"] >= 0.25 or toks[i + 1] in HARD_FILLERS

    out = {}
    for i, t in enumerate(toks):
        if i in out:
            continue
        if t in HARD_FILLERS:
            out[i] = ("FILLER", '"%s"' % _bare(words[i]["w"]))
        elif t in SOFT_FILLERS and pause_before(i) and pause_after(i):
            out[i] = ("FILLER", '"%s" between pauses' % _bare(words[i]["w"]))
        elif i + 1 < n and (t, toks[i + 1]) in FILLER_PAIRS and pause_before(i) and pause_after(i + 1):
            why = '"%s %s" between pauses' % (_bare(words[i]["w"]), _bare(words[i + 1]["w"]))
            out[i] = out[i + 1] = ("FILLER", why)
        elif (i + 1 < n and t in STUTTER and toks[i + 1] == t and words[i + 1]["s"] - words[i]["e"] < 0.6
              and not words[i]["w"].rstrip().endswith((".", "!", "?"))):
            out[i] = ("FILLER", 'stutter "%s %s"' % (_bare(words[i]["w"]), _bare(words[i + 1]["w"])))
    return out


def _hesitation(words, prev, nxt, drop):
    """True when the speaker hesitated between words prev and nxt (pause, dropped filler, cut-off mark).

    A comma is not a hesitation: it closes a clause ("if you want views, if you want followers").
    """
    gap = words[nxt]["s"] - words[prev]["e"]
    if gap >= 0.3:
        return True
    if any(i in drop for i in range(prev + 1, nxt)):
        return True
    return words[prev]["w"].rstrip().endswith(CUTOFF)


def _parallel(toks, i, j, m, window):
    """True when the opening toks[j:j+m] starts three or more phrases that each go on differently.

    That is a spoken list ("if you want views if you want followers if you
    want money"), even in captions without punctuation; three takes of one
    flubbed phrase restart where the last one began ("my first tip my
    first tip my first tip is hooks").
    """
    opening = toks[j:j + m]
    starts = [k for k in range(max(0, i - window), min(len(toks) - m, j + window) + 1) if toks[k:k + m] == opening]
    if len(starts) < 3:
        return False
    nexts = [toks[k + m] if k + m < len(toks) else "" for k in starts]
    return len(set(nexts)) == len(nexts) and opening[0] not in nexts and "" not in nexts[:-1]


def mark_retakes(words, drop, window=30):
    """{index: ("RETAKE", why)} for false starts: a phrase restarted with the same 3+ words keeps the last take.

    The restart must follow a hesitation (a pause of 0.3 s or more, a
    dropped filler, or a cut-off mark: dash or ellipsis), or repeat the
    whole unfinished first take back to back ("the first mistake is the
    first mistake is lighting..."). A first take that is a complete
    sentence, or a clause closed by a comma, counts only when what followed
    its opening comes back too, so deliberate repetition and parallel
    phrasing stay ("We will fight on the beaches. We will fight on the
    landing grounds.", "if you want views, if you want followers, ...",
    "I love you, I love you"). A comma after a word no phrase ends with
    ("the best way to, the best way to grow") does not close anything.
    An unfinished first take counts when it was cut short (at most two
    words past the opening), is mostly repeated, or a long pause separates
    them; three or more phrases with one opening that each go on
    differently are a list and stay.
    """
    seq = [i for i in range(len(words)) if i not in drop]
    toks = [_tok(words[i]["w"]) for i in seq]
    out = {}
    j = 3
    while j < len(seq):
        found = None
        last1 = words[seq[j - 1]]["w"].rstrip()
        complete = bool(_SENT_END.search(last1))
        clause = not complete and last1.endswith(CLAUSE_END) and toks[j - 1] not in OPEN_ENDED
        for i in range(j - 3, max(-1, j - window - 1), -1):
            if seq[i] in out or not toks[i]:
                continue
            m = 0
            while j + m < len(seq) and i + m < j and toks[i + m] and toks[i + m] == toks[j + m]:
                m += 1
            # the same unfinished phrase twice in a row, mid-sentence, is a restart even without a pause
            back_to_back = j - i == m and not complete and not clause
            if m < 3 or not (back_to_back or _hesitation(words, seq[j - 1], seq[j], drop)):
                continue
            take1 = toks[i:j]
            # how much of what followed the opening the first time comes back the second time
            rest1 = set(take1[m:])
            rest2 = set(toks[j + m:j + m + len(rest1) + 2])
            sim = len(rest1 & rest2) / float(len(rest1)) if rest1 else 1.0
            gap = words[seq[j]]["s"] - words[seq[j - 1]]["e"]
            if complete or clause:
                ok = sim >= 0.6
            else:
                ok = (len(take1) <= m + 2 or sim >= 0.5 or gap >= 0.8) and (
                    not rest1 or sim >= 0.5 or not _parallel(toks, i, j, m, window))
            if ok:
                found = (i, m)
                break
        if found:
            i, m = found
            why = 'first take of "%s" (said again at %.2f s)' % (
                " ".join(_bare(words[seq[k]]["w"]) for k in range(j, j + m)), words[seq[j]]["s"])
            for k in range(i, j):
                out[seq[k]] = ("RETAKE", why)
        j += 1
    return out


# =========================================================================== numbers

_YEAR = re.compile(r"^(19|20)\d\d$")
_NUM_RE = re.compile(r"^(?P<cur>[$\u20ac\u00a3\u20b9])?(?P<num>\d{1,3}(?:,\d{3})+|\d+)(?:\.(?P<dec>\d+))?"
                     r"(?P<mag>[kKmMbB]|bn)?(?P<pct>%)?(?P<x>[xX])?$")
_ORD_RE = re.compile(r"^(\d+)(st|nd|rd|th)$")
# currency words after a number ("500 euros"); "pounds" is left out, it is as often a weight
CURRENCY_WORDS = {"dollars": "$", "dollar": "$", "bucks": "$", "usd": "$", "euros": "\u20ac", "euro": "\u20ac",
                  "eur": "\u20ac", "quid": "\u00a3", "gbp": "\u00a3", "rupees": "\u20b9", "rupee": "\u20b9",
                  "inr": "\u20b9"}


def _fmt(value, decimals=0, sep=","):
    s = "{:,.{d}f}".format(value, d=decimals)
    return s.replace(",", sep) if sep != "," else s


def _short_money(v):
    if v >= 1e9:
        return "%sB" % ("%.1f" % (v / 1e9)).rstrip("0").rstrip(".")
    if v >= 1e6:
        return "%sM" % ("%.1f" % (v / 1e6)).rstrip("0").rstrip(".")
    return _fmt(v, 0 if float(v).is_integer() else 2)


def number_spans(words):
    """Numbers spoken in a list of word dicts.

    Each: {"a", "b" (word indices, inclusive), "value", "kind", "display",
    "prefix", "suffix", "decimals", "unit"}; kind is percent, money,
    multiplier, count, year or ordinal. Understands digits ("$4.2M", "73%",
    "10,000", "3x", "2nd") and number words ("seventy three percent",
    "ten thousand dollars", "a million"). Money keeps the currency that was
    said: $, \u20ac, \u00a3 or \u20b9 before the digits, or a currency word after the
    number (CURRENCY_WORDS: "500 euros" is \u20ac500).
    """
    toks = [_tok(w["w"]) for w in words]
    raws = [_bare(w["w"]) for w in words]
    spans = []
    i, n = 0, len(words)
    while i < n:
        raw, t = raws[i], toks[i]
        value, decimals, cur, kind, j = None, 0, "", None, i + 1
        m = _NUM_RE.match(raw.replace(" ", ""))
        mo = _ORD_RE.match(t)
        if mo:
            spans.append({"a": i, "b": i, "value": float(mo.group(1)), "kind": "ordinal", "display": raw,
                          "prefix": "", "suffix": "", "decimals": 0, "unit": ""})
            i += 1
            continue
        if m:
            value = float(m.group("num").replace(",", ""))
            if m.group("dec"):
                value += float("0." + m.group("dec"))
                decimals = len(m.group("dec"))
            mag = (m.group("mag") or "").lower()
            value *= {"k": 1e3, "m": 1e6, "b": 1e9, "bn": 1e9}.get(mag, 1)
            cur = m.group("cur") or ""
            if m.group("pct"):
                kind = "percent"
            elif m.group("x"):
                kind = "multiplier"
        elif t in UNITS or t in TENS or (t in ("a", "an") and i + 1 < n and toks[i + 1] in SCALES
                                          and toks[i + 1] != "k"):
            total, current, k, any_num = 0.0, 0.0, i, False
            while k < n:
                tk = toks[k]
                if tk in UNITS or tk in TENS:
                    current += UNITS.get(tk, TENS.get(tk, 0))
                    any_num = True
                elif tk in ("a", "an") and k == i:
                    current = 1.0
                elif tk == "hundred" and (any_num or current):
                    current = (current or 1) * 100
                elif tk in ("thousand", "million", "billion") and (any_num or current):
                    total += (current or 1) * SCALES[tk]
                    current = 0.0
                elif tk == "and" and k + 1 < n and (toks[k + 1] in UNITS or toks[k + 1] in TENS) and any_num:
                    pass
                else:
                    break
                k += 1
                if re.search(r"[.!?,;:]$", words[k - 1]["w"].strip()):
                    break   # punctuation ends a spoken number
            value = total + current
            j = k
            if j == i or (not any_num and value == 0):
                i += 1
                continue
        else:
            i += 1
            continue
        # scale and unit words after the number
        if j < n and toks[j] in ("thousand", "million", "billion", "k") and m:
            value *= SCALES[toks[j]]
            j += 1
        if float(value).is_integer():
            decimals = 0            # "1.5 million" is 1,500,000
        unit = ""
        if j < n and kind is None:
            u = toks[j]
            if u in ("percent", "per") or raws[j] == "%":
                kind = "percent"
                j += 2 if u == "per" and j + 1 < n and toks[j + 1] == "cent" else 1
            elif u in CURRENCY_WORDS:
                cur, j = cur or CURRENCY_WORDS[u], j + 1
            elif u in ("x", "times") and value >= 2:
                kind, j = "multiplier", j + 1
        if j < n:
            unit = _stem(toks[j])
        if kind is None:
            if cur:
                kind = "money"
            elif m and float(value).is_integer() and 1900 <= value <= 2100 and not m.group("mag"):
                kind = "year"
            else:
                kind = "count"
        if kind == "percent":
            display, prefix, suffix = "%s%%" % _fmt(value, decimals), "", "%"
        elif kind == "multiplier":
            display, prefix, suffix = "%sx" % _fmt(value, decimals), "", "x"
        elif kind == "money":
            display, prefix, suffix = cur + _short_money(value), cur, ""
        elif kind == "year":
            display, prefix, suffix = "%d" % value, "", ""
        else:
            display, prefix, suffix = _fmt(value, decimals), "", ""
        spans.append({"a": i, "b": j - 1, "value": value, "kind": kind, "display": display, "prefix": prefix,
                      "suffix": suffix, "decimals": decimals, "unit": unit, "digits": bool(m)})
        i = max(j, i + 1)
    return spans


# =========================================================================== cutting

def keep_ranges(words, drop, duration, E, env=None, end_tail=END_TAIL):
    """Kept source ranges and the cuts between them.

    Returns (ranges [[in, out]], between [cut between range k and k+1],
    head cut or None, tail cut or None). Each cut: {"kind", "in", "out",
    "why"}.
    """
    kept = [i for i in range(len(words)) if i not in drop]
    if not kept:
        return [[0.0, duration]], [], None, None
    first, last = words[kept[0]], words[kept[-1]]
    # words dropped before the first kept one (a first take, a filler) go with the head cut, so it ends after them
    lo = words[kept[0] - 1]["e"] if kept[0] > 0 else 0.0
    start = max(0.0, first["s"] - min(LEAD_IN, max(0.0, first["s"] - lo)))
    head = None
    if start > 0.05:
        start = _quietest(env, max(lo, start - 0.03), min(first["s"] - 0.02, start + 0.03), start)
        kind, why = _drop_reason(range(kept[0]), drop, "silence before the first word")
        head = {"kind": kind, "in": 0.0, "out": _r3(start), "why": why}
    else:
        start = 0.0
    ranges, between = [], []
    cur = start
    for a, b in zip(kept, kept[1:]):
        wa, wb = words[a], words[b]
        gap = wb["s"] - wa["e"]
        dropped = list(range(a + 1, b))
        if dropped:
            d0, d1 = words[dropped[0]]["s"], words[dropped[-1]]["e"]
            g1, g2 = max(0.0, d0 - wa["e"]), max(0.0, wb["s"] - d1)
            c0, c1 = wa["e"] + min(E["tail"], g1), wb["s"] - min(E["lead"], g2)
            kind, why = _drop_reason(dropped, drop)
        elif gap > E["gap"]:
            c0, c1 = wa["e"] + E["tail"], wb["s"] - E["lead"]
            kind, why = "DEAD", "%.2f s pause" % gap
        else:
            continue
        if c1 - c0 < 0.04:
            continue
        c0 = _quietest(env, max(wa["e"], c0 - 0.03), min(c0 + 0.03, c1 - 0.02), c0)
        c1 = _quietest(env, max(c0 + 0.02, c1 - 0.03), min(wb["s"], c1 + 0.03), c1)
        if c1 - c0 < 0.04:
            continue
        ranges.append([_r3(cur), _r3(c0)])
        between.append({"kind": kind, "in": _r3(c0), "out": _r3(c1), "why": why})
        cur = c1
    after = list(range(kept[-1] + 1, len(words)))
    # the breath after the last kept word stops where a dropped word after it begins
    end = min(duration, last["e"] + (min(end_tail, max(0.0, words[after[0]]["s"] - last["e"])) if after
                                     else end_tail))
    tail = None
    if duration - end > 0.05:
        kind, why = _drop_reason(after, drop, "silence after the last word")
        tail = {"kind": kind, "in": _r3(end), "out": _r3(duration), "why": why}
    else:
        end = duration
    ranges.append([_r3(cur), _r3(end)])
    return ranges, between, head, tail


_RANK = {"RETAKE": 3, "TRIM": 2, "FILLER": 1}


def _drop_reason(dropped, drop, silence=None):
    """(kind, why) of a cut that removes the words `dropped`: the weightiest kind, its reasons first.

    With no words in it the cut is ("DEAD", silence).
    """
    dropped = [i for i in dropped if i in drop]
    if not dropped:
        return "DEAD", silence
    kind = max((drop[i][0] for i in dropped), key=lambda k: _RANK.get(k, 0))
    whys = []
    for i in sorted(dropped, key=lambda i: drop[i][0] != kind):   # the main reason first
        if drop[i][1] not in whys:
            whys.append(drop[i][1])
    return kind, "; ".join(whys[:3]) + (" (+%d more)" % (len(whys) - 3) if len(whys) > 3 else "")


def merge_short(ranges, between, min_len=MIN_SEGMENT, max_undo=0.7):
    """Undo small pause cuts next to kept pieces shorter than min_len (a 0.2 s shot reads as a glitch)."""
    ranges, between = [list(r) for r in ranges], [dict(c) for c in between]
    k = 0
    while k < len(ranges):
        if ranges[k][1] - ranges[k][0] >= min_len or len(ranges) == 1:
            k += 1
            continue
        options = []
        if k > 0 and between[k - 1]["kind"] == "DEAD" and between[k - 1]["out"] - between[k - 1]["in"] <= max_undo:
            options.append((between[k - 1]["out"] - between[k - 1]["in"], k - 1))
        if k < len(between) and between[k]["kind"] == "DEAD" and between[k]["out"] - between[k]["in"] <= max_undo:
            options.append((between[k]["out"] - between[k]["in"], k))
        if not options:
            k += 1
            continue
        _, c = min(options)
        ranges[c][1] = ranges[c + 1][1]   # join ranges c and c+1 across cut c
        del ranges[c + 1]
        del between[c]
        k = max(0, c)
    return ranges, between


def sentence_value(ws):
    """How much a sentence carries: numbers, emphasis words, keywords, questions (0 = filler talk)."""
    toks = [_tok(w["w"]) for w in ws]
    nums = [s for s in number_spans(ws) if s["kind"] not in ("ordinal",)]
    v = 3.0 * min(1, len(nums))
    v += 1.5 if any(_is_superlative(t) or t in NEGATIONS for t in toks) else 0.0
    v += 0.75 * min(4, len(keywords(ws)))
    v += 1.0 if ws and ws[-1]["w"].rstrip().endswith("?") else 0.0
    return v


def detect_cta(words, sents):
    """(sentence position, word index, verb, icon) of a call to action in the last two sentences, or None."""
    for p in range(len(sents) - 1, max(-1, len(sents) - 3), -1):
        idx = sents[p]
        toks = [_tok(words[i]["w"]) for i in idx]
        for k, t in enumerate(toks):
            if t not in CTA_VERBS:
                continue
            nxt = toks[k + 1] if k + 1 < len(toks) else ""
            if CTA_NEXT[t] is not None and nxt not in CTA_NEXT[t]:
                continue
            if k == 0 or toks[k - 1] in CTA_LEAD:
                return p, idx[k], t, CTA_VERBS[t]
    return None


# =========================================================================== geometry

def canvas_for(aspect, info, size=None):
    """(W, H) of the output: an ASPECTS name, 'source' (the source size, long side at most 1920) or a size."""
    if size:
        return _even(size[0]), _even(size[1])
    if aspect in ASPECTS:
        return ASPECTS[aspect]
    sw, sh = info.get("width") or 1080, info.get("height") or 1920
    k = min(1.0, 1920.0 / max(sw, sh))
    return _even(sw * k), _even(sh * k)


def safe_zone(W, H):
    """(left, top, right, bottom) px of the platform safe zone (same rules as templates.py)."""
    ratio = H / float(W)
    if ratio >= 1.5:
        top, bottom, side = 0.11, 0.20, 0.06
    elif ratio < 0.95:
        top, bottom, side = 0.08, 0.12, 0.05
    else:
        top, bottom, side = 0.07, 0.12, 0.06
    return W * side, H * top, W * (1 - side), H * (1 - bottom)


def cover_map(src, canvas, focus):
    """Cover-fit geometry of a source in a canvas around `focus`: (scale, crop x, crop y)."""
    sw, sh = src
    W, H = canvas
    s = max(W / float(sw), H / float(sh))
    scw, sch = sw * s, sh * s
    ox = max(0.0, min(scw - W, focus[0] * scw - W / 2.0))
    oy = max(0.0, min(sch - H, focus[1] * sch - H / 2.0))
    return s, ox, oy


def default_captions(W, H, energy):
    """A strong caption style for the output shape (captions.py keys)."""
    ratio = H / float(W)
    if ratio >= 1.5:
        size, y, width = 4.8, 70.0, 80.0
    elif ratio < 0.95:
        size, y, width = 6.4, 82.0, 70.0
    else:
        size, y, width = 5.4, 76.0, 80.0
    return {"mode": "word-by-word", "words_per_card": {"median": 4 if energy == "calm" else 3,
                                                       "max": 4 if energy == "calm" else 3},
            "lines_per_card": 1, "case": "none" if energy == "calm" else "upper", "font_size_pct": size,
            "center_x_pct": 50.0, "center_y_pct": y, "max_width_pct": width, "align": "center", "fill": "#FFFFFF",
            "highlight": "#FFE500", "outline": "#000000", "outline_pct": 0.4, "box": None,
            "font_candidates": ["Montserrat Black", "Inter Black"]}


def caption_style(style):
    """Caption style from a style_dna.json path or a dict, filtered to the keys captions.py reads."""
    if isinstance(style, dict):
        st = style
    else:
        st = paths.load_skill("captions").caption_style(style)
    return {k: copy.deepcopy(v) for k, v in st.items() if k in timeline.CAPTION_STYLE_KEYS}


def caption_band(style, H):
    """(top, bottom) px the captions occupy (with margin for outline, box and the pop)."""
    size = float(style.get("font_size_pct") or 4.6) / 100.0 * H * 1.2
    lines = max(1, int(style.get("lines_per_card") or 1))
    half = size * 0.62 * lines + (float(style.get("outline_pct") or 0) / 100.0 * H)
    if style.get("box"):
        half += size * 0.3
    half *= 1.12
    cy = float(style.get("center_y_pct") or 68) / 100.0 * H
    return cy - half, cy + half


def fit_captions(style, segs, W, H, E):
    """(style, note): the caption style moved off the speaker's face when its band would cover it.

    Only the vertical position changes (just under the chin, else above the
    head), and only when there is room in the safe zone; the face is taken
    at its typical (median) place and largest planned zoom.
    """
    rects = []
    for g in segs:
        if not g.get("face"):
            continue
        k = g["k"] * min(E["zoom"], MAX_TOTAL_ZOOM / g["k"])
        Fx, Fy = g["F"]
        rects.append((Fy + k * (g["face"][1] - Fy), Fy + k * (g["face"][3] - Fy)))
    if not rects:
        return style, None
    fy0, fy1 = float(np.median([r[0] for r in rects])), float(np.median([r[1] for r in rects]))
    top, bottom = caption_band(style, H)
    if bottom <= fy0 or top >= fy1:
        return style, None
    half, m = (bottom - top) / 2.0, 0.02 * H
    _, T, _, B = safe_zone(W, H)
    if fy1 + m + 2 * half <= B:
        cy = fy1 + m + half
    elif fy0 - m - 2 * half >= T:
        cy = fy0 - m - half
    else:
        return style, "the captions cover part of the face: there is no room above or below it"
    old = float(style.get("center_y_pct") or 68)
    new = round(100.0 * cy / H, 1)
    return dict(style, center_y_pct=new), ("captions moved from %g %% to %g %% of the height so they do not cover "
                                           "the face" % (old, new))


# =========================================================================== template placement

def _num_text(L):
    v = float(L.get("to") or 0)
    d = int(L.get("decimals") or 0)
    sep = L.get("separator") if L.get("separator") is not None else ","
    return "%s%s%s" % (L.get("prefix") or "", _fmt(v, d, sep or ","), L.get("suffix") or "")


def extent(layers, W, H):
    """(x0, y0, x1, y1) px covered by template layers at rest (mid-life), or None."""
    if not layers:
        return None
    s = timeline.normalize({"version": 1, "width": W, "height": H, "layers": copy.deepcopy(layers)})
    box = [1e9, 1e9, -1e9, -1e9]
    for L in s["layers"]:
        if not isinstance(L, dict):
            continue
        t = L.get("type")
        mid = (float(L["end"]) - float(L["start"])) / 2.0
        an = L.get("animate") or {}
        x = anim.value_at(an.get("x"), mid, float(L["x"]))
        y = anim.value_at(an.get("y"), mid, float(L["y"]))
        sc = anim.value_at(an.get("scale"), mid, float(L.get("scale") or 1.0))
        if t in ("text", "counter"):
            L2 = dict(L, type="text", text=_num_text(L) if t == "counter" else L["text"])
            _, b = textrender.sprite(L2, 0.25)
            w, h = (b[2] - b[0]) / 0.25, (b[3] - b[1]) / 0.25
            st = (L.get("stroke") or {}).get("width") or 0
            w, h = w + 2 * st, h + 2 * st
        elif t in ("shape", "progress", "image"):
            w, h = L.get("w"), L.get("h")
            if not isinstance(w, (int, float)) or not isinstance(h, (int, float)):
                continue
        else:
            continue
        ax, ay = timeline.ANCHORS.get(L.get("anchor"), (0.5, 0.5))
        x0, y0 = x - ax * w * sc, y - ay * h * sc
        box = [min(box[0], x0), min(box[1], y0), max(box[2], x0 + w * sc), max(box[3], y0 + h * sc)]
    return tuple(box) if box[0] < box[2] else None


def shift_layers(layers, dx, dy, default_xy):
    """Move template layers by (dx, dy) px, keyframed positions included."""
    for L in layers:
        for key, d, dflt in (("x", dx, default_xy[0]), ("y", dy, default_xy[1])):
            if not isinstance(L.get(key), (int, float)):
                L[key] = dflt
            L[key] = round(float(L[key]) + d, 2)
            keys = (L.get("animate") or {}).get(key)
            if keys:
                L["animate"][key] = [[k[0], round(float(k[1]) + d, 2)] + list(k[2:]) for k in keys]
    return layers


def _gaps(lo, hi, blocks):
    """[(top, bottom, block above or None)] parts of [lo, hi] not covered by blocks [(top, bottom, name)]."""
    out, cur, above = [], lo, None
    for top, bottom, name in sorted(blocks):
        if top > cur:
            out.append((cur, min(top, hi), above))
        if bottom > cur:
            cur, above = bottom, name
    if cur < hi:
        out.append((cur, hi, above))
    return [g for g in out if g[1] > g[0]]


def free_bands(W, H, face, cap, wide):
    """Rectangles of the safe zone a graphic may use: away from the face rect and the caption band.

    Returns [(name, x0, y0, x1, y1)]. Full-width bands are named by what is
    above them: "top" (nothing: above the face and captions), "middle" (just
    under the face), "low" (under the captions). Wide frames also get "left"
    and "right" of the face, as tall as the room above or below the captions.
    """
    L, T, R, B = safe_zone(W, H)
    m = 0.015 * H
    blocks = []
    if face:
        blocks.append((face[1] - m, face[3] + m, "face"))
    if cap:
        blocks.append((cap[0] - m, cap[1] + m, "captions"))
    names = {None: "top", "face": "middle", "captions": "low"}
    out = [(names[above], L, y0, R, y1) for y0, y1, above in _gaps(T, B, blocks)]
    if face and wide:
        room = max(_gaps(T, B, blocks[1:]), key=lambda g: g[1] - g[0], default=None)
        if room:
            out.append(("left", L, room[0], min(R, face[0] - m), room[1]))
            out.append(("right", max(L, face[2] + m), room[0], R, room[1]))
    return [b for b in out if b[3] - b[1] >= 0.05 * W and b[4] - b[2] >= 0.05 * H]


def _text_px(layers):
    """Largest text size in a layer list (px): how big a graphic reads on screen."""
    sizes = [float(L.get("size") or 0) * float(L.get("scale") or 1) for L in layers or []
             if L.get("type") in ("text", "counter")]
    return max(sizes) if sizes else 0.0


def place_template(name, params, start, end, canvas, style, bands, prefer=(), scales=(1.0, 0.86, 0.74, 0.64),
                   compact=None):
    """Build a template scaled to fit the best free band; (layers, info) or (None, reason).

    The template is built on a virtual canvas k times the frame (a
    portrait one for side bands), measured, and moved into the band. A
    bigger scale wins; `prefer` band names win ties. `compact` is a
    lighter version of the params (a stat without its label) used when it
    fits clearly bigger than the full one, so text never ends up tiny.
    """
    W, H, fps = canvas["width"], canvas["height"], canvas.get("fps", 30)
    variants = [(params, 12.0)] + ([(compact, 0.0)] if compact else [])
    built = {}
    refs = {}  # variant -> its largest text size built on the whole frame
    best = None
    for vi, (prm, bonus) in enumerate(variants):
        for ki, k in enumerate(scales):
            best_rel = 0.0
            for band in bands:
                bname, x0, y0, x1, y1 = band
                bw, bh = x1 - x0, y1 - y0
                if bname in ("left", "right"):
                    vw, vh = _even(bw * k), _even(bw * k * 16 / 9.0)
                else:
                    vw, vh = _even(W * k), _even(H * k)
                key = (vi, vw, vh)
                if key not in built:
                    try:
                        lay = templates.build(name, prm, start, end, {"width": vw, "height": vh, "fps": fps}, style)
                    except (KeyError, ValueError):
                        return None, "template %s could not be built" % name
                    built[key] = (lay, extent(lay, vw, vh) if lay else None)
                lay, ext = built[key]
                if not lay or not ext:
                    return None, "nothing to draw"
                ew, eh = (ext[2] - ext[0]) * 1.04, (ext[3] - ext[1]) * 1.06   # room for overshoot and drift
                if ew > bw or eh > bh:
                    continue
                # how big it reads on screen, relative to the template on the whole frame: k for full-width
                # bands; a side band's narrow portrait canvas makes everything smaller than its k says
                rel = k
                if bname in ("left", "right"):
                    if vi not in refs:
                        refs[vi] = _text_px(templates.build(name, prm, start, end,
                                                            {"width": W, "height": H, "fps": fps}, style))
                    px = _text_px(lay)
                    rel = px / refs[vi] if refs[vi] and px else k * vw / float(W)
                score = rel * 100 + bonus + (5 if bname in prefer else 0) - (prefer.index(bname) if bname in prefer
                                                                              else 3)
                best_rel = max(best_rel, rel)
                if best is None or score > best[0]:
                    best = (score, rel, band, key, ext, vi)
            if ki + 1 < len(scales) and best_rel >= scales[ki + 1]:
                break   # no smaller scale of this variant can read bigger than this fit
    if best is None:
        return None, "no room beside the face and captions"
    _, rel, band, key, ext, vi = best
    lay = copy.deepcopy(built[key][0])
    bname, x0, y0, x1, y1 = band
    dx = (x0 + x1) / 2.0 - (ext[0] + ext[2]) / 2.0
    dy = (y0 + y1) / 2.0 - (ext[1] + ext[3]) / 2.0
    shift_layers(lay, dx, dy, (key[1] / 2.0, key[2] / 2.0))
    return lay, {"band": bname, "scale": round(rel, 2), "compact": vi == 1,
                 "box": [round(ext[0] + dx), round(ext[1] + dy), round(ext[2] + dx), round(ext[3] + dy)]}


def landing(layers):
    """Seconds when a graphic visibly lands (its first entrance half done)."""
    best = None
    for L in layers:
        en = L.get("enter") or {}
        t = float(L.get("start") or 0) + float(en.get("delay") or 0) + 0.5 * float(en.get("duration") or 0)
        best = t if best is None else min(best, t)
    return best or 0.0


# =========================================================================== speech -> graphics

def _sentence_case(text):
    """First letter capitalized, the rest as spoken ("you post late" -> "You post late")."""
    return text[:1].upper() + text[1:] if text else text


def _phrase(words, a, b, max_words=6):
    """Up to max_words display words from words[a:b], stopping at punctuation, without a dangling last word."""
    out = []
    for w in words[a:b]:
        out.append(_bare(w["w"]))
        if len(out) >= max_words or re.search(r"[.!?;:]['\"\u201d]*$", w["w"]):
            break
    while out and _tok(out[-1]) in DANGLING:
        out.pop()
    return " ".join(x for x in out if x)


def _label(ws, span):
    """What a number means: the words after it (or before it when too few follow)."""
    after = _phrase(ws, span["b"] + 1, len(ws), 7)
    if len(keywords(after.split())) >= 1 and len(after.split()) >= 2:
        return after
    before = [_bare(w["w"]) for w in ws[max(0, span["a"] - 6):span["a"]]]
    while before and _tok(before[0]) in STOP | LEAD_WORDS:
        before.pop(0)
    return " ".join(before) or after


def _hook_score(lt, a, total):
    """How good the words lt (starting at word a of `total`) are as a hook title."""
    n = len(lt)
    nums = [q for q, t in enumerate(lt) if any(c.isdigit() for c in t) and not _YEAR.match(t)]
    strong = sorted(set(nums) | {q for q, t in enumerate(lt) if _is_superlative(t) or t in NEGATIONS})
    sc = sum(1.0 for t in lt if _content(t) or _YEAR.match(t)) + 2.0 * len(nums)   # meaning and numbers
    sc += 1.5 * len(strong) + (1.0 if strong and strong[0] <= 1 else 0.0)         # opens on the strong word
    sc -= 1.5 * (lt[0] in DANGLING and lt[0] not in ("the", "my", "your", "this", "i", "you", "we"))
    sc -= 1.5 * (lt[-1] in DANGLING)                                              # "... beginners make on"
    sc -= 2.0 * (bool(nums) and nums[-1] == n - 1 and a + n < total)              # "in 7" without "days"
    sc -= 1.0 * bool(_YEAR.match(lt[0]))
    return sc + 0.15 * n - 0.4 * a / float(total)                                 # longer, earlier


def hook_phrase(ws, max_words=7):
    """The key phrase of the opening sentence for the hook title, and the words to highlight.

    A sentence of up to max_words + 1 words is used whole (after leading
    "so / okay / guys"); a longer one gives its best 3..max_words window.
    """
    toks = [_bare(w["w"]) for w in ws]
    k = 0
    while k < len(toks) - 2 and _tok(toks[k]) in LEAD_WORDS:
        k += 1
    toks = [t for t in toks[k:] if t]
    if len(toks) > max_words + 1:
        windows = [(a, n) for n in range(3, max_words + 1) for a in range(0, len(toks) - n + 1)]
        a, n = max(windows, key=lambda w: _hook_score([_tok(t) for t in toks[w[0]:w[0] + w[1]]], w[0], len(toks)))
        toks = toks[a:a + n]
    text = " ".join(toks).strip()
    return text, hook_highlight(text)


def hook_highlight(text):
    """Words of a title to paint in the accent: numbers, else superlatives/negations, else the longest content word."""
    toks = [t for t in text.split() if t]
    nums = [t for t in toks if any(c.isdigit() for c in t) and not _YEAR.match(_tok(t))]
    if nums:
        return nums[:2]
    strong = [t for t in toks if _is_superlative(_tok(t)) or _tok(t) in NEGATIONS]
    if strong:
        return strong[:1]
    content = sorted((t for t in toks if _content(_tok(t))), key=lambda t: -len(t))
    return content[:1]


def _callout(t, s_end, template, priority, params, why, **extra):
    d = {"t": t, "s_end": s_end, "template": template, "priority": priority, "params": params, "why": why}
    d.update(extra)
    return d


def _number_callouts(S):
    """stat / counter_reveal for the numbers of a sentence that are worth showing."""
    ws, out = S["words"], []
    for sp in number_spans(ws):
        v, kind = sp["value"], sp["kind"]
        t, label = ws[sp["a"]]["s"], _label(ws, sp)
        if kind in ("percent", "multiplier"):
            out.append(_callout(t, S["e"], "stat", 4, {"value": sp["display"], "label": _sentence_case(label)},
                                sp["display"]))
        elif kind == "money" or (kind == "count" and v >= 100):
            params = {"value": v, "prefix": sp["prefix"], "suffix": sp["suffix"],
                      "decimals": sp["decimals"] if v < 1e6 else 0, "label": _sentence_case(label)}
            out.append(_callout(t, S["e"], "counter_reveal", 4, params, sp["display"]))
        elif kind == "count" and 2 <= v < 100 and sp["unit"] in COUNT_UNITS and (sp.get("digits") or v >= 10):
            # small numbers only with a unit ("3 seconds", "12 tips"), so "two kids" stays plain speech
            out.append(_callout(t, S["e"], "stat", 3, {"value": sp["display"], "label": _sentence_case(label)},
                                sp["display"]))
    return out


def _list_callout(S):
    """list_item / step_number for 'step 2', 'number 3', 'tip 1', 'the first mistake', 'Second, ...'."""
    ws = S["words"]
    toks = [_tok(w["w"]) for w in ws]
    n = len(ws)
    for k, t in enumerate(toks):
        num, tpl, after = None, "list_item", k + 1
        if t in ("step", "number", "tip", "mistake", "rule", "reason") and k + 1 < n:
            nxt = toks[k + 1]
            val = float(nxt) if nxt.isdigit() else UNITS.get(nxt)
            if val is not None and 1 <= val <= 20:
                num, after = int(val), k + 2
                tpl = "step_number" if t == "step" else "list_item"
        elif t in ORDINALS:
            pre = [x for x in toks[max(0, k - 2):k] if x not in ("the", "and", "my", "your", "so", "our")]
            if (k <= 2 and not pre) or (k + 1 < n and _stem(toks[k + 1]) in LIST_NOUNS):
                num = ORDINALS[t]
        if num is None:
            continue
        a = after
        while a < n and (_stem(toks[a]) in LIST_NOUNS or toks[a] in ("is", "are", "of", "all", "up", "was")):
            a += 1
        text = _sentence_case(_phrase(ws, a, n, 6))
        if text:
            why = "%s %d" % ("step" if tpl == "step_number" else "point", num)
            return _callout(ws[k]["s"], S["e"], tpl, 5, {"number": num, "text": text}, why, list=True,
                            ordinal=t in ORDINALS)
    return None


def _question_callout(S):
    """question for a short question to the viewer (by its question mark, or its opening when unpunctuated)."""
    ws = S["words"]
    n = len(ws)
    asked = 3 <= n <= 12 and ws[-1]["w"].rstrip().endswith("?")
    if not asked and not S.get("punct") and 3 <= n <= 10:
        asked = tuple(_tok(w["w"]) for w in ws[:2]) in QUESTION_STARTS
    if asked:
        return _callout(ws[0]["s"], S["e"], "question", 2, {"text": _sentence_case(_strip_lead(ws)) + "?"},
                        "question")
    return None


def _side(ws):
    """Up to two content words naming one side of a comparison (stops at punctuation and filler words)."""
    out = []
    for w in ws:
        tk = _tok(w["w"])
        if (_content(tk) or tk.isdigit()) and tk not in COMPARATIVES:
            out.append(_bare(w["w"]))
        elif out:
            break
    return out


def _versus_callout(S):
    """versus for 'X vs Y', 'X versus Y', 'X compared to Y'."""
    ws = S["words"]
    toks = [_tok(w["w"]) for w in ws]
    for k, t in enumerate(toks):
        if t in ("vs", "versus") or (t == "compared" and k + 1 < len(ws) and toks[k + 1] in ("to", "with")):
            before = ws[max(0, k - 3):k]
            # the left side ends at the word before "vs" and must not reach back over punctuation
            for c in range(len(before) - 2, -1, -1):
                if re.search(r"[.!?,;:]$", before[c]["w"]):
                    before = before[c + 1:]
                    break
            after = []
            for w in ws[k + (2 if t == "compared" else 1):k + 4]:
                after.append(w)
                if re.search(r"[.!?,;:]$", w["w"]):
                    break
            left, right = _side(before[::-1])[::-1], _side(after)
            if left and right:
                l, r = " ".join(left[-2:]), " ".join(right[:2])
                return _callout(ws[k]["s"], S["e"], "versus", 3, {"left": l, "right": r}, "%s vs %s" % (l, r))
            return None
    return None


def _quote_callout(S):
    """quote for 3-16 words between quote marks, with the speaker when the sentence says who ('Jobs said')."""
    ws = S["words"]
    for k, w in enumerate(ws):
        if not re.match(r'^["\u201c]', w["w"].strip()):
            continue
        for e in range(k, min(len(ws), k + 16)):
            closes = re.search(r'["\u201d][.!?,]*$', ws[e]["w"].strip()) and (e > k or len(w["w"].strip()) > 2)
            if not closes:
                continue
            if e - k + 1 < 3:
                return None
            pre = [_bare(x["w"]) for x in ws[max(0, k - 4):k]]
            low = [p.lower() for p in pre]
            author = ""
            for verb in ("said", "says", "told"):
                if verb in low:
                    names = [p for p in pre[:low.index(verb)] if p[:1].isupper() and p.lower() not in STOP]
                    author = " ".join(names[-2:])
            return _callout(w["s"], ws[e]["e"], "quote", 1, {"text": _text(ws[k:e + 1]), "author": author}, "quote")
        return None
    return None


def find_callouts(sents):
    """Graphic candidates from the edited speech.

    sents: [{"words": [...output words...], "s", "e", "punct"}]. Returns
    dicts {"t", "s_end", "template", "params", "priority", "why"}; priority
    decides which wins when two want the same moment (lists 5, numbers 4,
    comparisons 3, questions 2, quotes 1).
    """
    out = []
    for S in sents:
        out += _number_callouts(S)
        out += [c for c in (_list_callout(S), _question_callout(S), _versus_callout(S), _quote_callout(S)) if c]
    # ordinal words only make a list when the speech really is one ("first ... second ...")
    if len({c["params"]["number"] for c in out if c.get("ordinal")}) < 2:
        out = [c for c in out if not c.get("ordinal")]
    for tpl in ("list_item", "step_number"):
        nums = sorted({c["params"]["number"] for c in out if c["template"] == tpl})
        if len(nums) >= 2 and nums == list(range(1, len(nums) + 1)):
            for c in out:
                if c["template"] == tpl:
                    c["params"]["total"] = len(nums)
    return out


def _strip_lead(ws):
    toks = [_bare(w["w"]) for w in ws]
    k = 0
    while k < len(toks) - 2 and _tok(toks[k]) in LEAD_WORDS:
        k += 1
    return " ".join(t for t in toks[k:] if t)


def cta_text(ws, k, verb):
    """Button text from a call to action starting at word k: 'FOLLOW FOR PART 2', 'COMMENT "GUIDE"'."""
    if verb == "comment" and k + 1 < len(ws):
        nxt = ws[k + 1]["w"].strip()
        word = _bare(nxt)
        if word and (nxt[:1] in "\"'\u201c" or (word.isupper() and len(word) > 1)):
            return 'COMMENT "%s"' % word.upper()
    text = _phrase(ws, k, len(ws), 5)
    return text or verb.upper()


# =========================================================================== b-roll

def broll_files(folder):
    """[(path, kind 'video'|'image', keywords)] from a b-roll folder, in name order."""
    out = []
    for name in sorted(os.listdir(folder)):
        path = os.path.join(folder, name)
        low = name.lower()
        if not os.path.isfile(path) or name.startswith("."):
            continue
        kind = "video" if low.endswith(VIDEO_EXT) else "image" if low.endswith(IMAGE_EXT) else None
        if not kind:
            continue
        stem = os.path.splitext(low)[0]
        kw = {_stem(t) for t in re.split(r"[^a-z0-9]+", stem) if _content(t)}
        out.append((path, kind, kw))
    return out


# =========================================================================== the plan

def _opts(opts):
    o = {"aspect": "9:16", "size": None, "fps": None, "energy": "auto", "style": None, "broll": None,
         "music": None, "zoom": True, "graphics": True, "sfx": True, "captions": True, "max_duration": None,
         "hook": None, "progress": None, "words_source": None, "notes": [], "download_model": True}
    for k, v in (opts or {}).items():
        if v is not None or k in ("style", "hook", "max_duration"):
            o[k] = v
    return o


def pick_energy(words, requested):
    """(preset, why): the requested one, or from the speaking rate when 'auto'."""
    if requested in ENERGY:
        return requested, "chosen"
    if not words:
        return "medium", "auto: no transcript"
    sents = sentences(words)
    talk = sum(max(0.05, words[s[-1]]["e"] - words[s[0]]["s"]) for s in sents)
    wpm = 60.0 * len(words) / max(talk, 1.0)
    preset = "high" if wpm >= 160 else "medium" if wpm >= 125 else "calm"
    return preset, "auto: %d words per minute while talking" % wpm


def plan_edit(video, words, opts=None):
    """A TIMELINE SPEC (dict) that edits `video` like a short-form editor.

    words: source-time words [{"w", "s", "e"}] or None (silence-based cuts
    only). opts (all optional): aspect ("9:16", "16:9", "1:1", "4:5",
    "source"), size ((W, H) override), fps, energy ("auto", "high",
    "medium", "calm"), style (style_dna.json path or caption style dict),
    broll (folder), music (file), zoom, graphics, sfx, captions (bools),
    max_duration (seconds), hook (title text), progress (bool, default:
    on for portrait videos up to 3 min), words_source (label for the
    report), notes (strings for the report), download_model (fetch the face
    model if missing). What was done and why is in spec["meta"]["edit"]
    (report_md() turns it into EDIT_REPORT.md).
    """
    o = _opts(opts)
    video = os.path.abspath(video)
    info = timeline.probe(video)
    if not info or not info.get("has_video"):
        raise ValueError("%s is not a readable video (ffprobe found no video stream)" % video)
    src_dur = float(info.get("duration") or 0)
    if src_dur <= 0:
        raise ValueError("%s has no duration" % video)
    sw, sh = info["width"], info["height"]
    W, H = canvas_for(o["aspect"], info, o["size"])
    fps = float(o["fps"] or (30 if (info.get("fps") or 30) >= 28 or (info.get("fps") or 30) < 23
                             else round(info["fps"])))
    canvas = {"width": W, "height": H, "fps": fps}
    rep = {"input": {"path": video, "duration": _r3(src_dur), "width": sw, "height": sh,
                     "fps": round(info.get("fps") or 0, 3)},
           "cuts": [], "zooms": [], "graphics": [], "skipped_graphics": [], "broll": [], "broll_unused": [],
           "sfx": [], "not_done": list(o["notes"] or []), "music": None}
    env = audio_env(video) if info.get("has_audio") else None
    if not info.get("has_audio"):
        rep["not_done"].append("the video has no sound: no pause cuts, no loudness-based emphasis")

    words = [w for w in transcribe.clean(words or []) if w["s"] < src_dur]
    for w in words:
        w["e"] = min(w["e"], _r3(src_dur))
    energy, energy_why = pick_energy(words, o["energy"])
    E = ENERGY[energy]
    rep["energy"] = {"preset": energy, "why": energy_why}

    # ------------------------------------------------------------- cuts
    drop, sents_src, cta = {}, [], None
    if words:
        words, moved = refine_words(words, env)
        rep["transcript"] = {"source": o["words_source"] or "words given", "words": len(words), "refined": moved}
        drop.update(mark_fillers(words))
        drop.update(mark_retakes(words, drop))
        kept = [i for i in range(len(words)) if i not in drop]
        if not kept:
            rep["not_done"].append("every word looked like a filler; nothing was cut")
            drop, kept = {}, list(range(len(words)))
        sents_src = sentences(words, kept)
        cta = detect_cta(words, sents_src)
        end_tail = END_TAIL
        if cta and o["graphics"]:
            # give the CTA graphic room to land: up to 1.2 s of picture after the last word
            last_e = words[kept[-1]]["e"]
            end_tail = min(1.2, max(END_TAIL, words[cta[1]]["s"] + 1.8 - last_e))
        ranges, between, head, tail = keep_ranges(words, drop, src_dur, E, env, end_tail)
        if o["max_duration"]:
            ranges, between, head, tail = _trim_to(words, drop, sents_src, cta, float(o["max_duration"]), src_dur,
                                                   E, env, end_tail, rep)
        ranges, between = merge_short(ranges, between)
    else:
        rep["transcript"] = {"source": None, "words": 0, "refined": 0}
        rep["not_done"].append("no transcript: cuts are silence-based only (ffmpeg silencedetect); no captions, "
                               "no word-driven zooms or callouts")
        ranges, between, head, tail = _silence_ranges(video, src_dur, E, env, rep)
    for c in ([head] if head else []) + between + ([tail] if tail else []):
        rep["cuts"].append(dict(c, len=_r3(c["out"] - c["in"])))

    # ------------------------------------------------------------- segments: frames, pace cuts, timing
    segs = [{"in": a, "out": b, "kind": "start" if k == 0 else between[k - 1]["kind"]}
            for k, (a, b) in enumerate(ranges)]
    if o["zoom"] and E["hold"] and words:
        segs = _split_long(segs, words, drop, E["hold"], env)
    segs = _frame_align(segs, fps, src_dur)
    frames = 0
    for g in segs:
        # a boundary floored to the millisecond never lands after the frame it belongs to (32/30 s = 1.0667
        # rounded up to 1.067 would show the old shot on the first frame of the new one)
        g["t0"] = _floor_ms(frames / fps)
        frames += g["n"]
    D = _floor_ms(frames / fps)
    for k, g in enumerate(segs):
        g["t1"] = segs[k + 1]["t0"] if k + 1 < len(segs) else D
    rep["output"] = {"duration": D, "width": W, "height": H, "fps": fps, "aspect": o["aspect"]}
    rep["segments"] = len(segs)
    rep["pace_cuts"] = sum(1 for g in segs if g["kind"] == "PACE")

    # words in output time, with their sentence
    out_words, out_sents = [], []
    if words:
        tagged = []
        for si, idx in enumerate(sents_src):
            for i in idx:
                tagged.append(dict(words[i], i=i, si=si))
        out_words = transcribe.remap(tagged, segs)
        by = {}
        for w in out_words:
            by.setdefault(w["si"], []).append(w)
        punct = any(re.search(r"[.!?]$", w["w"]) for w in out_words)
        for si in sorted(by):
            ws = by[si]
            out_sents.append({"si": si, "words": ws, "s": ws[0]["s"], "e": ws[-1]["e"], "punct": punct})

    # ------------------------------------------------------------- reframe and punch-ins
    reframed = abs((sw / float(sh)) - (W / float(H))) > 0.02 * (W / float(H))
    _focus(video, segs, (sw, sh), (W, H), reframed, o, rep)
    for k, g in enumerate(segs):
        g["k"] = 1.0 if not o["zoom"] or k % 2 == 0 else E["punch"][(k // 2) % len(E["punch"])]
    layers = [_clip_layer(video, k, g, W, H) for k, g in enumerate(segs)]

    # ------------------------------------------------------------- captions
    cap_style, cap = None, None
    if words and o["captions"] and out_words:
        if o["style"]:
            try:
                cap_style = caption_style(o["style"])
                rep["captions_style"] = o["style"] if isinstance(o["style"], str) else "custom"
            except (OSError, ValueError) as e:
                rep["not_done"].append("could not read the style %s (%s); default captions used" % (o["style"], e))
        if cap_style is None:
            cap_style = default_captions(W, H, energy)
            rep["captions_style"] = "default"
        cap_style, note = fit_captions(cap_style, segs, W, H, E)
        if note:
            rep["not_done"].append(note)
        # punctuation stays on the words: captions.chunk ends a card at a sentence mark
        cap = {"words": transcribe.strip(out_words), "style": cap_style, "pop": bool(E["pop"])}
    elif not o["captions"]:
        rep["not_done"].append("captions turned off")
    accent = (cap_style or {}).get("highlight") or "#FFE500"
    tstyle = dict(templates.DEFAULT_STYLE, energy=energy,
                  accent=accent if re.match(r"^#[0-9A-Fa-f]{6}$", str(accent)) else "#FFE500")
    fonts = (cap_style or {}).get("font_candidates") or []
    if fonts:
        tstyle["font"] = fonts[0]
    band = caption_band(cap_style, H) if cap else None

    # ------------------------------------------------------------- graphics windows (times only)
    plan, busy, hook_end = _schedule(out_sents, out_words, cta, D, E, o)

    # ------------------------------------------------------------- b-roll windows
    broll_layers = []
    broll_windows = []
    if o["broll"]:
        broll_layers, broll_windows = _broll(o["broll"], out_sents, busy, D, hook_end, E, rep)

    # ------------------------------------------------------------- camera punches on emphasis
    punches = _punches(out_sents, segs, env, E, broll_windows, rep) if (o["zoom"] and out_sents) else []
    camera = _camera(punches, W, H)

    # ------------------------------------------------------------- graphics placement
    graphic_layers, placed = _place_all(plan, segs, punches, band, canvas, tstyle, rep)
    progress = o["progress"] if o["progress"] is not None else (H > W * 1.2 and D <= 180)
    if progress:
        graphic_layers.extend(templates.build("progress_bar", {"position": "top"}, 0.0, D, canvas, tstyle))

    # ------------------------------------------------------------- sound, transitions, music
    effects = [{"type": "grade", "contrast": 1.04, "saturation": 1.06}]
    audio = []
    cuts_t = [g["t0"] for g in segs[1:]]
    if o["sfx"]:
        audio, fx = _sound(placed, cuts_t, broll_windows, D, E, rep)
        effects.extend(fx)
    else:
        rep["not_done"].append("sound effects turned off")
    if o["music"]:
        music = os.path.abspath(o["music"])
        audio.insert(0, {"src": music, "start": 0.0, "role": "music", "volume": E["music"], "duck": True,
                         "loop": True, "fade_in": 0.5, "fade_out": min(1.5, D / 4.0), "duration": D})
        rep["music"] = music

    spec = {
        "version": timeline.SCHEMA_VERSION, "width": W, "height": H, "fps": fps, "duration": D,
        "background": "#000000",
        "layers": layers + broll_layers + graphic_layers,
        "camera": camera,
        "effects": effects,
        "captions": cap,
        "audio": audio,
        "meta": {"title": "Edit of %s" % os.path.basename(video), "source": "autoedit",
                 "notes": "%s -> %s s, %d cuts, %s energy" % (_r3(src_dur), D, len(rep["cuts"]), energy),
                 "edit": rep},
    }
    return spec


def _schedule(sents, words, cta, D, E, o):
    """When each graphic shows: the hook, the CTA, then callouts by priority, spaced by the energy preset.

    Returns (plan, busy windows, hook end). A plan item is {"t0", "t1",
    "template", "kind", "params", "prefer", "why"}; placement comes later.
    """
    busy, plan = [], []
    hook_text = (o["hook"] or "").strip()
    hook_hl = hook_highlight(hook_text) if hook_text else []
    if not hook_text and sents and o["graphics"]:
        hook_text, hook_hl = hook_phrase(sents[0]["words"])
    hook_end = 0.0
    if hook_text and D > 1.0 and (o["graphics"] or o["hook"]):
        first_e = sents[0]["e"] if sents else 2.4
        hook_end = min(max(1.8, min(3.0, first_e + 0.25)), D - 0.2 if D > 2.2 else D)
        plan.append({"t0": 0.0, "t1": _r3(hook_end), "template": "hook_title", "kind": "hook",
                     "params": {"text": hook_text, "highlight": hook_hl}, "prefer": ("top", "middle", "left"),
                     "why": "hook" + (" (given)" if o["hook"] else " (opening sentence)")})
        busy.append((0.0, hook_end))
    cta_t0 = None
    if cta and o["graphics"] and words:
        p, wi, verb, icon = cta
        ws = [w for w in words if w["si"] == p]
        k = next((n for n, w in enumerate(ws) if w["i"] == wi), None)
        if k is not None and D - max(hook_end + 0.3, ws[k]["s"] - 0.1) >= 1.2:
            cta_t0 = max(hook_end + 0.3, ws[k]["s"] - 0.1)
            plan.append({"t0": _r3(cta_t0), "t1": D, "template": "cta", "kind": "cta",
                         "params": {"text": cta_text(ws, k, verb), "icon": icon},
                         "prefer": ("middle", "low", "right"), "why": "call to action (%s)" % verb})
            busy.append((cta_t0, D))
    if o["graphics"] and sents:
        cands = sorted(find_callouts(sents), key=lambda c: (-c["priority"], c["t"]))
        accepted = []
        limit_end = (cta_t0 - 0.3) if cta_t0 is not None else D - 0.1
        for c in cands:
            t0 = max(0.0, c["t"] - 0.08)      # lands with the word
            lst = bool(c.get("list"))
            if lst:
                t1 = min(max(t0 + 1.6, c["s_end"] + 0.4), t0 + 4.0)   # a list point stays while it is explained
            else:
                t1 = min(t0 + E["callout_len"], max(c["s_end"] + 0.5, t0 + 1.4))
            t1 = min(t1, limit_end)
            if t1 - t0 < 1.2 or t0 < hook_end + 0.3:
                continue
            if any(t0 < b + 0.4 and a < t1 + 0.4 for a, b in busy):
                continue
            clash = [a for a in accepted if t0 < a["t1"] + 0.4 and a["t0"] < t1 + 0.4]
            # the points of one list hand over to each other: skipping point 2 of 3 reads as a mistake
            series = lst and clash and all(
                a["list"] and (a["t0"] + 1.4 <= t0 - 0.15 if a["t0"] < t0 else t0 + 1.4 <= a["t0"] - 0.15)
                for a in clash)
            if clash and not series:
                continue
            if any(abs(t0 - a["t0"]) < E["graphic_spacing"] for a in accepted if not (lst and a["list"])):
                continue
            for a in clash:   # shorten the neighbouring point so the two never overlap
                if a["t0"] < t0:
                    a["t1"] = _r3(min(a["t1"], t0 - 0.15))
                else:
                    t1 = min(t1, a["t0"] - 0.15)
            item = {"t0": _r3(t0), "t1": _r3(t1), "template": c["template"], "kind": "callout",
                    "params": c["params"], "prefer": ("middle", "top", "low", "right", "left"), "why": c["why"],
                    "list": lst}
            accepted.append(item)
        plan.extend(accepted)
        busy.extend((a["t0"], a["t1"]) for a in accepted)
    plan.sort(key=lambda g: g["t0"])
    return plan, busy, hook_end


def _place_all(plan, segs, punches, cap_band, canvas, style, rep):
    """Build and place every planned graphic away from the face and captions; (layers, placed plan items)."""
    W, H = canvas["width"], canvas["height"]
    layers, placed = [], []
    for g in plan:
        face = _face_zone(segs, punches, g["t0"], g["t1"])
        bands = free_bands(W, H, face, cap_band, W > H * 1.05)
        scales = (1.0, 0.86, 0.74, 0.64, 0.54) if g["template"] == "versus" else (1.0, 0.86, 0.74, 0.64)
        compact = None
        if g["template"] in ("stat", "counter_reveal") and g["params"].get("label"):
            compact = dict(g["params"], label="")
        lay, how = place_template(g["template"], g["params"], g["t0"], g["t1"], canvas, style, bands,
                                  g["prefer"], scales, compact)
        entry = {"start": g["t0"], "end": g["t1"], "template": g["template"], "why": g["why"],
                 "text": _graphic_text(g)}
        if lay is None:
            rep["skipped_graphics"].append(dict(entry, reason=how))
            continue
        for n, L in enumerate(lay):
            L["id"] = "%s%d.%s" % (g["kind"], len(placed), L.get("id") or n)
        layers.extend(lay)
        entry.update(how)
        rep["graphics"].append(entry)
        placed.append(dict(g, land=landing(lay)))
    return layers, placed


def _graphic_text(g):
    p = g["params"]
    for k in ("text", "value", "left"):
        if p.get(k) not in (None, ""):
            if k == "left":
                return "%s vs %s" % (p["left"], p.get("right", ""))
            if k == "value" and g["template"] == "counter_reveal":
                return "%s%s%s" % (p.get("prefix", ""), _fmt(float(p["value"]), int(p.get("decimals") or 0)),
                                   p.get("suffix", ""))
            return str(p[k])
    return ""


def _trim_to(words, drop, sents, cta, max_dur, src_dur, E, env, end_tail, rep):
    """Drop the lowest-value middle sentences until the edit fits max_dur (never the hook or the ending)."""
    while True:
        ranges, between, head, tail = keep_ranges(words, drop, src_dur, E, env, end_tail)
        total = sum(b - a for a, b in ranges)
        if total <= max_dur + 1e-6:
            return ranges, between, head, tail
        live = [s for s in sents if any(i not in drop for i in s)]
        if len(live) <= 2:
            break
        protect = {0, len(live) - 1}
        t_out = 0.0
        for p, s in enumerate(live):   # sentences that start in the first 3 s of the edit are the hook
            if t_out < 3.0:
                protect.add(p)
            t_out += words[s[-1]]["e"] - words[s[0]]["s"]
        if cta:
            protect.update(p for p, s in enumerate(live) if cta[1] in s)
        cands = [(sentence_value([words[i] for i in s]), -(words[s[-1]]["e"] - words[s[0]]["s"]), p)
                 for p, s in enumerate(live) if p not in protect]
        if not cands:
            break
        cands.sort()
        v, _, p = cands[0]
        text = _text([words[i] for i in live[p]])
        why = 'low-value sentence "%s" (max duration %g s)' % (text[:60] + ("..." if len(text) > 60 else ""), max_dur)
        for i in live[p]:
            drop[i] = ("TRIM", why)
    rep["not_done"].append("could not reach --max-duration %g s without cutting the hook or the ending (%.1f s)"
                           % (max_dur, total))
    return ranges, between, head, tail


def _silence_ranges(video, src_dur, E, env, rep):
    """Kept ranges from ffmpeg silencedetect when there is no transcript."""
    if env is None:
        rep["not_done"].append("no sound to find pauses in: the video is kept whole")
        return [[0.0, _r3(src_dur)]], [], None, None
    thr = env["thr"] if env["contrast"] >= 10 else -35.0
    sil = silences(video, thr, E["gap"])
    rep["silence_threshold_db"] = round(thr, 1)
    ranges, between, head, tail = [], [], None, None
    cur = 0.0
    for a, b in sil:
        b = src_dur if b is None else min(b, src_dur)
        c0 = a + (E["tail"] if a > 0.01 else 0.0)
        c1 = b - (E["lead"] if b < src_dur - 0.01 else 0.0)
        if c1 - c0 < 0.05:
            continue
        cut = {"kind": "DEAD", "in": _r3(c0), "out": _r3(c1), "why": "%.2f s of silence" % (b - a)}
        if c0 <= 0.01:
            head, cur = dict(cut, why="silence at the start"), c1
            continue
        if c1 >= src_dur - 0.01:
            tail = dict(cut, why="silence at the end")
            ranges.append([_r3(cur), _r3(c0)])
            cur = None
            break
        ranges.append([_r3(cur), _r3(c0)])
        between.append(cut)
        cur = c1
    if cur is not None:
        ranges.append([_r3(cur), _r3(src_dur)])
    ranges = [r for r in ranges if r[1] - r[0] > 0.01]
    if not ranges:
        rep["not_done"].append("the whole recording is below the silence threshold: kept whole")
        return [[0.0, _r3(src_dur)]], [], None, None
    return ranges, between, head, tail


def _split_long(segs, words, drop, hold, env):
    """Split shots longer than ~1.6 x hold at the best pause inside them (a 'PACE' cut, nothing removed)."""
    limit = hold * 1.6
    kept = [i for i in range(len(words)) if i not in drop]
    out = []
    todo = list(segs)
    while todo:
        g = todo.pop(0)
        if g["out"] - g["in"] <= limit:
            out.append(g)
            continue
        inside = [i for i in kept if words[i]["s"] >= g["in"] and words[i]["e"] <= g["out"]]
        best, best_s = None, -1e9
        mid = (g["in"] + g["out"]) / 2.0
        for a, b in zip(inside, inside[1:]):
            gap = words[b]["s"] - words[a]["e"]
            t = (words[a]["e"] + words[b]["s"]) / 2.0
            if gap < 0.06 or t - g["in"] < 1.2 or g["out"] - t < 1.2:
                continue
            score = gap * 4 + (1.0 if re.search(r"[.!?,]$", words[a]["w"]) else 0) - abs(t - mid) / (g["out"] - g["in"])
            if score > best_s:
                best, best_s = (a, b, t), score
        if best is None:
            out.append(g)
            continue
        a, b, t = best
        t = _r3(_quietest(env, words[a]["e"], words[b]["s"], t))
        todo.insert(0, {"in": t, "out": g["out"], "kind": "PACE"})
        todo.insert(0, {"in": g["in"], "out": t, "kind": g["kind"]})
    return out


def _frame_align(segs, fps, src_dur):
    """Whole-frame segment lengths, so the picture cuts exactly where the sound does."""
    out, prev_out = [], None
    for g in segs:
        g = dict(g)
        if out and prev_out is not None and abs(prev_out - g["in"]) < 1e-6:
            g["in"] = out[-1]["out"]       # a pace cut: the next shot starts exactly where the last ended
        prev_out = g["out"]
        n = max(1, int(round((g["out"] - g["in"]) * fps)))
        n = min(n, max(1, int((src_dur - g["in"]) * fps + 1e-6)))
        g["n"] = n
        g["out"] = _r3(g["in"] + n / fps)
        out.append(g)
    return out


def _focus(video, segs, src, canvas, reframed, o, rep):
    """Give each segment a focus point, its face rect in canvas px and a zoom anchor."""
    W, H = canvas
    times, owner = [], []
    budget = 600
    total = sum(g["out"] - g["in"] for g in segs) or 1.0
    for k, g in enumerate(segs):
        L = g["out"] - g["in"]
        n = max(1, min(12, int(L / 0.5), int(budget * L / total)))
        for j in range(n):
            times.append(g["in"] + (j + 0.5) * L / n)
            owner.append(k)
    tr = {"points": [], "method": "centre", "found": 0, "samples": 0, "detector": None}
    if times and (reframed or o["graphics"] or o["zoom"]):
        try:
            tr = reframe.track(video, times, aspect=canvas, download=o["download_model"])
        except (RuntimeError, ValueError, OSError) as e:
            rep["not_done"].append("face tracking failed (%s): centre framing" % e)
    pts = tr["points"]
    prev_focus = None
    with_face = 0
    for k, g in enumerate(segs):
        mine = [p for p, ow in zip(pts, owner) if ow == k]
        faces = [p["face"] for p in mine if p.get("face")]
        detected = sum(1 for p in mine if p.get("detected"))
        focus = [0.5, 0.5]
        if reframed and mine:
            focus = [float(np.median([p["focus"][0] for p in mine])), float(np.median([p["focus"][1] for p in mine]))]
        if prev_focus is not None and reframed:
            s, _, _ = cover_map(src, canvas, focus)
            dx = abs(focus[0] - prev_focus[0]) * src[0] * s
            dy = abs(focus[1] - prev_focus[1]) * src[1] * s
            if dx < 0.04 * W and dy < 0.04 * H:
                focus = list(prev_focus)    # a dead zone: shots of one take keep one framing
        g["focus"] = [round(focus[0], 4), round(focus[1], 4)]
        prev_focus = g["focus"]
        s, ox, oy = cover_map(src, canvas, g["focus"])
        if faces and detected:
            with_face += 1
            fx, fy, fw, fh = (float(np.median([f[i] for f in faces])) for i in range(4))
            rect = [fx * src[0] * s - ox, fy * src[1] * s - oy, (fx + fw) * src[0] * s - ox,
                    (fy + fh) * src[1] * s - oy]
            g["face"] = [round(v, 1) for v in rect]
            g["F"] = [min(W, max(0.0, (rect[0] + rect[2]) / 2.0)), min(H, max(0.0, (rect[1] + rect[3]) / 2.0))]
        else:
            g["face"] = None
            g["F"] = [W / 2.0, 0.42 * H]
    rep["focus"] = {"reframed": reframed, "method": tr["method"], "detector": tr.get("detector"),
                    "found": tr["found"], "samples": tr["samples"], "segments_with_face": with_face,
                    "segments": len(segs)}
    if reframed and not tr["found"]:
        why = "no face detector (YuNet model not downloaded)" if not tr.get("detector") else "no face found"
        rep["not_done"].append("reframing used centre crops: %s" % why)


def _clip_layer(video, k, g, W, H):
    """The video layer of segment k: cover fit around its focus, punched in by g['k'] about the face."""
    L = {"type": "video", "id": "clip%02d" % k, "src": video, "in": g["in"], "start": g["t0"], "end": g["t1"],
         "fit": "cover", "focus": g["focus"], "volume": 1.0}
    kz = g["k"]
    if kz > 1.0:
        w, h = _even(W * kz), _even(H * kz)
        kx, ky = w / float(W), h / float(H)
        Fx, Fy = g["F"]
        x = w / 2.0 + (1 - kx) * Fx
        y = h / 2.0 + (1 - ky) * Fy
        x = min(w / 2.0, max(W - w / 2.0, x))   # the punched-in frame must still cover the canvas
        y = min(h / 2.0, max(H - h / 2.0, y))
        L.update({"w": w, "h": h, "x": int(round(x)), "y": int(round(y))})
    return L


def _seg_at(segs, t):
    for g in segs:
        if g["t0"] <= t < g["t1"]:
            return g
    return segs[-1] if segs else None


def _punches(sents, segs, env, E, avoid, rep):
    """Camera punches on the strongest word of each sentence, spaced by the energy preset."""
    cands = []
    for S in sents:
        ws = S["words"]
        best, best_s = None, 0.0
        levels = [word_level(env, w["s"], w["e"]) if env else -100.0 for w in ws]
        content = [i for i, w in enumerate(ws) if _content(_tok(w["w"]))]
        loudest = max(content, key=lambda i: levels[i]) if content and env else None
        nums = {sp["a"] for sp in number_spans(ws) if sp["kind"] not in ("year", "ordinal")
                and (sp.get("digits") or sp["value"] >= 10)}
        for i, w in enumerate(ws):
            t = _tok(w["w"])
            sc = 3.0 if i in nums else 2.5 if _is_superlative(t) else 2.0 if t in NEGATIONS else 0.0
            if i == loudest:
                sc += 1.5
            if sc > best_s:
                best, best_s = i, sc
        if best is None or best_s < 1.5:
            continue
        w = ws[best]
        cands.append((best_s, w["s"], _bare(w["w"]), S["e"]))
    cands.sort(key=lambda c: (-c[0], c[1]))
    chosen = []
    for score, t, word, s_end in cands:
        if t < 0.25 or any(a - 0.3 < t < b + 0.1 for a, b in avoid):
            continue
        if any(abs(t - c["t"]) < E["zoom_spacing"] for c in chosen):
            continue
        g = _seg_at(segs, t)
        if g is None:
            continue
        t_in = max(g["t0"] + 0.02, t - 0.06)
        t_peak = t_in + E["zoom_in"]
        hold = min(s_end + 0.15, g["t1"])
        if hold - t_peak < 0.3:
            continue
        z = min(E["zoom"], MAX_TOTAL_ZOOM / g["k"])
        if z <= 1.005:
            continue
        release = "ease"
        if g["t1"] - hold < 0.35 or hold + E["zoom_release"] > g["t1"] - 0.02:
            hold, release = g["t1"], "cut"
        chosen.append({"t": t, "word": word, "t_in": t_in, "t_peak": t_peak, "hold": hold, "release": release,
                       "z": round(z, 3), "F": g["F"], "seg": g})
    chosen.sort(key=lambda c: c["t_in"])
    out = []
    for c in chosen:   # punches must not overlap: an earlier one releases before the next begins
        if out:
            p = out[-1]
            end = p["hold"] + (0 if p["release"] == "cut" else E["zoom_release"])
            if c["t_in"] < end + 0.05:
                continue
        c["release_len"] = E["zoom_release"]
        c["ease"] = E["zoom_ease"]
        out.append(c)
        rep["zooms"].append({"t": _r3(c["t"]), "word": c["word"], "zoom": c["z"]})
    return out


def _camera(punches, W, H):
    """Camera keyframes: zoom about the face (zoom plus the x/y offset that keeps the face still)."""
    zk, xk, yk = [[0, 1.0]], [[0, 0.0]], [[0, 0.0]]
    C = (W / 2.0, H / 2.0)
    for p in punches:
        z, F = p["z"], p["F"]
        ox, oy = (1 - z) * (F[0] - C[0]) + 0.0, (1 - z) * (F[1] - C[1]) + 0.0
        keys = [(p["t_in"], 1.0, 0.0, 0.0, None), (p["t_peak"], z, ox, oy, p["ease"])]
        if p["release"] == "cut":
            keys.append((p["hold"], 1.0, 0.0, 0.0, "step"))
        else:
            keys.append((p["hold"], z, ox, oy, None))
            keys.append((p["hold"] + p["release_len"], 1.0, 0.0, 0.0, "in-out-cubic"))
        for t, zv, xv, yv, e in keys:
            for lst, v in ((zk, zv), (xk, xv), (yk, yv)):
                key = [_r3(t), round(v, 4) + 0.0]
                if e:
                    key.append(e)
                if key[0] <= lst[-1][0]:
                    key[0] = _r3(lst[-1][0] + 0.001)
                lst.append(key)
    return {"zoom": zk, "x": xk, "y": yk, "rotation": []}


def _face_zone(segs, punches, t0, t1):
    """Union of the face rects (with punch-in and camera zoom) on screen during [t0, t1], or None."""
    box = None
    for g in segs:
        if g["t1"] <= t0 or g["t0"] >= t1 or not g.get("face"):
            continue
        z = max([p["z"] for p in punches if p["seg"] is g and p["t_in"] < t1 and p["hold"] > t0] or [1.0])
        k = g["k"] * z
        Fx, Fy = g["F"]
        x0, y0, x1, y1 = g["face"]
        # the face grows about the zoom anchor; 10 % margin so text never brushes it
        mx, my = 0.1 * (x1 - x0), 0.1 * (y1 - y0)
        r = [Fx + k * (x0 - mx - Fx), Fy + k * (y0 - my - Fy), Fx + k * (x1 + mx - Fx), Fy + k * (y1 + my - Fy)]
        box = r if box is None else [min(box[0], r[0]), min(box[1], r[1]), max(box[2], r[2]), max(box[3], r[3])]
    return box


def _broll(folder, sents, busy, D, hook_end, E, rep):
    """B-roll cutaway layers and their windows: keyword matches first, then topic changes."""
    if not os.path.isdir(folder):
        rep["not_done"].append("b-roll folder not found: %s" % folder)
        return [], []
    files = broll_files(folder)
    if not files:
        rep["not_done"].append("no videos or images in the b-roll folder %s" % folder)
        return [], []
    taken = list(busy)
    windows, layers = [], []
    used = set()

    def room(t0, want):
        """Largest window from t0 (<= want s) that avoids busy windows and the ends of the video."""
        if t0 < max(hook_end + 0.2, 0.8) or t0 > D - 1.6:
            return 0.0
        t1 = min(t0 + want, D - 0.3)
        for a, b in taken:
            if a - 0.25 < t0 < b + 0.25:
                return 0.0
            if t0 < a < t1 + 0.25:
                t1 = min(t1, a - 0.25)
        for a, b in windows:
            if abs(t0 - a) < 2.5 + (b - a):
                return 0.0
        return t1 - t0

    def add(path, kind, t0, dur, why):
        n = len(windows)
        if kind == "video":
            info = timeline.probe(path) or {}
            length = info.get("duration") or 0
            if not info.get("has_video") or length < 1.0:
                return False
            dur = min(dur, length)
            src_in = round(max(0.0, (length - dur) / 2.0), 3) if length > dur + 0.5 else 0.0
            L = {"type": "video", "src": path, "in": src_in, "fit": "cover", "volume": 0.0,
                 "kenburns": [1.0, 1.06] if n % 2 == 0 else [1.06, 1.0]}
        else:
            L = {"type": "image", "src": path, "fit": "cover", "kenburns": [1.0, 1.12] if n % 2 == 0 else [1.12, 1.0]}
        if dur < 1.2:
            return False
        L.update({"id": "broll%d" % n, "start": _r3(t0), "end": _r3(t0 + dur)})
        kind_in, d_in = E["broll_in"]
        if kind_in == "whip-left" and n % 2:
            kind_in = "whip-right"
        L["enter"] = {"type": kind_in, "duration": d_in}
        if E["broll_out"]:
            L["exit"] = {"type": E["broll_out"][0], "duration": E["broll_out"][1]}
        layers.append(L)
        windows.append((t0, t0 + dur))
        used.add(path)
        rep["broll"].append({"file": os.path.basename(path), "start": _r3(t0), "end": _r3(t0 + dur), "why": why})
        return True

    # 1) keyword matches: the file name says what it shows
    pairs = []
    for path, kind, kw in files:
        for S in sents:
            for w in S["words"]:
                t = _stem(_tok(w["w"]))
                if t in kw:
                    score = len(kw & keywords(S["words"]))
                    pairs.append((score, w["s"], path, kind, t, S))
                    break
    pairs.sort(key=lambda p: (-p[0], p[1]))
    for score, t, path, kind, word, S in pairs:
        if path in used:
            continue
        t0 = max(0.0, t - 0.15)
        want = min(3.0, max(2.0, S["e"] - t0 + 0.3))
        r = room(t0, want)
        if r >= 1.5:
            add(path, kind, t0, r, 'matches "%s"' % word)
    # 2) topic changes for the rest
    rest = [f for f in files if f[0] not in used]
    if rest and len(sents) > 2:
        kws = [keywords(S["words"]) for S in sents]
        changes = []
        for k in range(1, len(sents)):
            before = set().union(*kws[max(0, k - 2):k])
            after = set().union(*kws[k:k + 2])
            changes.append((_jaccard(before, after), sents[k]["s"]))
        changes.sort()
        for sim, t in changes:
            if not rest or sim > 0.2:
                break
            t0 = max(0.0, t - 0.1)
            r = room(t0, 2.5)
            if r >= 2.0 and add(rest[0][0], rest[0][1], t0, r, "topic change"):
                rest.pop(0)
    rep["broll_unused"] = [os.path.basename(f[0]) for f in files if f[0] not in used]
    return layers, windows


def _sound(placed, cuts, broll_windows, D, E, rep):
    """Sound effects (audio entries) and the zoom-blur transitions that go with the whooshes."""
    want = []   # (priority, time, name, volume, why)
    for g in placed:
        t = g["land"]
        if g["kind"] == "hook":
            want.append((5, t, "hit", 0.55, "hook title lands"))
        else:
            want.append((4, t, "pop", 0.45, "%s appears" % g["template"]))
    every = E["whoosh_every"]
    fx = []
    if every:
        gfx = [(g["t0"], g["t1"]) for g in placed]
        for n, c in enumerate(cuts):
            if n % every != 0:
                continue
            if any(a - 0.1 < c < b + 0.1 for a, b in broll_windows):
                continue   # under a cutaway: the cut is not seen
            want.append((2, c, "whoosh", 0.32, "cut at %.2f s" % c))
            if E["blur"] and not any(a - 0.2 < c < b + 0.2 for a, b in gfx):
                fx.append({"type": "zoom-blur", "start": _r3(max(0.0, c - 0.08)), "duration": 0.16,
                           "intensity": E["blur"]})
    for a, b in broll_windows:
        if E["broll_in"][0].startswith("whip"):
            want.append((3, a + 0.1, "swoosh", 0.32, "b-roll whip"))
    # a riser into the payoff: the first number callout after a third of the video, else the CTA
    if E["whoosh_every"]:
        pay = [g for g in placed if g["template"] in ("stat", "counter_reveal") and g["t0"] >= D / 3.0]
        pay = pay or [g for g in placed if g["kind"] == "cta"]
        if pay and pay[0]["land"] - 1.91 > 0.5:
            want.append((1, pay[0]["land"], "riser", 0.28, "builds into the payoff at %.2f s" % pay[0]["land"]))
    peaks = {"whoosh": 0.37, "swoosh": 0.23, "riser": 1.91}
    want.sort(key=lambda w: (-w[0], w[1]))
    chosen = []
    for pr, t, name, vol, why in want:
        if name != "riser" and any(abs(t - c[1]) < 0.3 and c[2] != "riser" for c in chosen):
            continue
        chosen.append((pr, t, name, vol, why))
    chosen.sort(key=lambda c: c[1])
    audio = []
    for pr, t, name, vol, why in chosen:
        start = _r3(t - peaks.get(name, 0.0))
        if start < 0 or start >= D:
            continue
        audio.append({"sfx": name, "start": start, "volume": vol})
        rep["sfx"].append({"name": name, "t": start, "why": why})
    audio.sort(key=lambda a: a["start"])
    rep["sfx"].sort(key=lambda a: a["t"])
    return audio, fx


# =========================================================================== report

def _ts(t):
    t = max(0.0, float(t))
    return "%d:%05.2f" % (int(t // 60), t - 60 * int(t // 60))


def report_md(spec, out_dir=None):
    """EDIT_REPORT.md text for a spec made by plan_edit."""
    r = spec["meta"]["edit"]
    i, o = r["input"], r["output"]
    saved = i["duration"] - o["duration"]
    lines = ["# Edit report: %s" % os.path.basename(i["path"]), ""]
    lines += ["| | input | output |", "| --- | --- | --- |",
              "| duration | %.2f s | %.2f s (%.2f s cut, %.0f %% shorter) |" % (
                  i["duration"], o["duration"], saved, 100.0 * saved / max(i["duration"], 1e-6)),
              "| frame | %sx%s | %sx%s (%s) |" % (i["width"], i["height"], o["width"], o["height"], o["aspect"]),
              "| fps | %s | %s |" % (i["fps"], o["fps"]), ""]
    tr = r.get("transcript") or {}
    lines.append("- **Energy:** %s (%s)" % (r["energy"]["preset"], r["energy"]["why"]))
    if tr.get("source"):
        lines.append("- **Transcript:** %s, %d words%s" % (tr["source"], tr["words"],
                     ", %d word edges tightened against the audio" % tr["refined"] if tr.get("refined") else ""))
    else:
        lines.append("- **Transcript:** none (silence-based cuts%s)" % (
            ", threshold %.1f dB" % r["silence_threshold_db"] if "silence_threshold_db" in r else ""))
    f = r.get("focus") or {}
    if f.get("reframed"):
        how = ("face tracking (%s), a face in %d of %d samples, %d of %d shots framed on the face"
               % (f.get("detector"), f["found"], f["samples"], f["segments_with_face"], f["segments"])
               if f["found"] else "centre crop (no face found)")
        lines.append("- **Reframe:** %sx%s -> %sx%s by %s" % (i["width"], i["height"], o["width"], o["height"], how))
    else:
        lines.append("- **Reframe:** not needed (the source already has the output shape); faces found in %d of "
                     "%d samples were kept clear of graphics" % (f.get("found", 0), f.get("samples", 0)))
    lines.append("- **Shots:** %d (%d of them pace cuts inside a long take, nothing removed there)"
                 % (r["segments"], r["pace_cuts"]))
    if r.get("captions_style"):
        lines.append("- **Captions:** %d words, style: %s" % (len((spec.get("captions") or {}).get("words") or []),
                                                            r["captions_style"]))
    if r.get("music"):
        lines.append("- **Music:** %s, ducked under the voice" % os.path.basename(r["music"]))
    lines.append("")
    kinds = {}
    for c in r["cuts"]:
        kinds.setdefault(c["kind"], [0, 0.0])
        kinds[c["kind"]][0] += 1
        kinds[c["kind"]][1] += c["len"]
    names = {"DEAD": "dead air", "FILLER": "filler words", "RETAKE": "retakes", "TRIM": "trimmed for length"}
    lines.append("## Cuts (%d, %.2f s removed)" % (len(r["cuts"]), sum(c["len"] for c in r["cuts"])))
    lines.append("")
    if kinds:
        lines.append(", ".join("%s: %d (%.2f s)" % (names.get(k, k), v[0], v[1]) for k, v in sorted(kinds.items())))
        lines.append("")
        lines += ["| # | kind | source | length | why |", "| --- | --- | --- | --- | --- |"]
        for n, c in enumerate(r["cuts"], 1):
            lines.append("| %d | %s | %s - %s | %.2f s | %s |" % (n, c["kind"], _ts(c["in"]), _ts(c["out"]), c["len"],
                                                                 c["why"].replace("|", "/")))
    else:
        lines.append("Nothing was cut.")
    lines += ["", "## Graphics (%d)" % len(r["graphics"]), ""]
    if r["graphics"]:
        lines += ["| time | template | text | placed |", "| --- | --- | --- | --- |"]
        for g in r["graphics"]:
            lines.append("| %s - %s | %s | %s | %s band, %d %% size%s |" % (
                _ts(g["start"]), _ts(g["end"]), g["template"], str(g["text"]).replace("|", "/"), g["band"],
                round(100 * g["scale"]), ", label dropped to fit" if g.get("compact") else ""))
    for g in r["skipped_graphics"]:
        lines.append("- skipped %s \"%s\" at %s: %s" % (g["template"], g["text"], _ts(g["start"]), g["reason"]))
    if r["zooms"]:
        lines += ["", "## Emphasis zooms (%d)" % len(r["zooms"]), ""]
        lines.append(", ".join('%s "%s" x%.2f' % (_ts(z["t"]), z["word"], z["zoom"]) for z in r["zooms"]))
    if r["broll"] or r["broll_unused"]:
        lines += ["", "## B-roll", ""]
        for b in r["broll"]:
            lines.append("- %s - %s: %s (%s)" % (_ts(b["start"]), _ts(b["end"]), b["file"], b["why"]))
        if r["broll_unused"]:
            lines.append("- not used (no matching moment or no room): %s" % ", ".join(r["broll_unused"]))
    if r["sfx"]:
        lines += ["", "## Sound design (%d effects)" % len(r["sfx"]), ""]
        lines.append(", ".join("%s %s" % (_ts(s["t"]), s["name"]) for s in r["sfx"]))
    if r["not_done"]:
        lines += ["", "## Could not be done / notes", ""]
        lines += ["- %s" % n for n in r["not_done"]]
    lines += ["", "## Change it", "",
              "Everything above is in `edit.json` (a timeline spec): move a cut by changing a clip's `in`/`start`/"
              "`end`, fix a framing with its `focus`, edit or delete a graphic's layers (or open it on the studio's "
              "Timeline page, `python -m vstudio serve`), then re-render:", "",
              "```", "python -m vstudio render %s --out %s" % (
                  os.path.join(out_dir, "edit.json") if out_dir else "edit.json",
                  os.path.join(out_dir, "edited.mp4") if out_dir else "edited.mp4"), "```", ""]
    return "\n".join(lines)


# =========================================================================== CLI

def _slug(path):
    s = re.sub(r"[^a-z0-9]+", "-", os.path.splitext(os.path.basename(path))[0].lower()).strip("-")
    return s or "edit"


def _size(text):
    m = re.match(r"^\s*(\d+)\s*[xX]\s*(\d+)\s*$", text or "")
    if not m:
        raise argparse.ArgumentTypeError("size must look like 1080x1920")
    return int(m.group(1)), int(m.group(2))


def _utf8_console():
    """Print paths, hooks and transcripts in any script: on Windows a piped stdout uses the ANSI code page
    (cp1252), which cannot encode a Hindi folder name or an emoji and would stop the command after its work."""
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):   # not a text stream (redirected in tests, pythonw)
            pass


def main(argv=None):
    _utf8_console()
    ap = argparse.ArgumentParser(prog="vstudio autoedit",
                                 description="Edit a raw talking video like a short-form editor: cuts, reframing, "
                                             "zooms, captions, graphics, b-roll, sound.")
    ap.add_argument("video", help="the raw video")
    ap.add_argument("--out-dir", help="output folder (default workspace/edits/<video name>)")
    ap.add_argument("--words", help="words JSON [{w, s, e}] (or whisper JSON)")
    ap.add_argument("--captions", help="SRT / VTT / json3 captions file of the video")
    ap.add_argument("--style", help="style_dna.json whose caption track styles the captions")
    ap.add_argument("--aspect", default="9:16", choices=list(ASPECTS) + ["source"])
    ap.add_argument("--broll", help="folder of b-roll clips and images (file names say what they show)")
    ap.add_argument("--music", help="music file, ducked under the voice")
    ap.add_argument("--no-zoom", action="store_true", help="no punch-ins or emphasis zooms")
    ap.add_argument("--no-graphics", action="store_true", help="no hook title, callouts or CTA")
    ap.add_argument("--no-sfx", action="store_true", help="no sound effects")
    ap.add_argument("--no-captions", action="store_true", help="no burned-in captions")
    ap.add_argument("--max-duration", type=float, help="trim low-value sentences until the edit fits (seconds)")
    ap.add_argument("--energy", default="auto", choices=["auto"] + list(ENERGY),
                    help="editing intensity (auto = from the speaking rate)")
    ap.add_argument("--hook", help="hook title text for the first seconds (default: from the first sentence)")
    ap.add_argument("--render", action="store_true", help="render edited.mp4 after planning")
    ap.add_argument("--preview", action="store_true", help="render at half size and half frame rate")
    ap.add_argument("--whisper-model", default="small", help="faster-whisper model when transcribing (default small)")
    ap.add_argument("--lang", help="spoken language code for whisper (default: detect)")
    ap.add_argument("--fps", type=float, help="output frame rate (default: 30, or 24/25 like the source)")
    ap.add_argument("--size", type=_size, help="output size WxH instead of the aspect's 1080p size")
    a = ap.parse_args(argv)

    video = os.path.abspath(a.video)
    if not os.path.isfile(video):
        print("autoedit: no such video: %s" % a.video, file=sys.stderr)
        return 1
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        print("autoedit: ffmpeg and ffprobe are needed (https://ffmpeg.org/download.html)", file=sys.stderr)
        return 1
    for flag, path in (("--words", a.words), ("--captions", a.captions), ("--style", a.style),
                       ("--music", a.music)):
        if path and not os.path.isfile(path):
            print("autoedit: %s file not found: %s" % (flag, path), file=sys.stderr)
            return 1
    if a.broll and not os.path.isdir(a.broll):
        print("autoedit: --broll folder not found: %s" % a.broll, file=sys.stderr)
        return 1
    out_dir = os.path.abspath(a.out_dir or os.path.join(paths.WORKSPACE, "edits", _slug(video)))
    paths.ensure(out_dir)
    t_start = time.time()

    words, source, notes = transcribe.load_words(video, a.captions, a.words, a.whisper_model, a.lang)
    print("transcript: %s" % ("%d words from %s" % (len(words), source) if words else "none"))
    for n in notes:
        print("  note: %s" % n)
    if words and source and source.startswith("faster-whisper"):
        timeline.save(words, os.path.join(out_dir, "transcript.json"))
        print("  saved transcript.json (pass it back with --words to skip transcribing next time)")
    opts = {"aspect": a.aspect, "energy": a.energy, "style": a.style, "broll": a.broll, "music": a.music,
            "zoom": not a.no_zoom, "graphics": not a.no_graphics, "sfx": not a.no_sfx,
            "captions": not a.no_captions, "max_duration": a.max_duration, "hook": a.hook, "fps": a.fps,
            "size": a.size, "words_source": source, "notes": notes}
    try:
        spec = plan_edit(video, words, opts)
    except (ValueError, RuntimeError, OSError) as e:
        print("autoedit failed: %s" % e, file=sys.stderr)
        return 1
    r = spec["meta"]["edit"]
    cut = sum(c["len"] for c in r["cuts"])
    print("cuts: %d (%.1f s removed) -> %.1f s, %d shots, %s energy" % (len(r["cuts"]), cut, spec["duration"],
                                                                      r["segments"], r["energy"]["preset"]))
    f = r["focus"]
    print("framing: %s" % ("%s, face in %d/%d samples" % (f["method"], f["found"], f["samples"]) if f["reframed"]
                           else "source shape kept"))
    print("graphics: %d placed%s, %d zooms, %d b-roll, %d sound effects" % (
        len(r["graphics"]), " (%d skipped)" % len(r["skipped_graphics"]) if r["skipped_graphics"] else "",
        len(r["zooms"]), len(r["broll"]), len(r["sfx"])))
    spec_path = os.path.join(out_dir, "edit.json")
    timeline.save(spec, spec_path)
    report_path = os.path.join(out_dir, "EDIT_REPORT.md")
    with open(report_path, "w", encoding="utf-8") as fh:
        fh.write(report_md(spec, out_dir))
    print("wrote %s" % spec_path)
    print("wrote %s" % report_path)
    errors = timeline.validate(spec)
    if errors:   # written anyway, so the problem can be seen and fixed in the file
        print("autoedit: the planned timeline is invalid:\n- " + "\n- ".join(errors), file=sys.stderr)
        return 1
    if a.render:
        from . import render
        out = os.path.join(out_dir, "edited.mp4")
        t0 = time.time()
        last = [0.0]

        def bar(done, total):
            now = time.time()
            if now - last[0] < 0.5 and done < total:
                return
            last[0] = now
            n = int(30 * done / total)
            sys.stderr.write("\r[%s%s] %d/%d frames  %.0f s" % ("#" * n, "." * (30 - n), done, total, now - t0))
            if done == total:
                sys.stderr.write("\n")
            sys.stderr.flush()

        try:
            render.render(spec, out, preview=a.preview, progress=bar)
        except (render.RenderError, OSError, ValueError) as e:
            print("render failed: %s" % e, file=sys.stderr)
            return 1
        info = timeline.probe(out) or {}
        print("wrote %s (%sx%s, %.2f s)" % (out, info.get("width"), info.get("height"), info.get("duration") or 0))
    print("done in %.1f s" % (time.time() - t_start))
    return 0


if __name__ == "__main__":
    sys.exit(main())
