"""rmem upgrade: dry run by default, conflicts preserved, legacy hubs recognized through
KIT_HISTORY.json, manifest rewritten, project data untouched. Disposable hubs and kits only."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest


KIT = Path(__file__).resolve().parents[1] / "rmem-kit"
CLI = KIT / "bin" / "rmem"
VERSION = (KIT / "VERSION").read_text().strip()
PROJECT = ("STATE.md", "MAP.md", "ledger.jsonl", "hub-config.json", "findings/README.md", "threads/alpha.md")


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class UpgradeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="rmem-upgrade-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.hub = self.root / "hub"
        self.env = {k: v for k, v in os.environ.items() if not k.startswith(("RMEM_", "CODEX_", "CLAUDE"))}
        self.env.update(RMEM_HUB=str(self.hub), RMEM_SESSION="session-a", RMEM_AGENT="codex",
                        PYTHONDONTWRITEBYTECODE="1")
        self.call("init", self.hub, "--project-name", "Upgrade test")
        self.call("claim", "alpha")
        self.kit = self.root / "kit"
        shutil.copytree(KIT, self.kit, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        (self.kit / "VERSION").write_text("0.9.0\n")
        for name in ("README.md", "AGENTS.md", "findings/README.md", "bin/research_records.py"):
            with (self.kit / name).open("a") as stream:
                stream.write("\n# changed in kit 0.9.0\n")
        (self.kit / "NEWDOC.md").write_text("# A document new in kit 0.9.0\n")
        source = (self.kit / "bin/rmem").read_text()
        marker = "KIT_FILES = ("
        self.assertIn(marker, source)
        updated = source.replace(marker, marker + '"NEWDOC.md", ', 1)
        (self.kit / "bin/rmem").write_text(updated)

    def call(self, *args, ok=True, env=None):
        result = subprocess.run([sys.executable, str(CLI), *map(str, args)], cwd=self.root,
                                env=env or self.env, capture_output=True, text=True, timeout=60)
        if ok:
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout)
        return result

    def upgrade(self, *extra, ok=True):
        return self.call("upgrade", "--from", self.kit, *extra, ok=ok)

    def snapshot(self):
        return {str(path.relative_to(self.hub)): path.read_bytes() for path in sorted(self.hub.rglob("*"))
                if path.is_file() and path.name != ".rmem.lock"}

    def events(self):
        return [json.loads(line) for line in (self.hub / "ledger.jsonl").read_text().splitlines()]

    def test_dry_run_is_the_default_and_changes_nothing(self):
        before = self.snapshot()
        output = self.upgrade().stdout
        self.assertEqual(self.snapshot(), before)
        self.assertIn("dry run; nothing was changed", output)
        self.assertIn(f"from: kit {VERSION} (recorded in .kit-manifest.json)", output)
        self.assertIn(f"to: kit 0.9.0 from {self.kit}", output)
        self.assertRegex(output, r"update \(unchanged since install; the kit changed it\): \d+")
        self.assertIn("- README.md (as installed, per the manifest)", output)
        self.assertIn("- bin/research_records.py", output)
        self.assertIn("add (new kit files): 1\n- NEWDOC.md", output)
        self.assertIn("project data left alone (differs from the kit): findings/README.md", output)
        self.assertIn("Rerun with --apply", output)

    def test_apply_updates_adds_keeps_conflicts_and_leaves_project_data(self):
        local = (self.hub / "AGENTS.md").read_bytes() + b"\nProject rule added locally.\n"
        (self.hub / "AGENTS.md").write_bytes(local)
        (self.hub / "hub-config.json").write_text(json.dumps({"caps": {"thread_max_chars": 5000}}))
        project = {name: (self.hub / name).read_bytes() for name in PROJECT}
        ledger = (self.hub / "ledger.jsonl").read_bytes()
        dry = self.upgrade().stdout
        self.assertIn("conflict (locally modified; never overwritten): 1", dry)
        self.assertIn(f"- AGENTS.md: inspect with `diff -u {self.hub / 'AGENTS.md'} {self.kit / 'AGENTS.md'}`", dry)
        output = self.upgrade("--apply").stdout
        self.assertIn("# rmem upgrade (applied)", output)
        for name in ("README.md", "bin/research_records.py", "bin/rmem", "VERSION", "NEWDOC.md"):
            self.assertEqual((self.hub / name).read_bytes(), (self.kit / name).read_bytes(), name)
        self.assertTrue(os.access(self.hub / "bin/rmem", os.X_OK))
        self.assertEqual((self.hub / "AGENTS.md").read_bytes(), local)
        for name, data in project.items():
            if name != "ledger.jsonl":
                self.assertEqual((self.hub / name).read_bytes(), data, name)
        after = (self.hub / "ledger.jsonl").read_bytes()
        self.assertTrue(after.startswith(ledger))
        self.assertEqual(after[len(ledger):].count(b"\n"), 1)
        event = self.events()[-1]
        self.assertEqual((event["kind"], event["from_version"], event["to_version"]), ("kit_upgraded", VERSION, "0.9.0"))
        self.assertEqual((event["counts"]["add"], event["counts"]["conflict"]), (1, 1))
        self.assertGreaterEqual(event["counts"]["update"], 4)
        self.assertEqual(event["conflicts"], ["AGENTS.md"])
        manifest = json.loads((self.hub / ".kit-manifest.json").read_text())
        self.assertEqual(manifest["kit_version"], "0.9.0")
        self.assertEqual(manifest["upgraded_from"], VERSION)
        self.assertIn("first_installed_at", manifest)
        self.assertEqual(manifest["files"]["NEWDOC.md"], sha((self.kit / "NEWDOC.md").read_bytes()))
        self.assertEqual(manifest["files"]["AGENTS.md"], sha((self.kit / "AGENTS.md").read_bytes()))
        self.assertNotEqual(manifest["files"]["findings/README.md"], sha((self.kit / "findings/README.md").read_bytes()))
        check = self.call("check").stdout
        self.assertIn("warn: kit file AGENTS.md is modified since install (kit 0.9.0)", check)
        self.assertNotIn("kit file README.md", check)
        again = self.upgrade().stdout
        self.assertIn("Nothing to upgrade: the hub already has kit 0.9.0 apart from the conflicts above.", again)
        self.assertIn("nothing recorded", self.upgrade("--apply").stdout)
        self.assertEqual(len([e for e in self.events() if e["kind"] == "kit_upgraded"]), 1)

    def test_apply_lists_the_linked_projects_whose_block_to_refresh(self):
        project = self.root / "project"
        project.mkdir()
        self.call("link", project)
        output = self.upgrade("--apply").stdout
        self.assertIn("Refresh the managed block in each linked project (1) with the upgraded CLI:", output)
        self.assertIn(f"{self.hub / 'bin/rmem'} link {project}", output)

    def test_a_forked_conversation_is_refused_before_any_file_changes(self):
        self.call("log", "--note", "binds session-a", env={**self.env, "CLAUDE_CODE_SESSION_ID": "11111111-1111-4111-8111-111111111111"})
        before = self.snapshot()
        forked = {**self.env, "CLAUDE_CODE_SESSION_ID": "22222222-2222-4222-8222-222222222222"}
        refused = self.call("upgrade", "--from", self.kit, "--apply", env=forked, ok=False).stderr
        self.assertIn("forked or copied conversation", refused)
        self.assertEqual(self.snapshot(), before)

    def test_a_locally_changed_code_file_blocks_apply_until_resolved(self):
        with (self.hub / "bin/research_records.py").open("a") as stream:
            stream.write("# local patch\n")
        before = self.snapshot()
        dry = self.upgrade().stdout
        self.assertIn("- bin/research_records.py: inspect with `diff -u", dry)
        self.assertIn("--apply is refused while kit code files have local changes: bin/research_records.py", dry)
        refused = self.upgrade("--apply", ok=False)
        self.assertIn("upgrade refused: kit code files have local changes (bin/research_records.py)", refused.stderr)
        self.assertEqual(self.snapshot(), before)
        shutil.copyfile(self.kit / "bin/research_records.py", self.hub / "bin/research_records.py")
        self.upgrade("--apply")
        self.assertEqual(self.events()[-1]["counts"]["conflict"], 0)

    def test_missing_plugins_directory_and_config_are_created(self):
        shutil.rmtree(self.hub / "bin/plugins")
        (self.hub / "hub-config.json").unlink()
        dry = self.upgrade().stdout
        self.assertIn("create bin/plugins/ for project plugins", dry)
        self.assertIn("write hub-config.json from the kit template (the hub has none)", dry)
        self.assertIn("- bin/plugins/README.md", dry)
        self.upgrade("--apply")
        self.assertTrue((self.hub / "bin/plugins").is_dir())
        self.assertEqual((self.hub / "bin/plugins/README.md").read_bytes(), (self.kit / "bin/plugins/README.md").read_bytes())
        self.assertEqual((self.hub / "hub-config.json").read_bytes(), (self.kit / "HUB_CONFIG_TEMPLATE.json").read_bytes())
        self.assertIn("ok", self.call("check").stdout)

    def test_legacy_hub_without_manifest_is_recognized_through_kit_history(self):
        legacy_readme = b"# rmem-kit README as shipped by an old release\n"
        (self.hub / "README.md").write_bytes(legacy_readme)
        (self.hub / "AGENTS.md").write_bytes(b"# project-specific protocol\n")
        for name in (".kit-manifest.json", "hub-config.json", "CONFIG.md", "VERSION"):
            (self.hub / name).unlink()
        shutil.rmtree(self.hub / "bin/plugins")
        code = {name: sha((self.hub / name).read_bytes()) for name in ("bin/rmem", "bin/research_records.py")}
        history = {"schema_version": 1, "versions": [
            {"version": "0.2.1", "tag": "v0.2.1", "files": {"README.md": sha(legacy_readme)}},
            {"version": "0.3.1", "tag": "v0.3.1", "files": {**code, "README.md": sha(b"another readme")}}]}
        (self.kit / "KIT_HISTORY.json").write_text(json.dumps(history))
        project = {name: (self.hub / name).read_bytes() for name in PROJECT if name != "hub-config.json"}
        dry = self.upgrade().stdout
        self.assertIn("from: kit 0.3.1 (no .kit-manifest.json; inferred from KIT_HISTORY.json)", dry)
        self.assertIn("- README.md (matches kit 0.2.1)", dry)
        self.assertIn("- bin/rmem (matches kit 0.3.1)", dry)
        self.assertIn("- CONFIG.md", dry)
        self.assertIn("- AGENTS.md: inspect with `diff -u", dry)
        self.upgrade("--apply")
        self.assertEqual((self.hub / "README.md").read_bytes(), (self.kit / "README.md").read_bytes())
        self.assertEqual((self.hub / "AGENTS.md").read_bytes(), b"# project-specific protocol\n")
        self.assertTrue((self.hub / "bin/plugins/README.md").is_file())
        self.assertTrue((self.hub / "hub-config.json").is_file())
        for name, data in project.items():
            if name != "ledger.jsonl":
                self.assertEqual((self.hub / name).read_bytes(), data, name)
        event = self.events()[-1]
        self.assertEqual((event["from_version"], event["to_version"]), ("0.3.1", "0.9.0"))
        self.assertIn("inferred from KIT_HISTORY.json", event["from_basis"])
        manifest = json.loads((self.hub / ".kit-manifest.json").read_text())
        self.assertEqual(manifest["kit_version"], "0.9.0")
        self.assertNotIn("first_installed_at", manifest)
        self.assertNotIn("findings/README.md", manifest["files"])

    def test_unknown_legacy_files_are_conflicts(self):
        (self.hub / ".kit-manifest.json").unlink()
        (self.kit / "KIT_HISTORY.json").write_text(json.dumps({"schema_version": 1, "versions": []}))
        dry = self.upgrade().stdout
        self.assertIn("from: kit unknown (no .kit-manifest.json; no file matches a known kit version)", dry)
        self.assertIn("- README.md: inspect with `diff -u", dry)
        self.assertIn("--apply is refused while kit code files have local changes: bin/research_records.py, bin/rmem", dry)

    def test_gitignore_is_merged_line_by_line(self):
        (self.hub / ".gitignore").write_text("runs/\nclaims/\n.rmem.lock\nmydata/\n!keep/this.log\n")
        # WHY: derive the expected rules from the kit, so a new kit ignore rule does not break this test.
        kit_rules = [line for line in (self.kit / ".gitignore").read_text().splitlines()
                     if line and not line.startswith("#")]
        added = [rule for rule in kit_rules if rule not in ("/runs/", "/claims/", "/.rmem.lock")]
        dry = self.upgrade().stdout
        self.assertIn(f"- .gitignore (anchors 3 kit rule(s), adds {len(added)} missing kit rule(s) at the top)", dry)
        self.upgrade("--apply")
        lines = (self.hub / ".gitignore").read_text().splitlines()
        self.assertEqual(lines[-5:], ["/runs/", "/claims/", "/.rmem.lock", "mydata/", "!keep/this.log"])
        self.assertEqual(set(lines[:len(added)]), set(added))
        manifest = json.loads((self.hub / ".kit-manifest.json").read_text())
        self.assertEqual(manifest["files"][".gitignore"], sha((self.hub / ".gitignore").read_bytes()))

    def test_refuses_a_source_that_is_not_a_kit(self):
        self.assertIn("names this hub", self.call("upgrade", "--from", self.hub, ok=False).stderr)
        self.assertIn("is not a directory", self.call("upgrade", "--from", self.root / "missing", ok=False).stderr)
        empty = self.root / "empty"
        empty.mkdir()
        self.assertIn("is not an rmem kit of version 0.4.0 or newer", self.call("upgrade", "--from", empty, ok=False).stderr)
        other = self.root / "other-hub"
        self.call("init", other)
        self.assertIn("is an installed hub", self.call("upgrade", "--from", other, ok=False).stderr)

    def test_current_kit_is_readable_and_a_fresh_hub_is_up_to_date(self):
        output = self.call("upgrade", "--from", KIT).stdout
        self.assertIn(f"Nothing to upgrade: the hub already has kit {VERSION}.", output)
        self.assertIn("nothing recorded", self.call("upgrade", "--from", KIT, "--apply").stdout)

    def test_kit_history_lists_the_released_versions(self):
        history = json.loads((KIT / "KIT_HISTORY.json").read_text())
        self.assertEqual(history["schema_version"], 1)
        self.assertEqual([row["version"] for row in history["versions"]], ["0.2.0", "0.2.1", "0.3.0", "0.3.1", "0.4.0"])
        for row in history["versions"]:
            self.assertEqual(row["tag"], "v" + row["version"])
            self.assertIn("bin/rmem", row["files"])
            self.assertIn("README.md", row["files"])
            for name, digest in row["files"].items():
                self.assertFalse(name.startswith("rmem-kit/"), name)
                self.assertRegex(digest, r"^[0-9a-f]{64}$")
        self.assertIn("bin/context_reader.py", history["versions"][-1]["files"])
        self.assertTrue(re.search(r"KIT_HISTORY\.json", (KIT / "UPGRADE.md").read_text()))


if __name__ == "__main__":
    unittest.main()
