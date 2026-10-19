---
name: viral-extract
description: >-
  Extract the complete Style DNA of a viral video so it can be recreated
  exactly: the caption font look (weight, width, case, free look-alikes), font
  size, position, line spacing, fill/outline/box/highlight colours, words per
  card and timing; the dialogue (hook, pace in wpm, sentence rhythm, pauses,
  re-hooks, signature phrases, CTA, loop ending); and the edit (cuts, shot
  length, cuts on beat, camera moves, BPM, look). Then re-apply that caption
  style to the user's own footage. Use for "what font is this", "copy this
  caption style", "break this video down", "how was this edited", "extract
  the style", "make my captions look like this", or any pasted reel/short/
  TikTok link the user wants to recreate.
---

# viral-extract

Pixels and audio in, a recreate spec out. Every number is measured, not
guessed, and the parts that can't be measured (exact font name, motion
effects) are called out and settled by looking.

## Step 1: extract

```bash
python3 .claude/skills/viral-extract/scripts/extract.py "<file-or-url>" --out workspace/dna/<slug>
```

- URLs: YouTube, Instagram, TikTok public posts all work through yt-dlp (one
  reference at a time, for study).
- Transcript: YouTube captions are pulled automatically; otherwise local
  `faster-whisper` is used if installed (`pip install faster-whisper`, first
  run downloads a model); or pass `--captions file.srt`.
- OCR runs locally (`rapidocr-onnxruntime`). `--fps 6` for very fast
  word-by-word captions; default 4.
- About 30 to 90 seconds for a 60 s reel on a laptop.

Outputs in the folder: `STYLE_DNA.md` (read this), `style_dna.json`,
`text_cards.json` (every card with timing and style), `transcript.json`,
`fonts_sheet.jpg`, `text_crops/`, and `analysis/` (contact sheets, edit report).

## Step 2: look, then settle what pixels can't

Open with Read: `fonts_sheet.jpg`, `analysis/sheet_scenes.jpg`,
`analysis/sheet_1fps.jpg`. Then add a **Visual read** section to
`STYLE_DNA.md`:

1. **Font.** Compare the crops to the candidates the extractor proposed
   (`references/fonts.json` has what each looks like). Check: geometric round
   O (Montserrat/Poppins) vs grotesk (Inter/Archivo) vs condensed caps
   (Anton/Bebas) vs rounded (Fredoka/Nunito) vs serif vs handwritten. Name the
   closest free font and say how sure you are. If it is clearly a platform's
   built-in text (TikTok/Instagram/CapCut presets), say which preset style.
2. **Caption motion.** From consecutive 1 fps frames: pop/bounce in, typewriter,
   word-by-word highlight, slide, static. Emoji or icons next to words?
3. **Framing.** Talking head / voiceover over B-roll / screen recording /
   gameplay / skit. Face size, eye-line, background, lighting.
4. **First frame.** What is on screen at 0.0 s and why a thumb stops.
5. **Edit tricks.** Zoom punch-ins on emphasis words, jump cuts at breaths,
   SFX on cuts (whoosh/pop), B-roll every N seconds, colour grade.

## Step 3: the recreate spec

Finish `STYLE_DNA.md` with a **Recreate** block the user (or an editor) can
follow line by line:

```
CAPTIONS   font Montserrat Black, 96 px on 1080x1920, UPPER, white #FFFFFF,
           black outline 8 px, active word yellow #FFE500, 2-3 words per card,
           centred 70% down, max width 84%, pop-in 90 ms
TITLE      Anton 64 px, black on yellow box, pinned 12% down for the whole video
PACE       175 wpm, first word at 0.2 s, sentences 4-9 words, pause every 3 s
EDIT       cut every 1.4 s, 70% of cuts on the beat, punch-in on every number
HOOK       on-screen text and first spoken line both land inside 1.5 s
```

## Step 4: put it on the user's video

```bash
python3 .claude/skills/viral-extract/scripts/captions.py --style workspace/dna/<slug>/style_dna.json \
  --video their_take.mp4 --out workspace/out/their_take_captioned.mp4 [--font "Montserrat Black"] [--pop]
```

- Timing comes from `--words`, `--srt`, or faster-whisper on their video; with
  only `--script` it previews at the reference pace.
- Nothing filmed yet: `--script script.txt --ass-only --out captions/<n>-<slug>.ass`
  with no `--video` writes just the caption file, laid out for 1080x1920
  (`--size WxH` for another frame).
- The `.ass` file is written next to the output: it imports into CapCut,
  Premiere and DaVinci if they'd rather finish in an editor (that computer
  needs the font installed).
- Fonts: `python -m vstudio fonts install "<Family>"` puts the free font in
  `workspace/fonts/`, where captions.py finds it by itself (as it does a
  folder given with `--fonts-dir`, subfolders included). A variable font
  (`Family[wght].ttf`) is cut to the asked weight, which needs `fonttools`
  (in requirements.txt). The `wrote ... font <name> from <file>` line names
  the font actually burned in; a missing font prints `font '<name>' not
  found; using <other>` and the look drifts: say so.

## Accuracy, stated plainly

Calibrated by rendering captions with known settings and reading them back:
position within about 1% of frame, font size within about 6%, outline
thickness within about 1 px, colours exact on clean text. Busy backgrounds
behind thin text make colour and outline readings noisier. Exact font
identity always needs the visual check in step 2.

## Never

- Reuse the reference's footage, audio, script or a creator's face or voice.
  This extracts **style**, which is not ownable; the content is.
- Download more than the references being studied.
