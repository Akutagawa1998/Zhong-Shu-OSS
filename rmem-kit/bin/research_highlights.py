"""Small immutable memory snapshots. No network, model, or controller operations.

Manifest v1 (0.3, and `--freeze-ledger`) copies the ledger prefix into the snapshot.
Manifest v2 (0.4) records the prefix's size and SHA-256 plus a short digest and the
event ID of every captured line, as a delta against an earlier v2 highlight, and
rebuilds the prefix from the live ledger to verify or restore it.
"""
import datetime as dt
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import tempfile
import uuid
from urllib.parse import urlsplit

FILE_LIMIT = 1024 * 1024
TOTAL_LIMIT = 8 * FILE_LIMIT
LEDGER_LIMIT = 16 * FILE_LIMIT  # v1 (--freeze-ledger) only
DIGESTS_LIMIT = 64 * FILE_LIMIT
SPEC_LIMIT = 48000
MANIFEST_LIMIT = 128000
VIEW_LIMIT = 12000
MAX_FILES = 32
MAX_CHAIN = 16      # every 16th v2 highlight lists all lines, so a verification follows at most 16 bases
INDEX_ROWS = 100
PREFIX_NAME = "ledger-prefix.jsonl"
DIGESTS_NAME = "ledger-lines.txt"
INDEX_NAME = "HIGHLIGHTS_INDEX.md"
LINE_DIGEST = "sha256-first-16-hex"
HIGHLIGHT_ID = re.compile(r"hl-[0-9a-f]{32}\Z")
SHA = re.compile(r"[0-9a-f]{64}\Z")
EVENT_UUID = re.compile(r"[0-9a-f]{32}|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
DIGEST_ROW = re.compile(rb"([0-9a-f]{16}) (-|[0-9a-f-]{32,36})\n")
GIT_LOCATION = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR", "GIT_PREFIX")
KINDS = {"milestone", "version", "finding", "correction"}
STATUSES = {"observed", "reported", "inferred", "inconclusive", "retracted"}
CONTROL_PARTS = {".git", ".codex", ".claude", ".ssh", ".aws", ".gnupg", "claims"}
SECRET_NAME = re.compile(r"(^\.env($|\.)|secret|credential|(^|[_.-])(token|private[-_]?key)([_.-]|$)|^(auth|id_rsa|id_ed25519)(\.|$)|\.(pem|key|p12|pfx)$)", re.I)
SECRET_TEXT = re.compile(
    r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----|"
    r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b|\bgh[pousr]_[A-Za-z0-9]{20,}\b|\bgithub_pat_[A-Za-z0-9_]{20,}\b|"
    r"\bsk-(?:proj-|ant-)?[A-Za-z0-9_-]{20,}\b|\bhf_[A-Za-z0-9]{30,}\b|\bxox[abprs]-[A-Za-z0-9-]{10,}\b|"
    r"(?i:(?:api[_-]?key|access[_-]?token|client[_-]?secret|password)\s*[\"']?\s*[:=]\s*[\"'][^\"'\s]{6,}[\"'])|"
    # An unquoted NAME=value whose name ends like a credential (HF_TOKEN=..., WANDB_API_KEY=...) and whose
    # value mixes letters and digits; a shell reference such as $HF_TOKEN is not a value.
    r"(?i:\b[A-Za-z0-9_]*(?:token|_key|apikey|secret|password|passwd)[\"']?\s*=\s*"
    r"(?=[^\s\"'$]*[0-9])(?=[^\s\"'$]*[A-Za-z])[^\s\"'$]{8,})"
)


def redact_secrets(text):
    """Text with every secret-looking value replaced by <redacted>; an assignment keeps its name."""
    def replace(match):
        cut = re.search(r"[:=]\s*[\"']?", match.group(0))
        return (match.group(0)[:cut.end()] if cut else "") + "<redacted>"
    return SECRET_TEXT.sub(replace, text)


def need(condition, message):
    if not condition:
        raise ValueError(message)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def instant(value):
    need(isinstance(value, str), "time must be a zoned ISO timestamp")
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    need(parsed.tzinfo is not None, "time must include timezone")
    return parsed


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode()


def no_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        need(key not in result, "duplicate JSON key")
        result[key] = value
    return result


def parse_json(data):
    return json.loads(data, object_pairs_hook=no_duplicate_keys,
                      parse_constant=lambda value: (_ for _ in ()).throw(ValueError("nonfinite JSON")))


def relative_name(name):
    need(isinstance(name, str) and 0 < len(name) <= 300, "file path must be a short repository-relative string")
    parts = name.split("/")
    need(not name.startswith("/") and "\\" not in name and ":" not in name
         and all(p not in {"", ".", ".."} for p in parts)
         and not any(ord(c) < 32 for c in name), "unsafe relative file path")
    return PurePosixPath(name)


def safe_path(root, relative):
    path = root
    for part in relative_name(relative).parts:
        path = path / part
        need(not path.is_symlink(), "symlink paths are forbidden")
    need(path.is_relative_to(root), "path escapes root")
    return path


def regular_bytes(path, limit):
    # Traverse with directory FDs: a substituted parent symlink must not redirect
    # the read outside the reviewed tree between path validation and open.
    path = path.absolute()
    parent_fd = os.open(path.anchor, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        for part in path.parts[1:-1]:
            next_fd = os.open(part, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0), dir_fd=parent_fd)
            os.close(parent_fd)
            parent_fd = next_fd
        fd = os.open(path.name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=parent_fd)
    finally:
        os.close(parent_fd)
    try:
        before = os.fstat(fd)
        need(stat.S_ISREG(before.st_mode) and before.st_nlink == 1,
             "only regular, non-hardlinked files can be frozen")
        need(before.st_size <= limit, "file exceeds snapshot size limit")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            data = stream.read(limit + 1)
        after = os.fstat(fd)
        live = path.lstat()
        need(len(data) <= limit and (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
             == (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
             == (live.st_dev, live.st_ino, live.st_size, live.st_mtime_ns), "file changed during capture")
        return data
    finally:
        os.close(fd)


def safe_text(data):
    text = data.decode("utf-8")
    need("\0" not in text, "only UTF-8 text files can be frozen")
    need(not SECRET_TEXT.search(text), "possible secret material: snapshot rejected; content was not printed")
    return text


def selected_name(name):
    parts = relative_name(name).parts
    need(not any(part.lower() in CONTROL_PARTS or SECRET_NAME.search(part) for part in parts),
         "secret or control paths cannot be selected")
    need(parts[0] != ".rmem", "hub routing files cannot be selected")
    need(not any(part.startswith(".rmem-lock") or part == ".rmem.lock" for part in parts), "lock files cannot be selected")


def validate_spec(spec):
    required = {"schema_version", "kind", "title", "occurred_at", "summary", "significance",
                "evidence_status", "caveats", "event_ids", "experiment_ids", "supersedes",
                "files", "remote_artifacts", "content_reviewed_no_secrets"}
    need(isinstance(spec, dict) and set(spec) == required, "highlight spec keys must exactly match HIGHLIGHT_TEMPLATE.json")
    need(spec["schema_version"] == 1 and type(spec["schema_version"]) is int, "unsupported highlight schema")
    need(spec["kind"] in KINDS and spec["evidence_status"] in STATUSES, "invalid highlight kind/evidence_status")
    for name, limit in (("title", 200), ("summary", 1200), ("significance", 2000)):
        need(isinstance(spec[name], str) and 0 < len(spec[name].strip()) <= limit, "invalid " + name)
    instant(spec["occurred_at"])
    need(spec["content_reviewed_no_secrets"] is True, "explicit content_reviewed_no_secrets=true attestation required")
    for key in ("caveats", "event_ids", "experiment_ids", "supersedes", "files"):
        values = spec[key]
        need(isinstance(values, list) and len(values) <= (MAX_FILES if key == "files" else 32), "invalid " + key)
        need(all(isinstance(v, str) and 0 < len(v) <= 1000 for v in values), "invalid " + key + " entry")
        need(len({v.casefold() for v in values}) == len(values), "duplicate " + key)
    need(spec["files"], "select at least one small repository file to freeze")
    for name in spec["files"]:
        selected_name(name)
    for identity in spec["supersedes"]:
        need(HIGHLIGHT_ID.fullmatch(identity), "supersedes must contain highlight IDs")
    remotes = spec["remote_artifacts"]
    need(isinstance(remotes, list) and len(remotes) <= 32, "invalid remote_artifacts")
    for item in remotes:
        need(isinstance(item, dict) and set(item) == {"platform", "uri", "evidence_status", "checked_at"}, "invalid remote artifact fields")
        need(item["platform"] in {"torch", "alice", "145", "github", "hf", "remote", "other"}, "invalid remote platform")
        need(item["evidence_status"] in STATUSES, "invalid remote evidence_status")
        uri = item["uri"]
        need(isinstance(uri, str) and 0 < len(uri) <= 1500 and not any(ord(c) < 32 for c in uri), "invalid remote pointer")
        parsed = urlsplit(uri)
        need(parsed.username is None and parsed.password is None and not parsed.query and not parsed.fragment,
             "remote pointer cannot contain credentials, query strings or fragments")
        if item["checked_at"] is not None:
            instant(item["checked_at"])
    data = json_bytes(spec)
    need(len(data) <= SPEC_LIMIT, "highlight spec too large")
    safe_text(data)


def git_state(hub):
    def git(*args):
        return subprocess.run(["git", "-C", str(hub), *args], text=True, capture_output=True, check=True).stdout.strip()
    try:
        root = Path(git("rev-parse", "--show-toplevel")).resolve()
        revision = git("rev-parse", "HEAD")
        dirty = bool(git("status", "--porcelain"))
    except subprocess.CalledProcessError as error:
        raise ValueError("capture requires a hub inside a Git repository with a commit") from error
    return root, {"revision": revision, "dirty": dirty}


def highlight_directory(hub, identity=None):
    parent = safe_path(hub, "highlights")
    need(not parent.exists() or parent.is_dir(), "highlights must be a directory")
    if identity is None:
        return parent
    need(isinstance(identity, str) and HIGHLIGHT_ID.fullmatch(identity), "invalid highlight ID")
    path = safe_path(hub, "highlights/" + identity)
    need(path.is_dir(), "unknown highlight ID")
    return path


def read_live(hub):
    """A stable byte snapshot of the live ledger (no size limit)."""
    path = safe_path(hub, "ledger.jsonl")
    return regular_bytes(path, path.stat().st_size)


def short_digest(line):
    return hashlib.sha256(line).hexdigest()[:16]


def line_event_id(line):
    """The event UUID of a ledger line, or None (blank, not JSON, or no UUID)."""
    try:
        event = json.loads(line) if line.strip() else None
    except (ValueError, RecursionError):
        return None
    value = event.get("event_id") if isinstance(event, dict) else None
    return value if isinstance(value, str) and EVENT_UUID.fullmatch(value) else None


def digest_rows(lines):
    return b"".join(f"{short_digest(line)} {line_event_id(line) or '-'}\n".encode() for line in lines)


def choose_base(hub, events, live):
    """The newest v2 highlight whose prefix is still the exact live prefix, as a base record; else None."""
    for event in reversed(events):
        if event.get("kind") != "highlight_created" or not isinstance(event.get("id"), str):
            continue
        try:
            manifest = verify(hub, event["id"], events, live=live)
        except (ValueError, OSError, KeyError, TypeError):
            return None
        if manifest["schema_version"] != 2:
            continue
        prefix = manifest["ledger_prefix"]
        if manifest["history_verification"]["relation"] != "exact_prefix" or prefix["chain_depth"] + 1 >= MAX_CHAIN:
            return None
        lines = live.splitlines(keepends=True)
        if len(b"".join(lines[:prefix["line_count"]])) != prefix["bytes"]:
            return None
        return {key: prefix[key] for key in ("line_count", "bytes", "sha256", "chain_depth")} | {"highlight_id": event["id"]}
    return None


def ledger_record(hub, events, live):
    """(v2 ledger_prefix record, ledger-lines.txt bytes) for the whole live ledger."""
    lines = live.splitlines(keepends=True)
    base = choose_base(hub, events, live)
    rows = digest_rows(lines[base["line_count"]:] if base else lines)
    record = {"storage": "line_digests", "bytes": len(live), "sha256": digest(live), "line_count": len(lines),
              "event_count": len(events), "last_event_id": events[-1].get("event_id") if events else None,
              "last_sequence": events[-1].get("sequence") if events else None, "line_digest": LINE_DIGEST,
              "base": base, "chain_depth": base["chain_depth"] + 1 if base else 0, "digests_path": DIGESTS_NAME,
              "digests_bytes": len(rows), "digests_sha256": digest(rows),
              "digests_count": len(lines) - (base["line_count"] if base else 0)}
    return record, rows


def freeze(hub, spec_file, who, events, freeze_ledger=False):
    """Caller holds the hub lock and project-state claim; returns unpublished ledger receipt."""
    spec_file = Path(spec_file).expanduser().absolute()
    for parent in (spec_file, *spec_file.parents):
        need(not parent.is_symlink(), "spec path cannot contain symlinks")
    spec = parse_json(regular_bytes(spec_file, SPEC_LIMIT))
    validate_spec(spec)
    need(all(e.get("kind") != "CORRUPT" for e in events), "cannot capture corrupt ledger")
    known = {e.get("event_id") for e in events}
    need(set(spec["event_ids"]) <= known, "unknown linked event ID")
    for identity in spec["supersedes"]:
        verify(hub, identity, events)
    repo, git = git_state(hub)
    sources = []
    total = 0
    for name in spec["files"]:
        source = safe_path(repo, name)
        need(not source.is_relative_to(highlight_directory(hub)), "nested highlight capture is forbidden")
        need(not source.is_relative_to(hub / "runs"), "raw command capture directories are not snapshot inputs")
        ignored = subprocess.run(["git", "-C", str(repo), "check-ignore", "--quiet", "--", name], capture_output=True)
        need(ignored.returncode == 1, "ignored paths cannot be frozen")
        data = regular_bytes(source, FILE_LIMIT)
        safe_text(data)
        total += len(data)
        need(total <= TOTAL_LIMIT, "selected files exceed total size limit")
        sources.append((name, data))
    if freeze_ledger:
        ledger = regular_bytes(hub / "ledger.jsonl", LEDGER_LIMIT)
        safe_text(ledger)
        record = {"path": PREFIX_NAME, "bytes": len(ledger), "sha256": digest(ledger),
                  "event_count": len(events), "last_event_id": events[-1].get("event_id") if events else None,
                  "last_sequence": events[-1].get("sequence") if events else None}
        extra = (PREFIX_NAME, ledger)
    else:
        # WHY: v2 keeps line digests instead of a ledger copy, so snapshots stay small as the ledger grows.
        record, rows = ledger_record(hub, events, read_live(hub))
        extra = (DIGESTS_NAME, rows)
    identity = "hl-" + uuid.uuid4().hex
    parent = highlight_directory(hub)
    parent.mkdir(exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".capture-", dir=parent))
    target = parent / identity
    try:
        manifest = {"schema_version": 1 if freeze_ledger else 2, "highlight_id": identity,
                    "captured_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                    "captured_by": {key: who[key] for key in ("agent", "session")}, "spec": spec,
                    "source_repository": {"path_at_capture": str(repo), **git},
                    "ledger_prefix": record,
                    "files": [],
                    "limits": "Historical bytes and stated evidence status only; no scientific acceptance, remote backup, live authority or execution authorization."}
        for name, data in sources:
            frozen = "files/" + name
            destination = safe_path(staging, frozen)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(data)
            manifest["files"].append({"source_path": name, "frozen_path": frozen, "bytes": len(data), "sha256": digest(data)})
        (staging / extra[0]).write_bytes(extra[1])
        encoded = json_bytes(manifest)
        need(len(encoded) <= MANIFEST_LIMIT, "highlight manifest too large")
        (staging / "MANIFEST.json").write_bytes(encoded)
        (staging / "MANIFEST.sha256").write_text(digest(encoded) + "\n")
        for path in staging.rglob("*"):
            if path.is_file():
                path.chmod(0o444)
        need(not target.exists(), "highlight ID collision")
        os.rename(staging, target)
        return {"id": identity, "manifest_sha256": digest(encoded), "highlight_path": str(target.relative_to(hub)),
                "summary": spec["summary"], "occurred_at": spec["occurred_at"], "captured_at": manifest["captured_at"]}
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def ledger_rows(data):
    """(line bytes, event or None for a blank line); an unterminated unparseable final line is skipped."""
    result = []
    lines = data.splitlines(keepends=True)
    for index, line in enumerate(lines):
        if line.strip():
            if index == len(lines) - 1 and not line.endswith(b"\n"):
                try:
                    parse_json(line)
                except ValueError:
                    break  # an append in progress, seen by a reader without the hub lock
            event = parse_json(line)
            need(isinstance(event, dict), "live ledger prefix verification requires JSON event objects")
            result.append((line, event))
        else:
            result.append((line, None))
    return result


_LIVE = {"data": None, "index": None}


def live_index(live):
    """(rows, event_id -> (position, line, event), highlight id -> anchors) for the live ledger, parsed once."""
    if _LIVE["data"] is not live:
        rows, by_id, anchors = ledger_rows(live), {}, {}
        for index, (line, event) in enumerate(rows):
            event_id = event.get("event_id") if event else None
            if event_id:
                need(isinstance(event_id, str) and event_id not in by_id, "live ledger prefix verification rejects duplicate event UUIDs")
                by_id[event_id] = (index, line, event)
            if event and event.get("kind") == "highlight_created":
                anchors.setdefault(event.get("id"), []).append(event)
        _LIVE.update(data=live, index=(rows, by_id, anchors))
    return _LIVE["index"]


def live_anchor(anchors, identity, manifest_hash):
    found = anchors.get(identity, [])
    need(len(found) == 1 and found[0].get("manifest_sha256") == manifest_hash,
         "live ledger lacks a unique matching highlight anchor")


def subsequence_relation(captured, current, by_id, count):
    """Match captured (event_id, line check) pairs in live order; returns (positions, relation)."""
    positions = []
    for event_id, same in captured:
        need(event_id is not None, "live ledger prefix differs and frozen row has no stable event UUID")
        try:
            uuid.UUID(event_id)
        except (ValueError, AttributeError) as error:
            raise ValueError("live ledger prefix differs and frozen event ID is not a UUID") from error
        need(event_id in by_id, "live ledger prefix differs: captured event is missing")
        position, current_line, _ = by_id[event_id]
        need(same(current_line), "live ledger prefix differs: captured event JSON line bytes changed")
        positions.append(position)
    need(all(left < right for left, right in zip(positions, positions[1:])),
         "live ledger prefix differs: captured events were reordered")
    end = positions[-1] + 1 if positions else 0
    captured_positions = set(positions)
    for index, (_, event) in enumerate(current[:end]):
        if index not in captured_positions:
            need(event is not None and isinstance(event.get("event_id"), str),
                 "live ledger prefix differs: inserted row lacks an independent event UUID")
            try:
                uuid.UUID(event["event_id"])
            except (ValueError, AttributeError) as error:
                raise ValueError("live ledger prefix differs: inserted event ID is not a UUID") from error
    return positions, {"relation": "captured_events_preserved_with_insertions", "captured_event_count": count,
                       "inserted_event_count": end - len(captured), "original_relative_order_preserved": True,
                       "warning": "Live history contains inserted events; the frozen history is an ordered subsequence, not the current continuous prefix. Original bytes and order are preserved. This validates historical evidence, not current ownership, authorization or scientific acceptance."}


def exact_relation(count):
    return {"relation": "exact_prefix", "captured_event_count": count,
            "inserted_event_count": 0, "original_relative_order_preserved": True, "warning": None}


def history_relationship(frozen, live, identity, manifest_hash):
    """v1: require unchanged prefix or byte-identical UUID ordered subsequence."""
    old = ledger_rows(frozen)
    current, by_id, anchors = live_index(live)
    old_ids = [event.get("event_id") for _, event in old if event and event.get("event_id")]
    need(len(set(old_ids)) == len(old_ids), "frozen ledger prefix contains duplicate event UUIDs")
    live_anchor(anchors, identity, manifest_hash)
    count = sum(event is not None for _, event in old)
    if live[:len(frozen)] == frozen:
        return exact_relation(count)
    captured = []
    for line, event in old:
        event_id = event.get("event_id") if event else None
        captured.append((event_id if isinstance(event_id, str) else None, lambda current, line=line: current == line))
    return subsequence_relation(captured, current, by_id, count)[1]


def check_v2_prefix(prefix):
    keys = {"storage", "bytes", "sha256", "line_count", "event_count", "last_event_id", "last_sequence", "line_digest",
            "base", "chain_depth", "digests_path", "digests_bytes", "digests_sha256", "digests_count"}
    need(isinstance(prefix, dict) and set(prefix) == keys and prefix["storage"] == "line_digests"
         and prefix["line_digest"] == LINE_DIGEST and prefix["digests_path"] == DIGESTS_NAME, "invalid v2 ledger prefix record")
    for name in ("bytes", "line_count", "event_count", "chain_depth", "digests_bytes", "digests_count"):
        need(type(prefix[name]) is int and prefix[name] >= 0, f"invalid v2 ledger prefix {name}")
    need(SHA.fullmatch(str(prefix["sha256"])) and SHA.fullmatch(str(prefix["digests_sha256"])), "invalid v2 ledger prefix hash")
    base = prefix["base"]
    if base is None:
        need(prefix["digests_count"] == prefix["line_count"] and prefix["chain_depth"] == 0, "v2 ledger line list does not cover the prefix")
        return
    need(isinstance(base, dict) and set(base) == {"highlight_id", "line_count", "bytes", "sha256", "chain_depth"}
         and HIGHLIGHT_ID.fullmatch(str(base["highlight_id"])) and SHA.fullmatch(str(base["sha256"]))
         and all(type(base[name]) is int and base[name] >= 0 for name in ("line_count", "bytes", "chain_depth")),
         "invalid v2 highlight base record")
    need(base["line_count"] + prefix["digests_count"] == prefix["line_count"] and base["bytes"] <= prefix["bytes"]
         and prefix["chain_depth"] == base["chain_depth"] + 1 < MAX_CHAIN, "v2 highlight base does not fit this prefix")


def anchored_manifest(hub, identity, events):
    """(parsed manifest, its sha256) after checking the checksum file and the unique ledger anchor."""
    directory = highlight_directory(hub, identity)
    encoded = regular_bytes(safe_path(directory, "MANIFEST.json"), MANIFEST_LIMIT)
    expected = regular_bytes(safe_path(directory, "MANIFEST.sha256"), 65).decode().strip()
    need(SHA.fullmatch(expected) and digest(encoded) == expected, "highlight manifest hash mismatch")
    anchors = [e for e in events if e.get("kind") == "highlight_created" and e.get("id") == identity]
    need(len(anchors) == 1 and anchors[0].get("manifest_sha256") == expected, "highlight lacks a unique matching ledger anchor")
    manifest = parse_json(encoded)
    need(isinstance(manifest, dict) and manifest.get("schema_version") in (1, 2)
         and manifest.get("highlight_id") == identity, "invalid highlight manifest identity")
    return manifest, expected


_DIGESTS = {}


def read_digests(hub, identity, prefix):
    """[(short digest, event UUID or None)] stored by one v2 highlight (its delta only)."""
    key = (str(hub), identity, prefix["digests_sha256"])
    if key not in _DIGESTS:
        data = regular_bytes(safe_path(highlight_directory(hub, identity), DIGESTS_NAME), DIGESTS_LIMIT)
        need(len(data) == prefix["digests_bytes"] and digest(data) == prefix["digests_sha256"], "ledger line list hash mismatch")
        rows = []
        for line in data.splitlines(keepends=True):
            match = DIGEST_ROW.fullmatch(line)
            need(match is not None, "invalid ledger line list row")
            event_id = match[2].decode()
            rows.append((match[1].decode(), None if event_id == "-" else event_id))
        need(len(rows) == prefix["digests_count"], "ledger line list count mismatch")
        _DIGESTS[key] = rows
    return _DIGESTS[key]


def captured_lines(hub, manifest, events):
    """(short digest, event UUID or None) for every captured line, oldest first, following base highlights."""
    parts = [read_digests(hub, manifest["highlight_id"], manifest["ledger_prefix"])]
    current, seen = manifest, {manifest["highlight_id"]}
    while current["ledger_prefix"]["base"] is not None:
        base = current["ledger_prefix"]["base"]
        need(base["highlight_id"] not in seen and len(seen) <= MAX_CHAIN, "highlight base chain is too long or loops")
        seen.add(base["highlight_id"])
        try:
            current, _ = anchored_manifest(hub, base["highlight_id"], events)
            need(current["schema_version"] == 2, "it is not a v2 highlight")
            check_v2_prefix(current["ledger_prefix"])
            need(all(current["ledger_prefix"][key] == base[key] for key in ("line_count", "bytes", "sha256", "chain_depth")),
                 "its manifest does not match the base record")
            parts.append(read_digests(hub, base["highlight_id"], current["ledger_prefix"]))
        except (ValueError, OSError) as error:
            raise ValueError(f"base highlight {base['highlight_id']} does not verify ({error}); this v2 highlight lists "
                             "only the ledger lines added after its base, so restore the base from Git first") from error
    rows = [row for part in reversed(parts) for row in part]
    need(len(rows) == manifest["ledger_prefix"]["line_count"], "captured line list does not match the recorded line count")
    return rows


def rebuild_prefix(hub, manifest, live, identity, manifest_hash, events):
    """v2: (history relation, captured prefix bytes rebuilt from the live ledger)."""
    prefix = manifest["ledger_prefix"]
    current, by_id, anchors = live_index(live)
    captured = captured_lines(hub, manifest, events)
    ids = [event_id for _, event_id in captured if event_id]
    need(len(set(ids)) == len(ids), "frozen ledger prefix contains duplicate event UUIDs")
    live_anchor(anchors, identity, manifest_hash)
    size, expected = prefix["bytes"], prefix["sha256"]
    if len(live) >= size and digest(live[:size]) == expected:
        return exact_relation(prefix["event_count"]), live[:size]
    pairs = [(event_id, lambda line, short=short: short_digest(line) == short) for short, event_id in captured]
    positions, relation = subsequence_relation(pairs, current, by_id, prefix["event_count"])
    rebuilt = b"".join(current[position][0] for position in positions)
    need(len(rebuilt) == size and digest(rebuilt) == expected,
         "live ledger prefix differs: captured event JSON line bytes changed")
    return relation, rebuilt


def ledger_storage(manifest):
    return "line_digests" if manifest.get("schema_version") == 2 else "copy"


def verify(hub, identity, events, live=None):
    directory = highlight_directory(hub, identity)
    manifest, expected = anchored_manifest(hub, identity, events)
    version = manifest["schema_version"]
    instant(manifest["captured_at"])
    validate_spec(manifest["spec"])
    files = manifest["files"]
    need(isinstance(files, list) and 0 < len(files) <= MAX_FILES, "invalid frozen file manifest")
    seen, wanted, total = set(), {"MANIFEST.json", "MANIFEST.sha256", PREFIX_NAME if version == 1 else DIGESTS_NAME}, 0
    for item in files:
        need(isinstance(item, dict) and set(item) == {"source_path", "frozen_path", "bytes", "sha256"}, "invalid frozen file entry")
        selected_name(item["source_path"])
        need(item["source_path"].casefold() not in seen, "duplicate frozen path")
        seen.add(item["source_path"].casefold())
        need(item["frozen_path"] == "files/" + item["source_path"], "invalid frozen path mapping")
        data = regular_bytes(safe_path(directory, item["frozen_path"]), FILE_LIMIT)
        need(type(item["bytes"]) is int and len(data) == item["bytes"] and digest(data) == item["sha256"], "frozen file hash/size mismatch")
        # WHY: the bytes match the anchored manifest, so they are the bytes scanned at capture; no rescan.
        wanted.add(item["frozen_path"])
        total += len(data)
    need(total <= TOTAL_LIMIT and seen == {name.casefold() for name in manifest["spec"]["files"]}, "frozen file coverage mismatch")
    prefix = manifest["ledger_prefix"]
    # Read a stable live snapshot, checking exact event bytes rather than
    # reserializing JSON. Git reconciliation may insert independent new events.
    live = read_live(hub) if live is None else live
    if version == 1:
        need(prefix["path"] == PREFIX_NAME, "invalid ledger prefix location")
        ledger = regular_bytes(safe_path(directory, prefix["path"]), LEDGER_LIMIT)
        need(len(ledger) == prefix["bytes"] and digest(ledger) == prefix["sha256"], "frozen ledger prefix hash mismatch")
        # The frozen prefix matches its anchored hash too, so it is not rescanned for secrets.
        relation = history_relationship(ledger, live, identity, expected)
    else:
        check_v2_prefix(prefix)
        relation, _ = rebuild_prefix(hub, manifest, live, identity, expected, events)
    actual = set()
    for path in directory.rglob("*"):
        need(not path.is_symlink(), "snapshot tree contains symlink")
        if path.is_file():
            actual.add(path.relative_to(directory).as_posix())
        else:
            need(path.is_dir(), "snapshot tree contains special file")
    need(actual == wanted, "snapshot contains missing or unexpected files")
    # Computed verification metadata is never written into the frozen manifest.
    manifest["history_verification"] = relation
    return manifest


def present(manifest):
    spec = manifest["spec"]
    return {"highlight_id": manifest["highlight_id"], "kind": spec["kind"], "title": spec["title"],
            "occurred_at": spec["occurred_at"], "captured_at": manifest["captured_at"],
            "summary": spec["summary"], "evidence_status": spec["evidence_status"],
            "event_ids": spec["event_ids"], "experiment_ids": spec["experiment_ids"], "supersedes": spec["supersedes"],
            "history_verification": manifest["history_verification"],
            "file_count": len(manifest["files"]), "manifest": "highlights/" + manifest["highlight_id"] + "/MANIFEST.json"}


def inventory(hub, events):
    parent = highlight_directory(hub)
    result = []
    names = sorted(path.name for path in parent.iterdir() if not path.name.startswith(".capture-")) if parent.exists() else []
    live = read_live(hub) if names else None
    for name in names:
        result.append(verify(hub, name, events, live=live))
    return result


def superseding(manifest, all_manifests):
    identity = manifest["highlight_id"]
    newer = [m for m in all_manifests if identity in m["spec"]["supersedes"]]
    newer.sort(key=lambda m: instant(m["captured_at"]))
    return {"superseded_by": [{"highlight_id": m["highlight_id"], "title": m["spec"]["title"],
                              "evidence_status": m["spec"]["evidence_status"],
                              "occurred_at": m["spec"]["occurred_at"], "captured_at": m["captured_at"]}
                             for m in newer[-32:]],
            "superseding_count": len(newer),
            "correction_scope": "Later correction links are shown even when outside the requested time/text filter."}


def list_view(hub, events, args):
    selected = []
    all_manifests = inventory(hub, events)
    for manifest in all_manifests:
        row = {**present(manifest), **superseding(manifest, all_manifests)}
        point = instant(row[args.time_field])
        if args.since and point < args.since:
            continue
        if args.until and point > args.until:
            continue
        if args.query and args.query.casefold() not in json.dumps(manifest["spec"], ensure_ascii=False).casefold():
            continue
        selected.append(row)
    selected.sort(key=lambda row: (instant(row[args.time_field]), row["highlight_id"]), reverse=True)
    view = {"rows": selected[:args.n], "matching_count": len(selected), "truncated": len(selected) > args.n,
            "time_field": args.time_field, "limits": "Historical highlights, not current execution or scientific acceptance. Follow superseding records."}
    while len(json_bytes(view)) > VIEW_LIMIT and view["rows"]:
        view["rows"].pop()
        view["truncated"] = True
    return view


def show_view(manifest, all_manifests):
    summary = {**present(manifest), **superseding(manifest, all_manifests)}
    view = {**summary, "significance": manifest["spec"]["significance"], "caveats": manifest["spec"]["caveats"],
            "remote_artifacts": manifest["spec"]["remote_artifacts"], "files": manifest["files"],
            "source_repository": manifest["source_repository"], "ledger_storage": ledger_storage(manifest),
            "ledger_prefix": manifest["ledger_prefix"], "limits": manifest["limits"]}
    if len(json_bytes(view)) > VIEW_LIMIT:
        fallback = {**summary, "detail_omitted": "Read the named verified manifest for full metadata; frozen bytes were not dropped."}
        for key in ("event_ids", "experiment_ids", "supersedes"):
            fallback[key + "_count"] = len(fallback.pop(key))
        while len(json_bytes(fallback)) > VIEW_LIMIT and fallback["superseded_by"]:
            fallback["superseded_by"].pop(0)
        return fallback
    return view


def stored_schema(hub, identity):
    try:
        return parse_json(regular_bytes(safe_path(highlight_directory(hub, identity), "MANIFEST.json"),
                                        MANIFEST_LIMIT)).get("schema_version")
    except (ValueError, OSError, AttributeError):
        return None


def restore(hub, identity, destination, events):
    try:
        manifest = verify(hub, identity, events)
    except ValueError as error:
        if str(error).startswith("live ledger prefix differs") and stored_schema(hub, identity) == 2:
            raise ValueError(f"cannot restore {identity}: {error}. This highlight stores line digests, not a copy of the "
                             "ledger, so restore rebuilds the captured prefix from the live ledger, which no longer "
                             "contains those events unchanged. Restore from a Git commit or backup that still has them; "
                             "use `highlight create --freeze-ledger` for snapshots that must survive a ledger rewrite") from error
        raise
    supplied = Path(destination).expanduser().absolute()
    for path in (supplied, *supplied.parents):
        need(not path.is_symlink(), "restore destination cannot contain symlinks")
    destination = supplied.resolve()
    # A restored tree is a new review artifact, never a working checkout rollback.
    repo, _ = git_state(hub)
    need(not destination.is_relative_to(repo) and not repo.is_relative_to(destination), "restore destination must be outside the live repository")
    need(not destination.is_relative_to(hub) and not hub.is_relative_to(destination), "restore destination must be outside the hub")
    need(not any(part.lower() in CONTROL_PARTS or part.lower() == ".research-memory" for part in destination.parts), "restore destination cannot be a control directory")
    need(destination.parent.is_dir(), "restore destination parent must already exist")
    need(not destination.exists() or (destination.is_dir() and not any(destination.iterdir())), "restore requires a new or empty directory")
    directory = highlight_directory(hub, identity)
    staging = Path(tempfile.mkdtemp(prefix=".rmem-restore-", dir=destination.parent))
    try:
        for item in manifest["files"]:
            data = regular_bytes(safe_path(directory, item["frozen_path"]), FILE_LIMIT)
            need(len(data) == item["bytes"] and digest(data) == item["sha256"], "frozen bytes changed after verification")
            target = safe_path(staging, "frozen_files/" + item["source_path"])
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            target.chmod(0o444)
        anchor_hash = next(e["manifest_sha256"] for e in events if e.get("kind") == "highlight_created" and e.get("id") == identity)
        prefix = manifest["ledger_prefix"]
        version = manifest["schema_version"]
        for name in ("MANIFEST.json", "MANIFEST.sha256", PREFIX_NAME if version == 1 else DIGESTS_NAME):
            limit = LEDGER_LIMIT if name == PREFIX_NAME else DIGESTS_LIMIT if name == DIGESTS_NAME else MANIFEST_LIMIT
            data = regular_bytes(safe_path(directory, name), limit)
            if name == "MANIFEST.json":
                need(digest(data) == anchor_hash, "manifest changed after verification")
            elif name == "MANIFEST.sha256":
                need(data.decode().strip() == anchor_hash, "manifest checksum changed after verification")
            elif name == DIGESTS_NAME:
                need(digest(data) == prefix["digests_sha256"], "ledger line list changed after verification")
            else:
                need(len(data) == prefix["bytes"] and digest(data) == prefix["sha256"], "ledger snapshot changed after verification")
            (staging / name).write_bytes(data)
            (staging / name).chmod(0o444)
        note = ""
        if version == 2:
            # The captured prefix is rebuilt again here; the hash check proves it equals the bytes captured.
            _, rebuilt = rebuild_prefix(hub, manifest, read_live(hub), identity, anchor_hash, events)
            (staging / PREFIX_NAME).write_bytes(rebuilt)
            (staging / PREFIX_NAME).chmod(0o444)
            note = (f"{PREFIX_NAME} was rebuilt from the live ledger and matches the captured size and SHA-256 "
                    "recorded in MANIFEST.json.\n")
        (staging / "RESTORE_README.txt").write_text("Historical recovery artifact only. frozen_files/ preserves the selected paths and bytes.\n" + note + "No live file, controller, claim, scheduler, or automation has been rolled back or enabled.\n")
        # rename refuses an existing nonempty directory; never replace file content.
        os.rename(staging, destination)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return {"highlight_id": identity, "destination": str(destination), "restored_files": len(manifest["files"]),
            "history_verification": manifest["history_verification"],
            "execution_authorized": False, "live_state_changed": False}


def table_cell(value, limit=160):
    text = " ".join(str(value).split()).replace("|", "\\|")
    return text if len(text) <= limit else text[:limit - 1] + "…"


def index_text(hub, events):
    """HIGHLIGHTS_INDEX.md: verified highlights, newest first by occurred_at, with superseding links."""
    manifests = inventory(hub, events)
    manifests.sort(key=lambda m: (instant(m["spec"]["occurred_at"]), m["highlight_id"]), reverse=True)
    link = lambda identity: f"[{identity}](highlights/{identity}/MANIFEST.json)"
    lines = ["# Highlights index", "",
             "<!-- Generated by `rmem highlight index --write` from highlights/*/MANIFEST.json; regenerate it instead of editing. -->",
             "", f"{len(manifests)} verified highlight(s), newest first by the time the event occurred. When a row is "
             "superseded, read the later highlight too. Check one with `rmem highlight verify ID`.", "",
             "| Occurred at | Kind | Title | Evidence | Highlight | Superseded by |", "|---|---|---|---|---|---|"]
    for manifest in manifests[:INDEX_ROWS]:
        spec, later = manifest["spec"], superseding(manifest, manifests)
        newer = ", ".join(f"{link(row['highlight_id'])} ({table_cell(row['evidence_status'], 20)})"
                          for row in later["superseded_by"][-3:])
        if later["superseding_count"] > 3:
            newer += f" and {later['superseding_count'] - 3} more"
        lines.append(f"| {table_cell(spec['occurred_at'], 40)} | {spec['kind']} | {table_cell(spec['title'], 120)} | "
                     f"{spec['evidence_status']} | {link(manifest['highlight_id'])} | {newer or 'none'} |")
    if len(manifests) > INDEX_ROWS:
        lines += ["", f"{len(manifests) - INDEX_ROWS} older highlight(s) are not listed; use "
                  "`rmem highlight list --until TIME` to find them."]
    return "\n".join(lines) + "\n"


def ignored_snapshot_files(hub, identities):
    """{highlight ID: hub-relative snapshot files that Git ignores}; empty outside a Git work tree."""
    owners = {}
    for identity in identities:
        directory = highlight_directory(hub, identity)
        for path in sorted(directory.rglob("*")):
            if path.is_file() and not path.is_symlink():
                owners[path.relative_to(hub).as_posix()] = identity
    if not owners or shutil.which("git") is None:
        return {}
    environment = {key: value for key, value in os.environ.items() if key not in GIT_LOCATION}
    result = subprocess.run(["git", "-C", str(hub), "check-ignore", "-z", "--stdin"], capture_output=True,
                            input=b"".join(os.fsencode(name) + b"\0" for name in owners), env=environment, timeout=120)
    if result.returncode != 0:
        return {}  # 1: nothing ignored; 128: not a Git work tree
    found = {}
    for name in os.fsdecode(result.stdout).split("\0"):
        if name in owners:
            found.setdefault(owners[name], []).append(name)
    return found


def ignored_warning(identity, paths):
    return (f"highlight {identity}: {len(paths)} snapshot file(s) are ignored by Git and will not be committed, "
            f"for example {paths[0]}. An unanchored rule such as `runs/` in .gitignore also matches inside "
            "highlights/; anchor it (`/runs/`) or add the files with `git add -f`")
