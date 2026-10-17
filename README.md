# Viral Hyper Agent

**A social media team for one person, built for Claude Code.**

Tell it your niche. It finds what is breaking out right now on YouTube Shorts,
Instagram Reels and TikTok, ranks it against each creator's own baseline (not
raw views), picks the trends that fit you, takes the best one apart shot by
shot, and writes your version for every platform: script, hook, shot list,
beat-timed edit sheet, caption, hashtags, carousel, stories, long-form
YouTube. It strips the AI tone out, schedules only what you approve, then
reads the results and gets sharper every week.

It combines seven open-source projects (all MIT) into one agent:

| from | what it brings |
| --- | --- |
| [youtube-agent-skill](https://github.com/Jakeschincariol/youtube-agent-skill) | 11 YouTube skills: viral research, scripts, Shorts, titles, SEO, retention, editing |
| [instagram-agent-skill](https://github.com/Jakeschincariol/instagram-agent-skill) | 13 Instagram skills: reels off 26 hook formulas, captions, carousels, stories, DMs, the humanizer |
| [founder-skill](https://github.com/Jakeschincariol/founder-skill) | 11 founder skills: offer, pricing, competitors, launch, marketing, unit economics |
| [ReelMimic](https://github.com/edenfunf/reelmimic) | the reel analyzer (cuts, BPM, beats, camera, look) and the full 2D remake studio |
| [Harvey](https://github.com/ethanplusai/harvey) | sales playbooks, used here for sponsor and brand-deal outreach |
| [gstack](https://github.com/garrytan/gstack) | optional: real-browser `browse` and `scrape` skills, plus a dev toolkit |
| [REA](https://github.com/morluto/rea) | optional: reverse-engineer a competitor's app or website feature |

## What's inside

```
.claude/agents/
  viral-hyper-agent     the conductor: runs the whole loop, delegates, holds the bar
  trend-scout           cross-platform trend research
  reel-mimic-director   breaks down a trending reel, plans your remake
  growth-strategist     offer, pricing, launch, brand deals
  publisher             calendar, approved scheduling, analytics
.claude/skills/
  viral        the main loop: radar > slate > make > humanize > ship > learn
  trend-hunt   trend radar ranked by outlier multiple (+ scripts/trends.py)
  reel-mimic   take any reel apart and rebuild the formula (+ scripts/analyze.py)
  publish      calendar + Zernio scheduling, only with your yes
  brand-deals  sponsors: media kit, fit, pitch, pricing, replies
  yt-*  ig-*  founder-*   the 35 specialist skills
templates/     voice files: fill one so everything sounds like you
workspace/     your brand file, trends, content and learnings (stays local)
tools/         full ReelMimic, Harvey, gstack, REA (fetched by setup)
```

## Install

You need [Claude Code](https://code.claude.com), Python 3.10+, Git, and
FFmpeg for the reel analyzer.

**Windows (PowerShell):**

```powershell
git clone https://github.com/sanjayhatwa/Claude-AI-.git D:\viral-hyper-agent
cd D:\viral-hyper-agent
.\setup.ps1            # add -Global to use the agents in every project
```

**macOS / Linux:**

```bash
git clone https://github.com/sanjayhatwa/Claude-AI-.git viral-hyper-agent
cd viral-hyper-agent
./setup.sh             # add --global to use the agents in every project
```

**As a Claude Code plugin** (skills and agents only):

```
/plugin marketplace add sanjayhatwa/Claude-AI-
/plugin install viral-hyper-agent
```

**In a Claude Code cloud session:** start a session on this repo at
claude.ai/code. The agents and skills load by themselves. For the trend
radar, the environment's network policy must allow YouTube.

Downloaded the ZIP instead of cloning? Unzip it, open the folder, and run the
same setup script.

## Use it

Open Claude Code in the folder and talk to it:

| say | what happens |
| --- | --- |
| `make me go viral` | the full loop for this week (asks for your niche first time) |
| `what's trending in <niche>?` | `/trend-hunt`: rising, peaking and dying trends with evidence |
| `copy this trend: <link>` | `/reel-mimic`: breakdown + your shoot plan and edit sheet |
| `write a reel about <idea>` | `/ig-reel`: scored hooks, script, on-screen text, beat sheet |
| `turn this video into a week of posts` | `/ig-repurpose` + `/yt-shorts` |
| `schedule this week` | `/publish`: shows the batch, schedules after your yes |
| `find me sponsors` | `/brand-deals`: media kit, 10 fits, pitches |
| `what should I sell?` | growth-strategist + founder skills |

First run: it copies `workspace/brand.example.md` to `workspace/brand.md`
and fills it in with you (niche, platforms, what you will film, how many
posts a week you can make). Spend ten more minutes on
`templates/voice-instagram.md`; it is the difference between generic and you.

### Scheduling

`/publish` schedules through **Zernio** when it is connected to Claude
(Settings > Connectors). Without it you get a copy-paste pack per post.

## The rules it follows

- **Nothing goes live without your yes**, per batch.
- **Never asks for your passwords** and never logs in for you.
- **Reads, does not scrape.** No bots, crawlers or background loops against
  Instagram, TikTok or YouTube. That is how accounts get blocked.
- **Copies formulas, never content.** Hook shapes, structure and pacing,
  never someone's footage, script, voice or face.
- **No invented numbers.** If it could not reach a source, it says so.
- **No guarantees.** Nobody controls the algorithm. This raises the odds:
  timing, hooks, retention, volume, and learning from every post.

## Tests

```bash
python3 -m unittest discover -s tests
```

## License

MIT. Each bundled project keeps its own MIT license; see
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) and `licenses/`.
