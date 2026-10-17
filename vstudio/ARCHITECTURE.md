# Viral Studio: architecture contract

Viral Studio is the local video engine of the Viral Hyper Agent. It runs on
the user's own machine (`python -m vstudio serve` opens a web app on
http://127.0.0.1:4320) and uses the user's Claude subscription through the
Claude Code CLI (`claude -p`) for planning. No cloud render service, no API
key required. Everything below is a contract: modules are built in parallel
against it, so names, fields and signatures here are binding.

```
prompt + reference videos + competitor videos + assets
        │
        ▼
  director.py ──► extract.py (Style DNA, existing skill) ──► style targets
        │
        ├─► llm.py (claude -p, JSON)  or  planner.heuristic_plan (offline)
        ▼
     PLAN (beats)  ──► planner.compile_plan ──► TIMELINE SPEC (JSON)
                                                   │
raw talking video ──► autoedit.py ─────────────────┤
                                                   ▼
                                     render.py (numpy compositor + ffmpeg)
                                                   │
                                                   ▼
                                                 MP4
channel URLs ──► channel.py + thumbs.py ──► CHANNEL_DNA.md/json (topic-tagged)
web UI ──► server.py ──► jobs (python -m vstudio <cmd>) ──► project folders
```

## Package layout and owners

```
vstudio/
  __init__.py        version
  __main__.py        CLI dispatcher: python -m vstudio <command> [args]
  paths.py           repo paths, skill-module loader, workspace dirs
  ARCHITECTURE.md    this file
  timeline.py        spec schema, defaults, normalize(), validate(), duration()      [engine]
  anim.py            easing functions, keyframe interpolation, enter/exit presets     [engine]
  fonts.py           font resolution + `fonts install` (Google Fonts, OFL)            [engine]
  textrender.py      text → RGBA sprite (Pillow): stroke, shadow, box, highlight      [engine]
  render.py          compositor, effects, camera, captions burn, encode; CLI render   [engine]
  sfx.py             procedural sound effects (no copyrighted audio)                  [engine]
  audio.py           audio mixdown with ducking, fades, clip audio                    [engine]
  templates.py       motion-graphics catalog → layer lists (pure data)                [templates]
  transcribe.py      words from captions file / whisper / json                        [autoedit]
  reframe.py         face/subject tracking for 16:9 → 9:16                            [autoedit]
  autoedit.py        raw video → professionally edited timeline                       [autoedit]
  thumbs.py          thumbnail analysis + contact sheets                              [channel]
  channel.py         channel / profile analysis → CHANNEL_DNA                         [channel]
  llm.py             Claude Code CLI wrapper (subscription), JSON + schema            [director]
  planner.py         PLAN → TIMELINE compiler, offline heuristic planner              [director]
  director.py        prompt + refs + competitors + assets → plan → spec → mp4 → QA    [director]
  jobs.py            background job runner (subprocess + logs)                        [studio]
  server.py          local web app + JSON API (stdlib only)                           [studio]
  web/               index.html, app.js, style.css (no build step)                    [studio]
tests/test_vs_<module>.py                                                             [each owner]
```

Rules for every module:

- Python 3.10+, stdlib + numpy, opencv-python(-headless), Pillow, and what is
  in requirements.txt. No new heavy dependencies without a fallback.
- Every external tool (ffmpeg, yt-dlp, claude, faster-whisper) is optional at
  import time: import lazily, fail with a clear message at call time.
- Every command has `main(argv=None) -> int` and is wired in `__main__.py`.
- No model identifiers anywhere in code or docs. The Claude CLI's default
  model is used unless the user sets `VSTUDIO_CLAUDE_MODEL`.
- Paths in specs may be absolute or relative to the spec file's folder
  (`base_dir`).
- Tests must run offline in under ~60 s per file, at small resolutions
  (e.g. 180x320, 10 fps, 1-3 s) using fonts that exist (`fonts.resolve`
  falls back to whatever the system has).

