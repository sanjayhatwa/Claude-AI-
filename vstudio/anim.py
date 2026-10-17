"""
anim.py - motion for the timeline: easing curves, keyframe interpolation and
the enter/exit presets every layer can use.

    ease("out-back", 0.5)                      -> 1.0877
    value_at([[0, 1.0], [2, 1.2, "out-cubic"]], 1.0, 1.0)
    enter_state("pop", 0.4, layer)             -> {"scale": 1.07, "opacity": 1, ...}
    state_at(layer, local_t, canvas=(W, H), fps=30, frame=i)  -> everything combined

Easings are the standard Robert Penner formulas (the same curves After
Effects, CSS and every motion library use), plus `step` (hold, then jump).

A preset turns a progress value p (0 at the start of the animation, 1 at the
end) into a state: offsets in px (`dx`, `dy`), multipliers (`scale`,
`opacity`), `blur` (Gaussian sigma in px), `rotation` (deg), `reveal`
(fraction of text shown, for typewriter/words), `wipe` (visible window of
the layer's box as fractions `(x0, x1, y0, y1)`), `rgb_split` (px),
`mblur` (horizontal motion-blur length in px) and `glitch` (slice
displacement strength 0..1). Every preset is the identity at its resting end
(enter at p = 1, exit at p = 0), so a layer never jumps when the animation
finishes. Distances scale with the canvas, so presets look the same at
1080x1920 and 1920x1080. Glitch randomness is seeded by layer id and frame
number: the same frame always renders the same way.
"""

import bisect
import math
import zlib

# --------------------------------------------------------------------------- easings

_C1 = 1.70158          # Penner's "back" overshoot constant (10 % overshoot)
_C2 = _C1 * 1.525
_C3 = _C1 + 1
_C4 = 2 * math.pi / 3  # elastic period


def _out_bounce(x):
    n1, d1 = 7.5625, 2.75
    if x < 1 / d1:
        return n1 * x * x
    if x < 2 / d1:
        x -= 1.5 / d1
        return n1 * x * x + 0.75
    if x < 2.5 / d1:
        x -= 2.25 / d1
        return n1 * x * x + 0.9375
    x -= 2.625 / d1
    return n1 * x * x + 0.984375


def _in_out_expo(x):
    if x <= 0:
        return 0.0
    if x >= 1:
        return 1.0
    return 2 ** (20 * x - 10) / 2 if x < 0.5 else (2 - 2 ** (-20 * x + 10)) / 2


def _in_out_back(x):
    if x < 0.5:
        return ((2 * x) ** 2 * ((_C2 + 1) * 2 * x - _C2)) / 2
    return ((2 * x - 2) ** 2 * ((_C2 + 1) * (x * 2 - 2) + _C2) + 2) / 2


def _out_elastic(x):
    if x <= 0:
        return 0.0
    if x >= 1:
        return 1.0
    return 2 ** (-10 * x) * math.sin((x * 10 - 0.75) * _C4) + 1


EASINGS = {
    "linear": lambda x: x,
    "in-quad": lambda x: x * x,
    "out-quad": lambda x: 1 - (1 - x) ** 2,
    "in-out-quad": lambda x: 2 * x * x if x < 0.5 else 1 - (-2 * x + 2) ** 2 / 2,
    "in-cubic": lambda x: x ** 3,
    "out-cubic": lambda x: 1 - (1 - x) ** 3,
    "in-out-cubic": lambda x: 4 * x ** 3 if x < 0.5 else 1 - (-2 * x + 2) ** 3 / 2,
    "in-quart": lambda x: x ** 4,
    "out-quart": lambda x: 1 - (1 - x) ** 4,
    "in-out-quart": lambda x: 8 * x ** 4 if x < 0.5 else 1 - (-2 * x + 2) ** 4 / 2,
    "in-expo": lambda x: 0.0 if x <= 0 else 2 ** (10 * x - 10),
    "out-expo": lambda x: 1.0 if x >= 1 else 1 - 2 ** (-10 * x),
    "in-out-expo": _in_out_expo,
    "out-back": lambda x: 1 + _C3 * (x - 1) ** 3 + _C1 * (x - 1) ** 2,
    "in-back": lambda x: _C3 * x ** 3 - _C1 * x ** 2,
    "in-out-back": _in_out_back,
    "out-elastic": _out_elastic,
    "out-bounce": _out_bounce,
    "step": lambda x: 1.0 if x >= 1 else 0.0,
}


