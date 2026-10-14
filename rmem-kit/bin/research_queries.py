"""Bounded ledger queries, JSONL export, note bodies and provenance fields.

A built-in feature module of bin/rmem (rmem-kit 0.4.0). Callers pass events that
were already read under the hub lock; nothing here rereads the ledger. Note and
command texts live in notes/<yyyy-mm>/<sha256 prefix>.md, named by their content,
so the ledger keeps a one-line summary plus the path and hash of the full text.
"""
import argparse
from collections import deque
import datetime as dt
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shlex
import stat
import sys
from types import SimpleNamespace
import uuid

import research_records as records
import rmem_config

QUERY_MAX = 200
LOCK_KINDS = frozenset({"claim", "release"})
NOTE_MAX_CHARS = 1000
VALUE_MAX_CHARS = 1000
CMD_INLINE_MAX = 4000
PAGE_DEFAULT, PAGE_MIN, PAGE_MAX = 4000, 200, 8000
READ_MAX_BYTES = 4 * 1024 * 1024
STATUS_VALUES = ("planned", "running", "keep", "discard", "crash", "inconclusive", "cancelled")
STORED_PREFIXES = ("body", "cmd")
STORED_PATH = re.compile(r"notes/[0-9]{4}-[0-9]{2}/(?:[0-9a-f]{16}|[0-9a-f]{64})\.md\Z")
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
FIELD_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,99}\Z")
DECIMAL = re.compile(r"[+-]?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?\Z")
CODE_REF = re.compile(r"[^\s:/@]+:\S*@([^\s@:/]+)\Z")
NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)

EVENT_STRING_FIELDS = {"body_path", "body_sha256", "cmd", "cmd_path", "cmd_sha256", "code_ref", "workdir"}

if "notes" not in rmem_config.DEFAULTS:
    rmem_config.add_group("notes", {"body_max_chars": 200000},
                          {"body_max_chars": rmem_config.integer(1000, 1000000)})


# Argument types -------------------------------------------------------------

def limit(value):
    """-n for recent and catalog: any nonnegative integer; the command reduces it to QUERY_MAX."""
    try:
        number = int(value)
    except ValueError:
        number = -1
    if number < 0:
        raise argparse.ArgumentTypeError(f"expected a nonnegative integer (above {QUERY_MAX} shows {QUERY_MAX})")
    return number


def sequence(value):
    try:
        number = int(value)
    except ValueError:
        number = -1
    if number < 0:
        raise argparse.ArgumentTypeError("expected a sequence number (0 or more), as shown after # in recent")
    return number


def page_number(value):
    try:
        number = int(value)
    except ValueError:
        number = 0
    if number < 1:
        raise argparse.ArgumentTypeError("expected an integer of 1 or more")
    return number


def kind_list(value):
    names = [name.strip() for name in value.split(",")]
    if not all(names):
        raise argparse.ArgumentTypeError("use one kind or a comma-separated list such as exp,finding")
    return frozenset(names)


def status_value(value):
    if value not in STATUS_VALUES:
        raise argparse.ArgumentTypeError(
            f"unknown status {value!r}; use planned or running for work in progress, keep, discard, crash "
            "or inconclusive for a result, or cancelled")
    return value


def clamp(n, noun):
    """(n reduced to QUERY_MAX, notice text or '')."""
    if n > QUERY_MAX:
        return QUERY_MAX, f"-n {n} is above the maximum of {QUERY_MAX}; showing at most {QUERY_MAX} {noun}"
    return n, ""


def add_provenance_options(parser):
    """Options shared by log and capture."""
    parser.add_argument("--note-file", help="multi-line note body from a file, or - for standard input; "
                        "stored under notes/ and read with show ID --field body")
    parser.add_argument("--cmd", help="exact command text that produced the result (long text is stored under notes/)")
    parser.add_argument("--cmd-file", help="read the exact command text from a file, or - for standard input")
    parser.add_argument("--workdir", help="working directory of the command, on the host where it ran")
    parser.add_argument("--code-ref", help="code that ran elsewhere, as HOST:PATH@REVISION")
    parser.add_argument("--set", action="append", metavar="KEY=VALUE",
                        help="structured field stored under 'fields'; decimal numbers become numbers; repeatable")
    parser.add_argument("--no-provenance", action="store_true",
                        help="skip the local Git probe (the code ran on another machine)")
    parser.add_argument("--allow-sensitive", action="store_true",
                        help="record text even though it looks like a key, token or password (it cannot be redacted later)")


