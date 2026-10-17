"""
sfx.py - the sound-design kit: twelve effects synthesized from scratch with
numpy, so every render can use them without copyrighted samples.

    python -m vstudio sfx list
    python -m vstudio sfx render whoosh --out whoosh.wav
    python -m vstudio sfx render all --out sfx_folder/

    make("whoosh") -> path to a cached 48 kHz stereo wav (workspace/cache/sfx)

How each one is made:
    whoosh, swoosh   noise through a band-pass filter whose centre sweeps up
                     and back down, with a swell envelope and a stereo pan
    pop, click, tick pitched blips (fast pitch drop / inharmonic partials)
                     with short exponential envelopes and a noise transient
    hit, boom        a sine "kick" with a pitch drop plus filtered noise,
                     gently saturated; boom adds a long sub and rumble
    riser            band-passed noise sweeping up plus a rising, detuned
                     tone and a speeding tremolo; ends at its loudest
    ding             a small bell: inharmonic partials with their own decays
    glitch           bit-crushed, sample-and-held bursts of square waves and
                     noise, gated in a stutter
    typing           keystrokes: filtered noise clicks with a little thump,
                     at uneven human intervals
    camera           a shutter: two mechanical clicks with the mirror whir

Every effect is deterministic (fixed seeds), DC-free, faded at the edges and
peak-normalized to about -3 dBFS.
"""

import argparse
import math
import os
import shutil
import sys
import wave

import numpy as np

from . import paths

NAMES = ["whoosh", "swoosh", "pop", "click", "tick", "hit", "boom", "riser", "ding", "glitch", "typing", "camera"]
VERSION = 1           # bump when a sound changes so old cache files are not reused
PEAK = 10 ** (-3 / 20)  # -3 dBFS

# lengths in seconds, fixed by the synthesis below (a test keeps them in step)
LENGTHS = {"whoosh": 0.75, "swoosh": 0.4, "pop": 0.14, "click": 0.05, "tick": 0.04, "hit": 0.7, "boom": 2.2,
           "riser": 2.0, "ding": 1.4, "glitch": 0.4, "typing": 1.2, "camera": 0.45}

DESCRIPTIONS = {
    "whoosh": "airy pass-by for transitions and slides (0.75 s)",
    "swoosh": "short bright swipe for whips and quick moves (0.4 s)",
    "pop": "bubbly pop for pop-in text and stickers (0.14 s)",
    "click": "clean UI click for buttons, checks and cursors (0.05 s)",
    "tick": "wood tick for counters, lists and timers (0.04 s)",
    "hit": "punchy impact for stamps, cuts and reveals (0.7 s)",
    "boom": "deep cinematic boom for big reveals (2.2 s)",
    "riser": "tension build that ends on the drop (2 s)",
    "ding": "bright bell for correct answers and highlights (1.4 s)",
    "glitch": "digital stutter for glitch transitions (0.4 s)",
    "typing": "keyboard typing for typewriter text (1.2 s)",
    "camera": "camera shutter for photo moments and freeze frames (0.45 s)",
}


# --------------------------------------------------------------------------- DSP helpers

def _t(dur, sr):
    return np.arange(int(round(dur * sr))) / float(sr)


def _filter(x, sr, lo=None, hi=None, order=2):
    """Static Butterworth-shaped band filter applied in the frequency domain (zero phase)."""
    pad = int(0.05 * sr)
    n = len(x) + 2 * pad
    spec = np.fft.rfft(np.pad(x, pad), n)
    f = np.fft.rfftfreq(n, 1.0 / sr)
    f[0] = 1e-6
    gain = np.ones_like(f)
    if lo:
        gain /= np.sqrt(1 + (lo / f) ** (2 * order))
    if hi:
        gain /= np.sqrt(1 + (f / hi) ** (2 * order))
    return np.fft.irfft(spec * gain, n)[pad:pad + len(x)]


def _sweep_bandpass(x, fc, q, sr):
    """Band-pass with a moving centre frequency (per-sample array): a TPT state-variable filter."""
    g = np.tan(np.pi * np.clip(fc, 20, sr * 0.45) / sr)
    k = 1.0 / q
    a1 = 1.0 / (1.0 + g * (g + k))
    a2 = g * a1
    a3 = g * a2
    out = np.empty_like(x)
    ic1 = ic2 = 0.0
    for i in range(len(x)):
        v3 = x[i] - ic2
        v1 = a1[i] * ic1 + a2[i] * v3
        v2 = ic2 + a2[i] * ic1 + a3[i] * v3
        ic1 = 2 * v1 - ic1
        ic2 = 2 * v2 - ic2
        out[i] = v1 * k  # unity gain at the centre frequency
    return out


