# QMoney 2.0.81

Alguns envios antigos foram encerrados como `failed` no Minute após uma falha de DNS. A versão 2.0.80 corrigiu a classificação da falha de rede, mas ainda tentava transferir mídia para esses recibos encerrados, que o servidor recusava concluir.

A retomada agora consulta o recibo antes da transferência e confere sua sessão, conta, organização, tarefa e dados de gravação. Uma consulta inconclusiva preserva a pendência. Um recibo encerrado como falho permanece no diagnóstico, sem nova transferência ou exclusão remota. Somente um grupo completo com todas as partes comprovadamente encerradas deixa de reservar o conteúdo.

A campanha pode tentar esse conteúdo pela rotina existente com uma nova sessão vinculada à anterior, respeitando o orçamento total de tentativas. Uma nova sessão pendente impede outras tentativas duplicadas. Envios confirmados continuam protegidos contra repetição.

A limpeza verifica também os caminhos e hashes de mídia persistidos por retomadas antigas e preserva arquivos compartilhados com qualquer outra pendência. A Recuperação identifica explicitamente os recibos encerrados no Minute.
