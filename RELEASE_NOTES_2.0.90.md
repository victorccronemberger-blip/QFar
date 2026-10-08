# QMoney 2.0.90

O rodapé preserva o resultado do cadastro e as mensagens de estado da interface. Saídas técnicas do processo local, incluindo logs HTTP divididos entre leituras, deixam de sobrescrever essas mensagens. Erros de inicialização, reinício e indisponibilidade do serviço continuam com diagnóstico próprio.

Inclui as correções da 2.0.89: bloqueio recuperável distinto de banimento, confirmação de criação separada do login, retomada sem repetir cadastro incerto e prazo de erro 429 persistente entre lotes e reinícios. Mantém campanha sem meta obrigatória, preparo sob demanda e preservação de mídia pendente.

Validado com regressões do criador e da interface. Esses testes não comprovam a causa dos banimentos históricos nem garantem aprovação de contas pelo provedor.
