# Third-party notices

This repository combines work from the following MIT-licensed projects. Each
license text is in `licenses/`.

| project | copyright | used here | upstream commit |
| --- | --- | --- | --- |
| youtube-agent-skill | (c) 2026 Jake Schincariol | `.claude/skills/yt-*`, `templates/voice-youtube.md` (copied unchanged) | a2feb21 |
| instagram-agent-skill | (c) 2026 Jake Schincariol | `.claude/skills/ig-*`, `templates/voice-instagram.md` (copied unchanged) | d03c56b |
| founder-skill | (c) 2026 Jake Schincariol | `.claude/skills/founder-*` (unchanged), `tests/` (paths adapted) | fd9385b |
| ReelMimic | (c) 2026 ReelMimic contributors | `.claude/skills/reel-mimic/scripts/analyze.py` (one fix: zero-bpm guard); full studio fetched into `tools/reelmimic`, launched by `python -m vstudio mimic2d` | 782a3ba |
| Harvey | (c) 2026 Ethan Rogers | `.claude/skills/brand-deals/references/*.md` (unchanged); full agent fetched into `tools/harvey` | e114e95 |
| gstack | (c) 2026 Garry Tan | fetched into `tools/gstack` only | d258bcf |
| REA | (c) 2026 morluto | fetched into `tools/rea` only | 2092f90 |

Projects fetched into `tools/` by `setup.sh` / `setup.ps1` are not
redistributed in this repository and carry their own licenses and
third-party notices.

Python packages installed by setup (not redistributed here): yt-dlp
(Unlicense), rapidocr-onnxruntime (Apache-2.0, the local OCR used by
extract.py), faster-whisper (MIT), fontTools (MIT), OpenCV (Apache-2.0), librosa (ISC),
PySceneDetect (BSD-3), NumPy (BSD-3), Pillow (MIT-CMU).

`viral-extract/references/fonts.json` names free fonts from Google Fonts
(SIL Open Font License); no font files are included. `python -m vstudio fonts install`
downloads them from the google/fonts repository with their license files. The face
model (YuNet, opencv_zoo, MIT) is downloaded by setup into workspace/models.
Sound effects are synthesized at runtime; no audio files are included.

New work in this repository (the `viral`, `trend-hunt`, `viral-extract`,
`script-forge`, `reel-mimic`, `publish` and `brand-deals` skills, the
agents, `trends.py`, `radar.py`, `extract.py`, `captions.py`,
`voiceprint.py`, `scriptcheck.py`, `spoken.py`, the `vstudio` package, the setup scripts
and docs) is also released under the MIT license.
