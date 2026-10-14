"""Handoff between sessions: requests with an addressee and a state, collaboration lessons,
pending items, checkpoints, the bounded `rmem resume` card and optional harness hooks.

A request is addressed to `thread:T`, `role:R` or `session:S` and moves open -> acked ->
done, withdrawn or rejected. It may be written into a thread that another session leases.
A lesson is one collaboration rule with its reason, kept as lessons/<ID>.md plus a ledger
event; a newer lesson can supersede it. Pending items (for example options given to the
user) and checkpoints are append-only events about the writer's own conversation.

`rmem resume` prints what a session needs after compaction, a resume or a fork: identity,
requests addressed to it, pending items, leases with their Next step sections, its latest
checkpoints and events, decisions by other sessions since its last event, lessons and the
authority entries of MAP.md. `rmem hooks install` prints (or merges with --write) the
Claude Code settings that run it at session start and write a checkpoint before compaction.
"""
import argparse
import datetime as dt
import json
import os
from pathlib import Path
import re
import select
import shlex
import shutil
import sys
import time
from types import SimpleNamespace

import research_identity as identity
import rmem_config

EVENT_KINDS = {"request", "request_acked", "request_closed", "lesson", "pending", "pending_closed", "checkpoint"}
# WHY: these fields are validated per kind in validate_event, not globally, so a legacy row
# that happens to use one of these names for something else never turns CORRUPT.
EVENT_STRING_FIELDS = set()
TEXT_FIELDS = ("to", "quote", "source_event", "by_event", "relation", "rule", "why", "applies_to",
               "lesson_path", "lesson_sha256", "hook_source")
FLAG_FIELDS = ("blocking", "user_decision", "auto")
REQUEST_KINDS = ("request", "request_acked", "request_closed")
# Events that address or hold a thread without working on it (a release even says the work stopped).
NOT_WORK_KINDS = (*REQUEST_KINDS, "claim", "release")
CLOSE_STATUSES = ("done", "withdrawn", "rejected")
SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}\Z")
ADDRESS = re.compile(r"(thread|role|session):(.+)\Z")
SEQUENCE_REF = re.compile(r"(?:#|seq:)?([0-9]{1,7})\Z")
EVENT_ID_REF = re.compile(r"[0-9a-f]{8,32}\Z")
HEADING = re.compile(r" {0,3}(#{1,6})\s")
NEXT_HEADING = re.compile(r" {0,3}(#{1,6})\s+next\s+steps?\b", re.IGNORECASE)
AUTHORITY = re.compile(r"authorit|controller|canonical", re.IGNORECASE)
STATUS_REQUESTS, STATUS_LESSONS = 5, 5
HOOK_INPUT_MAX, HOOK_INPUT_SECONDS = 1024 * 1024, 2.0
CARD_TRAILER = ("\n[resume card cut at its character budget; raise --max-chars, or read `rmem inbox`, "
                "`rmem pending list` and `rmem status --thread SLUG`]\n")
SESSION_START_MATCHERS = ("startup", "resume", "compact")
PRE_COMPACT_MATCHERS = ("auto", "manual")
SETTINGS_NAME = "settings.local.json"   # per user and device; Claude Code keeps it out of Git

if "resume" not in rmem_config.DEFAULTS:
    rmem_config.add_group("resume", {"max_chars": 6000, "recent_events": 8},
                          {"max_chars": rmem_config.integer(1000, 100000),
                           "recent_events": rmem_config.integer(0, 50)})


# Small helpers

def when(ts):
    try:
        parsed = dt.datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def utc_now():
    return dt.datetime.now(dt.timezone.utc)


def age_days(ts, now) -> float:
    start = when(ts)
    return max(0.0, (now - start).total_seconds() / 86400) if start else 0.0


def address(value) -> str:
    """'thread:T', 'role:R' or 'session:S' (normalized), or '' when malformed."""
    match = ADDRESS.fullmatch(value.strip()) if isinstance(value, str) else None
    if not match:
        return ""
    kind, name = match.groups()
    if kind == "thread":
        valid = SAFE_ID.fullmatch(name)
    elif kind == "session":
        valid = identity.SAFE_SESSION.fullmatch(name)
    else:
        valid = name == name.strip() and len(name) <= 80 and not any(c in name for c in "\r\n\0")
    return f"{kind}:{name}" if valid else ""


def applies_address(value) -> str:
    """'all', 'thread:T' or 'role:R' for lessons; '' when malformed."""
    if isinstance(value, str) and value.strip() == "all":
        return "all"
    result = address(value)
    return result if result and not result.startswith("session:") else ""


def thread_known(core, hub, slug, events) -> bool:
    return (core.safe_path(hub, "threads", f"{slug}.md").exists() or core.safe_path(hub, "claims", slug).exists()
            or any(event.get("thread") == slug for event in events))


def require_thread(core, hub, slug, events):
    if not thread_known(core, hub, slug, events):
        raise core.RmemError(f"thread '{slug}' does not exist (no threads/{slug}.md and no ledger events); check the slug")


def event_reference(core, events, value, option):
    """(stored label, event) for an event ID, 8+ leading hex digits of one, or #SEQUENCE as `rmem recent` shows."""
    text = value.strip()
    sequence = SEQUENCE_REF.fullmatch(text)
    if sequence:
        number = int(sequence.group(1))
        matches = [e for i, e in enumerate(events, 1) if (e.get("sequence") if type(e.get("sequence")) is int else i) == number]
    elif EVENT_ID_REF.fullmatch(text.lower()):
        matches = [e for e in events if isinstance(e.get("event_id"), str) and e["event_id"].startswith(text.lower())]
    else:
        matches = []
    if len(matches) > 1:
        raise core.RmemError(f"{option} {core.clipped(value, 80)!r} matches {len(matches)} events; give more digits of the event ID")
    if not matches:
        raise core.RmemError(f"{option} {core.clipped(value, 80)!r} matches no ledger event; give an event ID (or its first "
                             "8 or more hex digits) or #SEQUENCE as `rmem recent` shows it (quote '#42' in the shell)")
    event = matches[0]
    if event.get("event_id"):
        return event["event_id"], event
    position = next(i for i, e in enumerate(events, 1) if e is event)
    return f"#{event.get('sequence', position)}", event


def ensure_new_id(core, events, ident, label, own_kinds, reusable=False):
    """Refuse an ID already used by another kind of event (or by an open item of this kind)."""
    for event in events:
        if event.get("id") != ident:
            continue
        if event.get("kind") not in own_kinds:
            raise core.RmemError(f"ID {ident} is already used by a {core.clipped(event.get('kind'), 40)} event; choose a "
                                 f"distinct {label} ID, for example with a prefix such as {label[:3].upper()}-{ident}")
        if not reusable:
            raise core.RmemError(f"{label} {ident} already exists; choose a new ID")


def text_option(core, value, name, limit):
    return core.one_line(value, name, limit) if value is not None else None


# Ledger views

def requests(events) -> dict:
    """Request ID -> {'event', 'state', 'acked', 'closed'} in ledger order; the first request with an ID wins."""
    table = {}
    for event in events:
        kind, ident = event.get("kind"), event.get("id")
        if kind not in REQUEST_KINDS or not isinstance(ident, str):
            continue
        item = table.get(ident)
        if kind == "request":
            table.setdefault(ident, {"event": event, "state": "open", "acked": None, "closed": None})
        elif item is None or item["closed"]:
            continue
        elif kind == "request_acked":
            item["acked"] = item["acked"] or event
            item["state"] = "acked"
        else:
            item["closed"] = event
            item["state"] = event.get("status") if event.get("status") in CLOSE_STATUSES else "done"
    return table


def open_requests(events) -> list:
    """Open and acknowledged requests, blocking first, then oldest first."""
    items = [item for item in requests(events).values() if not item["closed"]]
    return sorted(items, key=lambda item: (not item["event"].get("blocking"), item["event"].get("ts", "")))


