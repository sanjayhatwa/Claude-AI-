---
name: auto-edit
description: >-
  Edit the user's raw video like a professional short-form editor, locally:
  cuts dead air, filler words and retakes, reframes 16:9 to 9:16 by tracking
  the face, adds punch-in zooms on emphasis, a hook title, motion-graphic
  callouts for numbers/lists/questions, captions in a chosen viral style,
  b-roll cutaways, sound design and ducked music, then renders. Use for "edit
  my video", "cut this", "make this a reel", "add captions and zooms", "turn
  my podcast/talking head into shorts", "edit like <creator>".
---

# auto-edit

```bash
python -m vstudio autoedit <raw.mp4> --out-dir workspace/edits/<slug> \
  [--style workspace/dna/<ref>/style_dna.json] [--aspect 9:16] [--energy high|medium|calm] \
  [--broll <folder>] [--music <file>] [--hook "TEXT"] [--max-duration 60] --render [--preview]
```

Outputs `edit.json` (the full timeline, editable), `EDIT_REPORT.md` (every
cut with reason, seconds saved, graphics added, focus method) and
`edited.mp4`.

## How to run it well

1. **Style.** If the user names a creator or pastes a video they want to look
   like, run `/viral-extract` on it first and pass its `style_dna.json` as
   `--style`: captions then match its font look, size, position, colours and
   words per card.
2. **Transcript.** Word timings come from captions they have (`--captions
   file.srt`), a words JSON, or local faster-whisper. Without any transcript
   it falls back to silence-based cuts only (no captions or graphics) and
   says so.
3. **Preview first** (`--preview`, half resolution), look at stills:
   `python -m vstudio render workspace/edits/<slug>/edit.json --still 3 --out /tmp/a.png`.
4. Fix in `edit.json` (or the web studio's Timeline page) and re-render:
   `python -m vstudio render edit.json --out edited.mp4`.
5. Write the caption with `/ig-caption` or `/yt-seo` and hand to `/publish`.

## Long video into shorts

Run `/yt-shorts` on the transcript to pick self-contained moments, cut each
moment's range with ffmpeg (`-ss START -to END -c copy`), then auto-edit each
clip with `--max-duration 60`.

## Honest limits

- Reframing follows the largest face; two-person shots and fast motion may
  need a manual `focus` in `edit.json`.
- Retake detection compares sentence openings; it will miss retakes that
  start differently.
- Music: the user's own or royalty-free tracks only.
