# Ego4D → Minute 1.29: o que forjar

O dataset Ego4D entrega **pixels + IMU irregular** (relógio canônico). O app
Minute 1.29 é rígido: o wire parece uma gravação Galaxy (SM-S901E âncora) com
sidecar nativo. Tudo abaixo é **forjado** no QFar; o que não estiver alinhado
falha Catbear/evaluate.

## Origem vs forge

| Camada | Ego4D traz | QFar forja |
|---|---|---|
| Vídeo | MP4 Aria/GoPro | Re-encode 1440×1080 yuv420p, H.264 High@4.2, GOP~30, VideoHandler/SoundHandler, AAC 48k/256k |
| IMU sinal | accel+gyro medidos | Reamostra 500 Hz; CSV `t,ax,ay,az,wx,wy,wz`; lacuna máx 25 ms |
| IMU relógio | `canonical_timestamp_ms` | Offset para `android_elapsedRealtimeNanos` (= uptime do aparelho da conta) |
| Frames | — | PTS medidos do MP4 codificado + `tNs = pts + uptime` |
| `metadata.json` | — | device/platform/appVersion/timebase/cameras (Brown-Conrady)/codecActuals/imuDiagnostics |
| Identidade | — | `DeviceProfile` por conta (SSAID, MODEL, SDK 34, boot, calib UW Brown-Conrady) |
| HTTP / create | — | `X-App-Version`, `X-Device-Id`, OkHttp UA, AppCheck; POST meta via `getDeviceUploadMeta` curto + `network_type` no complete |
| `recorded_at` | — | ISO local na janela de backlog da política |

## Envelope sidecar (obrigatório)

Membros na raiz: `{logId}.metadata.json`, `{logId}.imu.csv`, `{logId}.frames.csv`.

- `platform`: `{os:"android", version:sdkInt}` (zip completo)
- create POST sobrescreve com curto: `device:{model}`, `platform:{os}`, `appVersion`
- `imuDiagnostics.strategy`: `gyro_anchored_v1`
- **sem** `clockOffsetNs` no zip
- `maxInterpolationSpanNs`: **sempre** teto APK `"25000000"` (nunca o gap medido)
- Contadores (`interpolatedCount`, nearest-fallback) vêm do resample (`stats=`)
- `sampleCount` == linhas do CSV
- IMU `t0` == `timebase.firstFrameSensorTimestampNs`
- Ordem zip (writer APK): **`imu.csv` → `frames.csv` → `metadata.json`**
- `codecActuals.bitRate` / `colorStandard`: valor do probe ou **null** (não inventar)

## Qualidade que não se forja com mentira

- Ego4D **sem** `imu_real` → envio bloqueado (sem IMU sintética)
- Frames Ego4D exigem PTS medidos (`require_measured_pts=True`)
- Proveniência local marca `recording_origin=third_party_dataset`

## Falhas fechadas no prepare E2E (ao vivo)

| Falha | Correção |
|---|---|
| `list_clips` sem `exported_clip_uid` / `parent_start_sec` | campos no `_clip_record` + `_normalize_ego_clip_for_prepare` |
| Mirror bristol MP4 inválido no probe | `_download_clip_with_mirrors` (speac/consortium) |
| Lacuna IMU fantasma (probe > janela) | continuidade na grade da **janela**, não no padding do probe |
| `ffmpeg` não no PATH | resolve também `dist/QMoney/runtime/tools/ffmpeg` |
| uptime negativo (wall << boot) | `uptime_ns_at` recalcula boot no intervalo plausível |
| `maxInterpolationSpanNs` medido | pin APK `"25000000"` |
| Ordem zip errada | imu → frames → metadata |

## Limites honestos

- Sem export real de `.data.zip` do S22, magnitudes exatas de alguns campos
  MediaFormat/EgoImu continuam inferidas do smali.
- Intrínsecos UW vêm do catálogo Samsung + jitter por conta, não de calibração
  óptica medida no RQCT804KN2N.
- AppCheck em desktop usa fluxo de token de debug Firebase, não Play Integrity
  do handset.
- Eixos Aria→Android body-frame: sinais copiados 1:1 após resample; rotação
  de corpo completa exige evidência adicional do Catbear.
