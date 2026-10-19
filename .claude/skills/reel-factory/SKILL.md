---
name: reel-factory
description: >-
  Make a finished reel from a prompt plus reference videos (the style to
  copy), competitor videos (the bar to beat) and the user's own assets:
  analyzes every input, writes a brief, plans beats with motion-graphics
  templates, renders the MP4 locally with captions, transitions, camera
  moves, sound design and music, then QA-checks the result against the
  reference's measured style. Use for "make a reel about X like this
  video", "create a video in this style", "here are my clips, make a reel",
  "beat these competitor videos", "motion graphics video about X".
---

# reel-factory

The local engine is `vstudio` (in this repo). It renders on the user's own
machine; planning runs on their Claude subscription. You, the agent, are the
director: you write the plan; the engine does the pixels.

## Inputs to collect (one message, only what is missing)

- **Prompt**: what the reel is about, the one idea, who it's for.
- **Reference video(s)**: the style to mimic: a file, a link, or a folder
  already measured (`workspace/dna/<slug>` from `/viral-extract`,
  `<channel folder>/dna/<video>` from `/channel-dna`, or the
  `style_dna.json` inside one). A measured folder is reused as it is, with
  no 20-90 s re-measuring: pass it whenever one exists.
- **Competitor video(s)**: same topic, the bar to beat (optional; file, link
  or measured folder).
- **Assets**: their clips, screen recordings, gameplay, images, logo (optional).
- **Script / voiceover / music** (optional). No voiceover = text-driven reel
  with captions from the beats.
- Aspect (default 9:16) and length (default 30 s).

## The two ways to run it

**A. Agent mode (best quality, you write the plan).**

1. Analyze inputs (runs locally):

   ```bash
   python -m vstudio reel --prompt "<prompt>" --ref <ref> --competitor <comp> --asset <file> \
     --duration 30 --out-dir workspace/reels/<slug> --no-render --llm none
   ```

   This writes `BRIEF.md` (style targets measured from the reference: cut
   rate, caption font/size/position/colours, pace, palette, hook timing;
   competitor hooks and structure) plus a first heuristic `plan.json`.
2. Read `BRIEF.md`, the reference `STYLE_DNA.md` and its `fonts_sheet.jpg`.
   Run `python -m vstudio templates doc` for the catalog: when to use each
   template and every param (`python -m vstudio templates` lists only the names).
3. Write the spoken lines with `/script-forge` (scriptcheck >= 80) if the reel
   is voiced or captioned.
4. Rewrite `plan.json` as an elite short-form director (schema: section 2 of
   `vstudio/ARCHITECTURE.md`): hook inside 1.5 s, a re-hook every 6-10 s, one
   idea per beat, on-screen text of 2-6 words, beat lengths matching the
   reference's cut rate, the reference's caption style in
   `style.caption_style`, templates chosen for what each beat says, sfx on
   transitions, CTA last. Only use listed templates and the user's assets.
   Never reuse competitor wording.
5. Render a preview, look, fix, then render final. A bare `--plan` run on
   the project folder keeps what step 1 saved in `analysis.json` (prompt,
   measured inputs, assets, length and aspect), so nothing is analyzed
   again and the brief stays complete. Pass `--asset`, `--duration` or
   `--aspect` only to change them.

   ```bash
   python -m vstudio reel --plan workspace/reels/<slug>/plan.json --out-dir workspace/reels/<slug> --preview
   python -m vstudio render workspace/reels/<slug>/spec.json --still 1.0 --out /tmp/s1.png   # look at 4-6 stills
   python -m vstudio reel --plan workspace/reels/<slug>/plan.json --out-dir workspace/reels/<slug>
   ```

6. Read `QA.md` (output re-measured against the reference) and fix any
   metric marked warn. QA passes cut rate and mean shot within 35% of the
   reference, caption size within 20%, caption position within 6% of the
   height and pace within 15%. `--no-qa` skips this pass (drafts only).

**B. One command (Claude plans through the CLI, or offline heuristics):**

```bash
python -m vstudio reel --prompt "<prompt>" --ref <ref> --competitor <comp> --asset <file> \
  --duration 30 --out-dir workspace/reels/<slug> --llm auto
```

`--llm none` = offline planner (works with no Claude at all).

## Fine edits

`spec.json` is the full timeline (layers, camera, effects, captions, audio;
section 1 of the architecture doc). Small changes: edit it and run
`python -m vstudio render spec.json --out reel.mp4`. The web studio's
Timeline page does the same with a form and an "Ask Claude" box.

For a 2D animated remake (painted, pixel, paper cut-out, anime cel...), use
the full ReelMimic studio: `python -m vstudio mimic2d start`.

## Never

- Use a competitor's or reference's footage, audio, script or a person's
  face or voice. Style and structure only.
- Ship without looking at stills of the render.
