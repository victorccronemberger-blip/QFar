"""Server-owned candidate snapshot and public review without media preparation."""
from __future__ import annotations

import copy
import hashlib
import json

from . import campaign, sent_registry
from .campaign_types import CampaignConfig


def registry_fingerprint() -> str:
    return hashlib.sha256(json.dumps(sent_registry.load(), sort_keys=True).encode()).hexdigest()


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
            recorded = set(registry.get(task.registry_key, {}).get(uid, []))
            review.append({
                "task_id": task.task_id, "task": task.task_label or task.task_name or task.scenario,
                "clip_uid": uid, "duration_s": float(clip.get("dur_s") or 0),
                "eligible_accounts": [a.email for a in config.accounts if a.email not in recorded],
                "excluded_accounts": [a.email for a in config.accounts if a.email in recorded],
                "exclusion_reason": "already_recorded",
            })
    return pools, review, fingerprint
