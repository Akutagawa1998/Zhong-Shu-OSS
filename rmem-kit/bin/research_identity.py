"""Session identity: harness-derived session IDs, session_started/session_updated
events, fork detection and the read-only `whoami`.

Claude Code and Codex run every tool call in a fresh shell, so an exported
RMEM_SESSION does not persist. Without --session or RMEM_SESSION, rmem derives the
session from the conversation that runs it: CLAUDE_CODE_SESSION_ID becomes
claude-<full ID>, CODEX_THREAD_ID becomes codex-<full ID>. Every appended event
records the full harness ID as harness_session (and CLAUDE_EFFORT as effort).
A session first recorded under one conversation refuses writes from another one:
that is a forked or copied conversation, which needs its own session.

This module also lists resource leases in `status`; thread leases appear in the
thread list through core.describe_owner.
"""
import hashlib
import os
import re
import sys

import rmem_config

EVENT_KINDS = {"session_started", "session_updated"}
EVENT_STRING_FIELDS = {"harness", "harness_session", "effort", "model", "role", "parent_session",
                       "transcript", "session_source", "resource"}
# WHY: Codex is usually launched from inside Claude Code (and inherits its variables),
# rarely the reverse, so the Codex ID names the innermost conversation when both exist.
HARNESS_VARIABLES = (("codex", "CODEX_THREAD_ID"), ("claude", "CLAUDE_CODE_SESSION_ID"))
SAFE_HARNESS_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
SAFE_EFFORT = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,31}\Z")
SAFE_SESSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:@+-]{0,127}\Z")
SAFE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}\Z")
SECRET_PREFIXES = ("sk-", "sk_", "ghp_", "gho_", "ghs_", "ghu_", "github_pat_", "glpat-", "xox", "akia",
                   "aiza", "hf_", "eyj")
SECRET_WORDS = ("secret", "token", "passw", "apikey", "api_key")
TEST_SESSION = re.compile(r"(test|tmp|temp|fixture)([-_.:@+]|\Z)", re.IGNORECASE)
FIELD_LIMITS = {"role": 80, "model": 200, "effort": 40}


def recordable(value, pattern):
    """An environment value fit to store, or None: malformed or secret-looking values are never recorded."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    lowered = value.lower()
    if not pattern.fullmatch(value) or lowered.startswith(SECRET_PREFIXES) or any(w in lowered for w in SECRET_WORDS):
        return None
    return value


def harness(environ=None):
    """(name, full conversation ID) of the Claude Code or Codex conversation running rmem, or (None, None)."""
    environ = os.environ if environ is None else environ
    for name, variable in HARNESS_VARIABLES:
        value = recordable(environ.get(variable), SAFE_HARNESS_ID)
        if value:
            return name, value
    return None, None


def derived_session(environ=None):
    """claude-<full ID> or codex-<full ID> of the harness conversation; None outside a harness.
    WHY the full ID: Codex thread IDs are UUIDv7, whose leading digits are a timestamp, so a prefix
    would give conversations started within about a minute the same session. The full form is also
    the name earlier hubs used by convention, so a continuing conversation keeps its history."""
    name, value = harness(environ)
    if not name:
        return None
    session = f"{name}-{value}"
    return session if SAFE_SESSION.fullmatch(session) else f"{name}-{hashlib.sha256(value.encode()).hexdigest()[:32]}"


def session_is_derived(a, environ=None) -> bool:
    """Whether this process's session comes from the harness rather than --session or RMEM_SESSION."""
    source = identity_sources(a, environ)[1]
    return bool(source) and source.startswith("derived")


