# Track B: read and write path (0.4.0)

Plan items P0-4 (bounded queries), P0-5 (the rest of write friction) and P1-9
(lightweight provenance); pitfalls PF-02 and PF-10.

### Added

- `recent --since/--until` take a local time in the display zone
  (`2026-10-14 09:00`), ISO 8601 with an offset, or an age (`90m`, `6h`, `3d`), and
  compare ingestion time.
- `recent --after-seq N` lists the events after `#N`, oldest first, and ends with
  the command for the next page. Legacy rows without a stored sequence show their
  ledger position, which `--after-seq` also uses.
- `recent --from-session S` (only that session's events; first shipped as `recent --session S`), `--other-sessions` (hide your
  own session), `--include-locks`, and `--kind` with a comma-separated list.
- `show ID --field NAME [--page N] [--page-size C] [--format text]` reads one
  field completely in pages: `note`, `body`, `cmd`, `research`, `sources`,
  `metrics`, a dotted path such as `research.recipe`, or `event`. `--body` is short
  for `--field body`. The JSON view gives `offset`, `length` and the `next`
  command; the pages concatenate to the exact text.
- `catalog --regex PATTERN` and repeated `--query`: alternatives (OR) that share
  one `-n` and one output budget; with more than one, each row lists `matched_by`.
- `rmem export --out FILE [--from-seq A] [--to-seq B] [--since] [--until] [--kind]
  [--thread] [--force]` writes the selected events as JSONL to a file you page with
  your own tools. It refuses to overwrite without `--force` and refuses paths inside
  the hub except under `runs/`.
- `log --note-file PATH|-` stores a multi-line body once, by content, under
  `notes/<yyyy-mm>/<sha256 prefix>.md`; the event records `body_path`,
  `body_sha256` and `body_chars`. Without `--note`, the summary is the body's first
  line. `rmem sync` commits `notes/`, `.gitattributes` keeps note bytes exact, and
  `check` fails on a changed note file, warns on a missing one and reports
  unreferenced ones as `info:`. New config key `notes.body_max_chars` (200,000).
- `log` options `--cmd TEXT`, `--cmd-file PATH|-` (exact command text; over 4,000
  characters it is stored like a note body), `--workdir`, `--code-ref
  HOST:PATH@REVISION` (its revision fills `commit` unless `--commit` is given),
  repeatable `--set KEY=VALUE` (stored under `fields`; plain decimal numbers become
  numbers) and `--no-provenance`.
- `rmem capture --id ID --thread T [--host --job-id --scheduler --workdir
  --code-ref --set --note --note-file] -- COMMAND ...` records an already submitted
  command without running it: `kind` `exp`, `status` `running`, `captured: true`,
  `argv` and its shell rendering in `cmd`.
- `log --host H` for a remote host that would carry this machine's commit prints a
  hint to use `--code-ref` or `--no-provenance`.

### Changed

- `recent` and `status` hide `claim`/`release` events by default and say how many
  were hidden; `--include-locks` (or `--kind claim,release`) shows them. Scripts
  that read lock events from `recent` need `--include-locks`.
- `recent` and `catalog` accept any `-n`; above 200 it is reduced to 200 with a
  notice instead of an argparse error (the old maximum was 100).
- `recent` fits whole lines into its output budget, dropping the oldest lines
  first (with `--after-seq`, the newest), and ends with a note that says how many
  matching events were left out and how to read them.
- `--note` on `log` stays one line of at most 1,000 characters; a longer or
  multi-line note now fails with a message that names `--note-file`.
- `log --status` rejects an unknown value with the list of accepted values and what
  they mean (for example `--status done`).
- The Git probe of `log` and `run` leaves a hub that lives inside the code
  repository out of `dirty` and the status hash (`provenance.dirty_excludes`), so
  memory writes no longer mark the code dirty.
- `show` on a view over 12,000 characters now also suggests `--field NAME --page N`.

### Fixed

- Every `log` argument, including the final event shape and size, is checked
  before the hub lock is taken, so a refused `log` leaves no ledger line, no lock
  file and no note file. A note file written under the lock is removed when the
  append is refused.

### Notes for the integrator

- Note files are named by content (`notes/<yyyy-mm>/<first 16 hex digits of the
  SHA-256>.md`, the full hash on a prefix clash) rather than by event ID as the
  design sketched, because the task asked for content addressing and reuse of
  identical text. The design's `show ID --body` exists as an alias of
  `--field body`.
- `recent` first defined its own `--session` as a filter. The review renamed it
  `--from-session`, because agents append `--session ID` to every call and then
  silently saw only their own events; `--session` after `recent` is identity again,
  and `--other-sessions` reads it. Paging now follows ledger order (`--after-pos`
  when stored sequences repeat after a merge).
- Core edits outside the listed regions, all small: `memory_path` and `cmd_sync`
  include `notes/`; `cmd_run` passes the hub to `provenance`; `import_fingerprint`
  includes the new content fields (absent from older imports, so their
  fingerprints do not change); `cmd_status` hides lock events; the `log` path was
  split into `build_log_event` (pure, now with `config` and `hub` parameters and a
  `preflight_event` check) and `commit_log_event` (the locked half shared by `log`
  and `capture`; Track A's ownership rules still enter only through
  `authorize_thread_write`). `HUB_CONFIG_TEMPLATE.json` gained the `notes` group
  and `.gitattributes` one rule.
- Three foundation tests changed on purpose: the registry is no longer empty, the
  status claim line needs `--include-locks`, and the command list now starts with
  `capture`.
