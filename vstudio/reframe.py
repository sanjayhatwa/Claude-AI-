"""
reframe.py - follow the speaker's face so a 16:9 recording can be cropped to
9:16 (or any other shape) without cutting their head off.

    ensure_model()                         -> path of the YuNet face model (downloads it once), or None
    detect_faces(frame_bgr)                -> [(x, y, w, h, score), ...] in frame pixels, best first
    track_focus("raw.mp4", [0.5, 1.0, 1.5], aspect=(9, 16))  -> [[fx, fy], ...]
    track("raw.mp4", times, aspect)        -> {"points": [...], "method": "yunet", "found": n, "samples": m, ...}

How it works
    Frames are sampled at the requested times (one ffmpeg pass at a few
    frames per second, scaled to 640 px wide; sparse times are fetched with
    one seek each instead). Each frame goes through OpenCV's YuNet face
    detector (cv2.FaceDetectorYN with face_detection_yunet_2023mar.onnx from
    the OpenCV model zoo, about 230 KB, downloaded into workspace/models and
    checked against its published SHA-256). Without YuNet a Haar cascade is
    used when OpenCV ships one; otherwise nothing is detected.

    The face to follow is the largest confident one; when another face of
    similar size is closer to where the speaker was a moment ago, that one
    wins, so two-person shots do not flip back and forth.

    Smoothing: a median of 5 over the detections removes one-off false
    hits; the track is split where the face jumps (a camera change in the
    source) so the smoothing never drags across a cut; inside each run a
    zero-phase exponential moving average (forward and backward) settles the
    remaining jitter without lag. Samples without a face take the nearest
    detection of their run (interpolated between two); with no face at all
    the focus is the centre.

    Focus is what a timeline video layer's `focus` means: the normalized
    source point the crop is centred on. When the crop trims the sides
    (16:9 -> 9:16) the face is centred horizontally; when it trims top and
    bottom (9:16 -> 16:9 or 1:1) the face sits 40 % down the frame, leaving
    headroom, instead of dead centre.

Limits: a focus point per sample, not a moving crop path (autoedit uses one
focus per jump-cut segment); profile faces and faces smaller than about 20
px in the 640 px sample may be missed; fast whip pans and several people
talking over each other need a manual `focus` in the spec.
"""

import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request

import numpy as np

from . import paths, timeline

MODEL_NAME = "face_detection_yunet_2023mar.onnx"
MODEL_SHA256 = "8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4"
MODEL_SIZE = 232589
_ZOO = "opencv/opencv_zoo/main/models/face_detection_yunet/" + MODEL_NAME
# the raw URL serves a Git LFS pointer for this file; the media URL serves the model itself
MODEL_URLS = ["https://raw.githubusercontent.com/" + _ZOO,
              "https://media.githubusercontent.com/media/" + _ZOO]

SAMPLE_WIDTH = 640       # frames are analysed at this width (faces of talking heads are large)
MIN_SCORE = 0.7          # YuNet confidence for a face
MAX_RATE = 4.0           # frames per second decoded at most in the single-pass sampler
JUMP = 0.15              # normalized move between detections that counts as a different shot
EMA = 0.4                # smoothing factor inside a run (higher = follows faster)
HEADROOM = 0.40          # where the face sits vertically when the crop trims top and bottom

_STATE = {"yunet": None, "yunet_path": None, "haar": None, "warned": False}


# --------------------------------------------------------------------------- model

def model_path():
    """Where the YuNet model lives (workspace/models)."""
    return os.path.join(paths.MODELS, MODEL_NAME)


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 16), b""):
            h.update(block)
    return h.hexdigest()


def _valid(path):
    try:
        return os.path.getsize(path) == MODEL_SIZE and _sha256(path) == MODEL_SHA256
    except OSError:
        return False


def ensure_model(download=True, timeout=60):
    """Path to the YuNet face model, downloading it into workspace/models if it is missing.

    Returns None (with a note on stderr) when it cannot be fetched, for
    example offline; callers then fall back to a centre crop. Set
    VSTUDIO_OFFLINE=1 to never download.
    """
    path = model_path()
    if _valid(path):
        return path
    if not download or os.environ.get("VSTUDIO_OFFLINE"):
        return None
    paths.ensure(paths.MODELS)
    errors = []
    for url in MODEL_URLS:
        try:
            with urllib.request.urlopen(url, timeout=timeout) as r:
                data = r.read(MODEL_SIZE * 4)
        except OSError as e:
            errors.append("%s: %s" % (url, e))
            continue
        if data.startswith(b"version https://git-lfs"):
            continue  # a pointer to the real file: the next URL serves it
        if hashlib.sha256(data).hexdigest() != MODEL_SHA256:
            errors.append("%s: the download does not match the published checksum" % url)
            continue
        fd, tmp = tempfile.mkstemp(prefix=".yunet_", dir=paths.MODELS)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
        return path
    print("reframe: could not download the YuNet face model (%s); reframing uses centre crops"
          % ("; ".join(errors) or "no URL served it"), file=sys.stderr)
    return None