## Existing pieces to reuse (do not duplicate)

`paths.load_skill(name)` imports these from `.claude/skills/*/scripts/`:

| module | path | use |
| --- | --- | --- |
| `extract` | viral-extract/scripts/extract.py | `parse_json3`, `parse_srt_vtt`, `load_captions`, `whisper_words`, `dialogue_dna`, `text_dna`, `edit_dna`, `youtube_captions`, `ANALYZE` (path to analyze.py) |
| `captions` | viral-extract/scripts/captions.py | `build_ass(words, cap, W, H, font, pop)`, `DEFAULT_CAPTION`, `caption_style(dna_path)`, `chunk`, `words_from_script(text, wpm, start)` |
| `trends` | trend-hunt/scripts/trends.py | `ytdlp(args, timeout)` JSON helper, `entry_row` |
| `radar` | trend-hunt/scripts/radar.py | `classify_format(text)`, `keywords(text)`, `jaccard` |
| `spoken` | script-forge/scripts/spoken.py | `text_stats`, `sentences`, `words` |
| `scriptcheck` | script-forge/scripts/scriptcheck.py | `score_script(text, seconds, wpm, voice)` |
| `deadair` | yt-edit/deadair.py | reference for DEAD / FILLER / REPEAT logic |

Words everywhere are `[{"w": str, "s": float, "e": float}]` (seconds).

---

## 1. TIMELINE SPEC (version 1)

The single format every producer emits and `render.py` consumes. JSON.

```json
{
  "version": 1,
  "width": 1080, "height": 1920, "fps": 30,
  "duration": 30.0,
  "background": "#0E0E12",
  "layers": [ ...layer objects, drawn in order: later = on top... ],
  "camera": { "zoom": [[0, 1.0], [2.0, 1.12, "out-cubic"]], "x": [], "y": [], "rotation": [] },
  "effects": [ ...effect objects... ],
  "captions": { "words": [...], "style": {...}, "style_dna": "path/to/style_dna.json", "font": "Montserrat Black", "pop": true },
  "audio": [ ...audio objects... ],
  "meta": { "title": "...", "source": "director|autoedit|manual", "notes": "..." }
}
```

- `duration` optional: default = max(layer.end, audio end, last caption word).
- `background`: `"#RRGGBB"`, or `{"gradient": ["#hex", "#hex"], "angle": 90}` (degrees, 0 = left→right, 90 = top→bottom).
- Colors everywhere: `"#RRGGBB"` or `"#RRGGBBAA"`.
- Keyframe lists: `[[t, value], [t, value, "easing"], ...]`; `t` in seconds
  relative to the owner's start (layer start; for `camera`, timeline time).
  The easing on a keyframe applies to the segment ARRIVING at that keyframe.
  Before the first key: first value; after the last: last value.
- Easing names: `linear, in-quad, out-quad, in-out-quad, in-cubic, out-cubic,
  in-out-cubic, in-quart, out-quart, in-out-quart, in-expo, out-expo,
  in-out-expo, out-back, in-back, in-out-back, out-elastic, out-bounce, step`.

### Common layer fields

