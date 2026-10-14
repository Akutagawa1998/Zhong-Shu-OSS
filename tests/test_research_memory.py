"""Research-memory CLI integration tests; every mutable hub is disposable."""
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import uuid


REPOSITORY = Path(__file__).resolve().parents[1]
SOURCE_CLI = REPOSITORY / "rmem-kit/bin/rmem"


class ResearchMemoryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="rmem-record-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.hub = self.root / "hub"
        self.session = "test-" + uuid.uuid4().hex
        self.environment = {
            key: value for key, value in os.environ.items()
            if not key.startswith("RMEM_")
        }
        self.environment.update(RMEM_HUB=str(self.hub), PYTHONDONTWRITEBYTECODE="1")
        self._command(SOURCE_CLI, "init", str(self.hub), "--project-name", "Disposable test")
        self.cli = self.hub / "bin/rmem"
        self.ledger = self.hub / "ledger.jsonl"

    def _command(self, cli, *arguments, session=None, succeeds=True):
        result = subprocess.run(
            [sys.executable, str(cli), "--agent", "test", "--session",
             session or self.session, *map(str, arguments)],
            cwd=self.root, env=self.environment, text=True, capture_output=True,
            timeout=15,
        )
        if succeeds:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def run_cli(self, *arguments, **options):
        return self._command(self.cli, *arguments, **options)

    def view(self, *arguments):
        return json.loads(self.run_cli(*arguments).stdout)

    def events(self):
        return [json.loads(line) for line in self.ledger.read_text().splitlines()]

    def write_events(self, events):
        self.ledger.write_text("".join(json.dumps(event) + "\n" for event in events))

    def record(self, experiment_id="TEST-INFERENCE", completed=0):
        return {
            "schema_version": 1,
            "experiment_id": experiment_id,
            "task_type": "inference",
            "summary": "Measure paired arithmetic outputs on two fixed inputs.",
            "purpose": "Distinguish task failure from output-format failure.",
            "lineage": "Disposable test fixture, no trained model or GPU run.",
            "steps": {"unit": "evaluation_calls", "planned": 2, "completed": completed},
            "model": {"identity": "fixture", "base": None, "checkpoint": None, "seed": 7},
            "datasets": [{"id": "fixed-pair", "split": "dev", "role": "evaluation",
                          "count": 2, "sha256": None, "location": None}],
            "recipe": {"method": "greedy", "config": {"max_new_tokens": 32},
                       "code_revision": None, "location": None},
            "evaluation": {"purpose": "paired comparison", "metrics": ["strict_correct"],
                           "acceptance": "All expected inputs must be present."},
            "findings": [], "artifacts": [], "legacy_refs": [],
            "gaps": ["Fixture only; there are no actual model outputs."],
            "source_event_ids": [], "as_of": "2026-09-22T00:00:00+00:00",
        }

    def save_record(self, record, name="input-record.json"):
        path = self.root / name
        path.write_text(json.dumps(record))
        return path

    def log_record(self, record, **options):
        path = self.save_record(record)
        return self.run_cli("log", "--thread", "experiment", "--record", path,
                            "--status", "inconclusive", **options)

    def test_new_init_copies_record_runtime_and_empty_catalog_works(self):
        self.assertTrue((self.hub / "bin/research_records.py").is_file())
        self.assertEqual(self.view("catalog")["rows"], [])
        self.run_cli("check", "--records")

    def test_updates_have_unique_uuid_and_monotonic_sequence(self):
        self.run_cli("claim", "experiment")
        self.log_record(self.record(completed=0))
        self.log_record(self.record(completed=2))
        events = self.events()
        ids = [event["event_id"] for event in events]
        self.assertEqual(len(set(ids)), len(ids))
        for identity in ids:
            self.assertEqual(uuid.UUID(identity).version, 4)
        self.assertEqual([event["sequence"] for event in events], list(range(1, len(events) + 1)))
        self.assertEqual([e["research"]["steps"]["completed"] for e in events if "research" in e], [0, 2])

    def test_record_requires_thread_and_no_other_live_lease_without_partial_append(self):
        record_path = self.save_record(self.record())
        before = self.ledger.read_bytes()
        self.run_cli("log", "--record", record_path, succeeds=False)
        self.assertEqual(self.ledger.read_bytes(), before)
        self.log_record(self.record(), succeeds=False)
        self.assertEqual(self.ledger.read_bytes(), before)
        self.run_cli("claim", "experiment")
        before = self.ledger.read_bytes()
        self.log_record(self.record(), session="different-session", succeeds=False)
        self.assertEqual(self.ledger.read_bytes(), before)
        self.log_record(self.record())
        self.assertEqual(self.events()[-1]["session"], self.session)
        # Since 0.4.0 a thread write needs no claim once nobody else holds a live lease.
        self.run_cli("release", "experiment")
        self.log_record(self.record(completed=1), session="different-session")
        self.assertEqual(self.events()[-1]["session"], "different-session")

    def test_missing_or_empty_inference_dataset_is_rejected_atomically(self):
        self.run_cli("claim", "experiment")
        before = self.ledger.read_bytes()
        for missing in (True, False):
            with self.subTest(missing_field=missing):
                record = self.record()
                if missing:
                    record.pop("datasets")
                else:
                    record["datasets"] = []
                rejected = self.log_record(record, succeeds=False)
                self.assertIn("dataset", rejected.stderr.lower())
                self.assertEqual(self.ledger.read_bytes(), before)

    def test_exact_event_recovery_preserves_old_update_and_experiment_shows_latest(self):
        self.run_cli("claim", "experiment")
        self.log_record(self.record(completed=0))
        first = self.events()[-1]
        self.log_record(self.record(completed=2))
        last = self.events()[-1]
        latest = self.view("show", "TEST-INFERENCE")
        exact = self.view("show", first["event_id"], "--section", "record")
        self.assertEqual(latest["event_id"], last["event_id"])
        self.assertEqual(latest["record"]["kind"], "embedded_event")
        self.assertEqual(latest["record"]["record"]["steps"]["completed"], 2)
        self.assertEqual(exact["record"]["record"]["steps"]["completed"], 0)
        self.assertEqual(latest["history_total"], 2)

    def test_catalog_updates_are_bounded_valid_json_and_do_not_collapse_updates(self):
        self.run_cli("claim", "experiment")
        for completed in (0, 1, 2):
            self.log_record(self.record(completed=completed))
        self.assertEqual(len(self.view("catalog")["rows"]), 1)
        result = self.run_cli("catalog", "--updates", "-n", "2")
        view = json.loads(result.stdout)
        self.assertEqual(view["matching_count"], 3)
        self.assertTrue(view["truncated"])
        self.assertEqual(len(view["rows"]), 2)
        self.assertLessEqual(len(result.stdout), 12001)
        self.assertEqual([row["record"][0]["steps"]["completed"] for row in view["rows"]], [2, 1])
        self.assertEqual(self.view("catalog", "--query", "no-such-task")["rows"], [])

    def test_large_catalog_truncates_rows_without_breaking_json(self):
        events = [{"ts": "2020-01-01T00:00:00+00:00", "kind": "exp",
                   "id": f"OLD-{i}", "note": "Long historical explanation " * 20}
                  for i in range(100)]
        self.write_events(events)
        result = self.run_cli("catalog", "--updates", "-n", "100")
        view = json.loads(result.stdout)
        self.assertEqual(view["matching_count"], 100)
        self.assertTrue(view["truncated"])
        self.assertLess(len(view["rows"]), 100)
        self.assertLessEqual(len(result.stdout), 12001)

    def test_legacy_without_record_remains_readable_but_strict_cutoff_rejects(self):
        legacy = {"ts": "2020-01-01T00:00:00+00:00", "kind": "exp",
                  "id": "LEGACY", "note": "Old result, incomplete metadata."}
        self.write_events([legacy])
        self.run_cli("check", "--records")
        show = self.view("show", "LEGACY")
        self.assertIsNone(show["record"])
        self.assertIn("record_gap", show)
        catalog = self.view("catalog")
        self.assertEqual(catalog["rows"][0]["sequence_source"], "derived_current_ledger_position")
        self.assertFalse(catalog["rows"][0]["structured"])
        rejected = self.run_cli("check", "--records", "--require-records-since",
                                "2020-01-01T00:00:00+00:00", succeeds=False)
        self.assertIn("requires a research record", rejected.stdout)
        self.run_cli("check", "--records", "--require-records-since", "2021-01-01T00:00:00+00:00")

    def test_check_rejects_duplicate_event_identity_or_stored_sequence(self):
        self.run_cli("claim", "experiment")
        self.log_record(self.record())
        original = self.events()
        for field, expected in (("event_id", "duplicate event_id"),
                                ("sequence", "duplicate stored sequence")):
            with self.subTest(field=field):
                duplicate = copy.deepcopy(original[-1])
                duplicate["event_id"] = uuid.uuid4().hex
                duplicate["sequence"] += 1
                duplicate[field] = original[-1][field]
                self.write_events(original + [duplicate])
                result = self.run_cli("check", "--records", succeeds=False)
                self.assertIn(expected, result.stdout)

    def test_orphan_reviewed_card_is_queryable_without_implying_execution(self):
        record = self.record("OLD-UNIMPORTED")
        record["legacy_refs"] = ["historical-ledger#17"]
        records = self.hub / "records"
        records.mkdir(exist_ok=True)
        (records / "old.json").write_text(json.dumps(record))
        view = self.view("catalog", "--query", "OLD-UNIMPORTED")
        self.assertEqual(len(view["rows"]), 1)
        row = view["rows"][0]
        self.assertIsNone(row["event_id"])
        self.assertIsNone(row["sequence"])
        self.assertEqual(row["sequence_source"], "reviewed_card_not_new_execution")
        self.assertEqual(row["status"], "reviewed_snapshot")
        show = self.view("show", "OLD-UNIMPORTED")
        self.assertEqual(show["record"]["kind"], "reviewed_historical_snapshot")
        self.assertIsNone(show["event_id"])
        self.assertEqual(show["history"], [{"source_event_ids": [],
                                            "legacy_refs": ["historical-ledger#17"],
                                            "as_of": record["as_of"]}])
        self.assertEqual(self.ledger.read_text(), "")

    def test_reviewed_old_update_does_not_fill_a_later_update_record_gap(self):
        self.run_cli("claim", "experiment")
        self.run_cli("log", "--thread", "experiment", "--id", "OLD-TASK", "--note", "First update")
        first_id = self.events()[-1]["event_id"]
        self.run_cli("log", "--thread", "experiment", "--id", "OLD-TASK", "--note", "Later correction")
        record = self.record("OLD-TASK")
        record["source_event_ids"] = [first_id]
        (self.hub / "records").mkdir(exist_ok=True)
        (self.hub / "records/old.json").write_text(json.dumps(record))
        self.assertEqual(self.view("show", first_id)["record"]["kind"], "reviewed_historical_snapshot")
        self.assertIsNone(self.view("show", "OLD-TASK")["record"])

    def test_catalog_old_keyword_match_keeps_latest_retraction_visible(self):
        self.run_cli("claim", "experiment")
        self.run_cli("log", "--thread", "experiment", "--id", "RETRACTED-TASK",
                     "--status", "keep", "--note", "RareKeyword originally looked successful")
        original_id = self.events()[-1]["event_id"]
        self.run_cli("log", "--thread", "experiment", "--id", "RETRACTED-TASK",
                     "--status", "discard", "--note", "Withdrawn after parser correction")
        correction_id = self.events()[-1]["event_id"]
        row = self.view("catalog", "--query", "RareKeyword")["rows"][0]
        self.assertEqual(row["event_id"], correction_id)
        self.assertEqual(row["status"], "discard")
        self.assertEqual(row["matched_event_ids"], [original_id])
        self.assertFalse(row["latest_update_matches_filter"])
        historical = self.view("catalog", "--updates", "--query", "RareKeyword")["rows"]
        self.assertEqual([row["event_id"] for row in historical], [original_id])

    def test_catalog_latest_uses_append_order_and_display_uses_zoned_instants(self):
        self.write_events([
            {"ts": "2026-09-22T23:00:00+12:00", "kind": "exp", "id": "FIRST-TASK",
             "event_id": uuid.uuid4().hex, "status": "keep", "note": "Initial 11:00 UTC"},
            {"ts": "2026-09-22T00:30:00-12:00", "kind": "exp", "id": "FIRST-TASK",
             "event_id": uuid.uuid4().hex, "status": "discard", "note": "Correction 12:30 UTC"},
            {"ts": "2026-09-22T23:30:00+12:00", "kind": "exp", "id": "SECOND-TASK",
             "event_id": uuid.uuid4().hex, "status": "keep", "note": "Another task 11:30 UTC"},
        ])
        rows = self.view("catalog")["rows"]
        self.assertEqual([row["experiment_id"] for row in rows], ["FIRST-TASK", "SECOND-TASK"])
        self.assertEqual(rows[0]["status"], "discard")
        events = self.events()
        events.append({"ts": "2026-09-21T00:00:00+00:00", "kind": "exp", "id": "FIRST-TASK",
                       "event_id": uuid.uuid4().hex, "status": "inconclusive",
                       "note": "Backfilled update appended last remains latest for this ID"})
        self.write_events(events)
        latest = next(row for row in self.view("catalog")["rows"] if row["experiment_id"] == "FIRST-TASK")
        self.assertEqual(latest["event_id"], events[-1]["event_id"])
        self.assertEqual(latest["status"], "inconclusive")

    def test_oversize_orphan_record_retains_paths_and_narrow_sections_are_readable(self):
        record = self.record("LARGE-HISTORICAL")
        record["recipe"]["config"]["historical_metadata"] = "x" * 15000
        (self.hub / "records").mkdir(exist_ok=True)
        path = self.hub / "records/large.json"
        path.write_text(json.dumps(record))
        self.run_cli("check", "--records")
        self.assertGreater(path.stat().st_size, 12000)
        for section in ("all", "record"):
            with self.subTest(section=section):
                result = self.run_cli("show", "LARGE-HISTORICAL", "--section", section)
                view = json.loads(result.stdout)
                self.assertIn("records/large.json", view["record_file_paths"])
                self.assertLessEqual(len(result.stdout), 12001)
        for section in ("sources", "history"):
            with self.subTest(section=section):
                result = self.run_cli("show", "LARGE-HISTORICAL", "--section", section)
                view = json.loads(result.stdout)
                self.assertNotIn("error", view)
                self.assertEqual(view["identifier"], "LARGE-HISTORICAL")
                self.assertLessEqual(len(result.stdout), 12001)
        self.assertEqual(self.ledger.read_text(), "")

    def test_same_experiment_card_citing_other_acceptance_event_is_only_related(self):
        self.run_cli("claim", "experiment")
        self.run_cli("log", "--thread", "experiment", "--id", "FIXTURE-PLANNED",
                     "--status", "planned", "--note", "Queue-time update, no result yet")
        planned = self.events()[-1]
        self.run_cli("log", "--thread", "experiment", "--id", "FIXTURE-ACCEPTANCE",
                     "--status", "keep", "--note", "Subsequent raw-output acceptance")
        accepted = self.events()[-1]
        record = self.record("FIXTURE-PLANNED", completed=2)
        record["source_event_ids"] = [accepted["event_id"]]
        (self.hub / "records").mkdir(exist_ok=True)
        (self.hub / "records/reviewed.json").write_text(json.dumps(record))
        view = self.view("show", planned["event_id"])
        self.assertEqual(view["event_id"], planned["event_id"])
        self.assertEqual(view["event"]["status"], "planned")
        self.assertIsNone(view["record"])
        self.assertIn("record_gap", view)
        related = view["related_reviewed_records"]
        self.assertEqual(len(related), 1)
        self.assertEqual(related[0]["path"], "records/reviewed.json")
        self.assertEqual(related[0]["record"]["source_event_ids"], [accepted["event_id"]])
        self.assertIn("related_record_limits", view)

    def test_sources_find_snapshot_by_hash_when_original_is_missing(self):
        original = self.root / "external-evidence.txt"
        original.write_text("Immutable fixture evidence.\n")
        digest = hashlib.sha256(original.read_bytes()).hexdigest()
        self.run_cli("claim", "experiment")
        self.run_cli("log", "--thread", "experiment", "--id", "WITH-SOURCE", "--source", original)
        snapshot = self.hub / "sources/evidence/snapshot.txt"
        snapshot.parent.mkdir(parents=True)
        snapshot.write_bytes(original.read_bytes())
        provenance = {"sources": [{"sha256": digest, "snapshot": "sources/evidence/snapshot.txt"}]}
        (self.hub / "sources/selected-provenance.json").write_text(json.dumps(provenance))
        original.unlink()
        view = self.view("show", "WITH-SOURCE", "--section", "sources")
        source = view["sources"][0]
        self.assertEqual(source["sha256"], digest)
        self.assertFalse(any(candidate["exists"] for candidate in source["local_candidates"]))
        self.assertEqual(source["snapshot_candidates"], [{"path": str(snapshot), "exists": True,
                                                          "expected_sha256": digest}])
        self.assertIn("verify expected SHA", source["verification"])


    def test_distribution_reinit_does_not_copy_project_evidence_or_routes(self):
        # Use only this disposable installed hub as the source of a second init.
        (self.hub / "records").mkdir(exist_ok=True)
        (self.hub / "records/project.json").write_text(json.dumps(self.record("PRIVATE-PROJECT")))
        (self.hub / "ACTIVE_CONTEXT.json").write_text('{"private":true}')
        (self.hub / "ARTIFACT_ROUTES.json").write_text('{"schema_version":1,"files":[]}')
        (self.hub / "STATE.md").write_text("private source state")
        self.run_cli("claim", "experiment")
        self.log_record(self.record())
        next_hub = self.root / "second-hub"
        self.run_cli("init", next_hub, "--project-name", "Independent project")
        self.assertEqual((next_hub / "ledger.jsonl").read_text(), "")
        self.assertNotIn("private source state", (next_hub / "STATE.md").read_text())
        for name in ("records/project.json", "ACTIVE_CONTEXT.json", "ARTIFACT_ROUTES.json"):
            self.assertFalse((next_hub / name).exists(), name)
        for name in ("research_records.py", "artifact_routes.py"):
            self.assertTrue((next_hub / "bin" / name).is_file(), name)
        self.assertTrue((next_hub / "RECORDS.md").is_file())
        self.assertTrue((next_hub / "RECORD_TEMPLATE.json").is_file())

    def test_project_neutral_remote_platform_keeps_availability_honest(self):
        self.run_cli("claim", "experiment")
        record = self.record()
        record["artifacts"] = [{"role":"raw", "platform":"remote",
            "uri":"cluster-a:/scratch/example/raw.jsonl", "sha256":None,
            "availability":"reported_remote", "checked_at":None}]
        self.log_record(record)
        self.assertEqual(self.events()[-1]["research"]["artifacts"][0]["availability"], "reported_remote")
        record["artifacts"][0]["availability"] = "verified_remote"
        self.log_record(record, succeeds=False)

    def test_relocated_project_source_uses_explicit_root_not_project_name(self):
        old_root = self.root / "old-location"
        source = old_root / "plans/old.md"
        source.parent.mkdir(parents=True)
        source.write_text("Frozen original bytes, not a redirect.")
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        self.run_cli("claim", "experiment")
        self.run_cli("log", "--thread", "experiment", "--id", "ROUTED", "--source", source)
        target = self.root / "archive/original.md"
        target.parent.mkdir()
        target.write_bytes(source.read_bytes())
        source.unlink()
        sources = self.hub / "sources"
        sources.mkdir()
        (sources / "roots.json").write_text(json.dumps([
            {"id":"arbitrary-project-name","path":str(old_root),"project_relative":"."}]))
        (self.hub / "ARTIFACT_ROUTES.json").write_text(json.dumps({"schema_version":1,"files":[
            {"original_path":"plans/old.md","current_path":"archive/original.md",
             "sha256":digest,"bytes":target.stat().st_size,"role":"historical_original"}]}))
        view = self.view("show", "ROUTED", "--section", "sources")
        route = view["sources"][0]["relocated_artifact"]
        self.assertEqual(route["path"], str(target))
        self.assertTrue(route["verified"])
        self.assertEqual(route["relative_links_base"], "plans")
        target.write_text("Tampered")
        rejected = self.view("show", "ROUTED", "--section", "sources")["sources"][0]
        self.assertIn("relocation_error", rejected)
        self.assertNotIn("relocated_artifact", rejected)


if __name__ == "__main__":
    unittest.main()