def pending_items(events) -> dict:
    """Open pending items by ID (a later pending_closed with the same ID closes one)."""
    table = {}
    for event in events:
        if event.get("kind") == "pending" and isinstance(event.get("id"), str):
            table[event["id"]] = event
        elif event.get("kind") == "pending_closed":
            table.pop(event.get("id"), None)
    return table


def lessons(events) -> dict:
    """Lesson ID -> {'event', 'superseded_by'}; the first lesson with an ID wins."""
    table = {}
    for event in events:
        if event.get("kind") != "lesson" or not isinstance(event.get("id"), str):
            continue
        table.setdefault(event["id"], {"event": event, "superseded_by": None})
        old = event.get("supersedes")
        if isinstance(old, str) and old in table and old != event["id"] and not table[old]["superseded_by"]:
            table[old]["superseded_by"] = event["id"]
    return table


def active_lessons(events, targets=None) -> list:
    """Lessons not superseded; with targets, only those for 'all' or one of the targets."""
    items = [item for item in lessons(events).values() if not item["superseded_by"]]
    if targets is not None:
        items = [item for item in items if item["event"].get("applies_to") in ({"all"} | set(targets))]
    return items


def parents(events) -> dict:
    links = {}
    for event in events:
        parent = event.get("parent_session")
        if event.get("kind") == "session_started" and isinstance(event.get("session"), str) and isinstance(parent, str) and parent:
            links[event["session"]] = parent
    return links


def lineage(events, session, depth=3) -> list:
    """The session, its parent and further ancestors (from session start --parent), nearest first."""
    chain, links = [], parents(events)
    while session and session not in chain and len(chain) <= depth:
        chain.append(session)
        session = links.get(session)
    return chain


def role_threads(config, role) -> list:
    value = (config.get("roles") or {}).get(role) if role else None
    threads = value.get("threads") if isinstance(value, dict) else None
    return [t for t in threads if isinstance(t, str) and SAFE_ID.fullmatch(t)] if isinstance(threads, list) else []


def worked_threads(events, sessions, config, now) -> set:
    """Threads the sessions wrote to recently (claims and releases aside): since the session's latest session_started, or within
    requests.ack_warn_days. Logging needs no lease, so a lease alone does not say who works on a thread."""
    window = now - dt.timedelta(days=config["requests"]["ack_warn_days"])
    started, threads = {}, set()
    for event in events:
        session = event.get("session")
        if session not in sessions:
            continue
        stamp = when(event.get("ts"))
        if event.get("kind") == "session_started" and stamp:
            started[session] = stamp
        thread = event.get("thread")
        if (isinstance(thread, str) and SAFE_ID.fullmatch(thread) and event.get("kind") not in NOT_WORK_KINDS and stamp
                and (stamp >= window or (session in started and stamp >= started[session]))):
            threads.add(thread)
    return threads


def caller_scope(core, hub, events, config, who, extra_threads=(), with_lineage=True):
    """The addresses that reach the caller: its session (and ancestors), its role, the threads it leases,
    the threads it (or an ancestor) wrote to recently, the threads its role lists in hub-config.json roles,
    and any extra threads."""
    session = who.get("session")
    sessions = (lineage(events, session) if with_lineage else [session]) if session else []
    roles = identity.session_roles(events)
    role = next((roles[s] for s in sessions if s in roles), None)
    threads = set(extra_threads) | set(role_threads(config, role)) | worked_threads(events, sessions, config, utc_now())
    held = core.held_leases(hub, who, events, config) if session else []
    threads |= {key for key, _meta, _lease in held if not key.startswith("@")}
    targets = {f"session:{s}" for s in sessions} | {f"thread:{t}" for t in threads}
    if role:
        targets.add(f"role:{role}")
    return SimpleNamespace(session=session, sessions=sessions, role=role, threads=sorted(threads), targets=targets, held=held)


def for_caller(items, scope) -> list:
    """Open requests that reach the caller, except the ones this very session sent (a request it filed in a
    thread it works on is for whoever works there next, not a task for itself)."""
    return [item for item in items if item["event"].get("to") in scope.targets
            and (item["event"].get("session") != scope.session or item["event"].get("to") == f"session:{scope.session}")]


def last_activity(events, sessions):
    times = [when(e.get("ts")) for e in events if e.get("session") in sessions]
    times = [t for t in times if t is not None]
    return max(times) if times else None


def is_decision(event) -> bool:
    kind = event.get("kind")
    return (kind == "decision" or (kind == "pending_closed" and bool(event.get("quote")))
            or (kind == "request" and bool(event.get("user_decision"))))


def decisions_since(events, sessions, since) -> list:
    return [e for e in events if e.get("session") not in sessions and is_decision(e)
            and (since is None or (when(e.get("ts")) or since) > since)]


# Lines shared by status, inbox and resume

def request_line(core, item, now, detail=False) -> str:
    event = item["event"]
    bits = [core.clipped(event.get("id"), 80), item["state"] + (", blocking" if event.get("blocking") else ""),
            f"open {age_days(event.get('ts'), now):.1f} days", f"to {core.clipped(event.get('to'), 120)}",
            f"from {core.clipped(event.get('agent', '?'), 40)} / {core.clipped(event.get('session', '?'), 128)}"]
    if event.get("thread"):
        bits.append(f"in thread {event['thread']}")
    text = " · ".join(bits) + " — " + core.clipped(event.get("note", ""), 300 if detail else 160)
    if detail:
        cited = []
        if event.get("source_event"):
            cited.append(f"cites event {core.clipped(event['source_event'], 40)}")
        if event.get("quote"):
            cited.append(f"quote \"{core.clipped(event['quote'], 300)}\"")
        if event.get("user_decision"):
            cited.append("carries a user decision")
        if cited:
            text += "\n  " + "; ".join(cited)
    return text


def applies_text(value) -> str:
    if value == "all":
        return "every session on this project"
    kind, _, name = str(value).partition(":")
    return f"work on thread {name}" if kind == "thread" else f"sessions with role {name}"


def lesson_line(core, item, limit=200) -> str:
    event = item["event"]
    extra = f", superseded by {item['superseded_by']}" if item["superseded_by"] else ""
    return f"{event['id']} (applies to {core.clipped(event.get('applies_to'), 100)}{extra}): {core.clipped(event.get('rule'), limit)}"


def lesson_section(core, items, limit) -> list:
    if not items:
        return []
    lines = [f"## Lessons that apply: {len(items)} (rmem lesson list)"]
    lines += ["- " + lesson_line(core, item, 160) for item in items[-limit:]]
    if len(items) > limit:
        lines.append(f"[{len(items) - limit} older lessons not shown: rmem lesson list]")
    return lines


# Requests

def authorize(core, hub, who, events, thread):
    """Integrity and fork checks; a thread-scoped request is authorized with purpose 'request' (never lease-refused)."""
    core.consistency_preflight(hub, events=events, who=who)
    if thread:
        core.authorize_thread_write(hub, thread, who, "request")