| field | default | meaning |
| --- | --- | --- |
| `type` | required | `video`, `image`, `text`, `shape`, `progress`, `counter` |
| `id` | null | free label |
| `start`, `end` | 0, duration | seconds on the timeline (`end` exclusive) |
| `x`, `y` | canvas centre | anchor position in px; also accepts `"50%"` strings (of width / height) |
| `anchor` | `"center"` | `center, top-left, top, top-right, left, right, bottom-left, bottom, bottom-right` |
| `opacity` | 1 | 0..1 |
| `scale` | 1 | uniform |
| `rotation` | 0 | degrees, clockwise |
| `animate` | {} | keyframes per prop: `x, y, opacity, scale, rotation, blur` (blur in px) |
| `enter` | null | `{"type": <enter>, "duration": 0.3, "delay": 0}` |
| `exit` | null | `{"type": <exit>, "duration": 0.25}` (ends at `end`) |
| `fixed` | false | true = not moved by `camera` (HUD, captions, progress) |
| `blend` | `"normal"` | `normal, add, screen, multiply` |
| `shadow` | null | `{"color": "#00000099", "dx": 0, "dy": 8, "blur": 16}` (drop shadow of the layer's alpha) |

Enter types: `none, fade, pop, slide-up, slide-down, slide-left, slide-right,
zoom-in, zoom-out, drop, bounce, stamp, blur-in, wipe-right, wipe-left,
wipe-up, typewriter, words, crossfade, whip-left, whip-right, glitch`.
Exit types: `none, fade, pop-out, slide-up, slide-down, slide-left,
slide-right, zoom-in, zoom-out, blur-out, wipe-right, wipe-left, whip-left,
whip-right, glitch`.

`typewriter` and `words` apply to text and counter layers (reveal characters
or words across the enter duration; with `"word_times": [s, s, ...]` on the
text layer, word i appears at layer-relative second `word_times[i]`).
`crossfade` = fade in while the layer below stays (used for video
transitions; the producer overlaps layers by the duration). `whip-*` = fast
slide with horizontal motion blur. `glitch` = RGB split + offset jitter.
Enter/exit animations multiply with `animate` values (scale, opacity) and
add to them (x, y, rotation).
`animate` keyframes are absolute values of the property (an `x` key of 540
means x = 540 px); the static field applies only to a property without keys.

### `video`

```json
{"type": "video", "src": "clip.mp4", "in": 3.2, "speed": 1.0,
 "fit": "cover", "focus": [0.5, 0.4], "w": null, "h": null,
 "volume": 1.0, "kenburns": [1.0, 1.08], "loop": false}
```
- Plays source from `in` (seconds) at `speed`. Frame at timeline t =
  source time `in + (t - start) * speed`.
- `fit`: `cover` (fill w×h, crop around `focus` = normalized point in the
  source that stays in frame), `contain` (letterbox, transparent bars),
  `fill` (stretch). `w`/`h` default to canvas size.
- `kenburns`: scale from a to b over the layer's life (multiplies `scale`).
- `volume`: clip audio included in the mix (0 = muted). Audio follows `in`/`speed` (atempo for 0.5..2).
- `loop`: wrap source when it runs out; otherwise hold the last frame.

### `image`

```json
{"type": "image", "src": "logo.png", "fit": "contain", "w": 400, "h": 400, "kenburns": null, "radius": 0}
```
`w`/`h` null = natural size (or canvas size with fit cover). `radius` rounds corners.

### `text`

```json
{"type": "text", "text": "NOBODY TELLS YOU THIS", "font": "Montserrat Black",
 "size": 96, "color": "#FFFFFF", "case": "upper",
 "stroke": {"color": "#000000", "width": 8},
 "box": {"color": "#FFE500", "pad": [24, 14], "radius": 18},
 "highlight": {"words": [2], "color": "#FFE500", "box": null},
 "max_width": 900, "align": "center", "line_spacing": 1.05, "letter_spacing": 0,
 "word_times": null, "gradient": null}
```
- `case`: `none, upper, lower, title`.
- Wraps to `max_width` (px). `align`: `left, center, right`.
- `highlight.words`: word indices (0-based, after case transform) drawn in
  `highlight.color`; `highlight.box`: optional `{"color","pad","radius"}`
  pill behind those words.
- `gradient`: `["#hex", "#hex"]` vertical fill gradient instead of `color`.
- `font`: family name with optional weight word ("Montserrat Black",
  "Inter Bold", "Anton") or a file path; resolved by `fonts.resolve`.

### `shape`

```json
{"type": "shape", "shape": "rect", "w": 800, "h": 12, "color": "#FFE500",
 "radius": 6, "stroke": {"color": "#000000", "width": 0},
 "points": null, "gradient": null, "draw": null}
```
`shape`: `rect, circle, ellipse, line, polygon, ring`. `polygon.points` =
`[[x, y], ...]` in 0..1 of the w×h box. `line` draws from (0, h/2) to (w, h/2)
with thickness `h`. `ring` is a circle outline (`stroke.width` thickness).
`draw`: `{"from": 0, "to": 1, "ease": "out-cubic"}` progressively reveals the
shape over the enter duration (left→right for rect/line, clockwise for
circle/ring), from `enter.delay`, whatever the enter type (`"none"` draws
without also fading in). A stroke with a transparent `color` is an outline.

### `progress`

```json
{"type": "progress", "y": 0, "anchor": "top-left", "x": 0, "w": 1080, "h": 10,
 "color": "#FFE500", "track": "#FFFFFF33", "from": 0.0, "to": 1.0, "fixed": true}
```
Fills from `from` to `to` linearly across the layer's life.

### `counter`

Text layer fields plus:
```json
{"type": "counter", "from": 0, "to": 12500, "decimals": 0, "prefix": "$",
 "suffix": "", "separator": ",", "count_duration": 1.2, "ease": "out-expo"}
```

### `camera`

Keyframes in timeline seconds: `zoom` (1 = none), `x`, `y` (px offset),
`rotation` (deg). Applied around the canvas centre to the composite of all
non-`fixed` layers.

### `effects`

```json
{"type": "flash", "start": 4.0, "duration": 0.15, "color": "#FFFFFF", "opacity": 0.85}
{"type": "shake", "start": 4.0, "duration": 0.3, "intensity": 18}
{"type": "glitch", "start": 7.0, "duration": 0.25, "intensity": 1.0}
{"type": "zoom-blur", "start": 9.0, "duration": 0.2, "intensity": 1.0}
{"type": "vignette", "strength": 0.35}
{"type": "grade", "contrast": 1.08, "saturation": 1.12, "brightness": 0.0, "warmth": 0.05}
{"type": "grain", "strength": 0.04}
{"type": "letterbox", "start": 0, "duration": 3, "ratio": 2.35}
```
Timed effects apply between `start` and `start + duration`; `vignette`,
`grade`, `grain` without `start` apply to the whole video. Effects act on
the full frame after camera, before captions.

### `captions`

Burned last, with libass, through `captions.build_ass` (existing skill).
`words` are timeline-time words. Style resolution order: `style` dict
(captions.py keys: `font_size_pct, center_x_pct, center_y_pct, max_width_pct,
case, fill, highlight, outline, outline_pct, box, words_per_card, lines_per_card,
align, font_candidates, font_look`), else `style_dna` path, else
`captions.DEFAULT_CAPTION`. `font` overrides the font; `pop` adds the
bounce-in. Across a silence longer than 0.6 s a card ends 0.4 s after its
last word (it never lingers into a stretch without words), and trailing
`. , ; :` are dropped on screen (words keep them, `chunk` ends cards on them).

### `audio`

```json
{"src": "music.mp3", "start": 0, "in": 0, "duration": null, "volume": 0.25,
 "fade_in": 0.5, "fade_out": 1.5, "role": "music", "duck": true, "loop": true}
{"src": "voice.wav", "start": 0, "role": "voice", "volume": 1.0}
{"sfx": "whoosh", "start": 2.0, "volume": 0.7}
```
- `role`: `music`, `voice`, `sfx`, `clip`. Music with `duck: true` is
  compressed under every `voice` source and every `video` layer whose
  `volume` > 0 (sidechain).
- `sfx` names (generated by `sfx.py`, cached as wav): `whoosh, swoosh,
  pop, click, tick, hit, boom, riser, ding, glitch, typing, camera`.
- Output loudness normalized to about -14 LUFS (`loudnorm`).

---

## 2. PLAN (director / LLM output, compiled to a timeline)

A plan is what Claude writes. It is higher level than a timeline so the
model never has to place pixels.

```json
{
  "title": "3 settings costing you views",
  "aspect": "9:16", "fps": 30, "duration": 32,
  "style": {
    "font": "Montserrat Black", "font_secondary": "Inter Bold",
    "text": "#FFFFFF", "accent": "#FFE500", "bg": "#0E0E12", "bg2": "#1B1B24",
    "energy": "high", "cut_every": 1.6,
    "caption_style": { ...captions.py keys... }, "captions": true, "progress_bar": true,
    "grade": {"contrast": 1.06, "saturation": 1.1}
  },
  "voiceover": {"src": null, "words": null},
  "music": {"src": null, "volume": 0.22},
  "beats": [
    {
      "start": 0.0, "end": 2.4,
      "say": "I lost three hundred dollars on one setting.",
      "visual": {"template": "hook_title", "params": {"text": "I LOST $300", "highlight": ["$300"]}},
      "background": {"asset": "broll1.mp4", "in": 2.0, "zoom": [1.0, 1.1]},
      "transition": "flash",
      "sfx": "hit",
      "camera": "punch"
    }
  ],
  "cta": {"template": "cta", "params": {"text": "Follow for part 2"}}
}
```

- `beats` are contiguous or may overlap by a transition; `start`/`end` seconds.
- `visual`: one template call (see catalog) or `null`. `visual` may also be
  a list of template calls.
- `background`: `{"asset": file, "in": s, "zoom": [a, b], "focus": [fx, fy]}`,
  `{"color": hex}`, `{"gradient": [hex, hex], "angle": deg}`, `{"keep": true}`
  (continue the previous background) or null (style.bg).
- `transition` into this beat: `cut, flash, whip, zoom, fade, glitch, slide`.
- `sfx`: any sfx name or null. `camera`: `none, punch, shake, drift, push`.
- Captions: when `voiceover.words` exist they drive captions; otherwise
  each beat's `say` is timed evenly inside the beat (and used as the
  on-screen caption track when `style.captions` is true).
