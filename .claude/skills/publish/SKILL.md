---
name: publish
description: >-
  Turn finished content into a posting calendar across Instagram, TikTok,
  YouTube, X, LinkedIn, Threads and Facebook, schedule it through a connected
  scheduler (Zernio) only after the user approves, and pull post analytics
  back for the learning loop. Use for "schedule this", "post this", "make a
  content calendar", "when should I post", "how did my posts do".
---

# publish

The only skill in this pack that touches a live account, so it has the
strictest gate.

## 1. Calendar

Read `workspace/content/<week>/` and `workspace/brand.md`. Build `WEEK.md`:
date, time (user's time zone), platform, account, piece, caption file, media
file, status. Spread posts so no platform gets two in four hours. If the
scheduler is connected, get best times from it (Zernio
`analytics_get_best_time_to_post`); otherwise use the user's own past
insights; otherwise say the times are a starting guess.

## 2. Schedule (Zernio MCP, if connected)

1. `accounts_list` to get account IDs. If more than one account exists on a
   platform, ask which one. Never guess.
2. `validate_post` / `validate_post_length` / `validate_media` for every post.
3. Show the user the full batch: platform, account, time, first line of
   caption, media. **Schedule only after an explicit yes for that batch.**
   A yes last week is not a yes this week.
4. `posts_create` with `account_id` and `schedule_minutes` (minutes from now
   to the slot), or `is_draft: true` to park it in Zernio for the user to
   review there. `posts_cross_post` sends one piece to several platforms.
   Media goes through `media_generate_upload_link`; YouTube needs a `title`.
5. Report what was scheduled, with links, and anything that failed.

`posts_publish_now` is for when the user explicitly says "post it now".

## 3. No scheduler connected

Hand back a copy-paste pack per post (caption, hashtags, alt text, posting
time, media file name) and say in one line that connecting Zernio (or any
scheduler connector) in Claude settings lets this skill schedule directly.

## 4. Results (48 to 72 h later)

Zernio `analytics_get_analytics` / `analytics_get_post_timeline` /
`accounts_get_follower_stats`, or the user's pasted insights. Report each
post's views against the user's **own median**, not in absolute terms. Hand
the numbers to `/viral` step 6.

## Never

- Post, schedule, delete, reply or DM without an explicit yes.
- Store or ask for social media passwords.
- Use engagement pods, bought followers, or follow/unfollow automation.
