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

## Publicação

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
