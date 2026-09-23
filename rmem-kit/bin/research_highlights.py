"""Small immutable memory snapshots. No network, model, or controller operations."""
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
LEDGER_LIMIT = 16 * FILE_LIMIT
SPEC_LIMIT = 48000
MANIFEST_LIMIT = 128000
VIEW_LIMIT = 12000
MAX_FILES = 32
HIGHLIGHT_ID = re.compile(r"hl-[0-9a-f]{32}\Z")
SHA = re.compile(r"[0-9a-f]{64}\Z")
KINDS = {"milestone", "version", "finding", "correction"}
STATUSES = {"observed", "reported", "inferred", "inconclusive", "retracted"}
CONTROL_PARTS = {".git", ".codex", ".claude", ".ssh", ".aws", ".gnupg", "claims"}
SECRET_NAME = re.compile(r"(^\.env($|\.)|secret|credential|(^|[_.-])(token|private[-_]?key)([_.-]|$)|^(auth|id_rsa|id_ed25519)(\.|$)|\.(pem|key|p12|pfx)$)", re.I)
SECRET_TEXT = re.compile(
    r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----|"
    r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b|\bgh[pousr]_[A-Za-z0-9]{20,}\b|"
    r"\bsk-(?:proj-|ant-)?[A-Za-z0-9_-]{20,}\b|"
    r"(?i:(?:api[_-]?key|access[_-]?token|client[_-]?secret|password)\s*[\"']?\s*[:=]\s*[\"'][^\"'\s]{6,}[\"'])"
)


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


def freeze(hub, spec_file, who, events):
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
    ledger = regular_bytes(hub / "ledger.jsonl", LEDGER_LIMIT)
    safe_text(ledger)
    identity = "hl-" + uuid.uuid4().hex
    parent = highlight_directory(hub)
    parent.mkdir(exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".capture-", dir=parent))
    target = parent / identity
    try:
        manifest = {"schema_version": 1, "highlight_id": identity, "captured_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                    "captured_by": {key: who[key] for key in ("agent", "session")}, "spec": spec,
                    "source_repository": {"path_at_capture": str(repo), **git},
                    "ledger_prefix": {"path": "ledger-prefix.jsonl", "bytes": len(ledger), "sha256": digest(ledger),
                                      "event_count": len(events), "last_event_id": events[-1].get("event_id") if events else None,
                                      "last_sequence": events[-1].get("sequence") if events else None},
                    "files": [],
                    "limits": "Historical bytes and stated evidence status only; no scientific acceptance, remote backup, live authority or execution authorization."}
        for name, data in sources:
            frozen = "files/" + name
            destination = safe_path(staging, frozen)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(data)
            manifest["files"].append({"source_path": name, "frozen_path": frozen, "bytes": len(data), "sha256": digest(data)})
        (staging / "ledger-prefix.jsonl").write_bytes(ledger)
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


def history_relationship(frozen, live, identity, manifest_hash):
    """Require unchanged prefix or byte-identical UUID ordered subsequence."""
    def rows(data):
        result = []
        for line in data.splitlines(keepends=True):
            if line.strip():
                event = parse_json(line)
                need(isinstance(event, dict), "live ledger prefix verification requires JSON event objects")
                result.append((line, event))
            else:
                result.append((line, None))
        return result

    old, current = rows(frozen), rows(live)
    by_id = {}
    for index, (line, event) in enumerate(current):
        event_id = event.get("event_id") if event else None
        if event_id:
            need(isinstance(event_id, str) and event_id not in by_id, "live ledger prefix verification rejects duplicate event UUIDs")
            by_id[event_id] = (index, line, event)
    old_ids = [event.get("event_id") for _, event in old if event and event.get("event_id")]
    need(len(set(old_ids)) == len(old_ids), "frozen ledger prefix contains duplicate event UUIDs")
    anchors = [event for _, event in current if event and event.get("kind") == "highlight_created" and event.get("id") == identity]
    need(len(anchors) == 1 and anchors[0].get("manifest_sha256") == manifest_hash,
         "live ledger lacks a unique matching highlight anchor")
    count = sum(event is not None for _, event in old)
    if live[:len(frozen)] == frozen:
        return {"relation": "exact_prefix", "captured_event_count": count,
                "inserted_event_count": 0, "original_relative_order_preserved": True, "warning": None}
    positions = []
    for line, event in old:
        need(event is not None and isinstance(event.get("event_id"), str),
             "live ledger prefix differs and frozen row has no stable event UUID")
        event_id = event["event_id"]
        try:
            uuid.UUID(event_id)
        except (ValueError, AttributeError) as error:
            raise ValueError("live ledger prefix differs and frozen event ID is not a UUID") from error
        need(event_id in by_id, "live ledger prefix differs: captured event is missing")
        position, current_line, _ = by_id[event_id]
        need(current_line == line, "live ledger prefix differs: captured event JSON line bytes changed")
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
    return {"relation": "captured_events_preserved_with_insertions", "captured_event_count": count,
            "inserted_event_count": end - len(old), "original_relative_order_preserved": True,
            "warning": "Live history contains inserted events; the frozen history is an ordered subsequence, not the current continuous prefix. Original bytes and order are preserved. This validates historical evidence, not current ownership, authorization or scientific acceptance."}