def _pink(n, rng):
    """Pink-ish noise (-3 dB/octave): white noise shaped in the frequency domain."""
    spec = np.fft.rfft(rng.standard_normal(n))
    f = np.arange(len(spec), dtype=float)
    f[0] = 1
    return np.fft.irfft(spec / np.sqrt(f), n)


def _log_sweep(t, points):
    """Frequency curve through (time, Hz) points, interpolated on a log scale with smoothstep."""
    out = np.empty_like(t)
    out[:] = points[0][1]
    for (t0, f0), (t1, f1) in zip(points, points[1:]):
        m = (t >= t0) & (t <= t1)
        u = (t[m] - t0) / max(t1 - t0, 1e-9)
        u = u * u * (3 - 2 * u)
        out[m] = np.exp(np.log(f0) + (np.log(f1) - np.log(f0)) * u)
    out[t > points[-1][0]] = points[-1][1]
    return out


def _phase(freq, sr):
    return 2 * np.pi * np.cumsum(freq) / sr


def _decay(t, tau, attack=0.001):
    return (1 - np.exp(-t / max(attack, 1e-5))) * np.exp(-t / tau)


def _pan(mono, pan):
    """Equal-power pan: pan is -1 (left) .. +1 (right), scalar or per sample."""
    ang = (np.asarray(pan) + 1) * np.pi / 4
    return np.stack([mono * np.cos(ang), mono * np.sin(ang)], axis=1)


def _place(buf, sound, at, sr):
    i = int(round(at * sr))
    j = min(len(buf), i + len(sound))
    if i < len(buf):
        buf[i:j] += sound[:j - i]


def _sat(x, drive):
    return np.tanh(drive * x) / np.tanh(drive)


# --------------------------------------------------------------------------- the sounds

def _whoosh(sr, dur=0.75, up=(260, 1500, 600), peak=0.55, q=1.1, pan=(-0.6, 0.6), seed=11):
    rng = np.random.default_rng(seed)
    t = _t(dur, sr)
    n = len(t)
    fc = _log_sweep(t, [(0, up[0]), (peak * dur, up[1]), (dur, up[2])])
    body = _sweep_bandpass(_pink(n, rng), fc, q, sr)
    air = _sweep_bandpass(_pink(n, rng), fc * 2.5, q * 0.8, sr) * 0.35
    u = np.clip(t / (peak * dur), 0, 1)
    v = np.clip((t - peak * dur) / ((1 - peak) * dur), 0, 1)
    env = np.where(t < peak * dur, np.sin(np.pi / 2 * u) ** 2.5, (1 - v) ** 2)
    return _pan((body + air) * env, np.linspace(pan[0], pan[1], n))


def _pop(sr):
    t = _t(0.14, sr)
    f = 230 + 820 * np.exp(-t / 0.011)
    tone = np.sin(_phase(f, sr)) + 0.25 * np.sin(2 * _phase(f, sr))
    body = tone * _decay(t, 0.032, 0.0015)
    click = _filter(np.random.default_rng(3).standard_normal(len(t)), sr, lo=2500) * _decay(t, 0.002, 0.0002) * 0.25
    return _pan(_sat(body + click, 1.6), 0.0)


def _click(sr):
    t = _t(0.05, sr)
    tone = np.sin(2 * np.pi * 2900 * t) * _decay(t, 0.0045, 0.0002)
    tone += 0.5 * np.sin(2 * np.pi * 1450 * t) * _decay(t, 0.008, 0.0003)
    noise = _filter(np.random.default_rng(5).standard_normal(len(t)), sr, lo=3000, hi=9000) * _decay(t, 0.0015, 0.0001)
    return _pan(tone + 0.5 * noise, 0.0)


def _tick(sr):
    t = _t(0.04, sr)
    s = np.sin(2 * np.pi * 1900 * t) * _decay(t, 0.012, 0.0002)
    s += 0.45 * np.sin(2 * np.pi * 5230 * t) * _decay(t, 0.004, 0.0002)
    s += 0.2 * _filter(np.random.default_rng(7).standard_normal(len(t)), sr, lo=4000) * _decay(t, 0.001, 0.0001)
    return _pan(s, 0.0)


def _hit(sr):
    rng = np.random.default_rng(13)
    t = _t(0.7, sr)
    f = 46 + 115 * np.exp(-t / 0.035)
    kick = np.sin(_phase(f, sr)) * _decay(t, 0.22, 0.0008)
    crack = _filter(rng.standard_normal(len(t)), sr, lo=200, hi=2600) * _decay(t, 0.028, 0.0005) * 0.55
    thwack = _filter(rng.standard_normal(len(t)), sr, lo=700, hi=1800, order=3) * _decay(t, 0.06, 0.001) * 0.25
    mono = _sat(kick + crack + thwack, 2.2)
    return _pan(mono, 0.0)


