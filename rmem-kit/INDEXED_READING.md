# Layered, readable context recovery

Keep STATE near **3 KB of UTF-8 text** and each active thread near **1–3 KB**.
These are editorial targets, not new rejection thresholds. `rmem check` retains
the existing hard caps: STATE 120 lines / 8,000 bytes; threads 80 lines / 6,000
bytes. Explain unfamiliar abbreviations and keep normal spaces, sentences and
paragraphs. Do not delete spaces, flatten a document into dense lines, or remove
failures and caveats to fit a cap. Move details behind explicit indexed pointers.

After the project's short entry, registered current-context instructions and
hub status, recover in this order:

1. **Metadata index:** identify the relevant topic/thread using summaries and
   locations without opening all documents.
2. **Relevant thread:** read its short question, current result, caveat and next
   step. Follow its experiment IDs instead of loading every thread.
3. **Catalog / show:** select an exact experiment or event and inspect its record,
   corrections and evidence locations.
4. **Exact evidence:** open only the necessary registered source and line range.
   Registration and file existence do not establish scientific validity or
   freshness; verify hashes and later corrections where the claim requires them.

Missing metadata is a gap to report and repair under `project-state`, not a reason
to scan all files. Keep one canonical writable index and use stable IDs. A project
may provide a richer gateway; this kit does not import or infer its active context.

## Optional generic reader

`bin/context_reader.py` is a read-only gateway with explicit `--root` and `--index`.
It neither searches directories nor reads an active-context registry. Copy the
empty `CONTEXT_INDEX_TEMPLATE.json` to a project-owned `CONTEXT_INDEX.json`, then
register selected sources under the appropriate shared-state claim:

```json
{
  "schema_version": 1,
  "entries": [
    {"id": "baseline-thread", "path": ".research-memory/threads/baseline.md", "scope": "threads", "summary": "Baseline question, current evidence and next step"},
    {"id": "baseline-readout", "path": "evidence/readout.md", "scope": "evidence", "summary": "Selected baseline readout; follow the experiment's corrections"}
  ]
}
```

Paths are relative to the explicitly selected project root. IDs must be unique;
scope labels belong to the project. Only `id`, `path`, `scope` and `summary` are
used for metadata lookup. Initialization ships the empty template, not a populated
index or any project registration.

From an installed project's root:

```bash
python3 .research-memory/bin/context_reader.py --root . --index .research-memory/CONTEXT_INDEX.json index --query baseline --scope threads
python3 .research-memory/bin/context_reader.py --root . --index .research-memory/CONTEXT_INDEX.json read baseline-thread --start 1 --limit 40
python3 .research-memory/bin/rmem catalog --thread baseline -n 5
python3 .research-memory/bin/rmem show EXACT_EVENT_ID --section sources
python3 .research-memory/bin/context_reader.py --root . --index .research-memory/CONTEXT_INDEX.json read baseline-readout --start 20 --limit 30
```

The metadata query opens the index only. `read` accepts an exact registered ID or
path, preferring an exact ID; an unindexed path fails even when the file exists.
There is no repository-wide or historical fallback. Absolute paths, traversal,
symlinks, nonregular sources and oversized inputs are rejected. UTF-8 source text
is limited to 16 MiB; create a provenance-preserving excerpt for larger logs.
The index is limited to 256 KiB and 1,000 entries. Metadata limits default to 15
rows (maximum 50); reads default to 40 lines (maximum 100). Individual lines are
clipped at 1,000 characters and labeled; output stays within 12,000 characters
with pagination hints. File byte limits are distinct from these output limits.

## What the refusal means

An unindexed read is **rejected by this reader**. The tool does not install an OS
sandbox, revoke shell/editor access, stop a user from running `cat`/`rg`, or force
an agent to use it. File access can still occur through other permitted tools.
Project entry instructions establish the cooperative reading policy; filesystem
permissions are a separate mechanism. The reader also does not verify scientific
acceptance, manage ownership, change controllers, or register evidence itself.
