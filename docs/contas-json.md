# Importação e exportação de contas

Na tela **Contas**, use **Importar JSON**, **Exportar todas** ou **Exportar selecionadas**. A seleção múltipla funciona com Ctrl e Shift.

O backup contém tokens de acesso e as senhas que estiverem salvas. Ele não é criptografado: guarde-o em um local privado. O arquivo não inclui histórico, vídeos, integrações ou perfis de aparelho.

## Importar

São aceitos backups exportados pelo QMoney, arquivos de token de sessão e listas de e-mail/senha em UTF-8, com ou sem BOM:

```json
[
  {"email": "conta@example.com", "password": "sua-senha"}
]
```

Também são aceitos o campo `senha` e um objeto com a lista em `accounts`. Limites: 10 MB e 1.000 registros.

A primeira etapa apenas valida o arquivo e mostra o resumo. Ao confirmar, o aplicativo importa os registros válidos, preserva os duplicados e informa os erros por linha. Os e-mails são comparados sem diferenciar maiúsculas e minúsculas ou espaços nas extremidades. Senhas mantêm seus caracteres originais.

Contas existentes nunca são substituídas pela importação. Uma conta removida pode ser restaurada explicitamente. Conflitos entre nomes de arquivos são rejeitados, sem substituir outra conta. Uma falha de autenticação ou gravação é relatada separadamente; as contas importadas com sucesso permanecem disponíveis. Reimportar o mesmo arquivo não cria duplicatas.

Contas com senha são autenticadas durante a importação. Tokens são restaurados localmente e podem já estar expirados ou revogados: use **Verificar todas** antes de iniciar uma campanha. A importação fica bloqueada durante campanhas ou consultas de saldos.

## Exportar

O arquivo usa o formato `qmoney-accounts`, versão `1`, inclui a data de exportação e uma lista `accounts`. O aplicativo grava o arquivo completo antes de substituir o destino. Nenhum backup é criado automaticamente; você escolhe onde salvá-lo.
