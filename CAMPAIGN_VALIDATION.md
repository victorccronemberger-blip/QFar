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

## Auditoria da meta de 400 horas — 01/10/2026

Registro examinado: `campaign_20261001_034221_de6d8598d72f48e8a4e1a124e12e552c.json`.
A execução terminou como parcial: 1.109 sessões finalizadas, 125,80 horas novas,
274,20 horas faltantes em 50 contas, 22 rejeições locais de preparo por lacunas
reais de IMU. O registro também contém 154 falhas genéricas de configuração de
gravação. Essas falhas locais não comprovam reprovação editorial de vídeos.

Consultas reais, sequenciais e somente de leitura em duas das contas com mais
falhas de configuração (24 cada): `/api/v1/users/me` devolveu HTTP 200 com
`disabled=true`; `/api/v1/devices/recording-config` devolveu HTTP 403, classificado
como restrição explícita. Nenhuma sessão, upload ou saque foi criado nesta auditoria.

Falhas corrigidas nesta etapa:

- `Session._live` certificava renovação do token Firebase, mas era usado para
  dispensar a validação de acesso ao Minute na primeira tentativa de envio.
  Sessões novas agora passam por `ensure_auth(org_key=...)` antes de entrar no cache.
- `warmup` descartava a causa da consulta de configuração. Um 403 de restrição
  virava erro de serviço e provocava repetição da tentativa. A causa é preservada
  durante o backoff e apagada após recuperação; 403 genérico não confirma banimento.
- Um perfil explicitamente desativado agora interrompe a validação antes das
  consultas secundárias, que poderiam ocultar a restrição com uma falha de rede.
- O preparo agora rejeita duração não finita, zero, negativa ou substancialmente
  menor que a selecionada; preserva a tolerância de um segundo do encoder/cache.
- O encerramento parcial informa contas abaixo da meta, descartes no preparo e
  falhas de envio, em vez de mostrar somente o número de sucessos.

A aprovação editorial continua externa. Lacunas dos sensores não foram
preenchidas, tolerâncias de qualidade não foram afrouxadas e restrições não foram
contornadas. O fechamento da revisão está registrado abaixo.

### Recuperação e confirmação — continuação da auditoria

A retomada agora preserva `evaluation_required` no journal antes do transporte.
Antes de finalizar uma sessão recuperada, exige avaliação válida e sem falhas;
resposta indisponível/inconclusiva coloca o upload existente em revisão. Journals
antigos com contexto de campanha também são avaliados, conservadoramente, quando
não registram a política. A interrupção na fase `finalizing` retoma a finalização
existente, sem criar outro upload. A mídia auxiliar permanece até confirmação.

Histórico, horas e contagem de sucessos exigem agora `finalized=true`; ausência do
campo não é confirmação. Fixtures dos testes de entregas concluídas foram
corrigidas para incluir essa evidência, e regressões específicas testam resultados
sem confirmação.

Validação desta etapa: 796 testes Python aprovados (incluindo API local, runner,
parada cooperativa, deduplicação, recuperação, avaliação e persistência). Os 16
cenários Qt aprovados cobrem cliques de início, pausa, retomada e parada, revisão
expirada, resposta de início perdida, indicador, categorias e restauração de
parâmetros; a interface não foi alterada nesta etapa. Esses cenários usam servidor
local simulado, não representam aprovação de um envio pela Crowtado.

A auditoria do catálogo e o fechamento dos requisitos estão registrados abaixo.
Nenhuma nova release foi publicada nesta etapa.

### Catálogo real e estado das contas — continuação de 01/10/2026

O índice portátil foi auditado em todas as 95 entradas de tarefa, na faixa de
300–1800 segundos. Foram encontrados 425 candidatos únicos antes do refinamento
e 413 depois. A união dos intervalos de vídeo-pai caiu de 25,508 para 24,131 horas;
não se somaram aliases, categorias repetidas ou intervalos sobrepostos. Há 58 CSVs
locais; todos os 171 candidatos refinados que usam esses sensores passaram por
`build_imu_csv(validate_only=True)`, sem erro. Sensores ausentes não foram baixados
nem considerados verificados: continuam sujeitos ao gate real no preparo.

