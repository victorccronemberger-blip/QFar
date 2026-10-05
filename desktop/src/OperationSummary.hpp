#pragma once
#include <QJsonArray>
#include <QJsonObject>
#include <QString>
#include <QSet>
#include <cmath>

// Read-only presentation: absence of a running campaign is not proof of readiness.
struct OperationSummary {
  QString title, detail, action;
  int destination{1};
  int progress{0};
  static bool number(const QJsonValue& value) {
    return value.isDouble() && std::isfinite(value.toDouble()) && value.toDouble() >= 0
        && value.toDouble() <= 9007199254740991.;
  }
  static bool attention(const QString& state) {
    return state == "failed" || state == "excluded" || state == "pending" || state == "unconfirmed";
  }
  static bool attention(const QJsonObject& row) {
    return attention(row.value("state").toString()) || row.value("failed").toInt()>0 || row.value("needs_attention").toBool();
  }
  static bool valid(const QJsonObject& snapshot) {
    const QSet<QString> states{"idle", "running", "stopping", "done", "stopped", "error"};
    if (!states.contains(snapshot.value("state").toString())
        || !snapshot.value("operation").isObject() || !snapshot.value("totals").isObject()) return false;
    if (snapshot.contains("pause_requested") && !snapshot.value("pause_requested").isBool()) return false;
    if (snapshot.contains("events") && !snapshot.value("events").isArray()) return false;
    for (const auto value : snapshot.value("events").toArray()) {
      if (!value.isObject()) return false;
      const auto event = value.toObject();
      if (event.contains("ts") && (!number(event.value("ts")) || event.value("ts").toDouble()>253402300799.)) return false;
      for (const auto key : {"title", "detail", "level"})
        if (event.contains(key) && !event.value(key).isString()) return false;
    }
    const auto operation = snapshot.value("operation").toObject();
    if (!operation.value("accounts").isArray() || !operation.value("counts").isObject()) return false;
    if (snapshot.value("state")=="idle" && !operation.value("accounts").toArray().isEmpty()) return false;
    const QSet<QString> phases{"queued", "waiting", "preparing", "sending", "confirming", "confirmed",
        "failed", "skipped", "recovering", "excluded", "pending", "unconfirmed"};
    QSet<QString> emails;
    for (const auto value : operation.value("accounts").toArray()) {
      if (!value.isObject()) return false;
      const auto row = value.toObject();
      const auto email = row.value("email").toString();
      if (email.trimmed().isEmpty() || emails.contains(email) || !phases.contains(row.value("state").toString())) return false;
      emails.insert(email);
      if (row.contains("needs_attention") && !row.value("needs_attention").isBool()) return false;
      const auto phase = row.value("state").toString();
      const auto state = snapshot.value("state").toString();
      if ((state=="done" || state=="stopped" || state=="error")
          && (phase=="queued" || phase=="waiting" || phase=="preparing" || phase=="sending" || phase=="confirming" || phase=="recovering")) return false;
      for (const auto key : {"detail", "clip_uid", "session_id"})
        if (row.contains(key) && !row.value(key).isNull() && !row.value(key).isString()) return false;
      const auto progress = row.value("progress");
      if (!progress.isUndefined() && !progress.isNull() && (!number(progress) || progress.toDouble() > 100)) return false;
      for (const auto key : {"confirmed", "failed", "skipped"})
        if (row.contains(key) && (!number(row.value(key)) || row.value(key).toDouble()>2147483647.
            || std::floor(row.value(key).toDouble()) != row.value(key).toDouble())) return false;
    }
    const auto counts = operation.value("counts").toObject();
    for (const auto value : counts)
      if (!number(value) || std::floor(value.toDouble()) != value.toDouble()) return false;
    const auto totals = snapshot.value("totals").toObject();
    if (totals.contains("progress_unit") && totals.value("progress_unit")!="sends" && totals.value("progress_unit")!="seconds") return false;
    for (const auto key : {"progress_target", "progress_completed", "total_sends", "done_sends", "ok_sends", "failed_sends", "skipped_sends"})
      if (totals.contains(key) && !number(totals.value(key))) return false;
    for (const auto key : {"total_sends", "done_sends", "ok_sends", "failed_sends", "skipped_sends"})
      if (totals.contains(key) && std::floor(totals.value(key).toDouble())!=totals.value(key).toDouble()) return false;
    return true;
  }
  static OperationSummary from(const QJsonArray& accounts, const QJsonObject& snapshot) {
    OperationSummary result;
    const auto state = snapshot.value("state").toString();
    const auto totals = snapshot.value("totals").toObject();
    const double total = totals.value("progress_target").toDouble(totals.value("total_sends").toDouble());
    const double done = totals.value("progress_completed").toDouble(totals.value("ok_sends").toDouble());
    result.progress = std::isfinite(total) && std::isfinite(done) && total > 0 && done >= 0
        ? int(100.0 * qBound(0., done / total, 1.)) : 0;
    const bool paused = state == "running" && snapshot.value("pause_requested").toBool();
    const auto rows = snapshot.value("operation").toObject().value("accounts").toArray();
    bool pending = totals.value("failed_sends").toDouble() > 0;
    for (const auto row : rows) pending |= attention(row.toObject());
    if (state == "running" || state == "stopping") {
      result.title = state == "stopping" ? QStringLiteral("Encerrando com segurança")
          : paused ? QStringLiteral("Pausa solicitada") : QStringLiteral("Campanha em andamento");
      result.detail = snapshot.value("current").toString();
      if (result.detail.isEmpty()) result.detail = QStringLiteral("Acompanhe o progresso e os resultados por conta.");
      result.action = QStringLiteral("Acompanhar campanha");
      result.destination = 3;
      if (paused) result.detail = QStringLiteral("Novos envios aguardam Retomar. Requisições em andamento podem terminar.");
    } else if (state == "error") {
      result.title = QStringLiteral("Uma execução precisa de atenção");
      result.detail = QStringLiteral("Confira o resultado da campanha antes de iniciar outra operação.");
      result.action = QStringLiteral("Revisar execução");
      result.destination = 3;
    } else if (state == "done" || state == "stopped") {
      result.title = state == "stopped" ? QStringLiteral("Campanha interrompida")
          : pending ? QStringLiteral("Encerrada com pendências") : QStringLiteral("Campanha encerrada");
      result.detail = pending ? QStringLiteral("Há resultados que precisam de revisão. Confira os detalhes por conta.")
          : QStringLiteral("Confira os recibos e os resultados desta execução no histórico.");
      result.action = QStringLiteral("Ver resultados no histórico");
      result.destination = 7;
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
