# Import an existing research project

Use one coordinator and an empty project hub. Keep sources read-only until the
import is verified. Inventory, interpretation, and enabling execution are separate
actions; importing history never starts jobs or enables automation.

1. **Inventory before reading.** Use `rg --files --hidden` with cache/VCS/model
   exclusions and file sizes. Identify code repos, source artifacts, snapshots,
   conversation exports, and canonical project configuration. Record roles in
   MAP. Inspect archive members before extracting anything; do not unpack an old
   snapshot over newer work. Mark uninspected content.
   Inventory active AGENTS/CLAUDE/override files too: linking leaves old contents
   intact. The new repository needs concise current instructions, with historical
   instructions archived as evidence, and a fresh-session loading check.
2. **Establish authority.** Locate the current project entry point, canonical
   mode/owner/host constraints, and supersession records. A newer mtime, larger
   entry number, folder name, or copied handoff is not proof of authority.
   Record contradictions and source paths/hashes. Distinguish operating holds
   from old job results or unfinished proposed plans.
3. **Read progressively.** Read short entry points/current status first, then the
   specific config, metric, or readout needed for each claim. Slice long files;
   write one bounded source summary before the next folder. Independent agents
   may inspect separate folders when permitted, but must cite evidence and
   uncertainty. Do not delegate competing STATE writes or concurrent imports.
4. **Preserve identities and provenance.** Do not renumber historical experiments.
   Preserve dataset, model, seed, config, code revision, attempt identity, outcome,
   and caveats when established. If original revision is unknown, leave it unknown;
   the importer's HEAD is not the experiment's code. Distinguish transcripts'
   reports from raw measured data.
5. **Append identifiable imports.** Use a stable namespace and record identity,
   with the actual evidence files:

   ```bash
   python3 bin/rmem --agent codex --session import-session-unique log --kind import --source-namespace old-project --source-id D12-readout --id D12 --status inconclusive --source /path/to/readout.md --evidence-level reported --note 'Reported outcome; config alignment remains unresolved.'
   ```

   Repeat `--source` for relevant files. Identical retries must not duplicate
   imports. Conflicting evidence under the same identity needs a separately
   identified correction referencing the superseded record. Do not label an
   ambiguous result `keep` or invent metrics.
6. **Build the recovery view.** Hold `project-state` while drafting STATE/MAP.
   Preserve current permissions, paused hosts, and controller/heartbeat ownership.
   Claim each thread while writing its hypothesis, evidence, dead ends, blockers,
   and next step. Choose threads from the research; do not impose a fixed number.
   Put unsettled scope decisions in open questions.
7. **Validate before takeover.** Run `check`; resolve corruption warnings.
   Verify imported metrics against sources. Reconcile duplicate snapshots and
   superseded claims. Test release/reclaim between distinct sessions. Using only
   STATE, a thread, and cited evidence, a fresh reader must recover the same goal,
   constraints, pending jobs, and next step. Move a disposable project copy and
   verify pointers/artifacts resolve. Hash-check files copied to the final repo.
8. **Finish with an honest receipt.** Report source coverage, records imported,
   unresolved conflicts, inaccessible artifacts, and execution authorization.
   Save a reviewed Git snapshot. Keep migration acceptance separate from research
   execution acceptance; do not create an extra scheduler heartbeat.

After migration, prefer project-relative artifact paths. Preserve old source
locations as provenance and map them to new paths in MAP; historical hashes remain
useful after a path moves. Audit reports are not live state.
