## QMoney 2.0.48

O botão **Reset completo**, em Nova campanha, apaga o histórico local de campanhas e envios, recibos, pendências de recuperação, registros de vídeos usados e reservas de início e de gravação. Limpa também os resultados antigos da interface e as cópias operacionais legadas que poderiam restaurar esses registros.

- Contas, credenciais, perfis dos aparelhos, saldos, preferências e os arquivos da Biblioteca são preservados.
- A limpeza exige que campanhas, recuperações e consultas tenham terminado. Operações paralelas ficam protegidas por uma barreira local, sem serializar os envios entre contas.
- Se a limpeza de arquivos for interrompida, novos envios ficam bloqueados até concluir o reset. A interface só informa sucesso depois da confirmação do serviço.
- O reset não altera envios já registrados no Minute e não inicia, retoma, avalia ou finaliza envios.

Inclui a Biblioteca Nymeria e o SDK Aria da versão 2.0.47.
