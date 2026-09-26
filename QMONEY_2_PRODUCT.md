# QMoney 2.0 — produto por instalação

Cada cliente instala o aplicativo e mantém suas próprias contas e dados. A versão 2.0.0 foi publicada em 26/09/2026, com pacote Windows, checksum e assinatura de atualização. Não introduz serviço compartilhado.

## Direção

Uma pessoa nova deve entender o próximo passo sem conhecer a implementação. A central informa o estado observado e encaminha para contas, conteúdo, requisitos, campanha e histórico. Cadastro não significa acesso validado; campanha parada não significa operação pronta.

## Identidade

Referência aprovada: prévia grafite, prata e violeta gerada em 26/09/2026. Substitui integralmente a direção azul/laranja anterior. Grafite `#18191d`, prata `#f3f4f7`, superfície `#ffffff`, texto `#20212b`, violeta `#7357ec`. Navegação compacta, tipografia expressiva, percurso visual de execução e painel contextual. A implementação deve distinguir os dados disponíveis de informações ainda não verificadas.

## Implementado nesta primeira etapa

- Central de operação nativa com orientação para primeira utilização.
- Ação principal derivada de contas e estado de campanha; execução e erro têm prioridade.
- Histórico separado do estado corrente, sem disputa entre respostas assíncronas.
- Proteção contra respostas antigas sobrescrevendo uma nova sincronização.
- Falha de leitura visível, sem apresentar prontidão falsa.
- Conteúdo rolável em janelas pequenas e temas claro/escuro.
- Modelo de apresentação testável e fixture visual isolada, sem iniciar serviço ou verificar atualização.

## Próximos marcos e critérios de conclusão

1. Plano confirmado: prévia por conta/clipe com motivo de inclusão ou exclusão, validade e resumo idênticos aos usados na execução.
2. Evidência por envio: inspeção de sessão, confirmação remota e registro local, distinguindo confirmação, pendência, falha e skip.
3. Recuperação unificada: pendências de campanha e restauração de método de saque visíveis após reinício, sem iniciar nova operação antes de reconciliar a anterior.
4. Distribuição para novos clientes: instalação limpa, isolamento de credenciais, diagnóstico com redação de segredos, backup/restauração validado e atualização/rollback testados.
5. Candidata 2.0: revisão de todas as telas, testes de resolução/DPI, acessibilidade de teclado, instalação limpa e migração de dados de uma versão 1.x.

Não prometer operação sem erros, validação remota sem consulta ou funcionamento multiusuário compartilhado. Não lançar 2.0 apenas pela mudança visual.

## Auditoria atual da candidata 2.0.0

| Requisito | Evidência e limite |
|---|---|
| Identidade e operação | Interface nativa reconstruída, navegação grafite, superfícies claras, violeta, etapas, progresso por conta e contexto. Capturas inspecionadas em 1586×992 e 980×680; variante escura e escala 150%. Não são telas web sobrepostas ao aplicativo. |
| Campanha ponta a ponta | 615 testes backend passaram: prévia congelada, invalidação por reset, confirmação, execução, cancelamento, pausa, deduplicação e recuperação. Provedores controlados; sem uploads reais. |
| Evidências | Teste Qt via HTTP local percorre operação → pausa → retomada → reconciliação → histórico, validando sessão confirmada e sessão pendente. Captura preenchida inspecionada. |
| Wise e Dots | Cinco contas sequenciais, restauração persistente, bloqueio quando limpeza falha e recuperação sem repetir saque; testes controlados, sem saque real. Diálogo Wise inspecionado. |
| Instalações independentes | Três processos testam dois clientes e reinício, com biblioteca compartilhada sem compartilhar credenciais/journals. Serviço executável empacotado repetiu verificação em diretórios vazios. |
| Migração e backup | Testes de journals legados, credenciais e importação/exportação idempotente; transação de atualização preserva dados e faz rollback. |
| Diagnóstico e pacote | Testes de redação de segredos; ZIP com 685 arquivos inspecionado, SHA-256 e assinatura RSA verificados com a chave pública do atualizador. |
| Git e distribuição | Código publicado, build remoto aprovado e release 2.0.0 publicada com ZIP/checksum/assinatura. Links e evidência final abaixo. |

### Limites da validação

Diretórios novos foram usados no Windows atual; não foi executada uma VM de sistema operacional recém-instalado. Testes não substituem disponibilidade ou comportamento futuro dos provedores. Não houve saque financeiro real nem upload produtivo. A fixture visual usa dados de demonstração identificados; a aplicação usa as APIs reais. Não prometer ausência absoluta de erros.