def verify(hub, identity, events):
    directory = highlight_directory(hub, identity)
    encoded = regular_bytes(safe_path(directory, "MANIFEST.json"), MANIFEST_LIMIT)
    expected = regular_bytes(safe_path(directory, "MANIFEST.sha256"), 65).decode().strip()
    need(SHA.fullmatch(expected) and digest(encoded) == expected, "highlight manifest hash mismatch")
    anchors = [e for e in events if e.get("kind") == "highlight_created" and e.get("id") == identity]
    need(len(anchors) == 1 and anchors[0].get("manifest_sha256") == expected, "highlight lacks a unique matching ledger anchor")
    manifest = parse_json(encoded)
    need(manifest.get("schema_version") == 1 and manifest.get("highlight_id") == identity, "invalid highlight manifest identity")
    instant(manifest["captured_at"])
    validate_spec(manifest["spec"])
    files = manifest["files"]
    need(isinstance(files, list) and 0 < len(files) <= MAX_FILES, "invalid frozen file manifest")
    seen, wanted, total = set(), {"MANIFEST.json", "MANIFEST.sha256", "ledger-prefix.jsonl"}, 0
    for item in files:
        need(isinstance(item, dict) and set(item) == {"source_path", "frozen_path", "bytes", "sha256"}, "invalid frozen file entry")
        selected_name(item["source_path"])
        need(item["source_path"].casefold() not in seen, "duplicate frozen path")
        seen.add(item["source_path"].casefold())
        need(item["frozen_path"] == "files/" + item["source_path"], "invalid frozen path mapping")
        data = regular_bytes(safe_path(directory, item["frozen_path"]), FILE_LIMIT)
        need(type(item["bytes"]) is int and len(data) == item["bytes"] and digest(data) == item["sha256"], "frozen file hash/size mismatch")
        safe_text(data)
        wanted.add(item["frozen_path"])
        total += len(data)
    need(total <= TOTAL_LIMIT and seen == {name.casefold() for name in manifest["spec"]["files"]}, "frozen file coverage mismatch")
    prefix = manifest["ledger_prefix"]
    need(prefix["path"] == "ledger-prefix.jsonl", "invalid ledger prefix location")
    ledger = regular_bytes(safe_path(directory, prefix["path"]), LEDGER_LIMIT)
    need(len(ledger) == prefix["bytes"] and digest(ledger) == prefix["sha256"], "frozen ledger prefix hash mismatch")
    safe_text(ledger)
    # Read a stable live snapshot, checking exact event bytes rather than
    # reserializing JSON. Git reconciliation may insert independent new events.
    live_path = safe_path(hub, "ledger.jsonl")
    live = regular_bytes(live_path, live_path.stat().st_size)
    relation = history_relationship(ledger, live, identity, expected)
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
    if parent.exists():
        for path in parent.iterdir():
            if not path.name.startswith(".capture-"):
                result.append(verify(hub, path.name, events))
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
            "source_repository": manifest["source_repository"], "ledger_prefix": manifest["ledger_prefix"], "limits": manifest["limits"]}
    if len(json_bytes(view)) > VIEW_LIMIT:
        fallback = {**summary, "detail_omitted": "Read the named verified manifest for full metadata; frozen bytes were not dropped."}
        for key in ("event_ids", "experiment_ids", "supersedes"):
            fallback[key + "_count"] = len(fallback.pop(key))
        while len(json_bytes(fallback)) > VIEW_LIMIT and fallback["superseded_by"]:
            fallback["superseded_by"].pop(0)
        return fallback
    return view


def restore(hub, identity, destination, events):
    manifest = verify(hub, identity, events)
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
        for name in ("MANIFEST.json", "MANIFEST.sha256", "ledger-prefix.jsonl"):
            data = regular_bytes(safe_path(directory, name), LEDGER_LIMIT if name.endswith("jsonl") else MANIFEST_LIMIT)
            if name == "MANIFEST.json":
                need(digest(data) == anchor_hash, "manifest changed after verification")
            elif name == "MANIFEST.sha256":
                need(data.decode().strip() == anchor_hash, "manifest checksum changed after verification")
            else:
                need(len(data) == manifest["ledger_prefix"]["bytes"] and digest(data) == manifest["ledger_prefix"]["sha256"], "ledger snapshot changed after verification")
            (staging / name).write_bytes(data)
            (staging / name).chmod(0o444)
        (staging / "RESTORE_README.txt").write_text("Historical recovery artifact only. frozen_files/ preserves the selected paths and bytes.\nNo live file, controller, claim, scheduler, or automation has been rolled back or enabled.\n")
        # rename refuses an existing nonempty directory; never replace file content.
        os.rename(staging, destination)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return {"highlight_id": identity, "destination": str(destination), "restored_files": len(manifest["files"]),
            "history_verification": manifest["history_verification"],
            "execution_authorized": False, "live_state_changed": False}
