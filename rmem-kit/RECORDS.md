# Structured research memory

Use one canonical writable hub for each project. This registry is a bounded
recovery view, not a scheduler, remote filesystem, second experiment ledger, or
proof of scientific success. Historical ledger entries remain immutable.

## Recover a task without scanning the repository

1. Read the linked hub's `AGENTS.md`; run `bin/rmem status`.
2. Query the project metadata index, then read the relevant short
   `threads/<slug>.md`; see [INDEXED_READING.md](INDEXED_READING.md).
3. Use `catalog` / `show` to retrieve the exact experiment or event, then only
   its cited evidence slices.
4. If fields or artifacts are missing, report the gap and follow explicit source
   mappings. An empty query result does not justify loading the whole repository.

From an installed project's root:

```bash
python3 .research-memory/bin/rmem catalog --query E042 -n 5
python3 .research-memory/bin/rmem catalog --thread baseline --updates -n 10
python3 .research-memory/bin/rmem show E042 --section record
python3 .research-memory/bin/rmem show EVENT_UUID --section sources
python3 .research-memory/bin/rmem show E042 --section history
python3 .research-memory/bin/rmem show E042 --field research --page 2
python3 .research-memory/bin/rmem check --records
```

`catalog` searches ledger metadata and small reviewed cards; it does not search
source code, datasets, model files, full logs, or remote hosts. Its default view
keeps the latest update visible even when the query matched an older claim.
Use `--updates` to select individual historical updates. `show` accepts exact IDs
and returns bounded valid JSON; oversized records retain paths for a narrower
read rather than silently dropping scientific fields, and `--field NAME --page N`
reads any one field completely in pages. Several `--query` texts and a `--regex`
are alternatives that share one result budget.

## Identity, sequence, and training progress

| Field | Meaning |
|---|---|
| `event_id` | Permanent UUID for one update. |
| `sequence` | Monotonic append sequence within one canonical local hub. Legacy rows use an explicitly labeled derived ledger position. |
| `experiment_id` / ledger `id` | Stable original task or experiment ID, reused by subsequent updates. |
| `steps` | Task progress, with `optimizer_updates`, `evaluation_calls`, `checks`, or `not_applicable` units. |
| `source_event_ids` | Exact prior events covered by a reviewed historical card. |
| `legacy_refs` | Original source-specific identifiers, such as `legacy-ledger#17`. |

A continuation from checkpoint100 for50 additional updates has final planned150;
record starting100 and new_planned50 separately. It is not the150th memory update.
A shard is not a training replicate; a generated call is not an optimizer update.
Two writable Git clones do not share locks or a global counter.

## Required record content

Start with [RECORD_TEMPLATE.json](RECORD_TEMPLATE.json). Replace placeholders and
put `null` plus an explanation in `gaps` for facts that cannot be established.
A template is not an actual experiment. The schema validates structure; it cannot
establish that a stated count, hash, conclusion, or remote availability is true.

- `task_type`, `summary`, `purpose`, `lineage`: exact work, question, and model/data
  cohort. Distinguish training, inference, analysis, and engineering.
- `steps`: planned/completed progress and its unit/scope. Preserve failures and
  partial results; unknown completion is not zero.
- `model`: base, exact identity/checkpoint, and seed. Multi-state cohorts may use
  lists; preserve individual identities instead of a size-only label.
- `datasets`: ID, split, role, count, SHA256, and exact location. Distinguish raw
  versus effective rows, training versus DEV/qualification/test, and neutral calls.
- `recipe`: method, configuration or frozen manifest, code revision, and location.
  Training should identify optimizer/LR/batch/accumulation/epochs/steps/precision/
  supervision; inference should identify decoder/tokenizer/cap/precision/reader.
- `evaluation`, `findings`: question, metrics with denominators, evidence level,
  acceptance, limits, and source pointers. Process completion, complete raw-output
  acceptance, scientific qualification, causality, and model preservation differ.
- `artifacts`: role, platform, exact URI/path, hash, availability, and checked time.
  Use `remote` with an explicit host in the URI for any project-specific host;
  `local`, `github`, and `hf` are supported. Existing `torch`, `alice`, and `145`
  aliases remain accepted for compatibility. `reported_remote` is not a fresh
  verification, a central download, or a backup.
