"""Git integration for a hub: embedded commits, canonical links, ledger checks, repair and hooks.

Standard library only. Nothing here pushes, and nothing rewrites the working tree.
Commands that rewrite files outside the ledger or touch Git are dry runs unless
--apply or --write is given. See GIT.md.
"""
import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace

import research_highlights as highlights
import research_identity as identity

EVENT_KINDS = {"ledger_repair"}
EVENT_STRING_FIELDS = {"repair", "backup", "backup_sha256"}

# Lines Git writes around a conflict. A JSON event line never starts with these characters.
CONFLICT_MARKER = re.compile(r"(?:<{7,}|>{7,}|\|{7,})(?: .*)?|={7,}")
HOOKS = ("post-checkout", "post-merge", "post-rewrite", "pre-rebase")
HOOK_MARK = "# rmem guard: installed by `rmem guard install --write`"
IN_PROGRESS = ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "rebase-merge", "rebase-apply", "BISECT_LOG")
# Variables a Git hook exports; they would point our own git calls at the wrong repository.
LOCATION_VARIABLES = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY",
                      "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_COMMON_DIR", "GIT_PREFIX", "GIT_NAMESPACE")
HIGHLIGHT_CHECK_SECONDS = 10
LIST_LIMIT = 30
NAME_LIMIT = 20
# Leftovers of interrupted highlight captures, restores and inits (tempfile.mkdtemp prefixes).
TEMPORARY_PREFIXES = (".capture-", ".rmem-restore-", ".rmem-init-")
# What rmem writes in a hub besides the kit files: untracked files here are committed by sync --embedded.
HUB_DIRECTORIES = ("threads", "findings", "journal", "notes", "lessons", "history", "records", "highlights")
HUB_FILES = ("STATE.md", "MAP.md", "ledger.jsonl", "hub-config.json", ".kit-manifest.json", "ARTIFACT_ROUTES.json",
             "CONTEXT_INDEX.json", highlights.INDEX_NAME)


class GitRefusal(ValueError):
    """A state rmem will not change; the message says what to do next."""


def clipped(value, limit=300):
    text = str(value).replace("\r", " ").replace("\n", " ")
    return text if len(text) <= limit else text[:limit - 14] + " … [truncated]"


def marker_problem(line: str) -> str:
    """The CORRUPT-row text for a Git conflict marker line, or ''."""
    if CONFLICT_MARKER.fullmatch(line.rstrip("\r\n")):
        return "Git conflict marker line; preview the fix with `rmem repair --conflict-markers`"
    return ""


def git_environment(extra=None) -> dict:
    environ = {key: value for key, value in os.environ.items() if key not in LOCATION_VARIABLES}
    environ["GIT_OPTIONAL_LOCKS"] = "0"
    environ.update(extra or {})
    return environ


