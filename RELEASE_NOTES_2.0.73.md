# QMoney 2.0.73

A consulta de saldos aguarda o limite temporário da Crowtado e tenta novamente a mesma conta antes de prosseguir. Respeita `Retry-After`; quando o serviço não informa um prazo, aguarda 60 e depois 120 segundos. O botão Parar cancela a espera. Se três tentativas forem recusadas, as demais contas permanecem pendentes e seus saldos anteriores são preservados.

O diagnóstico identifica quando a Hostinger recusa o token da caixa de e-mail, indicando a renovação em Integrações. O campo agora se chama **Token Hostinger** e explica que serve para conectar ou renovar uma caixa. Credenciais continuam protegidas no cofre local.

As tentativas automáticas são exclusivas da consulta de saldos. Saques mantêm suas verificações e não são repetidos por essa alteração. Campanhas continuam sem meta obrigatória de horas, com aquisição de mídia sob demanda e proteção de arquivos referenciados por envios pendentes.

Validação: testes isolados de consulta, espera, parada, autenticação por e-mail e proteção dos saques; regressões de campanhas, catálogo, armazenamento e reset. A publicação exige os testes do backend, da interface nativa, do atualizador e do serviço empacotado no CI.
