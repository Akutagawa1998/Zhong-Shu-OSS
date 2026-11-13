# Sessions, leases and handoff

This file explains how rmem knows which conversation wrote an event, how a
session holds a thread or an outside resource for a while, and how work moves
between sessions. [AGENTS.md](AGENTS.md) has the short rules; this file has the
details and the commands.

## Session identity

Every ledger event names an agent label and a session. The session is what
rmem uses for ownership; the agent label (`claude`, `codex`, `human`, or what
you pass with `--agent`) is descriptive and is compared together with the
session. Neither is a password.

rmem finds the session in this order:

1. `--session ID` on the command line (before or after the command name);
2. the environment variable `RMEM_SESSION`;
3. inside Claude Code or Codex, the conversation's own ID:
   `CLAUDE_CODE_SESSION_ID` becomes `claude-<full conversation ID>` and
   `CODEX_THREAD_ID` becomes `codex-<full thread ID>` (the whole ID, because
   Codex thread IDs begin with a timestamp, so a prefix would be shared by
   conversations started within the same minute). When both are set (Codex
   started from inside Claude Code), the Codex ID is used; `whoami`, `status`
   and `claim` warn about it, because a Claude Code conversation started from a
   shell that inherited `CODEX_THREAD_ID` must pass its own `--session`.

Claude Code and Codex run every tool call in a new shell, so an exported
`RMEM_SESSION` is lost after the call. In those tools, either pass `--session`
on every call or omit it and let rmem derive the session. Never generate a new
ID per command, and never share one ID between two conversations. Parallel
subagents inside one conversation share its harness ID; give each of them an
explicit `--session` of its own. When `CLAUDE_CODE_CHILD_SESSION` is set (a
Claude Code subagent) and no session is given, `whoami`, `status`, `doctor`,
`claim` and `release` warn that the derived session is shared. A conversation
that already wrote under an explicit session before upgrading keeps passing
that session; the optional hooks find it by its recorded conversation ID (see
below).

Every appended event also records `harness_session`, the full conversation ID,
when one is available, and `effort` from `CLAUDE_EFFORT` when that is set.
Values that look malformed or secret are never recorded. The model is not
visible to rmem; record it yourself (below).

### Recording the session

```bash
rmem session start --role writer --model opus --effort high \
    --transcript ~/.claude/projects/PROJECT/CONVERSATION.jsonl
rmem session update --model fable          # after a mid-session model switch
rmem whoami                                # read-only
```

`session start` writes a `session_started` event with the role, model, effort,
parent session and transcript pointer. When `hub-config.json` lists `roles`
(a JSON object whose keys are role names), the role must be one of them;
without configured roles any text is accepted with a warning. `session update`
writes a `session_updated` event; use it whenever the model, effort or role
changes during a conversation, so later readers can attribute each stretch of
work. `whoami` prints the agent and session and where each came from (flag,
environment or derived), the harness conversation ID, the recorded role, model,
effort and parent (or `unknown`), the session's last activity, the fork check
and the leases it holds.

### Forked conversations

A session is bound to the conversation of its `session_started` event, or, if
it never ran `session start`, to the conversation of its first event that
recorded a harness ID. When a different conversation writes under that session
ID (a forked or copied conversation that kept the old `--session`), every write
is refused with a message that says so: claims, logs, runs, notes, highlights
and `session start` itself. `status` shows the same warning at the top.

To continue after a fork, use a new session and link the old one:

```bash
rmem session start --parent claude-00000000-0000-4000-8000-000000000001   # omit --session to derive this conversation's own ID
```

The refusal names both full conversation IDs. When the refused session is the one
derived for this conversation (someone used it explicitly elsewhere), pass an
explicit new `--session` instead of omitting it.

Writes from processes without a harness ID (people at a terminal, scripts,
cron jobs) are not checked. `rmem check` warns when a session already has events
from more than one conversation (for example after merging another clone), and
notes sessions named like tests (`test-…`, `tmp-…`, `fixture-…`), which belong in
a temporary hub set with `RMEM_HUB`.

## Leases

A claim is a lease: it says that one session is working on a thread (or on an
outside resource) and asks others not to write there for now.

- `rmem claim SLUG [--ttl HOURS]` takes a lease and creates `threads/SLUG.md`
  if it does not exist. The default length is `leases.default_ttl_hours` in
  `hub-config.json` (12 hours). The claim event and `claims/SLUG/meta.json`
  record `lease_ttl_hours`.
- A lease stays live while the holder session keeps writing ledger events, of
  any kind, with no gap longer than its TTL since the claim. Writing anything
  renews it; there are no heartbeat or renewal events. The holder's own
  `rmem run` on the leased thread counts as activity for as long as it runs, so
  a run longer than the TTL keeps the lease (an unfinished run that was killed
  keeps it too; recover with `--force --reason` after checking the process).
