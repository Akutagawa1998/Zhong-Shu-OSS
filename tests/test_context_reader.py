"""A generic metadata gateway; all indexed files and hubs are disposable."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


KIT = Path(__file__).resolve().parents[1] / "rmem-kit"
sys.path.insert(0, str(KIT / "bin"))
SPEC = importlib.util.spec_from_file_location("generic_context_reader", KIT / "bin/context_reader.py")
reader = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(reader)


class ContextReaderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="rmem-context-reader-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.index = self.root / "index.json"
        self.target = self.root / "thread.md"
        self.target.write_text("first\nsecond\nthird\n", encoding="utf-8")
        self.rows = [{"id": "topic-thread", "path": "thread.md", "scope": "threads",
                      "summary": "The selected research thread"}]
        self.save_index()

    def save_index(self, rows=None):
        self.index.write_text(json.dumps({"schema_version": 1, "entries": self.rows if rows is None else rows}))

    def cli(self, *args, succeeds=True):
        result = subprocess.run([sys.executable, str(KIT / "bin/context_reader.py"),
                                 "--root", str(self.root), "--index", "index.json", *map(str, args)],
                                text=True, capture_output=True, timeout=5)
        self.assertEqual(result.returncode == 0, succeeds, result.stdout + result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        return result

    def test_metadata_query_opens_only_the_named_index(self):
        self.save_index(self.rows + [{"id": "absent", "path": "not-created.md",
                                     "scope": "evidence", "summary": "Missing source stays discoverable"}])
        original = Path.open
        opened = []

        def guard(path, *args, **kwargs):
            opened.append(path)
            self.assertEqual(path, self.index)
            return original(path, *args, **kwargs)

        with mock.patch.object(Path, "open", guard):
            entries = reader.registered_entries(self.root, "index.json")
            result = reader.index_view(entries, query="SELECTED", scope="threads")
        self.assertEqual(opened, [self.index])
        self.assertEqual(result["matching_count"], 1)
        self.assertEqual(result["entries"][0]["id"], "topic-thread")

    def test_exact_id_and_path_read_bounded_slices(self):
        for identifier in ("topic-thread", "thread.md"):
            result = json.loads(self.cli("read", identifier, "--start", 2, "--limit", 1).stdout)
            self.assertEqual(result["lines"], [{"line": 2, "text": "second", "line_clipped": False}])
            self.assertEqual(result["next_line"], 3)

    def test_unindexed_existing_file_is_refused_without_content_open(self):
        hidden = self.root / "unindexed.md"
        hidden.write_text("this must not be read")
        entries = reader.registered_entries(self.root, "index.json")
        with mock.patch.object(Path, "open", side_effect=AssertionError("opened unindexed file")):
            with self.assertRaisesRegex(ValueError, "Unregistered.*No filesystem fallback"):
                reader.read_view(self.root, entries, hidden.name)
        self.cli("read", hidden.name, succeeds=False)

    def test_registered_paths_reject_traversal_absolute_and_symlinks(self):
        (self.root / "alias.md").symlink_to(self.target)
        for name in ("../outside.md", str(self.target), "alias.md"):
            with self.subTest(name=name):
                self.save_index([{**self.rows[0], "path": name}])
                self.cli("index", succeeds=False)

    def test_symlinked_index_is_rejected(self):
        self.index.unlink()
        self.index.symlink_to(self.target)
        self.cli("index", succeeds=False)

    def test_invalid_schema_duplicate_ids_and_limits_are_actionable(self):
        for value in ({"schema_version": True, "entries": []}, [],
                      {"schema_version": 1, "entries": [self.rows[0], self.rows[0]]}):
            self.index.write_text(json.dumps(value))
            self.cli("index", succeeds=False)
        self.save_index()
        for args in (("index", "--limit", 0), ("read", "topic-thread", "--start", 0),
                     ("read", "topic-thread", "--limit", 101)):
            self.cli(*args, succeeds=False)

    def test_long_lines_preserve_physical_line_numbers_and_bound_output(self):
        self.target.write_text("x" * 200000 + "\nsecond\n" + "tail\n" * 100)
        result = self.cli("read", "topic-thread", "--limit", 100)
        view = json.loads(result.stdout)
        self.assertLessEqual(len(result.stdout), 12000)
        self.assertEqual(view["lines"][0], {"line": 1, "text": "x" * 1000, "line_clipped": True})
        self.assertEqual(view["lines"][1]["line"], 2)
        self.assertEqual(view["lines"][1]["text"], "second")
        later = json.loads(self.cli("read", "topic-thread", "--start", 2, "--limit", 1).stdout)
        self.assertEqual(later["lines"][0]["text"], "second")

    def test_index_and_source_size_limits_fail_without_traceback(self):
        with self.target.open("wb") as stream:
            stream.truncate(reader.FILE_MAX_BYTES + 1)
        self.cli("read", "topic-thread", succeeds=False)
        with self.index.open("wb") as stream:
            stream.truncate(reader.INDEX_MAX_BYTES + 1)
        self.cli("index", succeeds=False)

    def test_large_index_rows_and_aliases_stay_bounded(self):
        rows = [{"id": str(i) + "q" * 120, "path": "thread.md", "scope": "threads",
                 "summary": "selected " + "s" * 900} for i in range(100)]
        self.save_index(rows)
        index = self.cli("index", "--limit", 50)
        self.assertLessEqual(len(index.stdout), 12000)
        self.assertTrue(json.loads(index.stdout)["truncated"])
        result = self.cli("read", "thread.md")
        self.assertLessEqual(len(result.stdout), 12000)
        self.assertTrue(json.loads(result.stdout)["index_ids_truncated"])

    def test_fresh_init_copies_reader_and_empty_template_without_project_registration(self):
        hub = self.root / "hub"
        env = {key: value for key, value in os.environ.items() if not key.startswith("RMEM_")}
        initialized = subprocess.run([sys.executable, str(KIT / "bin/rmem"), "init", str(hub)],
                                     env=env, text=True, capture_output=True, timeout=5)
        self.assertEqual(initialized.returncode, 0, initialized.stderr)
        self.assertTrue((hub / "bin/context_reader.py").is_file())
        self.assertTrue((hub / "INDEXED_READING.md").is_file())
        template = json.loads((hub / "CONTEXT_INDEX_TEMPLATE.json").read_text())
        self.assertEqual(template, {"schema_version": 1, "entries": []})
        self.assertFalse((hub / "CONTEXT_INDEX.json").exists())
        self.assertFalse((hub / "ACTIVE_CONTEXT.json").exists())
        queried = subprocess.run([sys.executable, str(hub / "bin/context_reader.py"),
                                  "--root", str(hub), "--index", "CONTEXT_INDEX_TEMPLATE.json", "index"],
                                 env=env, text=True, capture_output=True, timeout=5)
        self.assertEqual(queried.returncode, 0, queried.stderr)
        self.assertEqual(json.loads(queried.stdout)["entries"], [])


if __name__ == "__main__":
    unittest.main()
