"""Ordenação de candidatos já validados, sem alterar elegibilidade ou metadados."""
from collections import deque
from typing import Any


def parent_key(clip: dict[str, Any]) -> tuple[str, str]:
    return (str(clip.get("source") or "ego4d"),
            str(clip.get("parent_video_uid") or clip.get("clip_uid") or clip.get("s3_path") or ""))


def diverse_order(clips: list[dict[str, Any]], *,
                  used_parents: set[tuple[str, str]] | None = None) -> list[dict[str, Any]]:
    """Uma janela por vídeo-pai a cada rodada; preserva prioridade dentro do pai.

    A entrada já define a preferência por mídia exportada/duração. Esta função
    não consulta disco, não descarta candidatos e não altera suas janelas.
    """
    parents: dict[tuple[str, str], deque] = {}
    for index, clip in enumerate(clips):
        key = parent_key(clip)
        if not key[1]:
            key = (key[0], f"unknown-{index}")
        parents.setdefault(key, deque()).append(clip)
    used_parents = used_parents or set()
    active = [queue for key, queue in sorted(parents.items(), key=lambda item: item[0] in used_parents)]
    ordered = []
    while active:
        next_round = []
        for queue in active:
            ordered.append(queue.popleft())
            if queue:
                next_round.append(queue)
        active = next_round
    return ordered


def diversity_summary(clips: list[dict[str, Any]]) -> dict[str, Any]:
    by_source: dict[str, set[str]] = {}
    unknown = 0
    for clip in clips:
        source = str(clip.get("source") or "ego4d")
        parent = str(clip.get("parent_video_uid") or "")
        if not parent:
            unknown += 1
            continue
        by_source.setdefault(source, set()).add(parent)
    return {"parent_video_count": sum(len(parents) for parents in by_source.values()),
            "parent_video_sources": {source: len(parents) for source, parents in by_source.items()},
            "unknown_parent_count": unknown}