def _boom(sr):
    rng = np.random.default_rng(17)
    t = _t(2.2, sr)
    f = 36 + 68 * np.exp(-t / 0.09)
    sub = np.sin(_phase(f, sr)) * _decay(t, 0.75, 0.002)
    impact = _filter(rng.standard_normal(len(t)), sr, hi=1500) * _decay(t, 0.05, 0.0008) * 0.5
    left = _filter(rng.standard_normal(len(t)), sr, lo=25, hi=170) * _decay(t, 0.9, 0.01) * 1.6
    right = _filter(rng.standard_normal(len(t)), sr, lo=25, hi=170) * _decay(t, 0.9, 0.01) * 1.6
    mono = sub + impact
    st = np.stack([mono + left, mono + right], axis=1)
    return _sat(st / max(np.abs(st).max(), 1e-9), 1.5)


def _riser(sr):
    rng = np.random.default_rng(19)
    dur = 2.0
    t = _t(dur, sr)
    u = t / dur
    fc = 300 * (9000 / 300) ** (u ** 1.3)
    noise = _sweep_bandpass(rng.standard_normal(len(t)), fc, 2.5, sr) * u ** 2.2
    f = 220 * 8 ** (u ** 1.5)
    tone_l = np.sin(_phase(f, sr))
    tone_r = np.sin(_phase(f * 1.004, sr))
    trem = 1 - 0.3 * (0.5 + 0.5 * np.sin(_phase(4 + 12 * u ** 2, sr)))
    tone_env = u ** 2.5 * 0.35 * trem
    return np.stack([noise + tone_l * tone_env, noise + tone_r * tone_env], axis=1)


def _ding(sr):
    t = _t(1.4, sr)
    f0 = 1318.5
    ratios = [1.0, 2.0, 2.76, 3.0, 5.4]
    amps = [1.0, 0.4, 0.25, 0.15, 0.08]
    taus = [0.9, 0.5, 0.35, 0.25, 0.12]
    bell = sum(a * np.sin(2 * np.pi * f0 * r * t) * _decay(t, tau, 0.002) for r, a, tau in zip(ratios, amps, taus))
    return _pan(bell, 0.0)  # mono: phone speakers sum to mono, a detuned stereo bell would cancel


def _glitch(sr):
    rng = np.random.default_rng(23)
    dur = 0.4
    out = np.zeros((int(dur * sr), 2))
    at = 0.0
    while at < dur - 0.02:
        seg = rng.uniform(0.02, 0.07)
        t = _t(seg, sr)
        if rng.random() < 0.55:
            src = np.sign(np.sin(2 * np.pi * rng.uniform(80, 2000) * t))
        else:
            src = rng.standard_normal(len(t)) * 0.7
        hold = int(rng.integers(4, 17))          # sample-and-hold = crude rate reduction
        src = np.repeat(src[::hold], hold)[:len(t)]
        bits = int(rng.integers(3, 7))            # bit crush
        src = np.round(src * 2 ** (bits - 1)) / 2 ** (bits - 1)
        fade = np.minimum(1, np.minimum(t, seg - t) / 0.001)
        src = src * fade * rng.uniform(0.5, 1.0)
        pan = rng.uniform(-0.7, 0.7)
        _place(out, _pan(src, pan), at, sr)
        at += seg + (rng.uniform(0.005, 0.015) if rng.random() < 0.6 else 0)
    return out


def _key(sr, rng, strength=1.0, space=False):
    t = _t(0.06, sr)
    lo, hi = (1200, 3500) if space else (2500, 6000)
    click = _filter(rng.standard_normal(len(t)), sr, lo=lo, hi=hi) * _decay(t, 0.009 if space else 0.006, 0.0003)
    thump = np.sin(2 * np.pi * (120 if space else 190) * t) * _decay(t, 0.02 if space else 0.014, 0.0005) * 0.35
    return (click + thump) * strength


def _typing(sr):
    rng = np.random.default_rng(29)
    dur = 1.2
    out = np.zeros((int(dur * sr), 2))
    at, n = 0.01, 0
    while at < dur - 0.08:
        space = n % 5 == 4
        k = _key(sr, rng, rng.uniform(0.7, 1.0), space)
        _place(out, _pan(k, rng.uniform(-0.25, 0.25)), at, sr)
        _place(out, _pan(_key(sr, rng, 0.25, space), 0.0), at + rng.uniform(0.035, 0.05), sr)  # key release
        at += rng.uniform(0.07, 0.14)
        n += 1
    return out


