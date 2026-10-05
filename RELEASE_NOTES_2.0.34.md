## QMoney 2.0.34

O criador agora registra Crowtado + Minute com o proxy selecionado, verifica o acesso e deixa as etapas do site para conclusão manual.

- **Proxies:** botão para importar TXT no formato `host:porta:login:senha`, seleção individual ou distribuição automática por conta. Retomadas mantêm o mesmo proxy; falhas interrompem o cadastro sem usar a conexão direta. Credenciais ficam protegidas pelo cofre Windows e não aparecem nos resultados ou no Git.
- **Fluxo reduzido:** código de e-mail automático preservado. Idade, gênero, equipamento e vínculo em Tarefas ficam manuais, identificados nos detalhes da conta. Esses dados deixam de ser exigidos no formulário de criação.
- **Verificação de bloqueios:** conta criada não basta para contar sucesso. Login Crowtado, acesso Minute e organização precisam ser confirmados. Banimento/bloqueio ou consulta inconclusiva resultam em falha, com credenciais e etapas confirmadas preservadas para revisão ou retomada.
- **Indicação:** novos cadastros usam `4G7PWD9E`; a opção sem indicação continua disponível. O convite Minute permanece `PE8EAR5V`.
- **Retomada e importação:** validação integral do arquivo antes de salvar, preservação do cofre anterior em erro, seleção validada antes de iniciar trabalho remoto e proteção contra pedidos duplicados.
- **Carteira, Operação e Contas:** incorpora as revisões da tag v2.0.33, cujo CI não publicou um pacote. Mantém saldos e restrições verificáveis, recuperação de estado e organização das ações na interface.
- **Persistência:** consultas de registros de envio agora compartilham a reserva local com gravações e limpeza, evitando leitura simultânea à substituição de um registro. Arquivos inválidos continuam exigindo revisão.

Validação inclui testes isolados de criação e retomada, bloqueios, importação, transporte e interface Qt; revisão visual em claro, escuro e janela compacta; teste real somente de saída de rede, confirmando o mesmo IP pelos transportes Crowtado e Minute. Esses testes não criam contas externas nem garantem elegibilidade de saque. CAPTCHA pode exigir intervenção manual no Chrome.

Validação local concluída: **2.116 testes Python e 2.601 subtestes**, sem falhas ou testes ignorados, e **43 testes nativos/Qt**, todos aprovados. Os testes de campanha usam serviços simulados e não enviam vídeos externos.

O pacote Windows é publicado após o CI, verificação do serviço empacotado e assinatura RSA.
