# Revisão da atualização — 18/09/2026

## Reparos posteriores à revisão

Os achados abaixo registram o estado anterior. Após o pedido de correção:

- Corrigidos precedência da quota, parser de versão, leitura de diagnóstico,
  persistência da procedência, detecção de falha/atividade de VPN, confirmação
  HTTP de app/opened, isolamento dos testes e inclusão do catálogo no wheel.
- Âncoras exigem schemaVersion 2 e formato válido do ID. Um ID declarado é
  marcado `anchor_reported`; o cliente não tem como certificar sua origem e
  não atribui mais `minute_verified` automaticamente. Perfis legados mantêm
  o ID existente e procedência `unknown` quando ausente.
- Suíte após correções: **405 testes aprovados**, em 31,0 segundos.
- Wheel construído sem baixar dependências; conteúdo conferido, incluindo
  `moneymin/resources/samsung_device_catalog.json`.
- Sem alterações em perfis persistidos, envios reais ou publicação. Validação
  local não substitui uma verificação autorizada contra o serviço e dispositivo.

## Registro da revisão original

Resultado: atualização ainda não pronta para publicação. Revisão estática dos caminhos alterados, comparação com OpenAPI/Hermes local, suíte automatizada e reproduções isoladas. Não foram feitos envios reais, chamadas autenticadas, alterações de perfis existentes ou regeneração de device_state. Este relatório não certifica funcionamento em produção.

## Achados prioritários

1. **P1 — Quota libera negações explícitas.** `moneymin/minute_api.py:1111`: o retorno antecipado usa `APPROVED OR canUpload=True`. Reproduzido em memória: `APPROVED + canUpload=False + quota_exceeded` é liberado; `REQUIRES_LOCATION + canUpload=True` também. Uma resposta contendo apenas `canUpload=True` passa, embora o OpenAPI exija ambos os campos. Validar o contrato completo e não permitir que uma aprovação parcial sobreponha restrições de envio ou localização.

2. **P2 — Parser de versão diverge do Hermes.** `moneymin/minute_api.py:201`: remove caracteres e completa componentes; Hermes exige exatamente três componentes numéricos com a expressão `^(\d+)\.(\d+)\.(\d+)$` (`Duvi/analysis/hermes/decompiled.js:537593`). Reproduzido: `garbage 99` vira `(99,0,0)`; `1.2.3-beta4` vira `(1,2,34)`; `1.23` e `1..2` também são aceitos. Isso pode persistir um bloqueio indevido ou comparar versões incorretamente, apesar do discriminador de erro correto.

3. **P2 — Origem da identidade desaparece na persistência.** `moneymin/device_profile.py:470`: a construção de DeviceProfile descarta `device_id_source`, `from_anchor` e `anchor_owner`. Esses campos também não existem no dataclass. O resultado intermediário diferencia um identificador sintético de um verificado, mas o perfil salvo não permite essa distinção. Preservar a procedência sem reclassificar IDs sintéticos como identificadores emitidos pelo Android.

4. **P2 — “Verificado” significa apenas campo preenchido.** `moneymin/device_catalog.py:242`: qualquer valor convertido em string não vazia pode ser promovido a `minute_verified` quando o e-mail coincide com o proprietário. Não há validação de formato; `load_anchor` também não valida schemaVersion. O arquivo atual tem `minute_app_android_id=null`, portanto esse ramo não está ativo nele. A presença do campo não demonstra verificação: validar estrutura e exigir procedência confiável antes de atribuir esse estado.

5. **P2 — Suíte depende da âncora pessoal e usa dois mecanismos incompatíveis de configuração.** `test/test_device_catalog.py:40` e `:61` dependem do arquivo pessoal. `scripts/run_tests.py:18` executa unittest, enquanto `test/conftest.py:9` só é aplicado pelo pytest. Assim, o desligamento dos controles de transporte anunciado não ocorre no comando oficial. Usar fixtures independentes de dados pessoais e configurar explicitamente o ambiente do runner utilizado.

