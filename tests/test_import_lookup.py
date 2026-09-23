"""Historical source-ID lookup; all events and hubs are disposable fixtures."""
import copy
import importlib.util
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "import_lookup_records", ROOT / "rmem-kit/bin/research_records.py")
records = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(records)

ORIGINAL_ID = "a" * 32
INGESTION_ID = "b" * 32


class ImportLookupTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="rmem-import-lookup-")
        self.addCleanup(self.temporary.cleanup)
        self.hub = Path(self.temporary.name)

    def imported(self, event_id=INGESTION_ID, namespace="git-stash-fixture"):
        return {
            "ts": "2026-09-23T01:00:00+00:00", "kind": "import",
            "event_id": event_id, "id": "RECOVERED-ENGINEERING",
            "source_namespace": namespace, "source_id": ORIGINAL_ID,
            "thread": "project-state", "status": "keep", "sources": [],
        }

    def test_original_uuid_resolves_import_and_labels_ingestion_identity(self):
        event = self.imported()
        result = records.show_view(self.hub, [event], ORIGINAL_ID, "history")
        self.assertEqual(result["lookup_kind"], "historical_import_source_id")
        self.assertEqual(result["original_source_id"], ORIGINAL_ID)
        self.assertEqual(result["identifier"], ORIGINAL_ID)
        self.assertEqual(result["event_id"], INGESTION_ID)
        self.assertEqual(result["ingestion_event_id"], INGESTION_ID)
        self.assertEqual(result["source_namespace"], "git-stash-fixture")
        self.assertEqual(result["history"][0]["event_id"], INGESTION_ID)
        self.assertIn("not the original event", result["limits"])
        self.assertIn("no historical claim", result["limits"])

    def test_same_source_id_in_multiple_namespaces_requires_exact_event(self):
        events = [self.imported(), self.imported("c" * 32, "other-archive")]
        with self.assertRaisesRegex(ValueError, "multiple source namespaces.*ingestion event_id"):
            records.show_view(self.hub, events, ORIGINAL_ID, "record")
        exact = records.show_view(self.hub, events, INGESTION_ID, "record")
        self.assertEqual(exact["event_id"], INGESTION_ID)
        self.assertNotIn("lookup_kind", exact)

    def test_catalog_finds_source_id_and_namespace_for_exact_ingestion_selection(self):
        events = [self.imported(), self.imported("c" * 32, "other-archive")]
        args = SimpleNamespace(query=ORIGINAL_ID, thread=None, updates=True, n=10)
        result = records.catalog_view(self.hub, events, args)
        self.assertEqual(result["matching_count"], 2)
        self.assertEqual({row["event_id"] for row in result["rows"]}, {INGESTION_ID, "c" * 32})
        self.assertEqual({row["source_id"] for row in result["rows"]}, {ORIGINAL_ID})
        self.assertEqual({row["source_namespace"] for row in result["rows"]},
                         {"git-stash-fixture", "other-archive"})
        args.query = "other-archive"
        selected = records.catalog_view(self.hub, events, args)
        self.assertEqual(selected["matching_count"], 1)
        self.assertEqual(selected["rows"][0]["event_id"], "c" * 32)

    def test_direct_event_uuid_has_priority_over_even_ambiguous_import_sources(self):
        direct = {"ts": "2026-09-22T00:00:00+00:00", "kind": "exp",
                  "event_id": ORIGINAL_ID, "id": "DIRECT-EVENT", "sources": []}
        events = [direct, self.imported(), self.imported("c" * 32, "other-archive")]
        result = records.show_view(self.hub, events, ORIGINAL_ID, "record")
        self.assertEqual(result["event_id"], ORIGINAL_ID)
        self.assertEqual(result["experiment_id"], "DIRECT-EVENT")
        self.assertNotIn("lookup_kind", result)

    def test_direct_experiment_id_keeps_priority_over_source_id_fallback(self):
        direct = {"ts": "2026-09-22T00:00:00+00:00", "kind": "exp",
                  "event_id": "d" * 32, "id": ORIGINAL_ID, "sources": []}
        result = records.show_view(self.hub, [direct, self.imported()], ORIGINAL_ID, "record")
        self.assertEqual(result["event_id"], "d" * 32)
        self.assertNotIn("lookup_kind", result)

    def test_same_namespace_history_uses_latest_ingestion_and_preserves_input(self):
        first, last = self.imported(), self.imported("c" * 32)
        last["ts"] = "2026-09-23T02:00:00+00:00"
        original = copy.deepcopy([first, last])
        result = records.show_view(self.hub, [first, last], ORIGINAL_ID, "history")
        self.assertEqual(result["event_id"], "c" * 32)
        self.assertEqual(result["history_total"], 2)
        self.assertEqual([first, last], original)

    def test_non_import_source_id_does_not_create_a_historical_match(self):
        event = self.imported()
        event["kind"] = "exp"
        with self.assertRaisesRegex(ValueError, "Unknown exact"):
            records.show_view(self.hub, [event], ORIGINAL_ID, "record")

    def test_oversize_record_retains_original_and_ingestion_identity(self):
        event = self.imported()
        event["research"] = {
            "schema_version": 1, "experiment_id": event["id"],
            "task_type": "engineering", "summary": "Fixture only",
            "purpose": "Bounded lookup identity", "lineage": "Disposable test",
            "steps": {"unit": "not_applicable", "planned": None, "completed": None},
            "model": {"identity": None, "base": None, "checkpoint": None, "seed": None},
            "datasets": [], "recipe": {"method": "Fixture", "config": {},
                                         "code_revision": None, "location": None},
            "evaluation": {"purpose": None, "metrics": [], "acceptance": None},
            "findings": [], "artifacts": [], "legacy_refs": [], "source_event_ids": [],
            "gaps": ["Unresolved fixture field: " + str(i) + "x" * 2000 for i in range(10)],
            "as_of": "2026-09-23T00:00:00+00:00",
        }
        self.assertEqual(records.validate_record(event["research"]), "")
        result = records.show_view(self.hub, [event], ORIGINAL_ID, "record")
        self.assertIn("error", result)
        self.assertEqual(result["lookup_kind"], "historical_import_source_id")
        self.assertEqual(result["original_source_id"], ORIGINAL_ID)
        self.assertEqual(result["event_id"], INGESTION_ID)
        self.assertEqual(result["ingestion_event_id"], INGESTION_ID)
        self.assertLessEqual(len(json.dumps(result, ensure_ascii=False)), 12000)


if __name__ == "__main__":
    unittest.main()
