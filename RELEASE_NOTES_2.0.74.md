# QMoney 2.0.74

A campanha não deve tratar uma falha transitória da avaliação como defeito no preparo do vídeo. A avaliação agora repete respostas de transporte HTTP -1, 408 e 5xx com limite e espera, usando o mesmo recibo. Não cria outra sessão nem reenvia o vídeo. HTTP 429 permanece pendente, sem uma sequência imediata de novas consultas.

Se a avaliação continuar indisponível, a campanha tenta uma retomada limitada do recibo já persistido antes de interromper o lote. Os registros antigos com o diagnóstico exato de avaliação indisponível também permitem retomada em Recuperação de envios. Reprovações de qualidade, respostas inválidas e recibos sem identificação continuam exigindo revisão; somente uma avaliação válida permite finalizar.

A recuperação permite buscar conta, clipe ou sessão e retomar apenas o registro selecionado, preservando as outras pendências antigas da mesma conta.

Falhas classificadas antes de qualquer recibo retiram a conta apenas daquela execução, permitindo continuar com as demais. A conta e o seu cadastro permanecem preservados. Uma pendência remota não confirmada continua protegendo sua mídia e impedindo aquisição ilimitada de novos arquivos. O diagnóstico de lote incompleto agora identifica a falta de confirmação, sem atribuí-la ao preparo.

Validação: testes isolados de avaliação, journal persistido, retomada sem create/PUT, continuidade do lote, proteção de mídia e mensagens da interface. O sucesso desses testes não representa uma campanha real concluída no Minute.
