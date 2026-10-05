## QMoney 2.0.32

Corrige os bloqueios de teste e publicação encontrados após a atualização 2.0.31.

- A consulta de clipes prontos do Acelerador mantém o catálogo portátil disponível quando os arquivos locais Ego4D estão ausentes ou incompletos, sem tentar baixar o catálogo pela AWS nessa consulta.
- Os testes de seleção reconhecem a expansão de clipes prontos nos modos Cache e Cache + dataset, inclusive com preparação desativada.
- As fixtures de metadados acompanham o contrato Minute 1.29 já implementado.
- O teste de empacotamento do navegador funciona com a política padrão de scripts do Windows, usando uma configuração restrita ao processo de teste.
- O workflow publica a release somente depois dos testes, build e assinatura, com o pacote Windows, SHA-256 e assinatura RSA.

As alterações de compatibilidade Minute 1.29 e aproveitamento do cache introduzidas na 2.0.31 foram preservadas.
