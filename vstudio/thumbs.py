"""
thumbs.py - read a thumbnail the way a packaging strategist would, and tile
the best ones onto a labelled contact sheet.

    analyze_thumb(path) -> dict        text, colour, light, busyness, faces, composition
    sheet(paths, out_path, labels=None) -> str

Everything is measured from pixels on a copy 640 px wide, after trimming the
black bars YouTube pads 4:3 thumbnails (hqdefault.jpg) with:

  text         OCR (rapidocr, local): the words, how many, the share of the
               frame covered by text, where the text sits on a 3x3 grid, the
               tallest line as % of the height, and the main block's fill,
               outline and background colours (a 3-colour split, approximate)
  palette      five dominant colours (k-means) with their share, and the
               dominant hue family (red, yellow, blue, ... or neutral)
  light        brightness and contrast (mean and spread of luma, 0-1),
               saturation (mean HSV S, 0-1), colourfulness (Hasler and
               Suesstrunk 2003: under 15 is grey, over 60 is vivid)
  busyness     Canny edge density and a 0-100 busyness score from edges plus
               colour entropy (a clean thumbnail scores under ~40)
  faces        count, the largest face as a share of the frame and where it
               sits; needs the YuNet face model (workspace/models, fetched by
               vstudio.reframe.ensure_model) and an OpenCV that can run it
               (4.8+). Without one faces are null: a missing or unusable
               detector is reported, never guessed.
  composition  where the visual weight sits (gradient-energy saliency):
               centre of mass, left / centre / right thirds, and the subject
               side (the largest face when there is one)

Limits: arrows, circles and facial expressions are not detected; stylised,
rotated or very small text can escape the OCR; "busy" and "vivid" are
measurements, not taste. Look at the sheet before concluding.
"""

import glob
import math
import os
import statistics
import sys

from . import paths

WORK_WIDTH = 640           # every measurement runs on a copy this wide
OCR_MIN_CONF = 0.6
FACE_MIN_SCORE = 0.7
FACE_MODEL_GLOBS = ("*yunet*.onnx", "face_detection*.onnx")

_ocr = None                # lazily created RapidOCR engine (False = not installed)
_face = {}                 # model path -> (cv2.FaceDetectorYN or None, why it is None)
_face_note = [None]        # why the last face_detector() call returned None (None after a success)


# --------------------------------------------------------------------------- loading

def load_rgb(path, width=WORK_WIDTH, trim=True):
    """The image as an RGB uint8 array `width` px wide (aspect kept), letterbox bars trimmed."""
    import numpy as np
    from PIL import Image
    with Image.open(path) as im:
        im = im.convert("RGB")
        rgb = np.asarray(im)
    if trim:
        rgb = trim_bars(rgb)
    h, w = rgb.shape[:2]
    if width and w != width:
        import cv2
        nh = max(1, int(round(h * width / float(w))))
        interp = cv2.INTER_AREA if width < w else cv2.INTER_CUBIC
        rgb = cv2.resize(rgb, (width, nh), interpolation=interp)
    return np.ascontiguousarray(rgb)


