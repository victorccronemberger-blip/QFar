"""
MoneyMin — pipeline para preparar vídeos egocêntricos de datasets documentados e
enquadrá-los nas categorias do app Minute (com.bakerdata.minute).

Módulos:
  config       Configuração central (URLs, chaves, caminhos).
  minute_api   Cliente da minute-api (auth Firebase + sessão + endpoints).
  datasets     Catálogo de datasets egocêntricos + download.
  framing      Enquadramento de clipes -> categorias/tarefas do Minute.
  deliverable  Montagem do pacote de entrega (vídeo + dossiê .txt + manifesto).
  upload       Réplica do fluxo nativo: SAS -> Blob -> registro -> complete -> finalize -> evaluate.
  sidecar      Sidecar .data.zip nativo (metadata.json + imu.csv + frames.csv).
  ego4d        Acesso ao dataset Ego4D (clipe + IMU real) e conversão p/ sidecar.
  holoassist   Acesso seletivo ao HoloAssist (vídeo + IMU real).
  campaign     Orquestração end-to-end: datasets -> N contas -> relatório.
"""
from __future__ import annotations

__version__ = "0.3.0"

from . import (
    campaign,
    config,
    datasets,
    deliverable,
    ego4d,
    framing,
    holoassist,
    minute_api,
    sidecar,
    upload,
)

__all__ = ["campaign", "config", "datasets", "deliverable", "ego4d",
           "framing", "holoassist", "minute_api", "sidecar", "upload"]
