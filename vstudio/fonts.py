"""
fonts.py - find, load and install fonts for the renderer and the captions.

    python -m vstudio fonts list                     families found, where they live
    python -m vstudio fonts resolve "Montserrat Black"
    python -m vstudio fonts install Montserrat Anton "Bebas Neue" Poppins

Names are "Family" or "Family Weight [Italic]": weights Thin, ExtraLight,
Light, Regular, Medium, SemiBold, Bold, ExtraBold, Black (also "Extra Bold",
"Semi-Bold", Heavy, Book...). A path to a .ttf/.otf works too.

Search order: $VSTUDIO_FONTS (folders separated by os.pathsep),
workspace/fonts, assets/fonts, the user's and the system's font folders
(Windows, macOS, Linux), then `fc-match` when fontconfig exists, then any
bold sans that is installed, then Pillow's built-in font. resolve() never
raises: a missing family falls back (with a one-line notice on stderr) so a
render never fails over a font.

Variable fonts (Google Fonts ships most families as Family[wght].ttf) are
handled: load() sets the weight axis to the requested weight. libass (the
caption burner) cannot use variable fonts from a folder, so ass_font()
makes a static instance with fontTools when it is installed, and otherwise
picks the closest static font.

`install` downloads open-licensed families from the google/fonts GitHub
repository (raw.githubusercontent.com), reading each family's METADATA.pb
for its file names, into workspace/fonts/<Family>/ with the licence file.
"""

import argparse
import json
import os
import re
import shutil
import ssl
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

from PIL import ImageFont

from . import paths

FONT_EXT = (".ttf", ".otf", ".ttc", ".otc")
GOOGLE_RAW = "https://raw.githubusercontent.com/google/fonts/main"
LICENSE_DIRS = ("ofl", "apache", "ufl")
INDEX_VERSION = 2

WEIGHTS = {
    "thin": 100, "hairline": 100, "extralight": 200, "ultralight": 200, "light": 300, "regular": 400,
    "normal": 400, "book": 400, "roman": 400, "medium": 500, "semibold": 600, "demibold": 600, "bold": 700,
    "extrabold": 800, "ultrabold": 800, "black": 900, "heavy": 900, "extrablack": 950, "ultrablack": 950,
}
WEIGHT_NAMES = {100: "Thin", 200: "ExtraLight", 300: "Light", 400: "Regular", 500: "Medium", 600: "SemiBold",
                700: "Bold", 800: "ExtraBold", 900: "Black"}
ITALIC_WORDS = {"italic", "oblique"}
# tried in this order when the requested family is not installed
FALLBACK_SANS = ["Montserrat", "Inter", "Poppins", "Roboto", "Open Sans", "Arial", "Helvetica Neue", "Helvetica",
                 "Segoe UI", "DejaVu Sans", "Liberation Sans", "Noto Sans", "Verdana"]

_index = None
_resolved = {}
_warned = set()


def _key(s):
    return re.sub(r"[^a-z0-9]", "", str(s).lower())


def search_dirs():
    """Font folders in priority order (existing or not)."""
    dirs = [d for d in os.environ.get("VSTUDIO_FONTS", "").split(os.pathsep) if d]
    dirs += [paths.FONTS, os.path.join(paths.REPO, "assets", "fonts")]
    return dirs + search_dirs_system()


def search_dirs_system():
    """The user's and the operating system's font folders."""
    dirs = []
    home = os.path.expanduser("~")
    if sys.platform.startswith("win"):
        local = os.environ.get("LOCALAPPDATA") or os.path.join(home, "AppData", "Local")
        windir = os.environ.get("WINDIR") or "C:\\Windows"
        dirs += [os.path.join(local, "Microsoft", "Windows", "Fonts"), os.path.join(windir, "Fonts")]
    elif sys.platform == "darwin":
        dirs += [os.path.join(home, "Library", "Fonts"), "/Library/Fonts", "/System/Library/Fonts",
                 "/System/Library/Fonts/Supplemental"]
    else:
        dirs += [os.path.join(home, ".fonts"), os.path.join(home, ".local", "share", "fonts"),
                 "/usr/local/share/fonts", "/usr/share/fonts"]
    return dirs