def trim_bars(rgb, level=18, max_share=0.3):
    """Crop the black padding YouTube adds to its 4:3 thumbnail sizes (hqdefault 480x360, sddefault
    640x480): letterbox bars above and below a 16:9 picture, pillarbox bars beside a vertical one.

    Only a 4:3 image is ever trimmed, and only bars that are symmetric (padding is): a 16:9 thumbnail
    (maxresdefault) with a dark design keeps its dark edges, which are part of what it looks like.
    Where the dark design runs on past the padding, exactly the padding a 16:9 (or 9:16) picture
    needs is cut, never the design."""
    import numpy as np
    lum = rgb.mean(axis=2)
    h, w = lum.shape
    if not h or not w or abs(w / float(h) - 4 / 3.0) > 0.04:
        return np.ascontiguousarray(rgb)

    def edge(lines, limit):
        n = 0
        for line in lines:
            if line.max() > level + 12 or line.mean() > level:
                break
            n += 1
            if n >= limit:
                break
        return n

    def pair(a, b, size, expected):
        """(cut_a, cut_b) for one axis: the expected padding when both bars reach it, else
        symmetric bars covering at least 4% of the axis, else nothing."""
        if expected > 0 and a >= expected - 2 and b >= expected - 2:
            return expected, expected
        if a + b >= size * 0.04 and abs(a - b) <= max(2, 0.02 * size):
            return min(a, b), min(a, b)
        return 0, 0

    top = edge(lum, int(h * max_share))
    bottom = edge(lum[::-1], int(h * max_share))
    top, bottom = pair(top, bottom, h, int(round((h - w * 9 / 16.0) / 2)))
    left = right = 0
    if not (top or bottom):
        left = edge(lum.T, int(w * 0.45))
        right = edge(lum.T[::-1], int(w * 0.45))
        left, right = pair(left, right, w, int(round((w - h * 9 / 16.0) / 2)))
    if top or bottom or left or right:
        rgb = rgb[top:h - bottom, left:w - right]
    return np.ascontiguousarray(rgb)


# --------------------------------------------------------------------------- colour and light

def colorfulness(rgb):
    """Hasler and Suesstrunk (2003) colourfulness: ~0 grey, 15-33 moderate, 45-59 high, 60+ vivid."""
    import numpy as np
    px = rgb.reshape(-1, 3).astype("float32")
    rg = px[:, 0] - px[:, 1]
    yb = 0.5 * (px[:, 0] + px[:, 1]) - px[:, 2]
    return float(np.sqrt(rg.std() ** 2 + yb.std() ** 2) + 0.3 * np.sqrt(rg.mean() ** 2 + yb.mean() ** 2))


def palette(rgb, k=5, sample=6000):
    """Dominant colours by k-means: [{"hex", "share", "family"}], largest first; near-duplicates merged."""
    import numpy as np
    import cv2
    px = rgb.reshape(-1, 3)
    step = max(1, len(px) // sample)
    data = px[::step].astype("float32")
    k = max(1, min(k, len(data)))
    crit = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 25, 1.0)
    cv2.setRNGSeed(7)  # same image -> same palette
    _, lab, cen = cv2.kmeans(data, k, None, crit, 3, cv2.KMEANS_PP_CENTERS)
    share = np.bincount(lab.ravel(), minlength=k) / float(len(lab))
    cols = sorted(zip(share.tolist(), cen.tolist()), key=lambda x: -x[0])
    merged = []
    for s, c in cols:
        for m in merged:
            if math.dist(m[1], c) < 20:
                m[0] += s
                break
        else:
            merged.append([s, c])
    return [{"hex": _hex(c), "share": round(s, 3), "family": hue_family(c)} for s, c in merged]


def hue_family(c):
    """A colour's family name: black / white / grey for unsaturated colours, else red .. pink."""
    import colorsys
    r, g, b = [max(0.0, min(255.0, float(v))) / 255.0 for v in c[:3]]
    h, s, v = colorsys.rgb_to_hsv(r, g, b)
    if v < 0.18:
        return "black"
    if s < 0.22:
        return "white" if v > 0.82 else "grey"
    deg = h * 360
    for name, top in (("red", 15), ("orange", 40), ("yellow", 70), ("green", 160), ("cyan", 200),
                      ("blue", 255), ("purple", 290), ("pink", 345), ("red", 361)):
        if deg < top:
            return name
    return "red"


def dominant_hue(pal):
    """The biggest saturated colour family holding at least 8% of the frame, else the biggest neutral one."""
    for p in pal:
        if p["family"] not in ("black", "white", "grey") and p["share"] >= 0.08:
            return p["family"]
    return pal[0]["family"] if pal else None


def _hex(c):
    return "#%02X%02X%02X" % tuple(int(max(0, min(255, round(v)))) for v in c[:3])