### Registro de implementação

Os checkpoints abaixo são históricos; pendências citadas em etapas anteriores devem ser lidas junto à auditoria atual acima.

### Checkpoint — reconstrução fiel à referência

- A imagem aprovada é especificação de composição, não apenas de paleta. Substituídos: navegação, cabeçalho, estrutura da operação, etapas desenhadas, delegate de linhas e painel de contexto/saldo.
- `OperationState` expõe por conta etapa, percentual observado de transporte, sessão, clipe e contadores; não infere confirmação de um upload a 100%. Snapshot independente do buffer limitado de eventos.
- Painel consulta os dados locais de saldos sem iniciar saques e marca leitura parcial/desatualizada.
- Poll da operação consulta a execução a cada 3 segundos enquanto a página está visível.
- Validação nesta etapa: 584 testes backend passaram; build Qt compilado antes do último ajuste de polling, recompilação pendente no momento deste registro.
- Ainda não há fidelidade visual concluída: faltam linha do tempo desenhada, controles do cabeçalho (incluindo definir/implementar pausa real), identidade de operador, revisão em janela pequena e revisão integral das demais telas/diálogos. Não declarar equivalência visual ainda.
- Captura atual: `build/v2-reference-actual.png` (fixture, não operação real).

### Checkpoint — pausa, busca e validação integrada

- Pausa cooperativa real no runner: `pause_requested` impede continuação nos checkpoints; requisições em voo podem terminar. Retomar libera a mesma execução. Parar sempre libera os waiters e cancela. Rotas pause/resume retornam conflito em estado inválido.
- Controle Pausar/Retomar conectado à API e badge de pausa solicitada sem afirmar quiescência remota.
- Linha do tempo desenhada com eventos reais. Cabeçalho compartilhado; busca Ctrl+K consulta contas e campanhas locais e encaminha ao registro selecionado.
- Layout a partir de 980 px reorganiza cabeçalho e coloca o contexto abaixo da operação; inspeção visual confirmou ausência das sobreposições anteriores.
- Build nativo recompilado. 589 testes de backend passaram. Teste `operation_ui_live` passou com QTcpServer controlado, carregamento real via ApiClient e clique em Pausar/Retomar. Integrado esse teste ao workflow de release, ainda não executado no GitHub para 2.0.
- Próximas pendências: revisão dos estados vazios e diálogos de todas as telas; dados de operador e detalhes visuais remanescentes; E2E pausa com uploads controlados, recuperação e testes de distribuição/isolamento; auditoria Wise; evidência detalhada no histórico. Release continua pendente.

### Checkpoint — evidências, isolamento e navegação

- Histórico distingue confirmação remota, registro legado, pendência e conta pulada. Evidências incluem conta, clipe e sessão; seleção funciona também por teclado e busca.
- Diagnóstico exporta apenas verificações permitidas, sem detalhes livres de exceções ou caminhos. Testes com segredos sentinela passaram.
- Journals de upload agora pertencem à raiz da instalação; migração copia apenas contas presentes nela, preserva originais, não sobrescreve estados locais e impede travessia de diretórios.
- Configurações preserva a seleção da página e pode ser reaberta após fechar o menu.
- Validação: 594 testes backend passaram no ambiente isolado; build Qt recompilado e teste integrado de interface passou. Nenhum saque ou upload real foi emitido.
- A tabela de auditoria acima registra o ponto inicial: evidências e isolamento têm agora implementação e testes, mas a revisão visual integral, distribuição e release permanecem pendentes. Não declarar equivalência visual completa.

### Checkpoint — estados vazios e revisão de janela pequena

- Orientações específicas em tabelas de contas, saldos, requisitos, criação de contas, restrições e histórico; componente acompanha inserção/remoção de linhas e redimensionamento.
- Histórico usa toda a largura para evidências, limpa dados ao trocar seleção e ignora respostas de verificação de outra campanha. Rolagem preserva acesso ao detalhe em 980×680.
- Navegação reduz altura das linhas em janelas baixas, mantendo Configurações acessível. Cabeçalhos de tabela passaram de 9 para 12 px.
- Capturas inspecionadas: build/v2-history-fullwidth.png, build/v2-history-dark-compact.png e build/v2-accounts-empty-compact.png. Esta última motivou a correção do corte da navegação.
- Build Qt e smoke de interface passaram durante a etapa. Auditoria dos testes Wise confirma asserções da ordem completa de operações para cinco contas e bloqueio de rede no teste de protocolo.

### Checkpoint — revisão e composição da campanha

