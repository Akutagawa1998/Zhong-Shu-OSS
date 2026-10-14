"""Thread lifecycle (seal, reopen, archive, roll), staleness and multi-hop artifact
routes. Disposable hubs only."""
import contextlib
import hashlib
import importlib.machinery
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest import mock


KIT = Path(__file__).resolve().parents[1] / "rmem-kit"
CLI = KIT / "bin" / "rmem"
SPEC = importlib.util.spec_from_file_location("artifact_routes_lifecycle", KIT / "bin" / "artifact_routes.py")
routes = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(routes)

THREAD = """# Thread: alpha

Preamble paragraph that is not inside a section.

## Results so far
E1 failed: the loss diverged at step 40 (negative result).

E2 kept: accuracy 0.81 with seed 1.

E3 kept: accuracy 0.83 with seed 2.

## Dead ends
```text
fenced line one

fenced line three
```

Second dead end.

## Next step
Run E4.
"""


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="rmem-lifecycle-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.hub = self.root / "hub"
        self.env = {k: v for k, v in os.environ.items() if not k.startswith(("RMEM_", "CODEX_", "CLAUDE", "GIT_"))}
        self.env.update(RMEM_HUB=str(self.hub), RMEM_SESSION="session-a", RMEM_AGENT="codex",
                        PYTHONDONTWRITEBYTECODE="1", RMEM_TZ="UTC")
        self.call("init", self.hub, "--project-name", "Lifecycle test")
        self.call("claim", "alpha")
        self.thread = self.hub / "threads" / "alpha.md"
        self.thread.write_text(THREAD, encoding="utf-8")

    def call(self, *args, ok=True, env=None):
        result = subprocess.run([sys.executable, str(CLI), *map(str, args)], cwd=self.root,
                                env=env or self.env, capture_output=True, text=True, timeout=30)
        if ok:
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout)
        return result

    def events(self):
        return [json.loads(line) for line in (self.hub / "ledger.jsonl").read_text().splitlines()]

    def snapshot(self):
        """Bytes of every file in the hub except the lock."""
        return {str(path.relative_to(self.hub)): path.read_bytes() for path in sorted(self.hub.rglob("*"))
                if path.is_file() and path.name != ".rmem.lock"}

    # Seal and reopen

    def test_seal_hides_the_thread_and_reopen_shows_it_again(self):
        self.call("claim", "beta")
        status = self.call("status").stdout
        self.assertIn("- alpha:", status)
        self.call("thread", "seal", "alpha", "--summary", "E3 kept; evidence in findings/alpha.md")
        status = self.call("status").stdout
        self.assertNotIn("- alpha:", status)
        self.assertIn("- beta:", status)
        self.assertIn("[1 sealed or archived thread(s) hidden; `rmem status --all` lists them", status)
        everything = self.call("status", "--all").stdout
        self.assertIn("- alpha:", everything)
        self.assertIn("· sealed", everything)
        self.assertIn("Run E4.", self.call("status", "--thread", "alpha").stdout)
        self.assertIn("already sealed", self.call("thread", "seal", "alpha", "--summary", "again").stdout)
        sealed = [e for e in self.events() if e["kind"] == "thread_sealed"]
        self.assertEqual(len(sealed), 1)
        self.assertEqual((sealed[0]["thread"], sealed[0]["previous_state"]), ("alpha", "active"))
        self.assertEqual(sealed[0]["note"], "E3 kept; evidence in findings/alpha.md")
        self.call("thread", "reopen", "alpha", "--note", "new data arrived")
        self.assertIn("- alpha:", self.call("status").stdout)
        self.assertIn("already active", self.call("thread", "reopen", "alpha").stdout)
        self.assertEqual(self.thread.read_text(), THREAD)
        self.assertIn("ok", self.call("check").stdout)

    def test_lifecycle_writes_need_the_thread_write_authorization(self):
        other = {**self.env, "RMEM_SESSION": "session-b"}
        refused = self.call("thread", "seal", "alpha", "--summary", "not mine", ok=False, env=other)
        self.assertIn("belongs to session session-a", refused.stderr)
        refused = self.call("thread", "archive", "alpha", "--summary", "not mine", "--apply", ok=False, env=other)
        self.assertIn("belongs to session session-a", refused.stderr)
        self.call("thread", "seal", "missing", "--summary", "x", ok=False)
        self.assertEqual(self.thread.read_text(), THREAD)
        self.assertFalse([e for e in self.events() if e["kind"].startswith("thread_")])

    # Archive

    def test_archive_is_a_dry_run_by_default(self):
        before = self.snapshot()
        output = self.call("thread", "archive", "alpha", "--summary", "E3 kept").stdout
        self.assertIn("dry run; nothing was changed", output)
        self.assertIn("Rerun with --apply", output)
        self.assertIn("pointer page that replaces the thread", output)
        self.assertEqual(self.snapshot(), before)

    def test_archive_keeps_exact_bytes_and_leaves_a_pointer_page(self):
        original = self.thread.read_bytes()
        digest = hashlib.sha256(original).hexdigest()
        output = self.call("thread", "archive", "alpha", "--summary", "E3 kept; E1 diverged", "--apply").stdout
        self.assertIn("archived alpha", output)
        event = [e for e in self.events() if e["kind"] == "thread_archived"][-1]
        relative = event["archived_path"]
        self.assertRegex(relative, r"^history/threads/alpha/\d{4}-\d{2}-\d{2}-" + digest[:12] + r"\.md$")
        self.assertEqual((self.hub / relative).read_bytes(), original)
        self.assertEqual((event["archived_sha256"], event["archived_bytes"]), (digest, len(original)))
        self.assertEqual(event["original_path"], "threads/alpha.md")
        index = json.loads((self.hub / "history/threads/SNAPSHOTS.json").read_text())
        entry = index["snapshots"][-1]
        self.assertEqual((entry["sha256"], entry["bytes"], entry["original_path"], entry["event_id"]),
                         (digest, len(original), "threads/alpha.md", event["event_id"]))
        self.assertEqual(entry["archived_path"], relative)
        page = self.thread.read_text()
        self.assertTrue(page.startswith("# Thread: alpha\n"))
        self.assertIn("Lifecycle: archived on", page)
        self.assertIn("Summary: E3 kept; E1 diverged", page)
        self.assertIn(f"](../{relative})", page)
        self.assertIn("Negative results, dead ends and corrections in the archived text stay in force", page)
        self.assertIn(event["event_id"], page)
        self.assertNotIn("- alpha:", self.call("status").stdout)
        self.assertIn("· archived", self.call("status", "--all").stdout)
        again = self.call("thread", "archive", "alpha", "--summary", "twice", "--apply", ok=False)
        self.assertIn("already archived", again.stderr)
        self.assertIn("already archived", self.call("thread", "archive", "alpha", "--summary", "dry", ok=False).stderr)
        self.assertIn("ok", self.call("check").stdout)

    def test_archive_refuses_a_destination_with_different_bytes(self):
        digest = hashlib.sha256(self.thread.read_bytes()).hexdigest()
        day = time.strftime("%Y-%m-%d", time.gmtime())
        destination = self.hub / "history/threads/alpha" / f"{day}-{digest[:12]}.md"
        destination.parent.mkdir(parents=True)
        destination.write_text("someone else's bytes")
        refused = self.call("thread", "archive", "alpha", "--summary", "x", "--apply", ok=False)
        self.assertIn("already exists with different bytes", refused.stderr)
        self.assertEqual(self.thread.read_text(), THREAD)
        self.assertEqual(destination.read_text(), "someone else's bytes")
        self.assertFalse([e for e in self.events() if e["kind"] == "thread_archived"])

    def test_an_edit_made_while_archiving_is_never_lost(self):
        if str(KIT / "bin") not in sys.path:
            sys.path.insert(0, str(KIT / "bin"))
        loader = importlib.machinery.SourceFileLoader("rmem_lifecycle_under_test", str(CLI))
        module = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
        loader.exec_module(module)
        lifecycle = sys.modules["research_lifecycle"]
        args = SimpleNamespace(slug="alpha", summary="done", note=None, apply=True, agent="codex", session="session-a")
        real_write, real_append = lifecycle.write_exact, module.core.append_locked

        def edit_after_the_copy(*arguments):
            real_write(*arguments)
            with self.thread.open("a", encoding="utf-8") as stream:
                stream.write("\nLATE EDIT: E999 kept\n")

        def edit_after_the_event(hub, event):
            recorded = real_append(hub, event)
            with self.thread.open("a", encoding="utf-8") as stream:
                stream.write("\nLATER EDIT\n")
            return recorded

        with mock.patch.dict(os.environ, self.env, clear=True), contextlib.redirect_stdout(io.StringIO()):
            with mock.patch.object(lifecycle, "write_exact", edit_after_the_copy), \
                    self.assertRaisesRegex(module.RmemError, "changed while it was being archived"):
                lifecycle.cmd_rewrite(args, module.core, "archive")
            self.assertIn("LATE EDIT", self.thread.read_text())
            self.assertEqual(list((self.hub / "history").rglob("*.md")), [])
            self.assertFalse([e for e in self.events() if e["kind"] == "thread_archived"])
            with mock.patch.object(module.core, "append_locked", edit_after_the_event), \
                    self.assertRaisesRegex(module.RmemError, "live file was left untouched"):
                lifecycle.cmd_rewrite(args, module.core, "archive")
        text = self.thread.read_text()
        self.assertIn("LATE EDIT", text)
        self.assertIn("LATER EDIT", text)
        archived = list((self.hub / "history/threads/alpha").glob("*.md"))
        self.assertEqual(len(archived), 1)
        self.assertIn("LATE EDIT", archived[0].read_text())
        self.assertNotIn("LATER EDIT", archived[0].read_text())
        archived[0].write_text("tampered\n")
        self.assertIn("no longer matches the SHA-256 in its event", self.call("check", ok=False).stdout)

    def test_reopen_after_archive_makes_the_pointer_page_visible(self):
        self.call("thread", "archive", "alpha", "--summary", "done", "--apply")
        self.call("thread", "reopen", "alpha")
        self.assertIn("- alpha:", self.call("status").stdout)
        states = [e.get("previous_state") for e in self.events() if e["kind"] == "thread_reopened"]
        self.assertEqual(states, ["archived"])

    # Roll

    def test_roll_is_a_dry_run_by_default(self):
        before = self.snapshot()
        output = self.call("thread", "roll", "alpha", "--keep", "1").stdout
        self.assertIn("dry run; nothing was changed", output)
        self.assertIn("## Results so far: 3 block(s), moves 2", output)
        self.assertIn("## Next step: 1 block(s), unchanged", output)
        self.assertEqual(self.snapshot(), before)

    def test_roll_keeps_the_last_blocks_per_section_and_archives_the_original(self):
        original = self.thread.read_bytes()
        output = self.call("thread", "roll", "alpha", "--keep", "1", "--note", "trim old results", "--apply").stdout
        self.assertIn("rolled alpha: moved 3 block(s)", output)
        event = [e for e in self.events() if e["kind"] == "thread_rolled"][-1]
        self.assertEqual((event["keep"], event["moved_blocks"], event["note"]), (1, 3, "trim old results"))
        self.assertEqual((self.hub / event["archived_path"]).read_bytes(), original)
        text = self.thread.read_text()
        # Content outside sections is untouched; each section keeps its heading and last block.
        self.assertTrue(text.startswith("# Thread: alpha\n\nPreamble paragraph that is not inside a section.\n\n## Results so far\n"))
        self.assertNotIn("E1 failed", text)
        self.assertNotIn("E2 kept", text)
        self.assertIn("E3 kept: accuracy 0.83 with seed 2.", text)
        self.assertNotIn("fenced line one", text)  # the fenced block moved as one block
        self.assertIn("Second dead end.", text)
        self.assertIn("## Next step\nRun E4.\n", text)
        links = [line for line in text.splitlines() if line.startswith("Older entries of this section")]
        self.assertEqual(len(links), 2)
        self.assertTrue(all(f"](../{event['archived_path']})" in line for line in links))
        self.assertIn("(2 blocks)", links[0])
        self.assertIn("(1 block)", links[1])
        index = json.loads((self.hub / "history/threads/SNAPSHOTS.json").read_text())
        self.assertEqual(index["snapshots"][-1]["action"], "roll")
        self.assertEqual(index["snapshots"][-1]["sha256"], hashlib.sha256(original).hexdigest())
        nothing = self.call("thread", "roll", "alpha", "--keep", "5", "--apply").stdout
        self.assertIn("nothing to roll", nothing)
        self.assertEqual(len([e for e in self.events() if e["kind"] == "thread_rolled"]), 1)
        self.assertIn("- alpha:", self.call("status").stdout)  # rolling does not change the lifecycle state

    def test_roll_keep_zero_leaves_only_headings_and_links(self):
        self.call("thread", "roll", "alpha", "--keep", "0", "--apply")
        text = self.thread.read_text()
        for heading in ("## Results so far\n", "## Dead ends\n", "## Next step\n"):
            self.assertIn(heading, text)
        self.assertNotIn("Run E4.", text)
        self.assertEqual(len([l for l in text.splitlines() if l.startswith("Older entries of this section")]), 3)

    # Staleness

    def log_events(self, number):
        for index in range(number):
            self.call("log", "--thread", "alpha", "--id", f"E{index + 10}", "--commit", "abc", "--note", "result")

    def test_check_warns_when_many_events_follow_the_last_thread_edit(self):
        (self.hub / "hub-config.json").write_text(json.dumps({"staleness": {"events_after_edit": 2}}))
        past = time.time() - 3600
        os.utime(self.thread, (past, past))
        self.log_events(3)
        check = self.call("check").stdout
        self.assertIn("warn: research_lifecycle: thread alpha has 3 ledger events since its file last changed", check)
        self.assertIn("staleness.events_after_edit = 2", check)
        self.call("thread", "seal", "alpha", "--summary", "done")
        self.assertNotIn("thread alpha has", self.call("check").stdout)

    def test_staleness_ignores_claims_and_a_fresh_edit(self):
        (self.hub / "hub-config.json").write_text(json.dumps({"staleness": {"events_after_edit": 1}}))
        past = time.time() - 3600
        os.utime(self.thread, (past, past))
        self.call("release", "alpha")
        self.call("claim", "alpha")
        self.assertNotIn("thread alpha has", self.call("check").stdout)
        self.log_events(2)
        self.thread.write_text(THREAD + "\nUpdated.\n")
        self.assertNotIn("thread alpha has", self.call("check").stdout)

    def test_staleness_uses_the_last_commit_time_in_git(self):
        if shutil.which("git") is None:
            self.skipTest("git is not installed")
        (self.hub / "hub-config.json").write_text(json.dumps({"staleness": {"events_after_edit": 1}}))
        git = ["git", "-C", str(self.hub), "-c", "user.name=Test", "-c", "user.email=test@example.invalid"]
        subprocess.run(git + ["init", "-q"], check=True, timeout=15)
        subprocess.run(git + ["add", "threads/alpha.md"], check=True, timeout=15)
        old = {**self.env, "GIT_AUTHOR_DATE": "2020-01-01T00:00:00+00:00", "GIT_COMMITTER_DATE": "2020-01-01T00:00:00+00:00"}
        subprocess.run(git + ["commit", "-q", "-m", "thread"], check=True, timeout=15, env=old)
        self.log_events(2)
        os.utime(self.thread, None)  # a fresh mtime alone (checkout, copy) does not count as an edit
        self.assertIn("thread alpha has 2 ledger events", self.call("check").stdout)
        self.thread.write_text(THREAD + "\nUncommitted edit.\n")
        self.assertNotIn("thread alpha has", self.call("check").stdout)

    def test_invalid_lifecycle_event_is_reported(self):
        with (self.hub / "ledger.jsonl").open("a") as stream:
            stream.write(json.dumps({"ts": "2026-10-14T00:00:00+00:00", "kind": "thread_archived",
                                     "thread": "alpha", "archived_path": "elsewhere.md"}) + "\n")
        check = self.call("check", ok=False).stdout
        self.assertIn("thread_archived requires archived_path under history/threads/", check)


class MultiHopRouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        (self.root / ".research-memory").mkdir()
        (self.root / "archive").mkdir()
        self.rows = []

    def route(self, old, new, data, role="historical_original"):
        self.rows.append(dict(original_path=old, current_path=new, bytes=len(data),
                              sha256=hashlib.sha256(data).hexdigest(), role=role))
        (self.root / ".research-memory/ARTIFACT_ROUTES.json").write_text(json.dumps(dict(schema_version=1, files=self.rows)))

    def test_two_hops_resolve_to_the_final_location(self):
        data = b"draft v3 bytes"
        self.route("drafts/paper.md", "writing/paper.md", data)
        self.route("writing/paper.md", "archive/paper.md", data)
        (self.root / "archive/paper.md").write_bytes(data)
        result = routes.resolve_artifact(self.root, "drafts/paper.md")
        self.assertEqual(result["path"], str(self.root / "archive/paper.md"))
        self.assertEqual(result["hops"], 2)
        self.assertEqual(result["route"], ["drafts/paper.md", "writing/paper.md", "archive/paper.md"])
        self.assertEqual(result["relative_links_base"], "drafts")
        self.assertEqual(routes.resolve_artifact(self.root, "drafts/paper.md", hashlib.sha256(data).hexdigest())["hops"], 2)

    def test_a_hop_whose_file_still_holds_the_bytes_stops_there(self):
        data = b"kept in place"
        self.route("a.md", "archive/a.md", data)
        self.route("archive/a.md", "archive/b.md", data)
        (self.root / "archive/a.md").write_bytes(data)
        self.assertEqual(routes.resolve_artifact(self.root, "a.md")["hops"], 1)

    def test_a_redirect_page_at_an_intermediate_path_is_followed(self):
        data = b"original evidence"
        self.route("plan.md", "writing/plan.md", data)
        self.route("writing/plan.md", "archive/plan.md", data)
        (self.root / "writing").mkdir()
        (self.root / "writing/plan.md").write_bytes(b"moved; see archive/plan.md")
        (self.root / "archive/plan.md").write_bytes(data)
        self.assertEqual(routes.resolve_artifact(self.root, "plan.md")["path"], str(self.root / "archive/plan.md"))

    def test_cycles_hop_limits_forks_and_missing_bytes_fail(self):
        data = b"looping"
        self.route("a.md", "b.md", data)
        self.route("b.md", "a.md", data)
        with self.assertRaisesRegex(ValueError, "cycle"):
            routes.resolve_artifact(self.root, "a.md")
        self.rows.clear()
        names = [f"n{i}.md" for i in range(routes.MAX_HOPS + 2)]
        for old, new in zip(names, names[1:]):
            self.route(old, new, data)
        with self.assertRaisesRegex(ValueError, "longer than"):
            routes.resolve_artifact(self.root, names[0])
        self.rows.clear()
        self.route("x.md", "y.md", data)
        self.route("y.md", "archive/one.md", data)
        self.route("y.md", "archive/two.md", data)
        with self.assertRaisesRegex(ValueError, "forks"):
            routes.resolve_artifact(self.root, "x.md")
        self.rows.clear()
        self.route("p.md", "q.md", data)
        self.route("q.md", "archive/r.md", data)
        with self.assertRaises(OSError):
            routes.resolve_artifact(self.root, "p.md")
        (self.root / "archive/r.md").write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "does not match"):
            routes.resolve_artifact(self.root, "p.md")


if __name__ == "__main__":
    unittest.main()