def shared_identity_warnings(a, environ=None) -> list:
    """Reasons the derived session may be shared with another agent; [] for an explicit session.
    Printed by whoami, status, claim and release; never a refusal (variables alone cannot tell
    Codex inside Claude Code from Claude Code inside a Codex shell)."""
    environ = os.environ if environ is None else environ
    if not session_is_derived(a, environ):
        return []
    session, warnings = derived_session(environ), []
    if environ.get("CLAUDE_CODE_CHILD_SESSION") and harness(environ)[0] == "claude":
        warnings.append(f"this process looks like a Claude Code subagent (CLAUDE_CODE_CHILD_SESSION is set): it shares "
                        f"the conversation ID with its parent and sibling subagents, so the derived session {session} "
                        "is theirs too and its leases do not separate you; pass your own --session ID on every call")
    if all(recordable(environ.get(variable), SAFE_HARNESS_ID) for _name, variable in HARNESS_VARIABLES):
        warnings.append(f"both CODEX_THREAD_ID and CLAUDE_CODE_SESSION_ID are set, so rmem uses the Codex ID ({session}); "
                        "that is right for Codex started from Claude Code. A Claude Code conversation started from a "
                        "shell that inherited CODEX_THREAD_ID must pass its own --session ID")
    return warnings


def agent_label(environ=None):
    environ = os.environ if environ is None else environ
    name = harness(environ)[0]
    if name:
        return name
    if environ.get("CLAUDECODE"):
        return "claude"
    return "codex" if any(key.startswith("CODEX_") for key in environ) else "human"


def env_effort(environ=None):
    environ = os.environ if environ is None else environ
    return recordable(environ.get("CLAUDE_EFFORT"), SAFE_EFFORT)


def process_fields(environ=None):
    """Fields append_locked adds to every event: the full harness ID and CLAUDE_EFFORT, when present."""
    fields = {}
    value = harness(environ)[1]
    if value:
        fields["harness_session"] = value
    effort = env_effort(environ)
    if effort:
        fields["effort"] = effort
    return fields


def identity_sources(a, environ=None):
    """(where the agent label came from, where the session came from or None)."""
    environ = os.environ if environ is None else environ
    name = harness(environ)[0]
    if getattr(a, "agent", None):
        agent = "flag (--agent)"
    elif environ.get("RMEM_AGENT"):
        agent = "env (RMEM_AGENT)"
    elif name or environ.get("CLAUDECODE") or any(key.startswith("CODEX_") for key in environ):
        agent = "detected harness"
    else:
        agent = "default"
    if getattr(a, "session", None):
        session = "flag (--session)"
    elif environ.get("RMEM_SESSION"):
        session = "env (RMEM_SESSION)"
    elif name:
        session = "derived (" + dict(HARNESS_VARIABLES)[name] + ")"
    else:
        session = None
    return agent, session


# Fork detection. A session is bound to the conversation of its latest session_started
# event, or else of its first event that recorded a harness ID. Writes without a harness
# ID (humans, scripts) are never checked.

def next_binding(bound, event):
    """Fold one earlier event of the same session into its conversation binding."""
    value = event.get("harness_session")
    if not isinstance(value, str) or not value:
        return bound
    if event.get("kind") == "session_started":
        return {"harness_session": value, "how": "its session start", "ts": event.get("ts")}
    return bound or {"harness_session": value, "how": "its first event", "ts": event.get("ts")}


def binding(events, session):
    bound = None
    if session:
        for event in events:
            if event.get("session") == session:
                bound = next_binding(bound, event)
    return bound


def fork_problem(session, current, bound, environ=None):
    """'' unless this session is bound to a different conversation than the current harness ID."""
    if not (session and current and bound) or bound["harness_session"] == current:
        return ""
    # WHY: the full IDs, because UUIDv7 IDs of conversations started close together share their first digits.
    if session == derived_session(environ):
        remedy = "Pass an explicit new --session ID on every call (the ID derived for this conversation is taken)"
    else:
        remedy = ("Use a new session ID (omit --session inside Claude Code or Codex so rmem derives one for this "
                  "conversation, or pass a new --session ID)")
    return (f"session '{session}' was recorded in another conversation ({bound['how']}, harness ID "
            f"{bound['harness_session']}), but this process runs in {current}: this looks like "
            f"a forked or copied conversation. {remedy}, then run `rmem session start --parent {session}`")


