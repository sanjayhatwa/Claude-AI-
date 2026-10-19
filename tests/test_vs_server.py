"""Tests for the local web studio: vstudio.jobs (argument checking, the job runner,
restart recovery), vstudio.server (every API route, uploads, Range requests, path
traversal, Host/Origin checks, Ask Claude with a stand-in model module) and
vstudio.doctor. A real server runs on an ephemeral port with a temp workspace;
jobs are tiny `python -c` commands from an injected builder, so nothing here
renders video or needs the network. The last test drives the page in a headless
browser (Python Playwright, or Node Playwright with a preinstalled Chromium) and
is skipped when neither is available."""

import errno
import http.client
import importlib.util
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
import urllib.parse
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from vstudio import doctor, jobs, server  # noqa: E402

try:
    from vstudio import timeline
except ImportError:
    timeline = None

try:
    from vstudio import templates
except ImportError:
    templates = None

VALID_SPEC = {"version": 1, "width": 180, "height": 320, "fps": 10, "duration": 1.0, "background": "#000000",
              "layers": [{"type": "text", "text": "HI", "start": 0, "end": 1}]}

# A job that prints render-style progress, writes out/result.txt and exits with a chosen code.
FAKE_JOB = ("import os, sys, time\n"
            "secs, code = float(sys.argv[2]), int(sys.argv[3])\n"
            "for i in range(4):\n"
            "    print('%d/4 frames' % (i + 1), flush=True)\n"
            "    time.sleep(secs / 4)\n"
            "open(os.path.join(sys.argv[1], 'result.txt'), 'w').write('hi')\n"
            "if code: print('error: the fake job failed on purpose', flush=True)\n"
            "sys.exit(code)\n")


def fake_builder(kind, args, ctx):
    """Render jobs run FAKE_JOB; other kinds are checked by the real builder, then replaced by a no-op."""
    if kind == "render" and isinstance(args, dict) and "sleep" in args:
        return [[sys.executable, "-c", FAKE_JOB, ctx.out_dir, str(float(args.get("sleep", 0))),
                 str(int(args.get("exit", 0)))]]
    jobs.build_steps(kind, args, ctx)
    return [[sys.executable, "-c", "print('ok')"]]


def wait_for(fn, timeout=15.0, step=0.05):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = fn()
        if value:
            return value
        time.sleep(step)
    return fn()


def make_project(root, pid="p1"):
    path = os.path.join(root, "projects", pid)
    os.makedirs(os.path.join(path, "uploads"), exist_ok=True)
    os.makedirs(os.path.join(path, "jobs"), exist_ok=True)
    jobs.write_json(os.path.join(path, "project.json"), {"id": pid, "name": pid, "created": 1, "updated": 1})
    return path


def fake_llm_module(answer, available=True):
    """A stand-in vstudio.llm whose complete_json returns `answer` (or answer(prompt)); prompts land in .calls."""
    mod = types.ModuleType("vstudio.llm")
    mod.LLMUnavailable = type("LLMUnavailable", (Exception,), {})
    mod.calls = []
    mod.available = lambda: available

    def complete_json(prompt, schema=None, system=None, timeout=900, retries=2):
        mod.calls.append(prompt)
        return answer(prompt) if callable(answer) else answer
    mod.complete_json = complete_json
    return mod


# ----------------------------------------------------------------------------- pure functions

class PureFunctionTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="vs_pure_")
        self.pdir = make_project(self.tmp)
        with open(os.path.join(self.pdir, "uploads", "a.mp4"), "wb") as f:
            f.write(b"x" * 10)
        with open(os.path.join(self.pdir, "uploads", "style_dna.json"), "w") as f:
            f.write("{}")
        self.ctx = jobs.JobContext(self.pdir, os.path.join(self.pdir, "jobs", "j"), os.path.join(self.pdir, "jobs", "j", "out"), [])
        os.makedirs(self.ctx.out_dir)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_resolve_in_rejects_escapes(self):
        self.assertEqual(jobs.resolve_in(self.pdir, "uploads/a.mp4"), os.path.realpath(os.path.join(self.pdir, "uploads", "a.mp4")))
        self.assertTrue(jobs.resolve_in(self.pdir, "uploads\\a.mp4").endswith("a.mp4"))
        for bad in ["../x", "uploads/../../x", "/etc/passwd", "C:\\Windows\\win.ini", "c:x", "uploads/a.mp4:stream",
                    "", "   ", "uploads/\x00a", "."]:
            with self.assertRaises(jobs.PathError, msg=bad):
                jobs.resolve_in(self.pdir, bad)
        with self.assertRaises(jobs.PathError):
            jobs.resolve_in(self.pdir, "uploads/missing.mp4")

    @unittest.skipIf(os.name == "nt", "symlinks need extra rights on Windows")
    def test_resolve_in_rejects_symlink_out(self):
        outside = os.path.join(self.tmp, "secret.txt")
        with open(outside, "w") as f:
            f.write("secret")
        os.symlink(outside, os.path.join(self.pdir, "uploads", "link.txt"))
        with self.assertRaises(jobs.PathError):
            jobs.resolve_in(self.pdir, "uploads/link.txt")

    def test_clean_upload_name(self):
        self.assertEqual(server.clean_upload_name("clip.mp4"), "clip.mp4")
        self.assertEqual(server.clean_upload_name(" my clip?.mp4 "), "my clip_.mp4")
        self.assertEqual(server.clean_upload_name("CON.txt"), "_CON.txt")
        self.assertEqual(server.clean_upload_name(".env"), "env")
        self.assertTrue(len(server.clean_upload_name("a" * 400 + ".mp4")) <= 150)
        for bad in ["../evil.txt", "..\\evil.txt", "a/b.txt", "/etc/passwd", "..", ".", "", None, "a\x00b", "a\nb"]:
            with self.assertRaises(server.ApiError, msg=repr(bad)):
                server.clean_upload_name(bad)

    def test_parse_range(self):
        self.assertEqual(server.parse_range("bytes=0-9", 100), (0, 9))
        self.assertEqual(server.parse_range("bytes=90-", 100), (90, 99))
        self.assertEqual(server.parse_range("bytes=-10", 100), (90, 99))
        self.assertEqual(server.parse_range("bytes=50-500", 100), (50, 99))
        self.assertIsNone(server.parse_range("bytes=100-", 100))
        self.assertIsNone(server.parse_range("bytes=-0", 100))
        self.assertEqual(server.parse_range("bytes=5-2", 100), "ignore")
        self.assertEqual(server.parse_range("items=0-1", 100), "ignore")
        self.assertEqual(server.parse_range("bytes=0-1,5-6", 100), "ignore")
        # more digits than int() converts (4300) answer like any huge number, never a ValueError
        huge = "9" * 5000
        self.assertIsNone(server.parse_range("bytes=%s-" % huge, 100))
        self.assertEqual(server.parse_range("bytes=0-%s" % huge, 100), (0, 99))
        self.assertEqual(server.parse_range("bytes=-%s" % huge, 100), (0, 99))
        self.assertEqual(server.parse_range("bytes=\u00b2-", 100), "ignore")

    def test_parse_count(self):
        self.assertEqual(server.parse_count("42"), 42)
        self.assertEqual(server.parse_count(" 7 "), 7)
        for bad in [None, "", "\u00b2", "\u0663", "-1", "1e3", "0x10", "9" * 5000]:
            self.assertIsNone(server.parse_count(bad), repr(bad))

    def test_upload_names_fit_in_bytes_and_path_length(self):
        # 112 Devanagari letters are 336 bytes in UTF-8: over the 255-byte limit of macOS and Linux disks
        name = server.clean_upload_name("\u0915" * 112 + ".mp4")
        self.assertTrue(name.endswith(".mp4"))
        self.assertLessEqual(len(name.encode("utf-8")), server.MAX_NAME_BYTES)
        self.assertLessEqual(len(server.clean_upload_name("\U0001F600" * 100 + ".mov").encode("utf-8")), server.MAX_NAME_BYTES)
        # Windows without long paths: the whole path, " (99)" included, stays under the limit
        folder = os.path.join(self.tmp, "uploads")
        limit = len(folder) + 40
        fitted = server.fit_upload_name(folder, "a" * 100 + ".mp4", max_path=limit)
        self.assertTrue(fitted.endswith(".mp4"))
        self.assertLessEqual(len(os.path.join(folder, fitted)) + len(" (99)"), limit)
        self.assertEqual(server.fit_upload_name(folder, "short.mp4", max_path=limit), "short.mp4")
        self.assertEqual(server.fit_upload_name(folder, "a" * 300 + ".mp4", max_path=None), "a" * 300 + ".mp4")
        with self.assertRaises(server.ApiError):
            server.fit_upload_name(folder, "clip.mp4", max_path=len(folder) + 4)

    def test_windows_wrapper_check_uses_creation_time(self):
        procs = {7: ("C:\\Python312\\python.exe", 1111), 8: ("C:\\Windows\\notepad.exe", 5)}
        with mock.patch.object(jobs, "_windows_process", lambda pid: procs.get(pid)):
            self.assertTrue(jobs._windows_alive(7, 1111))
            self.assertFalse(jobs._windows_alive(7, 2222), "a python.exe that got the pid later is not the wrapper")
            self.assertTrue(jobs._windows_alive(7, None), "records from before creation times were kept")
            self.assertFalse(jobs._windows_alive(8, None))
            self.assertFalse(jobs._windows_alive(9, 1111))

    def test_host_allowed(self):
        for ok in ["127.0.0.1:4320", "localhost:4320", "localhost", "[::1]:4320", "studio.localhost:4320", "192.168.1.5:4320", None]:
            self.assertTrue(server.host_allowed(ok), ok)
        for bad in ["evil.com", "evil.com:4320", "127.0.0.1.nip.io:4320", "localhost.evil.com"]:
            self.assertFalse(server.host_allowed(bad), bad)
        self.assertTrue(server.host_allowed("mypc.lan:4320", extra=("mypc.lan",)))

    def test_parse_progress_and_failure_reason(self):
        frac, stage = jobs.parse_progress("== render job x\n$ python -m vstudio render\n\r[###...] 10/40 frames\r[######] 20/40 frames  1.0 s")
        self.assertAlmostEqual(frac, 0.5)
        self.assertIn("20/40 frames", stage)
        self.assertAlmostEqual(jobs.parse_progress("PROGRESS 45% scoring drafts\n")[0], 0.45)
        self.assertAlmostEqual(jobs.parse_progress("[3/12] downloading thumbnails\n")[0], 0.25)
        self.assertAlmostEqual(jobs.parse_progress("[download]  42.0% of 3.1MiB\n")[0], 0.42)
        self.assertIsNone(jobs.parse_progress("73% of viewers swipe\n")[0])
        reason = jobs.failure_reason("Traceback (most recent call last):\n  File x\nValueError: bad spec\n== exit 1 after 1 s\n")
        self.assertEqual(reason, "ValueError: bad spec")

    def test_build_reel(self):
        steps = jobs.build_steps("reel", {"prompt": "-3 tips", "refs": ["https://youtu.be/abc", "uploads/a.mp4"],
                                          "competitors": "youtube.com/@x", "aspect": "1:1", "duration": 20,
                                          "script_text": "Line one.", "llm": "none", "preview": True}, self.ctx)
        self.assertEqual(len(steps), 1)
        argv = steps[0]
        self.assertEqual(argv[:4], [sys.executable, "-m", "vstudio", "reel"])
        self.assertIn("--prompt=-3 tips", argv)                 # a leading dash cannot become an option
        self.assertIn("--ref=https://youtu.be/abc", argv)
        self.assertIn("--ref=" + os.path.realpath(os.path.join(self.pdir, "uploads", "a.mp4")), argv)
        self.assertIn("--competitor=https://youtube.com/@x", argv)
        self.assertEqual(argv[argv.index("--aspect") + 1], "1:1")
        self.assertEqual(argv[argv.index("--duration") + 1], "20")
        self.assertEqual(argv[argv.index("--out-dir") + 1], self.ctx.out_dir)
        self.assertTrue(os.path.isfile(argv[argv.index("--script") + 1]))
        self.assertIn("--preview", argv)
        self.assertEqual(argv[argv.index("--llm") + 1], "none")

    def test_build_rejects_bad_args(self):
        bad = [("reel", {"prompt": "x", "refs": ["../../../etc/passwd"]}),
               ("reel", {"prompt": "x", "assets": ["/etc/passwd"]}),
               ("reel", {"prompt": "x", "assets": ["C:\\Windows\\win.ini"]}),
               ("reel", {"prompt": "x", "shell": "rm -rf /"}),
               ("reel", {"prompt": "x", "duration": 9999}),
               ("reel", {"prompt": "x", "aspect": "2:1"}),
               ("reel", {}),
               ("channel", {"urls": ["javascript:alert(1)"]}),
               ("channel", {"urls": ["ftp://example.com/x"]}),
               ("channel", {"urls": []}),
               ("autoedit", {"video": "uploads/style_dna.json"}),
               ("render", {"spec": "uploads/a.mp4"}),
               ("template", {"name": "../x"}),
               ("script", {"topic": "x", "format": "rm -rf"}),
               ("nope", {})]
        for kind, args in bad:
            with self.assertRaises(jobs.JobError, msg="%s %r" % (kind, args)):
                jobs.build_steps(kind, args, self.ctx)

    def test_build_channel_handles(self):
        argv = jobs.build_steps("channel", {"urls": "@mkbhd\nhttps://www.tiktok.com/@x, www.instagram.com/y/",
                                            "max_videos": 12, "deep": 2, "download": False}, self.ctx)[0]
        self.assertIn("https://www.youtube.com/@mkbhd", argv)
        self.assertIn("https://www.tiktok.com/@x", argv)
        self.assertIn("https://www.instagram.com/y/", argv)
        self.assertEqual(argv[argv.index("--max-videos") + 1], "12")
        self.assertIn("--no-download", argv)

    def test_build_autoedit(self):
        steps = jobs.build_steps("autoedit", {"video": "uploads/a.mp4", "style": "https://www.tiktok.com/@x/video/1",
                                              "zoom": False, "captions": False, "broll": ["uploads/a.mp4"],
                                              "energy": "calm", "hook": "WAIT"}, self.ctx)
        self.assertEqual(len(steps), 2, "a reference video is extracted first")
        self.assertTrue(steps[0][1].endswith("extract.py"))
        argv = steps[1]
        self.assertEqual(argv[argv.index("--style") + 1], os.path.join(self.ctx.out_dir, "style_ref", "style_dna.json"))
        self.assertIn("--no-zoom", argv)
        self.assertNotIn("--no-graphics", argv)
        self.assertIn("--render", argv)
        broll = argv[argv.index("--broll") + 1]
        self.assertEqual(len(os.listdir(broll)), 1)
        if jobs._cli_supports("autoedit", "--no-captions"):
            self.assertIn("--no-captions", argv)
        else:
            self.assertTrue(any("captions" in n for n in self.ctx.notes))
        one = jobs.build_steps("autoedit", {"video": "uploads/a.mp4", "style": "uploads/style_dna.json"}, self.ctx)
        self.assertEqual(len(one), 1)

    def test_build_other_kinds(self):
        with open(os.path.join(self.pdir, "uploads", "spec.json"), "w") as f:
            json.dump(VALID_SPEC, f)
        still = jobs.build_steps("render", {"spec": "uploads/spec.json", "still": 1.5}, self.ctx)[0]
        self.assertTrue(still[still.index("--out") + 1].endswith("still_1_5.png"))
        final = jobs.build_steps("render", {"spec": "uploads/spec.json"}, self.ctx)[0]
        self.assertTrue(final[final.index("--out") + 1].endswith("final.mp4"))
        plan = jobs.build_steps("plan", {"plan": "uploads/spec.json"}, self.ctx)[0]
        self.assertEqual(plan[3:5], ["plan", "compile"])
        tpl = jobs.build_steps("template", {"name": "hook_title", "params": {"text": "HI"}, "aspect": "16:9", "still": False}, self.ctx)[0]
        self.assertEqual(tpl[tpl.index("--size") + 1], "640x360")
        self.assertEqual(json.loads(tpl[tpl.index("--params") + 1]), {"text": "HI"})
        ext = jobs.build_steps("extract", {"src": "uploads/a.mp4", "skip_edit": True, "whisper": "false"}, self.ctx)[0]
        self.assertTrue(ext[1].endswith(os.path.join("viral-extract", "scripts", "extract.py")))
        self.assertIn("--skip-edit", ext)
        self.assertIn("--no-whisper", ext)
        scr = jobs.build_steps("script", {"topic": "why I quit", "seconds": 30, "format": "storytime"}, self.ctx)[0]
        self.assertEqual(scr[1:4], ["-m", "vstudio.jobs", "script"])
        self.assertIn("--format=storytime", scr)

    def test_job_title(self):
        self.assertEqual(jobs.job_title("autoedit", {"video": "uploads/raw.mp4"}), "Edit raw.mp4")
        self.assertTrue(jobs.job_title("reel", {"prompt": "x" * 200}).endswith("..."))


# ----------------------------------------------------------------------------- the runner on its own

class RunnerTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="vs_runner_")
        make_project(self.tmp)
        self.runners = []

    def tearDown(self):
        for r in self.runners:
            r.close(cancel_running=True)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def runner(self, **kw):
        r = jobs.JobRunner(self.tmp, builder=fake_builder, **kw)
        self.runners.append(r)
        return r

    def test_restart_picks_up_running_and_queued_jobs(self):
        a = self.runner()
        first = a.submit("p1", "render", {"sleep": 1.2})
        second = a.submit("p1", "render", {"sleep": 0})
        self.assertEqual(a.get(second["id"])["status"], "queued")
        a.close()                                   # the studio stops; the running job keeps going
        b = self.runner()                           # ... and a new studio starts on the same folder
        self.assertEqual(b.get(first["id"])["status"], "running")
        self.assertEqual(b.wait(first["id"], 15)["status"], "done")
        self.assertEqual(b.wait(second["id"], 15)["status"], "done", "the queued job ran after the restart")

    def test_restart_marks_lost_jobs_interrupted(self):
        r = self.runner()
        job = r.submit("p1", "render", {"sleep": 0})
        r.wait(job["id"], 15)
        path = os.path.join(self.tmp, "projects", "p1", "jobs", job["id"])
        record = jobs.read_json(os.path.join(path, "job.json"))
        record.update(status="running", pid=2 ** 22 + 12345)   # a pid no live process has
        jobs.write_json(os.path.join(path, "job.json"), record)
        os.remove(os.path.join(path, "exit.json"))
        again = self.runner()
        got = again.get(job["id"])
        self.assertEqual(got["status"], "failed")
        self.assertIn("interrupted", got["error"])

    def test_light_jobs_skip_the_queue(self):
        r = self.runner(light_kinds={"render"})
        a = r.submit("p1", "render", {"sleep": 0.6})
        b = r.submit("p1", "render", {"sleep": 0.6})
        self.assertEqual(r.get(b["id"])["status"], "running")
        r.wait(a["id"], 10)
        r.wait(b["id"], 10)

    def test_parallel_limit(self):
        r = self.runner(max_parallel=2)
        ids = [r.submit("p1", "render", {"sleep": 0.8})["id"] for _ in range(3)]
        states = [r.get(i)["status"] for i in ids]
        self.assertEqual(states, ["running", "running", "queued"])
        self.assertEqual(r.get(ids[2])["queue_position"], 1)
        for i in ids:
            self.assertEqual(r.wait(i, 15)["status"], "done")

    def test_restart_rebuilds_queued_commands_from_kind_and_args(self):
        a = self.runner()
        first = a.submit("p1", "render", {"sleep": 1.0})
        second = a.submit("p1", "render", {"sleep": 0})
        third = a.submit("p1", "render", {"sleep": 0})
        a.close()
        marker = os.path.join(self.tmp, "PWNED")
        jdir = os.path.join(self.tmp, "projects", "p1", "jobs")
        record = jobs.read_json(os.path.join(jdir, second["id"], "job.json"))
        record.update(steps=[[sys.executable, "-c", "open(%r, 'w').write('ran')" % marker]], cwd=self.tmp, light=True)
        jobs.write_json(os.path.join(jdir, second["id"], "job.json"), record)
        record = jobs.read_json(os.path.join(jdir, third["id"], "job.json"))
        record.update(args={"spec": "../../../etc/passwd"})
        jobs.write_json(os.path.join(jdir, third["id"], "job.json"), record)
        b = self.runner()
        self.assertEqual(b.get(third["id"])["status"], "failed", "args that no longer pass the checks do not run")
        self.assertEqual(b.wait(second["id"], 15)["status"], "done")
        self.assertFalse(os.path.exists(marker), "a queued job's commands come from its kind's builder, not from job.json")
        self.assertIn("result.txt", [o["name"] for o in b.get(second["id"])["outputs"]])
        self.assertEqual(b.wait(first["id"], 15)["status"], "done")

    def test_runner_keeps_the_wrapper_creation_time(self):
        with mock.patch.object(jobs, "process_created", lambda pid: 4242):
            a = self.runner()
            job = a.submit("p1", "render", {"sleep": 1.0})
        record = jobs.read_json(os.path.join(self.tmp, "projects", "p1", "jobs", job["id"], "job.json"))
        self.assertEqual(record["pid_created"], 4242)
        a.close()
        seen = []
        real = jobs.wrapper_alive
        with mock.patch.object(jobs, "wrapper_alive", lambda pid, jid, created=None: seen.append(created) or real(pid, jid, created)):
            b = self.runner()
        self.assertIn(4242, seen, "restart recovery checks the process by pid and creation time")
        self.assertEqual(b.wait(job["id"], 15)["status"], "done")

    def test_wrapper_lines_append_even_through_a_non_append_stdout(self):
        # On Windows a child does not inherit append mode: its stdout writes at its own position.
        # A plain read/write handle at the end of the file behaves the same way on every platform.
        jdir = os.path.join(self.tmp, "wrapped")
        os.makedirs(jdir)
        steps = [[sys.executable, "-c", "print('first-step-' + 'output')"],      # the command line differs from
                 [sys.executable, "-c", "print('second-step-' + 'output')"]]     # what it prints
        jobs.write_json(os.path.join(jdir, "job.json"), {"id": "x", "kind": "render", "steps": steps, "cwd": ROOT})
        log = os.path.join(jdir, "log.txt")
        with open(log, "wb"):
            pass
        with open(log, "r+b") as out:
            out.seek(0, os.SEEK_END)
            r = subprocess.run([sys.executable, "-m", "vstudio.jobs", "exec", jdir], stdout=out, stderr=subprocess.STDOUT,
                               cwd=ROOT, env=jobs.job_env(), timeout=60)
        self.assertEqual(r.returncode, 0)
        with open(log, encoding="utf-8") as f:
            text = f.read()
        needles = ["== render job x started", "== step 1/2", "first-step-output", "== step 2/2",
                   "second-step-output", "== exit 0"]
        for n in needles:
            self.assertIn(n, text)
        self.assertEqual([text.index(n) for n in needles], sorted(text.index(n) for n in needles), text)


# ----------------------------------------------------------------------------- the HTTP server

class ServerTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="vs_server_")
        cls.httpd = server.make_server("127.0.0.1", 0, os.path.join(cls.tmp, "studio"), builder=fake_builder, quiet=True)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        cls.thread.start()
        cls.pid = cls.call("POST", "/api/projects", {"name": "Test project"})[2]["id"]

    @classmethod
    def tearDownClass(cls):
        cls.httpd.studio.runner.close(cancel_running=True)
        cls.httpd.shutdown()
        cls.httpd.server_close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    @classmethod
    def call(cls, method, path, body=None, headers=None, raw=None):
        """(status, headers, parsed JSON or bytes)."""
        conn = http.client.HTTPConnection("127.0.0.1", cls.port, timeout=30)
        hdrs = {"Host": "127.0.0.1:%d" % cls.port}
        hdrs.update(headers or {})
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        conn.request(method, path, body=data, headers=hdrs)
        resp = conn.getresponse()
        payload = resp.read()
        conn.close()
        ctype = resp.getheader("Content-Type") or ""
        if ctype.startswith("application/json") and payload and not path.startswith("/files/"):
            payload = json.loads(payload)
        return resp.status, resp, payload

    def upload(self, name, data, pid=None):
        return self.call("PUT", "/api/projects/%s/upload?name=%s" % (pid or self.pid, urllib.parse.quote(name)), raw=data)

    def pdir(self):
        return os.path.join(self.tmp, "studio", "projects", self.pid)

    # ---- basics

    def test_health(self):
        status, _, data = self.call("GET", "/api/health")
        self.assertEqual(status, 200)
        self.assertEqual(data["app"], "viral-studio")
        self.assertIn("jobs", data)

    def test_page_and_assets_are_served(self):
        status, resp, body = self.call("GET", "/")
        self.assertEqual(status, 200)
        self.assertTrue(resp.getheader("Content-Type").startswith("text/html"))
        self.assertIn("script-src 'self'", resp.getheader("Content-Security-Policy"))
        self.assertIn(b"/app.js", body)
        for path, ctype in (("/app.js", "text/javascript"), ("/style.css", "text/css")):
            status, resp, body = self.call("GET", path)
            self.assertEqual(status, 200, path)
            self.assertTrue(resp.getheader("Content-Type").startswith(ctype))
            self.assertGreater(len(body), 1000)
        self.assertEqual(self.call("GET", "/nope")[0], 404)
        self.assertEqual(self.call("GET", "/nope")[2]["error"][:9], "not found")

    def test_projects(self):
        status, _, created = self.call("POST", "/api/projects", {"name": "Second <b>one</b>"})
        self.assertEqual(status, 201)
        self.assertRegex(created["id"], jobs.PROJECT_ID_RE.pattern)
        listing = self.call("GET", "/api/projects")[2]
        self.assertIn(created["id"], [p["id"] for p in listing])
        status, _, detail = self.call("GET", "/api/projects/" + created["id"])
        self.assertEqual(status, 200)
        for key in ("uploads", "jobs", "specs", "asks"):
            self.assertIn(key, detail)
        self.assertEqual(self.call("GET", "/api/projects/nope-0000")[0], 404)
        self.assertEqual(self.call("GET", "/api/projects/..%2F..")[0], 404)
        self.assertEqual(self.call("POST", "/api/projects", raw=b"{not json")[0], 400)
        self.assertEqual(self.call("DELETE", "/api/projects")[0], 405)

    # ---- uploads

    def test_upload_and_traversal(self):
        status, _, info = self.upload("clip.mp4", b"0123456789" * 100)
        self.assertEqual(status, 201)
        self.assertEqual(info["path"], "uploads/clip.mp4")
        self.assertEqual(info["size"], 1000)
        self.assertEqual(self.upload("clip.mp4", b"again")[2]["name"], "clip (2).mp4")
        for bad in ["../evil.txt", "..\\evil.txt", "a/b.txt", "/etc/passwd", "..", "%2e%2e%2fevil.txt"]:
            status, _, data = self.call("PUT", "/api/projects/%s/upload?name=%s" % (self.pid, urllib.parse.quote(bad, safe="%")), raw=b"x")
            self.assertEqual(status, 400, bad)
            self.assertIn("error", data)
        self.assertFalse(os.path.exists(os.path.join(self.pdir(), "evil.txt")))
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "studio", "projects", "evil.txt")))
        self.assertEqual(self.call("PUT", "/api/projects/%s/upload" % self.pid, raw=b"x")[0], 400)
        detail = self.call("GET", "/api/projects/" + self.pid)[2]
        self.assertIn("clip.mp4", [u["name"] for u in detail["uploads"]])
        status, _, _ = self.call("DELETE", "/api/projects/%s/upload?name=%s" % (self.pid, urllib.parse.quote("clip (2).mp4")))
        self.assertEqual(status, 200)
        self.assertEqual(self.call("DELETE", "/api/projects/%s/upload?name=..%%2Fproject.json" % self.pid)[0], 400)
        self.assertTrue(os.path.isfile(os.path.join(self.pdir(), "project.json")))

    def test_upload_size_limit(self):
        studio = self.httpd.studio
        old = studio.max_upload
        studio.max_upload = 100
        try:
            status, _, data = self.upload("big.bin", b"x" * 500)
            self.assertEqual(status, 413)
            self.assertIn("too large", data["error"])
        finally:
            studio.max_upload = old
        self.assertFalse(os.path.exists(os.path.join(self.pdir(), "uploads", "big.bin")))
        self.assertEqual([n for n in os.listdir(os.path.join(self.pdir(), "uploads")) if n.endswith(".part")], [])

    # ---- files

    def test_files_range_and_types(self):
        data = bytes(range(256)) * 40                        # 10240 bytes
        self.upload("movie.mp4", data)
        url = "/files/%s/uploads/movie.mp4" % self.pid
        status, resp, body = self.call("GET", url)
        self.assertEqual((status, body), (200, data))
        self.assertEqual(resp.getheader("Accept-Ranges"), "bytes")
        self.assertEqual(resp.getheader("Content-Type"), "video/mp4")
        status, resp, body = self.call("GET", url, headers={"Range": "bytes=100-199"})
        self.assertEqual(status, 206)
        self.assertEqual(body, data[100:200])
        self.assertEqual(resp.getheader("Content-Range"), "bytes 100-199/10240")
        status, _, body = self.call("GET", url, headers={"Range": "bytes=-16"})
        self.assertEqual((status, body), (206, data[-16:]))
        status, _, body = self.call("GET", url, headers={"Range": "bytes=10000-"})
        self.assertEqual((status, body), (206, data[10000:]))
        status, resp, _ = self.call("GET", url, headers={"Range": "bytes=999999-"})
        self.assertEqual(status, 416)
        self.assertEqual(resp.getheader("Content-Range"), "bytes */10240")
        status, resp, body = self.call("HEAD", url)
        self.assertEqual((status, body, resp.getheader("Content-Length")), (200, b"", "10240"))
        status, resp, _ = self.call("GET", url + "?download=1")
        self.assertIn("attachment", resp.getheader("Content-Disposition"))
        for name, ctype in (("a.png", "image/png"), ("a.jpg", "image/jpeg"), ("a.json", "application/json"),
                            ("a.md", "text/markdown"), ("a.srt", "application/x-subrip"), ("a.ass", "text/x-ssa"),
                            ("a.wav", "audio/wav"), ("a.html", "text/plain")):
            self.upload(name, b"{}")
            status, resp, _ = self.call("GET", "/files/%s/uploads/%s" % (self.pid, name))
            self.assertEqual(status, 200)
            self.assertTrue(resp.getheader("Content-Type").startswith(ctype), name)
            self.assertIn("sandbox", resp.getheader("Content-Security-Policy"))

    def test_files_traversal(self):
        other = self.call("POST", "/api/projects", {"name": "Other"})[2]["id"]
        for path in ["/files/%s/../%s/project.json" % (self.pid, other),
                     "/files/%s/%%2e%%2e/%%2e%%2e/%%2e%%2e/etc/passwd" % self.pid,
                     "/files/%s/uploads/..%%2f..%%2f..%%2fproject.json" % self.pid,
                     "/files/%s/C:%%5CWindows%%5Cwin.ini" % self.pid,
                     "/files/%s/%%2Fetc%%2Fpasswd" % self.pid]:
            status, _, _ = self.call("GET", path)
            self.assertIn(status, (403, 404), path)
            self.assertNotEqual(status, 200)
        self.assertEqual(self.call("GET", "/files/nope-0000/project.json")[0], 404)
        self.assertEqual(self.call("GET", "/files/%s/uploads" % self.pid)[0], 404)          # folders are not listed
        if os.name != "nt":
            secret = os.path.join(self.tmp, "secret.txt")
            with open(secret, "w") as f:
                f.write("secret")
            os.symlink(secret, os.path.join(self.pdir(), "uploads", "sneaky.txt"))
            self.assertEqual(self.call("GET", "/files/%s/uploads/sneaky.txt" % self.pid)[0], 403)

    # ---- request guards

    def test_host_header_rejected(self):
        status, _, data = self.call("GET", "/api/health", headers={"Host": "evil.example.com"})
        self.assertEqual(status, 403)
        self.assertIn("error", data)
        self.assertEqual(self.call("GET", "/", headers={"Host": "attacker.test:%d" % self.port})[0], 403)
        self.assertEqual(self.call("GET", "/api/health", headers={"Host": "localhost:%d" % self.port})[0], 200)

    def test_cross_site_requests_rejected(self):
        evil = {"Origin": "https://evil.example.com"}
        self.assertEqual(self.call("POST", "/api/projects", {"name": "x"}, headers=evil)[0], 403)
        self.assertEqual(self.call("POST", "/api/projects", {"name": "x"}, headers={"Origin": "null"})[0], 403)
        same = {"Origin": "http://127.0.0.1:%d" % self.port}
        self.assertEqual(self.call("POST", "/api/projects", {"name": "ok"}, headers=same)[0], 201)
        self.assertEqual(self.call("GET", "/api/projects", headers={"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "cors"})[0], 403)
        self.assertEqual(self.call("GET", "/files/%s/project.json" % self.pid, headers={"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "no-cors"})[0], 403)
        self.assertEqual(self.call("GET", "/", headers={"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "navigate"})[0], 200)
        _, resp, _ = self.call("GET", "/api/health")
        self.assertEqual(resp.getheader("Cross-Origin-Resource-Policy"), "same-origin")

    # ---- jobs

    def submit(self, kind, args, pid=None):
        return self.call("POST", "/api/projects/%s/jobs" % (pid or self.pid), {"kind": kind, "args": args})

    def wait_job(self, jid, timeout=15):
        return wait_for(lambda: (lambda j: j if j["status"] in jobs.FINAL else None)(self.call("GET", "/api/jobs/" + jid)[2]), timeout)

    def test_job_lifecycle(self):
        status, _, job = self.submit("render", {"sleep": 0.4})
        self.assertEqual(status, 201)
        self.assertIn(job["status"], ("queued", "running"))
        done = self.wait_job(job["id"])
        self.assertEqual(done["status"], "done")
        self.assertEqual(done["exit_code"], 0)
        self.assertEqual(done["progress"], 1.0)
        self.assertIn("jobs/%s/out/result.txt" % job["id"], [o["path"] for o in done["outputs"]])
        self.assertIn("4/4 frames", done["log_tail"])
        log = self.call("GET", "/api/jobs/%s/log?offset=0" % job["id"])[2]
        self.assertIn("== exit 0", log["text"])
        more = self.call("GET", "/api/jobs/%s/log?offset=%d" % (job["id"], log["offset"]))[2]
        self.assertEqual(more["text"], "")
        status, _, body = self.call("GET", "/files/%s/jobs/%s/out/result.txt" % (self.pid, job["id"]))
        self.assertEqual((status, body), (200, b"hi"))
        listing = self.call("GET", "/api/jobs?project=" + self.pid)[2]
        self.assertIn(job["id"], [j["id"] for j in listing])
        self.assertIn(job["id"], [j["id"] for j in self.call("GET", "/api/projects/%s/jobs" % self.pid)[2]])

    def test_job_failure_is_reported(self):
        job = self.submit("render", {"sleep": 0, "exit": 3})[2]
        done = self.wait_job(job["id"])
        self.assertEqual(done["status"], "failed")
        self.assertEqual(done["exit_code"], 3)
        self.assertIn("failed on purpose", done["error"])

    def test_cancel_running_job(self):
        job = self.submit("render", {"sleep": 30})[2]
        wait_for(lambda: self.call("GET", "/api/jobs/" + job["id"])[2]["status"] == "running")
        t0 = time.time()
        status, _, _ = self.call("POST", "/api/jobs/%s/cancel" % job["id"])
        self.assertEqual(status, 200)
        done = self.wait_job(job["id"], timeout=10)
        self.assertEqual(done["status"], "cancelled")
        self.assertLess(time.time() - t0, 8)
        pid = done["pid"]
        if os.name != "nt":
            gone = wait_for(lambda: not jobs.wrapper_alive(pid, job["id"]), 5)
            self.assertTrue(gone, "the wrapper process is gone")

    def test_queue_and_cancel_queued(self):
        first = self.submit("render", {"sleep": 2})[2]
        second = self.submit("render", {"sleep": 0})[2]
        got = self.call("GET", "/api/jobs/" + second["id"])[2]
        self.assertEqual(got["status"], "queued")
        self.assertEqual(self.call("POST", "/api/jobs/%s/cancel" % second["id"])[2]["status"], "cancelled")
        self.call("POST", "/api/jobs/%s/cancel" % first["id"])
        self.assertEqual(self.wait_job(first["id"])["status"], "cancelled")

    def test_bad_job_requests(self):
        self.assertEqual(self.submit("nope", {})[0], 400)
        status, _, data = self.submit("reel", {"prompt": "x", "refs": ["../../../../etc/passwd"]})
        self.assertEqual(status, 400)
        self.assertIn("project", data["error"])
        self.assertEqual(self.submit("reel", {"prompt": "x", "rm": "-rf"})[0], 400)
        self.assertEqual(self.submit("render", {"sleep": 0}, pid="nope-0000")[0], 404)
        self.assertEqual(self.call("POST", "/api/projects/%s/jobs" % self.pid, raw=b"[1,2")[0], 400)
        self.assertEqual(self.call("GET", "/api/jobs/20990101-000000-abcdef")[0], 404)
        self.assertEqual(self.call("GET", "/api/jobs/../../etc")[0], 404)
        self.assertEqual(self.call("POST", "/api/jobs/20990101-000000-abcdef/cancel")[0], 404)

    # ---- timeline specs and Ask Claude

    def put_spec(self, spec, name="spec.json"):
        self.upload(name, json.dumps(spec).encode())
        return "uploads/" + name

    @unittest.skipIf(timeline is None, "timeline module not available")
    def test_spec_get_put_versions(self):
        path = self.put_spec(VALID_SPEC, "edit.json")
        status, _, data = self.call("GET", "/api/projects/%s/spec?path=%s" % (self.pid, path))
        self.assertEqual(status, 200)
        self.assertEqual(data["errors"], [])
        self.assertEqual(data["kind"], "spec")
        self.assertEqual([v["version"] for v in data["versions"]], [1])
        bad = dict(VALID_SPEC, width=181)
        status, _, data = self.call("PUT", "/api/projects/%s/spec?path=%s" % (self.pid, path), bad)
        self.assertEqual(status, 422)
        self.assertTrue(any("even" in e for e in data["errors"]))
        self.assertEqual(self.call("PUT", "/api/projects/%s/spec?path=%s&force=1" % (self.pid, path), bad)[0], 200)
        status, _, data = self.call("PUT", "/api/projects/%s/spec?path=%s&version=new" % (self.pid, path), VALID_SPEC)
        self.assertEqual((status, data["path"], data["version"]), (200, "uploads/edit.v2.json", 2))
        versions = self.call("GET", "/api/projects/%s/spec?path=uploads/edit.v2.json" % self.pid)[2]["versions"]
        self.assertEqual([v["version"] for v in versions], [1, 2])
        self.assertEqual(self.call("PUT", "/api/projects/%s/spec?path=project.json" % self.pid, VALID_SPEC)[0], 403)
        self.assertEqual(self.call("PUT", "/api/projects/%s/spec?path=../../x.json" % self.pid, VALID_SPEC)[0], 403)
        self.assertEqual(self.call("GET", "/api/projects/%s/spec?path=uploads/none.json" % self.pid)[0], 404)
        specs = self.call("GET", "/api/projects/" + self.pid)[2]["specs"]
        self.assertIn("uploads/edit.v2.json", [s["path"] for s in specs])

    def with_llm(self, module):
        old = sys.modules.get("vstudio.llm")
        sys.modules["vstudio.llm"] = module
        self.addCleanup(lambda: sys.modules.__setitem__("vstudio.llm", old) if old is not None else sys.modules.pop("vstudio.llm", None))

    def fake_llm(self, answer, available=True):
        mod = types.ModuleType("vstudio.llm")
        mod.LLMUnavailable = type("LLMUnavailable", (Exception,), {})
        mod.calls = []
        mod.available = lambda: available

        def complete_json(prompt, schema=None, system=None, timeout=900, retries=2):
            mod.calls.append(prompt)
            return answer(prompt) if callable(answer) else answer
        mod.complete_json = complete_json
        return mod

    def test_ask_without_claude_is_503(self):
        path = self.put_spec(VALID_SPEC, "ask0.json")
        self.with_llm(self.fake_llm({}, available=False))
        status, _, data = self.call("POST", "/api/projects/%s/ask" % self.pid, {"message": "make it red", "spec_path": path})
        self.assertEqual(status, 503)
        self.assertIn("Claude Code", data["error"])
        self.assertEqual(self.call("POST", "/api/projects/%s/ask" % self.pid, {"message": "", "spec_path": path})[0], 400)

    @unittest.skipIf(timeline is None, "timeline module not available")
    def test_ask_saves_a_new_version(self):
        path = self.put_spec(VALID_SPEC, "ask1.json")
        changed = json.loads(json.dumps(VALID_SPEC))
        changed["layers"][0]["text"] = "HELLO"
        changed["layers"][0]["color"] = "#FF0000"
        answer = {"summary": "Made the title red.", "spec": changed}
        if templates is not None and "cta" in templates.CATALOG:
            answer["add_templates"] = [{"template": "cta", "params": {"text": "Follow"}, "start": 0.2, "end": 1.0}]
        llm = self.fake_llm(answer)
        self.with_llm(llm)
        status, _, data = self.call("POST", "/api/projects/%s/ask" % self.pid, {"message": "red title", "spec_path": path})
        self.assertEqual(status, 200, data)
        self.assertEqual(data["path"], "uploads/ask1.v2.json")
        self.assertEqual(data["summary"], "Made the title red.")
        with open(os.path.join(self.pdir(), "uploads", "ask1.v2.json"), encoding="utf-8") as f:
            saved = json.load(f)
        self.assertEqual(saved["layers"][0]["text"], "HELLO")
        if "add_templates" in answer:
            self.assertGreater(len(saved["layers"]), 1, "template layers were added")
        prompt = llm.calls[0]
        self.assertIn("red title", prompt)
        self.assertIn('"HI"', prompt)                          # the current spec went along
        if templates is not None:
            self.assertIn("hook_title", prompt)                # and the catalog
        asks = self.call("GET", "/api/projects/" + self.pid)[2]["asks"]
        self.assertEqual(asks[-1]["new_path"], "uploads/ask1.v2.json")

    @unittest.skipIf(timeline is None, "timeline module not available")
    def test_ask_invalid_answer_is_not_saved(self):
        path = self.put_spec(VALID_SPEC, "ask2.json")
        llm = self.fake_llm({"summary": "oops", "spec": dict(VALID_SPEC, width=181)})
        self.with_llm(llm)
        status, _, data = self.call("POST", "/api/projects/%s/ask" % self.pid, {"message": "x", "spec_path": path})
        self.assertEqual(status, 422)
        self.assertFalse(data["saved"])
        self.assertEqual(len(llm.calls), 2, "one retry with the errors")
        self.assertIn("even", llm.calls[1])
        self.assertFalse(os.path.exists(os.path.join(self.pdir(), "uploads", "ask2.v2.json")))

    def test_ask_cannot_point_at_files_outside_the_project(self):
        path = self.put_spec(VALID_SPEC, "ask3.json")
        outside = os.path.join(self.tmp, "outside.png")
        with open(outside, "wb") as f:
            f.write(b"not really a png")
        sneaky = json.loads(json.dumps(VALID_SPEC))
        sneaky["layers"].append({"type": "image", "src": outside, "start": 0, "end": 1})
        self.with_llm(self.fake_llm({"summary": "added a logo", "spec": sneaky}))
        status, _, data = self.call("POST", "/api/projects/%s/ask" % self.pid, {"message": "add my logo", "spec_path": path})
        self.assertEqual(status, 422)
        self.assertTrue(any("outside the project" in e for e in data["errors"]), data["errors"])
        self.assertFalse(os.path.exists(os.path.join(self.pdir(), "uploads", "ask3.v2.json")))

    @unittest.skipIf(timeline is None, "timeline module not available")
    def test_ask_cannot_reach_home_folder_or_font_files_outside_the_project(self):
        home = os.path.join(self.tmp, "home")
        os.makedirs(home, exist_ok=True)
        for name in ("private.png", "secret.ttf"):
            with open(os.path.join(home, name), "wb") as f:
                f.write(b"x")
        with open(os.path.join(self.pdir(), "uploads", "brand.ttf"), "wb") as f:
            f.write(b"x")
        words = [{"w": "HI", "start": 0.0, "end": 0.5}]
        cases = {
            "home_src": lambda s: s["layers"].append({"type": "image", "src": "~/private.png", "start": 0, "end": 1}),
            "font_abs": lambda s: s["layers"][0].update(font=os.path.join(home, "secret.ttf")),
            "font_home": lambda s: s["layers"][0].update(font="~/secret.ttf"),
            "caption_font": lambda s: s.update(captions={"words": words, "font": os.path.join(home, "secret.ttf")}),
            "caption_candidates": lambda s: s.update(captions={"words": words, "style": {
                "font_candidates": [os.path.join(home, "secret.ttf")]}}),
        }
        with mock.patch.dict(os.environ, {"HOME": home, "USERPROFILE": home}):
            for name, change in cases.items():
                path = self.put_spec(VALID_SPEC, "ask_%s.json" % name)
                sneaky = json.loads(json.dumps(VALID_SPEC))
                change(sneaky)
                self.with_llm(fake_llm_module({"summary": "added a logo", "spec": sneaky}))
                status, _, data = self.call("POST", "/api/projects/%s/ask" % self.pid, {"message": "logo", "spec_path": path})
                self.assertEqual(status, 422, name)
                self.assertTrue(any("outside the project" in e for e in data["errors"]), (name, data["errors"]))
                self.assertFalse(os.path.exists(os.path.join(self.pdir(), "uploads", "ask_%s.v2.json" % name)), name)
            # a font file that is in the project is fine
            path = self.put_spec(VALID_SPEC, "ask_font_ok.json")
            ok = json.loads(json.dumps(VALID_SPEC))
            ok["layers"][0]["font"] = "brand.ttf"
            self.with_llm(fake_llm_module({"summary": "brand font", "spec": ok}))
            status, _, data = self.call("POST", "/api/projects/%s/ask" % self.pid, {"message": "font", "spec_path": path})
            self.assertEqual(status, 200, data)

    def test_bookkeeping_files_are_protected_in_any_case(self):
        job = self.submit("render", {"sleep": 0})[2]
        self.wait_job(job["id"])
        jid = job["id"]
        url = "/api/projects/%s/spec?path=%%s" % self.pid
        for rel in ["project.json", "PROJECT.JSON", "Project.Json", "Chat.JSON", "x.json",
                    "jobs/%s/JOB.JSON" % jid, "jobs/%s/Job.json" % jid, "jobs/%s/Exit.json" % jid,
                    "jobs/%s/other.json" % jid]:
            status, _, data = self.call("PUT", url % urllib.parse.quote(rel), VALID_SPEC)
            self.assertEqual(status, 403, (rel, data))
        for rel in ["jobs/%s/JOB.JSON" % jid, "PROJECT.json"]:
            self.assertEqual(self.call("GET", url % urllib.parse.quote(rel))[0], 403, rel)
        names = os.listdir(os.path.join(self.pdir(), "jobs", jid))
        self.assertEqual(sorted(n for n in names if n.lower().endswith(".json")), ["exit.json", "job.json"])
        self.assertEqual(jobs.read_json(os.path.join(self.pdir(), "jobs", jid, "job.json"))["id"], jid)
        self.assertEqual(self.call("PUT", url % ("jobs/%s/out/spec.json" % jid), VALID_SPEC)[0], 200)
        self.assertEqual(self.call("PUT", url % "uploads/fine.json", VALID_SPEC)[0], 200)

    # ---- odd numbers in headers and queries

    def raw(self, request):
        """Send raw bytes; returns the status code of the answer (None when the connection just closed)."""
        with socket.create_connection(("127.0.0.1", self.port), timeout=10) as s:
            s.sendall(request)
            data = b""
            while True:
                try:
                    chunk = s.recv(65536)
                except (ConnectionResetError, socket.timeout):
                    break
                if not chunk:
                    break
                data += chunk
        while data.startswith(b"HTTP/1.1 100"):           # skip an interim "100 Continue"
            data = data.split(b"\r\n\r\n", 1)[1] if b"\r\n\r\n" in data else b""
        line = data.split(b"\r\n", 1)[0].split()
        return int(line[1]) if len(line) > 1 and line[1].isdigit() else None

    def test_odd_numbers_in_headers_and_queries(self):
        host = b"Host: 127.0.0.1:%d\r\nConnection: close\r\n" % self.port
        # '\xb2' is a superscript two: str.isdigit() says yes, int() says no
        self.assertEqual(self.raw(b"POST /api/projects HTTP/1.1\r\n" + host + b"Content-Length: \xb2\r\n\r\n"), 411)
        self.assertEqual(self.raw(b"GET /nope HTTP/1.1\r\n" + host + b"Content-Length: \xb2\r\n\r\n"), 404)
        self.assertEqual(self.raw(b"PUT /api/projects/%s/upload?name=a.bin HTTP/1.1\r\n" % self.pid.encode() + host +
                                  b"Expect: 100-continue\r\nContent-Length: \xb2\r\n\r\n"), 411)
        self.upload("range.mp4", b"0123456789")
        self.assertEqual(self.raw(b"GET /files/%s/uploads/range.mp4 HTTP/1.1\r\n" % self.pid.encode() + host +
                                  b"Range: bytes=" + b"9" * 5000 + b"-\r\n\r\n"), 416)
        self.assertEqual(self.call("GET", "/api/jobs?limit=%C2%B2")[0], 200)
        job = self.submit("render", {"sleep": 0})[2]
        self.wait_job(job["id"])
        status, _, data = self.call("GET", "/api/jobs/%s/log?offset=%%C2%%B2" % job["id"])
        self.assertEqual(status, 200)
        self.assertIn("== exit 0", data["text"])

    def test_long_non_ascii_upload_name(self):
        name = "कुछ " * 40 + ".mp4"           # 160 characters, 400+ bytes in UTF-8
        status, _, info = self.upload(name, b"abc")
        self.assertEqual(status, 201, info)
        self.assertTrue(info["name"].endswith(".mp4"))
        status, _, again = self.upload(name, b"abc")
        self.assertEqual(status, 201, again)
        self.assertTrue(again["name"].endswith(" (2).mp4"))
        self.assertTrue(os.path.isfile(os.path.join(self.pdir(), "uploads", again["name"])))

    # ---- catalog and doctor

    @unittest.skipIf(templates is None, "templates module not available")
    def test_templates(self):
        status, _, data = self.call("GET", "/api/templates")
        self.assertEqual(status, 200)
        self.assertIn("hook_title", data)
        self.assertIn("params", data["hook_title"])

    def test_doctor_endpoint(self):
        status, _, data = self.call("GET", "/api/doctor")
        self.assertEqual(status, 200)
        self.assertIsInstance(data, list)
        self.assertIn("ffmpeg", [c["name"] for c in data])


class StudioStateTest(unittest.TestCase):
    """The Studio object without HTTP: symlinked workspaces, partial uploads, disk errors, Windows file locks."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="vs_state_")
        self.studios = []
        self.old_llm = sys.modules.get("vstudio.llm")

    def tearDown(self):
        for st in self.studios:
            st.runner.close(cancel_running=True)
        if self.old_llm is None:
            sys.modules.pop("vstudio.llm", None)
        else:
            sys.modules["vstudio.llm"] = self.old_llm
        shutil.rmtree(self.tmp, ignore_errors=True)

    def studio(self, workspace=None):
        st = server.Studio(workspace or os.path.join(self.tmp, "ws"), quiet=True, builder=fake_builder)
        self.studios.append(st)
        return st

    def round_trip(self, st, pid):
        """Every path the spec API hands out is project-relative and opens again."""
        self.assertEqual(st.write_spec(pid, "uploads/s.json", VALID_SPEC)["path"], "uploads/s.json")
        data = st.read_spec(pid, "uploads/s.json")
        self.assertEqual(data["path"], "uploads/s.json")
        self.assertEqual([v["path"] for v in data["versions"]], ["uploads/s.json"])
        self.assertEqual(st.read_spec(pid, data["path"])["path"], "uploads/s.json")
        saved = st.write_spec(pid, data["path"], VALID_SPEC, new_version=True)
        self.assertEqual(saved["path"], "uploads/s.v2.json")
        self.assertEqual([v["path"] for v in st.read_spec(pid, saved["path"])["versions"]],
                         ["uploads/s.json", "uploads/s.v2.json"])
        with open(os.path.join(st.project_dir(pid), "uploads", "clip.mp4"), "wb") as f:
            f.write(b"x")
        self.assertIn("uploads/clip.mp4", [m["path"] for m in st.read_spec(pid, "uploads/s.json")["renders"]])
        self.assertIn("uploads/s.v2.json", [s["path"] for s in st.find_specs(pid)])
        changed = json.loads(json.dumps(VALID_SPEC))
        changed["layers"][0]["text"] = "HELLO"
        sys.modules["vstudio.llm"] = fake_llm_module({"summary": "said hello", "spec": changed})
        asked = st.ask(pid, "say hello", "uploads/s.json")
        self.assertEqual(asked["path"], "uploads/s.v3.json")
        self.assertIn("# Current spec (uploads/s.json)", sys.modules["vstudio.llm"].calls[0])
        self.assertIn("- clip.mp4 (video", sys.modules["vstudio.llm"].calls[0])

    @unittest.skipIf(os.name == "nt", "symlinks need extra rights on Windows")
    def test_workspace_behind_a_symlink(self):
        real = os.path.join(self.tmp, "private", "var")         # like macOS /var -> /private/var
        os.makedirs(real)
        os.symlink(real, os.path.join(self.tmp, "var"))
        st = self.studio(os.path.join(self.tmp, "var", "ws"))
        self.round_trip(st, st.create_project("x")["id"])

    @unittest.skipIf(os.name == "nt", "symlinks need extra rights on Windows")
    def test_project_folder_linked_from_another_drive(self):
        st = self.studio()
        pid = st.create_project("x")["id"]
        pdir = os.path.join(st.projects_dir, pid)
        moved = os.path.join(self.tmp, "external", pid)
        os.makedirs(os.path.dirname(moved))
        shutil.move(pdir, moved)
        os.symlink(moved, pdir)
        self.round_trip(st, pid)

    def test_stale_partial_uploads_are_removed_at_start(self):
        st = self.studio()
        pid = st.create_project("x")["id"]
        up = os.path.join(st.project_dir(pid), "uploads")
        stale, fresh, other = (os.path.join(up, n) for n in (".upload-aaaaaaaaaaaa.part", ".upload-bbbbbbbbbbbb.part",
                                                              "keep.part"))
        for p in (stale, fresh, other):
            with open(p, "wb") as f:
                f.write(b"x" * 100)
        old = time.time() - 3600
        os.utime(stale, (old, old))
        os.utime(other, (old, old))
        self.studio()                                       # the studio starts again on the same workspace
        self.assertFalse(os.path.exists(stale))
        self.assertTrue(os.path.exists(fresh), "an upload that is still being written is left alone")
        self.assertTrue(os.path.exists(other), "only the studio's own temp files are removed")

    def test_uploads_in_flight_are_removed_at_shutdown(self):
        st = self.studio()
        pid = st.create_project("x")["id"]
        gate, started = threading.Event(), threading.Event()

        class Slow(io.RawIOBase):
            sent = False

            def read(self, n=-1):
                if not self.sent:
                    self.sent = True
                    return b"x" * 10
                started.set()
                gate.wait(10)
                return b""

        errors = []
        t = threading.Thread(target=lambda: errors.append(self._try(st.save_upload, pid, "big.mp4", Slow(), 1000)))
        t.start()
        self.assertTrue(started.wait(10))
        up = os.path.join(st.project_dir(pid), "uploads")
        self.assertEqual(len([n for n in os.listdir(up) if n.endswith(".part")]), 1)
        st.drop_inflight_uploads()
        self.assertEqual([n for n in os.listdir(up) if n.endswith(".part")], [])
        gate.set()
        t.join(10)
        self.assertFalse(os.path.exists(os.path.join(up, "big.mp4")))

    @staticmethod
    def _try(fn, *args):
        try:
            return fn(*args)
        except Exception as e:
            return e

    def test_disk_full_during_upload_is_a_clear_error(self):
        st = self.studio()
        pid = st.create_project("x")["id"]
        real_open = open

        class Full(io.BytesIO):
            def write(self, b):
                raise OSError(errno.ENOSPC, "No space left on device")

        def fake_open(path, mode="r", *a, **kw):
            if os.path.basename(str(path)).startswith(".upload-"):
                real_open(path, mode, *a, **kw).close()
                return Full()
            return real_open(path, mode, *a, **kw)

        with mock.patch("vstudio.server.open", fake_open, create=True):
            with self.assertRaises(server.ApiError) as cm:
                st.save_upload(pid, "a.mp4", io.BytesIO(b"x" * 10), 10)
        self.assertEqual(cm.exception.status, 507)
        self.assertIn("disk", cm.exception.message)
        self.assertEqual(os.listdir(os.path.join(st.project_dir(pid), "uploads")), [])
        self.assertEqual(server.save_error(OSError(errno.ENAMETOOLONG, "File name too long"), "x.mp4").status, 400)

    def test_replace_waits_while_windows_holds_the_file(self):
        st = self.studio()
        pid = st.create_project("x")["id"]
        real = os.replace
        failures = []

        def held(src, dst):
            if os.path.basename(dst) in ("s.json", "c.mp4") and len([f for f in failures if f == dst]) < 3:
                failures.append(dst)
                raise PermissionError(13, "The process cannot access the file because it is being used")
            return real(src, dst)

        with mock.patch("os.replace", held):
            self.assertTrue(st.write_spec(pid, "uploads/s.json", VALID_SPEC)["saved"])
            info = st.save_upload(pid, "c.mp4", io.BytesIO(b"abc"), 3)
        self.assertEqual(len(failures), 6)
        self.assertEqual(info["name"], "c.mp4")
        up = os.path.join(st.project_dir(pid), "uploads")
        self.assertEqual(sorted(os.listdir(up)), ["c.mp4", "s.json"])


class ScriptJobTest(unittest.TestCase):
    """The script job (python -m vstudio.jobs script) run in-process with a stand-in vstudio.llm;
    scoring uses script-forge's real scriptcheck.py."""

    def setUp(self):
        self.out = tempfile.mkdtemp(prefix="vs_script_")
        self.old = sys.modules.get("vstudio.llm")

    def tearDown(self):
        if self.old is None:
            sys.modules.pop("vstudio.llm", None)
        else:
            sys.modules["vstudio.llm"] = self.old
        shutil.rmtree(self.out, ignore_errors=True)

    def run_script(self, *extra):
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = jobs.script_main(["--topic=why I stopped posting daily", "--out-dir", self.out, "--seconds", "20",
                                     "--angles", "2"] + list(extra))
        return code, buf.getvalue()

    def test_missing_llm_module_is_a_clear_error(self):
        sys.modules["vstudio.llm"] = None             # makes `import vstudio.llm` fail
        code, out = self.run_script()
        self.assertEqual(code, 3)
        self.assertIn("vstudio/llm.py", out)

    def test_writes_scores_and_fixes(self):
        drafts = {"title": "Posting less", "story": {"claim": "Less is more", "proof": "[YOUR NUMBER]", "turn": "Quality"},
                  "missing": ["your view counts before and after"],
                  "drafts": [{"angle": "contrarian", "alt_hooks": ["Stop posting daily."],
                              "beats": [{"say": "I stopped posting every day.", "on_screen": "I STOPPED", "shot": "close-up"},
                                        {"say": "And my views went up [YOUR NUMBER].", "on_screen": "VIEWS UP"},
                                        {"say": "But here's the part nobody tells you.", "on_screen": "THE PART"},
                                        {"say": "One good video beats five rushed ones. Every time.", "on_screen": "1 > 5"}]},
                             {"angle": "story", "beats": [{"say": "In today's fast-paced world, it is important to delve into consistency."}]}]}
        fixed = {"story": drafts["story"], "drafts": [dict(drafts["drafts"][0], angle="contrarian (fixed)")]}
        calls = []
        mod = types.ModuleType("vstudio.llm")
        mod.LLMUnavailable = type("LLMUnavailable", (Exception,), {})
        mod.available = lambda: True

        def complete_json(prompt, schema=None, system=None, timeout=900, retries=2):
            calls.append(prompt)
            return drafts if len(calls) == 1 else fixed
        mod.complete_json = complete_json
        sys.modules["vstudio.llm"] = mod
        code, out = self.run_script("--notes=I went from 7 to 3 posts a week", "--format=storytime")
        self.assertEqual(code, 0, out)
        self.assertIn("why I stopped posting daily", calls[0])
        self.assertIn("I went from 7 to 3 posts a week", calls[0])
        self.assertIn("## storytime", calls[0])                       # the format playbook went along
        with open(os.path.join(self.out, "script.md"), encoding="utf-8") as f:
            sheet = f.read()
        self.assertIn("| 0:00-", sheet)
        self.assertIn("I stopped posting every day.", sheet)
        self.assertIn("Fill these in before you shoot", sheet)
        with open(os.path.join(self.out, "score.json"), encoding="utf-8") as f:
            score = json.load(f)
        self.assertIn(score["verdict"], ("SHIP", "FIX", "REWRITE"))
        self.assertTrue(score["angle"].startswith("contrarian"), "the human-sounding draft beats the AI-sounding one")
        self.assertEqual(len(score["drafts"]), 2)
        self.assertIn("HOOK", score["checks"])
        self.assertEqual(len(calls), 1 + score["revisions"])
        self.assertIn("PROGRESS 100%", out)


class AddTemplatesTest(unittest.TestCase):
    """Ask Claude's add_templates never ships the catalog's sample content."""

    def test_samples_blanked_and_numbers_required(self):
        studio = server.Studio.__new__(server.Studio)     # _expand_templates needs no server state
        spec = {"width": 1080, "height": 1920, "fps": 30, "layers": []}
        problems = studio._expand_templates(spec, [
            {"template": "stat", "params": {}, "start": 0, "end": 2},
            {"template": "counter_reveal", "params": {"label": "subs"}, "start": 0, "end": 2},
            {"template": "source_card", "params": {"title": "My notes"}, "start": 0, "end": 2}])
        self.assertEqual(len(problems), 1)
        self.assertIn("counter_reveal needs value", problems[0])
        texts = " ".join(str(l.get("text")) for l in spec["layers"] if l["type"] in ("text", "counter"))
        self.assertIn("My notes", texts)
        for sample in ("73%", "2,000", "Survey"):
            self.assertNotIn(sample, texts)


class DoctorTest(unittest.TestCase):

    def test_check_shape(self):
        results = doctor.check()
        names = [r["name"] for r in results]
        for expected in ("Python", "ffmpeg", "ffprobe", "yt-dlp", "RapidOCR", "faster-whisper", "Face tracking", "Fonts",
                         "Claude Code", "ReelMimic (2D remakes)", "Disk space"):
            self.assertIn(expected, names)
        for r in results:
            self.assertEqual(set(r) >= {"name", "ok", "detail", "fix"}, True, r)
            self.assertIsInstance(r["ok"], bool)
            self.assertIsInstance(r["detail"], str)
            if r["ok"]:
                self.assertEqual(r["fix"], "")
            else:
                self.assertTrue(r["fix"], r)
        self.assertTrue(next(r for r in results if r["name"] == "Python")["ok"])

    def test_main_prints_a_table(self):
        import contextlib
        import io
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = doctor.main([])
        self.assertIn(code, (0, 1))
        self.assertIn("ffmpeg", out.getvalue())


# ----------------------------------------------------------------------------- the page in a real browser

UI_SCRIPT = r"""
const { chromium } = require("playwright");
(async () => {
  const [,, base, shot] = process.argv;
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1360, height: 900 } });
  const errors = [];
  page.on("console", m => { if (m.type() === "error") errors.push(m.text()); });
  page.on("pageerror", e => errors.push(e.message));
  await page.goto(base + "/#/projects");
  await page.waitForSelector(".nav-item");
  const views = await page.$$eval(".nav-item", els => els.map(e => e.dataset.view));
  const titles = {};
  for (const v of views) {
    await page.click(`.nav-item[data-view="${v}"]`);
    await page.waitForTimeout(350);
    titles[v] = await page.textContent("#page-title");
  }
  await page.click('.nav-item[data-view="reel"]');
  await page.fill("textarea.big", "test prompt");
  await page.waitForTimeout(200);
  await page.screenshot({ path: shot });
  console.log(JSON.stringify({ views, titles, errors }));
  await browser.close();
})().catch(e => { console.log(JSON.stringify({ fatal: String(e) })); process.exit(1); });
"""

UI_PY = r"""
import json, sys
from playwright.sync_api import sync_playwright
base, shot = sys.argv[1], sys.argv[2]
with sync_playwright() as p:
    b = p.chromium.launch()
    page = b.new_page(viewport={"width": 1360, "height": 900})
    errors = []
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(base + "/#/projects")
    page.wait_for_selector(".nav-item")
    views = page.eval_on_selector_all(".nav-item", "els => els.map(e => e.dataset.view)")
    titles = {}
    for v in views:
        page.click('.nav-item[data-view="%s"]' % v)
        page.wait_for_timeout(350)
        titles[v] = page.text_content("#page-title")
    page.click('.nav-item[data-view="reel"]')
    page.fill("textarea.big", "test prompt")
    page.screenshot(path=shot)
    print(json.dumps({"views": views, "titles": titles, "errors": errors}))
    b.close()
"""


def _ui_runner():
    """('python'|'node', env) when a headless Chromium can be driven here, else None."""
    env = dict(os.environ)
    if os.path.isdir("/opt/pw-browsers") and "PLAYWRIGHT_BROWSERS_PATH" not in env:
        env["PLAYWRIGHT_BROWSERS_PATH"] = "/opt/pw-browsers"
    if importlib.util.find_spec("playwright") is not None:
        return "python", env
    node = shutil.which("node")
    if not node:
        return None
    probe = subprocess.run([node, "-e", "require.resolve('playwright')"], capture_output=True, env=env)
    if probe.returncode != 0 and shutil.which("npm"):
        root = subprocess.run(["npm", "root", "-g"], capture_output=True, text=True).stdout.strip()
        if root:
            env["NODE_PATH"] = root
            probe = subprocess.run([node, "-e", "require.resolve('playwright')"], capture_output=True, env=env)
    return ("node", env) if probe.returncode == 0 else None


UI = _ui_runner()


@unittest.skipUnless(UI, "no Playwright (Python or Node) with a Chromium to drive the page")
class BrowserTest(unittest.TestCase):

    def test_every_section_loads_without_console_errors(self):
        tmp = tempfile.mkdtemp(prefix="vs_ui_")
        httpd = server.make_server("127.0.0.1", 0, os.path.join(tmp, "studio"), builder=fake_builder, quiet=True)
        threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        try:
            kind, env = UI
            shot = os.environ.get("VSTUDIO_UI_SHOT") or os.path.join(tmp, "studio.png")
            script = os.path.join(tmp, "ui." + ("js" if kind == "node" else "py"))
            with open(script, "w") as f:
                f.write(UI_SCRIPT if kind == "node" else UI_PY)
            argv = [shutil.which("node"), script] if kind == "node" else [sys.executable, script]
            r = subprocess.run(argv + ["http://127.0.0.1:%d" % httpd.server_address[1], shot],
                               capture_output=True, text=True, env=env, timeout=120)
            line = (r.stdout.strip().splitlines() or ["{}"])[-1]
            result = json.loads(line)
            if "Executable doesn't exist" in result.get("fatal", "") or "browserType.launch" in result.get("fatal", ""):
                self.skipTest("Playwright is installed but its Chromium is not: " + result["fatal"][:120])
            self.assertNotIn("fatal", result, r.stderr[-800:])
            self.assertEqual(result["errors"], [])
            self.assertEqual(len(result["views"]), 10)
            self.assertEqual(result["titles"]["timeline"], "Timeline")
            self.assertTrue(os.path.getsize(shot) > 10000)
        finally:
            httpd.studio.runner.close(cancel_running=True)
            httpd.shutdown()
            httpd.server_close()
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