def _quiet_cv2():
    import cv2
    try:  # OpenCV 5 logs a backend notice for every new network
        cv2.utils.logging.setLogLevel(cv2.utils.logging.LOG_LEVEL_ERROR)
    except AttributeError:
        pass
    return cv2


def _yunet():
    """A YuNet detector when the model is on disk and OpenCV has FaceDetectorYN, else None."""
    path = model_path()
    if _STATE["yunet"] is not None and _STATE["yunet_path"] == path:
        return _STATE["yunet"]
    cv2 = _quiet_cv2()
    if not hasattr(cv2, "FaceDetectorYN") or not os.path.isfile(path):
        return None
    try:
        det = cv2.FaceDetectorYN.create(path, "", (320, 320), MIN_SCORE, 0.3, 5000)
    except cv2.error as e:
        if not _STATE["warned"]:
            print("reframe: OpenCV could not load %s: %s" % (path, e), file=sys.stderr)
            _STATE["warned"] = True
        return None
    _STATE["yunet"], _STATE["yunet_path"] = det, path
    return det


def _haar():
    """OpenCV's frontal-face Haar cascade when this OpenCV build ships it (OpenCV 5 does not)."""
    if _STATE["haar"] is None:
        cv2 = _quiet_cv2()
        folder = getattr(getattr(cv2, "data", None), "haarcascades", "") or ""
        xml = os.path.join(folder, "haarcascade_frontalface_default.xml")
        cc = cv2.CascadeClassifier(xml) if os.path.isfile(xml) else None
        _STATE["haar"] = cc if cc is not None and not cc.empty() else False
    return _STATE["haar"] or None


def method():
    """'yunet', 'haar' or None: the detector detect_faces() will use right now."""
    if _yunet() is not None:
        return "yunet"
    if _haar() is not None:
        return "haar"
    return None


# --------------------------------------------------------------------------- detection

def detect_faces(frame_bgr):
    """Faces in a BGR frame as (x, y, w, h, score) in frame pixels, best (largest x confidence) first.

    Uses YuNet when its model is present (see ensure_model), else a Haar
    cascade if OpenCV ships one, else returns []. Large frames are analysed
    at 640 px wide and the boxes scaled back.
    """
    if frame_bgr is None or getattr(frame_bgr, "ndim", 0) != 3 or frame_bgr.size == 0:
        return []
    cv2 = _quiet_cv2()
    h, w = frame_bgr.shape[:2]
    k = min(1.0, SAMPLE_WIDTH / float(max(w, h)))
    img = frame_bgr if k >= 1.0 else cv2.resize(frame_bgr, (max(1, int(w * k)), max(1, int(h * k))),
                                               interpolation=cv2.INTER_AREA)
    img = np.ascontiguousarray(img)
    out = []
    det = _yunet()
    if det is not None:
        det.setInputSize((img.shape[1], img.shape[0]))
        _, faces = det.detect(img)
        for f in faces if faces is not None else []:
            out.append((float(f[0]) / k, float(f[1]) / k, float(f[2]) / k, float(f[3]) / k, float(f[14])))
    else:
        cc = _haar()
        if cc is not None:
            gray = cv2.equalizeHist(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY))
            m = max(24, int(min(img.shape[:2]) * 0.08))
            for (x, y, fw, fh) in cc.detectMultiScale(gray, 1.1, 5, minSize=(m, m)):
                out.append((x / k, y / k, fw / k, fh / k, 0.8))
    out = [f for f in out if f[2] > 1 and f[3] > 1]
    out.sort(key=lambda f: f[2] * f[3] * f[4], reverse=True)
    return out


# --------------------------------------------------------------------------- frame sampling

def _ffmpeg():
    exe = shutil.which("ffmpeg")
    if not exe:
        raise RuntimeError("ffmpeg is not installed or not on PATH (https://ffmpeg.org/download.html)")
    return exe


def _read_exact(stream, n):
    buf = bytearray()
    while len(buf) < n:
        chunk = stream.read(n - len(buf))
        if not chunk:
            break
        buf += chunk
    return bytes(buf)


