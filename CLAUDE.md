# Viral Hyper Agent

This repository is a social media team for one creator, packaged for Claude
Code. Opening Claude Code here (on a computer or in a cloud session) loads all
of it.

## Default behaviour

- Any request about content, trends, growth, posting or brand deals: act as
  the `viral-hyper-agent` and load `/viral`.
- First run in a fresh clone: if `workspace/brand.md` is missing, copy
  `workspace/brand.example.md` to it and fill it in with the user before
  doing anything else.
- Short status line per stage; findings and drafts go into `workspace/`, not
  only into chat.

## Map

- `.claude/agents/`: `viral-hyper-agent` (conductor), `trend-scout`,
  `reel-mimic-director`, `growth-strategist`, `publisher`
- `.claude/skills/`: `viral`, `trend-hunt`, `reel-mimic`, `publish`,
  `brand-deals`, plus `yt-*` (11), `ig-*` (13), `founder-*` (11)
- `templates/`: voice files for Instagram and YouTube
- `tools/` (fetched by `setup.sh`, not committed): full ReelMimic studio,
  Harvey, gstack, REA
- `workspace/`: the creator's own state; `brand.md`, `trends/`, `content/`,
  `mimic/`, `learnings.md`

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
