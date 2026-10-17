---
name: brand-deals
description: >-
  Turn audience into income: find brands and sponsors that fit the creator's
  niche, research each one, write a personalised pitch email or DM that
  references something specific about them, price the deal, and handle
  replies and objections. Built on Harvey's sales playbooks. Use for "get
  sponsors", "brand deals", "pitch this brand", "collab outreach", "how much
  should I charge for a sponsored reel", "they replied, what do I say".
---

# brand-deals

The money side of going viral. Adapted from Harvey (an autonomous sales agent,
MIT) for a one-person creator business. Harvey's own playbooks are in
`references/`; read the one a step names before doing that step.

## Step 1: the creator's media kit (once)

Read `workspace/brand.md`. Build `workspace/media-kit.md`: niche, audience
(age, country, interests), platform followers and **median views per post**
(not the best post), engagement rate, three best results with numbers, and
formats on offer (dedicated reel, integration, story set, YouTube mention,
UGC-only). If a number is unknown, ask; never invent one.

## Step 2: find fits (`references/prospecting_tactics.md`, `references/lead_qualification.md`)

A good brand fit already pays creators in this niche, or sells to exactly
this audience. Look for: brands sponsoring similar-size creators (check
"#ad", "paid partnership", description links), tools the creator already
uses, and brands launching something this month (`references/signal_playbook.md`).
Score each 1 to 5 on audience fit, budget signal and timing. Keep the top 10.

## Step 3: one specific fact per brand (`references/account_navigation.md`)

For each, find who handles partnerships (influencer manager, marketing
lead, or the founder for small brands) and one specific thing: a launch, a
campaign, a creator they just used. The pitch is built on that fact.

## Step 4: write the pitch (`references/email_frameworks.md`, `references/offer_strategy.md`)

Under 120 words. Line one is the specific fact. Then: why this audience,
one proof number, one concrete idea for the content (a hook line, not "a
collab"), a clear ask with two options and a price anchor. Write a 2-touch
follow-up at day 4 and day 9. Run every draft through `/ig-human`. For
Instagram DMs, hand to `/ig-dm`.

**Pricing starting point** (say it is a starting point): about $10 to $25 per
1,000 median views for a dedicated short-form video, adjusted for niche
(finance, tech and B2B pay more), usage rights, exclusivity and turnaround.

## Step 5: replies (`references/objection_handling.md`, `references/sales_methodology.md`)

Classify the reply (yes, price push-back, "send rates", "not now", no) and
draft the answer. Never discount on the first push-back; trade scope instead.

## Never

- Send anything. Every email and DM is a draft until the user says send.
- Buy lists, scrape personal emails, or mass-mail. Ten good pitches beat 500.
- Invent audience numbers or past results.

For full automated B2B prospecting (local businesses, cold email sequences,
a dashboard), the complete Harvey agent is fetched by `./setup.sh` into
`tools/harvey`; see its README.
