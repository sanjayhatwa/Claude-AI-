"""
planner.py - turns a PLAN (beats: what is said, what is shown, how it cuts;
ARCHITECTURE.md section 2) into a TIMELINE SPEC the renderer draws, and
writes a decent plan offline when no LLM is around.

    plan = heuristic_plan("3 settings costing you views", script=text, duration=30, assets=["b1.mp4"])
    errors = validate_plan(plan)              # [] = compilable
    spec = compile_plan(plan, base_dir=".")   # passes timeline.validate()

    python -m vstudio plan compile plan.json --out spec.json
    python -m vstudio plan validate plan.json
    python -m vstudio plan heuristic --prompt "..." [--script s.txt] [--asset clip.mp4]... --out plan.json

What the compiler does, in drawing order:
  * ambient glows (style.ambient): soft accent light drifting behind beats
    that have no footage, so text-only reels never sit on a dead backdrop;
  * one background layer per beat: an asset video (in point, ken-burns
    zoom, focus; cut into sub-shots every `style.cut_every` seconds so the
    edit keeps the reference's rhythm), an image, a colour, a gradient at
    any angle, `keep` (the previous shot runs on) or nothing (style bg);
  * transitions into a beat become enter/exit motion on the two shots plus
    frame effects: flash, whip (motion-blurred push), zoom (scale + zoom
    blur), fade (crossfade, the outgoing shot overlaps), glitch, slide;
  * a scrim (top and bottom darkening) over footage so text stays readable;
  * the beat's templates (templates.build, already HUD layers that the
    camera does not move), the CTA, and the progress bar;
  * camera moves on the footage (punch, push, drift as camera keyframes,
    shake as an effect), grade, vignette and grain from the style;
  * captions: voiceover words, or each beat's `say` timed inside the beat
    by syllables at style.wpm; hidden while a full-screen graphic or a
    template showing the same words is up; text templates are moved out
    of the caption band, and captions are kept inside the safe zone;
  * sfx placed so their peak lands on the cut (a riser ends on it), music
    looped and ducked under voice and clip audio.

Template params the plan leaves out never show catalog sample content
("Alex Rivera", "73%"): sample text is blanked, and a template whose real
numbers are missing (counter, ring, chart, rating, poll) is dropped, so
nothing invented reaches a reel.

heuristic_plan() splits a script (or the prompt) into one beat per spoken
unit, sizes beats by syllables at the style's words per minute, fits them to
the target length, picks a template from what each line says (numbers,
lists, questions, versus, quotes, warnings, short punches), alternates the
given assets as backgrounds (a file ffprobe or Pillow cannot read is
skipped with a note) and assigns transitions, sfx and camera moves by
energy. It only ever puts numbers on screen that are in the text. Lines
are split at . ! ? only where a sentence ends, so decimals ("3.5x",
"$1,250.50"), domains and abbreviations ("vs.", "Dr.") stay whole; timing
counts syllables in any script (Latin, Cyrillic, Greek, Indic, CJK).

validate_plan never raises on a malformed plan (wrong types anywhere): it
reports each problem. compile_plan leaves out the parts it cannot draw
(a template call that is not an object, a number that is not finite, a
colour that is not a colour) and raises ValueError when nothing usable is
left.

Limits: the heuristic reads English cues; it cannot judge meaning the way
an LLM plan can. Collision avoidance between templates and captions works
on the caption band and template positions, not on measured pixels.
"""

import argparse
import difflib
import json
import math
import os
import re
import shutil
import sys
import unicodedata

from . import paths, sfx, templates, timeline

ASPECTS = {"9:16": (1080, 1920), "1:1": (1080, 1080), "4:5": (1080, 1350), "16:9": (1920, 1080)}
TRANSITIONS = ["cut", "flash", "whip", "zoom", "fade", "glitch", "slide"]
CAMERAS = ["none", "punch", "shake", "drift", "push"]
ENERGIES = ["high", "medium", "calm"]
VIDEO_EXT = (".mp4", ".mov", ".m4v", ".mkv", ".webm", ".avi", ".mts", ".gif")
IMAGE_EXT = (".png", ".jpg", ".jpeg", ".webp", ".bmp")
AUDIO_EXT = (".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac", ".opus")
DEFAULT_WPM = 165

STYLE_DEFAULTS = dict(templates.DEFAULT_STYLE, **{
    "cut_every": None, "caption_style": None, "captions": True, "progress_bar": True, "grade": None,
    "wpm": DEFAULT_WPM, "vignette": 0.22, "grain": 0.0, "scrim": 0.5, "ambient": True,
    "captions_over_graphics": False, "caption_font": None, "sfx_volume": 1.0,
})
BACKGROUND_KEYS = {"asset", "in", "zoom", "focus", "color", "gradient", "angle", "keep", "volume", "speed"}
BEAT_KEYS = {"start", "end", "say", "visual", "background", "transition", "sfx", "camera", "notes"}
PLAN_KEYS = {"title", "aspect", "fps", "duration", "style", "voiceover", "music", "beats", "cta", "notes", "assets"}

# Catalog defaults that are sample content: blanked when a plan leaves them out.
SAMPLE_PARAMS = {
    "hook_title": ["text"], "title_card": ["title"], "chapter_marker": ["title"], "question": ["text"],
    "alert_banner": ["text"], "kinetic_text": ["text"], "emphasis_word": ["word"], "word_stack": ["words"],
    "underline_reveal": ["text"], "quote": ["text"], "stat": ["value", "label"], "counter_reveal": ["label"],
    "percent_ring": ["label"], "bar_chart": ["labels"], "price_tag": ["price"], "big_number_bg": ["number"],
    "list_item": ["text"], "checklist": ["items"], "ranking": ["items"], "timeline_steps": ["steps"],
    "step_number": ["text"], "pros_cons": ["pros", "cons"], "versus": ["left", "right"],
    "split_screen": ["a", "b"], "myth_vs_fact": ["myth", "fact"], "comment_bubble": ["text"],
    "post_card": ["name", "handle", "text", "likes", "comments"], "notification_pop": ["title", "text"],
    "text_message": ["messages"], "search_bar": ["query"], "poll": ["question", "options"], "callout": ["text"],
    "lower_third": ["name", "title"], "source_card": ["title", "detail"], "location_tag": ["place"],
}
# Numbers a template shows as fact: without them the template is dropped (never a sample number).
REQUIRED_NUMBERS = {"counter_reveal": ["value"], "percent_ring": ["value"], "bar_chart": ["values"],
                    "rating": ["value"], "poll": ["results"]}
SAFE_FILL = {"cta": {"text": "FOLLOW FOR MORE"}}

# Graphics that fill the frame with their own text: captions pause while they are up.
FULL_TEMPLATES = {"title_card", "word_stack", "versus", "before_after", "split_screen", "myth_vs_fact", "pros_cons",
                  "ranking", "timeline_steps", "bar_chart", "poll", "text_message", "post_card", "checklist",
                  "percent_ring", "rating", "price_tag", "countdown", "quote", "kinetic_text", "image_card"}
# caption_style keys that are numbers (captions.py reads them as such)
_CAPTION_NUMBERS = {"font_size_pct", "center_x_pct", "center_y_pct", "max_width_pct", "outline_pct", "lines_per_card"}
Y_FRACTION_PARAMS = {"alert_banner": "y", "emphasis_word": "y", "list_item": "y", "icon_badge": "y"}

# seconds from an sfx's start to its peak, so the peak lands on the cut
SFX_LEAD = {"whoosh": 0.41, "swoosh": 0.2, "riser": 2.0, "glitch": 0.05, "camera": 0.05, "typing": 0.0}
SFX_VOLUME = {"whoosh": 0.5, "swoosh": 0.5, "pop": 0.55, "click": 0.5, "tick": 0.45, "hit": 0.6, "boom": 0.55,
              "riser": 0.45, "ding": 0.45, "glitch": 0.45, "typing": 0.35, "camera": 0.5}
ENERGY_K = {"high": 1.0, "medium": 0.75, "calm": 0.5}
TRANSITION_D = {"whip": 0.2, "slide": 0.32, "zoom": 0.28, "fade": 0.36, "glitch": 0.2, "flash": 0.0, "cut": 0.0}


# =========================================================================== small helpers

def _isnum(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _finite(v):
    """v as a finite float (numbers and numeric text such as "12.5"), else None ("1e400", inf, "1,250", True).

    Text the templates could not convert ("1,250", "12%") counts as not a number: drawing it would fall back
    to the catalog's sample value.
    """
    if isinstance(v, bool) or not isinstance(v, (int, float, str)):
        return None
    try:
        f = float(v.strip() if isinstance(v, str) else v)
    except (ValueError, OverflowError):
        return None
    return f if math.isfinite(f) else None


def _bad_number(ptype, v):
    """True when v cannot be a template param of catalog type ptype (int, float, list[float]) as a finite number."""
    if ptype in ("int", "float"):
        return _finite(v) is None
    if ptype == "list[float]":
        items = [x for x in re.split(r"[,|\s]+", v.strip()) if x] if isinstance(v, str) else v
        return not isinstance(items, list) or any(_finite(x) is None for x in items)
    return False


def _call_params(call):
    """A template call's params as a dict, without numbers that are not finite (they fall back to the default).

    Only for drawing: validate_plan reports the same values as problems.
    """
    params = call.get("params") if isinstance(call, dict) else None
    if not isinstance(params, dict):
        return {}
    spec = templates.CATALOG.get(call.get("template"), {}).get("params", {}) \
        if isinstance(call.get("template"), str) else {}
    return {k: v for k, v in params.items()
            if not (k in spec and v is not None and _bad_number(spec[k]["type"], v))}


def _hex_ok(c):
    return isinstance(c, str) and bool(timeline.COLOR_RE.match(c.strip()))


def _rgb(c):
    c = c.lstrip("#")
    return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))


def _alpha(c, a):
    return c[:7].upper() + "%02X" % max(0, min(255, int(round(a * 255))))


def _suggest(name, choices):
    m = difflib.get_close_matches(str(name), list(choices), n=1, cutoff=0.6)
    return " (did you mean %r?)" % m[0] if m else ""


def media_kind(path):
    """"video", "image", "audio" or None, from the file extension."""
    ext = os.path.splitext(str(path))[1].lower()
    return "video" if ext in VIDEO_EXT else "image" if ext in IMAGE_EXT else "audio" if ext in AUDIO_EXT else None


def resolve_path(path, base_dir):
    """An absolute, normalised path (relative paths resolve against base_dir, else the current folder)."""
    if not isinstance(path, str) or not path.strip():
        return path
    p = os.path.expanduser(path.strip())
    if not os.path.isabs(p):
        p = os.path.join(base_dir or os.getcwd(), p)
    return os.path.normpath(os.path.abspath(p))


def _same_file(a, b):
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def media_duration(path):
    """Seconds of a media file (ffprobe), or None."""
    info = timeline.probe(path) if isinstance(path, str) else None
    return info.get("duration") if info else None


_ENERGY_ALIASES = {"hype": "high", "intense": "high", "fast": "high", "punchy": "high", "energetic": "high",
                   "normal": "medium", "mid": "medium", "moderate": "medium", "balanced": "medium",
                   "low": "calm", "chill": "calm", "slow": "calm", "soft": "calm", "relaxed": "calm"}


def _energy(st):
    e = str(st.get("energy") or "high").strip().lower()
    return e if e in ENERGIES else _ENERGY_ALIASES.get(e, "medium")


_HAN_RE = re.compile(r"[㐀-䶿一-鿿豈-﫿]")
_KANA_RE = re.compile(r"[぀-ヿｦ-ﾟ]")
_HANGUL_RE = re.compile(r"[가-힯]")
_CJK_RE = re.compile(r"[぀-ヿ㐀-䶿一-鿿豈-﫿가-힯ｦ-ﾟ]")
_CJK_BREAK = "、。，！？；："          # 、。，！？；：
_CYRILLIC_VOWELS = set("аеёиоуыэюяіїєў")
_GREEK_VOWELS = "αεηιουωάέήίόύώϊϋΐΰ"
_STOP_PUNCT = r"[.!?。！？]"
_PAUSE_PUNCT = r"[,;:、，；：]"


