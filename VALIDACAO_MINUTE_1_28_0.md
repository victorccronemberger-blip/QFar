# Revisão da atualização Minute 1.28.0 — 29/09/2026

Resultado: falhas locais corrigidas e testes de regressão adicionados. A compatibilidade remota integral continua não certificada.

## Consultas reais, sequenciais e sem alterações de saldo/conteúdo

Headers efetivos: X-App-Version 1.28.0; versionCode configurado 1004033.
Na primeira conta, /users/me retornou 200, mas as demais rotas informaram conta desabilitada.
Na segunda conta, /users/me confirmou disabled=false:

| Rota GET | HTTP |
|---|---|
| /api/v1/users/me | 200 |
| /api/v1/categories | 403: Request cannot be completed. |
| /api/v1/devices/native-camera-policy | 200 |
| /api/v1/devices/recording-config | 200 |
| /api/v1/apk/version-check | 200, blocked=false |

A verificação de versão informou app_version=1.28.0, min_version=1.21.0 e latest_version=1.21.0.
Isso confirma aceitação do header, não equivalência integral com o APK 1.28.0.
Não foi enviado App Check; o 403 genérico não permite atribuir a causa a App Check.
Não foram realizados uploads, campanhas, saques ou consultas reais de saldo Crowtado nesta revisão.

## Testes locais

Após instalar pytest no ambiente local: 702 testes unittest passaram. Os 9 testes pytest passaram separadamente, com cache isolado. A suíte unittest continua sem coletar essas 9 funções.

## Achados da revisão inicial (antes das correções)

1. moneymin/appcheck.py:107: URL de exchangeDebugToken não contém o recurso apps/{app_id}, exigido pelo contrato oficial. A chave declarada também não é utilizada na chamada.
2. moneymin/appcheck.py:143,182: expiração calculada com TTL é descartada. Teste simulado com TTL=60s deixou cache válido por 3300s.
3. Cache em APPDATA/QMoney, sem isolamento por QMONEY_USER_ROOT/projeto/token e salvo em texto simples. A fixture de testes chama clear_cache nesse caminho real; testes desta revisão foram redirecionados para diretório temporário.
4. O runner oficial executa unittest. Os 9 testes novos são funções pytest e não são coletados por unittest; pytest também não está declarado nas dependências. Antes de instalá-lo localmente, a suíte falhou ao importar test_appcheck.
5. Os 9 testes pytest passam, mas simulam a função de emissão inteira e não verificam contrato HTTP, TTL, concorrência, falhas transitórias ou integração request/request_detailed.
6. Ausência de controle de concorrência/backoff: uma configuração inválida pode repetir a tentativa de emissão a cada consulta; ausência de token emite aviso a cada request.
7. Alterar comentários de 1.22.0 para 1.28.0 não valida novamente o contrato de upload/sidecar extraído anteriormente.
8. O capturador procura JWT onde o debug provider fornece um segredo de depuração; são credenciais diferentes. Debug tokens precisam ser cadastrados pelo administrador autorizado do projeto Firebase. Captura/reutilização de tokens de outro cliente não foi testada.

Fontes oficiais:
- https://firebase.google.com/docs/reference/appcheck/rest/v1/projects.apps/exchangeDebugToken
- https://firebase.google.com/docs/app-check/android/debug-provider

Não houve commit nem release nesta revisão.


## Correções aplicadas

- Contrato HTTP com projeto e app explícitos, credenciais fornecidas por administrador autorizado.
- Removidas constantes que presumiam autorização para um projeto Firebase de terceiros.
- Cache exclusivamente em memória; respeita TTL, margem proporcional e duração da requisição.
- Isolamento por configuração/instalação; uma emissão concorrente; backoff de 60 segundos.
- Logs sem token, segredo, corpo remoto ou URL autenticada; sem dependência de requests/pytest.
- Rejeição explícita App Check não provoca refresh/relogin do token de usuário.
- 13 testes App Check em unittest, com contrato HTTP, TTL, falhas, concorrência e integração das duas APIs de sessão.
- 3 testes de catálogo: HTTP recusado ou payload inválido não se transforma em lista vazia.
- Documentação deixa de afirmar equivalência do APK ou funcionamento integral com base em HTTP 200 de uma rota.
- Utilitário local de captura desativado; não captura segredos nem escreve no .env.
- Correção de menus preservada no build, aplicável à dependência local e à obtida por FetchContent.

O erro remoto genérico 403 não foi contornado nem interpretado como sucesso. Não houve emissão real de token App Check, uploads, saques ou campanhas nesta correção.

Validação final após as correções: 718 testes de backend passaram pelo runner oficial; 10 testes da interface Qt passaram, incluindo Configurações. `git diff --check` sem erros. Nenhum commit ou release realizado nesta etapa.
