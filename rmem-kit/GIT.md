# Git and the hub

A hub is ordinary files, so Git versions it. Two layouts are common:

- **Embedded**: the hub is a folder inside the project repository, for example
  `project/.research-memory/`. This is the default when you run `init` inside a project.
- **Standalone**: the hub is its own repository.

In both layouts, Git operations change memory too. A checkout, stash, merge or
rebase can hide uncommitted ledger events, insert another branch's events in the
middle of the ledger, or leave conflict markers in it. The commands below make
those cases visible and give each one a safe repair path. None of them pushes,
and none of them rewrites the working tree.

## Commit memory: `rmem sync --embedded`

```sh
python3 .research-memory/bin/rmem sync --embedded --per-event --dry-run   # preview
python3 .research-memory/bin/rmem sync --embedded --per-event             # one commit per new event
python3 .research-memory/bin/rmem sync --embedded -m "Hub: weekly memory" # one commit
```

`sync --embedded` commits the files under the hub path and nothing else:

- It builds the commits in a temporary Git index seeded from `HEAD`, so your
  staged changes outside the hub, your working-tree files and your unstaged
  edits stay exactly as they were.
- It moves the current branch with a compare-and-swap (`git update-ref REF NEW
  OLD`). If `HEAD` moved while the commits were built, nothing changes and you
  run it again.
- Afterwards it refreshes only the hub paths in your real index, so `git status`
  is clean for them.
- It commits every change to a file Git already tracks, and of the untracked
  files only those rmem writes: the kit files and `bin/rmem` with its helpers,
  `bin/plugins/`, `STATE.md`, `MAP.md`, the ledger, `hub-config.json`,
  `.kit-manifest.json`, `ARTIFACT_ROUTES.json`, `CONTEXT_INDEX.json`,
  `HIGHLIGHTS_INDEX.md`, and files under `threads/`, `findings/`, `journal/`,
  `notes/`, `lessons/`, `history/`, `records/` and `highlights/`. Any other
  untracked file under the hub (a draft, a `.env`, a large binary) is named and
  the sync is refused; after reviewing them, `--include-untracked` commits them
  too, or move them out of the hub or list them in its `.gitignore`.
- Both sync modes check the staged contents of added and modified files for
  common key, token and password patterns. They refuse non-regular files and
  secret-looking paths, and report the affected file without printing its
  contents. Embedded sync also limits new files outside `highlights/` to
  `storage.file_warn_mb` (5 MB by default).
- The output names the added, modified and deleted hub files (up to 30), in the
  dry run and in the real run.
- Runtime and device-local files never go into a commit, even when an old
  `.gitignore` misses them: `claims/`, `runs/`, `.rmem.lock`, `.rmem-lock*`,
  `__pycache__/`, ledger backups (`ledger.jsonl.bak-*`), `local-roots.json`,
  `*.tmp` files and leftovers of interrupted captures (`.capture-*`,
  `.rmem-restore-*`, `.rmem-init-*`).
- It holds only a shared hub lock while it reads the ledger and stages the
  snapshot, then builds the commits after releasing it, so other sessions keep
  writing during a long sync. Their new events go into the next sync.
- It refuses, and tells you what to do, when `HEAD` is detached, during a merge,
  rebase, cherry-pick, revert or bisect, when Git ignores the ledger, and when you
  have staged changes under the hub path (unstage them with
  `git reset -q -- HUB_PATH`; your files stay as they are).
- `--dry-run` builds the same commits in a throwaway object store and prints
  them; the repository is left byte for byte unchanged.
- Git commit hooks do not run, because the commits are built with plumbing
  commands (`commit-tree`).

With `--per-event`, every ledger event that is not yet committed becomes its own
commit:

- The author and committer dates are the event's `ts`, so the history and a
  contribution graph show when the work happened.
- The message is `Hub: <kind> <id or thread=...> (<status>)`, for example
  `Hub: exp E043 (keep)`.
- Each commit holds the ledger up to that event. Other changed hub files
  (threads, STATE, findings, highlights, configuration) go into the commit of the
  first event at or after their modification time, or into the last commit.
  Deleted files go into the last commit.
- "Not yet committed" is derived from the ledger at `HEAD`, never stored: the
  committed ledger must be a byte prefix of the live one. After a merge or a
  repair that changed earlier lines, `--per-event` refuses; commit once without
  it.
- With no new events, the changed files go into one commit named by `-m`.
- Every per-event commit stores a full copy of the ledger blob. Git compresses
  and later packs them; run `git gc` after syncing many events.

`--embedded` also works when the hub is its own repository. Plain `rmem sync`
(without `--embedded`) keeps its old behavior for that layout: it stages memory
files in your index and runs `git commit`.

## One writable hub per device: `rmem link --canonical`

A worktree or a second checkout of the project contains its own copy of the
hub's tracked files. That copy follows the worktree's branch and is not where
memory should be written. Point every checkout at one hub instead:

```sh
cd /path/to/project/.research-memory
python3 bin/rmem link --canonical /path/to/project
python3 bin/rmem link --canonical /path/to/project-worktree-feature
```