def parse_name(name):
    """'Montserrat Extra Bold Italic' -> ('Montserrat', 800, True); weight None when not given."""
    tokens = [t for t in re.split(r"[\s_]+", str(name).strip()) if t]
    if len(tokens) == 1 and "-" in tokens[0]:  # PostScript style: Montserrat-BlackItalic
        fam, _, sty = tokens[0].rpartition("-")
        sty_tokens = re.findall(r"[A-Z][a-z]*|[a-z]+", sty)
        if fam and sty_tokens and all(_key(t) in WEIGHTS or _key(t) in ITALIC_WORDS or _key(t) in ("extra", "semi", "ultra", "demi")
                                      for t in sty_tokens):
            tokens = [fam] + sty_tokens
    italic = False
    while len(tokens) > 1 and _key(tokens[-1]) in ITALIC_WORDS:
        italic = True
        tokens.pop()
    weight = None
    if len(tokens) > 2 and _key(tokens[-2] + tokens[-1]) in WEIGHTS:
        weight = WEIGHTS[_key(tokens[-2] + tokens[-1])]
        tokens = tokens[:-2]
    elif len(tokens) > 1 and _key(tokens[-1]) in WEIGHTS:
        weight = WEIGHTS[_key(tokens[-1])]
        tokens = tokens[:-1]
    while len(tokens) > 1 and _key(tokens[-1]) in ITALIC_WORDS:
        italic = True
        tokens.pop()
    return " ".join(tokens), weight, italic


def _style_weight(style):
    """Weight and italic flag from a style string such as 'SemiBold Italic' or 'Condensed Black'."""
    k = _key(style)
    italic = "italic" in k or "oblique" in k
    k = k.replace("italic", "").replace("oblique", "")
    for word, w in sorted(WEIGHTS.items(), key=lambda kv: -len(kv[0])):
        if word in k:
            return w, italic
    return 400, italic


# --------------------------------------------------------------------------- index

def _scan_file(path):
    faces = []
    for idx in range(32):
        try:
            f = ImageFont.truetype(path, 24, index=idx)
        except OSError:
            break
        family, style = f.getname()
        weight, italic = _style_weight(style or "")
        entry = {"path": path, "index": idx, "family": family or os.path.splitext(os.path.basename(path))[0],
                 "style": style or "Regular", "weight": weight, "italic": italic, "variable": False,
                 "wght": None, "instances": []}
        try:
            axes = f.get_variation_axes()
            entry["variable"] = True
            for ax in axes:
                nm = ax.get("name")
                nm = nm.decode("latin-1") if isinstance(nm, bytes) else str(nm)
                if nm.lower() in ("weight", "wght"):
                    entry["wght"] = [ax["minimum"], ax["maximum"]]
            entry["instances"] = [n.decode("latin-1") if isinstance(n, bytes) else str(n)
                                  for n in f.get_variation_names()]
        except (OSError, AttributeError, ValueError):
            pass
        faces.append(entry)
        if not path.lower().endswith((".ttc", ".otc")):
            break
    return faces


def _index_path():
    return os.path.join(paths.CACHE, "fonts_index.json")


def _load_index(refresh=False):
    """All fonts in the search folders, scanned once and cached by path + mtime."""
    global _index
    if _index is not None and not refresh:
        return _index
    cache = {}
    try:
        with open(_index_path(), encoding="utf-8") as f:
            data = json.load(f)
        if data.get("version") == INDEX_VERSION:
            cache = data.get("files", {})
    except (OSError, ValueError):
        pass
    files, entries, seen = {}, [], set()
    for rank, d in enumerate(search_dirs()):
        if not os.path.isdir(d):
            continue
        for root, _, names in os.walk(d):
            for n in sorted(names):
                if not n.lower().endswith(FONT_EXT):
                    continue
                p = os.path.normpath(os.path.join(root, n))
                if p in seen:
                    continue
                seen.add(p)
                try:
                    st = os.stat(p)
                except OSError:
                    continue
                c = cache.get(p)
                if c and c.get("mtime") == st.st_mtime and c.get("size") == st.st_size:
                    faces = c["faces"]
                else:
                    faces = _scan_file(p)
                files[p] = {"mtime": st.st_mtime, "size": st.st_size, "faces": faces}
                for fc in faces:
                    entries.append(dict(fc, rank=rank))
    try:
        os.makedirs(paths.CACHE, exist_ok=True)
        tmp = _index_path() + ".tmp%d" % os.getpid()
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"version": INDEX_VERSION, "files": files}, f)
        os.replace(tmp, _index_path())
    except OSError:
        pass  # a read-only workspace only costs a rescan next time
    _index = entries
    return entries