- Nova campanha reorganizada em Conteúdo e contas, Ritmo e limites e Acompanhamento. Seleção usa colunas na tela larga e empilha em janela compacta; listas vazias orientam conexão de conta e consulta de categorias.
- Confirmação substitui a caixa de texto truncada por CampaignReviewDialog: lista completa de contas, métricas verificadas, avisos e parâmetros efetivamente enviados. Voltar é a ação padrão; iniciar exige confirmação explícita.
- A contagem de envios continua identificada como estimativa. Ainda falta concluir a especificação de prévia por clipe: o motor atual escolhe clipes elegíveis em execução, portanto esta interface não afirma um plano congelado que o motor não garante.
- Capturas inspecionadas: build/v2-campaign-review.png e build/v2-campaign-sections.png. Compilação Qt e smoke passaram após a reorganização. Nenhuma operação externa foi iniciada.

### Checkpoint — conjunto de clipes aprovado

- Seleção automática extraída para automatic_candidates e compartilhada pela prévia e pelo motor. A interface solicita include_clip_plan; o servidor conserva o conjunto de candidatos no recibo e o entrega à execução, sem aceitar clipes fornecidos pelo cliente.
- Revisão tem aba de clipes, duração, categoria e elegibilidade por conta. Exclusão significa registro local de envio anterior, sem afirmar nova confirmação remota.
- Mudança da lista de vídeos usados invalida o recibo sob o mesmo lock que protege reset/início. Catálogo alterado após a prévia não adiciona candidatos à execução. Conjunto sem conta elegível é bloqueado antes de preparar mídia.
- Trata-se do conjunto candidato aprovado, não de promessa de que todos serão enviados: quotas, preparação e resultados ainda determinam o total. Identificadores/caminhos privados do catálogo não são expostos na revisão.
- Validação: suíte backend anterior com 594 testes passou após extração; suíte de campanha com os quatro novos casos passou (44 testes). Build Qt e smoke passaram, captura build/v2-review-clips.png inspecionada. Última alteração de texto ainda será recompilada na próxima verificação.
- Revisão visual adicional encontrou contraste insuficiente na linha selecionada da tabela; corrigido com fundo violeta explícito. Texto final e correção recompilados, captura reinspecionada.

### Checkpoint — biblioteca e integrações

- Biblioteca reconstruída em duas colunas: configuração clara e painel de mídia grafite, com empilhamento e rolagem na janela compacta. Campos numéricos têm largura adequada e a nomenclatura coincide com a navegação.
- Integrações agrupadas em Conteúdo licenciado, Códigos de verificação e Esta instalação, mantendo as ações reais de validar, salvar, consultar e reparar.
- Cartões mantêm o título no topo; corrigidos contraste do indicador de proteção e afirmações iniciais antes da consulta.
- Capturas de biblioteca larga/compacta e integrações clara/escura inspecionadas; build Qt e smoke de interface passaram antes do último ajuste de contraste, que foi recompilado.
- Auditoria de recuperação: Dots tem ação persistente na carteira, mas a visão unificada dos journals de campanha ainda não está exposta na interface. Essa é uma pendência funcional real da meta; não confundir reconciliação interna com experiência de recuperação concluída.

### Checkpoint — recuperação visível e reconciliação local

- Sino do cabeçalho e Configurações abrem recuperação: conta, clipe, sessão, pendência e confirmação disponível. O link para a carteira aparece quando Wise/Dots precisa de restauração.
- GET /api/recovery permite apenas campos públicos; journals ilegíveis, nomes inconsistentes e sessão com donos conflitantes falham explicitamente. Não expõe URLs assinadas, erros brutos ou caminhos.
- POST /api/recovery/reconcile só aplica confirmações já persistidas à lista local, preserva partes incompletas e cura acknowledgements parcialmente gravados. Não usa rede, não envia mídia, não solicita saque. Bloqueado durante campanha ativa.
- Validação: suíte de 604 testes passou antes dos últimos testes adicionais; testes específicos de recuperação passaram (9), incluindo rotas, corrupção, isolamento de identidade e idempotência. Smoke Qt agora abre recuperação via ApiClient, carrega a sessão, clica reconciliar e verifica a atualização da tabela; passou.
- Pendência preservada: integrar retomada explícita dos transportes existentes e impedir início de campanhas afetadas até resolver as sessões. A visualização e a reconciliação local não encerram essa parte da meta.

### Checkpoint — retomada explícita das sessões

