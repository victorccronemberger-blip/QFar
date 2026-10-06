## QMoney 2.0.47

Nymeria usa a Biblioteca local do QMoney e o SDK Project Aria Gen 1 incluído na instalação Windows. Metadados sem vídeo, IMU e narrações verificadas continuam como acervo incompleto.

- Cada tarefa recebe somente intervalos comprovados pelas narrações da sequência, no mesmo relógio DEVICE_TIME do vídeo e dos sensores.
- O preparo preserva os PTS reais, orienta a imagem conforme o SDK Aria e ancora a IMU no primeiro frame capturado. Os diagnósticos vêm das amostras efetivamente reamostradas.
- Encodes e partes de Nymeria preservam os timestamps. Cada parte recebe IMU e diagnóstico próprios, alinhados ao seu primeiro frame real.
- Conteúdo ou vínculo de tarefa alterado interrompe o preparo antes da autenticação e do envio.

Inclui as correções de capacidade, checkpoints e acompanhamento da versão 2.0.46. A atualização não inicia nem retoma campanhas automaticamente.
