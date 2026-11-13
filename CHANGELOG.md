## [0.4.1] - 2026-11-12

- Check staged additions and modifications for common credential patterns in both
  sync modes. Refused standalone syncs preserve the caller's Git index.
- Require `RMEM_TRUSTED_PLUGIN_HUB` to match the hub before loading project Python
  plugins. Git guard hooks continue to disable plugins.
- Add an MIT license, a security policy and Linux and macOS CI.

## [0.4.0] - 2026-10-14

Per-area details are in [docs/changes/](docs/changes/).

### Features
- **Configuration and versions.** Optional `hub-config.json` (caps, display time
  zone, roles, lease TTL, request and staleness thresholds). `VERSION`,
  `rmem version`, and a `.kit-manifest.json` written by `init` so `check` can name
  locally changed kit files. `rmem upgrade --from KIT` (dry run unless `--apply`)
  updates unchanged kit files, adds new ones, lists conflicts, never touches
  project data, and recognizes hubs installed before 0.4.0 through
  `KIT_HISTORY.json`. Project code goes in `bin/plugins/`, loaded after the
  built-in feature modules.
- **Context caps count characters.** The thread cap is 9,000 characters (was
  6,000 bytes, three times stricter for Chinese text). `check` warns from 70% and
  fails above 100% with advice to move detail into findings, never to compress
  wording. `status` shows each file's share of its cap.
- **Read without write access.** Read commands fall back to an unlocked snapshot
  when the hub is not writable (Codex sandboxes), skipping an in-progress final
  line. `rmem doctor` explains access, identity sources, config and plugins.
- **Bounded queries.** `recent --since/--until` (local time, ISO or ages such as
  `6h`), `--after-seq`, `--from-session`, `--other-sessions`; claim/release lines
  hidden by default; `-n` is clamped instead of refused. `show --field NAME --page N`
  reads a long field completely; `catalog --regex` and repeated `--query`;
  `export` writes a JSONL slice for other tools.
- **Write path.** `--note-file` stores multi-line bodies under `notes/` (content
  addressed; the ledger keeps path and hash). All validation happens before any
  lock. `--code-ref`, `--cmd`/`--cmd-file`, `--workdir`, `--set KEY=VALUE` and
  `--no-provenance` describe code that ran elsewhere; `rmem capture` records a
  submitted remote command without running it. The local Git probe ignores the
  hub's own files. Secret-looking text is refused.
- **Session identity.** Without `--session`, rmem derives `claude-<conversation
  ID>` or `codex-<thread ID>`, records `harness_session` and effort on every event,
  and refuses writes from a probable fork. `session start/update` record role,
  model, effort and parent; `whoami` shows the identity in use. `--agent` and
  `--session` work before or after the command name.
- **Leases instead of write locks.** A claim is a lease with a TTL, renewed by
  any event of its holder; expiry is sticky. `log --thread` needs no claim and is
  refused only by another session's live lease. `claim --wait`,
  `claim --takeover --reason` (expired leases), and resource leases for files
  outside the hub (`claim --resource NAME`).
- **Handoff.** Requests with addressees and states (`request`, `ack`, `close`,
  `inbox`), shown at the top of `status`; lessons (`lesson add/list/export`) that
  move collaboration rules out of private agent memories; `pending`,
  `checkpoint`, and `rmem resume`, a bounded card to read after a compaction,
  resume or fork. `hooks install --claude` (optional) runs it from Claude Code.
- **Git for hubs inside a project repository.** `sync --embedded [--per-event]`
  commits only hub files through a temporary index (one commit per event, dated
  with the event time; the working tree and the user's index stay untouched;
  never pushes). `link --canonical`, `repair --conflict-markers`, `guard install`,
  and `check` lines for conflict markers, timestamp order, linked worktrees and
  old uncommitted events.
- **Highlights without a ledger copy.** New highlights store line digests (with
  deltas) instead of the whole ledger; old highlights still verify and restore;
  `--freeze-ledger` keeps the old format; `highlight index` writes the index.
- **Thread lifecycle.** `thread seal/reopen/archive/roll` (rewrites are dry runs
  unless `--apply`; originals keep exact bytes and hashes under `history/`),
  sealed threads hidden from `status`, staleness warnings in `check`, multi-hop
  archive routes.

### Design Rationale
- The hub should make the right workflow the easy one: agents parsed the raw
  ledger, compressed prose to pass byte caps, wrote claim/release pairs around
  every log, and kept collaboration rules in private memories because the CLI
  had no better path. Each feature above removes one of those reasons.
- Anything that rewrites files outside the ledger, installs hooks or upgrades a
  hub is a dry run by default. Ledger history is never rewritten; repairs and
  upgrades append an event.

### Notes & Caveats
- Kits before 0.4.0 cannot read version-2 highlights; use `--freeze-ledger` while
  old and new kits share one hub. Upgrade installed hubs with `rmem upgrade`, then
  relink projects so their instruction block mentions `resume`.
