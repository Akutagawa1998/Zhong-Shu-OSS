"""Thread lifecycle: seal, reopen, archive and roll threads, and flag stale threads.

Lifecycle state comes from ledger events only (thread_sealed, thread_reopened,
thread_archived); a thread without such events is active. Rewrites of a thread
file are dry runs unless --apply. Before a rewrite, the thread's exact bytes are
copied to history/threads/<slug>/<UTC date>-<sha12>.md and indexed, with their
SHA-256 and ledger event, in history/threads/SNAPSHOTS.json. See LIFECYCLE.md.
"""
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import stat

EVENT_KINDS = {"thread_sealed", "thread_reopened", "thread_archived", "thread_rolled"}
EVENT_STRING_FIELDS = {"archived_path", "archived_sha256", "previous_state"}
STATE_OF_KIND = {"thread_sealed": "sealed", "thread_reopened": "active", "thread_archived": "archived"}
# Ownership bookkeeping and lifecycle maintenance do not make a thread summary stale.
NOT_CONTENT_KINDS = {"claim", "release", "thread_rolled", *STATE_OF_KIND}
HISTORY_DIR = "history/threads"
SNAPSHOTS = "SNAPSHOTS.json"
SNAPSHOTS_DOC = ("Archived thread copies written by `rmem thread archive|roll`. Each entry keeps the exact "
                 "bytes and SHA-256 of the thread as it was, its original path, and the ledger event. "
                 "Links inside an archived copy are relative to relative_links_base. See LIFECYCLE.md.")
ROLL_LINE = "Older entries of this section"
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
FENCE = re.compile(r" {0,3}(```|~~~)")
STALENESS_MAX_THREADS = 60
PREVIEW_LINES = 12


# Lifecycle state (status filter and staleness)

def states(events) -> dict:
    """slug -> state ('sealed' | 'archived' | 'active') from the latest lifecycle event."""
    result = {}
    for event in events:
        state = STATE_OF_KIND.get(event.get("kind"))
        if state and isinstance(event.get("thread"), str):
            result[event["thread"]] = state
    return result


def state_of(ctx, slug: str) -> str:
    cached = getattr(ctx, "lifecycle_states", None)
    if cached is None:
        cached = ctx.lifecycle_states = states(getattr(ctx, "events", None) or [])
    return cached.get(slug, "active")


def visible(ctx, slug: str) -> bool:
    return state_of(ctx, slug) == "active"


def validate_event(event):
    kind = event.get("kind")
    if kind not in EVENT_KINDS:
        return ""
    if not isinstance(event.get("thread"), str) or not event["thread"]:
        return f"{kind} requires a thread"
    if kind in ("thread_archived", "thread_rolled"):
        if not str(event.get("archived_path", "")).startswith(HISTORY_DIR + "/"):
            return f"{kind} requires archived_path under {HISTORY_DIR}/"
        if not SHA256.fullmatch(str(event.get("archived_sha256", ""))):
            return f"{kind} requires a 64-character archived_sha256"
        size = event.get("archived_bytes")
        if type(size) is not int or size < 0:
            return f"{kind} requires archived_bytes as a nonnegative integer"
    return ""


# Commands

def register(commands, core):
    p = commands.add_parser("thread", help="seal, reopen, archive or roll a thread (rewrites are dry runs unless --apply)")
    sub = p.add_subparsers(dest="thread_command", required=True)
    p.rmem_choices = sub.choices
    s = sub.add_parser("seal", help="mark a finished thread sealed; status hides it, its file stays")
    s.add_argument("slug", type=core.identifier)
    s.add_argument("--summary", required=True, help="one line: the outcome and where its evidence is")
    s.set_defaults(fn=lambda a: cmd_mark(a, core, "thread_sealed"))
    s = sub.add_parser("reopen", help="make a sealed or archived thread active again")
    s.add_argument("slug", type=core.identifier)
    s.add_argument("--note", help="one line: why the thread is active again")
    s.set_defaults(fn=lambda a: cmd_mark(a, core, "thread_reopened"))
    s = sub.add_parser("archive", help="keep the thread's exact bytes in history/ and leave a pointer page (dry run unless --apply)")
    s.add_argument("slug", type=core.identifier)
    s.add_argument("--summary", required=True, help="one line for the pointer page: outcome and what stays in force")
    s.add_argument("--apply", action="store_true", help="write the archive, pointer page and ledger event")
    s.set_defaults(fn=lambda a: cmd_rewrite(a, core, "archive"))
    s = sub.add_parser("roll", help="move older paragraphs of each section to an archived copy (dry run unless --apply)")
    s.add_argument("slug", type=core.identifier)
    s.add_argument("--keep", type=core.count, required=True, help="paragraph blocks to keep at the end of each '## ' section")
    s.add_argument("--note", help="one line for the ledger event")
    s.add_argument("--apply", action="store_true", help="write the archive, the shortened thread and the ledger event")
    s.set_defaults(fn=lambda a: cmd_rewrite(a, core, "roll"))


