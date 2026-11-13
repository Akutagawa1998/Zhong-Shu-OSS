"""CLI regression tests. All mutable state is created in disposable directories."""
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest


CLI = Path(__file__).resolve().parents[1] / "rmem-kit" / "bin" / "rmem"


class RmemTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="rmem-test-")
        self.root = Path(self.tmp.name).resolve()
        self.hub = self.root / "hub"
        self.env = {k: v for k, v in os.environ.items() if not k.startswith(("RMEM_", "CODEX_", "CLAUDE"))}
        self.env["RMEM_HUB"] = str(self.hub)
        self.env["RMEM_SESSION"] = "session-a"
        self.env["RMEM_AGENT"] = "codex"
        self.coverage = os.environ.get("RMEM_TEST_COVERAGE")
        self.call("init", str(self.hub), "--project-name", "Test research")

    def tearDown(self):
        self.tmp.cleanup()

    def command(self, *args):
        prefix = [sys.executable]
        if self.coverage:
            destination = Path(self.coverage)
            destination.mkdir(parents=True, exist_ok=True)
            prefix += ["-m", "coverage", "run", "--parallel-mode",
                       "--data-file", str(destination / ".coverage")]
        return prefix + [str(CLI), *map(str, args)]

    def call(self, *args, ok=True, env=None, cwd=None):
        result = subprocess.run(self.command(*args), cwd=cwd or self.root,
                                env=env or self.env, capture_output=True, text=True,
                                timeout=15)
        if ok:
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout)
        return result

    def events(self):
        return [json.loads(line) for line in (self.hub / "ledger.jsonl").read_text().splitlines()]

    def claim(self, slug="alpha"):
        self.call("claim", slug)

    def git(self, *args, cwd=None, ok=True):
        result = subprocess.run(["git", "-C", str(cwd or self.hub), *args],
                                capture_output=True, text=True, timeout=10)
        if ok:
            self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def git_init(self, path=None):
        path = path or self.hub
        self.git("init", "-q", cwd=path)
        self.git("config", "user.name", "Rmem test", cwd=path)
        self.git("config", "user.email", "rmem@example.test", cwd=path)

    def test_init_is_empty_self_contained_and_refuses_overwrite(self):
        self.assertIn("Test research", (self.hub / "STATE.md").read_text())
        self.assertEqual(self.events(), [])
        self.assertTrue((self.hub / "bin/rmem").is_file())
        self.assertTrue((self.hub / "SKILL.md").is_file())
        self.assertIn("authority", (self.hub / "MAP.md").read_text())
        self.assertFalse((self.hub / ".git").exists())
        self.call("init", str(self.hub), "--project-name", "Overwrite", ok=False)
        self.call("status")
        self.call("check")
        empty = self.root / "empty"
        empty.mkdir()
        self.call("init", str(empty))

    def test_git_clone_recovers_ignored_claim_directory(self):
        self.git_init()
        self.git("add", "--all")
        self.git("commit", "-qm", "initial reusable hub")
        restored = self.root / "restored"
        self.git("clone", "-q", str(self.hub), str(restored), cwd=self.root)
        self.assertFalse((restored / "claims").exists())
        restored_env = {**self.env, "RMEM_HUB": str(restored)}
        self.call("status", env=restored_env, cwd=restored)
        self.call("claim", "recovered", env=restored_env, cwd=restored)
        self.call("release", "recovered", env=restored_env, cwd=restored)
        self.call("check", env=restored_env, cwd=restored)
        # Missing memory is corruption, not an empty directory to recreate.
        shutil.rmtree(restored / "threads")
        self.call("status", env=restored_env, cwd=restored, ok=False)

    def test_session_identity_and_force_audit(self):
        no_session = {k: v for k, v in self.env.items() if k != "RMEM_SESSION"}
        self.call("claim", "alpha", env=no_session, ok=False)
        self.claim()
        self.call("claim", "alpha")
        other = {**self.env, "RMEM_SESSION": "session-b"}
        self.call("claim", "alpha", env=other, ok=False)
        self.call("release", "alpha", env=other, ok=False)
        self.call("claim", "alpha", "--force", env=other, ok=False)
        self.call("claim", "alpha", "--force", "--reason", "explicit recovery", env=other)
        forced = self.events()[-1]
        self.assertEqual(forced["previous_owner"]["session"], "session-a")
        self.assertEqual(forced["reason"], "explicit recovery")
        self.call("release", "alpha", env=other)

    def test_concurrent_claim_has_exactly_one_session_owner(self):
        procs = [subprocess.Popen(self.command("claim", "race"), cwd=self.root,
                                 env={**self.env, "RMEM_SESSION": f"session-{i}"},
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                 for i in range(6)]
        for p in procs:
            p.communicate(timeout=15)
        self.assertEqual(sum(p.returncode == 0 for p in procs), 1)
        self.assertEqual(len([e for e in self.events() if e["kind"] == "claim"]), 1)

    def test_traversal_and_symlinks_cannot_delete_external_files(self):
        victim = self.root / "victim"
        victim.mkdir()
        (victim / "keep").write_text("keep me")
        for slug in ("../../victim", str(victim), "..", "a/b"):
            self.call("release", slug, "--force", "--reason", "test", ok=False)
            self.call("claim", slug, ok=False)
        (self.hub / "claims/escape").symlink_to(victim, target_is_directory=True)
        self.call("release", "escape", "--force", "--reason", "test", ok=False)
        self.call("claim", "escape", "--force", "--reason", "test", ok=False)
        self.assertEqual((victim / "keep").read_text(), "keep me")
        (self.hub / "ledger.jsonl").unlink()
        (self.hub / "ledger.jsonl").symlink_to(victim / "keep")
        self.call("log", "--note", "do not write", ok=False)
        self.assertEqual((victim / "keep").read_text(), "keep me")

    def test_thread_logs_and_runs_are_refused_under_another_live_lease(self):
        self.claim()
        other = {**self.env, "RMEM_SESSION": "session-b"}
        self.call("log", "--thread", "alpha", "--note", "bad", env=other, ok=False)
        self.call("run", "--thread", "alpha", "--id", "E1", "--", sys.executable,
                  "-c", "raise SystemExit(0)", env=other, ok=False)
        self.call("log", "--thread", "alpha", "--id", "E1", "--status", "keep",
                  "--metric", "acc=0.9", "--note", "good")
        self.call("release", "missing", ok=False)

    def test_unique_logs_and_durable_start_events(self):
        self.claim()
        procs = [subprocess.Popen(self.command("run", "--thread", "alpha", "--id", "E1",
                     "--", sys.executable, "-c", f'import time; print("METRIC label={i}", flush=True); time.sleep(.2)'),
                     cwd=self.root, env=self.env, stdout=subprocess.PIPE,
                     stderr=subprocess.PIPE, text=True) for i in range(3)]
        for p in procs:
            output = p.communicate(timeout=15)
            self.assertEqual(p.returncode, 0, output)
        starts = [e for e in self.events() if e["kind"] == "run_started"]
        finals = [e for e in self.events() if e["kind"] == "run"]
        self.assertEqual(len(starts), 3)
        self.assertEqual(len({e["run_id"] for e in finals}), 3)
        self.assertEqual(len({e["log"] for e in finals}), 3)
        self.assertEqual(len(list((self.hub / "runs/E1").glob("*.log"))), 3)

    def test_argument_validation_happens_before_launch(self):
        self.claim()
        marker = self.root / "marker"
        script = f"from pathlib import Path; Path({str(marker)!r}).touch()"
        for extra in (("--grep", "["), ("--tail", "-1"), ("--max-lines", "-1"),
                      ("--max-lines", "1000000")):
            self.call("run", "--thread", "alpha", "--id", "E1", *extra,
                      "--", sys.executable, "-c", script, ok=False)
        self.assertFalse(marker.exists())
        self.assertFalse(any(e["kind"] == "run_started" for e in self.events()))

    def test_launch_failure_and_crash_are_recorded(self):
        self.claim()
        self.call("run", "--thread", "alpha", "--id", "E1", "--",
                  "/no/such/program", ok=False)
        self.assertEqual(self.events()[-1]["status"], "launch_failed")
        result = self.call("run", "--thread", "alpha", "--id", "E2", "--",
                          sys.executable, "-c", "print('explanation'); raise SystemExit(3)", ok=False)
        self.assertEqual(result.returncode, 3)
        self.assertIn("explanation", result.stdout)
        self.assertEqual(self.events()[-1]["exit_code"], 3)

    def test_interrupt_records_terminal_event_and_protects_live_claim(self):
        self.claim()
        p = subprocess.Popen(self.command("run", "--thread", "alpha", "--id", "E1",
                             "--", sys.executable, "-c", "import time; time.sleep(30)"),
                             cwd=self.root, env=self.env, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True)
        try:
            deadline = time.monotonic() + 8
            while not any(e["kind"] == "run_started" for e in self.events()):
                self.assertLess(time.monotonic(), deadline)
                time.sleep(.02)
            self.call("release", "alpha", ok=False)
            status = self.call("status", "-n", "0").stdout
            self.assertIn("completion unknown", status)
            self.assertIn("run_id=", status)
            self.assertIn("log=runs/E1/", status)
            p.send_signal(signal.SIGTERM)
            p.communicate(timeout=10)
            self.assertEqual(self.events()[-1]["status"], "interrupted")
            self.call("release", "alpha")
        finally:
            if p.poll() is None:
                p.kill()
                p.communicate()

    def test_git_provenance_is_captured_before_command(self):
        code = self.root / "code"
        code.mkdir()
        self.git_init(code)
        (code / "model.py").write_text("original")
        self.git("add", ".", cwd=code)
        self.git("commit", "-qm", "before", cwd=code)
        before = self.git("rev-parse", "HEAD", cwd=code)
        (code / "model.py").write_text("dirty starting code")
        self.claim()
        script = "import subprocess; subprocess.run(['git','add','.'],check=True); subprocess.run(['git','commit','-qm','after'],check=True)"
        self.call("run", "--thread", "alpha", "--id", "E1", "--",
                  sys.executable, "-c", script, cwd=code)
        result = self.events()[-1]
        self.assertEqual(result["commit"], before)
        self.assertEqual(len(result["commit"]), 40)
        self.assertTrue(result["dirty"])
        self.assertNotEqual(before, self.git("rev-parse", "HEAD", cwd=code))

    def test_output_bounds_include_single_huge_line_and_zero_counts(self):
        self.claim()
        result = self.call("run", "--thread", "alpha", "--id", "E1", "--",
                           sys.executable, "-c", "print('METRIC '+ 'x'*1000000)")
        self.assertLessEqual(len(result.stdout), 16000)
        self.assertIn("truncated", result.stdout)
        self.assertEqual(self.call("recent", "-n", "0").stdout, "")
        self.assertNotIn("| claim |", self.call("status", "-n", "0").stdout)
        (self.hub / "STATE.md").write_text("state\n" * 10000)
        self.assertLessEqual(len(self.call("status").stdout), 16000)
        self.call("check", ok=False)

    def test_check_detects_corrupt_shape_and_empty_claim(self):
        (self.hub / "claims/empty").mkdir()
        with (self.hub / "ledger.jsonl").open("a") as stream:
            stream.write('not json\n[]\n{"kind":3}\n')
        result = self.call("check", ok=False)
        self.assertIn("empty", result.stdout)
        self.assertIn("ledger", result.stdout)
        self.call("status")
        self.call("claim", "empty", ok=False)
        before = (self.hub / "ledger.jsonl").read_bytes()
        self.call("claim", "empty", "--force", "--reason", "recover incomplete claim", ok=False)
        self.assertEqual((self.hub / "ledger.jsonl").read_bytes(), before)

    def test_incomplete_claim_can_be_recovered_with_valid_ledger(self):
        (self.hub / "claims/empty").mkdir()
        self.call("claim", "empty", ok=False)
        self.call("claim", "empty", "--force", "--reason", "reviewed incomplete claim")
        self.call("check")

    def test_import_is_idempotent_and_conflicts_do_not_append(self):
        source = self.root / "old.log"
        source.write_text("acc=0.91\n")
        args = ("log", "--kind", "import", "--id", "E-old", "--source-namespace", "archive",
                "--source-id", "job-17", "--source", str(source), "--metric", "acc=0.91")
        self.call(*args)
        self.call(*args, env={**self.env, "RMEM_AGENT": "claude"})
        self.assertEqual(len(self.events()), 1)
        event = self.events()[0]
        self.assertEqual(len(event["sources"][0]["sha256"]), 64)
        self.assertNotIn("commit", event)
        self.assertEqual(event["evidence_level"], "reported")
        source.write_text("acc=0.4\n")
        self.call(*args, ok=False)
        self.assertEqual(len(self.events()), 1)
        self.call("log", "--kind", "import", "--source", str(source), ok=False)

    def test_import_survives_relocation_renaming_and_source_reordering(self):
        first = self.root / "first-project"
        first.mkdir()
        self.hub.rename(first / "hub")
        self.hub = first / "hub"
        self.env["RMEM_HUB"] = str(self.hub)
        (first / "a.log").write_text("acc=0.91\n")
        (first / "z.cfg").write_text("seed=3\n")
        common = ("log", "--kind", "import", "--id", "E-old",
                  "--source-namespace", "archive", "--source-id", "run-17",
                  "--event-time", "2020-01-02T03:04:05Z", "--supersedes", "legacy:17",
                  "--note", "reported historical result", "--host", "old-worker",
                  "--artifact", "archive:run-17/weights")
        self.call(*common, "--metric", "acc=0.91", "--source", "a.log", "--source", "z.cfg", cwd=first)
        original_receipt = (self.hub / "ledger.jsonl").read_bytes()
        original_sources = self.events()[0]["sources"]

        second = self.root / "second-project"
        shutil.copytree(first, second)
        (second / "a.log").rename(second / "z-renamed.log")
        (second / "z.cfg").rename(second / "a-renamed.cfg")
        shutil.copyfile(second / "z-renamed.log", second / "duplicate.log")
        relocated_env = {**self.env, "RMEM_HUB": str(second / "hub"),
                         "RMEM_SESSION": "different-session", "RMEM_AGENT": "claude"}
        moved_sources = ("--source", "z-renamed.log", "--source", "duplicate.log",
                         "--source", "a-renamed.cfg")
        result = self.call(*common, "--metric", "acc=0.91", *moved_sources,
                           cwd=second, env=relocated_env)
        self.assertIn("already imported", result.stdout)
        self.assertEqual((second / "hub/ledger.jsonl").read_bytes(), original_receipt)
        recorded = json.loads((second / "hub/ledger.jsonl").read_text())
        self.assertEqual(recorded["sources"], original_sources)

        self.call(*common, "--metric", "acc=0.4", *moved_sources,
                  cwd=second, env=relocated_env, ok=False)
        (second / "z-renamed.log").write_text("different evidence\n")
        self.call(*common, "--metric", "acc=0.91", *moved_sources,
                  cwd=second, env=relocated_env, ok=False)
        self.assertEqual((second / "hub/ledger.jsonl").read_bytes(), original_receipt)
        self.assertEqual((self.hub / "ledger.jsonl").read_bytes(), original_receipt)

    def test_relative_links_relocate_and_preserve_existing_text(self):
        project = self.root / "project with spaces"
        project.mkdir()
        (project / "AGENTS.md").write_text("# Existing\nKeep this.  \n")
        (project / "CLAUDE.md").write_text("# Claude\nKeep me too.\n")
        self.call("link", str(project))
        first = (project / "AGENTS.md").read_text()
        self.assertTrue(first.startswith("# Existing\nKeep this.  \n"))
        self.assertFalse(Path((project / ".rmem").read_text().strip()).is_absolute())
        self.call("link", str(project))
        self.assertEqual(first, (project / "AGENTS.md").read_text())
        moved = self.root.parent / (self.root.name + "-moved")
        self.root.rename(moved)
        try:
            clean = {k: v for k, v in self.env.items() if k != "RMEM_HUB"}
            self.call("status", env=clean, cwd=moved / project.name)
        finally:
            moved.rename(self.root)

    def test_link_validation_precedes_writes_and_relink_requires_replace(self):
        project = self.root / "project"
        project.mkdir()
        malformed = "Existing\n<!-- RMEM:START -->\nunfinished\n"
        (project / "AGENTS.md").write_text(malformed)
        self.call("link", str(project), ok=False)
        self.assertFalse((project / ".rmem").exists())
        self.assertEqual((project / "AGENTS.md").read_text(), malformed)
        (project / "AGENTS.md").write_text("Existing\n")
        (project / ".rmem").write_text("../other-hub\n")
        self.call("link", str(project), ok=False)
        self.call("link", str(project), "--replace")
        before = (project / "AGENTS.md").read_text()
        (project / "CLAUDE.md").unlink()
        (project / "CLAUDE.md").symlink_to("AGENTS.md")
        self.call("link", str(project), ok=False)
        self.assertEqual((project / "AGENTS.md").read_text(), before)

    def test_sync_stages_memory_only_and_reports_failures(self):
        self.call("sync", ok=False)
        self.git_init()
        (self.hub / "unrelated.txt").write_text("do not commit")
        self.git("add", "unrelated.txt")
        self.call("sync", ok=False)
        self.assertFalse(self.git("rev-parse", "--verify", "HEAD", ok=False))
        self.git("rm", "--cached", "unrelated.txt")
        self.claim()
        self.call("sync", "-m", "memory snapshot")
        tracked = self.git("ls-files").splitlines()
        self.assertNotIn("unrelated.txt", tracked)
        self.assertFalse(any(p.startswith("claims/") for p in tracked))
        self.call("sync")
        (self.hub / "STATE.md").write_text("changed\n")
        hook = self.hub / ".git/hooks/pre-commit"
        hook.write_text("#!/bin/sh\nexit 1\n")
        hook.chmod(0o755)
        self.call("sync", ok=False)

    def test_standalone_sync_checks_the_snapshot_and_preserves_staging_on_refusal(self):
        self.git_init()
        self.call("sync")
        old = self.git("rev-parse", "HEAD")
        state = self.hub / "STATE.md"
        state.write_text("# Safe staged state\n")
        self.git("add", "STATE.md")
        index = (self.hub / ".git/index").read_bytes()
        value = "fixture" + "-123456"
        state.write_text(f'# Draft\npassword = "{value}"\n')
        refused = self.call("sync", ok=False).stderr
        self.assertIn("STATE.md (the content looks like", refused)
        self.assertNotIn(value, refused)
        self.assertEqual(self.git("rev-parse", "HEAD"), old)
        self.assertEqual((self.hub / ".git/index").read_bytes(), index)
        self.assertEqual(self.git("show", ":STATE.md").strip(), "# Safe staged state")
        state.write_text("# Safe final state\n")
        self.call("sync")
        self.assertEqual(self.git("show", "HEAD:STATE.md").strip(), "# Safe final state")

    def test_metadata_notes_and_filtered_queries(self):
        self.claim()
        self.call("note", "remember this")
        self.call("log", "--thread", "alpha", "--id", "E1", "--status", "running",
                  "--scheduler", "slurm", "--job-id", "123", "--host", "torch",
                  "--evidence-level", "reported", "--artifact", "torch:/scratch/job.log",
                  "--note", "pending result", "--metric", "label=pending")
        event = self.events()[-1]
        self.assertEqual(event["job_id"], "123")
        self.assertEqual(event["host"], "torch")
        self.assertIn("torch:/scratch/job.log", event["artifacts"])
        recent = self.call("recent", "--thread", "alpha", "--kind", "exp", "--grep", "pending")
        self.assertIn("pending", recent.stdout)
        self.call("status", "--thread", "alpha")
        self.call("recent", "--grep", "[", ok=False)
        self.call("log", "--metric", "broken", ok=False)
        self.call("log", "--metric", "bad=nan", ok=False)

    def test_git_is_optional_and_notes_validate_before_mutation(self):
        without_git = {**self.env, "PATH": str(self.root / "empty-path")}
        self.call("log", "--note", "works without git", env=without_git)
        self.assertEqual(self.events()[-1]["provenance"]["git_error"], "git executable unavailable")
        self.call("claim", "alpha", "--note", "x" * 9000, ok=False)
        self.assertFalse((self.hub / "claims/alpha").exists())
        self.claim()
        self.call("release", "alpha", "--note", "x" * 9000, ok=False)
        self.assertTrue((self.hub / "claims/alpha/meta.json").exists())
        self.call("run", "--thread", "alpha", "--id", "E1", "--", sys.executable,
                  "-c", "print('METRIC okay=1')", env=without_git)

    def test_event_time_and_source_locators_are_preserved(self):
        (self.root / "old.log").write_text("historical evidence")
        self.call("log", "--kind", "import", "--source-namespace", "archive",
                  "--source-id", "original", "--source", "old.log",
                  "--event-time", "2020-01-02T03:04:05Z", "--supersedes", "legacy:record-1")
        event = self.events()[-1]
        self.assertEqual(event["event_time"], "2020-01-02T03:04:05+00:00")
        self.assertNotEqual(event["event_time"], event["ts"])
        self.assertEqual(event["sources"][0]["relative_path"], "old.log")
        self.assertEqual(event["sources"][0]["base"], str(self.root))
        self.assertEqual(event["supersedes"], "legacy:record-1")
        for invalid in ("yesterday", "2h"):
            self.assertIn("--event-time", self.call("log", "--event-time", invalid, ok=False).stderr)
        # A time without an offset is read in the display zone, as CONFIG.md promises for every time option.
        self.call("log", "--event-time", "2020-01-02 03:04", env={**self.env, "RMEM_TZ": "America/Los_Angeles"})
        self.assertEqual(self.events()[-1]["event_time"], "2020-01-02T03:04:00-08:00")

    def test_metadata_shape_and_unicode_claim_notes(self):
        self.call("claim", "unicode", "--note", "🧪" * 1000)
        self.call("check")
        self.call("release", "unicode")
        malformed = [{"argv": "not a list"}, {"artifacts": [5]}, {"provenance": []},
                     {"dirty": "yes"}, {"exit_code": True}, {"secs": -1},
                     {"evidence_level": "certain"}]
        with (self.hub / "ledger.jsonl").open("a") as stream:
            for fields in malformed:
                stream.write(json.dumps({"ts": "2020-01-01T00:00:00+00:00", "kind": "run", **fields}) + "\n")
        result = self.call("check", ok=False)
        self.assertEqual(result.stdout.count("! ledger:"), len(malformed))
        self.call("status")


if __name__ == "__main__":
    unittest.main()
