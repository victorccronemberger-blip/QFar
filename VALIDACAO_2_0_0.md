# Verificação da versão publicada — 26/09/2026

## Resultado

Foi encontrada uma lacuna na interface: a continuação após detectar contas restritas enviava a solicitação de início sem passar pela revisão final de contas/clipes. Corrigida no código local; esta correção ainda não integra o ZIP publicado da 2.0.0.

Agora o usuário revisa somente as contas aprovadas, vê o aviso de remoção e confirma antes de qualquer solicitação de início/remoção. Voltar preserva as contas. A prévia mantém o recibo original validado pelo backend.

## Evidências

- 615 testes backend passaram em diretório isolado: campanhas, prévia/reset, deduplicação, cancelamento, pausa, recuperação, migração e fluxo Wise/Dots com provedores controlados.
- 3 testes Qt de resumo/API/atualização passaram; o teste nativo de transação/rollback do atualizador também passou.
- 3 testes da interface passaram após a correção: operação/pausa/recuperação/histórico, cancelamento da continuação sem solicitar início, confirmação da continuação com o recibo e a remoção autorizada.
- ZIP baixado diretamente da release publicada, extraído e verificado. SHA-256: `1b37d4714e3dde70654a9067110b2e22b70a3e20bf4eec1fbd28d911f55824f8`. Assinatura validada com a chave pública do atualizador.
- O QMoneyService.exe extraído desse ZIP passou pelo teste em diretórios novos: primeira inicialização, credenciais preservadas no reinício, dados separados entre clientes, autenticação da API e recuperação vazia.

Logs locais em `build/v2-postrelease-backend.log`, `build/v2-postrelease-native.log`, `build/v2-audit-fix-tests.log` e `build/v2-published-audit/service-report.json`.

## Limites

Não houve saques financeiros nem uploads produtivos. Os testes de negócio usam provedores controlados; não comprovam disponibilidade futura dos serviços externos. O executável de serviço publicado foi testado neste Windows em diretórios novos, não em uma VM recém-instalada. A aprovação dos testes não significa ausência de qualquer defeito.
