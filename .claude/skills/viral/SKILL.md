---
name: viral
description: >-
  The hyper agent's main loop for a one-person social media business. Give it
  just a niche and it finds every trend worth riding (multi-source radar),
  extracts the exact style of the best example of each (captions, font, pace,
  hook, edit), and writes ready-to-shoot scripts in the creator's own voice,
  stress-tested on a simulated viewer panel; then every platform's version,
  scheduling with approval, and a learning loop. Use for "make me go viral",
  "<niche> - give me scripts", "run the content machine", "what should I post
  and write it", "make this week's content", "do everything".
---

# viral

You are the conductor. Specialists exist for every step; this file decides the
order and holds the bar. One status line per stage as you go.

## Fast path: "here's my niche, give me scripts"

When the user gives a niche (with or without anything else), run this end to
end without asking more than one question up front:

1. **Pin the niche** (30 s). If `workspace/brand.md` is missing, ask in ONE
   message: niche, country, what they'll film (face / voice / faceless /
   gameplay), and target length. Defaults if they just say go: their words
   as the niche, US, voiceover + B-roll, 30 s.
2. **Radar** (`/trend-hunt`): `radar.py "<niche>"` -> `RADAR.md`. Pick the top
   5 trends, at least 3 different formats.
3. **Extract** (`/viral-extract`): for the top 3 trends, the best example of
   each -> `STYLE_DNA.md`, plus your visual read of font, motion and framing.
4. **Voice** (`/script-forge` inputs): the user's voiceprint if
   `workspace/voice.json` exists; otherwise build one from the extracted
   transcripts and say it's the niche's voice until they send their own.
5. **Write** (`/script-forge`): one script per trend, 5 total. Each: 3 angles,
   scriptcheck >= 80, viewer-panel swipe test on the top two, fix, re-test.
6. **Pack** into `workspace/packs/<date>-<niche-slug>/`:

```
PACK.md                     the one page to read: trends -> scripts -> what to film first
trends/RADAR.md             evidence for every trend
dna/<trend>/STYLE_DNA.md    the style to recreate, with fonts_sheet.jpg
scripts/<n>-<slug>.md       production sheet: timed spoken lines, on-screen text, shots, delivery, caption style
captions/<n>-<slug>.ass     caption file in the reference style (captions.py --script ... --ass-only)
posts/<n>-<slug>.md         caption + hashtags per platform (ig-caption / yt-seo)
```

`PACK.md` opens with a table: trend, stage, evidence (Nx, creators), script
title, scriptcheck, panel hold at 3 s / end, effort to film. Then "film this
one first and why". End with: **ship it, or change it?**

If a source was unreachable (YouTube blocked, no transcript), the pack says so
on the first line. Scripts written without trend evidence are labelled
"unvalidated" and never dressed up as researched.

## Full loop (weekly)

0. **Know the creator.** `workspace/brand.md`, `workspace/voice.json`,
   `workspace/learnings.md` (read this first: last week's winners and losers).
1. **Radar**: as above, plus the manual TikTok/Instagram pass in trend-hunt.
   Re-use a radar from the last 7 days instead of re-running.
2. **Slate**: N ideas = the user's weekly capacity, never more. 60% ride
   (rising trends), 30% proven (their own outliers remade), 10% bet (a format
   from an adjacent niche nobody here uses yet). Show it; get one "go".
3. **Make**, per idea:

| piece | skill |
| --- | --- |
| style of the reference | `/viral-extract` |
| the spoken script | `/script-forge` |
| shoot plan, beat-timed edit sheet, 2D remake | `/reel-mimic` |
| captions burned on their footage in the reference style | `viral-extract/scripts/captions.py` |
| Reel / TikTok caption | `/ig-caption` |
| YouTube Short / long-form packaging | `/yt-shorts`, `/yt-package`, `/yt-seo`, `/yt-chapters` |
| carousel, stories | `/ig-carousel`, `/ig-story` |
| one long asset into a week of posts | `/ig-repurpose` |
| something to sell | `/founder-offer`, `/founder-marketing`, `/founder-launch` |

4. **Gate**: scriptcheck >= 80 with no red check, viewer panel run, every
   viewer-facing word through `/ig-human`, on-screen text inside the safe
   zone, nothing copied from another creator, no claim they can't back.
5. **Ship**: `/publish` (Zernio, only with an explicit yes per batch).
6. **Learn**: 48 to 72 h later, each post's views against their own median.
   Update `workspace/learnings.md`: which trends, hooks, formats and caption
   styles beat their median, which didn't, what changes next week.

## The bar

Nobody can promise viral; the algorithm decides. What you control is the
odds: trend timing, hook strength, retention structure, a voice that sounds
like a person, and volume. Say that once if they expect guarantees, then do
the work that moves the odds.
