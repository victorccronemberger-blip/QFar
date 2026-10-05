## QMoney 2.0.35

Corrige categorias Ego4D que apareciam com zero vídeos compatíveis mesmo com o provedor configurado e o catálogo do dataset selecionado.

A API Minute passou a atender o parâmetro de idioma e retornava nomes em português, enquanto as regras de seleção e o índice portátil usam os nomes canônicos em inglês. A consulta operacional agora pede explicitamente `langCode=en`, independentemente do idioma configurado. A interface mantém os rótulos em português para as tarefas conhecidas.

- IDs das tarefas e critérios de atividade, sensores e duração são preservados.
- Tarefas sem regra ou sem conteúdo elegível continuam indisponíveis; a correção não inventa compatibilidade.
- A consulta real de categorias confirmou 10 categorias com conteúdo na faixa de 5 a 30 minutos após a correção. Nenhuma campanha foi iniciada e nenhum vídeo foi enviado nessa validação.
- Testes de regressão cobrem português, espanhol e inglês, com o índice portátil real, além da preservação dos IDs e da recusa de atividades sem regra.

Inclui as alterações da v2.0.34: proxies importados por TXT, fluxo Crowtado + Minute com e-mail automático, etapas do site manuais e bloqueios ou consultas inconclusivas fora da contagem de sucesso.

Validação local: **2.118 testes Python e 2.604 subtestes**, todos aprovados, além dos testes nativos de carregamento de categorias, navegação e respostas da API. O executável foi compilado com versão 2.0.35. A publicação depende também da verificação completa do pacote Windows pelo CI.