`--canonical` writes the hub's absolute path into `.rmem` and adds `/.rmem` to the
repository's `.git/info/exclude` (shared by all worktrees), because the path is
per device. If `.rmem` is already tracked, rmem prints the `git rm --cached .rmem`
command to run and does not run it. The managed block in `AGENTS.md` then tells
agents to run `"$(cat .rmem)/bin/rmem"`, which works in every linked checkout.
Plain `link` keeps writing a relative pointer, which suits a hub that moves
together with a single checkout.

`rmem status` and `rmem check` warn when the hub you are using sits in a linked
Git worktree (its Git directory differs from the common directory). The warning
names the main checkout's hub, which is the canonical writable one.

## What `rmem check` looks for

| Problem | Level | What to do |
|---|---|---|
| Git conflict markers (`<<<<<<<`, `=======`, `>>>>>>>`, `\|\|\|\|\|\|\|`) in `ledger.jsonl` | fail | `rmem repair --conflict-markers` (below) |
| Timestamps or stored sequence numbers that go backward in file order | warn | Usually a merge or rebase inserted events. Highlights still verify when their captured events kept their order. Record a decision event if the order matters |
| The hub is inside a linked Git worktree | warn | Write to the main checkout's hub; `rmem link --canonical` |
| Ledger events not committed for more than `git.uncommitted_warn_hours` (default 24) | warn | `rmem sync --embedded --per-event` |
| A highlight that does not verify | fail | Inspect `highlights/ID/`; never rewrite it to pass. Verification runs within a 10-second budget; the rest is reported as `info:` |
| Snapshot files that Git ignores | warn | Anchor rules such as `runs/` as `/runs/`, or `git add -f` the files |

Configure the hour limit in `hub-config.json` (see [CONFIG.md](CONFIG.md)).

## Remove conflict markers: `rmem repair --conflict-markers`

```sh
python3 .research-memory/bin/rmem repair --conflict-markers           # dry run
python3 .research-memory/bin/rmem repair --conflict-markers --apply   # needs a session
```

Two devices that append to the ledger and then pull with an autostash can leave
conflict markers around both sides' events. Because the ledger is append-only,
both sides are kept; only the marker lines go. The dry run lists the marker
lines and checks that every remaining line is a valid event with a unique
`event_id`. With `--apply`, rmem takes the exclusive hub lock, copies the ledger
to `ledger.jsonl.bak-<UTC time>` (ignored by Git; delete it after review),
removes only the marker lines, and appends a `ledger_repair` event with the
counts, line numbers and the backup's SHA-256. It refuses anything else: a
partial line, a line that is not a valid event, or a repeated `event_id` needs a
reviewed manual repair. Repeated sequence numbers from the two sides are
reported; `check` keeps reporting them, so record a decision about them.

## Warn before Git changes memory: `rmem guard install`

```sh
python3 .research-memory/bin/rmem guard install          # print what would be installed
python3 .research-memory/bin/rmem guard install --write  # create the hooks
```

The hooks are `post-checkout`, `post-merge`, `post-rewrite` and `pre-rebase`.
After a checkout, merge or rewrite they run `rmem check` and print up to five
failures; before a rebase, and after the others, they warn about ledger events
that are not committed. They only print to stderr and always exit 0, so they never
block Git. Installation respects `core.hooksPath` and never overwrites an existing
hook: for an existing one it prints the line to add yourself. Set `RMEM_PYTHON`
if `python3` is not the interpreter the hooks should use. Remove a hook file to
disable it.

The hooks run Python on every checkout, merge, rewrite and bisect step, so they
never run code from the checked-out tree:

- `install --write` copies the installing CLI and its helpers (not
  `bin/plugins/`) to `.git/rmem-guard/<hash>/`, and the hooks run that pinned
  copy, not the hub's tracked `bin/rmem`. Install again after a kit upgrade to
  pin the new version.
- The hooks and the `check` they start set `RMEM_NO_PLUGINS=1`, so hub plugins,
  which a branch can change, are not loaded.
- Each hook first checks that it runs in the repository it was installed for and
  does nothing elsewhere.
- When `core.hooksPath` comes from your global or system Git configuration, the
  hooks would run for every repository that uses it; install refuses unless you
  pass `--global-hooks-path`. It also refuses a `core.hooksPath` that is a
  directory Git tracks, because the hooks hold this device's absolute paths.

## Limits

- Claims stay local runtime files; Git does not carry them. See
  [CONSISTENCY.md](CONSISTENCY.md) for claim and ledger reconciliation.
- Nothing here merges two ledgers for you. A merge that inserts events keeps
  both sides' events; `check` reports the order change and highlights verify the
  captured events as an ordered subsequence.
- rmem never pushes, pulls, rebases or force-updates a branch.
- Keep hubs private. Sync checks recognizable credential patterns in changed
  Git blobs. Review research notes, paths, session IDs and any artifacts before
  sharing a hub. A push exposes both the current files and their history.
