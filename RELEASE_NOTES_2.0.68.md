# QMoney 2.0.68

Uma instalação em outro PC agora reconhece o arquivo privado `nymeria_plus_download_urls.json` ao lado do executável. O serviço importa esse catálogo pela Biblioteca existente e sincroniza apenas metadados e anotações em segundo plano. Vídeos e IMU continuam sendo adquiridos durante a campanha. A interface acompanha a preparação, e contas e verificação de saúde continuam disponíveis.

A configuração existente da Biblioteca é preservada. Um arquivo ausente, inválido ou acima de 32 MiB não impede o serviço de iniciar e recebe um diagnóstico na Biblioteca. Links assinados e credenciais não acompanham a release pública; a configuração portátil é fornecida separadamente pelo titular do dataset.

A interface nativa remove variáveis Nymeria herdadas antes de iniciar o serviço empacotado, evitando referências ao Python ou à Biblioteca de outro PC. O SDK oficial já acompanha a instalação.

Validação: testes de importação sem rede, preservação de catálogo, resposta do serviço durante sincronização e seleção da Biblioteca. A verificação do executável empacotado inclui configuração automática em um diretório novo, sem Python externo e sem baixar VRS ou IMU. Após a sincronização, o teste exige catálogo disponível e confirma que fontes grandes ainda não foram adquiridas. Os testes normalizam nomes curtos de diretórios temporários do Windows antes de consultar caminhos internos. Nenhum envio real é iniciado pelos testes.