- `as_of`, `source_event_ids`, `legacy_refs`, `gaps`: saved cutoff, exact evidence
  links, original identifiers, and unresolved questions.

Finding status is `accepted`, `reported`, `inconclusive`, or `retracted`.
Evidence level is `observed`, `reported`, or `inferred`. Artifact availability is
`observed_local`, `reported_remote`, `verified_remote`, `planned`, or `missing`;
observed or verified availability needs an actual timezone-qualified checked time.

## Append a structured update

Use the conversation's stable unique session and claim its research thread first.
Shared findings and reviewed historical cards also require `project-state`.

```bash
python3 .research-memory/bin/rmem --agent codex --session UNIQUE_SESSION claim baseline
python3 .research-memory/bin/rmem --agent codex --session UNIQUE_SESSION log \
  --thread baseline --record /path/to/completed-record.json --status inconclusive \
  --source /path/to/frozen-manifest.json --note 'Exact update and remaining limits'
```

The CLI validates the record and claim, hashes its source file, and appends both
record content and source identity to the ledger. It never rewrites an old entry.
A later update uses the same experiment ID but receives a new UUID/sequence.
A reviewed card in `records/*.json` enriches only named `source_event_ids`; it does
not fill a later update's missing record or silently replace a correction.
Historical cards with no corresponding hub event remain queryable using original
IDs, but must not fabricate a hub UUID or imply a new execution.

Legacy `log` calls remain supported. `check --records` reports coverage and gaps.
A project can explicitly check adoption after its own cutoff:

```bash
python3 .research-memory/bin/rmem check --records --require-records-since 2026-01-01T00:00:00+00:00
```

Replace this example cutoff with the actual adoption date. Missing records on
exp/finding/decision/todo events written after it cause a nonzero exit. This does
not install a Git hook, CI job, automation, or blanket blocking policy.

## Note bodies and command text

`--note` is the one-line summary that list views show (at most 1,000
characters). Put longer or multi-line text in a file and pass `--note-file PATH`,
or `--note-file -` to read it from standard input:

```bash
python3 .research-memory/bin/rmem log --id E042 --thread baseline --status keep \
  --note 'Seeds 1 to 3 agree; readout in the body' --note-file /tmp/e042-readout.md
python3 .research-memory/bin/rmem show E042 --field body
python3 .research-memory/bin/rmem show E042 --body --page 2 --format text
```

- The text is stored once under `notes/<yyyy-mm>/<first 16 hex digits of its
  SHA-256>.md` (UTC month); logging an identical text again reuses that file.
  The event keeps `body_path`, `body_sha256` and `body_chars`. Without `--note`,
  the summary is the first nonempty line of the body (up to 300 characters).
- Every argument is checked before the hub lock is taken, so a refused `log`
  leaves no ledger line and no file. The body is written under the lock just
  before the append; when the append is refused, the file is removed again.
- Note files are memory: `rmem sync` commits them with the ledger, and
  `.gitattributes` keeps their bytes exact. They are never edited. `rmem check`
  fails when one no longer matches its hash, warns when one is missing, and
  reports files that no event names as `info:`.
- `show ID --field body` (or `--body`) pages the text and reports `verified`
  against the recorded hash. The limit is `notes.body_max_chars` (200,000
  characters by default; see [CONFIG.md](CONFIG.md)).

## Provenance fields

`log` and `capture` accept these optional fields. All of them are plain event
fields; none is required, and older events simply lack them.

| Option | Event field | Meaning |
|---|---|---|
| `--cmd TEXT`, `--cmd-file PATH` | `cmd`, or `cmd_path`, `cmd_sha256`, `cmd_chars` | exact command text; text over 4,000 characters is stored like a note body and read with `show ID --field cmd` |
| `--workdir TEXT` | `workdir` | working directory on the host where the command ran |
| `--code-ref HOST:PATH@REVISION` | `code_ref`, `commit` | code that ran elsewhere; the revision after the last `@` fills `commit` unless `--commit` is given |
| `--set KEY=VALUE` (repeatable) | `fields` | structured values such as `seed=42` or `data.sha=abc123`; plain decimal numbers are stored as numbers, anything else (including `007`) as text |
| `--no-provenance` | `provenance.skipped` | skip the local Git probe because the code ran on another machine |

