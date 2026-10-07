## QMoney 2.0.61

Contas com banimento confirmado deixam de aparecer na lista de contas disponíveis e na seleção de campanhas.

- A leitura de contas reconcilia os banimentos confirmados que versões anteriores deixaram entre os acessos disponíveis. Usa o mesmo arquivo protegido de Banidas e a mesma remoção permanente já usada pelas campanhas.
- Verificações individuais e em lote arquivam os novos banimentos de acesso após consultar os serviços. Falhas temporárias, diagnósticos inconclusivos e retenções de saque não removem contas.
- Cadastros com banimento confirmado também ficam excluídos; uma verificação posterior de acesso ativo ao mesmo serviço prevalece sobre um diagnóstico antigo de cadastro.
- A remoção preserva os registros de campanha, os journals de envio e os registros de recuperação que protegem mídias pendentes.

Mantém a retirada de contas restritas durante a campanha sem repetir a validação já concluída e o preparo de um recorte por vez.
