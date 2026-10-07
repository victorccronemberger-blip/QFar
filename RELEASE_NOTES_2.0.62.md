## QMoney 2.0.62

Contas com banimento confirmado deixam de aparecer na lista de contas disponíveis e na seleção de campanhas.

- A leitura de contas reconcilia os banimentos confirmados que versões anteriores deixaram entre os acessos disponíveis. Usa o mesmo arquivo protegido de Banidas e a mesma remoção permanente já usada pelas campanhas.
- Verificações individuais e em lote arquivam os novos banimentos de acesso após consultar os serviços. Falhas temporárias, diagnósticos inconclusivos e retenções de saque não removem contas.
- Cadastros com banimento confirmado também ficam excluídos; uma verificação posterior de acesso ativo ao mesmo serviço prevalece sobre um diagnóstico antigo de cadastro.
- A remoção preserva os registros de campanha, os journals de envio e os registros de recuperação que protegem mídias pendentes.

Mantém a retirada de contas restritas durante a campanha sem repetir a validação já concluída e o preparo de um recorte por vez.

A 2.0.61 não foi publicada: o CI detectou uma comparação instável de ponto flutuante no teste de limite de tempo do catálogo. A 2.0.62 verifica um instante após o limite, sem alterar os limites do serviço.
