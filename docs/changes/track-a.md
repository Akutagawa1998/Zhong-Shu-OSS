# Track A — identity and handoff (0.4.0)

## Step 1: session identity and leases

### Added

- Session identity from the harness (`bin/research_identity.py`). Without
  `--session` or `RMEM_SESSION`, rmem derives the session from the conversation:
  `CLAUDE_CODE_SESSION_ID` becomes `claude-<full ID>`, `CODEX_THREAD_ID`
  becomes `codex-<full ID>` (Codex wins when both are set; the review changed the first 8 hex digits to the full ID because UUIDv7 prefixes are timestamps). Claude Code and
  Codex shells forget exported variables between tool calls, so agents can now
  omit `--session` there.
- Every appended event records `harness_session` (the full conversation ID) when
  one is available and `effort` from `CLAUDE_EFFORT` when it is set. Malformed or
  secret-looking values are never recorded.
- `rmem session start [--parent S] [--role R] [--model M] [--effort E]
  [--transcript PATH]` writes a `session_started` event. A role must be one of the
  keys of `roles` in `hub-config.json` when roles are configured; otherwise any
  text is accepted with a warning.
- `rmem session update --model/--effort/--role` writes a `session_updated` event
  for a mid-session model, effort or role change.
- `rmem whoami` (read-only): agent and session and where each came from (flag,
  environment or derived), the harness conversation ID, the recorded role, model,
  effort and parent (or `unknown`), last activity, the fork check and the leases
  this session holds.
- Fork detection: a session is bound to the conversation of its `session_started`
  event (or of its first event with a harness ID). Writes from another
  conversation under that session ID are refused with a message that explains the
  fork and the fix (`rmem session start --parent OLD` under a new or derived
  session). Writes without a harness ID (people, scripts) are not checked.
  `status` shows the same warning at the top.
- Leases. `rmem claim SLUG [--ttl HOURS]` takes a lease that stays live while the
  holder session writes any ledger event within the TTL
  (`leases.default_ttl_hours`, default 12 hours); no renewal events are written.
  The claim event and `claims/SLUG/meta.json` record `lease_ttl_hours`.
- `rmem claim SLUG --wait SECONDS` waits for another session's live lease to be
  released (or to expire, with `--takeover --reason`). It polls only the small
  claim file and releases the hub lock between polls.
- `rmem claim SLUG --takeover --reason TEXT` takes over an expired lease (not while
  the thread has an unfinished run). The event records `takeover`,
  `previous_owner`, `reason`, `previous_last_activity` and
  `previous_lease_expired_at`; the consistency check treats it like a forced claim.
- Resource leases for things outside the hub: `rmem claim --resource NAME` and
  `rmem release --resource NAME`. They live in `claims/@NAME/`, their events carry
  `resource` instead of `thread`, they never collide with a thread of the same
  name, they are part of the claim/ledger consistency check, and `status` lists
  them under "Resource leases".
- `check` warns about sessions with events from more than one conversation and
  notes sessions named like tests (`test-…`, `tmp-…`, `fixture-…`).
- `HANDOFF.md` (shipped by `init`) explains identity, forks, leases, waiting,
  takeover, resource leases and which STATE parts need which lease.

### Changed

- `log --thread`, `run --thread` and `highlight create` no longer need a claim.
  A thread write is refused only while another session holds a live lease on
  that thread; writers without a lease append normally, without claim/release
  pairs. A thread slug with no file and no events is refused as a probable typo.
- `status` shows each owner as `agent / session since TIME · last activity TIME ·
  lease 12h`, and `(expired N h ago; takeover with --takeover --reason)` once the
  lease has expired.
- `check` reports an expired lease as a `warn:` line instead of failing with
  "older than 12h"; claim files that disagree with the ledger still fail.
- Claiming a thread you already hold records a new claim event only when the
  lease had expired or `--ttl` changes it; otherwise nothing is written.
- `--reason` is accepted with `--force` or `--takeover`.
- The block that `rmem link` writes now tells agents to pass `--session` on every
  call or omit it inside Claude Code or Codex, and to run `session start --parent`
  after a fork, instead of "Use a unique RMEM_SESSION".
- AGENTS.md: a thread's row in the STATE thread index is edited under that
  thread's own lease; only the global STATE sections need `project-state`.
- Event lines show `resource=NAME` for resource leases.

## Step 2: requests, lessons and session resume

### Added