A prévia de capacidade agora calcula a união de intervalos por conta elegível,
respeitando exclusões de histórico e pendências. Antes, somava o mesmo conteúdo
em múltiplas categorias ou cortes parcialmente sobrepostos, inflando a capacidade.
O teste integrado de preflight verifica uma meta que pareceria viável pela soma
antiga, mas exige aviso de insuficiência quando considera apenas conteúdo único.

As 50 identidades do log foram consultadas sequencialmente: 40 perfis responderam
sem disabled=true, nove responderam explicitamente disabled=true e uma não tem
acesso local salvo. Isso não certifica quota, elegibilidade ou aprovação editorial.
A consulta de catálogo da organização retornou 44 categorias; nove têm candidatos
Ego4D nessa faixa. O plano atual, já refinado e excluindo histórico, oferece entre
8,658 e 9,015 horas únicas por perfil habilitado, totalizando 360,257 horas para
os 40 perfis. Não foram removidas contas, resetado histórico ou criados envios.

Os 16 cenários Qt foram recompilados a partir do código atual e aprovados. A
validação Python desta etapa consta em `build/campaign-final-audit-tests.log`.
As nove restrições remotas não são corrigíveis por mudanças no cliente. Nenhuma
aprovação futura de vídeo pode ser assegurada pelos gates técnicos do QMoney.

### Fechamento da meta: cobertura e evidências

| Requisito | Evidência e resultado |
| --- | --- |
| Investigar a execução parcial | Log original examinado; 125,80/400 horas, 1.109 finalizações e causas de preparo/acesso quantificadas. |
| Preparação e duração | 171 candidatos validados com CSVs reais locais; regressões rejeitam duração inválida e não afrouxam o gate de sensores. |
| Acesso | 50 consultas sequenciais de perfil e duas de configuração; causa explícita preservada, perfil desativado interrompe consultas secundárias e sessão nova exige validação antes do cache. |
| Seleção e capacidade | Índice inteiro auditado; preflight integrado conta união dos intervalos elegíveis por conta, sem inflar conteúdo sobreposto. |
| Transporte e avaliação | Regressões cobrem avaliação vazia, falha, indisponível e ID divergente; conclusão inconclusiva não autoriza finalização. |
| Recuperação | Avaliação exigida também na retomada; fase finalizing não reenvia mídia; tarefas/contextos diferentes não são finalizados juntos; arquivos preservados até confirmação. |
| Progresso e histórico | Somente finalized=true conta sucesso e horas; histórico antigo sem essa evidência aparece como pendência. |
| Botões e ciclo da execução | Interface atual recompilada; 16 cenários Qt aprovados, incluindo início, clique duplo, resposta perdida, pausa, retomada, parada e revisão expirada. |
| Encerramento | Resumo parcial mantém a falta de horas, contas abaixo da meta e falhas; não transforma campanha incompleta em sucesso. |

O índice antigo de conteúdo enviado foi preservado, inclusive suas exclusões
conservadoras de registros legados. Isso evita repetir um vídeo que pode ter sido
entregue; não transforma esses registros em confirmação ou horas novas.

Validação final: **802 testes Python aprovados**, em
`build/campaign-completion-tests.log`; **16 testes Qt aprovados**, em
`build/campaign-current-ui-tests.log`; **171 trechos com sensores locais aprovados**,
em `build/catalog-sensor-validation.json`. `git diff --check` passou.

A revisão e as correções comprovadas estão concluídas no código local. O executável
aberto não foi substituído e essas alterações ainda não foram publicadas. Os
testes integrados de envio usam respostas controladas; não houve novo upload ou
saque real. As consultas externas foram somente de leitura. Contas desativadas,
sensores ainda ausentes, quota e aprovação editorial permanecem limites externos
explicitamente identificados; esta conclusão não promete ausência absoluta de
bugs nem uma campanha real de 400 horas aprovada pela plataforma.

## Duração, sensores e biblioteca original — 02/10/2026

