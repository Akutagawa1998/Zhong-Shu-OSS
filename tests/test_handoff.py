"""Handoff: requests and their addressing, lessons, pending items, checkpoints, the resume card
and hook installation. Disposable hubs only."""
import datetime as dt
import hashlib
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
SESSION_A = f"claude-{CLAUDE_A}"


class HandoffTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="rmem-handoff-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.hub = self.root / "hub"
        self.base = {k: v for k, v in os.environ.items() if not k.startswith(("RMEM_", "CODEX_", "CLAUDE"))}
        self.base.update(RMEM_HUB=str(self.hub), RMEM_AGENT="codex", PYTHONDONTWRITEBYTECODE="1")
        self.env = {**self.base, "RMEM_SESSION": "owner-a"}
        self.other = {**self.base, "RMEM_SESSION": "other-b"}
        self.third = {**self.base, "RMEM_SESSION": "third-c"}
        self.call("init", self.hub)

    def call(self, *args, ok=True, env=None, stdin=""):
        result = subprocess.run([sys.executable, str(CLI), *map(str, args)], cwd=self.root, env=env or self.env,
                                input=stdin, capture_output=True, text=True, timeout=30)
        if ok:
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout)
        return result

    def events(self):
        return [json.loads(line) for line in (self.hub / "ledger.jsonl").read_text().splitlines()]

    def ledger(self):
        return (self.hub / "ledger.jsonl").read_bytes()

    def write_config(self, **groups):
        path = self.hub / "hub-config.json"
        path.write_text(json.dumps({**json.loads(path.read_text()), **groups}))

    def age(self, predicate, days):
        stamp = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)).isoformat()
        rows = [{**event, "ts": stamp} if predicate(event) else event for event in self.events()]
        (self.hub / "ledger.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows))

    def thread(self, slug, env=None):
        self.call("claim", slug, env=env)
        self.call("release", slug, env=env)

    @staticmethod
    def top(status):
        return status.split("Budget:")[0]

    # Requests

    def test_request_lifecycle_role_addressing_and_status(self):
        self.write_config(roles={"writer": "paper text", "exec": {"note": "jobs", "threads": ["alpha"]}})
        self.call("session", "start", "--role", "writer", env=self.other)
        self.call("request", "--to", "role:writer", "--id", "REQ-1", "--note", "review section 2", "--blocking")
        event = self.events()[-1]
        self.assertEqual((event["kind"], event["to"], event["blocking"], event["session"]),
                         ("request", "role:writer", True, "owner-a"))
        top = self.top(self.call("status", env=self.other).stdout)
        self.assertIn("## Requests for you: 1 open, 1 blocking", top)
        self.assertRegex(top, r"- REQ-1 · open, blocking · open 0\.0 days · to role:writer · from codex / owner-a — review")
        mine = self.top(self.call("status").stdout)
        self.assertNotIn("## Requests for you", mine)
        self.assertIn("Open requests for other addressees: 1 (rmem inbox --all)", mine)
        before = self.ledger()
        self.assertIn("not one of the roles", self.call("request", "--to", "role:nobody", "--id", "REQ-X",
                                                         "--note", "x", ok=False).stderr)
        self.assertIn("--to must be", self.call("request", "--to", "writer", "--id", "REQ-X", "--note", "x", ok=False).stderr)
        self.assertEqual(self.ledger(), before)

        self.call("ack", "REQ-1", "--note", "on it", env=self.other)
        self.assertEqual((self.events()[-1]["kind"], self.events()[-1]["relation"]), ("request_acked", "addressee"))
        before = self.ledger()
        self.assertIn("already acknowledged", self.call("ack", "REQ-1", env=self.other).stdout)
        self.assertEqual(self.ledger(), before)
        self.call("log", "--note", "reviewed section 2", env=self.other)
        done = self.events()[-1]
        self.call("close", "REQ-1", "--by", f"#{done['sequence']}", env=self.other)
        closed = self.events()[-1]
        self.assertEqual((closed["kind"], closed["status"], closed["by_event"], closed["relation"]),
                         ("request_closed", "done", done["event_id"], "addressee"))
        self.assertNotIn("Requests for you", self.top(self.call("status", env=self.other).stdout))
        history = json.loads(self.call("show", "REQ-1", "--section", "history").stdout)["history"]
        self.assertEqual([h["kind"] for h in history], ["request", "request_acked", "request_closed"])
        self.assertIn("already closed (done)", self.call("close", "REQ-1", "--reason", "again", ok=False).stderr)
        self.call("ack", "REQ-1", ok=False)
        self.assertIn("already exists", self.call("request", "--to", "role:writer", "--id", "REQ-1", "--note", "again",
                                                  ok=False).stderr)
        self.call("log", "--id", "E7", "--note", "an experiment")
        self.assertIn("already used by a exp event", self.call("request", "--to", "role:writer", "--id", "E7",
                                                               "--note", "x", ok=False).stderr)
        self.call("request", "--to", "session:other-b", "--id", "REQ-2", "--note", "rerun E7")
        self.call("close", "REQ-2", "--by", done["event_id"][:10], "--status", "rejected", "--reason", "not needed",
                  env=self.other)
        self.assertEqual((self.events()[-1]["status"], self.events()[-1]["note"]), ("rejected", "not needed"))
        self.call("request", "--to", "session:other-b", "--id", "REQ-3", "--note", "x")
        self.assertIn("--by EVENT", self.call("close", "REQ-3", ok=False).stderr)
        self.assertIn("matches no ledger event", self.call("close", "REQ-3", "--by", "#999", ok=False).stderr)
        self.assertIn("rmem show ID", self.call("inbox", env=self.other).stdout)
        recent = self.call("recent", "--grep", "REQ-1").stdout
        self.assertIn("to=role:writer", recent)
        self.assertEqual(self.call("check").stdout.splitlines()[-1], "ok")

    def test_requests_are_written_into_a_thread_another_session_leases(self):
        self.call("claim", "alpha")
        self.call("log", "--thread", "alpha", "--note", "blocked by the lease", env=self.other, ok=False)
        self.call("request", "--to", "thread:alpha", "--id", "REQ-A", "--note", "please rerun E3", env=self.other)
        event = self.events()[-1]
        self.assertEqual((event["thread"], event["to"], event["session"]), ("alpha", "thread:alpha", "other-b"))
        top = self.top(self.call("status").stdout)
        self.assertIn("## Requests for you: 1 open", top)
        self.assertIn("REQ-A", top)
        self.assertIn("request | id=REQ-A | thread=alpha | to=thread:alpha", self.call("recent", "--thread", "alpha").stdout)
        self.call("ack", "REQ-A")
        self.assertEqual((self.events()[-1]["thread"], self.events()[-1]["relation"]), ("alpha", "addressee"))
        closed = self.call("close", "REQ-A", "--reason", "covered by E4", env=self.third)
        self.assertIn("relation=other", closed.stderr)
        self.assertEqual(self.events()[-1]["relation"], "other")
        before = self.ledger()
        missing = self.call("request", "--to", "thread:alpah", "--id", "REQ-B", "--note", "typo", ok=False)
        self.assertIn("thread 'alpah' does not exist", missing.stderr)
        self.call("request", "--to", "session:owner-a", "--thread", "nothere", "--id", "REQ-B", "--note", "x", ok=False)
        self.assertEqual(self.ledger(), before)
        self.call("check")

    def test_a_thread_request_reaches_sessions_that_work_there_without_a_lease(self):
        self.thread("alpha")
        self.call("log", "--thread", "alpha", "--note", "works on alpha without a lease", env=self.other)
        self.call("request", "--to", "thread:alpha", "--id", "REQ-1", "--note", "rerun E3 with seed 2")
        self.assertIn("## Requests for you: 1 open", self.top(self.call("status", env=self.other).stdout))
        self.assertIn("- REQ-1 ·", self.call("inbox", env=self.other).stdout)
        self.assertIn("REQ-1", self.call("resume", env=self.other).stdout)
        # The sender filed it for whoever works on alpha; it is not the sender's own task.
        self.assertNotIn("## Requests for you", self.top(self.call("status").stdout))
        # A session started with --parent sees requests to its parent in status and inbox, as in resume.
        child = {**self.base, "RMEM_SESSION": "child-b"}
        self.call("session", "start", "--parent", "other-b", env=child)
        self.call("request", "--to", "session:other-b", "--id", "REQ-2", "--note", "for the parent")
        self.assertIn("- REQ-2 ·", self.call("inbox", env=child).stdout)
        self.assertIn("REQ-2", self.top(self.call("status", env=child).stdout).split("Open requests for other")[0])

    def test_a_request_that_carries_a_user_decision_must_cite_the_user(self):
        before = self.ledger()
        refused = self.call("request", "--to", "session:other-b", "--id", "REQ-U", "--note", "user wants E3 first",
                            "--user-decision", ok=False)
        self.assertIn("--quote", refused.stderr)
        self.assertIn("not a user decision", refused.stderr)
        self.call("request", "--to", "session:other-b", "--id", "REQ-U", "--note", "x", "--user-decision",
                  "--source", "deadbeefdeadbeef", ok=False)
        self.assertEqual(self.ledger(), before)
        self.call("log", "--kind", "decision", "--note", "the user: run E3 before anything else")
        decision = self.events()[-1]
        self.call("request", "--to", "session:other-b", "--id", "REQ-U", "--note", "run E3 first", "--user-decision",
                  "--source", "#1")
        self.assertEqual((self.events()[-1]["source_event"], self.events()[-1]["user_decision"]),
                         (decision["event_id"], True))
        self.call("request", "--to", "session:other-b", "--id", "REQ-V", "--note", "then E4", "--user-decision",
                  "--quote", "E4 after E3, same seed")
        self.assertEqual(self.events()[-1]["quote"], "E4 after E3, same seed")
        inbox = self.call("inbox", "--for", "session:other-b").stdout
        self.assertIn("quote \"E4 after E3, same seed\"; carries a user decision", inbox)
        self.assertIn(f"cites event {decision['event_id']}", inbox)

    def test_check_warns_about_requests_not_acknowledged_in_time(self):
        self.write_config(requests={"ack_warn_days": 1})
        self.call("request", "--to", "session:other-b", "--id", "REQ-OLD", "--note", "old")
        self.call("request", "--to", "session:other-b", "--id", "REQ-NEW", "--note", "new")
        self.age(lambda event: event.get("id") == "REQ-OLD", 2.5)
        check = self.call("check").stdout
        self.assertIn("warn: research_handoff: request REQ-OLD to session:other-b has waited 2.5 days without an ack "
                      "(requests.ack_warn_days is 1)", check)
        self.assertNotIn("REQ-NEW", check)
        self.assertEqual(check.splitlines()[-1], "ok")
        self.assertIn("open 2.5 days", self.call("inbox", env=self.other).stdout)
        self.call("ack", "REQ-OLD", env=self.other)
        self.assertNotIn("REQ-OLD", self.call("check").stdout)

    def test_inbox_filters_by_session_role_thread_and_all(self):
        self.write_config(roles={"writer": {}, "exec": {"threads": ["alpha"]}})
        self.thread("alpha")
        self.call("session", "start", "--role", "writer", env=self.other)
        self.call("session", "start", "--role", "exec", env=self.third)
        for ident, to in (("R1", "role:writer"), ("R2", "session:other-b"), ("R3", "thread:alpha"), ("R4", "session:third-c")):
            self.call("request", "--to", to, "--id", ident, "--note", f"note {ident}")

        def listed(result):
            return [ident for ident in ("R1", "R2", "R3", "R4") if f"- {ident} ·" in result.stdout]

        writer = self.call("inbox", env=self.other)
        self.assertEqual(listed(writer), ["R1", "R2"])
        self.assertIn("# Open requests for session other-b; role writer: 2", writer.stdout)
        self.assertEqual(listed(self.call("inbox", env=self.third)), ["R3", "R4"])
        self.assertEqual(listed(self.call("inbox", "--for", "role:writer")), ["R1"])
        self.assertEqual(listed(self.call("inbox", "--for", "thread:alpha")), ["R3"])
        self.assertEqual(listed(self.call("inbox", "--all")), ["R1", "R2", "R3", "R4"])
        self.assertIn("no session", self.call("inbox", env=self.base, ok=False).stderr)
        self.assertEqual(listed(self.call("inbox", "--all", env=self.base)), ["R1", "R2", "R3", "R4"])
        self.call("inbox", "--for", "writer", ok=False)
        self.assertIn("[1 more not shown; raise -n]", self.call("inbox", "-n", "1", env=self.other).stdout)
        self.call("close", "R1", "--reason", "done", env=self.other)
        self.assertEqual(listed(self.call("inbox", env=self.other)), ["R2"])
        status = self.top(self.call("status", env=self.base).stdout)
        self.assertIn("## Open requests in this hub: 3 (rmem inbox --all", status)

    # Lessons

    def test_lessons_add_list_export_supersede_and_check(self):
        self.call("lesson", "add", "--id", "L1", "--rule", "One commit per ledger event", "--why", "history stays bisectable",
                  "--quote", "commit each event separately")
        path = self.hub / "lessons/L1.md"
        text = path.read_text()
        for expected in ("# Lesson L1", "Rule: One commit per ledger event", "Why: history stays bisectable",
                         "Applies to: all", "The user's words: \"commit each event separately\""):
            self.assertIn(expected, text)
        event = self.events()[-1]
        self.assertEqual((event["kind"], event["applies_to"], event["lesson_path"]), ("lesson", "all", "lessons/L1.md"))
        self.assertEqual(event["lesson_sha256"], hashlib.sha256(path.read_bytes()).hexdigest())
        self.thread("alpha")
        self.call("lesson", "add", "--id", "L2", "--rule", "Report times in local time", "--why", "the user reads PDT",
                  "--applies-to", "thread:alpha")
        self.call("lesson", "add", "--id", "L3", "--rule", "Read both regimes", "--why", "one regime misled E4",
                  "--applies-to", "role:writer")
        self.call("lesson", "add", "--id", "L9", "--rule", "x", "--why", "y", "--applies-to", "session:x", ok=False)

        def ids(result):
            return [ident for ident in ("L1", "L2", "L3", "L4") if f"- {ident} (" in result.stdout]

        self.assertEqual(ids(self.call("lesson", "list")), ["L1", "L2", "L3"])
        self.assertEqual(ids(self.call("lesson", "list", "--applies-to", "thread:alpha")), ["L1", "L2"])
        self.assertEqual(ids(self.call("lesson", "list", "--applies-to", "all")), ["L1"])
        self.call("lesson", "add", "--id", "L4", "--rule", "One commit per ledger event, with its real time",
                  "--why", "time order", "--supersedes", "L1")
        self.assertEqual(ids(self.call("lesson", "list")), ["L2", "L3", "L4"])
        listed = self.call("lesson", "list", "--all").stdout
        self.assertIn("- L1 (applies to all, superseded by L4)", listed)
        self.assertTrue(path.exists())
        self.assertIn("already superseded by L4", self.call("lesson", "add", "--id", "L5", "--rule", "r", "--why", "w",
                                                            "--supersedes", "L1", ok=False).stderr)
        self.call("lesson", "add", "--id", "L5", "--rule", "r", "--why", "w", "--supersedes", "L0", ok=False)
        self.call("lesson", "add", "--id", "L2", "--rule", "r", "--why", "w", ok=False)
        self.assertFalse((self.hub / "lessons/L5.md").exists())

        files = sorted(str(p.relative_to(self.hub)) for p in self.hub.rglob("*"))
        before = self.ledger()
        memory = self.call("lesson", "export", "--format", "claude-memory").stdout
        self.assertTrue(memory.startswith("---\nname: "))
        for expected in ("type: feedback", "- One commit per ledger event, with its real time", "**Why:** time order",
                         "**How to apply:** in every session on this project (rmem lesson L4).",
                         "**How to apply:** in work on thread alpha (rmem lesson L2)."):
            self.assertIn(expected, memory)
        self.assertNotIn("bisectable", memory)
        agents = self.call("lesson", "export", "--format", "codex-agents", "--applies-to", "thread:alpha").stdout
        self.assertIn("## Project lessons (rmem, ", agents)
        self.assertIn("- Report times in local time Why: the user reads PDT (Applies to work on thread alpha; lesson L2.)", agents)
        self.assertNotIn("Read both regimes", agents)
        self.assertEqual(sorted(str(p.relative_to(self.hub)) for p in self.hub.rglob("*")), files)
        self.assertEqual(self.ledger(), before)

        for number in range(6, 11):
            self.call("lesson", "add", "--id", f"L{number}", "--rule", f"rule {number}", "--why", "w")
        top = self.top(self.call("status").stdout)
        section = top.split("## Lessons that apply: 6 (rmem lesson list)")[1]
        self.assertEqual(sum(1 for line in section.splitlines() if line.startswith("- L")), 5)
        self.assertIn("[1 older lessons not shown: rmem lesson list]", section)
        self.assertNotIn("L2 (applies", top)  # thread:alpha is not leased by this session
        self.assertEqual(self.call("check").stdout.splitlines()[-1], "ok")
        for args in (("init", "-q"), ("config", "user.name", "Rmem test"), ("config", "user.email", "rmem@example.test")):
            subprocess.run(["git", "-C", str(self.hub), *args], check=True, capture_output=True, timeout=10)
        self.call("sync", "-m", "memory with lessons")
        tracked = subprocess.run(["git", "-C", str(self.hub), "ls-files"], capture_output=True, text=True,
                                 timeout=10).stdout.splitlines()
        self.assertIn("lessons/L1.md", tracked)
        (self.hub / "lessons/L2.md").write_text("edited by hand\n")
        (self.hub / "lessons/L3.md").unlink()
        check = self.call("check").stdout
        self.assertIn("2 lesson file(s) differ from their ledger events: L2 (changed), L3 (missing)", check)
        self.assertEqual(check.splitlines()[-1], "ok")

    # Pending items and checkpoints

    def test_pending_items_and_checkpoints(self):
        self.call("pending", "add", "--id", "P1", "--note", "gave the user options A, B and C")
        self.assertIn("already open", self.call("pending", "add", "--id", "P1", "--note", "again", ok=False).stderr)
        self.assertIn("- P1 · open 0.0 days · session owner-a — gave the user options", self.call("pending", "list").stdout)
        self.assertIn("## Pending (waiting for an answer): 1", self.call("resume").stdout)
        self.call("pending", "close", "P1", "--note", "the user chose B", "--quote", "B, and keep seed 2")
        event = self.events()[-1]
        self.assertEqual((event["kind"], event["quote"], event["relation"]), ("pending_closed", "B, and keep seed 2", "owner"))
        self.assertNotIn("## Pending", self.call("resume").stdout)
        self.assertIn("# 0 open pending item(s)", self.call("pending", "list").stdout)
        self.call("pending", "close", "P1", "--note", "again", ok=False)
        self.call("pending", "add", "--id", "P1", "--note", "a new question with the same ID")

        self.call("checkpoint", "--note", "next step: run E4 with seed 2")
        self.assertEqual((self.events()[-1]["kind"], self.events()[-1]["note"]), ("checkpoint", "next step: run E4 with seed 2"))
        self.call("checkpoint", ok=False)
        self.call("claim", "alpha", env=self.other)
        self.call("checkpoint", "--thread", "alpha", "--note", "x", ok=False)

        claude = {**self.base, "CLAUDECODE": "1", "CLAUDE_CODE_SESSION_ID": CLAUDE_A}
        before = self.ledger()
        skipped = self.call("checkpoint", "--auto", env=claude)
        self.assertIn(f"automatic checkpoint skipped: session {SESSION_A} has not written to this hub", skipped.stderr)
        self.assertEqual(self.ledger(), before)
        self.call("log", "--note", "the conversation uses the hub", env=claude)
        hook = json.dumps({"session_id": CLAUDE_A, "hook_event_name": "PreCompact", "trigger": "auto"})
        self.call("checkpoint", "--auto", env=self.base, stdin=hook)
        event = self.events()[-1]
        self.assertEqual((event["kind"], event["session"], event["harness_session"], event["auto"], event["hook_source"]),
                         ("checkpoint", SESSION_A, CLAUDE_A, True, "auto"))
        forked = {**claude, "CLAUDE_CODE_SESSION_ID": "0f1e2d3c-4b5a-4968-8776-655443322110", "RMEM_SESSION": SESSION_A}
        before = self.ledger()
        refused = self.call("checkpoint", "--auto", env=forked)
        self.assertIn("automatic checkpoint skipped", refused.stderr)
        self.assertIn("forked", refused.stderr)
        self.assertEqual(self.ledger(), before)

        self.call("session", "start", "--parent", "owner-a", env={**self.base, "RMEM_SESSION": "child-1"})
        resume = self.call("resume", env={**self.base, "RMEM_SESSION": "child-1"}).stdout
        self.assertIn("parent owner-a", resume)
        self.assertIn("- P1 · open 0.0 days · session owner-a — a new question", resume)
        self.assertIn("next step: run E4 with seed 2", resume)
        self.assertIn("(with parent sessions)", resume)
        self.call("pending", "close", "P1", "--note", "answered", env={**self.base, "RMEM_SESSION": "child-1"})
        self.assertEqual(self.events()[-1]["relation"], "descendant")

    # Resume

    def build_resume_hub(self):
        self.call("claim", "alpha")
        (self.hub / "threads/alpha.md").write_text(
            "# Thread: alpha\n\n## Question\nDoes E3 hold?\n\n## Next step\n1. Run E4 with seed 2.\n"
            "2. Compare with E3.\n\n## Results so far\nnone yet\n")
        (self.hub / "MAP.md").write_text(
            "# MAP — project source authority\n\n| Location | Role / authority | Availability | New |\n|---|---|---|---|\n"
            "| /data | raw data | now | - |\n\n## Authority\n- Controller: /ctl/mode.json is canonical; read it before acting.\n\n"
            "## Rules\n- one canonical hub\n")
        self.call("pending", "add", "--id", "P1", "--note", "options A or B given to the user")
        self.call("checkpoint", "--note", "next step: compare E4 with E3")
        self.call("lesson", "add", "--id", "L1", "--rule", "Read both regimes before concluding", "--why", "E4 was misread")
        self.call("request", "--to", "thread:alpha", "--id", "REQ-A", "--note", "rerun E3 with seed 2", "--blocking",
                  env=self.other)
        self.call("log", "--kind", "decision", "--note", "user: seed 2 is the default", env=self.other)

    def test_resume_card_content_order_and_read_only(self):
        self.build_resume_hub()
        before = self.ledger()
        card = self.call("resume").stdout
        self.assertEqual(self.ledger(), before)
        self.assertLessEqual(len(card), 6000)
        order = ["# rmem resume: session owner-a (codex)", "## Identity", "## Requests for you: 1 open, 1 blocking",
                 "REQ-A", "## Pending (waiting for an answer): 1", "## Latest checkpoints",
                 "next step: compare E4 with E3", "## Leases held: 1", "- alpha: live, expires",
                 "## Next step: alpha (threads/alpha.md)", "  1. Run E4 with seed 2.", "  2. Compare with E3.",
                 "## Decisions by other sessions since your last event", "user: seed 2 is the default",
                 "## Authority (MAP.md; read the entries you need)", "- Controller: /ctl/mode.json is canonical",
                 "## Lessons that apply: 1", "- L1 (applies to all): Read both regimes",
                 "## Your last", "Next: answer blocking requests"]
        positions = [card.index(text) for text in order]
        self.assertEqual(positions, sorted(positions), card)
        self.assertNotIn("Results so far", card)
        self.assertNotIn("one canonical hub", card)
        self.assertNotIn("raw data", card)

        self.thread("beta", env=self.other)
        focused = self.call("resume", "--thread", "beta").stdout
        self.assertIn("## Next step: beta (threads/beta.md)", focused)
        self.assertIn("## Thread beta: last 2 of 2 events", focused)
        self.call("resume", "--thread", "gamma", ok=False)
        fresh = self.call("resume", env={**self.base, "RMEM_SESSION": "fresh-1"}).stdout
        self.assertIn("This session has no events yet", fresh)
        self.assertRegex(fresh, r"- other-b: \d+ events, last activity")
        self.assertIn("no session: pass --session", self.call("resume", env=self.base).stdout)

    def test_resume_stays_within_its_budget(self):
        self.build_resume_hub()
        for number in range(30):
            self.call("log", "--note", f"filler event {number} " + "x" * 300)
        card = self.call("resume", "--max-chars", "1000", "-n", "30").stdout
        self.assertLessEqual(len(card), 1000)
        self.assertIn("[resume card cut at its character budget", card)
        self.assertIn("## Requests for you", card)
        self.write_config(resume={"max_chars": 1500, "recent_events": 30})
        card = self.call("resume").stdout
        self.assertLessEqual(len(card), 1500)
        self.assertGreater(len(card), 1000)
        self.call("resume", "--max-chars", "999", ok=False)
        self.write_config(resume={"max_chars": 10})
        self.assertIn("resume.max_chars", self.call("resume", ok=False).stderr)

    def test_resume_hook_mode_reads_the_hook_input_and_never_fails(self):
        self.build_resume_hub()
        unlabeled = {k: v for k, v in self.base.items() if k != "RMEM_AGENT"}
        self.call("log", "--note", "this conversation", env={**unlabeled, "CLAUDE_CODE_SESSION_ID": CLAUDE_A})
        payload = json.dumps({"session_id": CLAUDE_A, "hook_event_name": "SessionStart", "source": "compact"})
        card = self.call("resume", "--hook", env=unlabeled, stdin=payload).stdout
        self.assertIn(f"# rmem resume: session {SESSION_A} (claude)", card)
        self.assertIn("this conversation", card)
        self.assertIn("Printed by a Claude Code session-start hook (compact).", card)
        broken = self.call("resume", "--hook", env={**self.base, "RMEM_HUB": str(self.root / "missing")})
        self.assertIn("rmem resume could not run", broken.stdout)
        self.call("resume", env={**self.base, "RMEM_HUB": str(self.root / "missing")}, ok=False)

    def test_hooks_follow_an_explicit_session_of_the_same_conversation(self):
        conversation = "33333333-3333-4333-8333-333333333333"
        claude = {**{k: v for k, v in self.base.items() if k != "RMEM_AGENT"}, "CLAUDECODE": "1",
                  "CLAUDE_CODE_SESSION_ID": conversation}
        explicit = ("--agent", "claude", "--session", "claude-writer-01")
        self.call("claim", "writing", *explicit, env=claude)
        self.call("pending", "add", "--id", "P-opts", "--note", "options A or B", *explicit, env=claude)
        self.call("checkpoint", "--note", "next step: draft section 3", *explicit, env=claude)
        hook = json.dumps({"session_id": conversation, "hook_event_name": "PreCompact", "trigger": "auto"})
        self.call("checkpoint", "--auto", env=self.base, stdin=hook)
        event = self.events()[-1]
        self.assertEqual((event["kind"], event["session"], event["agent"], event["auto"]),
                         ("checkpoint", "claude-writer-01", "claude", True))
        payload = json.dumps({"session_id": conversation, "hook_event_name": "SessionStart", "source": "compact"})
        card = self.call("resume", "--hook", env=self.base, stdin=payload).stdout
        self.assertIn("# rmem resume: session claude-writer-01 (claude)", card)
        self.assertIn("this conversation writes as session claude-writer-01", card)
        self.assertIn("P-opts", card)
        self.assertIn("next step: draft section 3", card)
        self.assertIn("- writing: live", card)
        self.assertNotIn("This session has no events yet", card)
        # The conversation's own later writes under the explicit session still own the lease.
        self.call("log", "--thread", "writing", "--note", "after compaction", *explicit, env=claude)

    # Hooks

    def test_hooks_install_dry_run_write_merge_and_backup(self):
        project = self.root / "project"
        settings = project / ".claude/settings.local.json"
        project.mkdir()
        dry = self.call("hooks", "install", "--claude", "--project", project)
        self.assertFalse((project / ".claude").exists())
        self.assertIn("Dry run: 5 hook group(s)", dry.stdout)
        for text in ('"matcher": "startup"', '"matcher": "resume"', '"matcher": "compact"', '"matcher": "auto"',
                     f"{self.hub}/bin/rmem resume --hook", f"{self.hub}/bin/rmem checkpoint --auto", "Nothing was written"):
            self.assertIn(text, dry.stdout)
        settings.parent.mkdir()
        original = {"permissions": {"allow": ["Bash(ls:*)"]},
                    "hooks": {"SessionStart": [{"matcher": "startup", "hooks": [{"type": "command", "command": "echo hi"}]}]}}
        settings.write_text(json.dumps(original))
        before = settings.read_bytes()
        self.call("hooks", "install", "--claude", "--project", project, "--write")
        merged = json.loads(settings.read_text())
        self.assertEqual(merged["permissions"], original["permissions"])
        self.assertEqual([g["matcher"] for g in merged["hooks"]["SessionStart"]], ["startup", "startup", "resume", "compact"])
        self.assertEqual([g["matcher"] for g in merged["hooks"]["PreCompact"]], ["auto", "manual"])
        self.assertEqual(merged["hooks"]["SessionStart"][0]["hooks"][0]["command"], "echo hi")
        backups = sorted(settings.parent.glob("settings.local.json.rmem-backup-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), before)
        again = self.call("hooks", "install", "--claude", "--project", project, "--write")
        self.assertIn("already installed", again.stdout)
        self.assertEqual(len(list(settings.parent.glob("settings.local.json.rmem-backup-*"))), 1)

        command = merged["hooks"]["SessionStart"][3]["hooks"][0]["command"]
        payload = json.dumps({"session_id": CLAUDE_A, "hook_event_name": "SessionStart", "source": "compact"})
        ran = subprocess.run(command, shell=True, cwd=project, env=self.base, input=payload, capture_output=True,
                             text=True, timeout=30)
        self.assertEqual(ran.returncode, 0, ran.stderr)
        self.assertIn(f"# rmem resume: session {SESSION_A}", ran.stdout)

        settings.write_text("{not json")
        refused = self.call("hooks", "install", "--claude", "--project", project, "--write", ok=False)
        self.assertIn("is not valid JSON", refused.stderr)
        self.assertEqual(settings.read_text(), "{not json")
        settings.write_text(json.dumps({"hooks": {"PreCompact": {"matcher": "auto"}}}))
        self.assertIn("hooks.PreCompact", self.call("hooks", "install", "--claude", "--project", project, ok=False).stderr)
        self.assertEqual(len(list(settings.parent.glob("settings.local.json.rmem-backup-*"))), 1)
        self.call("hooks", "install", "--claude", "--project", self.hub, ok=False)
        codex = self.call("hooks", "install", "--codex").stdout
        self.assertIn("## Session continuity (rmem)", codex)
        self.assertIn(f"{self.hub}/bin/rmem resume", codex)
        self.call("hooks", "install", ok=False)

    # Compatibility

    def test_init_ships_the_module_and_fields_are_checked_per_kind(self):
        self.assertTrue((self.hub / "bin/research_handoff.py").is_file())
        manifest = json.loads((self.hub / ".kit-manifest.json").read_text())
        self.assertIn("bin/research_handoff.py", manifest["files"])
        self.assertEqual(json.loads((self.hub / "hub-config.json").read_text())["resume"],
                         {"max_chars": 6000, "recent_events": 8})
        with (self.hub / "ledger.jsonl").open("a") as stream:
            stream.write(json.dumps({"ts": "2026-01-01T00:00:00+00:00", "kind": "exp", "to": 3, "rule": ["x"]}) + "\n")
        self.assertEqual(self.call("check").stdout.splitlines()[-1], "ok")
        with (self.hub / "ledger.jsonl").open("a") as stream:
            stream.write(json.dumps({"ts": "2026-01-01T00:00:00+00:00", "kind": "request", "session": "s", "id": "R",
                                     "to": "nowhere"}) + "\n")
        self.assertIn("request to must be thread:SLUG", self.call("check", ok=False).stdout)


if __name__ == "__main__":
    unittest.main()