def thread_file(core, hub: Path, slug: str) -> Path:
    path = core.safe_path(hub, "threads", f"{slug}.md")
    if not path.is_file():
        raise core.RmemError(f"no thread file threads/{slug}.md in this hub; check the slug with `rmem status --all`")
    return path


def cmd_mark(a, core, kind):
    """Seal or reopen: one ledger event under the thread's write authorization; no file changes."""
    hub, who = core.find_hub(), core.actor(a, required=True)
    text = a.summary if kind == "thread_sealed" else a.note
    if text is not None:
        core.one_line(text, "summary" if kind == "thread_sealed" else "note")
    with core.hub_lock(hub):
        core.consistency_preflight(hub)
        core.authorize_thread_write(hub, a.slug, who, "log")
        thread_file(core, hub, a.slug)
        state = states(core.events_locked(hub)).get(a.slug, "active")
        if (state == "active") == (kind == "thread_reopened"):
            print(f"thread {a.slug} is already {state}; nothing recorded")
            return 0
        event = {**who, "kind": kind, "thread": a.slug, "previous_state": state}
        if text is not None:
            event["note"] = text
        core.append_locked(hub, event)
    if kind == "thread_sealed":
        print(f"sealed {a.slug}: `rmem status` hides it (`status --all` lists it); "
              f"`rmem thread reopen {a.slug}` undoes this. Release your lease if you took one.")
    else:
        print(f"reopened {a.slug} (was {state}); update its summary and next step before others rely on it")
    return 0


def utc_date() -> str:
    return dt.datetime.now(dt.timezone.utc).date().isoformat()


def archive_name(slug: str, data: bytes):
    digest = hashlib.sha256(data).hexdigest()
    return f"{HISTORY_DIR}/{slug}/{utc_date()}-{digest[:12]}.md", digest


def read_snapshots(core, hub: Path) -> dict:
    path = core.safe_path(hub, HISTORY_DIR, SNAPSHOTS)
    if not path.exists():
        return {"_doc": SNAPSHOTS_DOC, "schema_version": 1, "snapshots": []}
    try:
        with core.open_regular(path, os.O_RDONLY, "r") as stream:
            index = json.loads(stream.read())
    except (OSError, json.JSONDecodeError, RecursionError) as exc:
        raise core.RmemError(f"{HISTORY_DIR}/{SNAPSHOTS} is unreadable ({core.clipped(exc, 200)}); "
                             "repair it before archiving; nothing was changed") from exc
    if not isinstance(index, dict) or not isinstance(index.get("snapshots"), list):
        raise core.RmemError(f"{HISTORY_DIR}/{SNAPSHOTS} must be an object with a snapshots list; "
                             "repair it before archiving; nothing was changed")
    return index


def read_bytes(core, path: Path) -> bytes:
    """Exact bytes of a regular file; refuses symlinks and FIFOs."""
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise core.RmemError(f"expected a regular file: {path}")
        return stream.read()


def existing_archive(core, hub: Path, relative: str, data: bytes) -> bool:
    """True when the destination already holds these exact bytes; refuse different bytes."""
    path = core.safe_path(hub, relative)
    if not path.exists() and not path.is_symlink():
        return False
    if path.is_symlink() or not path.is_file() or read_bytes(core, path) != data:
        raise core.RmemError(f"{relative} already exists with different bytes; inspect it, nothing was changed")
    return True


def write_exact(core, hub: Path, relative: str, data: bytes, mode: int) -> None:
    """Create a new file with exactly these bytes (never replaces an existing file)."""
    path = core.safe_path(hub, relative)
    path.parent.mkdir(parents=True, exist_ok=True)
    core.safe_path(hub, relative)  # WHY: recheck components that mkdir may have met concurrently.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), mode)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    if read_bytes(core, path) != data:
        raise core.RmemError(f"{relative} does not hold the bytes just written; inspect the disk before retrying")


def split_lines(text: str) -> list:
    """Lines with their '\\n' endings; only '\\n' separates lines, so joining restores the text exactly."""
    return [line for line in re.split(r"(?<=\n)", text) if line]