- Expiry is sticky: once a gap exceeds the TTL, the lease stays expired even if
  the holder writes again later, because other sessions may have written to the
  thread in between. The holder's next write to the thread prints a warning that
  names them, and `rmem resume` lists them; claiming again with `rmem claim SLUG`
  (or with a new `--ttl`) records a new claim event by the same owner and starts
  a new lease.
- `rmem release SLUG` ends it. Release finished work; a lease you no longer
  need blocks others until it expires. While your own `rmem run` on the thread
  is unfinished, release is refused; another session's unfinished run does not
  block it.

### Writing without a lease

`log --thread T`, `run --thread T` and `highlight create` (thread
`project-state`) need no claim. Each write is checked inside the hub lock and is
refused only while **another** session holds a live lease on that thread. A
writer that holds no lease appends normally, without claim and release events.
Requests (`rmem request`, below) are never refused by a lease. A session that
started `rmem run --id E7 --thread T` before another session claimed T may still
record that run's outcome (`rmem log --id E7 --thread T …`); `claim` warns when
the thread has another session's unfinished run. A thread slug
that has no file and no events is refused as a probable typo; create a new
thread with `rmem claim SLUG` (and release it if you need no lease).
`project-state`, the protocol's own thread, is always accepted, so
`highlight create` works on a fresh hub.

### Waiting, taking over and forcing

| Situation | Command |
|---|---|
| another session holds a live lease and you can wait | `rmem claim SLUG --wait SECONDS` |
| the lease expired and its owner is gone | `rmem claim SLUG --takeover --reason TEXT` |
| you can wait for it to be released or to expire, then take it over | `rmem claim SLUG --wait SECONDS --takeover --reason TEXT` |
| the claim is invalid or inconsistent, or the owner confirmed it stopped while the lease is still live | `rmem claim SLUG --force --reason TEXT` (see [CONSISTENCY.md](CONSISTENCY.md)) |

`--wait` releases the hub lock between polls and polls only the small claim
file, not the ledger. It acquires the lease as soon as it is released. If the
lease expires while you wait, it takes it over when you also passed
`--takeover --reason`, and otherwise stops with the holder and its last
activity. On timeout it fails with the same information.

`--takeover` works only on an expired lease, and not while the thread has an
unfinished `rmem run`; check the old owner's jobs first. The claim event records
`takeover`, `previous_owner`, `reason`, `previous_last_activity` and
`previous_lease_expired_at`, and the consistency check treats it like a forced
recovery.

`status` shows each thread's owner as `agent / session since TIME · last
activity TIME · lease 12h`, with `(expired N h ago; takeover with --takeover
--reason)` once the lease has expired. `check` reports an expired lease as a
`warn:` line; claim files that disagree with the ledger still fail.

### Resource leases

Something outside the hub, such as a section of a shared document or an
external guide, can be leased the same way:

```bash
rmem claim --resource alice-guide.s8 --ttl 2 --note "editing section 8"
rmem release --resource alice-guide.s8
```

Resource leases live in `claims/@NAME/` and their events carry `resource`
instead of `thread`, so a resource never collides with a thread of the same
name. They follow the same TTL, `--wait`, `--takeover` and `--force` rules, and
the same claim/ledger consistency check. `status` lists them under "Resource
leases". A lease only coordinates sessions that ask rmem; it cannot stop an
editor or a push that ignores it.

## Shared files

STATE has two parts. Each thread's row in the STATE thread index is edited under
that thread's own lease. Only the global STATE sections (goal, constraints,
settled decisions, open questions), MAP, `findings/`, `records/` and
`highlights/` need the `project-state` lease. This keeps `project-state` from
becoming a queue that every session waits for.

## Requests

A request asks another thread, role or session for something and stays visible
until it is answered. Free-text notes such as "REQUEST TO …" inside a thread are
easy to miss after a compaction or a fork; a request has an addressee, an ID and
a state.

```bash
rmem request --to role:exec-7b --id REQ-7B-E3 --blocking \
    --note "rerun E3 with seed 2 before the next stage"
rmem request --to thread:internalization-7b --id REQ-DOSE --note "add the dose sweep" \
    --user-decision --quote "do the dose sweep before the paper figures"
