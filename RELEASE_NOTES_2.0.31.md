## QMoney 2.0.31 — campanha Ego4D / Minute 1.29

Pronto para testar campanha com provedor Ego4D.

### Destaques
- Envelope Minute **1.29.0** (âncora SM-S901E): IMU no domínio `android_elapsedRealtimeNanos`, platform `{os,version}`, gap **25 ms**, `imuDiagnostics` a partir do resample (sem mentir `interpolatedCount == sampleCount`).
- Removido o gate artificial de delivery que bloqueava o envio antes do transporte.
- Melhor aproveitamento do dataset: restaurada `_with_cached_expansion` (best-of v1.0.73/v2.0.2) para incluir clipes já prontos no Acelerador; catálogo IMU alinhado a 25 ms.
- Docs: `EGO4D_FORGE_MAP.md`, `EGO4D_BEST_OF_VERSIONS.md`, `VALIDACAO_MINUTE_1_29_0.md`.

### Como buildar o instalável
Na máquina com Qt 6.8 + mingw + `.venv`:

```powershell
.\scripts\build_release.ps1 -Version 2.0.31
```

### Nota
CatBear continua aprovação manual. Este release maximiza qualidade local do Ego4D + wire Minute.