def cmd_request(a, core):
    hub, who = core.find_hub(), core.actor(a, required=True)
    config = core.load_config(hub)
    to = address(a.to)
    if not to:
        raise core.RmemError("--to must be thread:SLUG, role:ROLE or session:SESSION, for example --to role:writer")
    note = core.one_line(a.note, "note", 2000)
    quote = text_option(core, a.quote, "quote", 2000)
    if a.user_decision and not (a.source or quote):
        raise core.RmemError("--user-decision needs the user's own words: cite the event that recorded them with --source "
                             "EVENT_ID, or pass --quote \"the user's exact words\". A session's proposal is not a user decision")
    kind, name = to.split(":", 1)
    thread = a.thread or (name if kind == "thread" else None)
    roles, notes = config.get("roles") or {}, []
    if kind == "role" and roles and name not in roles:
        raise core.RmemError(f"role {name!r} is not one of the roles in hub-config.json "
                             f"({core.clipped(', '.join(sorted(roles)), 300)}); address one of them")
    with core.hub_lock(hub):
        events = list(core.events_locked(hub))
        authorize(core, hub, who, events, thread)
        for slug in {thread, name if kind == "thread" else None} - {None}:
            require_thread(core, hub, slug, events)
        ensure_new_id(core, events, a.id, "request", REQUEST_KINDS)
        if kind == "role" and name not in identity.session_roles(events).values():
            notes.append(f"no session has recorded role {name} yet; it sees this request after `rmem session start --role {name}`")
        if kind == "session" and not any(e.get("session") == name for e in events):
            notes.append(f"session {name} has no events in this ledger; check the ID")
        event = {**who, "kind": "request", "id": a.id, "to": to, "note": note}
        if thread:
            event["thread"] = thread
        if a.blocking:
            event["blocking"] = True
        if a.source:
            event["source_event"] = event_reference(core, events, a.source, "--source")[0]
        if quote:
            event["quote"] = quote
        if a.user_decision:
            event["user_decision"] = True
        recorded = core.append_locked(hub, event)
    for line in notes:
        print("rmem: warning: " + line, file=sys.stderr)
    print("request recorded: " + core.fmt_event(recorded, config))
    print(f"The addressee sees it in `rmem status`, `rmem inbox` and `rmem resume`, acknowledges it with `rmem ack {a.id}` "
          f"and closes it with `rmem close {a.id} --by EVENT` (or --reason TEXT).")
    return 0


def relation(request, who, scope) -> str:
    if who.get("session") == request.get("session"):
        return "requester"
    return "addressee" if request.get("to") in scope.targets else "other"


def request_update(a, core, kind):
    """ack or close one request; closing or acknowledging another session's request is allowed and recorded."""
    hub, who = core.find_hub(), core.actor(a, required=True)
    config = core.load_config(hub)
    ident = a.identifier
    if kind == "request_closed":
        if not (a.by or a.reason):
            raise core.RmemError("close needs --by EVENT (the event that completed the request) or --reason TEXT")
        note = text_option(core, a.reason, "reason", 2000)
    else:
        note = text_option(core, a.note, "note", 2000)
    with core.hub_lock(hub):
        events = list(core.events_locked(hub))
        core.consistency_preflight(hub, events=events, who=who)
        item = requests(events).get(ident)
        if item is None:
            raise core.RmemError(f"no request {ident}; `rmem inbox --all` lists the open requests")
        request = item["event"]
        if item["closed"]:
            closed = item["closed"]
            raise core.RmemError(f"request {ident} is already closed ({item['state']}) by {closed.get('agent')} / "
                                 f"{closed.get('session')} at {core.display_time(closed.get('ts'), config)}")
        if kind == "request_acked" and item["acked"]:
            acked = item["acked"]
            print(f"already acknowledged by {acked.get('agent')} / {acked.get('session')} at "
                  f"{core.display_time(acked.get('ts'), config)}; nothing written")
            return 0
        if request.get("thread"):
            core.authorize_thread_write(hub, request["thread"], who, "request")
        scope = caller_scope(core, hub, events, config, who, with_lineage=True)
        event = {**who, "kind": kind, "id": ident, "relation": relation(request, who, scope)}
        if request.get("thread"):
            event["thread"] = request["thread"]
        if kind == "request_closed":
            event["status"] = a.status
            if a.by:
                event["by_event"] = event_reference(core, events, a.by, "--by")[0]
        if note:
            event["note"] = note
        recorded = core.append_locked(hub, event)
    if recorded["relation"] == "other":
        print(f"rmem: note: request {ident} was sent by {request.get('session')} to {request.get('to')}; this session is "
              "neither, so the event records relation=other", file=sys.stderr)
    verb = "acknowledged" if kind == "request_acked" else f"closed ({a.status})"
    print(f"{verb}: " + core.fmt_event(recorded, config))
    return 0


def cmd_inbox(a, core):
    hub, who = core.find_hub(), reader_identity(a, core)
    config = core.load_config(hub)
    with core.hub_lock(hub, shared=True, readonly_ok=True):
        events = list(core.events_locked(hub))
        scope = caller_scope(core, hub, events, config, who)
    items, now = open_requests(events), utc_now()
    if a.all:
        heading, selected = "Open requests in this hub (every addressee)", items
    elif a.for_address:
        target = address(a.for_address)
        if not target:
            raise core.RmemError("--for must be thread:SLUG, role:ROLE or session:SESSION")
        heading, selected = f"Open requests addressed to {target}", [i for i in items if i["event"].get("to") == target]
    else:
        if not scope.session:
            raise core.RmemError("no session: pass --session ID (or run inside Claude Code or Codex), "
                                 "or name the addressee with --for ADDRESS, or use --all")
        reach = [f"session {scope.session}"] + ([f"role {scope.role}"] if scope.role else [])
        if scope.threads:
            reach.append("threads " + ", ".join(scope.threads[:8]) + (" …" if len(scope.threads) > 8 else ""))
        heading = "Open requests for " + "; ".join(reach)
        selected = for_caller(items, scope)
    out = core.Output(config["caps"]["output_max_chars"])
    blocking = sum(1 for item in selected if item["event"].get("blocking"))
    out.add(f"# {heading}: {len(selected)}" + (f" ({blocking} blocking)" if blocking else ""))
    for item in selected[:a.n]:
        out.add("- " + request_line(core, item, now, detail=True))
    if len(selected) > a.n:
        out.add(f"[{len(selected) - a.n} more not shown; raise -n]")
    if selected:
        out.add("Acknowledge with `rmem ack ID`; close with `rmem close ID --by EVENT` or `--reason TEXT "
                "[--status withdrawn|rejected]`; `rmem show ID` lists a request's history.")
    out.emit()
    return 0


def reader_identity(a, core) -> dict:
    try:
        return core.actor(a)
    except core.RmemError:
        return {}


# Lessons

def lesson_text(event) -> str:
    lines = [f"# Lesson {event['id']}", "", f"Rule: {event['rule']}", "", f"Why: {event['why']}", "",
             f"Applies to: {event['applies_to']}"]
    if event.get("source_event"):
        lines.append(f"Source: ledger event {event['source_event']}")
    if event.get("quote"):
        lines.append(f"The user's words: \"{event['quote']}\"")
    if event.get("supersedes"):
        lines.append(f"Supersedes: lesson {event['supersedes']} (lessons/{event['supersedes']}.md)")
    lines += ["", f"Recorded by {event['agent']} / {event['session']}. The ledger event is the source of truth: "
              f"record a change as a new lesson with `rmem lesson add --supersedes {event['id']}`; do not edit this file."]
    return "\n".join(lines) + "\n"


def cmd_lesson_add(a, core):
    hub, who = core.find_hub(), core.actor(a, required=True)
    config = core.load_config(hub)
    applies = applies_address(a.applies_to)
    if not applies:
        raise core.RmemError("--applies-to must be all, thread:SLUG or role:ROLE")
    fields = {"rule": core.one_line(a.rule, "rule", 500), "why": core.one_line(a.why, "why", 1000), "applies_to": applies}
    quote, notes = text_option(core, a.quote, "quote", 2000), []
    roles = config.get("roles") or {}
    if applies.startswith("role:") and roles and applies[5:] not in roles:
        raise core.RmemError(f"role {applies[5:]!r} is not one of the roles in hub-config.json; use one of them")
    directory = core.safe_path(hub, "lessons")
    path = core.safe_path(hub, "lessons", f"{a.id}.md")
    with core.hub_lock(hub):
        events = list(core.events_locked(hub))
        core.consistency_preflight(hub, events=events, who=who)
        ensure_new_id(core, events, a.id, "lesson", ("lesson",))
        table = lessons(events)
        if a.supersedes:
            old = table.get(a.supersedes)
            if old is None:
                raise core.RmemError(f"no lesson {a.supersedes} to supersede; `rmem lesson list --all` lists them")
            if old["superseded_by"]:
                raise core.RmemError(f"lesson {a.supersedes} is already superseded by {old['superseded_by']}; supersede that one")
        if applies.startswith("thread:") and not thread_known(core, hub, applies[7:], events):
            notes.append(f"thread {applies[7:]} does not exist yet; the lesson applies once it does")
        if path.exists() or path.is_symlink():
            raise core.RmemError(f"lessons/{a.id}.md already exists without a lesson event; move it away or choose another ID")
        event = {**who, "kind": "lesson", "id": a.id, **fields}
        if a.source:
            event["source_event"] = event_reference(core, events, a.source, "--source")[0]
        if quote:
            event["quote"] = quote
        if a.supersedes:
            event["supersedes"] = a.supersedes
        directory.mkdir(exist_ok=True)
        core.atomic_write(path, lesson_text(event))
        try:
            recorded = core.append_locked(hub, {**event, "lesson_path": f"lessons/{a.id}.md",
                                                "lesson_sha256": core.file_sha256(path)})
        except BaseException:
            path.unlink(missing_ok=True)
            raise
    for line in notes:
        print("rmem: warning: " + line, file=sys.stderr)
    print(f"lesson recorded → lessons/{a.id}.md: " + core.fmt_event(recorded, config))
    return 0


