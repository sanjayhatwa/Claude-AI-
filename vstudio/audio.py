"""
audio.py - the sound mix of a timeline: music, voice-over, sound effects
and the audio of the video clips, mixed by ffmpeg into one 48 kHz wav.

    mixdown(spec, video_duration, "mix.wav")  -> "mix.wav", or None when nothing is audible

What goes in:
    spec["audio"] entries with a file (`src`): trimmed from `in` for
        `duration` (or to the end of the file / the video), placed at
        `start`, `volume`, `fade_in` / `fade_out`, `loop` to repeat a short
        track under the whole video;
    spec["audio"] entries with `sfx`: the procedural effects from sfx.py;
    every `video` layer with volume > 0 whose file has sound: its audio from
        `in`, at `speed` (atempo, any speed), for the layer's life, with a
        5 ms fade at each cut so jump cuts never click (none where the next
        clip continues the same take).

Music with `duck: true` is pushed down under the voice: a sidechain
compressor keyed by every `voice` source and every audible video clip (about
-9 dB while someone speaks, 15 ms attack, 400 ms release). Everything is
summed without amix's level normalization (so volumes mean what they say),
then loudness-normalized to -14 LUFS / -1.5 dBTP with ffmpeg's loudnorm in
two passes (measure, then apply linearly, so the mix keeps its dynamics).
The output is exactly video_duration long.
"""

import json
import math
import os
import re
import shutil
import subprocess
import tempfile

from . import sfx, timeline

TARGET_LUFS = -14.0
TRUE_PEAK = -1.5
LRA = 11.0
SR = 48000
CLIP_EDGE_FADE = 0.005  # s: a jump cut lands mid-waveform; a few ms of fade at each cut removes the click


class AudioError(RuntimeError):
    """ffmpeg could not produce the mix."""


def _atempo(speed):
    """atempo filters whose product is speed (each instance takes 0.5..2)."""
    out = []
    s = float(speed)
    while s > 2.0:
        out.append(2.0)
        s /= 2.0
    while s < 0.5:
        out.append(0.5)
        s /= 0.5
    if abs(s - 1.0) > 1e-6:
        out.append(s)
    return ["atempo=%.6f" % f for f in out]


def sources(spec, video_duration):
    """Everything audible, as dicts {path, start, in, dur, speed, volume, fade_in, fade_out, role, duck, loop}."""
    D = float(video_duration)
    out = []
    for a in spec.get("audio") or []:
        start = float(a.get("start") or 0)
        vol = float(a.get("volume") if a.get("volume") is not None else 1.0)
        if start >= D or vol <= 0:
            continue
        if a.get("sfx"):
            path = sfx.make(a["sfx"])
            length = sfx.duration(a["sfx"])
            src_in, loop = 0.0, False
        else:
            path = a["src"]
            info = timeline.probe(path)
            if info is not None and not info.get("has_audio"):
                continue
            src_in, loop = float(a.get("in") or 0), bool(a.get("loop"))
            length = None if loop or not info or not info.get("duration") else max(0.0, info["duration"] - src_in)
        dur = D - start
        if a.get("duration"):
            dur = min(dur, float(a["duration"]))
        if length is not None:
            dur = min(dur, length)
        if dur <= 0.01:
            continue
        out.append({"path": path, "start": start, "in": src_in, "dur": dur, "speed": 1.0, "volume": vol,
                    "fade_in": float(a.get("fade_in") or 0), "fade_out": float(a.get("fade_out") or 0),
                    "role": a.get("role") or ("sfx" if a.get("sfx") else "music"),
                    "duck": bool(a.get("duck")), "loop": loop})
    for L in spec.get("layers") or []:
        if L.get("type") != "video" or float(L.get("volume") or 0) <= 0:
            continue
        info = timeline.probe(L["src"])
        if not info or not info.get("has_audio"):
            continue
        start = float(L["start"])
        speed = float(L.get("speed") or 1.0)
        dur = min(float(L["end"]), D) - start
        src_in = float(L.get("in") or 0)
        if not L.get("loop") and info.get("duration"):
            dur = min(dur, max(0.0, info["duration"] - src_in) / speed)  # after the source ends: silence
        if dur <= 0.01:
            continue
        out.append({"path": L["src"], "start": start, "in": src_in, "dur": dur, "speed": speed,
                    "volume": float(L["volume"]), "fade_in": 0.0, "fade_out": 0.0, "role": "clip",
                    "duck": False, "loop": bool(L.get("loop"))})
    _clip_edges([s for s in out if s["role"] == "clip"])
    return out


def _continues(a, b):
    """True when clip b picks up exactly where clip a stops: one take split in two, nothing removed."""
    return (a["path"] == b["path"] and not a["loop"] and abs(a["speed"] - b["speed"]) < 1e-6
            and abs(a["start"] + a["dur"] - b["start"]) < 2e-3
            and abs(a["in"] + a["dur"] * a["speed"] - b["in"]) < 2e-3)


def _clip_edges(clips):
    """Short fades at the edges of clip audio, except where the next clip continues the same take seamlessly."""
    for c in clips:
        d = min(CLIP_EDGE_FADE, c["dur"] / 4.0)
        if not any(_continues(o, c) for o in clips if o is not c):
            c["fade_in"] = d
        if not any(_continues(c, o) for o in clips if o is not c):
            c["fade_out"] = d