rmem inbox                      # open requests for this session, its role and its threads
rmem ack REQ-7B-E3 --note "queued after the current run"
rmem close REQ-7B-E3 --by '#3512'                  # the event that did the work
rmem close REQ-DOSE --reason "superseded by REQ-DOSE2" --status withdrawn
```

- Addresses are `thread:SLUG`, `role:ROLE` (a key of `roles` in
  `hub-config.json` when roles are configured) and `session:SESSION`.
- A request reaches a session when it names the session, the role the session
  recorded with `rmem session start --role`, a thread it leases, a thread it
  wrote to (claims and releases aside) since its latest `session start` or within `requests.ack_warn_days`
  (logging needs no lease, so a lease alone does not say who works there), or a
  thread its role lists under `roles.ROLE.threads` (see [CONFIG.md](CONFIG.md)).
  A session started with `--parent` is also reached by what reaches its parent,
  in `status`, `inbox` and `resume` alike. A request a session sent itself to a
  thread is for whoever works there next and is not listed as its own task.
- States: open → acked → done, withdrawn or rejected. `rmem ack ID` acknowledges
  (once; a second ack writes nothing). `rmem close ID` needs `--by EVENT`, the
  event that completed it (an event ID, its first 8 or more hex digits, or
  `#SEQUENCE` as `rmem recent` shows it; quote `'#42'` in the shell), or
  `--reason TEXT`, which is recorded as the note. `--status` defaults to `done`.
- Anyone may acknowledge or close a request. The event records `relation`:
  `addressee`, `requester` or `other`, and rmem prints a note for `other`.
- A request may name a thread that another session leases: requests are never
  refused by a lease. With `--to thread:T` the request is filed in T (so
  `rmem recent --thread T` shows it); `--thread T` files it elsewhere. A thread
  slug with no file and no events is refused as a probable typo.
- A request that carries a user decision (`--user-decision`) must cite the user:
  `--source EVENT` names the event that recorded the decision, `--quote` keeps
  the user's exact words. Without either it is refused, so a session's own
  proposal is never recorded as the user's division of work.
- Request IDs share the event `id` field, so `rmem show REQ-7B-E3` lists a
  request's history. An ID already used by an experiment or another kind of event
  is refused.
- `rmem status` starts with the open requests addressed to the caller (count
  and up to 5 lines, blocking first) and a count of open requests for others.
  `rmem resume` shows them too. `rmem check` warns about each open request that
  nobody acknowledged within `requests.ack_warn_days` (2 days by default).

## Lessons

A lesson is one collaboration rule and the reason for it: how to commit, which
clock to quote, which session drives which jobs. Rules like these used to live
in one agent's private memory, where other agents and later sessions could not
see them. A lesson lives in the hub, where every agent reads it.

```bash
rmem lesson add --id L-commits --rule "One Git commit per ledger event, with the event's time" \
    --why "history stays bisectable and in time order" --quote "commit each event separately"
rmem lesson add --id L-torch-clock --applies-to role:exec-7b \
    --rule "Copy log timestamps from the cluster's date output" --why "estimates were wrong twice"
rmem lesson add --id L-commits-2 --supersedes L-commits --rule "…" --why "…"
rmem lesson list [--applies-to thread:T|role:R|all] [--all]
rmem lesson export --format claude-memory      # or codex-agents
```

- `lesson add` writes `lessons/<ID>.md` (rule, reason, scope, source) and a
  `lesson` event that records the file's SHA-256. The ledger event is the source
  of truth: change a lesson by adding a new one with `--supersedes OLD`. A
  superseded lesson is hidden from `list` and `status` but kept; `list --all`
  shows it. `rmem check` warns when a lesson file is missing or edited.
- `--applies-to` is `all` (default), `thread:SLUG` or `role:ROLE`. `status`
  shows at most 5 lesson titles that apply to the caller (lessons for all, its
  role and its threads); `resume` shows up to 8.
- `--source EVENT` or `--quote` records where the rule came from.
- `lesson export` prints Markdown and never writes a file. `claude-memory` is
  one feedback-style memory file (front matter with `name`, `description` and
  `type: feedback`, then each rule with **Why:** and **How to apply:** lines),
  as far as we know the layout of Claude Code auto-memory; paste it into one
  memory file. `codex-agents` is an AGENTS.md section. Re-export after a change
  instead of editing the pasted copy.

## Pending items and checkpoints

Some state belongs to one conversation and is lost in a compaction: the options
you just gave the user, a question you are waiting on, the next step you were
about to take.

```bash
rmem pending add --id P-seeds --note "offered the user A) seed 2 only, B) seeds 2 and 3, C) wait"
rmem pending close P-seeds --note "the user chose B" --quote "B, and keep seed 2 as the default"
rmem pending list [--all]
rmem checkpoint --note "next step: compare E4 with E3 on the native split"
rmem checkpoint --thread internalization-7b --note "next step: …"
```