- `aspect`: `9:16` (1080x1920), `1:1` (1080x1080), `4:5` (1080x1350), `16:9` (1920x1080).

---

## 3. Module APIs

### timeline.py
```python
SCHEMA_VERSION = 1
def normalize(spec: dict, base_dir: str | None = None) -> dict   # fill defaults, resolve % coords and relative paths
def validate(spec: dict) -> list[str]                             # human-readable errors; [] = valid
def duration(spec: dict) -> float
def load(path_or_dict, base_dir=None) -> dict                     # read JSON, normalize
def save(spec, path) -> None
```

### anim.py
```python
EASINGS: dict[str, callable]
def ease(name, x) -> float
def value_at(keys: list, t: float, default: float) -> float      # keyframe interpolation
def enter_state(kind, p, layer) -> dict   # p in 0..1 → {"dx","dy","scale","opacity","blur","reveal","wipe","rgb_split"}
def exit_state(kind, p, layer) -> dict
```

### fonts.py
```python
def resolve(name_or_path: str | None, fallback_bold=True) -> str          # path to a .ttf/.otf, never raises
def load(name_or_path, size: int) -> PIL.ImageFont.FreeTypeFont           # handles variable fonts (set_variation_by_name)
def installed() -> list[dict]
def install(families: list[str]) -> list[str]   # downloads OFL fonts from raw.githubusercontent.com/google/fonts into workspace/fonts
def main(argv=None) -> int                       # `vstudio fonts list|install <family>...|resolve <name>`
```
Search order: `$VSTUDIO_FONTS`, `workspace/fonts`, `assets/fonts`, user font
dirs (Windows `%LOCALAPPDATA%\Microsoft\Windows\Fonts`, `C:\Windows\Fonts`,
macOS `~/Library/Fonts`, `/Library/Fonts`, Linux `~/.fonts`,
`~/.local/share/fonts`, `/usr/share/fonts`), then `fc-match` if present,
then any bold sans found, then Pillow's default font.