def light(rgb):
    """Brightness, contrast (luma mean / std, 0-1) and saturation (mean HSV S, 0-1)."""
    import cv2
    luma = (0.2126 * rgb[..., 0] + 0.7152 * rgb[..., 1] + 0.0722 * rgb[..., 2]) / 255.0
    hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)
    return {"brightness": round(float(luma.mean()), 3), "contrast": round(float(luma.std()), 3),
            "saturation": round(float(hsv[..., 1].mean()) / 255.0, 3)}


def edge_density(gray):
    """Share of pixels on a Canny edge (0-1). Clean studio thumbnails sit near 0.04, cluttered ones above 0.15."""
    import cv2
    return float((cv2.Canny(gray, 80, 180) > 0).mean())


def busyness(rgb, edges):
    """0-100: 60% edge density (0.03 -> 0, 0.20 -> 1) + 40% colour entropy over 64 colour bins."""
    import numpy as np
    q = (rgb // 64).reshape(-1, 3).astype("int32")
    idx = q[:, 0] * 16 + q[:, 1] * 4 + q[:, 2]
    p = np.bincount(idx, minlength=64) / float(len(idx))
    p = p[p > 0]
    entropy = float(-(p * np.log(p)).sum() / math.log(64))
    e = min(1.0, max(0.0, (edges - 0.03) / 0.17))
    return round(100 * (0.6 * e + 0.4 * entropy))


# --------------------------------------------------------------------------- composition

def saliency(gray):
    """Where the visual weight sits, from gradient energy: centre of mass and the left/centre/right thirds."""
    import numpy as np
    import cv2
    g = cv2.GaussianBlur(gray.astype("float32"), (5, 5), 0)
    mag = np.hypot(cv2.Sobel(g, cv2.CV_32F, 1, 0), cv2.Sobel(g, cv2.CV_32F, 0, 1))
    sal = cv2.GaussianBlur(mag, (0, 0), max(2.0, gray.shape[1] / 40.0))
    total = float(sal.sum())
    h, w = gray.shape
    if total <= 1e-6:
        return {"x": 0.5, "y": 0.5, "thirds": [0.33, 0.34, 0.33], "weight": "centre"}
    cols = sal.sum(axis=0)
    rows = sal.sum(axis=1)
    cx = float((cols * np.arange(w)).sum() / total) / max(1, w - 1)
    cy = float((rows * np.arange(h)).sum() / total) / max(1, h - 1)
    a, b = w // 3, 2 * w // 3
    thirds = [float(cols[:a].sum() / total), float(cols[a:b].sum() / total), float(cols[b:].sum() / total)]
    return {"x": round(cx, 3), "y": round(cy, 3), "thirds": [round(t, 3) for t in thirds], "weight": side(cx)}


def side(x):
    """left / centre / right for a normalised x."""
    return "left" if x < 0.42 else "right" if x > 0.58 else "centre"


def grid_cell(x, y):
    """3x3 position name for a normalised point: top-left ... centre ... bottom-right."""
    col = "left" if x < 1 / 3.0 else "right" if x > 2 / 3.0 else "centre"
    row = "top" if y < 1 / 3.0 else "bottom" if y > 2 / 3.0 else "middle"
    if row == "middle":
        return col if col != "centre" else "centre"
    return row if col == "centre" else "%s-%s" % (row, col)


# --------------------------------------------------------------------------- text (OCR)

def ocr_engine():
    """The shared RapidOCR engine, or None when rapidocr-onnxruntime is not installed."""
    global _ocr
    if _ocr is None:
        try:
            from rapidocr_onnxruntime import RapidOCR
            _ocr = RapidOCR()
        except Exception:  # not installed, or its models failed to load
            _ocr = False
    return _ocr or None


def read_text(rgb):
    """OCR one image: {"text", "words", "chars", "lines", "area_share", "position", "max_line_pct", "style"}.

    None when OCR is not installed. Boxes come from viral-extract's ocr_frame / group_lines; `style`
    is the main block's {"fill", "outline", "behind", "case"} (text_colors), None when too small.
    """
    import numpy as np
    engine = ocr_engine()
    if engine is None:
        return None
    extract = paths.load_skill("extract")
    boxes = extract.ocr_frame(engine, rgb, min_conf=OCR_MIN_CONF)
    h, w = rgb.shape[:2]
    empty = {"text": "", "words": 0, "chars": 0, "lines": 0, "area_share": 0.0, "position": "none",
             "max_line_pct": 0.0, "style": None}
    if not boxes:
        return empty
    mask = np.zeros((h, w), bool)
    for b in boxes:
        mask[int(max(0, b["y0"])):int(min(h, b["y1"])), int(max(0, b["x0"])):int(min(w, b["x1"]))] = True
    area = sum((b["x1"] - b["x0"]) * (b["y1"] - b["y0"]) for b in boxes) or 1.0
    cx = sum((b["x0"] + b["x1"]) / 2.0 * (b["x1"] - b["x0"]) * (b["y1"] - b["y0"]) for b in boxes) / area / w
    cy = sum((b["y0"] + b["y1"]) / 2.0 * (b["x1"] - b["x0"]) * (b["y1"] - b["y0"]) for b in boxes) / area / h
    ordered = sorted(boxes, key=lambda b: (round(b["y0"] / max(1.0, h / 12.0)), b["x0"]))
    text = " ".join(b["text"] for b in ordered)
    blocks = extract.group_lines(boxes)
    main = max(blocks, key=lambda blk: sum((b["x1"] - b["x0"]) * (b["y1"] - b["y0"]) for b in blk))
    style = text_colors(rgb, main)
    if style is not None:
        style["case"] = extract.case_style(text)
    return {
        "text": text,
        "words": len([t for t in text.split() if any(ch.isalnum() for ch in t)]),
        "chars": len(text.replace(" ", "")),
        "lines": len(boxes),
        "area_share": round(float(mask.mean()), 3),
        "position": grid_cell(cx, cy),
        "max_line_pct": round(100.0 * max(b["y1"] - b["y0"] for b in boxes) / h, 1),
        "style": style,
    }


def text_colors(rgb, boxes):
    """Fill, outline and the colour behind one text block: {"fill", "outline", "behind"} or None.

    Thumbnail text is usually three colours (fill, a thick stroke, the picture), so the block's
    box is split into 3 colour classes: the class owning most of the box border is what lies
    behind the text; of the other two, the one that rarely touches the background is the fill
    and the one wrapped around it is the outline. Without a clear outline the fill is the class
    that contrasts most with the background. An approximation: check it on the sheet.
    """
    import numpy as np
    import cv2
    h, w = rgb.shape[:2]
    x0, y0 = int(max(0, min(b["x0"] for b in boxes))), int(max(0, min(b["y0"] for b in boxes)))
    x1, y1 = int(min(w, max(b["x1"] for b in boxes))), int(min(h, max(b["y1"] for b in boxes)))
    region = rgb[y0:y1, x0:x1]
    if region.shape[0] < 6 or region.shape[1] < 6:
        return None
    data = region.reshape(-1, 3).astype("float32")
    crit = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 20, 1.0)
    cv2.setRNGSeed(7)
    _, lab, cen = cv2.kmeans(data, 3, None, crit, 3, cv2.KMEANS_PP_CENTERS)
    lab = lab.reshape(region.shape[:2])
    share = np.bincount(lab.ravel(), minlength=3) / float(lab.size)
    border = np.concatenate([lab[0, :], lab[-1, :], lab[:, 0], lab[:, -1]])
    bg = int(np.argmax(np.bincount(border, minlength=3)))
    a, b = sorted((c for c in range(3) if c != bg), key=lambda c: -share[c])
    bg_near = cv2.dilate((lab == bg).astype("uint8"), np.ones((3, 3), "uint8")) > 0

    def touch(c):
        m = lab == c
        return float((m & bg_near).sum()) / max(1, int(m.sum()))

    fill, outline = a, None
    if share[b] >= 0.06 and math.dist(cen[a], cen[b]) > 60:
        ta, tb = touch(a), touch(b)
        if max(ta, tb) > 1.6 * min(ta, tb):
            fill, outline = (a, b) if ta < tb else (b, a)
        else:
            fill = max((a, b), key=lambda c: math.dist(cen[c], cen[bg]))
    return {"fill": _hex(cen[fill]), "outline": _hex(cen[outline]) if outline is not None else None,
            "behind": _hex(cen[bg])}


