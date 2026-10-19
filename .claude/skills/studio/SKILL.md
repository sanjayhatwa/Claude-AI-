---
name: studio
description: >-
  Run and drive Viral Studio, the local web app and video engine in this
  repo (python -m vstudio): make reels from a prompt and reference videos,
  auto-edit raw footage, analyze channels, extract styles, write scripts,
  edit timelines with an "Ask Claude" box, preview templates. Runs on the
  user's own machine with their Claude subscription; no cloud rendering.
  Use for "open the studio", "run it locally", "start the app", "render
  this", "change the title in my reel", "which templates are there", or
  when something about vstudio needs checking or fixing.
---

# studio

## Start it

| | |
| --- | --- |
| Windows | double-click `studio.bat` |
| macOS / Linux | `./studio.sh` |
| any | `python -m vstudio serve --open` |

Opens http://127.0.0.1:4320. Projects live in `workspace/studio/projects/`.
It listens on this computer only. `python -m vstudio doctor` checks ffmpeg,
fonts, OCR, whisper, the face model, Claude Code login and ReelMimic, with a
fix for anything missing.

## Commands (all local)

| command | what |
| --- | --- |
| `python -m vstudio reel ...` | prompt + references + competitors + assets -> reel (see `/reel-factory`) |
| `python -m vstudio autoedit ...` | raw video -> edited video (see `/auto-edit`) |
| `python -m vstudio channel ...` | channel analysis (see `/channel-dna`) |
| `python -m vstudio render spec.json --out f.mp4 [--preview]` | render a timeline |
| `python -m vstudio render spec.json --still 2.0 --out f.png` | one frame, to look at |
| `python -m vstudio plan compile plan.json --out spec.json` | plan -> timeline |
| `python -m vstudio templates [--json]` / `templates doc` / `templates preview <name> --out f.mp4 [--still 1.5]` | motion-graphics catalog (`doc`: when to use each, every param) |
| `python -m vstudio fonts install "Montserrat" "Anton"` | free caption fonts (OFL) |
| `python -m vstudio sfx list` | built-in sound effects (generated, royalty-free) |
| `python -m vstudio mimic2d start` | full ReelMimic studio for 2D animated remakes |

## Editing a render by hand

`spec.json` / `edit.json` are timelines (format: section 1 of
`vstudio/ARCHITECTURE.md`): layers (video, image, text, shape, progress,
counter) with timing, position, enter/exit animations and keyframes;
`camera` keyframes; `effects` (flash, shake, glitch, zoom-blur, vignette,
grade, grain, letterbox); `captions` (words + style); `audio` (voice, music
with ducking, sfx). Change what was asked, keep everything else, validate by
rendering a still, then render.

## Design rules for anything you add

- 9:16 safe zone: nothing important in the top 11% or bottom 20%, ~6% side
  margins. Text 2-6 words per card, never over a face.
- One motion idea per beat; ease out (out-cubic / out-back) on entrances,
  ease in on exits, exits finish before the next beat.
- Accent colour on one word per line at most.
- Sound on every transition you can see; nothing louder than the voice.