### textrender.py
```python
def render_text(layer: dict, scale: float = 1.0, reveal: float = 1.0, words_visible: int | None = None) -> PIL.Image (RGBA)
```
Returns a tight sprite (with stroke/box/shadow padding). Cached by content.

### render.py
```python
def render(spec, out_path, preview=False, progress=None, workdir=None, base_dir=None) -> str
def render_frame(spec, t, preview=False) -> numpy.ndarray  # HxWx3 uint8 (no captions)
def still(spec, t, out_png) -> str
def main(argv=None) -> int   # vstudio render spec.json --out out.mp4 [--preview] [--still 2.5 --out f.png]
```
`preview=True` renders at half resolution and half fps (fast drafts).
Video decoding: one ffmpeg reader per video layer, sequential.
Encode: frames piped to ffmpeg (libx264, yuv420p, crf 18, veryfast) with
captions burned in the same pass via the `ass` filter; then audio mixed by
`audio.mixdown` and muxed (aac 192k). Progress callback `(done_frames, total)`.

### sfx.py
```python
NAMES = ["whoosh", "swoosh", "pop", "click", "tick", "hit", "boom", "riser", "ding", "glitch", "typing", "camera"]
def make(name: str, sr=48000) -> str        # path to cached wav (workspace/cache/sfx)
def main(argv=None) -> int                  # vstudio sfx list | vstudio sfx render <name> --out f.wav
```