6. **P2 — VPN instalada é confundida com VPN ativa.** `moneymin/vpn.py:36`: conta adaptadores pelo nome, sem verificar atividade. Um adaptador desconectado pode bloquear todas as chamadas com o novo default ON. Em contrapartida, erro/timeout na sondagem retorna False, confundindo estado desconhecido com ausência de VPN. Diferenciar presença, atividade e falha de detecção, mantendo a política de restrição explícita.

7. **P2 — Catálogo ausente na configuração do pacote Python.** `pyproject.toml:22` inclui JSON da raiz e recursos HoloAssist, mas não `resources/samsung_device_catalog.json`, agora necessário na importação de device_profile. Instalações por wheel ficam expostas a arquivo ausente. Achado por inspeção da configuração; wheel não foi construído nesta revisão. O build desktop em `scripts/build_release.ps1` inclui a pasta resources inteira, portanto não apresenta essa mesma omissão.

8. **P2 — app/opened marca entrega sem conferir HTTP.** `moneymin/minute_api.py:1177`: o retorno `(status, body)` é ignorado e a flag de publicado é ativada mesmo quando a resposta é de erro e não lança exceção. A flag também não é reiniciada na troca de e-mail da sessão. Problema latente com a opção OFF; relevante se habilitada para eventos reais. Confirmar sucesso antes de registrar entrega e vincular o estado à identidade da sessão.

9. **P2 — Diagnóstico liberado apenas no GET direto.** `moneymin/minute_api.py:990`: mesmo após GET /users/me retornar 200, ensure_auth lança erro se o latch estiver ativo. Assim, consumidores que passam por ensure_auth não recebem o diagnóstico, embora Session.request(GET) permaneça permitido. Separar validação de leitura da autorização para mutações quando esse método atender a diagnósticos.

## Conferência dos pontos anunciados

| Item | Resultado da inspeção |
|---|---|
| Latch exige detail.error específico e min_version | Discriminador correto; parser de versão incorreto |
| Mutações bloqueadas, GET permitido | Correto no request; ressalva em ensure_auth |
| 403 version/device/uber-device | Distinções presentes nos caminhos examinados; sem validação contra serviço real |
| Câmera sem androidDeniedModels | Alinhada ao contrato OpenAPI e decisão Hermes examinados |
| Quota por /orgs/{org}/quota | Rota correta; decisão de autorização falha |
| Campaign usa session.recording_policy | Presente |
| recorded_at sem clamp silencioso | Validação explícita presente no caminho examinado |
| VPN e REQUIRE_CURL default ON | Presentes; detector e runner precisam correção |
| app/opened default OFF; android {sdk_int} | Presentes; confirmação de entrega falha se ativado |
| Âncora schemaVersion 2 | Arquivo atual declara 2; leitor não valida versão |
| Shell ID usado como semente | Diferenciado no gerador; procedência perdida no perfil |
| Promoção somente para proprietário | Comparação de proprietário presente; verificação do ID insuficiente |
| Documentação de auditoria antiga obsoleta | Marcada obsoleta; matriz “feito” precisa refletir as pendências acima |

O HMAC é um identificador sintético. Não estabelece equivalência com o SSAID do aplicativo: o Android documenta ANDROID_ID no Android 8+ como específico da combinação de chave de assinatura, usuário e dispositivo. Referência: https://developer.android.com/about/versions/oreo/android-8.0-changes .

## Validação executada

- `.venv/Scripts/python.exe scripts/run_tests.py`: **397 testes; 395 passaram, 1 falha e 1 erro**, saída 1, aproximadamente 32,6 segundos.
- Falha: `test_anchor_propagation_locks_family_and_varies_ids`, âncora ausente no ambiente isolado.
- Erro: `test_anchor_owner_ignores_unverified_shell_android_id`, KeyError em anchor_owner após fallback sem âncora.
- Reproduções isoladas confirmaram os casos de semver e quota descritos acima e a ausência dos campos de procedência no dataclass.
- Não houve validação end-to-end com servidor, dispositivo físico ou publicação. Testes locais aprovados não seriam prova de compatibilidade integral nem de ausência absoluta de bugs.

Recomendação: corrigir primeiro a autorização por quota; depois parser, procedência/validação de identidade e isolamento dos testes. Resolver as demais pendências antes de publicar e adicionar regressões que exercitem negações explícitas, respostas incompletas e falhas de transporte.
