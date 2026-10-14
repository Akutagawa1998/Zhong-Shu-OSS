# Thread lifecycle

A thread is a short working summary, not a history. When a thread is finished,
too long, or out of date, use the commands below instead of rewriting it by
hand. They keep the original bytes, record what happened in the ledger, and
never rewrite past ledger events.

## States

| State | Set by | `rmem status` |
|---|---|---|
| active | default, or `rmem thread reopen` | listed |
| sealed | `rmem thread seal` | hidden (counted in one line) |
| archived | `rmem thread archive --apply` | hidden (counted in one line) |

The state comes from the latest `thread_sealed`, `thread_reopened` or
`thread_archived` event for that thread. A thread with none of these events is
active, so every thread of a hub installed before 0.4.0 starts active; nothing is
rewritten. `rmem status --all` lists every thread with its state, and
`rmem status --thread SLUG` shows one thread whatever its state.

## Commands

Every write needs a session (`--session` or `RMEM_SESSION`) and the same write
authorization as `rmem log --thread SLUG`: it is refused while another session
holds a live lease on the thread. Commands that rewrite the thread file are dry
runs unless `--apply` is given; the dry run prints what would change and writes
nothing.

- `rmem thread seal SLUG --summary TEXT` records `thread_sealed` with the summary
  as its note. The file stays as it is. Release your lease afterwards if you took one.
- `rmem thread reopen SLUG [--note TEXT]` records `thread_reopened`. Update the
  summary and next step before others rely on the thread.
- `rmem thread archive SLUG --summary TEXT [--apply]` keeps the whole thread in
  history and replaces it with a pointer page. With `--apply`:
  1. copies the thread's exact bytes to
     `history/threads/<slug>/<UTC date>-<first 12 hex digits of its SHA-256>.md`
     (an existing file with the same bytes is reused; one with different bytes
     stops the command before anything changes);
  2. appends `thread_archived` with `archived_path`, `archived_sha256`,
     `archived_bytes`, `original_path`, `previous_state` and the summary as note;
  3. adds an entry to `history/threads/SNAPSHOTS.json`;
  4. replaces `threads/<slug>.md` with a pointer page: the original title, a
     lifecycle line (date, session, ledger event), the summary, a relative link to
     the archived copy with its SHA-256 and size, and a note that negative
     results, dead ends and corrections in the archived text stay in force.
- `rmem thread roll SLUG --keep N [--apply]` shortens a long thread. In each
  `## ` section it keeps the heading and the last N paragraph blocks (runs of
  lines separated by blank lines; a fenced code block stays whole) and replaces
  the older blocks with one line such as
  `Older entries of this section (3 blocks) are archived in [history/threads/...](../history/threads/...)`.
  Text before the first `## ` heading, and text under a `# ` heading, is never
  changed. The archived copy is the whole thread before the roll (exact bytes),
  so the moved blocks keep their context. It appends `thread_rolled` (`keep`,
  `moved_blocks`, the archive fields) and does not change the thread's state.
  When no section has more than N blocks it changes nothing.

The order of the writes is: archived copy, ledger event, `SNAPSHOTS.json`, live
thread file. The original bytes are therefore safe before anything else changes;
if a command stops early, fix the cause and rerun it (identical archive bytes are
reused).

Agents edit thread files directly, without the hub lock, so an archive or roll
compares the thread with the bytes it copied twice: before it records the event
(a change stops it, removes the new archived copy and records nothing) and just
before it replaces the live file (a change then leaves the live file untouched;
the event is recorded, so reopen and archive again, or roll again). An edit
made while the command ran is therefore never lost. `rmem check` fails when an
archived copy no longer matches the SHA-256 in its event.

## `history/threads/SNAPSHOTS.json`

```json
{
  "_doc": "Archived thread copies written by rmem thread archive|roll ...",
  "schema_version": 1,
  "snapshots": [
    {"thread": "baseline", "action": "archive", "original_path": "threads/baseline.md",
     "archived_path": "history/threads/baseline/2026-10-14-3f2a9c1d0b4e.md",
     "bytes": 2481, "sha256": "3f2a9c1d0b4e…", "event_id": "…", "sequence": 812,
     "captured_at": "2026-10-14T08:34:00.000000+00:00", "agent": "codex",
     "session": "codex-analysis-unique123", "relative_links_base": "threads",
     "summary": "E42 kept; matched-seed comparison in findings/baseline.md"}
  ]
}
```

Links inside an archived copy were written relative to `threads/`
(`relative_links_base`). Verify a copy with `shasum -a 256 FILE`. Archived copies
are ordinary memory files: commit them together with the pointer page or rolled
thread that links to them, and do not edit them. `rmem sync` (and `sync --embedded`)
commits `history/` together with threads, findings, notes and lessons.

## Staleness warnings in `rmem check`

`check` warns about an active thread when more than
`staleness.events_after_edit` ledger events (default 20, set in
`hub-config.json`; see CONFIG.md) name the thread after its file last changed.
The last change is the time of the last Git commit that touched the file when the
hub is in a Git repository and the file has no uncommitted changes; otherwise it is
the file's modification time. Git time comes first because a clone or checkout
resets modification times. Claim, release and lifecycle events do not count.
Sealed and archived threads are not checked. To clear the warning, update the
thread's results and next step, or seal the thread. The warning never fails `check`.

## Moved artifacts: multi-hop routes

A project's `ARTIFACT_ROUTES.json` (see RECORDS.md) maps an old path to the
current location of the same bytes. When a file moves again, add a new row whose
`original_path` is the previous location, with the same `sha256` and `bytes`;
do not rewrite the old row. Resolution then follows the chain A → B → C: a hop is
followed only when the registered location no longer holds the recorded bytes
and that location is itself registered with the same SHA-256 and size. At most 16
hops are followed. A cycle, two onward rows for the same bytes (a fork), a missing
final file and changed bytes all fail with an explicit error. The result lists
`route` and `hops`; `relative_links_base` stays the original file's directory.
A route establishes which bytes a file holds, never its current authority.
