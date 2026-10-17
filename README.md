# Viral Hyper Agent

**A social media team for one person, built for Claude Code.**

Give it your niche. It finds every trend breaking out right now, measures
exactly how the best videos were made (the caption font, size, colours,
position, words per card, speaking pace, hook, edit rhythm), and writes
ready-to-shoot scripts in your own voice. Each script is scored as speech and
tested on a simulated audience that shows where viewers would swipe away.
Then it puts captions on your footage in the same style, writes every
platform's post, schedules what you approve, and learns from the results.

What a run looks like (illustrative; real numbers come from your niche):

```
you:    horror games. give me scripts.

agent:  radar ......... 16 searches + autocomplete + Reddit + Google Trends -> 9 trends, 6 formats
        extract ....... top 3 trends: captions Montserrat Black 96px, white/black 8px outline,
                        2 words per card, 70% down; 182 wpm; first cut 0.6s; hook lands in 1.4s
        voice ......... your voiceprint (or the niche's, until you send your own)
        scripts ....... 5 scripts, 3 angles each, scriptcheck 84-91, panel holds 6/8 at 3s
        pack .......... workspace/packs/2026-10-17-horror-games/PACK.md
```

## What's new in v2

| | v1 | v2 |
| --- | --- | --- |
| Trend research | one YouTube search, by hand elsewhere | **radar.py**: niche -> ~16 searches across 16 formats, YouTube autocomplete, Shorts + long-form, Reddit top of the week, Google Trends; views/day, engagement, outlier multiple; clusters into trends with a stage (rising / peaking / saturated); forced variety across formats; content gaps |
| Copying a viral video's style | edit rhythm only | **extract.py**: on-screen text read by local OCR, so caption font look (weight, width, case + free look-alikes), size, position, line spacing, fill / outline / box / highlight colours, words per card, timing, pinned titles; dialogue pace, sentence rhythm, pauses, hook, re-hooks, signature phrases, CTA, loop; plus the edit (cuts, beats, camera, look) |
| Recreating it | a written plan | **captions.py**: burns captions on your video in the extracted style (or exports an .ass file for CapCut / Premiere / DaVinci) |
| Scripts | template-driven, sounded like AI | **script-forge**: your voiceprint from real transcripts, three angles written for the ear, **scriptcheck.py** (8 speech checks + line-by-line fixes), **viewer-panel** agent (simulated swipe test), production sheet with timed lines, on-screen text, shots and delivery notes |
| One-command flow | no | `/viral` fast path: niche -> trends -> style DNA -> scripts -> pack |

## Install

