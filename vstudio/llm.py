"""
llm.py - Claude through the Claude Code CLI, on the user's own subscription.

    from vstudio import llm
    if llm.available():
        text = llm.complete("Three hooks about cold brew, one per line.")
        plan = llm.complete_json(prompt, schema=PLAN_SCHEMA, system=DIRECTOR_RULES,
                                 check=lambda data: my_validator(data))   # [] = accept

How a call runs: `claude -p --output-format json` with the prompt on stdin
(no command-line length limit, nothing personal in the process list), every
tool switched off (`--tools ""`: planning is text in, JSON out), no session
saved, and optionally `--json-schema` (Claude Code then returns the parsed
object as `structured_output`) and `--append-system-prompt`. The child runs
in the temp folder so no project's CLAUDE.md steers it. The model is the
CLI's default unless VSTUDIO_CLAUDE_MODEL is set; VSTUDIO_CLAUDE_BIN points
at a claude executable that is not on PATH.

complete_json() reads `structured_output`, else the JSON inside `result`
(code fences and chatter around it are stripped), checks it against the
schema (a small JSON-Schema subset: type, required, properties, items,
enum, minItems) and then against `check`, and when anything is wrong asks
again with the problems listed and the previous answer attached, up to
`retries` more times. Each call is stateless, so the retry resends the
whole prompt (Claude Code's prompt cache keeps that cheap).

Portability: on Windows an npm install is a `claude.cmd` batch file, and
cmd.exe re-reads its arguments, so text that cmd would interpret (newlines,
&, |, %, ^ ...) never goes on the command line: the system prompt is then
folded into stdin and a schema with such characters is pasted into the
prompt instead of passed as a flag. Flags an older CLI does not know
(`--tools`, `--no-session-persistence`, `--json-schema`) are dropped one by
one on "unknown option" and the call is repeated.

Tests (and other front ends) replace the subprocess with set_runner(fn),
fn(argv, stdin_text, timeout, cwd, env) -> (returncode, stdout, stderr).

Limits: one answer per call (no streaming); a timeout kills the CLI but on
Windows a batch-file launch can leave its node child running until it ends.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

ENV_MODEL = "VSTUDIO_CLAUDE_MODEL"
ENV_BIN = "VSTUDIO_CLAUDE_BIN"

INSTALL_HELP = ("Claude Code is not installed (the `claude` command was not found). Install it "
                "(npm install -g @anthropic-ai/claude-code, or the installer from claude.com/claude-code), run "
                "`claude` once in a terminal to log in with your subscription, then try again. Set "
                "VSTUDIO_CLAUDE_BIN if it is installed somewhere that is not on PATH.")
LOGIN_HELP = "Claude Code is installed but not logged in: run `claude` once in a terminal and log in, then try again."

# Read-only tools are harmless, but planning needs none: this list is the fallback for CLIs without --tools.
_ALL_TOOLS = "Bash,Edit,Write,MultiEdit,NotebookEdit,Read,Glob,Grep,LS,WebFetch,WebSearch,Task,TodoWrite"
_LOGIN_RE = re.compile(r"(not logged in|please run /login|/login|invalid api key|authentication|unauthori[sz]ed|"
                       r"oauth token|credentials)", re.I)
_UNKNOWN_OPT_RE = re.compile(r"unknown option\s*['\"]?(--[\w-]+)", re.I)
_CMD_UNSAFE = re.compile(r"[&|<>^%!\r\n]")
_FENCE_RE = re.compile(r"```[ \t]*(?:json|JSON|js|javascript)?[ \t]*\r?\n?(.*?)```", re.S)


class LLMUnavailable(Exception):
    """Claude Code cannot be used here (not installed, not logged in); the message says how to fix it."""


class LLMError(RuntimeError):
    """Claude Code ran but gave no usable answer. `data`/`errors` hold the last parsed answer and its problems."""

    def __init__(self, message, data=None, errors=None):
        RuntimeError.__init__(self, message)
        self.data = data
        self.errors = list(errors or [])


# --------------------------------------------------------------------------- the subprocess

def _run_subprocess(argv, stdin_text, timeout, cwd, env):
    """Default runner: the real CLI. Returns (returncode, stdout, stderr)."""
    try:
        r = subprocess.run(argv, input=stdin_text, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout, cwd=cwd, env=env)
    except subprocess.TimeoutExpired:
        raise LLMError("Claude did not answer within %d s (raise the timeout or shorten the request)" % timeout)
    except FileNotFoundError:
        raise LLMUnavailable(INSTALL_HELP)
    except OSError as e:
        raise LLMUnavailable("could not start Claude Code (%s). %s" % (e, INSTALL_HELP))
    return r.returncode, r.stdout or "", r.stderr or ""


_runner = None


def set_runner(fn):
    """Replace the subprocess call (fn(argv, stdin_text, timeout, cwd, env) -> (code, stdout, stderr)).

    None restores the real CLI. Returns the previous runner so callers can put it back.
    """
    global _runner
    prev, _runner = _runner, fn
    return prev


def claude_path():
    """Path of the claude executable, or None. VSTUDIO_CLAUDE_BIN wins, then PATH, then common install places."""
    p = os.environ.get(ENV_BIN)
    if p and os.path.isfile(p):
        return p
    exe = shutil.which("claude")
    if exe:
        return exe
    home = os.path.expanduser("~")
    if os.name == "nt":
        cands = [os.path.join(home, ".local", "bin", "claude.exe"),
                 os.path.join(os.environ.get("APPDATA", ""), "npm", "claude.cmd")]
    else:
        cands = [os.path.join(home, ".local", "bin", "claude"), os.path.join(home, ".claude", "local", "claude"),
                 "/opt/homebrew/bin/claude", "/usr/local/bin/claude"]
    for c in cands:
        if c and os.path.isfile(c):
            return c
    return None


def available():
    """True when a Claude call can be attempted (the CLI exists, or a runner was injected).

    It does not prove the user is logged in: that is only known when a call answers.
    """
    return _runner is not None or claude_path() is not None


def _is_batch(exe):
    return os.name == "nt" and exe.lower().endswith((".cmd", ".bat"))


def _schema_arg(schema):
    return json.dumps(schema, separators=(",", ":"), ensure_ascii=True)


def _argv(exe, flags):
    argv = [exe, "-p", "--output-format", "json"]
    for name, value in flags:
        argv.append(name)
        if value is not None:
            argv.append(value)
    return argv


def _envelope(stdout):
    """The CLI's result object from its stdout (a single object, or an array of messages in verbose mode)."""
    text = (stdout or "").strip()
    data = None
    try:
        data = json.loads(text)
    except ValueError:
        try:  # warnings printed before the JSON
            data = extract_json(text)
        except ValueError:
            data = None
    if isinstance(data, list):
        data = next((m for m in reversed(data) if isinstance(m, dict) and m.get("type") == "result"), None)
    return data if isinstance(data, dict) else None