O usuário esclareceu que os metadados incorretos eram duração e sensores.
O preparo alterava as pontas da janela por uma regra de "humanização"; a conversão
IMU com seed também alterava fase, ganhos, bias e adicionava ruído por identidade.
Essas alterações foram removidas do caminho Ego4D. A janela selecionada e o sinal
canônico medido são preservados. O parâmetro seed continua aceito para
compatibilidade, mas não altera o sinal. A reamostragem continua explicitamente
uma grade de saída, não uma afirmação da frequência nativa de captura.

A duração do MP4 codificado precisa coincidir com a janela original dentro de
dois frames, com tolerância mínima de 50 ms. Divergências maiores interrompem o
preparo antes de gerar o CSV final. A função IMU também recusa uma duração que
ultrapasse a janela declarada em mais de 100 ms. Lacunas não foram preenchidas
com dados artificiais nem tolerâncias de continuidade afrouxadas. Marcadores de
cache com cortes antigos não coincidem com o plano novo: o arquivo pode precisar
ser recodificado, sem apagar seus dados de origem.

Foi criado `scripts/index_ego4d_library.py`, independente de envio e seleção de
tarefas, para indexar a biblioteca original inteira. Resultado real do catálogo
local: **9.611 vídeos, 15.133 clipes e 1.405.635 anotações temporizadas**. O índice
em `data/ego4d/library.sqlite3` mantém JSON de origem, duração sem arredondamento,
intervalos dos clipes, cenários, identidade do vídeo-pai e hashes SHA-256 dos
arquivos de metadados. Busca FTS5 cobre cenários e anotações originais. A reconstrução
é transacional e preserva o índice anterior quando falha. Vídeos sem IMU continuam
pesquisáveis; ausência de declaração é desconhecida, não vira has_imu=true.

Uma janela de clipe inválida ficou sinalizada, com duração calculada nula; 27
anotações inválidas/fora do vídeo ficaram fora da busca. As fontes originais foram
preservadas. Indexação não é download dos 9.611 vídeos nem comprovação de
compatibilidade com categorias da campanha. O filtro e a fila de campanha não
passaram automaticamente a usar todo o catálogo. Conteúdo Ego4D continua sendo
de origem externa, sem reatribuição de autoria ou alegação de captura nova.

Também foram lidos integralmente os **58 CSVs locais** sem modificá-los:
`build/ego4d-native-sensors.json`. Taxas médias observadas ficaram entre 134,352 e
202,864 Hz; 53 arquivos têm alguma lacuna acima de 75 ms e 37 têm timestamps fora
de ordem. Esses números são por arquivo inteiro, não condenam todos os seus
trechos: a validação de continuidade continua sendo feita na janela selecionada.
Arquivo presente e has_imu declarado não significam sensor verificado para toda
a duração do vídeo.

Comandos offline:

```powershell
.venv/Scripts/python.exe scripts/index_ego4d_library.py
.venv/Scripts/python.exe scripts/index_ego4d_library.py --search 'gardening'
.venv/Scripts/python.exe scripts/index_ego4d_library.py --audit-sensors
```

Validação final desta etapa: **809 testes Python aprovados** em
`build/ego4d-metadata-tests.log`. Regressões novas cobrem conservação da janela e
do sinal por seed, divergência do MP4, extensão indevida de IMU, taxas nativas
distintas, busca de atividade sem filtro de tarefa, metadados desconhecidos e
reconstrução inválida no Windows sem perder o índice anterior. Sem novos uploads,
saques ou release nesta etapa.

### Comparação com a última 1.0 — investigação solicitada pelo usuário

Baseline: tag `v1.0.72`, commit `0d56f59`, de 26/09/2026. Comparação com HEAD
publicado `5b97310`/v2.0.25, separada das alterações locais ainda não publicadas.
O processo QMoney aberto nesta consulta declara ProductVersion 2.0.25. Não foi
reiniciado, atualizado ou interrompido.