- Recuperação oferece seleção de conta, confirmação de que pode transferir mídia pendente e polling do trabalho em segundo plano. O worker retoma apenas IDs de sessões existentes, com autenticação e organização correspondentes; não cria uma nova identidade de envio.
- Conjuntos incompletos, conflitantes ou em quarentena não recebem ação automática. Registros continuam preservados; estados e erros públicos não incluem segredos.
- Início de campanha bloqueia contas com registros não reconciliados e aguarda o worker de recuperação. Limpeza de mídia, reset e outras operações conflitantes são bloqueados enquanto ele trabalha.
- Validação: suíte de 607 testes passou após a integração inicial; suíte específica de recuperação passou com 15 casos e campanha com 45 casos. Foram usados provedores controlados e bloqueio de rede, sem upload real.
- Smoke nativo detectou um crash na criação do seletor do diálogo com o tema aplicado. Corrigido usando ComboBox com parent definido na construção. Retiradas as opções temporárias de debug; build Release e operation_ui_live passaram.
- Ainda faltam auditoria visual final de todos os estados, instalação/migração completa e pacote assinado. A recuperação automática não pode garantir solução para arquivo perdido, sessão inválida ou erro do provedor; esses casos permanecem explícitos para revisão.

### Checkpoint — instalação e pacote candidato

- Teste com três processos independentes cobre instalação vazia, dois clientes com biblioteca compartilhada e reinício: contas/credenciais permanecem privadas, a API exige o token da instância e os registros de recuperação ficam na raiz local. Corrigida criação da pasta de segredos quando a raiz ainda não existe.
- Construído pacote candidato local 2.0.0 em dist/staging, com 685 arquivos. SHA-256 e assinatura RSA verificados com a chave pública embutida no atualizador. Não publicado.
- scripts/verify_packaged_service.py executou o QMoneyService.exe real do pacote em raízes temporárias: instalação vazia, credenciais preservadas no reinício, segundo cliente sem dados do primeiro, autenticação da API e nenhuma campanha iniciada. Relatório em build/v2-packaged-service-report.json.
- A validação usa diretórios limpos neste Windows, não uma VM com sistema operacional recém-instalado. Os testes do atualizador cobrem preservação e rollback separadamente.
- Detectada ausência de versão nas propriedades Windows: adicionado VERSIONINFO aos executáveis QMoney e QMoneyUpdater. Versão de desenvolvimento agora 2.0.0; isso não representa release publicada. Pacote candidato anterior precisa ser reconstruído com esse metadado e os ajustes finais.
- Verificação do serviço empacotado e correspondência de versão passaram a ser gates do script local e do workflow de release. O workflow atualizado ainda não rodou no GitHub.

### Checkpoint — candidata Windows final

- Carteira reorganizada, confirmação Wise revisada, rolagem compacta corrigida e estados desconhecidos sem saldo zero inventado.
- Ícones PNG/ICO derivados do SVG violeta, incluindo executável e barra de tarefas.
- 615 testes backend, teste integrado Qt, três testes Qt de resumo/API/atualização e transação nativa do atualizador aprovados.
- Histórico preenchido validado pelo ApiClient real contra servidor local controlado, com distinção entre confirmação e pendência.
- Pacote local assinado: SHA-256 `8112e7f416d6b49ba4675d8104d2e2a830e177d7f022825fe34464d275603643`. Serviço empacotado validado em instalações temporárias sem operação externa.

### Publicação concluída — 26/09/2026

- Código da release: `55135814600d9f53d93eda4b54a9bc8c6575b335`.
- [Build Windows aprovado](https://github.com/victorccronemberger-blip/QFar/actions/runs/36245291089): backend, transação do atualizador, Qt, interface integrada e serviço empacotado.
- [Release v2.0.0](https://github.com/victorccronemberger-blip/QFar/releases/tag/v2.0.0), publicada como versão estável.
- Artefato do build remoto baixado, assinatura RSA verificada com a chave pública do atualizador e metadados PE 2.0.0 conferidos. ZIP com 688 arquivos, sem diretórios de credenciais/journals do usuário.
- Hash do pacote no momento da publicação: `c68fcd496cbb28ec5eaebd7f8cd9abc4d19649c71a5d0bf9f0f86e498fe3ede2`. Digest informado pelo GitHub coincidiu com o arquivo validado.
- Correção de portabilidade nos testes: comparação de caminhos resolvidos evita falso negativo entre nomes Windows curtos e longos (`RUNNER~1`/`runneradmin`). As asserções de isolamento e preservação permaneceram ativas.
- Permanecem os limites declarados: provedores controlados, sem saque/upload real, instalação em diretórios vazios no Windows disponível e runner Windows do GitHub; sem VM interativa recém-instalada.