def ease(name, x):
    """Eased value of progress x (clamped to 0..1). None means linear; unknown names raise ValueError."""
    fn = EASINGS.get(name or "linear")
    if fn is None:
        raise ValueError("unknown easing %r (use one of: %s)" % (name, ", ".join(EASINGS)))
    return float(fn(min(1.0, max(0.0, float(x)))))


def back_out(x, s=_C1):
    """out-back with a custom overshoot constant s (s = 1.70158 overshoots by 10 %)."""
    x = min(1.0, max(0.0, x)) - 1
    return 1 + (s + 1) * x ** 3 + s * x ** 2


def back_in(x, s=_C1):
    """in-back with a custom anticipation constant s."""
    x = min(1.0, max(0.0, x))
    return (s + 1) * x ** 3 - s * x ** 2


def spring(x, damping=6.0, cycles=2.2):
    """Damped spring from 0 to 1 (gentler than out-elastic); exactly 1 at x = 1."""
    x = min(1.0, max(0.0, x))
    if x >= 1:
        return 1.0
    return 1 - math.exp(-damping * x) * math.cos(2 * math.pi * cycles * x) * (1 - x ** 8)


def _clamp01(x):
    return min(1.0, max(0.0, x))


# --------------------------------------------------------------------------- keyframes

def value_at(keys, t, default):
    """Interpolated value of a keyframe list [[t, v], [t, v, "easing"], ...] at time t.

    The easing on a keyframe shapes the segment arriving at it. Before the
    first key the first value holds, after the last key the last value.
    Keys must be sorted by time (timeline.normalize sorts them).
    """
    if not keys:
        return default
    if t <= keys[0][0]:
        return float(keys[0][1])
    if t >= keys[-1][0]:
        return float(keys[-1][1])
    i = bisect.bisect_right([k[0] for k in keys], t) - 1
    k0, k1 = keys[i], keys[i + 1]
    span = k1[0] - k0[0]
    if span <= 0:
        return float(k1[1])
    e = k1[2] if len(k1) > 2 and k1[2] else "linear"
    return float(k0[1]) + (float(k1[1]) - float(k0[1])) * ease(e, (t - k0[0]) / span)


# --------------------------------------------------------------------------- presets

ENTER_TYPES = ["none", "fade", "pop", "slide-up", "slide-down", "slide-left", "slide-right",
               "zoom-in", "zoom-out", "drop", "bounce", "stamp", "blur-in", "wipe-right", "wipe-left",
               "wipe-up", "typewriter", "words", "crossfade", "whip-left", "whip-right", "glitch"]
EXIT_TYPES = ["none", "fade", "pop-out", "slide-up", "slide-down", "slide-left", "slide-right",
              "zoom-in", "zoom-out", "blur-out", "wipe-right", "wipe-left", "whip-left", "whip-right",
              "glitch"]

# Default durations (s) when a spec gives none. Typewriter and words scale with the text.
_ENTER_DUR = {"pop": 0.35, "slide-up": 0.35, "slide-down": 0.35, "slide-left": 0.35, "slide-right": 0.35,
              "zoom-in": 0.35, "zoom-out": 0.35, "drop": 0.55, "bounce": 0.6, "stamp": 0.3,
              "blur-in": 0.4, "wipe-right": 0.4, "wipe-left": 0.4, "wipe-up": 0.4, "crossfade": 0.4,
              "whip-left": 0.25, "whip-right": 0.25, "glitch": 0.3}
_EXIT_DUR = {"pop-out": 0.25, "blur-out": 0.3, "whip-left": 0.22, "whip-right": 0.22, "glitch": 0.25}

POP_OVERSHOOT_S = 2.2525  # back constant that makes 0.5 -> 1.0 peak at 1.08


def default_duration(kind, layer=None, which="enter"):
    """Tasteful default duration for an enter/exit preset (typewriter/words scale with the text)."""
    if which == "enter" and kind in ("typewriter", "words"):
        text = str((layer or {}).get("text") or "")
        if kind == "typewriter":
            return round(min(2.5, max(0.3, 0.035 * len(text.replace(" ", "")))), 3)
        return round(min(3.0, max(0.3, 0.16 * len(text.split()))), 3)
    table = _ENTER_DUR if which == "enter" else _EXIT_DUR
    return table.get(kind, 0.3 if which == "enter" else 0.25)