def git(cwd, *args, env=None, data=None, check=True, timeout=300):
    try:
        result = subprocess.run(["git", "-C", str(cwd), *args], input=data, capture_output=True,
                                env=git_environment(env), timeout=timeout)
    except FileNotFoundError as exc:
        raise GitRefusal("git is not installed or not on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise GitRefusal(f"git {args[0]} did not finish within {timeout} seconds") from exc
    if check and result.returncode:
        raise GitRefusal(f"git {' '.join(args[:2])} failed ({result.returncode}): "
                         f"{clipped(result.stderr.decode('utf-8', 'replace').strip(), 1200)}")
    return result


def output(result) -> str:
    return result.stdout.decode("utf-8", "surrogateescape").strip()


def nul_fields(result) -> list:
    return [item for item in result.stdout.decode("utf-8", "surrogateescape").split("\0") if item]


def literal(path: str) -> str:
    return ":(literal)" + path


def glob_escape(text: str) -> str:
    return re.sub(r"([*?\[\]\\])", r"\\\1", text)


def repository(directory):
    """The Git work tree that contains a directory, or None (no Git, or not in a work tree)."""
    if shutil.which("git") is None:
        return None
    result = git(directory, "rev-parse", "--show-toplevel", "--absolute-git-dir", "--git-common-dir",
                 "--show-prefix", check=False)
    fields = result.stdout.decode("utf-8", "surrogateescape").split("\n")
    if result.returncode or len(fields) < 4:
        return None
    top, git_dir, common, prefix = fields[:4]
    common_dir = Path(common) if os.path.isabs(common) else Path(directory) / common
    git_dir, common_dir = Path(git_dir).resolve(), common_dir.resolve()
    hub_path = prefix.rstrip("/")
    return SimpleNamespace(top=Path(top).resolve(), git_dir=git_dir, common_dir=common_dir, prefix=prefix,
                           hub_path=hub_path or ".", linked=git_dir != common_dir,
                           ledger=prefix + "ledger.jsonl", spec=literal(hub_path) if hub_path else ".")


def head(repo):
    """(symbolic ref or None when detached, commit or None when the branch has no commit yet)."""
    ref = git(repo.top, "symbolic-ref", "-q", "HEAD", check=False)
    commit = git(repo.top, "rev-parse", "-q", "--verify", "HEAD^{commit}", check=False)
    return (output(ref) or None) if ref.returncode == 0 else None, output(commit) if commit.returncode == 0 else None


def in_progress(repo) -> list:
    return [name for name in IN_PROGRESS if (repo.git_dir / name).exists()]


def ledger_ignored(repo) -> bool:
    return git(repo.top, "check-ignore", "-q", "--", repo.ledger, check=False).returncode == 0


def committed_ledger(repo, commit):
    if not commit:
        return None
    result = git(repo.top, "cat-file", "blob", f"{commit}:{repo.ledger}", check=False)
    return result.stdout if result.returncode == 0 else None


def event_time(event):
    try:
        moment = dt.datetime.fromisoformat(str(event.get("ts")).replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else None


def pending_events(repo, events):
    """Ledger events after the number of event lines committed at HEAD; None when Git ignores the ledger."""
    if ledger_ignored(repo):
        return None
    data = committed_ledger(repo, head(repo)[1]) or b""
    committed = sum(1 for line in data.splitlines() if line.strip())
    return events[committed:]


def overdue_line(repo, ctx):
    """A warning when ledger events have waited longer than git.uncommitted_warn_hours, else ''."""
    pending = pending_events(repo, ctx.events)
    oldest = min((moment for moment in map(event_time, pending or []) if moment), default=None)
    if oldest is None:
        return ""
    hours = (dt.datetime.now(dt.timezone.utc) - oldest).total_seconds() / 3600
    limit = ctx.config["git"]["uncommitted_warn_hours"]
    if hours <= limit:
        return ""
    standalone = " (or `rmem sync` in a hub that is its own repository)" if repo.prefix == "" else ""
    return (f"{len(pending)} ledger event(s) are not committed to Git; the oldest is from "
            f"{ctx.core.display_time(oldest.isoformat(), ctx.config)} ({hours:.0f} hours ago; "
            f"git.uncommitted_warn_hours is {limit:g}). A checkout, stash or merge can hide uncommitted "
            f"events: commit them with `rmem sync --embedded --per-event`{standalone}")


def main_checkout(repo):
    entries = git(repo.top, "worktree", "list", "--porcelain", check=False)
    for line in output(entries).splitlines():
        if line.startswith("worktree "):
            return Path(line[len("worktree "):])
    return repo.common_dir.parent


def worktree_line(repo) -> str:
    if not repo.linked:
        return ""
    canonical = main_checkout(repo) / repo.prefix
    state = "" if (canonical / "ledger.jsonl").is_file() else " (no hub there yet)"
    return (f"this hub is inside a linked Git worktree ({repo.top}); its memory files are a branch copy, "
            f"not the canonical writable hub. The canonical hub is in the main checkout: {canonical}{state}. "
            f"Write memory there: run `rmem link --canonical {shlex.quote(str(repo.top))}` from that hub, "
            "or set RMEM_HUB to it")


# ---------------------------------------------------------------------------
# Hooks for rmem status and rmem check

def status_sections(ctx):
    try:
        repo = repository(ctx.hub)
        if repo is None:
            return []
        lines = ["Warning: " + text for text in (worktree_line(repo), overdue_line(repo, ctx)) if text]
    except (GitRefusal, OSError) as exc:
        return [f"Note: Git state not checked ({clipped(exc, 200)})"]
    return ["## Git"] + lines if lines else []


def marker_lines(events):
    numbers = []
    for event in events:
        if event.get("kind") == "CORRUPT" and "Git conflict marker" in str(event.get("note", "")):
            match = re.match(r"line (\d+):", event["note"])
            numbers.append(match[1] if match else "?")
    return numbers


def order_problems(ctx):
    """Warnings for timestamps or stored sequences that go backward in file order."""
    stamps, sequences = [], []
    previous_time = previous_sequence = None
    for position, event in enumerate(ctx.events, 1):
        if event.get("kind") == "CORRUPT":
            continue
        moment = event_time(event)
        if moment and previous_time and moment < previous_time:
            stamps.append((position, moment, previous_time))
        previous_time = moment or previous_time
        sequence = event.get("sequence")
        if type(sequence) is int:
            if previous_sequence is not None and sequence < previous_sequence:
                sequences.append((position, sequence, previous_sequence))
            previous_sequence = sequence
    show = lambda moment: ctx.core.display_time(moment.isoformat(), ctx.config)
    problems = []
    if stamps:
        position, moment, before = stamps[0]
        problems.append(("warn", f"ledger order: {len(stamps)} event(s) have an earlier timestamp than the event "
                         f"before them (first at event position {position}: {show(moment)} after {show(before)}). "
                         "A Git merge, rebase or import probably inserted them; highlights still verify when "
                         "their captured events keep their order. Record a decision event if the order matters"))
    if sequences:
        position, sequence, before = sequences[0]
        problems.append(("warn", f"ledger order: {len(sequences)} stored sequence number(s) go backward "
                         f"(first at event position {position}: {sequence} after {before}). Events from another "
                         "branch or device were merged in; sequences are local counters, not a global order"))
    return problems


def highlight_problems(ctx, repo):
    """Verify every highlight within a time budget; fail on a failed verification."""
    parent = ctx.hub / "highlights"
    if parent.is_symlink() or not parent.is_dir():
        return []
    names = sorted(path.name for path in parent.iterdir() if not path.name.startswith(".capture-"))
    if not names:
        return []
    problems, verified, inserted = [], [], 0
    try:
        live = highlights.read_live(ctx.hub)
    except (ValueError, OSError) as exc:
        return [("warn", f"highlights not verified: cannot read the live ledger ({clipped(exc, 200)})")]
    started = time.monotonic()
    for index, name in enumerate(names):
        if time.monotonic() - started > HIGHLIGHT_CHECK_SECONDS:
            problems.append(("info", f"verified {index} of {len(names)} highlights within the "
                             f"{HIGHLIGHT_CHECK_SECONDS}-second budget; run `rmem highlight verify ID` for the rest"))
            break
        try:
            manifest = highlights.verify(ctx.hub, name, ctx.events, live=live)
        except Exception as exc:  # WHY: any reason a highlight fails to verify is a finding, not a module bug.
            problems.append(("fail", f"highlight {clipped(name, 80)} does not verify: {clipped(exc, 300)}. "
                             f"Inspect highlights/{clipped(name, 80)}/ (see HIGHLIGHTS.md); never rewrite it to pass"))
            continue
        verified.append(name)
        if manifest["history_verification"]["relation"] != "exact_prefix":
            inserted += 1
    if inserted:
        problems.append(("info", f"{inserted} highlight(s) verify with events inserted into the live history "
                         "(an ordered subsequence, not the current continuous prefix); `rmem highlight show ID` explains"))
    if repo is not None and verified:
        try:
            ignored = highlights.ignored_snapshot_files(ctx.hub, verified)
        except (ValueError, OSError) as exc:
            ignored = {}
            problems.append(("warn", f"could not ask Git which snapshot files are ignored ({clipped(exc, 200)})"))
        for identity, paths in sorted(ignored.items())[:10]:
            problems.append(("warn", highlights.ignored_warning(identity, paths)))
    return problems


def check_problems(ctx):
    problems = []
    numbers = marker_lines(ctx.events)
    if numbers:
        shown = ", ".join(numbers[:8]) + (", …" if len(numbers) > 8 else "")
        problems.append(("fail", f"ledger.jsonl contains {len(numbers)} Git conflict marker line(s) (line {shown}). "
                         "Preview the repair with `rmem repair --conflict-markers`; `--apply` backs up the ledger, "
                         "removes only the marker lines and records the repair"))
    problems += order_problems(ctx)
    repo = None
    try:
        repo = repository(ctx.hub)
        if repo is not None:
            problems += [("warn", text) for text in (worktree_line(repo), overdue_line(repo, ctx)) if text]
    except (GitRefusal, OSError) as exc:
        problems.append(("warn", f"Git state not checked: {clipped(exc, 300)}"))
    if not getattr(getattr(ctx, "args", None), "records", False):  # check --records: ledger and records only
        problems += highlight_problems(ctx, repo)
    return problems


def validate_event(event):
    if event.get("kind") != "ledger_repair":
        return ""
    for name in ("removed_lines", "kept_events"):
        if type(event.get(name)) is not int or event[name] < 0:
            return f"ledger_repair {name} must be a nonnegative integer"
    numbers = event.get("removed_line_numbers", [])
    if not isinstance(numbers, list) or any(type(number) is not int or number < 1 for number in numbers):
        return "ledger_repair removed_line_numbers must be a list of positive line numbers"
    return ""


# ---------------------------------------------------------------------------
# rmem sync --embedded

def runtime_path(repo, path: str) -> bool:
    """Runtime and device-local files that never go into a memory commit, even when an old .gitignore misses
    them: claims, runs, locks, ledger backups, local root overrides, and leftovers of interrupted writes."""
    relative = path[len(repo.prefix):]
    parts = relative.split("/")
    return (parts[0] in ("claims", "runs") or "__pycache__" in parts or relative in (".rmem.lock", "local-roots.json")
            or parts[-1].endswith(".tmp") or any(part.startswith(TEMPORARY_PREFIXES) for part in parts)
            or (len(parts) == 1 and relative.startswith((".rmem-lock", "ledger.jsonl.bak-"))))


def written_by_rmem(relative: str, core) -> bool:
    """Whether a hub-relative path is one that rmem or its documented workflow writes. Only these untracked
    files are committed by default; anything else needs --include-untracked after a review."""
    parts = relative.split("/")
    if relative in HUB_FILES or relative in core.kit_files:
        return True
    if parts[0] == "bin":
        return ((len(parts) == 2 and (parts[1] == "rmem" or parts[1] in core.kit_helpers))
                or (len(parts) == 3 and parts[1] == "plugins" and parts[2].endswith((".py", ".md"))))
    return (parts[0] in HUB_DIRECTORIES and len(parts) >= 2
            and not any(part.startswith(".") and part != ".gitkeep" for part in parts[1:]))


def path_names(paths, limit=NAME_LIMIT) -> str:
    shown = ", ".join(clipped(path, 160) for path in paths[:limit])
    return shown + (f" and {len(paths) - limit} more" if len(paths) > limit else "")


def check_staged_files(repo, changes, config, env, *, limit_new_files=False):
    """Check the exact blobs in the proposed index, including changes to tracked files."""
    limit = config["storage"]["file_warn_mb"] * 1024 * 1024
    entries = index_entries(repo, env)
    problems = []
    for status, path in changes:
        if status == "D":
            continue
        mode, blob = entries[path]
        if mode not in ("100644", "100755"):
            problems.append(f"{path} (only regular files can be synced)")
            continue
        relative = Path(path[len(repo.prefix):])
        if any(highlights.SECRET_NAME.search(part) for part in relative.parts):
            problems.append(f"{path} (the name looks like a secret or key file)")
            continue
        if limit_new_files and status == "A" and relative.parts[0] != "highlights":
            size = int(output(git(repo.top, "cat-file", "-s", blob, env=env)))
            if size > limit:
                problems.append(f"{path} ({size / 1048576:.1f} MB, above storage.file_warn_mb "
                                f"{config['storage']['file_warn_mb']:g})")
                continue
        data = git(repo.top, "cat-file", "blob", blob, env=env).stdout
        if highlights.SECRET_TEXT.search(data.decode("utf-8", "replace")):
            problems.append(f"{path} (the content looks like a key, token or password; it is not printed)")
    if problems:
        raise GitRefusal(f"refusing to commit {len(problems)} file(s) that need review: {path_names(problems)}. "
                         "Remove sensitive content or exclude the file from sync. Nothing was committed")


def stage_snapshot(repo, core, env, include_untracked) -> None:
    """Stage the hub into the temporary index: every change to a tracked file, plus the untracked files
    that rmem writes. Other untracked files require include_untracked."""
    tracked = git(repo.top, "ls-files", "-z", "--", repo.spec, env=env).stdout
    if tracked:
        git(repo.top, "add", "-u", "--", repo.spec, env=env)
    listing = git(repo.top, "ls-files", "--others", "--exclude-standard", "-z", "--", repo.spec, env=env)
    untracked = [path for path in nul_fields(listing) if not runtime_path(repo, path)]
    expected = [path for path in untracked if written_by_rmem(path[len(repo.prefix):], core)]
    others = [path for path in untracked if path not in set(expected)]
    if others and not include_untracked:
        raise GitRefusal(f"{len(others)} untracked file(s) under {repo.hub_path} are not files rmem writes: "
                         f"{path_names(others)}. Review them; then commit the ones that belong to memory with "
                         "`rmem sync --embedded --include-untracked`, and move the others out of the hub or list "
                         "them in the hub's .gitignore. Nothing was committed")
    chosen = expected + (others if include_untracked else [])
    if chosen:
        git(repo.top, "update-index", "--add", "-z", "--stdin", data=os.fsencode("\0".join(chosen) + "\0"), env=env)


def commit_standalone(repo, paths, memory_path, config, message):
    """Keep the user's index intact on refusal and commit from the inspected index."""
    index = repo.git_dir / "index"
    lock = index.with_name("index.lock")
    with tempfile.TemporaryDirectory(prefix="rmem-sync-", dir=repo.git_dir) as scratch:
        staged_index = Path(scratch) / "index"
        env = {"GIT_INDEX_FILE": str(staged_index)}
        try:
            fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            raise GitRefusal("Git is updating the index; finish that operation and retry") from None
        os.close(fd)
        try:
            if index.exists():
                shutil.copy2(index, staged_index)
            ref, old = head(repo)
            if in_progress(repo):
                raise GitRefusal("finish the current Git operation before syncing")
            staged = changed_paths(repo, old, env)
            unrelated = [path for _, path in staged if not memory_path(path)]
            if unrelated:
                raise GitRefusal("unrelated staged files would be committed: " + path_names(unrelated))
            if paths:
                git(repo.top, "add", "-A", "--", *(literal(path) for path in sorted(paths)), env=env)
            changes = changed_paths(repo, old, env)
            if not changes:
                return False
            check_staged_files(repo, changes, config, env)
            if head(repo) != (ref, old) or in_progress(repo):
                raise GitRefusal("HEAD changed while staging memory; retry the sync")
            git(repo.top, "commit", "-q", "-m", message, env=env)
            os.replace(staged_index, index)
        finally:
            lock.unlink()
    return True


def drop_runtime_paths(repo, old, env) -> None:
    """Put runtime paths in the temporary index back to their state in the old commit."""
    runtime = {path for _, path in changed_paths(repo, old, env) if runtime_path(repo, path)}
    if not runtime:
        return
    restore = []
    if old:
        listing = ["ls-tree", "-r", "-z", old] + (["--", repo.prefix.rstrip("/")] if repo.prefix else [])
        for item in nul_fields(git(repo.top, *listing)):
            if item.partition("\t")[2] in runtime:
                restore.append(item)
                runtime.discard(item.partition("\t")[2])
    if restore:
        git(repo.top, "update-index", "-z", "--index-info", data=os.fsencode("\0".join(restore) + "\0"), env=env)
    if runtime:
        git(repo.top, "update-index", "-z", "--force-remove", "--stdin", data=os.fsencode("\0".join(sorted(runtime)) + "\0"), env=env)


def staged_hub_paths(repo, old) -> list:
    if old:
        return nul_fields(git(repo.top, "diff-index", "--cached", "--name-only", "-z", old, "--", repo.spec))
    return nul_fields(git(repo.top, "ls-files", "-z", "--", repo.spec))


def changed_paths(repo, old, env) -> list:
    """(status letter, path) for index entries that differ from the old commit."""
    if not old:
        return [("A", path) for path in nul_fields(git(repo.top, "ls-files", "-z", env=env))]
    fields = nul_fields(git(repo.top, "diff-index", "--cached", "-z", "--no-renames", "--name-status", old, env=env))
    return list(zip(fields[0::2], fields[1::2]))


def index_entries(repo, env) -> dict:
    """path -> (mode, object id) for every hub entry of an index."""
    entries = {}
    for item in nul_fields(git(repo.top, "ls-files", "-s", "-z", "--", repo.spec, env=env)):
        meta, _, path = item.partition("\t")
        mode, oid, _stage = meta.split(" ")
        entries[path] = (mode, oid)
    return entries


def event_message(event) -> str:
    label = event.get("id") or (f"thread={event['thread']}" if event.get("thread") else "")
    text = " ".join(part for part in ("Hub:", str(event.get("kind", "?")), str(label)) if part)
    if event.get("status"):
        text += f" ({event['status']})"
    return re.sub(r"[\x00-\x1f\x7f]+", " ", clipped(text, 200))


def event_chunks(live: bytes, committed: bytes) -> list:
    """[end offset, event] for every event line after the committed prefix."""
    chunks, offset = [], len(committed)
    for line in live[len(committed):].splitlines(keepends=True):
        offset += len(line)
        if not line.strip():
            continue
        if not line.endswith(b"\n"):
            raise GitRefusal("ledger.jsonl ends with an unfinished line; run `rmem check` before syncing")
        chunks.append([offset, json.loads(line)])
    if chunks:
        chunks[-1][0] = len(live)  # trailing blank lines belong to the last commit
    return chunks


def assign_files(repo, changes, chunks) -> list:
    """Per commit, the non-ledger changes it carries: the first event at or after the file's mtime, else the last."""
    times = [event_time(event) for _, event in chunks]
    groups = [[] for _ in chunks]
    for status, path in changes:
        if path == repo.ledger:
            continue
        try:
            changed = dt.datetime.fromtimestamp((repo.top / path).lstat().st_mtime, dt.timezone.utc)
        except OSError:
            changed = None  # deleted: no modification time, so it goes into the last commit
        index = next((i for i, moment in enumerate(times) if changed and moment and moment >= changed), len(chunks) - 1)
        groups[index].append(path)
    return groups


def git_date(moment) -> str:
    return f"@{int(moment.timestamp())} +0000"


def build_commits(repo, old, final, message, per_event, live, env, scratch):
    """Create the commit objects (never a ref). Returns a list of commit records, oldest first."""
    changes = changed_paths(repo, old, env)
    chunks = []
    if per_event:
        committed = committed_ledger(repo, old) or b""
        if not live.startswith(committed) or (committed and not committed.endswith(b"\n")):
            raise GitRefusal("the ledger committed at HEAD is not a prefix of the live ledger (a merge, rebase or "
                             "repair changed earlier lines), so per-event commits cannot be derived. Run `rmem check`, "
                             "then commit once with `rmem sync --embedded` (without --per-event)")
        chunks = event_chunks(live, committed)
    if not chunks:
        args = ["commit-tree", final, "-m", message] + (["-p", old] if old else [])
        oid = output(git(repo.top, *args, env=env))
        return [SimpleNamespace(oid=oid, message=message, moment=None, files=len(changes))]
    entries = index_entries(repo, env)
    if repo.ledger not in entries:
        raise GitRefusal("ledger.jsonl is not in the staged hub files; check that Git does not ignore it")
    build = {**env, "GIT_INDEX_FILE": str(Path(scratch) / "build-index")}
    git(repo.top, *(("read-tree", old) if old else ("read-tree", "--empty")), env=build)
    groups = assign_files(repo, changes, chunks)
    ledger_mode = entries[repo.ledger][0]
    commits, parent = [], old
    for (end, event), paths in zip(chunks, groups):
        blob = output(git(repo.top, "hash-object", "-w", "--stdin", data=live[:end], env=build))
        info = [f"{ledger_mode} {blob}\t{repo.ledger}"] + [f"{entries[p][0]} {entries[p][1]}\t{p}" for p in paths if p in entries]
        git(repo.top, "update-index", "-z", "--index-info", data=os.fsencode("\0".join(info) + "\0"), env=build)
        removed = [p for p in paths if p not in entries]
        if removed:
            git(repo.top, "update-index", "-z", "--force-remove", "--stdin", data=os.fsencode("\0".join(removed) + "\0"), env=build)
        tree = output(git(repo.top, "write-tree", env=build))
        moment = event_time(event)
        dates = {"GIT_AUTHOR_DATE": git_date(moment), "GIT_COMMITTER_DATE": git_date(moment)}
        text = event_message(event)
        args = ["commit-tree", tree, "-m", text] + (["-p", parent] if parent else [])
        parent = output(git(repo.top, *args, env={**build, **dates}))
        commits.append(SimpleNamespace(oid=parent, message=text, moment=moment, files=len(paths)))
    if tree != final:
        raise GitRefusal("the per-event commits do not add up to the staged hub files (a file changed during the "
                         "sync?); nothing was committed. Run `rmem sync --embedded` again")
    return commits


def cmd_sync_embedded(a, core):
    hub, who = core.find_hub(), core.actor(a)
    message = core.one_line(a.message, "commit message", 1000) if a.message else f"rmem sync by {who['agent']}"
    config = core.load_config(hub)
    out = core.Output(config["caps"]["output_max_chars"])
    with tempfile.TemporaryDirectory(prefix="rmem-sync-") as scratch:
        env = {"GIT_INDEX_FILE": str(Path(scratch) / "index")}
        # WHY: sync never appends to the hub, so a shared lock that holds the ledger still while the snapshot is
        # staged is enough; the commits are built after it is released, so other sessions can keep writing.
        with core.hub_lock(hub, shared=True):
            core.consistency_preflight(hub)
            repo = repository(hub)
            if repo is None:
                raise GitRefusal("the hub is not inside a Git work tree (or git is not installed); `rmem sync --embedded` "
                                 "commits hub files into the repository that contains the hub")
            ref, old = head(repo)
            if ref is None:
                raise GitRefusal("HEAD is detached; check out the branch that should carry the memory commits "
                                 "(for example `git switch main`) and run sync again")
            busy = in_progress(repo)
            if busy:
                raise GitRefusal(f"a Git operation is in progress ({', '.join(busy)}); finish or abort it, run "
                                 "`rmem check`, then sync again")
            staged = staged_hub_paths(repo, old)
            if staged:
                raise GitRefusal(f"you have staged changes under the hub path {repo.hub_path} "
                                 f"({clipped(', '.join(staged), 400)}); sync builds its own commits and will not mix "
                                 f"them with your staging. Unstage them with `git reset -q -- {shlex.quote(repo.hub_path)}` "
                                 "(working-tree files stay as they are) and run sync again")
            if ledger_ignored(repo):
                raise GitRefusal(f"Git ignores {repo.ledger} in {repo.top}; remove the ignore rule that covers the hub "
                                 "before syncing memory")
            if a.dry_run:
                # WHY: objects go to a throwaway store, so a dry run leaves the repository byte for byte unchanged.
                (Path(scratch) / "objects").mkdir()
                env.update(GIT_OBJECT_DIRECTORY=str(Path(scratch) / "objects"),
                           GIT_ALTERNATE_OBJECT_DIRECTORIES=str(repo.common_dir / "objects"))
            git(repo.top, *(("read-tree", old) if old else ("read-tree", "--empty")), env=env)
            stage_snapshot(repo, core, env, getattr(a, "include_untracked", False))
            drop_runtime_paths(repo, old, env)
            changes = changed_paths(repo, old, env)
            check_staged_files(repo, changes, config, env, limit_new_files=True)
            final = output(git(repo.top, "write-tree", env=env))
            live = git(repo.top, "cat-file", "blob", f"{final}:{repo.ledger}", env=env).stdout if a.per_event else b""
        base = output(git(repo.top, "rev-parse", f"{old}^{{tree}}")) if old else None
        if final == base or (not old and not changes):
            print("no memory changes to commit")
            return 0
        commits = build_commits(repo, old, final, message, a.per_event, live, env, scratch)
        if a.dry_run:
            return report(out, core, config, repo, ref, old, commits, changes, dry_run=True)
        if head(repo) != (ref, old) or in_progress(repo):
            raise GitRefusal("HEAD moved while the commits were built; nothing was changed. Run sync again")
        zero = "0" * len(final)
        moved = git(repo.top, "update-ref", "-m", f"rmem sync --embedded: {len(commits)} commit(s)",
                    ref, commits[-1].oid, old or zero, check=False)
        if moved.returncode:
            raise GitRefusal(f"{ref} moved while the commits were built, so it was not updated (compare-and-swap "
                             f"failed: {clipped(moved.stderr.decode('utf-8', 'replace').strip(), 300)}); nothing "
                             "else changed. Run `rmem sync --embedded` again")
    refreshed = git(repo.top, "reset", "-q", commits[-1].oid, "--", repo.spec, check=False)
    if refreshed.returncode:
        out.add(f"Warning: {ref} moved, but Git could not refresh the index for {repo.hub_path} "
                f"({clipped(refreshed.stderr.decode('utf-8', 'replace').strip(), 300)}); run "
                f"`git reset -q -- {shlex.quote(repo.hub_path)}` so `git status` stops showing old hub files as staged")
    return report(out, core, config, repo, ref, old, commits, changes, dry_run=False)


def report(out, core, config, repo, ref, old, commits, changes, dry_run):
    branch = ref[len("refs/heads/"):] if ref.startswith("refs/heads/") else ref
    start = old[:10] if old else "an empty branch"
    verb = "dry run: would create" if dry_run else "created"
    out.add(f"{verb} {len(commits)} commit(s) on {branch} after {start}, holding only files under {repo.hub_path}")
    files = [(status, path) for status, path in changes if path != repo.ledger]
    if files:
        counts = {letter: sum(1 for status, _ in files if status == letter) for letter in "AMD"}
        out.add(f"Hub files besides the ledger: {counts['A']} added, {counts['M']} modified, {counts['D']} deleted"
                + (f" (showing {LIST_LIMIT})" if len(files) > LIST_LIMIT else ""))
        for status, path in files[:LIST_LIMIT]:
            out.add(f"  {status} {clipped(path, 200)}")
    shown = commits if len(commits) <= LIST_LIMIT else commits[:LIST_LIMIT // 2] + [None] + commits[-LIST_LIMIT // 2:]
    for commit in shown:
        if commit is None:
            out.add(f"- [{len(commits) - LIST_LIMIT} more commits not shown]")
            continue
        when = core.display_time(commit.moment.isoformat(), config) if commit.moment else "now"
        name = "" if dry_run else commit.oid[:10] + " "
        extra = f" (+{commit.files} other file(s))" if commit.files and commit.moment else ""
        out.add(f"- {name}{when} {clipped(commit.message, 200)}{extra}")
    if dry_run:
        out.add("No commit, branch, index or working-tree file was changed; objects went to a temporary store.")
    else:
        out.add("The working tree and your other staged files were not touched. Nothing was pushed.")
        if len(commits) > 20:
            out.add(f"{len(commits)} ledger versions were written as loose objects; `git gc` packs them.")
    out.emit()
    return 0


# ---------------------------------------------------------------------------
# rmem link --canonical (called from cmd_link while it holds the locks)

def exclude_pointer(pointer: Path) -> list:
    """Keep a per-device .rmem out of Git; returns lines to print and never raises."""
    try:
        return exclude_rule(pointer)
    except (GitRefusal, OSError, ValueError) as exc:
        return [f"Warning: could not add .rmem to the repository's exclude file ({clipped(exc, 300)}); "
                "add `/.rmem` to .git/info/exclude yourself so the per-device pointer stays out of commits"]


def exclude_rule(pointer: Path) -> list:
    repo = repository(pointer.parent)
    if repo is None:
        return ["the project is not inside a Git work tree; nothing was added to an exclude file"]
    relative = pointer.relative_to(repo.top).as_posix()
    location = output(git(repo.top, "rev-parse", "--git-path", "info/exclude"))
    exclude = Path(location) if os.path.isabs(location) else repo.top / location
    rule = "/" + glob_escape(relative)
    text = exclude.read_text(encoding="utf-8") if exclude.is_file() else ""
    notes = []
    if rule in text.splitlines():
        notes.append(f"{exclude} already excludes {rule}")
    else:
        exclude.parent.mkdir(parents=True, exist_ok=True)
        separator = "" if not text or text.endswith("\n") else "\n"
        with exclude.open("a", encoding="utf-8") as stream:
            stream.write(f"{separator}# rmem: per-device hub pointer\n{rule}\n")
        notes.append(f"added {rule} to {exclude} (shared by every worktree of this repository)")
    if git(repo.top, "ls-files", "--error-unmatch", "--", literal(relative), check=False).returncode == 0:
        notes.append(f"`.rmem` is tracked by Git, so every checkout shares one pointer; untrack it with "
                     f"`git -C {shlex.quote(str(repo.top))} rm --cached {shlex.quote(relative)}` and commit that change "
                     "(rmem does not run it for you)")
    return notes


# ---------------------------------------------------------------------------
# rmem repair --conflict-markers

def repair_plan(data: bytes, event_problem):
    lines = data.splitlines(keepends=True)
    markers, kept, problems, seen, duplicates, sequences = [], [], [], set(), [], {}
    events = 0
    for number, line in enumerate(lines, 1):
        try:
            text = line.decode("utf-8")
        except UnicodeDecodeError:
            kept.append(line)
            problems.append(f"line {number}: not UTF-8")
            continue
        if CONFLICT_MARKER.fullmatch(text.rstrip("\r\n")):
            markers.append((number, text.rstrip("\r\n")))
            continue
        kept.append(line)
        if not text.strip():
            continue
        try:
            event = json.loads(text)
            problem = event_problem(event)
        except (json.JSONDecodeError, RecursionError):
            event, problem = None, "invalid JSON"
        if problem:
            problems.append(f"line {number}: {problem}")
            continue
        events += 1
        if event.get("event_id"):
            if event["event_id"] in seen:
                duplicates.append(f"line {number}: event_id {clipped(event['event_id'], 40)}")
            seen.add(event["event_id"])
        if type(event.get("sequence")) is int:
            sequences[event["sequence"]] = sequences.get(event["sequence"], 0) + 1
    repeated = sorted(number for number, total in sequences.items() if total > 1)
    return SimpleNamespace(markers=markers, kept=b"".join(kept), problems=problems, duplicates=duplicates,
                           events=events, repeated_sequences=repeated)


def cmd_repair(a, core):
    if not a.conflict_markers:
        raise GitRefusal("rmem repair only supports --conflict-markers; other ledger problems need a reviewed, "
                         "hand-made recovery (see CONSISTENCY.md)")
    hub = core.find_hub()
    who = core.actor(a, required=a.apply)
    out = core.Output()
    with core.hub_lock(hub, shared=not a.apply, readonly_ok=not a.apply):
        path = core.safe_path(hub, "ledger.jsonl")
        data = path.read_bytes()
        plan = repair_plan(data, core.event_problem)
        if not plan.markers:
            print("no Git conflict marker lines in ledger.jsonl; nothing to repair")
            return 0
        if plan.problems or plan.duplicates:
            detail = "; ".join((plan.problems + plan.duplicates)[:5])
            raise GitRefusal(f"cannot repair automatically: after removing the {len(plan.markers)} marker line(s), "
                             f"lines remain that are not unique valid events ({clipped(detail, 800)}). Resolve the "
                             "conflict by hand from a copy (keep each side's complete event lines, drop partial or "
                             "repeated ones), then run `rmem check`")
        shown = ", ".join(f"{number} `{clipped(text, 40)}`" for number, text in plan.markers[:10])
        more = f" and {len(plan.markers) - 10} more" if len(plan.markers) > 10 else ""
        if plan.repeated_sequences:
            note = (f"Note: {len(plan.repeated_sequences)} stored sequence number(s) appear twice "
                    f"(first {plan.repeated_sequences[0]}); both sides appended concurrently. `rmem check` keeps "
                    "reporting them; record a decision event that explains the merge.")
        else:
            note = ""
        if not a.apply:
            out.add(f"dry run: ledger.jsonl has {len(plan.markers)} Git conflict marker line(s): line {shown}{more}.")
            out.add(f"Removing only those lines leaves {plan.events} valid events with unique event IDs.")
            if note:
                out.add(note)
            out.add("Run `rmem repair --conflict-markers --apply` to back up the ledger to ledger.jsonl.bak-<UTC time>, "
                    "remove only these lines and record a ledger_repair event.")
            out.emit()
            return 0
        # WHY: the full preflight refuses the CORRUPT rows that marker lines produce, so check the identity
        # binding directly, before the backup and the rewrite, so a refused process changes nothing.
        fork = identity.current_fork(list(core.events_locked(hub)), who.get("session"))
        if fork:
            raise GitRefusal(fork + "; nothing was changed")
        stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup = core.safe_path(hub, f"ledger.jsonl.bak-{stamp}")
        mode = path.stat().st_mode & 0o777
        fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), mode)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        core.atomic_write(path, plan.kept.decode("utf-8"))
        try:
            event = core.append_locked(hub, {
                **who, "kind": "ledger_repair", "repair": "conflict_markers", "removed_lines": len(plan.markers),
                "kept_events": plan.events, "removed_line_numbers": [number for number, _ in plan.markers[:50]],
                "backup": backup.name, "backup_sha256": hashlib.sha256(data).hexdigest(),
                "note": f"Removed {len(plan.markers)} Git conflict marker line(s); backup {backup.name}"})
        except BaseException:
            # An unrecorded rewrite could never be recorded later (a rerun finds no markers): put the bytes back.
            core.atomic_write(path, data.decode("utf-8"))
            backup.unlink(missing_ok=True)
            raise
    out.add(f"repaired ledger.jsonl: removed {len(plan.markers)} marker line(s) ({shown}{more}); "
            f"{plan.events} events kept byte for byte.")
    out.add(f"Backup: {backup} (ignored by Git; delete it after review). Recorded: {core.fmt_event(event)}")
    if note:
        out.add(note)
    out.add("Run `rmem check` next.")
    out.emit()
    return 0


# ---------------------------------------------------------------------------
# rmem guard install|run

def hooks_directory(repo):
    """(hooks directory, description, configuration scope or None)."""
    configured = git(repo.top, "config", "--show-scope", "--get", "core.hooksPath", check=False)
    scope, _, value = output(configured).partition("\t")
    if configured.returncode not in (0, 1):  # a Git older than 2.26 has no --show-scope
        configured = git(repo.top, "config", "--get", "core.hooksPath", check=False)
        value = output(configured)
        local = output(git(repo.top, "config", "--local", "--get", "core.hooksPath", check=False))
        scope = "local" if value and local == value else "global or system"
    if configured.returncode == 0 and value:
        path = Path(os.path.expanduser(value))
        return (path if path.is_absolute() else repo.top / path), "core.hooksPath", scope
    location = output(git(repo.top, "rev-parse", "--git-path", "hooks"))
    return (Path(location) if os.path.isabs(location) else repo.top / location), "the repository's hooks directory", None


def cli_path() -> Path:
    # WHY: the CLI that installs the hooks knows `guard run`; an older hub's own bin/rmem may not.
    return Path(__file__).resolve().with_name("rmem")


def guard_copy(repo, core):
    """(directory, {name: bytes}): a pinned copy of this CLI and its helpers (never bin/plugins/) under
    <git-common-dir>/rmem-guard/<sha256 prefix>/, which the hooks run instead of the checked-out bin/rmem.
    WHY: a hook runs on every checkout, so running tracked code would execute whatever the checked-out
    commit contains."""
    source = cli_path().parent
    files = {"bin/rmem": cli_path().read_bytes()}
    for helper in core.kit_helpers:
        files["bin/" + helper] = (source / helper).read_bytes()
    version = source.parent / "VERSION"
    if version.is_file():
        files["VERSION"] = version.read_bytes()
    digest = hashlib.sha256(b"".join(hashlib.sha256(name.encode() + b"\0" + data).digest()
                                     for name, data in sorted(files.items()))).hexdigest()
    return repo.common_dir / "rmem-guard" / digest[:16], files


def write_guard_copy(directory: Path, files: dict) -> None:
    """Create the pinned copy once (staged, then renamed); an existing copy must hold the same bytes."""
    if directory.exists():
        for name, data in files.items():
            if (directory / name).read_bytes() != data:
                raise GitRefusal(f"{directory} exists with different contents; remove it and install again")
        return
    directory.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".rmem-guard-", dir=directory.parent))
    try:
        for name, data in files.items():
            target = staging / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        (staging / "bin/rmem").chmod(0o755)
        os.replace(staging, directory)
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def hook_line(hub: Path, name: str, cli: Path) -> str:
    return (f'RMEM_HUB={shlex.quote(str(hub))} RMEM_NO_PLUGINS=1 "${{RMEM_PYTHON:-python3}}" {shlex.quote(str(cli))} '
            f'guard run {name} "$@" >&2 || true')


