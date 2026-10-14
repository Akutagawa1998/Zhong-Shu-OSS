"""Session identity: derivation from the harness, session start/update, fork refusal,
whoami and identity checks. Disposable hubs only."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


KIT = Path(__file__).resolve().parents[1] / "rmem-kit"
CLI = KIT / "bin" / "rmem"
CLAUDE_A = "034dbdbb-9890-4a67-8aef-ead70efd6013"
CLAUDE_B = "0f1e2d3c-4b5a-4968-8776-655443322110"
CODEX_A = "01a0aeb1-7c6d-7e8f-9a0b-1c2d3e4f5a6b"
SESSION_A, SESSION_B, CODEX_SESSION = f"claude-{CLAUDE_A}", f"claude-{CLAUDE_B}", f"codex-{CODEX_A}"


def load_identity():
    if str(KIT / "bin") not in sys.path:
        sys.path.insert(0, str(KIT / "bin"))
    import research_identity
    return research_identity


class IdentityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="rmem-identity-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.hub = self.root / "hub"
        # Scrub the harness variables of whatever runs the tests; each test sets its own.
        self.base = {k: v for k, v in os.environ.items() if not k.startswith(("RMEM_", "CODEX_", "CLAUDE"))}
        self.base.update(RMEM_HUB=str(self.hub), PYTHONDONTWRITEBYTECODE="1")
        self.call("init", self.hub, env=self.base)

    def call(self, *args, ok=True, env=None):
        result = subprocess.run([sys.executable, str(CLI), *map(str, args)], cwd=self.root,
                                env=env or self.base, capture_output=True, text=True, timeout=30)
        if ok:
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout)
        return result

    def claude(self, conversation=CLAUDE_A, **extra):
        return {**self.base, "CLAUDECODE": "1", "CLAUDE_CODE_SESSION_ID": conversation, **extra}

    def events(self):
        return [json.loads(line) for line in (self.hub / "ledger.jsonl").read_text().splitlines()]

    def ledger(self):
        return (self.hub / "ledger.jsonl").read_bytes()

    def test_session_is_derived_from_the_harness_and_flags_still_win(self):
        self.call("log", "--note", "from claude code", env=self.claude())
        event = self.events()[-1]
        self.assertEqual((event["agent"], event["session"]), ("claude", SESSION_A))
        self.assertEqual(event["harness_session"], CLAUDE_A)
        codex = {**self.base, "CODEX_THREAD_ID": CODEX_A}
        self.call("log", "--note", "from codex", env=codex)
        event = self.events()[-1]
        self.assertEqual((event["agent"], event["session"], event["harness_session"]), ("codex", CODEX_SESSION, CODEX_A))
        # Codex launched from inside Claude Code inherits its variables; the Codex ID wins.
        self.call("log", "--note", "nested", env={**self.claude(), "CODEX_THREAD_ID": CODEX_A})
        self.assertEqual(self.events()[-1]["session"], CODEX_SESSION)
        self.call("log", "--note", "explicit", "--session", "chosen-1", env=self.claude())
        self.assertEqual((self.events()[-1]["session"], self.events()[-1]["harness_session"]), ("chosen-1", CLAUDE_A))
        self.call("log", "--note", "env", env=self.claude(RMEM_SESSION="from-env"))
        self.assertEqual(self.events()[-1]["session"], "from-env")
        self.call("claim", "alpha", env=self.claude())
        self.assertEqual(self.events()[-1]["session"], SESSION_A)
        self.call("claim", "beta", ok=False)  # no harness, flag or RMEM_SESSION: no session
        self.call("log", "--note", "a person at a terminal")
        self.assertNotIn("harness_session", self.events()[-1])
        self.assertEqual(self.events()[-1]["agent"], "human")

    def test_effort_is_recorded_and_secret_looking_values_never_are(self):
        self.call("log", "--note", "effort", env=self.claude(CLAUDE_EFFORT="high"))
        self.assertEqual(self.events()[-1]["effort"], "high")
        secret_effort = "sk-ant-api03-abcdefghijklmnop"
        self.call("log", "--note", "bad effort", env=self.claude(CLAUDE_EFFORT=secret_effort))
        self.assertNotIn("effort", self.events()[-1])
        token = "ghp_abcdefghijklmnopqrstuvwxyz0123456789"
        self.call("log", "--note", "bad id", "--session", "named", env={**self.base, "CLAUDE_CODE_SESSION_ID": token})
        self.assertNotIn("harness_session", self.events()[-1])
        self.call("claim", "alpha", env={**self.base, "CLAUDE_CODE_SESSION_ID": token}, ok=False)
        raw = self.ledger().decode()
        self.assertNotIn(secret_effort, raw)
        self.assertNotIn(token, raw)
        identity = load_identity()
        self.assertEqual(identity.derived_session({"CLAUDE_CODE_SESSION_ID": CLAUDE_A}), SESSION_A)
        self.assertIsNone(identity.derived_session({"CLAUDE_CODE_SESSION_ID": "has spaces in it"}))
        self.assertEqual(identity.derived_session({"CODEX_THREAD_ID": "thread-xyz"}), "codex-thread-xyz")
        self.assertEqual(identity.process_fields({"CLAUDE_EFFORT": "token123"}), {})
        rows = [{"kind": "session_started", "session": "s1", "role": "writer"}, {"kind": "exp", "session": "s1", "role": "x"},
                {"kind": "session_updated", "session": "s1", "role": "torch"}, {"kind": "session_started", "session": "s2"}]
        self.assertEqual(identity.session_roles(rows), {"s1": "torch"})

    def test_session_start_and_update_record_role_model_effort_and_parent(self):
        env = self.claude(CLAUDE_EFFORT="high")
        started = self.call("session", "start", "--role", "writer", "--model", "opus", "--transcript",
                            "~/.claude/projects/x/034dbdbb.jsonl", "--parent", "earlier-session", env=env)
        self.assertIn("no roles", started.stderr)
        self.assertIn("parent session earlier-session has no events", started.stderr)
        event = self.events()[-1]
        self.assertEqual(event["kind"], "session_started")
        for name, value in (("role", "writer"), ("model", "opus"), ("effort", "high"), ("harness", "claude"),
                            ("parent_session", "earlier-session"), ("session_source", "derived"),
                            ("harness_session", CLAUDE_A), ("session", SESSION_A)):
            self.assertEqual(event[name], value, name)
        self.call("session", "update", "--model", "fable", "--effort", "max", env=env)
        update = self.events()[-1]
        self.assertEqual((update["kind"], update["model"], update["effort"]), ("session_updated", "fable", "max"))
        rejected = self.call("session", "update", env=env, ok=False)
        self.assertIn("--model, --effort or --role", rejected.stderr)
        self.call("session", "start", "--parent", SESSION_A, env=env, ok=False)
        (self.hub / "hub-config.json").write_text(json.dumps({"roles": {"writer": "paper", "torch": "jobs"}}))
        rejected = self.call("session", "update", "--role", "reviewer", env=env, ok=False)
        self.assertIn("torch, writer", rejected.stderr)
        self.assertNotIn("no roles", self.call("session", "update", "--role", "torch", env=env).stderr)
        self.assertEqual(self.events()[-1]["role"], "torch")
        self.call("check")

    def test_fork_is_refused_and_a_new_session_links_its_parent(self):
        parent = self.claude()
        self.call("session", "start", "--role", "writer", env=parent)
        self.call("claim", "alpha", env=parent)
        before = self.ledger()
        fork = self.claude(CLAUDE_B)
        for args in (("log", "--note", "continued"), ("log", "--thread", "alpha", "--note", "x"),
                     ("claim", "beta"), ("note", "journal line"), ("session", "start"),
                     ("release", "alpha")):
            with self.subTest(command=args[0]):
                rejected = self.call(*args, "--session", SESSION_A, env=fork, ok=False)
                self.assertIn("forked or copied conversation", rejected.stderr)
                self.assertIn(f"session start --parent {SESSION_A}", rejected.stderr)
        self.assertEqual(self.ledger(), before)
        self.assertFalse((self.hub / "claims/beta").exists())
        self.assertEqual(list((self.hub / "journal").glob("*.md")), [])
        status = self.call("status", "--session", SESSION_A, env=fork).stdout
        self.assertIn("Identity warning", status)
        # Humans and scripts carry no harness ID and are not checked.
        self.call("log", "--note", "typed by the user", "--session", SESSION_A, "--agent", "claude")
        # The fork derives its own session and links the parent.
        self.call("session", "start", "--parent", SESSION_A, env=fork)
        self.assertEqual((self.events()[-1]["session"], self.events()[-1]["parent_session"]),
                         (SESSION_B, SESSION_A))
        self.call("log", "--note", "fork continues under its own session", env=fork)
        self.call("check")

    def test_codex_threads_started_in_the_same_minute_get_their_own_sessions(self):
        # UUIDv7: the first 48 bits are a millisecond timestamp, so these two share their first 8 hex digits.
        first, second = "01a085ad-e5a8-7a30-8b59-56229c292205", "01a085ad-e533-7880-a0fc-3ef402fed45d"
        self.call("claim", "t1", env={**self.base, "CODEX_THREAD_ID": first})
        self.call("log", "--note", "second thread", env={**self.base, "CODEX_THREAD_ID": second})
        self.assertEqual([e["session"] for e in self.events()[-2:]], [f"codex-{first}", f"codex-{second}"])
        out = self.call("whoami", env={**self.base, "CODEX_THREAD_ID": second}).stdout
        self.assertIn("leases held: 0", out)
        # A fork message names both full IDs, and a taken derived ID asks for an explicit session.
        rejected = self.call("log", "--note", "x", "--session", f"codex-{first}",
                             env={**self.base, "CODEX_THREAD_ID": second}, ok=False)
        self.assertIn(f"harness ID {first}), but this process runs in {second}", rejected.stderr)
        self.call("log", "--note", "squatter", "--session", f"codex-{second}", "--agent", "codex",
                  env={**self.base, "CODEX_THREAD_ID": first}, ok=False)
        third = "01a085ad-0000-7000-8000-000000000003"
        self.call("log", "--note", "uses another thread's derived ID", "--session", f"codex-{third}",
                  env={**self.base, "CODEX_THREAD_ID": first})
        taken = self.call("log", "--note", "own thread", env={**self.base, "CODEX_THREAD_ID": third}, ok=False)
        self.assertIn("Pass an explicit new --session ID", taken.stderr)
        self.assertNotIn("omit --session", taken.stderr)

    def test_an_existing_full_id_session_is_the_derived_session(self):
        # Earlier hubs named sessions claude-<full conversation ID> by convention.
        self.call("claim", "storage", "--session", SESSION_A, "--agent", "claude", env=self.claude())
        self.call("log", "--thread", "storage", "--note", "continues without --session", env=self.claude())
        self.call("release", "storage", env=self.claude())
        self.assertEqual({e["session"] for e in self.events()}, {SESSION_A})

    def test_subagents_and_inherited_codex_ids_are_warned_about(self):
        child = self.claude(CLAUDE_CODE_CHILD_SESSION="1")
        self.assertIn("looks like a Claude Code subagent", self.call("whoami", env=child).stdout)
        claimed = self.call("claim", "alpha", env=child)
        self.assertIn("looks like a Claude Code subagent", claimed.stderr)
        again = self.call("claim", "alpha", env=child)
        self.assertIn("already held by this session", again.stdout)
        self.assertIn("parallel subagents of this conversation derive the same one", again.stdout)
        self.assertIn("this session may be shared", self.call("status", env=child).stdout)
        explicit = self.call("whoami", "--session", "worker-2", env=child).stdout
        self.assertNotIn("subagent", explicit)
        both = self.call("whoami", env={**self.claude(), "CODEX_THREAD_ID": CODEX_A}).stdout
        self.assertIn("both CODEX_THREAD_ID and CLAUDE_CODE_SESSION_ID are set", both)

    def test_a_session_is_bound_by_its_first_event_without_session_start(self):
        self.call("log", "--note", "first", "--session", "shared-id", env=self.claude())
        rejected = self.call("log", "--note", "second", "--session", "shared-id", env=self.claude(CLAUDE_B), ok=False)
        self.assertIn("its first event", rejected.stderr)
        self.call("log", "--note", "same conversation", "--session", "shared-id", env=self.claude())

    def test_whoami_is_read_only_and_reports_sources_and_records(self):
        env = self.claude(CLAUDE_EFFORT="medium")
        out = self.call("whoami", env=env).stdout
        self.assertIn("agent: claude (from detected harness)", out)
        self.assertIn(f"session: {SESSION_A} (from derived (CLAUDE_CODE_SESSION_ID))", out)
        self.assertIn(f"harness: claude, conversation ID {CLAUDE_A}", out)
        self.assertIn("role: unknown", out)
        self.assertIn("effort: medium (from CLAUDE_EFFORT", out)
        self.assertIn("started: not recorded", out)
        self.call("session", "start", "--role", "writer", "--model", "opus", env=env)
        self.call("claim", "alpha", env=env)
        self.call("claim", "--resource", "shared-doc", env=env)
        before = self.ledger()
        out = self.call("whoami", env=env).stdout
        self.assertEqual(self.ledger(), before)
        self.assertRegex(out, r"model: opus \(recorded \d{4}-\d\d-\d\d \d\d:\d\d")
        self.assertIn("role: writer (recorded", out)
        self.assertIn("fork check: ok", out)
        self.assertIn("leases held: 2", out)
        self.assertIn("- alpha: live", out)
        self.assertIn("- @shared-doc: live", out)
        flagged = self.call("whoami", "--session", SESSION_A, env=self.claude(CLAUDE_B)).stdout
        self.assertIn(f"session: {SESSION_A} (from flag (--session))", flagged)
        self.assertIn("fork check: REFUSED", flagged)
        plain = self.call("whoami", "--agent", "codex", env={**self.base, "RMEM_SESSION": "s-1"}).stdout
        self.assertIn("agent: codex (from flag (--agent))", plain)
        self.assertIn("session: s-1 (from env (RMEM_SESSION))", plain)
        self.assertIn("harness: none detected", plain)
        self.assertIn("fork check: not applicable", plain)

    def test_check_reports_multi_conversation_and_test_sessions(self):
        rows = [{"ts": "2026-10-01T00:00:00+00:00", "kind": "exp", "agent": "claude", "session": "shared",
                 "harness_session": conversation, "event_id": f"{index:032x}", "sequence": index}
                for index, conversation in enumerate((CLAUDE_A, CLAUDE_B), 1)]
        rows.append({"ts": "2026-10-01T00:00:00+00:00", "kind": "exp", "agent": "claude",
                     "session": "test-install", "event_id": f"{3:032x}", "sequence": 3})
        (self.hub / "ledger.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows))
        check = self.call("check").stdout
        self.assertIn("1 session(s) have events from more than one conversation", check)
        self.assertIn("shared", check)
        self.assertIn("info: research_identity: 1 session(s) are named like tests (test-install)", check)

    def test_link_block_explains_session_identity_for_agent_shells(self):
        project = self.root / "project"
        project.mkdir()
        self.call("link", project, "--session", "linker")
        block = (project / "AGENTS.md").read_text()
        self.assertIn("pass `--session ID` on every call", block)
        self.assertIn("omit it inside Claude Code or Codex", block)
        self.assertIn("session start --parent OLD-SESSION", block)
        self.assertNotIn("Use a unique RMEM_SESSION", block)
        self.assertIn("passes its own `--session ID`", block)
        # A block written by an older kit is reported until `link` refreshes it in place.
        agents = project / "AGENTS.md"
        agents.write_text("# Project rules\n\n" + block.replace("run `../hub/bin/rmem resume` before anything else",
                                                              "use a unique RMEM_SESSION"))
        self.assertIn("differs from what this kit writes", self.call("check").stdout)
        self.call("link", project, "--session", "linker")
        self.assertNotIn("differs from what this kit writes", self.call("check").stdout)
        self.assertTrue(agents.read_text().startswith("# Project rules\n\n<!-- RMEM:START -->"))
        self.assertTrue((self.hub / "HANDOFF.md").is_file())
        self.assertTrue((self.hub / "bin/research_identity.py").is_file())
        manifest = json.loads((self.hub / ".kit-manifest.json").read_text())
        self.assertIn("bin/research_identity.py", manifest["files"])
        self.assertIn("HANDOFF.md", manifest["files"])


if __name__ == "__main__":
    unittest.main()
