# Upgrading an installed hub

A hub holds two kinds of files. **Kit files** come from the kit: `bin/rmem`, the
helper modules in `bin/`, and the documents and templates that `init` copies
(listed in `.kit-manifest.json`). **Project data** is everything the project
writes. `rmem upgrade` replaces kit files that are still as installed, adds new
kit files, and leaves everything else alone.

Run the new kit's own `bin/rmem` (a hub installed before 0.4.0 has no `upgrade`
command), from inside the hub or with `RMEM_HUB` set:

```bash
python3 /path/to/Zhong-Shu/rmem-kit/bin/rmem upgrade --from /path/to/Zhong-Shu/rmem-kit
python3 /path/to/Zhong-Shu/rmem-kit/bin/rmem --session upgrade-unique123 upgrade --from /path/to/Zhong-Shu/rmem-kit --apply
```

Without `--apply` the command is a dry run: it prints the plan and changes
nothing. `--from` must be a kit directory (with `VERSION` and `bin/rmem`),
not the hub itself or another installed hub.

## How each kit file is classified

The kit files are the ones the source kit's `init` copies: `KIT_FILES`,
`bin/rmem` and `KIT_HELPERS`, read from the source kit's `bin/rmem` without
running it. Each one is compared with what the hub had at install time: the hash
recorded in `.kit-manifest.json`, or, for a hub installed before 0.4.0 (no
manifest), any hash that a released kit version shipped for that path
(`KIT_HISTORY.json`).

| Action | When | What `--apply` does |
|---|---|---|
| update | the hub file is as installed and the kit changed it | replaces it with the kit file (keeping its file mode) |
| add | the kit ships a file the hub does not have | copies it |
| merge | `.gitignore` lacks kit rules or has them unanchored | anchors kit rules found unanchored (`runs/` becomes `/runs/`) and puts missing kit rules at the top; every local line stays, so later local rules such as `!keep/this.log` still win |
| conflict | the hub file differs from both the install-time version and the kit | nothing: the file is never overwritten |
| same | the hub file already equals the kit file | nothing |

A conflict line shows the command to inspect it, such as
`diff -u HUB/AGENTS.md KIT/AGENTS.md`. To resolve one, move your change into
`hub-config.json` or `bin/plugins/` (or into a project document), then copy the
kit file over yours. You may also keep your file: `rmem check` then reports it
as changed since install.

A conflict in a code file (`bin/rmem` or `bin/*.py`) blocks `--apply`: an old
`bin/rmem` with new helpers, or the reverse, can break the hub's CLI. Resolve
those files first, then rerun. Conflicts in documents do not block.

## Never touched

`STATE.md`, `MAP.md`, `ledger.jsonl` (apart from the one appended event),
`hub-config.json` (written only when it is missing), and everything under
`threads/`, `findings/`, `journal/`, `claims/`, `history/`, `highlights/`,
`notes/`, `lessons/`, `runs/` and `records/`. A kit file in one of these places
(such as `findings/README.md`) is reported as "project data left alone" when it
differs from the kit. Files in `bin/plugins/` other than the kit's README are
yours and are never compared. Kit files the new kit no longer ships are left in
place and listed.

## What `--apply` does

1. Takes the hub's exclusive lock and runs the claim/ledger consistency check,
   including the fork check of this session, before any file changes.
2. Recomputes the plan and stops if a code file has a conflict.
3. Creates `bin/plugins/` if it is missing, copies updated and new kit files
   (each through a temporary file and an atomic rename), merges `.gitignore`,
   and writes `hub-config.json` from the kit template if the hub has none.
4. Rewrites `.kit-manifest.json`: the new `kit_version`, `installed_at` (now),
   `upgraded_from`, `first_installed_at` (from the previous manifest, if any),
   and a SHA-256 for every kit file as the kit ships it. A file kept because of a
   conflict is recorded with the kit's hash, so `rmem check` keeps reporting it.
5. Appends one `kit_upgraded` event with `from_version`, `from_basis`,
   `to_version`, `kit_source`, `counts` (per action) and the conflicting paths.

When nothing would change, `--apply` records nothing. Nothing is committed or
pushed: review the result with `rmem check` and `rmem version`, then commit the
hub with your normal Git workflow.

Upgrade changes files inside the hub only. Each linked project's `AGENTS.md`
still holds the managed block that the old kit's `link` wrote, which is the first
thing agents read there. Refresh it with `bin/rmem link PROJECT` (or
`bin/rmem link --canonical CHECKOUT` for an absolute pointer); an existing block
is replaced in place and the rest of the file is kept. After `--apply`, the
output lists the linked projects to refresh, and `rmem check` warns about a
linked project whose block differs from what the current kit writes.

## Hubs installed before 0.4.0

Such a hub has no `.kit-manifest.json`. A file counts as unchanged when its
SHA-256 equals the hash some released kit version shipped for that path in
`KIT_HISTORY.json`; the dry run shows which version matched (for example
"matches kit 0.2.1"). The "from" version is the newest release that matches the
hub's code files (or, failing that, any file); if nothing matches it is
"unknown", and every file that differs from the kit is a conflict. After
`--apply`, the hub has a manifest and later upgrades use it.

## `KIT_HISTORY.json` and how to regenerate it

`KIT_HISTORY.json` lists, for each released tag, the SHA-256 of every file under
`rmem-kit/` at that tag, keyed by its path in an installed hub (`rmem-kit/X` is
`X`). It only serves hubs without a manifest, so it lists released versions
(0.2.0, 0.2.1, 0.3.0, 0.3.1) and is regenerated when a release is tagged. Run this
with bash from the repository root after adding the new tag to the list:

```bash
{
  printf '{\n  "_doc": "SHA-256 of every file under rmem-kit/ at each released kit tag, keyed by its path in an installed hub (rmem-kit/X is X). rmem upgrade uses it to recognize unchanged kit files in hubs installed before 0.4.0. Regenerate with the command in UPGRADE.md.",\n  "schema_version": 1,\n  "versions": [\n'
  sep=''
  for tag in v0.2.0 v0.2.1 v0.3.0 v0.3.1; do
    printf '%s    {"version": "%s", "tag": "%s", "files": {\n' "$sep" "${tag#v}" "$tag"
    git ls-tree -r --name-only "$tag" -- rmem-kit | LC_ALL=C sort | {
      first=1
      while IFS= read -r path; do
        sum=$(git show "${tag}:${path}" | shasum -a 256 | cut -c1-64)
        [ "$first" = 1 ] || printf ',\n'
        first=0
        printf '      "%s": "%s"' "${path#rmem-kit/}" "$sum"
      done
    }
    printf '\n    }}'
    sep=',
'
  done
  printf '\n  ]\n}\n'
} > rmem-kit/KIT_HISTORY.json
```
