"""python -m vstudio <command> [args]  -  run `python -m vstudio help` for the list."""

import importlib
import sys

COMMANDS = {
    "serve": ("server", "open the local web studio (http://127.0.0.1:4320)"),
    "reel": ("director", "prompt + reference/competitor videos + assets -> finished reel"),
    "autoedit": ("autoedit", "raw video -> professionally edited video (cuts, zooms, captions, graphics, sfx)"),
    "channel": ("channel", "analyze channels/profiles: thumbnails, titles, topics, dialogue, energy, style"),
    "render": ("render", "render a timeline spec (JSON) to MP4 or a still"),
    "plan": ("planner", "compile a plan (beats) into a timeline spec"),
    "templates": ("templates", "list or preview the motion-graphics templates"),
    "fonts": ("fonts", "list, resolve or install free fonts"),
    "sfx": ("sfx", "list or render the built-in sound effects"),
    "mimic2d": ("mimic", "start the full ReelMimic studio for 2D animated remakes (tools/reelmimic)"),
    "doctor": ("doctor", "check that ffmpeg, fonts, OCR, whisper and Claude are available"),
}


def usage():
    print("Viral Studio\n\nusage: python -m vstudio <command> [args]\n")
    for name, (_, doc) in COMMANDS.items():
        print("  %-10s %s" % (name, doc))
    print("\n`python -m vstudio <command> --help` for a command's options.")


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("help", "-h", "--help"):
        usage()
        return 0
    cmd = argv.pop(0)
    if cmd not in COMMANDS:
        print("unknown command: %s\n" % cmd, file=sys.stderr)
        usage()
        return 2
    mod = importlib.import_module("vstudio." + COMMANDS[cmd][0])
    return mod.main(argv) or 0


if __name__ == "__main__":
    sys.exit(main())