def current_fork(events, session, environ=None):
    return fork_problem(session, harness(environ)[1], binding(events, session), environ)


def bound_sessions(events, current):
    """Sessions bound to the conversation `current` (its full harness ID), most recently active first,
    as [(session, agent label of its latest event)]. Lets hooks, which always derive the session, find
    the explicit session a conversation actually writes under."""
    if not current:
        return []
    latest, bindings = {}, {}
    for position, event in enumerate(events):
        session = event.get("session")
        if not isinstance(session, str) or not session:
            continue
        bindings[session] = next_binding(bindings.get(session), event)
        latest[session] = (position, event.get("agent"))
    found = [(position, session, agent) for session, (position, agent) in latest.items()
             if bindings.get(session) and bindings[session]["harness_session"] == current]
    return [(session, agent) for _position, session, agent in sorted(found, reverse=True)]


def session_profile(events, session):
    """What the ledger records about one session: start, role/model/effort/parent, counts and last activity."""
    profile = {"events": 0, "last": None, "started": None, "fields": {}}
    for event in events:
        if not session or event.get("session") != session:
            continue
        profile["events"] += 1
        profile["last"] = event.get("ts")
        if event.get("kind") == "session_started":
            profile["started"] = event
        if event.get("kind") in EVENT_KINDS:
            for name in ("role", "model", "effort", "parent_session", "transcript"):
                if isinstance(event.get(name), str) and event[name]:
                    profile["fields"][name] = (event[name], event.get("ts"))
    return profile


def session_roles(events):
    """Latest recorded role per session (from session_started/session_updated), for role addressing."""
    roles = {}
    for event in events:
        if event.get("kind") in EVENT_KINDS and isinstance(event.get("role"), str) and event.get("session"):
            roles[event["session"]] = event["role"]
    return roles


# Commands

def recorded_fields(event):
    shown = [f"{name.replace('_', ' ')} {event[name]}" for name in ("role", "model", "effort", "parent_session") if event.get(name)]
    return " | " + ", ".join(shown) if shown else ""


def session_fields(a, core, config):
    """Validated role/model/effort values from the arguments, plus warnings to print."""
    fields, notes = {}, []
    roles = config.get("roles") or {}
    if getattr(a, "role", None) is not None:
        role = core.one_line(a.role, "role", FIELD_LIMITS["role"])
        if roles and role not in roles:
            raise core.RmemError(f"role {role!r} is not one of the roles in hub-config.json "
                                 f"({core.clipped(', '.join(sorted(roles)), 300)}); use one of them or add it there")
        if not roles:
            notes.append("hub-config.json configures no roles, so the role is recorded as free text; "
                         "list the project's roles under \"roles\" to validate them")
        fields["role"] = role
    for name in ("model", "effort"):
        value = getattr(a, name, None)
        if value is not None:
            fields[name] = core.one_line(value, name, FIELD_LIMITS[name])
    return fields, notes


def start(a, core):
    hub, who = core.find_hub(), core.actor(a, required=True)
    config = core.load_config(hub)
    fields, notes = session_fields(a, core, config)
    if a.parent is not None:
        if not SAFE_SESSION.fullmatch(a.parent):
            raise core.RmemError("--parent must be a session ID: 1–128 letters/digits/._:@+- starting with a letter or digit")
        if a.parent == who["session"]:
            hint = ("pass an explicit new --session ID: the ID derived for this conversation is the parent itself"
                    if session_is_derived(a) else "omit --session inside Claude Code or Codex to derive one, "
                    "or pass a new --session ID")
            raise core.RmemError(f"--parent names the earlier session, not this one; a forked conversation needs a "
                                 f"new session ID ({hint})")
        fields["parent_session"] = a.parent
    if a.transcript is not None:
        fields["transcript"] = core.one_line(a.transcript, "transcript", 4096)
    event = {**who, "kind": "session_started", **fields,
             "session_source": identity_sources(a)[1].split(" ")[0]}
    name = harness()[0]
    if name:
        event["harness"] = name
    with core.hub_lock(hub):
        events = list(core.events_locked(hub))
        core.consistency_preflight(hub, events=events, who=who)
        if a.parent and not any(e.get("session") == a.parent for e in events):
            notes.append(f"the parent session {a.parent} has no events in this ledger; check the ID")
        recorded = core.append_locked(hub, event)
    for note in notes:
        print("rmem: warning: " + note, file=sys.stderr)
    print("session started: " + core.fmt_event(recorded, config) + recorded_fields(recorded))
    return 0