def hook_script(hub: Path, name: str, cli: Path, common: Path) -> str:
    # WHY: a hooks directory can be shared by several repositories (a global core.hooksPath); the hook
    # does nothing outside the repository it was installed for.
    check = (f'[ "$(cd "$(git rev-parse --git-common-dir 2>/dev/null)" 2>/dev/null && pwd -P)" = '
             f'{shlex.quote(str(common))} ] || exit 0')
    return (f"#!/bin/sh\n{HOOK_MARK}\n"
            "# Warns about hub problems and uncommitted ledger events; it never blocks Git. It runs a pinned copy\n"
            "# of the rmem CLI, not the checked-out bin/rmem, and loads no hub plugins.\n"
            f"{check}\nif [ -f {shlex.quote(str(cli))} ]; then\n  {hook_line(hub, name, cli)}\nfi\nexit 0\n")


def tracked_inside(repo, directory: Path) -> bool:
    """Whether a hooks directory lies inside the work tree and Git tracks files in it."""
    try:
        relative = directory.resolve().relative_to(repo.top).as_posix()
    except ValueError:
        return False
    return bool(nul_fields(git(repo.top, "ls-files", "-z", "--", literal(relative), check=False)))


def cmd_guard_install(a, core):
    hub = core.find_hub()
    repo = repository(hub)
    if repo is None:
        raise GitRefusal("the hub is not inside a Git work tree; guard hooks protect a hub that Git checks out")
    directory, source, scope = hooks_directory(repo)
    if scope and scope != "local" and scope != "worktree" and not a.global_hooks_path:
        raise GitRefusal(f"core.hooksPath comes from your {scope} Git configuration ({directory}), so hooks there run "
                         "for every repository that uses it. Pass --global-hooks-path to install there anyway (each "
                         "hook then does nothing outside this repository), or set a hooks path for this repository "
                         "only with `git config --local core.hooksPath .git/hooks`")
    if tracked_inside(repo, directory):
        raise GitRefusal(f"core.hooksPath {directory} is a directory Git tracks in this repository; the generated "
                         "hooks hold this device's absolute paths and would be committed. Point core.hooksPath at an "
                         "untracked directory, or add the line that `rmem guard install` prints to your hooks yourself")
    pinned, files = guard_copy(repo, core)
    cli = pinned / "bin" / "rmem"
    out = core.Output()
    verb = "installing" if a.write else "dry run: would install"
    where = f"{source}, {scope} configuration" if scope else source
    out.add(f"{verb} rmem guard hooks in {directory} ({where}); they run `rmem check` after a checkout, merge "
            "or rewrite and warn about uncommitted ledger events before a rebase. They only print warnings.")
    out.add(f"The hooks run Python on every checkout, merge and rewrite: a pinned copy of this CLI in {pinned} "
            "(never the checked-out bin/rmem), with hub plugins disabled.")
    if a.write:
        write_guard_copy(pinned, files)
    snippets = []
    for name in HOOKS:
        path = directory / name
        wanted = hook_script(hub, name, cli, repo.common_dir)
        if path.exists() or path.is_symlink():
            try:
                current = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                current = ""
            if current == wanted:
                out.add(f"- {name}: already installed")
            else:
                out.add(f"- {name}: a hook already exists; not changed. Add this line to it yourself:")
                snippets.append(f"  {hook_line(hub, name, cli)}")
                out.add(snippets[-1])
            continue
        if not a.write:
            out.add(f"- {name}: would be created")
            continue
        directory.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o755)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(wanted)
        out.add(f"- {name}: created")
    if not a.write:
        out.add("Each new hook would contain:")
        for line in hook_script(hub, "HOOK-NAME", cli, repo.common_dir).splitlines():
            out.add("    " + line)
        out.add("Run `rmem guard install --write` to create the missing hooks; existing hooks are never overwritten.")
    out.emit()
    return 0