IDENTITY = {"dx": 0.0, "dy": 0.0, "scale": 1.0, "opacity": 1.0, "blur": 0.0, "reveal": 1.0,
            "wipe": None, "rgb_split": 0.0, "rotation": 0.0, "mblur": 0.0, "glitch": 0.0}


def _state(**kw):
    s = dict(IDENTITY)
    s.update(kw)
    return s


def rng_unit(*parts):
    """Deterministic pseudo-random float in [0, 1) from any hashable parts (stable across runs)."""
    h = zlib.crc32(("|".join(str(p) for p in parts)).encode("utf-8"))
    # one round of an integer mixer so neighbouring frames are uncorrelated
    h = (h ^ (h >> 16)) * 0x45D9F3B & 0xFFFFFFFF
    h = (h ^ (h >> 16)) * 0x45D9F3B & 0xFFFFFFFF
    return ((h ^ (h >> 16)) & 0xFFFFFF) / float(0x1000000)


def _distance(layer, which, axis, canvas, frac):
    spec = (layer or {}).get(which) or {}
    if isinstance(spec, dict) and spec.get("distance") is not None:
        return float(spec["distance"])
    w, h = canvas
    return frac * (w if axis == "x" else h)


def _duration(layer, which):
    spec = (layer or {}).get(which) or {}
    d = spec.get("duration") if isinstance(spec, dict) else None
    return float(d) if d else default_duration(spec.get("type") if isinstance(spec, dict) else None, layer, which)


def _whip_blur(dist, curve, p, layer, which, fps):
    """Motion-blur length (px) = speed x shutter (180 degrees = half a frame)."""
    h = 1e-3
    a, b = max(0.0, p - h), min(1.0, p + h)
    speed = abs(dist) * abs(curve(b) - curve(a)) / max(b - a, 1e-9) / max(_duration(layer, which), 1e-3)
    return speed * 0.5 / max(fps, 1)


def _glitch_state(intensity, layer, frame, canvas, salt):
    """RGB split + jitter + flicker, deterministic per (layer id, frame)."""
    w, _ = canvas
    lid = (layer or {}).get("id") or "layer"
    r1, r2, r3 = (rng_unit(lid, salt, frame, k) for k in range(3))
    split = intensity * 0.014 * w * (0.5 + 0.5 * r1)
    jitter = intensity * 0.025 * w * (r2 * 2 - 1) if r3 > 0.45 else 0.0
    return split, jitter