def refresh():
    """Forget cached lookups and rescan the font folders."""
    _resolved.clear()
    _load_index(refresh=True)


def installed():
    """Every font face found: [{path, index, family, style, weight, italic, variable, wght, instances, rank}]."""
    return [dict(e) for e in _load_index()]


# --------------------------------------------------------------------------- matching

def _face_score(e, weight, italic):
    """Lower is better: weight distance (a variable axis covering it is exact), italic mismatch, folder rank."""
    want = weight or 400
    if e["variable"] and e.get("wght") and e["wght"][0] <= want <= e["wght"][1]:
        dist = 0
    else:
        w = e["weight"]
        dist = abs(w - want)
        if want >= 500 and w < want:   # CSS rule: heavier first for bold requests
            dist += 25
        if want < 500 and w > want:
            dist += 25
    return (dist + (1000 if bool(e["italic"]) != bool(italic) else 0), 1 if e["variable"] else 0, e["rank"])


def _best(entries, weight, italic):
    return min(entries, key=lambda e: _face_score(e, weight, italic)) if entries else None


def _family_matches(name, weight, italic, entries):
    fam_key = _key(name)
    hits = [e for e in entries if _key(e["family"]) == fam_key]
    if hits:
        return _best(hits, weight, italic)
    # legacy names: family "Montserrat Black" + style "Regular", or a full-name match
    full = _key(name + (" " + WEIGHT_NAMES.get(weight, "") if weight else ""))
    hits = [e for e in entries if _key(e["family"] + ("" if _key(e["style"]) in ("regular", "") else e["style"])) == full
            or _key(os.path.splitext(os.path.basename(e["path"]))[0]) == full]
    return _best(hits, weight, italic)


