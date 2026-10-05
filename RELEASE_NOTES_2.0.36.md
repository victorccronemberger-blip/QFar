## QMoney 2.0.36

A verificação de contas agora consulta Minute e Crowtado separadamente. Identifica banimento em apenas um serviço ou nos dois, sem confundir uma consulta inconclusiva com acesso liberado.

- Crowtado: reutiliza o mesmo login Clerk usado na criação, incluindo código de e-mail automático, e consulta retenção e elegibilidade de saque. Login recusado com restrição explícita e saque suspenso são apresentados como conta banida, indicando a causa.
- Minute: verifica o perfil, a organização correta e `quality-screen`; estados `on_hold` e `inactive` confirmam restrição. Respostas incompletas ou indisponíveis ficam inconclusivas.
- Contas: colunas individuais para Minute e Crowtado, diagnóstico dos dois serviços, datas e histórico por serviço. A verificação também funciona quando o acesso Minute ainda não está conectado.
- A última restrição é preservada quando uma consulta falha. Só uma verificação conclusiva pode substituí-la. Registros antigos continuam atribuídos ao Minute, pois não comprovavam acesso Crowtado.
- Uma verificação não apaga os acessos da conta. Assim, a restrição de um serviço não elimina a possibilidade de consultar o outro.
- Contas arquivadas: monitor verifica ambos os serviços e não marca a conta como desbanida se a Crowtado ainda estiver restrita. Saque permanece bloqueado quando a verificação Crowtado está banida ou inconclusiva.
- Verificações usam o proxy já associado à identidade, quando houver. Falhas dessa rota ficam inconclusivas, sem fallback para outro IP.

Validação real: duas contas existentes consultadas pelo mesmo fluxo de login da criação; uma passou no login, na elegibilidade Crowtado e no estado ativo do Minute. A outra confirmou restrição no login Crowtado. Nenhuma conta nova, campanha ou solicitação de saque foi executada.

Validação local: **2.164 testes Python e 2.604 subtestes**, todos aprovados, e **44 testes nativos**. Inclui a matriz de resultados dos dois serviços, preservação de histórico, falhas de rede/proxy, dados corrompidos e exibição independente na interface. O executável foi compilado com versão 2.0.36; a publicação passa também pela verificação completa do pacote Windows no CI.

Inclui a correção da v2.0.35 para categorias Ego4D zeradas por nomes traduzidos na API Minute.
