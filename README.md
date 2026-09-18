# QMoney

Aplicativo nativo para Windows, construído com Qt 6 e
[Qlementine](https://github.com/oclero/qlementine). Não existe interface web:
o serviço HTTP interno atende exclusivamente ao aplicativo desktop.

## Para o usuário

1. Baixe `QMoney-windows-x64.zip` na página de Releases.
2. Extraia a pasta em um local permanente.
3. Abra somente `QMoney.exe`.

O QMoney inicia o serviço local automaticamente. Dados de contas, tokens,
campanhas e preferências ficam em `%LOCALAPPDATA%\QMoney` e não são apagados
por atualizações. Quando uma nova Release estiver disponível, o próprio app
oferecerá a instalação, validará o SHA-256 e abrirá novamente após concluir.

Na primeira abertura, use a página **Integrações** para configurar e testar:

- credenciais AWS temporárias recebidas após a aprovação da licença Ego4D;
- token da API Mail da Hostinger, necessário somente para registrar contas;
- catálogo Ego4D, biblioteca HoloAssist e ferramentas de vídeo incluídas.

Os campos protegidos nunca são preenchidos de volta na tela. Ego4D e Hostinger
são validados antes de salvar, e os valores ficam criptografados pelo DPAPI do
Windows em `%LOCALAPPDATA%\QMoney\secrets\integrations.dat`, acessível somente
ao mesmo usuário do Windows.

## Migração de contas

Na aba **Contas**, use **Atualizar organização Crowtado** para migrar as contas
cadastradas para Datoric (`PE8EAR5V`). Se elas estiverem em um arquivo, use
**Importar JSON** primeiro. O progresso aparece na própria tela e **Ver relatório**
mostra o resultado por conta, com opção de salvar um JSON sem credenciais.

Contas com domínio `claru.ai` ou seus subdomínios, como `supply.claru.ai`, são
identificadas como Claru e ignoradas pela migração, sem login nem aplicação de
convite. As demais contas seguem a política Crowtado. A tabela identifica o tipo
e a organização salva; o início da campanha continua exigindo confirmação ao vivo.

Mantenha o QMoney aberto durante a migração. Se houver interrupção, o relatório
parcial é preservado e o lote pode ser executado novamente; contas já na
organização nova não recebem outro convite.

## Verificação de contas

Em **Contas**, uma verificação inconclusiva significa que o serviço não pôde
confirmar o acesso naquele momento; não significa conta inválida. O QMoney tenta
novamente uma vez em falhas temporárias e preserva a data do último acesso
confirmado. **Reconectar acesso** indica uma pendência de sessão local;
**Organização pendente** indica vínculo de destino ausente. Somente uma resposta
explícita de restrição é apresentada como **Restrição confirmada**.

**Verificar todas** não aplica convites nem migra organizações. Use a ação de
migração para isso. Erros de campanha não substituem a verificação da conta.
Em Saldos, um `*` indica o último valor salvo quando a atualização falhou; passe
o mouse para consultar o diagnóstico. Não remova uma conta para resolver falhas
temporárias de rede, saldo ou envio.

## Build e publicação

Cada Release precisa conter exatamente estes três arquivos:

- `QMoney-windows-x64.zip`
- `QMoney-windows-x64.zip.sha256`
- `QMoney-windows-x64.zip.sig` (assinatura RSA-3072)

O workflow `Build e publicar QMoney` gera os três automaticamente quando uma
Release é publicada no GitHub. Configure o secret `QMONEY_UPDATE_PRIVATE_KEY`
com a chave privada correspondente à chave pública embutida no aplicativo. A
versão vem da tag, por exemplo `v1.1.0`.

Para montar uma versão local, com Qt e FFmpeg já disponíveis na árvore:

```powershell
.\scripts\build_release.ps1 -Version 1.0.0
```

O resultado fica em `release\QMoney-windows-x64.zip`.

## Segurança e dados

`data/`, `secrets/`, `.env`, builds, ferramentas baixadas e artefatos de release
são ignorados pelo Git. Nunca publique credenciais ou dados operacionais.