def _fc_match(family, weight, italic):
    if not shutil.which("fc-match"):
        return None
    fc_weight = {100: 0, 200: 40, 300: 50, 400: 80, 500: 100, 600: 180, 700: 200, 800: 205, 900: 210}
    w = fc_weight.get(int(round((weight or 400) / 100.0)) * 100, 80)
    pattern = "%s:weight=%d%s" % (family, w, ":slant=100" if italic else "")
    try:
        r = subprocess.run(["fc-match", "-f", "%{file}\n%{family}", pattern], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    lines = r.stdout.splitlines()
    if len(lines) < 2 or not os.path.isfile(lines[0]):
        return None
    families = [_key(f) for f in lines[1].split(",")]
    if _key(family) not in families:  # fc-match substitutes silently; only accept the real family
        return None
    faces = _scan_file(lines[0])
    return _best([dict(f, rank=99) for f in faces], weight, italic)


def _entry(name_or_path, fallback_bold=True):
    """The best index entry for a name (or a synthetic entry for a font file), or None."""
    if name_or_path and os.path.isfile(str(name_or_path)):
        faces = _scan_file(str(name_or_path))
        return dict(faces[0], rank=0) if faces else {"path": str(name_or_path), "index": 0, "variable": False,
                                                     "weight": 400, "italic": False, "family": "", "style": ""}
    name = (name_or_path or "").strip()
    cache_key = (name, fallback_bold)
    if cache_key in _resolved:
        return _resolved[cache_key]
    family, weight, italic = parse_name(name) if name else ("", None, False)
    entries = _load_index()
    e = _family_matches(family, weight, italic, entries) if family else None
    if e is None and family:
        e = _fc_match(family, weight, italic)
    if e is None:
        # not installed: the closest weight of a common bold sans (bold when no weight was asked for)
        weight = weight or (700 if fallback_bold else 400)
        for fam in FALLBACK_SANS:
            e = _family_matches(fam, weight, italic, entries) or _fc_match(fam, weight, italic)
            if e:
                break
        if e is None and entries:
            e = _best([x for x in entries if not x["italic"]] or entries, weight, False)
        if name and name not in _warned:
            _warned.add(name)
            got = "%s at weight %d" % (e["family"], weight if e["variable"] else e["weight"]) if e else "Pillow's built-in font"
            print("font %r not found; using %s (try: python -m vstudio fonts install \"%s\")" % (name, got, family),
                  file=sys.stderr)
    if e is not None:
        e = dict(e)
        e["want_weight"] = weight
        e["want_italic"] = italic
    _resolved[cache_key] = e
    return e


def resolve(name_or_path, fallback_bold=True):
    """Path to the font file for a name like "Montserrat Black" (or a path). Never raises.

    Returns "" only when no font file exists at all (load() then uses Pillow's built-in font).
    """
    try:
        e = _entry(name_or_path, fallback_bold)
    except Exception as err:  # never fail a render over a font
        print("font lookup failed for %r: %s" % (name_or_path, err), file=sys.stderr)
        return ""
    return e["path"] if e else ""


def info(name_or_path):
    """What resolve() picked: {path, index, family, style, weight, variable, ...} or None."""
    e = _entry(name_or_path)
    return dict(e) if e else None


_font_cache = {}


def load(name_or_path, size):
    """A Pillow font at a pixel size; variable fonts get their weight axis set to the requested weight."""
    size = max(1, int(round(size)))
    e = _entry(name_or_path)
    if not e:
        return ImageFont.load_default(size)
    weight = e.get("want_weight") or (400 if e["variable"] else e["weight"])
    key = (e["path"], e["index"], size, weight)
    f = _font_cache.get(key)
    if f is None:
        f = ImageFont.truetype(e["path"], size, index=e["index"])
        if e["variable"]:
            _set_weight(f, e, weight)
        if len(_font_cache) > 256:
            _font_cache.clear()
        _font_cache[key] = f
    return f


def _set_weight(font, e, weight):
    """Variable font: pick the named instance for the weight, else set the axes directly."""
    wanted = WEIGHT_NAMES.get(int(round(weight / 100.0)) * 100)
    italic = e.get("want_italic")
    names = e.get("instances") or []
    for cand in ([wanted + " Italic", wanted] if italic else [wanted]):
        if cand in names:
            try:
                font.set_variation_by_name(cand)
                return
            except (OSError, ValueError):
                break
    try:
        axes = font.get_variation_axes()
        vals = []
        for ax in axes:
            nm = ax.get("name")
            nm = (nm.decode("latin-1") if isinstance(nm, bytes) else str(nm)).lower()
            vals.append(max(ax["minimum"], min(ax["maximum"], weight)) if nm in ("weight", "wght") else ax["default"])
        font.set_variation_by_axes(vals)
    except (OSError, ValueError):
        pass


def weight_of(name_or_path):
    """The weight a name renders at (requested weight for variable fonts, the file's weight otherwise)."""
    e = _entry(name_or_path)
    if not e:
        return 400
    return (e.get("want_weight") or 400) if e["variable"] else e["weight"]


# --------------------------------------------------------------------------- captions (libass)

def ass_font(name, dest_dir):
    """Prepare a font for libass: (font name to put in the .ass, weight it renders at).

    The font file is copied into dest_dir (pass that folder as the ass
    filter's fontsdir). libass ignores variable fonts in a fonts folder, so
    a variable font becomes a static instance (fontTools) or, without
    fontTools, the closest static fallback.
    """
    e = _entry(name)
    if not e:
        return parse_name(name or "Arial")[0] or "Arial", 700
    os.makedirs(dest_dir, exist_ok=True)
    if e["variable"]:
        weight = e.get("want_weight") or 400
        static = _static_instance(e, weight)
        if static:
            shutil.copyfile(static[0], os.path.join(dest_dir, os.path.basename(static[0])))
            return static[1], weight
        statics = [x for x in _load_index() if not x["variable"] and not x["italic"]]
        alt = None
        for fam in [e["family"]] + FALLBACK_SANS:
            alt = _family_matches(fam, weight, False, statics)
            if alt:
                break
        key = ("ass", e["path"])
        if key not in _warned:
            _warned.add(key)
            print("captions: %s is a variable font and fontTools is not installed; captions use %s %s "
                  "(pip install fonttools for the exact font)" % (e["family"], alt["family"] if alt else "the default",
                                                                  alt["style"] if alt else "font"), file=sys.stderr)
        if not alt:
            return e["family"], weight
        e = alt
    shutil.copyfile(e["path"], os.path.join(dest_dir, os.path.basename(e["path"])))
    style = "" if _key(e["style"]) in ("regular", "") else " " + e["style"]
    return e["family"] + style, e["weight"]


def _static_instance(e, weight):
    """(path, family name) of a cached static instance of a variable font, or None without fontTools."""
    try:
        from fontTools.ttLib import TTFont
        from fontTools.varLib import instancer
    except ImportError:
        return None
    wname = WEIGHT_NAMES.get(int(round(weight / 100.0)) * 100, str(weight))
    family = e["family"] if wname == "Regular" else "%s %s" % (e["family"], wname)
    folder = paths.ensure(os.path.join(paths.CACHE, "fonts"))
    out = os.path.join(folder, "%s-%s.ttf" % (re.sub(r"[^A-Za-z0-9]", "", e["family"]), wname))
    if not os.path.exists(out) or os.path.getmtime(out) < os.path.getmtime(e["path"]):
        try:
            font = TTFont(e["path"], fontNumber=e.get("index", 0))
            axes = {a.axisTag: a.defaultValue for a in font["fvar"].axes}
            axes["wght"] = max(e["wght"][0], min(e["wght"][1], weight)) if e.get("wght") else weight
            inst = instancer.instantiateVariableFont(font, axes, updateFontNames=True)
            names = inst["name"]
            # make sure libass finds it under "Family Weight" whatever the STAT table said
            for rec in list(names.names):
                if rec.nameID in (1, 4):
                    names.setName(family, rec.nameID, rec.platformID, rec.platEncID, rec.langID)
            tmp = out + ".tmp%d" % os.getpid()
            inst.save(tmp)
            os.replace(tmp, out)
        except Exception as err:  # a broken instance must not break captions
            print("could not make a static %s %s: %s" % (e["family"], wname, err), file=sys.stderr)
            return None
    return out, family


# --------------------------------------------------------------------------- install

def _ssl_context():
    ctx = ssl.create_default_context()
    if not os.environ.get("SSL_CERT_FILE"):
        try:  # python.org builds on macOS ship without system certificates
            import certifi
            ctx.load_verify_locations(certifi.where())
        except (ImportError, OSError):
            pass
    return ctx


def _get(url, timeout=60):
    req = urllib.request.Request(url, headers={"User-Agent": "vstudio-fonts"})
    with urllib.request.urlopen(req, timeout=timeout, context=_ssl_context()) as r:
        return r.read()


def install(families, force=False):
    """Download Google Fonts families into workspace/fonts/<Family>/; returns the font file paths.

    Raises RuntimeError naming every family that could not be installed
    (after installing the others).
    """
    out, failed = [], []
    for family in families:
        slug = re.sub(r"[^a-z0-9]", "", family.lower())
        meta = lic = None
        for lic in LICENSE_DIRS:
            try:
                meta = _get("%s/%s/%s/METADATA.pb" % (GOOGLE_RAW, lic, slug)).decode("utf-8", "replace")
                break
            except urllib.error.HTTPError as err:
                if err.code != 404:
                    failed.append("%s (HTTP %d)" % (family, err.code))
                    meta = False
                    break
            except (urllib.error.URLError, OSError) as err:
                failed.append("%s (%s)" % (family, getattr(err, "reason", err)))
                meta = False
                break
        if meta is False:
            continue
        if meta is None:
            failed.append("%s (not in Google Fonts; check the spelling at fonts.google.com)" % family)
            continue
        name = (re.search(r'^name:\s*"([^"]+)"', meta, re.M) or [None, family])[1]
        files = list(dict.fromkeys(re.findall(r'filename:\s*"([^"]+)"', meta)))
        folder = paths.ensure(os.path.join(paths.FONTS, re.sub(r"[^A-Za-z0-9]", "", name)))
        try:
            for fn in files + ["OFL.txt" if lic == "ofl" else "LICENSE.txt"]:
                dest = os.path.join(folder, fn)
                if os.path.exists(dest) and not force:
                    if fn.lower().endswith(FONT_EXT):
                        out.append(dest)
                    continue
                try:
                    data = _get("%s/%s/%s/%s" % (GOOGLE_RAW, lic, slug, urllib.parse.quote(fn)))
                except urllib.error.HTTPError:
                    if fn.lower().endswith(FONT_EXT):
                        raise
                    continue  # a missing licence file name variant is not fatal
                tmp = dest + ".part"
                with open(tmp, "wb") as f:
                    f.write(data)
                os.replace(tmp, dest)
                if fn.lower().endswith(FONT_EXT):
                    out.append(dest)
        except (urllib.error.URLError, OSError) as err:
            failed.append("%s (%s)" % (family, getattr(err, "reason", err)))
    if out:
        refresh()
    if failed:
        raise RuntimeError("could not install: " + "; ".join(failed) + (
            "\ninstalled: " + ", ".join(out) if out else ""))
    return out


# --------------------------------------------------------------------------- CLI

def _list(show_all):
    by_family = {}
    for e in installed():
        by_family.setdefault((e["rank"], e["family"]), []).append(e)
    dirs = search_dirs()
    n_local = len(dirs) - len(search_dirs_system())
    many = len(by_family) > 60
    for (rank, fam), faces in sorted(by_family.items(), key=lambda kv: (kv[0][0], kv[0][1].lower())):
        if not show_all and rank >= n_local and many:
            continue
        styles = []
        for f in sorted(faces, key=lambda f: (f["italic"], f["weight"])):
            if f["variable"] and f.get("wght"):
                styles.append("%s %d-%d (variable)" % ("italic" if f["italic"] else "upright", f["wght"][0], f["wght"][1]))
            else:
                styles.append(f["style"])
        print("%-28s %s\n%28s   in %s" % (fam, ", ".join(dict.fromkeys(styles)), "", dirs[rank] if rank < len(dirs) else "?"))
    if not show_all and many:
        print("\n(system fonts hidden; --all to show them)")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="vstudio fonts", description="List, resolve or install free fonts.")
    sub = ap.add_subparsers(dest="cmd")
    ls = sub.add_parser("list", help="font families found, by folder")
    ls.add_argument("--all", action="store_true", help="include system fonts even when there are many")
    rs = sub.add_parser("resolve", help="which file a font name uses")
    rs.add_argument("name", nargs="+")
    ins = sub.add_parser("install", help="download Google Fonts families (open licences) into workspace/fonts")
    ins.add_argument("family", nargs="+")
    ins.add_argument("--force", action="store_true", help="download again even if present")
    a = ap.parse_args(argv)
    if a.cmd in (None, "list"):
        _list(getattr(a, "all", False))
        return 0
    if a.cmd == "resolve":
        name = " ".join(a.name)
        e = info(name)
        if not e:
            print("no font file found; Pillow's built-in font will be used")
            return 1
        if e["variable"]:
            rng = "%d-%d" % tuple(e["wght"]) if e.get("wght") else "?"
            print("%s\n  family %s, variable font (weights %s), renders at weight %d"
                  % (e["path"], e["family"], rng, weight_of(name)))
        else:
            print("%s\n  family %s, style %s, weight %d" % (e["path"], e["family"], e["style"], e["weight"]))
        return 0
    try:
        files = install(a.family, force=a.force)
    except RuntimeError as err:
        print(err, file=sys.stderr)
        return 1
    for f in files:
        print("installed %s" % f)
    return 0


if __name__ == "__main__":
    sys.exit(main())
