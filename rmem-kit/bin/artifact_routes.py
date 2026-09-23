"""Resolve explicitly registered artifact moves without searching a repository."""
import hashlib
import json
from pathlib import Path, PurePosixPath


def safe_path(root, name):
    """Repository-relative, nonsymlink paths only, including ancestor components."""
    if not isinstance(name, str) or not name or "\\" in name:
        raise ValueError("Invalid artifact path")
    rel = PurePosixPath(name)
    if rel.is_absolute() or any(x in {".", ".."} for x in name.split("/")):
        raise ValueError("Artifact paths must be normalized and repository-relative")
    root = Path(root).resolve()
    path = root
    for part in rel.parts:
        path = path / part
        if path.is_symlink():
            raise ValueError("Artifact route cannot follow a symlink")
    if not path.resolve().is_relative_to(root):
        raise ValueError("Artifact path escapes repository")
    return path


def resolve_artifact(root, original_path, expected_sha256=None, manifest=None):
    """Return a byte-verified route or None if the exact old name is unregistered.

    Historical originals outrank retired redirect pages when no hash is given.
    A caller citing a hash always gets those exact bytes, or an explicit failure.
    A route never grants a file current scientific or operational authority.
    """
    root = Path(root).resolve()
    safe_path(root, original_path)
    manifest = Path(manifest) if manifest else root / ".research-memory/ARTIFACT_ROUTES.json"
    if not manifest.is_file():
        return None
    catalog = json.loads(manifest.read_text())
    if catalog.get("schema_version") != 1:
        raise ValueError("Unsupported artifact route schema")
    rows = [x for x in catalog["files"] if x["original_path"] == original_path]
    if not rows:
        return None
    if expected_sha256:
        rows = [x for x in rows if x["sha256"] == expected_sha256]
    else:
        preferred = [x for x in rows if x.get("role") != "retired_redirect"]
        rows = preferred or rows
    identities = {(x["current_path"], x["sha256"], x["bytes"]) for x in rows}
    if len(identities) != 1:
        raise ValueError("Old path has no unique matching identity; specify its recorded SHA-256")
    name, digest, size = identities.pop()
    path = safe_path(root, name)
    data = path.read_bytes()
    if len(data) != size or hashlib.sha256(data).hexdigest() != digest:
        raise ValueError("Relocated artifact does not match its recorded bytes")
    return {"original_path": original_path, "path": str(path), "sha256": digest,
            "bytes": size, "role": rows[0].get("role"), "verified": True,
            "relative_links_base": str(PurePosixPath(original_path).parent),
            "authority": "SOURCE_IDENTITY_ONLY; current roles require ACTIVE_CONTEXT and thread registration"}
