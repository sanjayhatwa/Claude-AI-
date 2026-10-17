---
name: reel-mimic
description: >-
  Take a trending or reference reel apart (cuts, shot lengths, BPM and beat
  hits, camera moves, colour, on-screen text, hook timing) and rebuild the
  same formula with the user's own content: a shot-by-shot shoot plan they
  can film on a phone, an edit sheet timed to the beat, or a fully animated
  2D version through the ReelMimic studio. Use for "copy this trend", "make
  one like this", "mimic this reel", "do this format for my niche", a pasted
  reel/short/TikTok link, or when /trend-hunt surfaces a format worth riding.
---

# reel-mimic

Ride a trend in hours, not days. Learn **how** a reel was made, then make the
user's own version. The output is never the original's footage, script,
characters or voice. It is the structure.

## Step 1: break the reference down

```bash
python3 .claude/skills/reel-mimic/scripts/analyze.py "<file-or-url>" --out workspace/mimic/<slug>/analysis
```

(Analyzer from ReelMimic, MIT. Needs `ffmpeg` plus `pip install -r requirements.txt`.)

It writes `sheet_1fps.jpg`, `sheet_scenes.jpg` and `report.json`: every cut,
average shot length, BPM and beat grid, per-shot camera move and speed,
brightness/contrast/saturation, dominant colours, and `peak_candidates` (the
moments where the music builds and hits).

**Open both contact sheets with Read and look at them.** Numbers without the
frames miss the point of most reels. Then write `analysis/BREAKDOWN.md`:

- **Hook (0 to 3 s)**: what is on screen in frame one, the first spoken line,
  the first text card, and why it stops a thumb.
- **Shot list**: number, start to end, what is shown, camera move, cut type,
  and whether it lands on a beat.
- **Structure**: hook, setup, turn, payoff, CTA, with timestamps.
- **Pacing**: average shot length, where it speeds up, where it holds.
- **Look**: framing, colour, text style and position, captions.
- **Sound**: trending audio name if any, BPM, where the drop is.
- **Why it works**: one paragraph, and the formula id from `ig-reel/hooks.json`
  if one fits.

## Step 2: map it to the user

Read `workspace/brand.md` and `templates/voice-*.md` if filled. Then decide
the route with the user (one question, not five):

| route | when | output |
| --- | --- | --- |
| **Film it** (default) | live action, talking head, B-roll, POV, game footage | shoot plan + edit sheet |
| **Animate it** | the reference is 2D: motion graphics, painted, pixel, whiteboard, cut-out, anime | ReelMimic studio run |
| **Screen it** | gameplay, app or desktop capture | capture list + edit sheet |

## Step 3a: film it (shoot plan + edit sheet)

Write `workspace/mimic/<slug>/PLAN.md`:

1. **Three hook options** in the reference's hook shape but the user's story.
   Score each with `python3 .claude/skills/ig-reel/hookscore.py --hook "<hook>"` and
   keep the best.
2. **Shot list** matching the reference's shot count and lengths within about
   20%: shot, seconds, what to film, framing (close/medium/wide), camera move,
   on-screen text, and which beat it cuts on.
3. **Edit sheet**: cut timestamps aligned to the analyzer's beat grid,
   text-card timings, caption style, the sound to use (the trending audio name
   if the user picks it in-app, else a royalty-free match at the same BPM).
4. **Caption + hashtags**: hand to `/ig-caption` (Reels/TikTok) or
   `/yt-seo` (Shorts).
5. Run every word the viewer will read or hear through `/ig-human` before
   handing it over.

## Step 3b: animate it (ReelMimic studio)

The full studio is fetched by `./setup.sh` into `tools/reelmimic` (Node 22.18+,
Python 3.10+, FFmpeg, Chrome). Then either:

- `cd tools/reelmimic && ./start.sh` and open http://localhost:4318, drop the
  reference in and paste the brief, or
- open Claude Code inside `tools/reelmimic` and run `/video-clone` with the
  reference and a one-line brief.

Tell the user up front: 2D only, a 30 to 60 s video takes 1 to 3.5 hours after
the plan is approved, and it uses their own Claude plan. No real people.

## Step 4: compare before shipping

When the user has a cut, analyze it the same way and put the two
`report.json` files side by side: hook timing, shot count, average shot
length, cuts-on-beat percentage. Flag anything more than 25% off the
reference and say what to change.

## Never

- Reuse the reference's footage, audio stems, characters, script or voice.
- Mimic a real person's likeness or impersonate a creator.
- Download more than the one reference being studied.