def lesson_filter(core, value):
    if value is None:
        return None
    target = applies_address(value)
    if not target:
        raise core.RmemError("--applies-to must be all, thread:SLUG or role:ROLE")
    return target


def selected_lessons(events, target, include_superseded=False) -> list:
    items = list(lessons(events).values())
    if not include_superseded:
        items = [item for item in items if not item["superseded_by"]]
    if target == "all":
        return [item for item in items if item["event"].get("applies_to") == "all"]
    if target:
        return [item for item in items if item["event"].get("applies_to") in ("all", target)]
    return items


def read_events(core, hub):
    with core.hub_lock(hub, shared=True, readonly_ok=True):
        return list(core.events_locked(hub))


def cmd_lesson_list(a, core):
    hub = core.find_hub()
    config = core.load_config(hub)
    target = lesson_filter(core, a.applies_to)
    items = selected_lessons(read_events(core, hub), target, a.all)
    out = core.Output(config["caps"]["output_max_chars"])
    scope = f" for {target} (with lessons for all)" if target and target != "all" else (" for all" if target else "")
    out.add(f"# {len(items)} {'lessons, including superseded ones' if a.all else 'active lessons'}{scope}")
    for item in items[-a.n:] if a.n else []:
        event = item["event"]
        out.add("- " + lesson_line(core, item, 500))
        cited = f"; source event {event['source_event']}" if event.get("source_event") else ""
        cited += f"; the user's words \"{core.clipped(event['quote'], 300)}\"" if event.get("quote") else ""
        out.add(f"  why: {core.clipped(event.get('why'), 600)}{cited}")
    if len(items) > a.n:
        out.add(f"[{len(items) - a.n} older lessons not shown; raise -n]")
    out.emit()
    return 0


def project_name(hub) -> str:
    return hub.parent.name if hub.name.startswith(".") else hub.name


def cmd_lesson_export(a, core):
    """Print lessons as Markdown for Claude auto-memory or a Codex AGENTS.md; never writes those files."""
    hub = core.find_hub()
    config = core.load_config(hub)
    target = lesson_filter(core, a.applies_to)
    items = selected_lessons(read_events(core, hub), target)
    out = core.Output(config["caps"]["output_max_chars"])
    stamp = core.display_time(core.now(), config)
    origin = (f"Exported by `rmem lesson export --format {a.format}` at {stamp} from {hub}. The hub is the source "
              "of truth: re-export after a change instead of editing this text.")
    if a.format == "claude-memory":
        for line in ("---", f"name: {project_name(hub)} collaboration lessons (rmem)",
                     f"description: {len(items)} collaboration rules recorded in the project's rmem hub; re-export instead of editing",
                     "type: feedback", "---", "", f"<!-- {origin} -->", ""):
            out.add(line)
        for item in items:
            event = item["event"]
            out.add(f"- {event['rule']}")
            out.add(f"  **Why:** {event['why']}")
            out.add(f"  **How to apply:** in {applies_text(event['applies_to'])} (rmem lesson {event['id']}).")
    else:
        out.add(f"## Project lessons (rmem, {project_name(hub)})")
        out.add("")
        out.add(f"<!-- {origin} -->")
        out.add("")
        for item in items:
            event = item["event"]
            out.add(f"- {event['rule']} Why: {event['why']} (Applies to {applies_text(event['applies_to'])}; lesson {event['id']}.)")
    if not items:
        out.add("(no active lessons match)")
    out.emit()
    return 0


# Pending items and checkpoints

def cmd_pending_add(a, core):
    hub, who = core.find_hub(), core.actor(a, required=True)
    config = core.load_config(hub)
    note = core.one_line(a.note, "note", 2000)
    with core.hub_lock(hub):
        events = list(core.events_locked(hub))
        core.consistency_preflight(hub, events=events, who=who)
        ensure_new_id(core, events, a.id, "pending item", ("pending", "pending_closed"), reusable=True)
        current = pending_items(events).get(a.id)
        if current:
            raise core.RmemError(f"pending item {a.id} is already open (session {current.get('session')}); "
                                 f"close it with `rmem pending close {a.id} --note TEXT` or choose another ID")
        recorded = core.append_locked(hub, {**who, "kind": "pending", "id": a.id, "note": note})
    print("pending: " + core.fmt_event(recorded, config))
    print(f"When the answer arrives: rmem pending close {a.id} --note TEXT --quote \"the user's exact words\"")
    return 0


def cmd_pending_close(a, core):
    hub, who = core.find_hub(), core.actor(a, required=True)
    config = core.load_config(hub)
    note = core.one_line(a.note, "note", 2000)
    quote = text_option(core, a.quote, "quote", 2000)
    with core.hub_lock(hub):
        events = list(core.events_locked(hub))
        core.consistency_preflight(hub, events=events, who=who)
        item = pending_items(events).get(a.identifier)
        if item is None:
            raise core.RmemError(f"no open pending item {a.identifier}; `rmem pending list --all` lists the open ones")
        owner = item.get("session")
        kin = "owner" if owner == who["session"] else ("descendant" if owner in lineage(events, who["session"]) else "other")
        event = {**who, "kind": "pending_closed", "id": a.identifier, "note": note, "relation": kin}
        if quote:
            event["quote"] = quote
        recorded = core.append_locked(hub, event)
    if kin == "other":
        print(f"rmem: note: pending item {a.identifier} belongs to session {owner}; the close records relation=other",
              file=sys.stderr)
    print("closed: " + core.fmt_event(recorded, config))
    return 0


def cmd_pending_list(a, core):
    hub, who = core.find_hub(), reader_identity(a, core)
    config = core.load_config(hub)
    events = read_events(core, hub)
    items, now = list(pending_items(events).values()), utc_now()
    if not a.all:
        if not who.get("session"):
            raise core.RmemError("no session: pass --session ID (or run inside Claude Code or Codex), or use --all")
        sessions = lineage(events, who["session"])
        items = [item for item in items if item.get("session") in sessions]
    out = core.Output(config["caps"]["output_max_chars"])
    out.add(f"# {len(items)} open pending item(s)" + ("" if a.all else f" of session {who['session']} and its parents"))
    for item in items[-a.n:] if a.n else []:
        out.add(pending_line(core, item, now))
    if len(items) > a.n:
        out.add(f"[{len(items) - a.n} older items not shown; raise -n]")
    out.emit()
    return 0


def pending_line(core, item, now) -> str:
    return (f"- {item['id']} · open {age_days(item.get('ts'), now):.1f} days · session {core.clipped(item.get('session'), 128)}"
            f" — {core.clipped(item.get('note'), 300)}")


