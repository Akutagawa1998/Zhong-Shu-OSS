"""Compare local claim files with append-ordered ownership history, and compute
lease liveness from the holder session's activity since its claim (expiry is sticky).

No filesystem mutation, cross-clone locking, or automatic ownership recovery.
"""
import datetime as dt
import math

# A resource lease (something outside the hub, such as a shared document section)
# lives in claims/@NAME and its events carry `resource` instead of `thread`. Thread
# slugs start with a letter or digit, so the two kinds of key never collide.
RESOURCE_PREFIX = "@"


def lease_key(event):
    """Ownership key of a claim/release event: the thread slug or '@NAME'; None for neither."""
    if event.get("thread"):
        return event["thread"]
    if event.get("resource"):
        return RESOURCE_PREFIX + event["resource"]
    return None


def forced(event):
    # A --takeover of an expired lease records previous_owner and reason too, so it
    # is treated exactly like a forced recovery.
    return bool(event.get("reason")) and "previous_owner" in event


def same_owner(left, right):
    return (left.get("session"), left.get("agent")) == (right.get("session"), right.get("agent"))


def ownership_history(events):
    states = {}
    for event in events:
        slug = lease_key(event)
        if event.get("kind") not in {"claim", "release"} or not slug:
            continue
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
    """disk_claims maps each present directory (thread slug or '@NAME') to {metadata, problem}."""
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


def parse_time(value):
    """A stored ISO time with an offset, as an aware datetime; None when unreadable."""
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def session_activity(events):
    """Per session, its activity as [start, end, thread] spans (ingestion time, ts). An event of any kind is
    an instant (ts, ts, None); a run also spans from its run_started to its terminal run event (end None
    while unfinished) on its thread, so a long `rmem run` keeps the lease on that thread live."""
    activity, open_runs = {}, {}
    for event in events:
        session = event.get("session")
        when = parse_time(event.get("ts")) if session else None
        if when is None:
            continue
        spans = activity.setdefault(session, [])
        run = (session, event.get("run_id"))
        if event.get("kind") == "run_started" and event.get("run_id"):
            open_runs[run] = len(spans)
            spans.append([when, None, event.get("thread")])
            continue
        if event.get("kind") == "run" and run in open_runs:
            spans[open_runs.pop(run)][1] = when
        spans.append([when, when, None])
    return activity


def valid_ttl(value):
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and value > 0)


def lease_state(meta, activity, default_ttl, now, key=None):
    """Liveness of one lease (claims/<key>/meta.json); legacy claims without lease_ttl_hours use the default.
    The lease starts at its claim and stays live while the holder session's activity since then has no gap
    longer than the TTL. Expiry is sticky: once a gap exceeds the TTL, a later event of the holder does not
    revive the lease (the holder claims again, which records a new claim). The holder's own run on the
    leased thread counts as activity from its start to its end, or to now while it is unfinished."""
    ttl = meta.get("lease_ttl_hours") if valid_ttl(meta.get("lease_ttl_hours")) else default_ttl
    span = dt.timedelta(hours=ttl)
    since = parse_time(meta.get("ts"))
    spans = []
    for start, end, thread in activity.get(meta.get("session"), ()):
        if thread is not None and thread != key:
            end = start  # a run on another thread is activity only at its start (and its end event)
        elif end is None:
            end = now
        if since is not None and end < since:
            continue
        spans.append((max(start, since) if since is not None else start, end))
    spans.sort()
    last = since if since is not None else (spans[0][0] if spans else now)
    expired = None
    for start, end in spans:
        if start - last > span:
            expired = last + span
            break
        last = max(last, end)
    expires = expired or last + span
    # WHY: after a lapse the holder may keep working; show that, so a lapsed lease is not mistaken for an idle one.
    holder_last = max([last] + [end for _, end in spans])
    return {"ttl_hours": ttl, "since": since, "last_activity": last, "expires_at": expires,
            "holder_last_activity": holder_last, "live": expired is None and now <= expires}