def sample_frames(video, times, width=SAMPLE_WIDTH):
    """{t: BGR frame} for the requested times (each the nearest decoded frame), at `width` px wide.

    Dense times are read in one ffmpeg pass at up to 4 frames per second;
    sparse ones with one seek each. Times past the end may be missing.
    """
    info = timeline.probe(video) or {}
    sw, sh = info.get("width") or 0, info.get("height") or 0
    ts = sorted({round(float(t), 3) for t in times if t is not None and float(t) >= 0})
    if not ts or not sw or not sh:
        return {}
    w = max(2, int(min(width, sw)) // 2 * 2)
    h = max(2, int(round(w * sh / float(sw) / 2.0)) * 2)
    n = w * h * 3
    ff = _ffmpeg()
    base = [ff, "-nostdin", "-hide_banner", "-loglevel", "error"]
    tail = ["-an", "-sn", "-dn", "-pix_fmt", "bgr24", "-f", "rawvideo", "-"]
    gaps = [b - a for a, b in zip(ts, ts[1:]) if b - a > 1e-3]
    rate = min(MAX_RATE, max(2.0, 1.0 / max(min(gaps) if gaps else 1.0, 1.0 / MAX_RATE)))
    span = ts[-1] - ts[0]
    out = {}
    if span * rate + 1 > 4 * len(ts) + 40:
        # sparse: one seek per time costs less than decoding everything in between
        for t in ts:
            r = subprocess.run(base + ["-ss", "%.3f" % t, "-i", video, "-frames:v", "1", "-vf",
                                       "scale=%d:%d" % (w, h)] + tail, capture_output=True)
            if len(r.stdout) >= n:
                out[t] = np.frombuffer(r.stdout[:n], np.uint8).reshape(h, w, 3)
        return out
    # the fps filter's frame k shows source time seek + (k + 0.5) / rate, so seek half a frame early
    seek = max(0.0, ts[0] - 0.5 / rate)
    want = {}
    for t in ts:
        want.setdefault(max(0, int(round((t - seek) * rate - 0.5))), []).append(t)
    last = max(want)
    cmd = base + ["-ss", "%.3f" % seek, "-i", video, "-t", "%.3f" % (span + 2.0 / rate),
                  "-vf", "fps=%r,scale=%d:%d" % (rate, w, h)] + tail
    proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        k = 0
        prev = None
        while k <= last:
            buf = _read_exact(proc.stdout, n)
            if len(buf) < n:
                break
            prev = np.frombuffer(buf, np.uint8).reshape(h, w, 3)
            for t in want.get(k, ()):
                out[t] = prev
            k += 1
        if prev is not None:   # times just past the last decoded frame take the last frame
            for kk, tt in want.items():
                if kk >= k and kk <= k + 1:
                    for t in tt:
                        out.setdefault(t, prev)
    finally:
        proc.kill()
        proc.stdout.close()
        proc.wait()
    return out


# --------------------------------------------------------------------------- tracking

def _pick(faces, W, H, prev):
    """The face to follow among detections (normalized cx, cy, w, h), or None."""
    if not faces:
        return None
    top = faces[0]
    best = top
    if prev is not None:
        big = [f for f in faces if f[2] * f[3] >= 0.6 * top[2] * top[3]]
        best = min(big, key=lambda f: (f[0] + f[2] / 2.0 - prev[0] * W) ** 2 + (f[1] + f[3] / 2.0 - prev[1] * H) ** 2)
    x, y, fw, fh, _ = best
    return ((x + fw / 2.0) / W, (y + fh / 2.0) / H, fw / W, fh / H)


def _median5(v):
    v = np.asarray(v, float)
    if len(v) < 3:
        return v.copy()
    pad = np.pad(v, 2, mode="edge")
    return np.median(np.stack([pad[i:i + len(v)] for i in range(5)]), axis=0)


def _ema2(v, a=EMA):
    """Zero-phase exponential smoothing: forward, then backward over the result (like filtfilt).

    The ends are padded with an odd reflection so a face moving steadily is
    not dragged toward the middle of the run at its first and last samples.
    """
    v = np.asarray(v, float)
    if len(v) < 3:
        return v.copy()
    p = min(len(v) - 1, 8)
    x = np.concatenate([2 * v[0] - v[p:0:-1], v, 2 * v[-1] - v[-2:-p - 2:-1]])
    for i in range(1, len(x)):
        x[i] = a * x[i] + (1 - a) * x[i - 1]
    for i in range(len(x) - 2, -1, -1):
        x[i] = a * x[i] + (1 - a) * x[i + 1]
    return x[p:p + len(v)]


def smooth(raw, times):
    """Smoothed (cx, cy, w, h) per sample from raw detections (None = no face); None when nothing was found.

    Returns (values, run_id) lists aligned with `times`.
    """
    obs = [i for i, r in enumerate(raw) if r is not None]
    if not obs:
        return None
    arr = np.array([raw[i] for i in obs], float)
    cx, cy = _median5(arr[:, 0]), _median5(arr[:, 1])
    # runs: a jump in the median-filtered track is a different shot (or a different person)
    run = [0]
    for j in range(1, len(obs)):
        jump = abs(cx[j] - cx[j - 1]) > JUMP or abs(cy[j] - cy[j - 1]) > JUMP
        run.append(run[-1] + 1 if jump else run[-1])
    run = np.array(run)
    sm = np.zeros((len(obs), 4))
    for r in np.unique(run):
        m = run == r
        sm[m, 0] = _ema2(cx[m])
        sm[m, 1] = _ema2(cy[m])
        sm[m, 2] = np.median(arr[m, 2])
        sm[m, 3] = np.median(arr[m, 3])
    values, runs = [None] * len(raw), [None] * len(raw)
    for j, i in enumerate(obs):
        values[i], runs[i] = tuple(sm[j]), int(run[j])
    # gaps: interpolate between two detections of the same run, else take the nearest detection in time
    for i in range(len(raw)):
        if values[i] is not None:
            continue
        k = int(np.searchsorted(obs, i))
        lo = obs[k - 1] if k > 0 else None
        hi = obs[k] if k < len(obs) else None
        if lo is not None and hi is not None and runs[lo] == runs[hi]:
            span = times[hi] - times[lo]
            q = (times[i] - times[lo]) / span if span > 0 else 0.0
            values[i] = tuple(np.array(values[lo]) + q * (np.array(values[hi]) - np.array(values[lo])))
            runs[i] = runs[lo]
        else:
            near = lo if hi is None or (lo is not None and times[i] - times[lo] <= times[hi] - times[i]) else hi
            values[i], runs[i] = values[near], runs[near]
    return values, runs


def focus_for(cx, cy, src_size, aspect):
    """The layer `focus` that frames a face centred at (cx, cy) (normalized) for a crop to `aspect`."""
    sw, sh = src_size
    a_s = sw / float(sh)
    a_t = aspect[0] / float(aspect[1])
    if a_s > a_t * 1.01:            # the crop trims the sides: centre the face horizontally
        return [round(min(1.0, max(0.0, float(cx))), 4), 0.5]
    if a_s < a_t / 1.01:            # the crop trims top and bottom: keep headroom above the face
        v = a_s / a_t               # visible share of the source height
        return [0.5, round(min(1.0, max(0.0, float(cy) + (0.5 - HEADROOM) * v)), 4)]
    return [0.5, 0.5]


def track(video, times, aspect=(9, 16), download=True):
    """Face track at `times` (seconds): {"points", "method", "detector", "found", "samples"}.

    points[i] = {"t", "focus": [fx, fy], "face": [x, y, w, h] normalized or
    None, "detected": bool (a face was seen in that very frame)}; method is
    "yunet" or "haar" when faces were found, else "centre"; detector is the
    detector that ran (None when neither YuNet nor a Haar cascade exists).
    download=False never fetches the YuNet model.
    """
    info = timeline.probe(video)
    if not info or not info.get("has_video"):
        raise ValueError("%s has no video stream" % video)
    src = (info["width"], info["height"])
    times = [float(t) for t in times]
    if download:
        ensure_model()
    how = method()
    raw = [None] * len(times)
    if how and times:
        frames = sample_frames(video, times)
        prev = None
        for i, t in enumerate(times):
            fr = frames.get(round(t, 3))
            if fr is None:
                continue
            raw[i] = _pick(detect_faces(fr), fr.shape[1], fr.shape[0], prev)
            if raw[i] is not None:
                prev = raw[i]
    found = sum(1 for r in raw if r is not None)
    sm = smooth(raw, times) if found else None
    points = []
    for i, t in enumerate(times):
        if sm is None:
            points.append({"t": t, "focus": focus_for(0.5, 0.5, src, aspect), "face": None, "detected": False})
            continue
        cx, cy, fw, fh = (float(v) for v in sm[0][i])
        points.append({"t": t, "focus": focus_for(cx, cy, src, aspect),
                       "face": [round(cx - fw / 2, 4), round(cy - fh / 2, 4), round(fw, 4), round(fh, 4)],
                       "detected": raw[i] is not None})
    return {"points": points, "method": how if found else "centre", "found": found, "samples": len(times),
            "detector": how}


def track_focus(video, times, aspect=(9, 16)):
    """Smoothed, normalized focus points [[fx, fy], ...] for `times`; the centre where no face is found."""
    return [p["focus"] for p in track(video, times, aspect)["points"]]