def _call(prompt, system=None, schema=None, timeout=900):
    """One CLI call; returns the result envelope (dict with `result`, maybe `structured_output`)."""
    runner = _runner or _run_subprocess
    exe = "claude" if _runner is not None else claude_path()
    if not exe:
        raise LLMUnavailable(INSTALL_HELP)
    batch = _is_batch(exe)
    stdin = prompt
    flags = [("--tools", ""), ("--no-session-persistence", None)]
    if system:
        if batch and _CMD_UNSAFE.search(system):
            stdin = "# Instructions\n\n%s\n\n# Task\n\n%s" % (system, prompt)
        else:
            flags.append(("--append-system-prompt", system))
    if schema is not None:
        arg = _schema_arg(schema)
        if batch and _CMD_UNSAFE.search(arg):
            stdin += "\n\nThe JSON must match this JSON Schema:\n" + arg
        else:
            flags.append(("--json-schema", arg))
    model = (os.environ.get(ENV_MODEL) or "").strip()
    if model:
        flags.append(("--model", model))
    env = dict(os.environ)
    env.pop("CLAUDECODE", None)       # allow a call from inside a Claude Code session (agent mode)
    cwd = tempfile.gettempdir()       # no project CLAUDE.md steers a planning call
    dropped = []
    while True:
        code, out, err = runner(_argv(exe, flags), stdin, timeout, cwd, env)
        m = _UNKNOWN_OPT_RE.search(err + "\n" + out) if code != 0 else None
        names = [f[0] for f in flags]
        if m and m.group(1) in names and m.group(1) not in dropped:
            bad = m.group(1)
            dropped.append(bad)
            flags = [f for f in flags if f[0] != bad]
            if bad == "--tools":
                flags.append(("--disallowedTools", _ALL_TOOLS))
            elif bad == "--json-schema":
                stdin += "\n\nThe JSON must match this JSON Schema:\n" + _schema_arg(schema)
            elif bad == "--append-system-prompt":
                stdin = "# Instructions\n\n%s\n\n# Task\n\n%s" % (system, stdin)
            continue
        break
    env_obj = _envelope(out)
    if env_obj is None:
        msg = (err.strip() or out.strip() or "no output")[-800:]
        if _LOGIN_RE.search(msg):
            raise LLMUnavailable("%s (%s)" % (LOGIN_HELP, msg[-200:]))
        raise LLMError("Claude Code failed (exit code %s): %s" % (code, msg))
    if env_obj.get("is_error") or env_obj.get("subtype") not in (None, "success"):
        msg = str(env_obj.get("result") or env_obj.get("subtype") or "unknown error")
        if _LOGIN_RE.search(msg):
            raise LLMUnavailable("%s (%s)" % (LOGIN_HELP, msg[:200]))
        raise LLMError("Claude Code returned an error: %s" % msg[:800])
    return env_obj


