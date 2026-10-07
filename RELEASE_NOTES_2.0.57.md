## QMoney 2.0.57

Corrige a falha de carregar contas quando o cálculo inicial do catálogo ocupava o serviço local.

- O app inicia o serviço e carrega as contas sem começar a classificação completa de Ego4D em segundo plano. O catálogo é calculado quando solicitado na campanha, preservando o acompanhamento e a recuperação da consulta da versão 2.0.56.
- Consultas de catálogo cedem brevemente tempo de execução durante a classificação para que as requisições locais de contas continuem atendidas. As mesmas regras, evidências e janelas permanecem exigidas.
- A lista de contas lê cada credencial individual uma vez, em vez de abrir o cofre completo e repetir a leitura por conta. A leitura estrita continua impedindo usar uma senha legada quando o registro individual está corrompido.

Mantém a campanha sob demanda, a limpeza após confirmação, a proteção das pendências, o Reset completo e a retirada de contas restritas sem repetir a validação. A publicação não inicia uma campanha nem envia conteúdo ao Minute.
