## QMoney 2.0.45

A Biblioteca passa a mostrar o armazenamento local geral como visão principal, incluindo vídeos originais, normalizados e sensores, mesmo sem recorte preparado.

- Busca, filtros por origem e tipo, paginação, tamanho e caminho dos arquivos locais.
- Resumo do espaço ocupado e livre, com acesso à pasta da Biblioteca.
- Preparação antecipada com faixa de duração, orçamento de cache e reserva de disco configuráveis.
- A conferência dos recortes preparados Ego4D continua numa visão secundária.
- Nenhuma lista de vídeos aceita em uma campanha limita o acervo ou sua preparação.
- Vídeos e sensores referenciados por operações pendentes ficam protegidos contra limpeza e substituição.
- A seleção Ego4D confere a tarefa e o intervalo antes de reutilizar recortes; os diagnósticos IMU descrevem cada parte do vídeo.

Mídia previamente preparada continua preservada para reutilização; o ZIP de envio permanece específico de cada conta.

Validação local: 2.285 testes do serviço e 47 testes da interface passaram. Quatro testes Nymeria ficaram sem execução por ausência da sequência piloto. O pacote local também passou pelas 11 verificações do serviço empacotado e pela conferência de SHA-256 e assinatura RSA.
