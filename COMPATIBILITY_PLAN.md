# Meta de compatibilidade — QMoney

## Objetivo e limites

Melhorar a correção dos contratos e o respeito às políticas da integração.
Validar localmente sem enviar uploads, eventos ou solicitações de teste ao
serviço real. A equivalência com o aplicativo Android não é um critério de
aceitação: o cliente Windows precisa representar a origem real dos dados.

Não aperfeiçoar geração de identidade Android, localização, sensores ou eventos
de uso simulados. O pipeline existente contém essas suposições e precisa de uma
definição de integração autorizada para operar com dados reais; esta etapa não
certifica esse pipeline nem a aceitação de conteúdo pelo servidor.

## Etapas e critérios

1. **Inicialização recuperável:** consultas inválidas não são sucesso; novas
   tentativas têm intervalo limitado; restrições locais são verificadas mesmo
   após uma inicialização anterior. Não publicar abertura Android automaticamente.
2. **Configuração por sessão:** validar tipos e relações dos limites remotos;
   guardar a resposta na sessão sem modificar os limites globais de outra conta.
   No registro de upload, rejeitar duração e horário incompatíveis com a política
   da sessão, sem alterar os metadados para tentar fazê-los caber nos limites.
3. **Políticas explícitas:** recusar novo registro quando a configuração ou a
   política de câmera não estiver verificada, a câmera for negada, a organização
   estiver restrita ou a origem não estiver permitida. Recusas devem ter códigos
   distintos de falhas temporárias; leitura e diagnóstico permanecem disponíveis.
4. **Regressão local:** testar falha seguida de recuperação, limites distintos
   entre sessões, contratos incompletos, cache, ausência de telemetria automática
   e bloqueios antes do transporte. Executar a suíte isolada do projeto.

## Pendências externas para compatibilidade integral

- Confirmar com o provedor a integração permitida para um cliente Windows e as
  fontes de gravação aceitas; o APK e a OpenAPI local são referências históricas.
- Definir proveniência real de vídeo, dispositivo e sensores. Perfis gerados por
  conta não constituem evidência de dispositivo físico ou autorização de captura.
- Validar campos e fluxos em ambiente de teste autorizado pelo serviço.
- Definir diagnóstico próprio do QMoney, com minimização de dados e consentimento
  adequado, em vez de reproduzir analytics do aplicativo de terceiros.

## Publicação

Entregar primeiro as alterações locais e os resultados dos testes. Git/release
somente quando solicitado. Não prometer compatibilidade integral ou ausência de
erros a partir dos testes locais.

## Resultado da implementação local

- Inicialização por sessão com cache de 60 segundos e nova tentativa após falhas
  em intervalos de 5 a 60 segundos; troca de identidade invalida o estado anterior.
- Contrato de configuração validado sem conversão silenciosa de tipos ou limites.
  A configuração remota deixa de sobrescrever `config._EFFECTIVE_LIMITS` global.
- `POST /api/v1/uploads` passa por validação de versão, configuração, câmera,
  organização, origem, duração e horário antes de ser enviado. Estado desconhecido
  não é autorização; apenas o estado de organização `active` é aceito nesta etapa.
- Leituras JSON não interpretam corpos HTTP de erro como respostas bem-sucedidas.
- Telemetria de abertura Android removida da inicialização automática.
- Diagnóstico `policy` apresentado sem classificar a conta como banida ou sugerir
  troca de senha. A aplicação continua distinguindo restrição e falha temporária.

### Limitações que permanecem

Os preparadores legados ainda usam limites padrão locais para planejar clipes;
o registro é recusado se o resultado não atender à política atual da sessão.
Não foi aperfeiçoada a adaptação de conteúdo ou sensores desses preparadores.
A validação local de novos registros não certifica fluxos antigos de transporte
anterior ao registro, nem todo o ciclo de retomada de uploads já existentes.
Perfis de dispositivo gerados continuam sendo dívida técnica do pipeline legado;
uma resposta favorável da allowlist não comprova captura em hardware real.

Validação da primeira etapa: **327 testes passaram** na suíte isolada e `git diff --check`
sem erros. As quatro etapas locais foram concluídas dentro do escopo acima.
Nenhum teste de aceitação contra o serviço
real foi realizado. A publicação ainda não faz parte desta entrega.

## Contratos, autenticação e recuperação — segunda etapa

- Login e renovação exigem tokens não vazios e validade positiva. Respostas
  inválidas não são persistidas; a renovação valida tudo antes de substituir
  credenciais. Erros de leitura do login não exibem o corpo da resposta.
- O bloqueio de versão é observado também nas respostas após refresh e relogin.
- Conflitos de conclusão exigem consulta do estado remoto; palavras soltas no
  corpo de erro não comprovam conclusão. JSON inválido não vira sucesso.
