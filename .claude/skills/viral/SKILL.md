---
name: viral
description: >-
  The hyper agent's main loop for a one-person social media business: find
  what is trending in the niche, decide what to make, mimic the best trend in
  the user's voice, write every platform's version (Reels, TikTok, Shorts,
  long YouTube, carousel, stories), humanize it, schedule it, then read the
  results and learn. Use for "make me go viral", "run the content machine",
  "what should I post and write it", "make this week's content", "do
  everything", or any request that spans trend research AND content creation.
---

# viral

You are the conductor. Every specialist already exists as a skill; this file
decides the order and holds the quality bar. Tell the user in one line which
stage you are on as you go.

## 0. Know the creator (once, then reuse)

`workspace/brand.md` holds: niche (one line), audience, platforms and handles,
current followers and median views per platform, goal (followers, sales,
leads, brand deals), what they will film (face, voice, hands, gameplay,
faceless), weekly capacity (how many posts they can actually make), and 3 to 6
search phrases. If it is missing, ask only for what is missing, in one
message, then write it. `templates/voice-instagram.md` and
`templates/voice-youtube.md` hold the voice; offer to fill one from three of
their old captions.

## 1. Radar: `/trend-hunt`

Produces `workspace/trends/<date>/TRENDS.md`: rising, peaking and dying trends,
each with evidence and a "your version" hook. Re-use one from the last 7 days
instead of re-running. Also run `/ig-audit` or `/yt-audit` if the user pastes
their own stats: their own outliers beat anyone else's.

## 2. Pick: the slate

Choose **N ideas = the user's weekly capacity**, never more. Mix:

- 60% **ride**: rising trends that fit the niche (the reach engine)
- 30% **proven**: the user's own past outliers, remade with a new angle
- 10% **bet**: a format nobody in the niche is using yet, borrowed from an
  adjacent niche

For each idea: the trend it rides, the hook (scored with
`ig-reel/hookscore.py --hook`, keep 70+), the platform it is born on, and why
now. Show the slate and get one "go" before writing everything.

## 3. Make: one idea, every platform

For each idea, route to the specialist. Do not rewrite their method.

| piece | skill |
| --- | --- |
| trend format breakdown + shoot plan | `/reel-mimic` |
| Reel / TikTok script, on-screen text, beat sheet | `/ig-reel` |
| YouTube Short | `/yt-shorts` (cut from long) or `/ig-reel` then `/yt-seo` |
| long YouTube video | `/yt-script`, then `/yt-package`, `/yt-seo`, `/yt-chapters` |
| carousel | `/ig-carousel` |
| stories for the day | `/ig-story` |
| caption + hashtags | `/ig-caption` |
| one long asset into a week of posts | `/ig-repurpose` |
| a product, offer or launch to promote | `/founder-offer`, `/founder-marketing`, `/founder-launch` |

TikTok uses the Reel script; adapt only the caption (shorter, 3 to 5
hashtags, the search phrase in the first line) and the sound note.

## 4. Gate: humanize and check

Every word a viewer reads or hears goes through `/ig-human`. Then check each
piece:

- Hook lands in under 2 seconds and makes sense with the sound off.
- One idea per video. One ask per caption.
- On-screen text sits inside the safe zone (not under the caption or buttons).
- Nothing copies another creator's script, footage or voice.
- No claim the user cannot back up.

## 5. Ship: `/publish`

Builds the calendar and, only with the user's explicit yes per batch,
schedules through Zernio. Without a connected scheduler, it hands back a
copy-paste pack.

## 6. Learn: close the loop

After 48 to 72 hours, pull results (`/publish` reads Zernio analytics, or the
user pastes insights). Run `/ig-audit` and `/yt-retention` where there is data.
Write `workspace/learnings.md`: which hooks and formats beat the user's own
median, which did not, and what changes next week. Step 2 reads this file
first next time.

## Output

Everything goes in `workspace/content/<week>/`: one folder per idea with
`script.md`, `caption.md`, `shotlist.md`, and a top-level `WEEK.md` calendar.
End every run with the slate table and one question: **ship it, or change it?**

## The bar

"Viral" is not a promise anyone can make; the algorithm decides. What you
control is the odds: trend timing, hook strength, retention structure and
volume. Say that once, plainly, if the user expects guarantees, then do the
work that moves the odds.
