## QMoney 2.0.42

A verificação da campanha passa a rodar em segundo plano. A tela mostra o progresso, inclusive quando a consulta das contas demora, e nenhum envio começa antes da revisão.

- A interface consulta o serviço local em intervalos curtos, sem manter um único pedido aberto durante toda a verificação.
- Se a comunicação falhar, a mensagem explica o problema em português e indica **Corrigir instalação** quando o componente local não responde.
- **Corrigir instalação** fica no topo da tela e em **Integrações**. O reparo baixa de novo o pacote oficial assinado, preserva contas, credenciais, campanhas e configurações, e pode reinstalar a mesma versão.
- O aplicativo espera o encerramento seguro das operações antes de instalar.

A origem da campanha continua com **Ego4D**, **Nymeria** e **Ambos**. O pacote Windows é gerado e assinado pelo CI ao publicar a tag `v2.0.42`.
