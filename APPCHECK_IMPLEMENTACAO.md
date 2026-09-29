# App Check no QMoney

Integração opcional para um projeto/app Firebase autorizado pelo seu administrador.
Não substitui a autorização do serviço nem garante que endpoints específicos aceitem o cliente.

## Configuração

O administrador deve cadastrar um segredo do debug provider no projeto correto e
fornecer FIREBASE_APP_CHECK_PROJECT_ID, FIREBASE_APP_CHECK_APP_ID,
FIREBASE_APP_CHECK_API_KEY e FIREBASE_APP_CHECK_DEBUG_TOKEN. Não comite os valores.
Não configure um JWT capturado como segredo debug. O projeto Minute não é presumido
como um projeto administrado pelo usuário do QMoney.

Sem configuração completa, nenhum header App Check é enviado e não há chamadas
ao Firebase App Check. Isso não transforma uma rejeição do servidor em sucesso.
A implementação não captura nem reutiliza tokens de outros clientes.

## Comportamento

- Endpoint oficial projects/{project}/apps/{app}:exchangeDebugToken.
- Cache somente na memória do processo, separado por configuração e raiz de dados.
- Validade calculada pelo TTL recebido, com margem proporcional e relógio monotônico.
- Uma emissão por vez; falhas impõem intervalo mínimo de 60 segundos.
- Nenhum token, corpo remoto, segredo ou URL autenticada vai para logs.
- Respostas explícitas de rejeição App Check não disparam reautenticação Firebase.
- Cache antigo em APPDATA/QMoney/appcheck_token.json não é lido nem apagado.

## Validação

Os testes em test/test_appcheck.py usam unittest, transporte simulado e nenhum cache
de instalação. São executados por `python scripts/run_tests.py`.
A emissão real exige credenciais cadastradas por administrador e não foi validada
contra o projeto de terceiros nesta revisão.

Referências:
https://firebase.google.com/docs/reference/appcheck/rest/v1/projects.apps/exchangeDebugToken
https://firebase.google.com/docs/app-check/android/debug-provider
