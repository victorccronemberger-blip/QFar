## QMoney 2.0.58

Corrige a verificação da campanha que expirava enquanto a prévia varria arquivos completos de sensores.

- A prévia estima candidatos pelo catálogo, sem adquirir mídia, varrer CSVs de IMU ou abrir fontes VRS. Os candidatos continuam identificados como estimativas que exigem medição no preparo; a meta e a faixa de duração são preservadas.
- Timeout, desconexão e HTTP 504 com cálculo ainda ativo retomam o mesmo identificador e corpo da verificação. O acompanhamento tem limite de quatro recuperações; erros definitivos continuam visíveis.
- Leituras de IMU em trabalhos de catálogo cedem tempo à API local, mantendo o hash real da fonte e a verificação de continuidade dos dois sensores.

Preserva a retirada de contas restritas sem repetir a validação, o plano revisado, a aquisição de um recorte por vez, a limpeza após confirmação e a proteção dos arquivos pendentes. A atualização não inicia envios ao Minute.
