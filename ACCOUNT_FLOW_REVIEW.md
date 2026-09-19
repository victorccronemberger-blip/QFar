# Revisão do fluxo e armazenamento de contas — 19/09/2026

## Isolamento solicitado: cada computador/usuário com seus dados

- Removida a cópia automática de `secrets/`, históricos, configurações e
  `device_state` a partir da biblioteca/pasta antiga durante a inicialização.
- O pacote continua usando `%LOCALAPPDATA%/QMoney` do usuário Windows atual.
  A biblioteca de mídia não é uma fonte de contas ou segredos.
- O subprocesso do pacote não herda variáveis de credenciais/políticas dos
  provedores do ambiente externo. Os arquivos AWS apontam para o perfil local
  do QMoney, sem fallback implícito para o arquivo global; configurações salvas
  no cofre e `.env` do próprio perfil continuam sendo desse usuário.
- A interface gera uma chave por execução e a envia ao serviço por ambiente,
  sem colocá-la na URL ou na linha de comando. Todas as chamadas locais exigem
  essa chave no header `X-QMoney-Session`. O serviço empacotado recusa iniciar
  sem a chave. O modo Python de desenvolvimento sem chave mantém seu contrato
  anterior; não deve ser exposto como servidor de produção.
- Não foi acrescentada sincronização, exportação ou importação automática.
  Dados já misturados por versões antigas não foram apagados nem redistribuídos.

Limite: não é isolamento entre pessoas que compartilham o mesmo login Windows,
nem contra administradores ou processos comprometidos desse usuário. Os arquivos
de tokens e senhas ainda são legíveis pelo próprio usuário. O pacote de release
não deve conter `data/`, `secrets/` ou `.env`; essas pastas são ignoradas no Git.

Validação: 425 testes Python aprovados, incluindo DPAPI real, corrupção de
arquivos, respostas de erro locais e rejeição de chave ausente/de outra execução.
Build desktop aprovado. O teste Qt verifica o envio do header por conexão TCP
local real, além dos testes existentes de resposta e timeout.

## Escopo e limites

Inspeção do cadastro, integrações, credenciais, listagem e transferência entre
computadores. Correções limitadas à integridade dos arquivos locais e à
comunicação de falhas de armazenamento. Nenhuma conta real foi criada e nenhum
arquivo de credenciais do usuário foi alterado nos testes.

O produto é distribuído para Windows x64. Não há evidência para garantir
funcionamento em qualquer computador. Os testes foram executados neste Windows;
não constituem homologação de uma segunda máquina, instalação limpa, proxy
corporativo, antivírus ou versões diferentes do Windows.

## Fluxo observado

1. A integração Hostinger fornece as rotas de domínio e acesso à caixa de e-mail.
2. O cadastro Crowtado é conduzido pelo navegador. O perfil do navegador é local.
3. Após confirmar criação ou acesso existente, a senha é salva localmente.
4. O fluxo preenche demografia, registra/autentica no Minute, verifica organização
   e vincula o cadastro. No final, tenta validar acesso aos dois serviços.
5. A lista principal deriva dos arquivos `secrets/token_*.json`. A existência de
   senha Crowtado sozinha não faz a conta aparecer nessa lista.

O código atual gera dados demográficos e identidades para cadastros. Esta revisão
não certifica a veracidade desses dados nem aperfeiçoa cadastro em massa ou
contorno das verificações do provedor. Um ensaio real legítimo exige identidades
e informações autorizadas e verdadeiras.

## Correções implementadas

- Cofre existente ilegível não pode mais ser sobrescrito como se estivesse vazio.
  A migração de integrações exige leitura íntegra e a API informa erro JSON 409
  `local_vault_unreadable`, preservando o arquivo original.
- Arquivo de senhas corrompido, com estrutura inválida ou falha de leitura não
  é substituído pelo salvamento de outra conta.
- Falha local ao salvar uma conta autenticada é informada como resultado parcial,
  sem sinalizar sucesso completo nem restaurar indevidamente uma conta removida.
