"""Bounded queries (0.4.0 track B): recent filters and paging, show --field pages,
catalog --regex and repeated --query, export. Disposable hubs only."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


CLI = Path(__file__).resolve().parents[1] / "rmem-kit" / "bin" / "rmem"


class QueryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="rmem-queries-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.hub = self.root / "hub"
        self.env = {k: v for k, v in os.environ.items() if not k.startswith(("RMEM_", "CODEX_", "CLAUDE"))}
        self.env.update(RMEM_HUB=str(self.hub), RMEM_SESSION="session-a", RMEM_AGENT="codex",
                        RMEM_TZ="America/Los_Angeles", PYTHONDONTWRITEBYTECODE="1")
        self.call("init", self.hub, "--project-name", "Query test")

    def call(self, *args, ok=True, env=None):
        result = subprocess.run([sys.executable, str(CLI), *map(str, args)], cwd=self.root,
                                env=env or self.env, capture_output=True, text=True, timeout=30)
        if ok:
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout)
        return result

    def view(self, *args):
        return json.loads(self.call(*args).stdout)

    def append(self, *events):
        with (self.hub / "ledger.jsonl").open("a") as stream:
            for event in events:
                stream.write(json.dumps(event) + "\n")

    def events(self):
        return [json.loads(line) for line in (self.hub / "ledger.jsonl").read_text().splitlines()]

    def lines(self, *args, **options):
        return self.call("recent", *args, **options).stdout.splitlines()

    # recent

    def test_since_and_until_agree_for_local_time_offset_and_utc(self):
        self.append(*({"ts": f"2026-10-14T{hour:02d}:00:00+00:00", "kind": "exp", "id": f"E{hour}",
                       "note": f"at {hour} UTC"} for hour in (8, 9, 10)))
        local = self.lines("--since", "2026-10-14 02:00")
        self.assertEqual(local, self.lines("--since", "2026-10-14T09:00:00Z"))
        self.assertEqual(local, self.lines("--since", "2026-10-14T02:00-07:00"))
        self.assertEqual([line.split(" | ")[2] for line in local], ["exp", "exp"])
        self.assertIn("id=E9", local[0])
        self.assertIn("id=E10", local[1])
        until = self.lines("--until", "2026-10-14 02:00")
        self.assertEqual([("id=E8" in until[0]), ("id=E9" in until[1]), len(until)], [True, True, 2])
        self.assertIn("2026-10-14 02:00 PDT #2", local[0])
        self.call("log", "--note", "just now")
        recent = self.lines("--since", "1h")
        self.assertEqual(len(recent), 1)
        self.assertIn("just now", recent[0])
        self.assertIn("--since must not be after --until",
                      self.call("recent", "--since", "3d", "--until", "4d", ok=False).stderr)
        self.assertIn("cannot read the time", self.call("recent", "--since", "yesterday", ok=False).stderr)

    def test_after_seq_pages_forward_and_reports_the_next_page(self):
        for number in range(1, 6):
            self.call("log", "--note", f"event {number}")
        first = self.lines("--after-seq", "2", "-n", "2")
        self.assertEqual(len(first), 3)
        self.assertIn("#3 |", first[0])
        self.assertIn("#4 |", first[1])
        self.assertEqual(first[2], "[1 more matching events; next page: --after-seq 4]")
        last = self.lines("--after-seq", "4", "-n", "2")
        self.assertEqual(len(last), 1)
        self.assertIn("#5 |", last[0])
        self.assertEqual(self.lines("--after-seq", "5"), ["[no matching events after #5]"])
        newest = self.lines("-n", "2")
        self.assertIn("#4 |", newest[0])
        self.assertEqual(newest[2], "[showing the newest 2 of 5 matching events; read them from the start "
                                    "with --after-seq 0 and the same filters]")
        self.call("recent", "--after-seq", "-1", ok=False)

    def test_legacy_rows_use_their_ledger_position(self):
        self.append({"ts": "2026-01-01T00:00:00+00:00", "kind": "exp", "id": "OLD1", "note": "legacy one"},
                    {"ts": "2026-01-02T00:00:00+00:00", "kind": "exp", "id": "OLD2", "note": "legacy two"})
        self.call("log", "--note", "new")
        lines = self.lines()
        self.assertIn("#1 |", lines[0])
        self.assertIn("#2 |", lines[1])
        self.assertIn("#3 |", lines[2])
        self.assertEqual(self.events()[-1]["sequence"], 3)
        after = self.lines("--after-seq", "1")
        self.assertEqual(len(after), 2)
        self.assertIn("legacy two", after[0])

    def test_session_filters(self):
        self.call("log", "--note", "from a")
        self.call("log", "--note", "from b", env={**self.env, "RMEM_SESSION": "session-b"})
        self.call("log", "--note", "from c", env={**self.env, "RMEM_SESSION": "session-c"})
        only_b = self.lines("--from-session", "session-b")
        self.assertEqual(len(only_b), 1)
        self.assertIn("s=session-b", only_b[0])
        others = self.lines("--other-sessions")
        self.assertEqual(len(others), 2)
        self.assertFalse(any("s=session-a" in line for line in others))
        for args in (("--session", "session-b", "recent", "--other-sessions"),
                     ("recent", "--other-sessions", "--session", "session-b")):
            hidden = self.call(*args).stdout
            self.assertNotIn("s=session-b", hidden)
            self.assertIn("s=session-a", hidden)
        conflict = self.call("recent", "--from-session", "session-b", "--other-sessions", ok=False).stderr
        self.assertIn("use one", conflict)
        no_session = {k: v for k, v in self.env.items() if k != "RMEM_SESSION"}
        self.assertIn("--other-sessions needs your session",
                      self.call("recent", "--other-sessions", env=no_session, ok=False).stderr)

    def test_session_after_recent_is_identity_not_a_filter(self):
        self.call("log", "--note", "from a")
        self.call("log", "--note", "from b", env={**self.env, "RMEM_SESSION": "session-b"})
        # The per-call convention appends --session to every command; recent must still show everyone.
        lines = self.lines("-n", "5", "--session", "never-wrote-1")
        self.assertEqual(len(lines), 2)
        self.assertTrue(any("s=session-b" in line for line in lines))
        self.assertEqual(self.lines("--from-session", "never-wrote-1"), ["[no matching events]"])
        self.assertEqual(self.lines("--grep", "nothing-like-this"), ["[no matching events]"])

    def test_paging_follows_ledger_order_when_sequences_repeat(self):
        for number in range(1, 3):
            self.call("log", "--note", f"base {number}")
        base = self.events()
        for number in range(3, 6):
            self.call("log", "--note", f"side A {number}")
        # Side B appended #3 and #4 on another branch; a merge (or repair --conflict-markers) keeps both.
        stamp = base[-1]["ts"]
        self.append(*({**base[-1], "event_id": f"{number:032x}", "sequence": number, "ts": stamp,
                       "note": f"side B {number}"} for number in (3, 4)))
        first = self.lines("--after-seq", "2", "-n", "3")
        self.assertEqual([("side A" in line) for line in first[:3]], [True, True, True])
        self.assertEqual(first[3], "[2 more matching events; next page: --after-pos 5]")
        self.assertIn("pages follow ledger order with --after-pos", first[4])
        rest = self.lines("--after-pos", "5")
        self.assertEqual([("side B" in line) for line in rest], [True, True])
        self.assertEqual(self.lines("--after-seq", "5"), rest)
        newest = self.lines("-n", "2")
        self.assertIn("read them from the start with --after-pos 0", newest[2])
        self.assertEqual(self.lines("--after-pos", "7"), ["[no matching events after line position 7]"])
        self.call("recent", "--after-seq", "1", "--after-pos", "1", ok=False)

    def test_lock_events_are_hidden_by_default_in_recent_and_status(self):
        self.call("claim", "alpha")
        self.call("log", "--thread", "alpha", "--id", "E1", "--note", "work")
        self.call("release", "alpha")
        lines = self.lines()
        self.assertEqual(len(lines), 2)
        self.assertIn("| exp |", lines[0])
        self.assertEqual(lines[1], "[2 claim/release events hidden; add --include-locks to show them]")
        self.assertEqual(len(self.lines("--include-locks")), 3)
        claims = self.lines("--kind", "claim")
        self.assertEqual(len(claims), 1)
        self.assertIn("| claim |", claims[0])
        self.assertEqual(len(self.lines("--kind", "exp,release")), 2)
        status = self.call("status").stdout
        self.assertIn("## Last 1 ledger events (2 claim/release events hidden;", status)
        self.assertNotIn("| claim |", status)
        self.assertIn("| claim |", self.call("status", "--include-locks").stdout)
        self.call("recent", "--kind", "exp,", ok=False)

    def test_count_above_the_maximum_is_clamped_with_a_notice(self):
        self.call("log", "--note", "one")
        lines = self.lines("-n", "500")
        self.assertIn("one", lines[0])
        self.assertEqual(lines[-1], "[-n 500 is above the maximum of 200; showing at most 200 events]")
        self.assertEqual(self.call("recent", "-n", "0").stdout, "")
        self.call("recent", "-n", "-1", ok=False)
        self.call("recent", "-n", "many", ok=False)
        catalog = self.view("catalog", "-n", "1000")
        self.assertEqual(catalog["notice"], "-n 1000 is above the maximum of 200; showing at most 200 rows")
        self.assertEqual(len(catalog["rows"]), 1)

    def test_output_budget_drops_the_oldest_lines_and_says_so(self):
        (self.hub / "hub-config.json").write_text(json.dumps({"caps": {"output_max_chars": 3000}}))
        self.append(*({"ts": "2026-01-01T00:00:00+00:00", "kind": "exp", "id": f"E{number}",
                       "note": f"{number} " + "x" * 300} for number in range(1, 41)))
        result = self.call("recent", "-n", "40").stdout
        self.assertLessEqual(len(result), 3000)
        lines = result.splitlines()
        self.assertIn("id=E40", lines[-2])
        self.assertIn("(the output budget held no more)", lines[-1])
        self.assertIn("--after-seq 0", lines[-1])
        forward = self.call("recent", "-n", "40", "--after-seq", "0").stdout.splitlines()
        self.assertIn("id=E1 ", forward[0])
        self.assertRegex(forward[-1], r"^\[\d+ more matching events; next page: --after-seq \d+\]$")

    # show --field

    def test_long_legacy_field_reads_completely_in_pages(self):
        text = "".join(f"line {number}: " + "word " * 30 + "\n" for number in range(200))
        self.append({"ts": "2026-01-01T00:00:00+00:00", "kind": "exp", "id": "LONG", "hyp": text})
        whole = self.view("show", "LONG")
        self.assertIn("--field NAME --page N", whole["error"])
        pages, page = [], 1
        while True:
            view = self.view("show", "LONG", "--field", "hyp", "--page", page, "--page-size", 5000)
            pages.append(view["text"])
            self.assertEqual((view["chars"], view["page_size"]), (len(text), 5000))
            if view["next"] is None:
                break
            self.assertEqual(view["next"], f"rmem show LONG --field hyp --page {page + 1} --page-size 5000")
            page += 1
        self.assertEqual("".join(pages), text)
        self.assertEqual(view["pages"], page)
        self.call("show", "LONG", "--field", "hyp", "--page", page + 1, "--page-size", 5000, ok=False)
        clamped = self.view("show", "LONG", "--field", "hyp", "--page-size", 50000)
        self.assertEqual((clamped["page_size"], len(clamped["text"])), (8000, 8000))
        self.assertIn("using 8000", clamped["notice"])
        plain = self.call("show", "LONG", "--field", "hyp", "--format", "text").stdout
        self.assertTrue(plain.startswith("# LONG field hyp: characters 1 to 4,000 of"))
        self.assertIn("[next page: rmem show LONG --field hyp --page 2]", plain)
        event = self.view("show", "LONG", "--field", "event")
        self.assertEqual(event["encoding"], "json")
        self.assertIn('"id": "LONG"', event["text"])

    def test_field_errors_point_to_the_next_step(self):
        self.call("log", "--id", "E1", "--note", "short", "--metric", "acc=0.5")
        self.assertEqual(self.view("show", "E1", "--field", "note")["text"], "short")
        self.assertEqual(json.loads(self.view("show", "E1", "--field", "metrics")["text"]), {"acc": 0.5})
        self.assertEqual(self.view("show", "E1", "--field", "metrics.acc")["text"], "0.5")
        missing = self.call("show", "E1", "--field", "nothing", ok=False).stderr
        self.assertIn("available:", missing)
        self.assertIn("metrics", missing)
        self.assertIn("no note body", self.call("show", "E1", "--body", ok=False).stderr)
        self.assertIn("add --field NAME", self.call("show", "E1", "--page", "2", ok=False).stderr)
        self.call("show", "E1", "--section", "record", "--field", "note", ok=False)
        self.call("show", "E1", "--field", "note", "--page", "0", ok=False)
        self.assertEqual(self.view("show", "E1", "--section", "history")["history_total"], 1)
        self.assertIn("event", self.view("show", "E1"))

    # catalog

    def test_catalog_regex_and_repeated_queries_share_one_budget(self):
        for identifier, note in (("E1", "alpha result"), ("E2", "beta result"), ("E3", "gamma result")):
            self.call("log", "--id", identifier, "--note", note)
        both = self.view("catalog", "--query", "alpha", "--query", "BETA")
        self.assertEqual(sorted(row["experiment_id"] for row in both["rows"]), ["E1", "E2"])
        self.assertEqual({row["experiment_id"]: row["matched_by"] for row in both["rows"]},
                         {"E1": ["alpha"], "E2": ["BETA"]})
        regex = self.view("catalog", "--regex", "E[13]")
        self.assertEqual(sorted(row["experiment_id"] for row in regex["rows"]), ["E1", "E3"])
        self.assertNotIn("matched_by", regex["rows"][0])
        mixed = self.view("catalog", "--query", "beta", "--regex", "gamma", "-n", "1", "--updates")
        self.assertEqual((mixed["matching_count"], len(mixed["rows"]), mixed["truncated"]), (2, 1, True))
        self.assertEqual(mixed["rows"][0]["matched_by"], ["regex"])
        self.assertEqual(self.view("catalog", "--query", "alpha")["rows"][0]["experiment_id"], "E1")
        self.call("catalog", "--regex", "[", ok=False)

    # export

    def test_export_writes_selected_events_outside_the_hub(self):
        self.append({"ts": "2026-01-01T00:00:00+00:00", "kind": "exp", "id": "OLD", "note": "legacy"})
        for number in range(2, 6):
            self.call("log", "--note", f"event {number}")
        target = self.root / "out" / "slice.jsonl"
        self.call("export", "--from-seq", "2", "--to-seq", "4", "--out", target, ok=False)
        target.parent.mkdir()
        result = self.call("export", "--from-seq", "2", "--to-seq", "4", "--out", target).stdout
        self.assertIn("exported 3 events (#2 to #4)", result)
        rows = [json.loads(line) for line in target.read_text().splitlines()]
        self.assertEqual([row["sequence"] for row in rows], [2, 3, 4])
        self.assertIn("already exists", self.call("export", "--out", target, ok=False).stderr)
        self.call("export", "--to-seq", "1", "--out", target, "--force")
        legacy = [json.loads(line) for line in target.read_text().splitlines()]
        self.assertEqual(legacy, [{"ts": "2026-01-01T00:00:00+00:00", "kind": "exp", "id": "OLD",
                                   "note": "legacy", "derived_sequence": 1}])
        recent = self.root / "out" / "recent.jsonl"
        self.call("export", "--since", "1h", "--kind", "exp", "--out", recent)
        self.assertEqual(len(recent.read_text().splitlines()), 4)
        for inside in (self.hub / "threads" / "x.jsonl", self.hub / "ledger.jsonl", self.hub / "x.jsonl"):
            self.assertIn("inside the hub", self.call("export", "--out", inside, "--force", ok=False).stderr)
        self.call("export", "--out", self.hub / "runs" / "slice.jsonl")
        self.assertEqual(len((self.hub / "runs" / "slice.jsonl").read_text().splitlines()), 5)
        self.call("export", "--from-seq", "4", "--to-seq", "2", "--out", self.root / "out" / "bad.jsonl", ok=False)
        self.assertEqual(len(self.events()), 5)


if __name__ == "__main__":
    unittest.main()