def hook_payload() -> dict:
    """The JSON object a Claude Code hook passes on stdin (session_id, source or trigger), or {}.
    Reads at most 1 MiB for at most two seconds, so a stdin that never closes cannot hang a hook."""
    try:
        if sys.stdin is None or sys.stdin.isatty():
            return {}
        fd, chunks, deadline = sys.stdin.fileno(), [], time.monotonic() + HOOK_INPUT_SECONDS
        while sum(map(len, chunks)) <= HOOK_INPUT_MAX:
            ready = select.select([fd], [], [], max(0.0, deadline - time.monotonic()))[0]
            if not ready:
                break
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            chunks.append(chunk)
            try:
                payload = json.loads(b"".join(chunks))
                return payload if isinstance(payload, dict) else {}
            except ValueError:
                continue
    except (OSError, ValueError):
        pass
    return {}


def adopt_hook_identity(payload) -> None:
    """A hook process may lack the conversation's variables; take its ID from the hook input instead."""
    value = identity.recordable(payload.get("session_id"), identity.SAFE_HARNESS_ID)
    if value and payload.get("hook_event_name") and not identity.harness()[0]:
        os.environ["CLAUDE_CODE_SESSION_ID"] = value
        os.environ.setdefault("CLAUDECODE", "1")


def conversation_identity(core, a, events, who):
    """(who, note). Hooks always derive the session from the conversation ID; when that derived session has
    no events but the conversation wrote under an explicit --session (found by its recorded harness ID),
    use that session, so the resume card and automatic checkpoints follow the conversation's real identity."""
    session = who.get("session")
    if not session or not identity.session_is_derived(a) or any(e.get("session") == session for e in events):
        return who, ""
    bound = identity.bound_sessions(events, identity.harness()[1])
    if not bound:
        return who, ""
    chosen, agent = bound[0]
    note = (f"this conversation writes as session {chosen} (found by its conversation ID; the derived session "
            f"{session} has no events), so this card and automatic checkpoints use it")
    if len(bound) > 1:
        note += "; it also wrote as " + ", ".join(core.clipped(s, 128) for s, _ in bound[1:4])
    agent = agent if isinstance(agent, str) and identity.SAFE_NAME.fullmatch(agent) else who["agent"]
    return {**who, "session": chosen, "agent": agent}, note


def cmd_checkpoint(a, core):
    if not a.auto:
        return write_checkpoint(a, core, {})
    payload = hook_payload()
    adopt_hook_identity(payload)
    try:
        return write_checkpoint(a, core, payload)
    except (core.RmemError, OSError, UnicodeError, ValueError) as exc:
        # WHY: a hook must never block compaction; say why nothing was written and succeed.
        print(f"rmem: automatic checkpoint skipped: {core.clipped(exc, 600)}", file=sys.stderr)
        return 0


def write_checkpoint(a, core, payload):
    hub, who = core.find_hub(), core.actor(a, required=True)
    config = core.load_config(hub)
    trigger = payload.get("trigger") if isinstance(payload.get("trigger"), str) else None
    if a.note is not None:
        note = core.one_line(a.note, "note", 2000)
    elif a.auto:
        note = (f"automatic checkpoint before context compaction ({core.clipped(trigger or 'trigger unknown', 40)}); "
                "the latest manual checkpoint and the leased threads' Next step sections hold the plan")
    else:
        raise core.RmemError("give --note \"next step: …\" (hooks use --auto)")
    with core.hub_lock(hub):
        events = list(core.events_locked(hub))
        if a.auto:
            who = conversation_identity(core, a, events, who)[0]
        core.consistency_preflight(hub, events=events, who=who)
        if a.auto and not any(e.get("session") == who["session"] for e in events):
            print(f"rmem: automatic checkpoint skipped: session {who['session']} has not written to this hub", file=sys.stderr)
            return 0
        if a.thread:
            core.authorize_thread_write(hub, a.thread, who, "log")
        event = {**who, "kind": "checkpoint", "note": note}
        if a.thread:
            event["thread"] = a.thread
        if a.auto:
            event["auto"] = True
            if trigger and identity.recordable(trigger, identity.SAFE_EFFORT):
                event["hook_source"] = trigger
        recorded = core.append_locked(hub, event)
    print("checkpoint: " + core.fmt_event(recorded, config))
    return 0


# Resume

def next_step(core, hub, slug, max_lines=12, max_chars=1200) -> list:
    """The bounded 'Next step' section of threads/<slug>.md."""
    path = core.safe_path(hub, "threads", f"{slug}.md")
    if not path.is_file():
        return [f"(threads/{slug}.md is missing)"]
    taken, used, level = [], 0, None
    for line, truncated in core.bounded_lines(path, 400):
        if level is None:
            match = NEXT_HEADING.match(line)
            level = len(match.group(1)) if match else None
            continue
        heading = HEADING.match(line)
        if heading and len(heading.group(1)) <= level:
            break
        if not taken and not line.strip():
            continue
        if len(taken) >= max_lines or used + len(line) > max_chars:
            taken.append("[cut; read the thread]")
            break
        taken.append(line + (" [line truncated]" if truncated else ""))
        used += len(line)
    if level is None:
        return [f"(threads/{slug}.md has no Next step section)"]
    while taken and not taken[-1].strip():
        taken.pop()
    return taken or ["(the Next step section is empty)"]


def authority_lines(core, hub, limit=4) -> list:
    """Lines of MAP.md that name an authority or controller: a '## …Authority…' section, else matching table rows."""
    path = core.safe_path(hub, "MAP.md")
    if not path.is_file():
        return []
    section, rows, level = [], [], None
    for line, _truncated in core.bounded_lines(path, 400):
        heading = HEADING.match(line)
        if heading:
            depth = len(heading.group(1))
            if level is not None and depth <= level:
                level = None
            # The level-1 title ("MAP — … source authority") names the whole file, not a section.
            if level is None and depth >= 2 and AUTHORITY.search(line) and not section:
                level = depth
            continue
        table_rule = re.fullmatch(r"[\s|:-]+", line) or "role / authority" in line.lower()
        if not line.strip() or table_rule:
            continue
        if level is not None:
            section.append(line.strip())
        elif line.lstrip().startswith("|") and AUTHORITY.search(line):
            rows.append(line.strip())
    chosen = section or rows
    return [core.clipped(line, 240) for line in chosen[:limit]] + (
        [f"[{len(chosen) - limit} more lines in MAP.md]"] if len(chosen) > limit else [])


def recent_sessions(core, events, exclude, config, limit=5) -> list:
    latest, counts = {}, {}
    for event in events:
        session, stamp = event.get("session"), when(event.get("ts"))
        if not isinstance(session, str) or session == exclude or stamp is None:
            continue
        counts[session] = counts.get(session, 0) + 1
        latest[session] = max(stamp, latest.get(session, stamp))
    roles = identity.session_roles(events)
    ordered = sorted(latest, key=latest.get, reverse=True)[:limit]
    return [f"- {core.clipped(s, 128)}" + (f" (role {core.clipped(roles[s], 80)})" if s in roles else "")
            + f": {counts[s]:,} events, last activity {core.display_time(latest[s].isoformat(), config)}" for s in ordered]


def max_chars(value):
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected a number of characters from 1000 to 100000") from exc
    if not 1000 <= number <= 100000:
        raise argparse.ArgumentTypeError("expected a number of characters from 1000 to 100000")
    return number


def cmd_resume(a, core):
    payload = hook_payload() if a.hook else {}
    adopt_hook_identity(payload)
    try:
        return resume_card(a, core, payload)
    except (core.RmemError, OSError, UnicodeError, ValueError) as exc:
        if not a.hook:
            raise
        # WHY: SessionStart output goes into the model's context; an error there is more useful than silence.
        print(f"rmem resume could not run: {core.clipped(exc, 600)}. Run `rmem doctor`, then `rmem status`.")
        return 0


