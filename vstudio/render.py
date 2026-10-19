"""
render.py - the frame renderer: a TIMELINE SPEC (JSON) in, an MP4 out.

    python -m vstudio render spec.json --out reel.mp4
    python -m vstudio render spec.json --out draft.mp4 --preview      # half size, half fps
    python -m vstudio render spec.json --still 1.5 --out frame.png

    render(spec, "reel.mp4", progress=lambda done, total: ...)
    render_frame(spec, 2.0)  -> HxWx3 uint8 (no captions)

How a frame is made (deterministic: the same spec and time give the same
pixels):
  1. background colour or gradient (dithered so it does not band)
  2. every non-`fixed` layer, in order, through the camera
  3. the `fixed` layers on top (HUD, progress bars)
  4. effects in a fixed pipeline order: shake, zoom-blur, glitch, grade,
     vignette, flash, letterbox, grain
  5. captions, burned by libass during the encode (captions.build_ass)

Compositing is premultiplied float32. Every layer becomes a sprite
(text: textrender; shapes: anti-aliased signed-distance masks; images and
video: fitted with cover/contain/fill around a focus point), gets its
per-layer looks in sprite space (shadow, blur, motion blur, wipe, RGB split,
glitch slices, opacity) and is placed with one affine warp (scale, rotation,
anchor, camera). A layer at rest with no scaling is copied at whole pixels,
so static text stays perfectly crisp; moving layers use sub-pixel warps so
they glide without judder.

The camera (zoom about the canvas centre, then `x`/`y` offsets that move the
picture: +x shifts the scene right, then `rotation`) is folded into each
non-fixed layer's transform instead of resampling the finished composite:
the same picture, one resample fewer, and a zoom-out shows the background
at the edges rather than smeared pixels.

Video layers are decoded by one ffmpeg process each, streaming raw frames
already scaled and cropped, at the timeline fps, from `in` with `speed`
(the frame at timeline t is the source frame nearest to source time
in + (t - start) * speed, on the file's own clock, the one the clip audio
is cut on); a source that runs out holds its last frame, or wraps with
`loop`. HDR footage (HLG, as iPhones record by default, or PQ) is
tone-mapped to BT.709 SDR with zscale + tonemap, and BT.2020 SDR gets its
gamut converted; an ffmpeg without zscale prints a warning instead.

Encoding: raw RGB frames are piped to ffmpeg (libx264, yuv420p BT.709,
crf 18, preset veryfast, +faststart); the audio mix (audio.mixdown) is
muxed as AAC 192k.
"""

import argparse
import collections
import fractions
import math
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time

import cv2
import numpy as np
from PIL import Image, ImageOps

from . import anim, audio, fonts, paths, textrender, timeline

EFFECT_ORDER = ["shake", "zoom-blur", "glitch", "grade", "vignette", "flash", "letterbox", "grain"]


class RenderError(RuntimeError):
    """A render could not be completed (bad spec, missing ffmpeg, encoder failure)."""


def _ffmpeg():
    exe = shutil.which("ffmpeg")
    if not exe:
        raise RenderError("ffmpeg is not installed or not on PATH (https://ffmpeg.org/download.html)")
    return exe


def ffmpeg_filter_path(path):
    """Escape a file path for use as an option value inside an ffmpeg -vf filter graph.

    Two parsers read it: the graph parser (special: \\ ' [ ] , ;) and the
    filter's option parser (special: \\ ' :). Backslashes become forward
    slashes first, which ffmpeg accepts on Windows too (C:/Users/...).
    """
    p = os.path.abspath(path).replace("\\", "/")
    for ch in ("\\", "'", ":"):
        p = p.replace(ch, "\\" + ch)
    for ch in ("\\", "'", "[", "]", ",", ";"):
        p = p.replace(ch, "\\" + ch)
    return p


def _even(v):
    return max(2, int(round(v / 2.0)) * 2)


# --------------------------------------------------------------------------- geometry

def _affine(pos, anchor, k, rot_deg):
    """3x3 matrix: sprite px -> canvas px (scale k and rotation about the anchor, anchor lands on pos)."""
    th = math.radians(rot_deg)
    c, s = math.cos(th) * k, math.sin(th) * k
    ax, ay = anchor
    return np.array([[c, -s, pos[0] - (c * ax - s * ay)],
                     [s, c, pos[1] - (s * ax + c * ay)],
                     [0, 0, 1]], np.float64)


def _camera_matrix(spec, t, S, W, H):
    cam = spec.get("camera") or {}
    z = anim.value_at(cam.get("zoom"), t, 1.0)
    x = anim.value_at(cam.get("x"), t, 0.0) * S
    y = anim.value_at(cam.get("y"), t, 0.0) * S
    r = anim.value_at(cam.get("rotation"), t, 0.0)
    if abs(z - 1) < 1e-9 and abs(x) < 1e-9 and abs(y) < 1e-9 and abs(r) < 1e-9:
        return None
    return _affine((W / 2.0 + x, H / 2.0 + y), (W / 2.0, H / 2.0), z, r)


def _camera_moving(spec, t):
    cam = spec.get("camera") or {}
    return any(anim.animating(cam.get(k), t) for k in ("zoom", "x", "y", "rotation"))


def _cv_affine(M):
    """Continuous-coordinate matrix -> OpenCV's pixel-centre convention (2x3)."""
    A = M[:2, :2]
    t = M[:2, 2] + A @ np.array([0.5, 0.5]) - 0.5
    return np.hstack([A, t[:, None]]).astype(np.float64)


# --------------------------------------------------------------------------- blending

def _needs_inverse(mode):
    """add and screen only use the premultiplied colour; normal and multiply also need 1 - alpha."""
    return mode not in ("add", "screen")


def split_alpha(spr, opacity=1.0, inverse=True):
    """Premultiplied RGBA -> (rgb, 1 - alpha as 3 channels), contiguous, ready for in-place OpenCV blending.

    inverse=False skips the second array (returned as None) for blend modes that do not use it;
    on full-frame sprites that saves two 25 MB temporaries per frame.
    """
    rgb = cv2.cvtColor(spr, cv2.COLOR_RGBA2RGB)
    if opacity < 0.999:
        rgb *= opacity
    if not inverse:
        return rgb, None
    a = cv2.extractChannel(spr, 3)
    if opacity < 0.999:
        a *= opacity
    return rgb, cv2.cvtColor(1.0 - a, cv2.COLOR_GRAY2RGB)


def _blend(region, rgb, inv3, mode):
    """Blend premultiplied rgb with its (1 - alpha) onto an opaque float region, in place (OpenCV).

    inv3 may be None for add and screen, which do not use it.
    """
    if mode == "add":
        cv2.add(region, rgb, dst=region)
    elif mode == "screen":  # s + d - s*d, with a single temporary
        sd = cv2.multiply(region, rgb)
        cv2.subtract(region, sd, dst=region)
        cv2.add(region, rgb, dst=region)
    elif mode == "multiply":  # d * (s + 1 - a)
        cv2.multiply(region, cv2.add(rgb, inv3), dst=region)
    else:
        cv2.multiply(region, inv3, dst=region)
        cv2.add(region, rgb, dst=region)


def _blend_opaque(region, src, opacity, mode):
    """Blend an opaque RGB source at a uniform opacity."""
    if mode == "normal":
        if opacity >= 0.999:
            region[...] = src
        else:
            cv2.addWeighted(region, 1.0 - opacity, src, opacity, 0.0, dst=region)
        return
    rgb = src * opacity if opacity < 0.999 else src
    _blend(region, rgb, np.full_like(rgb, 1.0 - opacity) if mode == "multiply" else None, mode)