def cmd_guard_run(a, core):
    """What the installed hooks call: bounded warnings on stderr, always exit 0."""
    out = core.Output(4000)
    try:
        hub = core.find_hub()
        config = core.load_config(hub)
        with core.hub_lock(hub, shared=True, readonly_ok=True):
            events = list(core.events_locked(hub))
        repo = repository(hub)
        pending = pending_events(repo, events) if repo else None
        if pending:
            oldest = min((moment for moment in map(event_time, pending) if moment), default=None)
            since = f" (oldest {core.display_time(oldest.isoformat(), config)})" if oldest else ""
            out.add(f"rmem guard ({a.hook}): {len(pending)} ledger event(s) in {hub} are not committed to Git{since}. "
                    "A checkout, stash or rebase can hide them; commit them with `rmem sync --embedded --per-event`.")
        if a.hook != "pre-rebase":
            environ = git_environment({"RMEM_HUB": str(hub)})
            environ.pop("GIT_OPTIONAL_LOCKS", None)
            environ["RMEM_NO_PLUGINS"] = "1"  # WHY: plugins come from the checked-out tree
            result = subprocess.run([sys.executable, str(cli_path()), "check"],
                                    capture_output=True, text=True, env=environ, timeout=180)
            if result.returncode:
                failures = [line for line in result.stdout.splitlines() if line.startswith("! ")]
                out.add(f"rmem guard ({a.hook}): `rmem check` found {len(failures) or 'some'} problem(s) in {hub}:")
                for line in failures[:5]:
                    out.add("  " + clipped(line, 300))
                if len(failures) > 5:
                    out.add(f"  [{len(failures) - 5} more]")
                out.add("  Run `rmem check` before writing memory; this Git operation may have changed the ledger.")
    except Exception as exc:  # WHY: a hook must never break the user's Git command.
        out.add(f"rmem guard ({a.hook}): could not check the hub: {clipped(exc, 300)}")
    out.emit()
    return 0