def _bare_north_indic_consonant(c):
    """A Devanagari, Bengali, Gurmukhi or Gujarati consonant letter (not an independent vowel)."""
    name = unicodedata.name(c, "")
    return 0x0915 <= ord(c) <= 0x0AFF and unicodedata.category(c) == "Lo" and " LETTER " in name \
        and not re.search(r"LETTER (?:[AEIOU]{1,2}|VOCALIC .*|CANDRA .*|SHORT .*)$", name)


def _syllables(word):
    """Spoken syllables in one token, for any script.

    Latin (accents folded): vowel groups with a silent final e. Cyrillic:
    vowels. Greek: vowel groups. Indic scripts: base letters minus viramas
    (each consonant carries a vowel). Han: one a character; kana and hangul
    a little less (morae and blocks are spoken faster). Other scripts (Arabic,
    Hebrew, Thai...): about one per two letters. Digits only: one each.
    """
    low = str(word).lower()
    latin, greek = [], []
    n, folded_end, other = 0.0, False, 0
    letters = [c for c in low if unicodedata.category(c)[:1] in ("L", "M")]
    if len(letters) > 1 and _bare_north_indic_consonant(letters[-1]):
        n -= 1          # Hindi, Bengali, Punjabi, Gujarati drop the vowel of a word-final bare consonant ("ek")
    for c in low:
        o = ord(c)
        if c.isascii():
            if "a" <= c <= "z":
                latin.append(c)
                folded_end = False
            continue
        if _HAN_RE.match(c):
            n += 1.0
        elif _KANA_RE.match(c):
            n += 0.6
        elif _HANGUL_RE.match(c):
            n += 0.8
        elif 0x00C0 <= o <= 0x024F or 0x1E00 <= o <= 0x1EFF:            # accented Latin: fold to the base letter
            base = unicodedata.normalize("NFKD", c)[:1]
            if "a" <= base <= "z":
                latin.append(base)
                folded_end = True
        elif 0x0400 <= o <= 0x052F:
            n += c in _CYRILLIC_VOWELS
        elif 0x0370 <= o <= 0x03FF or 0x1F00 <= o <= 0x1FFF:
            greek.append(c if unicodedata.normalize("NFD", c)[:1] in _GREEK_VOWELS else " ")
        elif 0x0900 <= o <= 0x0DFF:                                      # Devanagari ... Sinhala
            if unicodedata.category(c) == "Lo":
                n += 1
            elif "VIRAMA" in unicodedata.name(c, "") or "AL-LAKUNA" in unicodedata.name(c, ""):
                n -= 1
        elif c.isalpha():
            other += 1
    w = "".join(latin)
    if w:
        k = len(re.findall(r"[aeiouy]+", w))
        if w.endswith("e") and not w.endswith(("le", "ee")) and k > 1 and not folded_end:
            k -= 1
        n += max(1, k)
    if greek:
        n += len(re.findall(r"[^ ]+", "".join(greek)))
    if other:
        n += math.ceil(other / 2.0)
    if n <= 0 and not (w or greek or other):
        return max(1, len(re.sub(r"\D", "", low)))                     # digits: about one syllable each
    return max(1, int(round(n)))


def _speech_tokens(text, size=4):
    """Spoken tokens of a line: its words, with runs of CJK characters cut into pieces of up to `size`
    characters (at CJK punctuation first), so a line without spaces is timed and captioned in steps."""
    out = []
    for tok in clean_say(text).split():
        if not _CJK_RE.search(tok) or len(tok) <= size + 1:
            out.append(tok)
            continue
        cur, k = "", 0
        for ch in tok:
            if _CJK_RE.match(ch):
                if k >= size:
                    out.append(cur)
                    cur, k = "", 0
                k += 1
            cur += ch
            if ch in _CJK_BREAK:
                out.append(cur)
                cur, k = "", 0
        if cur:
            if out and not re.search(r"\w", cur):
                out[-1] += cur                                          # a closing mark stays with its words
            else:
                out.append(cur)
    return out


def speak_seconds(text, wpm=DEFAULT_WPM):
    """How long a line takes to say at wpm (syllable weighted, pauses at punctuation)."""
    toks = _speech_tokens(text)
    if not toks:
        return 0.0
    per_syl = 60.0 / max(60.0, float(wpm)) / 1.45   # ~1.45 syllables per spoken English word
    t = sum(_syllables(w) for w in toks) * per_syl
    t += sum(0.22 for w in toks[:-1] if re.search(_STOP_PUNCT + "$", w)) + \
        sum(0.1 for w in toks[:-1] if re.search(_PAUSE_PUNCT + "$", w))
    return t


def clean_say(text):
    """Spoken words only: stage directions, labels, markdown and emoji removed."""
    s = str(text or "")
    s = re.sub(r"\[[^\]]*\]|\([^)]*\)|\{[^}]*\}|<[^>]*>", " ", s)
    s = re.sub(r"[*_#`~|]+", " ", s)
    s = re.sub(r"[\U0001F000-\U0001FAFF☀-➿]", " ", s)
    s = s.replace("—", ", ").replace("–", ", ").replace(" - ", ", ")
    return re.sub(r"\s+", " ", s).strip()


def time_words(text, start, end, wpm=DEFAULT_WPM):
    """Words of `text` timed inside [start, end]: natural pace at wpm, compressed only when the beat is short.

    The same idea as captions.words_from_script, but each word's length
    follows its syllables and the line never spills out of its beat.
    """
    toks = _speech_tokens(text)
    if not toks or end - start < 0.2:
        return []
    per_syl = 60.0 / max(60.0, float(wpm)) / 1.45
    lens = [_syllables(w) * per_syl for w in toks]
    gaps = [0.22 if re.search(_STOP_PUNCT + "$", w) else 0.1 if re.search(_PAUSE_PUNCT + "$", w) else 0.0
            for w in toks]
    gaps[-1] = 0.0
    lead = min(0.1, (end - start) * 0.05)
    avail = max(0.15, (end - start) - lead - 0.08)
    natural = sum(lens) + sum(gaps)
    k = min(1.0, avail / natural) if natural > 0 else 1.0
    t, out = start + lead, []
    for w, d, g in zip(toks, lens, gaps):
        out.append({"w": w, "s": round(t, 3), "e": round(min(end, t + d * k), 3)})
        t += (d + g) * k
    return out


def _norm_word(w):
    return re.sub(r"[^\w$%]", "", str(w).lower())


_STOP = set("a an the and or but so to of in on at for with is are was were be been it its it's this that "
            "i you we they he she my your our their me us them do does did not no yes just very really "
            "can will would should could have has had from by as if then than there here what when how why "
            "who which all any some more most".split())


def _content_words(text):
    return [w for w in (_norm_word(x) for x in str(text).split()) if w and w not in _STOP]


# =========================================================================== validation

def _template_problems(where, call, errors):
    """Problems of one template call {"template", "params"} (appended to errors)."""
    if not isinstance(call, dict):
        errors.append("%s must be {\"template\": name, \"params\": {...}}, got %r" % (where, call))
        return
    name = call.get("template")
    if not isinstance(name, str):
        errors.append("%s: template must be a template name (text), got %r" % (where, name))
        return
    if name not in templates.CATALOG:
        errors.append("%s: unknown template %r%s (see the catalog)" % (where, name, _suggest(name, templates.CATALOG)))
        return
    params = call.get("params") or {}
    if not isinstance(params, dict):
        errors.append("%s (%s): params must be an object {\"name\": value}, got %r" % (where, name, params))
        return
    known = templates.CATALOG[name]["params"]
    for k in params:
        if k not in known:
            errors.append("%s (%s): unknown param %r%s; it takes: %s"
                          % (where, name, k, _suggest(k, known), ", ".join(known)))
        elif params[k] is not None and _bad_number(known[k]["type"], params[k]):
            errors.append("%s (%s): params.%s must be %s, got %r" % (
                where, name, k, "a list of finite numbers" if known[k]["type"].startswith("list") else
                "a finite number", params[k]))
    for k in REQUIRED_NUMBERS.get(name, []):
        if _blank(params.get(k)):
            errors.append("%s (%s): params.%s is required (a real number from the script; never invent one)"
                          % (where, name, k))
    # what the template would get after compile blanks the sample content it was not given
    essential = getattr(templates, "_REQUIRES", {}).get(name) or SAMPLE_PARAMS.get(name) or []
    filled = _fill_params(name, params, {})
    if essential and all(_blank(filled.get(k)) for k in essential):
        errors.append("%s (%s): give params.%s (it would draw nothing)" % (where, name, " or params.".join(essential)))
    for k in ("start", "end"):
        if k in call and not _isnum(call[k]):
            errors.append("%s (%s): %s must be seconds from the beat start" % (where, name, k))


def _blank(v):
    return v is None or (isinstance(v, str) and not v.strip()) or \
        (isinstance(v, list) and not any(str(x).strip() for x in v))


def visual_calls(visual):
    """A beat's `visual` as a list of template calls (null -> [], one call -> [call])."""
    if visual is None:
        return []
    return list(visual) if isinstance(visual, list) else [visual]


