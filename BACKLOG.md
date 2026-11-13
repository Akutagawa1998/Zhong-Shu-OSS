# Backlog

Open work in rough priority order.

## Not finished from the 0.4.0 plan

- `recent --unread`: a per-session cursor (P0-4).
- Partial records: a minimal record profile whose missing fields are listed as
  gaps, `record fill ID`, and a `status` count of result events without fields
  (P1-9).
- Private agent memories: register their paths in MAP, and let `check` say when a
  private memory changed after the last lesson import (PF-11).
- An explicit `rmem session fork` command; today `session start --parent` and the
  resume lineage cover recovery after a fork (P1-6).
- A machine-readable canonical hub (per device) so `status` can warn when it runs
  on a non-canonical copy outside a linked worktree (P1-4).
- `sync --embedded --per-event` stores a full ledger blob per commit until `git gc`
  packs them; write packed history with `git fast-import` instead (P1-4).

## Verification gaps

- The Claude Code hook schema (SessionStart sources, PreCompact, stdin fields) is
  assumed from documentation; confirm it on a real installation before
  recommending `hooks install --claude --write`.

## P2: governance and long-term structure

- **Storage report and quotas** (P2-1): `rmem gc --report`, size quotas from
  `storage.*`, one event per `rmem run` with argv in `runs/<id>/meta.json`, and a
  rule that `runs/` holds logs only.
- **Instruction hygiene** (P2-2): a single `rmem start` entry card, `check
  --instructions` (duplicated rules, conflicting order, numbers that disagree with
  the code), and docs rendered from configuration.
- **Authority projection** (P2-3): `rmem authority show` with declared fields of an
  external controller, its hash and observation time, plus a small `AUTHORITY.json`
  of standing permissions that cite the user's words.
- **Context reader in the CLI** (P2-4): `rmem index/read/route`, including
  content-hash lookup.
- **Device identity** (P2-5): a local `device_id`, `devices.json` aliases, and
  logical roots instead of absolute paths.
- **Artifact registry** (P2-6): `rmem artifact add/refs` so nothing is deleted
  while a ledger event still points to it.
- **Ledger sharding** (P2-7): `ledger.d/<device>/<month>.jsonl` with
  `(device, counter)` sequences, for hubs that several machines write.
