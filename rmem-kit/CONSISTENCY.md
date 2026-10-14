# Claim and history consistency

Claims are ignored local runtime files, while the ledger follows Git. A checkout,
stash, reset, partial restore, or independent clone can therefore pair current
claim files with different history. File existence alone is not proof of current
ownership.

`rmem check` (including `--records`) compares append-ordered claim/release events
with claim files. It reports orphan files, unreleased ledger claims with missing
files, incorrect session/agent ownership, and releases or takeovers that do not
record explicit force/reason. New claims bind their metadata to the exact claim
event ID; old session-bearing claims remain compatible without that added field.
Closed legacy ownership history remains readable. Active legacy claims without
session identity require an explicit recovery decision.

Claims are leases (see [HANDOFF.md](HANDOFF.md)). An expired lease is a `warn:`
line, not a failure: inactivity is not an integrity problem. A `claim --takeover
--reason` of an expired lease records `previous_owner` and `reason`, so this
check treats it exactly like a forced claim. Resource leases (`claim --resource
NAME`) are stored in `claims/@NAME/`, their events carry `resource` instead of
`thread`, and they are compared with the ledger in the same way; issues name them
as `@NAME`.

A normal release does not clear an earlier unrecorded ownership change. The
problem remains visible after the thread closes until a forced recovery records
the decision. When such a closed history has no claim file, explicitly claim
with `--force --reason` and then release if no work remains.

Ordinary mutations run this check before writing or starting a command. `status`
shows warnings without preventing read-only investigation. A mismatch never
automatically cancels a job, deletes an event, releases a claim, changes a
controller, or chooses which Git branch is authoritative.

## Deliberate recovery

First inspect the previous owner, running processes/jobs, branches and backups.
Restore the intended ledger history when it was accidentally hidden by Git.
After deciding which owner should remain, existing commands support a recorded
recovery:

```sh
rmem --agent codex --session YOUR_UNIQUE_SESSION claim THREAD --force --reason 'Reviewed owner and jobs; describe the decision'
rmem --agent codex --session YOUR_UNIQUE_SESSION release THREAD --force --reason 'Reviewed stranded claim; describe the decision'
```

Force recovery records the prior metadata, ledger ownership and detected issues;
it does not rewrite previous events. A forced release can close an unreleased
ledger claim whose local file is missing. Recovery may fix one thread while
others remain inconsistent, so multiple problems do not create a circular lock.
Ordinary writes remain blocked until all claim inconsistencies are resolved.
Corrupt or duplicate ledger identities must be investigated separately.

Claim publication writes the event before its anchored metadata. A crash between
these writes leaves a detectable missing/mismatched claim rather than a silently
unrecorded grant. A command already running when external history changes keeps
its raw log; a blocked terminal ledger append must be reconciled explicitly.

## Git history checks

Since 0.4.0, `rmem check` also looks at the ledger as Git leaves it (details and
commands in [GIT.md](GIT.md)):

- Git conflict marker lines fail with their line numbers and the repair command
  `rmem repair --conflict-markers` (a dry run unless `--apply`; it backs up the
  ledger, removes only the marker lines and records a `ledger_repair` event).
  Ordinary writes stay blocked until the ledger is repaired.
- Timestamps or stored sequence numbers that go backward in file order are
  `warn:` lines: a merge or rebase inserted events from another branch or device.
- A hub inside a linked Git worktree gets a `warn:` naming the main checkout's
  hub, the canonical writable one (`rmem link --canonical` points worktrees there).
- Ledger events that stay uncommitted longer than `git.uncommitted_warn_hours`
  are a `warn:` line; `rmem sync --embedded --per-event` commits them without
  touching other files. A checkout or stash can otherwise hide them.
- Every highlight is verified; a failed verification fails the check.

`rmem guard install --write` adds Git hooks that run these checks after a
checkout, merge or rewrite and warn about uncommitted events before a rebase.
They print warnings only; they never block Git.

## Limits

This is a consistency check, not hard isolation. An old CLI, direct file edit,
Git checkout, or another clone can bypass it. Rolling back both claims and
history together may leave a consistent-looking older state. Claims do not
implement distributed ownership or identify a live scheduler owner. The guard
hooks warn after the fact; they do not lock a checkout. Keep the canonical hub on
its designated checkout, point other checkouts at it with `link --canonical`, and
move it only by an explicit, hash-verified operation.
