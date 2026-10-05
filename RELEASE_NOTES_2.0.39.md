## QMoney 2.0.39

Quando a recuperação falhava com HTTP 409, a campanha era bloqueada e a interface mostrava uma tabela vazia com uma mensagem genérica. Agora a falha informa a etapa que precisa de revisão e permite copiar o diagnóstico.

- A leitura dos registros de envio usa a mesma proteção das gravações e da limpeza local. O bloqueio cobre apenas a listagem e leitura desses registros; a inspeção de históricos e arquivos de retomada usa a cópia obtida e libera as gravações de outras operações. Se a proteção estiver ocupada, a interface informa para aguardar e tentar novamente.
- Diagnósticos distinguem registro ilegível, identidade conflitante, histórico de reset inválido, índice de migração inválido e cópias divergentes na biblioteca e nesta instalação.
- A verificação da campanha oferece **Abrir recuperação** quando essa leitura falha. A janela tem **Tentar novamente** e **Copiar diagnóstico**, com versão, código da falha e referência anônima do registro. A nova tentativa faz uma leitura atualizada, sem reutilizar o erro em cache ou duplicar consultas em andamento.
- Uma leitura com erro aparece como **Leitura não concluída**, sem apresentar a lista como vazia. Reconciliar e retomar ficam indisponíveis até uma leitura válida.
- O diagnóstico não inclui contas, senhas, tokens, conteúdo dos registros ou caminhos privados. Nenhum registro ilegível é apagado, sobrescrito ou ignorado para liberar campanhas.

O HTTP 409 mostrado pelo usuário ocorre em outro computador. A causa específica dessa instalação ainda depende do diagnóstico copiado após atualizar; esta release não promete restaurar arquivos corrompidos automaticamente.

Inclui a verificação independente de Minute e Crowtado da v2.0.36 e a correção das categorias Ego4D da v2.0.35. Os testes usam dados isolados e serviços simulados; nenhuma campanha real foi iniciada.

Validação local: **2.174 testes Python e 2.604 subtestes**, todos aprovados. A suíte nativa de **45 testes** passou e as áreas alteradas foram revalidadas após os ajustes finais. Inclui leitura protegida, liberação de gravações durante inspeção do histórico, preservação de arquivos inválidos, diagnóstico privado, nova tentativa e revisão de campanhas. O executável tem versão 2.0.39; a publicação também exige a validação completa do pacote Windows no CI.

Os testes da biblioteca aguardam explicitamente a conclusão dos trabalhadores antes de conferir os resultados e remover os arquivos temporários. Isso elimina dependência de uma janela de um segundo no Windows, sem alterar a preservação dos arquivos nem as respostas esperadas. Após esse ajuste, 55 testes direcionados e 78 subtestes passaram.
