"""Server-owned candidate snapshot and public review without media preparation."""
from __future__ import annotations

import copy
import hashlib
import json
import math

from . import campaign, sent_registry
from .campaign_types import CampaignConfig


def registry_fingerprint() -> str:
    return hashlib.sha256(json.dumps(sent_registry.load(), sort_keys=True).encode()).hexdigest()


def available_seconds(review: list[dict], emails: list[str]) -> dict[str, float]:
    """Count unique source footage per eligible account, across categories."""
    ranges = {email: {} for email in emails}
    for row in review:
        duration = float(row.get("duration_s") or 0)
        if not math.isfinite(duration) or duration <= 0:
            continue
        window = row.get("window_s")
        parent = row.get("parent_video_uid")
        if (parent and isinstance(window, (list, tuple)) and len(window) == 2
                and all(type(value) in (int, float) and math.isfinite(value) for value in window)
                and window[1] > window[0]):
            identity, interval = ("parent", parent), (window[0], window[1])
        else:
            identity, interval = ("clip", row["clip_uid"]), (0, duration)
        for email in row.get("eligible_accounts", []):
            if email in ranges:
                ranges[email].setdefault(identity, []).append(interval)
    seconds = {}
    for email, sources in ranges.items():
        total = 0.0
        for intervals in sources.values():
            previous_end = float("-inf")
            for start, end in sorted(intervals):
                total += max(0, end - max(start, previous_end))
                previous_end = max(previous_end, end)
        seconds[email] = total
    return seconds


def build(config: CampaignConfig) -> tuple[dict, list[dict], str]:
    registry = sent_registry.load()
    fingerprint = hashlib.sha256(json.dumps(registry, sort_keys=True).encode()).hexdigest()
    pools = {}
    review = []
    for task in config.tasks:
        candidates = campaign.automatic_candidates(task, config)
        pools[task.task_id] = copy.deepcopy(candidates)
        for clip in candidates:
            uid = clip["clip_uid"]
            identities = [uid, *clip.get("dedup_clip_uids", [])]
            recorded = set().union(*(sent_registry.recorded_emails(registry, task.registry_key, identity)
                                     for identity in identities))
            pending = set().union(*(set(config.recovery_exclusions.get(identity, [])) for identity in identities))
            excluded = recorded | pending
            review.append({
                "task_id": task.task_id, "task": task.task_label or task.task_name or task.scenario,
                "clip_uid": uid, "duration_s": float(clip.get("dur_s") or 0),
                "parent_video_uid": clip.get("parent_video_uid"),
                "window_s": list(clip["window_s"]) if clip.get("window_s") else None,
                "imu_refined_from": clip.get("imu_refined_from"),
                "eligible_accounts": [a.email for a in config.accounts if a.email not in excluded],
                "excluded_accounts": [a.email for a in config.accounts if a.email in excluded],
                "pending_accounts": [a.email for a in config.accounts if a.email in pending],
                "exclusion_reason": "pending_recovery" if pending else "already_recorded",
            })
    return pools, review, fingerprint