def composite(canvas, spr, M, blend="normal", opacity=1.0, crisp=False, split=None):
    """Place a sprite (premultiplied RGBA float32, or opaque RGB) onto an opaque RGB canvas with matrix M.

    crisp: when M is a pure translation, snap to whole pixels (no resampling).
    split: (rgb, inv3) of spr from split_alpha when the caller caches it, or a callable returning it
           (only called when the sprite is placed without resampling, the one case that uses it).
    """
    H, W = canvas.shape[:2]
    h, w = spr.shape[:2]
    if opacity <= 0.002 or h == 0 or w == 0:
        return
    opaque = spr.shape[2] == 3
    A = M[:2, :2]
    is_translate = abs(A[0, 0] - 1) < 1e-6 and abs(A[1, 1] - 1) < 1e-6 and abs(A[0, 1]) < 1e-9 and abs(A[1, 0]) < 1e-9
    tx, ty = M[0, 2], M[1, 2]
    if is_translate and (crisp or (abs(tx - round(tx)) < 0.01 and abs(ty - round(ty)) < 0.01)):
        ox, oy = int(round(tx)), int(round(ty))
        x0, y0, x1, y1 = max(0, ox), max(0, oy), min(W, ox + w), min(H, oy + h)
        if x1 <= x0 or y1 <= y0:
            return
        region = canvas[y0:y1, x0:x1]
        sy, sx = slice(y0 - oy, y1 - oy), slice(x0 - ox, x1 - ox)
        if opaque:
            _blend_opaque(region, spr[sy, sx], opacity, blend)
            return
        if split is None or opacity < 0.999:
            rgb, inv3 = split_alpha(spr[sy, sx], opacity, inverse=_needs_inverse(blend))
            _blend(region, rgb, inv3, blend)
        else:
            rgb, inv3 = split() if callable(split) else split
            _blend(region, rgb[sy, sx], inv3[sy, sx], blend)
        return

    # downscaling a lot: shrink with area averaging first, so the warp does not alias
    k = math.sqrt(abs(np.linalg.det(A)))
    if k < 0.7 and min(h, w) > 4:
        nw, nh = max(1, int(round(w * k))), max(1, int(round(h * k)))
        spr = cv2.resize(spr, (nw, nh), interpolation=cv2.INTER_AREA)
        M = M @ np.diag([w / float(nw), h / float(nh), 1.0])
        h, w = nh, nw
    corners = np.array([[0, 0, 1], [w, 0, 1], [0, h, 1], [w, h, 1]], np.float64).T
    pc = (M @ corners)[:2]
    x0, y0 = max(0, int(math.floor(pc[0].min()))), max(0, int(math.floor(pc[1].min())))
    x1, y1 = min(W, int(math.ceil(pc[0].max()))), min(H, int(math.ceil(pc[1].max())))
    if x1 <= x0 or y1 <= y0:
        return
    Mr = M.copy()
    Mr[0, 2] -= x0
    Mr[1, 2] -= y0
    cvM = _cv_affine(Mr)
    size = (x1 - x0, y1 - y0)
    region = canvas[y0:y1, x0:x1]
    if opaque:
        # does the sprite cover the whole region? then no alpha channel is needed
        inv = np.linalg.inv(Mr)
        rc = np.array([[0, 0, 1], [size[0], 0, 1], [0, size[1], 1], [size[0], size[1], 1]], np.float64).T
        sc = (inv @ rc)[:2]
        if sc[0].min() >= -1e-6 and sc[1].min() >= -1e-6 and sc[0].max() <= w + 1e-6 and sc[1].max() <= h + 1e-6:
            if opacity >= 0.999 and blend == "normal":
                cv2.warpAffine(spr, cvM, size, dst=region, flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
            else:
                warped = cv2.warpAffine(spr, cvM, size, flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
                _blend_opaque(region, warped, opacity, blend)
            return
        spr = _to_rgba(spr)
    warped = cv2.warpAffine(spr, cvM, size, flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT,
                            borderValue=(0, 0, 0, 0))
    rgb, inv3 = split_alpha(warped, opacity, inverse=_needs_inverse(blend))
    _blend(region, rgb, inv3, blend)


# --------------------------------------------------------------------------- per-sprite looks

def _to_rgba(spr):
    return spr if spr.shape[2] == 4 else cv2.cvtColor(spr, cv2.COLOR_RGB2RGBA)  # alpha = 1


def _pad(spr, left, top, right, bottom):
    """Transparent border around a sprite; returns (sprite, left, top) as whole pixels added."""
    left, top, right, bottom = (max(0, int(math.ceil(v))) for v in (left, top, right, bottom))
    if not (left or top or right or bottom):
        return spr, 0, 0
    return cv2.copyMakeBorder(spr, top, bottom, left, right, cv2.BORDER_CONSTANT, value=(0, 0, 0, 0)), left, top


def _apply_looks(spr, box, st, R, layer, frame):
    """Wipe, blur, motion blur, RGB split and glitch slices in sprite space.

    Returns (sprite, box) with the box shifted by any padding added.
    """
    bx0, by0, bx1, by1 = box
    shift_x = shift_y = 0

    wipe = st.get("wipe")
    if wipe is not None:
        spr = _to_rgba(spr).copy()
        h, w = spr.shape[:2]
        bw, bh = max(bx1 - bx0, 1.0), max(by1 - by0, 1.0)
        feather = max(1.0, 0.012 * bw)
        xs = np.arange(w, dtype=np.float32) + 0.5
        ys = np.arange(h, dtype=np.float32) + 0.5
        # the layout box edges count as 0 and 1; beyond the box the wipe extends (shadows, strokes)
        lo_x = bx0 + wipe[0] * bw if wipe[0] > 0 else -1e9
        hi_x = bx0 + wipe[1] * bw if wipe[1] < 1 else 1e9
        lo_y = by0 + wipe[2] * bh if wipe[2] > 0 else -1e9
        hi_y = by0 + wipe[3] * bh if wipe[3] < 1 else 1e9
        mx = np.clip((xs - lo_x) / feather + 0.5, 0, 1) * np.clip((hi_x - xs) / feather + 0.5, 0, 1)
        my = np.clip((ys - lo_y) / feather + 0.5, 0, 1) * np.clip((hi_y - ys) / feather + 0.5, 0, 1)
        spr *= (my[:, None] * mx[None, :])[..., None]

    sigma = st.get("blur", 0) * R
    if sigma > 0.25:
        spr, l, t = _pad(_to_rgba(spr), 3 * sigma, 3 * sigma, 3 * sigma, 3 * sigma)
        spr = cv2.GaussianBlur(spr, (0, 0), sigma)
        shift_x, shift_y = shift_x + l, shift_y + t

    mb = st.get("mblur", 0) * R
    if mb >= 2:
        n = int(round(mb)) | 1
        spr, l, t = _pad(_to_rgba(spr), n / 2, 0, n / 2, 0)
        spr = cv2.blur(spr, (n, 1))
        shift_x += l

    split = st.get("rgb_split", 0) * R
    g = st.get("glitch", 0)
    if split >= 0.5 or g > 0.02:
        spr = _to_rgba(spr)
        d = int(round(split))
        spr, l, t = _pad(spr, d + g * 0.04 * spr.shape[1], 0, d + g * 0.04 * spr.shape[1], 0)
        shift_x += l
        if d:
            out = spr.copy()
            out[:, d:, 0] = spr[:, :-d, 0]
            out[:, :d, 0] = 0
            out[:, :-d, 2] = spr[:, d:, 2]
            out[:, -d:, 2] = 0
            ra = np.zeros_like(spr[..., 3])
            ba = np.zeros_like(spr[..., 3])
            ra[:, d:] = spr[:, :-d, 3]
            ba[:, :-d] = spr[:, d:, 3]
            out[..., 3] = np.maximum(spr[..., 3], np.maximum(ra, ba))
            spr = out
        if g > 0.02:
            spr = spr.copy()
            h, w = spr.shape[:2]
            lid = layer.get("id") or "layer"
            for k in range(5):  # horizontal slices pushed sideways
                if anim.rng_unit(lid, "slice-on", frame, k) > 0.55:
                    continue
                y0 = int(anim.rng_unit(lid, "slice-y", frame, k) * h)
                hh = max(1, int(h * (0.04 + 0.12 * anim.rng_unit(lid, "slice-h", frame, k))))
                dx = int(round((anim.rng_unit(lid, "slice-dx", frame, k) * 2 - 1) * g * 0.04 * w))
                if dx:
                    spr[y0:y0 + hh] = np.roll(spr[y0:y0 + hh], dx, axis=1)
    return spr, (bx0 + shift_x, by0 + shift_y, bx1 + shift_x, by1 + shift_y)


# --------------------------------------------------------------------------- shapes

def _shape_sprite(L, R, draw_p):
    """Anti-aliased shape at raster scale R; draw_p (0..1) reveals it (left-to-right or clockwise)."""
    w, h = max(1.0, L["w"] * R), max(1.0, L["h"] * R)
    m = 2  # margin for anti-aliasing
    W, H = int(math.ceil(w)) + 2 * m, int(math.ceil(h)) + 2 * m
    kind = L["shape"]
    stroke = L.get("stroke") or {}
    sw = float(stroke.get("width") or 0) * R
    ys = np.arange(H, dtype=np.float32)[:, None] + 0.5 - m
    xs = np.arange(W, dtype=np.float32)[None, :] + 0.5 - m

    def ellipse_cov(rx, ry):
        rx, ry = max(rx, 0.5), max(ry, 0.5)
        px, py = xs - w / 2, ys - h / 2
        k0 = np.sqrt((px / rx) ** 2 + (py / ry) ** 2)
        k1 = np.sqrt((px / rx ** 2) ** 2 + (py / ry ** 2) ** 2)
        d = k0 * (k0 - 1) / np.maximum(k1, 1e-6)
        return np.clip(0.5 - d, 0, 1)

    if kind in ("rect", "line"):
        r = float(L.get("radius") or 0) * R
        fill = textrender.rrect_mask(H, W, m, m, m + w, m + h, r)
        inner = None
        if sw > 0:
            inner = textrender.rrect_mask(H, W, m + sw, m + sw, m + w - sw, m + h - sw, max(0.0, r - sw))
    elif kind in ("circle", "ellipse"):
        fill = ellipse_cov(w / 2, h / 2)
        inner = ellipse_cov(w / 2 - sw, h / 2 - sw) if sw > 0 else None
    elif kind == "ring":
        sw = max(sw, 1.0)
        fill = np.clip(ellipse_cov(w / 2, h / 2) - ellipse_cov(w / 2 - sw, h / 2 - sw), 0, 1)
        inner = None
    else:  # polygon: supersampled fill, then area-averaged down
        ss = 4
        pts = np.array([[(m + px * w) * ss, (m + py * h) * ss] for px, py in L.get("points") or []], np.float64)
        big = np.zeros((H * ss, W * ss), np.uint8)
        cv2.fillPoly(big, [np.round(pts * 16).astype(np.int32)], 255, lineType=cv2.LINE_AA, shift=4)
        fill = cv2.resize(big, (W, H), interpolation=cv2.INTER_AREA).astype(np.float32) / 255.0
        inner = None
        if sw > 0:
            edge = np.zeros_like(big)
            cv2.polylines(edge, [np.round(pts * 16).astype(np.int32)], True, 255,
                          thickness=max(1, int(round(sw * 2 * ss))), lineType=cv2.LINE_AA, shift=4)
            stroke_m = cv2.resize(edge, (W, H), interpolation=cv2.INTER_AREA).astype(np.float32) / 255.0
            inner = np.clip(fill - np.minimum(fill, stroke_m), 0, 1)

    if draw_p < 1:
        if kind in ("circle", "ring", "ellipse"):
            ang = np.arctan2(xs - w / 2, -(ys - h / 2)) % (2 * np.pi)   # 0 at 12 o'clock, clockwise
            rad = np.maximum(np.sqrt((xs - w / 2) ** 2 + (ys - h / 2) ** 2), 1.0)
            reveal = np.clip((draw_p * 2 * np.pi - ang) * rad + 0.5, 0, 1)
        else:
            reveal = np.clip(draw_p * w - xs + 0.5, 0, 1) * np.ones_like(ys)
        fill = fill * reveal
        if inner is not None:
            inner = inner * reveal

    arr = np.zeros((H, W, 4), np.float32)
    stroked = inner is not None and sw > 0
    body = inner if stroked else fill
    if L.get("gradient"):
        textrender.over_gradient(arr, L["gradient"], body, m, h)
    else:
        textrender.over(arr, textrender.rgba01(L.get("color", "#FFFFFF")), body)
    if stroked:
        # the stroke band (fill - inner) and the body (inner) are disjoint areas, so their premultiplied
        # colours add: seamless where the body is opaque, a clean outline where it is transparent
        band = np.zeros_like(arr)
        textrender.over(band, textrender.rgba01(stroke.get("color", "#000000")), np.clip(fill - inner, 0, 1))
        arr += band
    return arr, (m, m, m + w, m + h)


def _progress_sprite(L, R, frac):
    w, h = max(1.0, L["w"] * R), max(1.0, L["h"] * R)
    W, H = int(math.ceil(w)), int(math.ceil(h))
    r = float(L.get("radius") or 0) * R
    arr = np.zeros((H, W, 4), np.float32)
    if L.get("track"):
        textrender.over(arr, textrender.rgba01(L["track"]), textrender.rrect_mask(H, W, 0, 0, w, h, r))
    if frac > 0:
        fill_w = max(frac * w, 2 * r)  # a rounded bar starts as a dot, not a sliver
        textrender.over(arr, textrender.rgba01(L["color"]), textrender.rrect_mask(H, W, 0, 0, fill_w, h, r))
    return arr, (0, 0, w, h)


# --------------------------------------------------------------------------- images and video

def _fit_size(sw, sh, bw, bh, fit):
    """Scaled source size for a fit inside a bw x bh box."""
    if fit == "fill":
        return bw, bh
    s = max(bw / sw, bh / sh) if fit == "cover" else min(bw / sw, bh / sh)
    return sw * s, sh * s


def _crop_origin(scaled, box, focus):
    """Top-left of a crop of length box out of scaled, keeping the focus point in frame."""
    return max(0.0, min(scaled - box, focus * scaled - box / 2.0))


HDR_TRANSFERS = ("arib-std-b67", "smpte2084")   # HLG (iPhone, most phones and drones), PQ (HDR10)
SEEK_PREROLL = 0.5   # s decoded before a seek target, so the frame on screen at that time is never trimmed away
# what zscale is told about the source (ffprobe's names), with the BT.2020 defaults for untagged fields
_ZSCALE_IN = (("tin", "color_transfer", ("arib-std-b67", "smpte2084", "bt709", "bt2020-10", "bt2020-12"), "bt2020-10"),
              ("pin", "color_primaries", ("bt2020", "bt709"), "bt2020"),
              ("min", "color_space", ("bt2020nc", "bt2020c", "bt709", "gbr"), "bt2020nc"))
_filters = None
_hdr_warned = []


def ffmpeg_filters():
    """Names of the filters the installed ffmpeg has (asked once)."""
    global _filters
    if _filters is None:
        names = set()
        try:
            r = subprocess.run([_ffmpeg(), "-hide_banner", "-filters"], capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=30)
            for line in r.stdout.splitlines():
                parts = line.split()
                if len(parts) >= 3 and re.match(r"^[A-Z.|]{2,3}$", parts[0]):
                    names.add(parts[1])
        except (OSError, subprocess.SubprocessError, RenderError):
            pass
        _filters = names
    return _filters


def color_filters(info, size=None):
    """Filters that turn HDR (HLG / PQ) or BT.2020 video into the BT.709 SDR the renderer composites.

    HDR is tone-mapped (zscale + tonemap, mobius: mid-tones and skin keep their level and colour,
    highlights roll off); BT.2020 SDR gets its gamut converted. size (w, h) folds the resize into the
    first zscale, so the float stages run at output size. [] for ordinary BT.709 / untagged video.
    Without zscale (an ffmpeg built without libzimg) a one-time warning says so, and HLG / BT.2020 get
    the native colorspace filter's approximation; PQ cannot be converted without it.
    """
    trc, prim = info.get("color_transfer"), info.get("color_primaries")
    hdr = trc in HDR_TRANSFERS
    if not hdr and prim != "bt2020":
        return []
    have = ffmpeg_filters()
    if "zscale" in have and (not hdr or "tonemap" in have):
        # say what the source is (zscale cannot guess untagged fields), resize in the same step
        src = ["%s=%s" % (opt, info.get(key) if info.get(key) in known else default)
               for opt, key, known, default in _ZSCALE_IN]
        if size:
            src += ["w=%d" % size[0], "h=%d" % size[1], "f=bicubic"]
        if hdr:
            return ["zscale=%s" % ":".join(src + ["t=linear", "npl=100"]), "format=gbrpf32le", "zscale=p=bt709",
                    "tonemap=tonemap=mobius:desat=0", "zscale=t=bt709", "format=gbrp"]
        return ["zscale=%s" % ":".join(src + ["p=bt709", "t=bt709"]), "format=gbrp"]
    if not _hdr_warned:
        _hdr_warned.append(True)
        print("this video is %s, and this ffmpeg has no zscale filter (it was built without libzimg), so its "
              "colours cannot be converted properly and will look %s. Install a full ffmpeg build (Windows: the "
              "gyan.dev or BtbN builds; macOS: brew install ffmpeg) and render again."
              % ("HDR (%s)" % ("HLG" if trc == "arib-std-b67" else "PQ") if hdr else "wide-gamut BT.2020",
                 "washed out" if hdr else "desaturated"), file=sys.stderr)
    if trc == "smpte2084":
        return []
    return ["colorspace=all=bt709:iall=bt2020:format=yuv420p"]


class VideoReader:
    """Frames of one video layer, at the timeline fps, already fitted to its box (uint8 RGB).

    Frame k of the reader is the source frame nearest to source time in + k / fps * speed, measured
    on the file's own clock (the one the clip audio is cut on): the decoder seeks a little before
    the target and the frames are timed against the target, not against the first frame decoded.
    """

    def __init__(self, L, R, fps):
        self.L, self.fps = L, fps
        self.info = timeline.probe(L["src"]) or {}
        sw, sh = self.info.get("width") or 1920, self.info.get("height") or 1080
        bw, bh = max(1.0, L["w"] * R), max(1.0, L["h"] * R)
        fw, fh = _fit_size(sw, sh, bw, bh, L["fit"])
        if L["fit"] == "cover":
            self.size = (max(1, int(round(bw))), max(1, int(round(bh))))
            self.scaled = (max(self.size[0], int(round(fw))), max(self.size[1], int(round(fh))))
        else:
            self.size = self.scaled = (max(1, int(round(fw))), max(1, int(round(fh))))
        self.box = ((self.size[0] - bw) / 2.0, (self.size[1] - bh) / 2.0)  # box origin in frame px
        self.proc = self.err = self.frames = self.pump = None
        self.next_k = None
        self.last = None
        self.last_k = None
        self.eof = False

    def _filter(self, offset=0.0):
        """The -vf chain; offset = seconds from the seek point to the first wanted source time."""
        L = self.L
        sw, sh = self.scaled
        speed = float(L["speed"])
        vf = []
        grid = self.info.get("base_fps") or self.info.get("fps")
        if grid and 1.0 <= grid <= 300.0:  # (not a container's 1000/1 "frame rate")
            # snap timestamps to the source's frame grid first: MKV/WebM store whole milliseconds
            # (33, 67, 100 ...), and that rounding would otherwise decide which frame fills a slot.
            # The grid is the base rate (r_frame_rate), never the average: variable-rate phone footage
            # averages 27.2 fps on a 30 fps grid, and snapping to the average drops and doubles frames
            vf.append("fps=%s" % _fps_str(grid))
        # time every frame against the target, then pick the frame nearest each output slot; the tiny
        # bias (1/1000 of a slot) breaks exact ties the same way every time (towards the frame on screen)
        vf += ["settb=AVTB", "setpts=(PTS-%.1f)/%r+%.1f" % (offset * 1e6, speed, 1000.0 / self.fps),
               "fps=%s:start_time=0" % _fps_str(self.fps)]
        crop = None
        if L["fit"] == "cover" and (sw, sh) != self.size:
            fx, fy = L["focus"]
            crop = (int(round(_crop_origin(sw, self.size[0], fx))), int(round(_crop_origin(sh, self.size[1], fy))))
        src_w, src_h = self.info.get("width"), self.info.get("height")
        if crop and src_w and src_h and color_filters(self.info):
            # HDR: crop in source pixels first (even, for 4:2:0), so the float tone-mapping stages only
            # process what is shown (twice as fast for a 16:9 clip in a 9:16 frame)
            k = sw / float(src_w)
            cw = min(src_w, max(2, int(round(self.size[0] / k / 2.0)) * 2))
            ch = min(src_h, max(2, int(round(self.size[1] / k / 2.0)) * 2))
            cx = min(src_w - cw, max(0, int(round(crop[0] / k / 2.0)) * 2))
            cy = min(src_h - ch, max(0, int(round(crop[1] / k / 2.0)) * 2))
            vf.append("crop=%d:%d:%d:%d" % (cw, ch, cx, cy))
            vf += color_filters(self.info, self.size)
            vf += ["scale=%d:%d:flags=bicubic" % self.size, "setsar=1"]
            return ",".join(vf)
        vf += color_filters(self.info, (sw, sh))
        vf += ["scale=%d:%d:flags=bicubic" % (sw, sh), "setsar=1"]
        if crop:
            vf.append("crop=%d:%d:%d:%d" % (self.size[0], self.size[1], crop[0], crop[1]))
        return ",".join(vf)

    def _start(self, k, src_t=None):
        """Start decoding at layer frame k (or at an explicit source time)."""
        self.close()
        L = self.L
        if src_t is None:
            src_t = float(L["in"]) + k / self.fps * float(L["speed"])
        dur = self.info.get("duration")
        cmd = [_ffmpeg(), "-nostdin", "-hide_banner", "-loglevel", "error"]
        if L["loop"]:
            if dur:
                src_t %= dur
            cmd += ["-stream_loop", "-1"]
        src_t = max(0.0, src_t)
        seek = max(0.0, src_t - SEEK_PREROLL)
        cmd += ["-ss", "%.6f" % seek, "-i", L["src"], "-an", "-sn", "-dn", "-vf", self._filter(src_t - seek),
                "-pix_fmt", "rgb24", "-f", "rawvideo", "-"]
        self.err = tempfile.TemporaryFile()
        self.proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=self.err,
                                     bufsize=self.size[0] * self.size[1] * 3 * 2)
        # a pump thread reads ahead, so decoding overlaps compositing
        self.frames = queue.Queue(maxsize=3)
        self.pump = threading.Thread(target=self._pump, args=(self.proc, self.frames, self.size), daemon=True)
        self.pump.start()
        self.next_k = k
        self.eof = False

    @staticmethod
    def _pump(proc, frames, size):
        n = size[0] * size[1] * 3
        try:
            while True:
                buf = proc.stdout.read(n)
                if len(buf) < n:
                    break
                frames.put(np.frombuffer(buf, np.uint8).reshape(size[1], size[0], 3))
        except (OSError, ValueError):
            pass  # the reader was closed under us
        frames.put(None)

    def _read(self):
        f = self.frames.get()
        if f is None:
            self.eof = True
        return f

    def frame_at(self, k):
        """Frame k (0 = layer start) as uint8 RGB; holds the last frame when the source ends."""
        if k == self.last_k and self.last is not None:
            return self.last
        if self.proc is None or self.next_k is None or k < self.next_k or k - self.next_k > 3 * self.fps:
            if not (self.eof and self.last is not None and k >= (self.last_k or 0)):
                self._start(k)
        while not self.eof and self.next_k <= k:
            f = self._read()
            if f is None:
                break
            self.last, self.last_k = f, self.next_k
            self.next_k += 1
        if self.eof and self.last is None:
            # sought past the end: decode the final second once to find the last frame
            self._hold_last()
        if self.last is None:
            msg = b""
            if self.err is not None:
                self.err.seek(0)
                msg = self.err.read()[-600:]
            detail = msg.decode("utf-8", "replace").strip() or "no frames"
            raise RenderError("could not decode %s: %s" % (self.L["src"], detail))
        self.last_k = k
        return self.last

    def _hold_last(self):
        dur = self.info.get("duration")
        if not dur:
            return
        self._start(0, src_t=max(0.0, dur - 1.0))
        while True:
            f = self._read()
            if f is None:
                break
            self.last = f

    def close(self):
        if self.proc is not None:
            try:
                self.proc.kill()
                while self.pump.is_alive():  # unblock the pump if it waits on a full queue
                    try:
                        self.frames.get(timeout=0.05)
                    except queue.Empty:
                        pass
                self.proc.stdout.close()
                self.proc.wait(timeout=5)
            except (OSError, subprocess.SubprocessError):
                pass
            self.proc = self.pump = self.frames = None
        if self.err is not None:
            self.err.close()
            self.err = None


def _fps_str(fps):
    fr = fractions.Fraction(fps).limit_denominator(1001)
    return "%d/%d" % (fr.numerator, fr.denominator)


def _load_image(path):
    img = ImageOps.exif_transpose(Image.open(path))
    arr = np.asarray(img.convert("RGBA"), np.float32) / 255.0
    arr[..., :3] *= arr[..., 3:4]  # premultiply
    return arr


# --------------------------------------------------------------------------- the compositor

SPRITE_CACHE_BYTES = 512 * 1024 * 1024   # settled shape and image sprites kept between frames
SPLIT_CACHE_BYTES = 384 * 1024 * 1024    # their colour / (1 - alpha) planes for unresampled placement


class SpriteCache:
    """Least-recently-used cache with a byte budget, grouped by owner (a layer) so that everything a
    finished layer left behind can be dropped at once. A full-frame sprite at 1080x1920 is 33 MB:
    a long plan with a background per beat must not keep every one of them for the whole render."""

    def __init__(self, budget):
        self.budget, self.used = budget, 0
        self.data = collections.OrderedDict()   # key -> (value, nbytes, owner)
        self.owners = {}                        # owner -> set of keys

    def __len__(self):
        return len(self.data)

    def get(self, key):
        hit = self.data.get(key)
        if hit is None:
            return None
        self.data.move_to_end(key)
        return hit[0]

    def put(self, key, value, nbytes, owner=None):
        self.pop(key)
        self.data[key] = (value, int(nbytes), owner)
        self.owners.setdefault(owner, set()).add(key)
        self.used += int(nbytes)
        while self.used > self.budget and len(self.data) > 1:
            self.pop(next(iter(self.data)))

    def pop(self, key):
        hit = self.data.pop(key, None)
        if hit is not None:
            self.used -= hit[1]
            keys = self.owners.get(hit[2])
            if keys is not None:
                keys.discard(key)
                if not keys:
                    del self.owners[hit[2]]
        return hit

    def drop(self, owner):
        """Forget everything an owner put in."""
        for key in list(self.owners.get(owner, ())):
            self.pop(key)


class Compositor:
    """Renders frames of a normalized, valid spec at output scale S (1 = full size)."""

    def __init__(self, spec, scale=1.0, fps=None):
        self.spec = spec
        self.S = float(scale)
        self.W, self.H = _even(spec["width"] * self.S), _even(spec["height"] * self.S)
        self.fps = float(fps or spec["fps"])
        self.readers = {}
        self.cache = {}  # full-frame effect masks (vignette, grain): one each, kept for the render
        self.sprites = SpriteCache(SPRITE_CACHE_BYTES)   # settled shape / image sprites, per layer
        self.splits = SpriteCache(SPLIT_CACHE_BYTES)     # id(sprite) -> (sprite, rgb, inv3), per layer
        self.bg = self._background()
        self.normal = [L for L in spec["layers"] if not L["fixed"]]
        self.fixed = [L for L in spec["layers"] if L["fixed"]]

    # ----- background
    def _background(self):
        bg = self.spec["background"]
        H, W = self.H, self.W
        if isinstance(bg, dict):
            cols = np.array([textrender.rgba01(c)[:3] for c in bg["gradient"]], np.float32)
            th = math.radians(float(bg.get("angle", 90)))
            dx, dy = math.cos(th), math.sin(th)
            span = abs(W * dx) + abs(H * dy)
            xs = np.arange(W, dtype=np.float32)[None, :] + 0.5 - W / 2
            ys = np.arange(H, dtype=np.float32)[:, None] + 0.5 - H / 2
            u = np.clip((xs * dx + ys * dy) / max(span, 1) + 0.5, 0, 1) * (len(cols) - 1)
            i0 = np.minimum(np.floor(u).astype(int), len(cols) - 2)
            f = (u - i0)[..., None]
            img = cols[i0] * (1 - f) + cols[i0 + 1] * f
            # triangular dither of +-1 code value hides 8-bit banding in smooth gradients
            rng = np.random.default_rng(7)
            img += ((rng.random((H, W, 1), np.float32) - rng.random((H, W, 1), np.float32)) / 255.0)
            return img.astype(np.float32)
        r, g, b, _ = textrender.rgba01(bg)
        img = np.empty((H, W, 3), np.float32)
        img[...] = (r, g, b)
        return img

    # ----- sprites
    def _raster(self, L):
        """Sprite px per spec px: the output scale times the largest scale the layer reaches by keyframes."""
        keys = (L.get("animate") or {}).get("scale") or []
        top = max([float(L["scale"])] + [float(k[1]) for k in keys])
        cap = 2.0 if L["type"] == "video" else 4.0
        return self.S * min(cap, max(0.05, top))

    def _text_layer(self, L, lt, st):
        if L["type"] == "counter":
            L = self._counter_text(L, lt)
        words_visible, reveal = None, st["reveal"]
        en = L.get("enter") or {}
        if en.get("type") in ("typewriter", "words") and L.get("word_times"):
            words_visible = sum(1 for wt in L["word_times"] if wt <= lt)
            reveal = 1.0
        elif en.get("type") == "words" and reveal < 1:
            n = len(timeline.text_words(L))
            words_visible = int(math.floor(reveal * n + 1e-9))
            reveal = 1.0
        return L, reveal, words_visible

    @staticmethod
    def _format_number(L, v):
        dec = int(L.get("decimals") or 0)
        s = "{:,.{d}f}".format(abs(v), d=dec).replace(",", "\x00")
        s = s.replace("\x00", L.get("separator") if L.get("separator") is not None else "")
        return ("-" if v < 0 and round(abs(v), dec) > 0 else "") + L.get("prefix", "") + s + L.get("suffix", "")

    def _counter_text(self, L, lt):
        delay = float((L.get("enter") or {}).get("delay") or 0)
        p = anim.ease(L["ease"], (lt - delay) / max(float(L["count_duration"]), 1e-6))
        v = float(L["from"]) + (float(L["to"]) - float(L["from"])) * p
        ends = [self._format_number(L, float(L["from"])), self._format_number(L, float(L["to"]))]
        return dict(L, text=self._format_number(L, v), _tabular=True, _reserve=max(ends, key=len))

    def _sprite(self, L, lt, st, k_frame):
        """(sprite, layout box, raster scale R) for a layer at layer time lt."""
        R = self._raster(L)
        t = L["type"]
        if t in ("text", "counter"):
            L2, reveal, words_visible = self._text_layer(L, lt, st)
            arr, box = textrender.sprite(L2, R, reveal, words_visible)
            return arr, box, R
        if t == "shape":
            en = L.get("enter") or {}
            draw_p = 1.0
            if L.get("draw"):
                d = L["draw"]
                dur = float(en.get("duration") or 0.5)
                q = anim.ease(d["ease"], (lt - float(en.get("delay") or 0)) / max(dur, 1e-6))
                draw_p = float(d["from"]) + (float(d["to"]) - float(d["from"])) * q
            key = ("shape", id(L), R, round(draw_p, 4))
            hit = self.sprites.get(key)
            if hit is None:
                hit = self._with_shadow(L, *_shape_sprite(L, R, draw_p), R=R)
                if draw_p >= 1:
                    self.sprites.put(key, hit, hit[0].nbytes, id(L))
            return hit[0], hit[1], R
        if t == "progress":
            life = max(float(L["end"]) - float(L["start"]), 1e-6)
            frac = float(L["from"]) + (float(L["to"]) - float(L["from"])) * min(1.0, max(0.0, lt / life))
            arr, box = self._with_shadow(L, *_progress_sprite(L, R, frac), R=R)
            return arr, box, R
        if t == "image":
            key = ("image", id(L), R)
            hit = self.sprites.get(key)
            if hit is None:
                hit = self._with_shadow(L, *self._image_sprite(L, R), R=R)
                self.sprites.put(key, hit, hit[0].nbytes, id(L))
            return hit[0], hit[1], R
        # video
        reader = self.readers.get(id(L))
        if reader is None:
            reader = self.readers[id(L)] = VideoReader(L, R, self.fps)
        frame = reader.frame_at(k_frame)
        arr = frame.astype(np.float32)
        arr *= np.float32(1.0 / 255.0)
        bx, by = reader.box
        box = (bx, by, bx + L["w"] * R, by + L["h"] * R)
        if L.get("shadow") and L["fit"] == "contain":
            arr, box = self._with_shadow(L, _to_rgba(arr), box, R=R)
        return arr, box, R

    def _with_shadow(self, L, arr, box, R):
        sh = L.get("shadow")
        if not sh:
            return arr, box
        ml, mt, mr, mb = textrender.shadow_margin(sh, R)
        arr, l, t = _pad(_to_rgba(arr), ml + 1, mt + 1, mr + 1, mb + 1)
        arr = textrender.add_shadow(arr, sh, R)
        return arr, (box[0] + l, box[1] + t, box[2] + l, box[3] + t)

    def _image_sprite(self, L, R):
        img = _load_image(L["src"])
        ih, iw = img.shape[:2]
        if L.get("w") is None and L.get("h") is None:
            bw, bh = float(iw), float(ih)
        elif L.get("w") is None:
            bw, bh = L["h"] * iw / float(ih), float(L["h"])
        elif L.get("h") is None:
            bw, bh = float(L["w"]), L["w"] * ih / float(iw)
        else:
            bw, bh = float(L["w"]), float(L["h"])
        bw, bh = bw * R, bh * R
        fw, fh = _fit_size(iw, ih, bw, bh, L["fit"])
        sw, sh = max(1, int(round(fw))), max(1, int(round(fh)))
        interp = cv2.INTER_AREA if sw < iw else cv2.INTER_CUBIC
        img = np.clip(cv2.resize(img, (sw, sh), interpolation=interp), 0, 1)
        if L["fit"] == "cover":
            cw, ch = max(1, int(round(bw))), max(1, int(round(bh)))
            x = int(round(_crop_origin(sw, cw, L["focus"][0])))
            y = int(round(_crop_origin(sh, ch, L["focus"][1])))
            img = img[y:y + ch, x:x + cw]
        h, w = img.shape[:2]
        r = float(L.get("radius") or 0) * R
        if r > 0:
            img = img * textrender.rrect_mask(h, w, 0, 0, w, h, r)[..., None]
        if img.shape[2] == 4 and img[..., 3].min() >= 0.999:
            img = np.ascontiguousarray(img[..., :3])
        bx, by = (w - bw) / 2.0, (h - bh) / 2.0
        return img, (bx, by, bx + bw, by + bh)

    # ----- one layer
    def _draw_layer(self, canvas, L, t, frame_idx, cam):
        lt = t - float(L["start"])
        st = anim.state_at(L, lt, canvas=(self.spec["width"], self.spec["height"]), fps=self.fps, frame=frame_idx)
        if st["opacity"] <= 0.002 or st["scale"] <= 1e-4:
            return
        # the reader frame nearest lt; a layer starting on a half frame puts every lt*fps on x.5, so ties
        # must always go the same way (round() rounds halves to even: frames 0, 2, 2, 4, 4 ... judder).
        # They go to the earlier frame, the one already on screen, as in the reader: the in-point shows first
        k_frame = max(0, int(math.floor(lt * self.fps + 0.5 - 1e-6)))
        spr, box, R = self._sprite(L, lt, st, k_frame)
        cached = spr
        spr, box = _apply_looks(spr, box, st, R, L, frame_idx)
        fx, fy = timeline.ANCHORS[L["anchor"]]
        anchor = (box[0] + fx * (box[2] - box[0]), box[1] + fy * (box[3] - box[1]))
        scale = st["scale"]
        kb = L.get("kenburns")
        if kb:
            life = max(float(L["end"]) - float(L["start"]), 1e-6)
            scale *= float(kb[0]) + (float(kb[1]) - float(kb[0])) * min(1.0, max(0.0, lt / life))
        k = scale * self.S / R
        M = _affine((st["x"] * self.S, st["y"] * self.S), anchor, k, st["rotation"])
        if cam is not None:
            M = cam @ M
        crisp = not st["moving"] and not (cam is not None and _camera_moving(self.spec, t))
        split = None
        if spr is cached and spr.shape[2] == 4 and L["type"] in ("text", "counter", "shape", "image"):
            split = lambda: self._split(spr, id(L))  # noqa: E731 - computed only if composite places it unresampled
        composite(canvas, spr, M, L["blend"], st["opacity"], crisp=crisp, split=split)

    def _split(self, spr, owner=None):
        """split_alpha of a sprite that comes back every frame (text at rest, shapes, images), cached."""
        hit = self.splits.get(id(spr))
        if hit is not None and hit[0] is spr:
            return hit[1], hit[2]
        rgb, inv3 = split_alpha(spr)
        # holding spr keeps its id from being reused; it counts towards the budget while held here
        self.splits.put(id(spr), (spr, rgb, inv3), spr.nbytes + rgb.nbytes + inv3.nbytes, owner)
        return rgb, inv3

    def _done(self, L):
        """A layer is over (sequential rendering is past its end): free its decoder and sprites."""
        if L["type"] == "video" and id(L) in self.readers:
            self.readers.pop(id(L)).close()
        self.sprites.drop(id(L))
        self.splits.drop(id(L))

    # ----- effects
    def _effects(self, img, t):
        effects = [(i, e) for i, e in enumerate(self.spec["effects"]) if isinstance(e, dict)]
        effects.sort(key=lambda ie: (EFFECT_ORDER.index(ie[1]["type"]), ie[0]))
        tick = int(math.floor(t * 24))  # randomness changes 24 times a second whatever the fps
        for i, e in effects:
            start = e.get("start")
            if start is not None:
                dur = float(e.get("duration") or 0)
                if not (start <= t < start + dur):
                    continue
                p = (t - start) / max(dur, 1e-6)
            else:
                p = 0.0
            img = getattr(self, "_fx_" + e["type"].replace("-", "_"))(img, e, p, i, tick)
        return img

    def _fx_shake(self, img, e, p, i, tick):
        amp = float(e["intensity"]) * self.S * (1 - p) ** 1.5
        if amp < 0.05:
            return img
        dx = amp * (anim.rng_unit("shake", i, tick, 0) * 2 - 1)
        dy = amp * 0.8 * (anim.rng_unit("shake", i, tick, 1) * 2 - 1)
        rot = 0.04 * float(e["intensity"]) * (1 - p) * (anim.rng_unit("shake", i, tick, 2) * 2 - 1)
        z = 1 + 2.2 * amp / min(self.W, self.H)  # zoom in a hair so the edges never show
        M = _affine((self.W / 2.0 + dx, self.H / 2.0 + dy), (self.W / 2.0, self.H / 2.0), z, rot)
        return cv2.warpAffine(img, _cv_affine(M), (self.W, self.H), flags=cv2.INTER_LINEAR,
                              borderMode=cv2.BORDER_REFLECT101)

    def _fx_zoom_blur(self, img, e, p, i, tick):
        strength = 0.07 * float(e["intensity"]) * math.sin(math.pi * p)
        if strength < 0.003:
            return img
        n = 8
        acc = img.copy()
        c = (self.W / 2.0, self.H / 2.0)
        for j in range(1, n):
            M = _affine(c, c, 1 + strength * j / (n - 1), 0)
            acc += cv2.warpAffine(img, _cv_affine(M), (self.W, self.H), flags=cv2.INTER_LINEAR,
                                  borderMode=cv2.BORDER_REFLECT101)
        acc *= 1.0 / n
        return acc

    def _fx_glitch(self, img, e, p, i, tick):
        g = float(e["intensity"]) * (0.35 + 0.65 * anim.rng_unit("glitch", i, tick, "on"))
        d = int(round(0.012 * self.W * g * (0.5 + anim.rng_unit("glitch", i, tick, "split"))))
        out = img.copy()
        if d:
            out[:, d:, 0] = img[:, :-d, 0]
            out[:, :-d, 2] = img[:, d:, 2]
        for k in range(7):
            if anim.rng_unit("glitch", i, tick, "band", k) > 0.6:
                continue
            y0 = int(anim.rng_unit("glitch", i, tick, "y", k) * self.H)
            hh = max(2, int(self.H * (0.01 + 0.07 * anim.rng_unit("glitch", i, tick, "h", k))))
            dx = int(round((anim.rng_unit("glitch", i, tick, "dx", k) * 2 - 1) * 0.06 * self.W * g))
            if dx:
                out[y0:y0 + hh] = np.roll(out[y0:y0 + hh], dx, axis=1)
        return out

    def _fx_grade(self, img, e, p, i, tick):
        c, s = float(e["contrast"]), float(e["saturation"])
        b, w = float(e["brightness"]), float(e["warmth"])
        luma = np.array([0.2126, 0.7152, 0.0722], np.float32)
        sat = s * np.eye(3, dtype=np.float32) + (1 - s) * np.outer(np.ones(3, np.float32), luma)
        warm = np.diag([1 + 0.12 * w, 1 + 0.02 * w, 1 - 0.12 * w]).astype(np.float32)
        M = warm @ sat * c
        off = warm @ np.full(3, c * b + 0.5 * (1 - c), np.float32)
        return cv2.transform(img, np.hstack([M, off[:, None]]).astype(np.float32), dst=img)

    def _fx_vignette(self, img, e, p, i, tick):
        key = ("vignette", float(e["strength"]))
        mask = self.cache.get(key)
        if mask is None:
            xs = (np.arange(self.W, dtype=np.float32) + 0.5) / self.W * 2 - 1
            ys = (np.arange(self.H, dtype=np.float32) + 0.5) / self.H * 2 - 1
            r = np.sqrt(xs[None, :] ** 2 + ys[:, None] ** 2) / math.sqrt(2)
            u = np.clip((r - 0.35) / 0.65, 0, 1)
            m = 1 - float(e["strength"]) * (u * u * (3 - 2 * u))
            mask = self.cache[key] = cv2.merge([m.astype(np.float32)] * 3)
        return cv2.multiply(img, mask, dst=img)

    def _fx_flash(self, img, e, p, i, tick):
        env = p / 0.1 if p < 0.1 else ((1 - p) / 0.9) ** 1.6
        a = float(e["opacity"]) * env
        if a <= 0.002:
            return img
        r, g, b, ca = textrender.rgba01(e["color"])
        a *= ca
        img *= 1 - a
        cv2.add(img, (r * a, g * a, b * a, 0.0), dst=img)
        return img

    def _fx_letterbox(self, img, e, p, i, tick):
        bar = (self.H - self.W / float(e["ratio"])) / 2.0
        if bar <= 0:
            return img
        dur = float(e.get("duration") or 0)
        ramp = min(0.35, dur / 3.0) if dur else 0.0
        el = p * dur
        if ramp:
            if el < ramp:
                bar *= anim.ease("out-cubic", el / ramp)
            elif el > dur - ramp:
                bar *= anim.ease("out-cubic", (dur - el) / ramp)
        n = int(round(bar))
        if n > 0:
            img[:n] = 0
            img[self.H - n:] = 0
        return img

    def _fx_grain(self, img, e, p, i, tick):
        key = ("grain", i, float(e["strength"]))
        noise = self.cache.get(key)
        if noise is None:
            # one field of soft monochrome noise, slid around every 1/24 s
            rng = np.random.default_rng(1234 + i)
            n = rng.standard_normal((self.H + 64, self.W + 64)).astype(np.float32)
            n = cv2.GaussianBlur(n, (0, 0), 0.6 * max(self.S, 0.5)) * (1.6 * float(e["strength"]))
            noise = self.cache[key] = cv2.cvtColor(n, cv2.COLOR_GRAY2RGB)
        ox = int(anim.rng_unit("grain", i, tick, "x") * 64)
        oy = int(anim.rng_unit("grain", i, tick, "y") * 64)
        cv2.add(img, noise[oy:oy + self.H, ox:ox + self.W], dst=img)
        return img

    # ----- frames
    def frame(self, t, frame_idx=None):
        """Float32 RGB frame (0..1) at timeline time t."""
        if frame_idx is None:
            frame_idx = int(round(t * self.fps))
        canvas = self.bg.copy()
        cam = _camera_matrix(self.spec, t, self.S, self.W, self.H)
        if cam is not None and isinstance(self.spec["background"], dict):
            canvas = cv2.warpAffine(canvas, _cv_affine(cam), (self.W, self.H), flags=cv2.INTER_LINEAR,
                                    borderMode=cv2.BORDER_REFLECT101)
        for group, c in ((self.normal, cam), (self.fixed, None)):
            for L in group:
                if L["start"] <= t < L["end"]:
                    self._draw_layer(canvas, L, t, frame_idx, c)
                elif t >= L["end"]:
                    self._done(L)
        return self._effects(canvas, t)

    def frame_u8(self, t, frame_idx=None):
        """uint8 RGB frame at timeline time t."""
        img = self.frame(t, frame_idx)
        cv2.max(img, 0.0, dst=img)  # convertScaleAbs takes |x|: clamp negatives (strong grades) first
        return cv2.convertScaleAbs(img, alpha=255.0)

    def close(self):
        for r in self.readers.values():
            r.close()
        self.readers.clear()


# --------------------------------------------------------------------------- captions

def _caption_style(cap):
    mod = paths.load_skill("captions")
    if isinstance(cap.get("style"), dict):
        style = dict(mod.DEFAULT_CAPTION)
        style.update(cap["style"])
    elif cap.get("style_dna"):
        style = mod.caption_style(cap["style_dna"])
    else:
        style = dict(mod.DEFAULT_CAPTION)
    if not isinstance(style.get("words_per_card"), dict):
        n = style.get("words_per_card") or 3
        style["words_per_card"] = {"median": n, "max": n}
    return mod, style


CAPTION_GAP = 0.6  # a silence longer than this ends the card (0.4 s after its last word) instead of holding it


def _caption_runs(words, gap=CAPTION_GAP):
    """Split timeline words into runs at silences longer than gap.

    captions.build_ass keeps a card up until the next card starts; across a long gap (a beat whose line is
    already on screen as a graphic, b-roll, a cut pause) that would leave a stale card on screen for seconds.
    Each run is built separately, so its last card ends 0.4 s after its last word.
    """
    runs, cur = [], []
    for w in sorted(words, key=lambda w: float(w["s"])):
        if cur and float(w["s"]) - float(cur[-1]["e"]) > gap:
            runs.append(cur)
            cur = []
        cur.append(w)
    if cur:
        runs.append(cur)
    return runs


_OVERRIDE = re.compile(r"((?<!\\)\{[^}]*\})")   # an override block; an escaped \{ is literal text
_TRAILING_PUNCT = re.compile(r"(?<=[^\s.,;:])[.,;:]+(?=\s|$)")


_WORD_JOINER = "\u2060"


def ass_text(word):
    """A caption word as literal ASS text: "{" would open an override block (the word vanishes, or
    "{\\fs300}" restyles the card) and a backslash would start an escape ("\\n" becomes a line break
    once uppercased to "\\N"). Braces become libass's \\{ \\}; a backslash gets an invisible word
    joiner after it, so it prints and never pairs with the next character."""
    return str(word).replace("\\", "\\" + _WORD_JOINER).replace("{", "\\{").replace("}", "\\}")


def _clean_event(line):
    """Drop trailing . , ; : from the words of one ASS Dialogue line (outside override tags).

    The words keep their punctuation because captions.chunk ends cards on it; on screen, short-form
    captions read cleaner without it. ? and ! stay: they carry the tone.
    """
    head, sep, text = line.partition(",,0,0,0,,")
    if not sep:
        return line
    parts = _OVERRIDE.split(text)
    return head + sep + "".join(p if p.startswith("{") else _TRAILING_PUNCT.sub("", p) for p in parts)


def build_captions(spec, W, H, workdir):
    """Write captions.ass (and copy its font) into workdir; returns the ffmpeg filter, or None without words."""
    cap = spec.get("captions")
    if not cap or not cap.get("words"):
        return None
    mod, style = _caption_style(cap)
    font = cap.get("font") or (style.get("font_candidates") or ["Montserrat Black"])[0]
    fonts_dir = os.path.join(workdir, "fonts")
    fontname, weight = fonts.ass_font(font, fonts_dir)
    # the file's real weight decides libass's bold flag, so no faux-bold smear on heavy fonts
    style["font_look"] = dict(style.get("font_look") or {}, weight="bold" if weight >= 600 else "regular")
    ass = None
    words = [dict(w, w=ass_text(w["w"])) for w in cap["words"]]
    for run in _caption_runs(words):
        text, _ = mod.build_ass(run, style, W, H, fontname, bool(cap.get("pop")))
        lines = text.splitlines()
        if ass is None:  # header from the first run, Dialogue events from every run
            ass = [l for l in lines if not l.startswith("Dialogue:")]
        ass += [_clean_event(l) for l in lines if l.startswith("Dialogue:")]
    ass = "\n".join(ass) + "\n"
    path = os.path.join(workdir, "captions.ass")
    with open(path, "w", encoding="utf-8") as f:
        f.write(ass)
    return "ass=filename=%s:fontsdir=%s" % (ffmpeg_filter_path(path), ffmpeg_filter_path(fonts_dir))


# --------------------------------------------------------------------------- API

def _prepare(spec, base_dir=None):
    s = timeline.load(spec, base_dir)
    errors = timeline.validate(s)
    if errors:
        raise RenderError("the timeline has problems:\n- " + "\n- ".join(errors))
    return s


def render(spec, out_path, preview=False, progress=None, workdir=None, base_dir=None):
    """Render a spec (dict, JSON text or path) to an MP4; returns out_path.

    preview=True: half resolution, half frame rate. progress(done, total) is
    called after every frame. workdir keeps the intermediate files (captions,
    silent video, audio mix); by default they go to a temporary folder.
    """
    s = _prepare(spec, base_dir)
    ffmpeg = _ffmpeg()
    S = 0.5 if preview else 1.0
    fps = float(s["fps"]) / (2.0 if preview else 1.0)
    comp = Compositor(s, S, fps)
    W, H = comp.W, comp.H
    total = max(1, int(round(float(s["duration"]) * fps)))
    out_path = os.path.abspath(out_path)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    own_tmp = workdir is None
    workdir = tempfile.mkdtemp(prefix="vstudio_render_") if own_tmp else paths.ensure(os.path.abspath(workdir))
    try:
        vf = [f for f in [build_captions(s, W, H, workdir)] if f]
        vf.append("scale=out_color_matrix=bt709:out_range=tv,format=yuv420p")
        silent = os.path.join(workdir, "video.mp4")
        cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24",
               "-s", "%dx%d" % (W, H), "-r", _fps_str(fps), "-i", "-", "-vf", ",".join(vf),
               "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p",
               "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709", "-color_range", "tv",
               "-movflags", "+faststart", "-an", silent]
        err = tempfile.TemporaryFile()
        enc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=err)
        # a writer thread feeds the encoder while the next frame is composited
        pending, broken = queue.Queue(maxsize=3), []

        def writer():
            while True:
                item = pending.get()
                if item is None:
                    return
                if not broken:
                    try:
                        enc.stdin.write(item)
                    except (BrokenPipeError, OSError) as e:
                        broken.append(e)  # keep draining so the compositor never blocks

        th = threading.Thread(target=writer, daemon=True)
        th.start()
        try:
            for i in range(total):
                if broken:
                    break
                pending.put(comp.frame_u8(i / fps, i))
                if progress:
                    progress(i + 1, total)
        finally:
            pending.put(None)
            th.join()
            comp.close()
            try:
                enc.stdin.close()
            except OSError:
                pass
        code = enc.wait()
        err.seek(0)
        msg = err.read().decode("utf-8", "replace").strip()
        err.close()
        if code != 0:
            raise RenderError("ffmpeg could not encode the video: %s" % (msg[-1500:] or "exit code %d" % code))
        mix = audio.mixdown(s, total / fps, os.path.join(workdir, "mix.wav"), workdir=workdir)
        if mix:
            # the mix is exactly as long as the video; no -shortest: it cuts the last frames (the CTA, the loop
            # point) whenever the audio packets end a hair early, which at 29.97 / 59.94 fps is every time
            r = subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", silent, "-i", mix,
                                "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
                                "-ar", "48000", "-movflags", "+faststart", out_path],
                               capture_output=True, text=True, encoding="utf-8", errors="replace")
            if r.returncode != 0:
                raise RenderError("ffmpeg could not add the audio: %s" % r.stderr.strip()[-1500:])
        else:
            try:
                os.replace(silent, out_path)  # overwrites on Windows too
            except OSError:
                shutil.copyfile(silent, out_path)  # a different drive or filesystem
    finally:
        if own_tmp:
            shutil.rmtree(workdir, ignore_errors=True)
    return out_path