- The Claude Code hook schema is assumed from documentation, not verified on
  every version; hooks stay optional. Parallel subagents in one conversation
  share a harness ID and must pass their own `--session`.
- Leases coordinate one local hub; there is still no cross-clone lock.

## [0.3.1] - 2026-09-23

### Fixes and protocol changes
- Check claim files against append-ordered ownership events before ordinary
  mutations; detect ignored claims stranded by Git history changes, missing or
  mismatched owners, and exact claim-event anchor mismatches. Explicit recovery
  records the reason and prior state. A normal release cannot erase an earlier
  unauthorized transition; force recovery cannot bypass a corrupt ledger.
- Resolve historical import source IDs in `show` while separating original and
  ingestion UUIDs. Preserve direct-ID precedence and reject namespace ambiguity;
  catalog source IDs/namespaces to make exact ingestion selection discoverable.
- Verify frozen highlights after independent ledger insertions only when every
  captured event remains byte-identical, unique and in its original relative
  order. Report `history_verification` explicitly; reordering, missing/changed
  events and duplicate UUIDs still fail. Frozen manifests and history stay intact.
- Distribute the consistency helper/protocol to fresh hubs and add isolated
  stash, ownership, initialization, and import lookup regressions.
- Clarify index-first recovery and UTF-8 file byte budgets versus query character
  caps. Budgets require readable summaries and evidence pointers, not compressed
  shorthand. Project-specific gateways and state remain outside the generic kit.
- Add an optional project-neutral metadata index / exact-slice reader with an
  empty index template and fresh-init distribution. It requires explicit roots,
  rejects unindexed/outside/symlink inputs, and never imports active-context state.
  Recommend STATE near 3 KB and threads near 1–3 KB, retaining normal spaces;
  distinguish this reader's refusal from OS-level filesystem isolation.

### Limits
- No live hub is repaired, relocated, or made authoritative by installing this
  release. Cross-clone locking and a durable rollback guard remain unimplemented.

## [0.3.0] - 2026-09-22

### Features
- Add validated structured training/inference/analysis/engineering records through
  `log --record`, stable event UUIDs, and per-hub append sequences distinct from
  optimizer steps. Legacy ledger bytes and original IDs remain unchanged.
- Add bounded metadata-only `catalog` and exact-ID `show`, record-coverage checks,
  and an explicit adoption-cutoff check. Later corrections cannot disappear when
  a query matched an older claim; historical cards cover only their cited events.
- Resolve original evidence pointers through logical roots, device-local root
  overrides, and same-hash snapshots without claiming a file was revalidated.
- Ship a neutral record template, documentation, and disposable integration tests.
  Preserve atomic init/link, existing instructions, and one canonical writable hub.

- Add immutable milestone/version/finding/correction highlights with zoned event
  and capture times, selected text-file copies, manifest/ledger anchors, bounded
  time/text lookup, and superseding links preserved outside filters.
- Restore only into a separate review directory with all copied bytes rechecked.
  Reject ignored/control/secret inputs, symlink/hardlink escapes, binary data,
  oversized snapshots, unanchored copies, and changed historical ledger prefixes.

### Notes & Caveats
- Existing legacy calls remain valid and may lack structured records; coverage is
  reported rather than invented. Schema validation is not scientific acceptance.
- Remote pointers are not backups or current availability. New hub initialization
  imports no project results, active documents, permissions, controllers, or jobs.
- Memory-first routing is a cooperative protocol, not a filesystem read restriction,
  distributed lock, scheduler, or automatic context-reset mechanism.

## [0.2.1] - 2026-09-18

### Features
- Recover the ignored claim directory after a Git clone or bundle restore; keep
  strict validation of missing research memory. Add a clone-recovery regression.

### Design Rationale
- Git does not preserve empty runtime directories. Restore ephemeral claims
  without copying ownership or silently reconstructing missing research evidence.
- Keep project memory in its own hub and the reusable kit project-neutral.

### Notes & Caveats
- Validate a disposable restore, not only bundle integrity. Restored copies must
  not become concurrent writable authorities.

## [0.2.0] - 2026-09-17

### Features
- Package the kit with a skill entry point and explicit project initialization.
- Harden session ownership, file boundaries, run capture, evidence imports, and
  bounded context reads; add isolated regression tests.

### Design Rationale
- Store durable project memory inside the consuming repository by default; retain
  external hubs for multiple workspaces on the same machine.
- Keep source evidence, scientific interpretation, and operational authorization
  distinct. A memory import must not restart historical automation.

### Notes & Caveats
- This supersedes the unversioned prototype, which has no committed Git baseline.
- Mutating thread commands now require an explicit session identity. Existing
  claims without a session must be inspected and explicitly recovered.
- Locks coordinate a single local filesystem only. Remote clones are not concurrent
  writers. No live research takeover or cross-harness automatic injection was tested.
