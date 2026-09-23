"""Compare local claim files with append-ordered ownership history.

No filesystem mutation, cross-clone locking, or automatic ownership recovery.
"""


def forced(event):
    return bool(event.get("reason")) and "previous_owner" in event


def same_owner(left, right):
    return (left.get("session"), left.get("agent")) == (right.get("session"), right.get("agent"))


def ownership_history(events):
    states = {}
    for event in events:
        if event.get("kind") not in {"claim", "release"} or not event.get("thread"):
            continue
        slug = event["thread"]
        state = states.setdefault(slug, {"active_claim": None, "last_event": None, "problem": ""})
        active = state["active_claim"]
        if event["kind"] == "claim":
            if forced(event):
                state["problem"] = ""
            elif active and not same_owner(active, event):
                state["problem"] = "ledger changes claim owner without a recorded force/reason"
            state["active_claim"] = event
        elif forced(event):
            state["active_claim"] = None
            state["problem"] = ""
        elif active is None:
            # Historical closed releases can predate the imported claim history.
            # They assert no current owner and are not a new ownership grant.
            pass
        elif same_owner(active, event):
            state["active_claim"] = None
            # A normal close cannot erase an earlier unauthorized transition.
            # Only an explicit forced recovery acknowledges and clears it.
        else:
            state["problem"] = "ledger release owner differs from active claim without a recorded force/reason"
        state["last_event"] = event
    return states


def claim_issues(events, disk_claims):
    """disk_claims maps each present directory to {metadata, problem}."""
    states = ownership_history(events)
    issues = []
    for slug in sorted(set(states) | set(disk_claims)):
        state = states.get(slug, {})
        active, last = state.get("active_claim"), state.get("last_event")
        disk = disk_claims.get(slug)
        reasons = []
        if state.get("problem"):
            reasons.append(state["problem"])
        if disk is not None:
            if disk.get("problem"):
                reasons.append(disk["problem"])
            meta = disk.get("metadata") or {}
            if last is None:
                reasons.append("orphan claim file: no claim/release history in this ledger")
            elif active is None:
                reasons.append("claim file remains although ledger ownership was released")
            else:
                if not active.get("session"):
                    reasons.append("active legacy ledger claim lacks a session; explicit recovery required")
                elif not same_owner(active, meta):
                    reasons.append("claim file session/agent differs from last active ledger owner")
                anchor = meta.get("claim_event_id")
                if anchor is not None and anchor != active.get("event_id"):
                    reasons.append("claim_event_id does not match the active ledger claim")
                if active.get("claim_metadata_version") == 1 and anchor is None:
                    reasons.append("new claim file is missing its claim_event_id anchor")
        elif active is not None:
            reasons.append("missing claim file: last ledger claim has not been released")
        if reasons:
            issues.append({"thread": slug, "problems": reasons,
                           "ledger_claim_event_id": active.get("event_id") if active else None,
                           "ledger_session": active.get("session") if active else None,
                           "ledger_agent": active.get("agent") if active else None})
    return issues
