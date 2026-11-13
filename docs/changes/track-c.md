# Track C — Git integration and highlights without a ledger copy (0.4.0)

### Added

- `rmem sync --embedded [--per-event] [-m MSG] [--dry-run]` commits only the hub's
  files into the repository that contains the hub (also works when the hub is its
  own repository). Commits are built in a temporary Git index seeded from `HEAD`;
  your working tree and your staged changes outside the hub are never touched. The
  branch moves by compare-and-swap (`git update-ref REF NEW OLD`), and only the hub
  paths of your real index are refreshed afterwards, so `git status` is clean for
  them. It refuses a detached `HEAD`, a merge, rebase, cherry-pick, revert or bisect
  in progress, a ledger that Git ignores, and staged changes under the hub path
  (with the `git reset -q -- HUB_PATH` command to unstage them). Runtime files
  (`claims/`, `runs/`, lock files, `__pycache__/`, ledger backups) are never
  committed, even with an old `.gitignore`. Nothing is pushed.
- `--per-event`: one commit per ledger event not yet committed, with author and
  committer date equal to the event time and the message
  `Hub: <kind> <id or thread=...> (<status>)`. Other changed hub files go into the
  commit of the first event at or after their modification time (deleted files and
  later edits into the last commit). "Not yet committed" is derived from the ledger
  at `HEAD`, which must be a byte prefix of the live ledger; nothing is stored.
- `--dry-run` builds the same commits in a throwaway object store and prints them;
  the repository stays byte for byte unchanged.
- `rmem link --canonical CHECKOUT` writes this device's absolute hub path into
  `.rmem` and adds `/.rmem` to the repository's `.git/info/exclude` (shared by all
  worktrees). If `.rmem` is already tracked it prints the `git rm --cached` command
  instead of running it. The managed `AGENTS.md` block then runs
  `"$(cat .rmem)/bin/rmem"`, which works in every linked checkout.
- `rmem repair --conflict-markers [--apply]`: a dry run lists the Git conflict
  marker lines in `ledger.jsonl` and checks that every other line is a valid event
  with a unique `event_id`. `--apply` takes the exclusive lock, backs the ledger up
  to `ledger.jsonl.bak-<UTC time>`, removes only the marker lines and appends a
  `ledger_repair` event with the counts, line numbers and backup SHA-256. Anything
  else (a partial line, an invalid event, a repeated `event_id`) is refused.
- `rmem guard install [--write]` prints, or with `--write` creates,
  `post-checkout`, `post-merge`, `post-rewrite` and `pre-rebase` hooks that run
  `rmem check` and warn about uncommitted ledger events. They print warnings only
  and always exit 0. Installation respects `core.hooksPath` and never overwrites an
  existing hook; for one that exists it prints the line to add. `rmem guard run
  HOOK` is what the hooks call.
- `rmem check` (feature module `bin/research_git.py`):
  - fails on Git conflict marker lines, naming the lines and
    `rmem repair --conflict-markers` (they used to appear as "invalid JSON");
  - warns when timestamps or stored sequence numbers go backward in file order;
  - warns when the hub is inside a linked Git worktree and names the main
    checkout's hub, the canonical writable one;
  - warns when ledger events stay uncommitted longer than
    `git.uncommitted_warn_hours` (default 24);
  - verifies every highlight within a 10-second budget and fails on one that does
    not verify; warns when Git ignores snapshot files.
- `rmem status` shows a `## Git` section with the linked-worktree and
  uncommitted-events warnings when they apply.
- `rmem highlight index [--write]` prints, or saves, `HIGHLIGHTS_INDEX.md`: verified
  highlights newest first (at most 100 rows) with links to the highlights that
  supersede them. `highlight create` refreshes the file once it exists.
- `highlight verify` reports `ledger_storage` and adds `warnings` when Git ignores
  some snapshot files (the unanchored `runs/` rule of older hubs did this).
- New kit files: `GIT.md` and `bin/research_git.py`, copied by `init` and recorded
  in `.kit-manifest.json`. New hubs ignore `/ledger.jsonl.bak-*`.

### Changed

- New highlights (manifest `schema_version` 2) no longer copy the ledger. They
  record the captured prefix's size, SHA-256, event count, last event ID and
  sequence, and `ledger-lines.txt`: per captured line a 16-hex-character digest and
  the event ID, as a delta against the previous v2 highlight that is still an exact
  live prefix (a full list every 16th highlight, so a verification follows at most
  16 bases). Verification keeps 0.3.1's rules (exact prefix, or every captured
  event byte-identical, unique and in order with independent UUID insertions) and
  confirms the rebuilt prefix against the full SHA-256. `highlight restore`
  rebuilds `ledger-prefix.jsonl` from the live ledger and fails with an explanation
  when the live ledger no longer contains the captured events. The 16 MiB ledger
  limit no longer applies.
- `highlight create --freeze-ledger` keeps the 0.3 format (copied prefix, at most
  16 MiB). Version 1 highlights list, show, verify and restore as before. Kits
  before 0.4.0 cannot read version 2 manifests.
- Plain `rmem sync` on a hub inside a larger repository now says to use
  `--embedded`; `--per-event` and `--dry-run` require `--embedded`.
- A write blocked by a corrupt ledger names the first corrupt line and its problem.

### Notes for the integrator

- Core edits: `import research_git` and its `FEATURE_MODULES` entry; `events_locked`
  (conflict-marker text via `research_git.marker_problem`); `consistency_preflight`
  (names the corrupt line); `cmd_highlight` (`--freeze-ledger`, `index`, verify
  warnings, index refresh after create); `cmd_link` (`--canonical`); `cmd_sync`
  (`--embedded` dispatch, clearer errors); parser lines for `highlight create`,
  `highlight index`, `link` and `sync`; `KIT_FILES` (`GIT.md`), `KIT_HELPERS`
  (`research_git.py`), `IGNORE_RULES` (`/ledger.jsonl.bak-*`); the `core` namespace
  also exposes `event_problem` (used by `repair`).
- `repair` and `guard` are registered by `research_git.register`, not in the core
  parser.
- `tests/test_foundation.py` no longer expects `FEATURE_MODULES == []`; it checks
  that every built-in module is listed in `KIT_HELPERS`.
- The highlight check in `research_git.check_problems` reads the raw ledger bytes
  once (highlight verification needs the exact line bytes, which `ctx.events` does
  not carry), within the same lock as the rest of `check`.
