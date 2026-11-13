# rmem-kit 0.4.0 design

Status: implemented in 0.4.0 (2026-10-14). Where a track deviated from these
contracts, the reason is in `docs/changes/<track>.md`; the adversarial review
after integration changed two contracts: derived sessions use the full harness ID
(`claude-<ID>`, `codex-<ID>`), and the `recent` event filter is `--from-session`
so that `--session` always means the caller.

This file records the contracts for the four implementation tracks.

## Ground rules

- Python standard library only; Python 3.10+; macOS and Linux.
- Backward compatible with installed 0.2–0.3 hubs: legacy ledgers (rows without
  `event_id`/`sequence`), legacy `claims/<slug>/meta.json`, hubs without
  `hub-config.json` or `.kit-manifest.json`. Existing data is never rewritten.
  A new check on a legacy hub reports `warn:`/`info:`, not a failure, unless it
  finds a real integrity problem.
- Append-only ledger. No command rewrites past events. Corrections are new events.
- Defaults are safe: anything that rewrites files outside the ledger, touches
  Git, installs hooks, or upgrades a hub is a dry run unless `--apply`/`--write`
  is given. Nothing ever pushes.
- Output stays bounded (`Output`, explicit budgets). No command prints the whole
  ledger.
- Human-facing text: plain English, normal spaces, no shorthand. Error messages
  say what to do next.
- Run the suite with `python3 -m unittest discover -s tests -v`.
- Each track records its user-facing changes in `docs/changes/<track>.md`
  (Keep a Changelog style bullets). The integrator writes `CHANGELOG.md`.

## Architecture after the foundation

`rmem-kit/bin/rmem` keeps the core (hub discovery, locking, ledger I/O,
ownership, existing commands) and becomes a dispatcher for feature modules.

### Feature modules

A feature module is a stdlib Python file in `rmem-kit/bin/` (shipped by `init`)
or in a hub's `bin/plugins/` (project-local). It may define:

```python
EVENT_KINDS = {"request", ...}            # new ledger kinds it writes
EVENT_STRING_FIELDS = {"to", ...}         # optional string fields it writes
def validate_event(event: dict) -> str     # "" or a problem; called by event_problem
def register(commands, core) -> None       # add subcommands to the argparse subparsers
def status_sections(ctx) -> list[str]      # extra bounded lines for `rmem status`
def check_problems(ctx) -> list[tuple]     # (level, message); level in fail|warn|info
def resume_sections(ctx) -> list[str]      # extra lines for `rmem resume` (track A)
```

`core` is a `types.SimpleNamespace` built in `rmem` after all core functions
exist. It exposes at least: `RmemError, find_hub, validate_hub, hub_lock,
events_locked, append_locked, actor, one_line, identifier, count, regex,
safe_path, atomic_write, open_regular, bounded_lines, text_chars, text_excerpt,
Output, clipped, fmt_event, now, load_config, display_time, parse_user_time,
consistency_preflight, authorize_thread_write, describe_owner, thread_visible,
run_git, provenance, source_records, kit_root, kit_version`.

`ctx` for status/check/resume is a `SimpleNamespace(hub, events, config, who,
core, args)`; `events` is the list already read under the lock, so modules
must not reread the ledger.

The registry list in `rmem` is `FEATURE_MODULES`. A track adds its module with
one import line and one list entry. Plugins in `<hub>/bin/plugins/*.py` load
after built-in modules; a broken plugin is reported by `doctor`/`check` and
skipped, never crashes read-only commands.

### Configuration: `hub-config.json` (`rmem_config.py`, foundation)

Optional JSON object in the hub root, tracked by Git. Missing file = defaults.
`init` writes a commented default copy as `hub-config.json` (JSON has no
comments: use a `"_doc"` string field). Known keys:

```json
{
  "schema_version": 1,
  "caps": {"state_max_lines": 120, "state_max_chars": 8000,
           "thread_max_lines": 80, "thread_max_chars": 9000,
           "output_max_chars": 16000, "soft_ratio": 0.7},
  "display_tz": null,
  "roles": {},
  "leases": {"default_ttl_hours": 12},
  "requests": {"ack_warn_days": 2},
  "staleness": {"events_after_edit": 20},
  "git": {"uncommitted_warn_hours": 24},
  "storage": {"hub_warn_mb": 200, "file_warn_mb": 5}
}
```

Caps count characters (not bytes). `display_tz` is an IANA name; `null` uses
the system zone; env `RMEM_TZ` overrides. Unknown keys produce `warn:` in
check. Tracks may add a key group for their feature; they document it in
`rmem-kit/CONFIG.md` (foundation creates the file) under their own heading.

### Time

- Stored: UTC ISO 8601 with offset (unchanged).
- Displayed: `display_time(ts, config)` → `2026-10-14 01:34 PDT`.
  `fmt_event` shows display time, `#<sequence>` and `s=<session>`.
- Input: `parse_user_time(value, config)` accepts ISO 8601 with offset, a naive
  `YYYY-MM-DD[ HH:MM[:SS]]` in the display zone, and relative `90m`, `6h`,
  `3d` (ago). Use it for every new `--since/--until`.
- Journal file names use the UTC date.

### Ownership hook points

- `authorize_thread_write(hub, slug, who, purpose)` — the only place that
  decides whether a write to a thread is allowed. Foundation: current claim
  semantics. Track A: lease semantics. `purpose` in `log|run|highlight|request`.