def regions(text: str) -> list:
    """[(heading, body_lines)]: heading is a '## ' line, or None for text outside sections."""
    result, fenced = [[None, []]], False
    for line in split_lines(text):
        if not fenced and (line.startswith("## ") or line.startswith("# ")):
            result.append([line if line.startswith("## ") else None, []])
            if line.startswith("# "):
                result[-1][1].append(line)
            continue
        if FENCE.match(line):
            fenced = not fenced
        result[-1][1].append(line)
    return [(heading, body) for heading, body in result if heading is not None or body]


def blocks(body: list) -> list:
    """Split section lines into [kind, lines] runs: 'blank' or 'block' (blank lines inside a fence stay in a block)."""
    runs, fenced = [], False
    for line in body:
        kind = "blank" if not fenced and not line.strip() else "block"
        if FENCE.match(line):
            fenced = not fenced
        if runs and runs[-1][0] == kind:
            runs[-1][1].append(line)
        else:
            runs.append([kind, [line]])
    return runs


def roll_text(text: str, keep: int, link: str):
    """Return (new text, [(heading, total blocks, moved blocks)]); content outside '## ' sections is unchanged."""
    output, report = [], []
    for heading, body in regions(text):
        if heading is None:
            output += body
            continue
        output.append(heading)
        runs = blocks(body)
        indexes = [i for i, run in enumerate(runs) if run[0] == "block"]
        moved = len(indexes) - keep
        report.append((heading.strip(), len(indexes), max(moved, 0)))
        if moved <= 0:
            output += body
            continue
        lead = runs[0][1] if runs[0][0] == "blank" else []
        tail = runs[indexes[moved]:] if keep else [run for run in runs[indexes[-1] + 1:]]
        output += lead + [f"{ROLL_LINE} ({moved} block{'s' if moved != 1 else ''}) are archived in {link}\n"]
        if keep:
            output.append("\n")
        for run in tail:
            output += run[1]
    return "".join(output), report


def first_title(text: str, slug: str) -> str:
    for line in split_lines(text)[:20]:
        if line.startswith("# "):
            return line.rstrip("\n")[:200]
    return f"# Thread: {slug}"


def pointer_page(text, slug, summary, who, archived, digest, size, event_id) -> str:
    return (f"{first_title(text, slug)}\n\n"
            f"Lifecycle: archived on {utc_date()} (UTC) by {who.get('agent', '?')} / {who.get('session', '?')}; "
            f"ledger event {event_id}.\n\n"
            f"Summary: {summary}\n\n"
            f"The full previous text is kept byte for byte in [{archived}](../{archived}) "
            f"(sha256 {digest}, {size:,} bytes).\n\n"
            "Negative results, dead ends and corrections in the archived text stay in force. "
            "Read them there before repeating work or citing a result from this thread.\n\n"
            f"To continue this thread, run `rmem thread reopen {slug}` and write a new summary and next step here.\n")


def plan_rewrite(core, hub: Path, a, action: str):
    """Everything a rewrite needs, computed without writing: (thread path, original bytes, plan dict)."""
    path = thread_file(core, hub, a.slug)
    data = read_bytes(core, path)
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise core.RmemError(f"threads/{a.slug}.md is not UTF-8 text; nothing was changed") from exc
    archived, digest = archive_name(a.slug, data)
    plan = {"archived": archived, "digest": digest, "size": len(data), "text": text,
            "reuse": existing_archive(core, hub, archived, data), "snapshots": read_snapshots(core, hub)}
    if action == "roll":
        link = f"[{archived}](../{archived}) (rolled {utc_date()} UTC)."
        plan["new_text"], plan["sections"] = roll_text(text, a.keep, link)
        plan["moved"] = sum(moved for _, _, moved in plan["sections"])
    return path, data, plan


def print_plan(core, a, action, plan) -> None:
    out = core.Output()
    out.add(f"# rmem thread {action} {a.slug} (dry run; nothing was changed)")
    out.add(f"thread: threads/{a.slug}.md, {plan['size']:,} bytes, sha256 {plan['digest']}")
    out.add(f"archive copy: {plan['archived']}" + (" (already present with these bytes)" if plan["reuse"] else ""))
    if action == "roll":
        for heading, total, moved in plan["sections"][:40]:
            out.add(f"- {core.clipped(heading, 120)}: {total} block(s), {'moves ' + str(moved) if moved else 'unchanged'}")
        if len(plan["sections"]) > 40:
            out.add(f"[{len(plan['sections']) - 40} more sections not shown]")
        if not plan["moved"]:
            out.add(f"Nothing to roll: no section has more than {a.keep} block(s).")
            out.emit()
            return
        out.add(f"moves {plan['moved']} block(s); the thread shrinks from {len(plan['text']):,} to "
                f"{len(plan['new_text']):,} characters; then appends thread_rolled")
    else:
        page = pointer_page(plan["text"], a.slug, a.summary, {"agent": "AGENT", "session": "SESSION"},
                            plan["archived"], plan["digest"], plan["size"], "<assigned on --apply>")
        out.add("pointer page that replaces the thread:")
        for line in page.splitlines()[:PREVIEW_LINES]:
            out.add("  " + line)
        out.add("then appends thread_archived; status hides the thread until `rmem thread reopen`")
    out.add("Rerun with --apply to write these changes.")
    out.emit()