def validate_plan(plan, base_dir=None, assets=None):
    """Every problem with a plan as readable sentences; [] means compile_plan can build it.

    base_dir resolves relative asset paths; `assets`, when given, is the
    list of files the plan may use as backgrounds (anything else is an error).
    """
    if not isinstance(plan, dict):
        return ["the plan must be a JSON object, got %s" % type(plan).__name__]
    errs = []
    for k in plan:
        if k not in PLAN_KEYS:
            m = _suggest(k, PLAN_KEYS)
            if m:
                errs.append("unknown plan field %r%s" % (k, m))
    aspect = plan.get("aspect", "9:16")
    if not isinstance(aspect, str) or aspect not in ASPECTS:
        errs.append("aspect must be one of %s, got %r" % (", ".join(ASPECTS), aspect))
    if plan.get("fps") is not None and not (_isnum(plan["fps"]) and 5 <= plan["fps"] <= 60):
        errs.append("fps must be a number 5..60, got %r" % plan.get("fps"))
    dur = plan.get("duration")
    if dur is not None and not (_isnum(dur) and dur > 0):
        errs.append("duration must be seconds > 0, got %r" % dur)
    st = plan.get("style")
    if st is not None and not isinstance(st, dict):
        errs.append("style must be an object")
        st = {}
    st = st or {}
    for k in ("text", "accent", "bg", "bg2"):
        if k in st and st[k] is not None and not _hex_ok(st[k]):
            errs.append("style.%s must be a colour \"#RRGGBB\", got %r" % (k, st[k]))
    if st.get("energy") is not None and _energy(st) not in ENERGIES:
        errs.append("style.energy must be high, medium or calm")
    for k, lo, hi in (("wpm", 80, 320), ("cut_every", 0.3, 20), ("vignette", 0, 1), ("grain", 0, 1), ("scrim", 0, 1)):
        if st.get(k) is not None and not (_isnum(st[k]) and lo <= st[k] <= hi):
            errs.append("style.%s must be a number %s..%s, got %r" % (k, lo, hi, st[k]))
    cs = st.get("caption_style")
    if cs is not None:
        if not isinstance(cs, dict):
            errs.append("style.caption_style must be an object of captions.py keys")
        else:
            for k in cs:
                if k not in timeline.CAPTION_STYLE_KEYS:
                    errs.append("style.caption_style: unknown key %r%s" % (k, _suggest(k, timeline.CAPTION_STYLE_KEYS)))
                elif k in _CAPTION_NUMBERS and cs[k] is not None and not (_isnum(cs[k]) and cs[k] >= 0):
                    errs.append("style.caption_style.%s must be a number (copy the measured value), got %r"
                                % (k, cs[k]))
    gr = st.get("grade")
    if gr is not None and not (isinstance(gr, dict) and all(k in ("contrast", "saturation", "brightness", "warmth")
                                                         and _isnum(v) for k, v in gr.items())):
        errs.append("style.grade must be {\"contrast\", \"saturation\", \"brightness\", \"warmth\"} numbers")

    allowed = [resolve_path(a, base_dir) for a in assets] if assets is not None else None

    def check_file(where, path, kinds):
        if not isinstance(path, str) or not path.strip():
            errs.append("%s must be a file path" % where)
            return
        full = resolve_path(path, base_dir)
        if not os.path.isfile(full):
            errs.append("%s: file not found: %s" % (where, path))
        elif media_kind(full) not in kinds:
            errs.append("%s: %s is not a %s file" % (where, path, " or ".join(kinds)))
        elif allowed is not None and not any(_same_file(full, a) for a in allowed):
            errs.append("%s: %s is not one of the listed assets (use only those, by their exact path)" % (where, path))

    # music may be a video file too: its soundtrack is mixed (a song saved as .mp4 or .webm)
    for key, kinds in (("voiceover", ("audio", "video")), ("music", ("audio", "video"))):
        v = plan.get(key)
        if v is None:
            continue
        if not isinstance(v, dict):
            errs.append("%s must be an object or null" % key)
            continue
        if v.get("src") and not isinstance(v["src"], str):
            errs.append("%s.src must be a file path or null, got %r" % (key, v["src"]))
        elif v.get("src"):
            full = resolve_path(v["src"], base_dir)
            if not os.path.isfile(full):
                errs.append("%s.src: file not found: %s" % (key, v["src"]))
            elif media_kind(full) not in kinds:
                errs.append("%s.src: %s is not an %s file" % (key, v["src"], " or ".join(kinds)))
        if key == "voiceover" and v.get("words") is not None:
            ws = v["words"]
            if not (isinstance(ws, list) and all(isinstance(w, dict) and isinstance(w.get("w"), str)
                                                 and _isnum(w.get("s")) and _isnum(w.get("e")) for w in ws)):
                errs.append("voiceover.words must be [{\"w\": text, \"s\": seconds, \"e\": seconds}, ...]")
        if key == "music" and v.get("volume") is not None and not (_isnum(v["volume"]) and 0 <= v["volume"] <= 2):
            errs.append("music.volume must be a number 0..2")

    beats = plan.get("beats")
    if not isinstance(beats, list) or not beats:
        errs.append("beats must be a non-empty list")
        return errs
    prev = None
    for i, b in enumerate(beats):
        where = "beats[%d]" % i
        if not isinstance(b, dict):
            errs.append("%s must be an object" % where)
            continue
        for k in b:
            if k not in BEAT_KEYS:
                errs.append("%s: unknown field %r%s" % (where, k, _suggest(k, BEAT_KEYS)))
        s, e = b.get("start"), b.get("end")
        if not (_isnum(s) and _isnum(e)):
            errs.append("%s: start and end must be seconds" % where)
        else:
            if s < 0:
                errs.append("%s: start must be >= 0" % where)
            if e <= s:
                errs.append("%s: end (%g) must be after start (%g)" % (where, e, s))
            if i == 0 and s > 0.05:
                errs.append("beats[0] must start at 0 (the hook is on screen from the first frame), got %g" % s)
            if prev is not None and _isnum(prev.get("start")) and _isnum(prev.get("end")):
                if s < prev["start"]:
                    errs.append("%s starts before beats[%d]; keep beats in time order" % (where, i - 1))
                elif s - prev["end"] > 0.25:
                    errs.append("%s: %.2f s gap after beats[%d] (beats must be contiguous: start where the "
                                "previous ends)" % (where, s - prev["end"], i - 1))
                elif prev["end"] - s > 1.0:
                    errs.append("%s overlaps beats[%d] by %.2f s (overlap only by a transition, at most 1 s)"
                                % (where, i - 1, prev["end"] - s))
        if b.get("say") is not None and not isinstance(b["say"], str):
            errs.append("%s: say must be text or null" % where)
        for j, call in enumerate(visual_calls(b.get("visual"))):
            _template_problems("%s.visual%s" % (where, "[%d]" % j if isinstance(b.get("visual"), list) else ""),
                               call, errs)
        bg = b.get("background")
        if bg is not None:
            if not isinstance(bg, dict):
                errs.append("%s: background must be an object or null" % where)
            else:
                for k in bg:
                    if k not in BACKGROUND_KEYS:
                        errs.append("%s.background: unknown field %r%s" % (where, k, _suggest(k, BACKGROUND_KEYS)))
                kinds = [k for k in ("asset", "color", "gradient", "keep") if bg.get(k) not in (None, False)]
                if len(kinds) != 1:
                    errs.append("%s.background needs exactly one of asset, color, gradient, keep" % where)
                if bg.get("asset") is not None:
                    check_file("%s.background.asset" % where, bg["asset"], ("video", "image"))
                if bg.get("color") is not None and not _hex_ok(bg["color"]):
                    errs.append("%s.background.color must be \"#RRGGBB\"" % where)
                if bg.get("gradient") is not None and not (isinstance(bg["gradient"], list) and len(bg["gradient"]) >= 2
                                                           and all(_hex_ok(c) for c in bg["gradient"])):
                    errs.append("%s.background.gradient must be [\"#hex\", \"#hex\"]" % where)
                if bg.get("keep") and i == 0:
                    errs.append("beats[0] has no previous background to keep")
                z = bg.get("zoom")
                if z is not None and not (isinstance(z, list) and len(z) == 2 and all(_isnum(v) and 0.5 <= v <= 3 for v in z)):
                    errs.append("%s.background.zoom must be [from, to] scales, e.g. [1.0, 1.1]" % where)
                f = bg.get("focus")
                if f is not None and not (isinstance(f, list) and len(f) == 2 and all(_isnum(v) and 0 <= v <= 1 for v in f)):
                    errs.append("%s.background.focus must be [fx, fy] between 0 and 1" % where)
                for k in ("in", "volume"):
                    if bg.get(k) is not None and not (_isnum(bg[k]) and bg[k] >= 0):
                        errs.append("%s.background.%s must be a number >= 0" % (where, k))
                if bg.get("speed") is not None and not (_isnum(bg["speed"]) and 0.1 <= bg["speed"] <= 4):
                    errs.append("%s.background.speed must be 0.1..4" % where)
        if b.get("transition") not in (None,) + tuple(TRANSITIONS):
            errs.append("%s: transition must be one of %s, got %r%s"
                        % (where, ", ".join(TRANSITIONS), b.get("transition"), _suggest(b.get("transition"), TRANSITIONS)))
        if b.get("sfx") is not None and b["sfx"] not in sfx.NAMES:
            errs.append("%s: sfx must be one of %s or null, got %r%s"
                        % (where, ", ".join(sfx.NAMES), b["sfx"], _suggest(b["sfx"], sfx.NAMES)))
        if b.get("camera") not in (None,) + tuple(CAMERAS):
            errs.append("%s: camera must be one of %s, got %r" % (where, ", ".join(CAMERAS), b.get("camera")))
        prev = b
    last = beats[-1] if isinstance(beats[-1], dict) else {}
    if _isnum(dur) and _isnum(last.get("end")) and abs(last["end"] - dur) > 0.5:
        errs.append("the beats end at %.2f s but duration is %g s: beat lengths must add up to the duration"
                    % (last["end"], dur))
    cta = plan.get("cta")
    if cta is not None:
        _template_problems("cta", cta, errs)
    return errs


def resolve_assets(plan, assets, base_dir=None):
    """Point each background asset at the matching file in `assets` (by path or by file name). In place."""
    full = [resolve_path(a, base_dir) for a in assets or []]
    by_name = {}
    for a in full:
        by_name.setdefault(os.path.basename(a).lower(), a)
    beats = plan.get("beats") if isinstance(plan, dict) else None
    for b in beats if isinstance(beats, list) else []:
        bg = b.get("background") if isinstance(b, dict) else None
        if isinstance(bg, dict) and isinstance(bg.get("asset"), str) and bg["asset"].strip():
            p = resolve_path(bg["asset"], base_dir)
            if not os.path.isfile(p):
                hit = by_name.get(os.path.basename(bg["asset"].replace("\\", "/")).lower())
                if hit:
                    bg["asset"] = hit
    return plan


# =========================================================================== compile: plan -> timeline

def _style(plan):
    st = dict(STYLE_DEFAULTS)
    given = plan.get("style") if isinstance(plan.get("style"), dict) else {}
    for k, v in given.items():
        if v is not None:
            st[k] = v
    for k in ("text", "accent", "bg", "bg2"):
        if not _hex_ok(st.get(k)):
            st[k] = templates.DEFAULT_STYLE[k]
        st[k] = st[k].strip()[:7].upper()
    st["energy"] = _energy(st)
    return st


def _beats(plan, duration):
    """Beats sorted, gaps closed, the last one reaching the duration; returns (beats, duration)."""
    beats = plan.get("beats") if isinstance(plan.get("beats"), list) else []
    bs = [dict(b) for b in beats if isinstance(b, dict)
          and _isnum(b.get("start")) and _isnum(b.get("end")) and b["end"] > b["start"]]
    bs.sort(key=lambda b: b["start"])
    if not bs:
        raise ValueError("the plan has no beats with a start and an end")
    for b in bs:
        if not isinstance(b.get("say"), str):
            b["say"] = None            # only text is spoken (validate_plan reports anything else)
    if bs[0]["start"] <= 0.3:
        bs[0]["start"] = 0.0
    for a, b in zip(bs, bs[1:]):
        if b["start"] > a["end"]:
            a["end"] = b["start"]      # no black frames between beats
    end = bs[-1]["end"]
    if _isnum(duration) and duration > end:
        bs[-1]["end"] = end = float(duration)
    return bs, float(end)


def caption_band(style, H, W):
    """(centre %, half height %) of the caption line, kept inside the safe zone."""
    cs = style.get("caption_style") if isinstance(style.get("caption_style"), dict) else {}
    try:
        cap_mod = paths.load_skill("captions")
        base = dict(cap_mod.DEFAULT_CAPTION)
    except Exception:
        base = {"center_y_pct": 68.0, "font_size_pct": 4.6, "lines_per_card": 1}

    def num(key, default):
        for v in (cs.get(key), base.get(key)):
            if _isnum(v) and v > 0:
                return float(v)
        return default
    cy = num("center_y_pct", 68.0)
    size = num("font_size_pct", 4.6)
    lines = int(num("lines_per_card", 1))
    half = size * 1.25 * max(1, lines) / 2.0 + 0.6
    top, bottom = (11.0, 80.0) if H / float(W) >= 1.5 else (8.0, 88.0) if W > H else (7.0, 88.0)
    cy = max(top + half, min(bottom - half, cy))
    return cy, half


def _caption_style_for(style, H, W):
    """caption_style restricted to the keys captions.py reads, centre moved into the safe zone."""
    cs = style.get("caption_style")
    if not isinstance(cs, dict) or not cs:
        cs = {}
    # None is meaningful for the colour keys ("no box"), elsewhere it means "use the default"
    cs = {k: v for k, v in cs.items()
          if k in timeline.CAPTION_STYLE_KEYS and (v is not None or k in ("highlight", "outline", "box"))
          and (k not in _CAPTION_NUMBERS or v is None or _isnum(v))}
    if H / float(W) < 1.5 and "font_size_pct" not in cs:
        cs["font_size_pct"] = 6.2       # the default size is tuned for 9:16; wider frames need more of the height
    cy, _ = caption_band(dict(style, caption_style=cs), H, W)
    if cs or abs(cy - 68.0) > 0.01:
        cs["center_y_pct"] = round(cy, 2)
    # a caption line wider than the safe zone runs under the side margins
    widest = 88.0 if H / float(W) >= 1.5 else 90.0
    if isinstance(cs.get("max_width_pct"), (int, float)) and cs["max_width_pct"] > widest:
        cs["max_width_pct"] = widest
    return cs or None