# --------------------------------------------------------------------------- faces

def face_model_path(allow_download=False):
    """Path to a YuNet face model: $VSTUDIO_FACE_MODEL, then workspace/models, then (when allowed)
    vstudio.reframe.ensure_model(), which downloads it once. None when there is none."""
    env = os.environ.get("VSTUDIO_FACE_MODEL")
    if env and os.path.isfile(env):
        return env
    for pattern in FACE_MODEL_GLOBS:
        hits = sorted(glob.glob(os.path.join(paths.MODELS, pattern)))
        if hits:
            return hits[-1]
    if allow_download:
        try:
            from . import reframe
            p = reframe.ensure_model()
            if p and os.path.isfile(p):
                return p
        except Exception as e:  # reframe missing or offline: faces are reported as not measured
            print("face model unavailable: %s" % str(e)[:120], file=sys.stderr)
    return None


NO_FACE_MODEL = ("no face model (python -m vstudio autoedit or reframe.ensure_model() downloads YuNet into "
                 "workspace/models)")


def face_detector(allow_download=False):
    """A working cv2.FaceDetectorYN for the YuNet model, or None: no model, OpenCV without
    FaceDetectorYN, or an OpenCV that loads the model but cannot run it (YuNet 2023mar needs
    OpenCV 4.8+; 4.5.4-4.7 raise on every detect). face_detector_note() says which."""
    p = face_model_path(allow_download)
    if not p:
        _face_note[0] = NO_FACE_MODEL
        return None
    if p not in _face:
        _face[p] = _load_detector(p)
    det, _face_note[0] = _face[p]
    return det