- `describe_owner(ctx, slug)` — text for the thread list in status. Track A.
- `thread_visible(ctx, slug)` — status filter. Track D (sealed threads).

### Log path split

`cmd_log` = `build_log_event(a, who)` (pure: all validation, no lock; track B)
+ the locked commit part (preflight, `authorize_thread_write`, import dedup,
append; track A only touches it through `authorize_thread_write`).

## Track ownership

| Track | Plan items | Owns (new) | May edit in core |
|---|---|---|---|
| Foundation | P0-1 (VERSION, manifest, `version`), P0-2 (chars, config caps, soft warnings, `status --thread` budget), P0-3 (read-only lock, `doctor`), P0-6 (`/runs/`, `.rgignore`, time display, UTC journal names), global `--agent/--session` after the subcommand, did-you-mean, registry, `rmem_config.py` | `rmem_config.py`, `CONFIG.md`, `VERSION`, `HUB_CONFIG_TEMPLATE.json`, `.rgignore` template, `tests/test_foundation.py` | everything (it lands first) |
| A — identity and handoff | P1-6 session identity, P1-3 leases, P1-2 requests and lessons, P1-1 resume/pending/checkpoint/hooks | `research_identity.py`, `research_handoff.py`, `HANDOFF.md`, tests `test_identity.py`, `test_leases.py`, `test_handoff.py` | `actor`, `append_locked` (identity fields), claim/release, `authorize_thread_write`, `describe_owner`, stale-claim part of check, `research_consistency.py`, AGENTS.md concurrency/handoff sections |
| B — read and write path | P0-4 bounded queries, P0-5 rest (note files, validation before lock), P1-9 provenance fields and `capture` | `research_queries.py` (if useful), tests `test_queries.py`, `test_write_path.py` | `cmd_recent`, `cmd_show`, `cmd_catalog`, `research_records.py`, `build_log_event`, `provenance`, log/recent/show/catalog parsers, RECORDS.md |
| C — Git and storage | P1-4 embedded Git (`sync --embedded --per-event`, `link --canonical`, ledger checks, `repair --conflict-markers`, `guard`), P1-5 highlights without a full ledger copy (+ `highlight index`) | `research_git.py`, `GIT.md`, tests `test_git_embedded.py`, highlight tests | `cmd_sync`, `cmd_link`, `research_highlights.py`, HIGHLIGHTS.md, CONSISTENCY.md (Git part) |
| D — lifecycle and upgrade | P1-7 thread roll/archive/seal, staleness, multi-hop routes; P1-8 `upgrade` (+ known-version table) | `research_lifecycle.py`, `research_upgrade.py`, `KIT_HISTORY.json`, `LIFECYCLE.md`, `UPGRADE.md`, tests `test_lifecycle.py`, `test_upgrade.py` | `thread_visible`, `artifact_routes.py`, `cmd_init` only for new shipped files |

Shared files (`rmem` parser blocks, `event_problem`, `README.md`, `AGENTS.md`,
`SKILL.md`, `BOOTSTRAP.md`, `cmd_init` whitelist): touch only your own lines,
keep edits small, and prefer adding to your module. The integrator merges
tracks in the order foundation → B → A → C → D and resolves conflicts.

## Feature contracts the tracks rely on

- Identity (A): if neither `--session` nor `RMEM_SESSION` is set, derive it:
  Claude Code `CLAUDE_CODE_SESSION_ID` → `claude-<full ID>`; Codex
  `CODEX_THREAD_ID` → `codex-<full ID>` (UUIDv7 prefixes are timestamps). Every appended event records
  `harness_session` (full harness ID) when available. `rmem session start
  [--parent S] [--role R] [--model M] [--effort E]` writes `session_started`.
  A write whose session was started under a different `harness_session`
  is refused as a probable fork ("start a new session with --parent").
  `CLAUDE_EFFORT` is recorded when present. `rmem whoami` is read-only.
- Leases (A): `claim` acquires a lease (TTL from config or `--ttl`). Activity =
  ledger events of the holder session with no gap above the TTL since the claim (no renewal events; sticky expiry, added in review). Expired
  leases show as expired in status; `claim --takeover --reason` takes an
  expired lease and records `previous_owner` + `reason` (consistency treats it
  like a forced claim). `claim --wait SECONDS` waits without reading the ledger
  in a loop. `log --thread T` needs no claim: it is refused only when another
  session holds a live lease on T. Kinds `request*` are never refused.
  `check` reports expired leases as `warn:`; integrity mismatches still fail.
- Requests (A): `request --to thread:T|role:R|session:S --id ID`, `ack`,
  `close --by EVENT|--reason`, `inbox`; open requests appear at the top of
  `status` and `resume` for the addressee; check warns after `ack_warn_days`.
- Notes (B): `--note` stays one line (the summary). `--note-file PATH|-` stores a
  multi-line body under `notes/<yyyy-mm>/<event_id>.md`; the event keeps
  `body_path`, `body_sha256`, `body_chars`. `show ID --body` pages it.
- Highlights (C): new snapshots no longer copy the ledger; old snapshots still
  verify; `--freeze-ledger` keeps the old behavior.
- Lifecycle (D): `thread_sealed`/`thread_reopened`/`thread_archived` events
  drive `thread_visible`; archived originals keep bytes + sha256 under
  `history/threads/<slug>/`.
