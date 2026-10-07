## QMoney 2.0.59

Corrige a classificação demorada das categorias e a verificação da campanha que expirava ao ler sensores completos.

- A classificação só verifica exclusões quando há evidência da tarefa. Uma busca agrupada preserva as mesmas regras de palavras, frases e higiene, evitando pesquisas repetidas por anotação.
- O cache de evidência considera apenas os campos que determinam a ação, evitando calcular repetidamente o hash de tabelas de regras não utilizadas naquela anotação.
- Nymeria verifica primeiro se existe um componente contínuo de anotações com a duração mínima pedida. Componentes curtos não passam pela classificação completa e continuam excluídos da capacidade.
- A prévia consulta o catálogo sem baixar mídia, varrer CSVs de IMU ou abrir VRS. As durações são estimativas; a medição completa permanece obrigatória no preparo de cada recorte.
- Timeout ou desconexão retomam o mesmo identificador e corpo da verificação, com quatro recuperações no máximo e erros definitivos visíveis.
- O carregamento mostra contadores reais de vídeos e sequências classificados. Trabalho que avança não expira aos cinco minutos; continuam existindo limites de cinco minutos sem progresso e de trinta minutos no total, sem criar outro trabalhador ao consultar novamente.

Mantém a meta, a faixa de duração, a retirada de contas restritas sem repetir a validação, a aquisição de um recorte por vez e a limpeza somente após confirmação. A publicação não inicia envios. A versão 2.0.58 teve sua publicação cancelada durante a verificação local e estas correções são entregues juntas na 2.0.59.
