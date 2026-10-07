## QMoney 2.0.51

Quando a verificação encontra uma conta com restrição confirmada, a campanha retira essa conta e continua com as demais usando a mesma validação. Isso também funciona quando a restrição aparece ao consultar categorias ou o catálogo da primeira conta. A meta de horas precisa estar coberta para as contas restantes; falhas de conexão ou autenticação continuam exigindo revisão.

- **Todas compatíveis** acompanha as tarefas disponíveis ao recarregar o catálogo. A escolha de tarefas indisponíveis não é mantida como conteúdo utilizável.
- **Biblioteca → Acervo Nymeria** importa o manifesto completo, sincroniza as anotações, pesquisa as sequências e planeja a aquisição conforme tarefas, duração, meta e reserva de disco.
- Os downloads usam os arquivos e hashes oficiais, preservam originais e parciais verificáveis e medem o vídeo e o IMU reais pelo SDK Aria antes de disponibilizar a fonte à campanha.
- O planejamento distingue horas declaradas, atividade compatível nas anotações e fontes efetivamente medidas. Metadados sozinhos não tornam um vídeo pronto para envio.
- Corrige classificações indevidas de jogos, roupas e decoração como montagem de móveis e de aspiração de cômodos como limpeza interna de carro no Nymeria.
- A seleção Ego4D reutiliza a classificação das anotações dentro da mesma consulta, mantendo as verificações de atividade, sensores e origem em cada limite de preparação e envio. Marcadores antigos de cache são recusados antes de reler os vídeos inteiros; arquivos existentes são preservados.
- A preparação da Biblioteca impede início de envios e Reset completo enquanto está em execução; fechar o aplicativo aguarda a parada e preserva os arquivos verificados.

Preserva o Reset completo da versão 2.0.50. Importar ou preparar conteúdo na Biblioteca não envia, avalia nem finaliza sessões no Minute.