def _avoid_captions(name, params, cy, half, H, W):
    """Move a template's placement params away from the caption band (cy, half in % of height)."""
    out = dict(params)
    spec = templates.CATALOG[name]["params"]
    portrait = H / float(W) >= 1.5
    top, bottom = (11.0, 80.0) if portrait else (7.0, 88.0)
    if "position" in spec:
        pos = out.get("position") or spec["position"]["default"]
        choices = spec["position"].get("choices") or []
        if cy >= 58 and pos == "bottom":
            pos = "center" if "center" in choices else "top"
        elif 42 < cy < 58 and pos == "center":
            pos = "top"
        elif cy <= 42 and pos == "top":
            pos = "center" if "center" in choices else pos
        if pos in choices:
            out["position"] = pos
    key = Y_FRACTION_PARAMS.get(name)
    if key and key in spec:
        frac = out.get(key) if _isnum(out.get(key)) else spec[key]["default"]
        y = top + frac * (bottom - top)
        if abs(y - cy) < half + 9:
            y = cy - half - 12 if cy - half - 12 >= top + 4 else cy + half + 12
            out[key] = round(max(0.0, min(1.0, (y - top) / (bottom - top))), 3)
    return out


def _fill_params(name, params, ctx):
    """Plan params -> build params: samples blanked, safe fills, kinetic word times, ranking title."""
    spec = templates.CATALOG[name]["params"]
    p = {k: v for k, v in (params or {}).items() if k in spec}
    for k in SAMPLE_PARAMS.get(name, []):
        if k not in p or p[k] is None:
            p[k] = [] if spec[k]["type"].startswith("list") else ""
    for k, v in SAFE_FILL.get(name, {}).items():
        if _blank(p.get(k)):
            p[k] = v
    if name == "ranking" and _blank(p.get("title")) and isinstance(p.get("items"), list) and p["items"]:
        p["title"] = "TOP %d" % len(p["items"])
    if name == "kinetic_text" and not p.get("times") and ctx.get("words"):
        # the beat's spoken words drive the build when the template shows the same line
        tw = [_norm_word(w) for w in str(p.get("text") or "").split()]
        sw = ctx["words"]
        if tw and len(tw) == len(sw) and sum(1 for a, b in zip(tw, sw) if a == _norm_word(b["w"])) >= 0.7 * len(tw):
            p["times"] = [round(w["s"], 3) for w in sw]
    return p


def _overlap_share(say, calls):
    """Share of the spoken line's content words that a beat's templates already show.

    A line made only of small words ("Here is why.") is compared on all its words.
    """
    words = _content_words(say)
    pick = _content_words
    if not words:
        words = [w for w in (_norm_word(x) for x in str(say).split()) if w]
        pick = lambda text: [w for w in (_norm_word(x) for x in str(text).split()) if w]  # noqa: E731
    if not words:
        return 0.0
    shown = set()
    for c in calls:
        shows_number = REQUIRED_NUMBERS.get(c.get("template"), []) if isinstance(c.get("template"), str) else []
        for k, v in _call_params(c).items():
            vals = v if isinstance(v, list) else [v]
            for x in vals:
                if isinstance(x, str):
                    shown.update(pick(x))
                elif k in shows_number and _isnum(x):      # the counter's 12 is the spoken "12"
                    shown.add(("%f" % x).rstrip("0").rstrip("."))
    return sum(1 for w in words if w in shown) / float(len(words))


def _glow_png(color, size=256):
    """A soft radial light (cached PNG) used for the ambient backdrop."""
    folder = paths.ensure(os.path.join(paths.CACHE, "director"))
    path = os.path.join(folder, "glow_%s_v1.png" % color.lstrip("#").upper())
    if not os.path.isfile(path):
        import numpy as np
        from PIL import Image
        r, g, b = _rgb(color)
        ys, xs = np.mgrid[0:size, 0:size].astype(np.float32)
        d = np.sqrt((xs + 0.5 - size / 2) ** 2 + (ys + 0.5 - size / 2) ** 2) / (size / 2)
        a = np.clip(1 - d, 0, 1) ** 2.2                      # smooth falloff to 0 at the edge
        img = np.zeros((size, size, 4), np.uint8)
        img[..., 0], img[..., 1], img[..., 2] = r, g, b
        img[..., 3] = np.round(a * 255).astype(np.uint8)
        tmp = path + ".part.png"
        Image.fromarray(img, "RGBA").save(tmp)
        os.replace(tmp, path)
    return path


def _ranges(flags):
    """[(start, end)] runs of beats whose flag is True; flags = [(start, end, bool)]."""
    out = []
    for s, e, f in flags:
        if not f:
            continue
        if out and abs(out[-1][1] - s) < 1e-6:
            out[-1] = (out[-1][0], e)
        else:
            out.append((s, e))
    return out


def _ambient_layers(ranges, W, H, st):
    """Two slow glows (accent and a lifted bg2) drifting on Lissajous paths behind text-only beats."""
    layers = []
    acc = st["accent"]
    soft = "#" + "".join("%02X" % min(255, int(c * 1.6 + 40)) for c in _rgb(st["bg2"]))
    k = ENERGY_K[st["energy"]]
    glows = [(acc, 1.25, 0.30 + 0.06 * k, (0.28, 0.30), (0.22, 0.12), (13.0, 17.0, 0.0)),
             (soft, 1.05, 0.22, (0.74, 0.72), (0.18, 0.14), (19.0, 11.0, 1.7))]
    for gi, (col, size, opa, (cx, cy), (ax, ay), (tx, ty, ph)) in enumerate(glows):
        src = _glow_png(col)
        D = max(W, H) * size
        for ri, (s, e) in enumerate(ranges):
            xs, ys = [], []
            n = max(2, int(math.ceil((e - s) / 0.5)) + 1)
            for j in range(n):
                t = min(e, s + j * 0.5)
                lt = round(t - s, 3)
                xs.append([lt, round(W * (cx + ax * math.sin(2 * math.pi * t / tx + ph)), 1)])
                ys.append([lt, round(H * (cy + ay * math.sin(2 * math.pi * t / ty + ph * 0.6)), 1)])
            layers.append({"type": "image", "id": "ambient.glow%d.%d" % (gi + 1, ri + 1), "src": src, "fit": "fill",
                           "w": round(D, 1), "h": round(D, 1), "start": round(s, 3), "end": round(e, 3),
                           "opacity": round(opa, 3), "blend": "screen", "animate": {"x": xs, "y": ys}})
    return layers


def _scrim_layers(ranges, W, H, strength):
    """Top and bottom darkening over footage so text and captions read on any shot."""
    if strength <= 0:
        return []
    top_a, bot_a = min(1.0, 0.62 * strength), min(1.0, 0.95 * strength)
    stops = []
    for j in range(9):
        y = j / 8.0
        a = top_a * max(0.0, 1 - y / 0.38) ** 1.6 + bot_a * max(0.0, (y - 0.48) / 0.52) ** 1.4
        stops.append(_alpha("#000000", min(1.0, a)))
    return [{"type": "shape", "id": "scrim.%d" % (i + 1), "shape": "rect", "w": W, "h": H, "x": W / 2.0, "y": H / 2.0,
             "color": "#000000", "gradient": stops, "start": round(s, 3), "end": round(e, 3), "fixed": True}
            for i, (s, e) in enumerate(ranges)]


def _bg_layer(i, bg, s, e, W, H, st, base_dir, cut_every, cursor):
    """Background layers (a list: video beats may be cut into sub-shots) for one beat."""
    if not isinstance(bg, dict):
        return []

    def pair(v, lo, hi):
        return list(v) if isinstance(v, list) and len(v) == 2 and all(_isnum(x) and lo <= x <= hi for x in v) else None

    if isinstance(bg.get("asset"), str) and bg["asset"].strip():
        src = resolve_path(bg["asset"], base_dir)
        kind = media_kind(src)
        zoom = pair(bg.get("zoom"), 0.5, 3) or [1.0, 1.06 if i % 2 == 0 else 1.0]
        if zoom == [1.0, 1.0]:
            zoom = None
        focus = pair(bg.get("focus"), 0, 1) or [0.5, 0.5]
        if kind == "image":
            return [{"type": "image", "id": "bg.%d" % i, "src": src, "fit": "cover", "w": W, "h": H, "focus": focus,
                     "kenburns": zoom or [1.0, 1.05], "start": s, "end": e}]
        speed = float(bg["speed"]) if _isnum(bg.get("speed")) and 0.1 <= bg["speed"] <= 4 else 1.0
        src_len = media_duration(src)
        length = e - s
        n = 1
        if cut_every and length > 1.6 * cut_every:
            n = max(1, int(round(length / float(cut_every))))
        shots = []
        explicit = _isnum(bg.get("in"))
        t_in = float(bg["in"]) if explicit else cursor.get(src, 0.0)
        for k in range(n):
            a, b = s + length * k / n, s + length * (k + 1) / n
            need = (b - a) * speed
            loop = False
            if src_len:
                if need > src_len - 0.05:
                    loop, t_in = True, 0.0                       # shorter than the shot: loop it
                elif t_in + need > src_len - 0.05:
                    # a plan's own in point is pulled back to fit; the running cursor wraps to the start
                    t_in = max(0.0, src_len - need - 0.05) if explicit and k == 0 else 0.0
            # the first shot uses the plan's zoom; later sub-shots alternate push and pull
            kb = zoom if k == 0 else ([1.06, 1.0] if k % 2 else [1.0, 1.06])
            shots.append({"type": "video", "id": "bg.%d%s" % (i, "" if n == 1 else ".%d" % (k + 1)), "src": src,
                          "in": round(t_in, 3), "speed": speed, "fit": "cover", "focus": focus, "w": W, "h": H,
                          "volume": float(bg["volume"]) if _isnum(bg.get("volume")) and bg["volume"] >= 0 else 0.0,
                          "kenburns": kb, "loop": loop,
                          "start": round(a, 3), "end": round(b, 3)})
            t_in += need + (0.35 if n > 1 else 0.0)        # a small jump between sub-shots reads as a cut
        cursor[src] = t_in
        return shots
    if _hex_ok(bg.get("color")):
        return [{"type": "shape", "id": "bg.%d" % i, "shape": "rect", "w": W, "h": H, "x": W / 2.0, "y": H / 2.0,
                 "color": bg["color"].strip()[:9], "start": s, "end": e}]
    grad = bg.get("gradient")
    if isinstance(grad, list) and len(grad) >= 2 and all(_hex_ok(c) for c in grad):
        angle = float(bg.get("angle", 90) if _isnum(bg.get("angle", 90)) else 90)
        diag = math.ceil(math.hypot(W, H)) + 4
        return [{"type": "shape", "id": "bg.%d" % i, "shape": "rect", "w": diag, "h": diag, "x": W / 2.0,
                 "y": H / 2.0, "color": bg["gradient"][0], "gradient": list(bg["gradient"]),
                 "rotation": round(angle - 90.0, 3), "start": s, "end": e}]
    return []


def _transition(prev_layers, new_layers, tr, cut, d, W, effects, energy):
    """Motion on the outgoing and incoming shots plus frame effects for the cut at `cut`."""
    if tr in ("flash", "glitch", "zoom"):
        if tr == "flash":
            effects.append({"type": "flash", "start": round(max(0.0, cut - 0.02), 3), "duration": 0.2,
                            "color": "#FFFFFF", "opacity": 0.85 if energy == "high" else 0.7})
        elif tr == "glitch":
            effects.append({"type": "glitch", "start": round(max(0.0, cut - 0.06), 3), "duration": 0.24,
                            "intensity": 1.0 if energy == "high" else 0.7})
        else:
            effects.append({"type": "zoom-blur", "start": round(max(0.0, cut - d / 2), 3), "duration": round(d, 3),
                            "intensity": 1.0})
    if d <= 0 or tr in ("cut", "flash"):
        return
    out_l = prev_layers[-1] if prev_layers else None
    in_l = new_layers[0] if new_layers else None
    enter = {"fade": "crossfade", "whip": "whip-left", "slide": "slide-left", "zoom": "zoom-out",
             "glitch": "glitch"}[tr]
    # how the outgoing shot leaves; None = it simply stays underneath until the incoming one covers it
    leave = {"fade": None, "whip": "whip-left", "slide": "slide-left", "zoom": "zoom-in", "glitch": None}[tr]
    if in_l is not None:
        in_l["enter"] = {"type": enter, "duration": round(d, 3)}
        if tr == "slide":
            in_l["enter"]["distance"] = W
    if out_l is not None:
        out_l["end"] = round(max(out_l["end"], cut + d), 3)   # overlap the incoming shot for the transition
        if in_l is None and not leave:
            leave = "fade"                                    # nothing comes in: fade to the backdrop
        if leave:
            out_l["exit"] = {"type": leave, "duration": round(d, 3)}
            if tr == "slide":
                out_l["exit"]["distance"] = W


