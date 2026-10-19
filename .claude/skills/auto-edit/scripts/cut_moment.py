#!/usr/bin/env python3
"""
cut_moment.py - cut one moment out of a long video for auto-edit, with its words.

    python3 .claude/skills/auto-edit/scripts/cut_moment.py long.mp4 --start 12:34.2 --end 13:21.9 \
        --transcript long.srt --out workspace/edits/<slug>/moments/1-hook.mp4

The clip is re-encoded after an input-side seek (ffmpeg -ss before -i), so it
starts on the asked frame with picture and sound together. A stream copy
(-c copy) snaps to keyframes: the picture can start seconds late, out of sync
with the sound.

With --transcript (the long video's words: autoedit's or extract.py's
transcript.json, any words JSON, SRT, VTT or json3) the words spoken inside
the moment are written, shifted to the clip's own clock, to <out>.words.json
next to the clip. `python -m vstudio autoedit` picks that file up by itself,
so the clip gets cuts, captions and graphics without transcribing again.

--start / --end take seconds (754.2) or timecodes (12:34.2, 1:02:03).
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(HERE)), "viral-extract", "scripts"))
import extract  # noqa: E402


def seconds(text):
    """'754.2', '12:34.2' or '1:02:03' -> seconds."""
    m = re.fullmatch(r"\s*(?:(\d+):)?(?:(\d+):)?(\d+(?:[.,]\d+)?)\s*", str(text))
    if not m:
        raise argparse.ArgumentTypeError("give seconds (754.2) or a timecode (12:34.2)")
    parts = [p for p in m.groups()[:2] if p is not None]
    total = float(m.group(3).replace(",", "."))
    for i, p in enumerate(reversed(parts)):
        total += int(p) * 60 ** (i + 1)
    return total


def moment_words(words, start, end):
    """The words whose middle falls inside [start, end), on the clip's clock (0 = start)."""
    out = []
    for w in words:
        s, e = float(w["s"]), float(w["e"])
        if start <= (s + e) / 2.0 < end:
            out.append({"w": w["w"], "s": round(max(0.0, s - start), 3), "e": round(max(0.0, min(e, end) - start), 3)})
    return out


def cut(video, start, end, out):
    """Re-encode video[start:end] to out (H.264 + AAC, the first video and audio stream). Returns ffmpeg's code."""
    cmd = ["ffmpeg", "-v", "error", "-y", "-ss", "%.3f" % start, "-i", video, "-t", "%.3f" % (end - start),
           "-map", "0:v:0", "-map", "0:a:0?", "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
           "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", out]
    return subprocess.run(cmd).returncode


def sidecar_path(out):
    return os.path.splitext(out)[0] + ".words.json"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("video", help="the long video")
    ap.add_argument("--start", type=seconds, required=True, help="where the moment starts")
    ap.add_argument("--end", type=seconds, required=True, help="where it ends")
    ap.add_argument("--out", required=True, help="the clip to write (.mp4)")
    ap.add_argument("--transcript", help="the long video's words or captions file")
    a = ap.parse_args(argv)
    if a.end <= a.start:
        ap.error("--end must be after --start")
    if not os.path.isfile(a.video):
        sys.exit("no such video: %s" % a.video)
    if not shutil.which("ffmpeg"):
        sys.exit("ffmpeg is needed (https://ffmpeg.org/download.html)")
    words = None
    if a.transcript:
        try:
            words = extract.load_captions(a.transcript)
        except (OSError, ValueError) as e:
            sys.exit("could not read %s: %s" % (a.transcript, e))
    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    if cut(a.video, a.start, a.end, a.out) != 0:
        sys.exit("ffmpeg could not cut %s" % a.video)
    print("wrote %s (%.1f s from %.1f s)" % (a.out, a.end - a.start, a.start), file=sys.stderr)
    if words is not None:
        clip = moment_words(words, a.start, a.end)
        with open(sidecar_path(a.out), "w", encoding="utf-8") as f:
            json.dump(clip, f, ensure_ascii=False)
        print("wrote %s (%d words%s)" % (sidecar_path(a.out), len(clip),
                                         "" if clip else ": none inside the moment, check the times"), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
