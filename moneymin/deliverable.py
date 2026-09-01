"""
deliverable.py — Monta o pacote de entrega de um clipe enquadrado.

Dado um `FramingRecord` (de framing.frame_against_tasks) e a task casada do
Minute, produz em `data/output/<slug>/`:
  - cópia do vídeo,
  - `<slug>.txt` — dossiê com a documentação pertinente ao App Minute,
  - registro no manifesto (data/manifests/).

Sem envios externos: apenas escreve arquivos locais.
"""
from __future__ import annotations

import shutil
import time
from pathlib import Path
from typing import Any

from . import config, datasets, framing

# Métricas que o Minute avalia (docs/EPIC_KITCHENS_100_SPEC.md).
_QUALITY_TEXT = (
    "  Clarity  — iluminação e nitidez da cena.\n"
    "  Variety  — enquadramento de mãos e objetos (perspectiva 1ª pessoa).\n"
    "  Task     — conformidade da ação com a tarefa."
)


def probe_duration_s(video_path: str | Path) -> float | None:
    """Duração do vídeo em segundos (ffprobe / ffmpeg -i / PyAV)."""
    from .sidecar import probe_video
    ms = int(probe_video(video_path).get("duration_ms") or 0)
    return round(ms / 1000.0, 2) if ms else None


def build_dossier_text(
    record: framing.FramingRecord,
    task: dict[str, Any],
    dataset: str,
    org_name: str = "",
    actions: list[tuple[str, str, str, str]] | None = None,
    video_size: int | None = None,
    duration_s: float | None = None,
    license_note: str = "",
) -> str:
    """Gera o texto do dossiê de enquadramento."""
    ann = record.annotation
    cats = task.get("categories") or [{}]
    cat = cats[0]
    ds = datasets.CATALOG.get(dataset)
    ds_name = ds.name if ds else dataset

    conf = record.match_confidence
    verdict = "PERFEITO" if conf >= 0.95 else ("bom" if conf >= 0.5 else "BAIXO — conferir")

    action_lines = "\n".join(
        f"       {nid:11} {start} -> {stop}  {narr}" for nid, start, stop, narr in (actions or [])
    ) or "       (não informadas)"

    seg = (
        f"{ann.start_timestamp} -> {ann.stop_timestamp}"
        if ann.start_timestamp or ann.stop_timestamp else "(não informado)"
    )
    limit = task.get("limitMs")
    limit_txt = f"{limit} ms" if limit is not None else "sem limite (limitMs=null)"

    repro = ""
    if ds and ds.urls:
        repro = "\n".join(f"  {k:9}: {v}" for k, v in ds.urls.items())
    ffmpeg = ""
    if ann.start_timestamp and ann.stop_timestamp and ann.video_id:
        ffmpeg = (f"\n  recorte  : ffmpeg -ss {ann.start_timestamp} -to {ann.stop_timestamp} "
                  f"-i {ann.video_id}.MP4 -c copy {Path(record.video_path).name}")

    return f"""DOSSIÊ DE ENQUADRAMENTO — {task.get('name','').strip()}
Gerado: {time.strftime('%Y-%m-%d %H:%M:%S')}  |  Fluxo: {ds_name} -> App Minute
Modo: apenas VALIDAÇÃO + DOWNLOAD (nenhum envio externo)

================================================================
1) ENCAIXE NO APP MINUTE   (validado via API viva, GET read-only)
================================================================
  Task .............: {task.get('name','').strip()}
  Task ID ..........: {task.get('id')}
  Categoria (slug) .: {cat.get('slug')}
  Categoria (label) : {cat.get('label')}
  Tags .............: {', '.join(task.get('tags', []) or [])}
  Organização ......: {org_name}
  Prioridade .......: {task.get('priority')}
  Restrição usuário : {task.get('isUserRestricted')}
  Limite gravação ..: {limit_txt}
  Descrição oficial : {(task.get('description') or '').strip()}
  Confiança do match: {conf}  ({verdict})

================================================================
2) MÉTRICAS DE QUALIDADE AVALIADAS PELO MINUTE
================================================================
{_QUALITY_TEXT}
  (ref.: docs/EPIC_KITCHENS_100_SPEC.md)

================================================================
3) VÍDEO — PROVENIÊNCIA / DOCUMENTAÇÃO DA FONTE
================================================================
  Arquivo ..........: {Path(record.video_path).name}
  Tamanho ..........: {video_size if video_size is not None else '?'} bytes
  Duração ..........: {f'~{duration_s} s' if duration_s else '?'}  |  Tipo: video/mp4
  Dataset ..........: {ds_name}
  Participante .....: {ann.participant_id or '-'}  |  Vídeo-fonte: {ann.video_id or '-'}
  Segmento .........: {seg}
  Narração .........: {ann.narration or '-'}   (verbo={ann.verb or '-'}, substantivo={ann.noun or '-'})
  Ações anotadas (verbo+substantivo):
{action_lines}
  Licença ..........: {license_note or (ds.access_note if ds else 'ver termos oficiais do dataset')}

================================================================
4) COMO REPRODUZIR O DOWNLOAD (fonte oficial)
================================================================
{repro or '  (ver docs/datasets_egocentricos.md)'}{ffmpeg}

================================================================
5) FLUXO EXECUTADO
================================================================
  1. Provedor selecionado ....: {ds_name}
  2. Vídeo documentado .......: {ann.video_id or Path(record.video_path).stem}
  3. Validação de categoria ..: GET /api/v1/orgs/{{key}}/tasks -> task "{task.get('name','').strip()}" ({cat.get('slug')})
  4. Entrega .................: este pacote (vídeo + .txt)   — sem envios externos
"""


def build_package(
    video_path: str | Path,
    record: framing.FramingRecord,
    task: dict[str, Any],
    dataset: str,
    org_name: str = "",
    actions: list[tuple[str, str, str, str]] | None = None,
    license_note: str = "",
    out_root: Path | None = None,
) -> Path:
    """Cria o pacote de entrega e devolve o diretório gerado."""
    video_path = Path(video_path)
    if not video_path.exists():
        raise FileNotFoundError(f"vídeo não encontrado: {video_path}")

    slug = video_path.stem
    out_dir = (out_root or (config.MEDIA_DATA_DIR / "output")) / slug
    out_dir.mkdir(parents=True, exist_ok=True)

    dst = out_dir / video_path.name
    shutil.copy2(video_path, dst)
    record.video_path = str(dst)

    text = build_dossier_text(
        record, task, dataset, org_name=org_name, actions=actions,
        video_size=dst.stat().st_size, duration_s=probe_duration_s(dst),
        license_note=license_note,
    )
    (out_dir / f"{slug}.txt").write_text(text, encoding="utf-8")

    framing.save_manifest([record], f"{slug}_manifest.json")
    return out_dir
