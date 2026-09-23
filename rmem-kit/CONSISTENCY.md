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

## Limits and a possible durable history guard

This is a consistency check, not hard isolation. An old CLI, direct file edit,
Git checkout, or another clone can bypass it. Rolling back both claims and
history together may leave a consistent-looking older state. Claims do not
implement distributed ownership or identify a live scheduler owner.

No automatic topology or rollback lock is installed. A future opt-in history
guard could store a hub identity, canonical checkout identity, ledger byte count
and prefix hash under the Git common directory, outside branch-tracked files.
After validating an existing prefix it could advance an atomic checkpoint;
prefix mismatch would warn or block new work without modifying history. This
requires explicit enrollment of the one writable hub, documented clone/worktree
semantics, and an explicit audited reset/rebind command so migrations do not
cause an unrecoverable lock. Until those decisions are made, keep the canonical
hub on its designated checkout or migrate it outside changing code checkouts by
an explicit, hash-verified operation. No live hub is relocated by this change.