def register(commands, core):
    p = commands.add_parser("repair", help="preview or apply a ledger repair (only --conflict-markers; dry run unless --apply)")
    p.add_argument("--conflict-markers", action="store_true", help="remove only Git conflict marker lines from ledger.jsonl")
    p.add_argument("--apply", action="store_true",
                   help="back up the ledger, remove the marker lines and record a ledger_repair event")
    p.set_defaults(fn=lambda a: cmd_repair(a, core))
    p = commands.add_parser("guard", help="Git hooks that warn about hub problems (install is a dry run unless --write)")
    guard = p.add_subparsers(dest="guard_command", required=True)
    p.rmem_choices = guard.choices
    g = guard.add_parser("install", help="print, or with --write create, post-checkout/post-merge/post-rewrite/pre-rebase hooks")
    g.add_argument("--write", action="store_true", help="create missing hooks; existing hooks are never overwritten")
    g.add_argument("--global-hooks-path", action="store_true",
                   help="install even when core.hooksPath comes from the global or system Git configuration")
    g.set_defaults(fn=lambda a: cmd_guard_install(a, core))
    g = guard.add_parser("run", help="what the installed hooks call (read-only; always exits 0)")
    g.add_argument("hook", choices=HOOKS)
    g.add_argument("hook_args", nargs=argparse.REMAINDER, help=argparse.SUPPRESS)
    g.set_defaults(fn=lambda a: cmd_guard_run(a, core))