def update(a, core):
    hub, who = core.find_hub(), core.actor(a, required=True)
    config = core.load_config(hub)
    fields, notes = session_fields(a, core, config)
    if not fields:
        raise core.RmemError("give at least one of --model, --effort or --role")
    with core.hub_lock(hub):
        events = list(core.events_locked(hub))
        core.consistency_preflight(hub, events=events, who=who)
        if session_profile(events, who["session"])["started"] is None:
            notes.append("this session has no session_started event; `rmem session start` records its parent and transcript")
        recorded = core.append_locked(hub, {**who, "kind": "session_updated", **fields})
    for note in notes:
        print("rmem: warning: " + note, file=sys.stderr)
    print("session updated: " + core.fmt_event(recorded, config) + recorded_fields(recorded))
    return 0


def whoami(a, core):
    """Read-only: identity of this process and what the hub records about its session."""
    out, who = core.Output(), core.actor(a)
    agent_from, session_from = identity_sources(a)
    name, value = harness()
    session = who.get("session")
    out.add(f"agent: {who['agent']} (from {agent_from})")
    out.add(f"session: {session} (from {session_from})" if session else
            "session: none (pass --session ID on every call, or run inside Claude Code or Codex so rmem derives one)")
    out.add(f"harness: {name}, conversation ID {value}" if name else
            "harness: none detected (no CLAUDE_CODE_SESSION_ID or CODEX_THREAD_ID; fork checks do not apply)")
    for warning in shared_identity_warnings(a):
        out.add("warning: " + warning)
    try:
        hub = core.find_hub()
    except core.RmemError as exc:
        out.add(f"hub: none found ({core.clipped(exc, 300)}); role, model and effort: unknown")
        out.emit()
        return 0
    try:
        config = core.load_config(hub)
    except core.RmemError:
        config = rmem_config.defaults()
    with core.hub_lock(hub, shared=True, readonly_ok=True):
        events = list(core.events_locked(hub))
    profile = session_profile(events, session)
    recorded = profile["fields"]
    for field in ("role", "model", "effort"):
        if field in recorded:
            text, ts = recorded[field]
            out.add(f"{field}: {core.clipped(text, 200)} (recorded {core.display_time(ts, config)})")
        elif field == "effort" and env_effort():
            out.add(f"effort: {env_effort()} (from CLAUDE_EFFORT; not recorded by a session start or update)")
        else:
            out.add(f"{field}: unknown")
    parent = recorded.get("parent_session")
    out.add(f"parent: {parent[0]}" if parent else "parent: none recorded")
    started = profile["started"]
    out.add(f"started: {core.display_time(started['ts'], config)}" if started else
            "started: not recorded; `rmem session start [--parent S] [--role R] [--model M]` records it")
    problem = current_fork(events, session)
    if problem:
        out.add("fork check: REFUSED: " + problem)
    else:
        out.add("fork check: ok" if value and session else "fork check: not applicable")
    if session:
        out.add(f"events by this session: {profile['events']}" +
                (f", last activity {core.display_time(profile['last'], config)}" if profile["last"] else ""))
        held = core.held_leases(hub, who, events, config)
        out.add(f"leases held: {len(held)}")
        for key, _meta, lease in held[:20]:
            state = "live" if lease["live"] else "expired"
            out.add(f"- {key}: {state}, last activity {core.display_time(lease['last_activity'].isoformat(), config)}, "
                    f"lease {lease['ttl_hours']:g}h")
    out.emit()
    return 0