def _camera(beats, cuts, transitions, W, energy, effects):
    """Camera keyframes for the beat moves (footage only: templates and captions are HUD layers)."""
    k = ENERGY_K[energy]
    zoom, xs = [], []
    side = 1
    for i, b in enumerate(beats):
        s = cuts[i]
        e = cuts[i + 1] if i + 1 < len(beats) else b["end"]
        life = max(0.05, e - s)
        move = b.get("camera") or "none"
        if move == "punch":
            z = [(0.0, 1.0, None), (min(0.16, life * 0.4), 1.0 + 0.13 * k, "out-back"), (life, 1.0 + 0.15 * k, "linear")]
            x = [(0.0, 0.0, None), (life, 0.0, None)]
        elif move == "push":
            z = [(0.0, 1.0, None), (life, 1.0 + 0.09 * k, "in-out-quad")]
            x = [(0.0, 0.0, None), (life, 0.0, None)]
        elif move == "drift":
            z0 = 1.035 + 0.01 * k
            z = [(0.0, z0, None), (life, z0 + 0.03 * k, "in-out-quad")]
            amp = W * 0.012 * side
            side = -side
            x = [(0.0, -amp, None), (life, amp, "in-out-quad")]
        else:
            z = [(0.0, 1.0, None), (life, 1.0, None)]
            x = [(0.0, 0.0, None), (life, 0.0, None)]
            if move == "shake":
                effects.append({"type": "shake", "start": round(s, 3), "duration": round(min(0.4, life), 3),
                                "intensity": round(16 * k, 1)})
        smooth = i > 0 and transitions[i] == "fade"
        for keys, track in ((z, zoom), (x, xs)):
            for j, (t, v, ease) in enumerate(keys):
                T = round(s + t, 3)
                if j == 0:
                    if i == 0:
                        track.append([T, round(v, 4)])
                    elif smooth:
                        continue      # glide into the next move over the crossfade instead of jumping
                    else:
                        track.append([T, round(v, 4), "step"])
                    continue
                if j == len(keys) - 1 and i + 1 < len(beats):
                    T = round(max(s + 0.01, e - 0.001), 3)
                key = [T, round(v, 4)]
                if ease and ease != "linear":
                    key.append(ease)
                track.append(key)
    # drop keys that do nothing (a flat camera is no camera)
    out = {}
    if any(abs(kf[1] - 1.0) > 1e-6 for kf in zoom):
        out["zoom"] = zoom
    if any(abs(kf[1]) > 1e-6 for kf in xs):
        out["x"] = xs
    return out


def _cta_call(plan):
    cta = plan.get("cta")
    if not isinstance(cta, dict) or not isinstance(cta.get("template"), str) or cta["template"] not in templates.CATALOG:
        return None
    for b in plan.get("beats") or []:
        for c in visual_calls(b.get("visual") if isinstance(b, dict) else None):
            if isinstance(c, dict) and c.get("template") in ("cta", "subscribe") and c.get("template") == cta["template"]:
                return None       # the plan already closes on it
    return cta


def compile_plan(plan, base_dir=None):
    """PLAN -> TIMELINE SPEC (dict). Raises ValueError for a plan with no usable beats.

    base_dir resolves relative asset paths (default: the current folder).
    The spec carries absolute paths, so it renders from anywhere.
    """
    if not isinstance(plan, dict):
        raise ValueError("the plan must be a JSON object")
    aspect = plan.get("aspect") if isinstance(plan.get("aspect"), str) and plan["aspect"] in ASPECTS else "9:16"
    W, H = ASPECTS[aspect]
    fps = int(plan["fps"]) if _isnum(plan.get("fps")) and 5 <= plan["fps"] <= 60 else 30
    st = _style(plan)
    energy = st["energy"]
    beats, D = _beats(plan, plan.get("duration"))
    canvas = {"width": W, "height": H, "fps": fps}
    tstyle = {k: st[k] for k in ("font", "font_secondary", "text", "accent", "bg", "bg2", "energy")}
    notes = []
    cut_every = st.get("cut_every") if _isnum(st.get("cut_every")) and st["cut_every"] >= 0.4 else None

    # ----- transitions, cut times
    transitions = [None] + [b.get("transition") if b.get("transition") in TRANSITIONS else "cut" for b in beats[1:]]
    cuts = [b["start"] for b in beats]
    durs = []
    for i, tr in enumerate(transitions):
        if i == 0 or tr in (None, "cut", "flash"):
            durs.append(0.0)
            continue
        d = TRANSITION_D[tr] * (1.4 if energy == "calm" else 1.15 if energy == "medium" else 1.0)
        shorter = min(beats[i - 1]["end"] - beats[i - 1]["start"], beats[i]["end"] - beats[i]["start"])
        durs.append(round(max(0.08, min(d, 0.4 * shorter)), 3))

    # ----- backgrounds
    effects, bg_layers = [], []
    cursor, cur = {}, None
    has_footage = []
    for i, b in enumerate(beats):
        s = cuts[i]
        e = cuts[i + 1] if i + 1 < len(beats) else b["end"]
        bg = b.get("background")
        if isinstance(bg, dict) and bg.get("keep") and cur:
            cur[-1]["end"] = round(e, 3)          # the running shot plays on; only frame effects mark the beat
            has_footage.append(has_footage[-1])
            if transitions[i] in ("flash", "glitch", "zoom"):
                _transition([], [], transitions[i], s, durs[i], W, effects, energy)
            continue
        new = _bg_layer(i, bg, round(s, 3), round(e, 3), W, H, st, base_dir, cut_every, cursor)
        if i > 0:
            _transition(cur or [], new, transitions[i], s, durs[i], W, effects, energy)
        bg_layers.extend(new)
        cur = new or None
        has_footage.append(any(L["type"] in ("video", "image") for L in new))

    # ----- captions words (timeline time)
    vo = plan.get("voiceover") if isinstance(plan.get("voiceover"), dict) else {}
    vo_words = [w for w in vo.get("words") or [] if isinstance(w, dict) and isinstance(w.get("w"), str)
                and _isnum(w.get("s")) and _isnum(w.get("e"))]
    wpm = st["wpm"] if _isnum(st.get("wpm")) and 80 <= st["wpm"] <= 320 else DEFAULT_WPM
    beat_words = []
    for i, b in enumerate(beats):
        s, e = b["start"], (beats[i + 1]["start"] if i + 1 < len(beats) else b["end"])
        if vo_words:
            beat_words.append([w for w in vo_words if s - 1e-6 <= w["s"] < e])
        else:
            beat_words.append(time_words(b.get("say") or "", s, e, wpm))

    cap_style = _caption_style_for(st, H, W)
    cy, half = caption_band(dict(st, caption_style=cap_style or {}), H, W)
    show_caps = bool(st.get("captions"))

    # ----- templates per beat
    tpl_layers = []
    cta = _cta_call(plan)
    cap_words = []
    for i, b in enumerate(beats):
        s, e = b["start"], b["end"]
        calls = [c for c in visual_calls(b.get("visual")) if isinstance(c, dict)
                 and isinstance(c.get("template"), str) and c["template"] in templates.CATALOG]
        if cta is not None and i == len(beats) - 1:
            if calls and e - s >= 2.4:
                split = round(s + (e - s) * 0.5, 3)
                calls = [dict(c, end=split - s) if "end" not in c else c for c in calls]
                calls.append(dict(cta, start=split - s))
            else:
                calls = [cta]
        names = [c["template"] for c in calls]
        # a full-screen graphic carries its own text, and a template that already shows the line would
        # make the caption a duplicate (sound-off viewers still read it there)
        hide = (not st.get("captions_over_graphics") and any(n in FULL_TEMPLATES for n in names)) \
            or _overlap_share(b.get("say") or "", calls) >= 0.6
        if show_caps and not hide:
            cap_words.extend(beat_words[i])
        for j, c in enumerate(calls):
            name = c["template"]
            given = _call_params(c)            # numbers that are not finite count as not given
            missing = [k for k in REQUIRED_NUMBERS.get(name, []) if _blank(given.get(k))]
            if missing:
                notes.append("beat %d: %s dropped (no %s given; numbers are never invented)"
                             % (i + 1, name, ", ".join(missing)))
                continue
            ctx = {"words": beat_words[i]}
            params = _fill_params(name, given, ctx)
            if show_caps and not hide:
                params = _avoid_captions(name, params, cy, half, H, W)
            ts = s + (float(c["start"]) if _isnum(c.get("start")) else 0.0)
            te = s + (float(c["end"]) if _isnum(c.get("end")) else (e - s))
            ts, te = max(s, ts), min(e, te)
            if te - ts < 0.2:
                continue
            for L in templates.build(name, params, round(ts, 3), round(te, 3), canvas, tstyle):
                L["id"] = "b%02d.%s" % (i + 1, L.get("id") or name)
                L["fixed"] = True
                tpl_layers.append(L)

    # the reel ends on its last graphics held, not fading: the loop back to the hook is a clean cut
    for L in tpl_layers:
        if L.get("exit") and float(L.get("end") or 0) >= D - 1e-3:
            L["exit"] = None

    # ----- ambient, scrim, progress
    layers = []
    flags = [(cuts[i], cuts[i + 1] if i + 1 < len(beats) else beats[i]["end"], not has_footage[i])
             for i in range(len(beats))]
    if st.get("ambient"):
        layers += _ambient_layers(_ranges(flags), W, H, st)
    layers += bg_layers
    footage = _ranges([(s, e, not f) for s, e, f in flags])
    layers += _scrim_layers(footage, W, H, float(st["scrim"]) if _isnum(st.get("scrim")) else 0.0)
    layers += tpl_layers
    if st.get("progress_bar"):
        for L in templates.build("progress_bar", {"position": "top"}, 0.0, D, canvas, tstyle):
            L["id"] = "hud." + (L.get("id") or "progress")
            layers.append(L)

    # ----- camera and looks
    camera = _camera(beats, cuts, transitions, W, energy, effects)
    if isinstance(st.get("grade"), dict) and st["grade"]:
        effects.append(dict({"type": "grade"}, **{k: v for k, v in st["grade"].items()
                                                  if k in ("contrast", "saturation", "brightness", "warmth")}))
    if _isnum(st.get("vignette")) and st["vignette"] > 0:
        effects.append({"type": "vignette", "strength": round(min(1.0, st["vignette"]), 3)})
    if _isnum(st.get("grain")) and st["grain"] > 0:
        effects.append({"type": "grain", "strength": round(min(1.0, st["grain"]), 3)})

    # ----- audio
    audio = []
    vol_k = float(st.get("sfx_volume") or 1.0) if _isnum(st.get("sfx_volume")) else 1.0
    for i, b in enumerate(beats):
        name = b.get("sfx")
        if name not in sfx.NAMES:
            continue
        at = max(0.0, cuts[i] - SFX_LEAD.get(name, 0.0))
        audio.append({"sfx": name, "start": round(at, 3), "volume": round(SFX_VOLUME.get(name, 0.5) * vol_k, 3),
                      "role": "sfx"})
    if isinstance(vo.get("src"), str) and vo["src"].strip():
        audio.append({"src": resolve_path(vo["src"], base_dir), "start": 0.0, "role": "voice", "volume": 1.0})
    mu = plan.get("music") if isinstance(plan.get("music"), dict) else {}
    if isinstance(mu.get("src"), str) and mu["src"].strip():
        audio.append({"src": resolve_path(mu["src"], base_dir), "start": 0.0, "role": "music", "duck": True, "loop": True,
                      "volume": float(mu["volume"]) if _isnum(mu.get("volume")) else 0.22,
                      "in": float(mu["in"]) if _isnum(mu.get("in")) else 0.0,
                      "fade_in": 0.25, "fade_out": round(min(1.5, D * 0.1), 2), "duration": round(D, 3)})

    captions = None
    if show_caps and cap_words:
        font = st.get("caption_font") or ((cap_style or {}).get("font_candidates") or [None])[0] or st["font"]
        captions = {"words": cap_words, "style": cap_style, "font": font, "pop": energy != "calm"}

    spec = {"version": timeline.SCHEMA_VERSION, "width": W, "height": H, "fps": fps, "duration": round(D, 3),
            "background": {"gradient": [st["bg"], st["bg2"]], "angle": 90}, "layers": layers, "camera": camera,
            "effects": effects, "captions": captions, "audio": audio,
            "meta": {"title": str(plan.get("title") or "reel"), "source": "director",
                     "notes": "; ".join(notes + ([str(plan["notes"])] if plan.get("notes") else [])),
                     "beats": [{"start": round(b["start"], 3), "end": round(b["end"], 3), "say": b.get("say") or ""}
                               for b in beats],
                     # every spoken word, also those not captioned (the line is on screen as a graphic): QA measures pace on it
                     "spoken_words": [w for ws in beat_words for w in ws]}}
    return spec


