# Zhong-Shu

A reusable, file-based research memory kit for Codex, Claude Code, and humans.
It preserves experiment evidence and the next actionable step without repeatedly
loading the project history into a conversation. Several sessions, agents and
machines can share one hub: each session has an identity, threads are held by
leases, requests and lessons pass between sessions, and `rmem resume` gives a
bounded card to read after a compaction or a fork.

The reusable package is [`rmem-kit/`](rmem-kit/README.md) (version 0.4.1): a
Python standard-library CLI, a [skill entry point](rmem-kit/SKILL.md), and empty
project templates. Project memory belongs in the project that uses the kit.

```text
Zhong-Shu/                       reusable source repository
  rmem-kit/                     kit + skill + empty templates
  tests/                        isolated CLI regression tests
  docs/                         design notes and per-area change notes

YourResearchProject/            created later, in the target project
  .rmem                         pointer to .research-memory
  AGENTS.md                     existing instructions + managed memory block
  CLAUDE.md                     existing instructions + AGENTS.md import
  .research-memory/             independent kit copy and project memory
```

What the kit covers, with the guide for each part:

| Need | Commands | Guide |
|---|---|---|
| Recover context | `status`, `resume`, `recent`, `catalog`, `show` | [README](rmem-kit/README.md), [INDEXED_READING](rmem-kit/INDEXED_READING.md) |
| Record evidence | `log` (with `--note-file`, `--code-ref`, `--cmd`), `capture`, `run` | [RECORDS](rmem-kit/RECORDS.md) |
| Share work between sessions | `session`, `whoami`, `claim` (leases), `request`, `inbox`, `lesson`, `pending`, `checkpoint`, `hooks` | [HANDOFF](rmem-kit/HANDOFF.md) |
| Keep entry files small | caps in `hub-config.json`, `thread seal/archive/roll` | [CONFIG](rmem-kit/CONFIG.md), [LIFECYCLE](rmem-kit/LIFECYCLE.md) |
| Freeze milestones | `highlight create/verify/restore/index` | [HIGHLIGHTS](rmem-kit/HIGHLIGHTS.md) |
| Live inside a Git repository | `sync --embedded --per-event`, `link --canonical`, `repair`, `guard` | [GIT](rmem-kit/GIT.md), [CONSISTENCY](rmem-kit/CONSISTENCY.md) |
| Keep the kit current | `version`, `upgrade`, `doctor`, `bin/plugins/` | [UPGRADE](rmem-kit/UPGRADE.md) |

## Start a new project

Use Python 3.10+ on macOS or Linux, on a local filesystem. From this checkout,
replace `/path/to/ResearchProject` with your existing project directory:

```bash
python3 rmem-kit/bin/rmem init /path/to/ResearchProject/.research-memory --project-name ResearchProject
cd /path/to/ResearchProject/.research-memory
python3 bin/rmem link ..
python3 bin/rmem status
```

`init` copies the kit and creates an empty project hub. Keep each project's
memory in a private workspace. A hub can record local paths, hostnames,
conversation IDs, experiment logs and other project data.

## Upgrade an installed hub

```bash
python3 /path/to/Zhong-Shu/rmem-kit/bin/rmem upgrade --from /path/to/Zhong-Shu/rmem-kit
```

This is a dry run: it lists the kit files it would update or add and the files you
changed locally (conflicts). Add `--apply` (with `--session`) to make the changes,
then relink each project so its instruction block is current. Project data is
never touched. See [UPGRADE.md](rmem-kit/UPGRADE.md).

## Security and privacy

Run installed CLIs and project plugins only from sources you trust. Plugins
require an explicit opt-in for each hub. Review the files and Git history before
sharing a hub or any artifact derived from it. See the [security policy](SECURITY.md)
and [plugin setup](rmem-kit/bin/plugins/README.md).

## Development

```bash
python3 -m unittest discover -s tests -v
```

There are no runtime dependencies. Git is optional for provenance and commits.
The kit does not implement a distributed lock service, a scheduler, or automatic
model-context compaction; it provides bounded reads, a recovery protocol and
cooperative ownership. Scientific conclusions still require evidence review.
Design contracts for 0.4.0 are in [docs/design-0.4.0.md](docs/design-0.4.0.md).

## Project history

Development started in September 2026. This repository preserves five
reconstructed development milestones from `v0.2.0` through `v0.4.0` under
`history/` tags. The snapshots retain the original author dates and contain
the reusable kit and tests. For new installations, use `v0.4.1` or later.

## License

[MIT](LICENSE).