def enter_state(kind, p, layer=None, canvas=(1080, 1920), fps=30, frame=0):
    """State of an enter preset at progress p (0 = start, 1 = settled)."""
    p = _clamp01(p)
    w, h = canvas
    if kind in (None, "none") or p >= 1:
        return _state()
    if kind == "fade":
        return _state(opacity=ease("out-quad", p))
    if kind == "pop":
        return _state(scale=0.5 + 0.5 * back_out(p, POP_OVERSHOOT_S), opacity=ease("out-quad", _clamp01(p / 0.35)))
    if kind in ("slide-up", "slide-down", "slide-left", "slide-right"):
        e = ease("out-quart", p)
        fade = ease("out-quad", _clamp01(p / 0.6))
        if kind in ("slide-up", "slide-down"):
            d = _distance(layer, "enter", "y", canvas, 0.06)
            return _state(dy=(d if kind == "slide-up" else -d) * (1 - e), opacity=fade)
        d = _distance(layer, "enter", "x", canvas, 0.08)
        return _state(dx=(d if kind == "slide-left" else -d) * (1 - e), opacity=fade)
    if kind == "zoom-in":
        return _state(scale=0.55 + 0.45 * ease("out-expo", p), opacity=ease("out-quad", _clamp01(p / 0.5)))
    if kind == "zoom-out":
        e = ease("out-quart", p)
        return _state(scale=1.5 - 0.5 * e, opacity=ease("out-quad", _clamp01(p / 0.5)), blur=6 * (1 - e))
    if kind == "drop":
        d = _distance(layer, "enter", "y", canvas, 0.12)
        return _state(dy=-d * (1 - ease("out-bounce", p)), opacity=_clamp01(p / 0.15))
    if kind == "bounce":
        return _state(scale=0.6 + 0.4 * spring(p), opacity=ease("out-quad", _clamp01(p / 0.25)))
    if kind == "stamp":
        impact = 0.55
        if p < impact:
            q = ease("in-quad", p / impact)
            return _state(scale=2.2 - 1.2 * q, rotation=-7 * (1 - q), opacity=ease("out-quad", _clamp01(p / (impact * 0.5))))
        q = (p - impact) / (1 - impact)
        squash = 1 - 0.035 * math.sin(math.pi * q) * (1 - q)
        # a two-frame shake right at the impact
        k = int((p - impact) * _duration(layer, "enter") * fps)
        dx = dy = 0.0
        if k < 2:
            amp = 0.006 * min(w, h)
            sgn = 1 if rng_unit((layer or {}).get("id"), "stamp") > 0.5 else -1
            dx, dy = (amp * sgn, -amp * 0.7) if k == 0 else (-amp * sgn * 0.6, amp * 0.5)
        return _state(scale=squash, dx=dx, dy=dy)
    if kind == "blur-in":
        e = ease("out-cubic", p)
        return _state(blur=18 * (1 - e), opacity=ease("out-quad", p), scale=1.06 - 0.06 * e)
    if kind in ("wipe-right", "wipe-left", "wipe-up"):
        e = ease("out-quart", p)
        win = {"wipe-right": (0.0, e, 0.0, 1.0), "wipe-left": (1 - e, 1.0, 0.0, 1.0),
               "wipe-up": (0.0, 1.0, 1 - e, 1.0)}[kind]
        return _state(wipe=win)
    if kind in ("typewriter", "words"):
        return _state(reveal=p)
    if kind == "crossfade":
        return _state(opacity=ease("in-out-quad", p))
    if kind in ("whip-left", "whip-right"):
        d = _distance(layer, "enter", "x", canvas, 0.45) * (1 if kind == "whip-left" else -1)
        curve = EASINGS["out-expo"]
        return _state(dx=d * (1 - ease("out-expo", p)), opacity=_clamp01(p / 0.2),
                      mblur=min(0.25 * w, _whip_blur(d, curve, p, layer, "enter", fps)))
    if kind == "glitch":
        inten = 1 - ease("out-quad", p)
        split, jitter = _glitch_state(inten, layer, frame, canvas, "enter")
        flicker = 1.0 if p > 0.5 or rng_unit((layer or {}).get("id"), "flick", frame) > 0.3 else 0.25
        return _state(rgb_split=split, dx=jitter, glitch=inten, opacity=flicker * _clamp01(p / 0.1 + 0.4))
    raise ValueError("unknown enter type %r" % kind)


def exit_state(kind, p, layer=None, canvas=(1080, 1920), fps=30, frame=0):
    """State of an exit preset at progress p (0 = still resting, 1 = gone)."""
    p = _clamp01(p)
    w, h = canvas
    if kind in (None, "none") or p <= 0:
        return _state()
    if kind == "fade":
        return _state(opacity=1 - ease("in-quad", p))
    if kind == "pop-out":
        return _state(scale=1 - 0.6 * back_in(p, 1.2), opacity=1 - ease("in-quad", _clamp01((p - 0.4) / 0.6)))
    if kind in ("slide-up", "slide-down", "slide-left", "slide-right"):
        e = ease("in-quart", p)
        fade = 1 - ease("in-quad", _clamp01((p - 0.3) / 0.7))
        if kind in ("slide-up", "slide-down"):
            d = _distance(layer, "exit", "y", canvas, 0.06)
            return _state(dy=(-d if kind == "slide-up" else d) * e, opacity=fade)
        d = _distance(layer, "exit", "x", canvas, 0.08)
        return _state(dx=(-d if kind == "slide-left" else d) * e, opacity=fade)
    if kind == "zoom-in":
        e = ease("in-cubic", p)
        return _state(scale=1 + 0.5 * e, opacity=1 - ease("in-quad", p), blur=6 * e)
    if kind == "zoom-out":
        return _state(scale=1 - 0.45 * ease("in-cubic", p), opacity=1 - ease("in-quad", p))
    if kind == "blur-out":
        e = ease("in-cubic", p)
        return _state(blur=18 * e, opacity=1 - ease("in-quad", p), scale=1 + 0.06 * e)
    if kind in ("wipe-right", "wipe-left"):
        e = ease("in-quart", p)
        return _state(wipe=(e, 1.0, 0.0, 1.0) if kind == "wipe-right" else (0.0, 1 - e, 0.0, 1.0))
    if kind in ("whip-left", "whip-right"):
        d = _distance(layer, "exit", "x", canvas, 0.45) * (-1 if kind == "whip-left" else 1)
        curve = EASINGS["in-expo"]
        return _state(dx=d * ease("in-expo", p), opacity=1 - _clamp01((p - 0.8) / 0.2),
                      mblur=min(0.25 * w, _whip_blur(d, curve, p, layer, "exit", fps)))
    if kind == "glitch":
        inten = ease("in-quad", p)
        split, jitter = _glitch_state(inten, layer, frame, canvas, "exit")
        flicker = 1.0 if p < 0.5 or rng_unit((layer or {}).get("id"), "flick-x", frame) > 0.35 else 0.2
        return _state(rgb_split=split, dx=jitter, glitch=inten, opacity=flicker * (1 - _clamp01((p - 0.85) / 0.15)))
    raise ValueError("unknown exit type %r" % kind)


