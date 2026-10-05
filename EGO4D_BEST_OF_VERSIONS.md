# Melhor de cada versão — campanha Ego4D

CatBear é aprovação manual. Da nossa parte: aproveitar o dataset o máximo
possível e entregar o envelope Minute o mais coerente possível.

## Combinação adotada (Venom QFar-2.0.30)

| Origem | O que ficou |
|---|---|
| **v1.0.73 / v2.0.2** | `_with_cached_expansion` — pool da campanha inclui clipes já prontos no Acelerador |
| **v1.0.15–18** | Identidade por conta; IMU preflight antes do encode |
| **v1.0.21** | `complete → finalize` (sem misturar `session_complete`) |
| **v2.0.9 / v2.0.22 / v2.0.30** | Rank narrado + oficiais; janelas 60–1800 s; atividades longas |
| **v2.0.26** | `refine_candidates` sobre CSV real em disco |
| **v2.0.24** | Não tratar evaluate vazio como sucesso de finalização |
| **Venom (pós-2.0.30)** | Wire 1.29: IMU no `elapsedRealtime`, gap 25 ms, `imuDiagnostics` do resample, platform `{os,version}`, SM-S901E, sem gate artificial de delivery |

## O que não se restaura

- Gap 75 ms das tags públicas (admite mais furos; desalinha o EgoImu 25 ms)
- `imuDiagnostics` com `interpolatedCount == sampleCount` (mentira estrutural)
- Bloqueio `require_dataset_native_delivery_support` eterno
- Qualquer receita de forge para “garantir” CatBear

## Uso do dataset “mais perfeito”

1. Seleção: narrado + oficial + expansão do cache do acelerador  
2. Diversidade: `prefer_long_clips(prefer_parent_cuts=True)` + `diverse_order`  
3. Sensores: IMU real, preflight, refine em CSV local, gap único 25 ms  
4. Lacuna no prepare → **carve** da maior subjanela contínua (sem inventar sinal)  
5. Envelope: Android 1.29 coerente (relógio, CSV, metadata, ordem zip APK)  
6. CatBear: humano — nós só maximizamos qualidade e cobertura local  

## Funil honesto

`has_imu` (~24% do Ego4D) → task/narration/hygiene → gap 25 ms → prepare/carve → wire 1.29.  
Não restaurar gap 75 ms; não inventar IMU.  
