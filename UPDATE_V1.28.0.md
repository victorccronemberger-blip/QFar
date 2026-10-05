# Compatibilidade configurada com Minute 1.28.0

> **Supersedido:** defaults atuais são Minute **1.29.0 / 1004038** (Galaxy S22).
> Ver `VALIDACAO_MINUTE_1_29_0.md`. Este arquivo permanece como histórico 1.28.

Atualizados os defaults X-App-Version=1.28.0 e versionCode=1004033.
Valores de ambiente podem substituí-los. Isso não certifica o formato de upload,
telemetria, sidecar ou equivalência do cliente Windows com o aplicativo Android.
Comentários que documentam análises anteriores do APK 1.22.0 conservam essa origem.

App Check é opcional e usa configuração explicitamente autorizada pelo administrador;
veja APPCHECK_IMPLEMENTACAO.md. O cache, contrato HTTP e tratamento de falhas foram
corrigidos. Não há captura de credenciais nem promessa de contornar restrições remotas.

Na revisão de 29/09/2026 uma conta ativa recebeu HTTP 200 em perfil, política de
câmera, configuração de gravação e verificação de versão, sem App Check.
O endpoint global /api/v1/categories retornou HTTP 403 genérico. Não há evidência
suficiente para atribuí-lo a App Check; não está resolvido por trocar headers.
A conta usada no teste inicial tinha disabled=true nas verificações seguintes.

Uploads, campanhas e saques reais não foram executados. Testes locais com respostas
simuladas não devem ser apresentados como confirmação remota desses fluxos.