def _camera(sr):
    rng = np.random.default_rng(31)
    out = np.zeros((int(0.45 * sr), 2))
    t = _t(0.12, sr)
    for at, gain in ((0.0, 1.0), (0.13, 0.8)):
        sharp = _filter(rng.standard_normal(len(t)), sr, lo=2500, hi=7000) * _decay(t, 0.004, 0.0002)
        clack = _filter(rng.standard_normal(len(t)), sr, lo=600, hi=1400) * _decay(t, 0.015, 0.0005) * 0.7
        thunk = np.sin(2 * np.pi * 125 * t) * _decay(t, 0.025, 0.001) * 0.4
        _place(out, _pan((sharp + clack + thunk) * gain, 0.0), at, sr)
    whir_t = _t(0.1, sr)
    whir = _filter(rng.standard_normal(len(whir_t)), sr, lo=1300, hi=2400) * np.sin(np.pi * whir_t / 0.1) ** 2 * 0.15
    _place(out, _pan(whir, 0.0), 0.02, sr)
    return out


_SYNTHS = {
    "whoosh": lambda sr: _whoosh(sr),
    "swoosh": lambda sr: _whoosh(sr, dur=0.4, up=(600, 3600, 1600), peak=0.6, q=1.4, pan=(0.4, -0.4), seed=12),
    "pop": _pop, "click": _click, "tick": _tick, "hit": _hit, "boom": _boom, "riser": _riser,
    "ding": _ding, "glitch": _glitch, "typing": _typing, "camera": _camera,
}


# --------------------------------------------------------------------------- output

def synth(name, sr=48000):
    """The effect as a float array of shape (samples, 2), peak-normalized to -3 dBFS."""
    if name not in _SYNTHS:
        raise ValueError("unknown sfx %r (use one of: %s)" % (name, ", ".join(NAMES)))
    x = np.asarray(_SYNTHS[name](sr), dtype=np.float64)
    if x.ndim == 1:
        x = _pan(x, 0.0)
    x = x - x.mean(axis=0)  # DC
    n = len(x)
    fade_in = min(n // 4, max(1, int(0.0005 * sr)))
    fade_out = min(n // 4, max(1, int(0.006 * sr)))
    x[:fade_in] *= np.linspace(0, 1, fade_in)[:, None]
    x[n - fade_out:] *= np.linspace(1, 0, fade_out)[:, None]
    return x * (PEAK / max(np.abs(x).max(), 1e-9))


def write_wav(path, x, sr):
    """Write a (samples, channels) float array in -1..1 as 16-bit PCM."""
    pcm = np.clip(np.round(x * 32767), -32768, 32767).astype("<i2")
    tmp = path + ".tmp%d" % os.getpid()
    with wave.open(tmp, "wb") as w:
        w.setnchannels(pcm.shape[1])
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())
    os.replace(tmp, path)  # atomic, so parallel renders never read half a file
    return path


def make(name, sr=48000):
    """Path to the cached wav of an effect (created on first use under workspace/cache/sfx)."""
    folder = paths.ensure(os.path.join(paths.CACHE, "sfx"))
    path = os.path.join(folder, "%s_%d_v%d.wav" % (name, sr, VERSION))
    if not os.path.exists(path):
        write_wav(path, synth(name, sr), sr)
    return path


def duration(name):
    """Length of an effect in seconds (without synthesizing it)."""
    if name not in LENGTHS:
        raise ValueError("unknown sfx %r (use one of: %s)" % (name, ", ".join(NAMES)))
    return LENGTHS[name]


def main(argv=None):
    ap = argparse.ArgumentParser(prog="vstudio sfx", description="List or render the built-in sound effects.")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("list", help="names, lengths and what each is for")
    r = sub.add_parser("render", help="write an effect (or all of them) as a wav")
    r.add_argument("name", help="effect name, or 'all'")
    r.add_argument("--out", required=True, help="output .wav (a folder for 'all')")
    r.add_argument("--sr", type=int, default=48000)
    a = ap.parse_args(argv)
    if a.cmd in (None, "list"):
        for name in NAMES:
            print("  %-8s %s" % (name, DESCRIPTIONS[name]))
        return 0
    names = NAMES if a.name == "all" else [a.name]
    if a.name != "all" and a.name not in NAMES:
        print("unknown sfx %r; run `vstudio sfx list`" % a.name, file=sys.stderr)
        return 2
    for name in names:
        out = os.path.join(a.out, name + ".wav") if a.name == "all" else a.out
        os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
        shutil.copyfile(make(name, a.sr), out)
        print("wrote %s (%.2f s)" % (out, duration(name)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
