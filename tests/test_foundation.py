"""0.4.0 foundation: registry and plugins, configuration, character caps, read-only
access, doctor, version manifest, time display and the parser. Disposable hubs only."""
import datetime as dt
import hashlib
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import unittest


KIT = Path(__file__).resolve().parents[1] / "rmem-kit"
CLI = KIT / "bin" / "rmem"
VERSION = (KIT / "VERSION").read_text().strip()


def load_core():
    """Import bin/rmem as a module (it has no .py suffix) without running main()."""
    if str(KIT / "bin") not in sys.path:
        sys.path.insert(0, str(KIT / "bin"))
    loader = importlib.machinery.SourceFileLoader("rmem_under_test", str(CLI))
    module = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
    loader.exec_module(module)
    return module


PLUGIN = '''
EVENT_KINDS = {"demo_note"}
EVENT_STRING_FIELDS = {"to"}


def validate_event(event):
    if event["kind"] == "demo_note" and not event.get("to"):
        return "demo_note requires to"
    return ""


def register(commands, core):
    parser = commands.add_parser("demo", help="append a demo note")
    parser.add_argument("--to", required=True)

    def run(a):
        hub, who = core.find_hub(), core.actor(a, required=True)
        with core.hub_lock(hub):
            core.consistency_preflight(hub)
            event = core.append_locked(hub, {**who, "kind": "demo_note", "to": a.to})
        print("demo: " + core.fmt_event(event, core.load_config(hub)))
        return 0
    parser.set_defaults(fn=run)


def status_sections(ctx):
    return ["## Demo plugin", f"demo saw {len(ctx.events)} events as {ctx.who.get('session', '?')}"]


def check_problems(ctx):
    return [("warn", "demo warning"), ("info", "demo info")]
'''


class FoundationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="rmem-foundation-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.hub = self.root / "hub"
        self.env = {k: v for k, v in os.environ.items()
                    if not k.startswith(("RMEM_", "CODEX_", "CLAUDE"))}
        self.env.update(RMEM_HUB=str(self.hub), RMEM_SESSION="session-a", RMEM_AGENT="codex",
                        PYTHONDONTWRITEBYTECODE="1")
        self.call("init", self.hub, "--project-name", "Foundation test")

    def call(self, *args, ok=True, env=None, cwd=None):
        result = subprocess.run([sys.executable, str(CLI), *map(str, args)], cwd=cwd or self.root,
                                env=env or self.env, capture_output=True, text=True, timeout=30)
        if ok:
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout)
        return result

    def events(self):
        return [json.loads(line) for line in (self.hub / "ledger.jsonl").read_text().splitlines()]

    def write_config(self, value):
        (self.hub / "hub-config.json").write_text(json.dumps(value))

    def freeze(self, path=None):
        """Make the hub read-only for this (non-root) user; restored on cleanup."""
        path = path or self.hub
        self.addCleanup(self.thaw, path)
        for directory, names, files in os.walk(path, topdown=False):
            for name in files:
                target = Path(directory) / name
                if not target.is_symlink():
                    target.chmod(stat.S_IMODE(target.stat().st_mode) & 0o555)
            Path(directory).chmod(0o555)

    @staticmethod
    def thaw(path):
        for directory, names, files in os.walk(path):
            Path(directory).chmod(0o755)
            for name in files:
                target = Path(directory) / name
                if not target.is_symlink():
                    target.chmod(stat.S_IMODE(target.stat().st_mode) | 0o600)

    def require_non_root(self):
        if os.geteuid() == 0:
            self.skipTest("permission checks do not apply to root")

    # A. Registry, core namespace and plugins

    def test_core_namespace_exposes_the_documented_contract(self):
        core = load_core()
        self.assertTrue(all(isinstance(module, type(core)) for module in core.FEATURE_MODULES))
        for module in core.FEATURE_MODULES:  # every built-in module is shipped by init
            self.assertIn(Path(module.__file__).name, core.KIT_HELPERS)
            self.assertEqual(Path(module.__file__).resolve().parent, KIT / "bin", module.__name__)
        names = ("RmemError find_hub validate_hub hub_lock events_locked append_locked actor one_line "
                 "identifier count regex safe_path atomic_write open_regular bounded_lines text_chars "
                 "text_excerpt Output clipped fmt_event now load_config display_time parse_user_time "
                 "consistency_preflight authorize_thread_write describe_owner thread_visible run_git "
                 "provenance source_records kit_root kit_version").split()
        for name in names:
            self.assertTrue(callable(getattr(core.core, name)), name)
        self.assertEqual(core.core.kit_version(), (KIT / "VERSION").read_text().strip())

    def test_plugin_adds_command_kinds_status_and_check_lines(self):
        (self.hub / "bin/plugins/demo.py").write_text(PLUGIN)
        result = self.call("demo", "--to", "alice", "--session", "session-after")
        self.assertIn("demo_note", result.stdout)
        self.assertEqual(self.events()[-1]["to"], "alice")
        self.assertEqual(self.events()[-1]["session"], "session-after")
        status = self.call("status").stdout
        self.assertIn("## Demo plugin", status)
        self.assertIn("demo saw 1 events as session-a", status)
        check = self.call("check").stdout
        self.assertIn("warn: plugins/demo.py: demo warning", check)
        self.assertIn("info: plugins/demo.py: demo info", check)
        self.assertNotIn("neither the core", check)
        with (self.hub / "ledger.jsonl").open("a") as stream:
            stream.write(json.dumps({"ts": "2026-01-01T00:00:00+00:00", "kind": "mystery"}) + "\n")
        self.assertIn("mystery (1)", self.call("check").stdout)
        with (self.hub / "ledger.jsonl").open("a") as stream:
            stream.write(json.dumps({"ts": "2026-01-01T00:00:00+00:00", "kind": "demo_note"}) + "\n")
            stream.write(json.dumps({"ts": "2026-01-01T00:00:00+00:00", "kind": "exp", "to": 3}) + "\n")
        failed = self.call("check", ok=False).stdout
        self.assertIn("demo_note requires to", failed)
        self.assertIn("event to must be a string", failed)

    def test_plugins_others_could_write_are_never_run(self):
        marker = self.root / "PWNED"
        payload = f"open({str(marker)!r}, 'a').write('plugin ran\\n')\n"
        # A hub planted in a directory every user can write to, above the victim's working directory.
        shared = self.root / "shared"
        evil = shared / "evilhub"
        (evil / "bin/plugins").mkdir(parents=True)
        (evil / "bin/plugins/pwn.py").write_text(payload)
        for name in ("STATE.md", "ledger.jsonl"):
            (evil / name).write_text("")
        for name in ("threads", "journal", "findings"):
            (evil / name).mkdir()
        (shared / ".rmem").write_text("evilhub\n")
        work = shared / "victim/work"
        work.mkdir(parents=True)
        shared.chmod(0o777)
        self.addCleanup(shared.chmod, 0o755)
        unpointed = {k: v for k, v in self.env.items() if k != "RMEM_HUB"}
        for args in (("--help",), ("version",), ("doctor",), ("status",), ("recent",)):
            subprocess.run([sys.executable, str(CLI), *args], cwd=work, env=unpointed, capture_output=True,
                           text=True, timeout=30)
        self.assertFalse(marker.exists())
        self.assertIn("the search stopped at", self.call("doctor", env=unpointed, cwd=work).stdout)
        # In the real hub: a plugin file that others can write is skipped; RMEM_NO_PLUGINS skips all.
        plugin = self.hub / "bin/plugins/demo.py"
        plugin.write_text(PLUGIN)
        plugin.chmod(0o666)
        self.assertIn("every user can write to it", self.call("doctor").stdout)
        self.call("demo", "--to", "alice", ok=False)
        plugin.chmod(0o644)
        self.call("demo", "--to", "alice")
        self.call("demo", "--to", "alice", env={**self.env, "RMEM_NO_PLUGINS": "1"}, ok=False)
        # Top-level help, version and init never load plugins; a hub command does.
        (self.hub / "bin/plugins/mark.py").write_text(payload)
        for args in (("--help",), ("version",), ("init", self.root / "fresh")):
            self.call(*args)
        self.assertFalse(marker.exists())
        self.call("status")
        self.assertTrue(marker.exists())

    def test_broken_plugins_are_skipped_and_reported_without_breaking_reads(self):
        plugins = self.hub / "bin/plugins"
        (plugins / "broken.py").write_text("raise RuntimeError('boom at import')\n")
        (plugins / "syntax.py").write_text("def broken(:\n")
        (plugins / "hooks.py").write_text(
            "def status_sections(ctx):\n    raise ValueError('status hook bug')\n"
            "def check_problems(ctx):\n    return 'not a list'\n"
            "def register(commands, core):\n    commands.add_parser('status')\n")
        status = self.call("status")
        self.assertIn("## Threads", status.stdout)
        self.assertIn("not working", status.stdout)
        for command in ("recent", "catalog"):
            self.call(command)
        check = self.call("check")
        self.assertIn("warn: plugin module plugins/broken.py is not working", check.stdout)
        self.assertIn("boom at import", check.stdout)
        self.assertIn("plugins/syntax.py", check.stdout)
        # Python 3.11+ refuses the duplicate parser itself; 3.10 replaces it and rmem restores the core one.
        self.assertRegex(check.stdout, r"plugins/hooks\.py is not working: register (failed|tried to replace)")
        self.assertIn("ok", check.stdout.splitlines()[-1])
        doctor = self.call("doctor").stdout
        self.assertIn("not working: plugins/broken.py", doctor)
        self.assertIn("not working: plugins/hooks.py", doctor)

    # B. Configuration

    def test_init_writes_documented_config_template(self):
        config = json.loads((self.hub / "hub-config.json").read_text())
        self.assertIn("_doc", config)
        template = json.loads((KIT / "HUB_CONFIG_TEMPLATE.json").read_text())
        self.assertEqual(config, template)
        core = load_core()
        for key, value in core.rmem_config.DEFAULTS.items():
            self.assertEqual(template[key], value, key)
        self.assertNotIn("warn:", self.call("check").stdout)
        documented = (KIT / "CONFIG.md").read_text()
        for key, value in core.rmem_config.DEFAULTS.items():
            for member in (value if isinstance(value, dict) and key != "roles" else {}):
                self.assertIn(f"`{key}.{member}`", documented)

    def test_config_overrides_caps_and_reports_unknown_or_invalid_keys(self):
        self.write_config({"caps": {"thread_max_chars": 100, "bogus": 1}, "colour": "blue", "_note": "ignored"})
        (self.hub / "threads/small.md").write_text("x" * 101 + "\n")
        result = self.call("check", ok=False).stdout
        self.assertIn("threads/small.md exceeds context cap (102 / 100 characters", result)
        self.assertIn("warn: hub-config.json: unknown key 'colour'", result)
        self.assertIn("warn: hub-config.json: unknown key 'caps.bogus'", result)
        self.assertNotIn("_note", result)
        self.write_config({"caps": {"thread_max_chars": "9000"}})
        for command in ("status", "log"):
            rejected = self.call(command, ok=False)
            self.assertIn("caps.thread_max_chars must be an integer", rejected.stderr)
        self.assertIn("caps.thread_max_chars", self.call("check", ok=False).stdout)
        self.assertIn("invalid: hub-config.json: caps.thread_max_chars", self.call("doctor").stdout)
        # Every invalid key is named at once, not one per run.
        self.write_config({"caps": {"thread_max_chars": "9000"}, "leases": {"default_ttl_hours": -1}})
        for output in (self.call("status", ok=False).stderr, self.call("doctor").stdout):
            self.assertIn("caps.thread_max_chars must be an integer", output)
            self.assertIn("leases.default_ttl_hours must be a number greater than 0", output)
        for bad in ({"caps": {"soft_ratio": 0}}, {"display_tz": "Mars/Olympus"}, {"schema_version": 2},
                    {"caps": []}, {"leases": {"default_ttl_hours": True}}):
            self.write_config(bad)
            self.call("status", ok=False)
        (self.hub / "hub-config.json").write_text('{"caps": {}, "caps": {}}')
        self.assertIn("duplicate key", self.call("status", ok=False).stderr)
        (self.hub / "hub-config.json").unlink()
        self.call("check")

    def test_display_zone_comes_from_config_and_rmem_tz_overrides_it(self):
        self.call("log", "--note", "zoned")
        self.write_config({"display_tz": "Asia/Tokyo"})
        self.assertIn(" JST #1 | codex s=session-a | exp", self.call("recent").stdout)
        pacific = {**self.env, "RMEM_TZ": "America/Los_Angeles"}
        self.assertRegex(self.call("recent", env=pacific).stdout, r" P[DS]T #1 \| ")
        self.assertIn("RMEM_TZ", self.call("recent", env={**self.env, "RMEM_TZ": "Nowhere/City"}, ok=False).stderr)

    # C. Character caps and budgets

    def test_caps_count_characters_with_soft_warning(self):
        cjk = "研究记忆" * 750  # 3,000 characters, 9,000 UTF-8 bytes
        (self.hub / "threads/cjk.md").write_text(cjk)
        self.assertEqual(len((self.hub / "threads/cjk.md").read_bytes()), 9000)
        result = self.call("check").stdout
        self.assertNotIn("cjk.md", result)
        (self.hub / "threads/soft.md").write_text(("word " * 18 + "\n") * 72)  # 72 of 80 lines
        result = self.call("check")
        self.assertIn("warn: threads/soft.md is at 90% of its context cap", result.stdout)
        self.assertIn("never delete spaces", result.stdout)
        (self.hub / "threads/over.md").write_text("y" * 9000 + "\n")  # 9,001 characters
        failed = self.call("check", ok=False).stdout
        self.assertIn("! threads/over.md exceeds context cap (9,001 / 9,000 characters, 1 / 80 lines)", failed)
        self.assertIn("findings/<topic>.md", failed)
        (self.hub / "threads/over.md").write_text("y" * 8999 + "\n")
        self.call("check")

    def test_status_shows_budgets_and_gives_selected_thread_its_own_budget(self):
        (self.hub / "STATE.md").write_text(("state " * 11 + "words\n") * 110)  # 7,920 characters
        lines = [f"line {number:02d} " + "detail " * 14 for number in range(75)] + ["END-OF-THREAD"]
        (self.hub / "threads/big.md").write_text("\n".join(lines) + "\n")
        (self.hub / "threads/tiny.md").write_text("short\n")
        status = self.call("status", "--thread", "big").stdout
        self.assertRegex(status, r"Budget: STATE\.md 7,920 / 8,000 characters, 110 / 120 lines \(\d+% of cap \(above the 70% soft target\)\)")
        self.assertRegex(status, r"- big: free · \d+% of cap \(above the 70% soft target\)")
        self.assertRegex(status, r"- tiny: free · \d+%\n")
        self.assertIn("END-OF-THREAD", status)
        self.assertNotIn("[output truncated", status)
        self.assertLessEqual(len(self.call("status").stdout), 16000)

    def test_record_view_messages_use_normal_spaces(self):
        load_core()
        records = sys.modules["research_records"]
        view = records.bounded({"identifier": "X", "history": [], "padding": "z" * 13000})
        self.assertIn("exceeds 12,000 characters", view["error"])
        source = (KIT / "bin/research_records.py").read_text()
        self.assertNotRegex(source, r"exceeds\d|\dcharacters|\d+k;")
        self.assertIn("exceeds {RECORD_LIMIT:,} characters", source)

    # D. Read-only access

    def test_read_only_hub_reads_without_lock_and_writes_point_to_doctor(self):
        self.require_non_root()
        self.call("log", "--id", "E1", "--note", "readable evidence")
        (self.hub / ".rmem.lock").unlink()
        self.freeze()
        before = (self.hub / "ledger.jsonl").read_bytes()
        status = self.call("status")
        self.assertIn("read without the hub lock", status.stderr)
        self.assertEqual(status.stderr.count("\n"), 1)
        self.assertIn("readable evidence", self.call("recent").stdout)
        self.assertEqual(json.loads(self.call("catalog").stdout)["rows"][0]["experiment_id"], "E1")
        self.call("show", "E1")
        self.call("highlight", "list")
        self.call("check")
        self.call("version")
        doctor = self.call("doctor").stdout
        self.assertIn("ledger.jsonl: read ok, write denied", doctor)
        self.assertIn(".rmem.lock: missing; cannot be created", doctor)
        for args in (("log", "--note", "blocked"), ("claim", "alpha"), ("note", "blocked")):
            rejected = self.call(*args, ok=False)
            self.assertIn("rmem doctor", rejected.stderr)
        self.assertEqual((self.hub / "ledger.jsonl").read_bytes(), before)

    def test_existing_lock_file_is_opened_read_only_for_a_real_shared_lock(self):
        self.require_non_root()
        self.call("log", "--note", "creates the lock file")
        self.freeze()
        status = self.call("status")
        self.assertNotIn("without the hub lock", status.stderr)

    def test_unlocked_read_skips_an_in_progress_final_line(self):
        self.require_non_root()
        self.call("log", "--id", "E1", "--note", "complete event")
        with (self.hub / "ledger.jsonl").open("a") as stream:
            stream.write('{"ts": "2026-10-14T00:00:00+00:00", "kind": "exp", "no')
        (self.hub / ".rmem.lock").unlink()
        self.freeze()
        self.assertNotIn("CORRUPT", self.call("recent").stdout)
        self.call("catalog")
        self.call("check")
        self.thaw(self.hub)
        self.assertIn("! ledger: line 2: invalid JSON", self.call("check", ok=False).stdout)

    def test_highlight_history_ignores_an_in_progress_final_line(self):
        load_core()
        highlights = sys.modules["research_highlights"]
        frozen = json.dumps({"ts": "2026-10-14T00:00:00+00:00", "kind": "exp",
                             "event_id": "0" * 32}).encode() + b"\n"
        anchor = json.dumps({"ts": "2026-10-14T00:01:00+00:00", "kind": "highlight_created", "id": "hl-x",
                             "manifest_sha256": "a" * 64, "event_id": "1" * 32}).encode() + b"\n"
        relation = highlights.history_relationship(frozen, frozen + anchor + b'{"ts": "2026', "hl-x", "a" * 64)
        self.assertEqual(relation["relation"], "exact_prefix")
        with self.assertRaises(ValueError):
            highlights.history_relationship(frozen, frozen + anchor + b"[1]\n", "hl-x", "a" * 64)

    def test_read_only_clone_without_claims_directory_still_reads(self):
        self.require_non_root()
        for args in (("init", "-q"), ("config", "user.name", "Test"), ("config", "user.email", "t@example.test"),
                     ("add", "--all"), ("commit", "-qm", "hub")):
            subprocess.run(["git", "-C", str(self.hub), *args], check=True, capture_output=True, timeout=15)
        clone = self.root / "clone"
        subprocess.run(["git", "clone", "-q", str(self.hub), str(clone)], check=True, capture_output=True, timeout=15)
        self.freeze(clone)
        env = {**self.env, "RMEM_HUB": str(clone)}
        self.call("status", env=env)
        self.call("check", env=env)
        self.assertFalse((clone / "claims").exists())

    # E. Doctor

    def test_doctor_reports_access_identity_and_harness_hints_without_secrets(self):
        env = {**self.env, "CODEX_THREAD_ID": "secret-thread-value-123"}
        doctor = self.call("doctor", env=env).stdout
        self.assertIn(f"hub: {self.hub} (found via RMEM_HUB)", doctor)
        self.assertIn(f"python: {sys.version.split()[0]}", doctor)
        self.assertIn(f"kit: rmem-kit {VERSION}", doctor)
        self.assertIn("ledger.jsonl: read ok, write ok", doctor)
        self.assertIn("CODEX_THREAD_ID: present", doctor)
        self.assertIn("CLAUDE_CODE_SESSION_ID: absent", doctor)
        self.assertIn("RMEM_SESSION: present", doctor)
        self.assertNotIn("secret-thread-value-123", doctor)
        self.assertIn(f"codex --add-dir {self.hub}", doctor)
        self.assertNotIn("Claude Code:", doctor)
        self.assertIn("hub-config.json: present, valid", doctor)
        claude = self.call("doctor", env={**self.env, "CLAUDECODE": "1"}).stdout
        self.assertIn("Claude Code: allow this hub's CLI", claude)
        self.assertIn("Do not allow `rmem run` broadly", claude)
        project = self.root / "project"
        project.mkdir()
        self.call("link", project)
        unpointed = {k: v for k, v in self.env.items() if k != "RMEM_HUB"}
        self.assertIn(".rmem pointer", self.call("doctor", env=unpointed, cwd=project).stdout)
        # The allow rule uses the form the managed block tells agents to run, for every everyday command.
        hint = self.call("doctor", env={**unpointed, "CLAUDECODE": "1"}, cwd=project).stdout
        self.assertIn('"Bash(../hub/bin/rmem status:*)", "Bash(../hub/bin/rmem resume:*)"', hint)
        for name in ("recent", "whoami", "checkpoint", "log", "claim"):
            self.assertIn(name, hint.split("and the same for")[1].split(".")[0])
        self.assertNotIn(" run,", hint.split("and the same for")[1].split(".")[0])
        self.assertIn("settings.local.json", hint)

    # F. Version and manifest

    def test_init_writes_kit_manifest_and_version_reports_it(self):
        manifest = json.loads((self.hub / ".kit-manifest.json").read_text())
        version = (KIT / "VERSION").read_text().strip()
        self.assertRegex(version, r"^0\.4\.\d+(-dev)?$")
        self.assertEqual(manifest["kit_version"], version)
        self.assertEqual(manifest["schema_version"], 1)
        for name in ("bin/rmem", "bin/rmem_config.py", "VERSION", "CONFIG.md", ".rgignore",
                     "bin/plugins/README.md", "HUB_CONFIG_TEMPLATE.json", ".gitignore", "AGENTS.md"):
            self.assertIn(name, manifest["files"])
        for name in ("STATE.md", "MAP.md", "ledger.jsonl", "hub-config.json"):
            self.assertNotIn(name, manifest["files"])
        for name, digest in manifest["files"].items():
            self.assertEqual(hashlib.sha256((self.hub / name).read_bytes()).hexdigest(), digest, name)
        output = self.call("version").stdout
        self.assertIn(f"rmem-kit {version}", output)
        self.assertIn(f"hub manifest: kit {version}", output)
        self.assertIn("kit files changed since install: 0", output)

    def test_check_warns_on_modified_kit_files_and_notes_legacy_hubs(self):
        with (self.hub / "bin/rmem").open("a") as stream:
            stream.write("# local change\n")
        (self.hub / "CONFIG.md").unlink()
        check = self.call("check").stdout
        self.assertIn("warn: kit file CONFIG.md is missing since install", check)
        self.assertIn("warn: kit file bin/rmem is modified since install", check)
        version = self.call("version").stdout
        self.assertIn("kit files changed since install: 2", version)
        self.assertIn("- bin/rmem: modified", version)
        (self.hub / ".kit-manifest.json").unlink()
        self.assertIn("info: no .kit-manifest.json: this hub was installed before 0.4.0", self.call("check").stdout)
        self.assertIn("installed before 0.4.0", self.call("version").stdout)

    # G. Ignore rules, time display and journal names

    def test_ignore_rules_are_anchored_and_rgignore_is_installed(self):
        self.assertIn("/runs/", (self.hub / ".gitignore").read_text().splitlines())
        self.assertNotIn("runs/", (self.hub / ".gitignore").read_text().splitlines())
        subprocess.run(["git", "-C", str(self.hub), "init", "-q"], check=True, timeout=15)
        for name in ("runs/E1/x.log", "files/a/runs/x.txt", "claims/alpha/meta.json", "notes/claims/kept.md"):
            (self.hub / name).parent.mkdir(parents=True, exist_ok=True)
            (self.hub / name).write_text("x\n")

        def ignored(name):
            return subprocess.run(["git", "-C", str(self.hub), "check-ignore", "-q", name], timeout=15).returncode == 0
        self.assertTrue(ignored("runs/E1/x.log"))
        self.assertTrue(ignored("claims/alpha/meta.json"))
        self.assertFalse(ignored("files/a/runs/x.txt"))
        self.assertFalse(ignored("notes/claims/kept.md"))
        rg = (self.hub / ".rgignore").read_text().splitlines()
        for rule in ("/highlights/", "/history/", "/runs/"):
            self.assertIn(rule, rg)

    def test_display_time_and_parse_user_time(self):
        core = load_core()
        pacific = {"display_tz": "America/Los_Angeles"}
        self.assertEqual(core.display_time("2026-10-14T08:34:00.123+00:00", pacific), "2026-10-14 01:34 PDT")
        self.assertEqual(core.display_time("2026-01-14T08:34:00Z", pacific), "2026-01-14 00:34 PST")
        self.assertEqual(core.display_time("not a time", pacific), "not a time")
        utc = dt.timezone.utc
        expected = dt.datetime(2026, 10, 14, 8, 34, tzinfo=utc)
        for value in ("2026-10-14 01:34", "2026-10-14T01:34:00", "2026-10-14T08:34:00Z", "2026-10-14T01:34-07:00"):
            self.assertEqual(core.parse_user_time(value, pacific), expected, value)
        self.assertEqual(core.parse_user_time("2026-10-14", pacific), dt.datetime(2026, 10, 14, 7, 0, tzinfo=utc))
        before = dt.datetime.now(utc)
        for value, delta in (("90m", dt.timedelta(minutes=90)), ("6h", dt.timedelta(hours=6)), ("3d", dt.timedelta(days=3))):
            parsed = core.parse_user_time(value, pacific)
            self.assertLessEqual(abs((before - delta) - parsed), dt.timedelta(seconds=5))
        for invalid in ("yesterday", "5w", "2026-13-01", "999999d"):
            with self.assertRaises(core.RmemError):
                core.parse_user_time(invalid, pacific)

    def test_events_show_display_time_sequence_and_session(self):
        env = {**self.env, "RMEM_TZ": "America/Los_Angeles"}
        logged = self.call("log", "--note", "first", env=env).stdout
        self.assertRegex(logged, r"^logged: \d{4}-\d\d-\d\d \d\d:\d\d P[DS]T #1 \| codex s=session-a \| exp \|")
        self.call("claim", "alpha", env=env)
        status = self.call("status", "--include-locks", env=env).stdout
        self.assertRegex(status, r"- alpha: codex / session-a since \d{4}-\d\d-\d\d \d\d:\d\d P[DS]T")
        self.assertRegex(status, r"#2 \| codex s=session-a \| claim")

    def test_journal_file_uses_the_utc_date(self):
        before = dt.datetime.now(dt.timezone.utc).date().isoformat()
        self.call("note", "remember")
        after = dt.datetime.now(dt.timezone.utc).date().isoformat()
        names = [path.name for path in (self.hub / "journal").glob("*.md")]
        self.assertEqual(len(names), 1)
        self.assertIn(names[0][:10], {before, after})

    # H. Parser

    def test_identity_options_work_before_or_after_the_subcommand(self):
        env = {k: v for k, v in self.env.items() if k not in ("RMEM_SESSION", "RMEM_AGENT")}
        self.call("claim", "alpha", "--agent", "claude", "--session", "after-1", env=env)
        self.assertEqual((self.events()[-1]["agent"], self.events()[-1]["session"]), ("claude", "after-1"))
        self.call("--agent", "claude", "--session", "after-1", "release", "alpha", env=env)
        self.call("--agent", "x", "claim", "beta", "--agent", "claude", "--session", "after-2", env=env)
        self.assertEqual(self.events()[-1]["agent"], "claude")
        self.call("highlight", "list", "--session", "after-1", env=env)
        spec = self.root / "spec.json"
        spec.write_text("{}")
        rejected = self.call("highlight", "create", str(spec), "--agent", "claude", "--session", "after-1",
                             env=env, ok=False)
        self.assertNotIn("unrecognized arguments", rejected.stderr)
        self.assertIn("HIGHLIGHT_TEMPLATE.json", rejected.stderr)  # parsed, authorized, then the spec is checked
        rejected = self.call("run", "--id", "E1", "--thread", "beta", "--session", "after-1", "--",
                             sys.executable, "-c", "print(1)", env=env, ok=False)
        self.assertIn("belongs to session after-2", rejected.stderr)

    def test_unknown_command_suggests_close_matches(self):
        rejected = self.call("claims", "alpha", ok=False)
        self.assertEqual(rejected.returncode, 2)
        self.assertIn("unknown command 'claims'. Did you mean 'claim'", rejected.stderr)
        # Feature modules add commands, so check the sorted core names rather than a fixed prefix.
        self.assertRegex(rejected.stderr, r"Commands: [a-z, -]*\bcatalog, check, [a-z, -]*\bclaim, [a-z, -]*\bdoctor\b")
        nested = self.call("--session", "s", "highlight", "lst", ok=False)
        self.assertIn("Did you mean 'list'", nested.stderr)
        self.assertIn("unknown command 'zzzz'. Commands:", self.call("zzzz", ok=False).stderr)

    # I. Hook points used by the tracks

    def test_log_validation_is_separate_from_the_locked_commit(self):
        core = load_core()
        parser = core.build_parser()
        good = parser.parse_args(["log", "--note", "pure", "--commit", "abc123", "--metric", "acc=0.5"])
        event = core.build_log_event(good, {"agent": "test"})
        self.assertEqual((event["kind"], event["note"], event["metrics"]["acc"]), ("exp", "pure", 0.5))
        self.assertNotIn("event_id", event)
        bad = parser.parse_args(["log", "--metric", "broken"])
        with self.assertRaises(core.RmemError):
            core.build_log_event(bad, {"agent": "test"})
        with self.assertRaises(core.RmemError):
            core.authorize_thread_write(self.hub, "alpha", {"agent": "a", "session": "s"}, "delete")
        ctx = core.SimpleNamespace(hub=self.hub, config=core.rmem_config.defaults())
        self.assertEqual(core.describe_owner(ctx, "alpha"), "free")
        self.assertTrue(core.thread_visible(ctx, "alpha"))
        before = (self.hub / "ledger.jsonl").read_bytes()
        self.call("log", "--metric", "broken", ok=False)
        self.assertEqual((self.hub / "ledger.jsonl").read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
