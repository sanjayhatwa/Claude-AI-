---
name: trend-hunt
description: >-
  Cross-platform trend radar for one niche: what is breaking out right now on
  YouTube Shorts, Instagram Reels and TikTok, which hooks, formats and sounds
  are over-performing, and which are already dying. Ranks by outlier multiple
  (views over the creator's own median), never raw views. Use for "what's
  trending in my niche", "find trends", "what should I post this week",
  "what's going viral", "dig into what's working and what's not", or as the
  first step of /viral.
---

# trend-hunt

The radar. It looks across platforms, ranks what it finds against each
creator's own baseline, and hands back **trends you can act on this week**,
with evidence and a confidence level. It does not write the content; `/viral`
and the platform skills do that.

## The rule that makes this worth anything

**Raw views are not evidence.** 400k views on a 2M-follower account is a slow
day. 400k on a 4k-follower account is a formula worth copying. Everything here
is ranked on the **outlier multiple** = views / that creator's recent median.

- **3x or more**: signal. **1.5x to 3x**: weak. **Under 1.5x**: noise.
- A trend is a **pattern that repeats across 3+ unrelated creators** within
  the last 2 to 3 weeks. One viral video is an anecdote, not a trend.

## Step 0: pin the niche

Read `workspace/brand.md` if it exists (niche, audience, voice, platforms,
what the user will and will not do on camera). If not, ask for three things
and write it: **niche in one line, 3 to 6 search phrases, and 6 to 12
accounts they watch.** Do not ask more than that before starting.

## Step 1: the public radar (YouTube Shorts)

Shorts is where short-form view counts are public and no login is needed, and
the same hooks and formats run on Reels and TikTok the same week.

```bash
python3 .claude/skills/trend-hunt/scripts/trends.py all \
  "phrase one" "phrase two" "phrase three" --dir workspace/trends/<date>
```

It searches newest-first, keeps videos under 3 minutes, fetches each channel's
median, ranks by multiple, and pulls the spoken first three seconds of the top
ten from auto-captions. Outputs `ranked.json`, `report.txt`, and
`swipe-input.tsv` (ready for `ig-viral/swipe.py`).

If YouTube is unreachable (some cloud sandboxes block it), say so in one line
and continue with Steps 2 and 3. Never invent numbers to fill the gap.

## Step 2: Instagram and TikTok, by hand, at human speed

There is no public API for this and you do not need one. Use whatever browser
tool the session actually has (Claude in Chrome on the user's own logged-in
browser, the in-app browser, or computer use), or ask the user to paste.

- **Never log in for the user, never ask for a password**, never use a
  scraping service, never loop this in the background. Ten accounts, a dozen
  posts each. Automated collection breaks Instagram's and TikTok's terms and
  gets the user's account blocked.
- Capture per post: account, followers, median of the last 12, views, the
  first line (spoken or on-screen, verbatim), length, sound name, CTA.
- Then run `/ig-viral`'s ranker on it:
  `python3 .claude/skills/ig-viral/swipe.py captured.tsv`

## Step 3: the context layer (web)

Use web search for the last 14 days only: "<niche> trend", "<platform>
trending audio this week", creator newsletters, platform announcements
(format changes, new features the algorithm is pushing). This explains
*why* something is moving. It is never ranked evidence by itself.

## Step 4: call it

Group the outliers into trends. For each, write:

```
TREND: <name in plain words>
  what:        the hook shape / format / sound / edit pattern
  evidence:    N creators, median multiple Xx, best example (link, multiple)
  stage:       rising | peaking | saturated   (saturated = big accounts now copying it)
  fit:         how it maps to this user's niche and what they can film
  your version: one hook line they could say tomorrow
```

Then a **DYING** list: formats that were everywhere a month ago and now sit
under 1.5x. Telling the user what to stop is half the value.

Close with sample size and confidence in plain words. 40 posts across 8
creators supports a claim. 12 posts does not, and you say so.

Write the result to `workspace/trends/<date>/TRENDS.md`. `/viral`,
`/reel-mimic`, `/ig-reel`, `/yt-script` and `/ig-plan` all read it.

## Never

- Copy someone's video, script, voice or footage. Copy the **formula**.
- Present a single video as a trend.
- Post, like, follow, comment or DM. This skill only reads.