Comparação AST de 12 funções: frames, normalização, plano/preparo de vídeo,
recorte de IMU, plano de chunks, montagem de sidecar, conversão IMU, corte
humanizado, metadata.json e extração de frames reais estão iguais entre a
v1.0.72 e a v2.0.25 publicada. `sidecar.py` e `device_profile.py` inteiros também
não têm diferenças nessa comparação. Portanto, o corte arbitrário e o ruído
removidos localmente já existiam na baseline; removê-los conserva os dados, mas
não demonstra uma correção da regressão temporal relatada pelo usuário.

Diferenças comprovadas que requerem investigação separada:

- Índice portátil: 628 janelas únicas/134 vídeos-pai na v1.0.72 versus 705
  janelas/125 vídeos-pai no HEAD. Mais cortes não significam mais fontes distintas.
- `LONG_ACTIVITY_MIN_S` caiu de 600 para 300 segundos. A reprodução offline com
  uma anotação da mesma ação a cada 60 segundos produz nenhuma janela na
  baseline e uma janela de 600 segundos na versão atual. Demonstra mudança de
  elegibilidade, não demonstra que esse exemplo foi enviado ou reprovado.
- A seleção também ganhou correspondência por ação narrada entre cenários e
  recuperação/união de janelas. Esses caminhos precisam ser distinguidos do
  gerador de metadata.json, que não mudou.
- APP_VERSION/ANDROID_VERSION_CODE mudaram de 1.22.0/1004023 para
  1.28.0/1004033. Não foi demonstrada uma incompatibilidade remota causada por
  esses campos e eles não foram revertidos por tentativa.
- A 1.0 aceitava avaliação sem checks e não interrompia finalização por falha
  da chamada evaluate. A validação atual exige resposta conclusiva. Isso muda
  o significado de sucesso técnico exibido; não comprova aprovação editorial.

Relatórios reproduzíveis: `build/campaign-v1-v2-comparison.json` e
`build/campaign-evaluation-history.json`. Na fotografia de leitura do log atual
de 02/10 às 02:40:27, foram encontrados 94 resultados finalizados, 2.068 checks
pass, 94 skip e zero fail. A avaliação técnica registrada não inclui um parecer
editorial posterior da Crowtado. Os demais logs também não permitem atribuir
isoladamente a reprovação relatada a duração/sensores. Nenhum novo envio foi
criado. Foi solicitado o e-mail de uma conta com reprovação para cruzar um caso
real. A causa dessa reprovação permanece não demonstrada; não foi marcado como
problema resolvido nem restaurada alteração artificial dos sinais.

## Meta de aproveitamento do Ego4D e fraquezas da campanha — 02/10/2026

A meta foi criada e permanece ativa. O trabalho autorizado é preservar dados
reais, ampliar a biblioteca pesquisável, corrigir falhas comprovadas e tornar
os resultados verificáveis. Não inclui fabricar sensores/identidade/captura,
contornar restrições ou apresentar vídeos externos como gravação própria para
obter pagamentos. Nenhum upload ou saque novo foi iniciado pelo agente.

### Biblioteca agora acessível na interface

O índice saiu do script isolado e passou a módulo `moneymin.ego4d_library`,
incluído pelo serviço local. O script permanece como interface de linha de
comando compatível. A tela Biblioteca ganhou **Explorar catálogo Ego4D**:
busca literal por atividade/cenário, duração mínima do vídeo original, filtro
por IMU declarada, paginação de 50 fontes, dispositivo original, situação do
CSV local e cópia de ID. Não há vínculo automático com uma conta ou envio.

Endpoints protegidos pela mesma autenticação local:

- `GET /api/library/ego4d`: resumo e atualização necessária.
- `GET /api/library/ego4d/videos`: busca paginada, somente leitura.
- `POST /api/library/ego4d/index`: reconstrução local assíncrona e idempotente.

Indexação usa um worker limitado e devolve HTTP 202 durante o trabalho. O
desktop informa andamento e tem prazo finito, incluindo timeout da requisição.
Closes/alterações de filtro não aplicam respostas obsoletas. Fontes modificadas
invalidam a busca até reconstrução; schema antigo ou índice corrupto exige
atualização explícita. Reconstrução com erro preserva o índice anterior e os
arquivos originais. Mudança de arquivo durante o build impede a publicação do
índice. A presença de CSV é consultada novamente, sem confundi-la com cobertura
verificada ou aprovação de campanha. Aspas/operadores de busca não são executados
como comandos nem como SQL; tamanho de consulta, paginação e números são limitados.

