"""Pré-aquecimento retomável do Ego4D, com orçamento escolhido em GB.

O cache recebe primeiro os clipes que a campanha já aceita. O que ainda cabe
entra por cenário compatível e higiene, e só passa a ser usado depois de
estar no disco — a campanha não busca esse extra no Ego4D.
"""
from __future__ import annotations

import os
import shutil
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from . import config, ego4d, task_matching
from .atomic_io import load_json, save_json

DEFAULT_TASK = "Furniture Assembly"
MAX_BUDGET_GB = 2_147_483_647
DEFAULT_BUDGET_GB = 400
# Taxas medidas nos MP4 já gravados: clipe exportado, vídeo-pai e native 8 Mbit/s.
_SOURCE_EXPORTED_MIB_PER_MIN = 12
_SOURCE_PARENT_MIB_PER_MIN = 42
_NATIVE_MIB_PER_MIN = 58
_IMU_MIB_PER_MIN = 2


def data_dir() -> Path:
    return config.MEDIA_DATA_DIR / "ego4d"


def state_path() -> Path:
    return data_dir() / "warm_state.json"


def stop_path() -> Path:
    return data_dir() / "warm.stop"


def budget_path() -> Path:
    return config.DATA_DIR / "ego4d_cache_budget.json"


def budget_bytes(budget_gb: int) -> int:
    if isinstance(budget_gb, bool) or int(budget_gb) != float(budget_gb):
        raise ValueError("cache deve ser um número inteiro de GB")
    if not 0 <= int(budget_gb) <= MAX_BUDGET_GB:
        raise ValueError("tamanho de cache inválido")
    return int(budget_gb) * 1024 ** 3


def remember_budget(budget_gb: int) -> dict[str, Any]:
    payload = {
        "budget_gb": int(budget_gb),
        "bytes": budget_bytes(budget_gb),
    }
    save_json(budget_path(), payload)
    return payload


def configured_budget_gb(default: int = 0) -> int:
    """0 quando a campanha ainda deve usar só o recorte estrito."""
    payload = load_json(budget_path(), {"budget_gb": default})
    if not isinstance(payload, dict):
        return 0
    try:
        raw = payload["budget_gb"] if "budget_gb" in payload else int(payload.get("blocks") or 0) * 500
        budget_bytes(raw)
        budget_gb = int(raw)
    except (TypeError, ValueError, OverflowError):
        return 0
    if not 1 <= budget_gb <= MAX_BUDGET_GB:
        return 0
    return budget_gb


