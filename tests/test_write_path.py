"""Write path (0.4.0 track B): note bodies, validation before any lock, provenance
fields, the hub excluded from dirty, and capture. Disposable hubs and repositories only."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest


CLI = Path(__file__).resolve().parents[1] / "rmem-kit" / "bin" / "rmem"
REJECT_AT_APPEND = '''
def validate_event(event):
    # Accept the preflight probe (all-zero event_id) and refuse the real append.
    if event.get("note") == "reject at append" and event.get("event_id", "0" * 32) != "0" * 32:
        return "refused by the test plugin"
    return ""
'''


class WritePathTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="rmem-write-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.hub = self.root / "hub"
        self.env = {k: v for k, v in os.environ.items() if not k.startswith(("RMEM_", "CODEX_", "CLAUDE", "GIT_"))}
        self.env.update(RMEM_HUB=str(self.hub), RMEM_SESSION="session-a", RMEM_AGENT="codex",
                        PYTHONDONTWRITEBYTECODE="1", GIT_CONFIG_NOSYSTEM="1",
                        GIT_AUTHOR_NAME="Rmem test", GIT_AUTHOR_EMAIL="rmem@example.test",
                        GIT_COMMITTER_NAME="Rmem test", GIT_COMMITTER_EMAIL="rmem@example.test")
        self.call("init", self.hub, "--project-name", "Write path test")

    def call(self, *args, ok=True, env=None, cwd=None, stdin=None):
        result = subprocess.run([sys.executable, str(CLI), *map(str, args)], cwd=cwd or self.root,
                                env=env or self.env, capture_output=True, text=True, timeout=30, input=stdin)
        if ok:
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout)
        return result

    def git(self, *args, cwd):
        result = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True,
                                timeout=15, env=self.env)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def events(self):
        return [json.loads(line) for line in (self.hub / "ledger.jsonl").read_text().splitlines()]

    def note_files(self):
        notes = self.hub / "notes"
        return sorted(p.relative_to(self.hub).as_posix() for p in notes.rglob("*") if p.is_file()) if notes.exists() else []

    def view(self, *args):
        return json.loads(self.call(*args).stdout)

    # Note bodies

    def test_note_file_is_stored_once_by_content_and_read_back_in_pages(self):
        text = "# Result\n\n" + "".join(f"- seed {n}: accuracy 0.{n:02d}\n" for n in range(600))
        body = self.root / "body.md"
        body.write_text(text)
        logged = self.call("log", "--id", "E1", "--note-file", body).stdout
        event = self.events()[-1]
        digest = hashlib.sha256(text.encode()).hexdigest()
        self.assertRegex(event["body_path"], r"^notes/\d{4}-\d{2}/" + digest[:16] + r"\.md$")
        self.assertEqual((event["body_sha256"], event["body_chars"]), (digest, len(text)))
        self.assertEqual(event["note"], "# Result")
        self.assertEqual((self.hub / event["body_path"]).read_text(), text)
        self.assertIn(f"read it with rmem show {event['event_id']} --field body", logged)
        self.call("log", "--id", "E2", "--note", "Same body, own summary", "--note-file", body)
        again = self.events()[-1]
        self.assertEqual((again["body_path"], again["note"]), (event["body_path"], "Same body, own summary"))
        self.assertEqual(self.note_files(), [event["body_path"]])
        pages, page = [], 1
        while True:
            view = self.view("show", "E1", "--body", "--page", page)
            self.assertTrue(view["verified"])
            pages.append(view["text"])
            if not view["next"]:
                break
            page += 1
        self.assertGreater(page, 1)
        self.assertEqual("".join(pages), text)
        self.assertIn("rmem show", self.view("show", "E1")["full_text"][0])
        self.assertEqual(self.view("catalog")["rows"][0]["body_chars"], len(text))
        self.call("check")

    def test_note_file_reads_standard_input(self):
        self.call("log", "--note-file", "-", stdin="first line\nsecond line\n")
        event = self.events()[-1]
        self.assertEqual(event["note"], "first line")
        self.assertEqual((self.hub / event["body_path"]).read_text(), "first line\nsecond line\n")

    def test_bad_input_fails_before_any_lock_and_leaves_nothing(self):
        before = (self.hub / "ledger.jsonl").read_bytes()
        long_note = self.call("log", "--note", "x" * 1001, ok=False).stderr
        self.assertIn("found 1,001 characters", long_note)
        self.assertIn("--note-file PATH", long_note)
        self.assertIn("several lines", self.call("log", "--note", "one\ntwo", ok=False).stderr)
        empty = self.root / "empty.md"
        empty.write_text("  \n")
        self.assertIn("is empty", self.call("log", "--note-file", empty, ok=False).stderr)
        self.assertIn("cannot open", self.call("log", "--note-file", self.root / "missing.md", ok=False).stderr)
        (self.root / "latin1.md").write_bytes(b"caf\xe9\n")
        self.assertIn("UTF-8", self.call("log", "--note-file", self.root / "latin1.md", ok=False).stderr)
        good = self.root / "good.md"
        good.write_text("fine\n")
        self.call("log", "--note-file", good, "--metric", "broken", ok=False)
        (self.hub / "hub-config.json").write_text(json.dumps({"notes": {"body_max_chars": 1000}}))
        good.write_text("y" * 1001)
        self.assertIn("notes.body_max_chars", self.call("log", "--note-file", good, ok=False).stderr)
        self.assertEqual((self.hub / "ledger.jsonl").read_bytes(), before)
        self.assertFalse((self.hub / "notes").exists())
        self.assertFalse((self.hub / ".rmem.lock").exists())

    def test_refused_write_removes_the_note_file_it_stored(self):
        body = self.root / "body.md"
        body.write_text("multi\nline\n")
        self.call("claim", "alpha", env={**self.env, "RMEM_SESSION": "owner"})
        before = (self.hub / "ledger.jsonl").read_bytes()
        self.call("log", "--thread", "alpha", "--note-file", body, ok=False)
        self.assertFalse((self.hub / "notes").exists())
        (self.hub / "bin/plugins/reject.py").write_text(REJECT_AT_APPEND)
        refused = self.call("log", "--note", "reject at append", "--note-file", body, ok=False).stderr
        self.assertIn("refused by the test plugin", refused)
        self.assertEqual((self.hub / "ledger.jsonl").read_bytes(), before)
        self.assertEqual(self.note_files(), [])
        self.assertFalse((self.hub / "notes").exists())

    def test_secret_looking_text_is_refused_before_it_reaches_the_ledger(self):
        self.call("claim", "alpha")
        hf = "hf_" + "AbCdEfGhIjKlMnOpQrStUvWxYz0123456789"
        assignment_value = "0123456789" + "abcdef" + "0123"
        cases = [("capture", "--id", "E5", "--thread", "alpha", "--host", "torch", "--",
                  "ssh", "torch", f"HF_TOKEN={hf} sbatch up.sh"),
                 ("capture", "--id", "E6", "--thread", "alpha", "--cmd", f"export WANDB_API_KEY={assignment_value}"),
                 ("log", "--note", "token ghp_" + "a1" * 15),
                 ("log", "--id", "E7", "--set", "api_key=sk-ant-" + "x9" * 12),
                 ("note", "remember sk-proj-" + "Q7" * 12)]
        before = (self.hub / "ledger.jsonl").read_bytes()
        for args in cases:
            with self.subTest(command=args[0]):
                refused = self.call(*args, ok=False).stderr
                self.assertIn("looks like a key, token or password", refused)
                self.assertNotIn(hf, refused)
                self.assertNotIn(assignment_value, refused)
        self.assertEqual((self.hub / "ledger.jsonl").read_bytes(), before)
        self.assertEqual(list((self.hub / "journal").glob("*.md")), [])
        self.call("log", "--note", "a reference to $HF_TOKEN is fine; so is max_new_tokens=512")
        self.call("log", "--note", "ghp_" + "b2" * 15 + " is a revoked example", "--allow-sensitive")
        ran = self.call("run", "--id", "E8", "--thread", "alpha", "--", sys.executable, "-c", "print(1)",
                        f"HF_TOKEN={hf}")
        self.assertIn("records it as <redacted>", ran.stderr)
        started = [e for e in self.events() if e["kind"] == "run_started"][-1]
        self.assertEqual((started["argv"][-1], started["redacted"]), ("HF_TOKEN=<redacted>", ["argv"]))
        self.assertNotIn(hf, (self.hub / "ledger.jsonl").read_text())

    def test_status_value_error_lists_the_accepted_values(self):
        error = self.call("log", "--status", "done", ok=False)
        self.assertEqual(error.returncode, 2)
        self.assertIn("unknown status 'done'", error.stderr)
        self.assertIn("keep, discard, crash or inconclusive", error.stderr)
        self.call("log", "--status", "keep", "--note", "accepted value")

    def test_changed_or_missing_note_files_are_reported(self):
        body = self.root / "body.md"
        body.write_text("original\n")
        self.call("log", "--id", "E1", "--note-file", body)
        stored = self.hub / self.events()[-1]["body_path"]
        stored.write_text("edited\n")
        changed = self.call("check", ok=False).stdout
        self.assertIn("no longer matches the SHA-256", changed)
        view = self.view("show", "E1", "--field", "body")
        self.assertFalse(view["verified"])
        self.assertIn("unverified", view["integrity"])
        stored.unlink()
        missing = self.call("check").stdout
        self.assertIn("1 note file(s) named by ledger events are missing", missing)
        self.assertIn("is missing from this copy", self.call("show", "E1", "--body", ok=False).stderr)
        (stored.parent / "0123456789abcdef.md").write_text("orphan\n")
        self.assertIn("not named by any event", self.call("check").stdout)

    def test_sync_commits_note_files(self):
        self.git("init", "-q", cwd=self.hub)
        body = self.root / "body.md"
        body.write_text("tracked body\n")
        self.call("log", "--note-file", body)
        self.call("sync", "-m", "memory with a note body")
        tracked = self.git("ls-files", cwd=self.hub).splitlines()
        self.assertIn(self.events()[-1]["body_path"], tracked)

    # Provenance

    def make_repository(self):
        code = self.root / "code"
        code.mkdir()
        self.git("init", "-q", cwd=code)
        (code / "train.py").write_text("print('train')\n")
        self.git("add", "train.py", cwd=code)
        self.git("commit", "-qm", "code", cwd=code)
        return code, self.git("rev-parse", "HEAD", cwd=code)

    def test_hub_inside_the_code_repository_does_not_make_it_dirty(self):
        code, head = self.make_repository()
        hub = code / ".research-memory"
        self.call("init", hub)
        env = {**self.env, "RMEM_HUB": str(hub)}
        self.call("log", "--note", "memory files are untracked", env=env, cwd=code)
        event = json.loads((hub / "ledger.jsonl").read_text().splitlines()[-1])
        self.assertEqual((event["commit"], event["dirty"]), (head, False))
        self.assertEqual(event["provenance"]["dirty_excludes"], [".research-memory"])
        self.assertEqual(Path(event["provenance"]["repository"]).resolve(), code)
        (code / "train.py").write_text("print('changed')\n")
        self.call("log", "--note", "code changed", env=env, cwd=code)
        self.assertTrue(json.loads((hub / "ledger.jsonl").read_text().splitlines()[-1])["dirty"])
        self.call("log", "--note", "hub outside", cwd=code)
        outside = self.events()[-1]
        self.assertTrue(outside["dirty"])
        self.assertNotIn("dirty_excludes", outside["provenance"])

    def test_code_ref_workdir_cmd_and_fields(self):
        code, head = self.make_repository()
        self.call("log", "--id", "R1", "--host", "torch", "--code-ref", "torch:/scratch/sf@8566a1d",
                  "--workdir", "/scratch/sf/run1", "--cmd", "sbatch --array=0-3 train.sh\n",
                  "--set", "seed=42", "--set", "lr=1e-4", "--set", "data.sha=abc123", "--set", "tag=007",
                  "--set", "big=123456789012345678901", cwd=code)
        event = self.events()[-1]
        self.assertEqual((event["code_ref"], event["commit"]), ("torch:/scratch/sf@8566a1d", "8566a1d"))
        self.assertNotIn("dirty", event)
        self.assertEqual(event["provenance"]["commit"], head)
        self.assertEqual((event["workdir"], event["cmd"]), ("/scratch/sf/run1", "sbatch --array=0-3 train.sh\n"))
        self.assertEqual(event["fields"], {"seed": 42, "lr": 0.0001, "data.sha": "abc123", "tag": "007",
                                           "big": "123456789012345678901"})
        hinted = self.call("log", "--id", "R2", "--host", "torch", "--note", "remote", cwd=code).stdout
        self.assertIn("comes from this machine's checkout", hinted)
        self.assertIn("--code-ref HOST:PATH@REVISION", hinted)
        self.call("log", "--id", "R3", "--host", "torch", "--no-provenance", "--note", "remote", cwd=code)
        skipped = self.events()[-1]
        self.assertEqual(skipped["provenance"]["skipped"], True)
        self.assertNotIn("commit", skipped)
        self.call("log", "--code-ref", "torch:/no/revision", "--note", "unknown revision")
        self.assertNotIn("commit", {k: v for k, v in self.events()[-1].items() if k != "provenance"})
        for bad in (("--set", "seed"), ("--set", "a=1", "--set", "a=2"), ("--set", "-bad=1"),
                    ("--cmd", "x", "--cmd-file", "y"), ("--workdir", "two\nlines")):
            self.call("log", *bad, ok=False)

    def test_long_command_text_goes_to_a_note_file(self):
        script = "".join(f"python eval.py --shard {n}\n" for n in range(300))
        path = self.root / "submit.sh"
        path.write_text(script)
        self.call("log", "--id", "C1", "--cmd-file", path, "--note", "submitted the evaluation shards")
        event = self.events()[-1]
        self.assertNotIn("cmd", event)
        self.assertEqual(event["cmd_chars"], len(script))
        self.assertEqual(self.view("show", "C1", "--field", "cmd", "--page-size", "8000")["chars"], len(script))
        first = self.view("show", "C1", "--field", "cmd")
        self.assertGreater(first["pages"], 1)
        pages = [self.view("show", "C1", "--field", "cmd", "--page", n)["text"] for n in range(1, first["pages"] + 1)]
        self.assertEqual("".join(pages), script)

    # Capture

    def test_capture_records_a_submitted_command_without_running_it(self):
        marker = self.root / "MARKER"
        self.call("claim", "alpha")
        result = self.call("capture", "--id", "E9", "--thread", "alpha", "--host", "torch", "--job-id", "123",
                           "--scheduler", "slurm", "--workdir", "/scratch/x", "--code-ref", "torch:/scratch/x@abc123",
                           "--set", "array=0-3", "--", "sh", "-c", f"touch {marker}")
        self.assertIn("captured (not executed):", result.stdout)
        self.assertFalse(marker.exists())
        event = self.events()[-1]
        self.assertEqual((event["kind"], event["status"], event["captured"]), ("exp", "running", True))
        self.assertEqual(event["argv"], ["sh", "-c", f"touch {marker}"])
        self.assertEqual(event["cmd"], f"sh -c 'touch {marker}'")
        self.assertEqual((event["job_id"], event["scheduler"], event["host"]), ("123", "slurm", "torch"))
        self.assertEqual((event["commit"], event["evidence_level"]), ("abc123", "reported"))
        self.assertEqual(event["fields"], {"array": "0-3"})
        self.call("capture", "--id", "E10", "--thread", "alpha", "--cmd", "ssh torch 'sbatch run.sh'",
                  "--status", "planned")
        self.assertEqual((self.events()[-1]["cmd"], self.events()[-1]["status"]), ("ssh torch 'sbatch run.sh'", "planned"))
        self.call("claim", "beta", env={**self.env, "RMEM_SESSION": "owner"})
        count = len(self.events())
        self.assertIn("exactly once", self.call("capture", "--id", "E11", "--thread", "alpha", ok=False).stderr)
        self.call("capture", "--id", "E11", "--thread", "alpha", "--cmd", "x", "--", "y", ok=False)
        self.call("capture", "--id", "E11", "--thread", "beta", "--", "true", ok=False)
        self.call("capture", "--thread", "alpha", "--", "true", ok=False)
        self.assertEqual(len(self.events()), count)
        self.assertRegex(self.call("recent", "-n", "1").stdout, re.escape("id=E10 | thread=alpha | status=planned"))


if __name__ == "__main__":
    unittest.main()
