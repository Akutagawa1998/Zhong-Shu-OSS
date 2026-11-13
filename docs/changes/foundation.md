# Foundation track (0.4.0)

### Added

- `rmem-kit/VERSION` (`0.4.0-dev`) and `rmem version`: the running kit version, the
  hub's recorded kit version and install time, and the kit files changed since
  install (count and up to 20 names). Read-only.
- `init` writes `.kit-manifest.json` with `schema_version`, `kit_version`,
  `installed_at` and a SHA-256 for every file it copied from the kit (not STATE,
  MAP, the ledger or `hub-config.json`). `check` warns about each locally changed
  or missing kit file (first 10, plus a count) and adds an `info:` line when a hub
  has no manifest because it was installed before 0.4.0.
- Optional `hub-config.json` (`bin/rmem_config.py`, documented in `CONFIG.md`):
  caps, `display_tz`, and key groups reserved for later features. `init` writes it
  from `HUB_CONFIG_TEMPLATE.json`, with a `_doc` note. Keys that start with an
  underscore are comments. Invalid values stop `rmem` with an error that names the
  key; unknown keys are `warn:` lines in `check`. `RMEM_TZ` overrides `display_tz`.
- `rmem doctor` (read-only): Python and kit versions, which hub was found and how
  (`RMEM_HUB`, a `.rmem` pointer or the working directory), read and write access
  to the hub, ledger, lock file, `threads/` and `claims/`, which identity variables
  are present (never their values), Git, configuration problems, loaded and broken
  plugins, and a hint for the detected harness (Codex writable root via
  `--add-dir`; Claude Code permissions for the hub's `bin/rmem`).
- Feature-module registry (`FEATURE_MODULES` in `bin/rmem`) and project plugins in
  `<hub>/bin/plugins/*.py`, loaded after the built-in modules. Modules may declare
  `EVENT_KINDS`, `EVENT_STRING_FIELDS`, `validate_event`, `register`,
  `status_sections` and `check_problems`; a stable `core` namespace exposes the
  CLI functions they need. A broken plugin is skipped and reported by `doctor` and
  `check` (`warn:`), and read-only commands keep working. `init` installs
  `bin/plugins/README.md`, which explains the contract.
- `check` reports, as `info:`, ledger event kinds that neither the core nor a
  loaded module declares (a sign that a module or plugin is missing).
- `.rgignore` template (`/highlights/`, `/history/`, `/runs/`) copied by `init`.
- An unknown command or `highlight` subcommand gets a "did you mean" suggestion
  and the list of commands.

### Changed

- Caps count characters, not bytes. The default thread cap is 9,000 characters
  (was 6,000 bytes); STATE stays 120 lines / 8,000 characters. This raises the
  usable size of non-Latin text; lower the caps in `hub-config.json` if needed.
- `check` fails above a cap and warns from `caps.soft_ratio` (0.7) on, with advice
  to move settled detail to `findings/` and leave a pointer instead of deleting
  spaces or compressing wording.
- `status` prints a one-line STATE budget and each thread's share of its cap
  (marked at or above the soft ratio), shows the kit version, and gives
  `status --thread` its own output budget so a long STATE no longer cuts the
  selected thread.
- Times are shown in the display zone with the zone name, for example
  `2026-10-14 01:34 PDT`. Ledger lines show `#sequence` and `s=session`:
  `2026-10-14 01:34 PDT #42 | codex s=session-id | exp | ...`.
- `--agent` and `--session` are accepted before or after the command name,
  including after nested `highlight` subcommands.
- Journal file names use the UTC date, matching the stored event times.
- Ignore rules are anchored to the hub root (`/runs/`, `/claims/`, `/.rmem.lock`,
  `/local-roots.json`), so `runs/` or `claims/` folders deeper in a project stay
  visible to Git. `init` rewrites the unanchored legacy lines it finds.
- The stale-claim age in `check` comes from `leases.default_ttl_hours` (default 12).

### Fixed

- Read commands no longer need write access to the hub. A shared lock opens an
  existing lock file read-only. When the lock cannot be opened or created because
  the hub is not writable, `status`, `recent`, `catalog`, `show`, `highlight
  list|show|verify` and `check` read without the lock, print one note line, and
  skip a final ledger line that is still being appended instead of reporting it
  as corrupt. A read-only clone without `claims/` is treated as having no claims.
  Writes still take the exclusive lock and fail with a message that points to
  `rmem doctor`.
- Error text written without spaces: "exceeds12000characters" and
  "exceeds48k" now read "exceeds 12,000 characters" and "exceeds 48,000 characters".

### Notes for the other tracks (decisions where the design left room)

- The plugin README ships at `rmem-kit/bin/plugins/README.md`, the same relative
  path it gets in a hub.
- `validate_event` runs for every event; a module returns `""` for kinds it does
  not own. A hook that raises is recorded as a broken module and treated as "no
  problem", so a module bug cannot mark ledger rows corrupt. In `check`, a broken
  plugin is `warn:` and a broken built-in module is a failure.
- Module `status_sections` lines appear near the top of `status` (after the
  claim/ledger inconsistency block, before STATE), so alerts such as open requests
  are seen first.
- `highlight restore` still needs the real shared lock: it writes a review copy
  and is not one of the read-only commands.
- `research_highlights.history_relationship` (track C's file) ignores an
  unterminated, unparseable final line of the live ledger so that `highlight
  verify` works for an unlocked reader.
- `catalog`, `show` and `highlight` do not read `hub-config.json` (their views own
  their budgets), so an invalid config does not block them; `status`, `recent`,
  `log`, `run` and `check` stop with the config error.
- `core` also exposes `text_usage`, `add_identity_options`, `file_sha256` and
  `discover_hub`. Tracks add config key groups with `rmem_config.add_group()`.
- `doctor` checks directory write access with permission bits rather than by
  creating a probe file, so it stays strictly read-only; its output says so.
- Display times follow the design (`2026-10-14 01:34 PDT`), without the extra
  `(08:34Z)` suffix that the lessons report sketched.