# --------------------------------------------------------------------------- answers

def extract_json(text):
    """The JSON value in an answer: the whole text, a ```json fence, or the first object found in prose.

    Raises ValueError when there is none.
    """
    s = (text or "").strip().lstrip("﻿")
    if not s:
        raise ValueError("the answer is empty")
    candidates = [m.group(1).strip() for m in _FENCE_RE.finditer(s)] + [s]
    for c in candidates:
        try:
            return json.loads(c)
        except ValueError:
            pass
    dec = json.JSONDecoder()
    first_other = None
    tries = 0
    for i, ch in enumerate(s):
        if ch not in "{[":
            continue
        tries += 1
        if tries > 400:
            break
        try:
            obj, _ = dec.raw_decode(s, i)
        except ValueError:
            continue
        if isinstance(obj, dict):
            return obj
        if first_other is None:
            first_other = obj
    if first_other is not None:
        return first_other
    raise ValueError("no JSON object found in the answer (it starts: %r)" % s[:120])


_TYPES = {"object": dict, "array": list, "string": str, "boolean": bool, "null": type(None)}


def _type_ok(value, t):
    if t == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if t == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    py = _TYPES.get(t)
    return True if py is None else isinstance(value, py)


def schema_errors(data, schema, where="answer"):
    """Problems with data against a JSON-Schema subset (type, required, properties, items, enum, minItems)."""
    errs = []
    if not isinstance(schema, dict):
        return errs
    t = schema.get("type")
    if t is not None:
        types = t if isinstance(t, list) else [t]
        if not any(_type_ok(data, x) for x in types):
            return ["%s must be %s, got %s" % (where, " or ".join(types), type(data).__name__)]
    if "enum" in schema and data not in schema["enum"]:
        errs.append("%s must be one of %s, got %r" % (where, ", ".join(json.dumps(v) for v in schema["enum"]), data))
    if isinstance(data, dict):
        for k in schema.get("required") or []:
            if k not in data:
                errs.append("%s is missing \"%s\"" % (where, k))
        for k, sub in (schema.get("properties") or {}).items():
            if k in data:
                errs += schema_errors(data[k], sub, "%s.%s" % (where, k))
    if isinstance(data, list):
        if len(data) < int(schema.get("minItems") or 0):
            errs.append("%s needs at least %d item(s)" % (where, schema["minItems"]))
        if isinstance(schema.get("items"), dict):
            for i, item in enumerate(data):
                errs += schema_errors(item, schema["items"], "%s[%d]" % (where, i))
    return errs


