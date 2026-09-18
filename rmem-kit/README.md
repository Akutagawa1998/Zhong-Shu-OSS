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

Use one unique session value for the entire conversation. Reuse it in every tool
call; shell exports may not persist between agent tool invocations. `--agent`
describes the tool; it is not the lock identity. Global options precede the command.

```bash
python3 .research-memory/bin/rmem status
python3 .research-memory/bin/rmem --agent codex --session codex-analysis-unique123 claim baseline
python3 .research-memory/bin/rmem --agent codex --session codex-analysis-unique123 run --id E042 --thread baseline -- python train.py
python3 .research-memory/bin/rmem --agent codex --session codex-analysis-unique123 log --id E042 --thread baseline --status inconclusive --metric acc=0.83 --note 'Need matched seed comparison before retaining.'
python3 .research-memory/bin/rmem --agent codex --session codex-analysis-unique123 release baseline
python3 .research-memory/bin/rmem --agent claude --session claude-review-unique456 claim baseline
```

Environment alternatives: `RMEM_AGENT`, `RMEM_SESSION`, and `RMEM_HUB`. Generate a
session UUID once if no stable tool conversation ID is available. Do not generate
a new one per command or reuse an identity in a second concurrent session.
To work concurrently, use different claimed threads and separate code worktrees.
Claims protect cooperative memory writers, not shared source-code edits.

Before release, update `threads/baseline.md` with results, evidence references,
dead ends, and a concrete next step. Shared documents require the `project-state`
claim; see [the protocol](AGENTS.md). Stale claims are never automatically stolen.
Inspect owner and jobs first; explicit recovery uses `--force --reason '...'`.

## Record evidence and recover context

`status`, `recent`, and `run` output is bounded by characters as well as lines.
Full logs stay in `runs/`; the CLI digests them incrementally after a foreground
command exits. A successful exit records execution success, not research validity.
Use `recent --thread baseline --grep 'E042' -n 5` for relevant events.

Runs record the full starting Git commit and whether the worktree was dirty.
This is not a snapshot of uncommitted code: commit or freeze the actual code and
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
For example, with your normal session identity and held claim:

```bash
python3 .research-memory/bin/rmem --agent codex --session codex-analysis-unique123 log --status running --job-id 123 --scheduler slurm --host cluster --id E043 --thread baseline --artifact cluster:/path/run.log --note 'Submitted; not evaluated'
```

Later append the observed terminal result and readout. A missing completion event
means unknown, not failed or successful. Never relaunch because context was compacted.

Use one writer host for the canonical hub. Remote workers produce artifacts;
the coordinator imports readouts. Claims use local filesystem locks; Git clones,
network filesystems, and `merge=union` cannot provide distributed ownership or
resolve competing conclusions. Claims, locks, and raw runs are ignored by Git.
Keep artifact backups separately.

## Checks, history, and limits

`check` validates structural integrity and memory budgets, not scientific validity,
source availability, or all pending jobs. Consolidate settled evidence into
`findings/` without deleting ledger events. Run `check` before a handoff, then
commit memory using your project's normal Git flow. `sync` is an optional
memory-only commit helper; it never pushes/pulls and reports Git failures.
It requires the hub to be its own Git repository. For the default hub inside a
research repository, use the parent project's normal Git workflow. Do not use
`sync` while another agent is editing shared files.

Model context is not automatically cleared. Use short recovery views and explicit
checkpoints. The CLI is for cooperating agents, not hostile writers. Project
permissions, operating mode, and live host/job checks remain authoritative.
