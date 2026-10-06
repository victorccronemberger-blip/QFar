## QMoney 2.0.43

Uma conta com restrição confirmada pela plataforma sai da campanha e vai para **Banidas**.

- A verificação arquiva o acesso local e a senha, e a revisão segue só com as contas que continuam válidas.
- Falha de rede, senha recusada ou tempo esgotado não entra nessa lista.
- Se o registro de banidas não puder ser gravado, nenhuma conta é removida e a campanha não começa.
- Se todas as contas selecionadas estiverem restritas, elas vão para **Banidas** e não há campanha para iniciar.

O pacote Windows é gerado e assinado pelo CI ao publicar a tag `v2.0.43`.