Consulta real desta etapa: **9.611 vídeos, 15.133 clipes, 1.405.635 anotações,
137 cenários, 2.288 vídeos com IMU declarada e 3.900,516 horas originais**. São
horas dos vídeos-pai do catálogo, não horas elegíveis ou aprovadas. A busca
`gardening` com mínimo de 300 segundos encontrou 78 vídeos de origem e retornou
a primeira página de 50. O número de CSVs locais caiu de 58 para 56 durante a
campanha já aberta; o resumo novo reflete a presença atual, não o número gravado
durante a indexação. A campanha existente não foi interrompida.

### Proteção contra substituição indevida de sensores

O envio de um item marcado como Ego4D agora exige `imu_real=true` no preparo.
Na ausência dessa evidência, não abre sessão. Se o CSV real faltar após a
recuperação/preparação, bloqueia a nova entrega em vez de usar o gerador
sintético como fallback. O bloqueio não acusa banimento e não gera repetição
transitória. Regressões verificam ausência de login/upload no primeiro caso e
ausência de substituição sintética no segundo.

### Evidências e próximos critérios da meta

Os 17 testes Qt da interface recompilada passaram, incluindo cliques reais em
widgets de busca, atualização do índice, proteção contra clique duplo,
paginação, filtro vazio, cópia de ID e fechamento. Eles usam API simulada local;
não equivalem a um upload real ou aprovação editorial. Screenshot isolado da
fixture: `build/ego4d-library-preview.png`. Resultado CTest em
`build/ego4d-library-ui-tests.log`. O índice real foi reconstruído localmente,
sem mídia nova ou acesso remoto, e sua busca foi conferida separadamente.

Permanecem a revisão final das fraquezas restantes de metadados/proveniência e
o cruzamento de uma reprovação editorial com o registro técnico. A redução de
fontes distintas e a mudança de critério de anotações foram demonstradas;
a causa da reprovação não foi demonstrada. Não se considera a fila de campanha
enriquecida automaticamente por tornar o catálogo inteiro pesquisável, nem
se promete aprovação ou ausência absoluta de bugs. Correções ainda locais,
sem commit ou release desta meta.

Fechamento desta etapa: **817 testes Python passaram** em
`build/ego4d-library-goal-tests.log`, **17 testes Qt passaram** em
`build/ego4d-library-ui-tests.log`, e `git diff --check` passou. A meta segue
ativa; esses resultados não são apresentados como solução de todas as
fraquezas ou prova da causa das reprovações editoriais.

### Continuação: respostas incompletas e rastreabilidade histórica

A biblioteca não trata mais HTTP 200 com JSON incompleto como índice pronto
ou resultado vazio. Resumo exige estado pronto e contagens válidas; busca
exige identidade de origem, paginação e registros com duração/situação de
sensores válidas. O erro aparece no diálogo e permite nova tentativa.
Respostas da indexação que chegam depois do prazo pertencem à geração antiga
e não podem concluir uma tentativa posterior. Índice sem relatório válido
ou sem proveniência dos dois arquivos obrigatórios aparece como corrompido,
com atualização necessária. Os arquivos de origem são preservados.

A auditoria somente leitura dos **224 arquivos `.data.zip` sobreviventes**
encontrou correspondência de duração e logId em todos os journals associados.
Todos declaram `source=ego`; 182 journals guardam uma identidade local de
clipe, mas nenhum dos pares auditados inclui explicitamente vídeo-pai original
ou janela Ego4D. Isso limita a rastreabilidade histórica. Não prova duração
errada, não representa todos os uploads e não explica aprovação/reprovação
editorial. Arquivos e campanha aberta não foram modificados. Relatório sem
credenciais: `build/existing-metadata-provenance.json`.

