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
  [--captions <file.srt> | --words <words.json>] [--style workspace/dna/<ref>/style_dna.json] \
  [--aspect 9:16|16:9|1:1|4:5|source] [--energy auto|high|medium|calm] \
  [--broll <folder>] [--music <file>] [--hook "TEXT"] [--max-duration 60] \
  [--no-zoom] [--no-graphics] [--no-sfx] [--no-captions] [--fps 30] [--size 1080x1920] \
  [--whisper-model small] [--lang en] --render [--preview]
```

- `--energy auto` (the default) picks the editing intensity from how fast
  they talk; `high`, `medium` or `calm` force it.
- `--no-zoom`, `--no-graphics`, `--no-sfx`, `--no-captions` switch those
  parts off (for example `--no-captions` when the platform adds its own).
- `--fps` sets the output frame rate (default 30, or the source's 24/25);
  `--size WxH` overrides the aspect's 1080p size.
- `--whisper-model` (default `small`) and `--lang` (default: detected) only
  matter when it transcribes with faster-whisper.

Outputs `edit.json` (the full timeline, editable), `EDIT_REPORT.md` (every
cut with reason, seconds saved, graphics added, focus method),
`transcript.json` after a whisper run, and with `--render` `edited.mp4`.

## How to run it well

1. **Style.** If the user names a creator or pastes a video they want to look
   like, run `/viral-extract` on it first and pass its `style_dna.json` as
   `--style`: captions then match its font look, size, position, colours and
   words per card.
2. **Transcript.** Word timings come from, best first: `--words` (a words
   JSON or whisper JSON), `--captions` (SRT, VTT or json3), a captions file
   saved next to the video with the same name (`talk.srt`, `.vtt`, `.json3`,
   `.words.json`, `.en.srt`...: picked up by itself, the run's `transcript:`
   line says which), or local faster-whisper (the first run downloads a
   model of about 500 MB and takes minutes). After a whisper run it saves
   `transcript.json` in the output folder: pass it back with `--words` on
   every re-run instead of transcribing again. Without any transcript it
   falls back to silence-based cuts only (no captions or graphics) and says
   so.
3. **Preview first** (`--preview`, half resolution), look at stills:
   `python -m vstudio render workspace/edits/<slug>/edit.json --still 3 --out /tmp/a.png`.
4. Fix in `edit.json` (or the web studio's Timeline page) and re-render:
   `python -m vstudio render edit.json --out edited.mp4`.
5. Write the caption with `/ig-caption` or `/yt-seo` and hand to `/publish`.

## Long video into shorts

Run `/yt-shorts` on the transcript to pick self-contained moments, then cut
each one with its words and auto-edit the clip:

```bash
python3 .claude/skills/auto-edit/scripts/cut_moment.py long.mp4 --start 12:34.2 --end 13:21.9 \
  --transcript <the long video's transcript.json or .srt> --out workspace/edits/<slug>/moments/1.mp4
python -m vstudio autoedit workspace/edits/<slug>/moments/1.mp4 --out-dir workspace/edits/<slug>/1 \
  --max-duration 60 --render
```

`cut_moment.py` re-encodes from an input-side seek, so the clip starts on the
asked frame with picture and sound in sync (a `-c copy` cut snaps to
keyframes and can start the picture seconds late), and writes the moment's
words, shifted to the clip's clock, to `1.words.json` beside it, which
autoedit picks up by itself: captions and graphics without transcribing
again. Without `--transcript` the clip needs faster-whisper, or it gets
silence-only cuts with no captions or graphics.

## Honest limits

- Reframing follows the largest face; two-person shots and fast motion may
  need a manual `focus` in `edit.json`.
- Retake detection compares sentence openings; it will miss retakes that
  start differently.
- Music: the user's own or royalty-free tracks only.
