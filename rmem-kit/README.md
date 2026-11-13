# rmem-kit — shared research memory

Python 3.10+, standard library, macOS/Linux local filesystem. One hub per project;
multiple workspaces can point at the same hub. A hub can live inside your research
repository. The distribution stays empty and reusable.

## Install a project hub

```bash
python3 /path/to/Zhong-Shu/rmem-kit/bin/rmem init /path/to/project/.research-memory --project-name MyProject
cd /path/to/project/.research-memory
python3 bin/rmem link ..
python3 bin/rmem check
```

Initialization refuses a nonempty destination. Linking preserves existing
`AGENTS.md` and `CLAUDE.md` content and writes a managed block plus a relative
`.rmem` pointer. It refuses a different existing hub unless `link --replace` is
explicit. Relative links survive moving the whole project together. If you move
only the hub or one linked folder, relink that folder. Quote paths with spaces.
For Git worktrees or several checkouts of one project, use `link --canonical
CHECKOUT`: it writes this device's absolute hub path into an untracked `.rmem`,
so every checkout writes to the one canonical hub (see [GIT.md](GIT.md)).
Without `RMEM_HUB`, rmem looks for a `.rmem` pointer or a hub from the working
directory upward, and stops at a directory that another user owns or can write
to (such as `/tmp`), so a planted hub is never used; a `.rmem` pointer must be
yours and not writable by others.

