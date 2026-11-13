# Track D: thread lifecycle and kit upgrade (0.4.0)

### Added

- `rmem thread seal SLUG --summary TEXT` and `rmem thread reopen SLUG [--note TEXT]`
  record `thread_sealed` / `thread_reopened`. Sealed and archived threads are hidden
  from `rmem status`, which prints how many are hidden; `status --all` lists every
  thread with its state, and `status --thread SLUG` still shows any thread. A thread
  without lifecycle events is active, so threads of older hubs stay visible.
- `rmem thread archive SLUG --summary TEXT [--apply]` (dry run by default) copies the
  thread's exact bytes to `history/threads/<slug>/<UTC date>-<sha12>.md`, records
  `thread_archived` (path, SHA-256, size, original path), indexes the copy in
  `history/threads/SNAPSHOTS.json`, and replaces the thread with a pointer page that
  keeps the title, the summary, a link to the copy and a note that negative results
  and corrections in the original stay in force. It refuses a destination that
  already holds different bytes.
- `rmem thread roll SLUG --keep N [--apply]` (dry run by default) keeps each `## `
  section's heading and last N paragraph blocks (fenced code stays whole), moves the
  older blocks into an archived copy of the whole thread, leaves one link line per
  shortened section, never changes text outside `## ` sections, and records
  `thread_rolled`.
- `rmem check` warns about an active thread that has more than
  `staleness.events_after_edit` (default 20) ledger events since its file last
  changed: the last Git commit touching it when the hub is in Git and the file is
  clean, otherwise its modification time. Claims, releases and lifecycle events do
  not count.
- `rmem upgrade --from KIT_DIR [--apply]` (dry run by default) classifies every
  file the kit ships: update (still as installed and changed in the kit), add (new),
  merge (`.gitignore`: anchors kit rules, adds missing ones at the top, keeps every
  local line), conflict (locally modified; never overwritten; the diff command is
  printed) or same. Project data (STATE, MAP, ledger, `hub-config.json`, threads,
  findings, journal, claims, history, highlights, notes, lessons, runs, records) is
  never written. `--apply` takes the exclusive lock, copies files atomically, creates
  `bin/plugins/` and a missing `hub-config.json`, rewrites `.kit-manifest.json`
  (keeping `first_installed_at`) and records `kit_upgraded` with from/to versions and
  counts. A conflict in a code file (`bin/rmem`, `bin/*.py`) blocks `--apply`.
- `rmem-kit/KIT_HISTORY.json`: SHA-256 of every kit file at v0.2.0, v0.2.1, v0.3.0
  and v0.3.1, so `upgrade` recognizes unchanged files and the installed version of a
  hub without a manifest. UPGRADE.md holds the bash command that regenerates it.
- New kit documents `LIFECYCLE.md` and `UPGRADE.md`; README sections for both; a
  pointer in AGENTS.md; the staleness key documented in CONFIG.md.

### Changed

- Artifact routes resolve registered multi-hop moves (A → B → C): a hop is followed
  when a location no longer holds the recorded bytes and is itself registered with
  the same SHA-256 and size. Cycles, forks and routes longer than 16 hops fail
  explicitly; results add `route` and `hops`. Single-hop results and errors are
  unchanged.
- `init` also installs `LIFECYCLE.md`, `UPGRADE.md`, `KIT_HISTORY.json`,
  `bin/research_lifecycle.py` and `bin/research_upgrade.py` (recorded in the manifest).

### Notes for the integrator

- `thread_visible` (core) delegates to `research_lifecycle.visible` and returns
  True for every thread under `status --all`; it tolerates a ctx without
  `events`/`args`. The status thread list appends ` · sealed` / ` · archived` for
  hidden threads shown by `--all`, and the hidden-count line now names `--all`.
- Lifecycle writes call `authorize_thread_write(hub, slug, who, "log")`, so track
  A's lease semantics apply to them unchanged.
- `upgrade` reads `KIT_FILES` and `KIT_HELPERS` from the source kit's `bin/rmem`
  with `ast.literal_eval`, without running it. Keep both as tuple literals; a test
  (`test_current_kit_is_readable_and_a_fresh_hub_is_up_to_date`) fails otherwise.
- `KIT_HISTORY.json` covers released tags only; regenerate it (command in
  UPGRADE.md) when 0.4.0 is tagged so hubs that lost their manifest stay recognizable.
- `tests/test_foundation.py` asserted `FEATURE_MODULES == []`; it now checks that
  each built-in module is a file in the kit's `bin/`.
- `rmem sync` (`memory_path`, track C) does not commit `history/`, so a synced
  pointer page can reference an archived copy that is not committed. Track C or the
  integrator should let `sync` include `history/threads/` (its `.md` files and
  `SNAPSHOTS.json`); LIFECYCLE.md tells users to commit it meanwhile.
- `upgrade` dry runs and `thread archive|roll` dry runs read under the shared lock
  with `readonly_ok`, so they also work on a hub this process cannot write.
