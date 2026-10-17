---
name: script-forge
description: >-
  Write short-form video scripts that sound like a real person talking, not
  like AI: learns the creator's voice from their own transcripts, borrows the
  rhythm of outlier videos in the niche, writes three angles, stress-tests them
  on a simulated viewer panel that marks the second each viewer would swipe,
  fixes the weak beats, and scores the result as speech. Outputs a timed
  script with on-screen text, shots and delivery notes in the reference
  video's caption style. Use for "write me a script", "make it sound human",
  "this sounds like ChatGPT", "script for this trend", "hook for this", or
  as the writing stage of /viral.
---

# script-forge

AI scripts fail for one reason: they're written to be read. People talk in
fragments, start sentences with "and", repeat themselves, give one oddly
specific number, and stop mid-thought when the point lands. This skill writes
**for the ear** and proves it with numbers.

Never write from the model's default voice. Every script is anchored to real
human speech: the creator's own, and the outlier videos'.

## Inputs (gather once, reuse)

1. **The creator's voice.** 3+ transcripts or captions of their own videos:

   ```bash
   python3 .claude/skills/script-forge/scripts/voiceprint.py their/*.srt --out workspace/voice.json
   ```

   No recordings yet? Ask for a 60-second voice note about their niche (any
   phone transcribes it), or three captions they wrote themselves. Last
   resort: build the profile from the outlier creators' transcripts
   (`workspace/dna/*/transcript.json`) and say so.
2. **The trend and its reference.** From `/trend-hunt` (`RADAR.md`) and, for
   the chosen trend, `/viral-extract` on the best example: its `STYLE_DNA.md`
   gives pace (wpm), sentence rhythm, hook length, re-hook spacing, caption
   style and length.
3. **The format playbook.** `references/formats.md` for the trend's format.
4. **brand.md.** Audience, what they will film, and anything off-limits.

## Step 1: find the true story (before any wording)

Write three raw bullets, no style at all: **the claim** (one sentence), **the
proof** (the number, the moment, the before/after; ask the user if missing,
never invent), **the turn** (what most people get wrong, or what happened
next). If you cannot fill the proof bullet with something real, stop and ask.

## Step 2: three angles, written as dictation

Write three drafts, each a different angle on the same story: usually
**contrarian** ("stop doing X"), **story** (in medias res, past tense, "I"),
**proof/demo** (show the result first). Write each one as if the creator is
saying it into their phone to a friend who asked:

- Start on the claim, conflict or result. Never "hey guys", "in this video",
  "did you know", "have you ever".
- Sentences at the voiceprint's mean length, but at least a third under six
  words. One long run-on where the energy rises.
- Contract everything they'd contract. "And", "but", "so" can start sentences.
- One real number in the first two lines. One proper noun or a time ("last
  Tuesday", "at 2am") somewhere.
- Re-hook every 8 to 10 seconds with a turn: "but", "so then", "wait",
  "except". Match the reference's re-hook spacing from `STYLE_DNA.md`.
- Use one of the creator's signature phrases if it fits naturally.
- No moral at the end. End on the payoff, a loop back to line one, or one
  single ask.
- Length = target seconds x reference wpm / 60 words.

Then a **read-aloud pass**: say every line in your head at the reference pace.
Anything you'd stumble on, rewrite. Anything that sounds like a caption,
rewrite.

## Step 3: score as speech

```bash
python3 .claude/skills/script-forge/scripts/scriptcheck.py a.md b.md c.md \
  --seconds 35 --wpm <reference wpm> --voice workspace/voice.json
```

Fix every red flag. The bar: **80+ overall and no check under 50**. Then run
`ig-human/detect.py` on the spoken lines as a second opinion.

## Step 4: swipe test (simulated viewer panel)

Send the top two drafts to the `viewer-panel` agent: it simulates 6 to 8 distinct
viewers (from brand.md's audience, plus one cold scroller who doesn't know
the creator and one skeptic) watching the script beat by beat with
timestamps. Each viewer returns the second they'd swipe away, the line that
lost them, and whether they'd comment, share or follow.

Aggregate into a predicted retention line (share still watching at 3 s, 10 s,
50%, end). Rewrite the beat where most viewers left, re-score, and run the
panel once more on the winner. Two rounds maximum.

Say plainly this is a simulation that catches weak beats, not a prediction of
real numbers.

## Step 5: production sheet

Output `workspace/content/<week>/<slug>/script.md`:

```
TITLE (internal)   ...
TREND              <trend name> from RADAR.md, reference: <url> (Nx)
LENGTH             ~34 s at 172 wpm      SCORE  scriptcheck 86, panel 3s-hold 7/8

| time      | spoken (exactly as said)            | on-screen text (caption style)   | shot / B-roll            | delivery            |
| 0:00-0:02 | I lost $300 on this one setting.    | I LOST $300                      | close-up, phone in hand  | flat, no smile      |
| 0:02-0:05 | And nobody warned me.               | NOBODY WARNED ME                 | jump cut, push in        | beat after "And"    |
...

CAPTIONS   font / size / colours / words per card from STYLE_DNA.md
CAPTION    (hand to /ig-caption or /yt-seo)
ALT HOOKS  the two runner-up hooks with scores, for A/B on a repost
```

The spoken column is what scriptcheck scored. Do not "polish" it after the
gate: polishing is how it turns back into AI.

## Never

- Invent a number, a client, a result or a quote. Ask.
- Copy a line from a reference transcript. Borrow rhythm and structure only.
- Ship a draft below 80 or with a red check.
