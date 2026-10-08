# QMoney 2.0.75

A recuperação de uma sessão selecionada não deve analisar e reler os históricos de milhares de outras sessões antes de chegar ao recibo escolhido. A seleção agora ocorre antes dessa análise. Os contextos antigos, o índice de publicações e as marcações de reset são lidos em lote, uma vez por operação.

Os journals de todo o armazenamento continuam sendo validados para detectar corrupção e conflito de identidade. A otimização não libera mídia pendente nem autoriza retomada de outra sessão. A recuperação por conta preserva seu comportamento, com a mesma leitura compartilhada.

Mantém as correções da 2.0.74: avaliação transitória retoma o recibo existente sem reenvio, limite de tentativas, revisão de avaliações inválidas e continuidade das demais contas quando uma falha de acesso ocorre antes do envio.

Validação inclui um armazenamento com muitos grupos antigos e verifica que a análise da sessão escolhida não percorre registros alheios. A retomada real no aplicativo é acompanhada separadamente dos testes isolados.