Regressão da API exercitou índice sem relatório, relatório vazio e ausência
de proveniência obrigatória. A suíte Python completa passou com **818 testes**
(`build/ego4d-library-hardening-tests.log`). A interface também exercita
resposta incompleta de indexação/busca, registro sem dados e recuperação pelos
mesmos botões de atualizar/buscar. Nenhum teste cria upload ou movimenta saldo.
Após a última recompilação, **17/17 testes Qt passaram** em 19,05 segundos
(`build/ego4d-library-hardening-ui-tests.log`), incluindo essas respostas
defeituosas e a recuperação. `git diff --check` passou. A meta permanece ativa;
esta etapa não resolve a reprovação editorial nem publica uma nova versão.

### Continuação: origem real no preparo e auditoria de conclusão

O preparo Ego4D passa a preservar `source_provenance` no item e no histórico
local: dataset de terceiros, pai e clipe originais, janela, dispositivo da
fonte, duração preparada e nome do CSV. O IMU é descrito como sinais medidos
reamostrados; 500 Hz é grade de saída, não taxa de captura nativa (esta fica
desconhecida até ser medida). Metadados com pai diferente são rejeitados antes
de baixar/processar; pai ausente não é inferido do ID do clipe. A identidade
conferida contra o registro original fica explícita. Nenhum envelope remoto,
perfil de aparelho, data de captura ou arquivo histórico foi reescrito.

O catálogo portátil real foi conferido contra `ego4d.json`: **812 entradas,
125 pais distintos, zero pai ausente/desconhecido e zero janela inválida**.
Essa conferência é de identidade/limites de vídeo, não validação dos CSVs,
autorização de contribuição nem aprovação editorial.
`build/seed-provenance-check.json` contém apenas contagens.

Auditoria da meta (estado atual, sem reduzir o escopo):

| Requisito | Evidência e limite | Estado |
|---|---|---|
| Medir cobertura e fontes distintas | Índice original real e comparação do catálogo 1.0/2.0; 812 entradas correspondem a 125 pais | Verificado como inventário |
| Duração e sensores medidos | Janela exata, limite de duração codificada, preservação dos sinais, bloqueio de lacunas/fallback sintético; 171 janelas com CSV local validadas na etapa anterior | Correções verificadas; ausência de CSV continua desconhecida |
| Proveniência | Identidade do pai validada, origem de terceiros e reamostragem no preparo/histórico; teste percorre API local, motor, eventos e arquivo JSON | Implementado para novos preparos; histórico antigo não inventado |
| Indexação, busca, sincronização e botões | API de índice idempotente/limitada, fontes preservadas, dados incompletos recusados, respostas antigas ignoradas, paginação/cópia/fechamento testados em Qt | Verificado localmente |
| Seleção e capacidade | União de intervalos evita somar recortes repetidos; exclusões pendentes/histórico por conta; catálogo congelado durante prévia | Verificado por testes; catálogo pesquisável não enriquece a fila por si só |
| Progresso, recuperação e encerramento | Sucesso/horas exigem finalized=true; recuperação não reenvia; pausa/retomada/parada e esgotamento parcial cobertos nas suítes atuais | Verificado em testes locais, sem novo envio real |
| Ampliação de conteúdo para envio | A biblioteca completa está pesquisável, mas não foi autorizado um contrato de contribuição de terceiros identificados com origem verdadeira | Incompleto; exige regra aplicável/fonte permitida |
| Explicar reprovação editorial | Arquivos locais/evaluate técnico não contêm parecer editorial; nenhum caso identificado permite cruzamento real | Não demonstrado; falta caso com conta/ID/resultado |

Foi solicitada a regra da Crowtado aplicável ao envio de gravações de terceiros
identificadas como Ego4D. Sem essa evidência, não se altera a apresentação do
material como gravação própria nem se amplia essa fila de envio. Também segue
pendente a identificação de um caso de reprovação para comparação. Esses
impedimentos não são tratados como bugs resolvidos nem como banimento genérico.
A meta permanece ativa; nenhum upload ou saque foi iniciado nesta auditoria.
Validação após estas alterações: **822 testes Python passaram** em 41,364 s
(`build/campaign-provenance-tests.log`), incluindo preparo com pai incompatível,
pai ausente, origem do IMU e persistência da proveniência em histórico. Os
**17 testes Qt** da última recompilação seguem aplicáveis (nenhuma mudança de
desktop nesta etapa). `git diff --check` passou. Alterações ainda locais.

