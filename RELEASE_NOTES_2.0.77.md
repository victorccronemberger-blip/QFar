# QMoney 2.0.77

A validação das contas, a consulta de categorias e as operações autenticadas da campanha usam a conexão atribuída à identidade durante toda a operação. A criação da sessão e a renovação do acesso também ficam dentro dessa rota; uma falha de conexão não provoca uma tentativa direta silenciosa.

Sessões reaproveitadas precisam corresponder à conta solicitada. A retomada da avaliação continua no mesmo recibo, preservando o envio e as verificações de qualidade.

Inclui as correções da 2.0.76: reconciliação de históricos em lote, gravação concorrente protegida no Windows, acompanhamento limitado de recuperações e inventário Nymeria compartilhado entre consultas. Mantém campanha sem meta obrigatória de horas, aquisição sob demanda e proteção da mídia ainda referenciada por pendências.
