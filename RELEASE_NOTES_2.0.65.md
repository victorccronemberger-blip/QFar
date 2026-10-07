# QMoney 2.0.65

A campanha podia parar após envios confirmados porque um recibo antigo, concluído e sem histórico correspondente, ainda protegia a mesma mídia. A recuperação agora publica esse recibo localmente antes da limpeza. Registros pendentes e históricos ausentes, corrompidos ou ambíguos continuam protegidos. Os novos envios vinculam o recibo ao arquivo exato da campanha.

A limpeza resolve o armazenamento uma vez por varredura, evitando milhares de consultas repetidas. O lote só adquire o próximo vídeo após confirmar os envios e liberar os arquivos gerenciados. A interface informa arquivos e bytes retidos e apresenta uma parada como parada, sem prometer continuação.

A instalação respeita uma biblioteca escolhida mesmo quando ela está vazia ou contém somente Nymeria. A mensagem de capacidade diferencia vídeos excluídos por histórico ou pendências de uma seleção que não oferece horas suficientes. O pacote verifica a importação do catálogo Nymeria em uma instalação isolada com o SDK incluído.

Validação no Windows: 2.591 testes e 3.312 subtestes do backend, 17 testes Qt, 67 testes da interface nativa, dois testes do atualizador e verificação do serviço empacotado. Os testes de encerramento e reinício aguardam a transição real com prazo limitado. Nenhum envio real foi iniciado pela validação.

O CI produziu e assinou o pacote. A etapa automática de publicação encontrou o arquivo de notas ausente; a publicação foi recuperada com o mesmo artefato assinado e verificado.