- Journals aguardando finalização não voltam ao transporte nem dependem de
  arquivo local. A conta autenticada delimita a recuperação.
- A finalização exige conta e organização consistentes, quantidade esperada
  consistente e índices completos, únicos e válidos. Falhas terminais não entram
  automaticamente no conjunto de chunks prontos.
- Foram adicionados 11 testes de regressão com respostas simuladas, incluindo
  credenciais inválidas, bloqueio após renovação, conflito e retomadas isoladas.
  A suíte completa passou com **338 testes**. Nenhum envio ao serviço real.

Essas verificações corrigem os caminhos descritos; não constituem certificação
de todos os contratos do servidor nem alteram a origem dos vídeos ou sensores.

## Inventário Duvi (Minute 1.22.0) — terceira etapa

Inventário completo do catálogo Duvi (política, segurança, anti-fraude,
telemetria, OTA). Classes: **A** fechar no código, **B** aproximar no Windows,
**C** impossível/forjar (não-meta), **D** já alinhado.

Referência operacional: plano de sessão aprovado (P0–P7). A auditoria estática
`docs/auditoria_duvi_2026-09-18.md` está **obsoleta** em relação ao código
atual (warmup, câmera, VPN por request, política por sessão).

### D — já alinhado
Headers Bearer/`X-App-Version`/`X-Device-Id`/UA OkHttp; location só em rotas
geo; `RecordingPolicy` por sessão no register; câmera desconhecida/negada
bloqueia; `meta.source` ∈ `cameraSources`; só `userState=active`; leituras não
tratam HTTP de erro como sucesso.

### A — fechar (P1–P4)
| ID | Item | Status |
|---|---|---|
| A1 | Latch só `detail.error=app_version_too_old` + `min_version` | feito |
| A2 | Latch em todas as mutações autenticadas | feito |
| A3 | Camera decision = OpenAPI/Hermes (sem deny Android) | feito |
| A4 | Planners usam política da sessão | feito |
| A5 | Sem clamp silencioso de `recorded_at` | feito |
| A6 | `/orgs/{org}/quota` no write path | feito |
| A7 | 403 `device` / `uber-device` tipados | feito |
| A8 | Doc auditoria obsoleta marcada | feito |

### B — aproximar Windows (P5–P7)
| ID | Item | Status |
|---|---|---|
| B1 | VPN enforce default ON (`MINUTE_VPN_ENFORCE=0` desliga) | feito |
| B5 | `REQUIRE_CURL` default ON (`MINUTE_REQUIRE_CURL=0` desliga) | feito |
| B2/B3 | `app_opened` flag (`MINUTE_PUBLISH_APP_OPENED`) + `os_version=android {sdk}` | feito |
| B4 | geo só com coords reais (já omitia sem LAT/LNG; quota gate em A6) | feito |
| B6 | diagnóstico próprio (VPN/geo/quota/version/device sem PostHog Baker) | feito |

### C — não-meta até autorização de proveniência
PairIP, OTA/devFlags, PostHog/Sentry Baker, Uber lock UI, fingerprint
emulador→analytics, Trinet SEI hardware, reCAPTCHA phone/FIDO.

**Catálogo público (2026-09-18):** `resources/samsung_device_catalog.json` +
`device_catalog.py` alimentam modelo/OS/SDK a partir de Play/MobileModels/
SamMobile. Identificadores derivados continuam sintéticos (não há lista
pública legítima de SSAIDs). A procedência agora acompanha o perfil persistido;
perfis antigos sem esse campo permanecem `unknown`, sem trocar seus IDs.
O campo informado na âncora usa `anchor_reported`, não `minute_verified`:
validar formato e proprietário não comprova a origem Android do identificador.

### Correções da revisão de 18/09

- Quota exige aprovação e `canUpload=true`, sem motivo de bloqueio; negativas
  explícitas prevalecem. Campos de autorização incompletos não entram no cache.
- Parser do latch aceita somente três componentes numéricos. `ensure_auth`
  preserva a leitura de diagnóstico; mutações continuam bloqueadas no request.
- VPN considera adaptadores ativos; erro de sondagem impede a chamada com
  estado de serviço indisponível. É uma heurística Windows, não certificação
  de equivalência ao detector Android.
- Runner unittest configura seus próprios defaults isolados de transporte.
  Testes de âncora usam arquivo temporário independente dos dados pessoais.
- O wheel inclui o catálogo Samsung. `app/opened` só confirma publicação
  após HTTP 2xx e reinicia esse estado ao trocar a conta.
- Leitor de âncora valida schemaVersion 2 e formato do ID informado. Nenhuma
  regeneração em massa ou certificação automática de SSAID foi implementada.
