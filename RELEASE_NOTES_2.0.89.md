# QMoney 2.0.89

O criador preserva contas com bloqueio de acesso do Clerk (`user_locked`), distinguindo esse diagnóstico de banimento confirmado. Estados Minute `on_hold` e `inactive` permanecem disponíveis para nova verificação, sem autorizar exclusão permanente.

O cadastro grava a intenção antes de enviar e confirma a criação Minute separadamente do login. Uma resposta perdida ou falha depois do envio permite verificar o mesmo acesso na retomada, sem repetir automaticamente o cadastro. Falhas comprovadamente anteriores ao envio mantêm a tentativa limitada.

O lote interrompe novas identidades após limite, bloqueio ou resultado incerto. Respostas 429 preservam o prazo do provedor em armazenamento local; abrir o aplicativo novamente ou iniciar outro lote não ignora esse prazo. Sem prazo informado, aguarda cinco minutos como intervalo conservador local. A pré-verificação mostra a espera sem consultar os provedores.

Os diagnósticos preservam código permitido do provedor, etapa, HTTP e possibilidade de efeito remoto, sem guardar o corpo da resposta. A evidência sanitizada de um banimento confirmado acompanha o arquivo de banidas após a limpeza dos checkpoints operacionais.

Inclui regressões para retomada, falha de armazenamento, bloqueio temporário, 429 persistente e interface sem repetição. As correções não constituem garantia de aprovação nem prevenção de banimento pelo provedor. Mantém as etapas manuais do site, campanha sem meta obrigatória, preparo sob demanda e proteção da mídia pendente.