def resume_card(a, core, payload):
    hub, who = core.find_hub(), reader_identity(a, core)
    config = core.load_config(hub)
    limit = a.max_chars or config["resume"]["max_chars"]
    recent_n = config["resume"]["recent_events"] if a.n is None else a.n
    out = core.Output(limit - len(CARD_TRAILER) + 40)
    now = utc_now()
    with core.hub_lock(hub, shared=True, readonly_ok=True):
        events = list(core.events_locked(hub))
        if a.thread:
            require_thread(core, hub, a.thread, events)
        who, bound_note = conversation_identity(core, a, events, who)
        session = who.get("session")
        scope = caller_scope(core, hub, events, config, who, [a.thread] if a.thread else (), with_lineage=True)
        out.add(f"# rmem resume: {'session ' + session if session else 'no session'} ({who.get('agent', '?')}), "
                f"{core.display_time(now.isoformat(), config)}")
        source = payload.get("source") if isinstance(payload.get("source"), str) else None
        out.add(f"Hub {hub} (kit {core.kit_version()}). A card of at most {limit:,} characters; read only what the next "
                "step needs." + (f" Printed by a Claude Code session-start hook ({core.clipped(source, 40)})." if source else ""))
        # WHY: the card is cut at its budget, so sections run from most to least urgent after a context loss.
        resume_identity(core, out, events, config, who, scope, now)
        if bound_note:
            out.add("- " + bound_note)
        resume_requests(core, out, events, scope, now)
        resume_pending(core, out, events, scope, now)
        resume_checkpoints(core, out, events, config, scope)
        resume_leases(core, out, hub, config, scope, a.thread, events)
        resume_decisions(core, out, events, config, scope)
        authority = authority_lines(core, hub)
        if authority:
            out.add("\n## Authority (MAP.md; read the entries you need)")
            for line in authority:
                out.add(line if line.startswith("[") else "- " + line.lstrip("-* ").strip())
        for line in lesson_section(core, active_lessons(events, scope.targets), 8):
            out.add(line if line.startswith(("- ", "[")) else "\n" + line)
        resume_events(core, out, events, config, scope, recent_n, a.thread)
        ctx = SimpleNamespace(hub=hub, events=events, config=config, who=who, core=core, args=a)
        extra = core.section_lines(ctx, "resume_sections")
        if extra:
            out.add("")
            for line in extra:
                out.add(line)
    out.add("\nNext: answer blocking requests and pending items first, re-read a thread before editing it, and "
            "record a checkpoint (`rmem checkpoint --note \"next step: …\"`) before a long wait.")
    sys.stdout.write("".join(out.parts))
    if out.truncated:
        sys.stdout.write(CARD_TRAILER)
    return 0


def resume_identity(core, out, events, config, who, scope, now):
    out.add("\n## Identity")
    session = scope.session
    if not session:
        out.add("- no session: pass --session ID on every call, or run inside Claude Code or Codex; "
                "showing hub-wide requests, decisions and lessons")
        return
    profile = identity.session_profile(events, session)
    fields = profile["fields"]
    described = ", ".join(f"{name} {core.clipped(fields[name][0], 80)}" for name in ("role", "model", "effort") if name in fields)
    if scope.role and "role" not in fields:
        described = (described + ", " if described else "") + f"role {scope.role} (inherited)"
    parent = fields.get("parent_session")
    started = profile["started"]
    out.add(f"- {described or 'role, model and effort not recorded'}; parent {parent[0] if parent else 'none'}; "
            f"started {core.display_time(started['ts'], config) if started else 'not recorded'}")
    if profile["events"]:
        out.add(f"- {profile['events']:,} events, last activity {core.display_time(profile['last'], config)}")
    fork = identity.current_fork(events, session)
    if fork:
        out.add("- WRITES REFUSED: " + fork)
    if not profile["events"]:
        out.add("- This session has no events yet. If this conversation continues another session (a fork or a "
                "resumed transcript), run `rmem session start --parent OLD-SESSION`. Most recent sessions:")
        for line in recent_sessions(core, events, session, config):
            out.add("  " + line)


def resume_requests(core, out, events, scope, now):
    items = open_requests(events)
    mine = for_caller(items, scope) if scope.session else items
    if not mine:
        if items and scope.session:
            out.add(f"\nNo open requests for you; {len(items)} for other addressees (rmem inbox --all).")
        return
    blocking = sum(1 for item in mine if item["event"].get("blocking"))
    label = "Requests for you" if scope.session else "Open requests in this hub"
    out.add(f"\n## {label}: {len(mine)} open" + (f", {blocking} blocking" if blocking else "") +
            " (rmem ack ID; rmem close ID --by EVENT)")
    for item in mine[:8]:
        out.add("- " + request_line(core, item, now))
    if len(mine) > 8:
        out.add(f"[{len(mine) - 8} more: rmem inbox]")


def resume_pending(core, out, events, scope, now):
    items = [item for item in pending_items(events).values() if item.get("session") in scope.sessions]
    if not items:
        return
    out.add(f"\n## Pending (waiting for an answer): {len(items)} (rmem pending close ID --note TEXT --quote \"…\")")
    for item in items[-8:]:
        out.add(pending_line(core, item, now))


def writers_since(events, key, moment, session) -> list:
    """Other sessions that wrote to thread `key` after `moment` (an expired lease's end)."""
    return sorted({e["session"] for e in events if e.get("thread") == key and isinstance(e.get("session"), str)
                   and e["session"] != session and (when(e.get("ts")) or moment) > moment})


def resume_leases(core, out, hub, config, scope, thread, events=()):
    if scope.held:
        out.add(f"\n## Leases held: {len(scope.held)}")
        for key, _meta, lease in scope.held[:10]:
            if lease["live"]:
                state = f"live, expires {core.display_time(lease['expires_at'].isoformat(), config)}"
            else:
                others = writers_since(events, key, lease["expires_at"], scope.session)
                state = (f"EXPIRED at {core.display_time(lease['expires_at'].isoformat(), config)}: another session may "
                         f"take it over; `rmem claim {key}` holds it again"
                         + (f"; since then {', '.join(core.clipped(s, 80) for s in others[:5])} wrote to it" if others else ""))
            out.add(f"- {key}: {state}; last activity {core.display_time(lease['last_activity'].isoformat(), config)}, "
                    f"lease {lease['ttl_hours']:g}h")
    slugs = [key for key, _meta, _lease in scope.held if not key.startswith("@")]
    if thread and thread not in slugs:
        slugs.insert(0, thread)
    for slug in slugs[:4]:
        out.add(f"\n## Next step: {slug} (threads/{slug}.md)")
        for line in next_step(core, hub, slug):
            out.add("  " + line)
    if len(slugs) > 4:
        out.add(f"[{len(slugs) - 4} more leased threads: rmem resume --thread SLUG]")


# WHY: full event lines run to about 550 characters; clipping keeps one long section from
# pushing the rest of the card past its budget.
def card_line(core, event, config, limit=220) -> str:
    return core.clipped(core.fmt_event(event, config), limit)


def resume_checkpoints(core, out, events, config, scope):
    checkpoints = [e for e in events if e.get("kind") == "checkpoint" and e.get("session") in scope.sessions]
    if checkpoints:
        out.add(f"\n## Latest checkpoints ({len(checkpoints)} in all)")
        for event in checkpoints[-3:]:
            out.add("- " + card_line(core, event, config, 300) + (" [automatic]" if event.get("auto") else ""))


def resume_decisions(core, out, events, config, scope):
    since = last_activity(events, scope.sessions)
    decided = decisions_since(events, scope.sessions, since)
    if not decided:
        return
    out.add(f"\n## Decisions by other sessions since your last event ({core.display_time(since.isoformat(), config)}): "
            f"{len(decided)}" if since else "\n## Latest decisions in this hub")
    for event in decided[-8:]:
        out.add("- " + card_line(core, event, config))
    if len(decided) > 8:
        out.add(f"[{len(decided) - 8} earlier: rmem recent --kind decision]")


def resume_events(core, out, events, config, scope, recent_n, thread):
    if thread:
        in_thread = [e for e in events if e.get("thread") == thread]
        out.add(f"\n## Thread {thread}: last {min(5, len(in_thread))} of {len(in_thread)} events")
        for event in in_thread[-5:]:
            out.add("- " + card_line(core, event, config))
    own = [e for e in events if e.get("session") in scope.sessions]
    if own and recent_n:
        out.add(f"\n## Your last {min(recent_n, len(own))} events" + (" (with parent sessions)" if len(scope.sessions) > 1 else ""))
        for event in own[-recent_n:]:
            out.add("- " + card_line(core, event, config))