def refuse_rearchive(core, hub: Path, slug: str, action: str) -> str:
    """The thread's state; archiving a pointer page again would bury the archived copy one level deeper."""
    state = states(core.events_locked(hub)).get(slug, "active")
    if action == "archive" and state == "archived":
        raise core.RmemError(f"thread {slug} is already archived; `rmem thread reopen {slug}` first if it has new content")
    return state


def cmd_rewrite(a, core, action):
    hub = core.find_hub()
    who = core.actor(a, required=a.apply)
    if getattr(a, "summary", None) is not None:
        core.one_line(a.summary, "summary")
    if getattr(a, "note", None) is not None:
        core.one_line(a.note, "note")
    if not a.apply:
        with core.hub_lock(hub, shared=True, readonly_ok=True):
            refuse_rearchive(core, hub, a.slug, action)
            _, _, plan = plan_rewrite(core, hub, a, action)
        print_plan(core, a, action, plan)
        return 0
    with core.hub_lock(hub):
        core.consistency_preflight(hub)
        core.authorize_thread_write(hub, a.slug, who, "log")
        state = refuse_rearchive(core, hub, a.slug, action)
        path, data, plan = plan_rewrite(core, hub, a, action)
        if action == "roll" and not plan["moved"]:
            print(f"nothing to roll: no section of threads/{a.slug}.md has more than {a.keep} block(s)")
            return 0
        # Order: exact bytes first, then the ledger event, then the index, and the live page last.
        if not plan["reuse"]:
            write_exact(core, hub, plan["archived"], data, stat.S_IMODE(path.stat().st_mode))
        # WHY: agents edit thread files without the hub lock, so an edit made while this ran would be lost from
        # both the live page and the archive; compare before recording anything.
        if read_bytes(core, path) != data:
            if not plan["reuse"]:
                core.safe_path(hub, plan["archived"]).unlink(missing_ok=True)
            raise core.RmemError(f"threads/{a.slug}.md changed while it was being {'archived' if action == 'archive' else 'rolled'} (another writer edited "
                                 "it); nothing was recorded and the thread was left as it is. Re-read it and run the "
                                 "command again")
        event = {**who, "kind": "thread_archived" if action == "archive" else "thread_rolled",
                 "thread": a.slug, "original_path": f"threads/{a.slug}.md", "archived_path": plan["archived"],
                 "archived_sha256": plan["digest"], "archived_bytes": plan["size"]}
        if action == "archive":
            event.update(note=a.summary, previous_state=state)
        else:
            event.update(keep=a.keep, moved_blocks=plan["moved"])
            if a.note:
                event["note"] = a.note
        event = core.append_locked(hub, event)
        plan["snapshots"]["snapshots"].append({
            "thread": a.slug, "action": action, "original_path": f"threads/{a.slug}.md",
            "archived_path": plan["archived"], "bytes": plan["size"], "sha256": plan["digest"],
            "event_id": event["event_id"], "sequence": event["sequence"], "captured_at": event["ts"],
            "agent": who["agent"], "session": who["session"], "relative_links_base": "threads",
            "summary": a.summary if action == "archive" else (a.note or "")})
        core.atomic_write(core.safe_path(hub, HISTORY_DIR, SNAPSHOTS),
                          json.dumps(plan["snapshots"], ensure_ascii=False, indent=2) + "\n")
        new_text = plan["new_text"] if action == "roll" else pointer_page(
            plan["text"], a.slug, a.summary, who, plan["archived"], plan["digest"], plan["size"], event["event_id"])
        if read_bytes(core, path) != data:
            redo = (f"run `rmem thread reopen {a.slug}`, then archive it again" if action == "archive"
                    else "run the roll again")
            raise core.RmemError(f"threads/{a.slug}.md changed after event {event['event_id']} recorded the {action}, "
                                 f"so the live file was left untouched: {plan['archived']} holds the earlier text and "
                                 f"the thread holds the newer edit. Re-read the thread and {redo}")
        core.atomic_write(path, new_text)
    detail = (f"rolled {a.slug}: moved {plan['moved']} block(s)" if action == "roll"
              else f"archived {a.slug}: status now hides it")
    print(f"{detail}; original bytes in {plan['archived']} (sha256 {plan['digest'][:12]}…); "
          f"event {event['event_id']}")
    return 0