# Selection ------------------------------------------------------------------

def positioned(events):
    """(position, event): the stored sequence, or the ledger position for a legacy row without one."""
    for ordinal, event in enumerate(events, 1):
        stored = event.get("sequence")
        yield (stored if type(stored) is int else ordinal), event


def event_instant(event):
    try:
        parsed = dt.datetime.fromisoformat(str(event.get("ts", "")).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else None


class Selection:
    """Shared filter for recent and export. Unreadable (CORRUPT) rows pass the time filters so they stay visible."""

    def __init__(self, kinds=None, thread=None, session=None, exclude_session=None, grep=None,
                 since=None, until=None, first=None, last=None, include_locks=True):
        self.kinds, self.thread, self.session, self.exclude_session = kinds, thread, session, exclude_session
        self.grep, self.since, self.until, self.first, self.last = grep, since, until, first, last
        self.hide_locks = not include_locks and not (kinds and kinds & LOCK_KINDS)

    def matches(self, event, position):
        if (self.first is not None and position < self.first) or (self.last is not None and position > self.last):
            return False
        if self.kinds and event.get("kind") not in self.kinds:
            return False
        if self.thread and event.get("thread") != self.thread:
            return False
        if self.session and event.get("session") != self.session:
            return False
        if self.exclude_session and event.get("session") == self.exclude_session:
            return False
        if (self.since or self.until) and event.get("kind") != "CORRUPT":
            instant = event_instant(event)
            if instant is None or (self.since and instant < self.since) or (self.until and instant > self.until):
                return False
        return not self.grep or bool(self.grep.search(json.dumps(event, ensure_ascii=False)))


class Start:
    """Where a forward page begins, in ledger (file) order. --after-pos K starts after line position K.
    --after-seq N starts after the first row whose stored sequence is N, or at the first row whose sequence
    is above N when no row has N; from there every later row counts, even one whose stored sequence repeats
    or goes backward (a merge, or `repair --conflict-markers`), so no row is ever skipped."""

    def __init__(self, after_seq=None, after_pos=None):
        self.after_seq, self.after_pos, self.started = after_seq, after_pos, after_seq is None and after_pos is None

    def admits(self, position, ordinal):
        if self.started:
            return True
        if self.after_pos is not None:
            self.started = ordinal > self.after_pos
            return self.started
        if position == self.after_seq:
            self.started = True
            return False
        self.started = position > self.after_seq
        return self.started


def select_recent(events, rule, n, forward=False, start=None):
    """The newest n matches, or with forward=True the first n (a page after --after-seq or --after-pos).
    Returns a namespace: kept [(position, event, locks before it, ordinal)], matched, hidden lock events
    within the window, first (position, ordinal) match, and monotonic (stored sequences only increase in
    ledger order). The window reaches the end of the selection unless more matches follow it."""
    start = start or Start()
    kept = [] if forward else deque(maxlen=n)
    matched = locks = 0
    first = boundary = previous = None
    monotonic = True
    for ordinal, (position, event) in enumerate(positioned(events), 1):
        if previous is not None and position <= previous:
            monotonic = False
        previous = position
        if not start.admits(position, ordinal) or not rule.matches(event, position):
            continue
        if rule.hide_locks and event.get("kind") in LOCK_KINDS:
            locks += 1
            continue
        matched += 1
        first = (position, ordinal) if first is None else first
        if not forward or len(kept) < n:
            kept.append((position, event, locks, ordinal))
        elif boundary is None:
            boundary = locks
    kept = list(kept)
    if forward:
        hidden = locks if boundary is None else boundary
    else:
        hidden = locks - kept[0][2] if kept and matched > len(kept) else locks
    return SimpleNamespace(kept=kept, matched=matched, hidden=hidden, first=first, monotonic=monotonic)


def cursor(selection, position, ordinal):
    """The option that starts a page after one row: --after-seq while stored sequences only increase,
    else --after-pos (a ledger line position), which cannot skip rows."""
    return f"--after-seq {position}" if selection.monotonic else f"--after-pos {ordinal}"


def recent_lines(selection, fmt, budget, forward=False, after=None, notice=""):
    """Event lines that fit the budget (dropping the far end of the window first) plus bracketed notes."""
    kept, matched, hidden, first = selection.kept, selection.matched, selection.hidden, selection.first
    lines = [fmt(event if type(event.get("sequence")) is int else {**event, "sequence": position})
             for position, event, _, _ in kept]
    fitted, used = [], 0
    for line in (lines if forward else reversed(lines)):
        if fitted and used + len(line) + 1 > budget:
            break
        fitted.append(line)
        used += len(line) + 1
    if not forward:
        fitted.reverse()
    shown, notes = len(fitted), [f"[{notice}]"] if notice else []
    paged = False
    if forward:
        if shown < matched:
            position, _, _, ordinal = kept[shown - 1]
            notes.append(f"[{matched - shown} more matching events; next page: {cursor(selection, position, ordinal)}]")
            paged = True
        elif not matched:
            notes.append(f"[no matching events after {after}]")
    elif shown < matched:
        budget_note = " (the output budget held no more)" if shown < len(lines) else ""
        notes.append(f"[showing the newest {shown} of {matched} matching events{budget_note}; "
                     f"read them from the start with {cursor(selection, first[0] - 1, first[1] - 1)} and the same filters]")
        paged = True
    elif not matched:
        notes.append("[no matching events]")
    if paged and not selection.monotonic:
        notes.append("[stored sequence numbers repeat or go backward in this ledger (a merge or a repair), so pages "
                     "follow ledger order with --after-pos, a line position]")
    if hidden:
        notes.append(f"[{hidden} claim/release events hidden; add --include-locks to show them]")
    return fitted + notes


# Export ---------------------------------------------------------------------

def export_target(hub, value, force):
    """Resolve --out: an existing directory, outside the hub's memory (inside the hub only under runs/)."""
    path = Path(value).expanduser()
    if path.name in ("", ".", ".."):
        raise ValueError("--out must name a file, for example --out /tmp/ledger-slice.jsonl")
    try:
        parent = path.parent.resolve(strict=True)
    except (FileNotFoundError, NotADirectoryError):
        raise ValueError(f"the directory for --out does not exist: {path.parent}; create it first") from None
    if not parent.is_dir():
        raise ValueError(f"the parent of --out is not a directory: {parent}")
    target, hub = parent / path.name, Path(hub).resolve()
    runs = hub / "runs"
    if (target == hub or hub in target.parents) and (runs.is_symlink() or runs not in target.parents):
        raise ValueError("refusing to write an export inside the hub's memory (ledger, threads, findings and the "
                         "rest); write it outside the hub, for example to your scratch directory, or under "
                         f"{runs}/ which Git ignores")
    if target.is_symlink():
        raise ValueError(f"refusing to write through a symlink: {target}")
    if target.exists():
        if not target.is_file():
            raise ValueError(f"--out exists and is not a regular file: {target}")
        if not force:
            raise ValueError(f"{target} already exists; pass --force to replace it, or choose another --out")
    return target


def write_jsonl(target, rows, force):
    """Write (position, event) rows atomically; legacy rows carry their ledger position as derived_sequence."""
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    summary = {"count": 0, "chars": 0, "corrupt": 0, "first": None, "last": None}
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | NOFOLLOW, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            for position, event in rows:
                stored = type(event.get("sequence")) is int
                line = json.dumps(event if stored else {**event, "derived_sequence": position}, ensure_ascii=False) + "\n"
                stream.write(line)
                summary["count"] += 1
                summary["chars"] += len(line)
                summary["corrupt"] += event.get("kind") == "CORRUPT"
                summary["first"] = position if summary["first"] is None else summary["first"]
                summary["last"] = position
            stream.flush()
            os.fsync(stream.fileno())
        if target.exists() and not force:
            raise ValueError(f"{target} appeared while exporting; pass --force to replace it")
        os.replace(temporary, target)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return summary


def cmd_export(a, core):
    hub = core.find_hub()
    if a.from_seq is not None and a.to_seq is not None and a.from_seq > a.to_seq:
        raise core.RmemError("--from-seq must not be after --to-seq")
    config = core.load_config(hub) if (a.since or a.until) else None
    since = core.parse_user_time(a.since, config) if a.since else None
    until = core.parse_user_time(a.until, config) if a.until else None
    if since and until and since > until:
        raise core.RmemError("--since must not be after --until")
    target = export_target(hub, a.out, a.force)
    rule = Selection(kinds=a.kind, thread=a.thread, since=since, until=until, first=a.from_seq, last=a.to_seq)
    with core.hub_lock(hub, shared=True, readonly_ok=True):
        rows = (item for item in positioned(core.events_locked(hub)) if rule.matches(item[1], item[0]))
        summary = write_jsonl(target, rows, a.force)
    out = core.Output()
    span = f" (#{summary['first']} to #{summary['last']})" if summary["count"] else ""
    out.add(f"exported {summary['count']} events{span} to {target} ({summary['chars']:,} characters, one JSON "
            f"object per line); read it in pages with your own tools, for example: sed -n '1,20p' {shlex.quote(str(target))}")
    if summary["corrupt"]:
        out.add(f"[{summary['corrupt']} unreadable ledger lines are exported as CORRUPT placeholders; run rmem check]")
    out.emit()
    return 0


def register(commands, core):
    p = commands.add_parser("export", help="write selected ledger events as JSONL to a file you page with your own tools")
    p.add_argument("--from-seq", type=sequence, help="first stored sequence number to include (a filter on the number, not ledger order)")
    p.add_argument("--to-seq", type=sequence, help="last stored sequence number to include")
    p.add_argument("--since", help="ingested at or after: local time, ISO 8601 with offset, or an age such as 6h or 3d")
    p.add_argument("--until", help="ingested at or before (same formats as --since)")
    p.add_argument("--kind", type=kind_list, help="one kind or a comma-separated list")
    p.add_argument("--thread", type=core.identifier)
    p.add_argument("--out", required=True, help="new file outside the hub (or under the hub's runs/)")
    p.add_argument("--force", action="store_true", help="replace an existing --out file")
    p.set_defaults(fn=lambda a: cmd_export(a, core))


# Note bodies and command texts ----------------------------------------------

class Draft(dict):
    """A log event before append, plus the note and command texts to store under notes/."""

    def __init__(self, event, bodies=None):
        super().__init__(event)
        self.bodies = dict(bodies or {})


def single_line(value, label, limit_chars=VALUE_MAX_CHARS):
    if not value.strip() or len(value) > limit_chars or any(c in value for c in "\r\n\0"):
        raise ValueError(f"{label} must be one nonempty line of at most {limit_chars:,} characters")
    return value


def check_summary(value):
    """--note is the one-line summary; longer or multi-line text belongs in --note-file."""
    if value.strip() and len(value) <= NOTE_MAX_CHARS and not any(c in value for c in "\r\n\0"):
        return value
    found = ("several lines" if any(c in value for c in "\r\n") else
             f"{len(value):,} characters" if value.strip() else "an empty note")
    raise ValueError(f"--note must be one nonempty line of at most {NOTE_MAX_CHARS:,} characters (found {found}). "
                     "Keep a one-line summary in --note and pass the full text with --note-file PATH, "
                     "or --note-file - to read it from standard input")


def summary_from(text):
    first = next(line.strip() for line in text.splitlines() if line.strip())
    return first if len(first) <= 300 else first[:299] + "…"


def check_text(text, label, max_chars):
    if len(text) > max_chars:
        raise ValueError(f"{label} has {len(text):,} characters, above the limit of {max_chars:,} "
                         "(notes.body_max_chars in hub-config.json); split it, or keep it in a file you cite with --source")
    if "\0" in text:
        raise ValueError(f"{label} contains a NUL character; pass plain text")
    if not text.strip():
        raise ValueError(f"{label} is empty")
    return text


def read_text_input(value, label, max_chars, stdin=None):
    """UTF-8 text from a regular file, or from standard input for '-'."""
    cap = max_chars * 4 + 4
    if value == "-":
        data = (stdin or sys.stdin.buffer).read(cap + 1)
    else:
        try:
            fd = os.open(Path(value).expanduser(), os.O_RDONLY | os.O_NONBLOCK)
        except OSError as exc:
            raise ValueError(f"{label}: cannot open {value}: {exc.strerror}") from None
        with os.fdopen(fd, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ValueError(f"{label} must be a regular file, or - for standard input")
            data = stream.read(cap + 1)
    if len(data) > cap:
        raise ValueError(f"{label} is longer than {max_chars:,} characters (notes.body_max_chars in hub-config.json)")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError(f"{label} must be UTF-8 text") from None
    return check_text(text, label, max_chars)


def text_sha256(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def stored_fields(prefix, text):
    """Event fields for a text kept under notes/; the path is provisional until store_bodies writes it."""
    digest = text_sha256(text)
    month = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m")
    return {f"{prefix}_path": f"notes/{month}/{digest[:16]}.md", f"{prefix}_sha256": digest,
            f"{prefix}_chars": len(text)}


def number_or_text(value):
    if DECIMAL.fullmatch(value):
        number = float(value) if any(c in value for c in ".eE") else int(value)
        # WHY: keep integers that JSON readers cannot hold exactly as text.
        if math.isfinite(number) and abs(number) < 2 ** 53:
            return number
    return value


def parse_assignments(values):
    result = {}
    for item in values or []:
        key, separator, value = item.partition("=")
        if not separator or not key or not value:
            raise ValueError(f"--set needs KEY=VALUE (found {item[:80]!r}), for example --set seed=42 --set data.sha=abc123")
        if not FIELD_KEY.fullmatch(key):
            raise ValueError(f"--set key {key[:80]!r} must be 1 to 100 letters, digits or ._:/- and start with a letter or digit")
        if key in result:
            raise ValueError(f"--set key {key!r} is given twice; give each key once")
        result[key] = number_or_text(single_line(value, f"--set {key}"))
    return result


def provenance_fields(a, max_chars):
    """(event fields, texts for notes/) from --cmd/--cmd-file/captured argv, --workdir, --code-ref and --set."""
    get = lambda name: getattr(a, name, None)
    fields, texts = {}, {}
    if get("cmd") is not None and get("cmd_file"):
        raise ValueError("use either --cmd or --cmd-file, not both")
    argv = list(get("argv") or [])
    text = None
    if get("cmd_file"):
        text = read_text_input(a.cmd_file, "--cmd-file", max_chars)
    elif get("cmd") is not None:
        text = check_text(a.cmd, "--cmd", max_chars)
    elif argv:
        text = shlex.join(argv)
    if text is not None and len(text) <= CMD_INLINE_MAX:
        fields["cmd"] = text
        if argv:
            fields["argv"] = argv
    elif text is not None:
        fields.update(stored_fields("cmd", text))
        texts["cmd"] = text
    for name, flag in (("workdir", "--workdir"), ("code_ref", "--code-ref")):
        if get(name) is not None:
            fields[name] = single_line(get(name), flag)
    match = CODE_REF.fullmatch(fields.get("code_ref", ""))
    if match and get("commit") is None:
        fields["commit"] = match[1]  # the revision of the code that ran, not this machine's checkout
    assignments = parse_assignments(get("set"))
    if assignments:
        fields["fields"] = assignments
    if get("captured"):
        fields["captured"] = True
    return fields, texts


def provenance_hint(event):
    """A hint when a remote --host result carries this machine's checkout as its commit."""
    code = event.get("provenance") or {}
    if (event.get("host") and event.get("recorded_host") and event["host"] != event["recorded_host"]
            and code.get("commit") and code.get("commit") == event.get("commit")):
        return (f"commit {event['commit'][:12]} comes from this machine's checkout ({code.get('repository')}); "
                f"for code that ran on {event['host']} pass --code-ref HOST:PATH@REVISION, or --no-provenance")
    return ""


def remove_created(paths):
    for path in reversed(paths):
        try:
            if path.is_dir() and not path.is_symlink():
                path.rmdir()  # only when empty
            else:
                path.unlink()
        except OSError:
            pass


def stored_state(hub, relative, expected):
    """('ok'|'changed'|'missing', text or None) for a stored note or command file."""
    if not isinstance(relative, str) or not STORED_PATH.fullmatch(relative):
        raise ValueError(f"invalid stored text path {str(relative)[:120]!r}")
    path = Path(hub)
    for part in relative.split("/"):
        path = path / part
        if path.is_symlink():
            raise ValueError(f"refusing symlink: {relative}")
    try:
        fd = os.open(path, os.O_RDONLY | NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return "missing", None
    with os.fdopen(fd, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError(f"expected a regular file: {relative}")
        data = stream.read(READ_MAX_BYTES + 1)
    state = "ok" if hashlib.sha256(data).hexdigest() == expected else "changed"
    return state, data.decode("utf-8", errors="replace")


def store_text(hub, text, digest):
    """Write text under notes/<UTC yyyy-mm>/<sha256 prefix>.md unless an identical copy exists anywhere
    under notes/. Returns (hub-relative path, paths created) so a refused append can remove them."""
    notes = Path(hub) / "notes"
    if notes.is_symlink():
        raise ValueError("refusing symlink: notes/")
    for name in (digest[:16] + ".md", digest + ".md"):
        for existing in sorted(notes.glob("*/" + name)) if notes.is_dir() else []:
            relative = existing.relative_to(hub).as_posix()
            if STORED_PATH.fullmatch(relative) and stored_state(hub, relative, digest)[0] == "ok":
                return relative, []
    month, created = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m"), []
    for directory in (notes, notes / month):
        if directory.is_symlink():
            raise ValueError(f"refusing symlink: {directory}")
        if not directory.exists():
            directory.mkdir()
            created.append(directory)
    target = notes / month / (digest[:16] + ".md")
    if target.exists() or target.is_symlink():  # another text shares the prefix: use the full hash
        target = notes / month / (digest + ".md")
        if target.exists() or target.is_symlink():
            remove_created(created)
            raise ValueError(f"{target.relative_to(hub)} exists with different content; run rmem check")
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    done = False
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | NOFOLLOW, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)  # the caller holds the exclusive hub lock
        done = True
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        if not done:
            remove_created(created)
    return target.relative_to(hub).as_posix(), created + [target]


def store_bodies(hub, event):
    """Store a Draft's texts and set their final paths; returns the created paths."""
    created = []
    try:
        for prefix, text in getattr(event, "bodies", {}).items():
            relative, made = store_text(hub, text, event[f"{prefix}_sha256"])
            event[f"{prefix}_path"] = relative
            created += made
    except BaseException:
        remove_created(created)
        raise
    return created


# Field pages for show ----------------------------------------------------------

def field_text(hub, event, field):
    """(text, extra view keys) for one field: a stored body or command, a dotted path, or the whole event."""
    if field in STORED_PREFIXES and event.get(f"{field}_path"):
        relative, expected = event[f"{field}_path"], event.get(f"{field}_sha256")
        state, text = stored_state(hub, relative, expected)
        if state == "missing":
            raise ValueError(f"{relative} is missing from this copy of the hub; restore it from Git or from the "
                             "machine that logged it (rmem sync commits notes/)")
        extra = {"path": relative, "sha256": expected, "verified": state == "ok"}
        if state != "ok":
            extra["integrity"] = "The file no longer matches the SHA-256 recorded in the event; treat it as unverified and run rmem check."
        return text, extra
    if field == "body":
        raise ValueError("this event has no note body (it was logged without --note-file); its summary is --field note")
    value = event
    if field != "event":
        for part in field.split("."):
            if not isinstance(value, dict) or part not in value:
                names = sorted(set(event) | {"event"} | {p for p in STORED_PREFIXES if event.get(f"{p}_path")})
                raise ValueError(f"the event has no field {field!r}; available: {', '.join(names)[:600]}")
            value = value[part]
    if isinstance(value, str):
        return value, {}
    return json.dumps(value, ensure_ascii=False, indent=2), {"encoding": "json"}


def field_view(hub, events, identifier, field, page=1, page_size=None):
    """One page of one field of the latest event for an ID (or of an exact event UUID)."""
    history, exact, _ = records.event_history(events, identifier)
    if not history:
        raise ValueError(f"no ledger event has the ID {identifier!r}; --field reads ledger events "
                         "(use show ID --section record for a reviewed card, or catalog --query TEXT)")
    event = history[-1]
    text, extra = field_text(hub, event, field)
    requested = page_size or PAGE_DEFAULT
    size = min(max(requested, PAGE_MIN), PAGE_MAX)
    pages = max(1, math.ceil(len(text) / size))
    if page > pages:
        raise ValueError(f"page {page} does not exist: field {field} has {pages} page(s) of up to {size:,} characters")
    start = (page - 1) * size
    chunk = text[start:start + size]
    reference = event.get("event_id") or identifier
    following = None
    if page < pages:
        following = f"rmem show {reference} --field {field} --page {page + 1}" + (
            f" --page-size {size}" if size != PAGE_DEFAULT else "")
    view = {"identifier": identifier, "event_id": event.get("event_id"), "experiment_id": event.get("id"),
            "field": field, "chars": len(text), "page": page, "pages": pages, "page_size": size,
            "offset": start, "length": len(chunk), **extra, "text": chunk, "next": following}
    if size != requested:
        view["notice"] = f"--page-size {requested} is outside {PAGE_MIN} to {PAGE_MAX}; using {size}"
    if len(history) > 1 and not exact:
        view["limits"] = ("Latest update for this ID; pass an event_id from show ID --section history "
                          "to read an earlier update.")
    return view


def field_text_output(view):
    """--format text: a header line, the page text, and the next-page command."""
    end = view["offset"] + view["length"]
    header = (f"# {view['identifier']} field {view['field']}: characters {view['offset'] + 1:,} to {end:,} "
              f"of {view['chars']:,} (page {view['page']} of {view['pages']})")
    if view.get("event_id") and view["event_id"] != view["identifier"]:
        header += f", event {view['event_id']}"
    parts = [header + "\n"] + [f"[{view[key]}]\n" for key in ("notice", "integrity") if view.get(key)]
    text = view["text"]
    parts.append(text if not text or text.endswith("\n") else text + "\n")
    parts.append(f"[next page: {view['next']}]\n" if view["next"] else "[end of field]\n")
    return "".join(parts)


# Hooks ---------------------------------------------------------------------------

def validate_event(event):
    for prefix in STORED_PREFIXES:
        path, digest, chars = (event.get(f"{prefix}_{name}") for name in ("path", "sha256", "chars"))
        if path is None and digest is None and chars is None:
            continue
        if not isinstance(path, str) or not STORED_PATH.fullmatch(path):
            return f"event {prefix}_path must name a file under notes/<yyyy-mm>/"
        if not isinstance(digest, str) or not SHA256.fullmatch(digest):
            return f"event {prefix}_sha256 must be a SHA-256 hex digest"
        if type(chars) is not int or chars < 0:
            return f"event {prefix}_chars must be a nonnegative integer"
    fields = event.get("fields")
    if fields is not None:
        if not isinstance(fields, dict):
            return "event fields must be an object"
        for value in fields.values():
            if not isinstance(value, (str, int, float)) or isinstance(value, bool):
                return "event fields values must be strings or finite numbers"
            if isinstance(value, float) and not math.isfinite(value):
                return "event fields numbers must be finite"
    if "captured" in event and not isinstance(event["captured"], bool):
        return "event captured must be a boolean"
    return ""


def check_problems(ctx):
    """Stored note and command files: a changed file fails; a missing or unreferenced one is reported."""
    referenced, missing, changed, problems = set(), [], [], []
    for event in ctx.events:
        for prefix in STORED_PREFIXES:
            relative = event.get(f"{prefix}_path")
            if not relative or relative in referenced:
                continue
            referenced.add(relative)
            try:
                state, _ = stored_state(ctx.hub, relative, event.get(f"{prefix}_sha256"))
            except (OSError, ValueError) as exc:
                problems.append(("fail", f"note file {relative}: {exc}"))
                continue
            if state == "missing":
                missing.append(relative)
            elif state == "changed":
                changed.append(relative)
    for relative in changed[:5]:
        problems.append(("fail", f"note file {relative} no longer matches the SHA-256 in its event; note files "
                                 "are never edited, so restore the original bytes from Git history"))
    if len(changed) > 5:
        problems.append(("fail", f"{len(changed)} note files changed; showing the first 5"))
    if missing:
        problems.append(("warn", f"{len(missing)} note file(s) named by ledger events are missing (first: "
                                 f"{missing[0]}); restore them from Git or from the machine that logged them"))
    notes = Path(ctx.hub) / "notes"
    if notes.is_dir() and not notes.is_symlink():
        orphans = [p for p in notes.glob("*/*.md") if p.relative_to(ctx.hub).as_posix() not in referenced]
        if orphans:
            problems.append(("info", f"{len(orphans)} file(s) under notes/ are not named by any event "
                                     f"(first: {orphans[0].relative_to(ctx.hub).as_posix()}); an interrupted log can leave one"))
    return problems
