# Conteúdo Ego4D — auditoria de 18/09/2026

## Evidências locais

- Catálogo v2.0: 9.611 vídeos, 15.133 clipes; 2.288 vídeos com `has_imu=true`.
- Um clipe sem vídeo-pai no catálogo: `ef29f87d-3594-48fc-99db-0de55842e866`.
  O filtro agora o recusa mesmo quando a exigência de IMU está desativada.
- Nenhum ID de clipe duplicado, intervalo inválido, intervalo além do pai
  (tolerância de 0,1 s) ou endereço sem estrutura S3 foi encontrado pelo auditor.
- 144 MP4 originais em cache consultados por probe: nenhum sem metadados
  legíveis de vídeo e nenhuma divergência de duração superior a 2 segundos.
  Não foi executada decodificação integral desses arquivos.
- Amostragem visual: um frame central dos três primeiros arquivos por nome.
  Todos possuem cenário global Gardening. `0245a6d6-4a81-4c62-abd4-b2b90cf4e1fe`
  mostra uma mão abrindo uma porta; `03a8286a-4777-46b4-80f1-afc3135fcc67`
  mostra manuseio de plantas em vasos. O primeiro contém mãos/ferramenta e
  madeira, insuficiente para confirmar jardinagem isoladamente. A amostra não é
  aleatória nem representa o conjunto. Também não comprova erro na seleção de
  janelas do QMoney: são observações dos arquivos de origem.

Relatórios: `EGO4D_AUDIT.json` e `EGO4D_MEDIA_AUDIT.json`.
Auditoria reproduzível do catálogo:
`python scripts/audit_ego4d_catalog.py data/ego4d --output EGO4D_AUDIT.json`.

## Correções

- Cache MP4 exige probe com vídeo, dimensões e duração positiva, além do cabeçalho.
- Downloads de vídeo e CSV de IMU são validados antes da substituição atômica.
  A validação de CSV nessa etapa verifica estrutura; continuidade temporal é uma
  verificação distinta, já existente no preparador.
- Cliente HTTP alternativo detecta divergência em Content-Length.
- Arquivos temporários de catálogo e corte têm nomes únicos; timeout de corte
  limpa o parcial e preserva o destino anterior.
- Janelas inválidas são recusadas, sem fallback silencioso para o vídeo inteiro.

## Critérios para conteúdo de boa qualidade

| Aspecto | Evidência necessária | Limite atual |
|---|---|---|
| Origem | UID do vídeo-pai, UID do clipe, fonte S3 e versão do catálogo | Os IDs não substituem revisão do conteúdo |
| Integridade | Download completo, probe válido, duração coerente, decodificação sem falhas | Probe não lê todos os frames |
| Categoria | Narrações temporais dentro da janela e revisão visual da atividade | Cenário global e score heurístico não comprovam atividade contínua |
| Sensores | `has_imu`, cobertura do intervalo e continuidade dos dados reais | A flag sozinha não garante continuidade |
| Qualidade visual | Revisão de início/meio/fim e transições, oclusão, escuridão e câmera parada | Apenas um frame central de três arquivos foi inspecionado |
| Disponibilidade | Acesso confirmado ao objeto com credenciais válidas | Não houve teste remoto nem download de novos vídeos |

Os dados continuam identificados como conteúdo de dataset. Não foram alterados
origem, sensores ou identidade para aparentar outra captura. Não se deve afirmar
que todos os clipes estão aprovados para uma categoria apenas porque passaram no
filtro de texto. A revisão semântica/visual permanece uma etapa necessária.

Validação de código: **380 testes passaram**, incluindo oito regressões novas
de integridade Ego4D. Nenhum download remoto nem envio foi realizado.

## Referências primárias

- Vídeos, clips, padding e propriedades variáveis: https://ego4d-data.org/docs/data/videos/
- Metadados e origem: https://ego4d-data.org/docs/data/metadata/
- IMU e timestamps canônicos: https://ego4d-data.org/docs/data/imu/
- Download e manifests: https://ego4d-data.org/docs/CLI/

## Integração da diversidade na campanha

- Seleção automática intercala vídeos-pai: uma janela por pai em cada rodada.
  Prioriza pais sem clipes já concluídos para todas as contas selecionadas antes
  de novos cortes de pais já representados no histórico elegível.
- Nenhum candidato é descartado ou modificado pelo ordenador. A elegibilidade
  continua vindo do catálogo, independentemente da existência do MP4 no cache.
  A preferência por exports/duração vale dentro da prioridade de cada pai;
  diversidade pode exigir baixar outra origem em vez de reutilizar a mesma.
- O motor não limpa automaticamente o registro ao esgotar a seleção. Informa
  esgotamento e continua outras categorias, sem repetir envios silenciosamente.
- API de categorias informa quantidade de vídeos-pai e totais por provedor.
  A lista Qt distingue trechos de vídeos de origem; o tooltip mostra a cobertura.
  Origens ausentes nos metadados não são contadas como pais identificados.
- Teste de regressão confirma que uma transição neutra breve entre evidências
  recorrentes não elimina a atividade. Não foi introduzido veto por frame nem
  relaxamento de sensores, proveniência, restrições ou regras de categoria.

Comparação offline no índice portátil (95 tarefas): 10 tiveram aumento no número
de pais distintos entre os primeiros 12 candidatos; todas mantiveram a quantidade
de candidatos. Jardinagem: 4 → 12; lavagem de carro: 5 → 12; compras: 5 → 12.
Detalhes em `EGO4D_SELECTION_VALIDATION.json`. Essa comparação não representa o
catálogo completo nem uma validação visual das atividades.

Validação após integração: **389 testes Python passaram** e build Qt concluído.
Inclui teste API → runner → campanha da ordem A1/B1/A2 sem mídia local, preservação
do histórico esgotado, contagem de origens e transição neutra. Sem envios reais.
