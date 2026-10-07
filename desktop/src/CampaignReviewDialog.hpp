#pragma once

#include <QDialog>
#include <QDialogButtonBox>
#include <QFrame>
#include <QHeaderView>
#include <QJsonArray>
#include <QJsonObject>
#include <QLabel>
#include <QPushButton>
#include <QPlainTextEdit>
#include <QTableWidget>
#include <QTabWidget>
#include <QVBoxLayout>
#include <QSet>
#include <cmath>
#include <limits>

class CampaignReviewDialog final : public QDialog {
public:
  static double requestedSeconds(const QJsonObject& result, const QJsonObject& request) {
    if (request.value("run_until_exhausted").toBool(result.value("run_until_exhausted").toBool())) return 0;
    const auto value = request.contains("target_hours") ? request.value("target_hours") : result.value("target_hours");
    if (value.isUndefined()) return 0;
    if (!value.isDouble() || !std::isfinite(value.toDouble()) || value.toDouble() < 0)
      return std::numeric_limits<double>::quiet_NaN();
    return value.toDouble() * 3600;
  }
  static bool validCapacity(const QJsonObject& result, const QJsonObject& request) {
    const auto capacity = result.value("capacity").toObject();
    const auto number = [](const QJsonValue& value) {
      return value.isDouble() && std::isfinite(value.toDouble()) && value.toDouble() >= 0 && value.toDouble() <= 9007199254740991.;
    };
    const auto integer = [&number](const QJsonValue& value) { return number(value) && value.toDouble() == std::floor(value.toDouble()); };
    const double target = requestedSeconds(result, request);
    if (!std::isfinite(target) || capacity.value("schema") != QJsonValue(1) || capacity.value("known") != QJsonValue(true)
        || capacity.value("basis") != "estimated_admissible_delivery_duration_before_preparation"
        || !capacity.value("can_reach_goal").isBool() || !capacity.value("accounts").isArray()) return false;
    for (const auto* key : {"target_seconds_per_account", "available_seconds_min", "available_seconds_max", "total_available_seconds"})
      if (!number(capacity.value(QLatin1String(key)))) return false;
    for (const auto* key : {"shortfall_account_count", "estimated_sends", "candidate_clips"})
      if (!integer(capacity.value(QLatin1String(key)))) return false;
    if (std::abs(capacity.value("target_seconds_per_account").toDouble() - target) > 1e-6) return false;
    const auto accounts = capacity.value("accounts").toArray();
    if (accounts.isEmpty() || accounts.size() != result.value("accounts").toObject().value("validated").toInt(-1)) return false;
    QSet<QString> emails;
    double sum = 0, minimum = std::numeric_limits<double>::infinity(), maximum = 0;
    int shortfall = 0, sends = 0;
    for (const auto& value : accounts) {
      const auto row = value.toObject();
      const QString email = row.value("email").toString();
      if (!value.isObject() || email.trimmed().isEmpty() || emails.contains(email)) return false;
      emails.insert(email);
      for (const auto* key : {"available_seconds", "unique_footage_seconds", "unique_footage_upper_bound_seconds", "estimated_executable_seconds", "shortfall_seconds"})
        if (!number(row.value(QLatin1String(key)))) return false;
      for (const auto* key : {"eligible_clips", "estimated_sends", "recorded_clips", "pending_clips"})
        if (!integer(row.value(QLatin1String(key)))) return false;
      const double available = row.value("available_seconds").toDouble();
      if (std::abs(available - row.value("estimated_executable_seconds").toDouble()) > 1e-6
          || row.value("unique_footage_seconds").toDouble() > row.value("unique_footage_upper_bound_seconds").toDouble() + 1e-6
          || row.value("estimated_sends").toDouble() > row.value("eligible_clips").toDouble()
          || std::abs(row.value("shortfall_seconds").toDouble() - qMax(0., target - available)) > 1e-6) return false;
      sum += available; minimum = qMin(minimum, available); maximum = qMax(maximum, available);
      shortfall += target - available > 1e-6;
      sends += row.value("estimated_sends").toInt();
    }
    return std::abs(sum - capacity.value("total_available_seconds").toDouble()) <= 1e-3
        && std::abs(minimum - capacity.value("available_seconds_min").toDouble()) <= 1e-6
        && std::abs(maximum - capacity.value("available_seconds_max").toDouble()) <= 1e-6
        && shortfall == capacity.value("shortfall_account_count").toInt()
        && sends == capacity.value("estimated_sends").toInt()
        && capacity.value("can_reach_goal").toBool() == (shortfall == 0);
  }
  static bool capacityAllowsStart(const QJsonObject& result, const QJsonObject& request) {
    const double target = requestedSeconds(result, request);
    return std::isfinite(target) && (target == 0 || (validCapacity(result, request)
        && result.value("capacity").toObject().value("can_reach_goal").toBool()));
  }
  CampaignReviewDialog(const QJsonObject& result, const QStringList& accounts, QWidget* parent,
                       const QJsonObject& request = {})
      : QDialog(parent) {
    setWindowTitle(QStringLiteral("Revisar campanha"));
    resize(780, 620);
    setMinimumSize(580, 480);
    auto* layout = new QVBoxLayout(this);
    layout->setContentsMargins(28, 24, 28, 24);
    layout->setSpacing(16);
    auto* kicker = new QLabel(QStringLiteral("NOVA CAMPANHA / REVISÃO"));
    kicker->setObjectName(QStringLiteral("kicker"));
    layout->addWidget(kicker);
    const bool capacityValid = validCapacity(result, request);
    const bool capacityAllowed = capacityAllowsStart(result, request);
    const bool canStart = result.value("ok").toBool(true) && result.value("blockers").toArray().isEmpty() && capacityAllowed;
    auto* title = new QLabel(canStart ? QStringLiteral("Revisar antes de iniciar") : QStringLiteral("A campanha precisa de ajustes"));
    title->setObjectName(QStringLiteral("reviewTitle"));
    title->setWordWrap(true);
    layout->addWidget(title);
    auto* subtitle = new QLabel(QStringLiteral("Confira as contas e os limites antes de autorizar os envios."));
    subtitle->setObjectName(QStringLiteral("quiet"));
    subtitle->setWordWrap(true);
    layout->addWidget(subtitle);
    auto* metrics = new QHBoxLayout;
    const QList<QPair<QString, QString>> values{
      {QString::number(result.value("accounts").toObject().value("validated").toInt()), QStringLiteral("Contas validadas")},
      {QString::number(result.value("tasks").toObject().value("compatible").toInt()), QStringLiteral("Categorias compatíveis")},
      {QString::number(result.value("account_workers").toInt()), QStringLiteral("Envios simultâneos")}};
    for (const auto& value : values) {
      auto* frame = new QFrame;
      frame->setObjectName(QStringLiteral("reviewMetric"));
      auto* metric = new QVBoxLayout(frame);
      metric->setContentsMargins(16, 12, 16, 12);
      auto* number = new QLabel(value.first);
      number->setObjectName(QStringLiteral("reviewValue"));
      auto* caption = new QLabel(value.second);
      caption->setObjectName(QStringLiteral("quiet"));
      caption->setWordWrap(true);
      metric->addWidget(number);
      metric->addWidget(caption);
      metrics->addWidget(frame, 1);
    }
    layout->addLayout(metrics);
    auto* estimate = new QLabel(QStringLiteral("%1 clipes candidatos · %2 envios estimados. A quantidade final depende da preparação, dos resultados e dos limites da campanha.")
        .arg(result.value("clips").toInt()).arg(result.value("estimated_sends").toInt()));
    estimate->setWordWrap(true);
    estimate->setObjectName(QStringLiteral("quiet"));
    layout->addWidget(estimate);
    const auto capacity = result.value("capacity").toObject();
    if (capacityValid) {
      const bool untilExhausted = request.value("run_until_exhausted").toBool(result.value("run_until_exhausted").toBool());
      const QString availableText = untilExhausted
          ? QStringLiteral("Conteúdo novo estimado: até %1–%2 h por conta · %3 h disponíveis no total")
              .arg(capacity.value("available_seconds_min").toDouble() / 3600., 0, 'f', 2)
              .arg(capacity.value("available_seconds_max").toDouble() / 3600., 0, 'f', 2)
              .arg(capacity.value("total_available_seconds").toDouble() / 3600., 0, 'f', 2)
          : QStringLiteral("Meta: %1 h por conta · conteúdo novo estimado: até %2–%3 h por conta\n%4 conta(s) abaixo da meta · %5 h disponíveis no total")
          .arg(capacity.value("target_seconds_per_account").toDouble() / 3600., 0, 'f', 2)
          .arg(capacity.value("available_seconds_min").toDouble() / 3600., 0, 'f', 2)
          .arg(capacity.value("available_seconds_max").toDouble() / 3600., 0, 'f', 2)
          .arg(capacity.value("shortfall_account_count").toInt())
          .arg(capacity.value("total_available_seconds").toDouble() / 3600., 0, 'f', 2);
      auto* available = new QLabel(availableText);
      available->setObjectName(QStringLiteral("campaignCapacitySummary"));
      available->setWordWrap(true);
      layout->addWidget(available);
      auto* basis = new QLabel(QStringLiteral("Estimativa de trechos novos sem repetição material. Preparação, recebimento e avaliação ainda serão verificados."));
      basis->setObjectName(QStringLiteral("quiet")); basis->setWordWrap(true); layout->addWidget(basis);
      if (capacity.value("requires_measured_validation").toBool()) {
        auto* pending = new QLabel(QStringLiteral("%1 recorte(s) estimados pelas anotações. A fonte será adquirida e seu vídeo e sensores serão medidos antes do envio; as horas ainda dependem dessa validação.")
            .arg(capacity.value("pending_acquisition_clips").toInt()));
        pending->setObjectName(QStringLiteral("campaignAcquisitionEstimate"));
        pending->setWordWrap(true); layout->addWidget(pending);
      }
    } else if (requestedSeconds(result, request) > 0 || !std::isfinite(requestedSeconds(result, request))) {
      auto* unknown = new QLabel(QStringLiteral("A capacidade de conteúdo não foi confirmada para esta meta. Volte e verifique a campanha novamente."));
      unknown->setObjectName(QStringLiteral("campaignCapacitySummary")); unknown->setWordWrap(true); layout->addWidget(unknown);
    }
    if (!request.isEmpty()) {
      QString details = request.value("run_until_exhausted").toBool(result.value("run_until_exhausted").toBool())
          ? QStringLiteral("Até acabar o conteúdo elegível ou você parar · Vídeos: %1–%2 min")
              .arg(request.value("min_dur_s").toInt() / 60)
              .arg(request.value("max_dur_s").toInt() / 60)
          : QStringLiteral("Meta: %1 h por conta · Vídeos: %2–%3 min")
          .arg(request.value("target_hours").toDouble(), 0, 'f', 1)
          .arg(request.value("min_dur_s").toInt() / 60)
          .arg(request.value("max_dur_s").toInt() / 60);
      const auto hours = request.value("active_hours").toArray();
      if (hours.size() == 2)
        details += QStringLiteral(" · Janela: %1h–%2h").arg(hours[0].toInt()).arg(hours[1].toInt());
      auto* parameters = new QLabel(details);
      parameters->setWordWrap(true);
      parameters->setObjectName(QStringLiteral("quiet"));
      layout->addWidget(parameters);
    }
    auto* table = new QTableWidget(accounts.size(), 1);
    table->setHorizontalHeaderLabels({QStringLiteral("Contas incluídas")});
    table->horizontalHeader()->setSectionResizeMode(QHeaderView::Stretch);
    table->horizontalHeader()->setDefaultAlignment(Qt::AlignLeft | Qt::AlignVCenter);
    table->verticalHeader()->hide();
    table->verticalHeader()->setDefaultSectionSize(40);
    table->setShowGrid(false);
    table->setEditTriggers(QAbstractItemView::NoEditTriggers);
    table->setSelectionBehavior(QAbstractItemView::SelectRows);
    for (int row = 0; row < accounts.size(); ++row) {
      auto* item = new QTableWidgetItem(accounts[row]);
      item->setToolTip(accounts[row]);
      table->setItem(row, 0, item);
    }
    auto* tabs = new QTabWidget;
    tabs->addTab(table, QStringLiteral("Contas"));
    if (capacityValid) {
      const auto rows = capacity.value("accounts").toArray();
      auto* capacityTable = new QTableWidget(rows.size(), 4);
      capacityTable->setObjectName(QStringLiteral("campaignCapacityTable"));
      capacityTable->setHorizontalHeaderLabels({QStringLiteral("Conta"), QStringLiteral("Até h disponíveis"), QStringLiteral("Déficit h"), QStringLiteral("Envios estimados")});
      capacityTable->setColumnHidden(2, request.value("run_until_exhausted").toBool(result.value("run_until_exhausted").toBool()));
      capacityTable->horizontalHeader()->setSectionResizeMode(0, QHeaderView::Stretch);
      for (int column = 1; column < 4; ++column) capacityTable->horizontalHeader()->setSectionResizeMode(column, QHeaderView::ResizeToContents);
      capacityTable->verticalHeader()->hide(); capacityTable->setEditTriggers(QAbstractItemView::NoEditTriggers);
      capacityTable->setSelectionBehavior(QAbstractItemView::SelectRows);
      for (int row = 0; row < rows.size(); ++row) {
        const auto item = rows[row].toObject();
        const QStringList values{item.value("email").toString(), QString::number(item.value("available_seconds").toDouble() / 3600., 'f', 2),
            QString::number(item.value("shortfall_seconds").toDouble() / 3600., 'f', 2), QString::number(item.value("estimated_sends").toInt())};
        for (int column = 0; column < values.size(); ++column) capacityTable->setItem(row, column, new QTableWidgetItem(values[column]));
      }
      tabs->addTab(capacityTable, QStringLiteral("Capacidade por conta"));
    }
    const auto clips = result.value("clip_plan").toArray();
    if (!clips.isEmpty()) {
      auto* clipPage = new QWidget;
      auto* clipLayout = new QVBoxLayout(clipPage);
      clipLayout->setContentsMargins(0, 0, 0, 0);
      auto* clipTable = new QTableWidget(clips.size(), 6);
      clipTable->setObjectName(QStringLiteral("campaignClipPlanTable"));
      clipTable->setHorizontalHeaderLabels({QStringLiteral("Clipe"), QStringLiteral("Categoria"),
          QStringLiteral("Duração"), QStringLiteral("Elegíveis"), QStringLiteral("Excluídas"), QStringLiteral("Verificação")});
      clipTable->horizontalHeader()->setSectionResizeMode(QHeaderView::Stretch);
      clipTable->verticalHeader()->hide();
      clipTable->setSelectionBehavior(QAbstractItemView::SelectRows);
      clipTable->setSelectionMode(QAbstractItemView::SingleSelection);
      clipTable->setEditTriggers(QAbstractItemView::NoEditTriggers);
      clipTable->setShowGrid(false);
      auto* detail = new QPlainTextEdit;
      detail->setReadOnly(true);
      detail->setMaximumHeight(90);
      detail->setPlaceholderText(QStringLiteral("Selecione um clipe para conferir a elegibilidade de cada conta. O registro local não é uma nova consulta ao serviço."));
      for (int row = 0; row < clips.size(); ++row) {
        const auto clip = clips[row].toObject();
        const QStringList values{clip.value("clip_uid").toString(), clip.value("task").toString(),
            QStringLiteral("%1 min").arg(clip.value("duration_s").toDouble() / 60, 0, 'f', 1),
            QString::number(clip.value("eligible_accounts").toArray().size()),
            QString::number(clip.value("excluded_accounts").toArray().size()),
            clip.value("requires_measured_validation").toBool() ? QStringLiteral("Aquisição e medição pendentes")
                : clip.value("readiness").toString() == "measured" ? QStringLiteral("Vídeo e sensores medidos")
                : QStringLiteral("Preparação pendente")};
        for (int column = 0; column < values.size(); ++column) {
          auto* item = new QTableWidgetItem(values[column]);
          item->setToolTip(values[column]);
          clipTable->setItem(row, column, item);
        }
      }
      connect(clipTable, &QTableWidget::currentCellChanged, detail, [clips, detail](int row, int) {
        if (row < 0 || row >= clips.size()) { detail->clear(); return; }
        const auto clip = clips[row].toObject();
        QStringList lines;
        if (clip.value("requires_measured_validation").toBool())
          lines << QStringLiteral("Estimativa das anotações: a fonte será adquirida e medida antes do envio.");
        for (const auto account : clip.value("eligible_accounts").toArray())
          lines << account.toString() + QStringLiteral(" — elegível; envio sujeito à preparação e aos limites da campanha");
        for (const auto account : clip.value("excluded_accounts").toArray())
          lines << account.toString() + (clip.value("pending_accounts").toArray().contains(account)
              ? QStringLiteral(" — clipe reservado: envio anterior pendente; recebimento não confirmado nesta campanha")
              : QStringLiteral(" — excluída: envio anterior registrado na lista local"));
        detail->setPlainText(lines.join(QLatin1Char('\n')));
      });
      clipLayout->addWidget(clipTable, 1);
      clipLayout->addWidget(detail);
      tabs->addTab(clipPage, QStringLiteral("Clipes candidatos (%1)").arg(clips.size()));
    }
    layout->addWidget(tabs, 1);
    QStringList warnings;
    for (const auto blocker : result.value("blockers").toArray()) warnings << blocker.toString();
    if (!capacityAllowed && warnings.isEmpty()) warnings << QStringLiteral("O conteúdo novo não é suficiente ou não foi confirmado para a meta. Ajuste a seleção, a duração ou a meta antes de iniciar.");
    for (const auto warning : result.value("warnings").toArray()) warnings << warning.toString();
    if (!warnings.isEmpty()) {
      auto* note = new QLabel(warnings.join(QStringLiteral("\n")));
      note->setObjectName(QStringLiteral("reviewWarning"));
      note->setWordWrap(true);
      layout->addWidget(note);
    }
    auto* buttons = new QDialogButtonBox;
    auto* back = buttons->addButton(QStringLiteral("Voltar e ajustar"), QDialogButtonBox::RejectRole);
    auto* confirm = buttons->addButton(QStringLiteral("Confirmar e iniciar"), QDialogButtonBox::AcceptRole);
    confirm->setObjectName(QStringLiteral("campaignReviewConfirm"));
    confirm->setEnabled(canStart);
    confirm->setProperty("role", QStringLiteral("primary"));
    confirm->setAutoDefault(false);
    back->setDefault(true);
    connect(buttons, &QDialogButtonBox::accepted, this, &QDialog::accept);
    connect(buttons, &QDialogButtonBox::rejected, this, &QDialog::reject);
    layout->addWidget(buttons);
  }
};
