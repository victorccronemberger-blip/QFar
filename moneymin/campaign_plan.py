"""Server-owned candidate snapshot and public review without media preparation."""
from __future__ import annotations

import copy
import hashlib
import json
import math

from . import campaign, ego4d, sent_registry
from .campaign_types import CampaignConfig


def registry_fingerprint() -> str:
    return hashlib.sha256(json.dumps(sent_registry.load(persist_seed=False), sort_keys=True).encode()).hexdigest()


def available_seconds(review: list[dict], emails: list[str]) -> dict[str, float]:
    """Count unique source footage per eligible account, across categories."""
    ranges = {email: {} for email in emails}
    for row in review:
        duration = float(row.get("duration_s") or 0)
        if not math.isfinite(duration) or duration <= 0:
            continue
        window = campaign._clip_window(row)
        parent = row.get("parent_video_uid")
        if (parent and isinstance(window, (list, tuple)) and len(window) == 2
                and all(type(value) in (int, float) and math.isfinite(value) for value in window)
                and window[1] > window[0]):
            identity, interval = (str(row.get("source") or "ego4d"), "parent", parent), (window[0], window[1])
        else:
            identity, interval = (str(row.get("source") or "ego4d"), "clip", row["clip_uid"]), (0, duration)
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


def capacity(review: list[dict], emails: list[str], *, target_seconds: float = 0,
             count_per_task: int | None = None) -> dict:
    """Estimate new footage without promising preparation or remote acceptance.

    Estimate a usable subset, keeping longer candidates first and applying
    the engine's material-overlap rule per account. The hours goal measures
    admitted delivery durations; small permitted overlaps do not erase a
    delivery. Unique source seconds are reported separately. This does not
    change the reviewed pool or the engine's schedule.
    """
    if not math.isfinite(target_seconds) or target_seconds < 0:
        raise ValueError("meta de duração inválida")
    emails = list(dict.fromkeys(emails))
    upper_bounds = available_seconds(review, emails)
    accounts = []
    for email in emails:
        eligible = [row for row in review if email in row.get("eligible_accounts", ())
                    and math.isfinite(float(row.get("duration_s") or 0))
                    and float(row.get("duration_s") or 0) > 0]
        eligible.sort(key=lambda row: (-float(row["duration_s"]),
                                      str(row.get("clip_uid") or ""), str(row.get("task_id") or "")))
        kept, by_parent, by_uid, task_sends = [], {}, {}, {}
        for row in eligible:
            task_id = row.get("task_id")
            if count_per_task is not None and task_sends.get(task_id, 0) >= count_per_task:
                continue
            source = str(row.get("source") or "ego4d")
            parent = row.get("parent_video_uid")
            related = list(by_parent.get((source, parent), ())) if parent else []
            identities = [row["clip_uid"], *row.get("dedup_clip_uids", ())]
            for uid in identities:
                related.extend(by_uid.get((source, uid), ()))
            if any(campaign._batch_footage_overlaps(other, row) for other in related):
                continue
            kept.append(row)
            task_sends[task_id] = task_sends.get(task_id, 0) + 1
            if parent:
                by_parent.setdefault((source, parent), []).append(row)
            for uid in identities:
                by_uid.setdefault((source, uid), []).append(row)
        unique_seconds = available_seconds(kept, [email])[email]
        new_seconds = sum(float(row["duration_s"]) for row in kept)
        accounts.append({
            "email": email, "available_seconds": new_seconds,
            "unique_footage_seconds": unique_seconds,
            "unique_footage_upper_bound_seconds": upper_bounds[email],
            "estimated_executable_seconds": new_seconds,
            "shortfall_seconds": max(0.0, target_seconds - new_seconds),
            "eligible_clips": len(eligible), "estimated_sends": len(kept),
            "recorded_clips": sum(email in row.get("recorded_accounts", [
                account for account in row.get("excluded_accounts", ())
                if account not in row.get("pending_accounts", ())]) for row in review),
            "pending_clips": sum(email in row.get("pending_accounts", ()) for row in review),
        })
    available = [row["available_seconds"] for row in accounts]
    shortfall = sum(row["shortfall_seconds"] > 1e-6 for row in accounts)
    return {
        "schema": 1, "known": True, "basis": "estimated_admissible_delivery_duration_before_preparation",
        "target_seconds_per_account": target_seconds,
        "can_reach_goal": bool(accounts) and not shortfall,
        "available_seconds_min": min(available, default=0.0),
        "available_seconds_max": max(available, default=0.0),
        "total_available_seconds": sum(available),
        "shortfall_account_count": shortfall,
        "estimated_sends": sum(row["estimated_sends"] for row in accounts),
        "candidate_clips": len(review), "accounts": accounts,
        "pending_acquisition_clips": sum(row.get("acquisition_required") is True for row in review),
        "requires_measured_validation": any(row.get("requires_measured_validation") is True for row in review),
        "campaign_ready": False,
    }