### audio.py
```python
def mixdown(spec, video_duration, out_wav, base_dir=None, workdir=None) -> str | None   # None when no audio at all
```

### templates.py
```python
CATALOG: dict[str, dict]   # name -> {"description", "category", "params": {p: {"type","default","doc"[, "choices"]}}, "fn"}
DEFAULT_STYLE = {"font": "Montserrat Black", "font_secondary": "Inter Bold", "text": "#FFFFFF",
                 "accent": "#FFE500", "bg": "#0E0E12", "bg2": "#1B1B24", "energy": "high"}
def build(name, params, start, end, canvas, style=None) -> list[dict]   # layer dicts valid per timeline spec
def catalog_doc() -> str      # markdown for the LLM prompt (names, params, when to use)
def main(argv=None) -> int    # vstudio templates [--json] | vstudio templates preview <name> --out f.mp4
```
`canvas = {"width": W, "height": H, "fps": F}`; sizes must scale with W/H
(design at 1080x1920, multiply by `min(W/1080, H/1920)` or similar), so the
same template works at 16:9 and 1:1. Templates (at least): `hook_title,
kinetic_text, stat, counter_reveal, list_item, checklist, quote,
lower_third, cta, progress_bar, versus, before_after, highlight_box, arrow,
countdown, title_card, emphasis_word, icon_badge, subscribe, timeline_steps,
comment_bubble, ranking, question, source_card`. Icons are drawn with
`shape` polygons (heart, star, check, play, bell, arrow, fire-like flame,
lightning) since emoji fonts are not portable.

### transcribe.py
```python
def get_words(media: str | None = None, captions: str | None = None, words_json: str | None = None,
              model="small", lang=None) -> list | None
def remap(words, segments) -> list   # source-time words → output time after cuts; segments = [{"in","out","t0"}]
```

### reframe.py
```python
def detect_faces(frame_bgr) -> list[(x, y, w, h, score)]      # YuNet if model present, else Haar if present, else []
def track_focus(video, times: list[float], aspect=(9, 16)) -> list[[fx, fy]]   # smoothed, normalized
def ensure_model() -> str | None   # downloads YuNet onnx from opencv_zoo into workspace/models if missing
```

### autoedit.py
```python
def plan_edit(video, words, opts) -> dict      # returns a TIMELINE SPEC; opts keys below
def main(argv=None) -> int
# vstudio autoedit raw.mp4 --out-dir DIR [--words w.json | --captions f.srt] [--style style_dna.json]
#     [--aspect 9:16|16:9|1:1|4:5|source] [--broll DIR] [--music m.mp3] [--no-zoom] [--no-graphics]
#     [--no-sfx] [--max-duration 60] [--energy high|medium|calm] [--hook "TEXT"] [--render] [--preview]
```
Writes `DIR/edit.json` (timeline), `DIR/EDIT_REPORT.md` (what was cut and
why, seconds saved, graphics added), and with `--render`, `DIR/edited.mp4`.