def face_detector_note():
    """Why the last face_detector() call returned None, in words for a report (None after a success)."""
    return _face_note[0]


def _load_detector(p):
    """(detector, None) or (None, reason). Creating a detector can succeed where running it fails,
    so one detect on a small blank image proves it works before any thumbnail or video relies on it."""
    try:
        import numpy as np
        import cv2
    except ImportError as e:
        return None, "OpenCV is not installed (%s)" % e
    version = getattr(cv2, "__version__", "?")
    if not hasattr(cv2, "FaceDetectorYN"):
        return None, "OpenCV %s has no FaceDetectorYN (python -m pip install -U opencv-python-headless)" % version
    try:
        det = _quiet_cv(lambda: cv2.FaceDetectorYN.create(p, "", (320, 320), FACE_MIN_SCORE, 0.3, 5000))
        det.setInputSize((64, 64))
        _quiet_cv(lambda: det.detect(np.zeros((64, 64, 3), "uint8")))
    except Exception as e:  # cv2.error: e.g. 'findCommonShape' on OpenCV < 4.8 with YuNet 2023mar
        msg = str(e).strip().splitlines()
        print("face detector unusable (%s): %s" % (os.path.basename(p), (msg[-1] if msg else "")[:160]),
              file=sys.stderr)
        return None, ("OpenCV %s cannot run the face model %s (YuNet 2023mar needs OpenCV 4.8 or newer: "
                      "python -m pip install -U opencv-python-headless)" % (version, os.path.basename(p)))
    return det, None


def _quiet_cv(fn):
    """Run fn with OpenCV's log level at ERROR (its DNN backend prints a harmless warning per model load)."""
    import cv2
    log = getattr(getattr(cv2, "utils", None), "logging", None)
    if log is None:
        return fn()
    old = log.getLogLevel()
    log.setLogLevel(log.LOG_LEVEL_ERROR)
    try:
        return fn()
    finally:
        log.setLogLevel(old)


