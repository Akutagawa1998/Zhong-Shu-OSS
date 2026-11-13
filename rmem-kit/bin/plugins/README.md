# Project plugins

Put project-specific extensions here instead of editing kit files. The kit's
`bin/` files are recorded in `.kit-manifest.json`; `rmem check` warns when one
of them changes, and an upgrade can replace them. Files in this folder are yours.

After opt-in, `rmem` imports every `bin/plugins/<name>.py` (a regular file whose name starts
with a letter and uses only letters, digits and underscores) after its built-in
feature modules, in name order. A plugin is ordinary Python code that runs with
your permissions whenever `rmem` runs in this hub: review it like any script.
Use the Python standard library only.

Review the plugin files before setting `RMEM_TRUSTED_PLUGIN_HUB` to the absolute
path of a trusted hub in your shell or agent environment. Set it only while you
intend to run that hub's plugins. Keep the hub on a trusted revision and review
plugin changes before using it again.

Because a plugin runs as you, rmem loads it only when the hub folder, `bin/`,
`bin/plugins/` and the file itself belong to you (or root) and neither their
group nor other users can write to them; otherwise it skips the plugin and
`rmem doctor` says why (`chmod go-w` fixes the usual case). Plugins load only
for the hub the command uses after it validates, never for `rmem --help`,
`rmem version` or `rmem init`, and never when `RMEM_NO_PLUGINS=1` is set (the
Git guard hooks set it). The search for a hub from the working directory stops
at a directory that another user owns or can write to, such as `/tmp`, so a hub
planted there is never found; set `RMEM_HUB` to use a hub in such a place.

## What a plugin may define

Every name is optional.

```python
EVENT_KINDS = {"handoff_note"}          # new ledger kinds this plugin writes
EVENT_STRING_FIELDS = {"to"}            # optional event fields that must be strings

def validate_event(event):              # called for every ledger event
    return ""                           # "" when fine, otherwise a one-line problem

def register(commands, core):           # add subcommands to the argparse subparsers
    p = commands.add_parser("handoff", help="...")
    p.set_defaults(fn=lambda a: run_handoff(a, core))

def status_sections(ctx):               # extra lines near the top of `rmem status`
    return []

def check_problems(ctx):                # (level, message) pairs for `rmem check`
    return [("warn", "...")]            # level is "fail", "warn" or "info"

def resume_sections(ctx):               # extra lines near the end of the `rmem resume` card
    return []
```

`ctx` has `hub`, `events` (the ledger already read under the lock: do not read
it again), `config` (merged `hub-config.json`), `who` (agent/session, possibly
empty), `core` and `args`.

`core` is the stable surface of `bin/rmem`: `RmemError`, `find_hub`,
`validate_hub`, `hub_lock`, `events_locked`, `append_locked`, `actor`,
`one_line`, `identifier`, `count`, `regex`, `safe_path`, `atomic_write`,
`open_regular`, `bounded_lines`, `text_chars`, `text_excerpt`, `Output`,
`clipped`, `fmt_event`, `now`, `load_config`, `display_time`, `parse_user_time`,
`consistency_preflight`, `authorize_thread_write`, `describe_owner`,
`thread_visible`, `run_git`, `provenance`, `source_records`, `kit_root` and
`kit_version`, plus `event_problem(event)` (the ledger validator: "" or a
problem). Write to the ledger only through `append_locked` while holding
`hub_lock(hub)`, and ask `authorize_thread_write` before writing for a thread.
Lease helpers: `load_claim(hub, key)`, `lease_entries(hub)` (every `claims/` entry;
resource keys start with `@`), `lease_status(meta, events, config, key=KEY)` (`live`,
`last_activity`, `expires_at`, `ttl_hours`) and `held_leases(hub, who, events, config)`.
`section_lines(ctx, hook)` collects the bounded lines of every module's
`status_sections` or `resume_sections` hook.
`append_locked` adds `harness_session` and `effort` and refuses a forked session;
pass `who` to `consistency_preflight(hub, events=events, who=who)` to refuse it
before any side effect.

## Rules that keep the hub safe

- A plugin that fails to import, or whose hook raises, is skipped. `rmem doctor`
  and `rmem check` report it; read-only commands keep working.
- A plugin cannot replace a core command; such a registration is undone and
  reported.
- `--agent` and `--session` are added to every top-level command, so they work
  before or after the command name. Nested subcommands inside a plugin should call
  `core.add_identity_options(parser)` themselves.
- Keep output bounded (`core.Output`) and never rewrite past ledger events:
  corrections are new events.
- Plugin settings can live in `hub-config.json` under a key group named after the
  plugin. Declare it at import time with
  `rmem_config.add_group("my_plugin", {"limit": 5}, {"limit": rmem_config.integer(1)})`;
  undeclared keys only produce a `warn:` line in `rmem check`.
