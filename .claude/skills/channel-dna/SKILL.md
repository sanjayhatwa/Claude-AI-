---
name: channel-dna
description: >-
  Full analysis of a YouTube channel (and public Instagram/TikTok profiles):
  every long-form video, Short and live; performance and outliers;
  upload cadence; topic tags for every video; title patterns; thumbnail
  patterns (text, faces, colours, layout) with a contact sheet; and a deep
  dive into the top videos' dialogue (pace, word spacing, hooks, re-hooks),
  energy, caption style and edit style. Ends with a playbook to compete with
  or replicate the channel. Use for "analyze this channel", "what makes X
  work", "audit my channel", "compare these creators", "reverse-engineer
  this creator", a pasted channel or profile link.
---

# channel-dna

```bash
python -m vstudio channel <url> [<url> ...] --out-dir workspace/channels/<slug> \
  [--max-videos 30] [--deep 4] [--no-download]
```

- `--max-videos`: how many of the latest videos per format get full metadata.
- `--deep N`: download the top N Shorts and the first 90 s of the top N
  long-form videos for the dialogue, energy, caption and edit analysis.
  `--no-download` = metadata and thumbnails only (fast).
- Several URLs = several channels plus a comparison table.

Outputs `CHANNEL_DNA.md` (read this), `CHANNEL_DNA.json`, `thumbs_sheet.jpg`
and `dna/<video>/` (each deep-dived video's Style DNA).

## After it runs

1. Read `CHANNEL_DNA.md` and open `thumbs_sheet.jpg` and a couple of
   `dna/*/fonts_sheet.jpg`.
2. Add a short **Visual read** of the thumbnails (face expression, arrows,
   before/after, colour contrast) and captions (font, motion) that pixels
   can't settle.
3. Turn the playbook into action: `/trend-hunt` for their topics,
   `/script-forge` with their measured pace, `/reel-factory` or `/auto-edit`
   with a deep-dived video as the style reference, `/yt-package` for titles
   and thumbnails in their winning patterns.

## Limits, said plainly

- Instagram and most TikTok profiles need a logged-in browser: the report
  marks them "needs your browser". Drive the user's own browser with them
  present (Claude in Chrome), at human speed, or ask them to paste. Never ask
  for a password.
- Public numbers only. Studio-only data (CTR, retention, revenue) needs the
  creator's own export (`/yt-retention`).
- Polite pace by design: a few hundred lookups for a big channel, spaced out.