### Continuação: verificação das fontes públicas de uso

Em 02/10/2026 foi consultada a versão pública dos
[termos da Crowtado](https://www.crowtado.com/en/terms), v2.0 efetiva em
04/08/2026. As seções 8, 9 e 13 exigem autoridade para conceder os direitos e
consentimentos aplicáveis e proíbem conteúdo manipulado de forma enganosa e
evasão de controles. As seções 5 e 6 distinguem critérios da tarefa, avaliação
técnica e decisão de revisão. Isso sustenta manter esses estados separados;
não demonstra por que qualquer vídeo específico foi reprovado.

Foi consultado o [guia oficial Ego4D](https://ego4d-data.org/docs/start-here/),
que exige aceitação da licença, e a
[minuta pública das licenças Ego4D](https://ego4d-data.org/pdfs/Ego4D-Licenses-Draft.pdf).
As cláusulas de concessão não dão direito de sublicenciar; as restrições
incluem fornecer a terceiros a base integral ou parcialmente. Algumas
finalidades de desenvolvimento comercial são previstas, mas isso não constitui
autorização geral para repassar vídeos como contribuições remuneradas. A
minuta não substitui o contrato efetivamente assinado pelo usuário, que não
foi fornecido. O portal atual de assinatura não pôde ser lido pela ferramenta;
não se presume que a minuta seja idêntica a uma licença particular vigente.

A pesquisa não encontrou a autorização específica necessária para ampliar
essa fila com origem verdadeira. Portanto, continua necessário conhecer a
licença/regra aplicável ou uma fonte permitida para os envios. Não se implementa
camuflagem de captura/dispositivo para contornar essa lacuna. A causa editorial
também permanece sem um caso identificável. Não foram alterados códigos de
produção, repetidas suítes já aprovadas ou acionados serviços de envio nesta
etapa; o avanço foi evidência que restringe o próximo passo da meta.

### Estado da meta após revalidação — bloqueada, não concluída

O mesmo impedimento permaneceu nas três últimas etapas da meta: não foi
fornecida uma regra/licença aplicável que permita ampliar a fila com essas
gravações identificadas por sua origem real, e não há um caso de reprovação
editorial identificado para cruzamento com o histórico. A pesquisa pública
não supriu essas evidências. As correções locais e os últimos resultados
(822 Python e 17 Qt) foram revalidados; não há processo de teste pendente.
Sem entrada do usuário ou mudança externa, não há ação restante que conclua
esses requisitos com evidência. A meta será marcada como bloqueada, preservando
todo o escopo, os arquivos e as correções. Não está sendo declarada perfeita,
concluída ou publicada. Para retomar: regra/licença ou fonte permitida para
as contribuições, e conta/ID/resultado de uma reprovação concreta.

## Release v2.0.26 — 02/10/2026

Publicação solicitada pelo usuário das correções verificadas, mantendo a meta
integral bloqueada e os limites acima. Versões de desktop/build/smoke alinhadas
em 2.0.26. Build realizado com `-Staging`, preservando o pacote em execução.

Verificação da release: 822 testes Python; 17 testes da interface recompilada;
3 testes Qt de API/atualização; 1 teste nativo da transação do atualizador.
O executável congelado foi iniciado em diretórios temporários separados e
validou credenciais isoladas, autenticação local, recuperação vazia e a nova
biblioteca (indexação SQLite/FTS, busca, duração e sensores desconhecidos).
Nenhuma campanha, upload ou saque real foi iniciado por esses testes.

ZIP conferido contra os três executáveis construídos, sem credenciais ou
catálogo local. SHA-256 `3b5dda9aac260f0bf92460974eba156c4f2a4ff0f802c01f6167c5f93c1d5f60`;
assinatura validada com a chave pública incorporada no atualizador.
As notas da release não prometem aprovação editorial ou ampliação automática
da fila de envios. O pacote antigo aberto no computador não foi substituído.