# Hooks

def hub_command(hub, action) -> str:
    return f"RMEM_HUB={shlex.quote(str(hub))} {shlex.quote(str(hub / 'bin' / 'rmem'))} {action}"


def settings_groups(core, settings, name):
    groups = settings.get("hooks", {}).get(name, [])
    if not isinstance(groups, list) or any(not isinstance(g, dict) or not isinstance(g.get("hooks", []), list) for g in groups):
        raise core.RmemError(f"hooks.{name} in the settings file is not a list of {{\"matcher\", \"hooks\": [...]}} "
                             "groups; fix it by hand, then run this again")
    return groups


def installed(groups, matcher, command) -> bool:
    for group in groups:
        matchers = str(group.get("matcher", "")).split("|")
        if matcher in matchers or group.get("matcher") in (None, "", "*"):
            if any(isinstance(h, dict) and h.get("command") == command for h in group.get("hooks", [])):
                return True
    return False


def read_settings(core, path):
    if path.is_symlink():
        raise core.RmemError(f"refusing symlink: {path}")
    if not path.exists():
        return {}, None
    with core.open_regular(path, os.O_RDONLY, "r") as stream:
        text = stream.read(HOOK_INPUT_MAX + 1)
    try:
        settings = json.loads(text)
    except ValueError as exc:
        raise core.RmemError(f"{path} is not valid JSON ({exc}); fix it by hand first, nothing was changed") from exc
    if not isinstance(settings, dict) or not isinstance(settings.get("hooks", {}), dict):
        raise core.RmemError(f"{path} must hold a JSON object whose \"hooks\" is an object; fix it by hand first")
    return settings, text


def cmd_hooks_install(a, core):
    hub = core.find_hub()
    if a.codex:
        resume, checkpoint = hub_command(hub, "resume"), hub_command(hub, 'checkpoint --note "next step: …"')
        for line in ("<!-- Paste into the project's AGENTS.md. rmem never writes it: Codex offers no session-start hook "
                     "that rmem relies on. -->", "## Session continuity (rmem)",
                     f"- At the start of a session, and after a context compaction, a resume or a fork, run `{resume}` "
                     "first and handle its blocking requests and pending items before other work.",
                     f"- Before a long wait, or when the context is nearly full, run `{checkpoint}`.",
                     "- Record options you gave the user with `rmem pending add`, and close them with the user's exact "
                     "words (`rmem pending close ID --note TEXT --quote \"…\"`)."):
            print(line)
        return 0
    project = Path(a.project or os.getcwd()).expanduser().resolve()
    if not project.is_dir():
        raise core.RmemError(f"project is not a directory: {project}")
    if project == hub:
        raise core.RmemError("hooks belong in the project's .claude/settings.local.json, not in the hub; run this from the "
                             "project directory or pass --project DIR")
    # WHY: the command holds this device's absolute hub path, so it goes in the per-device file, not the shared one.
    path = core.safe_path(project, ".claude", SETTINGS_NAME)
    settings, original = read_settings(core, path)
    wanted = ([("SessionStart", m, hub_command(hub, "resume --hook")) for m in SESSION_START_MATCHERS]
              + [("PreCompact", m, hub_command(hub, "checkpoint --auto")) for m in PRE_COMPACT_MATCHERS])
    added = {}
    for name, matcher, command in wanted:
        if not installed(settings_groups(core, settings, name), matcher, command):
            added.setdefault(name, []).append({"matcher": matcher, "hooks": [{"type": "command", "command": command}]})
    if not added:
        print(f"already installed in {path}; nothing to do")
        return 0
    count = sum(map(len, added.values()))
    if not a.write:
        print(f"# Dry run: {count} hook group(s) would be merged into {path}")
        print(json.dumps({"hooks": added}, indent=2, ensure_ascii=False))
        print("Nothing was written. Re-run with --write to merge them (the old file is kept as a backup). "
              "SessionStart prints `rmem resume` into the context; PreCompact writes `rmem checkpoint --auto`. See HANDOFF.md.")
        return 0
    hooks = settings.setdefault("hooks", {})
    for name, groups in added.items():
        hooks.setdefault(name, []).extend(groups)
    backup = None
    if original is not None:
        stamp = utc_now().strftime("%Y%m%dT%H%M%SZ")
        backup = next(candidate for candidate in (path.with_name(f"{SETTINGS_NAME}.rmem-backup-{stamp}" + (f"-{n}" if n else ""))
                                                  for n in range(1000)) if not candidate.exists())
        shutil.copy2(path, backup)
    else:
        path.parent.mkdir(exist_ok=True)
    core.atomic_write(path, json.dumps(settings, indent=2, ensure_ascii=False) + "\n")
    print(f"merged {count} hook group(s) into {path}" + (f"; backup {backup.name}" if backup else "; created the file"))
    print("Restart Claude Code (or open /hooks) so the hooks load; remove the entries from that file to turn them off.")
    return 0


# Registration and hooks for status and check

