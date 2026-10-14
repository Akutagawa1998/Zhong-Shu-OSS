"""Immutable highlight integration tests, using only disposable repositories."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import uuid


CLI = Path(__file__).resolve().parents[1] / "rmem-kit/bin/rmem"


class MemoryHighlightsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="memory-highlights-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.repo = self.root / "source"
        self.repo.mkdir()
        self.git("init", "--quiet")
        self.git("config", "user.name", "Test Fixture")
        self.git("config", "user.email", "fixture@example.invalid")
        (self.repo / "README.md").write_text("Disposable fixture.\n")
        self.git("add", "README.md")
        self.git("commit", "--quiet", "-m", "Initial fixture")
        self.hub = self.repo / ".memory"
        self.session = "fixture-" + uuid.uuid4().hex
        self.env = {key: value for key, value in os.environ.items() if not key.startswith("RMEM_")}
        self.env.update(RMEM_HUB=str(self.hub), PYTHONDONTWRITEBYTECODE="1")
        self.invoke(CLI, "init", self.hub)
        self.cli = self.hub / "bin/rmem"
        self.source = self.repo / "notes/result.md"
        self.source.parent.mkdir()
        self.source.write_text("Result is reported, not independently verified.\n")
        self.spec_path = self.root / "reviewed-spec.json"

    def git(self, *arguments):
        return subprocess.run(["git", "-C", str(self.repo), *arguments], text=True,
                              capture_output=True, check=True, timeout=15).stdout.strip()

    def invoke(self, cli, *arguments, succeeds=True, session=None):
        result = subprocess.run([sys.executable, str(cli), "--agent", "test", "--session",
                                 session or self.session, *map(str, arguments)],
                                cwd=self.repo, env=self.env, text=True, capture_output=True, timeout=15)
        if succeeds:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def run_cli(self, *arguments, **options):
        return self.invoke(self.cli, *arguments, **options)

    def view(self, *arguments):
        return json.loads(self.run_cli(*arguments).stdout)

    def spec(self):
        return {"schema_version": 1, "kind": "finding", "title": "Reported arithmetic result",
                "occurred_at": "2026-09-20T12:00:00+00:00", "summary": "A bounded two-input audit was reported.",
                "significance": "Preserves the limited evidence available at this exact point.",
                "evidence_status": "reported", "caveats": ["No raw-output acceptance yet."],
                "event_ids": [], "experiment_ids": ["ORIGINAL-EXPERIMENT-17"], "supersedes": [],
                "files": ["notes/result.md"], "remote_artifacts": [
                    {"platform": "remote", "uri": "cluster-a:/scratch/fixture/result.json", "evidence_status": "reported", "checked_at": None}],
                "content_reviewed_no_secrets": True}

    def claim(self):
        self.run_cli("claim", "project-state")

    def capture(self, spec=None, **options):
        self.spec_path.write_text(json.dumps(spec or self.spec()))
        result = self.run_cli("highlight", "create", self.spec_path, **options)
        return json.loads(result.stdout) if result.returncode == 0 else result

    def snapshot(self, identity):
        return self.hub / "highlights" / identity

    def ledger_bytes(self):
        return (self.hub / "ledger.jsonl").read_bytes()

    def test_init_distributes_complete_runtime_and_templates(self):
        for name in ("bin/research_highlights.py", "bin/artifact_routes.py", "HIGHLIGHTS.md", "HIGHLIGHT_TEMPLATE.json"):
            self.assertTrue((self.hub / name).is_file(), name)
        self.assertEqual(self.view("highlight", "list")["rows"], [])

    def test_creation_follows_the_project_state_lease_atomically(self):
        # project-state is the protocol's own thread, so a fresh hub accepts a highlight without any claim.
        self.assertFalse((self.hub / "threads/project-state.md").exists())
        self.assertEqual(uuid.UUID(self.capture()["id"][3:]).version, 4)
        self.claim()
        before = self.ledger_bytes()
        self.capture(succeeds=False, session="wrong-owner")  # another session's live lease
        self.assertEqual(self.ledger_bytes(), before)
        receipt = self.capture()
        self.assertEqual(uuid.UUID(receipt["id"][3:]).version, 4)
        # Since 0.4.0 no claim is needed once nobody else holds a live lease.
        self.run_cli("release", "project-state")
        self.assertEqual(uuid.UUID(self.capture(session="later-session")["id"][3:]).version, 4)

    def test_copy_preserves_bytes_identity_git_state_and_ledger_cutoff(self):
        self.claim()
        before = self.ledger_bytes()
        original = self.source.read_bytes()
        spec = self.spec()
        spec["event_ids"] = [json.loads(before.splitlines()[-1])["event_id"]]
        receipt = self.capture(spec)
        directory = self.snapshot(receipt["id"])
        manifest = json.loads((directory / "MANIFEST.json").read_text())
        self.assertEqual((directory / "files/notes/result.md").read_bytes(), original)
        # 0.4 (manifest v2) records the cutoff instead of copying the ledger.
        self.assertEqual(manifest["schema_version"], 2)
        self.assertFalse((directory / "ledger-prefix.jsonl").exists())
        self.assertEqual(manifest["ledger_prefix"]["bytes"], len(before))
        self.assertEqual(manifest["ledger_prefix"]["sha256"], hashlib.sha256(before).hexdigest())
        self.assertEqual(manifest["source_repository"]["revision"], self.git("rev-parse", "HEAD"))
        self.assertTrue(manifest["source_repository"]["dirty"])
        self.assertEqual(manifest["spec"]["evidence_status"], "reported")
        self.assertNotEqual(manifest["captured_at"], manifest["spec"]["occurred_at"])
        self.source.write_text("Later mutable result, intentionally different.\n")
        self.run_cli("log", "--id", "LATER", "--note", "A subsequent unrelated update")
        self.assertTrue(self.view("highlight", "verify", receipt["id"])["verified"])
        self.assertEqual((directory / "files/notes/result.md").read_bytes(), original)

    def test_time_lookup_compares_instants_and_retains_corrections(self):
        self.claim()
        first_spec = self.spec()
        first_spec["occurred_at"] = "2026-09-20T23:00:00+12:00"
        first = self.capture(first_spec)
        second_spec = self.spec()
        second_spec.update(kind="correction", title="Corrected result", evidence_status="retracted",
                           occurred_at="2026-09-20T00:30:00-12:00", supersedes=[first["id"]])
        second = self.capture(second_spec)
        rows = self.view("highlight", "list")["rows"]
        self.assertEqual([row["highlight_id"] for row in rows], [second["id"], first["id"]])
        before = self.view("highlight", "list", "--until", "2026-09-20T12:00:00Z", "-n", "1")
        self.assertEqual(before["rows"][0]["highlight_id"], first["id"])
        self.assertEqual(before["rows"][0]["superseded_by"][0]["highlight_id"], second["id"])
        after = self.view("highlight", "list", "--since", "2026-09-20T12:00:00Z")
        self.assertEqual([row["highlight_id"] for row in after["rows"]], [second["id"]])
        captured = self.view("highlight", "list", "--time-field", "captured_at",
                             "--since", "2000-01-01T00:00:00Z")
        self.assertEqual(captured["matching_count"], 2)
        self.assertEqual(self.view("highlight", "show", second["id"])["supersedes"], [first["id"]])
        self.assertEqual(self.view("highlight", "show", first["id"])["superseded_by"][0]["highlight_id"], second["id"])
        self.run_cli("highlight", "list", "--since", "not a time", succeeds=False)
        # A time without an offset is read in the display zone; an age counts back from now.
        self.env["RMEM_TZ"] = "UTC"
        local = self.view("highlight", "list", "--since", "2026-09-20 12:00")
        del self.env["RMEM_TZ"]
        self.assertEqual([row["highlight_id"] for row in local["rows"]], [second["id"]])
        self.assertEqual(self.view("highlight", "list", "--since", "100000d")["matching_count"], 2)

    def test_old_keyword_lookup_displays_later_correction_outside_filter(self):
        self.claim()
        first_spec = self.spec()
        first_spec["summary"] = "RareKeyword apparent success"
        first = self.capture(first_spec)
        correction = self.spec()
        correction.update(kind="correction", evidence_status="retracted", supersedes=[first["id"]],
                          summary="Withdrawn after raw evidence audit")
        second = self.capture(correction)
        rows = self.view("highlight", "list", "--query", "RareKeyword")["rows"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["highlight_id"], first["id"])
        self.assertEqual(rows[0]["superseded_by"][0]["highlight_id"], second["id"])
        self.assertEqual(rows[0]["superseded_by"][0]["evidence_status"], "retracted")

    def test_restore_recovers_to_separate_tree_and_does_not_mutate_live_state(self):
        self.claim()
        original = self.source.read_bytes()
        receipt = self.capture()
        self.source.write_text("Current live text must stay.\n")
        ledger = self.ledger_bytes()
        destination = self.root / "restored"
        result = self.view("highlight", "restore", receipt["id"], "--destination", destination)
        self.assertFalse(result["live_state_changed"])
        self.assertFalse(result["execution_authorized"])
        self.assertEqual((destination / "frozen_files/notes/result.md").read_bytes(), original)
        self.assertEqual(self.source.read_text(), "Current live text must stay.\n")
        self.assertEqual(self.ledger_bytes(), ledger)
        self.assertFalse((destination / ".rmem").exists())
        self.assertTrue((destination / "RESTORE_README.txt").is_file())

    def test_restore_rejects_live_control_nonempty_and_symlink_destinations(self):
        self.claim()
        identity = self.capture()["id"]
        nonempty = self.root / "occupied"
        nonempty.mkdir()
        (nonempty / "preserve.txt").write_text("Do not overwrite")
        controlled = self.root / ".codex"
        controlled.mkdir()
        actual = self.root / "actual"
        actual.mkdir()
        symlink = self.root / "linked"
        symlink.symlink_to(actual, target_is_directory=True)
        for destination in (self.repo / "restored", self.hub / "restored", controlled / "restored", nonempty, symlink):
            with self.subTest(destination=destination):
                self.run_cli("highlight", "restore", identity, "--destination", destination, succeeds=False)
        self.assertEqual((nonempty / "preserve.txt").read_text(), "Do not overwrite")
        empty = self.root / "empty"
        empty.mkdir()
        self.run_cli("highlight", "restore", identity, "--destination", empty)

    def test_tampered_copied_file_is_rejected_before_restore(self):
        self.claim()
        identity = self.capture()["id"]
        frozen = self.snapshot(identity) / "files/notes/result.md"
        frozen.chmod(0o600)
        frozen.write_text("tampered\n")
        for command in ("show", "verify"):
            self.run_cli("highlight", command, identity, succeeds=False)
        destination = self.root / "must-not-appear"
        self.run_cli("highlight", "restore", identity, "--destination", destination, succeeds=False)
        self.assertFalse(destination.exists())

    def test_restore_rechecks_bytes_changed_after_initial_verification(self):
        self.claim()
        identity = self.capture()["id"]
        module_spec = importlib.util.spec_from_file_location("fixture_highlights", self.hub / "bin/research_highlights.py")
        module = importlib.util.module_from_spec(module_spec)
        module_spec.loader.exec_module(module)
        real_verify = module.verify
        events = [json.loads(line) for line in self.ledger_bytes().splitlines()]
        frozen = self.snapshot(identity) / "files/notes/result.md"

        def mutate_after_verification(*arguments):
            result = real_verify(*arguments)
            frozen.chmod(0o600)
            frozen.write_text("Changed after successful verification.\n")
            return result

        destination = self.root / "race-must-not-publish"
        with mock.patch.object(module, "verify", side_effect=mutate_after_verification):
            with self.assertRaisesRegex(ValueError, "changed after verification"):
                module.restore(self.hub, identity, destination, events)
        self.assertFalse(destination.exists())

    def test_recomputed_manifest_checksum_cannot_bypass_ledger_anchor(self):
        self.claim()
        identity = self.capture()["id"]
        directory = self.snapshot(identity)
        path = directory / "MANIFEST.json"
        manifest = json.loads(path.read_text())
        manifest["spec"]["evidence_status"] = "observed"
        data = json.dumps(manifest).encode()
        path.chmod(0o600)
        path.write_bytes(data)
        checksum = directory / "MANIFEST.sha256"
        checksum.chmod(0o600)
        checksum.write_text(hashlib.sha256(data).hexdigest() + "\n")
        result = self.run_cli("highlight", "verify", identity, succeeds=False)
        self.assertIn("ledger anchor", result.stderr)

    def test_rewritten_ledger_prefix_is_detected(self):
        self.claim()
        identity = self.capture()["id"]
        ledger = self.hub / "ledger.jsonl"
        events = [json.loads(line) for line in ledger.read_text().splitlines()]
        events[0]["note"] = "Rewritten old ownership evidence"
        ledger.write_text("".join(json.dumps(event) + "\n" for event in events))
        result = self.run_cli("highlight", "verify", identity, succeeds=False)
        self.assertIn("ledger prefix", result.stderr)

    def test_independent_insertions_preserve_snapshot_and_external_restore(self):
        self.claim()
        self.run_cli("log", "--id", "PRIOR", "--note", "Captured prior observation")
        identity = self.capture()["id"]
        ledger_path = self.hub / "ledger.jsonl"
        lines = self.ledger_bytes().splitlines(keepends=True)
        inserted = {"event_id": uuid.uuid4().hex, "kind": "finding", "id": "MERGED-OTHER",
                    "ts": "2026-09-22T00:00:00+00:00", "note": "Independent event from reconciled history"}
        extra = (json.dumps(inserted) + "\n").encode()
        merged = lines[0] + extra + b"".join(lines[1:])
        ledger_path.write_bytes(merged)
        self.assertTrue(self.view("highlight", "verify", identity)["verified"])
        report = self.view("highlight", "show", identity)["history_verification"]
        self.assertEqual(report["relation"], "captured_events_preserved_with_insertions")
        self.assertEqual(report["captured_event_count"], 2)
        self.assertEqual(report["inserted_event_count"], 1)
        self.assertTrue(report["original_relative_order_preserved"])
        self.assertTrue(report["warning"])
        destination = self.root / "restore-reconciled-history"
        restored = self.view("highlight", "restore", identity, "--destination", destination)
        self.assertEqual(restored["history_verification"], report)
        self.assertEqual((destination / "frozen_files/notes/result.md").read_bytes(), self.source.read_bytes())
        self.assertEqual((destination / "ledger-prefix.jsonl").read_bytes(), b"".join(lines[:-1]))
        self.assertEqual(ledger_path.read_bytes(), merged)

    def test_history_fallback_rejects_reordering_missing_changed_and_duplicate_events(self):
        self.claim()
        self.run_cli("log", "--id", "PRIOR", "--note", "Captured prior observation")
        identity = self.capture()["id"]
        ledger_path = self.hub / "ledger.jsonl"
        lines = self.ledger_bytes().splitlines(keepends=True)
        changed = json.loads(lines[0])
        changed["note"] = "Changed existing event content"
        cases = {
            "reordered": [lines[1], lines[0], *lines[2:]],
            "missing": lines[1:],
            "changed": [(json.dumps(changed) + "\n").encode(), *lines[1:]],
            "duplicate": [*lines, lines[0]],
        }
        for label, altered in cases.items():
            with self.subTest(label=label):
                ledger_path.write_bytes(b"".join(altered))
                self.run_cli("highlight", "verify", identity, succeeds=False)
                destination = self.root / ("rejected-" + label)
                self.run_cli("highlight", "restore", identity, "--destination", destination, succeeds=False)
                self.assertFalse(destination.exists())

    def test_history_fallback_rejects_reserialized_original_line(self):
        self.claim()
        identity = self.capture()["id"]
        lines = self.ledger_bytes().splitlines(keepends=True)
        same_event = json.loads(lines[0])
        lines[0] = (json.dumps(same_event, separators=(",", ":")) + "\n").encode()
        (self.hub / "ledger.jsonl").write_bytes(b"".join(lines))
        result = self.run_cli("highlight", "verify", identity, succeeds=False)
        self.assertIn("line bytes changed", result.stderr)

    def test_legacy_prefix_without_uuid_does_not_allow_insertion_fallback(self):
        legacy = {"ts": "2020-01-01T00:00:00+00:00", "kind": "exp", "id": "OLD"}
        (self.hub / "ledger.jsonl").write_text(json.dumps(legacy) + "\n")
        self.claim()
        identity = self.capture()["id"]
        exact = self.view("highlight", "show", identity)["history_verification"]
        self.assertEqual(exact["relation"], "exact_prefix")
        lines = self.ledger_bytes().splitlines(keepends=True)
        inserted = {"event_id": uuid.uuid4().hex, "ts": "2026-09-22T00:00:00+00:00", "kind": "exp", "id": "NEW"}
        (self.hub / "ledger.jsonl").write_bytes((json.dumps(inserted) + "\n").encode() + b"".join(lines))
        self.run_cli("highlight", "verify", identity, succeeds=False)

    def test_outside_duplicate_control_secret_and_ignored_inputs_are_rejected(self):
        self.claim()
        (self.repo / ".gitignore").write_text("ignored.txt\n")
        (self.repo / "ignored.txt").write_text("Ignored by Git")
        (self.repo / ".env").write_text("Sensitive configuration")
        before = self.ledger_bytes()
        cases = [
            {"files": ["../outside.txt"]}, {"files": [str(self.source)]},
            {"files": ["notes/result.md", "notes/result.md"]},
            {"files": ["notes/result.md", "notes/RESULT.md"]},
            {"files": [".codex/research-mode.json"]}, {"files": [".env"]},
            {"files": ["ignored.txt"]}, {"content_reviewed_no_secrets": False},
            {"event_ids": ["unknown-event"]}, {"supersedes": ["hl-" + "a" * 32]},
        ]
        for change in cases:
            with self.subTest(change=change):
                spec = self.spec()
                spec.update(change)
                self.capture(spec, succeeds=False)
                self.assertEqual(self.ledger_bytes(), before)
        self.assertEqual(self.view("highlight", "list")["rows"], [])

    def test_symlink_hardlink_binary_oversize_and_secret_content_rejected(self):
        self.claim()
        symlink = self.repo / "symlink.md"
        symlink.symlink_to(self.source)
        hardlink = self.repo / "hardlink.md"
        os.link(self.source, hardlink)
        linked_dir = self.repo / "linked-dir"
        linked_dir.symlink_to(self.source.parent, target_is_directory=True)
        oversized = self.repo / "oversized.txt"
        oversized.write_bytes(b"x" * (1024 * 1024 + 1))
        binary = self.repo / "binary.txt"
        binary.write_bytes(b"some\0bytes")
        secret = self.repo / "normal-name.json"
        secret.write_text(json.dumps({"api_key": "do-not-print-this-secret"}))
        before = self.ledger_bytes()
        for name in ("symlink.md", "hardlink.md", "linked-dir/result.md", "oversized.txt", "binary.txt", "normal-name.json"):
            with self.subTest(name=name):
                spec = self.spec()
                spec["files"] = [name]
                result = self.capture(spec, succeeds=False)
                self.assertNotIn("do-not-print-this-secret", result.stdout + result.stderr)
                self.assertEqual(self.ledger_bytes(), before)

    def test_duplicate_json_keys_and_authenticated_remote_pointers_rejected(self):
        self.claim()
        data = json.dumps(self.spec())
        self.spec_path.write_text(data[:-1] + ', "kind":"version"}')
        self.run_cli("highlight", "create", self.spec_path, succeeds=False)
        for pointer in ("https://user:password@example.invalid/object", "https://example.invalid/object?token=value"):
            spec = self.spec()
            spec["remote_artifacts"][0]["uri"] = pointer
            self.capture(spec, succeeds=False)

    def test_bounded_highlight_list_outputs_valid_json(self):
        self.claim()
        for index in range(4):
            spec = self.spec()
            spec["title"] = f"Version {index}"
            self.capture(spec)
        output = self.run_cli("highlight", "list", "-n", "2").stdout
        view = json.loads(output)
        self.assertEqual(view["matching_count"], 4)
        self.assertEqual(len(view["rows"]), 2)
        self.assertTrue(view["truncated"])
        self.assertLessEqual(len(output), 12001)

    def test_show_large_valid_identity_metadata_keeps_manifest_pointer_and_bound(self):
        self.claim()
        spec = self.spec()
        spec["experiment_ids"] = [f"historical-{index}-" + "x" * 900 for index in range(24)]
        identity = self.capture(spec)["id"]
        output = self.run_cli("highlight", "show", identity).stdout
        view = json.loads(output)
        self.assertLessEqual(len(output), 12001)
        self.assertEqual(view["highlight_id"], identity)
        self.assertEqual(view["experiment_ids_count"], 24)
        self.assertEqual(view["manifest"], f"highlights/{identity}/MANIFEST.json")
        manifest = json.loads((self.snapshot(identity) / "MANIFEST.json").read_text())
        self.assertEqual(manifest["spec"]["experiment_ids"], spec["experiment_ids"])

    # 0.4: manifest v2 records line digests instead of copying the ledger.

    def append_bulk(self, count, note_chars=400):
        """Grow the ledger quickly with valid events (direct appends stand in for many sessions)."""
        ledger = self.hub / "ledger.jsonl"
        sequence = len(ledger.read_bytes().splitlines())
        with ledger.open("a") as stream:
            for index in range(count):
                sequence += 1
                stream.write(json.dumps({"ts": "2026-10-01T00:00:00+00:00", "kind": "exp", "agent": "test",
                                         "id": f"BULK-{sequence}", "note": "x" * note_chars,
                                         "event_id": uuid.uuid4().hex, "sequence": sequence}) + "\n")

    def snapshot_bytes(self, identity):
        return sum(path.stat().st_size for path in self.snapshot(identity).rglob("*") if path.is_file())

    def manifest(self, identity):
        return json.loads((self.snapshot(identity) / "MANIFEST.json").read_text())

    def test_v2_snapshot_size_does_not_grow_with_the_ledger(self):
        self.claim()
        first = self.capture()["id"]
        self.append_bulk(300)
        second = self.capture()["id"]
        third = self.capture()["id"]
        ledger_size = len(self.ledger_bytes())
        self.assertGreater(ledger_size, 120000)
        # The second lists only the 300 new lines; the third only the anchor line after the second.
        self.assertEqual(self.manifest(second)["ledger_prefix"]["base"]["highlight_id"], first)
        self.assertEqual(self.manifest(third)["ledger_prefix"]["base"]["highlight_id"], second)
        self.assertEqual(self.manifest(third)["ledger_prefix"]["digests_count"], 1)
        self.assertLess(self.snapshot_bytes(third), 6000)
        self.assertLess(self.snapshot_bytes(second), ledger_size // 5)
        for identity in (first, second, third):
            self.assertFalse((self.snapshot(identity) / "ledger-prefix.jsonl").exists())
            self.assertEqual(self.view("highlight", "verify", identity)["history_verification"]["relation"], "exact_prefix")
            self.assertEqual(self.view("highlight", "verify", identity)["ledger_storage"], "line_digests")

    def test_v2_base_chain_is_bounded_by_a_full_list_every_sixteenth_highlight(self):
        self.claim()
        identities = [self.capture()["id"] for _ in range(17)]
        depths = [self.manifest(identity)["ledger_prefix"]["chain_depth"] for identity in identities]
        self.assertEqual(depths, list(range(16)) + [0])
        last = self.manifest(identities[-1])["ledger_prefix"]
        self.assertIsNone(last["base"])
        self.assertEqual(last["digests_count"], last["line_count"])
        self.assertEqual(self.view("highlight", "verify", identities[15])["history_verification"]["relation"], "exact_prefix")

    def test_v2_verifies_after_appends_and_insertions_and_restores_from_the_live_ledger(self):
        self.claim()
        self.run_cli("log", "--id", "PRIOR", "--note", "Captured prior observation")
        first = self.capture()["id"]
        second = self.capture()["id"]
        captured = self.ledger_bytes().splitlines(keepends=True)[:-1]  # everything before the second anchor
        self.run_cli("log", "--id", "LATER", "--note", "Appended after both highlights")
        for identity in (first, second):
            self.assertEqual(self.view("highlight", "verify", identity)["history_verification"]["relation"], "exact_prefix")
        lines = self.ledger_bytes().splitlines(keepends=True)
        inserted = {"event_id": uuid.uuid4().hex, "kind": "finding", "id": "MERGED-OTHER",
                    "ts": "2026-09-22T00:00:00+00:00", "note": "Independent event from another device"}
        merged = lines[0] + (json.dumps(inserted) + "\n").encode() + b"".join(lines[1:])
        (self.hub / "ledger.jsonl").write_bytes(merged)
        for identity in (first, second):
            report = self.view("highlight", "verify", identity)["history_verification"]
            self.assertEqual(report["relation"], "captured_events_preserved_with_insertions")
            self.assertEqual(report["inserted_event_count"], 1)
        destination = self.root / "restore-second"
        restored = self.view("highlight", "restore", second, "--destination", destination)
        self.assertEqual(restored["history_verification"]["inserted_event_count"], 1)
        self.assertEqual((destination / "ledger-prefix.jsonl").read_bytes(), b"".join(captured))
        self.assertTrue((destination / "ledger-lines.txt").is_file())
        self.assertIn("rebuilt from the live ledger", (destination / "RESTORE_README.txt").read_text())
        self.assertEqual(self.ledger_bytes(), merged)

    def test_v2_restore_fails_clearly_when_the_live_ledger_lost_captured_events(self):
        self.claim()
        self.run_cli("log", "--id", "PRIOR", "--note", "Captured prior observation")
        identity = self.capture()["id"]
        lines = self.ledger_bytes().splitlines(keepends=True)
        (self.hub / "ledger.jsonl").write_bytes(lines[0] + b"".join(lines[2:]))
        destination = self.root / "must-not-appear"
        result = self.run_cli("highlight", "restore", identity, "--destination", destination, succeeds=False)
        self.assertIn("cannot restore", result.stderr)
        self.assertIn("line digests", result.stderr)
        self.assertIn("captured event is missing", result.stderr)
        self.assertFalse(destination.exists())

    def test_v2_highlight_depends_on_its_base_and_reports_a_broken_one(self):
        self.claim()
        first = self.capture()["id"]
        second = self.capture()["id"]
        lines = self.snapshot(first) / "ledger-lines.txt"
        lines.chmod(0o600)
        lines.write_text(lines.read_text().replace("0", "1", 1))
        for identity in (first, second):
            self.run_cli("highlight", "verify", identity, succeeds=False)
        self.assertIn(f"base highlight {first}", self.run_cli("highlight", "verify", second, succeeds=False).stderr)
        check = self.run_cli("check", succeeds=False).stdout
        self.assertIn(f"highlight {first} does not verify", check)
        self.assertIn(f"highlight {second} does not verify", check)

    def test_freeze_ledger_keeps_the_v1_copy_and_old_highlights_still_work(self):
        self.claim()
        before = self.ledger_bytes()
        self.spec_path.write_text(json.dumps(self.spec()))
        frozen = json.loads(self.run_cli("highlight", "create", self.spec_path, "--freeze-ledger").stdout)["id"]
        manifest = self.manifest(frozen)
        self.assertEqual(manifest["schema_version"], 1)
        self.assertEqual((self.snapshot(frozen) / "ledger-prefix.jsonl").read_bytes(), before)
        self.assertEqual(self.view("highlight", "verify", frozen)["ledger_storage"], "copy")
        newer = self.capture()["id"]
        self.assertIsNone(self.manifest(newer)["ledger_prefix"]["base"])  # a v1 highlight is never a base
        rows = self.view("highlight", "list")["rows"]
        self.assertEqual({row["highlight_id"] for row in rows}, {frozen, newer})
        self.assertEqual(self.view("highlight", "show", frozen)["ledger_storage"], "copy")
        destination = self.root / "restore-v1"
        self.view("highlight", "restore", frozen, "--destination", destination)
        self.assertEqual((destination / "ledger-prefix.jsonl").read_bytes(), before)
        self.assertFalse((destination / "ledger-lines.txt").exists())

    def test_index_lists_newest_first_with_superseding_links_and_stays_current(self):
        self.claim()
        first = self.capture()["id"]
        correction = self.spec()
        correction.update(kind="correction", title="Corrected | result", evidence_status="retracted",
                          occurred_at="2026-09-21T12:00:00+00:00", supersedes=[first])
        second = self.capture(correction)["id"]
        preview = self.run_cli("highlight", "index").stdout
        self.assertIn("dry run", preview)
        self.assertFalse((self.hub / "HIGHLIGHTS_INDEX.md").exists())
        self.assertLess(preview.index(second), preview.index(f"| [{first}]"))
        self.assertIn(f"[{second}](highlights/{second}/MANIFEST.json) (retracted)", preview)
        self.assertIn("Corrected \\| result", preview)
        self.run_cli("highlight", "index", "--write")
        index = (self.hub / "HIGHLIGHTS_INDEX.md").read_text()
        self.assertEqual(index, preview[:len(index)])
        third = self.capture()["id"]
        self.assertIn(third, (self.hub / "HIGHLIGHTS_INDEX.md").read_text())

    def test_verify_and_check_warn_when_git_ignores_snapshot_files(self):
        self.claim()
        source = self.repo / "analysis/runs/summary.md"
        source.parent.mkdir(parents=True)
        source.write_text("Summary kept as evidence.\n")
        spec = self.spec()
        spec["files"] = ["analysis/runs/summary.md"]
        identity = self.capture(spec)["id"]
        self.assertNotIn("warnings", self.view("highlight", "verify", identity))
        with (self.hub / ".gitignore").open("a") as stream:
            stream.write("runs/\n")  # the unanchored rule of hubs installed before 0.4
        warnings = self.view("highlight", "verify", identity)["warnings"]
        self.assertIn(f"highlights/{identity}/files/analysis/runs/summary.md", warnings[0])
        self.assertIn("/runs/", warnings[0])
        self.assertIn("ignored by Git", self.run_cli("check").stdout)

    def test_check_verifies_highlights_and_fails_on_tampering(self):
        self.claim()
        identity = self.capture()["id"]
        self.assertNotIn("does not verify", self.run_cli("check").stdout)
        frozen = self.snapshot(identity) / "files/notes/result.md"
        frozen.chmod(0o600)
        frozen.write_text("tampered\n")
        result = self.run_cli("check", succeeds=False)
        self.assertIn(f"highlight {identity} does not verify: frozen file hash/size mismatch", result.stdout)


if __name__ == "__main__":
    unittest.main()
