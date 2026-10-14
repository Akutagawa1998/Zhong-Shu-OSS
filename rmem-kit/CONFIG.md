# Hub configuration (`hub-config.json`)

Project settings live in `hub-config.json` in the hub root, tracked by Git like
the rest of memory. The kit's `bin/` files stay identical to the kit, so they can
be upgraded; change behavior here or in `bin/plugins/` instead.

- The file is optional. A missing file, or a missing key, uses the kit default.
- `init` writes a copy of `HUB_CONFIG_TEMPLATE.json` that lists every default.
  JSON has no comments: keys that start with an underscore (such as `_doc`) are
  ignored and can hold notes.
- An invalid value stops `rmem` with one error that names every invalid key, for example
  `hub-config.json: caps.thread_max_chars must be an integer of at least 1
  (found "9000")`, so one round of fixes is enough. `rmem doctor` shows the same list.
- An unknown key is ignored and reported as a `warn:` line by `rmem check`.
- `rmem check` and `rmem doctor` read the file; nothing rewrites it.

## `schema_version`

Always `1` for this kit. Another value is an error.

## `caps` — context budgets

Caps count **characters, not bytes**, so Chinese or other non-Latin text gets
the same budget as English (a CJK character is three UTF-8 bytes). A file over a
cap makes `rmem check` fail. A file at or above `soft_ratio` of a cap produces a
`warn:` line, and `rmem status` marks it in its budget line and thread list.

Caps are alarms, not targets. When a file approaches its cap, move settled
detail into `findings/<topic>.md` (or another indexed evidence file) and leave a
one-line pointer. Never delete spaces, drop failures, or compress wording to fit.

| Key | Default | Meaning |
|---|---|---|
| `caps.state_max_lines` | 120 | lines allowed in `STATE.md` |
| `caps.state_max_chars` | 8000 | characters allowed in `STATE.md` |
| `caps.thread_max_lines` | 80 | lines allowed in each `threads/<slug>.md` |
| `caps.thread_max_chars` | 9000 | characters allowed in each thread |
| `caps.output_max_chars` | 16000 | output budget of `status`, `recent`, `check`, `log` and `run` (1,000 to 1,000,000); `status --thread` adds `thread_max_chars` for the selected thread |
| `caps.soft_ratio` | 0.7 | share of a cap that starts soft warnings (greater than 0, at most 1) |

Before 0.4.0 the thread cap was 6,000 bytes. Counting characters with a 9,000
cap lets a typical mixed-language thread hold more text; lower the cap here if
your agents need shorter threads.

## `display_tz` — how times are shown

`null` (default) shows times in the system time zone. Set an IANA name such as
`"America/Los_Angeles"` or `"Asia/Shanghai"` to fix it for everyone who uses the
hub. The environment variable `RMEM_TZ` overrides it for one process.

Stored times stay UTC ISO 8601 with an offset. Displays look like
`2026-10-14 01:34 PDT`. Inputs to options that take a time (`recent`, `export`
and `highlight list` `--since`/`--until`, `check --require-records-since`, and
`log --event-time`) accept ISO 8601
with an offset, a time without an offset (read in the display zone, such as
`2026-10-14 01:34`), or an age such as `90m`, `6h` or `3d`. `--event-time` says
when something happened, so it takes no age. Journal files are
named by the UTC date.

## `roles`

A JSON object whose keys are the project's session roles, for example
`{"writer": "paper text", "torch": "cluster jobs"}`; the values are free notes.
`rmem session start --role R` and `session update --role R` accept only these
keys when the object is not empty. With no roles configured, any role text is
recorded with a warning. Request addressing (`role:R`) and lessons
(`--applies-to role:R`) use the same names.

A value may instead be an object with a `threads` list, for example
`{"exec-7b": {"note": "7B runs", "threads": ["internalization-7b"]}}`. Requests
addressed to those threads (`thread:T`) then reach every session with that role
in `rmem status`, `rmem inbox` and `rmem resume`, and lessons for those threads
apply to it, whether or not it holds the thread's lease. `rmem check` warns when
`threads` is not a list of thread slugs.