Without `--commit`, `log` probes the Git checkout of the current directory and
records `provenance` (repository root, `HEAD`, dirty state and a status hash).
When the hub lives inside that repository, the hub directory is left out of the
dirty check and named in `provenance.dirty_excludes`, so memory writes no longer
mark the code dirty. With `--code-ref` the probe stays in `provenance` but does
not fill `commit`. A `--host` result that would carry this machine's commit gets
a hint to use `--code-ref` or `--no-provenance` instead.

## Capture a submitted command

`rmem run` wraps a local foreground process. For a command already submitted
through ssh, sbatch or another scheduler, record the exact command without
running anything:

```bash
python3 .research-memory/bin/rmem capture --id E043 --thread baseline --host cluster \
  --job-id 123 --scheduler slurm --workdir /scratch/e043 \
  --code-ref cluster:/scratch/code@8566a1d -- sbatch --array=0-3 train.sh
```

The event has `kind` `exp`, `status` `running` (or the given `--status`),
`captured: true`, the exact `argv`, and its shell rendering in `cmd`; `--cmd` or
`--cmd-file` can replace `-- COMMAND`. Its evidence level defaults to `reported`.
Capture writes to the thread like `log --thread`. A captured command was
submitted; it has not been observed to run or succeed, so log the outcome later.

## Secrets never go into the ledger

The ledger is append-only and note files are never edited; once a hub is
committed and pushed, a line cannot be redacted without rewriting Git history.
So `log` and `capture` refuse a `--note`, `--hyp`, `--where`, command text
(`--cmd`, `--cmd-file`, the captured `argv`), `--note-file` body or `--set`
value that looks like a key, token or password (private keys, AWS, GitHub,
OpenAI, Anthropic, Hugging Face and Slack tokens, and assignments such as
`HF_TOKEN=…` or `WANDB_API_KEY=…`), and `note` refuses such journal text. The
refusal names the field, never the value. Refer to a secret by its variable
name (`$HF_TOKEN`) instead; `--allow-sensitive` records the text anyway when you
have checked that it is not a secret. `rmem run` still runs the command as
given but records such an argument as `NAME=<redacted>` (or `<redacted>`) and
adds `redacted: ["argv"]` to its events.

## Portable source locations

Source views retain the original path/hash and display logical
`root_id + relative_path`, local candidates, and same-hash snapshot candidates.
Existence is not a hash check. Verify bytes before using them as evidence.
Optional `sources/roots.json` maps logical roots. Mark the project source with
`"project_relative": "."` so its original paths can be resolved under the current
hub parent after a project move; a same-path root is also recognized locally. A device-local, Git-ignored
`local-roots.json` may override a root, for example:

```json
{"execution-workspace": "/new/device/research"}
```

Preserve original identities; do not replace a live controller with a historical
snapshot. Prefer fixed Git commits and Hugging Face revisions over moving heads.
If a project maintains `ARTIFACT_ROUTES.json` in its hub, exact old paths resolve
through that manifest with byte-count and SHA256 verification. Its version1
`files` entries contain `original_path`, `current_path`, `bytes`, `sha256`, and
`role`; paths are normalized project-relative paths without symlinks or traversal.
Original evidence outranks `retired_redirect` pages when no hash is requested.
A cited hash must match exactly; ambiguity, missing files, or changed bytes fail
explicitly. Original relative links keep their original-directory base. No
project manifest is bundled or created automatically.

## Active context and limits

A project may explicitly register current documents in a small active-context
manifest and route old document paths through a hash-bound archive map. Current
status must come from deliberate registration, not the newest filename, mtime,
branch name, or the presence of old instructions. Keep negative results,
retractions, and lineage distinctions accessible through their original IDs.

The reusable kit does not choose current manuscripts or move any project files.
Its protocol supports memory-first recovery, but ordinary filesystem access still
permits agents to read other files. There is no tool-level whole-repository-read
ban, distributed lock, or automatic context compaction. Check actual Codex/Claude
entry loading in a fresh session; keep a single canonical writable hub.