def register(commands, core):
    p = commands.add_parser("session", help="record this working session's start, or a model, effort or role change")
    actions = p.add_subparsers(dest="session_command", required=True)
    p.rmem_choices = actions.choices
    s = actions.add_parser("start", help="write session_started with parent, role, model, effort and transcript")
    s.add_argument("--parent", help="the session this one continues or was forked from")
    s.add_argument("--role", help="a role from hub-config.json roles (free text when none are configured)")
    s.add_argument("--model")
    s.add_argument("--effort", help="reasoning effort; CLAUDE_EFFORT is recorded when this is omitted")
    s.add_argument("--transcript", help="path or pointer to this conversation's transcript")
    s.set_defaults(fn=lambda a: start(a, core))
    u = actions.add_parser("update", help="write session_updated after a mid-session model, effort or role change")
    u.add_argument("--role")
    u.add_argument("--model")
    u.add_argument("--effort")
    u.set_defaults(fn=lambda a: update(a, core))
    w = commands.add_parser("whoami", help="this process's agent, session, harness, role, model and effort (read-only)")
    w.set_defaults(fn=lambda a: whoami(a, core))


# Hooks for status and check

def status_sections(ctx):
    lines = []
    problem = current_fork(ctx.events, ctx.who.get("session"))
    if problem:
        lines += ["## Identity warning: writes from this process are refused", "- " + problem]
    shared = shared_identity_warnings(getattr(ctx, "args", None))
    if shared:
        lines += ["## Identity warning: this session may be shared"] + ["- " + text for text in shared]
    resources = [key for key, _meta, _problem in ctx.core.lease_entries(ctx.hub) if key.startswith("@")]
    if resources:
        lines.append("## Resource leases (outside the hub)")
        lines += [f"- {key}: {ctx.core.describe_owner(ctx, key)}" for key in resources[:10]]
        if len(resources) > 10:
            lines.append(f"[{len(resources) - 10} more resource leases; rmem check reports expired ones]")
    return lines


def check_problems(ctx):
    problems, harnesses, tests = [], {}, set()
    for event in ctx.events:
        session = event.get("session")
        if not isinstance(session, str):
            continue
        value = event.get("harness_session")
        if isinstance(value, str) and value:
            harnesses.setdefault(session, set()).add(value)
        if TEST_SESSION.match(session):
            tests.add(session)
    forked = sorted(session for session, values in harnesses.items() if len(values) > 1)
    if forked:
        problems.append(("warn", f"{len(forked)} session(s) have events from more than one conversation, a sign of a "
                         f"forked or copied session: {', '.join(forked[:5])}; give each conversation its own "
                         "session and link it with `rmem session start --parent OLD`"))
    if tests:
        problems.append(("info", f"{len(tests)} session(s) are named like tests ({', '.join(sorted(tests)[:3])}); "
                         "run tests against a temporary hub (RMEM_HUB) rather than the project ledger"))
    return problems


def validate_event(event):
    kind = event.get("kind")
    if kind in ("claim", "release"):
        if event.get("resource") is not None:
            if event.get("thread"):
                return f"{kind} names either a thread or a resource, not both"
            if not isinstance(event["resource"], str) or not SAFE_NAME.fullmatch(event["resource"]):
                return "event resource must be 1–80 letters/digits/._- starting with a letter or digit"
        ttl = event.get("lease_ttl_hours")
        if ttl is not None and (not isinstance(ttl, (int, float)) or isinstance(ttl, bool) or not 0 < ttl < float("inf")):
            return "event lease_ttl_hours must be a positive number"
        if "takeover" in event and not isinstance(event["takeover"], bool):
            return "event takeover must be a boolean"
    elif kind in EVENT_KINDS and not event.get("session"):
        return f"{kind} requires a session"
    return ""