You need [Claude Code](https://code.claude.com), Python 3.10+, Git and FFmpeg.

**Windows (PowerShell):**

```powershell
git clone https://github.com/sanjayhatwa/Claude-AI-.git D:\viral-hyper-agent
cd D:\viral-hyper-agent
.\setup.ps1            # add -Global to use the agents in every project
```

**macOS / Linux:** `git clone ... && cd viral-hyper-agent && ./setup.sh`

**Plugin only:** `/plugin marketplace add sanjayhatwa/Claude-AI-` then `/plugin install viral-hyper-agent`

**Cloud session:** start one on this repo at claude.ai/code. The trend radar
and URL downloads need the environment's network policy to allow
`youtube.com`, `googlevideo.com`, `reddit.com`, `trends.google.com` and
`suggestqueries.google.com`.

Setup installs the local OCR model (included in the package) and
faster-whisper (first transcription downloads a ~500 MB speech model).

## Use it

| say | what happens |
| --- | --- |
| `<your niche>. give me scripts` | the fast path: radar, style extraction, 5 tested scripts, one pack |
| `what's trending in <niche>?` | `/trend-hunt`: RADAR.md with evidence, stages, formats, gaps |
| `extract the style of <link>` | `/viral-extract`: STYLE_DNA.md + font sheet |
| `put these captions on my video` | `captions.py` with that style |
| `write a script about <idea>` | `/script-forge`: 3 angles, scored, panel-tested |
| `does this sound like AI?` + script | `scriptcheck.py` with line-by-line fixes |
| `learn my voice` + transcripts | `voiceprint.py` -> workspace/voice.json |
| `copy this trend: <link>` | `/reel-mimic`: style + shoot plan + edit sheet |
| `schedule this week` | `/publish`: shows the batch, schedules after your yes |
| `find me sponsors` | `/brand-deals` |

The single biggest upgrade to script quality: give it 3 of your own videos'
transcripts or captions once (`learn my voice`). Until then it writes in the
niche's voice, taken from the outlier videos.

### Run the tools directly

```bash
python3 .claude/skills/trend-hunt/scripts/radar.py "horror games" --geo IN --subreddits horrorgaming
python3 .claude/skills/viral-extract/scripts/extract.py "<url or file>" --out workspace/dna/ref1
python3 .claude/skills/viral-extract/scripts/captions.py --style workspace/dna/ref1/style_dna.json --video me.mp4 --out me_cap.mp4
python3 .claude/skills/script-forge/scripts/voiceprint.py my_videos/*.srt --out workspace/voice.json
python3 .claude/skills/script-forge/scripts/scriptcheck.py script.md --seconds 30 --wpm 175 --voice workspace/voice.json
```

## How accurate is it

- **Caption style**: tested by rendering captions with known settings, reading
  them back, re-applying them to new footage and reading again. Position is
  within about 1% of the frame, font size within about 6%, outline thickness
  within about 1 px, colours exact on clean text, words per card exact. Busy
  backgrounds make colour readings noisier.
- **Font names**: pixels give the font's weight, width and case, and the
  agent narrows it to free look-alikes, then confirms by looking at the crops.
  It is a look-alike, not a guaranteed identification of a paid font.
- **scriptcheck**: heuristics for what makes a script sound written. It
  separates a typical AI script (42, REWRITE) from a human one (94, SHIP) in
  the test fixtures, but it's a gate, not a judge. The viewer panel is the
  judgment layer, and it's a simulation that finds weak beats, not a view
  forecast.
- **Trends**: only as good as what's reachable. TikTok and Instagram numbers
  need your own browser (the agent drives it with you, at human speed).

## The rules it follows

- **Nothing goes live without your yes**, per batch. Never asks for passwords.
- **Reads, does not scrape**: a few dozen spaced requests, no bots, no logins.
- **Copies style and structure, never content**: no one else's footage,
  script, voice or face.
- **No invented numbers.** If a source was unreachable, the output says so.
- **No guarantees.** It raises the odds: timing, hooks, retention, a human
  voice, volume, and learning from every post.

## What's inside

```
.claude/agents/   viral-hyper-agent, trend-scout, reel-mimic-director, viewer-panel,
                  growth-strategist, publisher
.claude/skills/   viral, trend-hunt, viral-extract, script-forge, reel-mimic, publish,
                  brand-deals, yt-* (11), ig-* (13), founder-* (11)
templates/        voice files
workspace/        your brand, voice, trends, style DNA, packs (stays local)
tools/            full ReelMimic, Harvey, gstack, REA (fetched by setup)
tests/            python3 -m unittest discover -s tests
```

Built on [youtube-agent-skill](https://github.com/Jakeschincariol/youtube-agent-skill),
[instagram-agent-skill](https://github.com/Jakeschincariol/instagram-agent-skill),
[founder-skill](https://github.com/Jakeschincariol/founder-skill),
[ReelMimic](https://github.com/edenfunf/reelmimic),
[Harvey](https://github.com/ethanplusai/harvey),
[gstack](https://github.com/garrytan/gstack) and
[REA](https://github.com/morluto/rea), all MIT.

## License

MIT. Bundled projects keep their own MIT licenses: see
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) and `licenses/`.
