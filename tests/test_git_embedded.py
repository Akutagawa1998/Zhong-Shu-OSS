"""Git integration for a hub embedded in a project repository: sync --embedded, link --canonical,
ledger checks, repair --conflict-markers and guard hooks. Disposable repositories only."""
import contextlib
import datetime as dt
import hashlib
import importlib.machinery
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock
import uuid


KIT = Path(__file__).resolve().parents[1] / "rmem-kit"
CLI = KIT / "bin" / "rmem"


def load_core():
    """Import bin/rmem as a module (it has no .py suffix) without running main()."""
    if str(KIT / "bin") not in sys.path:
        sys.path.insert(0, str(KIT / "bin"))
    loader = importlib.machinery.SourceFileLoader("rmem_git_under_test", str(CLI))
    module = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
    loader.exec_module(module)
    return module


def ago(**delta):
    return (dt.datetime.now(dt.timezone.utc) - dt.timedelta(**delta)).isoformat(timespec="microseconds")


class EmbeddedGitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="rmem-git-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.repo = self.root / "project"
        self.repo.mkdir()
        # An isolated Git configuration: the machine's global settings must not change the results.
        (self.root / "gitconfig").write_text("[user]\n\tname = Fixture\n\temail = fixture@example.invalid\n"
                                             "[commit]\n\tgpgsign = false\n[init]\n\tdefaultBranch = main\n")
        self.env = {k: v for k, v in os.environ.items() if not k.startswith(("RMEM_", "GIT_", "CODEX_", "CLAUDE"))}
        self.env.update(GIT_CONFIG_GLOBAL=str(self.root / "gitconfig"), GIT_CONFIG_NOSYSTEM="1",
                        RMEM_SESSION="session-a", RMEM_AGENT="codex", RMEM_PYTHON=sys.executable,
                        PYTHONDONTWRITEBYTECODE="1")
        self.git("init", "-q", "-b", "main")
        (self.repo / "code.py").write_text("print(1)\n")
        self.git("add", "code.py")
        self.git("commit", "-q", "-m", "Initial project")
        self.hub = self.repo / ".research-memory"
        self.env["RMEM_HUB"] = str(self.hub)
        self.rmem("init", self.hub)
        self.git("add", ".research-memory")
        self.git("commit", "-q", "-m", "Add the hub")
        self.ledger = self.hub / "ledger.jsonl"

    def git(self, *args, cwd=None, ok=True):
        result = subprocess.run(["git", "-C", str(cwd or self.repo), *map(str, args)], capture_output=True,
                                text=True, env=self.env, timeout=30)
        if ok:
            self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def rmem(self, *args, ok=True, env=None, cwd=None):
        result = subprocess.run([sys.executable, str(CLI), *map(str, args)], cwd=cwd or self.root,
                                env=env or self.env, capture_output=True, text=True, timeout=60)
        if ok:
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout)
        return result

    def append(self, *events):
        """Append valid events with chosen times (stands in for sessions that logged earlier)."""
        sequence = len(self.ledger.read_bytes().splitlines())
        written = []
        with self.ledger.open("a") as stream:
            for event in events:
                sequence += 1
                record = {"agent": "codex", "session": "session-a", **event,
                          "event_id": uuid.uuid4().hex, "sequence": sequence}
                stream.write(json.dumps(record) + "\n")
                written.append(record)
        return written

    def head(self):
        return self.git("rev-parse", "HEAD")

    def outside_hub_status(self):
        return [line for line in self.git("status", "--porcelain=v1", "--untracked-files=all").splitlines()
                if ".research-memory" not in line]

    # sync --embedded

    def test_per_event_commits_carry_event_dates_and_files_by_modification_time(self):
        events = self.append({"ts": ago(days=3), "kind": "exp", "id": "E1", "status": "keep", "note": "first"},
                             {"ts": ago(days=2), "kind": "decision", "id": "D2", "note": "second"},
                             {"ts": ago(days=1), "kind": "finding", "thread": "alpha", "note": "third"})
        state = self.hub / "STATE.md"
        state.write_text(state.read_text() + "\nA settled decision.\n")
        between = dt.datetime.fromisoformat(ago(days=2, hours=12)).timestamp()
        os.utime(state, (between, between))
        (self.hub / "findings/README.md").unlink()
        (self.hub / "threads/alpha.md").write_text("# Thread: alpha\n")
        old = self.head()
        output = self.rmem("sync", "--embedded", "--per-event").stdout
        self.assertIn("created 3 commit(s) on main", output)
        log = self.git("log", "--reverse", "--format=%H%x09%at%x09%ct%x09%s", f"{old}..HEAD").splitlines()
        self.assertEqual(len(log), 3)
        for line, event in zip(log, events):
            commit, author, committer, subject = line.split("\t")
            stamp = int(dt.datetime.fromisoformat(event["ts"]).timestamp())
            self.assertEqual((int(author), int(committer)), (stamp, stamp))
        self.assertEqual([line.split("\t")[3] for line in log],
                         ["Hub: exp E1 (keep)", "Hub: decision D2", "Hub: finding thread=alpha"])
        files = [set(self.git("show", "--name-only", "--format=", line.split("\t")[0]).splitlines()) for line in log]
        self.assertEqual(files[0], {".research-memory/ledger.jsonl"})
        self.assertEqual(files[1], {".research-memory/ledger.jsonl", ".research-memory/STATE.md"})
        self.assertEqual(files[2], {".research-memory/ledger.jsonl", ".research-memory/findings/README.md",
                                    ".research-memory/threads/alpha.md"})
        prefix = self.git("show", f"{log[0].split(chr(9))[0]}:.research-memory/ledger.jsonl")
        self.assertEqual(prefix, json.dumps(events[0]))
        self.assertEqual(self.git("status", "--porcelain", "--", ".research-memory"), "")
        # Already-committed events come from the ledger line count at HEAD, so only new events follow.
        self.rmem("log", "--id", "E4", "--status", "discard", "--note", "fourth")
        self.rmem("sync", "--embedded", "--per-event")
        self.assertEqual(self.git("log", "-1", "--format=%s"), "Hub: exp E4 (discard)")
        self.assertEqual(self.git("rev-list", "--count", f"{old}..HEAD"), "4")
        self.assertIn("no memory changes to commit", self.rmem("sync", "--embedded", "--per-event").stdout)

    def test_sync_leaves_the_working_tree_and_user_index_alone(self):
        (self.repo / "code.py").write_text("print(2)\n")          # unstaged change
        (self.repo / "staged.txt").write_text("staged by the user\n")
        self.git("add", "staged.txt")                               # staged change outside the hub
        (self.repo / "scratch.txt").write_text("untracked\n")      # untracked file
        staged_entry = self.git("ls-files", "-s", "staged.txt")
        before = self.outside_hub_status()
        self.rmem("log", "--id", "E1", "--note", "one event")
        self.rmem("sync", "--embedded", "-m", "Hub: memory snapshot")
        self.assertEqual(self.git("log", "-1", "--format=%s"), "Hub: memory snapshot")
        self.assertEqual(self.outside_hub_status(), before)
        self.assertEqual(self.git("ls-files", "-s", "staged.txt"), staged_entry)
        self.assertEqual((self.repo / "code.py").read_text(), "print(2)\n")
        self.assertEqual(self.git("show", "HEAD:code.py"), "print(1)")
        self.assertNotIn("staged.txt", self.git("show", "--name-only", "--format=", "HEAD"))
        self.assertEqual(self.git("status", "--porcelain", "--", ".research-memory"), "")

    def test_untracked_files_rmem_does_not_write_are_named_and_need_a_review(self):
        self.rmem("log", "--id", "E1", "--note", "one event")
        (self.hub / "augment_plan").mkdir()
        (self.hub / "augment_plan/draft.md").write_text("an unreviewed draft\n")
        (self.hub / "threads/alpha.md").write_text("# Thread: alpha\n")
        (self.hub / "local-roots.json").write_text("{}\n")
        capture = self.hub / "highlights/.capture-abc123"
        capture.mkdir(parents=True)
        (capture / "manifest.json").write_text("{}\n")
        (self.hub / "leftover.md.tmp").write_text("partial write\n")
        before = self.head()
        refused = self.rmem("sync", "--embedded", "--per-event", ok=False).stderr
        self.assertIn("not files rmem writes: .research-memory/augment_plan/draft.md", refused)
        self.assertIn("--include-untracked", refused)
        self.assertEqual(self.head(), before)
        dry = self.rmem("sync", "--embedded", "--per-event", "--include-untracked", "--dry-run").stdout
        self.assertIn("Hub files besides the ledger: 2 added, 0 modified, 0 deleted", dry)
        self.assertIn("  A .research-memory/augment_plan/draft.md", dry)
        self.assertIn("  A .research-memory/threads/alpha.md", dry)
        # Secret-looking names or contents and oversized files are refused even with --include-untracked.
        (self.hub / ".env.local").write_text("API_TOKEN=abc\n")
        (self.hub / "threads/beta.md").write_text('# Thread: beta\npassword = "hunter2-hunter2"\n')
        (self.hub / "big.bin").write_bytes(b"\0" * (6 * 1024 * 1024))
        refused = self.rmem("sync", "--embedded", "--include-untracked", ok=False).stderr
        self.assertIn(".research-memory/.env.local (the name looks like a secret", refused)
        self.assertIn(".research-memory/threads/beta.md (the content looks like a key, token or password", refused)
        self.assertIn(".research-memory/big.bin (6.0 MB, above storage.file_warn_mb 5)", refused)
        self.assertNotIn("hunter2", refused)
        self.assertEqual(self.head(), before)
        for name in (".env.local", "threads/beta.md", "big.bin"):
            (self.hub / name).unlink()
        output = self.rmem("sync", "--embedded", "--include-untracked").stdout
        self.assertIn("  A .research-memory/augment_plan/draft.md", output)
        tracked = self.git("ls-files", ".research-memory").splitlines()
        self.assertIn(".research-memory/augment_plan/draft.md", tracked)
        for name in ("local-roots.json", ".capture-abc123", "leftover.md.tmp"):
            self.assertFalse(any(name in path for path in tracked), name)

    def test_sync_holds_only_a_shared_lock_while_it_builds_commits(self):
        module = load_core()
        research_git = sys.modules["research_git"]
        self.append({"ts": ago(hours=2), "kind": "exp", "id": "E1"}, {"ts": ago(hours=1), "kind": "exp", "id": "E2"})
        real_build = research_git.build_commits
        appended = []

        def build_while_another_session_writes(*args, **kwargs):
            other = {**self.env, "RMEM_SESSION": "session-b"}
            appended.append(subprocess.run([sys.executable, str(CLI), "log", "--id", "E3", "--note", "meanwhile"],
                                           env=other, capture_output=True, text=True, timeout=20))
            return real_build(*args, **kwargs)

        args = SimpleNamespace(embedded=True, per_event=True, dry_run=False, message=None, include_untracked=False,
                               agent="codex", session="session-a")
        with mock.patch.dict(os.environ, self.env, clear=True), \
                mock.patch.object(research_git, "build_commits", build_while_another_session_writes), \
                contextlib.redirect_stdout(io.StringIO()):
            research_git.cmd_sync_embedded(args, module.core)
        self.assertEqual(appended[0].returncode, 0, appended[0].stderr)
        self.assertEqual(self.git("log", "-1", "--format=%s"), "Hub: exp E2")
        self.assertIn("Hub: exp E3", self.rmem("sync", "--embedded", "--per-event").stdout)

    def test_dry_run_changes_nothing_in_the_repository(self):
        self.append({"ts": ago(hours=2), "kind": "exp", "id": "E1"}, {"ts": ago(hours=1), "kind": "exp", "id": "E2"})

        def fingerprint():
            objects = sorted(str(p.relative_to(self.repo)) for p in (self.repo / ".git/objects").rglob("*") if p.is_file())
            index = hashlib.sha256((self.repo / ".git/index").read_bytes()).hexdigest()
            return objects, index, self.git("for-each-ref"), self.head(), self.ledger.read_bytes()

        before = fingerprint()
        output = self.rmem("sync", "--embedded", "--per-event", "--dry-run").stdout
        self.assertIn("dry run: would create 2 commit(s)", output)
        self.assertIn("Hub: exp E2", output)
        self.assertEqual(fingerprint(), before)

    def test_refuses_detached_head_operation_in_progress_and_staged_hub_changes(self):
        self.rmem("log", "--id", "E1", "--note", "pending")
        self.git("checkout", "-q", "--detach")
        self.assertIn("HEAD is detached", self.rmem("sync", "--embedded", ok=False).stderr)
        self.git("checkout", "-q", "main")
        self.git("switch", "-q", "-c", "side")
        (self.repo / "code.py").write_text("print('side')\n")
        self.git("commit", "-q", "-m", "Side change", "code.py")
        self.git("switch", "-q", "main")
        (self.repo / "code.py").write_text("print('main')\n")
        self.git("commit", "-q", "-m", "Main change", "code.py")
        self.git("merge", "-q", "side", ok=False)
        before = self.head()
        self.assertIn("MERGE_HEAD", self.rmem("sync", "--embedded", ok=False).stderr)
        self.git("merge", "--abort")
        state = self.hub / "STATE.md"
        state.write_text(state.read_text() + "staged by hand\n")
        self.git("add", ".research-memory/STATE.md")
        refused = self.rmem("sync", "--embedded", "--per-event", ok=False).stderr
        self.assertIn("staged changes under the hub path", refused)
        self.assertIn("git reset -q -- .research-memory", refused)
        self.assertEqual(self.head(), before)
        self.assertIn(".research-memory/STATE.md", self.git("diff", "--cached", "--name-only"))

    def test_branch_moved_while_building_is_refused_by_compare_and_swap(self):
        module = load_core()
        research_git = sys.modules["research_git"]
        self.append({"ts": ago(hours=1), "kind": "exp", "id": "E1"})
        real_build, real_head = research_git.build_commits, research_git.head
        first = []

        def moving_build(*args, **kwargs):
            commits = real_build(*args, **kwargs)
            self.git("commit", "-q", "--allow-empty", "-m", "Concurrent commit")
            return commits

        def stale_head(repo):  # hides the move from the pre-check so that update-ref itself must refuse
            first.append(real_head(repo))
            return first[0]

        args = SimpleNamespace(embedded=True, per_event=True, dry_run=False, message=None,
                               agent="codex", session="session-a")
        for patches, expected in (((), "HEAD moved"), ((("head", stale_head),), "compare-and-swap")):
            with self.subTest(expected=expected), mock.patch.dict(os.environ, self.env, clear=True), \
                    mock.patch.object(research_git, "build_commits", moving_build), contextlib.ExitStack() as stack:
                for name, replacement in patches:
                    stack.enter_context(mock.patch.object(research_git, name, replacement))
                with contextlib.redirect_stdout(io.StringIO()), self.assertRaisesRegex(ValueError, expected):
                    research_git.cmd_sync_embedded(args, module.core)
                self.assertEqual(self.git("log", "-1", "--format=%s"), "Concurrent commit")
            first.clear()

    def test_plain_sync_points_to_embedded_and_standalone_hubs_also_work(self):
        self.assertIn("--embedded", self.rmem("sync", ok=False).stderr)
        self.assertIn("need --embedded", self.rmem("sync", "--per-event", ok=False).stderr)
        standalone = self.root / "standalone-hub"
        env = {**self.env, "RMEM_HUB": str(standalone)}
        self.rmem("init", standalone, env=env)
        self.git("init", "-q", "-b", "main", cwd=standalone)
        self.rmem("log", "--id", "S1", "--status", "keep", "--note", "standalone", env=env)
        self.rmem("sync", "--embedded", "--per-event", env=env)
        self.assertEqual(self.git("log", "-1", "--format=%s", cwd=standalone), "Hub: exp S1 (keep)")
        tracked = self.git("ls-files", cwd=standalone).splitlines()
        self.assertIn("ledger.jsonl", tracked)
        self.assertFalse(any(name.startswith(("claims/", "runs/")) for name in tracked))

    # link --canonical

    def test_link_canonical_writes_an_absolute_untracked_pointer(self):
        self.rmem("link", "--canonical", self.repo)
        self.assertEqual((self.repo / ".rmem").read_text().strip(), str(self.hub))
        self.assertIn("/.rmem", (self.repo / ".git/info/exclude").read_text().splitlines())
        self.assertNotIn(".rmem", self.git("status", "--porcelain", "--untracked-files=all"))
        self.assertIn('"$(cat .rmem)/bin/rmem" status', (self.repo / "AGENTS.md").read_text())
        worktree = self.root / "wt"
        self.git("worktree", "add", "-q", worktree, "-b", "experiment")
        self.rmem("link", "--canonical", worktree)
        self.assertEqual((worktree / ".rmem").read_text().strip(), str(self.hub))
        unpointed = {k: v for k, v in self.env.items() if k != "RMEM_HUB"}
        self.assertIn(f"# rmem hub: {self.hub}", self.rmem("status", env=unpointed, cwd=worktree).stdout)
        self.git("add", "-f", ".rmem")
        self.git("commit", "-q", "-m", "Track the pointer by mistake")
        output = self.rmem("link", "--canonical", self.repo).stdout
        self.assertIn("rm --cached .rmem", output)
        self.assertIn(".rmem", self.git("ls-files"))  # never untracked automatically

    # check: worktree, uncommitted events, order

    def test_linked_worktree_hub_warns_that_it_is_not_canonical(self):
        worktree = self.root / "wt"
        self.git("worktree", "add", "-q", worktree, "-b", "experiment")
        env = {**self.env, "RMEM_HUB": str(worktree / ".research-memory")}
        status = self.rmem("status", env=env).stdout
        self.assertIn("linked Git worktree", status)
        self.assertIn(str(self.hub), status)
        check = self.rmem("check", env=env).stdout
        self.assertIn("warn: research_git: this hub is inside a linked Git worktree", check)
        self.assertNotIn("linked Git worktree", self.rmem("check").stdout)

    def test_uncommitted_events_warn_after_the_configured_hours(self):
        self.append({"ts": ago(days=3), "kind": "exp", "id": "OLD"}, {"ts": ago(hours=1), "kind": "exp", "id": "NEW"})
        check = self.rmem("check").stdout
        self.assertIn("warn: research_git: 2 ledger event(s) are not committed to Git", check)
        self.assertIn("rmem sync --embedded --per-event", check)
        self.assertIn("not committed", self.rmem("status").stdout)
        config = json.loads((self.hub / "hub-config.json").read_text())
        config["git"]["uncommitted_warn_hours"] = 100
        (self.hub / "hub-config.json").write_text(json.dumps(config))
        self.assertNotIn("not committed", self.rmem("check").stdout)
        config["git"]["uncommitted_warn_hours"] = 24
        (self.hub / "hub-config.json").write_text(json.dumps(config))
        self.rmem("sync", "--embedded", "--per-event")
        self.assertNotIn("not committed", self.rmem("check").stdout)

    def test_order_inversions_warn_without_failing(self):
        self.append({"ts": ago(hours=1), "kind": "exp", "id": "LATE"}, {"ts": ago(days=1), "kind": "exp", "id": "MERGED"})
        lines = self.ledger.read_text().splitlines()
        swapped = json.loads(lines[1])
        swapped["sequence"] = 1
        first = json.loads(lines[0])
        first["sequence"] = 2
        self.ledger.write_text(json.dumps(first) + "\n" + json.dumps(swapped) + "\n")
        check = self.rmem("check").stdout
        self.assertIn("1 event(s) have an earlier timestamp than the event before them", check)
        self.assertIn("1 stored sequence number(s) go backward", check)
        self.assertIn("ok", check.splitlines()[-1])

    # repair --conflict-markers

    def test_conflict_markers_are_named_and_repaired_with_a_backup(self):
        events = self.append({"ts": ago(hours=3), "kind": "exp", "id": "E1"}, {"ts": ago(hours=2), "kind": "exp", "id": "E2"},
                             {"ts": ago(hours=1), "kind": "exp", "id": "E3"})
        lines = self.ledger.read_text().splitlines(keepends=True)
        conflicted = (lines[0] + "<<<<<<< Updated upstream\n" + lines[1] + "=======\n" + lines[2]
                      + ">>>>>>> Stashed changes\n")
        self.ledger.write_text(conflicted)
        check = self.rmem("check", ok=False).stdout
        self.assertIn("line 2: Git conflict marker line", check)
        self.assertIn("contains 3 Git conflict marker line(s)", check)
        self.assertIn("rmem repair --conflict-markers", check)
        self.assertIn("Git conflict marker", self.rmem("log", "--id", "X", "--note", "blocked", ok=False).stderr)
        preview = self.rmem("repair", "--conflict-markers").stdout
        self.assertIn("dry run: ledger.jsonl has 3 Git conflict marker line(s)", preview)
        self.assertIn("leaves 3 valid events", preview)
        self.assertEqual(self.ledger.read_text(), conflicted)
        self.assertEqual(list(self.hub.glob("ledger.jsonl.bak-*")), [])
        self.assertIn("only supports --conflict-markers", self.rmem("repair", ok=False).stderr)
        self.rmem("repair", "--conflict-markers", "--apply")
        backups = list(self.hub.glob("ledger.jsonl.bak-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(), conflicted)
        repaired = self.ledger.read_text().splitlines(keepends=True)
        self.assertEqual(repaired[:3], lines)
        repair = json.loads(repaired[3])
        self.assertEqual((repair["kind"], repair["removed_lines"], repair["kept_events"]), ("ledger_repair", 3, 3))
        self.assertEqual(repair["removed_line_numbers"], [2, 4, 6])
        self.assertEqual(repair["backup_sha256"], hashlib.sha256(conflicted.encode()).hexdigest())
        self.assertEqual(self.rmem("check").stdout.splitlines()[-1], "ok")
        self.assertNotIn("ledger.jsonl.bak", self.git("status", "--porcelain", "--untracked-files=all"))
        self.assertEqual([json.loads(line)["event_id"] for line in repaired[:3]], [e["event_id"] for e in events])

    def test_a_forked_conversation_changes_no_file_in_repair_or_link(self):
        bound = {**self.env, "CLAUDE_CODE_SESSION_ID": "11111111-1111-4111-8111-111111111111"}
        forked = {**self.env, "CLAUDE_CODE_SESSION_ID": "22222222-2222-4222-8222-222222222222"}
        self.rmem("log", "--id", "E1", "--note", "binds session-a to its conversation", env=bound)
        conflicted = self.ledger.read_text() + "<<<<<<< HEAD\n=======\n>>>>>>> other\n"
        self.ledger.write_text(conflicted)
        refused = self.rmem("repair", "--conflict-markers", "--apply", env=forked, ok=False).stderr
        self.assertIn("forked or copied conversation", refused)
        self.assertEqual(self.ledger.read_text(), conflicted)
        self.assertEqual(list(self.hub.glob("ledger.jsonl.bak-*")), [])
        self.rmem("repair", "--conflict-markers", "--apply", env=bound)
        self.assertEqual(json.loads(self.ledger.read_text().splitlines()[-1])["kind"], "ledger_repair")
        project = self.root / "second-project"
        project.mkdir()
        self.assertIn("forked", self.rmem("link", project, env=forked, ok=False).stderr)
        self.assertEqual(list(project.iterdir()), [])

    def test_repair_refuses_partial_lines_and_changes_nothing(self):
        self.append({"ts": ago(hours=2), "kind": "exp", "id": "E1"})
        conflicted = self.ledger.read_text() + '<<<<<<< HEAD\n{"ts": "2026-10-\n=======\n>>>>>>> other\n'
        self.ledger.write_text(conflicted)
        for args in (("--conflict-markers",), ("--conflict-markers", "--apply")):
            refused = self.rmem("repair", *args, ok=False).stderr
            self.assertIn("cannot repair automatically", refused)
        self.assertEqual(self.ledger.read_text(), conflicted)
        self.assertEqual(list(self.hub.glob("ledger.jsonl.bak-*")), [])

    # guard

    def test_guard_install_is_a_dry_run_and_never_overwrites_hooks(self):
        hooks = self.repo / ".git/hooks"
        preview = self.rmem("guard", "install").stdout
        self.assertIn("dry run", preview)
        self.assertIn("post-checkout: would be created", preview)
        self.assertFalse((hooks / "post-checkout").exists())
        custom = "#!/bin/sh\necho custom\n"
        (hooks / "post-merge").write_text(custom)
        installed = self.rmem("guard", "install", "--write").stdout
        for name in ("post-checkout", "post-rewrite", "pre-rebase"):
            self.assertIn(f"- {name}: created", installed)
            self.assertTrue(os.access(hooks / name, os.X_OK))
        self.assertIn("post-merge: a hook already exists; not changed", installed)
        self.assertIn("guard run post-merge", installed)
        self.assertEqual((hooks / "post-merge").read_text(), custom)
        again = self.rmem("guard", "install", "--write").stdout
        self.assertIn("post-checkout: already installed", again)
        self.append({"ts": ago(hours=1), "kind": "exp", "id": "PENDING"})
        switched = subprocess.run(["git", "-C", str(self.repo), "checkout", "-q", "-b", "other"], capture_output=True,
                                  text=True, env=self.env, timeout=60)
        self.assertEqual(switched.returncode, 0, switched.stderr)
        self.assertIn("rmem guard (post-checkout): 1 ledger event(s)", switched.stderr)
        self.assertEqual(self.git("rev-parse", "--abbrev-ref", "HEAD"), "other")

    def test_guard_hooks_run_a_pinned_cli_and_no_checked_out_plugin(self):
        self.rmem("guard", "install", "--write")
        hook = (self.repo / ".git/hooks/post-checkout").read_text()
        pinned = self.repo.resolve() / ".git" / "rmem-guard"
        self.assertIn(str(pinned), hook)
        self.assertIn("RMEM_NO_PLUGINS=1", hook)
        self.assertEqual(len(list(pinned.glob("*/bin/rmem"))), 1)
        marker = self.root / "PWNED"
        self.git("switch", "-q", "-c", "evil")
        (self.hub / "bin/plugins/pwn.py").write_text(f"open({str(marker)!r}, 'a').write('plugin ran')\n")
        cli = self.hub / "bin/rmem"
        cli.write_text(cli.read_text().replace("import argparse\n", f"import argparse\nopen({str(marker)!r}, 'a').write('cli ran')\n", 1))
        self.git("add", ".research-memory")
        self.git("commit", "-q", "-m", "A branch that changes hub code")
        self.git("switch", "-q", "main")
        self.append({"ts": ago(hours=1), "kind": "exp", "id": "PENDING"})
        switched = subprocess.run(["git", "-C", str(self.repo), "switch", "-q", "evil"], capture_output=True,
                                  text=True, env=self.env, timeout=60)
        self.assertEqual(switched.returncode, 0, switched.stderr)
        self.assertIn("rmem guard (post-checkout)", switched.stderr)
        self.assertFalse(marker.exists(), switched.stderr)

    def test_guard_refuses_a_global_hooks_path_and_its_hooks_skip_other_repositories(self):
        shared = self.root / "globalhooks"
        with (self.root / "gitconfig").open("a") as stream:
            stream.write(f"[core]\n\thooksPath = {shared}\n")
        refused = self.rmem("guard", "install", "--write", ok=False).stderr
        self.assertIn("comes from your global Git configuration", refused)
        self.assertFalse(shared.exists())
        self.rmem("guard", "install", "--write", "--global-hooks-path")
        self.assertTrue((shared / "post-checkout").is_file())
        other = self.root / "other"
        other.mkdir()
        self.git("init", "-q", "-b", "main", cwd=other)
        (other / "a.txt").write_text("a\n")
        self.git("add", "a.txt", cwd=other)
        self.git("commit", "-q", "-m", "Unrelated", cwd=other)
        self.append({"ts": ago(hours=1), "kind": "exp", "id": "PENDING"})
        elsewhere = subprocess.run(["git", "-C", str(other), "checkout", "-q", "-b", "feature"], capture_output=True,
                                   text=True, env=self.env, timeout=60)
        self.assertEqual(elsewhere.returncode, 0, elsewhere.stderr)
        self.assertNotIn("rmem guard", elsewhere.stderr)
        here = subprocess.run(["git", "-C", str(self.repo), "checkout", "-q", "-b", "feature"], capture_output=True,
                              text=True, env=self.env, timeout=60)
        self.assertIn("rmem guard (post-checkout)", here.stderr)

    def test_guard_refuses_a_tracked_hooks_directory(self):
        (self.repo / ".githooks").mkdir()
        (self.repo / ".githooks/pre-commit").write_text("#!/bin/sh\nexit 0\n")
        self.git("add", ".githooks")
        self.git("commit", "-q", "-m", "Shared hooks")
        self.git("config", "core.hooksPath", ".githooks")
        self.assertIn("a directory Git tracks", self.rmem("guard", "install", "--write", ok=False).stderr)
        self.assertFalse((self.repo / ".githooks/post-checkout").exists())

    def test_guard_respects_core_hooks_path(self):
        self.git("config", "core.hooksPath", ".githooks")
        output = self.rmem("guard", "install", "--write").stdout
        self.assertIn("core.hooksPath", output)
        self.assertTrue((self.repo / ".githooks/pre-rebase").is_file())
        self.assertFalse((self.repo / ".git/hooks/pre-rebase").exists())


if __name__ == "__main__":
    unittest.main()
