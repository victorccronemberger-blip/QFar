## QMoney 2.0.46

A campanha verifica o conteúdo novo disponível para cada conta antes de aceitar uma meta de horas. Uma seleção insuficiente mostra a capacidade e o déficit na revisão e bloqueia o início, inclusive em clientes que não solicitam a lista de clipes.

- A revisão considera vídeos já usados, reservas de sessões pendentes e a regra de sobreposição aplicada pelo motor.
- O início verifica novamente a capacidade da seleção revisada antes de registrar uma nova campanha.
- A gravação de checkpoints aguarda até 30 segundos por leituras locais concorrentes, evitando falhas prematuras em instalações com muitos registros. A identidade de cada sessão permanece protegida.
- Falhas locais indicam a recuperação da sessão preservada, sem atribuir o problema às credenciais da conta.
- O reset do histórico de vídeos usados conserva sessões interrompidas na recuperação e mantém seus clipes reservados.
- O acompanhamento mantém separados a meta de horas, os resultados dos envios, os recibos atuais e as prévias. A publicação de prévias não substitui o resultado da campanha.

Os registros e arquivos de campanhas anteriores continuam preservados. Esta atualização não inicia nem retoma envios automaticamente.