Before linking a legacy workspace, inspect its active instruction files. Linking
preserves them; it does not compact old instructions or reconcile contradictions.
An `AGENTS.override.md` can hide `AGENTS.md`, and a large inherited instruction
chain can omit the appended memory block. Codex documents a default combined
32 KiB limit. Keep the new project's entry instructions short, archive history as
evidence, and verify the loaded instructions in a fresh session. See
[instruction discovery](https://developers.openai.com/codex/guides/agents-md).

Run the copied CLI inside the project; no global PATH, shell-profile change,
account setting, or Git initialization is required. To share an external hub,
start both tools with access to that one directory:

```bash
codex --add-dir /absolute/path/to/hub
claude --add-dir /absolute/path/to/hub
```

Both flags were confirmed with the installed CLIs. Extra-directory access does
not override protected paths or managed policy; place live memory under an
ordinary directory, not `.codex` or `.agents`. See the official
[Codex permissions documentation](https://developers.openai.com/codex/agent-approvals-security)
and [Claude settings documentation](https://code.claude.com/docs/en/settings).
Do not add a blanket command allowlist for `rmem run`: it runs arbitrary commands.
Read commands work without write access to the hub (see "Diagnose access with
doctor" below); `rmem doctor` prints the setting your harness needs for writes.

Claude's `CLAUDE.md` imports `@AGENTS.md`, the documented way to share instructions
with Codex. Verify the import appears in Claude's `/context`; existing large
instruction files still consume context. See
[Claude memory documentation](https://code.claude.com/docs/en/memory).

## Optional skill discovery

The [SKILL.md](SKILL.md) entry point works directly, or install this whole package
as `rmem-kit` under a tool's skill directory. Keep its supporting files together.
Codex supports `.agents/skills/rmem-kit` and symlinked skill folders; see
[official skill documentation](https://developers.openai.com/codex/skills).
In Claude Code use `.claude/skills/rmem-kit`; see
[Claude skill documentation](https://code.claude.com/docs/en/skills).
Linked project instructions work without skill installation. An installed skill
must initialize a project hub rather than accumulate live state in itself.

## Start and hand off a thread

Use one session value for the entire conversation. Shell exports do not persist
between Claude Code or Codex tool calls, so pass `--session` on every call, or
omit it inside Claude Code or Codex: rmem then derives `claude-<conversation ID>`
or `codex-<thread ID>` from the full conversation ID and records that ID on every
event. A subagent or parallel worker inside one conversation shares the
conversation ID, so it passes its own `--session`.
`--agent` describes the tool and is checked alongside the session when validating
ownership; neither is an authentication credential. `--agent` and `--session` may
come before or after the command name for every command, including after
`highlight create` and `recent` (`recent --from-session S` filters by session).

```bash
python3 .research-memory/bin/rmem status
python3 .research-memory/bin/rmem --agent codex --session codex-analysis-unique123 session start --role analysis --model gpt-5   # optional: role, model, parent
python3 .research-memory/bin/rmem --agent codex --session codex-analysis-unique123 claim baseline --ttl 4
python3 .research-memory/bin/rmem --agent codex --session codex-analysis-unique123 run --id E042 --thread baseline -- python train.py
python3 .research-memory/bin/rmem --agent codex --session codex-analysis-unique123 log --id E042 --thread baseline --status inconclusive --metric acc=0.83 --note 'Need matched seed comparison before retaining.'
python3 .research-memory/bin/rmem --agent codex --session codex-analysis-unique123 release baseline
python3 .research-memory/bin/rmem --agent claude --session claude-review-unique456 claim baseline --wait 600
```

Environment alternatives: `RMEM_AGENT`, `RMEM_SESSION`, and `RMEM_HUB`. Outside
Claude Code and Codex, generate a session ID once per conversation. Do not
generate a new one per command or reuse an identity in a second conversation: a
forked conversation that keeps the old session is refused until it starts its own
(`rmem session start --parent OLD`). `rmem whoami` shows which identity is in use.
To work concurrently, use different threads and separate code worktrees.
Leases protect cooperative memory writers, not shared source-code edits.

A claim is a lease. It stays live while its session writes any ledger event
within the TTL of the previous one (`leases.default_ttl_hours`, default 12 hours,
or `--ttl`); once expired it stays expired until the holder claims again.
`log --thread` and `run --thread` need no claim; they are refused only while
another session holds a live lease on that thread. `claim --wait SECONDS` waits
for a lease to end; an expired lease is shown in `status` and can be taken over
with `claim --takeover --reason '...'` after checking the old owner's jobs.
`claim --resource NAME` leases something outside the hub, such as a shared
document section. See [HANDOFF.md](HANDOFF.md).

Before release, update `threads/baseline.md` with results, evidence references,
dead ends, and a concrete next step. Global STATE sections and shared documents
take the `project-state` lease; a thread's own STATE row takes that thread's lease
(see [the protocol](AGENTS.md)). Live leases are never automatically stolen.
Inspect owner and jobs first; recovery of a live or broken claim uses
`--force --reason '...'`.

Claim files are ignored by Git but ownership events are tracked. Ordinary writes
now compare both before proceeding, and new claim metadata carries its exact event
UUID. After stash/checkout/restore, inspect any `status`/`check` mismatch and the
intended history; do not assume an existing claim file is a valid grant. Follow
[CONSISTENCY.md](CONSISTENCY.md) for deliberate recovery and legacy compatibility.

## Requests, lessons and resuming

Sessions hand work to each other with requests instead of free-text notes:

```bash
python3 .research-memory/bin/rmem request --to role:reviewer --id REQ-E42 --blocking --note 'Check the E42 seed match'
python3 .research-memory/bin/rmem inbox                  # open requests for this session, its role and threads
python3 .research-memory/bin/rmem ack REQ-E42
python3 .research-memory/bin/rmem close REQ-E42 --by '#118'   # the event that did the work
```

A request is addressed to `thread:SLUG`, `role:ROLE` or `session:SESSION`, moves
from open to acked to done, withdrawn or rejected, and may be filed in a thread
that another session leases. Open requests lead `status` for their addressee;
`check` warns when one waits longer than `requests.ack_warn_days` for an ack. A
request that passes on a user decision must cite it (`--source EVENT` or
`--quote`). Collaboration rules go into `rmem lesson add --rule … --why …`
(`lessons/<ID>.md`), which `status` lists and `lesson export` prints for Claude
auto-memory or a Codex AGENTS.md.

Within a conversation, `rmem pending add|close` records what you wait for (such as
options given to the user) and `rmem checkpoint --note 'next step: …'` the next
step. After a compaction, a resume or a fork, `rmem resume` prints one bounded
card (6,000 characters by default) with identity, requests, pending items,
checkpoints, leases and their next steps, new decisions by others, lessons and
MAP authority entries. `rmem hooks install --claude --write` optionally makes
Claude Code run it at session start and write a checkpoint before compaction; it
is a dry run without `--write`. See [HANDOFF.md](HANDOFF.md).

## Record evidence and recover context

`status`, `recent`, and `run` output is bounded by characters as well as lines.
Full logs stay in `runs/`; the CLI digests them incrementally after a foreground
command exits. A successful exit records execution success, not research validity.
Use `recent --thread baseline --grep 'E042' -n 5` for relevant events.

### Bounded queries

Every query has an output budget, and each line shows its time, `#sequence` and
`s=session`. If you find yourself parsing `ledger.jsonl` with your own script,
the CLI is missing something: report it rather than loading the ledger. Below,
`rmem` stands for `python3 .research-memory/bin/rmem`.

```bash
rmem recent --since 6h                        # also '2026-10-14 09:00' (display zone) or ISO with offset
rmem recent --after-seq 3400 -n 50            # events after #3400, oldest first; ends with the next page
rmem recent --kind exp,finding --other-sessions
rmem recent --from-session codex-analysis-unique123 --until 3d
rmem show E042 --field note                   # one field, completely, in pages
rmem show E042 --field research --page 2 --format text
rmem show EVENT_UUID --body                   # the --note-file text of that event
rmem catalog --query seed --query relearn --regex 'E04[0-9]'
rmem export --from-seq 3000 --to-seq 3200 --out /tmp/ledger-slice.jsonl
```

- `recent` lists the newest 20 matching events (`-n` up to 200; a larger value is
  reduced with a notice). `claim`/`release` events are hidden unless you pass
  `--include-locks` or ask for them with `--kind`; a closing note counts them.
  `status` hides them the same way (`status --include-locks` shows them).
- `--since`/`--until` compare ingestion time. `--after-seq N` pages forward in
  ledger order from the event numbered N; without it, a note says how many older
  matches were left out and which option reads them from the start. Legacy rows
  without a stored sequence show their ledger position, which `--after-seq` also
  uses. When stored sequence numbers repeat or go backward (after a Git merge or
  `repair --conflict-markers`), the next-page note gives `--after-pos K`, a ledger
  line position, so no event is skipped. A query that matches nothing says so.
- `--from-session S` shows only that session's events; `--other-sessions` hides
  your own. `--session` after `recent` is your identity, as for every command.
- `export --from-seq/--to-seq` select by the stored sequence number, not by
  ledger order; after a merge, export by time or thread instead.
- `show ID --field NAME` pages one field of the latest event for an ID (or of an
  exact event UUID): `note`, `body`, `cmd`, `research`, `sources`, `metrics`, a
  dotted path such as `research.recipe`, or `event` for the whole event. Pages are
  4,000 characters by default (`--page-size` 200 to 8,000); the JSON view gives
  `offset`, `length` and the `next` command, and the pages concatenate to the
  exact text. `--format text` prints the page as plain text instead.
- `catalog` takes repeated `--query` texts and one `--regex`; they are
  alternatives (OR) that share one `-n` and one output budget, and each row
  names what matched it in `matched_by`.
- `export` writes the selected events (`--from-seq`, `--to-seq`, `--since`,
  `--until`, `--kind`, `--thread`) as one JSON object per line to a new file that
  you page with your own tools. It refuses to overwrite without `--force` and
  refuses paths inside the hub except under `runs/`. Legacy rows carry their
  ledger position as `derived_sequence`.

Start with the project's short entry, registered active context and hub status.
Then follow metadata index → relevant thread → `catalog` / `show` → exact cited
evidence. Use `catalog --query E042 -n 5`, followed by `show E042 --section record`
or `--section sources`, to recover a recipe, progress, findings and artifact
pointers. Disclose missing records rather than loading the whole repository.
Queries do not scan repository contents or remote hosts. The latest correction
stays visible when a query matched an earlier claim.

The optional [generic context reader](INDEXED_READING.md) queries an explicit
metadata index and reads only exact registered file slices. Unindexed inputs are
refused by that reader; no OS isolation or restriction on other tools is implied.

`check` caps the lines and characters (not bytes) of STATE and of each thread,
fails above a cap, and warns from the soft ratio on; `status` prints the STATE
budget and each thread's share. The caps are set in `hub-config.json`; see
[CONFIG.md](CONFIG.md) for the defaults. Aim for STATE around 3,000 characters and
active threads around 1,000–3,000. These size limits are separate from query
output caps. Preserve readable prose, normal spaces and paragraphs; expand
unfamiliar abbreviations. Move detail behind indexed pointers instead of deleting
spaces or dropping failures and corrections.

`show ORIGINAL_SOURCE_ID` also finds a historical import when no direct event or
experiment ID matches. It labels the original source ID separately from the new
ingestion event UUID; it does not replay historical ownership or acceptance.
If multiple source namespaces collide, use `catalog --updates --query
ORIGINAL_SOURCE_ID` (or query its namespace), then select the exact ingestion
event UUID shown with that source identity.

For new research updates, fill [RECORD_TEMPLATE.json](RECORD_TEMPLATE.json) and
use `log --thread baseline --record /path/to/record.json` (no claim needed unless
another session holds a live lease on the thread).
UUID and append sequence identify updates; `steps` records optimizer updates,
evaluation calls, or checks. Unknown fields stay `null` with explicit `gaps`.
See [RECORDS.md](RECORDS.md) for the schema, source locations, reviewed historical
cards, and optional `check --records --require-records-since TIMESTAMP` enforcement.
No existing project history is assumed to be complete or automatically migrated.

Runs record the full starting Git commit and whether the worktree was dirty.
A hub inside that repository is left out of the dirty check, so memory writes do
not mark the code dirty. This is not a snapshot of uncommitted code: commit or freeze the actual code and
configuration before a reproducibility-sensitive run, and cite those files with
`log --source`. A Git status hash alone does not identify dirty file contents.

For existing research follow [BOOTSTRAP.md](BOOTSTRAP.md). Import preserves the
original identity, never substitutes the importer's current Git commit, and hashes
the evidence files:

```bash
python3 .research-memory/bin/rmem --agent codex --session import-unique123 log --kind import --source-namespace legacy-project --source-id experiment-D12-readout --id D12 --status inconclusive --source evidence/readout.md --evidence-level reported --note 'Historical summary; raw metrics not yet checked.'
```

The same source identity, evidence bytes, and event content import once, including
after a project move. An identical retry keeps the original provenance receipt;
record the new locations in MAP rather than rewriting history. Conflicting
evidence under that identity is rejected; append a separately identified correction
referencing the earlier event. `--event-time` preserves the source timestamp separately from the
import timestamp; `--supersedes` references an earlier event or source identity.
`--source` may repeat for configs, readouts, data manifests, or scripts.
Hashes establish which bytes were read, not that their scientific claims are true.
`--artifact` stores a pointer; it does not copy or verify a remote artifact.

## Long jobs and remote hosts

The CLI is not a scheduler. Use the project's established Slurm/tmux/job workflow,
then log its real job ID, host, scheduler, configuration sources, and artifacts.
For example, with your normal session identity:

```bash
python3 .research-memory/bin/rmem --agent codex --session codex-analysis-unique123 log --status running --job-id 123 --scheduler slurm --host cluster --id E043 --thread baseline --artifact cluster:/path/run.log --note 'Submitted; not evaluated'
```

To keep the exact command, record it when you submit it; `capture` runs nothing:

```bash
python3 .research-memory/bin/rmem --agent codex --session codex-analysis-unique123 capture --id E043 --thread baseline --host cluster --job-id 123 --scheduler slurm --workdir /scratch/e043 --code-ref cluster:/scratch/code@8566a1d -- sbatch --array=0-3 train.sh
```

`log` and `capture` also take `--cmd`/`--cmd-file`, `--workdir`, `--code-ref`,
repeatable `--set KEY=VALUE` and `--no-provenance`; long text goes to
`--note-file` while `--note` stays a one-line summary. See
[RECORDS.md](RECORDS.md#provenance-fields).

Later append the observed terminal result and readout. A missing completion event
means unknown, not failed or successful. Never relaunch because context was compacted.

Use one writer host for the canonical hub. Remote workers produce artifacts;
the coordinator imports readouts. Claims use local filesystem locks; Git clones,
network filesystems, and `merge=union` cannot provide distributed ownership or
resolve competing conclusions. Claims, locks, and raw runs are ignored by Git.
After restoring a reviewed snapshot into a new location, the CLI recreates the
empty ignored `claims/` runtime directory. Missing memory directories still fail
validation. A restored copy is not a second concurrent authority; link workspaces
to it only after an intentional handoff.
Keep artifact backups separately.

## Milestone and version highlights

Use [HIGHLIGHTS.md](HIGHLIGHTS.md) and `HIGHLIGHT_TEMPLATE.json` to freeze selected
small, reviewed text files with their exact hashes and ledger cutoff, respecting
the `project-state` lease. `highlight list` supports time/text lookup, `show` preserves
later correction links, and `verify` checks bytes and the ledger anchor.
`highlight restore` writes only a separate historical review directory; it never
rolls back the live hub or operational permissions. Capture requires a Git commit
and explicit review for secret material. No remote artifact is fetched or backed up.
Since 0.4.0 a highlight records line digests instead of copying the ledger
(`--freeze-ledger` keeps the copy), and `highlight index --write` keeps a readable
`HIGHLIGHTS_INDEX.md`.

## Version and kit manifest

`rmem version` prints the running kit version (from `VERSION`), the version and
install time recorded in the hub's `.kit-manifest.json`, and the kit files that
changed since install. `init` writes that manifest with a SHA-256 for every file
it copied from the kit (not STATE, MAP, the ledger or `hub-config.json`). `check`
warns about each locally changed kit file, and notes when a hub has no manifest
because it was installed before 0.4.0. Keep project changes in `hub-config.json`
or `bin/plugins/` so kit files stay replaceable.

## Thread lifecycle

Finished, long or outdated threads have commands instead of hand rewrites; see
[LIFECYCLE.md](LIFECYCLE.md). `thread seal SLUG --summary TEXT` hides a finished
thread from `status` (`status --all` lists it; `thread reopen SLUG` undoes it).
`thread archive SLUG --summary TEXT --apply` keeps the thread's exact bytes under
`history/threads/` and leaves a pointer page; `thread roll SLUG --keep N --apply`
keeps the last N paragraph blocks of each section and archives the rest. Without
`--apply` both are dry runs. `check` warns when many ledger events about a thread
follow its last edit (`staleness.events_after_edit`). Artifact routes follow
registered multi-hop moves (A → B → C) with cycle detection.

## Upgrade an installed hub

`rmem upgrade --from /path/to/Zhong-Shu/rmem-kit` (run with the new kit's own
`bin/rmem`) is a dry run that classifies every kit file: unchanged since install
and changed in the kit (update), new (add), locally modified (conflict, never
overwritten). `--apply` copies the updates, creates `bin/plugins/` and a missing
`hub-config.json`, rewrites `.kit-manifest.json` and records a `kit_upgraded`
event; it never touches STATE, MAP, the ledger, threads, findings or other
project data. Hubs installed before 0.4.0 are recognized through the hashes of
released kit versions in `KIT_HISTORY.json`. See [UPGRADE.md](UPGRADE.md).

## Configuration

Caps, the display time zone and settings for later features live in the
optional `hub-config.json` (written by `init` from `HUB_CONFIG_TEMPLATE.json`).
Caps count characters. Invalid values stop `rmem` with an error that names the
key; unknown keys are `warn:` lines in `check`. Times are stored in UTC and shown
in `display_tz` (or `RMEM_TZ`, or the system zone), for example
`2026-10-14 01:34 PDT #42 | codex s=session-id | exp | ...`. See
[CONFIG.md](CONFIG.md) for every key.

## Plugins

Project-specific commands, status lines, checks and event kinds go in
`bin/plugins/<name>.py`; [bin/plugins/README.md](bin/plugins/README.md) describes
the contract. To enable plugins, review their source and set
`RMEM_TRUSTED_PLUGIN_HUB` to the absolute path of that hub in your environment.
Plugins run with your permissions, including during read commands.

## Diagnose access with doctor

`rmem doctor` is read-only. It reports the Python and kit versions, which hub
was found and how (`RMEM_HUB`, a `.rmem` pointer or the working directory), read
and write access to the hub, ledger, lock file, `threads/` and `claims/`, the
identity sources present (values of harness variables are never printed), Git,
configuration problems, broken plugins, and a hint for the detected harness:
make the hub a Codex writable root (`codex --add-dir HUB`) rather than approving
every command, or allow the hub's `bin/rmem` in Claude Code permissions with one
rule per everyday command, written in the form agents run it from the working
directory (the relative path of the managed block, or this device's absolute path
for a canonical link), with `--session` after the command name; `run` is never
in that list.

When the hub is not writable, `status`, `recent`, `catalog`, `show`, `highlight
list|show|verify`, `check`, `version` and `doctor` still run: they read without
the lock, print one note line, and skip a final ledger line that is still being
appended. Writes fail with a message that points to `rmem doctor`.

## Checks, history, and limits

`check` validates structural integrity and memory budgets, not scientific validity,
source availability, or all pending jobs. Consolidate settled evidence into
`findings/` without deleting ledger events. Run `check` before a handoff, then
commit memory. `sync` is an optional memory-only commit helper; it never
pushes/pulls and reports Git failures. For the default hub inside a research
repository, `sync --embedded [--per-event]` commits only the hub files without
touching your index or working tree (one commit per new event, dated by the
event, with `--per-event`); plain `sync` still serves a hub that is its own
repository. `check` reports conflict markers, order inversions, uncommitted
events, a hub inside a linked worktree and highlights that do not verify;
`repair --conflict-markers` and `guard install` help with the first two. See
[GIT.md](GIT.md). Do not use `sync` while another agent is editing shared files.

Model context is not automatically cleared. Use short recovery views and explicit
checkpoints. The CLI is for cooperating agents, not hostile writers. Project
permissions, operating mode, and live host/job checks remain authoritative.