- A pending item stays open until `pending close`; its ID can be reused after
  that. Close it with the user's exact words in `--quote` when the answer is a
  decision: other sessions see a quoted close in their "decisions" list.
- A checkpoint is an append-only event with one next step. `--thread T` files it
  in T and is refused while another session holds a live lease on T, like a log.
  Writing a checkpoint, like any event, keeps your leases live.
- `checkpoint --auto` is meant for hooks: it writes a short automatic note,
  writes nothing for a session that has not written to this hub yet (other
  conversations in the same project), and never fails (a problem is printed and
  the exit code is 0).

## Resuming after compaction, a resume or a fork

Run `rmem resume` first. It is read-only and prints one bounded card
(`resume.max_chars`, 6,000 characters by default; `--max-chars N`):

1. identity: role, model, effort, parent, start, last activity and the fork check;
   for a session without events, a hint to run `session start --parent` and the
   most recent sessions;
2. open requests addressed to it (blocking first);
3. its open pending items;
4. its latest checkpoints;
5. its leases with last activity and expiry, and the Next step section of each
   leased thread (and of `--thread T`);
6. decisions by other sessions since its last event (`decision` events, closes of
   pending items with a quote, and requests that carry a user decision);
7. the authority entries of MAP.md (an "Authority" or "controller" section, or
   table rows that name one), never the whole file;
8. lessons that apply to it;
9. its recent events (`resume.recent_events`, `-n N`) and, with `--thread T`,
   that thread's latest events;
10. lines that feature modules and plugins add through `resume_sections`.

The sections run from most to least urgent, and the card is cut at its budget.
A forked session that ran `rmem session start --parent OLD` also sees the
parent's pending items, checkpoints and recent events, and requests addressed to
the parent. Then read only the threads and evidence that the next step needs.

## Optional hooks

Hooks make resume and checkpoint automatic. They are optional, and rmem writes a
settings file only with `--write`.

```bash
rmem hooks install --claude [--project DIR]          # prints what it would merge
rmem hooks install --claude [--project DIR] --write  # merges, keeping a backup
rmem hooks install --codex                           # prints an AGENTS.md snippet
```

For Claude Code, rmem merges into `<project>/.claude/settings.local.json` (the
working directory by default, never the hub). That is the per-user, per-device
settings file, which Claude Code keeps out of Git: the hook commands hold this
device's absolute hub path, which is per device for the same reason `.rmem` is
(see GIT.md). Install the hooks once on each device and checkout:

- `SessionStart` hooks with the matchers `startup`, `resume` and `compact`
  that run `RMEM_HUB=<hub> <hub>/bin/rmem resume --hook`;
- `PreCompact` hooks with the matchers `auto` and `manual` that run
  `RMEM_HUB=<hub> <hub>/bin/rmem checkpoint --auto`.

Existing settings and hooks are kept; a hook already present is not added twice;
the old file is copied to `settings.local.json.rmem-backup-<UTC time>` first. A file
that is not valid JSON, or whose `hooks` entries have an unexpected shape, is
refused and left unchanged. To turn the hooks off, delete those entries.

This relies on what we know of Claude Code hooks; check it against your version
with `/hooks` after installing:

- the settings shape `{"hooks": {"SessionStart": [{"matcher": "compact",
  "hooks": [{"type": "command", "command": "…"}]}], "PreCompact": [...]}}`;
- `SessionStart` runs on a new conversation, a resumed one and after a
  compaction (sources `startup`, `resume`, `compact`), and its standard output
  is added to the model's context;
- `PreCompact` runs before a compaction (`auto` or `manual`);
- the hook receives a JSON object on standard input with `session_id`,
  `hook_event_name` and `source` or `trigger`.

`--hook` (and `checkpoint --auto`) read that JSON for at most two seconds. When
the hook process has no `CLAUDE_CODE_SESSION_ID`, rmem takes the conversation ID
from `session_id`. The hooks pass no `--session`, so they start from the derived
session (`claude-<conversation ID>`). When that session has no events but the
conversation wrote under an explicit `--session` (every event records the full
conversation ID), the card and the automatic checkpoint use that explicit
session instead, the most recently active one if there are several; the card
says so and names the others. `resume` without `--hook` does the same.
`resume --hook` prints a short message instead of failing, so a broken hub never
blocks a session. A new conversation that is not a fork also gets the card; it
then says the session has no events yet.

Codex has no session-start hook that rmem relies on. `hooks install --codex`
prints an AGENTS.md section that tells the agent to run `rmem resume` at the
start of a session and after a compaction, a resume or a fork, and to write a
checkpoint before a long wait. Paste it yourself; rmem never writes AGENTS.md.
