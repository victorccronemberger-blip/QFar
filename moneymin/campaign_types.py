"""Tipos de configuração e relatório do motor de campanhas."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import config
from .atomic_io import save_json

DEFAULT_TIMEOUT_BLOB = 1200
DEFAULT_ACCOUNT_STAGGER_S = 0.0
MIN_ACCOUNT_AGE_DAYS = 2.0

# Chaves de item que guardam caminhos de arquivo — gravadas relativas ao ROOT
# no log salvo (portátil, sem vazar diretórios do usuário).
_PATH_KEYS = ("video_path", "imu_path")


def _relpath(value: Any) -> Any:
    """Caminho relativo ao ROOT; fora do ROOT, só o nome do arquivo."""
    if not isinstance(value, str) or not value:
        return value
    try:
        return Path(value).resolve().relative_to(config.ROOT.resolve()).as_posix()
    except (OSError, ValueError):
        return Path(value).name


@dataclass
class TaskSpec:
    """Uma remessa de clipe de um cenário para uma tarefa Minute."""

    task_id: str
    scenario: str
    min_dur_s: float
    max_dur_s: float
    task_name: str | None = None
    task_label: str | None = None
    task_description: str = ""
    count: int = 1
    gopro_minor: int | None = None
    clip_uids: list[str] | None = None

    @property
    def registry_key(self) -> str:
        """Dedup por tarefa Minute; configurações antigas usam o cenário."""
        if self.task_name:
            return f"minute|{self.task_id}|{self.task_label or self.task_name}"
        return self.scenario


@dataclass
class AccountSpec:
    email: str
    org_key: str


@dataclass
class CampaignConfig:
    accounts: list[AccountSpec]
    tasks: list[TaskSpec]
    work_dir: Path = field(default_factory=lambda: config.MEDIA_DATA_DIR / "ego4d")
    timeout_blob: int = DEFAULT_TIMEOUT_BLOB
    evaluate: bool = True
    finalize: bool = True
    delay_mode: str = "off"
    delay_s: float = 0
    account_gap_s: float = DEFAULT_ACCOUNT_STAGGER_S
    account_workers: int = 6
    account_max_attempts: int = 1
    account_retry_s: float = 15.0
    require_all_accounts: bool = False
    active_hours: tuple[int, int] | None = None
    share_clips: bool = True
    unique_video: bool = False
    allow_new_accounts: bool = True
    min_account_age_days: float = MIN_ACCOUNT_AGE_DAYS
    shuffle_schedule: bool = True
    target_hours_per_account: float = 0.0
    dataset_provider: str = "all"
    # Evita crescimento contínuo do disco: após TODAS as contas pendentes
    # concluírem o upload, remove mídia/IMU baixadas e derivados locais.
    cleanup_after_upload: bool = True
    # Interface web: reserva gravações reais por conta e só envia após o fim.
    # False mantém chamadas de biblioteca/testes retrocompatíveis.
    realistic_timeline: bool = False


@dataclass
class CampaignLog:
    started_at: str
    accounts: list[str]
    items: list[dict[str, Any]] = field(default_factory=list)
    _path: Path | None = field(default=None, init=False, repr=False)

    def add_item(self, item: dict[str, Any]) -> None:
        self.items.append(item)

    def to_dict(self) -> dict[str, Any]:
        return {
            "started_at": self.started_at,
            "accounts": self.accounts,
            "items": self.items,
        }

    def save(self, path: Path | None = None) -> Path:
        destination = path or self._path
        if destination is None:
            destination = config.DATA_DIR / f"campaign_{time.strftime('%Y%m%d_%H%M%S')}.json"
        self._path = destination
        payload = self.to_dict()
        # Cópia sanitizada: os itens em memória seguem com caminhos absolutos
        # (o motor os usa durante a campanha); só o JSON gravado é relativizado.
        payload["items"] = [
            {k: (_relpath(v) if k in _PATH_KEYS else v) for k, v in item.items()}
            for item in self.items
        ]
        save_json(destination, payload)
        return destination


__all__ = [
    "AccountSpec",
    "CampaignConfig",
    "CampaignLog",
    "DEFAULT_ACCOUNT_STAGGER_S",
    "DEFAULT_TIMEOUT_BLOB",
    "MIN_ACCOUNT_AGE_DAYS",
    "TaskSpec",
]
