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
Up to 32 explicitly named files, 1 MiB each and 8 MiB total, are supported; the
ledger prefix must be at most 16 MiB. Larger evidence stays in its original store.

Selected bytes are copied under `highlights/<ID>/files/`; the exact ledger prefix
is copied separately. `MANIFEST.json` records hashes, lengths, Git revision and
dirty status, source paths, author/session, temporal fields and evidence scope.
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
file coverage, manifest hash, unique ledger anchor, and unchanged historical
ledger prefix. Subsequent ledger appends are allowed. It does not reread mutable
original files or reinterpret reported findings as accepted results.
`superseded_by` exposes subsequent corrections even when their dates or text are
outside the requested filter. An older version remains recoverable, with its
known corrections visible alongside it.

## Recover into a separate directory

```sh
python3 .research-memory/bin/rmem highlight restore hl-EXACT_UUID --destination /tmp/project-historical-review
```

The destination must be new or empty, outside the live Git repository, hub and
control directories. Its parent must exist and the destination must use canonical directories without
symlink ancestors (for example, resolve the system temporary directory first).
On macOS, `/tmp` normally resolves to `/private/tmp`; use the canonical path for
both snapshot specs and restore destinations rather than relaxing validation. `frozen_files/` contains the selected
paths and exact bytes; the manifest and ledger prefix accompany them. This is a
historical review artifact. No current files, controller, claims, scheduler jobs,
automation or research mode are restored, changed, enabled, or executed. A later
decision to adopt old material must follow the current thread/claim protocol.
