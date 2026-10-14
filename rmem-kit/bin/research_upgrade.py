"""Upgrade an installed hub's kit files from a kit directory; a dry run unless --apply.

Every file the kit ships is compared with what the hub had at install time: the
hash in .kit-manifest.json or, for a hub installed before 0.4.0, any hash that a
released kit version shipped (KIT_HISTORY.json). A file still as installed is
updated when the kit changed it; a new kit file is added; a locally modified file
is a conflict and is never overwritten. Project data is never touched. See UPGRADE.md.
"""
import ast
import hashlib
import json
import os
from pathlib import Path
import shlex
import stat
from types import SimpleNamespace
import uuid

import rmem_config

EVENT_KINDS = {"kit_upgraded"}
EVENT_STRING_FIELDS = {"from_version", "from_basis", "to_version", "kit_source"}
MANIFEST_NAME = ".kit-manifest.json"
HISTORY_NAME = "KIT_HISTORY.json"
TEMPLATE_NAME = "HUB_CONFIG_TEMPLATE.json"
# Project data: an upgrade never writes these, even when a kit ships a file there.
PROJECT_FILES = {"STATE.md", "MAP.md", "ledger.jsonl", rmem_config.CONFIG_NAME, MANIFEST_NAME}
PROJECT_DIRS = {"threads", "findings", "journal", "claims", "history", "highlights", "notes", "lessons",
                "runs", "records"}
ACTIONS = ("update", "add", "merge", "conflict", "same", "project")
TITLES = {"update": "update (unchanged since install; the kit changed it)", "add": "add (new kit files)",
          "merge": "merge (line merge; local lines stay)", "conflict": "conflict (locally modified; never overwritten)"}
LIST_MAX = 40


def project_path(name: str) -> bool:
    return name in PROJECT_FILES or name.split("/", 1)[0] in PROJECT_DIRS


def code_path(name: str) -> bool:
    return name == "bin/rmem" or (name.startswith("bin/") and name.endswith(".py") and name.count("/") == 1)


def validate_event(event):
    if event.get("kind") != "kit_upgraded":
        return ""
    if not isinstance(event.get("to_version"), str) or not event["to_version"]:
        return "kit_upgraded requires to_version"
    counts = event.get("counts")
    if not isinstance(counts, dict) or any(type(value) is not int or value < 0 for value in counts.values()):
        return "kit_upgraded counts must map actions to nonnegative integers"
    return ""


def register(commands, core):
    p = commands.add_parser("upgrade", help="update this hub's kit files from a kit directory (dry run unless --apply)")
    p.add_argument("--from", dest="kit", required=True, metavar="KIT_DIR",
                   help="the kit to install, for example /path/to/Zhong-Shu/rmem-kit")
    p.add_argument("--apply", action="store_true",
                   help="copy updated and new kit files, rewrite .kit-manifest.json and record kit_upgraded")
    p.set_defaults(fn=lambda a: cmd_upgrade(a, core))


def read_bytes(core, path: Path) -> bytes:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise core.RmemError(f"expected a regular file: {path}")
        return stream.read()


def replace_bytes(core, path: Path, data: bytes, mode: int) -> None:
    """Atomically replace (or create) path with exactly these bytes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        if path.is_symlink():
            raise core.RmemError(f"refusing symlink: {path}")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def kit_directory(core, value: str, hub: Path) -> Path:
    kit = Path(value).expanduser().resolve()
    if not kit.is_dir():
        raise core.RmemError(f"--from must be a kit directory; {kit} is not a directory")
    if kit == hub:
        raise core.RmemError("--from names this hub; pass the kit directory, for example /path/to/Zhong-Shu/rmem-kit")
    if (kit / MANIFEST_NAME).exists():
        raise core.RmemError(f"{kit} has {MANIFEST_NAME}, so it is an installed hub; pass the kit directory (rmem-kit)")
    for name in ("VERSION", "bin/rmem"):
        if not core.safe_path(kit, name).is_file():
            raise core.RmemError(f"{kit} is not an rmem kit of version 0.4.0 or newer (it has no {name})")
    return kit


def kit_version(core, kit: Path) -> str:
    return core.one_line(read_bytes(core, core.safe_path(kit, "VERSION")).decode("utf-8").strip(), "kit VERSION", 64)


def shipped_files(core, kit: Path) -> list:
    """Hub paths of every file the kit's init copies, read from its bin/rmem without running it."""
    names = {}
    try:
        tree = ast.parse(read_bytes(core, core.safe_path(kit, "bin", "rmem")).decode("utf-8"))
        for node in tree.body:
            if (isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)
                    and node.targets[0].id in ("KIT_FILES", "KIT_HELPERS")):
                names[node.targets[0].id] = ast.literal_eval(node.value)
    except (SyntaxError, ValueError, UnicodeError, OSError) as exc:
        raise core.RmemError(f"cannot read the kit file list from {kit}/bin/rmem: {core.clipped(exc, 200)}") from exc
    if set(names) != {"KIT_FILES", "KIT_HELPERS"} or not all(
            isinstance(group, tuple) and all(isinstance(item, str) for item in group) for group in names.values()):
        raise core.RmemError(f"{kit}/bin/rmem does not list KIT_FILES and KIT_HELPERS as tuples of names; "
                             "upgrade from kit 0.4.0 or newer")
    files = [name for name in names["KIT_FILES"] if core.safe_path(kit, name).is_file()]
    for helper in names["KIT_HELPERS"]:
        if not core.safe_path(kit, "bin", helper).is_file():
            raise core.RmemError(f"the kit is incomplete: bin/{helper} is missing from {kit}")
        files.append(f"bin/{helper}")
    return sorted(dict.fromkeys(files + ["bin/rmem"]))


