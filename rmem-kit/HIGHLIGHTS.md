# Immutable memory highlights

Highlights preserve an exact milestone, document version, finding, or correction
without replacing the current thread or rewriting old evidence. A highlight gets
an independent `hl-<UUID>` identity. `occurred_at` is when the described event
happened; `captured_at` is when this hub copied it. Both include time zones.

## Create a reviewed snapshot

Copy `HIGHLIGHT_TEMPLATE.json` to a working spec. Fill its title, summary,
significance, caveats, original experiment IDs, exact ledger event IDs, and
selected repository-relative file paths. `event_ids` must already exist;
`supersedes` points to verified earlier highlight IDs. Keep corrections explicit.
The evidence statuses `observed`, `reported`, `inferred`, `inconclusive`, and
`retracted` describe the frozen source; capture never upgrades acceptance.

```sh
python3 .research-memory/bin/rmem --agent codex --session YOUR_UNIQUE_SESSION claim project-state
python3 .research-memory/bin/rmem --agent codex --session YOUR_UNIQUE_SESSION highlight create reviewed-highlight.json
python3 .research-memory/bin/rmem --agent codex --session YOUR_UNIQUE_SESSION release project-state
```

The spec requires `content_reviewed_no_secrets: true` after reviewing its selected
inputs. This is an explicit attestation, not an automated guarantee. The CLI also
rejects credential/control paths, Git-ignored paths, common secret patterns,
symlinks, hardlinks, non-UTF-8/binary files, duplicate paths, and oversized input.
It does not recursively select repository files or dereference remote pointers.
Up to 32 explicitly named files, 1 MiB each and 8 MiB total, are supported.
Larger evidence stays in its original store.

Selected bytes are copied under `highlights/<ID>/files/`. `MANIFEST.json` records
hashes, lengths, Git revision and dirty status, source paths, author/session,
temporal fields, evidence scope and the ledger cutoff.

The manifest hash is anchored by a new `highlight_created` ledger event after
capture. The copied files are made read-only; the CLI never replaces a highlight.
This is tamper detection and cooperative immutability, not protection from a user
who can rewrite both the snapshot and its ledger anchor. Failed publication may
leave an unanchored snapshot, which verification rejects; inspect it rather than
silently promoting or deleting it.

`remote_artifacts` are metadata-only objects with `platform`, `uri`,
`evidence_status`, and nullable zoned `checked_at`. Platforms include `torch`,
`alice`, `145`, `github`, `hf`, `remote`, and `other`. Use secret-free stable pointers;
authenticated URLs, query strings and fragments are refused. Capture makes no
network request and does not imply that remote weights or outputs are backed up.

## How the ledger cutoff is stored

Since 0.4.0 a highlight does not copy the ledger (manifest `schema_version` 2).
It records the size and SHA-256 of the ledger at capture time, the event count,
the last event ID and sequence, and `ledger-lines.txt`: one row per captured
ledger line with the first 16 hex characters of that line's SHA-256 and its event
ID. A new highlight lists only the lines added since the previous v2 highlight
(its `ledger_prefix.base`), as long as that one is still an exact prefix of the
live ledger; every 16th highlight lists all lines, so a verification follows at
most 16 bases. A row is about 50 bytes, so a highlight costs about 50 bytes per
line added since its base (a full list for a 3,500-line ledger is about 175 KB,
where a copy was about 4 MB), and there is no ledger size limit.

Verification rebuilds the captured prefix from the live ledger and compares its
full SHA-256 with the recorded one, so the short line digests only locate lines.
Restore writes that rebuilt `ledger-prefix.jsonl` into the review directory. The
trade-off: a v2 highlight depends on the live ledger still containing the
captured events, and on its base highlights. If the ledger was rewritten (for
example, an event line was edited) restore fails and says so; restore from a Git
commit or backup that has the events. Do not delete a highlight that later ones
use as a base (`highlight show` prints `ledger_prefix.base`).

