"""Repository paths and access to the skill scripts this package builds on."""

import importlib.util
import os
import sys

PKG = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(PKG)
SKILLS = os.path.join(REPO, ".claude", "skills")
WORKSPACE = os.environ.get("VSTUDIO_WORKSPACE") or os.path.join(REPO, "workspace")
STUDIO = os.path.join(WORKSPACE, "studio")
CACHE = os.path.join(WORKSPACE, "cache")
FONTS = os.path.join(WORKSPACE, "fonts")
MODELS = os.path.join(WORKSPACE, "models")
WEB = os.path.join(PKG, "web")

_SKILL_FILES = {
    "extract": ("viral-extract", "scripts", "extract.py"),
    "captions": ("viral-extract", "scripts", "captions.py"),
    "trends": ("trend-hunt", "scripts", "trends.py"),
    "radar": ("trend-hunt", "scripts", "radar.py"),
    "spoken": ("script-forge", "scripts", "spoken.py"),
    "scriptcheck": ("script-forge", "scripts", "scriptcheck.py"),
    "voiceprint": ("script-forge", "scripts", "voiceprint.py"),
    "deadair": ("yt-edit", "deadair.py"),
    "hookscore": ("ig-reel", "hookscore.py"),
}


def skill_path(name):
    return os.path.join(SKILLS, *_SKILL_FILES[name])


def load_skill(name):
    """Import a skill script as a module (once). Its folder goes on sys.path so its own imports work."""
    key = "vstudio_skill_" + name
    if key in sys.modules:
        return sys.modules[key]
    path = skill_path(name)
    folder = os.path.dirname(path)
    if folder not in sys.path:
        sys.path.insert(0, folder)
    spec = importlib.util.spec_from_file_location(key, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[key] = mod
    spec.loader.exec_module(mod)
    return mod


def ensure(*dirs):
    for d in dirs:
        os.makedirs(d, exist_ok=True)
    return dirs[0] if len(dirs) == 1 else dirs