- Um token com UTF-8 inválido ou e-mail de tipo incorreto não derruba a listagem
  das demais contas. Preferências com `org_keys` inválido recebem tratamento
  defensivo, sem modificar os arquivos.

## Pendências identificadas

- **Cadastros parciais sem visibilidade:** a senha pode estar salva após criação
  Crowtado, mas a lista principal só inclui contas com token Minute. Interrupção
  entre essas etapas deixa um cadastro existente sem representação nessa lista.
- **Lote volátil:** `_BULK_REGISTER_STATE` vive em memória; encerrar o processo
  perde seu progresso detalhado. Credenciais salvas e estado do cadastro são
  conceitos diferentes. Não há journal durável completo desse fluxo.
- **Estado terminal inconsistente:** se a geração de identidade falha e define
  o lote como `failed`, o `finally` do worker pode sobrescrever esse estado com
  `done`. Achado estático, não alterado nesta rodada de proteção de dados.
- **Credenciais em texto legível:** `crowtado_passwords.json`, tokens JSON e
  exportações de contas contêm credenciais. O DPAPI protege as integrações, não
  todos esses arquivos. O backup exportado é sensível.
- **Cofre não portátil por cópia:** `integrations.dat` usa DPAPI associado ao
  usuário Windows. Copiá-lo para outra máquina/usuário não garante leitura.
  A correção preserva o arquivo ilegível, mas não o torna transferível.
- **Teste remoto pendente:** presença do navegador, token, domínio ou senha não
  comprova autenticação, recebimento de e-mail, vínculo ou cadastro completo.

## Migração suportada pelos caminhos existentes

Usar a distribuição Windows completa, preservando `runtime/`; importar as contas
pelo recurso de exportação/importação, e configurar as integrações no usuário
Windows de destino. O importador valida documentos, evita substituições
conflitantes e tenta restaurar os arquivos anteriores quando uma gravação falha.
Histórico de campanhas e identidade de aparelho não fazem parte do backup de
contas. Não copiar `integrations.dat` como substituto da configuração no destino.

## Evidências de teste

Novos testes usam arquivos temporários, casos de corrupção e resposta HTTP local.
Um teste usa **DPAPI real**, grava e relê o cofre em pasta com espaços e acentos,
confirma que a credencial de teste não aparece em texto no arquivo e preserva
uma integração ao atualizar outra. A falha de descriptografia de outro usuário
é simulada; não foi executado um segundo usuário Windows.

Comando: `.venv/Scripts/python.exe scripts/run_tests.py`.

## Dependências e preservação da instalação

- A distribuição Windows x64 já inclui Python, Qt, FFmpeg/FFprobe, Chromium e
  runtimes. Esses componentes são privados do aplicativo, sem substituir
  instalações globais do computador.
- Novo diagnóstico local `/api/runtime`: executa FFmpeg/FFprobe, verifica o
  navegador privado e carrega a biblioteca curl sem fazer requisições externas.
  Ao detectar componentes ausentes na distribuição, a interface encaminha ao
  reparo existente por pacote assinado. O diálogo de confirmação/reinício desse
  fluxo continua existindo. Não instala automaticamente ferramentas arbitrárias.
- O atualizador rejeita pacotes contendo caminhos privados; não reutiliza nem
  apaga backups anteriores e mantém o ZIP recebido. Arquivos de programa
  substituídos ficam no backup; arquivos extras são mantidos. Diretórios de
  extração são exclusivos de cada execução, sem limpar pastas preexistentes.
- Teste nativo executou instalação e rollback com arquivos temporários e
  verificou preservação de histórico, credenciais, `.env`, arquivos extras e
  backups. Os dois testes Qt passaram e o desktop compilou.
- Limites: não houve teste em um segundo Windows limpo. Se o próprio executável
  não iniciar, o diagnóstico interno não roda: é necessário extrair a distribuição
  completa. Rede, permissões do Windows, drivers e sistemas operacionais não
  suportados não são resolvidos automaticamente por esse reparo.

Alterações preparadas para a versão 1.0.49. O teste de dependências também carrega
e fecha a biblioteca curl real, sem acesso à rede.
