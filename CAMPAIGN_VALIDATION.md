# Revisão da campanha — 18/09/2026

## Incidente da versão 1.0.46: origem de câmera

O histórico local da campanha iniciada em 18/09 às 23:20 revelou 62 resultados
com `Origem da gravação não permitida pela organização.` O erro era levantado
pelo controle local antes do registro de upload, mas a interface mostrava uma
mensagem genérica recomendando validar a conta.

Causa: o controle comparava `meta.source=ego` com `cameraSources` da organização.
Esses campos têm domínios diferentes. O primeiro descreve o formato dos
metadados; a organização usa `built-in`/`external` para origens de câmera.
Os testes antigos usavam `native` em ambos os lados e mascaravam a divergência.

Correção local posterior à release: validar todas as origens declaradas em
`meta.cameras[].source`, normalizando apenas `builtin` para `built-in`.
Origem ausente, desconhecida ou não permitida continua bloqueada. Nenhuma
origem é substituída para obter autorização, e o payload não é alterado.
A interface agora distingue a restrição de origem de uma falha de autenticação.
As entradas dos testes refletem a estrutura efetiva dos metadados e os valores
do OpenAPI, incluindo câmeras mistas e negativas explícitas.

Validação da correção para 1.0.47: 412 testes Python aprovados.

Esta mudança não comprova a origem física do conteúdo e não altera perfis,
sensores ou permissões remotas. Nenhum novo envio foi iniciado para diagnosticá-la.

## Rodada mais recente: validação ampliada da Campanha

Resultado local: **410 testes Python aprovados**, incluindo 25 testes de
integração Flask → runner → motor → histórico. Esses 25 testes também foram
executados em **10 rodadas consecutivas (250 execuções), sem falhas**.

Correções reproduzidas e verificadas nesta rodada:

- Quantidade solicitada acima do catálogo disponível não termina como sucesso
  completo: registra `task_shortfall`, com pendências por conta, e status parcial.
- Meta de horas não atingida registra `goal_shortfall`, com segundos restantes
  por conta. O aviso aparece também no histórico apresentado pela API.
- Um relatório retornado com erro, sem evento terminal, não é convertido em
  sucesso pelo runner. Estado terminal inválido também é tratado como erro.
- Meta finita cujo cálculo estoura o intervalo numérico é recusada antes de
  alterar o runner; ele não fica preso em execução sem thread.
- Arquivos de preferências ou tokens contendo JSON válido mas estrutura inválida
  não derrubam o preflight. Tokens inválidos não contam como utilizáveis.

Regressões exercitadas: sucesso, falha parcial, falha de preparação, cancelamento
na preparação e no envio, resultados persistidos durante o lote, falha de disco,
falha do registro de enviados com workers concorrentes, liberação de recursos,
catálogo esgotado, alternância de vídeos de origem, repetição de falha transitória
antes da criação de sessão e ausência de recriação quando já existe session_id.
A suíte geral cobre adicionalmente contratos de recuperação/finalização,
chunks incompletos, isolamento de contas/organizações e corrupção de histórico.

Verificações complementares:

- `scripts/check_campaign_ready.py --provider ego4d`: prontidão local aprovada;
  FFmpeg/FFprobe, catálogo, presença de credenciais e espaço livre disponíveis.
  O navegador privado gerou aviso; não é necessário para este envio, mas é usado
  em cadastros/verificações. Presença de credenciais não comprova validade remota.
- `cmake --build dist/work/cmake --parallel 2`: sucesso (build atualizado).
- CTest: `api_responses`, `update_download` e `updater_transaction` aprovados.
  A primeira execução falhou ao carregar DLLs; os testes passaram com os bins
  do Qt 6.8.3 e MinGW 13.1 no PATH do processo. O teste de API inclui timeout
  real contra servidor local, sem acesso ao Minute.
- `git diff --check`: sem erros de whitespace.

Limites: preparação/envio dos testes de orquestração usam provedores simulados.
Não houve envio ao Minute, validação de aceite remoto, ensaio visual do Qt,
certificação de identidade/sensores ou regeneração de perfis existentes.
Não há garantia de ausência absoluta de bugs nem certificação de produção.

## Registro da rodada anterior

Escopo: API local, preflight, execução em segundo plano, cancelamento,
persistência e contratos de recuperação. Provedores e envios foram simulados;
nenhuma campanha real foi iniciada. As alterações já presentes no workspace
foram preservadas.

## Correções adicionais desta revisão

- Falha de autenticação ou serviço em uma conta secundária impede o início
  inteiro, em vez de reduzir silenciosamente a seleção de contas.
- Catálogo de categorias malformado bloqueia o início com HTTP 400, sem erro
  interno nem abertura de campanha parcial.
- Metas não finitas/negativas e contagens inválidas são recusadas antes da
  mudança de estado do runner, evitando ficar em execução sem thread.
- Registro de enviados corrompido bloqueia a consulta/alteração sem sobrescrever
  o arquivo. Inicialização e gravações concorrentes compartilham o mesmo lock.
- Cada resultado é salvo durante o lote. Falhas de persistência impedem novos
  envios; workers já ativos são recolhidos, mantendo seus resultados e contadores.
- Preflight e início tratam catálogos malformados; prontidão explicitamente
  negativa impede início mesmo sem uma lista de erros.
- Histórico inválido não derruba a listagem; detalhes e consulta remota recusam
  o arquivo danificado com erro controlado.

## Evidências locais

O teste `test/test_campaign_end_to_end.py` percorre Flask → runner real → motor
de campanha → consulta de estado e histórico persistido, simulando preparação
e envio. Cobre sucesso, falha parcial, falha de preparação, parada durante envio,
preservação da mídia, prevenção de recriação após timeout, validação de entrada,
falhas no preflight, liberação de recursos e cursor de eventos entre campanhas.

Os testes de contratos e recuperação exercitam separadamente conclusão,
finalização, isolamento entre contas/organizações e chunks incompletos.

Comando reproduzível: `.\.venv\Scripts\python.exe scripts/run_tests.py`.
Resultado final desta rodada: **372 testes Python passaram**.
O arquivo end-to-end agora contém 19 testes, incluindo falha de disco/registro
com workers paralelos, persistência antes do fim do lote e parada na preparação.
Há testes adicionais de concorrência, corrupção e reconstrução do registro e
histórico. `git diff --check` não encontrou erros de whitespace.

O build desktop (`cmake --build dist/work/cmake --parallel 2`) passou.
Os testes CTest `api_responses`, `update_download` e `updater_transaction`
passaram em builds locais separados. Eles cobrem API, timeout de rede e
atualização transacional; não são testes visuais da interface.

Limite: testes locais não garantem ausência absoluta de bugs. Não foi executado
um ensaio visual automatizado do Qt nem aceitação contra serviços externos.