def combine(a, b):
    """Merge two states: offsets add, multipliers multiply, reveal takes the min, wipes intersect."""
    out = dict(a)
    for k in ("dx", "dy", "blur", "rotation", "rgb_split", "mblur"):
        out[k] = a[k] + b[k]
    out["scale"] = a["scale"] * b["scale"]
    out["opacity"] = a["opacity"] * b["opacity"]
    out["reveal"] = min(a["reveal"], b["reveal"])
    out["glitch"] = max(a["glitch"], b["glitch"])
    if a["wipe"] is None or b["wipe"] is None:
        out["wipe"] = a["wipe"] or b["wipe"]
    else:
        out["wipe"] = (max(a["wipe"][0], b["wipe"][0]), min(a["wipe"][1], b["wipe"][1]),
                       max(a["wipe"][2], b["wipe"][2]), min(a["wipe"][3], b["wipe"][3]))
    return out


def phase(layer, lt):
    """(enter_progress or None, exit_progress or None) of a layer at layer-relative time lt."""
    enter_p = exit_p = None
    en = layer.get("enter")
    if isinstance(en, dict) and en.get("type") not in (None, "none"):
        d = float(en.get("duration") or default_duration(en["type"], layer, "enter"))
        enter_p = _clamp01((lt - float(en.get("delay") or 0)) / max(d, 1e-6))
    ex = layer.get("exit")
    if isinstance(ex, dict) and ex.get("type") not in (None, "none"):
        d = float(ex.get("duration") or default_duration(ex["type"], layer, "exit"))
        life = float(layer["end"]) - float(layer["start"])
        exit_p = _clamp01((lt - (life - d)) / max(d, 1e-6))
    return enter_p, exit_p


def state_at(layer, lt, canvas=(1080, 1920), fps=30, frame=0):
    """Everything about a (normalized) layer's motion at layer-relative time lt.

    Returns IDENTITY's keys plus absolute `x`, `y`, `scale`, `opacity`,
    `rotation`, `blur`: `animate` keyframes (defaulting to the layer's static
    values) combined with the enter and exit presets.
    """
    an = layer.get("animate") or {}
    base = {
        "x": value_at(an.get("x"), lt, float(layer.get("x", 0))),
        "y": value_at(an.get("y"), lt, float(layer.get("y", 0))),
        "scale": value_at(an.get("scale"), lt, float(layer.get("scale", 1))),
        "opacity": value_at(an.get("opacity"), lt, float(layer.get("opacity", 1))),
        "rotation": value_at(an.get("rotation"), lt, float(layer.get("rotation", 0))),
        "blur": value_at(an.get("blur"), lt, 0.0),
    }
    st = _state()
    enter_p, exit_p = phase(layer, lt)
    if enter_p is not None:
        st = combine(st, enter_state(layer["enter"]["type"], enter_p, layer, canvas, fps, frame))
    if exit_p is not None:
        st = combine(st, exit_state(layer["exit"]["type"], exit_p, layer, canvas, fps, frame))
    st["x"] = base["x"] + st["dx"]
    st["y"] = base["y"] + st["dy"]
    st["scale"] = base["scale"] * st["scale"]
    st["opacity"] = max(0.0, min(1.0, base["opacity"] * st["opacity"]))
    st["rotation"] = base["rotation"] + st["rotation"]
    st["blur"] = max(0.0, base["blur"] + st["blur"])
    st["moving"] = bool(st["dx"] or st["dy"] or animating(an.get("x"), lt) or animating(an.get("y"), lt))
    return st


def animating(keys, t):
    """True while a keyframe list is between its first and last key (the value is changing)."""
    return bool(keys) and len(keys) > 1 and keys[0][0] <= t <= keys[-1][0]
