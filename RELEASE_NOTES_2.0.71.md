# QMoney 2.0.71

Uma biblioteca Ego4D nova podia mostrar os recortes do índice portátil e recusar toda a fila no preparo por faltar a narração local. O QMoney agora obtém as anotações temporizadas oficiais com o acesso Ego4D configurado antes de criar a seleção e suas evidências. A validação de categoria, janela e sensores continua obrigatória.

O arquivo original de anotações é processado por fluxo: apenas o índice compacto dos vídeos com IMU permanece na Biblioteca. O programa preserva anotações existentes, serializa preparações simultâneas, fecha a conexão e remove o índice parcial se a leitura falhar. A primeira consulta acompanha o trabalho em segundo plano com prazo adequado; o leitor também acompanha o executável Windows.

Mantém a campanha sem meta obrigatória de horas: executa os recortes elegíveis até acabar o conteúdo ou o usuário apertar Parar. Pendências, histórico e limpeza após confirmação continuam protegidos.

Validação: regressões offline cobrem biblioteca nova, seleção e revalidação antes do download de mídia, preservação de arquivos, resposta interrompida e preparação concorrente. A publicação exige a suíte de backend, testes Qt e de interface e verificação do serviço empacotado. Nenhum envio real ao Minute é utilizado nesses testes.
