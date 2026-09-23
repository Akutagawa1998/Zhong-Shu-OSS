#!/usr/bin/env python3
"""Query a metadata index, then read bounded slices of exact registered text files.

This opt-in reader is not an OS sandbox and does not discover or register files.
"""
import argparse
import json
from pathlib import Path
import stat

from artifact_routes import safe_path


INDEX_MAX_BYTES = 256 * 1024
FILE_MAX_BYTES = 16 * 1024 * 1024
OUTPUT_MAX_CHARS = 12000


def encoded(view):
    return json.dumps(view, ensure_ascii=False, separators=(",", ":"))


def regular_file(root, name, maximum):
    path = safe_path(root, name)
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_size > maximum:
        raise ValueError(f"Expected a regular file of at most {maximum} bytes: {name}")
    return path


def registered_entries(root, index_name):
    """Open only the named metadata index; never enumerate source directories."""
    index = regular_file(root, index_name, INDEX_MAX_BYTES)
    with index.open("rb") as stream:
        data = stream.read(INDEX_MAX_BYTES + 1)
    if len(data) > INDEX_MAX_BYTES:
        raise ValueError("Context index exceeds byte limit")
    config = json.loads(data)
    if not isinstance(config, dict) or type(config.get("schema_version")) is not int or config["schema_version"] != 1:
        raise ValueError("Expected context index schema_version 1")
    entries = config.get("entries")
    if not isinstance(entries, list) or len(entries) > 1000:
        raise ValueError("Context index needs at most 1000 entries")
    selected, ids = [], set()
    for row in entries:
        if not isinstance(row, dict):
            raise ValueError("Context index entries must be objects")
        for key, maximum in (("id", 128), ("path", 512), ("scope", 80), ("summary", 1000)):
            value = row.get(key)
            if (not isinstance(value, str) or not value.strip() or len(value) > maximum
                    or any(ord(char) < 32 for char in value)):
                raise ValueError(f"Context index entry needs nonempty {key}, at most {maximum} characters")
        if row["id"] in ids:
            raise ValueError("Duplicate context index ID")
        safe_path(root, row["path"])
        ids.add(row["id"])
        selected.append({key: row[key] for key in ("id", "path", "scope", "summary")})
    return selected


def index_view(entries, query="", scope=None, limit=15):
    if not 1 <= limit <= 50:
        raise ValueError("Use an index limit from 1 to 50")
    matches = [row for row in entries if (scope is None or row["scope"] == scope)
               and (not query or query.casefold() in encoded(row).casefold())]
    view = {"entries": matches[:limit], "matching_count": len(matches),
            "truncated": len(matches) > limit,
            "limits": "Metadata only; document contents were not opened. No filesystem fallback."}
    while len(encoded(view)) >= OUTPUT_MAX_CHARS and view["entries"]:
        view["entries"].pop()
        view["truncated"] = True
    return view


def line_prefixes(stream):
    """Bound memory even for one very long physical line."""
    while True:
        chunk = stream.readline(1001)
        if not chunk:
            return
        content = chunk.rstrip("\r\n")
        clipped = len(content) > 1000
        while chunk and not chunk.endswith("\n") and len(chunk) == 1001:
            chunk = stream.readline(1001)
            if chunk and chunk.rstrip("\r\n"):
                clipped = True
        yield content[:1000], clipped


def read_view(root, entries, identifier, start=1, limit=40):
    if start < 1 or not 1 <= limit <= 100:
        raise ValueError("Use start >= 1 and a read limit from 1 to 100")
    exact = [row for row in entries if row["id"] == identifier]
    matches = exact or [row for row in entries if row["path"] == identifier]
    names = {row["path"] for row in matches}
    if len(names) != 1:
        raise ValueError("Unregistered or ambiguous input; select/register an exact source first. No filesystem fallback.")
    name = names.pop()
    path = regular_file(root, name, FILE_MAX_BYTES)
    view = {"path": name, "index_ids": [row["id"] for row in matches][:20],
            "index_ids_total": len(matches), "index_ids_truncated": len(matches) > 20,
            "start": start, "lines": [], "next_line": None,
            "limits": "Only the exact registered text file was opened. Registration is not scientific acceptance, freshness, or OS isolation."}
    characters = 0
    with path.open(encoding="utf-8") as stream:
        for number, (text, clipped) in enumerate(line_prefixes(stream), 1):
            if number < start:
                continue
            if len(view["lines"]) >= limit or characters + len(text) > 8000:
                view["next_line"] = number
                break
            if "\0" in text:
                raise ValueError("Expected UTF-8 text without NUL bytes")
            view["lines"].append({"line": number, "text": text, "line_clipped": clipped})
            characters += len(text)
    while len(encoded(view)) >= OUTPUT_MAX_CHARS and view["lines"]:
        view["next_line"] = view["lines"].pop()["line"]
    return view


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, help="explicit project root; no inferred active project")
    parser.add_argument("--index", required=True, help="root-relative schema-1 metadata index")
    commands = parser.add_subparsers(dest="command", required=True)
    index = commands.add_parser("index", help="query only indexed metadata")
    index.add_argument("--query", default="")
    index.add_argument("--scope")
    index.add_argument("--limit", type=int, default=15)
    read = commands.add_parser("read", help="read an exact registered ID/path; reject unindexed input")
    read.add_argument("identifier")
    read.add_argument("--start", type=int, default=1)
    read.add_argument("--limit", type=int, default=40)
    args = parser.parse_args()
    try:
        root = Path(args.root).expanduser().resolve(strict=True)
        if not root.is_dir():
            raise ValueError("Project root must be a directory")
        entries = registered_entries(root, args.index)
        view = (index_view(entries, args.query, args.scope, args.limit) if args.command == "index"
                else read_view(root, entries, args.identifier, args.start, args.limit))
        print(encoded(view))
        return 0
    except (OSError, ValueError, RecursionError) as error:
        parser.exit(2, f"Context reader: {error}\n")


if __name__ == "__main__":
    raise SystemExit(main())