def render_frame(spec, t, preview=False, base_dir=None):
    """One frame (HxWx3 uint8 RGB, captions not included) at timeline second t."""
    s = _prepare(spec, base_dir)
    fps = float(s["fps"]) / (2.0 if preview else 1.0)
    comp = Compositor(s, 0.5 if preview else 1.0, fps)
    try:
        return comp.frame_u8(float(t), int(round(float(t) * fps)))
    finally:
        comp.close()


def still(spec, t, out_png, preview=False, base_dir=None):
    """Save the frame at second t as an image, with the captions burned in when there are any."""
    s = _prepare(spec, base_dir)
    frame = render_frame(s, t, preview)
    out_png = os.path.abspath(out_png)
    os.makedirs(os.path.dirname(out_png), exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="vstudio_still_") as tmp:
        raw = os.path.join(tmp, "frame.png")
        Image.fromarray(frame).save(raw)
        flt = build_captions(s, frame.shape[1], frame.shape[0], tmp) if shutil.which("ffmpeg") else None
        if flt:
            # shift the frame's timestamp to t so libass shows the captions of that moment
            r = subprocess.run([_ffmpeg(), "-hide_banner", "-loglevel", "error", "-y", "-i", raw, "-vf",
                                "setpts=PTS+%.6f/TB,%s" % (float(t), flt), "-frames:v", "1", "-update", "1",
                                out_png], capture_output=True, text=True, encoding="utf-8", errors="replace")
            if r.returncode == 0:
                return out_png
            print("captions could not be burned into the still: %s" % r.stderr.strip()[-400:], file=sys.stderr)
        Image.fromarray(frame).save(out_png)
    return out_png


