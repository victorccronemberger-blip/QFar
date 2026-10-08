# QMoney 2.0.85

A consulta de categorias agora aguarda a conclusão do arquivamento de uma conta restrita quando as credenciais já foram removidas e o trabalho ainda está retornando. Essa janela produzia HTTP 409 e interrompia a escolha automática da próxima conta. O progresso fica vinculado ao mesmo trabalho, proprietário e seleção; nenhum resultado de categorias atravessa uma alteração de identidade.

Inclui teste que pausa o arquivamento real após a remoção das credenciais, verifica a espera e depois a resposta de exclusão. Mantém campanha sem meta de horas, preparo sob demanda, retomada e limpeza após confirmação.