def _feedback(data, errors):
    prev = json.dumps(data, ensure_ascii=False)
    if len(prev) > 60000:
        prev = prev[:60000] + " ...(cut)"
    lines = ["", "# Your previous answer had these problems",
             "Fix every one and return the complete corrected JSON (all of it, not a diff)."]
    lines += ["- " + e for e in errors[:40]]
    if len(errors) > 40:
        lines.append("- ...and %d more of the same kinds" % (len(errors) - 40))
    lines += ["", "# Your previous answer", "```json", prev, "```"]
    return "\n".join(lines)


# --------------------------------------------------------------------------- API

def complete(prompt, system=None, timeout=900):
    """Claude's text answer to a prompt (no tools). Raises LLMUnavailable or LLMError."""
    return str(_call(prompt, system=system, timeout=timeout).get("result") or "")


def complete_json(prompt, schema=None, system=None, timeout=900, retries=2, check=None):
    """A JSON answer (dict or list) that passes `schema` and `check`.

    check(data) -> list of problems ([] = accept) runs after the schema
    check; problems are sent back to Claude with its previous answer, at
    most `retries` more times. Raises LLMError (with .data and .errors of
    the last answer) when no answer passed, LLMUnavailable when Claude Code
    cannot run at all.
    """
    rule = "Answer with exactly one JSON value and nothing else: no prose, no code fences."
    base = prompt.rstrip() + "\n\n" + rule
    feedback, last_data, last_errors = "", None, []
    for _ in range(max(0, int(retries)) + 1):
        env = _call(base + feedback, system=system, schema=schema, timeout=timeout)
        data = env.get("structured_output")
        if not isinstance(data, (dict, list)):
            try:
                data = extract_json(env.get("result") or "")
            except ValueError as e:
                last_data, last_errors = None, ["the answer was not valid JSON: %s" % e]
                feedback = "\n\n# Your previous answer was not valid JSON (%s). %s" % (e, rule)
                continue
        errors = schema_errors(data, schema) if schema is not None else []
        if not errors and check is not None:
            errors = [str(e) for e in (check(data) or [])]
        if not errors:
            return data
        last_data, last_errors = data, errors
        feedback = _feedback(data, errors)
    raise LLMError("Claude's answer still had problems after %d attempt(s): %s"
                   % (max(0, int(retries)) + 1, "; ".join(last_errors[:5]) or "unknown"),
                   data=last_data, errors=last_errors)


def main(argv=None):
    """python -m vstudio.llm "prompt" [--json]  - a quick check that Claude Code answers."""
    argv = list(sys.argv[1:] if argv is None else argv)
    as_json = "--json" in argv
    argv = [a for a in argv if a != "--json"]
    if not argv:
        print("usage: python -m vstudio.llm \"prompt\" [--json]", file=sys.stderr)
        return 2
    if not available():
        print(INSTALL_HELP, file=sys.stderr)
        return 3
    try:
        if as_json:
            print(json.dumps(complete_json(" ".join(argv)), indent=2, ensure_ascii=False))
        else:
            print(complete(" ".join(argv)))
    except LLMUnavailable as e:
        print(e, file=sys.stderr)
        return 3
    except LLMError as e:
        print(e, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