### channel.py / thumbs.py
```python
def analyze(urls: list[str], out_dir: str, max_videos=30, deep=4, fetch=None) -> dict
def main(argv=None) -> int
# vstudio channel <url> [<url> ...] --out-dir DIR [--max-videos 30] [--deep 4] [--no-download]
thumbs.analyze_thumb(path) -> dict ; thumbs.sheet(paths, out_path, labels=None) -> str
```
`fetch` is an injectable object with methods `list_channel(url, tab, limit)`,
`video_meta(url)`, `download_thumb(url, path)`, `download_clip(url, path,
seconds)`, `captions(url, dir)` so tests can run on fixtures. Writes
`CHANNEL_DNA.json`, `CHANNEL_DNA.md`, `thumbs_sheet.jpg`, `dna/<video_id>/`.

### llm.py
```python
class LLMUnavailable(Exception): ...
def available() -> bool
def complete(prompt, system=None, timeout=900) -> str
def complete_json(prompt, schema=None, system=None, timeout=900, retries=2) -> dict
```
Runs `claude -p --output-format json [--json-schema S] [--append-system-prompt ...]`
with the prompt on stdin, tools disabled for planning calls, model from
`VSTUDIO_CLAUDE_MODEL` only if set. Parses `result` (or `structured_output`).

### planner.py
```python
def compile_plan(plan, base_dir=None) -> dict          # PLAN → TIMELINE SPEC (uses templates.build)
def heuristic_plan(prompt, script=None, style=None, duration=30, assets=None, aspect="9:16") -> dict
def validate_plan(plan) -> list[str]
def main(argv=None) -> int   # vstudio plan compile plan.json --out spec.json
```

### director.py
```python
def make_reel(prompt, refs=(), competitors=(), assets=(), script=None, voice=None, music=None,
              aspect="9:16", duration=30, out_dir=None, llm="auto", render=True, preview=False) -> dict
def main(argv=None) -> int
# vstudio reel --prompt "..." [--ref URL|FILE]... [--competitor URL|FILE]... [--asset FILE]...
#     [--script FILE] [--voice FILE] [--music FILE] [--aspect 9:16] [--duration 30]
#     [--out-dir DIR] [--llm auto|claude|none] [--plan plan.json] [--no-render] [--preview]
```
Writes `DIR/BRIEF.md`, `DIR/plan.json`, `DIR/spec.json`, `DIR/reel.mp4`,
`DIR/QA.md` (output re-extracted and compared to the reference DNA).
Compiled specs carry `meta.beats` (beat times and lines) and
`meta.spoken_words` (every spoken word, also where a graphic shows the line
instead of a caption); QA measures pace on the latter.

### jobs.py / server.py
```python
# vstudio serve [--host 127.0.0.1] [--port 4320] [--workspace workspace/studio] [--open]
```
API (JSON): `GET /api/health`, `GET /api/projects`, `POST /api/projects`,
`GET /api/projects/<id>`, `PUT /api/projects/<id>/upload?name=<file>`
(raw body), `POST /api/projects/<id>/jobs` `{kind, args}` with kind in
`reel, autoedit, channel, extract, render, script, plan`,
`GET /api/jobs/<id>`, `POST /api/jobs/<id>/cancel`,
`GET|PUT /api/projects/<id>/spec?path=...`, `POST /api/projects/<id>/ask`
`{message, spec_path}` (Claude edits the spec), `GET /api/templates`,
`GET /api/doctor`, `GET /files/<project>/<path>` (Range support for video
seeking). Binds 127.0.0.1 only by default. Workspace layout:
`workspace/studio/projects/<id>/{project.json, uploads/, jobs/<job>/{job.json, log.txt, out/}}`.