def _chain(i, src):
    """Filter chain for input i: trim, tempo, format, volume, fades, delay."""
    dur = src["dur"]
    f = ["atrim=duration=%.6f" % (dur * src["speed"]), "asetpts=PTS-STARTPTS"]
    f += _atempo(src["speed"])
    f += ["aresample=%d" % SR, "aformat=sample_fmts=fltp:sample_rates=%d:channel_layouts=stereo" % SR,
          "volume=%.6f" % src["volume"]]
    if src["fade_in"] > 0:
        f.append("afade=t=in:st=0:d=%.6f" % min(src["fade_in"], dur))
    if src["fade_out"] > 0:
        fo = min(src["fade_out"], dur)
        f.append("afade=t=out:st=%.6f:d=%.6f" % (max(0.0, dur - fo), fo))
    ms = int(round(src["start"] * 1000))
    if ms > 0:
        f.append("adelay=%d|%d" % (ms, ms))
    return "[%d:a]%s[s%d]" % (i, ",".join(f), i)


def _mix(labels, out_label):
    if len(labels) == 1:
        return "%sanull[%s]" % (labels[0], out_label)
    return "%samix=inputs=%d:normalize=0:duration=longest:dropout_transition=0[%s]" % (
        "".join(labels), len(labels), out_label)


def build_graph(srcs, D):
    """(input args, filter_complex) for a list of sources; the final label is [mix]."""
    args, chains = [], []
    for i, s in enumerate(srcs):
        if s["loop"]:
            args += ["-stream_loop", "-1"]
        if s["in"] > 0:
            args += ["-ss", "%.6f" % s["in"]]
        args += ["-i", s["path"]]
        chains.append(_chain(i, s))
    is_key = [s["role"] in ("voice", "clip") for s in srcs]           # what music ducks under
    is_ducked = [s["role"] == "music" and s["duck"] for s in srcs]
    key = ["[s%d]" % i for i in range(len(srcs)) if is_key[i]]
    ducked = ["[s%d]" % i for i in range(len(srcs)) if is_ducked[i]]
    rest = ["[s%d]" % i for i in range(len(srcs)) if not (is_key[i] or is_ducked[i])]
    final = list(rest)
    if ducked and key:
        chains.append(_mix(key, "keymix"))
        chains.append("[keymix]apad=whole_dur=%.6f,asplit=2[key][side]" % D)
        chains.append(_mix(ducked, "music"))
        chains.append("[music]apad=whole_dur=%.6f[musicp]" % D)
        chains.append("[musicp][side]sidechaincompress=threshold=0.03:ratio=8:attack=15:release=400:knee=3:"
                      "makeup=1[ducked]")
        final += ["[key]", "[ducked]"]
    else:
        final += key + ducked
    chains.append(_mix(final, "sum"))
    chains.append("[sum]apad=whole_dur=%.6f,atrim=end=%.6f[mix]" % (D, D))
    return args, ";".join(chains)


def _loudness(ffmpeg, path):
    """loudnorm's first-pass measurement of a file, or None when it is silent / unreadable."""
    r = subprocess.run([ffmpeg, "-hide_banner", "-nostats", "-i", path, "-af",
                        "loudnorm=I=%g:TP=%g:LRA=%g:print_format=json" % (TARGET_LUFS, TRUE_PEAK, LRA),
                        "-f", "null", "-"], capture_output=True, text=True)
    m = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", r.stderr)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
        if not math.isfinite(float(data["input_i"])) or float(data["input_i"]) < -70:
            return None
        return data
    except (ValueError, KeyError):
        return None


def mixdown(spec, video_duration, out_wav, base_dir=None, workdir=None):
    """Mix every audible source of a spec into a 48 kHz stereo wav at about -14 LUFS.

    Returns out_wav, or None when the spec has nothing audible. The spec
    may be raw (it is normalized here with base_dir) or already normalized.
    """
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise AudioError("ffmpeg is not installed or not on PATH")
    s = timeline.normalize(spec, base_dir)
    D = float(video_duration)
    srcs = sources(s, D)
    if not srcs:
        return None
    own_tmp = workdir is None
    work = tempfile.mkdtemp(prefix="vstudio_audio_") if own_tmp else workdir
    os.makedirs(work, exist_ok=True)
    try:
        pre = os.path.join(work, "mix_pre.wav")
        args, graph = build_graph(srcs, D)
        r = subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-nostdin"] + args +
                           ["-filter_complex", graph, "-map", "[mix]", "-c:a", "pcm_f32le", "-ar", str(SR), pre],
                           capture_output=True, text=True)
        if r.returncode != 0:
            raise AudioError("ffmpeg could not mix the audio: %s" % r.stderr.strip()[-1500:])
        out_wav = os.path.abspath(out_wav)
        os.makedirs(os.path.dirname(out_wav), exist_ok=True)
        m = _loudness(ffmpeg, pre)
        if m:
            af = ("loudnorm=I=%g:TP=%g:LRA=%g:measured_I=%s:measured_TP=%s:measured_LRA=%s:measured_thresh=%s:"
                  "offset=%s:linear=true:print_format=none,aresample=%d,apad=whole_dur=%.6f,atrim=end=%.6f"
                  % (TARGET_LUFS, TRUE_PEAK, LRA, m["input_i"], m["input_tp"], m["input_lra"], m["input_thresh"],
                     m["target_offset"], SR, D, D))
        else:
            af = "anull"
        r = subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-nostdin", "-i", pre, "-af", af,
                            "-c:a", "pcm_s16le", "-ar", str(SR), "-ac", "2", out_wav], capture_output=True, text=True)
        if r.returncode != 0:
            raise AudioError("ffmpeg could not normalize the audio: %s" % r.stderr.strip()[-1500:])
        return out_wav
    finally:
        if own_tmp:
            shutil.rmtree(work, ignore_errors=True)
