# QMoney 2.0.76

A campanha deixa de reler todos os históricos e o arquivo de reset para cada sessão antiga durante a reconciliação dos envios. Resolve os contextos em lote e mantém a verificação das sessões da conta antes de confirmar os recibos.

O histórico da campanha serializa as gravações dos workers para preservar todos os resultados sem colisão de arquivos no Windows. Violações temporárias de compartilhamento têm tentativas adicionais limitadas; erros persistentes continuam sendo informados.

Enquanto uma recuperação estiver em andamento, Iniciar campanha permanece bloqueado, inclusive ao fechar a janela de recuperação. O botão volta a ficar disponível quando o serviço confirma o término. A leitura prolongada das pendências tem acompanhamento limitado e permite nova consulta manual, preservando o trabalho no serviço.

A retomada usa a conexão atribuída à conta durante a autenticação, a confirmação da organização e o processamento dos recibos. O diagnóstico de falha informa a etapa e a categoria sem expor o texto bruto da exceção ou credenciais.

A Biblioteca Nymeria compartilha a construção do inventário entre consultas simultâneas. Inventário e resumo usam a mesma leitura, e as assinaturas de regras e SDK são calculadas uma vez por construção, preservando a invalidação quando os arquivos mudam.

Mantém a recuperação de avaliações transitórias no mesmo recibo, as campanhas sem meta obrigatória de horas, a aquisição de mídia sob demanda e a proteção dos arquivos ainda referenciados por envios pendentes.