def register(commands, core):
    p = commands.add_parser("request", help="ask a thread, role or session for something; never refused by a lease")
    p.add_argument("--to", required=True, help="thread:SLUG, role:ROLE or session:SESSION")
    p.add_argument("--id", required=True, type=core.identifier, help="a new request ID, such as REQ-7B-E3")
    p.add_argument("--note", required=True, help="what is asked, in one line")
    p.add_argument("--blocking", action="store_true", help="the requester waits for this")
    p.add_argument("--source", metavar="EVENT", help="the event that recorded the user's decision behind this request")
    p.add_argument("--quote", help="the user's exact words behind this request")
    p.add_argument("--user-decision", action="store_true", help="the request carries a user decision (needs --source or --quote)")
    p.add_argument("--thread", type=core.identifier, help="file the request in this thread (default: the thread in --to)")
    p.set_defaults(fn=lambda a: cmd_request(a, core))
    p = commands.add_parser("ack", help="acknowledge a request")
    p.add_argument("identifier", type=core.identifier)
    p.add_argument("--note")
    p.set_defaults(fn=lambda a: request_update(a, core, "request_acked"))
    p = commands.add_parser("close", help="close a request as done, withdrawn or rejected")
    p.add_argument("identifier", type=core.identifier)
    p.add_argument("--by", metavar="EVENT", help="the event that completed it (event ID, 8+ hex digits, or #SEQUENCE)")
    p.add_argument("--reason", help="why it is closed (recorded as the note)")
    p.add_argument("--status", choices=CLOSE_STATUSES, default="done")
    p.set_defaults(fn=lambda a: request_update(a, core, "request_closed"))
    p = commands.add_parser("inbox", help="open requests for this session, its role and its threads (read-only)")
    p.add_argument("--for", dest="for_address", metavar="ADDRESS", help="thread:SLUG, role:ROLE or session:SESSION")
    p.add_argument("--all", action="store_true", help="open requests for every addressee")
    p.add_argument("-n", type=core.count, default=20)
    p.set_defaults(fn=lambda a: cmd_inbox(a, core))

    p = commands.add_parser("lesson", help="collaboration rules shared by every agent (lessons/<ID>.md)")
    actions = p.add_subparsers(dest="lesson_command", required=True)
    p.rmem_choices = actions.choices
    s = actions.add_parser("add", help="record one rule and why; --supersedes replaces an older lesson")
    s.add_argument("--id", required=True, type=core.identifier)
    s.add_argument("--rule", required=True)
    s.add_argument("--why", required=True)
    s.add_argument("--applies-to", default="all", help="all (default), thread:SLUG or role:ROLE")
    s.add_argument("--source", metavar="EVENT", help="the event where the user gave this rule")
    s.add_argument("--quote", help="the user's exact words")
    s.add_argument("--supersedes", type=core.identifier, metavar="ID")
    s.set_defaults(fn=lambda a: cmd_lesson_add(a, core))
    s = actions.add_parser("list", help="active lessons (read-only); --all adds superseded ones")
    s.add_argument("--applies-to", help="all, thread:SLUG or role:ROLE (lessons for all are included)")
    s.add_argument("--all", action="store_true")
    s.add_argument("-n", type=core.count, default=50)
    s.set_defaults(fn=lambda a: cmd_lesson_list(a, core))
    s = actions.add_parser("export", help="print lessons as Markdown for Claude auto-memory or a Codex AGENTS.md")
    s.add_argument("--format", required=True, choices=("claude-memory", "codex-agents"))
    s.add_argument("--applies-to")
    s.set_defaults(fn=lambda a: cmd_lesson_export(a, core))

    p = commands.add_parser("pending", help="things this session waits for, such as the user's answer to options")
    actions = p.add_subparsers(dest="pending_command", required=True)
    p.rmem_choices = actions.choices
    s = actions.add_parser("add", help="record an open item, such as options given to the user")
    s.add_argument("--id", required=True, type=core.identifier)
    s.add_argument("--note", required=True)
    s.set_defaults(fn=lambda a: cmd_pending_add(a, core))
    s = actions.add_parser("close", help="close an item; --quote keeps the user's exact words")
    s.add_argument("identifier", type=core.identifier)
    s.add_argument("--note", required=True)
    s.add_argument("--quote")
    s.set_defaults(fn=lambda a: cmd_pending_close(a, core))
    s = actions.add_parser("list", help="open items of this session and its parents (read-only); --all for every session")
    s.add_argument("--all", action="store_true")
    s.add_argument("-n", type=core.count, default=20)
    s.set_defaults(fn=lambda a: cmd_pending_list(a, core))

    p = commands.add_parser("checkpoint", help="append this session's next step (hooks use --auto)")
    p.add_argument("--note", help="next step: …")
    p.add_argument("--thread", type=core.identifier)
    p.add_argument("--auto", action="store_true", help="written by a hook: short automatic note, never fails, skipped "
                   "for a session that has not written to the hub")
    p.set_defaults(fn=lambda a: cmd_checkpoint(a, core))
    p = commands.add_parser("resume", help="bounded continuation card after compaction, a resume or a fork (read-only)")
    p.add_argument("--thread", type=core.identifier, help="also show this thread's Next step and latest events")
    p.add_argument("--max-chars", type=max_chars, help="character budget (default resume.max_chars, 6000)")
    p.add_argument("-n", type=core.count, help="own recent events to show (default resume.recent_events, 8)")
    p.add_argument("--hook", action="store_true", help="called by a harness hook: read its JSON on stdin and never fail")
    p.set_defaults(fn=lambda a: cmd_resume(a, core))
    p = commands.add_parser("hooks", help="print or install optional harness hooks that run resume and checkpoint")
    actions = p.add_subparsers(dest="hooks_command", required=True)
    p.rmem_choices = actions.choices
    s = actions.add_parser("install", help="dry run unless --write; --claude merges into .claude/settings.local.json, --codex prints a snippet")
    which = s.add_mutually_exclusive_group(required=True)
    which.add_argument("--claude", action="store_true")
    which.add_argument("--codex", action="store_true")
    s.add_argument("--project", help="project directory that holds .claude/ (default: the working directory)")
    s.add_argument("--write", action="store_true", help="merge into the settings file, keeping a backup")
    s.set_defaults(fn=lambda a: cmd_hooks_install(a, core))


def status_sections(ctx):
    core, events, config = ctx.core, ctx.events, ctx.config
    scope = caller_scope(core, ctx.hub, events, config, ctx.who or {})
    items, now, lines = open_requests(events), utc_now(), []
    if scope.session:
        mine = for_caller(items, scope)
        if mine:
            blocking = sum(1 for item in mine if item["event"].get("blocking"))
            lines.append(f"## Requests for you: {len(mine)} open" + (f", {blocking} blocking" if blocking else "")
                         + " (rmem inbox; rmem ack ID)")
            lines += ["- " + request_line(core, item, now) for item in mine[:STATUS_REQUESTS]]
            if len(mine) > STATUS_REQUESTS:
                lines.append(f"[{len(mine) - STATUS_REQUESTS} more: rmem inbox]")
        if len(items) > len(mine):
            lines.append(f"Open requests for other addressees: {len(items) - len(mine)} (rmem inbox --all)")
        since = last_activity(events, scope.sessions)
        decided = decisions_since(events, scope.sessions, since) if since else []
        if decided:
            lines.append(f"Decisions by other sessions since your last event: {len(decided)} (rmem resume lists them)")
    elif items:
        lines.append(f"## Open requests in this hub: {len(items)} (rmem inbox --all; pass --session to see your own)")
    lines += lesson_section(core, active_lessons(events, scope.targets), STATUS_LESSONS)
    return lines


def check_problems(ctx):
    core, problems, now = ctx.core, [], utc_now()
    days = ctx.config["requests"]["ack_warn_days"]
    late = [item for item in requests(ctx.events).values()
            if item["state"] == "open" and age_days(item["event"].get("ts"), now) > days]
    for item in late[:10]:
        event = item["event"]
        problems.append(("warn", f"request {event['id']} to {core.clipped(event.get('to'), 120)} has waited "
                         f"{age_days(event.get('ts'), now):.1f} days without an ack (requests.ack_warn_days is {days:g}); "
                         f"the addressee runs `rmem ack {event['id']}` or `rmem close {event['id']} --by EVENT|--reason TEXT`"))
    if len(late) > 10:
        problems.append(("warn", f"{len(late)} requests wait for an ack; showing 10 (rmem inbox --all lists them)"))
    for role, value in sorted((ctx.config.get("roles") or {}).items()):
        threads = value.get("threads") if isinstance(value, dict) else None
        if isinstance(value, dict) and "threads" in value and (
                not isinstance(threads, list) or any(not isinstance(t, str) or not SAFE_ID.fullmatch(t) for t in threads)):
            problems.append(("warn", f"hub-config.json: roles.{role}.threads must be a list of thread slugs; it is ignored"))
    changed = []
    for item in lessons(ctx.events).values():
        event = item["event"]
        try:
            path = core.safe_path(ctx.hub, str(event.get("lesson_path") or f"lessons/{event['id']}.md"))
            state = ("missing" if not path.is_file() else
                     "changed" if event.get("lesson_sha256") and core.file_sha256(path) != event["lesson_sha256"] else "")
        except (OSError, core.RmemError) as exc:
            state = f"unreadable ({core.clipped(exc, 120)})"
        if state:
            changed.append(f"{event['id']} ({state})")
    if changed:
        problems.append(("warn", f"{len(changed)} lesson file(s) differ from their ledger events: {', '.join(changed[:5])}; "
                         "restore the file, or record the change as a new lesson with --supersedes ID"))
    return problems


def validate_event(event):
    kind = event.get("kind")
    if kind not in EVENT_KINDS:
        return ""
    for name in TEXT_FIELDS:
        if event.get(name) is not None and not isinstance(event[name], str):
            return f"event {name} must be a string"
    for name in FLAG_FIELDS:
        if name in event and not isinstance(event[name], bool):
            return f"event {name} must be a boolean"
    if not event.get("session"):
        return f"{kind} requires a session"
    if kind != "checkpoint" and not (isinstance(event.get("id"), str) and event["id"]):
        return f"{kind} requires an id"
    if kind == "request" and not address(event.get("to")):
        return "request to must be thread:SLUG, role:ROLE or session:SESSION"
    if kind == "request_closed" and event.get("status") not in CLOSE_STATUSES:
        return "request_closed status must be done, withdrawn or rejected"
    if kind == "lesson" and not (all(isinstance(event.get(n), str) and event[n] for n in ("rule", "why"))
                                 and applies_address(event.get("applies_to"))):
        return "lesson requires rule, why and applies_to (all, thread:SLUG or role:ROLE)"
    return ""