# =========================================================================== offline planner

_FILLER_START = re.compile(r"^(?:so|okay|ok|look|listen|alright|all right|well|now|and|but|basically|honestly|um+|uh+|"
                           r"here'?s the thing|the thing is)[,:]?\s+", re.I)
_REQUEST = re.compile(r"^\s*(?:please\s+)?(?:make|create|do|build|give|write)\s+(?:me\s+)?(?:a|an|the)?\s*"
                      r"(?:\d+\s*(?:s|sec|second)s?\s+)?(?:viral\s+|short\s+|quick\s+)?"
                      r"(?:reel|video|short|tiktok|clip|motion graphics?(?: video)?)\s+(?:about|on|for|explaining|"
                      r"showing|where|that)\s+", re.I)
_ORDINALS = ["first", "second", "third", "fourth", "fifth", "sixth", "seventh", "eighth", "ninth", "tenth"]
_NUMWORDS = {w: i + 1 for i, w in enumerate(["one", "two", "three", "four", "five", "six", "seven", "eight",
                                                "nine", "ten"])}
_LIST_RE = re.compile(r"^(?:(?P<ord>%s)(?:\s+(?:one|thing|tip|step|rule|mistake|reason|way|habit))?|"
                      r"(?:number|tip|step|rule|mistake|reason|no\.?|#)\s*#?(?P<num>\d+|%s))\b[\s,:.\-]*"
                      % ("|".join(_ORDINALS), "|".join(_NUMWORDS)), re.I)
_BULLET_RE = re.compile(r"^\s*(?:(?P<n>\d+)[.)]|[-*•])\s+(?P<t>.+)$")
_NUM_RE = re.compile(r"(?P<pre>[$€£])?(?P<num>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)\s?"
                     r"(?P<suf>%|percent\b|x\b|k\b|m\b|b\b|million\b|billion\b|thousand\b)?", re.I)
_CTA_RE = re.compile(r"\b(follow|subscribe|comment|save this|share this|link in (?:my )?bio|dm me|part (?:2|two))\b",
                     re.I)
_WARN_RE = re.compile(r"^(?:stop|don'?t|never|avoid|quit)\b", re.I)
_POWER = set("never always only secret mistake mistakes free fast stop wrong truth nobody everyone best worst "
             "biggest instantly double doubled triple half zero exactly".split())
_UNITS = re.compile(r"\b(views|followers|subscribers|subs|people|users|customers|sales|likes|comments|downloads|"
                    r"hours|days|weeks|months|years|videos|posts|times|dollars|bucks|members|clients|students)\b", re.I)


# A full stop after these is not the end of a sentence ("vs.", "Dr. Lee", "e.g."); after the second set only
# when the next word is capitalised ("etc. Then ...").
_ABBREV = {"vs", "v", "dr", "mr", "mrs", "ms", "prof", "st", "mt", "e.g", "i.e", "cf", "approx", "ca"}
_ABBREV_END = {"etc", "inc", "ltd", "co", "corp", "jr", "sr", "u.s", "u.k", "a.m", "p.m", "no"}
_SENT_END_RE = re.compile(r"[.!?]+[\"'”’)\]]*(?=\s|$)|[。！？]+[\"'”’」)\]]*")


def _ends_sentence(text, i, j):
    """True when the punctuation text[i:j] closes a sentence (not a decimal point, domain or abbreviation)."""
    mark = text[i:j]
    if not mark.startswith(".") or mark.rstrip("\"'”’)]") != ".":
        return True                                     # ! ? ... and CJK stops always end the sentence
    word = re.search(r"([A-Za-z][A-Za-z.]*)$", text[:i])
    word = word.group(1).lower() if word else ""
    nxt = text[j:].lstrip()[:1]
    if word in _ABBREV:
        return False
    if word in _ABBREV_END or re.fullmatch(r"(?:[a-z]\.)+[a-z]", word):
        if word == "no":
            return not (nxt.isdigit() or nxt == "#")    # "No. 1 rule" goes on; "No. I didn't." ends
        return nxt.isupper()
    return True


def _sentences(text):
    """Sentences with their end punctuation.

    Splits after . ! ? (and the CJK stops) followed by a space or the end, so
    "3.5x", "$1,250.50", "vstudio.app" and abbreviations ("vs.", "Dr.",
    "e.g.") stay inside their sentence; lowercase scripts still split.
    """
    text = str(text or "")
    out, start = [], 0
    for m in _SENT_END_RE.finditer(text):
        if not _ends_sentence(text, m.start(), m.end()):
            continue
        out.append(text[start:m.end()].strip())
        start = m.end()
    out.append(text[start:].strip())
    return [s for s in out if re.search(r"\w", s)]


def _split_long(sentence, max_words=13):
    words = sentence.split()
    if len(words) <= max_words:
        return [sentence]
    # break at a comma, semicolon or a pivot word nearest the middle
    best, mid = None, len(words) / 2.0
    for i in range(3, len(words) - 2):
        if re.search(r"[,;:]$", words[i - 1]) or words[i].lower() in ("but", "because", "so", "and", "which", "until"):
            if best is None or abs(i - mid) < abs(best - mid):
                best = i
    if best is None:
        # no comma or pivot word: cut before a word that opens a phrase ("... 12 times | to find out why"), else
        # nearest the middle where the first part does not end on a word that leaves it hanging ("... of my | own")
        bare = [re.sub(r"[^\w']", "", w.lower().replace("’", "'")) for w in words]
        inner = [i for i in range(3, len(words) - 2) if bare[i - 1] not in _DANGLING]
        best = min((i for i in inner if bare[i] in _PHRASE_OPENERS and abs(i - mid) <= len(words) / 4.0),
                   key=lambda i: abs(i - mid), default=None)
        if best is None:
            best = min(inner, key=lambda i: abs(i - mid), default=None)
    cut = best or int(mid)
    return _split_long(" ".join(words[:cut]), max_words) + _split_long(" ".join(words[cut:]), max_words)


def _units(text):
    """Spoken units [{"text", "list_no"}] from a script or a prompt."""
    text = str(text or "").replace("\r", "")
    units = []
    for raw in text.split("\n"):
        line = raw.strip()
        if not line:
            continue
        m = _BULLET_RE.match(line)
        if m:
            units.append({"text": clean_say(m.group("t")), "list_no": int(m.group("n")) if m.group("n") else -1})
            continue
        try:
            sp = paths.load_skill("spoken")
            kept = sp.spoken_lines(line)
            line = " ".join(kept) if kept else ""
        except Exception:
            pass
        line = clean_say(line)
        if not line:
            continue
        for s in _sentences(line):
            for part in _split_long(s):
                units.append({"text": part.strip(), "list_no": None})
    # number un-numbered bullets in order
    n = 0
    for u in units:
        if u["list_no"] == -1:
            n += 1
            u["list_no"] = n
    return [u for u in units if u["text"]]


def _prompt_units(prompt):
    """A bare prompt -> units: 'X: a, b and c' becomes a hook and three list items."""
    p = _REQUEST.sub("", clean_say(prompt)).strip()
    p = _cap_first(p) if p else p                   # "iPhone vs. Android" keeps its own casing
    m = re.match(r"^(?P<head>[^:]{6,}):\s*(?P<rest>.+)$", p)
    if m:
        items = [x.strip(" .") for x in re.split(r",\s*(?:and\s+|or\s+)?|\s+and\s+|;\s*", m.group("rest")) if x.strip(" .")]
        if 2 <= len(items) <= 8:
            return [{"text": m.group("head").strip() + ".", "list_no": None}] + \
                [{"text": _cap_first(it) + ".", "list_no": k + 1} for k, it in enumerate(items)]
    return _units(p)


# Words a short on-screen line never ends on: it would read as cut off ("FOLLOW FOR PART 2 WHERE").
_DANGLING = set("a an the and or but so nor yet to of in on at for with from by as into onto about over under "
                "through than like without within before after until while where when whenever if because since "
                "though although unless whether which who whom whose that my your our their his her its is are was "
                "were be been being am will would should could can may might must shall do does did have has had "
                "i we they he she not very really just every each some any i'm i'll we're we'll you're you'll".split())
# Words that open a new clause: cutting just before one keeps a whole phrase on screen.
_CLAUSE = set("where when whenever if because before after until while which who whom whose that so but unless "
              "since though although whereas then".split())
_SUBJECTS = set("i i'll i'm i've we we'll we're you you'll you're they it it's this he she".split())
# Words a spoken phrase can open with: a long line with no comma is split before one of them.
_PHRASE_OPENERS = _CLAUSE | set("to for with from about into without in on at by over under near through across "
                                "and or".split())


def _clause_start(words, i):
    """True when words[i] opens a new clause ("and" only before a subject: "Comment GUIDE and I'll send it")."""
    w = _norm_word(words[i]).replace("’", "'")
    if w in _CLAUSE:
        return True
    return w in ("and", "or") and i + 1 < len(words) and re.sub(r"[^\w']", "", words[i + 1].lower()) in _SUBJECTS


def _trim_dangling(words, keep=2):
    words = list(words)
    while len(words) > keep and re.sub(r"[^\w']", "", words[-1].lower().replace("’", "'")) in _DANGLING:
        words.pop()
    return words


def headline(text, max_words=7, cap=True):
    """A short on-screen version of a spoken line (fillers off, cut at a natural break, capitalised).

    A line too long for the card is cut at a comma, before a new clause or
    at max_words, and never ends on a word that leaves it hanging ("and",
    "where", "before", "your").
    """
    s = clean_say(text)
    for _ in range(3):
        s2 = _FILLER_START.sub("", s)
        if s2 == s:
            break
        s = s2
    s = re.sub(r"[.;,:]+$", "", s).strip()
    words = s.split()
    if len(words) > max_words + 2:       # a whole short sentence reads better than a clipped one
        parts = re.split(r"(?<=[,;:])\s+|\s+(?=but\b|because\b|so\b|which\b)", s)
        head = re.sub(r"[,;:]+$", "", parts[0]).split()
        if 2 <= len(head) <= max_words and len(_trim_dangling(head)) == len(head):
            s = " ".join(head)
        else:
            cut = max((i for i in range(2, max_words + 1) if _clause_start(words, i)), default=max_words)
            s = " ".join(_trim_dangling(words[:cut]))
        s = re.sub(r"[,;:]+$", "", s)
    return _cap_first(s) if cap else s