def capacity_error(summary: dict) -> str:
    accounts = summary.get('accounts', [])
    history = max((row.get('recorded_clips', 0) for row in accounts), default=0)
    pending = max((row.get('pending_clips', 0) for row in accounts), default=0)
    diagnosis = (f"Histórico: até {history} recorte(s) já enviado(s) por conta; "
                 f"pendências: até {pending} recorte(s) reservado(s) por conta. "
                 if history or pending else
                 "Nenhum recorte desta seleção foi excluído pelo histórico ou por pendências; "
                 "o Reset não aumentará essas horas. ")
    return (f"Conteúdo novo insuficiente para a meta em {summary['shortfall_account_count']} conta(s): "
            f"a seleção oferece até {summary['available_seconds_min'] / 3600:.2f}–"
            f"{summary['available_seconds_max'] / 3600:.2f} h por conta, para uma meta de "
            f"{summary['target_seconds_per_account'] / 3600:g} h. "
            + diagnosis + "Selecione mais categorias, reveja a faixa de duração ou reduza a meta. "
            "Vídeos já enviados e envios pendentes não contam como conteúdo novo.")


@ego4d.selection_boundary
def build(config: CampaignConfig, *, catalog_only: bool = False) -> tuple[dict, list[dict], str]:
    registry = sent_registry.load(persist_seed=False)
    fingerprint = hashlib.sha256(json.dumps(registry, sort_keys=True).encode()).hexdigest()
    pools = {}
    review = []
    for task in config.tasks:
        candidates = campaign.automatic_candidates(
            task, config, **({"catalog_only": True} if catalog_only else {}))
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
                "source": str(clip.get("source") or "ego4d"),
                "dedup_clip_uids": list(clip.get("dedup_clip_uids") or ()),
                "parent_video_uid": clip.get("parent_video_uid"),
                "window_s": list(clip["window_s"]) if clip.get("window_s") else None,
                "device_window_ns": list(clip["device_window_ns"]) if clip.get("device_window_ns") else None,
                "source_clock_domain": clip.get("source_clock_domain"),
                "window_origin_device_timestamp_ns": clip.get("window_origin_device_timestamp_ns"),
                "capacity_kind": clip.get("capacity_kind", "estimate_before_preparation"),
                "readiness": clip.get("readiness", "pending_preparation"),
                "acquisition_required": clip.get("acquisition_required") is True,
                "requires_measured_validation": clip.get("requires_measured_validation") is True,
                "imu_refined_from": clip.get("imu_refined_from"),
                "eligible_accounts": [a.email for a in config.accounts if a.email not in excluded],
                "excluded_accounts": [a.email for a in config.accounts if a.email in excluded],
                "recorded_accounts": [a.email for a in config.accounts if a.email in recorded],
                "pending_accounts": [a.email for a in config.accounts if a.email in pending],
                "exclusion_reason": "pending_recovery" if pending else "already_recorded",
            })
    return pools, review, fingerprint
