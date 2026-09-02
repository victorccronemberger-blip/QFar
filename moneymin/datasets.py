"""
datasets.py — Catálogo de datasets de vídeo egocêntrico e utilidades de download.

Fonte da verdade estruturada para os datasets documentados em
`docs/datasets_egocentricos.md`. Cada entrada traz nome, foco, links oficiais e
nota de acesso (muitos exigem cadastro/aceite de licença antes do download).

Também oferece utilidades para baixar um arquivo por URL direta e para inspecionar
os clipes já presentes em `data/videos/`.
"""
from __future__ import annotations

import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from . import config, tls


@dataclass(frozen=True)
class Dataset:
    key: str
    name: str
    focus: str
    urls: dict[str, str] = field(default_factory=dict)
    access_note: str = ""


# Catálogo derivado de docs/datasets_egocentricos.md e docs/EPIC_KITCHENS_100_SPEC.md.
CATALOG: dict[str, Dataset] = {
    "holoassist": Dataset(
        key="holoassist",
        name="HoloAssist",
        focus="Manipulação egocêntrica guiada, com ações temporizadas, erros "
        "anotados e RGB sincronizado a IMU real do HoloLens 2.",
        urls={
            "site": "https://holoassist.github.io/",
            "instructions": "https://holoassist.github.io/data_links/README.html",
            "paper": "https://openaccess.thecvf.com/content/ICCV2023/html/"
            "Wang_HoloAssist_an_Egocentric_Human_Interaction_Dataset_for_"
            "Interactive_AI_Assistants_ICCV_2023_paper.html",
        },
        access_note="CDLA-Permissive-2.0; downloads públicos. Mídia distribuída em TARs grandes.",
    ),
    "ego4d": Dataset(
        key="ego4d",
        name="Ego4D",
        focus="Reconhecimento de ações, interação mão-objeto, rastreamento, "
        "consultas em linguagem natural e antecipação.",
        urls={
            "docs": "https://ego4d-data.org/docs/",
            "cli": "https://ego4d-data.org/docs/CLI/",
            "repo": "https://github.com/facebookresearch/Ego4d",
        },
        access_note="Exige aceite de licença e credenciais (CLI oficial ego4d).",
    ),
    "ego_exo4d": Dataset(
        key="ego_exo4d",
        name="Ego-Exo4D",
        focus="Vídeos sincronizados 1ª/3ª pessoa, pose corporal e de mãos, "
        "segmentação de objetos, áudio, descrições e dados 3D.",
        urls={
            "docs": "https://docs.ego-exo4d-data.org/",
            "download": "https://docs.ego-exo4d-data.org/download/",
        },
        access_note="Exige cadastro e configuração de acesso.",
    ),
    "epic_kitchens_100": Dataset(
        key="epic_kitchens_100",
        name="EPIC-KITCHENS-100",
        focus="Ações não roteirizadas em cozinhas; tríade Verbo+Substantivo, "
        "interação mão-objeto. Modelagem espelhada pelo app Minute.",
        urls={
            "site": "https://epic-kitchens.github.io/2020-100",
            "annotations": "https://github.com/epic-kitchens/epic-kitchens-100-annotations",
        },
        access_note="Anotações públicas no GitHub; vídeos via portal oficial.",
    ),
    "assembly101": Dataset(
        key="assembly101",
        name="Assembly101",
        focus="Montagem/desmontagem de objetos; ações detalhadas, pose 3D das "
        "mãos e identificação de erros.",
        urls={
            "site": "https://assembly-101.github.io/",
            "paper": "https://arxiv.org/abs/2203.14712",
        },
        access_note="Download via página oficial (requer formulário).",
    ),
    "charades_ego": Dataset(
        key="charades_ego",
        name="Charades-Ego",
        focus="Vídeos pareados 1ª/3ª pessoa de atividades domésticas, com "
        "localização temporal e descrições.",
        urls={
            "site": "https://prior.allenai.org/projects/charades-ego",
            "paper": "https://arxiv.org/abs/1804.09626",
        },
    ),
    "gtea": Dataset(
        key="gtea",
        name="GTEA — Georgia Tech Egocentric Activities",
        focus="Dataset pequeno para reconhecimento de atividades, objetos e "
        "manipulação em 1ª pessoa.",
        urls={
            "site": "https://cbs.ic.gatech.edu/fpv/",
            "original": "https://ai.stanford.edu/~alireza/GTEA/",
        },
    ),
}


def list_datasets() -> list[Dataset]:
    return list(CATALOG.values())


def get_dataset(key: str) -> Dataset:
    if key not in CATALOG:
        raise KeyError(f"dataset desconhecido: {key!r}. Opções: {', '.join(CATALOG)}")
    return CATALOG[key]


def download_file(url: str, dest: str | Path | None = None, chunk: int = 1 << 20) -> Path:
    """Baixa um arquivo por URL direta para `data/videos/` (ou `dest`).

    Só serve para URLs de download direto. Datasets que exigem login/licença
    devem ser baixados com a ferramenta oficial (ver `Dataset.access_note`).
    """
    if dest is None:
        config.VIDEOS_DIR.mkdir(parents=True, exist_ok=True)
        dest = config.VIDEOS_DIR / Path(urllib.parse.urlparse(url).path).name
    dest = Path(dest)
    with tls.urlopen(url) as resp, open(dest, "wb") as out:
        while True:
            block = resp.read(chunk)
            if not block:
                break
            out.write(block)
    return dest


def local_videos() -> list[Path]:
    """Clipes de vídeo presentes em `data/videos/`."""
    if not config.VIDEOS_DIR.exists():
        return []
    exts = {".mp4", ".mov", ".mkv", ".avi", ".webm"}
    return sorted(p for p in config.VIDEOS_DIR.iterdir() if p.suffix.lower() in exts)
