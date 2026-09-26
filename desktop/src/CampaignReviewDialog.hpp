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

class CampaignReviewDialog final : public QDialog {
public:
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
    auto* title = new QLabel(QStringLiteral("Tudo pronto para começar?"));
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
    if (!request.isEmpty()) {
      QString details = QStringLiteral("Meta: %1 h por conta · Vídeos: %2–%3 min")
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
    const auto clips = result.value("clip_plan").toArray();
    if (!clips.isEmpty()) {
      auto* clipPage = new QWidget;
      auto* clipLayout = new QVBoxLayout(clipPage);
      clipLayout->setContentsMargins(0, 0, 0, 0);
      auto* clipTable = new QTableWidget(clips.size(), 5);
      clipTable->setHorizontalHeaderLabels({QStringLiteral("Clipe"), QStringLiteral("Categoria"),
          QStringLiteral("Duração"), QStringLiteral("Elegíveis"), QStringLiteral("Já registrados")});
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
            QString::number(clip.value("excluded_accounts").toArray().size())};
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
        for (const auto account : clip.value("eligible_accounts").toArray())
          lines << account.toString() + QStringLiteral(" — elegível; envio sujeito à preparação e aos limites da campanha");
        for (const auto account : clip.value("excluded_accounts").toArray())
          lines << account.toString() + QStringLiteral(" — excluída: envio anterior registrado na lista local");
        detail->setPlainText(lines.join(QLatin1Char('\n')));
      });
      clipLayout->addWidget(clipTable, 1);
      clipLayout->addWidget(detail);
      tabs->addTab(clipPage, QStringLiteral("Clipes candidatos (%1)").arg(clips.size()));
    }
    layout->addWidget(tabs, 1);
    QStringList warnings;
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
    confirm->setProperty("role", QStringLiteral("primary"));
    confirm->setAutoDefault(false);
    back->setDefault(true);
    connect(buttons, &QDialogButtonBox::accepted, this, &QDialog::accept);
    connect(buttons, &QDialogButtonBox::rejected, this, &QDialog::reject);
    layout->addWidget(buttons);
  }
};