## `leases`

| Key | Default | Meaning |
|---|---|---|
| `leases.default_ttl_hours` | 12 | hours a lease stays live after its holder session's latest ledger event of any kind (greater than 0); `rmem claim --ttl HOURS` sets it per lease |

An expired lease no longer blocks writes by other sessions, is marked in `rmem
status` with its last activity, is a `warn:` line in `rmem check`, and can be
taken over with `rmem claim SLUG --takeover --reason TEXT`. Claims made before
0.4.0 carry no TTL and use this default. See [HANDOFF.md](HANDOFF.md).

## `requests`

| Key | Default | Meaning |
|---|---|---|
| `requests.ack_warn_days` | 2 | days an open request may wait for `rmem ack` before `rmem check` warns (0 or more) |

The warning names the request, its addressee and its age. Acknowledging or
closing the request stops the warning. See [HANDOFF.md](HANDOFF.md).

## `resume`

| Key | Default | Meaning |
|---|---|---|
| `resume.max_chars` | 6000 | character budget of the `rmem resume` card (1,000 to 100,000); `--max-chars N` overrides it per call |
| `resume.recent_events` | 8 | own recent events the card lists (0 to 50); `-n N` overrides it per call |

The card lists its sections from most to least urgent and is cut at the budget,
so a smaller budget drops the recent events first. With the optional Claude Code
hooks, the card is added to the model's context at every session start, so keep
the budget modest.

## `staleness`

| Key | Default | Meaning |
|---|---|---|
| `staleness.events_after_edit` | 20 | ledger events about a thread after its file last changed before it counts as stale (at least 1) |

Read by `rmem check` (`bin/research_lifecycle.py`): an active thread with more
events than this since its last change gets a `warn:` line. The last change is
the last Git commit that touched the file when the hub is in Git and the file has
no uncommitted changes, otherwise its modification time. Claim, release and
lifecycle events do not count; sealed and archived threads are skipped. See
[LIFECYCLE.md](LIFECYCLE.md).

## `git`

| Key | Default | Meaning |
|---|---|---|
| `git.uncommitted_warn_hours` | 24 | hours a ledger event may stay uncommitted to Git before `rmem check` and `rmem status` warn (greater than 0) |

The Git integration (`bin/research_git.py`, see [GIT.md](GIT.md)) reads this key.
"Uncommitted" is derived from the number of event lines in the ledger at `HEAD`;
nothing is stored. A hub whose ledger Git ignores gets no warning.

## `storage`

| Key | Default | Meaning |
|---|---|---|
| `storage.hub_warn_mb` | 200 | hub size, in megabytes, that triggers a warning (greater than 0) |
| `storage.file_warn_mb` | 5 | single-file size, in megabytes, that triggers a warning (greater than 0) |

The `storage` group is reserved for a later storage report; today it only
validates.

## `notes` — note bodies and command texts

`rmem log --note-file` and `--cmd-file` (also long `--cmd` text and long
captured commands) store their text under `notes/<yyyy-mm>/` in the hub, named
by the SHA-256 of the text; the ledger keeps the path, the hash and the length.
See [RECORDS.md](RECORDS.md#note-bodies-and-command-text).

| Key | Default | Meaning |
|---|---|---|
| `notes.body_max_chars` | 200000 | characters allowed in one note body or command text (1,000 to 1,000,000) |

Note files are tracked by Git like threads, so a lower limit keeps the
repository small. `--note` itself stays a one-line summary of at most 1,000
characters.

## Adding settings for a feature or plugin

A feature module or plugin declares its own key group at import time:

```python
import rmem_config
rmem_config.add_group("my_feature", {"limit": 5}, {"limit": rmem_config.integer(1)})
```

Document each new group in this file under its own heading.
