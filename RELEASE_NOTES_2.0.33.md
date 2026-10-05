## QMoney 2.0.33

Revisa Carteira, Operação e Contas para apresentar estados verificáveis e preservar o progresso quando uma consulta, cadastro ou conexão falha.

- **Carteira:** destaca os valores em revisão e disponíveis para saque; distingue saldo confirmado, leitura vencida e consulta inconclusiva, preservando o último saldo conhecido após erros.
- **Restrições e saques:** mostra a retenção informada pela Crowtado mesmo quando o login funciona. Verifica saldo, elegibilidade e pagamentos em trânsito antes de solicitar saque, com mensagens específicas para falhas e bloqueios.
- **Operação:** reorganiza indicadores, controles e acompanhamento da campanha; corrige estados contraditórios e a recuperação da interface após respostas incompletas, interrupções e falhas de conexão.
- **Contas:** separa cadastro, acesso Minute e situação de saque Crowtado; acrescenta busca, filtros de pendências e detalhes das etapas. Melhora a organização das ações em telas menores e nos temas claro e escuro.
- **Criação e retomada:** salva o progresso do cadastro, permite retomar etapas pendentes, oferece cadastro sem referral e preenchimento de nascimento e gênero. Protege contra duplicação de pedidos, sobreposição de operações e perda do histórico após reinício.
- **Falhas externas:** diferencia banimento, autenticação, demografia obrigatória e tarefa Minute indisponível. Um cadastro parcial permanece identificado como incompleto até a confirmação das etapas exigidas.

Inclui testes de regressão do backend e da interface Qt para os fluxos de Carteira, Operação e criação de contas. A publicação do pacote Windows depende dos testes, da verificação do serviço empacotado e da assinatura RSA.

Validação local: 2.073 testes e 2.594 subtestes do backend aprovados; 43 testes Qt aprovados; aplicativo compilado com versão 2.0.33.
