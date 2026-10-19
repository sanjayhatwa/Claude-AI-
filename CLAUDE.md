# Viral Hyper Agent

This repository is a social media team for one creator, packaged for Claude
Code. Opening Claude Code here (on a computer or in a cloud session) loads all
of it.

## Default behaviour

- Any request about content, trends, growth, posting or brand deals: act as
  the `viral-hyper-agent` and load `/viral`.
- The user gives only a niche: run `/viral`'s fast path (radar -> extract ->
  script-forge -> pack) end to end.
- Every script goes through `/script-forge` (scriptcheck >= 80, viewer panel).
- Videos are made and edited locally with `vstudio` (`/reel-factory`,
  `/auto-edit`, `/channel-dna`, `/studio`). Look at rendered stills before
  calling anything finished.
- First run in a fresh clone: if `workspace/brand.md` is missing, copy
  `workspace/brand.example.md` to it and fill it in with the user before
  doing anything else.
- Short status line per stage; findings and drafts go into `workspace/`, not
  only into chat.

## Running Python

Skills write commands as `python3 <script>` and `python -m vstudio <cmd>`.
Both mean the same thing: the Python that setup installed the packages for.
Pick it once per session and use it for every command:

- `.venv/` exists (setup made one): `.venv/bin/python` on macOS/Linux,
  `.venv/Scripts/python.exe` on Windows.
- Otherwise macOS/Linux: `python3` (python.org and Homebrew install no
  `python`). Windows: `python`, or `py -3`; there `python3` is usually the
  Microsoft Store stub that prints "Python was not found" (exit 9009).
- `<that python> -m vstudio doctor` shows whether it has the packages.

## Map

- `.claude/agents/`: `viral-hyper-agent` (conductor), `trend-scout`,
  `reel-mimic-director`, `viewer-panel`, `channel-analyst`, `motion-designer`,
  `video-editor`, `growth-strategist`, `publisher`
- `.claude/skills/`: `viral`, `trend-hunt` (radar.py), `viral-extract`
  (extract.py, captions.py), `script-forge` (voiceprint.py, scriptcheck.py),
  `reel-mimic`, `reel-factory`, `auto-edit` (cut_moment.py), `channel-dna`, `studio`,
  `publish`, `brand-deals`, plus `yt-*` (11), `ig-*` (13), `founder-*` (11)
- `vstudio/`: the local video engine and web app (`python -m vstudio`);
  contract in `vstudio/ARCHITECTURE.md`
- `templates/`: voice files for Instagram and YouTube
- `tools/` (fetched by `setup.sh`, not committed): full ReelMimic studio,
  Harvey, gstack, REA
- `workspace/`: the creator's own state; `brand.md`, `trends/`, `content/`,
  `mimic/`, `dna/`, `packs/`, `voice.json`, `learnings.md`

## Hard rules

- Nothing is posted, scheduled, sent, liked, followed or messaged without the
  user's explicit yes for that batch.
- Never ask for or store social media passwords. Never log in for the user.
- Read, do not scrape: no crawlers, scraping services or background loops
  against Instagram, TikTok or YouTube.
- Copy formulas, never content: no reuse of other creators' footage, scripts,
  voice or likeness.
- Never invent numbers. Say where data could not be reached.

## Checks

`python3 -m unittest discover -s tests` runs the tool tests (no network).
`python -m vstudio doctor` checks the local video toolchain.
