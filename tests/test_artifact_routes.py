import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


SPEC = importlib.util.spec_from_file_location("artifact_routes", Path(__file__).resolve().parents[1] / "rmem-kit/bin/artifact_routes.py")
routes = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(routes)


class ArtifactRouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        (self.root / ".research-memory").mkdir()
        (self.root / "archive").mkdir()
        self.rows = []

    def register(self, old, new, data, role="historical_original"):
        p = self.root / new
        p.write_bytes(data)
        row = dict(original_path=old, current_path=new, bytes=len(data), sha256=hashlib.sha256(data).hexdigest(), role=role)
        self.rows.append(row)
        self.save()
        return row

    def save(self):
        (self.root / ".research-memory/ARTIFACT_ROUTES.json").write_text(json.dumps(dict(schema_version=1, files=self.rows)))

    def test_original_beats_retired_redirect_but_exact_hash_can_retrieve_either(self):
        original = self.register("plan.md", "archive/plan.md", b"original evidence")
        stub = self.register("plan.md", "archive/stub.md", b"old redirect", "retired_redirect")
        self.assertEqual(routes.resolve_artifact(self.root, "plan.md")["sha256"], original["sha256"])
        self.assertEqual(routes.resolve_artifact(self.root, "plan.md", stub["sha256"])["path"], str(self.root / "archive/stub.md"))

    def test_tamper_missing_wrong_hash_and_unknown(self):
        row = self.register("evidence.md", "archive/evidence.md", b"valid")
        self.assertIsNone(routes.resolve_artifact(self.root, "unregistered.md"))
        with self.assertRaises(ValueError): routes.resolve_artifact(self.root, "evidence.md", "0" * 64)
        (self.root / row["current_path"]).write_bytes(b"invalid")
        with self.assertRaises(ValueError): routes.resolve_artifact(self.root, "evidence.md")
        (self.root / row["current_path"]).unlink()
        with self.assertRaises(OSError): routes.resolve_artifact(self.root, "evidence.md")

    def test_ambiguity_does_not_silently_select_newest(self):
        first = self.register("draft.md", "archive/v1.md", b"v1")
        self.register("draft.md", "archive/v2.md", b"v2")
        with self.assertRaises(ValueError): routes.resolve_artifact(self.root, "draft.md")
        self.assertEqual(routes.resolve_artifact(self.root, "draft.md", first["sha256"])["bytes"], 2)

    def test_traversal_and_symlink_refused(self):
        self.register("file.md", "archive/file.md", b"valid")
        for bad in ("../file.md", "/file.md", "a/../file.md", "a\\file.md", "./file.md"):
            with self.assertRaises(ValueError): routes.resolve_artifact(self.root, bad)
        self.rows[0]["current_path"] = "archive/../file.md"
        self.save()
        with self.assertRaises(ValueError): routes.resolve_artifact(self.root, "file.md")
        self.rows[0]["current_path"] = "archive/link.md"
        (self.root / "archive/link.md").symlink_to(self.root / "archive/file.md")
        self.save()
        with self.assertRaises(ValueError): routes.resolve_artifact(self.root, "file.md")


if __name__ == "__main__":
    unittest.main()