def load_history(core, kit: Path) -> list:
    """[(version, {hub path: sha256})] in release order; [] when the kit has no KIT_HISTORY.json."""
    path = core.safe_path(kit, HISTORY_NAME)
    if not path.is_file():
        return []
    try:
        data = json.loads(read_bytes(core, path).decode("utf-8"))
        rows = [(row["version"], row["files"]) for row in data["versions"]]
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise core.RmemError(f"{kit}/{HISTORY_NAME} is unreadable ({core.clipped(exc, 200)}); "
                             "restore it from the kit repository") from exc
    if any(not isinstance(version, str) or not isinstance(files, dict) for version, files in rows):
        raise core.RmemError(f"{kit}/{HISTORY_NAME} must list versions with a files map; restore it from the kit repository")
    return rows


def read_manifest(core, hub: Path):
    """(manifest or None, problem): problem is '', 'missing' or a description."""
    path = core.safe_path(hub, MANIFEST_NAME)
    if not path.exists():
        return None, "missing"
    try:
        manifest = json.loads(read_bytes(core, path).decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        return None, f"unreadable ({core.clipped(exc, 200)})"
    if (not isinstance(manifest, dict) or not isinstance(manifest.get("files"), dict)
            or any(not isinstance(value, str) for value in manifest["files"].values())):
        return None, "invalid (expected an object with a files map of path to sha256)"
    return manifest, ""


def merge_ignore(current: str, kit_text: str):
    """Keep every local line, anchor kit rules found unanchored, and put missing kit rules first."""
    lines, missing, anchored = current.splitlines(), [], 0
    for rule in kit_text.splitlines():
        if not rule.strip() or rule.startswith("#") or rule in lines:
            continue
        if rule.startswith("/") and rule[1:] in lines:
            lines[lines.index(rule[1:])] = rule
            anchored += 1
        else:
            missing.append(rule)
    # WHY: later lines win in .gitignore, so kit rules go first and local negations keep their effect.
    merged = missing + lines
    return ("\n".join(merged) + "\n") if merged else "", anchored, len(missing)


def classify(core, hub: Path, kit: Path, name: str, recorded: dict, history: list):
    source, target = core.safe_path(kit, name), core.safe_path(hub, name)
    if target.is_symlink() or (target.exists() and not target.is_file()):
        raise core.RmemError(f"{name} in the hub is not a regular file; move it aside, then rerun")
    row = SimpleNamespace(name=name, source=source, target=target, kit_sha=core.file_sha256(source),
                          hub_sha=core.file_sha256(target) if target.exists() else None,
                          action="", detail="", data=None)
    known = [version for version, files in history if row.hub_sha and files.get(name) == row.hub_sha]
    if project_path(name):
        row.action, row.detail = "project", ("differs from the kit" if row.hub_sha not in (None, row.kit_sha) else "")
    elif row.hub_sha is None:
        row.action = "add"
    elif name == ".gitignore":
        text, anchored, added = merge_ignore(read_bytes(core, target).decode("utf-8", errors="replace"),
                                             read_bytes(core, source).decode("utf-8", errors="replace"))
        row.data = text.encode("utf-8")
        row.action = "same" if hashlib.sha256(row.data).hexdigest() == row.hub_sha else "merge"
        row.detail = f"anchors {anchored} kit rule(s), adds {added} missing kit rule(s) at the top"
    elif row.hub_sha == row.kit_sha:
        row.action = "same"
    elif name in recorded:
        row.action = "update" if recorded[name] == row.hub_sha else "conflict"
        row.detail = "as installed, per the manifest" if row.action == "update" else ""
    else:
        row.action = "update" if known else "conflict"
        row.detail = f"matches kit {', '.join(known)}" if known else ""
    return row, known


def make_plan(core, hub: Path, kit: Path):
    manifest, problem = read_manifest(core, hub)
    if problem not in ("", "missing"):
        raise core.RmemError(f"{MANIFEST_NAME} is {problem}; repair it, or move it aside so the hub is treated as "
                             "installed before 0.4.0, then rerun")
    recorded = manifest["files"] if manifest else {}
    history = load_history(core, kit)
    plan = SimpleNamespace(to_version=kit_version(core, kit), rows=[], history=bool(history), manifest=manifest)
    code_versions, any_versions = set(), set()
    for name in shipped_files(core, kit):
        row, known = classify(core, hub, kit, name, recorded, history)
        plan.rows.append(row)
        any_versions.update(known)
        if code_path(name):
            code_versions.update(known)
    if manifest:
        plan.from_version = core.clipped(manifest.get("kit_version", "unknown"), 64)
        plan.from_basis = f"recorded in {MANIFEST_NAME}"
    else:
        order = [version for version, _ in history]
        matched = [version for version in order if version in (code_versions or any_versions)]
        plan.from_version = matched[-1] if matched else "unknown"
        plan.from_basis = (f"no {MANIFEST_NAME}; inferred from {HISTORY_NAME}" if matched
                           else f"no {MANIFEST_NAME}; no file matches a known kit version")
    shipped = {row.name for row in plan.rows}
    plan.retired = sorted(name for name in recorded if name not in shipped and not project_path(name))
    plan.plugins_missing = not core.safe_path(hub, "bin", "plugins").is_dir()
    plan.config_missing = not core.safe_path(hub, rmem_config.CONFIG_NAME).exists()
    plan.blocked = [row.name for row in plan.rows if row.action == "conflict" and code_path(row.name)]
    files = {}
    for row in plan.rows:
        if row.action == "project":
            if row.name in recorded:
                files[row.name] = recorded[row.name]
        else:
            files[row.name] = hashlib.sha256(row.data).hexdigest() if row.data is not None else row.kit_sha
    plan.manifest_files = dict(sorted(files.items()))
    plan.changes = (any(row.action in ("update", "add", "merge") for row in plan.rows) or plan.plugins_missing
                    or plan.config_missing or not manifest or manifest.get("kit_version") != plan.to_version
                    or recorded != plan.manifest_files)
    return plan


def counts(plan) -> dict:
    return {action: sum(1 for row in plan.rows if row.action == action) for action in ACTIONS}


def apply_plan(core, hub: Path, kit: Path, who: dict, plan):
    if plan.plugins_missing:
        core.safe_path(hub, "bin", "plugins").mkdir(parents=True)
    for row in plan.rows:
        if row.action in ("update", "add"):
            data = read_bytes(core, row.source)
            if hashlib.sha256(data).hexdigest() != row.kit_sha:
                raise core.RmemError(f"kit file {row.name} changed during the upgrade; rerun when the kit is stable")
            mode = stat.S_IMODE((row.target if row.target.exists() else row.source).stat().st_mode)
            replace_bytes(core, row.target, data, mode)
        elif row.action == "merge":
            replace_bytes(core, row.target, row.data, stat.S_IMODE(row.target.stat().st_mode))
    if plan.config_missing:
        template = core.safe_path(kit, TEMPLATE_NAME)
        data = read_bytes(core, template) if template.is_file() else rmem_config.template_text().encode("utf-8")
        fd = os.open(core.safe_path(hub, rmem_config.CONFIG_NAME),
                     os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o644)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
    manifest = {"schema_version": 1, "kit_version": plan.to_version, "installed_at": core.now(),
                "upgraded_from": plan.from_version, "files": plan.manifest_files}
    if plan.manifest:
        manifest["first_installed_at"] = plan.manifest.get("first_installed_at") or plan.manifest.get("installed_at")
    target = core.safe_path(hub, MANIFEST_NAME)
    replace_bytes(core, target, (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8"),
                  stat.S_IMODE(target.stat().st_mode) if target.exists() else 0o644)
    tally = counts(plan)
    return core.append_locked(hub, {
        **who, "kind": "kit_upgraded", "from_version": plan.from_version, "from_basis": plan.from_basis,
        "to_version": plan.to_version, "kit_source": str(kit), "counts": tally,
        "conflicts": [row.name for row in plan.rows if row.action == "conflict"][:50],
        "note": f"kit {plan.from_version} to {plan.to_version}: {tally['update']} updated, {tally['add']} added, "
                f"{tally['merge']} merged, {tally['conflict']} conflicts kept"})


def show(core, hub: Path, kit: Path, plan, event=None) -> None:
    out = core.Output()
    out.add(f"# rmem upgrade ({'applied' if event else 'dry run; nothing was changed'})")
    out.add(f"hub: {hub}")
    out.add(f"from: kit {plan.from_version} ({plan.from_basis})")
    out.add(f"to: kit {plan.to_version} from {kit}")
    if not plan.manifest and not plan.history:
        out.add(f"note: the kit lists no released versions ({HISTORY_NAME} is missing or empty), so every "
                "file that differs from the kit is a conflict")
    for action in ("update", "add", "merge", "conflict"):
        rows = [row for row in plan.rows if row.action == action]
        if not rows:
            continue
        out.add(f"{TITLES[action]}: {len(rows)}")
        for row in rows[:LIST_MAX]:
            if action == "conflict":
                out.add(f"- {row.name}: inspect with `diff -u {shlex.quote(str(row.target))} {shlex.quote(str(row.source))}`")
            else:
                out.add(f"- {row.name}" + (f" ({row.detail})" if row.detail else ""))
        if len(rows) > LIST_MAX:
            out.add(f"[{len(rows) - LIST_MAX} more not shown]")
    tally = counts(plan)
    out.add(f"already identical to the kit: {tally['same']}")
    left = [row.name for row in plan.rows if row.action == "project" and row.detail]
    if left:
        out.add(f"project data left alone (differs from the kit): {core.clipped(', '.join(left), 600)}")
    if plan.retired:
        out.add(f"no longer shipped by the kit, left in place: {core.clipped(', '.join(plan.retired), 600)}")
    if plan.plugins_missing:
        out.add("create bin/plugins/ for project plugins")
    if plan.config_missing:
        out.add(f"write {rmem_config.CONFIG_NAME} from the kit template (the hub has none)")
    if plan.changes:
        out.add(f"write {MANIFEST_NAME} for kit {plan.to_version} ({len(plan.manifest_files)} files)")
    if tally["conflict"]:
        out.add("To resolve a conflict, move your change into hub-config.json or bin/plugins/, then copy the kit "
                "file over yours; or keep your file, which `rmem check` then reports as changed since install.")
    if event:
        out.add(f"recorded kit_upgraded event {event['event_id']}; run `rmem check` next")
    elif plan.blocked:
        out.add(f"--apply is refused while kit code files have local changes: {', '.join(plan.blocked)}. An old "
                "bin/rmem with new helpers (or the reverse) can break this hub's CLI. Resolve those files first.")
    elif plan.changes:
        out.add("Rerun with --apply to make these changes.")
    else:
        out.add(f"Nothing to upgrade: the hub already has kit {plan.to_version}"
                + (" apart from the conflicts above." if tally["conflict"] else "."))
    out.emit()


def cmd_upgrade(a, core):
    hub = core.find_hub()
    kit = kit_directory(core, a.kit, hub)
    if not a.apply:
        with core.hub_lock(hub, shared=True, readonly_ok=True):
            plan = make_plan(core, hub, kit)
        show(core, hub, kit, plan)
        return 0
    who = core.actor(a, required=True)
    with core.hub_lock(hub):
        core.consistency_preflight(hub, who=who)  # WHY: refuse a forked conversation before any file changes
        plan = make_plan(core, hub, kit)
        if plan.blocked:
            raise core.RmemError(f"upgrade refused: kit code files have local changes ({', '.join(plan.blocked)}); "
                                 "run without --apply to see the diff commands, resolve them, then rerun")
        if not plan.changes:
            print(f"nothing to upgrade: the hub already has kit {plan.to_version}; nothing recorded")
            return 0
        event = apply_plan(core, hub, kit, who, plan)
        linked = core.linked_projects(hub, list(core.events_locked(hub)))
    show(core, hub, kit, plan, event)
    if linked:
        # WHY: upgrade changes the hub only; each project's AGENTS.md keeps the old kit's managed block.
        print(f"Refresh the managed block in each linked project ({len(linked)}) with the upgraded CLI:")
        for project, canonical in linked[:20]:
            print(f"  {shlex.quote(str(hub / 'bin' / 'rmem'))} link {'--canonical ' if canonical else ''}"
                  f"{shlex.quote(str(project))}")
    return 0