IMAGE_EXT = (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="vstudio render", description="Render a timeline spec (JSON) to MP4 or a still.")
    ap.add_argument("spec", help="timeline spec .json")
    ap.add_argument("--out", required=True, help="output .mp4 (or .png with --still)")
    ap.add_argument("--preview", action="store_true", help="half resolution and half frame rate")
    ap.add_argument("--still", type=float, metavar="SECONDS", help="render one frame at this time instead")
    ap.add_argument("--workdir", help="keep intermediate files (captions .ass, silent video, audio mix) here")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args(argv)
    ext = os.path.splitext(a.out)[1].lower()
    if a.still is None and ext in IMAGE_EXT:
        print("render: %s is an image; add --still SECONDS to render one frame (or use --out video.mp4)" % a.out,
              file=sys.stderr)
        return 2
    if a.still is not None and ext not in IMAGE_EXT:
        print("render: --still writes an image; give --out a .png or .jpg name, not %s" % a.out, file=sys.stderr)
        return 2
    t0 = time.time()
    try:
        if a.still is not None:
            path = still(a.spec, a.still, a.out, a.preview)
            print("wrote %s" % path)
            return 0
        last = [0.0]

        def bar(done, total):
            now = time.time()
            if a.quiet or (now - last[0] < 0.25 and done < total):
                return
            last[0] = now
            n = int(30 * done / total)
            sys.stderr.write("\r[%s%s] %d/%d frames  %.1f s" % ("#" * n, "." * (30 - n), done, total, now - t0))
            if done == total:
                sys.stderr.write("\n")
            sys.stderr.flush()

        path = render(a.spec, a.out, preview=a.preview, progress=bar, workdir=a.workdir)
    except (RenderError, OSError, ValueError) as err:
        print("render failed: %s" % err, file=sys.stderr)
        return 1
    info = timeline.probe(path) or {}
    print("wrote %s (%sx%s, %.2f s) in %.1f s" % (path, info.get("width"), info.get("height"),
                                                  info.get("duration") or 0, time.time() - t0))
    return 0


if __name__ == "__main__":
    sys.exit(main())
