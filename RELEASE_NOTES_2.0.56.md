## QMoney 2.0.56

Corrige o carregamento das categorias que terminava em HTTP 504 ao consultar o catálogo completo de Ego4D e Nymeria.

- Nymeria compartilha o planejamento entre as categorias: lê as anotações uma vez por consulta e calcula as janelas uma vez por faixa de duração. A classificação ignora somente regras cuja evidência não pode aparecer em nenhuma ação da gravação, preservando as mesmas tarefas e janelas.
- A consulta das categorias usa o inventário local e os marcadores de preparo, sem reler vídeos ou IMUs para cada recorte. As contagens continuam sendo estimativas que exigem validação; o preparo e o envio mantêm as verificações completas dos arquivos, sensores e tarefa.
- Uma consulta demorada mantém seu identificador. A interface mostra o atraso, acompanha por tempo limitado e permite recuperar o resultado pelo botão Recarregar categorias, sem começar outro cálculo. Resultados ainda não recebidos ficam retidos em um cache limitado.
- O resultado pertence à seleção e à identidade atual da conta. Trocar a duração, o dataset ou as credenciais impede aproveitar uma resposta anterior; renovar a sessão ou preencher o cache da organização não descarta uma consulta válida.

Preserva a campanha sob demanda, a limpeza após confirmação, a proteção das pendências, o Reset completo e a retirada de contas restritas sem repetir a validação. Mantém a meta e a faixa de duração escolhidas pelo usuário. A publicação não executa uma campanha nem envia conteúdo ao Minute.
