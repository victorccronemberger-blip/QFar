# QMoney 2.0.63

Uma conta com falha inconclusiva de acesso deixava toda a campanha bloqueada. Agora a verificação oferece **Revisar contas aprovadas**, reaproveita a mesma prévia e permite iniciar apenas com as contas verificadas. As demais ficam fora somente dessa campanha e permanecem cadastradas. Banimentos confirmados continuam sendo movidos para Banidas.

A prévia e a capacidade usam somente os participantes aprovados. Falta de conteúdo, instalação incompleta, pendências de recuperação, mudança de identidade, pedido alterado ou recibo vencido continuam impedindo o início. A confirmação não repete a consulta das contas ou do catálogo; pedidos repetidos não iniciam outra campanha.

Corrige a persistência de acesso quando o Windows recusa a substituição comum do arquivo com erro de acesso negado. A alternativa nativa preserva permissões e metadados do arquivo, mantém backup durante a substituição e nunca trunca o acesso anterior. Falhas de permissão remanescentes recebem um diagnóstico específico e não classificam a conta como banida.

Validação: testes do backend e da interface nativa cobrem falha PermissionError, conservação do cadastro, continuação com a mesma verificação, confirmação única e bloqueios por capacidade. A publicação depende do CI completo e o pacote é assinado.
