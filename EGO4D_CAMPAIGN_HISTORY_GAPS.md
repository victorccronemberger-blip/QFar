# Lacunas da campanha Ego4D fechadas pelo histórico público

Fonte: [RELEASE_HISTORY.md](https://github.com/victorccronemberger-blip/QFar/blob/main/RELEASE_HISTORY.md) (101 releases, 05/10/2026).  
Âncora “ainda funcionava”: **v1.0.73**. Pipeline prepare/IMU/sidecar/envio semanticamente igual até o começo da v2; a UI mudou em **v2.0.0**.

## Marcos que importam para o wire (não para “truque de aparelho”)

| Versão | O que o histórico registra | Implicação atual |
|---|---|---|
| **v1.0.15** | Motor Android/Minute: identidade por conta, relógios/chunks coerentes, validação de sidecar, limites remotos de gravação | Base do forge estrutural (`DeviceProfile`, timebase, chunks) — manter |
| **v1.0.18** | Valida accel+giro **antes** de baixar/codificar; aceita taxas nativas diferentes dos sensores | Já no `prepare_clip` (IMU preflight) — não relaxar |
| **v1.0.21** | Restaura **complete → finalize**; evita combinar `session_complete` com finalize | Confirma a escolha desktop atual (`session_complete=False`) |
| **v1.0.22** | Campanha só “concluída” quando prévias remotas do Minute terminam | Complete/finalize ≠ Catbear `great`; aguardar prévia é regra de produto |
| **v1.0.24** | Vídeo 10–30 min → **uma** gravação / **um** envio | Evitar fatiar sem necessidade de política remota |
| **v1.0.46→47** | 1.0.46 bloqueou envios locais ao comparar formato `ego` com tipos de câmera da org; **1.0.47 removeu esse bloqueio indevido** | Mesmo padrão do gate artificial de “delivery policy”: bloqueio local ≠ rejeição do receptor |
| **v1.0.61–64** | Modos cache/dataset; Acelerador Ego4D com GB e cache pronto | Campanha deve reconhecer `_native` + IMU prontos |
| **v2.0.9** | Seleção = cortes narrados **+** clipes oficiais com evidência temporal | Já no ranking/seleção atual |
| **v2.0.22** | Atividades 5–9 min; trechos até 30 min | Aproveitamento do catálogo, não forge de sensor |
| **v2.0.24** | **Não finaliza** se evaluate estiver reprovado, **vazio**, inválido ou indisponível; preserva envio para revisão | Explica a mudança de `is_perfect` / checklist não vazio — decisão de release, não bug silencioso |
| **v2.0.26** | Preserva duração e **sensores medidos** Ego4D; biblioteca com origem | IMU real + janela; sem substituição sintética |
| **v2.0.30** | Campanha, envios, recuperação, rastreabilidade, anti-duplicação | Linha base publicada; alinhamento 1.29 é trabalho local pós-tag |

## O que o histórico **não** dá

- Payload/hash de sessão `great` (13/08) para listar campos “mágicos”
- Prova de que modelo/eixos/intrinsics/GOP determinam aceitação Catbear
- Autorização para falsificar origem ou colisão de aparelho

## Checklist operacional derivado do histórico

1. Rodar o serviço a partir do tree onde o gate de delivery é no-op (não um checkout antigo com `raise` eterno).
2. Manter IMU real + preflight antes do encode (legado 1.0.18).
3. Manter complete → finalize sem `session_complete` no mesmo passo (legado 1.0.21).
4. Tratar evaluate vazio como **não finalizável** (legado 2.0.24) — revisar, não fingir perfeito.
5. Seleção narrada+oficial e cache pronto (2.0.9 / 1.0.62) continuam no caminho de campanha.
6. Atualizar versão Minute no wire para o app real em uso (1.29 no tree Venom); a tag pública 2.0.30 pode ainda documentar 1.28 até republicar.