- Requests (`bin/research_handoff.py`). `rmem request --to thread:T|role:R|session:S
  --id ID --note TEXT [--blocking] [--source EVENT] [--quote TEXT] [--user-decision]
  [--thread T]` writes a `request` event. Requests are never refused by a lease, so
  a session can file one in a thread that another session holds. A thread slug
  with no file and no events is refused; a role must be a key of `roles` when
  roles are configured.
- A request marked `--user-decision` must cite the user with `--source EVENT` or
  `--quote "the user's exact words"`; otherwise it is refused.
- `rmem ack ID [--note]` and `rmem close ID --by EVENT|--reason TEXT
  [--status done|withdrawn|rejected]`: open → acked → done, withdrawn or rejected.
  `--by` takes an event ID, its first 8 or more hex digits, or `#SEQUENCE`. Anyone
  may acknowledge or close a request; the event records `relation`
  (`addressee`, `requester` or `other`). A second ack writes nothing; a closed
  request cannot be acknowledged or closed again.
- `rmem inbox [--for ADDRESS] [--all] [-n N]` (read-only) lists open requests with
  their age in days. By default it shows the requests that reach the caller: its
  session, the role it recorded with `session start --role`, the threads it leases
  and the threads its role lists under `roles.ROLE.threads`.
- `rmem status` starts with the open requests addressed to the caller (count and
  up to 5 lines, blocking first), a count of open requests for other addressees, a
  count of decisions by other sessions since the caller's last event, and up to 5
  lessons that apply to it. Without a session it shows the number of open requests.
- `rmem check` warns about each request that has waited longer than
  `requests.ack_warn_days` without an acknowledgment, about malformed
  `roles.ROLE.threads` entries, and about lesson files that are missing or no
  longer match their ledger event.
- Lessons. `rmem lesson add --id ID --rule TEXT --why TEXT [--applies-to
  all|thread:T|role:R] [--source EVENT|--quote TEXT] [--supersedes ID]` writes
  `lessons/<ID>.md` and a `lesson` event with the file's SHA-256. `rmem lesson list
  [--applies-to …] [--all]` hides superseded lessons unless `--all` is given.
  `rmem lesson export --format claude-memory|codex-agents` prints Markdown to paste
  into Claude auto-memory or a Codex AGENTS.md; it never writes those files.
- `rmem pending add --id ID --note TEXT`, `rmem pending close ID --note TEXT
  [--quote TEXT]` and `rmem pending list [--all]` record what a session waits for,
  such as options given to the user.
- `rmem checkpoint [--thread T] --note "next step: …"` appends the next step.
  `checkpoint --auto` (for hooks) writes a short automatic note, skips sessions
  that never wrote to the hub, and never fails.
- `rmem resume [--thread T] [--max-chars N] [-n N] [--hook]` (read-only) prints one
  bounded continuation card (`resume.max_chars`, 6,000 characters by default):
  identity and fork check, open requests, pending items, checkpoints, leases and
  each leased thread's Next step section, decisions by other sessions since the
  caller's last event, the authority entries of MAP.md, lessons, recent events and
  lines from module `resume_sections` hooks. A session started with `--parent`
  also sees its parent's pending items, checkpoints, events and requests. A
  session without events is told how to link a parent and shown the most recent
  sessions.
- `rmem hooks install --claude [--project DIR] [--write]` prints, or with
  `--write` merges into `<project>/.claude/settings.json` (keeping a timestamped
  backup and refusing malformed JSON), SessionStart hooks (`startup`, `resume`,
  `compact`) that run `rmem resume --hook` and PreCompact hooks (`auto`, `manual`)
  that run `rmem checkpoint --auto`. `--hook` reads the hook's JSON input for the
  conversation ID when the hook process lacks `CLAUDE_CODE_SESSION_ID`.
  `rmem hooks install --codex` prints an AGENTS.md snippet.
- Configuration group `resume` (`max_chars`, `recent_events`) and a `threads` list
  inside a `roles` entry (see CONFIG.md).
- Event lines show `to=ADDRESS` for requests.
- HANDOFF.md explains requests, lessons, pending items, checkpoints, resume and
  the hooks, including what the hooks assume about Claude Code.

### Changed

- AGENTS.md: after a compaction, a resume or a fork, run `rmem resume` first;
  record pending items, checkpoints, requests and lessons as they happen; the
  ownership table lists requests and `lessons/`.
- The block that `rmem link` writes tells agents to run `rmem resume` after a
  compaction, a resume or a fork.
- `rmem sync` commits `lessons/*.md` together with the other memory files.
- BOOTSTRAP.md ends with preparing roles, lessons, requests and hooks before
  handing the hub to working sessions.
