#pragma once
#include <QJsonArray>
#include <QJsonObject>
#include <QString>

// Read-only presentation: absence of a running campaign is not proof of readiness.
struct OperationSummary {
  QString title, detail, action;
  int destination{1};
  int progress{0};
  static OperationSummary from(const QJsonArray& accounts, const QJsonObject& snapshot) {
    OperationSummary result;
    const auto state = snapshot.value("state").toString();
    const auto totals = snapshot.value("totals").toObject();
    const int total = totals.value("total_sends").toInt();
    const int done = totals.value("done_sends").toInt();
    result.progress = total > 0 ? qBound(0, int(100.0 * done / total), 100) : 0;
    if (state == "running" || state == "stopping") {
      result.title = state == "stopping" ? QStringLiteral("Encerrando com segurança") : QStringLiteral("Sua campanha está em execução");
      result.detail = snapshot.value("current").toString();
      if (result.detail.isEmpty()) result.detail = QStringLiteral("Acompanhe o progresso e os resultados por conta.");
      result.action = QStringLiteral("Acompanhar campanha");
      result.destination = 3;
    } else if (state == "error") {
      result.title = QStringLiteral("Uma execução precisa de atenção");
      result.detail = QStringLiteral("Confira o resultado da campanha antes de iniciar outra operação.");
      result.action = QStringLiteral("Revisar execução");
      result.destination = 3;
    } else if (accounts.isEmpty()) {
      result.title = QStringLiteral("Sua primeira operação começa aqui");
      result.detail = QStringLiteral("Conecte suas contas, configure o conteúdo e confira os requisitos antes de enviar.");
      result.action = QStringLiteral("Conectar primeira conta");
      result.destination = 5;
    } else {
      result.title = QStringLiteral("Planeje a próxima campanha");
      result.detail = QStringLiteral("Você tem %1 conta(s) cadastrada(s). Confira os requisitos antes de começar.").arg(accounts.size());
      result.action = QStringLiteral("Verificar requisitos");
    }
    return result;
  }
};
