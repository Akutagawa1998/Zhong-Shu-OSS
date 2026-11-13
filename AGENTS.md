# Zhong-Shu development

This repository develops a reusable research memory kit. `rmem-kit/STATE.md`,
`ledger.jsonl`, `MAP.md`, and its empty runtime folders are project-neutral
distribution templates.

- Keep project audits, transcripts, run receipts and workspaces in private
  storage. Use synthetic records for public examples and tests.
- Exercise mutations only in disposable hubs during tests. Do not link or import
  external research folders while developing the kit unless the user requests it.
- Keep the CLI Python standard-library only and run `python3 -m unittest discover
  -s tests -v` after behavior changes.
- Changing a memory protocol requires updating the CLI, help/examples, regression
  tests, and `CHANGELOG.md` together where applicable.
- This kit does not grant host access or activate a research mode. Preserve target
  project authorization and canonical automation state during any later adoption.

- Keep structured-record schemas, CLI views, and installed documentation aligned.
  Import generic runtime code only; never copy another project's records, source
  indexes, controllers, active manuscripts, or absolute artifact paths into the kit.
- A project adoption uses one canonical writable hub and explicit active-context
  registration. An archived path must resolve to hash-verified original bytes;
  a redirect is navigation, not the original evidence.
- Keep claim files and append-ordered ownership history consistent. Test Git
  stash/restore and explicit recovery only in disposable hubs; never repair a
  consuming project's claims as part of kit development.
- Document index-first recovery after project entry, active context and status:
  metadata index → relevant thread → catalog/show → exact cited evidence.
  Caps count characters and live in `hub-config.json`; keep STATE and threads well
  under them. Keep normal spaces and explain abbreviations; do not compress prose to
  pass a size check. A reader's unindexed-path refusal is not OS isolation.

- Change the kit first and upgrade hubs with `rmem upgrade`. Put project-specific
  code in a hub's `bin/plugins/` and settings in its `hub-config.json`.
- A new feature is a module in `rmem-kit/bin/` registered in `FEATURE_MODULES`
  (see `rmem-kit/bin/plugins/README.md` for the hooks). List every new kit file in
  `KIT_FILES`/`KIT_HELPERS`, which must stay tuple literals because `upgrade`
  parses them.
- Keep Python 3.10 compatibility. Anything that rewrites files outside the ledger,
  installs hooks or upgrades a hub is a dry run unless `--apply` or `--write` is
  given. `sync` commits only hub files; nothing pushes.
- Record user-facing changes in `CHANGELOG.md`; open work goes to `BACKLOG.md`.
