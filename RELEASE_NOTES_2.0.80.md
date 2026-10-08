# QMoney 2.0.80

Uma falha transitória de DNS durante o envio do vídeo podia ser confundida com uma recusa HTTP, deixando o recibo sem retomada e encerrando a campanha após as demais contas concluírem. Os códigos de transporte do curl agora são separados dos status HTTP.

O QMoney retoma o MP4 e o ZIP persistidos pelo mesmo identificador de sessão e de upload, após validar conta, organização, conteúdo e metadados. Não cria outra sessão nem altera o pacote já preparado. A campanha aplica o orçamento de tentativas da conta e mostra a recuperação na interface.

O próximo recorte só é adquirido depois da confirmação e da limpeza do lote atual. Se a falha persistir ou o conteúdo não corresponder ao recibo, os arquivos são preservados para revisão. Mantém a limpeza na parada, a exclusão de contas com restrição confirmada e a execução sem meta obrigatória de horas.