def _cap_first(s):
    """s with its first letter upper case, except a word that has its own casing ("iPhone", "eBay")."""
    first = s.split(" ", 1)[0]
    if any(c.isupper() for c in first[1:]):
        return s
    return s[:1].upper() + s[1:]


def cta_text(text, max_words=7):
    """The ask in a closing line as card text: from the call to action to the end of its phrase.

    "So if this helped, share this with a friend who needs it" -> "Share this with a friend";
    "Follow for part 2 where I break down the hooks" -> "Follow for part 2".
    """
    s = clean_say(text)
    m = _CTA_RE.search(s)
    if m:
        s = s[m.start():]                 # the ask itself, without its lead-in
    words = s.split()
    for i in range(1, len(words)):
        if re.search(r"[,;:.!?]$", words[i - 1]) or _clause_start(words, i):
            words = words[:i]
            break
    words = _trim_dangling(words[:max_words], keep=1)
    out = re.sub(r"[.;,:!?]+$", "", " ".join(words)).strip()
    return _cap_first(out) if out else headline(text, 5)


def _power_words(text):
    """Words to paint in the accent: numbers first, then strong words, then the longest content word."""
    words = text.split()
    nums = [w for w in words if re.search(r"\d", w)]
    if nums:
        return [re.sub(r"[^\w$%.,]", "", nums[0]).strip(".,")]
    power = [w for w in words if _norm_word(w) in _POWER]
    if power:
        return [re.sub(r"[^\w'$%]", "", power[0])]
    content = sorted((w for w in words if len(_norm_word(w)) >= 5 and _norm_word(w) not in _STOP),
                     key=lambda w: -len(w))
    return [re.sub(r"[^\w'$%]", "", content[0])] if content else []


def _number(text):
    """The headline number of a line as {value, prefix, suffix, decimals, raw, kind, unit, span, from}, or None.

    "from 200 to 12,000 subscribers" gives 12,000 (counting up from 200).
    """
    rng = re.search(r"\bfrom\s+[$\u20ac\u00a3]?(\d[\d,.]*)\s*(?:\w+\s+)?to\s+(?=[$\u20ac\u00a3]?\d)", text, re.I)
    if rng:
        tail = _number(text[rng.end():])
        if tail:
            a, b = tail["span"]
            tail["span"] = (a + rng.end(), b + rng.end())
            tail["span_from"] = rng.span()
            try:
                tail["from"] = float(rng.group(1).replace(",", "").rstrip("."))
            except ValueError:
                pass
            if tail["kind"] == "plain":
                tail["kind"] = "count"
            return tail
    for m in _NUM_RE.finditer(text):
        raw_num = m.group("num")
        # years and list markers are not stats
        if not m.group("pre") and not m.group("suf") and re.fullmatch(r"(19|20)\d\d", raw_num):
            continue
        val = float(raw_num.replace(",", ""))
        suf = (m.group("suf") or "").lower()
        pre = m.group("pre") or ""
        after = text[m.end():m.end() + 30]
        if not pre and re.match(r"\s*dollars?\b|\s*bucks\b", after, re.I):
            pre = "$"
        big = suf in ("k", "m", "b", "million", "billion", "thousand")
        kind = "percent" if suf in ("%", "percent") else "money" if pre else "multiple" if suf == "x" else \
            "count" if _UNITS.search(after) or big else "plain"
        if kind in ("plain", "count") and val < 10 and not big:
            continue          # "3 settings", "2 times": a small count is not a stat
        dec = len(raw_num.split(".")[1]) if "." in raw_num else 0
        short = {"k": "K", "thousand": "K", "m": "M", "million": "M", "b": "B", "billion": "B"}.get(suf, "")
        raw = (pre + raw_num + ("%" if kind == "percent" else "X" if kind == "multiple" else short)).strip()
        unit = (_UNITS.search(after).group(1) if _UNITS.search(after) else "").lower()
        return {"value": val, "prefix": pre, "suffix": short, "decimals": dec, "raw": raw, "kind": kind,
                "unit": unit, "span": (m.start(), m.end()), "from": None}
    return None


def _label_around(text, num):
    """The words around a number, shortened into a stat label."""
    a = (num.get("span_from") or num["span"])[0]
    after = clean_say(re.sub(r"^\s*(?:percent|dollars?|bucks|x)\b", "", text[num["span"][1]:], flags=re.I))
    after = after.strip(" ,.:;-!?")
    # "73% of viewers decide..." reads on its own; the words before only help a short tail ("I lost $300 today"),
    # and only when they are few: "I rebuilt the first room of my own game 12 times" is labelled "times", never
    # "I rebuilt the first room of my own game times" (the captions carry the rest)
    before = clean_say(text[:a]).strip(" ,.:;-")
    rest = after if len(after.split()) >= 3 or (after and len(before.split()) > 4) else (before + " " + after).strip()
    rest = re.sub(r"^(?:because|and|but|so|that|which|while|that's|thats|it's|its)\s+", "", rest, flags=re.I)
    return headline(rest, 8, cap=False) if rest else ""


def _visual_for(u, i, n, ctx):
    """(template call or None, kind) for one spoken unit."""
    text = u["text"]
    head = headline(text)
    words = text.split()
    if i == 0:
        hl = _power_words(head)
        return {"template": "hook_title", "params": {"text": head, "highlight": hl,
                                                     "look": "stroke" if ctx["footage"] else "shadow"}}, "hook"
    if i == n - 1 and _CTA_RE.search(text):
        return {"template": "cta", "params": {"text": cta_text(text), "icon": "plus" if re.search(
            r"follow|subscribe", text, re.I) else "comment" if re.search(r"comment", text, re.I) else "bookmark"}}, "cta"
    if u.get("list_no"):
        body = headline(_LIST_RE.sub("", text) or text, 7)
        return {"template": "list_item", "params": {"number": u["list_no"], "text": body,
                                                    "total": ctx["list_total"]}}, "list"
    m = _LIST_RE.match(text)
    if m and len(words) > 2:
        no = m.group("num")
        no = _NUMWORDS.get(no.lower(), None) if no and not no.isdigit() else int(no) if no else \
            _ORDINALS.index(m.group("ord").lower()) + 1
        body = headline(text[m.end():] or text, 7)
        if re.match(r"^\s*step\b", text, re.I):
            return {"template": "step_number", "params": {"number": no, "text": body}}, "list"
        return {"template": "list_item", "params": {"number": no, "text": body, "total": ctx["list_total"]}}, "list"
    vs = re.split(r"\s+(?:vs\.?|versus)\s+", text, flags=re.I)
    if len(vs) == 2:
        # the two or three words on each side of "vs" are the contenders
        left = [w.strip(".,!?;:") for w in clean_say(vs[0]).split()[-3:]]
        right = [w.strip(".,!?;:") for w in clean_say(vs[1]).split()[:3]]
        return {"template": "versus", "params": {"left": " ".join(left), "right": " ".join(right)}}, "versus"
    if text.rstrip().endswith("?"):
        return {"template": "question", "params": {"text": headline(text, 9).rstrip("?") + "?"}}, "question"
    q = re.search(r"[\"“]([^\"”]{12,140})[\"”]", text)
    if q:
        who = re.search(r"(?:said|says|—|-)\s+([A-Z][\w.]+(?:\s+[A-Z][\w.]+){0,2})", text)
        return {"template": "quote", "params": {"text": q.group(1).strip(), "author": who.group(1) if who else ""}}, "quote"
    num = _number(text)
    if num:
        label = _label_around(text, num)
        if num["kind"] in ("money", "count") and num["value"] >= 10 and not ctx["used"].get("counter_reveal"):
            ctx["used"]["counter_reveal"] = 1
            params = {"value": num["value"], "prefix": num["prefix"], "suffix": num["suffix"],
                      "decimals": num["decimals"], "label": label}
            if num.get("from") is not None and num["from"] < num["value"]:
                params["from"] = num["from"]
            return {"template": "counter_reveal", "params": params}, "number"
        if num["kind"] == "percent" and 0 < num["value"] <= 100 and ctx["used"].get("stat"):
            return {"template": "percent_ring", "params": {"value": num["value"], "label": label,
                                                           "decimals": num["decimals"]}}, "number"
        ctx["used"]["stat"] = 1
        return {"template": "stat", "params": {"value": num["raw"], "label": label}}, "number"
    if _WARN_RE.match(text) and not ctx["used"].get("alert_banner"):
        ctx["used"]["alert_banner"] = 1
        return {"template": "alert_banner", "params": {"text": headline(text, 4)}}, "warn"
    if len(words) <= 3:
        return {"template": "emphasis_word", "params": {"word": re.sub(r"[.!,;:]+$", "", head)}}, "punch"
    # plain lines: kinetic type on text-only reels; on footage alternate kinetic type with captions alone
    ctx["plain"] += 1
    if not ctx["footage"] or ctx["plain"] % 2 == 1:
        mode = "punch" if ctx["energy"] == "high" and len(words) <= 9 else "build"
        return {"template": "kinetic_text", "params": {"text": clean_say(text).rstrip("."), "mode": mode,
                                                       "highlight": _power_words(text)}}, "plain"
    key = _power_words(text)
    if key and ctx["energy"] != "calm" and ctx["plain"] % 4 == 0:
        return {"template": "emphasis_word", "params": {"word": key[0], "y": 0.3}}, "punch"
    return None, "plain"


_TRANSITION_CYCLE = {"high": ["whip", "cut", "flash", "zoom", "cut", "slide", "glitch", "cut"],
                     "medium": ["slide", "cut", "fade", "zoom", "cut", "whip"],
                     "calm": ["fade", "cut", "fade", "slide"]}
_CAMERA_CYCLE = {"high": ["drift", "push", "drift", "punch"], "medium": ["push", "drift", "none", "drift"],
                 "calm": ["drift", "none", "push"]}
_TRANSITION_SFX = {"whip": "whoosh", "slide": "swoosh", "zoom": "whoosh", "flash": "hit", "glitch": "glitch"}
_KIND_SFX = {"hook": "boom", "number": "hit", "list": "pop", "question": "pop", "punch": "hit", "warn": "glitch",
             "cta": "ding", "versus": "hit", "quote": None, "plain": None}


def unreadable_reason(path, kind):
    """Why a video or image file cannot be drawn ("no video stream ffprobe can read"), or None when it can.

    Videos are probed with ffprobe (when ffprobe is missing nothing is
    claimed: the file is kept); images are opened and verified with Pillow.
    """
    if kind == "image":
        try:
            from PIL import Image
            with Image.open(path) as im:
                im.verify()
        except ImportError:
            return None
        except Exception as e:                 # Pillow raises many kinds for a broken or unsupported file
            return "not an image Pillow can open (%s)" % (str(e).splitlines()[0][:120] if str(e) else type(e).__name__)
        return None
    if kind == "video":
        if not shutil.which("ffprobe"):
            return None
        info = timeline.probe(path)
        if not info or not info.get("has_video"):
            return "no video stream ffprobe can read (a damaged, unfinished or renamed file?)"
    return None


def asset_rows(assets, skipped=None):
    """[{"path", "kind", "duration"}] for the existing, readable video and image files among `assets`.

    A file that exists with a media extension but cannot be decoded is left
    out (it would fail the whole render); `skipped`, a list, gets a
    (path, reason) for each.
    """
    rows = []
    for a in assets or []:
        p = os.path.abspath(a)
        k = media_kind(p)
        if k in ("video", "image") and os.path.isfile(p):
            why = unreadable_reason(p, k)
            if why:
                if skipped is not None:
                    skipped.append((p, why))
                continue
            rows.append({"path": p, "kind": k, "duration": media_duration(p) if k == "video" else None})
    return rows


def _word_ends_sentence(tok, nxt=""):
    """True when a transcript token closes a sentence ("views." yes; "vs.", "Dr.", "2.5M" no)."""
    m = re.search(r"(?:[.!?]+|[。！？]+)[\"'”’)\]]*$", str(tok))
    if not m:
        return False
    return _ends_sentence(str(tok) + " " + str(nxt or ""), m.start(), m.end())