# Staleness (rmem check)

def last_change_times(core, hub: Path, paths: list) -> dict:
    """path -> UTC datetime of its last change: last commit touching it if tracked and clean, else mtime."""
    times = {}
    for path in paths:
        try:
            times[path] = dt.datetime.fromtimestamp(path.stat().st_mtime, dt.timezone.utc)
        except OSError:
            continue  # vanished while checking
    paths = list(times)
    try:
        top = core.run_git(hub, "rev-parse", "--show-toplevel", check=False)
        if top.returncode:
            return times
        root = Path(top.stdout.strip()).resolve()
        status = core.run_git(hub, "--no-optional-locks", "status", "--porcelain=v1", "-z",
                              "--untracked-files=all", "--", "threads", check=False)
        if status.returncode:
            return times
        dirty = {entry[3:] for entry in status.stdout.split("\0") if len(entry) > 3}
        for path in paths:
            relative = str(path.resolve().relative_to(root))
            if relative in dirty:
                continue
            log = core.run_git(hub, "log", "-1", "--format=%ct", "--", f"threads/{path.name}", check=False)
            if not log.returncode and log.stdout.strip().isdigit():
                times[path] = dt.datetime.fromtimestamp(int(log.stdout.strip()), dt.timezone.utc)
    except (core.RmemError, OSError, ValueError):
        pass  # WHY: staleness is advice; without Git the file time still works.
    return times


def check_problems(ctx):
    core, hub = ctx.core, ctx.hub
    limit = ctx.config.get("staleness", {}).get("events_after_edit", 20)
    directory = core.safe_path(hub, "threads")
    paths = [path for path in sorted(directory.glob("*.md"))
             if not path.is_symlink() and path.is_file() and visible(ctx, path.stem)]
    problems = []
    if len(paths) > STALENESS_MAX_THREADS:
        problems.append(("info", f"staleness checked for the first {STALENESS_MAX_THREADS} of {len(paths)} "
                                 "visible threads; seal finished threads to keep status short"))
        paths = paths[:STALENESS_MAX_THREADS]
    changed = {path.stem: when for path, when in last_change_times(core, hub, paths).items()}
    counts = dict.fromkeys(changed, 0)
    for event in ctx.events:
        slug = event.get("thread")
        if slug not in changed or event.get("kind") in NOT_CONTENT_KINDS:
            continue
        try:
            when = dt.datetime.fromisoformat(str(event.get("ts")).replace("Z", "+00:00"))
        except ValueError:
            continue
        if when.tzinfo is not None and when > changed[slug]:
            counts[slug] += 1
    for slug, number in counts.items():
        if number > limit:
            problems.append(("warn", f"thread {slug} has {number} ledger events since its file last changed "
                                     f"({core.display_time(changed[slug].isoformat(), ctx.config)}; limit "
                                     f"staleness.events_after_edit = {limit}). Update its results and next step, "
                                     f"or seal it with `rmem thread seal {slug} --summary ...`"))
    return problems + archive_problems(ctx)


def archive_problems(ctx) -> list:
    """Archived thread copies must keep the exact bytes their archive or roll event recorded."""
    core, seen, missing, changed = ctx.core, set(), [], []
    for event in ctx.events:
        relative = event.get("archived_path")
        if event.get("kind") not in ("thread_archived", "thread_rolled") or not isinstance(relative, str) or relative in seen:
            continue
        seen.add(relative)
        try:
            path = core.safe_path(ctx.hub, relative)
            if not path.is_file():
                missing.append(relative)
            elif core.file_sha256(path) != event.get("archived_sha256"):
                changed.append(relative)
        except (OSError, core.RmemError) as exc:
            changed.append(f"{relative} (unreadable: {core.clipped(exc, 120)})")
    problems = [("fail", f"archived thread copy {relative} no longer matches the SHA-256 in its event; archived "
                         "copies are never edited, so restore the original bytes from Git history")
                for relative in changed[:5]]
    if len(changed) > 5:
        problems.append(("fail", f"{len(changed)} archived thread copies changed; showing the first 5"))
    if missing:
        problems.append(("warn", f"{len(missing)} archived thread copy(ies) named by ledger events are missing "
                                 f"(first: {missing[0]}); restore them from Git or from the machine that archived them"))
    return problems
