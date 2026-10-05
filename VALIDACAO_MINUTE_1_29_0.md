# Contrato Minute 1.29.0 — âncora Galaxy S22 (SM-S901E)

Fonte: APK instalado no aparelho real `RQCT804KN2N` (Galaxy S22), pacote
`com.bakerdata.minute`, versionName **1.29.0**, versionCode **1004038**,
splits arm64 / pt / xxhdpi. Extração ADB sem root; artefatos em
`OneDrive/Desktop/Minute/`.

O QFar 2.0.30 estava calibrado em **1.28.0 / 1004033** a partir de APK de
emulador. Os literais de sidecar (CSV, timebase, Brown-Conrady, gyro_anchored)
**permanecem no 1.29**; a âncora de aparelho e os defaults de versão passam a
ser o S22.

## O que o 1.29 confirmou no smali (`apktool/.../smali/l2.1/`)

| Literal | Arquivo smali |
|---|---|
| `.imu.csv` / `.frames.csv` / `.metadata.json` | `n0.1.smali` |
| header IMU `t,ax,ay,az,wx,wy,wz` | `K0$b.smali`, `Y.smali` |
| header frames `i,ptsNs,dtNs,tNs,key` | `n0.1.smali` |
| `android_elapsedRealtimeNanos` | `q.1.smali` |
| `trinet_camera_monotonic` | `K0.smali` |
| `gyro_anchored_v1` | `q0.1.smali`, `Y.smali` |
| `brown_conrady` + layout k1..p2 | `J0.smali`, `q.1.smali` |
| `no_camera_imu_calibration` | `J0.smali`, `q.1.smali` |
| `video/avc`, `codecActuals` | `Q0.smali`, `S.smali`, `V.smali` |
| Build.MODEL / RELEASE / SDK_INT no metadata | `S.smali` ~1697–1737 |
| ImuSample → CSV (só accel+gyro) | `N0$a.smali` |

JS (`Minute/bundle/decompiled.js`): `getDeviceUploadMeta`, upload machine, tasks,
`chunkSidecarSchema` (JSON ao lado do MP4), `imuDiagnostics`/`codecActuals` no
meta de sessão. Strings CSV/timebase **não** estão no Hermes — só no nativo.

## Defaults e contratos atualizados no QFar

| Chave | Antes (1.28) | Agora (1.29 S22) |
|---|---|---|
| `APP_VERSION` | 1.28.0 | **1.29.0** |
| `ANDROID_VERSION_CODE` | 1004033 | **1004038** |
| `NATIVE_*_MODEL` / `DeviceProfile` | SM-S918B | **SM-S901E** |
| Tasks query | `?lang=` | **`?langCode=`** (omite se `en`) |
| Categorias | `GET /api/v1/categories` | **`categories_from_tasks`** |
| `metadata.json` platform | `{type,version}` | **`{os,version}`** |
| PATCH complete | suppress | **+ `network_type`** |
| clockDomain desconhecido | warn | **fail** |
| `imuDiagnostics.clockOffsetNs` no zip | — | **fail** |

Arquivos: `config.py`, `device_profile.py`, `minute_api.py`, `sidecar.py`,
`validate.py`, `upload.py`, `framing.py`, `test/test_minute_1_29_contracts.py`.

## Layout `.data.zip` (inalterado estruturalmente)

Na raiz, prefixo `{log_id}.`:

1. `{log_id}.metadata.json`
2. `{log_id}.imu.csv` — `t,ax,ay,az,wx,wy,wz` (ns, m/s², rad/s)
3. `{log_id}.frames.csv` — `i,ptsNs,dtNs,tNs,key`

Domínios de relógio: telefone `android_elapsedRealtimeNanos`; Trinet USB
`trinet_camera_monotonic` (+ âncoras host opcionais no timebase).

## Diferenças 1.28 emulador → 1.29 S22 (operacionais)

- Version gate / header: usar 1.29.0 / 1004038.
- Modelo âncora: SM-S901E (o pool Samsung por conta continua em `device_profile`).
- Endpoint global `/api/v1/categories` visto em 1.28 com 403: **ausente** da
  string table JS do 1.29; categorias vêm embutidas em cada tarefa
  (`categories[{slug,label}]`).
- Overlay JS `onImuStat` (rateHz, sampleCount, accelMag, gyroMag, dropped)
  existe no 1.29; não substitui o CSV do zip.

## Ego4D → wire 1.29 (campanha)

Produção: o upload Ego4D segue o fluxo real do Minute (create→SAS→Blob→complete
com `network_type`→evaluate→finalize). Não há gate artificial de “receiver
policy” bloqueando o wire. Qualidade exigida no caminho:

- IMU **real** do dataset (sem sintético) + CSVs no item preparado
- frames com PTS medidos do MP4
- lineage/seleção íntegros quando o item traz proveniência
- auth Minute / org Crowtado / duração da política remota

Alinhamentos milimétricos no prepare→sidecar→upload:

- IMU relativa do prepare é deslocada para `uptime_ns` /
  `android_elapsedRealtimeNanos` no `.data.zip` (mesmo domínio de frames/`tNs`).
- `imuDiagnostics.maxInterpolationSpanNs` = teto APK `"25000000"`; contadores
  (`interpolatedCount`, nearest-fallback, spans) vêm do resample Ego4D
  (`build_imu_csv(..., stats=)`), sem fingir `interpolatedCount == sampleCount`.
- Mapa do que forjar: `EGO4D_FORGE_MAP.md`.
- Lacuna máxima Ego4D na reamostragem: **25 ms**.
- Validador local: `xcheck.imu_timebase` falha se a IMU ficar em relógio zero
  com âncora elapsedRealtime não-zero.

`session_complete` no PATCH complete permanece `False` no desktop (estratégia
`complete → finalize` separada; sem equivalência comprovada com `saveGated`).

## O que ainda exige amostra real do S22

Abrir um `{log_id}.data.zip` gerado pelo app no aparelho (pasta privada /
export) para confirmar ordem dos membros, presença de extras e valores reais
de `imuDiagnostics` / `codecActuals`. Até lá, o validador local espelha o
writer smali, não o resultado remoto do Catbear.

## Referências forenses

- `OneDrive/Desktop/Minute/RELATORIO.md`
- `OneDrive/Desktop/Minute/reports/MAPA-COMPLETO-GRAVACAO.md`
- `OneDrive/Desktop/Minute/reports/MAPA-TAREFAS-ENVIO.md`
- Histórico: `VALIDACAO_MINUTE_1_28_0.md`, `UPDATE_V1.28.0.md`