def detect_faces(bgr, detector=None, min_score=FACE_MIN_SCORE):
    """Faces in a BGR image as [(x, y, w, h, score)] in its pixels, biggest first. [] without a detector."""
    det = detector if detector is not None else face_detector()
    if det is None:
        return []
    h, w = bgr.shape[:2]
    det.setInputSize((w, h))
    _, faces = det.detect(bgr)
    out = []
    for f in faces if faces is not None else []:
        x, y, fw, fh, score = float(f[0]), float(f[1]), float(f[2]), float(f[3]), float(f[-1])
        if score >= min_score and fw > 0 and fh > 0:
            out.append((x, y, fw, fh, score))
    return sorted(out, key=lambda f: -(f[2] * f[3]))


def faces_summary(rgb, detector):
    """{"count", "largest_share", "position", "x", "y"} for an RGB image, or None without a detector
    (or when the detector fails on this image: that costs the faces field, never the thumbnail)."""
    import cv2
    if detector is None:
        return None
    h, w = rgb.shape[:2]
    try:
        found = detect_faces(cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR), detector)
    except Exception as e:  # cv2.error inside the DNN: faces not measured for this image
        print("face detection failed: %s" % (str(e).strip().splitlines() or [""])[-1][:120], file=sys.stderr)
        return None
    if not found:
        return {"count": 0, "largest_share": 0.0, "position": None, "x": None, "y": None}
    x, y, fw, fh, _ = found[0]
    fx, fy = (x + fw / 2.0) / w, (y + fh / 2.0) / h
    return {"count": len(found), "largest_share": round(fw * fh / float(w * h), 3),
            "position": grid_cell(fx, fy), "x": round(fx, 3), "y": round(fy, 3)}


# --------------------------------------------------------------------------- one thumbnail

def analyze_thumb(path, ocr=True, faces=True, detector=None):
    """Measure one thumbnail. Returns a JSON-ready dict (see the module docstring for every field).

    `faces=True` uses `detector` or the shared YuNet detector when its model is present;
    the "faces" field is None when no detector exists. `ocr=False` skips the text read.
    """
    import cv2
    rgb = load_rgb(path)
    h, w = rgb.shape[:2]
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    pal = palette(rgb)
    edges = edge_density(gray)
    out = {"path": path, "width": w, "height": h, "aspect": round(w / float(h), 3)}
    out.update(light(rgb))
    out["colorfulness"] = round(colorfulness(rgb), 1)
    out["edge_density"] = round(edges, 4)
    out["busyness"] = busyness(rgb, edges)
    out["palette"] = pal
    out["dominant_hue"] = dominant_hue(pal)
    out["text"] = read_text(rgb) if ocr else None
    det = detector if detector is not None else (face_detector() if faces else None)
    out["faces"] = faces_summary(rgb, det) if faces else None
    comp = saliency(gray)
    subject = comp["weight"]
    if out["faces"] and out["faces"]["count"]:
        subject = side(out["faces"]["x"])
    comp["subject"] = subject
    out["composition"] = comp
    return out


# --------------------------------------------------------------------------- contact sheet

def _font(size, weight="SemiBold"):
    try:
        from . import fonts
        return fonts.load("Inter %s" % weight, size)
    except Exception:
        from PIL import ImageFont
        return ImageFont.load_default()


def _runs(text, size, weight):
    """[[segment, font]] that draw `text` with Inter and, for characters Inter has no glyph for
    (Devanagari, CJK, Arabic, symbols...), an installed font that has them (fonts.glyph_fallbacks:
    Nirmala UI / Yu Gothic on Windows, Kohinoor / Hiragino on macOS, Noto on Linux). Characters no
    installed font can draw (colour emoji) are left out rather than drawn as boxes."""
    primary = _font(size, weight)
    try:
        from . import fonts
        found, nobody = fonts.glyph_fallbacks("Inter %s" % weight, text)
    except Exception:  # no font index: Inter (or Pillow's default) alone
        return [[text, primary]]
    if not found and not nobody:
        return [[text, primary]]
    faces, runs = {}, []
    for ch in text:
        if ch in nobody:
            continue
        e = found.get(ch)
        if e is not None:
            key = (e["path"], e.get("index", 0), e.get("want_weight"))
            if key not in faces:
                try:
                    faces[key] = fonts.load_face(e, size)
                except Exception:  # unloadable fallback: Inter's box beats no text
                    faces[key] = primary
            f = faces[key]
        elif runs and not fonts.needs_glyph(ch):
            f = runs[-1][1]   # marks, joiners, spaces: the font of the character before them
        else:
            f = primary
        if runs and runs[-1][1] is f:
            runs[-1][0] += ch
        else:
            runs.append([ch, f])
    return runs