`highlight create --freeze-ledger` keeps the 0.3 behavior (manifest version 1):
the exact ledger prefix is copied into `ledger-prefix.jsonl`, at most 16 MiB.
Use it for a snapshot that must survive a ledger rewrite. Version 1 highlights
made by older kits list, show, verify and restore exactly as before; a version 1
highlight is never used as a base. Kits before 0.4.0 cannot read version 2
manifests.

## Find, inspect, and verify

```sh
python3 .research-memory/bin/rmem highlight list -n 10
python3 .research-memory/bin/rmem highlight list --query 'qualification' --since '2026-09-20T00:00:00Z'
python3 .research-memory/bin/rmem highlight list --time-field occurred_at --until '2026-09-22T12:00:00Z' -n 1
python3 .research-memory/bin/rmem highlight list --time-field captured_at --since '2026-09-22T00:00:00Z'
python3 .research-memory/bin/rmem highlight show hl-EXACT_UUID
python3 .research-memory/bin/rmem highlight verify hl-EXACT_UUID
```

List/show output is bounded and includes a manifest pointer when detail is too
large. Lookup compares actual zoned instants. Verification checks copied bytes,
file coverage, manifest hash, unique ledger anchor, and historical event bytes.
An unchanged continuous ledger prefix is reported as `exact_prefix`.
If Git reconciliation inserted independent new events, verification only accepts
the original frozen events as an ordered subsequence: every original UUID must
occur exactly once, each full original JSON line must remain byte-identical, and
their original relative order must be preserved. This returns
`captured_events_preserved_with_insertions`, explicit counts and a warning that
the frozen history is no longer the current continuous prefix. Reordering,
missing or changed original events, duplicate UUIDs, and inserted rows without
independent UUIDs are rejected. The original snapshot, prefix hash and manifest
anchor are never rewritten. Exact-prefix legacy snapshots remain supported;
the insertion fallback requires stable UUIDs for every frozen event.
Subsequent appends are allowed. Verification does not reread mutable original
files or reinterpret reported findings as accepted results. Historical
validation does not establish the authority of the current ledger ordering.
`superseded_by` exposes subsequent corrections even when their dates or text are
outside the requested filter. An older version remains recoverable, with its
known corrections visible alongside it.

`verify` adds a `warnings` entry when Git ignores some of the snapshot's files,
so they would never be committed. Hubs installed before 0.4.0 have the
unanchored rule `runs/` in `.gitignore`, which also matches a selected file under
`highlights/<ID>/files/.../runs/`; anchor it as `/runs/` or add the files with
`git add -f`. `rmem check` verifies every highlight (within a 10-second budget;
`check --records` skips them), fails on one that does not verify, and repeats the
ignored-file warning. Copied bytes that match the anchored manifest are not
rescanned for secrets: they are the bytes that were scanned at capture.

## A readable index

```sh
python3 .research-memory/bin/rmem highlight index           # print it (dry run)
python3 .research-memory/bin/rmem highlight index --write   # save HIGHLIGHTS_INDEX.md
```

`HIGHLIGHTS_INDEX.md` lists verified highlights newest first by `occurred_at`
(at most 100 rows) with links to each manifest and to the highlights that
supersede it. It is generated from the manifests: regenerate it instead of
editing it. Once the file exists, `highlight create` refreshes it after the
highlight is recorded, holding only the shared hub lock while it rebuilds.

## Recover into a separate directory

```sh
python3 .research-memory/bin/rmem highlight restore hl-EXACT_UUID --destination /absolute/non-symlink/path/project-historical-review
```

The destination must be new or empty, outside the live Git repository, hub and
control directories. Its parent must exist. `frozen_files/` contains the selected
paths and exact bytes; the manifest and ledger prefix accompany them (for a
version 2 highlight the prefix is rebuilt from the live ledger and checked
against the recorded SHA-256). This is a
historical review artifact. No current files, controller, claims, scheduler jobs,
automation or research mode are restored, changed, enabled, or executed. A later
decision to adopt old material must follow the current thread/claim protocol.

Use canonical paths without symlink ancestors for specs and restore destinations. On macOS, `/tmp` is a symlink to `/private/tmp`; use the latter when choosing a temporary review directory.
