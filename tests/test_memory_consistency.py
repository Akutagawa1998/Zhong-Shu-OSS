"""Ownership regression tests. Only temporary hubs/repos/processes are used."""
import datetime as dt
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid


SOURCE_CLI = Path(__file__).resolve().parents[1] / "rmem-kit/bin/rmem"


class MemoryConsistencyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="rmem-consistency-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.hub = self.root / "hub"
        self.session = "test-" + uuid.uuid4().hex
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("RMEM_")}
        self.env.update(RMEM_HUB=str(self.hub), PYTHONDONTWRITEBYTECODE="1")
        result = subprocess.run([sys.executable, str(SOURCE_CLI), "init", str(self.hub)],
                                cwd=self.root, env=self.env, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.cli = self.hub / "bin/rmem"
        self.ledger = self.hub / "ledger.jsonl"

    def run_cli(self, *arguments, succeeds=True, session=None, agent="test"):
        result = subprocess.run([sys.executable, str(self.cli), "--agent", agent,
                                 "--session", session or self.session, *map(str, arguments)],
                                cwd=self.root, env=self.env, text=True, capture_output=True, timeout=15)
        if succeeds:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def events(self):
        return [json.loads(line) for line in self.ledger.read_text().splitlines()]

    def write_events(self, events):
        self.ledger.write_text("".join(json.dumps(event) + "\n" for event in events))

    def metadata_path(self, slug="task"):
        return self.hub / "claims" / slug / "meta.json"

    def git(self, *arguments):
        return subprocess.run(["git", "-C", str(self.root), *arguments], check=True,
                              text=True, capture_output=True, timeout=15)

    def test_new_claim_binds_event_and_normal_release_and_reclaim_pass(self):
        self.assertTrue((self.hub / "bin/research_consistency.py").is_file())
        self.assertTrue((self.hub / "CONSISTENCY.md").is_file())
        self.run_cli("claim", "task")
        first = self.events()[-1]
        meta = json.loads(self.metadata_path().read_text())
        self.assertEqual(meta["claim_event_id"], first["event_id"])
        self.assertEqual(first["claim_metadata_version"], 1)
        before = self.ledger.read_bytes()
        self.run_cli("claim", "task")
        self.assertEqual(self.ledger.read_bytes(), before)
        self.run_cli("check", "--records")
        self.run_cli("release", "task")
        self.run_cli("claim", "task", session="next-owner")
        self.run_cli("check")

    def test_real_git_stash_detaches_ignored_claim_and_pop_reconciles(self):
        self.git("init", "--quiet")
        self.git("config", "user.name", "Disposable")
        self.git("config", "user.email", "fixture@example.invalid")
        self.git("add", "hub")
        self.git("commit", "--quiet", "-m", "Empty initialized hub")
        self.run_cli("claim", "task")
        claimed = self.ledger.read_bytes()
        self.git("stash", "push", "--include-untracked", "-m", "Simulated desktop stash")
        self.assertTrue(self.metadata_path().is_file())
        self.assertEqual(self.ledger.read_text(), "")
        check = self.run_cli("check", "--records", succeeds=False)
        self.assertIn("orphan claim", check.stdout)
        status = self.run_cli("status", "-n", "0")
        self.assertIn("ordinary mutations are blocked", status.stdout)
        self.run_cli("log", "--id", "NEW", "--note", "must not append", succeeds=False)
        self.assertEqual(self.ledger.read_text(), "")
        self.git("stash", "pop")
        self.assertEqual(self.ledger.read_bytes(), claimed)
        self.run_cli("check", "--records")

    def test_orphan_claim_blocks_hub_mutations_before_side_effects(self):
        self.run_cli("claim", "task")
        self.ledger.write_text("")
        marker = self.root / "must-not-run.txt"
        project = self.root / "other-project"
        project.mkdir()
        commands = [
            ("claim", "task"), ("claim", "another"), ("release", "task"),
            ("log", "--id", "NEW", "--note", "must not append"),
            ("note", "must not save"), ("link", project), ("sync",),
            ("run", "--id", "NO-RUN", "--thread", "task", "--", sys.executable,
             "-c", f"from pathlib import Path; Path({str(marker)!r}).write_text('bad')"),
        ]
        for arguments in commands:
            with self.subTest(command=arguments[0]):
                rejected = self.run_cli(*arguments, succeeds=False)
                self.assertIn("claim/ledger inconsistency", rejected.stderr)
                self.assertEqual(self.ledger.read_text(), "")
        self.assertFalse(marker.exists())
        self.assertEqual(list((self.hub / "journal").glob("*.md")), [])
        self.assertFalse((project / ".rmem").exists())
        self.assertFalse((self.hub / "runs/NO-RUN").exists())

    def test_missing_claim_file_requires_explicit_recovery_and_force_release_works(self):
        self.run_cli("claim", "task")
        original = self.events()[-1]
        shutil.rmtree(self.metadata_path().parent)
        result = self.run_cli("check", "--records", succeeds=False)
        self.assertIn("missing claim file", result.stdout)
        self.run_cli("claim", "task", succeeds=False)
        before = self.ledger.read_bytes()
        self.run_cli("release", "task", "--force", succeeds=False)
        self.assertEqual(self.ledger.read_bytes(), before)
        self.run_cli("release", "task", "--force", "--reason", "Inspected prior owner and process; close stranded claim")
        recovery = self.events()[-1]["consistency_recovery"]
        self.assertEqual(recovery["ledger_previous_claim"]["event_id"], original["event_id"])
        self.assertGreater(recovery["issue_count"], 0)
        self.run_cli("check", "--records")
        self.run_cli("claim", "task", session="replacement")
        self.run_cli("check", "--records")

    def test_wrong_file_owner_and_same_session_wrong_agent_are_rejected(self):
        self.run_cli("claim", "task")
        original = json.loads(self.metadata_path().read_text())
        for key, value in (("session", "stranger"), ("agent", "other-agent")):
            with self.subTest(key=key):
                changed = {**original, key: value}
                self.metadata_path().write_text(json.dumps(changed))
                result = self.run_cli("check", "--records", succeeds=False)
                self.assertIn("session/agent differs", result.stdout)
                self.run_cli("log", "--thread", "task", "--id", "BLOCKED", succeeds=False)
        self.metadata_path().write_text(json.dumps(original))
        self.run_cli("claim", "task", agent="other-agent", succeeds=False)
        self.run_cli("log", "--thread", "task", "--id", "BLOCKED", agent="other-agent", succeeds=False)

    def test_same_owner_older_claim_is_detected_by_exact_event_anchor(self):
        self.run_cli("claim", "task")
        first = self.ledger.read_bytes()
        self.run_cli("release", "task")
        self.run_cli("claim", "task")
        self.ledger.write_bytes(first)
        result = self.run_cli("check", "--records", succeeds=False)
        self.assertIn("claim_event_id", result.stdout)
        self.run_cli("claim", "task", succeeds=False)

    def test_force_takeover_and_force_release_keep_ownership_consistent(self):
        self.run_cli("claim", "task")
        self.run_cli("claim", "task", session="second", succeeds=False)
        self.run_cli("claim", "task", "--force", "--reason", "Owner handed off after process inspection", session="second")
        self.run_cli("check", "--records")
        meta = json.loads(self.metadata_path().read_text())
        self.assertEqual(meta["session"], "second")
        self.assertEqual(self.events()[-1]["previous_owner"]["session"], self.session)
        self.run_cli("release", "task", "--force", "--reason", "Reviewed second owner and release request")
        self.run_cli("check", "--records")

    def test_multiple_orphans_can_be_recovered_without_circular_lock(self):
        self.run_cli("claim", "first")
        self.run_cli("claim", "second")
        self.ledger.write_text("")
        self.run_cli("claim", "first", "--force", "--reason", "Reviewed first owner and intended history")
        self.run_cli("check", "--records", succeeds=False)
        self.run_cli("log", "--thread", "first", "--id", "STILL-BLOCKED", succeeds=False)
        self.run_cli("release", "second", "--force", "--reason", "Reviewed second stale claim")
        self.run_cli("check", "--records")
        self.run_cli("log", "--thread", "first", "--id", "NOW-ALLOWED")

    def test_session_bearing_legacy_claims_work_without_new_anchor_field(self):
        self.run_cli("claim", "task")
        event = self.events()[0]
        event.pop("claim_metadata_version")
        self.write_events([event])
        meta = json.loads(self.metadata_path().read_text())
        meta.pop("claim_event_id")
        self.metadata_path().write_text(json.dumps(meta))
        self.run_cli("check", "--records")
        self.run_cli("log", "--thread", "task", "--id", "LEGACY-COMPATIBLE")
        self.run_cli("release", "task")
        self.run_cli("check", "--records")

    def test_active_sessionless_legacy_needs_recovery_but_closed_history_is_valid(self):
        timestamp = dt.datetime.now(dt.timezone.utc).isoformat()
        claim = {"kind": "claim", "thread": "legacy", "agent": "old", "ts": timestamp}
        self.write_events([claim])
        path = self.metadata_path("legacy")
        path.parent.mkdir()
        path.write_text(json.dumps({"agent": "old", "ts": timestamp}))
        self.run_cli("check", "--records", succeeds=False)
        self.run_cli("claim", "legacy", "--force", "--reason", "Inspected legacy job and replaced unidentified owner")
        self.run_cli("release", "legacy")
        self.run_cli("check", "--records")
        closed = [{**claim}, {**claim, "kind": "release"}]
        self.write_events(closed)
        self.run_cli("check", "--records")

    def test_unauthorized_ledger_takeover_and_release_are_detected(self):
        self.run_cli("claim", "task")
        original = self.events()
        metadata = self.metadata_path().read_bytes()
        for kind in ("claim", "release"):
            with self.subTest(kind=kind):
                event = {**original[-1], "event_id": uuid.uuid4().hex, "sequence": 2,
                         "kind": kind, "session": "unrecorded-new-owner"}
                self.write_events(original + [event])
                if kind == "claim":
                    changed = json.loads(metadata)
                    changed.update(session=event["session"], claim_event_id=event["event_id"])
                    self.metadata_path().write_text(json.dumps(changed))
                else:
                    self.metadata_path().write_bytes(metadata)
                result = self.run_cli("check", "--records", succeeds=False)
                self.assertIn("without a recorded force/reason", result.stdout)

    def test_history_change_during_run_preserves_raw_output_but_blocks_final_append(self):
        self.run_cli("claim", "task")
        code = f"from pathlib import Path; Path({str(self.ledger)!r}).write_text(''); print('completed fixture command')"
        result = self.run_cli("run", "--id", "MID-RUN", "--thread", "task", "--", sys.executable, "-c", code, succeeds=False)
        self.assertIn("terminal run event not appended", result.stderr)
        logs = list((self.hub / "runs/MID-RUN").glob("*.log"))
        self.assertEqual(len(logs), 1)
        self.assertIn("completed fixture command", logs[0].read_text())
        self.assertEqual(self.ledger.read_text(), "")

    def test_normal_close_cannot_hide_unauthorized_ownership_history(self):
        for illegal_kind in ("claim", "release"):
            with self.subTest(illegal_kind=illegal_kind):
                self.run_cli("claim", "task")
                first = self.events()[-1]
                illegal = {**first, "event_id": uuid.uuid4().hex,
                           "sequence": first["sequence"] + 1,
                           "kind": illegal_kind, "session": "unrecorded-new-owner"}
                closing = {**first, "event_id": uuid.uuid4().hex,
                           "sequence": first["sequence"] + 2, "kind": "release",
                           "session": illegal["session"] if illegal_kind == "claim" else self.session}
                history = self.events() + [illegal, closing]
                self.write_events(history)
                shutil.rmtree(self.metadata_path().parent)
                result = self.run_cli("check", "--records", succeeds=False)
                self.assertIn("without a recorded force/reason", result.stdout)
                # A second orphaned close must not launder the same anomaly.
                self.write_events(history + [{**closing, "event_id": uuid.uuid4().hex,
                                               "sequence": closing["sequence"] + 1}])
                self.run_cli("check", "--records", succeeds=False)
                self.run_cli("note", "must not bypass bad ownership history", succeeds=False)
                self.run_cli("claim", "task", "--force", "--reason",
                             "Inspected closed ownership anomaly; explicitly rebind")
                self.assertGreater(self.events()[-1]["consistency_recovery"]["issue_count"], 0)
                self.run_cli("release", "task")
                self.run_cli("check", "--records")


if __name__ == "__main__":
    unittest.main()