def _draw_label(draw, xy, text, size, weight, fill, width):
    """Draw one label line in up to `width` px (ellipsized), with per-script fallback fonts."""
    runs = _runs(text, size, weight)
    total = lambda rs: sum(draw.textlength(t, font=f) for t, f in rs)
    if total(runs) > width:
        dots = draw.textlength("...", font=runs[-1][1] if runs else _font(size, weight))
        while runs and total(runs) + dots > width:
            runs[-1][0] = runs[-1][0][:-1]
            if not runs[-1][0]:
                runs.pop()
        if runs:
            runs[-1][0] = runs[-1][0].rstrip() + "..."
    x, y = xy
    for t, f in runs:
        draw.text((x, y), t, font=f, fill=fill)
        x += draw.textlength(t, font=f)


def sheet(paths, out_path, labels=None, cols=4, tile_w=320, title=None):
    """Tile images (fit inside equal tiles, aspect kept) with a label strip under each; save as JPEG.

    `labels` is a list of strings (one per image; "\\n" separates lines, the first line is drawn
    bold). Missing or unreadable images are skipped. Returns out_path, or "" when nothing could
    be drawn.
    """
    from PIL import Image, ImageDraw
    items = []
    for i, p in enumerate(paths or []):
        try:
            with Image.open(p) as im:
                items.append((im.convert("RGB"), (labels[i] if labels and i < len(labels) else "") or ""))
        except Exception:
            continue
    if not items:
        return ""
    aspects = [im.height / float(im.width) for im, _ in items]
    tile_h = int(round(tile_w * min(1.8, max(0.5, statistics.median(aspects)))))
    k = tile_w / 320.0
    pad = int(round(10 * k))
    s_bold, s_reg = int(round(15 * k)), int(round(13 * k))
    n_lines = max(len(lbl.split("\n")) if lbl else 0 for _, lbl in items)
    line_h = int(round(19 * k))
    label_h = pad // 2 + n_lines * line_h + pad // 2 if n_lines else 0
    cols = max(1, min(cols, len(items)))
    rows = -(-len(items) // cols)
    head = int(round(44 * k)) if title else 0
    W = pad + cols * (tile_w + pad)
    H = head + pad + rows * (tile_h + label_h + pad)
    canvas = Image.new("RGB", (W, H), (22, 22, 28))
    draw = ImageDraw.Draw(canvas)
    if title:
        _draw_label(draw, (pad, pad), title, int(round(20 * k)), "Bold", (245, 245, 245), W - 2 * pad)
    for i, (im, lbl) in enumerate(items):
        r, c = divmod(i, cols)
        x0 = pad + c * (tile_w + pad)
        y0 = head + pad + r * (tile_h + label_h + pad)
        draw.rectangle([x0, y0, x0 + tile_w - 1, y0 + tile_h - 1], fill=(10, 10, 14))
        s = min(tile_w / float(im.width), tile_h / float(im.height))
        tw, th = max(1, int(round(im.width * s))), max(1, int(round(im.height * s)))
        canvas.paste(im.resize((tw, th), Image.LANCZOS), (x0 + (tile_w - tw) // 2, y0 + (tile_h - th) // 2))
        for j, line in enumerate(lbl.split("\n")[:n_lines] if lbl else []):
            _draw_label(draw, (x0, y0 + tile_h + pad // 2 + j * line_h), line, s_bold if j == 0 else s_reg,
                        "Bold" if j == 0 else "Regular", (255, 229, 0) if j == 0 else (215, 215, 222), tile_w)
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    canvas.save(out_path, "JPEG", quality=88)
    return out_path