def sentences_from_words(words, pause=0.45):
    """Timed words grouped into sentences: by punctuation when the transcript has it, else by pauses."""
    out, cur = [], []
    ends = [_word_ends_sentence(w["w"], words[i + 1]["w"] if i + 1 < len(words) else "") for i, w in enumerate(words)]
    punct = sum(ends) >= max(2, len(words) // 40)
    for i, w in enumerate(words):
        cur.append(w)
        gap = words[i + 1]["s"] - w["e"] if i + 1 < len(words) else 9
        if (ends[i] if punct else gap >= pause) or gap >= 1.2 or len(cur) >= 16:
            out.append(cur)
            cur = []
    if cur:
        out.append(cur)
    return out


def heuristic_plan(prompt, script=None, style=None, duration=30, assets=None, aspect="9:16", words=None):
    """An offline PLAN from a prompt (and script, voiceover words, assets): good enough to ship a draft.

    words: timeline words of a voiceover ([{"w","s","e"}]); beats then follow
    its sentences and the reel lasts as long as the voice.
    """
    st = dict(STYLE_DEFAULTS)
    st.update({k: v for k, v in (style or {}).items() if v is not None})
    st["energy"] = _energy(st)
    energy = st["energy"]
    wpm = st["wpm"] if _isnum(st.get("wpm")) and 80 <= st["wpm"] <= 320 else DEFAULT_WPM
    target = float(duration) if _isnum(duration) and duration > 0 else 30.0
    skipped = []
    rows = asset_rows(assets, skipped)
    notes = ["asset %s could not be read, skipped: %s" % (os.path.basename(p), why) for p, why in skipped]

    if words:
        sents = sentences_from_words(words)
        units = [{"text": " ".join(w["w"] for w in s), "list_no": None, "s": s[0]["s"]} for s in sents]
    else:
        units = _units(script) if script and str(script).strip() else []
        if not units:
            units = _prompt_units(prompt or "")
    if not units:
        # emoji, symbols or stage directions only: nothing to say or show, so the hook is a plain opener
        units = [{"text": "Watch this.", "list_no": None}]
        notes.append("the prompt has no words to put on screen; the hook says \"Watch this\" (give a script with "
                     "--script for real lines)")

    def is_item(u):
        return bool(u.get("list_no") or (_LIST_RE.match(u["text"]) and len(u["text"].split()) > 2))

    # a script that opens on its first item ("Mistake one: ...") would give that item to the hook and the list
    # would start at 2/3: the prompt's own first line opens the reel instead, so every item keeps its card
    if not words and script and str(prompt or "").strip() and is_item(units[0]):
        opener = _prompt_units(prompt)
        if opener and not is_item(opener[0]):
            units = [dict(opener[0], list_no=None)] + units
    list_total = sum(1 for u in units if u.get("list_no")) or \
        sum(1 for u in units if _LIST_RE.match(u["text"]) and len(u["text"].split()) > 2)
    # very short fragments ride with the next line (except the hook and real punches)
    merged = []
    for u in units:
        if merged and len(merged[-1]["text"].split()) <= 1 and len(merged) > 1 and not merged[-1].get("list_no") \
                and energy != "high" and not words:
            merged[-1] = dict(u, text=merged[-1]["text"] + " " + u["text"])
        else:
            merged.append(u)
    units = merged
    # a one- or two-word opener ("Wait.") is not a hook on its own: it opens the hook line instead
    if len(units) > 1 and len(units[0]["text"].split()) <= 2 and not units[0].get("list_no") and not words:
        units = [dict(units[1], text=units[0]["text"] + " " + units[1]["text"])] + units[2:]
    has_cta = bool(_CTA_RE.search(units[-1]["text"])) if units else False

    # ----- timing
    min_len = {"high": 1.1, "medium": 1.5, "calm": 2.0}[energy]
    max_len = {"high": 4.2, "medium": 5.2, "calm": 6.5}[energy]
    if words:
        # the voice sets the clock: one beat per spoken sentence, a CTA card after it when it has none
        starts = [0.0] + [u["s"] for u in units[1:]]
        end = words[-1]["e"] + 0.5
        spans = [(round(a, 3), round(b, 3)) for a, b in zip(starts, starts[1:] + [end])]
        if not has_cta:
            spans.append((round(end, 3), round(end + 2.2, 3)))
            units = units + [{"text": "", "list_no": None, "cta": True}]
        notes.append("beats follow the voiceover (%.1f s)" % end)
    else:
        lens = [max(min_len, speak_seconds(u["text"], wpm) + 0.35) for u in units]
        cta_len = 0.0 if has_cta else 2.2
        natural = sum(lens) + cta_len
        if natural > target * 1.25:
            notes.append("the script needs about %.0f s at %d wpm, longer than the %g s target; the reel runs long "
                         "rather than rushing the words" % (natural, wpm, target))
            k = 1.0
        elif natural > target:
            k = (target - cta_len) / sum(lens)          # speak a little faster
        else:
            k = min((target - cta_len) / sum(lens), max_len / max(lens)) if sum(lens) else 1.0
            k = max(1.0, k)
        hook_cap = {"high": 3.0, "medium": 3.5, "calm": 4.5}[energy]
        lens = [min(l * k, max(l, hook_cap)) if j == 0 else l * k for j, l in enumerate(lens)]
        total = sum(lens) + cta_len
        if total < target - 0.5:
            # not enough to say: hold the CTA a little longer, never pad a beat past the cap
            extra = target - total
            add_cta = min(extra, 1.5)
            if has_cta:
                lens[-1] += add_cta
            else:
                cta_len += add_cta
            if target - (sum(lens) + cta_len) > 0.5:
                notes.append("the text only fills %.0f s, so the reel is shorter than the %g s target"
                             % (sum(lens) + cta_len, target))
        t, spans = 0.0, []
        for l in lens:
            spans.append((round(t, 3), round(t + l, 3)))
            t += l
        if cta_len:
            spans.append((round(t, 3), round(t + cta_len, 3)))
            units = units + [{"text": "", "list_no": None, "cta": True}]

    # ----- beats
    ctx = {"footage": bool(rows), "list_total": list_total, "energy": energy, "used": {}, "plain": 0}
    beats, n = [], len(units)
    asset_i, tcycle, ccycle = 0, _TRANSITION_CYCLE[energy], _CAMERA_CYCLE[energy]
    for i, (u, (s, e)) in enumerate(zip(units, spans)):
        if u.get("cta"):
            call, kind = {"template": "cta", "params": {"text": "FOLLOW FOR MORE", "icon": "plus"}}, "cta"
        else:
            call, kind = _visual_for(u, i, n, ctx)
        bg = None
        if rows:
            r = rows[asset_i % len(rows)]
            asset_i += 1
            bg = {"asset": r["path"], "zoom": [1.0, 1.08] if i % 2 == 0 else [1.08, 1.0]}
        if i == 0:
            tr, cam = None, "punch" if energy == "high" else "push"
        else:
            tr = tcycle[(i - 1) % len(tcycle)]
            if kind == "cta":
                tr = "zoom" if energy != "calm" else "fade"
            cam = "punch" if kind in ("number", "punch") and energy != "calm" else ccycle[i % len(ccycle)]
        sound = _TRANSITION_SFX.get(tr) or _KIND_SFX.get(kind)
        if kind == "hook":
            sound = "boom" if energy == "high" else "hit" if energy == "medium" else None
        beats.append({"start": s, "end": e, "say": u["text"] or None, "visual": call, "background": bg,
                      "transition": tr, "sfx": sound, "camera": cam})
    dur = beats[-1]["end"]
    title = headline(units[0]["text"], 8) if units and units[0]["text"] else headline(prompt or "reel", 8)
    plan_style = {k: st[k] for k in ("font", "font_secondary", "text", "accent", "bg", "bg2", "energy")}
    plan_style.update({"captions": bool(st.get("captions", True)), "progress_bar": bool(st.get("progress_bar", True)),
                       "wpm": wpm})
    for k in ("cut_every", "caption_style", "grade", "vignette", "grain", "scrim", "ambient", "caption_font"):
        if st.get(k) is not None and st.get(k) != STYLE_DEFAULTS.get(k):
            plan_style[k] = st[k]
    plan = {"title": title, "aspect": aspect if aspect in ASPECTS else "9:16", "fps": 30, "duration": round(dur, 3),
            "style": plan_style, "voiceover": {"src": None, "words": words or None}, "music": {"src": None, "volume": 0.22},
            "beats": beats, "cta": None, "notes": "; ".join(["offline plan (no LLM)"] + notes)}
    return plan


# =========================================================================== CLI

def _read_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def main(argv=None):
    """vstudio plan compile plan.json --out spec.json | validate plan.json | heuristic --prompt ... --out plan.json"""
    argv = list(sys.argv[1:] if argv is None else argv)
    cmd = argv.pop(0) if argv and argv[0] in ("compile", "validate", "heuristic") else "compile"
    ap = argparse.ArgumentParser(prog="vstudio plan %s" % cmd, description="PLAN (beats) -> TIMELINE SPEC")
    if cmd in ("compile", "validate"):
        ap.add_argument("plan", help="plan.json")
        if cmd == "compile":
            ap.add_argument("--out", required=True, help="spec.json to write")
        a = ap.parse_args(argv)
        try:
            plan = _read_json(a.plan)
        except (OSError, ValueError) as e:
            print("could not read %s: %s" % (a.plan, e), file=sys.stderr)
            return 2
        base = os.path.dirname(os.path.abspath(a.plan))
        errors = validate_plan(plan, base_dir=base)
        for e in errors:
            print("plan: " + e, file=sys.stderr)
        if cmd == "validate":
            print("plan is valid" if not errors else "%d problem(s)" % len(errors))
            return 0 if not errors else 1
        try:
            spec = compile_plan(plan, base_dir=base)
        except ValueError as e:
            print("could not compile: %s" % e, file=sys.stderr)
            return 1
        problems = timeline.validate(spec)
        for e in problems:
            print("spec: " + e, file=sys.stderr)
        timeline.save(spec, a.out)
        print("wrote %s (%d layers, %.1f s)%s" % (os.path.abspath(a.out), len(spec["layers"]), spec["duration"],
                                                  " with %d problem(s)" % len(problems) if problems else ""))
        return 1 if problems else 0
    ap.add_argument("--prompt", default="")
    ap.add_argument("--script", help="script file (.txt/.md)")
    ap.add_argument("--asset", action="append", default=[])
    ap.add_argument("--duration", type=float, default=30.0)
    ap.add_argument("--aspect", default="9:16", choices=sorted(ASPECTS))
    ap.add_argument("--energy", choices=ENERGIES)
    ap.add_argument("--out", required=True, help="plan.json to write")
    ap.add_argument("--spec", help="also compile to this spec.json")
    a = ap.parse_args(argv)
    script = None
    if a.script:
        with open(a.script, encoding="utf-8", errors="replace") as f:
            script = f.read()
    if not (a.prompt or script):
        print("give --prompt or --script", file=sys.stderr)
        return 2
    plan = heuristic_plan(a.prompt, script, {"energy": a.energy} if a.energy else None, a.duration, a.asset, a.aspect)
    os.makedirs(os.path.dirname(os.path.abspath(a.out)) or ".", exist_ok=True)
    with open(a.out, "w", encoding="utf-8") as f:
        json.dump(plan, f, indent=2, ensure_ascii=False)
        f.write("\n")
    print("wrote %s (%d beats, %.1f s)" % (os.path.abspath(a.out), len(plan["beats"]), plan["duration"]))
    if a.spec:
        spec = compile_plan(plan, base_dir=os.path.dirname(os.path.abspath(a.out)))
        timeline.save(spec, a.spec)
        print("wrote %s" % os.path.abspath(a.spec))
    return 0


if __name__ == "__main__":
    sys.exit(main())
