# QMoney 2.0.82

Uma campanha com várias contas podia interromper uma conta antes de abrir seu envio: a leitura simultânea de milhares de recibos excedia a espera curta pela trava local e era apresentada como registro inválido.

A leitura agora aguarda a operação local com prazo limitado. Se a trava continuar ocupada, o diagnóstico identifica uma condição temporária que pode ser repetida, preservando os registros. Arquivos corrompidos e identidades inválidas continuam bloqueando o envio.

As entregas já confirmadas permanecem registradas e não são reenviadas na retomada. A campanha mantém a proteção de mídia pendente, a limpeza após confirmação e a execução até acabar o conteúdo ou o usuário parar.
