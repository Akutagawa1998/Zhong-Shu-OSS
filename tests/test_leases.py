"""Leases: TTL and activity, writes without claims, --wait, --takeover, resource leases,
consistency and status text. Disposable hubs only."""
import datetime as dt
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest


KIT = Path(__file__).resolve().parents[1] / "rmem-kit"
CLI = KIT / "bin" / "rmem"
TIME = r"\d{4}-\d\d-\d\d \d\d:\d\d \S+"


class LeaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="rmem-leases-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.hub = self.root / "hub"
        self.env = {k: v for k, v in os.environ.items() if not k.startswith(("RMEM_", "CODEX_", "CLAUDE"))}
        self.env.update(RMEM_HUB=str(self.hub), RMEM_AGENT="codex", RMEM_SESSION="owner-a",
                        PYTHONDONTWRITEBYTECODE="1")
        self.other = {**self.env, "RMEM_SESSION": "other-b"}
        self.call("init", self.hub)

    def command(self, *args):
        return [sys.executable, str(CLI), *map(str, args)]

    def call(self, *args, ok=True, env=None):
        result = subprocess.run(self.command(*args), cwd=self.root, env=env or self.env,
                                capture_output=True, text=True, timeout=30)
        if ok:
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout)
        return result

    def events(self):
        return [json.loads(line) for line in (self.hub / "ledger.jsonl").read_text().splitlines()]

    def ledger(self):
        return (self.hub / "ledger.jsonl").read_bytes()

    def meta(self, key="alpha"):
        return json.loads((self.hub / "claims" / key / "meta.json").read_text())

    def age_session(self, session, hours):
        """Move every event and the claim files of one session into the past (no other field changes)."""
        stamp = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=hours)).isoformat()
        rows = [{**event, "ts": stamp} if event.get("session") == session else event for event in self.events()]
        (self.hub / "ledger.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows))
        for path in (self.hub / "claims").glob("*/meta.json"):
            meta = json.loads(path.read_text())
            if meta.get("session") == session:
                path.write_text(json.dumps({**meta, "ts": stamp}))

    def test_log_and_run_need_no_claim_and_unknown_threads_are_refused(self):
        self.call("claim", "alpha")
        self.call("release", "alpha")
        count = len(self.events())
        self.call("log", "--thread", "alpha", "--id", "E1", "--note", "no claim needed", env=self.other)
        self.call("run", "--thread", "alpha", "--id", "E2", "--", sys.executable, "-c", "print(1)", env=self.other)
        kinds = [event["kind"] for event in self.events()[count:]]
        self.assertEqual(kinds, ["exp", "run_started", "run"])
        before = self.ledger()
        rejected = self.call("log", "--thread", "alpah", "--note", "typo", ok=False)
        self.assertIn("thread 'alpah' does not exist", rejected.stderr)
        self.assertIn("rmem claim alpah", rejected.stderr)
        self.call("run", "--thread", "alpah", "--id", "E3", "--", sys.executable, "-c", "print(1)", ok=False)
        self.assertEqual(self.ledger(), before)
        self.assertFalse((self.hub / "runs/E3").exists())
        # project-state is the protocol's own thread: a fresh hub accepts it without a claim.
        self.assertFalse((self.hub / "threads/project-state.md").exists())
        self.call("log", "--thread", "project-state", "--kind", "decision", "--note", "no claim needed")
        self.call("check")

    def test_live_lease_of_another_session_refuses_its_writes(self):
        self.call("claim", "alpha")
        before = self.ledger()
        rejected = self.call("log", "--thread", "alpha", "--note", "blocked", env=self.other, ok=False)
        self.assertIn("thread 'alpha' belongs to session owner-a (codex) under a live lease", rejected.stderr)
        self.assertRegex(rejected.stderr, f"last activity {TIME}, lease 12h, expires {TIME}")
        self.assertIn("--wait SECONDS", rejected.stderr)
        self.call("run", "--thread", "alpha", "--id", "E1", "--", sys.executable, "-c", "print(1)",
                  env=self.other, ok=False)
        self.call("log", "--thread", "alpha", "--note", "same session, other label",
                  env={**self.env, "RMEM_AGENT": "claude"}, ok=False)
        self.assertEqual(self.ledger(), before)
        self.call("log", "--thread", "alpha", "--note", "the owner writes")
        self.call("log", "--thread", "beta-free", "--note", "unknown", env=self.other, ok=False)

    def test_status_and_check_show_leases_and_expiry_is_a_warning(self):
        self.call("claim", "alpha", "--ttl", "2")
        status = self.call("status").stdout
        self.assertRegex(status, f"- alpha: codex / owner-a since {TIME} · last activity {TIME} · lease 2h · ")
        self.assertEqual(self.meta()["lease_ttl_hours"], 2)
        self.assertEqual(self.events()[-1]["lease_ttl_hours"], 2)
        self.age_session("owner-a", 5)
        status = self.call("status").stdout
        self.assertRegex(status, r"lease 2h \(expired 3\.\d h ago; takeover with --takeover --reason\)")
        check = self.call("check")
        self.assertIn("warn: lease on thread 'alpha' held by codex / owner-a", check.stdout)
        self.assertIn("rmem claim alpha --takeover --reason TEXT", check.stdout)
        self.assertNotIn("older than", check.stdout)
        self.assertEqual(check.stdout.splitlines()[-1], "ok")

    def append_rows(self, *rows):
        count = len(self.events())
        with (self.hub / "ledger.jsonl").open("a") as stream:
            for number, row in enumerate(rows, count + 1):
                stream.write(json.dumps({"agent": "codex", "event_id": f"{number:032x}", "sequence": number, **row}) + "\n")

    def test_events_within_the_ttl_renew_the_lease_but_expiry_is_sticky(self):
        self.call("claim", "alpha", "--ttl", "1")
        self.call("log", "--note", "the owner is active, on no thread at all")
        self.call("log", "--thread", "alpha", "--note", "renewed", env=self.other, ok=False)
        self.age_session("owner-a", 3)
        self.call("log", "--thread", "alpha", "--note", "expired lease allows writes", env=self.other)
        # A later event of the holder must not revive the lease: other-b already writes there.
        self.call("log", "--note", "the owner is back, elsewhere")
        self.call("log", "--thread", "alpha", "--note", "other-b keeps working", env=self.other)
        row = self.call("status", env=self.other).stdout.split("## Threads")[1].splitlines()[1]
        self.assertIn("expired", row)
        # The lapse is sticky, but the holder's later work must stay visible next to it.
        self.assertRegex(row, f"holder last active {TIME} \\(claim again to renew\\)")
        warned = self.call("log", "--thread", "alpha", "--note", "the owner writes to its old thread")
        self.assertIn("your lease on thread 'alpha' expired at", warned.stderr)
        self.assertIn("since then other-b wrote to it", warned.stderr)
        self.assertIn("since then other-b wrote to it", self.call("resume").stdout)
        self.call("claim", "alpha")  # a new claim event starts a new lease
        self.call("log", "--thread", "alpha", "--note", "refused again", env=self.other, ok=False)
        self.call("check")

    def test_the_holders_long_run_keeps_its_lease_live(self):
        self.call("claim", "gamma")
        self.age_session("owner-a", 13)
        started = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=12.5)).isoformat()
        run = {"session": "owner-a", "thread": "gamma", "run_id": "r" * 32, "id": "E9"}
        self.append_rows({**run, "ts": started, "kind": "run_started", "status": "running"})
        rejected = self.call("log", "--thread", "gamma", "--note", "during the run", env=self.other, ok=False)
        self.assertIn("under a live lease", rejected.stderr)
        self.append_rows({**run, "ts": dt.datetime.now(dt.timezone.utc).isoformat(), "kind": "run", "status": "ok",
                          "exit_code": 0, "secs": 45000.0})
        self.call("log", "--thread", "gamma", "--note", "after the run", env=self.other, ok=False)

    def test_a_runner_records_its_own_run_after_another_session_claims(self):
        self.call("claim", "delta")
        self.call("release", "delta")
        started = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=1)).isoformat()
        self.append_rows({"session": "owner-a", "thread": "delta", "run_id": "d" * 32, "id": "E7", "ts": started,
                          "kind": "run_started", "status": "running"})
        claimed = self.call("claim", "delta", env=self.other)
        self.assertIn("unfinished run E7 started by owner-a", claimed.stderr)
        self.call("release", "delta", env=self.other)  # another session's run does not block a release
        self.call("claim", "delta", env=self.other)
        recorded = self.call("log", "--id", "E7", "--thread", "delta", "--status", "keep", "--note", "own outcome")
        self.assertIn("is this session's own run started before that lease", recorded.stderr)
        self.call("log", "--id", "E8", "--thread", "delta", "--note", "other work", ok=False)
        self.call("log", "--id", "E7", "--thread", "delta", "--note", "not the runner",
                  env={**self.env, "RMEM_SESSION": "third-c"}, ok=False)

    def test_short_ttl_expires_in_real_time(self):
        self.call("claim", "alpha", "--ttl", "0.0006")  # about 2.2 seconds
        self.call("log", "--thread", "alpha", "--note", "still live", env=self.other, ok=False)
        time.sleep(2.6)
        self.call("log", "--thread", "alpha", "--note", "expired", env=self.other)

    def test_takeover_rules_and_consistency(self):
        self.call("claim", "alpha")
        live = self.call("claim", "alpha", "--takeover", "--reason", "owner looks idle", env=self.other, ok=False)
        self.assertIn("--takeover applies only to an expired lease", live.stderr)
        self.assertIn("--force --reason", live.stderr)
        self.age_session("owner-a", 30)
        plain = self.call("claim", "alpha", env=self.other, ok=False)
        self.assertIn("take it over with `rmem claim alpha --takeover --reason TEXT`", plain.stderr)
        self.call("claim", "alpha", "--takeover", env=self.other, ok=False)
        self.call("claim", "alpha", "--takeover", "--force", "--reason", "both", env=self.other, ok=False)
        self.call("release", "alpha", "--reason", "no flag", ok=False)
        before = self.ledger()
        self.call("claim", "alpha", "--takeover", "--reason", "Owner idle for a day; no jobs on the thread", env=self.other)
        event = self.events()[-1]
        self.assertTrue(event["takeover"])
        self.assertEqual(event["previous_owner"]["session"], "owner-a")
        self.assertEqual(event["reason"], "Owner idle for a day; no jobs on the thread")
        self.assertIn("previous_last_activity", event)
        self.assertGreater(len(self.ledger()), len(before))
        self.assertEqual(self.meta()["session"], "other-b")
        self.call("check")
        self.call("release", "alpha", ok=False)  # the previous owner no longer holds it
        self.call("release", "alpha", env=self.other)
        self.call("check")

    def test_takeover_is_refused_while_a_run_is_unfinished(self):
        self.call("claim", "alpha")
        started = {"ts": dt.datetime.now(dt.timezone.utc).isoformat(), "kind": "run_started", "agent": "codex",
                   "session": "owner-a", "thread": "alpha", "run_id": "r1", "id": "E1", "status": "running"}
        with (self.hub / "ledger.jsonl").open("a") as stream:
            stream.write(json.dumps(started) + "\n")
        self.age_session("owner-a", 30)
        rejected = self.call("claim", "alpha", "--takeover", "--reason", "idle", env=self.other, ok=False)
        self.assertIn("unfinished run", rejected.stderr)

    def test_same_owner_reclaim_keeps_or_renews_the_lease(self):
        self.call("claim", "alpha")
        before = self.ledger()
        self.assertIn("already held by this session", self.call("claim", "alpha").stdout)
        self.assertEqual(self.ledger(), before)
        self.call("claim", "alpha", "--ttl", "3")
        self.assertEqual((self.events()[-1]["kind"], self.events()[-1]["lease_ttl_hours"]), ("claim", 3))
        self.assertEqual(self.meta()["claim_event_id"], self.events()[-1]["event_id"])
        self.age_session("owner-a", 10)
        self.call("claim", "alpha")  # an expired lease of the same owner is renewed
        self.assertEqual(self.events()[-1]["lease_ttl_hours"], 3)
        self.call("check")

    def test_wait_times_out_with_the_holder_and_last_activity(self):
        self.call("claim", "alpha")
        start = time.monotonic()
        rejected = self.call("claim", "alpha", "--wait", "1", env=self.other, ok=False)
        self.assertGreaterEqual(time.monotonic() - start, 1)
        self.assertIn("is held by codex / owner-a (last activity", rejected.stderr)
        self.assertIn("after waiting 1 s", rejected.stderr)
        self.call("claim", "alpha", "--wait", "-1", env=self.other, ok=False)
        self.call("claim", "alpha", "--wait", "5", "--force", "--reason", "x", env=self.other, ok=False)

    def test_wait_acquires_after_release_without_holding_the_hub_lock(self):
        self.call("claim", "alpha")
        waiter = subprocess.Popen(self.command("claim", "alpha", "--wait", "20"), cwd=self.root, env=self.other,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            time.sleep(1.2)
            self.assertIsNone(waiter.poll())
            # The waiter polls claims/ without the hub lock, so the owner can still write.
            self.call("log", "--thread", "alpha", "--note", "owner finishes")
            self.call("release", "alpha")
            out, err = waiter.communicate(timeout=10)
        finally:
            if waiter.poll() is None:
                waiter.kill()
                waiter.communicate()
        self.assertEqual(waiter.returncode, 0, err)
        self.assertIn("claimed thread 'alpha' for session other-b", out)
        self.assertEqual(self.meta()["session"], "other-b")
        self.call("check")

    def test_wait_with_takeover_acquires_when_the_lease_expires(self):
        self.call("claim", "alpha", "--ttl", "0.0006")
        start = time.monotonic()
        took = self.call("claim", "alpha", "--wait", "15", "--takeover", "--reason", "waited for expiry", env=self.other)
        self.assertLess(time.monotonic() - start, 12)
        self.assertIn("took over thread 'alpha'", took.stdout)
        self.assertTrue(self.events()[-1]["takeover"])
        self.call("check")
        self.call("claim", "beta", "--ttl", "0.0006")
        start = time.monotonic()
        expired = self.call("claim", "beta", "--wait", "15", env=self.other, ok=False)
        self.assertLess(time.monotonic() - start, 12)
        self.assertIn("--takeover --reason", expired.stderr)

    def test_resource_leases_are_separate_from_threads_and_checked(self):
        claimed = self.call("claim", "--resource", "alice-guide.s8", "--note", "editing section 8")
        self.assertIn("claimed resource 'alice-guide.s8'", claimed.stdout)
        event = self.events()[-1]
        self.assertEqual((event["kind"], event["resource"]), ("claim", "alice-guide.s8"))
        self.assertNotIn("thread", event)
        self.assertTrue((self.hub / "claims/@alice-guide.s8/meta.json").is_file())
        self.assertFalse((self.hub / "threads/alice-guide.s8.md").exists())
        self.call("claim", "alice-guide.s8", env=self.other)  # a thread of the same name is another key
        status = self.call("status").stdout
        self.assertIn("## Resource leases (outside the hub)", status)
        self.assertRegex(status, f"- @alice-guide.s8: codex / owner-a since {TIME} · last activity")
        rejected = self.call("claim", "--resource", "alice-guide.s8", env=self.other, ok=False)
        self.assertIn("resource 'alice-guide.s8' is held by codex / owner-a", rejected.stderr)
        self.assertIn("rmem claim --resource alice-guide.s8 --wait SECONDS", rejected.stderr)
        self.call("release", "--resource", "alice-guide.s8", env=self.other, ok=False)
        self.call("check")
        self.call("claim", "alpha", "--resource", "x", ok=False)
        self.call("claim", ok=False)
        self.call("claim", "--resource", "../escape", ok=False)
        (self.hub / "claims/@alice-guide.s8/meta.json").unlink()
        (self.hub / "claims/@alice-guide.s8").rmdir()
        broken = self.call("check", ok=False).stdout
        self.assertIn("claim/ledger '@alice-guide.s8': missing claim file", broken)
        self.call("log", "--note", "blocked until recovery", ok=False)
        self.call("release", "--resource", "alice-guide.s8", "--force", "--reason", "Lost claim file; checked the document")
        self.call("check")
        self.call("release", "alice-guide.s8", env=self.other)
        self.call("check")

    def test_highlight_follows_the_project_state_lease_rule(self):
        self.call("claim", "project-state")
        self.call("release", "project-state")
        spec = self.root / "spec.json"
        spec.write_text("{}")
        rejected = self.call("highlight", "create", spec, env=self.other, ok=False)
        self.assertNotIn("live lease", rejected.stderr)  # passes authorization, then the empty spec is rejected
        self.call("claim", "project-state")
        rejected = self.call("highlight", "create", spec, env=self.other, ok=False)
        self.assertIn("belongs to session owner-a (codex) under a live lease", rejected.stderr)


if __name__ == "__main__":
    unittest.main()