def storage_limits(
    requested_gb: int, *, min_free_gb: float = 50,
    work_dir: Path | None = None,
) -> dict[str, Any]:
    """Teto total = cache existente + espaço livre acima da reserva, no mesmo volume."""
    import math

    requested_bytes = budget_bytes(requested_gb)
    if not math.isfinite(min_free_gb) or min_free_gb < 0:
        raise ValueError("reserva de disco inválida")
    root = Path(work_dir or data_dir()).resolve()
    existing = root
    while not existing.exists():
        existing = existing.parent
    free = shutil.disk_usage(existing).free
    used = used_bytes(root)
    available = max(0, free - int(min_free_gb * 1024 ** 3))
    maximum_gb = min(MAX_BUDGET_GB, (used + available) // 1024 ** 3)
    effective_gb = min(int(requested_gb), maximum_gb)
    return {
        "requested_budget_gb": int(requested_gb),
        "budget_gb": effective_gb,
        "budget_bytes": min(requested_bytes, effective_gb * 1024 ** 3),
        "max_budget_gb": maximum_gb,
        "free_gb": round(free / 1024 ** 3, 1),
        "min_free_gb": min_free_gb,
        "used_bytes": used,
        "used_gb": round(used / 1024 ** 3, 1),
        "cache_mode": "provider" if requested_gb == 0 else "cache",
    }


def used_bytes(work_dir: Path | None = None) -> int:
    """Bytes de mídia Ego4D. Catálogo, estado e HoloAssist ficam de fora."""
    root = Path(work_dir or data_dir())
    if not root.is_dir():
        return 0
    total = 0
    for path in root.iterdir():
        if not path.is_file():
            continue
        name = path.name.lower()
        if name.startswith("holoassist_"):
            continue
        if name.endswith(".mp4") or name.endswith("_imu.csv") or name.endswith(".source.json"):
            try:
                total += path.stat().st_size
            except OSError:
                continue
    return total


def estimate_clip_bytes(clip: dict[str, Any]) -> int:
    """Ordem de grandeza de fonte + native + IMU, para caber no orçamento."""
    minutes = max(float(clip.get("dur_s") or 0), 1.0) / 60.0
    parent = str(clip.get("parent_video_uid") or "")
    media = str(clip.get("media_uid") or parent)
    full_parent = bool(clip.get("needs_cut")) and media == parent and bool(parent)
    source_rate = _SOURCE_PARENT_MIB_PER_MIN if full_parent else _SOURCE_EXPORTED_MIB_PER_MIN
    mebibyte = 1024 ** 2
    return int(minutes * (source_rate + _NATIVE_MIB_PER_MIN + _IMU_MIB_PER_MIN) * mebibyte)


def assign_scenario_clips(
    clips: Iterable[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    """Uma tarefa por clipe: melhor cenário, empate fica na regra mais específica já listada.

    Higiene de ban continua obrigatória. Sem evidência da ação o clipe não
    entra no recorte estrito; ele só completa o cache e a campanha só o usa
    depois de gravado.
    """
    buckets: dict[str, list[dict[str, Any]]] = {name: [] for name in task_matching.TASK_RULES}
    rules = list(task_matching.TASK_RULES.items())
    for clip in clips:
        if task_matching.hygiene_reject_reason(clip):
            continue
        scenarios = clip.get("scenarios") or [clip.get("scenario", "")]
        best_name = ""
        best_score = -1
        for name, rule in rules:
            score = task_matching.score_scenarios(rule, scenarios)
            if score is None or score <= best_score:
                continue
            best_name = name
            best_score = score
        if not best_name:
            continue
        item = dict(clip)
        item["source"] = "ego4d"
        item["match_score"] = best_score
        item["match_tier"] = "scenario"
        buckets[best_name].append(item)
    return buckets


_scenario_cache: dict[str, list[dict[str, Any]]] | None = None


def scenario_buckets() -> dict[str, list[dict[str, Any]]]:
    global _scenario_cache
    if _scenario_cache is None:
        _scenario_cache = assign_scenario_clips(ego4d.list_clips(
            scenario=None, min_dur_s=60, max_dur_s=1800,
            require_imu=True, max_results=None))
    return _scenario_cache


def ready_scenario_clips(
    task: str,
    *,
    min_dur_s: float = 60,
    max_dur_s: float = 1800,
    work_dir: Path | None = None,
) -> list[dict[str, Any]]:
    """Clipes de cenário já normalizados. Sem orçamento gravado, a lista é vazia."""
    if configured_budget_gb() < 1:
        return []
    from .campaign import ego_clip_cache_state

    canonical = task_matching.canonical_task_name(task)
    work = Path(work_dir or data_dir())
    ready: list[dict[str, Any]] = []
    for clip in scenario_buckets().get(canonical, []):
        dur = float(clip.get("dur_s") or 0)
        if not min_dur_s <= dur <= max_dur_s:
            continue
        if ego_clip_cache_state(clip, work) == "ready":
            ready.append(clip)
    return ready


def task_names() -> list[str]:
    """As mesmas tarefas Minute que a campanha sabe casar com o Ego4D."""
    return sorted(task_matching.TASK_RULES)


def catalog_installed() -> bool:
    root = data_dir()
    meta = root / "ego4d.json"
    clips = root / "clips.csv"
    try:
        return (
            meta.is_file() and meta.stat().st_size > 0
            and clips.is_file() and clips.stat().st_size > 0
        )
    except OSError:
        return False


def eligible_clips(
    task: str = DEFAULT_TASK,
    *,
    min_dur_s: float = 60,
    max_dur_s: float = 1800,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Pool da tarefa, na mesma ordem de preferência da campanha."""
    if not catalog_installed():
        raise RuntimeError(
            "catálogo Ego4D ainda não está instalado; "
            "prepare-o na aba Integrações antes de aquecer o cache"
        )
    canonical = task_matching.canonical_task_name(task)
    if task_matching.rule_for(canonical) is None:
        raise ValueError("tarefa Ego4D inválida")
    # Import tardio: campaign importa ego4d e o acelerador não pode fechar ciclo.
    from .campaign import _compatible_task_clips

    pool = _compatible_task_clips(
        canonical, "ego4d", min_dur_s=min_dur_s, max_dur_s=max_dur_s)
    ordered = ego4d.prefer_long_clips(list(pool), shuffle=False)
    return ordered[:max(0, int(limit))] if limit is not None else ordered


def allocation_plan(
    task: str = DEFAULT_TASK,
    *,
    min_dur_s: float = 60,
    max_dur_s: float = 1800,
    limit: int | None = None,
    budget_gb: int = DEFAULT_BUDGET_GB,
) -> list[dict[str, Any]]:
    """Recorte estrito primeiro, depois cenário, até caber no orçamento em GB."""
    priority = task_matching.canonical_task_name(task)
    names = [priority, *[name for name in task_names() if name != priority]]
    ordered: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(clip: dict[str, Any]) -> None:
        uid = str(clip.get("clip_uid") or "")
        dur = float(clip.get("dur_s") or 0)
        if not uid or uid in seen or not min_dur_s <= dur <= max_dur_s:
            return
        seen.add(uid)
        ordered.append(clip)

    for name in names:
        try:
            batch = eligible_clips(name, min_dur_s=min_dur_s, max_dur_s=max_dur_s)
        except ValueError:
            continue
        for clip in batch:
            add(clip)
    buckets = scenario_buckets()
    for name in names:
        for clip in buckets.get(name, []):
            add(clip)

    budget = budget_bytes(budget_gb)
    fitted: list[dict[str, Any]] = []
    accrued = 0
    for clip in ordered:
        need = estimate_clip_bytes(clip)
        if accrued + need > budget:
            continue
        fitted.append(clip)
        accrued += need
        if accrued >= budget:
            break
    if limit is not None:
        fitted = fitted[:max(0, int(limit))]
    return fitted


def _states(
    clips: list[dict[str, Any]], work_dir: Path | None,
) -> list[str]:
    from .campaign import ego_clip_cache_state

    work = Path(work_dir or data_dir())
    return [ego_clip_cache_state(clip, work) for clip in clips]


def cache_status(
    task: str = DEFAULT_TASK,
    *,
    min_dur_s: float = 60,
    max_dur_s: float = 1800,
    limit: int | None = None,
    budget_gb: int | None = None,
    min_free_gb: float = 50,
    work_dir: Path | None = None,
) -> dict[str, Any]:
    storage = None
    if budget_gb is not None:
        storage = storage_limits(budget_gb, min_free_gb=min_free_gb, work_dir=work_dir)
        budget_gb = storage["budget_gb"]
    if budget_gb is None or int(budget_gb) == 0:
        clips = eligible_clips(
            task, min_dur_s=min_dur_s, max_dur_s=max_dur_s, limit=limit)
    else:
        clips = allocation_plan(
            task, min_dur_s=min_dur_s, max_dur_s=max_dur_s,
            limit=limit, budget_gb=budget_gb)
    states = _states(clips, work_dir)
    ready = states.count("ready")
    partial = states.count("partial")
    previous = load_json(state_path(), {})
    payload: dict[str, Any] = {
        "provider": "ego4d",
        "task": task_matching.canonical_task_name(task),
        "total": len(clips),
        "ready": ready,
        "partial": partial,
        "pending": len(clips) - ready,
        "last_run": previous,
    }
    if storage is not None:
        payload.update(storage)
    return payload


def request_stop() -> Path:
    path = stop_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("stop\n", encoding="utf-8")
    return path


def warm_cache(
    task: str = DEFAULT_TASK,
    *,
    min_dur_s: float = 60,
    max_dur_s: float = 1800,
    limit: int | None = None,
    budget_gb: int | None = None,
    min_free_gb: float = 150.0,
    work_dir: Path | None = None,
    progress: Callable[[str, dict[str, Any]], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Baixa e normaliza clipes elegíveis; cache pronto é pulado."""
    from .campaign import ego_clip_cache_state, prepare_clip, _ego_clip_inputs

    work = Path(work_dir or data_dir())
    work.mkdir(parents=True, exist_ok=True)
    if budget_gb == 0:
        remember_budget(0)
        return {
            "status": "provider",
            "provider": "ego4d",
            "task": task_matching.canonical_task_name(task),
            "budget_gb": 0,
            "total": 0,
            "ready": 0,
            "skipped": 0,
            "failed": 0,
            "errors": [],
        }
    if budget_gb is not None:
        storage = storage_limits(budget_gb, min_free_gb=min_free_gb, work_dir=work)
        budget_gb = storage["budget_gb"]
        if budget_gb == 0:
            return {"status": "disk_limit", "provider": "ego4d", **storage,
                    "total": 0, "ready": 0, "failed": 0, "skipped": 0, "errors": []}
    budget = budget_bytes(budget_gb) if budget_gb is not None else None
    if budget_gb is not None:
        remember_budget(budget_gb)
    # Limpa apenas a parada da execução anterior, antes do catálogo demorado.
    stop_path().unlink(missing_ok=True)
    if budget_gb is None:
        clips = eligible_clips(
            task, min_dur_s=min_dur_s, max_dur_s=max_dur_s, limit=limit)
    else:
        clips = allocation_plan(
            task, min_dur_s=min_dur_s, max_dur_s=max_dur_s,
            limit=limit, budget_gb=budget_gb)
    canonical = task_matching.canonical_task_name(task)
    started = time.time()
    state: dict[str, Any] = {
        "status": "running",
        "provider": "ego4d",
        "pid": os.getpid(),
        "task": canonical,
        "budget_gb": budget_gb,
        "budget_bytes": budget,
        "total": len(clips),
        "ready": 0,
        "skipped": 0,
        "failed": 0,
        "current": None,
        "started_at": started,
        "updated_at": started,
        "errors": [],
    }

    def emit(kind: str, **payload: Any) -> None:
        if progress:
            progress(kind, payload)

    def persist() -> None:
        state["updated_at"] = time.time()
        save_json(state_path(), state)

    persist()
    try:
        for index, clip in enumerate(clips, 1):
            if stop_path().exists() or (should_stop is not None and should_stop()):
                state["status"] = "stopped"
                break
            if budget is not None and used_bytes(work) >= budget:
                state["status"] = "budget"
                state["used_bytes"] = used_bytes(work)
                break
            name = str(clip.get("clip_uid") or clip.get("video_name") or "")
            state["current"] = name
            state["index"] = index
            persist()
            if ego_clip_cache_state(clip, work) == "ready":
                state["ready"] += 1
                state["skipped"] += 1
                emit("cached", index=index, total=len(clips), video_name=name)
                persist()
                continue
            # Não inicie outro clipe se a estimativa já excede o espaço restante.
            # O tamanho final varia com a fonte e com o encode.
            if budget is not None:
                used = used_bytes(work)
                if used + estimate_clip_bytes(clip) > budget:
                    state["status"] = "budget"
                    state["used_bytes"] = used
                    break
            free_gb = shutil.disk_usage(work).free / 1024 ** 3
            if free_gb - estimate_clip_bytes(clip) / 1024 ** 3 < min_free_gb:
                state["status"] = "disk_limit"
                state["free_gb"] = round(free_gb, 2)
                break
            emit("start", index=index, total=len(clips), video_name=name,
                 free_gb=free_gb)
            try:
                row, video = _ego_clip_inputs(clip)
                if row is None or video is None:
                    raise RuntimeError(f"clipe {name} não está no catálogo Ego4D")

                def preparation_progress(
                    phase: str,
                    data: dict[str, Any],
                    *,
                    current_index: int = index,
                    current_name: str = name,
                ) -> None:
                    phase_data = dict(data)
                    phase_current = phase_data.pop("current", None)
                    phase_total = phase_data.pop("total", None)
                    emit(
                        "phase", index=current_index, total=len(clips),
                        video_name=current_name, phase=phase,
                        phase_current=phase_current, phase_total=phase_total,
                        **phase_data)

                prepare_clip(row, video, work, progress=preparation_progress)
            except Exception as exc:  # noqa: BLE001 — registra e avança
                state["failed"] += 1
                state["errors"] = [
                    *state["errors"][-49:],
                    {"video_name": name, "error": f"{type(exc).__name__}: {exc}"},
                ]
                emit("failed", index=index, total=len(clips), video_name=name,
                     error=str(exc))
            else:
                state["ready"] += 1
                emit("done", index=index, total=len(clips), video_name=name)
            persist()
        else:
            state["status"] = "complete"
    except KeyboardInterrupt:
        state["status"] = "stopped"
    finally:
        state["current"] = None
        state["elapsed_s"] = round(time.time() - started, 1)
        persist()
    return state


__all__ = [
    "DEFAULT_BUDGET_GB",
    "DEFAULT_TASK",
    "MAX_BUDGET_GB",
    "allocation_plan",
    "assign_scenario_clips",
    "cache_status",
    "catalog_installed",
    "eligible_clips",
    "ready_scenario_clips",
    "remember_budget",
    "request_stop",
    "state_path",
    "stop_path",
    "task_names",
    "used_bytes",
    "warm_cache",
]
