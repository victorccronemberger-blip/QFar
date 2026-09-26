#include <QUuid>
#include <algorithm>
#include <cmath>
#include <utility>
#include "MainWindow.hpp"
#include "ComboBox.hpp"
#include "OperationSummary.hpp"
#include "OperationWidgets.hpp"
#include "TableEmptyState.hpp"
#include "CampaignReviewDialog.hpp"
#include <QMenu>
#include <QPointer>
#include <QResizeEvent>
#include <QShortcut>
#include <QToolButton>

#include <QApplication>
#include <QCheckBox>
#include <QClipboard>
#include <QCloseEvent>
#include <QColor>
#include <QComboBox>
#include <QCryptographicHash>
#include <QDateTime>
#include <QDebug>
#include <QDesktopServices>
#include <QDir>
#include <QDirIterator>
#include <QDialog>
#include <QDialogButtonBox>
#include <QDoubleSpinBox>
#include <QFileInfo>
#include <QFile>
#include <QFileDialog>
#include <QFormLayout>
#include <QFrame>
#include <QFontMetrics>
#include <QGroupBox>
#include <QHash>
#include <QHeaderView>
#include <QHBoxLayout>
#include <QInputDialog>
#include <QJsonDocument>
#include <QJsonArray>
#include <QJsonObject>
#include <QJsonValue>
#include <QLabel>
#include <QLineEdit>
#include <QListWidget>
#include <QLocale>
#include <QMessageBox>
#include <QPlainTextEdit>
#include <QPixmap>
#include <QProgressBar>
#include <QRandomGenerator>
#include <QProcess>
#include <QProcessEnvironment>
#include <QPushButton>
#include <QRegularExpression>
#include <QScrollArea>
#include <QScreen>
#include <QSaveFile>
#include <QSet>
#include <QSettings>
#include <QSizePolicy>
#include <QSpinBox>
#include <QStackedWidget>
#include <QStandardPaths>
#include <QSignalBlocker>
#include <QTableWidget>
#include <QTabWidget>
#include <QTimer>
#include <QUrl>
#include <QUrlQuery>
#include <QVBoxLayout>

#include <oclero/qlementine/style/QlementineStyle.hpp>

#ifdef Q_OS_WIN
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>
#include <tlhelp32.h>
#endif

namespace {
bool confirmWithdrawal(QWidget* parent, int count, QJsonObject& request) {
  QDialog dialog(parent);
  dialog.setWindowTitle(QStringLiteral("Confirmar saque"));
  dialog.resize(620, 510);
  auto* layout = new QVBoxLayout(&dialog);
  layout->setContentsMargins(28, 24, 28, 24);
  layout->setSpacing(18);
  auto* kicker = new QLabel(QStringLiteral("CARTEIRA / CONFIRMAÇÃO"), &dialog);
  kicker->setObjectName(QStringLiteral("kicker"));
  layout->addWidget(kicker);
  auto* title = new QLabel(QStringLiteral("Revise seu saque"), &dialog);
  title->setObjectName(QStringLiteral("reviewTitle"));
  layout->addWidget(title);
  auto* help = new QLabel(QStringLiteral("Solicitar saque para %1 conta(s) elegível(is)?").arg(count));
  help->setWordWrap(true);
  layout->addWidget(help);
  auto* form = new QFormLayout;
  auto* method = new ComboBox(&dialog);
  method->addItem(QStringLiteral("Método já configurado"), QStringLiteral("saved"));
  method->addItem(QStringLiteral("Wise"), QStringLiteral("wise"));
  auto* name = new QLineEdit;
  name->setPlaceholderText(QStringLiteral("Nome completo do titular"));
  name->setMaxLength(200);
  auto* email = new QLineEdit;
  email->setPlaceholderText(QStringLiteral("E-mail cadastrado na Wise"));
  email->setMaxLength(254);
  form->addRow(QStringLiteral("Método"), method);
  form->addRow(QStringLiteral("Nome legal na Wise"), name);
  form->addRow(QStringLiteral("E-mail da Wise"), email);
  layout->addLayout(form);
  auto* explanation = new QLabel;
  explanation->setWordWrap(true);
  layout->addWidget(explanation);
  auto* buttons = new QDialogButtonBox(QDialogButtonBox::Ok | QDialogButtonBox::Cancel);
  buttons->button(QDialogButtonBox::Ok)->setText(QStringLiteral("Confirmar e solicitar"));
  buttons->button(QDialogButtonBox::Ok)->setProperty("role", QStringLiteral("primary"));
  buttons->button(QDialogButtonBox::Ok)->setAutoDefault(false);
  buttons->button(QDialogButtonBox::Cancel)->setText(QStringLiteral("Voltar"));
  buttons->button(QDialogButtonBox::Cancel)->setDefault(true);
  auto update = [=] {
    const bool wise = method->currentData().toString() == QStringLiteral("wise");
    name->setEnabled(wise);
    email->setEnabled(wise);
    explanation->setText(wise
        ? QStringLiteral("Uma conta por vez: vincular Wise → solicitar saque → desvincular Wise → voltar para Dots. "
                         "A limpeza é tentada mesmo em caso de erro. Novos saques ficam bloqueados até confirmá-la.")
        : QStringLiteral("Usa o método salvo na Crowtado. No Dots, conclua pelo link da própria conta."));
    buttons->button(QDialogButtonBox::Ok)->setEnabled(!wise ||
        (name->text().trimmed().size() >= 2 && QRegularExpression(
            QStringLiteral("^[^\\s@]+@[^\\s@]+\\.[^\\s@]+$"))
                .match(email->text().trimmed()).hasMatch()));
  };
  QObject::connect(method, QOverload<int>::of(&QComboBox::currentIndexChanged), &dialog, [=](int) { update(); });
  QObject::connect(name, &QLineEdit::textChanged, &dialog, [=](const QString&) { update(); });
  QObject::connect(email, &QLineEdit::textChanged, &dialog, [=](const QString&) { update(); });
  QObject::connect(buttons, &QDialogButtonBox::accepted, &dialog, &QDialog::accept);
  QObject::connect(buttons, &QDialogButtonBox::rejected, &dialog, &QDialog::reject);
  layout->addStretch();
  layout->addWidget(buttons);
  update();
  if (dialog.exec() != QDialog::Accepted) return false;
  if (method->currentData().toString() == QStringLiteral("wise")) {
    request.insert(QStringLiteral("method"), QStringLiteral("wise"));
    request.insert(QStringLiteral("wise_confirmed"), true);
    request.insert(QStringLiteral("legal_name"), name->text().trimmed());
    request.insert(QStringLiteral("destination_email"), email->text().trimmed());
  }
  return true;
}

QString provisionEmbeddedService() {
#if defined(Q_OS_WIN) && QMONEY_HAS_EMBEDDED_SERVICE
  HMODULE module = GetModuleHandleW(nullptr);
  HRSRC resource = FindResourceW(module, MAKEINTRESOURCEW(201), RT_RCDATA);
  if (!resource) return {};
  HGLOBAL loaded = LoadResource(module, resource);
  const DWORD size = SizeofResource(module, resource);
  const void* bytes = loaded ? LockResource(loaded) : nullptr;
  if (!bytes || size == 0) return {};

  const QString runtime = QStandardPaths::writableLocation(
                              QStandardPaths::GenericDataLocation)
                          + QStringLiteral("/QMoney/runtime");
  QDir().mkpath(runtime);
  const QString target = runtime + QStringLiteral("/QMoneyService-%1.exe")
                                       .arg(QCoreApplication::applicationVersion());
  const QByteArray expected = QCryptographicHash::hash(
      QByteArrayView(static_cast<const char*>(bytes), size),
      QCryptographicHash::Sha256);

  QFile existing(target);
  if (existing.size() == static_cast<qint64>(size)
      && existing.open(QIODevice::ReadOnly)) {
    QCryptographicHash actual(QCryptographicHash::Sha256);
    if (actual.addData(&existing) && actual.result() == expected) return target;
  }

  QSaveFile output(target);
  if (!output.open(QIODevice::WriteOnly)
      || output.write(static_cast<const char*>(bytes), size) != size
      || !output.commit()) {
    qWarning() << "QMoney: não foi possível disponibilizar o motor incorporado";
    return {};
  }
  qInfo() << "QMoney: motor incorporado preparado para"
          << QCoreApplication::applicationVersion();
  return target;
#else
  return {};
#endif
}

void terminatePackagedServiceTree() {
#ifdef Q_OS_WIN
  // Uma distribuição PyInstaller --onefile mantém um processo filho vivo. As
  // primeiras versões do QMoney encerravam apenas o bootloader durante a
  // atualização; o filho antigo continuava atendendo a porta 8876 e a nova
  // interface acabava conectada ao motor errado. Como a interface é de
  // instância única, não existe outro serviço legítimo que deva sobreviver.
  HANDLE snapshot = CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0);
  if (snapshot == INVALID_HANDLE_VALUE) return;
  PROCESSENTRY32W entry{};
  entry.dwSize = sizeof(entry);
  int terminated = 0;
  if (Process32FirstW(snapshot, &entry)) {
    do {
      const QString name = QString::fromWCharArray(entry.szExeFile);
      if (!name.startsWith(QStringLiteral("QMoneyService"), Qt::CaseInsensitive)
          || !name.endsWith(QStringLiteral(".exe"), Qt::CaseInsensitive))
        continue;
      HANDLE process = OpenProcess(PROCESS_TERMINATE | SYNCHRONIZE, FALSE,
                                   entry.th32ProcessID);
      if (!process) continue;
      if (TerminateProcess(process, 0)) {
        WaitForSingleObject(process, 3000);
        ++terminated;
      }
      CloseHandle(process);
    } while (Process32NextW(snapshot, &entry));
  }
  CloseHandle(snapshot);
  if (terminated > 0)
    qInfo() << "QMoney: processos antigos do motor encerrados" << terminated;
#endif
}

QString jsonId(const QJsonValue& value) {
  if (value.isString()) return value.toString();
  if (value.isDouble()) return QString::number(value.toDouble(), 'f', 0);
  return value.toVariant().toString();
}

QString bytesText(qint64 bytes) {
  const double gib = static_cast<double>(bytes) / (1024.0 * 1024.0 * 1024.0);
  if (gib >= 1.0) return QLocale().toString(gib, 'f', gib >= 100.0 ? 0 : 1) + QStringLiteral(" GiB");
  const double mib = static_cast<double>(bytes) / (1024.0 * 1024.0);
  return QLocale().toString(mib, 'f', 1) + QStringLiteral(" MiB");
}

QTableWidgetItem* cell(const QString& text) {
  auto* item = new QTableWidgetItem(text);
  item->setFlags(item->flags() & ~Qt::ItemIsEditable);
  item->setToolTip(text);
  return item;
}

QLabel* quietLabel(const QString& text) {
  auto* label = new QLabel(text);
  label->setObjectName(QStringLiteral("quiet"));
  label->setWordWrap(true);
  return label;
}

void configureCombo(QComboBox* combo, int minimumWidth = 360) {
  combo->setMinimumWidth(minimumWidth);
  combo->setMinimumHeight(42);
  combo->setSizePolicy(QSizePolicy::Expanding, QSizePolicy::Fixed);
  combo->setMaxVisibleItems(8);
  combo->setSizeAdjustPolicy(QComboBox::AdjustToMinimumContentsLengthWithIcon);
  combo->setMinimumContentsLength(24);
  combo->view()->setTextElideMode(Qt::ElideRight);
  combo->view()->setHorizontalScrollBarPolicy(Qt::ScrollBarAlwaysOff);
  combo->view()->setVerticalScrollBarPolicy(Qt::ScrollBarAsNeeded);
  combo->view()->setVerticalScrollMode(QAbstractItemView::ScrollPerItem);
}

void configureTable(QTableWidget* table,
                    const QString& title = QStringLiteral("Os registros aparecerão aqui"),
                    const QString& detail = QStringLiteral("Sincronize os dados para consultar os resultados disponíveis.")) {
  table->setWordWrap(false);
  table->setTextElideMode(Qt::ElideRight);
  table->setHorizontalScrollMode(QAbstractItemView::ScrollPerPixel);
  table->setVerticalScrollMode(QAbstractItemView::ScrollPerPixel);
  table->setHorizontalScrollBarPolicy(Qt::ScrollBarAsNeeded);
  table->setVerticalScrollBarPolicy(Qt::ScrollBarAsNeeded);
  table->setAlternatingRowColors(true);
  table->setShowGrid(false);
  table->verticalHeader()->setVisible(false);
  table->verticalHeader()->setSectionResizeMode(QHeaderView::Fixed);
  table->verticalHeader()->setDefaultSectionSize(48);
  table->verticalHeader()->setMinimumSectionSize(48);
  table->horizontalHeader()->setMinimumHeight(42);
  table->horizontalHeader()->setMinimumSectionSize(92);
  table->horizontalHeader()->setDefaultAlignment(Qt::AlignLeft | Qt::AlignVCenter);
  table->horizontalHeader()->setStretchLastSection(false);
  new TableEmptyState(table, title, detail);
}

void configureForm(QFormLayout* form) {
  form->setFieldGrowthPolicy(QFormLayout::AllNonFixedFieldsGrow);
  form->setRowWrapPolicy(QFormLayout::WrapLongRows);
  form->setLabelAlignment(Qt::AlignLeft | Qt::AlignVCenter);
  form->setFormAlignment(Qt::AlignTop);
  form->setHorizontalSpacing(18);
  form->setVerticalSpacing(12);
}

}  // namespace

MainWindow::MainWindow(oclero::qlementine::QlementineStyle* style, QWidget* parent, bool startServices)
    : QMainWindow(parent), _style(style), _api(this), _backend(this) {
  setWindowTitle(QStringLiteral("QMoney — Central de operação"));
  resize(1280, 820);
  setMinimumSize(980, 680);

  _backendProbe.setInterval(650);
  _campaignPoll.setInterval(1100);
  _previewPoll.setInterval(15000);
  _taskReload.setInterval(350);
  _taskReload.setSingleShot(true);
  _balancePoll.setInterval(1500);
  _cachePoll.setInterval(3000);
  connect(&_backendProbe, &QTimer::timeout, this, &MainWindow::probeBackend);
  connect(&_campaignPoll, &QTimer::timeout, this, &MainWindow::pollCampaign);
  connect(&_previewPoll, &QTimer::timeout, this, &MainWindow::pollCampaignPreviews);
  connect(&_taskReload, &QTimer::timeout, this, &MainWindow::loadTasks);
  connect(&_balancePoll, &QTimer::timeout, this, &MainWindow::loadBalances);
  connect(&_cachePoll, &QTimer::timeout, this, &MainWindow::loadAccelerator);

  buildShell();
  auto* commandShortcut = new QShortcut(QKeySequence(QStringLiteral("Ctrl+K")), this);
  connect(commandShortcut, &QShortcut::activated, this, &MainWindow::openCommandPalette);
  _operationPoll.setInterval(3000);
  connect(&_operationPoll, &QTimer::timeout, this, [this] {
    if (!_backendReady || _pages->currentIndex() != 0 || _operationPolling) return;
    _operationPolling = true;
    const int generation = _homeGeneration;
    _api.get(QStringLiteral("/api/campaigns/current"), [this, generation](bool ok, const QJsonDocument& doc, const QString&) {
      _operationPolling = false;
      if (_closing || generation != _homeGeneration) return;
      if (!ok) {
        _homeSync->setText(QStringLiteral("Atualização interrompida • tentando novamente"));
        return;
      }
      renderOperation(doc.object());
      const auto state = doc.object().value("state").toString();
      const auto totals = doc.object().value("totals").toObject();
      const int total = totals.value("total_sends").toInt();
      _homePulseProgress->setValue(total > 0 ? qBound(0, int(100. * totals.value("done_sends").toInt() / total), 100) : 0);
      _operationProgressRow->setVisible(state == "running" || state == "stopping");
      if (state == "running" || state == "stopping") {
        _homePulseTitle->setText(state == "running" ? QStringLiteral("Campanha em andamento") : QStringLiteral("Encerrando com segurança"));
        _homePulseBody->setText(doc.object().value("current").toString());
      } else if (state == "done" || state == "stopped" || state == "error") {
        _homePulseTitle->setText(state == "error" ? QStringLiteral("Execução com pendências") : QStringLiteral("Execução encerrada"));
        _homePulseBody->setText(QStringLiteral("Confira os resultados por conta no histórico."));
      }
      _homeSync->setText(QStringLiteral("Atualizado às %1").arg(QTime::currentTime().toString("HH:mm:ss")));
    });
  });
  if (startServices) _operationPoll.start();
  qInfo() << "QMoney: shell pronto";
  const bool dark = QSettings().value(QStringLiteral("darkTheme"), false).toBool();
  // Aplicado apos a primeira passagem do event loop. O Qlementine instala
  // filtros de evento durante a construcao dos widgets; adiar o stylesheet
  // evita uma repolish aninhada nessa mesma pilha.
  QTimer::singleShot(0, this, [this, dark] { applyStructuralStyle(dark); });
  _themeButton->setText(dark ? QStringLiteral("☀  Usar tema claro")
                             : QStringLiteral("◐  Usar tema escuro"));
  qInfo() << "QMoney: tema estrutural pronto";
  if (startServices) startBackend();
  qInfo() << "QMoney: backend solicitado";

  connect(&_updates, &UpdateManager::statusChanged, this, &MainWindow::setStatus);
  connect(&_updates, &UpdateManager::errorOccurred, this,
          [this](const QString& message, bool interactive) {
            _updateButton->setEnabled(true);
            _updateButton->setText(QStringLiteral("↻  Verificar atualização"));
            if (_repairInstall) {
              _repairInstall->setEnabled(true);
              _repairInstall->setText(QStringLiteral("Reparar instalação"));
            }
            if (interactive) QMessageBox::warning(
                this, _updates.isRepair() ? QStringLiteral("Reparo da instalação")
                                          : QStringLiteral("Atualizações"),
                message);
          });
  connect(&_updates, &UpdateManager::checkFinished, this,
          [this](bool available, bool interactive) {
            if (!_updates.isBusy()) _updateButton->setEnabled(true);
            if (_repairInstall && !_updates.isBusy()) {
              _repairInstall->setEnabled(true);
              _repairInstall->setText(QStringLiteral("Reparar instalação"));
            }
            if (!available) {
              _updateButton->setText(QStringLiteral("✓  QMoney %1").arg(QCoreApplication::applicationVersion()));
              if (interactive)
                QMessageBox::information(this, QStringLiteral("Atualizações"),
                                         QStringLiteral("Você já está usando a versão mais recente."));
            }
          });
  connect(&_updates, &UpdateManager::updateAvailable, this,
          [this](const QString& version, const QString& notes) {
            const bool repair = _updates.isRepair();
            _updateButton->setEnabled(true);
            _updateButton->setText(repair
                ? QStringLiteral("Reparando componentes…")
                : QStringLiteral("⬇  Instalar QMoney %1").arg(version));
            const QString safeNotes = notes.trimmed().isEmpty()
                                          ? QStringLiteral("Esta versão não possui notas adicionais.")
                                          : notes.trimmed();
            const auto answer = QMessageBox::question(
                this, repair ? QStringLiteral("Reparar QMoney")
                             : QStringLiteral("QMoney %1 disponível").arg(version),
                repair
                    ? QStringLiteral(
                          "O QMoney baixará novamente o pacote oficial assinado e "
                          "restaurará FFmpeg, FFprobe, navegador privado e o motor local.\n\n"
                          "Suas contas, credenciais e campanhas serão preservadas. Continuar?")
                    : QStringLiteral("Uma nova versão está pronta para baixar.\n\n%1\n\nInstalar agora?")
                          .arg(safeNotes));
            if (answer == QMessageBox::Yes) {
              _updateButton->setEnabled(false);
              if (_repairInstall) _repairInstall->setEnabled(false);
              _updateButton->setText(repair ? QStringLiteral("Baixando reparo…")
                                            : QStringLiteral("Baixando atualização…"));
              _updates.downloadAndInstall();
            } else if (repair) {
              _updateButton->setText(
                  QStringLiteral("✓  QMoney %1").arg(QCoreApplication::applicationVersion()));
              if (_repairInstall) {
                _repairInstall->setEnabled(true);
                _repairInstall->setText(QStringLiteral("Reparar instalação"));
              }
            }
          });
  connect(&_updates, &UpdateManager::progress, this,
          [this](qint64 received, qint64 total) {
            if (total > 0)
              _updateButton->setText(QStringLiteral("Baixando… %1%").arg(received * 100 / total));
            if (total > 0 && _repairInstall && _updates.isRepair())
              _repairInstall->setText(QStringLiteral("Baixando… %1%").arg(received * 100 / total));
          });
  connect(&_updates, &UpdateManager::installReady, this, &MainWindow::installUpdate);
  if (startServices) QTimer::singleShot(1800, this, [this] { checkForUpdates(false); });
}

MainWindow::~MainWindow() {
  _closing = true;
  stopBackend();
}

void MainWindow::closeEvent(QCloseEvent* event) {
  if (_campaignDraftSave.isActive()) {
    _campaignDraftSave.stop();
    saveCampaignDraft();
  }
  _closing = true;
  _campaignPoll.stop();
  _previewPoll.stop();
  _cachePoll.stop();
  _orgMigrationPoll.stop();
  _balancePoll.stop();
  _backendProbe.stop();
  stopBackend();
  QMainWindow::closeEvent(event);
}

void MainWindow::resizeEvent(QResizeEvent* event) {
  QMainWindow::resizeEvent(event);
  const bool compact = width() < 1280;
  if (_navigation) {
    const int rowHeight = height() < 800 ? 72 : 96;
    _navigation->setGridSize(QSize(136, rowHeight));
    for (int row = 0; row < _navigation->count(); ++row)
      _navigation->item(row)->setSizeHint(QSize(136, rowHeight - 8));
  }
  for (auto* header : _pageHeaders)
    header->setDirection(compact ? QBoxLayout::TopToBottom : QBoxLayout::LeftToRight);
  for (auto* identity : _headerIdentities) identity->setVisible(!compact);
  if (_operationColumns) _operationColumns->setDirection(compact ? QBoxLayout::TopToBottom : QBoxLayout::LeftToRight);
  if (_campaignSelectionColumns) _campaignSelectionColumns->setDirection(compact ? QBoxLayout::TopToBottom : QBoxLayout::LeftToRight);
  if (_libraryColumns) _libraryColumns->setDirection(compact ? QBoxLayout::TopToBottom : QBoxLayout::LeftToRight);
  if (_operationInspector) _operationInspector->setMaximumWidth(compact ? QWIDGETSIZE_MAX : 480);
}

void MainWindow::buildShell() {
  auto* root = new QWidget;
  auto* rootLayout = new QHBoxLayout(root);
  rootLayout->setContentsMargins(0, 0, 0, 0);
  rootLayout->setSpacing(0);

  auto* sidebar = new QWidget;
  sidebar->setObjectName(QStringLiteral("sidebar"));
  sidebar->setFixedWidth(136);
  auto* side = new QVBoxLayout(sidebar);
  side->setContentsMargins(0, 30, 0, 24);
  side->setSpacing(10);

  auto* brandRow = new QVBoxLayout;
  auto* mark = new QLabel;
  mark->setObjectName(QStringLiteral("brandMark"));
  mark->setAlignment(Qt::AlignCenter);
  mark->setFixedSize(44, 44);
  mark->setPixmap(QIcon(QStringLiteral(":/qmoney/icons/brand.svg")).pixmap(44,44));
  auto* brandCopy = new QVBoxLayout;
  brandCopy->setSpacing(0);
  auto* brand = new QLabel(QStringLiteral("QMoney"));
  brand->setObjectName(QStringLiteral("brand"));
  brand->setAlignment(Qt::AlignCenter);
  brandCopy->addWidget(brand);
  auto* brandRole = quietLabel(QStringLiteral("2.0"));
  brandRole->setObjectName(QStringLiteral("brandRole"));
  brandRole->setAlignment(Qt::AlignCenter);
  brandCopy->addWidget(brandRole);
  brandRow->addWidget(mark, 0, Qt::AlignHCenter);
  brandRow->addSpacing(6);
  brandRow->addLayout(brandCopy);
  side->addLayout(brandRow);
  side->addSpacing(20);

  auto* navigationLabel = new QLabel(QStringLiteral("NAVEGAÇÃO"));
  navigationLabel->setObjectName(QStringLiteral("navigationLabel"));
  navigationLabel->setParent(sidebar);
  navigationLabel->hide();

  _navigation = new QListWidget;
  _navigation->setObjectName(QStringLiteral("navigation"));
  _navigation->setFrameShape(QFrame::NoFrame);
  _navigation->setHorizontalScrollBarPolicy(Qt::ScrollBarAlwaysOff);
  const QStringList pages = {
      QStringLiteral("Operação"), QStringLiteral("Requisitos"),
      QStringLiteral("Integrações"), QStringLiteral("Nova campanha"),
      QStringLiteral("Acelerador"),
      QStringLiteral("Contas"), QStringLiteral("Carteira"),
      QStringLiteral("Histórico"), QStringLiteral("Banidas")};
  const QStringList icons = {QStringLiteral(":/qmoney/icons/play.svg"),
                             QStringLiteral(":/qmoney/icons/readiness.svg"),
                             QStringLiteral(":/qmoney/icons/integrations.svg"),
                             QStringLiteral(":/qmoney/icons/play.svg"),
                             QStringLiteral(":/qmoney/icons/bolt.svg"),
                             QStringLiteral(":/qmoney/icons/users.svg"),
                             QStringLiteral(":/qmoney/icons/wallet.svg"),
                             QStringLiteral(":/qmoney/icons/history.svg"),
                             QStringLiteral(":/qmoney/icons/users.svg")};
  _navigation->setViewMode(QListView::IconMode);
  _navigation->setFlow(QListView::TopToBottom);
  _navigation->setWrapping(false);
  _navigation->setMovement(QListView::Static);
  _navigation->setGridSize(QSize(136, 96));
  _navigation->setIconSize(QSize(28, 28));
  for (int i = 0; i < pages.size(); ++i) {
    auto* item = new QListWidgetItem(QIcon(icons[i]), pages[i]);
    item->setSizeHint(QSize(136, 88));
    item->setTextAlignment(Qt::AlignCenter);
    item->setToolTip(pages[i]);
    _navigation->addItem(item);
  }
  for (int hidden : {1, 2, 3, 4, 8}) _navigation->item(hidden)->setHidden(true);
  auto* settingsItem = new QListWidgetItem(QIcon(QStringLiteral(":/qmoney/icons/settings.svg")), QStringLiteral("Configurações"));
  settingsItem->setSizeHint(QSize(136,88));
  settingsItem->setTextAlignment(Qt::AlignCenter);
  _navigation->addItem(settingsItem);
  _navigation->setCurrentRow(0);
  connect(_navigation, &QListWidget::currentRowChanged, this, &MainWindow::navigate);
  side->addWidget(_navigation, 1);

  auto* connectionCard = new QFrame;
  connectionCard->setObjectName(QStringLiteral("connectionCard"));
  auto* connectionLayout = new QVBoxLayout(connectionCard);
  connectionLayout->setContentsMargins(9, 12, 9, 12);
  connectionLayout->setSpacing(5);
  connectionLayout->addWidget(quietLabel(QStringLiteral("MOTOR LOCAL")));
  _backendState = new QLabel(QStringLiteral("●  Iniciando…"));
  _backendState->setObjectName(QStringLiteral("backendState"));
  connectionLayout->addWidget(_backendState);
  connectionCard->setParent(sidebar);
  connectionCard->hide();

  _themeButton = new QPushButton;
  _themeButton->setObjectName(QStringLiteral("sidebarUtility"));
  connect(_themeButton, &QPushButton::clicked, this, [this] {
    setDarkTheme(!QSettings().value(QStringLiteral("darkTheme"), false).toBool());
  });
  _themeButton->setParent(sidebar);
  _themeButton->hide();

  _updateButton = new QPushButton(
      QStringLiteral("↻  Verificar atualização"));
  _updateButton->setObjectName(QStringLiteral("sidebarUtility"));
  connect(_updateButton, &QPushButton::clicked, this, [this] {
    if (_updateButton->text().startsWith(QStringLiteral("⬇"))) {
      _updateButton->setEnabled(false);
      _updates.downloadAndInstall();
    } else {
      checkForUpdates(true);
    }
  });
  _updateButton->setParent(sidebar);
  _updateButton->hide();
  auto* settings = new QToolButton;
  settings->setText(QStringLiteral("Configurações"));
  settings->setIcon(QIcon(QStringLiteral(":/qmoney/icons/settings.svg")));
  settings->setIconSize(QSize(26,26));
  settings->setToolButtonStyle(Qt::ToolButtonTextUnderIcon);
  settings->setFixedWidth(136);
  settings->setObjectName(QStringLiteral("railSettings"));
  settings->setMinimumHeight(80);
  auto* settingsMenu = new QMenu(settings);
  _settingsMenu = settingsMenu;
  for (const auto& entry : QList<QPair<QString, int>>{{"Requisitos", 1}, {"Integrações", 2}, {"Biblioteca", 4}, {"Contas restritas", 8}})
    settingsMenu->addAction(entry.first, this, [this, entry] { _navigation->setCurrentRow(entry.second); });
  settingsMenu->addSeparator();
  settingsMenu->addAction(QStringLiteral("Recuperação de envios"), this, &MainWindow::openRecovery);
  settingsMenu->addAction(QStringLiteral("Alternar tema"), _themeButton, &QPushButton::click);
  settingsMenu->addAction(QStringLiteral("Verificar atualização"), _updateButton, &QPushButton::click);
  connect(settings, &QToolButton::clicked, this, [settings, settingsMenu] { settingsMenu->exec(settings->mapToGlobal(QPoint(settings->width(), 0))); });
  settings->setParent(sidebar);
  settings->hide();
  auto* profile = new QLabel(QStringLiteral("OL\n\nOperador local"));
  profile->setObjectName(QStringLiteral("railProfile"));
  profile->setAlignment(Qt::AlignCenter);
  side->addWidget(profile);

  auto* workspace = new QWidget;
  workspace->setObjectName(QStringLiteral("workspace"));
  auto* workspaceLayout = new QVBoxLayout(workspace);
  workspaceLayout->setContentsMargins(0, 0, 0, 0);
  workspaceLayout->setSpacing(0);

  _pages = new QStackedWidget;
  qInfo() << "QMoney: construindo home";
  _pages->addWidget(buildHomePage());
  qInfo() << "QMoney: construindo prontidao";
  _pages->addWidget(buildReadinessPage());
  qInfo() << "QMoney: construindo integracoes";
  _pages->addWidget(buildIntegrationsPage());
  qInfo() << "QMoney: construindo campanha";
  _pages->addWidget(buildCampaignPage());
  qInfo() << "QMoney: construindo acelerador";
  _pages->addWidget(buildAcceleratorPage());
  qInfo() << "QMoney: construindo contas";
  _pages->addWidget(buildAccountsPage());
  qInfo() << "QMoney: construindo saldos";
  _pages->addWidget(buildBalancesPage());
  qInfo() << "QMoney: construindo historico";
  _pages->addWidget(buildHistoryPage());
  _pages->addWidget(buildBannedPage());
  qInfo() << "QMoney: paginas prontas";
  workspaceLayout->addWidget(_pages, 1);

  auto* statusBar = new QWidget;
  statusBar->setObjectName(QStringLiteral("appStatusBar"));
  auto* statusLayout = new QHBoxLayout(statusBar);
  statusLayout->setContentsMargins(24, 8, 24, 8);
  _status = quietLabel(QStringLiteral("Preparando o serviço local…"));
  _status->setWordWrap(false);
  _status->setMinimumWidth(0);
  statusLayout->addWidget(_status, 1);
  auto* version = new QLabel(QStringLiteral("QMoney %1  •  Desktop nativo")
                                 .arg(QCoreApplication::applicationVersion()));
  version->setObjectName(QStringLiteral("statusVersion"));
  statusLayout->addWidget(version);
  statusLayout->addSpacing(18);
  auto* refresh = new QPushButton(QStringLiteral("Sincronizar dados"));
  refresh->setFlat(true);
  connect(refresh, &QPushButton::clicked, this, &MainWindow::refreshCurrentPage);
  statusLayout->addWidget(refresh);
  workspaceLayout->addWidget(statusBar);
  statusBar->setVisible(false);
  connect(_pages, &QStackedWidget::currentChanged, statusBar, [statusBar](int index) { statusBar->setVisible(index != 0); });

  rootLayout->addWidget(sidebar);
  rootLayout->addWidget(workspace, 1);
  setCentralWidget(root);
}

QWidget* MainWindow::pageShell(const QString& title, const QString& subtitle, QWidget* body) {
  auto* shell = new QWidget;
  auto* outer = new QVBoxLayout(shell);
  outer->setContentsMargins(19, 24, 14, 24);
  outer->setSpacing(9);
  {
    auto* header = new QHBoxLayout;
    _pageHeaders.append(header);
    auto* headings = new QVBoxLayout;
    auto* brand = new QLabel(QStringLiteral("QMoney  <span style='color:#754dff'>2.0</span>"));
    brand->setObjectName(QStringLiteral("workspaceBrand"));
    headings->addWidget(brand);
    auto* heading = new QLabel(title);
    heading->setObjectName(QStringLiteral("workspaceTitle"));
    headings->addWidget(heading);
    header->addLayout(headings, 1);
    auto* search = new QPushButton(QStringLiteral("Buscar conta, campanha ou ação     Ctrl K"));
    search->setObjectName(QStringLiteral("commandSearch"));
    search->setMinimumWidth(460);
    connect(search, &QPushButton::clicked, this, &MainWindow::openCommandPalette);
    auto* headerActions = new QWidget;
    auto* headerActionsLayout = new QHBoxLayout(headerActions);
    headerActionsLayout->setContentsMargins(0, 0, 0, 0);
    headerActionsLayout->setSpacing(18);
    headerActionsLayout->addWidget(search, 1);
    auto* recovery = new QToolButton;
    recovery->setIcon(QIcon(QStringLiteral(":/qmoney/icons/bell.svg")));
    recovery->setIconSize(QSize(26, 26));
    recovery->setFixedSize(42, 42);
    recovery->setAccessibleName(QStringLiteral("Pendências e recuperação"));
    recovery->setToolTip(QStringLiteral("Pendências e recuperação"));
    connect(recovery, &QToolButton::clicked, this, &MainWindow::openRecovery);
    headerActionsLayout->addWidget(recovery);
    header->addWidget(headerActions);
    auto* user = new QLabel(QStringLiteral("OL   Operador local\n      Nesta instalação"));
    user->setObjectName(QStringLiteral("operatorIdentity"));
    _headerIdentities.append(user);
    header->addSpacing(24);
    header->addWidget(user);
    outer->addLayout(header);
    outer->addSpacing(8);
  }
  if (title != QStringLiteral("Visão da operação")) outer->addWidget(quietLabel(subtitle));
  outer->addWidget(body, 1);
  return shell;
}

QWidget* MainWindow::card(const QString& title, QWidget* content) {
  auto* frame = new QFrame;
  frame->setObjectName(QStringLiteral("card"));
  auto* layout = new QVBoxLayout(frame);
  layout->setContentsMargins(18, 16, 18, 16);
  layout->setSpacing(13);
  if (!title.isEmpty()) {
    auto* heading = new QLabel(title);
    heading->setObjectName(QStringLiteral("cardTitle"));
    heading->setSizePolicy(QSizePolicy::Preferred, QSizePolicy::Fixed);
    layout->addWidget(heading);
  }
  if (content) layout->addWidget(content, 1);
  return frame;
}

QWidget* MainWindow::metric(const QString& value, const QString& caption, QLabel** valueLabel) {
  auto* box = new QWidget;
  auto* layout = new QVBoxLayout(box);
  layout->setContentsMargins(2, 1, 2, 1);
  layout->setSpacing(3);
  auto* label = new QLabel(caption.toUpper());
  label->setObjectName(QStringLiteral("metricCaption"));
  label->setWordWrap(true);
  layout->addWidget(label);
  *valueLabel = new QLabel(value);
  (*valueLabel)->setObjectName(QStringLiteral("metricValue"));
  layout->addWidget(*valueLabel);
  auto* timing = new QLabel(QStringLiteral("LEITURA ATUAL"));
  timing->setObjectName(QStringLiteral("metricTiming"));
  layout->addWidget(timing);
  return box;
}

QPushButton* MainWindow::primaryButton(const QString& text) {
  auto* button = new QPushButton(text);
  button->setProperty("role", QStringLiteral("primary"));
  button->setDefault(true);
  return button;
}

QWidget* MainWindow::buildHomePage() {
  auto* body = new QWidget;
  auto* layout = new QVBoxLayout(body);
  layout->setContentsMargins(0, 0, 0, 0);
  layout->setSpacing(18);
  auto* toolbar = new QHBoxLayout;
  auto addTab = [this, toolbar](const QString& text, int page) {
    auto* button = new QPushButton(text);
    button->setFlat(true);
    button->setObjectName(page == 0 ? QStringLiteral("operationTabActive") : QStringLiteral("operationTab"));
    connect(button, &QPushButton::clicked, this, [this, page] { _navigation->setCurrentRow(page); });
    toolbar->addWidget(button);
  };
  addTab(QStringLiteral("Operação"), 0);
  addTab(QStringLiteral("Contas"), 5);
  addTab(QStringLiteral("Biblioteca"), 4);
  addTab(QStringLiteral("Histórico"), 7);
  toolbar->addStretch();
  auto* create = primaryButton(QStringLiteral("+  Nova campanha"));
  create->setMinimumSize(212, 50);
  connect(create, &QPushButton::clicked, this, [this] { _navigation->setCurrentRow(3); });
  toolbar->addWidget(create);
  layout->addLayout(toolbar);

  auto* columns = new QHBoxLayout;
  _operationColumns = columns;
  columns->setSpacing(18);
  auto* operation = new QWidget;
  auto* main = new QVBoxLayout(operation);
  main->setContentsMargins(10, 14, 10, 14);
  main->setSpacing(14);
  _homePulseTitle = new QLabel(QStringLiteral("Consultando sua operação"));
  _homePulseTitle->setObjectName(QStringLiteral("operationTitle"));
  _homePulseTitle->setWordWrap(true);
  auto* operationHeading = new QHBoxLayout;
  operationHeading->addWidget(_homePulseTitle, 1);
  _operationLive = new QLabel(QStringLiteral("Aguardando"));
  _operationLive->setObjectName(QStringLiteral("liveBadge"));
  _operationLive->setFixedHeight(40);
  operationHeading->addWidget(_operationLive);
  _operationPause = new QPushButton(QStringLiteral("Ⅱ  Pausar"));
  _operationPause->setMinimumSize(118, 40);
  _operationPause->setEnabled(false);
  connect(_operationPause, &QPushButton::clicked, this, [this] {
    _operationPause->setEnabled(false);
    const QString path = _operationPauseRequested ? QStringLiteral("/api/campaigns/resume") : QStringLiteral("/api/campaigns/pause");
    _api.post(path, {}, [this](bool ok, const QJsonDocument&, const QString& error) {
      if (!ok) showError(QStringLiteral("Controle da campanha"), error);
      loadHome();
    });
  });
  operationHeading->addWidget(_operationPause);
  main->addLayout(operationHeading);
  _homePulseBody = quietLabel(QStringLiteral("Aguardando os dados do serviço local."));
  _homePulseBody->setObjectName(QStringLiteral("operationSubtitle"));
  main->addWidget(_homePulseBody);
  _operationStages = new QLabel(operation);
  _operationStages->hide();
  _operationTrack = new OperationTrack;
  main->addSpacing(14);
  main->addWidget(_operationTrack);
  _operationTotal = new QLabel(QStringLiteral("— / — <span style='font-size:14px'>contas concluídas</span>"));
  _operationTotal->setObjectName(QStringLiteral("operationTotal"));
  main->addWidget(_operationTotal);
  _homePulseProgress = new QProgressBar;
  _homePulseProgress->setObjectName(QStringLiteral("operationProgress"));
  _homePulseProgress->setRange(0, 100);
  _homePulseProgress->setValue(0);
  _homePulseProgress->setTextVisible(false);
  _homePulseProgress->setFixedHeight(9);
  _operationProgressRow = new QWidget;
  auto* progressRow = new QHBoxLayout(_operationProgressRow);
  progressRow->setContentsMargins(0, 0, 0, 0);
  progressRow->setSpacing(24);
  progressRow->addWidget(_homePulseProgress, 1);
  auto* percent = new QLabel(QStringLiteral("0%"));
  percent->setMinimumWidth(38);
  percent->setStyleSheet(QStringLiteral("font-size: 16px;"));
  progressRow->addWidget(percent);
  connect(_homePulseProgress, &QProgressBar::valueChanged, percent, [percent](int value) {
    percent->setText(QStringLiteral("%1%").arg(value));
  });
  _operationProgressRow->hide();
  main->addWidget(_operationProgressRow);
  _operationEmpty = quietLabel(QStringLiteral("Nenhuma campanha carregada. Conecte suas contas e revise uma prévia para começar."));
  main->addWidget(_operationEmpty);
  _operationTable = new QTableWidget(0, 4);
  _operationTable->setHorizontalHeaderLabels({QStringLiteral("Conta"), QStringLiteral("Etapa"), QStringLiteral("Progresso"), QStringLiteral("Resultado")});
  _operationTable->verticalHeader()->hide();
  _operationTable->setItemDelegate(new OperationRowDelegate(_operationTable));
  _operationTable->setEditTriggers(QAbstractItemView::NoEditTriggers);
  _operationTable->setSelectionBehavior(QAbstractItemView::SelectRows);
  _operationTable->setShowGrid(false);
  _operationTable->setMinimumHeight(260);
  _operationTable->horizontalHeader()->setSectionResizeMode(QHeaderView::Stretch);
  _operationTable->setObjectName(QStringLiteral("operationTable"));
  _operationTable->horizontalHeader()->setFixedHeight(46);
  _operationTable->horizontalHeader()->setDefaultAlignment(Qt::AlignLeft | Qt::AlignVCenter);
  main->addWidget(_operationTable, 1);

  auto* operationSurface = card(QString(), operation);
  operationSurface->setObjectName(QStringLiteral("operationSurface"));
  columns->addWidget(operationSurface, 68);

  auto* inspector = new QFrame;
  _operationInspector = inspector;
  inspector->setObjectName(QStringLiteral("operationInspector"));
  inspector->setMinimumWidth(310);
  inspector->setMaximumWidth(480);
  auto* context = new QVBoxLayout(inspector);
  context->setContentsMargins(24, 26, 24, 26);
  context->setSpacing(18);
  auto* kicker = new QLabel(QStringLiteral("PRÓXIMO PASSO"));
  kicker->setObjectName(QStringLiteral("heroEyebrow"));
  context->addWidget(kicker);
  _operationContextTitle = new QLabel(QStringLiteral("Prepare sua operação"));
  _operationContextTitle->setObjectName(QStringLiteral("inspectorTitle"));
  _operationContextTitle->setWordWrap(true);
  context->addWidget(_operationContextTitle);
  _homeAccountStep = new QLabel(QStringLiteral("Conecte suas contas e prepare o conteúdo."));
  _homeAccountStep->setObjectName(QStringLiteral("heroDescription"));
  _homeAccountStep->setWordWrap(true);
  context->addWidget(_homeAccountStep);
  _homeNextAction = primaryButton(QStringLiteral("Consultando…"));
  _homeNextAction->setEnabled(false);
  connect(_homeNextAction, &QPushButton::clicked, this, [this] { _navigation->setCurrentRow(_homeDestination); });
  _homeNextAction->setFlat(true);
  context->addWidget(_homeNextAction);
  _operationFeed = new QLabel(QStringLiteral("Os eventos da campanha aparecerão aqui."));
  _operationFeed->setTextFormat(Qt::PlainText);
  _operationFeed->setAlignment(Qt::AlignTop | Qt::AlignLeft);
  _operationFeed->setMargin(8);
  _operationFeed->setWordWrap(true);
  _operationFeed->setObjectName(QStringLiteral("heroDescription"));
  _operationFeed->setParent(inspector);
  _operationFeed->hide();
  _operationTimeline = new OperationTimeline;
  context->addWidget(_operationTimeline, 1);
  auto* balanceCaption = new QLabel(QStringLiteral("SALDO DISPONÍVEL"));
  balanceCaption->setObjectName(QStringLiteral("inspectorBalanceCaption"));
  context->addWidget(balanceCaption);
  _operationBalance = new QLabel(QStringLiteral("US$ —"));
  _operationBalance->setObjectName(QStringLiteral("inspectorBalance"));
  auto* balanceRow = new QHBoxLayout;
  balanceRow->addWidget(_operationBalance, 1);
  context->addLayout(balanceRow);
  _operationBalanceNote = new QLabel(QStringLiteral("Consulte os saldos das suas contas."));
  _operationBalanceNote->setObjectName(QStringLiteral("heroDescription"));
  _operationBalanceNote->setWordWrap(true);
  context->addWidget(_operationBalanceNote);
  auto* balances = new QPushButton(QStringLiteral("Ver saldos →"));
  connect(balances, &QPushButton::clicked, this, [this] { _navigation->setCurrentRow(6); });
  balances->setObjectName(QStringLiteral("inspectorBalanceAction"));
  balances->setMinimumHeight(40);
  balanceRow->addWidget(balances);
  _homeSync = new QLabel(QStringLiteral("Aguardando leitura"));
  _homeSync->setObjectName(QStringLiteral("heroDescription"));
  _homeSync->setWordWrap(true);
  columns->addWidget(inspector, 32);
  layout->addLayout(columns, 1);

  auto* summary = new QWidget;
  auto* stats = new QHBoxLayout(summary);
  stats->setContentsMargins(0, 0, 0, 0);
  stats->addWidget(metric(QStringLiteral("—"), QStringLiteral("contas cadastradas"), &_homeAccounts));
  stats->addWidget(metric(QStringLiteral("—"), QStringLiteral("campanhas no histórico"), &_homeCampaigns));
  stats->addWidget(metric(QStringLiteral("—"), QStringLiteral("envios OK / tentativas na última"), &_homeSuccess));
  summary->setParent(body);
  summary->hide();
  _homeRecent = quietLabel(QStringLiteral("Consultando histórico…"));
  _homeRecent->setParent(body);
  _homeRecent->hide();
  auto* footer = new QHBoxLayout;
  auto* privacy = quietLabel(QStringLiteral("Seus dados ficam nesta instalação"));
  privacy->setWordWrap(false);
  footer->addWidget(privacy);
  footer->addStretch();
  _homeSync->setObjectName(QStringLiteral("quiet"));
  _homeSync->setWordWrap(false);
  footer->addWidget(_homeSync);
  layout->addLayout(footer);
  auto* scroll = new QScrollArea;
  scroll->setWidgetResizable(true);
  scroll->setFrameShape(QFrame::NoFrame);
  scroll->setWidget(body);
  return pageShell(QStringLiteral("Visão da operação"), QStringLiteral("Acompanhe cada etapa. Decida o próximo passo."), scroll);
}

void MainWindow::renderOperation(const QJsonObject& snapshot) {
  const bool active = snapshot.value("state").toString() == "running";
  _operationPauseRequested = snapshot.value("pause_requested").toBool();
  _operationPause->setEnabled(active);
  _operationPause->setText(_operationPauseRequested ? QStringLiteral("▷  Retomar") : QStringLiteral("Ⅱ  Pausar"));
  _operationLive->setText(_operationPauseRequested ? QStringLiteral("Pausa solicitada") : active ? QStringLiteral("●  Ao vivo") : QStringLiteral("Aguardando"));
  _operationLive->setToolTip(_operationPauseRequested ? QStringLiteral("Requisições em andamento podem terminar; a continuação aguarda Retomar.") : QString());
  const auto operation = snapshot.value("operation").toObject();
  const auto rows = operation.value("accounts").toArray();
  const auto counts = operation.value("counts").toObject();
  const QHash<QString, QString> labels{
    {"queued", "Na fila"}, {"waiting", "Aguardando"}, {"preparing", "Preparando"},
    {"sending", "Enviando"}, {"confirming", "Confirmando"}, {"confirmed", "Confirmado"},
    {"failed", "Falhou"}, {"skipped", "Pulado"}, {"recovering", "Recuperando"},
    {"excluded", "Excluída"}, {"pending", "Pendente"}, {"unconfirmed", "Sem confirmação"}};
  _operationEmpty->setVisible(rows.isEmpty());
  _operationTrack->setCounts(counts);
  _operationContextTitle->setText(counts.value("confirming").toInt() > 0 ? QStringLiteral("Confirmar recebimento") : snapshot.value("state").toString() == "running" ? QStringLiteral("Acompanhar os envios") : QStringLiteral("Planejar sua campanha"));
  _operationTotal->setText(QStringLiteral("%1 / %2 <span style='font-size:14px'>contas com último envio confirmado</span>")
      .arg(counts.value("confirmed").toInt()).arg(rows.size()));
  _operationTable->setRowCount(rows.size());
  for (int i = 0; i < rows.size(); ++i) {
    const auto row = rows[i].toObject();
    const auto state = row.value("state").toString();
    const auto progress = row.value("progress");
    const QStringList cells{row.value("email").toString(), labels.value(state, QStringLiteral("Não informado")),
       progress.isDouble() ? QStringLiteral("%1%").arg(progress.toInt()) : QStringLiteral("—"),
       state == "confirmed" ? QStringLiteral("Enviado") : state == "failed" ? QStringLiteral("Falhou") : state == "sending" ? QStringLiteral("Em andamento") : labels.value(state)};
    for (int column = 0; column < cells.size(); ++column) {
      auto* cell = new QTableWidgetItem(cells[column]);
      cell->setData(Qt::UserRole, row);
      cell->setToolTip(QStringLiteral("Clipe: %1\nSessão: %2\nFalhas: %3 • Pulados: %4")
          .arg(row.value("clip_uid").toString(), row.value("session_id").toString())
          .arg(row.value("failed").toInt()).arg(row.value("skipped").toInt()));
      _operationTable->setItem(i, column, cell);
    }
    _operationTable->setRowHeight(i, 58);
  }
  _operationStages->setText(QStringLiteral("Preparando %1   →   Enviando %2   →   Confirmando %3   →   Confirmados %4")
      .arg(counts.value("preparing").toInt()).arg(counts.value("sending").toInt())
      .arg(counts.value("confirming").toInt()).arg(counts.value("confirmed").toInt()));
  QStringList feed;
  const auto events = snapshot.value("events").toArray();
  _operationTimeline->setEvents(events);
  for (int i = qMax(0, int(events.size()) - 3); i < events.size(); ++i) {
    const auto event = events[i].toObject();
    const auto time = QDateTime::fromSecsSinceEpoch(qint64(event.value("ts").toDouble())).toString("HH:mm");
    feed << time + QStringLiteral("  ") + event.value("title").toString() + QStringLiteral("\n") + event.value("detail").toString();
  }
  _operationFeed->setText(feed.isEmpty() ? QStringLiteral("Nenhum evento nesta execução.") : feed.join(QStringLiteral("\n\n")));
}

QWidget* MainWindow::buildReadinessPage() {
  auto* body = new QWidget;
  auto* layout = new QVBoxLayout(body);
  layout->setContentsMargins(0, 0, 0, 0);
  layout->setSpacing(14);

  auto* command = new QWidget;
  auto* commandLayout = new QHBoxLayout(command);
  commandLayout->setContentsMargins(0, 0, 0, 0);
  commandLayout->setSpacing(18);
  auto* copy = new QVBoxLayout;
  copy->setSpacing(6);
  auto* kicker = new QLabel(QStringLiteral("LIBERAÇÃO OPERACIONAL"));
  kicker->setObjectName(QStringLiteral("kicker"));
  copy->addWidget(kicker);
  _readinessHeadline = new QLabel(QStringLiteral("Medindo o ambiente…"));
  _readinessHeadline->setObjectName(QStringLiteral("pulseTitle"));
  copy->addWidget(_readinessHeadline);
  _readinessSummary = quietLabel(
      QStringLiteral("O QMoney validará contas, ferramentas, catálogos e armazenamento."));
  copy->addWidget(_readinessSummary);
  _readinessProgress = new QProgressBar;
  _readinessProgress->setRange(0, 100);
  _readinessProgress->setTextVisible(false);
  copy->addWidget(_readinessProgress);
  commandLayout->addLayout(copy, 1);
  auto* commandActions = new QVBoxLayout;
  _readinessRefresh = primaryButton(QStringLiteral("Executar verificação"));
  connect(_readinessRefresh, &QPushButton::clicked, this, &MainWindow::loadReadiness);
  commandActions->addWidget(_readinessRefresh);
  _diagnosticsExport = new QPushButton(QStringLiteral("Exportar diagnóstico"));
  connect(_diagnosticsExport, &QPushButton::clicked, this, &MainWindow::exportDiagnostics);
  commandActions->addWidget(_diagnosticsExport);
  commandActions->addStretch();
  commandLayout->addLayout(commandActions);
  auto* commandCard = card(QString(), command);
  commandCard->setObjectName(QStringLiteral("pulseCard"));
  layout->addWidget(commandCard);

  _readinessTable = new QTableWidget(0, 3);
  configureTable(_readinessTable, QStringLiteral("Prepare sua primeira operação"),
                 QStringLiteral("Execute a verificação para conferir contas, conteúdo e requisitos desta instalação."));
  _readinessTable->setHorizontalHeaderLabels(
      {QStringLiteral("Estado"), QStringLiteral("Componente"), QStringLiteral("Leitura")});
  _readinessTable->horizontalHeader()->setSectionResizeMode(0, QHeaderView::Fixed);
  _readinessTable->horizontalHeader()->setSectionResizeMode(1, QHeaderView::Interactive);
  _readinessTable->horizontalHeader()->setSectionResizeMode(2, QHeaderView::Stretch);
  _readinessTable->setColumnWidth(0, 118);
  _readinessTable->setColumnWidth(1, 230);
  layout->addWidget(card(QStringLiteral("Matriz de prontidão"), _readinessTable), 1);

  auto* library = new QWidget;
  auto* libraryLayout = new QHBoxLayout(library);
  libraryLayout->setContentsMargins(0, 0, 0, 0);
  libraryLayout->setSpacing(14);
  auto* libraryCopy = new QVBoxLayout;
  _libraryPath = new QLabel(QStringLiteral("Localizando biblioteca…"));
  _libraryPath->setObjectName(QStringLiteral("libraryPath"));
  _libraryPath->setTextInteractionFlags(Qt::TextSelectableByMouse);
  libraryCopy->addWidget(_libraryPath);
  _libraryUsage = quietLabel(QStringLiteral("Medindo uso e espaço livre."));
  libraryCopy->addWidget(_libraryUsage);
  libraryLayout->addLayout(libraryCopy, 1);
  auto* openLibrary = new QPushButton(QStringLiteral("Abrir pasta"));
  connect(openLibrary, &QPushButton::clicked, this, [this] {
    if (!_currentLibraryRoot.isEmpty())
      QDesktopServices::openUrl(QUrl::fromLocalFile(_currentLibraryRoot));
  });
  libraryLayout->addWidget(openLibrary);
  _libraryChoose = primaryButton(QStringLiteral("Escolher biblioteca"));
  connect(_libraryChoose, &QPushButton::clicked, this, &MainWindow::chooseLibrary);
  libraryLayout->addWidget(_libraryChoose);
  layout->addWidget(card(QStringLiteral("Biblioteca de mídia"), library));

  return pageShell(QStringLiteral("Prontidão"),
                   QStringLiteral("Confirme cada dependência antes de liberar uma campanha."), body);
}

QWidget* MainWindow::buildIntegrationsPage() {
  auto* body = new QWidget;
  auto* bodyLayout = new QVBoxLayout(body);
  bodyLayout->setContentsMargins(0, 0, 0, 0);

  auto* scroll = new QScrollArea;
  scroll->setWidgetResizable(true);
  scroll->setFrameShape(QFrame::NoFrame);
  auto* content = new QWidget;
  auto* layout = new QVBoxLayout(content);
  layout->setContentsMargins(0, 0, 8, 0);
  layout->setSpacing(14);

  auto* hero = new QWidget;
  auto* heroLayout = new QHBoxLayout(hero);
  heroLayout->setContentsMargins(0, 0, 0, 0);
  heroLayout->setSpacing(18);
  auto* heroCopy = new QVBoxLayout;
  heroCopy->setSpacing(5);
  auto* kicker = new QLabel(QStringLiteral("COFRE DE ACESSO"));
  kicker->setObjectName(QStringLiteral("kicker"));
  heroCopy->addWidget(kicker);
  _integrationsHeadline = new QLabel(QStringLiteral("Verificando suas conexões…"));
  _integrationsHeadline->setObjectName(QStringLiteral("pulseTitle"));
  heroCopy->addWidget(_integrationsHeadline);
  _integrationsSummary = quietLabel(QStringLiteral(
      "O QMoney organiza credenciais, catálogos e ferramentas sem exigir arquivos manuais."));
  _integrationsSummary->setWordWrap(true);
  heroCopy->addWidget(_integrationsSummary);
  heroLayout->addLayout(heroCopy, 1);
  _integrationSecurity = quietLabel(QStringLiteral("Verificando proteção"));
  _integrationSecurity->setObjectName(QStringLiteral("securityBadge"));
  _integrationSecurity->setAlignment(Qt::AlignCenter);
  _integrationSecurity->setSizePolicy(QSizePolicy::Preferred, QSizePolicy::Fixed);
  heroLayout->addWidget(_integrationSecurity);
  auto* heroCard = card(QString(), hero);
  heroCard->setObjectName(QStringLiteral("integrationsContext"));
  heroCard->setSizePolicy(QSizePolicy::Preferred, QSizePolicy::Fixed);
  layout->addWidget(heroCard);
  auto* connectors = new QTabWidget;
  layout->addWidget(connectors);
  const auto addConnector = [connectors](const QString& title, QWidget* panel) {
    auto* wrapper = new QWidget;
    auto* column = new QVBoxLayout(wrapper);
    column->setContentsMargins(0, 0, 0, 0);
    column->addWidget(panel);
    column->addStretch();
    connectors->addTab(wrapper, title);
  };

  auto* egoBody = new QWidget;
  auto* egoLayout = new QVBoxLayout(egoBody);
  egoLayout->setContentsMargins(0, 0, 0, 0);
  egoLayout->setSpacing(12);
  auto* egoStatusRow = new QHBoxLayout;
  _ego4dStatus = new QLabel(QStringLiteral("Verificando credencial…"));
  _ego4dStatus->setObjectName(QStringLiteral("integrationStatus"));
  egoStatusRow->addWidget(_ego4dStatus, 1);
  _ego4dCatalog = quietLabel(QStringLiteral("Catálogo: verificando…"));
  egoStatusRow->addWidget(_ego4dCatalog);
  egoLayout->addLayout(egoStatusRow);
  auto* egoHelp = quietLabel(QStringLiteral(
      "Cole as chaves recebidas após a aprovação da licença Ego4D. O QMoney valida o acesso antes de salvar."));
  egoHelp->setWordWrap(true);
  egoLayout->addWidget(egoHelp);

  auto* egoFormBody = new QWidget;
  auto* egoForm = new QFormLayout(egoFormBody);
  configureForm(egoForm);
  egoForm->setContentsMargins(0, 0, 0, 0);
  _ego4dAccessKey = new QLineEdit;
  _ego4dAccessKey->setEchoMode(QLineEdit::PasswordEchoOnEdit);
  _ego4dAccessKey->setPlaceholderText(QStringLiteral("Access Key ID recebido por email"));
  egoForm->addRow(QStringLiteral("Access Key ID"), _ego4dAccessKey);
  _ego4dSecretKey = new QLineEdit;
  _ego4dSecretKey->setEchoMode(QLineEdit::Password);
  _ego4dSecretKey->setPlaceholderText(QStringLiteral("Secret Access Key"));
  egoForm->addRow(QStringLiteral("Secret Access Key"), _ego4dSecretKey);
  auto updateEgoTest = [this] {
    if (!_ego4dAccessKey->text().trimmed().isEmpty()
        && !_ego4dSecretKey->text().trimmed().isEmpty())
      _ego4dTest->setEnabled(true);
  };
  connect(_ego4dAccessKey, &QLineEdit::textChanged, this, updateEgoTest);
  connect(_ego4dSecretKey, &QLineEdit::textChanged, this, updateEgoTest);
  _ego4dSessionToken = new QLineEdit;
  _ego4dSessionToken->setEchoMode(QLineEdit::Password);
  _ego4dSessionToken->setPlaceholderText(QStringLiteral("Opcional; deixe vazio para manter o salvo"));
  egoForm->addRow(QStringLiteral("Session Token"), _ego4dSessionToken);
  _ego4dRegion = new QLineEdit;
  _ego4dRegion->setPlaceholderText(QStringLiteral("Automática"));
  egoForm->addRow(QStringLiteral("Região AWS"), _ego4dRegion);
  egoLayout->addWidget(egoFormBody);

  auto* egoActions = new QHBoxLayout;
  auto* accessHelp = new QPushButton(QStringLiteral("Como obter acesso"));
  connect(accessHelp, &QPushButton::clicked, this, [] {
    QDesktopServices::openUrl(QUrl(QStringLiteral(
        "https://ego4d-data.org/docs/start-here/")));
  });
  egoActions->addWidget(accessHelp);
  _ego4dPrepare = new QPushButton(QStringLiteral("Preparar catálogo"));
  connect(_ego4dPrepare, &QPushButton::clicked,
          this, &MainWindow::prepareEgo4dCatalog);
  egoActions->addWidget(_ego4dPrepare);
  egoActions->addStretch();
  _ego4dTest = new QPushButton(QStringLiteral("Testar acesso"));
  connect(_ego4dTest, &QPushButton::clicked,
          this, &MainWindow::testEgo4dIntegration);
  egoActions->addWidget(_ego4dTest);
  _ego4dSave = primaryButton(QStringLiteral("Validar e salvar"));
  connect(_ego4dSave, &QPushButton::clicked,
          this, &MainWindow::saveEgo4dIntegration);
  egoActions->addWidget(_ego4dSave);
  egoLayout->addLayout(egoActions);
  addConnector(QStringLiteral("Conteúdo licenciado"), card(QStringLiteral("Ego4D"), egoBody));

  auto* hostBody = new QWidget;
  auto* hostLayout = new QVBoxLayout(hostBody);
  hostLayout->setContentsMargins(0, 0, 0, 0);
  hostLayout->setSpacing(12);
  _hostingerStatus = new QLabel(QStringLiteral("Verificando token…"));
  _hostingerStatus->setObjectName(QStringLiteral("integrationStatus"));
  hostLayout->addWidget(_hostingerStatus);
  auto* hostHelp = quietLabel(QStringLiteral(
      "Cole o token da API. O QMoney identifica sozinho as caixas, os endereços e "
      "os domínios, e passa a buscar cada código no lugar certo."));
  hostHelp->setWordWrap(true);
  hostLayout->addWidget(hostHelp);
  auto* hostFormBody = new QWidget;
  auto* hostForm = new QFormLayout(hostFormBody);
  configureForm(hostForm);
  hostForm->setContentsMargins(0, 0, 0, 0);
  auto* hostSelector = new QWidget;
  auto* hostSelectorLayout = new QHBoxLayout(hostSelector);
  hostSelectorLayout->setContentsMargins(0, 0, 0, 0);
  hostSelectorLayout->setSpacing(8);
  _hostingerProfile = new ComboBox;
  configureCombo(_hostingerProfile, 280);
  connect(_hostingerProfile, qOverload<int>(&QComboBox::currentIndexChanged),
          this, &MainWindow::selectHostingerIntegration);
  hostSelectorLayout->addWidget(_hostingerProfile, 1);
  _hostingerRemove = new QPushButton(QStringLiteral("Remover"));
  connect(_hostingerRemove, &QPushButton::clicked,
          this, &MainWindow::removeHostingerIntegration);
  hostSelectorLayout->addWidget(_hostingerRemove);
  hostForm->addRow(QStringLiteral("Caixas conectadas"), hostSelector);
  _hostingerToken = new QLineEdit;
  _hostingerToken->setEchoMode(QLineEdit::Password);
  _hostingerToken->setPlaceholderText(
      QStringLiteral("Cole o token de outra conta Hostinger"));
  hostForm->addRow(QStringLiteral("Adicionar API"), _hostingerToken);
  connect(_hostingerToken, &QLineEdit::textChanged, this, [this] {
    const bool entered = !_hostingerToken->text().trimmed().isEmpty();
    _hostingerTest->setEnabled(entered || _hostingerProfile->currentIndex() >= 0);
    _hostingerSave->setEnabled(entered);
  });
  hostLayout->addWidget(hostFormBody);
  auto* hostActions = new QHBoxLayout;
  hostActions->addStretch();
  _hostingerTest = new QPushButton(QStringLiteral("Testar"));
  connect(_hostingerTest, &QPushButton::clicked,
          this, &MainWindow::testHostingerIntegration);
  hostActions->addWidget(_hostingerTest);
  _hostingerSave = primaryButton(QStringLiteral("Identificar e conectar"));
  _hostingerSave->setEnabled(false);
  connect(_hostingerSave, &QPushButton::clicked,
          this, &MainWindow::saveHostingerIntegration);
  hostActions->addWidget(_hostingerSave);
  hostLayout->addLayout(hostActions);
  addConnector(QStringLiteral("Códigos de verificação"), card(QStringLiteral("Hostinger"), hostBody));

  auto* local = new QWidget;
  auto* localLayout = new QHBoxLayout(local);
  localLayout->setContentsMargins(0, 0, 0, 0);
  localLayout->setSpacing(18);
  auto* holoCopy = new QVBoxLayout;
  auto* holoTitle = new QLabel(QStringLiteral("HoloAssist"));
  holoTitle->setObjectName(QStringLiteral("integrationMiniTitle"));
  holoCopy->addWidget(holoTitle);
  _holoIntegrationStatus = quietLabel(QStringLiteral("Verificando catálogo e índices…"));
  _holoIntegrationStatus->setWordWrap(true);
  holoCopy->addWidget(_holoIntegrationStatus);
  localLayout->addLayout(holoCopy, 1);
  auto* runtimeCopy = new QVBoxLayout;
  auto* runtimeTitle = new QLabel(QStringLiteral("Ferramentas privadas"));
  runtimeTitle->setObjectName(QStringLiteral("integrationMiniTitle"));
  runtimeCopy->addWidget(runtimeTitle);
  _runtimeIntegrationStatus = quietLabel(QStringLiteral("Verificando FFmpeg e FFprobe…"));
  _runtimeIntegrationStatus->setWordWrap(true);
  runtimeCopy->addWidget(_runtimeIntegrationStatus);
  localLayout->addLayout(runtimeCopy, 1);
  auto* readinessButton = new QPushButton(QStringLiteral("Abrir prontidão"));
  connect(readinessButton, &QPushButton::clicked, this,
          [this] { _navigation->setCurrentRow(1); });
  localLayout->addWidget(readinessButton);
  _repairInstall = new QPushButton(QStringLiteral("Reparar instalação"));
  _repairInstall->setToolTip(QStringLiteral(
      "Baixa novamente o pacote oficial assinado sem apagar contas ou configurações."));
  connect(_repairInstall, &QPushButton::clicked, this, [this] {
    if (_updates.isBusy()) return;
    _repairInstall->setEnabled(false);
    _repairInstall->setText(QStringLiteral("Localizando pacote…"));
    _updates.repair();
  });
  localLayout->addWidget(_repairInstall);
  addConnector(QStringLiteral("Esta instalação"), card(QStringLiteral("Componentes incluídos no QMoney"), local));
  layout->addStretch();

  scroll->setWidget(content);
  bodyLayout->addWidget(scroll);
  return pageShell(QStringLiteral("Integrações"),
                   QStringLiteral("Configure tudo que o QMoney precisa sem procurar arquivos no computador."),
                   body);
}

QWidget* MainWindow::buildCampaignPage() {
  auto* body = new QWidget;
  auto* bodyLayout = new QVBoxLayout(body);
  bodyLayout->setContentsMargins(0, 0, 0, 0);
  bodyLayout->setSpacing(14);
  auto* tabs = new QTabWidget;
  bodyLayout->addWidget(tabs, 1);

  auto* scroll = new QScrollArea;
  scroll->setWidgetResizable(true);
  scroll->setFrameShape(QFrame::NoFrame);
  tabs->addTab(scroll, QStringLiteral("Conteúdo e contas"));
  const auto addSection = [tabs](const QString& title, QWidget* section) {
    auto* page = new QScrollArea;
    page->setWidgetResizable(true);
    page->setFrameShape(QFrame::NoFrame);
    auto* wrapper = new QWidget;
    auto* column = new QVBoxLayout(wrapper);
    column->setContentsMargins(0, 0, 8, 0);
    column->addWidget(section);
    column->addStretch();
    page->setWidget(wrapper);
    tabs->addTab(page, title);
  };
  auto* content = new QWidget;
  auto* layout = new QVBoxLayout(content);
  layout->setContentsMargins(0, 0, 8, 0);
  layout->setSpacing(14);

  auto* sourceBody = new QWidget;
  auto* sourceColumn = new QVBoxLayout(sourceBody);
  sourceColumn->setContentsMargins(0, 0, 0, 0);
  auto* sourceLayout = new QHBoxLayout;
  sourceColumn->addLayout(sourceLayout);
  _dataset = new ComboBox;
  configureCombo(_dataset, 180);
  _dataset->addItem(QStringLiteral("Conteúdo combinado"), QStringLiteral("all"));
  _dataset->addItem(QStringLiteral("Somente Ego4D"), QStringLiteral("ego4d"));
  _dataset->addItem(QStringLiteral("Somente HoloAssist"), QStringLiteral("holoassist"));
  _dataset->setCurrentIndex(1);
  connect(_dataset, &QComboBox::currentIndexChanged, this,
          [this] { _taskReload.start(); });
  sourceLayout->addWidget(new QLabel(QStringLiteral("Origem")));
  sourceLayout->addWidget(_dataset, 1);
  _contentMode = new ComboBox;
  configureCombo(_contentMode, 190);
  _contentMode->addItem(QStringLiteral("Cache + dataset"), QStringLiteral("both"));
  _contentMode->addItem(QStringLiteral("Somente cache pronto"), QStringLiteral("cache"));
  _contentMode->addItem(QStringLiteral("Catálogo do dataset"), QStringLiteral("dataset"));
  _contentMode->setToolTip(QStringLiteral(
      "Somente cache usa clipes já preparados. Catálogo seleciona clipes elegíveis sem priorizar o cache. "
      "Cache + dataset começa pelos prontos e completa com o catálogo."));
  connect(_contentMode, &QComboBox::currentIndexChanged, this,
          [this] { _taskReload.start(); });
  auto* reloadTasks = new QPushButton(QStringLiteral("Recarregar categorias"));
  connect(reloadTasks, &QPushButton::clicked, this, &MainWindow::loadTasks);
  sourceLayout->addWidget(reloadTasks);
  _campaignReset = new QPushButton(QStringLiteral("Resetar lista"));
  _campaignReset->setToolTip(QStringLiteral(
      "Limpa a lista de vídeos já utilizados e permite que eles sejam selecionados novamente."));
  connect(_campaignReset, &QPushButton::clicked, this, [this] {
    const auto choice = QMessageBox::warning(
        this, QStringLiteral("Resetar lista de vídeos usados"),
        QStringLiteral("Os vídeos registrados como já utilizados poderão ser selecionados "
                       "novamente nas próximas campanhas.\n\n"
                       "O histórico das campanhas e os dados das contas serão preservados. "
                       "Deseja continuar?"),
        QMessageBox::Reset | QMessageBox::Cancel, QMessageBox::Cancel);
    if (choice != QMessageBox::Reset) return;
    _campaignReset->setEnabled(false);
    _campaignReset->setText(QStringLiteral("Resetando…"));
    _api.post(QStringLiteral("/api/sent/reset"), {},
              [this](bool ok, const QJsonDocument&, const QString& error) {
      _campaignReset->setText(QStringLiteral("Resetar lista"));
      _campaignReset->setEnabled(!_campaignActive);
      if (!ok)
        return showError(QStringLiteral("Lista não resetada"), error);
      setStatus(QStringLiteral(
          "Lista de vídeos usados resetada. O histórico foi preservado."));
      loadTasks();
    });
  });
  sourceLayout->addWidget(_campaignReset);
  auto* modeLayout = new QHBoxLayout;
  modeLayout->addWidget(new QLabel(QStringLiteral("Uso da mídia")));
  modeLayout->addWidget(_contentMode);
  modeLayout->addStretch();
  sourceColumn->addLayout(modeLayout);
  layout->addWidget(card(QStringLiteral("Conteúdo da campanha"), sourceBody));

  auto* selection = new QWidget;
  auto* selectionLayout = new QHBoxLayout(selection);
  _campaignSelectionColumns = selectionLayout;
  selectionLayout->setContentsMargins(0, 0, 0, 0);
  auto* accountCol = new QVBoxLayout;
  auto* accountHeading = quietLabel(QStringLiteral("CONTAS DE DESTINO"));
  accountHeading->setWordWrap(false);
  accountHeading->setMinimumHeight(36);
  accountCol->addWidget(accountHeading);
  auto* accountControls = new QHBoxLayout;
  _campaignAccountMode = new ComboBox;
  _campaignAccountMode->addItem(QStringLiteral("Escolher manualmente"), QStringLiteral("manual"));
  _campaignAccountMode->addItem(QStringLiteral("Sortear contas"), QStringLiteral("random"));
  _campaignAccountMode->addItem(QStringLiteral("Rodízio: menos usadas"), QStringLiteral("rotation"));
  _campaignAccountMode->addItem(QStringLiteral("Maior saldo aprovado"), QStringLiteral("balance_available_desc"));
  _campaignAccountMode->addItem(QStringLiteral("Menor saldo aprovado"), QStringLiteral("balance_available_asc"));
  _campaignAccountMode->addItem(QStringLiteral("Maior saldo pendente"), QStringLiteral("balance_pending_desc"));
  _campaignAccountMode->addItem(QStringLiteral("Menor saldo pendente"), QStringLiteral("balance_pending_asc"));
  _campaignAccountMode->setToolTip(QStringLiteral(
      "Escolha manual, sorteio, rodízio ou ordenação pelo último saldo Crowtado confirmado."));
  accountControls->addWidget(_campaignAccountMode, 1);
  auto* allAccounts = new QPushButton(QStringLiteral("Todas"));
  allAccounts->setToolTip(QStringLiteral("Seleciona todas as contas cadastradas."));
  accountControls->addWidget(allAccounts);
  auto* clearAccounts = new QPushButton(QStringLiteral("Limpar"));
  clearAccounts->setToolTip(QStringLiteral("Desmarca todas as contas para escolher uma a uma."));
  accountControls->addWidget(clearAccounts);
  _campaignAccountCount = new QSpinBox;
  _campaignAccountCount->setRange(1, 1);
  _campaignAccountCount->setPrefix(QStringLiteral("Quantidade: "));
  _campaignAccountCount->setToolTip(QStringLiteral("Número de contas a incluir nesta campanha."));
  _campaignAccountCount->hide();
  accountControls->addWidget(_campaignAccountCount);
  _campaignDrawAccounts = new QPushButton(QStringLiteral("Sortear"));
  _campaignDrawAccounts->setToolTip(QStringLiteral("Faz um novo sorteio das contas disponíveis."));
  _campaignDrawAccounts->hide();
  accountControls->addWidget(_campaignDrawAccounts);
  accountCol->addLayout(accountControls);
  _campaignAccountSearch = new QLineEdit;
  _campaignAccountSearch->setPlaceholderText(QStringLiteral("Buscar conta por e-mail"));
  _campaignAccountSearch->setClearButtonEnabled(true);
  accountCol->addWidget(_campaignAccountSearch);
  _campaignAccounts = new QListWidget;
  new TableEmptyState(_campaignAccounts, QStringLiteral("Selecione suas contas"),
                      QStringLiteral("As contas cadastradas aparecem aqui. Use Contas para conectar a primeira."));
  _campaignAccounts->setMinimumHeight(190);
  _campaignAccounts->setSpacing(2);
  _campaignAccounts->setUniformItemSizes(true);
  connect(_campaignAccounts, &QListWidget::itemChanged, this, [this] {
    updateCampaignAccountCount();
    _taskReload.start();
  });
  accountCol->addWidget(_campaignAccounts);
  connect(_campaignAccountSearch, &QLineEdit::textChanged, this, [this](const QString& search) {
    for (int i = 0; i < _campaignAccounts->count(); ++i)
      _campaignAccounts->item(i)->setHidden(
          !_campaignAccounts->item(i)->data(Qt::UserRole).toString().contains(
              search.trimmed(), Qt::CaseInsensitive));
  });
  _campaignAccountSelection = quietLabel(QStringLiteral("0 de 0 contas selecionadas"));
  accountCol->addWidget(_campaignAccountSelection);
  _campaignBalanceHint = quietLabel(QString());
  _campaignBalanceHint->setWordWrap(true);
  _campaignBalanceHint->hide();
  accountCol->addWidget(_campaignBalanceHint);
  const auto setAllAccounts = [this](Qt::CheckState state) {
    {
      const QSignalBlocker blocker(_campaignAccounts);
      for (int i = 0; i < _campaignAccounts->count(); ++i)
        _campaignAccounts->item(i)->setCheckState(state);
    }
    updateCampaignAccountCount();
    _taskReload.start();
    _campaignDraftSave.start();
  };
  connect(allAccounts, &QPushButton::clicked, this,
          [setAllAccounts] { setAllAccounts(Qt::Checked); });
  connect(clearAccounts, &QPushButton::clicked, this,
          [setAllAccounts] { setAllAccounts(Qt::Unchecked); });
  connect(_campaignAccountMode, &QComboBox::currentIndexChanged, this, [this, allAccounts, clearAccounts] {
    const QString mode = _campaignAccountMode->currentData().toString();
    const bool automatic = mode != QStringLiteral("manual");
    allAccounts->setVisible(!automatic);
    clearAccounts->setVisible(!automatic);
    _campaignAccountCount->setVisible(automatic);
    _campaignDrawAccounts->setVisible(automatic);
    _campaignDrawAccounts->setText(mode.startsWith(QStringLiteral("balance_"))
        ? QStringLiteral("Recalcular lista") : mode == QStringLiteral("rotation")
        ? QStringLiteral("Atualizar rodízio") : QStringLiteral("Sortear"));
    _campaignAccounts->setEnabled(!automatic);
    _campaignBalanceHint->setVisible(mode.startsWith(QStringLiteral("balance_")));
    _campaignBalancesLoaded = false;
    ++_campaignBalanceRequestId;
    if (automatic) drawCampaignAccounts();
    else { updateCampaignAccountCount(); _taskReload.start(); }
  });
  connect(_campaignAccountCount, &QSpinBox::valueChanged, this,
          [this] { drawCampaignAccounts(); });
  connect(_campaignDrawAccounts, &QPushButton::clicked, this, [this] {
    if (_campaignAccountMode->currentData().toString().startsWith(QStringLiteral("balance_")))
      _campaignBalancesLoaded = false;
    drawCampaignAccounts();
  });
  auto* taskCol = new QVBoxLayout;
  auto* taskHead = new QHBoxLayout;
  auto* taskHeading = quietLabel(QStringLiteral("CATEGORIAS DO MINUTE"));
  taskHeading->setWordWrap(false);
  taskHeading->setMinimumHeight(36);
  taskHead->addWidget(taskHeading);
  taskHead->addStretch();
  auto* allTasks = new QPushButton(QStringLiteral("Marcar todas"));
  allTasks->setFlat(true);
  allTasks->setFixedHeight(36);
  connect(allTasks, &QPushButton::clicked, this, [this] {
    for (int i = 0; i < _campaignTasks->count(); ++i) {
      auto* item = _campaignTasks->item(i);
      if (item->flags() & Qt::ItemIsEnabled) item->setCheckState(Qt::Checked);
    }
  });
  taskHead->addWidget(allTasks);
  taskCol->addLayout(taskHead);
  _campaignTasks = new QListWidget;
  new TableEmptyState(_campaignTasks, QStringLiteral("Encontre conteúdo compatível"),
                      QStringLiteral("Selecione uma conta e recarregue as categorias disponíveis."));
  _campaignTasks->setMinimumHeight(190);
  _campaignTasks->setSpacing(2);
  _campaignTasks->setUniformItemSizes(true);
  connect(_campaignTasks, &QListWidget::itemChanged, this, [this] {
    _campaignTaskSelectionTouched = true;
    _campaignSelectedTaskIds.clear();
    for (int i = 0; i < _campaignTasks->count(); ++i) {
      auto* item = _campaignTasks->item(i);
      if (item->checkState() == Qt::Checked && !item->data(Qt::UserRole).toString().isEmpty())
        _campaignSelectedTaskIds.insert(item->data(Qt::UserRole).toString());
    }
  });
  taskCol->addWidget(_campaignTasks);
  selectionLayout->addLayout(accountCol, 1);
  selectionLayout->addLayout(taskCol, 1);
  auto* selectionBody = new QWidget;
  auto* selectionBodyLayout = new QVBoxLayout(selectionBody);
  selectionBodyLayout->setContentsMargins(0, 0, 0, 0);
  selectionBodyLayout->addWidget(selection);
  auto* draftHelp = quietLabel(QStringLiteral(
      "Rascunho salvo automaticamente neste computador. A campanha só começa após a prévia e sua confirmação."));
  draftHelp->setWordWrap(true);
  selectionBodyLayout->addWidget(draftHelp);
  layout->addWidget(card(QStringLiteral("Seleção"), selectionBody));

  auto* parameters = new QWidget;
  auto* form = new QFormLayout(parameters);
  configureForm(form);
  form->setContentsMargins(0, 0, 0, 0);
  _targetHours = new QDoubleSpinBox;
  _targetHours->setRange(0.5, 12.0);
  _targetHours->setSingleStep(0.5);
  _targetHours->setValue(8.0);
  _targetHours->setSuffix(QStringLiteral(" h / conta"));
  form->addRow(QStringLiteral("Meta de gravação"), _targetHours);
  _minDuration = new QSpinBox;
  _minDuration->setRange(1, 30);
  _minDuration->setValue(1);
  _minDuration->setSuffix(QStringLiteral(" min"));
  _minDuration->setToolTip(
      QStringLiteral("O QMoney selecionará somente vídeos com pelo menos esta duração."));
  _maxDuration = new QSpinBox;
  _maxDuration->setRange(1, 30);
  _maxDuration->setValue(30);
  _maxDuration->setSuffix(QStringLiteral(" min"));
  _maxDuration->setToolTip(
      QStringLiteral("O QMoney selecionará somente vídeos até esta duração."));
  connect(_minDuration, &QSpinBox::valueChanged, this, [this](int minimum) {
    const QSignalBlocker blocker(_maxDuration);
    _maxDuration->setMinimum(minimum);
    _taskReload.start();
  });
  connect(_maxDuration, &QSpinBox::valueChanged, this, [this](int maximum) {
    const QSignalBlocker blocker(_minDuration);
    _minDuration->setMaximum(maximum);
    _taskReload.start();
  });
  auto* duration = new QWidget;
  auto* durationLayout = new QHBoxLayout(duration);
  durationLayout->setContentsMargins(0, 0, 0, 0);
  durationLayout->addWidget(_minDuration);
  durationLayout->addWidget(new QLabel(QStringLiteral("até")));
  durationLayout->addWidget(_maxDuration);
  durationLayout->addStretch();
  _minDuration->setAccessibleName(QStringLiteral("Duração mínima"));
  _maxDuration->setAccessibleName(QStringLiteral("Duração máxima"));
  _targetHours->setMaximumWidth(200);
  _minDuration->setFixedWidth(130);
  _maxDuration->setFixedWidth(130);
  form->addRow(QStringLiteral("Duração dos vídeos"), duration);
  _accountWorkers = new ComboBox;
  configureCombo(_accountWorkers, 240);
  for (int workers : {1, 2, 3, 4, 6})
    _accountWorkers->addItem(QStringLiteral("%1 simultâneo(s)").arg(workers), workers);
  _accountWorkers->setCurrentIndex(_accountWorkers->findData(6));
  _accountWorkers->setToolTip(QStringLiteral(
      "Limite de uploads ao mesmo tempo. Mais conexões não aumentam a velocidade da internet; teste 1, 2 e 6 para comparar."));
  form->addRow(QStringLiteral("Envios por vez"), _accountWorkers);
  _delayMode = new ComboBox;
  configureCombo(_delayMode, 240);
  _delayMode->addItem(QStringLiteral("Sem intervalo"), QStringLiteral("off"));
  _delayMode->addItem(QStringLiteral("Duração do clipe"), QStringLiteral("clip"));
  _delayMode->addItem(QStringLiteral("Intervalo fixo"), QStringLiteral("fixed"));
  _delayMode->setCurrentIndex(0);
  form->addRow(QStringLiteral("Intervalo"), _delayMode);
  _delaySeconds = new QSpinBox;
  _delaySeconds->setRange(0, 3600);
  _delaySeconds->setSuffix(QStringLiteral(" s"));
  form->addRow(QStringLiteral("Intervalo fixo"), _delaySeconds);
  _delaySeconds->setMaximumWidth(200);
  form->setRowVisible(_delaySeconds, false);
  connect(_delayMode, &QComboBox::currentIndexChanged, parameters, [this, form] {
    form->setRowVisible(_delaySeconds, _delayMode->currentData().toString() == QStringLiteral("fixed"));
  });
  _cleanupAfter = new QCheckBox(QStringLiteral("Liberar mídia local após cada envio"));
  _cleanupAfter->setChecked(true);
  form->addRow(QString(), _cleanupAfter);
  auto* hours = new QWidget;
  auto* hoursLayout = new QHBoxLayout(hours);
  hoursLayout->setContentsMargins(0, 0, 0, 0);
  _activeHours = new QCheckBox(QStringLiteral("Enviar somente entre"));
  _activeHours->setChecked(true);
  _hourStart = new QSpinBox;
  _hourStart->setRange(0, 23);
  _hourStart->setValue(7);
  _hourStart->setSuffix(QStringLiteral("h"));
  _hourEnd = new QSpinBox;
  _hourEnd->setRange(1, 24);
  _hourEnd->setValue(18);
  _hourEnd->setSuffix(QStringLiteral("h"));
  _hourStart->setFixedWidth(100);
  _hourEnd->setFixedWidth(100);
  connect(_activeHours, &QCheckBox::toggled, _hourStart, &QWidget::setEnabled);
  connect(_activeHours, &QCheckBox::toggled, _hourEnd, &QWidget::setEnabled);
  hoursLayout->addWidget(_activeHours);
  hoursLayout->addWidget(_hourStart);
  hoursLayout->addWidget(new QLabel(QStringLiteral("e")));
  hoursLayout->addWidget(_hourEnd);
  hoursLayout->addStretch();
  form->addRow(QStringLiteral("Janela ativa"), hours);
  addSection(QStringLiteral("Ritmo e limites"), card(QStringLiteral("Como a campanha vai executar"), parameters));

  auto* execution = new QWidget;
  auto* executionLayout = new QVBoxLayout(execution);
  executionLayout->setContentsMargins(0, 0, 0, 0);
  executionLayout->setSpacing(10);
  auto* executionHead = new QHBoxLayout;
  auto* executionCopy = new QVBoxLayout;
  executionCopy->setSpacing(3);
  _campaignStage = new QLabel(QStringLiteral("Aguardando"));
  _campaignStage->setObjectName(QStringLiteral("campaignStage"));
  executionCopy->addWidget(_campaignStage);
  _campaignCurrent = quietLabel(QStringLiteral(
      "Configure a campanha; os acontecimentos importantes aparecerão aqui."));
  _campaignCurrent->setWordWrap(true);
  executionCopy->addWidget(_campaignCurrent);
  executionHead->addLayout(executionCopy, 1);
  _campaignStats = quietLabel(QStringLiteral("0 concluídos · 0 falhas"));
  _campaignStats->setObjectName(QStringLiteral("campaignStats"));
  _campaignStats->setAlignment(Qt::AlignRight | Qt::AlignVCenter);
  executionHead->addWidget(_campaignStats);
  executionLayout->addLayout(executionHead);
  _campaignProgress = new QProgressBar;
  _campaignProgress->setObjectName(QStringLiteral("campaignProgress"));
  _campaignProgress->setRange(0, 100);
  _campaignProgress->setValue(0);
  _campaignProgress->setFormat(QStringLiteral("Nenhum envio iniciado"));
  executionLayout->addWidget(_campaignProgress);
  _campaignFeed = new QPlainTextEdit;
  _campaignFeed->setObjectName(QStringLiteral("campaignTimeline"));
  _campaignFeed->setReadOnly(true);
  _campaignFeed->setMaximumBlockCount(500);
  _campaignFeed->setMinimumHeight(190);
  _campaignFeed->setPlaceholderText(QStringLiteral(
      "A linha do tempo mostrará preparação, envios, tentativas e resultados — sem logs técnicos."));
  executionLayout->addWidget(_campaignFeed);
  auto* actions = new QHBoxLayout;
  actions->addStretch();
  _campaignStop = new QPushButton(QStringLiteral("Parar com segurança"));
  _campaignStop->setEnabled(false);
  connect(_campaignStop, &QPushButton::clicked, this, [this] {
    _api.post(QStringLiteral("/api/campaigns/stop"), {}, [this](bool ok, const QJsonDocument&, const QString& error) {
      if (!ok) showError(QStringLiteral("Não foi possível parar"), error);
      else setStatus(QStringLiteral("Parada solicitada; o envio atual será concluído."));
    });
  });
  actions->addWidget(_campaignStop);
  _campaignStart = primaryButton(QStringLiteral("Iniciar campanha"));
  connect(_campaignStart, &QPushButton::clicked, this, &MainWindow::startCampaign);
  actions->addWidget(_campaignStart);
  addSection(QStringLiteral("Acompanhamento"), card(QStringLiteral("Execução"), execution));
  layout->addStretch();
  scroll->setWidget(content);
  bodyLayout->addLayout(actions);

  _campaignDraftSave.setSingleShot(true);
  _campaignDraftSave.setInterval(400);
  connect(&_campaignDraftSave, &QTimer::timeout, this, &MainWindow::saveCampaignDraft);
  const auto draft = QJsonDocument::fromJson(QSettings().value(
      QStringLiteral("campaign/draft")).toByteArray()).object();
  if (!draft.isEmpty()) {
    _campaignDraftLoaded = true;
    for (const auto value : draft.value(QStringLiteral("accounts")).toArray())
      _campaignDraftAccounts.insert(value.toString());
    _campaignTaskSelectionTouched = draft.value(QStringLiteral("tasks_touched")).toBool();
    for (const auto value : draft.value(QStringLiteral("tasks")).toArray())
      _campaignSelectedTaskIds.insert(value.toString());
    const auto restoreCombo = [](QComboBox* combo, const QString& value) {
      const int index = combo->findData(value);
      if (index >= 0) { const QSignalBlocker blocker(combo); combo->setCurrentIndex(index); }
    };
    restoreCombo(_dataset, draft.value(QStringLiteral("dataset")).toString());
    restoreCombo(_contentMode, draft.value(QStringLiteral("content_mode")).toString());
    restoreCombo(_campaignAccountMode, draft.value(QStringLiteral("mode")).toString());
    restoreCombo(_delayMode, draft.value(QStringLiteral("delay_mode")).toString());
    const int workerIndex = _accountWorkers->findData(draft.value(QStringLiteral("account_workers")).toInt(6));
    if (workerIndex >= 0) _accountWorkers->setCurrentIndex(workerIndex);
    _campaignDraftQuantity = qMax(1, draft.value(QStringLiteral("quantity")).toInt(1));
    _targetHours->setValue(draft.value(QStringLiteral("target_hours")).toDouble(8.0));
    _minDuration->setValue(draft.value(QStringLiteral("min_duration")).toInt(1));
    _maxDuration->setValue(draft.value(QStringLiteral("max_duration")).toInt(30));
    _delaySeconds->setValue(draft.value(QStringLiteral("delay_seconds")).toInt());
    _cleanupAfter->setChecked(draft.value(QStringLiteral("cleanup")).toBool(true));
    _activeHours->setChecked(draft.value(QStringLiteral("active_hours")).toBool(true));
    _hourStart->setValue(draft.value(QStringLiteral("hour_start")).toInt(7));
    _hourEnd->setValue(draft.value(QStringLiteral("hour_end")).toInt(18));
  }
  const QString restoredMode = _campaignAccountMode->currentData().toString();
  const bool automatic = restoredMode != QStringLiteral("manual");
  allAccounts->setVisible(!automatic);
  clearAccounts->setVisible(!automatic);
  _campaignAccountCount->setVisible(automatic);
  _campaignDrawAccounts->setVisible(automatic);
  _campaignDrawAccounts->setText(restoredMode == QStringLiteral("rotation")
      ? QStringLiteral("Atualizar rodízio") : restoredMode.startsWith(QStringLiteral("balance_"))
      ? QStringLiteral("Recalcular lista") : QStringLiteral("Sortear"));
  _campaignAccounts->setEnabled(!automatic);
  _campaignBalanceHint->setVisible(restoredMode.startsWith(QStringLiteral("balance_")));
  const auto scheduleDraft = [this] { _campaignDraftSave.start(); };
  connect(_campaignAccounts, &QListWidget::itemChanged, this, scheduleDraft);
  connect(_campaignTasks, &QListWidget::itemChanged, this, scheduleDraft);
  connect(_campaignAccountMode, &QComboBox::currentIndexChanged, this, scheduleDraft);
  connect(_campaignAccountCount, &QSpinBox::valueChanged, this, scheduleDraft);
  connect(_dataset, &QComboBox::currentIndexChanged, this, scheduleDraft);
  connect(_contentMode, &QComboBox::currentIndexChanged, this, scheduleDraft);
  connect(_targetHours, &QDoubleSpinBox::valueChanged, this, scheduleDraft);
  for (auto* spin : {_minDuration, _maxDuration, _delaySeconds, _hourStart, _hourEnd})
    connect(spin, &QSpinBox::valueChanged, this, scheduleDraft);
  connect(_delayMode, &QComboBox::currentIndexChanged, this, scheduleDraft);
  connect(_accountWorkers, &QComboBox::currentIndexChanged, this, scheduleDraft);
  connect(_cleanupAfter, &QCheckBox::toggled, this, scheduleDraft);
  connect(_activeHours, &QCheckBox::toggled, this, scheduleDraft);

  return pageShell(QStringLiteral("Nova campanha"),
                   QStringLiteral("Escolha o conteúdo, calibre a operação e acompanhe cada envio."), body);
}

QWidget* MainWindow::buildAcceleratorPage() {
  auto* body = new QWidget;
  auto* layout = new QHBoxLayout(body);
  _libraryColumns = layout;
  layout->setContentsMargins(0, 0, 0, 0);
  layout->setSpacing(14);

  auto* hero = new QWidget;
  auto* heroLayout = new QVBoxLayout(hero);
  heroLayout->setContentsMargins(0, 0, 0, 0);
  auto* line = new QVBoxLayout;
  _cacheState = new QLabel(QStringLiteral("Aguardando leitura"));
  _cacheState->setObjectName(QStringLiteral("pulseTitle"));
  line->addWidget(_cacheState);
  _cacheNumbers = quietLabel(QStringLiteral("—"));
  line->addWidget(_cacheNumbers);
  heroLayout->addLayout(line);
  _cacheProgress = new QProgressBar;
  _cacheProgress->setRange(0, 100);
  heroLayout->addWidget(_cacheProgress);
  _cacheLastRun = quietLabel(QStringLiteral("Última preparação: aguardando leitura."));
  _cacheLastRun->setWordWrap(true);
  heroLayout->addWidget(_cacheLastRun);
  auto* libraryContext = card(QStringLiteral("MÍDIA PREPARADA"), hero);
  libraryContext->setObjectName(QStringLiteral("libraryContext"));
  libraryContext->setMinimumWidth(280);
  _cacheState->setWordWrap(true);
  heroLayout->addSpacing(24);
  heroLayout->addWidget(quietLabel(QStringLiteral("Prepare uma vez, use nas campanhas")));
  heroLayout->addWidget(quietLabel(QStringLiteral("Os vídeos e sensores ficam nesta biblioteca. A preparação respeita o espaço livre reservado no disco.")));
  heroLayout->addStretch();

  auto* config = new QWidget;
  auto* form = new QFormLayout(config);
  configureForm(form);
  form->setContentsMargins(0, 0, 0, 0);
  _cacheProvider = new ComboBox;
  configureCombo(_cacheProvider, 240);
  _cacheProvider->addItem(QStringLiteral("Ego4D"), QStringLiteral("ego4d"));
  _cacheProvider->addItem(QStringLiteral("HoloAssist"), QStringLiteral("holoassist"));
  connect(_cacheProvider, qOverload<int>(&QComboBox::currentIndexChanged), this, [this, form] {
    _cacheTask->blockSignals(true);
    _cacheTask->clear();
    _cacheTask->blockSignals(false);
    const bool ego = _cacheProvider->currentData().toString() == QStringLiteral("ego4d");
    if (_cacheTaskLabel) {
      _cacheTaskLabel->setText(ego ? QStringLiteral("Priorizar categoria")
                                   : QStringLiteral("Tarefa a preparar"));
    }
    if (_cacheBudget) form->setRowVisible(_cacheBudget, ego);
    if (_cacheBudgetHelp) form->setRowVisible(_cacheBudgetHelp, ego);
    if (_cacheProviderHelp) _cacheProviderHelp->setText(ego
        ? QStringLiteral("Ego4D: prepara vídeos e sensores antecipadamente. A campanha usa primeiro os arquivos prontos.")
        : QStringLiteral("HoloAssist: prepara os clipes da tarefa escolhida para uso posterior na campanha."));
    if (_cacheTaskHelp) _cacheTaskHelp->setText(ego
        ? QStringLiteral("O cache alterna clipes entre categorias. A escolhida entra primeiro em cada rodada; o limite em GB é compartilhado.")
        : QStringLiteral("Somente a tarefa escolhida entra nesta preparação."));
    if (_cacheDiskHelp) _cacheDiskHelp->setText(ego
        ? QStringLiteral("O QMoney preserva o espaço livre indicado e ajusta o limite ao disco disponível.")
        : QStringLiteral("A preparação para quando o espaço livre cair abaixo da reserva."));
    if (_cacheLimitHelp) _cacheLimitHelp->setText(ego
        ? QStringLiteral("Todos usa o espaço escolhido; um número menor limita somente esta execução.")
        : QStringLiteral("Todos prepara todos os clipes disponíveis da tarefa escolhida."));
    if (_cacheStart) {
      const bool off = ego && _cacheBudget && _cacheBudget->value() == 0;
      _cacheStart->setText(off ? QStringLiteral("Desativar pré-cache")
                               : QStringLiteral("Preparar cache"));
    }
    loadAccelerator();
  });
  form->addRow(QStringLiteral("Provedor"), _cacheProvider);
  _cacheProviderHelp = quietLabel(QStringLiteral(
      "Ego4D: prepara vídeos e sensores antecipadamente. A campanha usa primeiro os arquivos prontos."));
  _cacheProviderHelp->setWordWrap(true);
  form->addRow(QString(), _cacheProviderHelp);
  _cacheTask = new ComboBox;
  configureCombo(_cacheTask, 240);
  connect(_cacheTask, qOverload<int>(&QComboBox::currentIndexChanged), this, [this] {
    loadAccelerator();
  });
  _cacheTaskLabel = new QLabel(QStringLiteral("Priorizar categoria"));
  form->addRow(_cacheTaskLabel, _cacheTask);
  _cacheTaskHelp = quietLabel(QStringLiteral(
      "O cache alterna clipes entre categorias. A escolhida entra primeiro em cada rodada; o limite em GB é compartilhado."));
  _cacheTaskHelp->setWordWrap(true);
  form->addRow(QString(), _cacheTaskHelp);
  _cacheBudgetLabel = new QLabel(QStringLiteral("Espaço para o cache"));
  _cacheBudget = new QSpinBox;
  _cacheBudget->setMaximumWidth(220);
  _cacheBudget->setRange(0, 2147483647);
  _cacheBudget->setKeyboardTracking(false);
  _cacheBudget->setSpecialValueText(QStringLiteral("0 GB · desativado"));
  _cacheBudget->setValue(400);
  _cacheBudget->setSuffix(QStringLiteral(" GB"));
  connect(_cacheBudget, qOverload<int>(&QSpinBox::valueChanged), this, [this] {
    const bool off = _cacheBudget->value() == 0;
    _cacheBudget->setSuffix(off ? QString() : QStringLiteral(" GB"));
    if (_cacheStart && _cacheProvider
        && _cacheProvider->currentData().toString() == QStringLiteral("ego4d")) {
      _cacheStart->setText(off ? QStringLiteral("Desativar pré-cache")
                               : QStringLiteral("Preparar cache"));
    }
    loadAccelerator();
  });
  form->addRow(_cacheBudgetLabel, _cacheBudget);
  _cacheBudgetHelp = quietLabel(QStringLiteral(
      "Limite total para arquivos Ego4D neste computador. 0 GB desativa a preparação antecipada; a campanha ainda pode buscar vídeos quando precisar."));
  _cacheBudgetHelp->setWordWrap(true);
  form->addRow(QString(), _cacheBudgetHelp);
  const bool egoInitiallySelected = _cacheProvider->currentData().toString() == QStringLiteral("ego4d");
  form->setRowVisible(_cacheBudget, egoInitiallySelected);
  form->setRowVisible(_cacheBudgetHelp, egoInitiallySelected);
  _cacheLimit = new QSpinBox;
  _cacheLimit->setMaximumWidth(220);
  _cacheLimit->setRange(0, 1000);
  _cacheLimit->setSpecialValueText(QStringLiteral("Todos"));
  _cacheLimit->setToolTip(QStringLiteral("0 prepara todos os clipes que couberem no espaço escolhido. Outro valor limita apenas esta execução."));
  connect(_cacheLimit, qOverload<int>(&QSpinBox::valueChanged), this, [this] {
    loadAccelerator();
  });
  form->addRow(QStringLiteral("Clipes nesta execução"), _cacheLimit);
  _cacheLimitHelp = quietLabel(QStringLiteral(
      "Todos usa o espaço escolhido; um número menor limita somente esta execução."));
  _cacheLimitHelp->setWordWrap(true);
  form->addRow(QString(), _cacheLimitHelp);
  _cacheReserve = new QSpinBox;
  _cacheReserve->setMaximumWidth(220);
  _cacheReserve->setRange(5, 1000);
  _cacheReserve->setValue(50);
  _cacheReserve->setSuffix(QStringLiteral(" GiB livres"));
  _cacheReserve->setKeyboardTracking(false);
  connect(_cacheReserve, qOverload<int>(&QSpinBox::valueChanged), this, [this] {
    loadAccelerator();
  });
  form->addRow(QStringLiteral("Manter livre no disco"), _cacheReserve);
  _cacheDiskHelp = quietLabel(QStringLiteral(
      "O QMoney preserva o espaço livre indicado e ajusta o limite ao disco disponível."));
  _cacheDiskHelp->setWordWrap(true);
  form->addRow(QString(), _cacheDiskHelp);
  auto* actions = new QWidget;
  auto* actionLayout = new QHBoxLayout(actions);
  actionLayout->setContentsMargins(0, 0, 0, 0);
  auto* cleanupProvider = new ComboBox;
  cleanupProvider->addItem(QStringLiteral("Ego4D"), QStringLiteral("ego4d"));
  cleanupProvider->addItem(QStringLiteral("HoloAssist"), QStringLiteral("holoassist"));
  cleanupProvider->addItem(QStringLiteral("Ambos"), QStringLiteral("all"));
  cleanupProvider->setToolTip(QStringLiteral("Escolha o cache a apagar"));
  actionLayout->addWidget(cleanupProvider);
  auto* cleanup = new QPushButton(QStringLiteral("Apagar cache"));
  connect(cleanup, &QPushButton::clicked, this, [this, cleanupProvider] {
    const QString provider = cleanupProvider->currentData().toString();
    const QString name = cleanupProvider->currentText();
    if (QMessageBox::question(this, QStringLiteral("Limpar mídia"),
          QStringLiteral("Apagar o cache de %1? Catálogos, contas e histórico serão preservados.").arg(name))
        != QMessageBox::Yes) return;
    _api.post(QStringLiteral("/api/storage/cleanup"), {{QStringLiteral("provider"), provider}}, [this](bool ok, const QJsonDocument& doc, const QString& error) {
      if (!ok) return showError(QStringLiteral("Falha na limpeza"), error);
      const auto result = doc.object();
      setStatus(QStringLiteral("%1 arquivo(s) removido(s).").arg(result.value(QStringLiteral("files")).toInt()));
      loadAccelerator();
    });
  });
  actionLayout->addWidget(cleanup);
  actionLayout->addStretch();
  _cacheStop = new QPushButton(QStringLiteral("Parar com segurança"));
  _cacheStop->setEnabled(false);
  connect(_cacheStop, &QPushButton::clicked, this, [this] {
    _api.post(QStringLiteral("/api/holo-cache/stop"), {}, [this](bool ok, const QJsonDocument&, const QString& error) {
      if (!ok) showError(QStringLiteral("Falha ao parar"), error);
      else setStatus(QStringLiteral("O acelerador parará depois do clipe atual."));
    });
  });
  actionLayout->addWidget(_cacheStop);
  _cacheStart = primaryButton(QStringLiteral("Preparar cache"));
  connect(_cacheStart, &QPushButton::clicked, this, &MainWindow::startAccelerator);
  actionLayout->addWidget(_cacheStart);
  auto* preparation = new QWidget;
  auto* preparationLayout = new QVBoxLayout(preparation);
  preparationLayout->setContentsMargins(0, 0, 0, 0);
  preparationLayout->setSpacing(20);
  preparationLayout->addWidget(config);
  preparationLayout->addWidget(actions);
  layout->addWidget(card(QStringLiteral("Preparar conteúdo"), preparation), 2);
  layout->addWidget(libraryContext, 1);
  auto* scroll = new QScrollArea;
  scroll->setWidgetResizable(true);
  scroll->setFrameShape(QFrame::NoFrame);
  scroll->setWidget(body);

  return pageShell(QStringLiteral("Biblioteca"),
                   QStringLiteral("Prepare seu conteúdo e acompanhe a mídia disponível para as campanhas."), scroll);
}

QWidget* MainWindow::buildAccountsPage() {
  auto* body = new QWidget;
  auto* layout = new QVBoxLayout(body);
  layout->setContentsMargins(0, 0, 0, 0);
  layout->setSpacing(14);
  auto* tabs = new QTabWidget;
  tabs->setDocumentMode(true);
  layout->addWidget(tabs);
  auto addTab = [tabs](const QString& title, QWidget* widget) {
    auto* content = new QWidget;
    auto* contentLayout = new QVBoxLayout(content);
    contentLayout->setContentsMargins(0, 0, 0, 0);
    contentLayout->addWidget(widget);
    contentLayout->addStretch();
    auto* scroll = new QScrollArea;
    scroll->setWidgetResizable(true);
    scroll->setFrameShape(QFrame::NoFrame);
    scroll->setWidget(content);
    tabs->addTab(scroll, title);
  };

  auto* formBody = new QWidget;
  auto* form = new QFormLayout(formBody);
  configureForm(form);
  form->setContentsMargins(0, 0, 0, 0);
  _accountEmail = new QLineEdit;
  _accountEmail->setPlaceholderText(QStringLiteral("email da conta"));
  form->addRow(QStringLiteral("Email"), _accountEmail);
  _accountPassword = new QLineEdit;
  _accountPassword->setEchoMode(QLineEdit::Password);
  _accountPassword->setPlaceholderText(QStringLiteral("senha do Minute / Crowtado"));
  form->addRow(QStringLiteral("Senha"), _accountPassword);
  auto* actions = new QWidget;
  auto* actionLayout = new QHBoxLayout(actions);
  actionLayout->setContentsMargins(0, 0, 0, 0);
  actionLayout->addStretch();
  _accountAdd = new QPushButton(QStringLiteral("Adicionar existente"));
  connect(_accountAdd, &QPushButton::clicked, this, [this] { addAccount(false); });
  actionLayout->addWidget(_accountAdd);
  _accountRegister = primaryButton(QStringLiteral("Registrar nova"));
  connect(_accountRegister, &QPushButton::clicked, this, [this] { addAccount(true); });
  actionLayout->addWidget(_accountRegister);
  form->addRow(QString(), actions);
  addTab(QStringLiteral("Conectar conta"), card(QStringLiteral("Conectar conta"), formBody));

  // --- Criador de contas ---------------------------------------------------
  auto* bulkBody = new QWidget;
  auto* bulkForm = new QFormLayout(bulkBody);
  configureForm(bulkForm);
  bulkForm->setContentsMargins(0, 0, 0, 0);
  _bulkRegisterDomain = new ComboBox;
  configureCombo(_bulkRegisterDomain);
  _bulkRegisterDomain->setEditable(false);
  _bulkRegisterDomain->addItem(QStringLiteral("carregando domínios…"));
  connect(_bulkRegisterDomain, qOverload<int>(&QComboBox::currentIndexChanged),
          this, [this] { checkBulkRegisterDomain(); });
  bulkForm->addRow(QStringLiteral("Domínio"), _bulkRegisterDomain);
  _bulkRegisterCount = new QSpinBox;
  _bulkRegisterCount->setRange(1, 50);
  _bulkRegisterCount->setValue(5);
  _bulkRegisterCount->setMaximumWidth(200);
  bulkForm->addRow(QStringLiteral("Quantidade"), _bulkRegisterCount);
  auto* bulkActions = new QWidget;
  auto* bulkActionLayout = new QHBoxLayout(bulkActions);
  bulkActionLayout->setContentsMargins(0, 0, 0, 0);
  bulkActionLayout->addStretch();
  auto* bulkRefreshDomains = new QPushButton(QStringLiteral("Atualizar domínios"));
  connect(bulkRefreshDomains, &QPushButton::clicked, this, &MainWindow::loadBulkRegisterDomains);
  bulkActionLayout->addWidget(bulkRefreshDomains);
  _bulkRegisterStart = primaryButton(QStringLiteral("Criar contas"));
  connect(_bulkRegisterStart, &QPushButton::clicked, this, &MainWindow::startBulkRegister);
  bulkActionLayout->addWidget(_bulkRegisterStart);
  bulkForm->addRow(QString(), bulkActions);
  _bulkRegisterStatus = quietLabel(QStringLiteral(
      "Fluxo completo: Crowtado → demografia → Minute → vínculo. "
      "Cada conta leva alguns minutos (Turnstile + verificação de email)."));
  _bulkRegisterStatus->setWordWrap(true);
  bulkForm->addRow(QString(), _bulkRegisterStatus);
  _bulkRegisterWebmail = new QLabel;
  _bulkRegisterWebmail->setOpenExternalLinks(false);
  _bulkRegisterWebmail->setTextFormat(Qt::RichText);
  _bulkRegisterWebmail->hide();
  connect(_bulkRegisterWebmail, &QLabel::linkActivated, this, &MainWindow::openWebmail);
  bulkForm->addRow(QString(), _bulkRegisterWebmail);
  _bulkRegisterProgress = new QProgressBar;
  _bulkRegisterProgress->setObjectName(QStringLiteral("bulkRegisterProgress"));
  _bulkRegisterProgress->setRange(0, 100);
  _bulkRegisterProgress->setValue(0);
  _bulkRegisterProgress->setTextVisible(true);
  _bulkRegisterProgress->setFormat(QStringLiteral("%v/%m"));
  bulkForm->addRow(QStringLiteral("Progresso"), _bulkRegisterProgress);
  _bulkRegisterTable = new QTableWidget(0, 6);
  configureTable(_bulkRegisterTable, QStringLiteral("Acompanhe as novas contas"),
                 QStringLiteral("Ao iniciar a criação, o resultado de cada conta aparece aqui."));
  _bulkRegisterTable->setMinimumHeight(160);
  _bulkRegisterTable->setHorizontalHeaderLabels({
      QStringLiteral("Email"), QStringLiteral("Nome"), QStringLiteral("Sobrenome"),
      QStringLiteral("Gênero"), QStringLiteral("Resultado"), QStringLiteral("Ação"),
  });
  _bulkRegisterTable->horizontalHeader()->setSectionResizeMode(0, QHeaderView::Stretch);
  _bulkRegisterTable->horizontalHeader()->setSectionResizeMode(1, QHeaderView::Interactive);
  _bulkRegisterTable->horizontalHeader()->setSectionResizeMode(2, QHeaderView::Interactive);
  _bulkRegisterTable->horizontalHeader()->setSectionResizeMode(3, QHeaderView::Interactive);
  _bulkRegisterTable->horizontalHeader()->setSectionResizeMode(4, QHeaderView::Interactive);
  _bulkRegisterTable->horizontalHeader()->setSectionResizeMode(5, QHeaderView::Fixed);
  _bulkRegisterTable->setColumnWidth(1, 140);
  _bulkRegisterTable->setColumnWidth(2, 140);
  _bulkRegisterTable->setColumnWidth(3, 90);
  _bulkRegisterTable->setColumnWidth(4, 180);
  _bulkRegisterTable->setColumnWidth(5, 108);
  _bulkRegisterTable->setSelectionBehavior(QAbstractItemView::SelectRows);
  _bulkRegisterTable->setSelectionMode(QAbstractItemView::NoSelection);
  bulkForm->addRow(quietLabel(QStringLiteral("Resultados")));
  bulkForm->addRow(_bulkRegisterTable);
  _bulkRegisterPoll.setInterval(900);
  connect(&_bulkRegisterPoll, &QTimer::timeout, this, &MainWindow::pollBulkRegister);
  _bulkRegisterStart->setEnabled(false);
  addTab(QStringLiteral("Criar contas"), card(QStringLiteral("Criador de contas"), bulkBody));

  _accountsTable = new QTableWidget(0, 4);
  configureTable(_accountsTable, QStringLiteral("Tudo começa pelas suas contas"),
                 QStringLiteral("Adicione uma conta ou importe suas credenciais para começar."));
  _accountsTable->setMinimumHeight(230);
  _accountsTable->setHorizontalHeaderLabels(
      {QStringLiteral("Conta"), QStringLiteral("Organização"), QStringLiteral("Última verificação"), QStringLiteral("Ações")});
  _accountsTable->horizontalHeader()->setSectionResizeMode(0, QHeaderView::Stretch);
  _accountsTable->horizontalHeader()->setSectionResizeMode(1, QHeaderView::Interactive);
  _accountsTable->horizontalHeader()->setSectionResizeMode(2, QHeaderView::Interactive);
  _accountsTable->horizontalHeader()->setSectionResizeMode(3, QHeaderView::Fixed);
  _accountsTable->setColumnWidth(1, 150);
  _accountsTable->setColumnWidth(2, 175);
  _accountsTable->setColumnWidth(3, 340);
  _accountsTable->setSelectionBehavior(QAbstractItemView::SelectRows);
  _accountsTable->setSelectionMode(QAbstractItemView::ExtendedSelection);

  auto* accountsBody = new QWidget;
  auto* accountsLayout = new QVBoxLayout(accountsBody);
  accountsLayout->setContentsMargins(0, 0, 0, 0);
  accountsLayout->setSpacing(10);
  auto* tableActions = new QHBoxLayout;
  tableActions->addWidget(quietLabel(
      QStringLiteral("Valide tokens e disponibilidade de todas as identidades em uma única leitura.")), 1);
  _accountsCheckAll = primaryButton(QStringLiteral("Verificar todas"));
  connect(_accountsCheckAll, &QPushButton::clicked, this, &MainWindow::checkAllAccounts);
  tableActions->addWidget(_accountsCheckAll);
  accountsLayout->addLayout(tableActions);
  auto* transferActions = new QHBoxLayout;
  _accountsImport = new QPushButton(QStringLiteral("Importar JSON"));
  _accountsExport = new QPushButton(QStringLiteral("Exportar todas"));
  _accountsExportSelected = new QPushButton(QStringLiteral("Exportar selecionadas"));
  connect(_accountsImport, &QPushButton::clicked, this, &MainWindow::importAccounts);
  connect(_accountsExport, &QPushButton::clicked, this, [this] { exportAccounts(false); });
  connect(_accountsExportSelected, &QPushButton::clicked, this, [this] { exportAccounts(true); });
  transferActions->addWidget(_accountsImport);
  transferActions->addWidget(_accountsExport);
  transferActions->addWidget(_accountsExportSelected);
  auto* exportBanned = new QPushButton(QStringLiteral("Exportar banidas"));
  transferActions->addWidget(exportBanned);
  connect(exportBanned, &QPushButton::clicked, this, [this] {
    const QString path = QFileDialog::getSaveFileName(this, QStringLiteral("Salvar contas banidas"),
        QStringLiteral("banned_accounts.json"), QStringLiteral("JSON (*.json)"));
    if (path.isEmpty()) return;
    _api.get(QStringLiteral("/api/accounts/banned"), [this, path](bool ok, const QJsonDocument& doc, const QString& error) {
      if (!ok) return showError(QStringLiteral("Falha ao exportar banidas"), error);
      QSaveFile output(path);
      if (!output.open(QIODevice::WriteOnly) || output.write(doc.toJson(QJsonDocument::Indented)) < 0 || !output.commit())
        return showError(QStringLiteral("Falha ao exportar banidas"), QStringLiteral("Não foi possível salvar o arquivo."));
      setStatus(QStringLiteral("Registro de contas banidas salvo."));
    });
  });
  transferActions->addStretch();
  accountsLayout->addLayout(transferActions);
  auto* migrationActions = new QHBoxLayout;
  _accountsMigrate = new QPushButton(QStringLiteral("Atualizar organização Crowtado"));
  _accountsMigrate->setToolTip(QStringLiteral("Atualiza todas as contas Crowtado cadastradas. Contas Claru são ignoradas."));
  _migrationReport = new QPushButton(QStringLiteral("Ver relatório"));
  _migrationReport->setEnabled(false);
  connect(_accountsMigrate, &QPushButton::clicked, this, &MainWindow::startOrgMigration);
  connect(_migrationReport, &QPushButton::clicked, this, &MainWindow::showOrgMigrationReport);
  migrationActions->addWidget(_accountsMigrate);
  migrationActions->addWidget(_migrationReport);
  migrationActions->addStretch();
  accountsLayout->addLayout(migrationActions);
  _migrationStatus = quietLabel(QStringLiteral("Crowtado: organização nova obrigatória. Claru: mantida sem troca de código."));
  _migrationStatus->setWordWrap(true);
  accountsLayout->addWidget(_migrationStatus);
  _orgMigrationPoll.setInterval(1200);
  connect(&_orgMigrationPoll, &QTimer::timeout, this, &MainWindow::pollOrgMigration);
  accountsLayout->addWidget(quietLabel(QStringLiteral(
      "Backup JSON com credenciais de acesso. Guarde em local seguro. Use Ctrl ou Shift para selecionar contas.")));
  accountsLayout->addWidget(_accountsTable, 1);
  auto* accountsScroll = new QScrollArea;
  accountsScroll->setWidgetResizable(true);
  accountsScroll->setFrameShape(QFrame::NoFrame);
  accountsScroll->setWidget(card(QStringLiteral("Contas cadastradas"), accountsBody));
  tabs->insertTab(0, accountsScroll, QStringLiteral("Contas cadastradas"));
  tabs->setCurrentIndex(0);
  return pageShell(QStringLiteral("Contas"),
                   QStringLiteral("Gerencie as identidades usadas no Minute e valide cada acesso."), body);
}

QWidget* MainWindow::buildBalancesPage() {
  auto* body = new QWidget;
  auto* layout = new QVBoxLayout(body);
  layout->setContentsMargins(0, 0, 0, 0);
  layout->setSpacing(14);
  auto* header = new QWidget;
  auto* headerContainer = new QVBoxLayout(header);
  headerContainer->setContentsMargins(0, 0, 0, 0);
  headerContainer->setSpacing(14);
  auto* headerLayout = new QHBoxLayout;
  headerLayout->setContentsMargins(0, 0, 0, 0);
  auto* identityCopy = new QWidget;
  auto* identityLayout = new QVBoxLayout(identityCopy);
  identityLayout->setContentsMargins(0, 0, 0, 0);
  identityLayout->setSpacing(3);
  _balancesState = quietLabel(QStringLiteral("Aguardando leitura…"));
  identityLayout->addWidget(_balancesState);
  auto* identityHelp = quietLabel(QStringLiteral(
      "O mesmo e-mail usa o Minute nas campanhas e o Crowtado nos saldos. "
      "Se as senhas forem diferentes, conecte o acesso Crowtado na linha abaixo."));
  identityHelp->setWordWrap(true);
  identityLayout->addWidget(identityHelp);
  headerContainer->addWidget(identityCopy);
  headerContainer->addLayout(headerLayout);
  _balancesRefresh = primaryButton(QStringLiteral("Atualizar todos"));
  connect(_balancesRefresh, &QPushButton::clicked, this, [this] {
    _balancesRefresh->setEnabled(false);
    _api.post(QStringLiteral("/api/balances/refresh"), {}, [this](bool ok, const QJsonDocument&, const QString& error) {
      if (!ok) {
        _balancesRefresh->setEnabled(true);
        showError(QStringLiteral("Não foi possível atualizar"), error);
      } else {
        _balancePoll.start();
        setStatus(QStringLiteral("Consulta de saldos iniciada."));
      }
    });
  });
  headerLayout->addWidget(_balancesRefresh);
  _balancesRefreshNeeded = new QPushButton(QStringLiteral("Atualizar pendentes"));
  _balancesRefreshNeeded->setEnabled(false);
  _balancesRefreshNeeded->setToolTip(QStringLiteral(
      "Consulta só contas Crowtado conectadas sem saldo confirmado, com erro ou com leitura de mais de 24 horas."));
  connect(_balancesRefreshNeeded, &QPushButton::clicked, this, [this] {
    const auto emails = _balancesSnapshot.value(QStringLiteral("refresh_needed")).toArray();
    if (emails.isEmpty()) return;
    _balancesRefreshNeeded->setEnabled(false);
    _api.post(QStringLiteral("/api/balances/refresh"),
              {{QStringLiteral("emails"), emails}},
              [this, count = emails.size()](bool ok, const QJsonDocument&, const QString& error) {
      if (!ok) {
        loadBalances();
        return showError(QStringLiteral("Não foi possível atualizar pendentes"), error);
      }
      _balancePoll.start();
      setStatus(QStringLiteral("Consultando %1 conta(s) pendente(s)…").arg(count));
    });
  });
  _balancesWithdrawAll = new QPushButton(QStringLiteral("Sacar tudo"));
  _balancesWithdrawAll->setEnabled(false);
  _balancesWithdrawAll->setToolTip(QStringLiteral(
      "Solicita saques para todas as contas Crowtado elegíveis, inclusive as ocultas pelo filtro da tabela."));
  connect(_balancesWithdrawAll, &QPushButton::clicked, this, [this] {
    const int eligible = _balancesWithdrawAll->property("eligibleCount").toInt();
    QJsonObject request;
    if (!confirmWithdrawal(this, eligible, request)) return;
    _balancesWithdrawAll->setEnabled(false);
    _api.post(QStringLiteral("/api/balances/withdraw-all"), request,
              [this](bool ok, const QJsonDocument& doc, const QString& error) {
      if (!ok) {
        loadBalances();
        return showError(QStringLiteral("Saque em lote não iniciado"), error);
      }
      _bulkWithdrawAwaitingResult = true;
      _balancePoll.start();
      setStatus(QStringLiteral("Solicitando saques para %1 conta(s)…")
          .arg(doc.object().value(QStringLiteral("total")).toInt()));
    });
  });
  headerLayout->addWidget(_balancesWithdrawAll);
  _balancesWiseCleanup = new QPushButton(QStringLiteral("Concluir limpeza Wise"));
  _balancesWiseCleanup->setVisible(false);
  _balancesWiseCleanup->setToolTip(QStringLiteral("Remove a Wise e restaura Dots na conta pendente, sem solicitar saque."));
  connect(_balancesWiseCleanup, &QPushButton::clicked, this, [this] {
    _balancesWiseCleanup->setEnabled(false);
    _api.post(QStringLiteral("/api/balances/wise-cleanup"), {},
              [this](bool ok, const QJsonDocument& doc, const QString& error) {
      _balancesWiseCleanup->setEnabled(true);
      loadBalances();
      if (!ok) return showError(QStringLiteral("Limpeza Wise pendente"), error);
      QMessageBox::information(this, QStringLiteral("Limpeza concluída"),
                               doc.object().value(QStringLiteral("message")).toString());
    });
  });
  headerLayout->addWidget(_balancesWiseCleanup);
  _balancesPayoutMethod = new QPushButton(QStringLiteral("Método de saque"));
  _balancesPayoutMethod->setEnabled(false);
  _balancesPayoutMethod->setToolTip(QStringLiteral("Configura Dots ou PayPal. Para Wise, escolha o método ao solicitar saque."));
  connect(_balancesPayoutMethod, &QPushButton::clicked, this, [this] {
    QDialog dialog(this);
    dialog.setWindowTitle(QStringLiteral("Método de saque Crowtado"));
    dialog.resize(620, 400);
    auto* layout = new QVBoxLayout(&dialog);
    layout->setContentsMargins(28, 24, 28, 24);
    layout->setSpacing(18);
    auto* title = new QLabel(QStringLiteral("Destino dos seus saques"), &dialog);
    title->setObjectName(QStringLiteral("reviewTitle"));
    layout->addWidget(title);
    auto* help = new QLabel(QStringLiteral(
        "A configuração será aplicada a todas as contas Crowtado conectadas. "
        "Para Wise, escolha o método ao solicitar saque; o vínculo será feito uma conta por vez. "
        "Contas Claru não serão alteradas. Os dados de destino serão enviados à Crowtado."));
    help->setWordWrap(true);
    layout->addWidget(help);
    auto* form = new QFormLayout;
    auto* method = new ComboBox(&dialog);
    method->addItem(QStringLiteral("Dots"), QStringLiteral("dots"));
    method->addItem(QStringLiteral("PayPal"), QStringLiteral("paypal"));
    auto* legalName = new QLineEdit;
    legalName->setPlaceholderText(QStringLiteral("Nome legal do beneficiário"));
    auto* destination = new QLineEdit;
    destination->setPlaceholderText(QStringLiteral("E-mail da conta PayPal"));
    form->addRow(QStringLiteral("Método"), method);
    form->addRow(QStringLiteral("Nome legal"), legalName);
    form->addRow(QStringLiteral("E-mail do destino"), destination);
    layout->addLayout(form);
    auto toggle = [=] {
      const bool manual = method->currentData().toString() != QStringLiteral("dots");
      legalName->setEnabled(manual);
      destination->setEnabled(manual);
    };
    connect(method, QOverload<int>::of(&QComboBox::currentIndexChanged), &dialog, [=](int) { toggle(); });
    toggle();
    auto* buttons = new QDialogButtonBox(QDialogButtonBox::Ok | QDialogButtonBox::Cancel);
    buttons->button(QDialogButtonBox::Ok)->setText(QStringLiteral("Aplicar em todas"));
    connect(buttons, &QDialogButtonBox::accepted, &dialog, &QDialog::accept);
    connect(buttons, &QDialogButtonBox::rejected, &dialog, &QDialog::reject);
    layout->addWidget(buttons);
    if (dialog.exec() != QDialog::Accepted) return;
    const QString selected = method->currentData().toString();
    const QString name = selected == QStringLiteral("dots") ? QString() : legalName->text().trimmed();
    const QString email = selected == QStringLiteral("dots") ? QString() : destination->text().trimmed();
    if (selected != QStringLiteral("dots") &&
        (name.size() < 2 || !QRegularExpression(QStringLiteral("^[^\\s@]+@[^\\s@]+\\.[^\\s@]+$"))
                                  .match(email).hasMatch())) {
      showError(QStringLiteral("Dados incompletos"), QStringLiteral("Informe o nome legal e um e-mail válido."));
      return;
    }
    _balancesPayoutMethod->setEnabled(false);
    _api.post(QStringLiteral("/api/balances/payout-methods/apply-all"),
              {{QStringLiteral("method"), selected}, {QStringLiteral("legal_name"), name},
               {QStringLiteral("destination_email"), email}},
              [this](bool ok, const QJsonDocument& doc, const QString& error) {
      if (!ok) { loadBalances(); return showError(QStringLiteral("Configuração não iniciada"), error); }
      _payoutMethodAwaitingResult = true;
      _balancePoll.start();
      setStatus(QStringLiteral("Configurando método de saque em %1 conta(s)…")
          .arg(doc.object().value(QStringLiteral("total")).toInt()));
    });
  });
  headerLayout->addWidget(_balancesPayoutMethod);
  _balancesWithdrawHistory = new QPushButton(QStringLiteral("Último lote"));
  _balancesWithdrawHistory->setEnabled(false);
  _balancesWithdrawHistory->setToolTip(QStringLiteral(
      "Mostra o resultado por conta do último lote de solicitações, mesmo após reiniciar o aplicativo."));
  connect(_balancesWithdrawHistory, &QPushButton::clicked, this, [this] {
    showWithdrawalReport(_lastWithdrawBulk);
  });
  headerLayout->addWidget(_balancesWithdrawHistory);
  _balancesExport = new QPushButton(QStringLiteral("Exportar CSV"));
  _balancesExport->setEnabled(false);
  _balancesExport->setToolTip(QStringLiteral("Salva os saldos exibidos em uma planilha CSV."));
  connect(_balancesExport, &QPushButton::clicked, this, [this] {
    QString path = QFileDialog::getSaveFileName(this, QStringLiteral("Exportar saldos"),
        QStringLiteral("QMoney-saldos-%1.csv")
            .arg(QDateTime::currentDateTime().toString(QStringLiteral("yyyyMMdd-HHmmss"))),
        QStringLiteral("CSV (*.csv)"));
    if (path.isEmpty()) return;
    if (!path.endsWith(QStringLiteral(".csv"), Qt::CaseInsensitive)) path += QStringLiteral(".csv");
    const auto balances = _balancesSnapshot.value(QStringLiteral("balances")).toObject();
    const auto kinds = _balancesSnapshot.value(QStringLiteral("account_kinds")).toObject();
    const auto csvCell = [](QString value) {
      const QString trimmed = value.trimmed();
      if (!trimmed.isEmpty() && QStringLiteral("=+-@").contains(trimmed.front()))
        value.prepend(QLatin1Char('\''));
      value.replace(QLatin1Char('"'), QStringLiteral("\"\""));
      return QStringLiteral("\"") + value + QStringLiteral("\"");
    };
    QStringList lines{QStringLiteral("Conta;Tipo;Disponível (USD);Pendente (USD);Atualizado;Situação")};
    for (int row = 0; row < _balancesTable->rowCount(); ++row) {
      if (_balancesTable->isRowHidden(row)) continue;
      const auto* emailItem = _balancesTable->item(row, 0);
      if (!emailItem) continue;
      const QString email = emailItem->text();
      const auto balance = balances.value(email).toObject();
      const bool isClaru = kinds.value(email).toString() == QStringLiteral("claru");
      const auto money = [](const QJsonValue& value) {
        return value.isDouble()
            ? QString::number(value.toDouble() / 100.0, 'f', 2).replace(QLatin1Char('.'), QLatin1Char(','))
            : QString();
      };
      const QString state = isClaru ? QStringLiteral("Não se aplica")
          : !balance.value(QStringLiteral("error")).toString().isEmpty()
              || balance.value(QStringLiteral("stale")).toBool()
          ? QStringLiteral("Consulta inconclusiva; valores salvos")
          : balance.value(QStringLiteral("availableCents")).isDouble()
          ? QStringLiteral("Confirmado") : QStringLiteral("Ainda não consultado");
      lines << QStringList{
          csvCell(email), csvCell(isClaru ? QStringLiteral("Claru") : QStringLiteral("Crowtado")),
          csvCell(isClaru ? QString() : money(balance.value(QStringLiteral("availableCents")))),
          csvCell(isClaru ? QString() : money(balance.value(QStringLiteral("pendingCents")))),
          csvCell(balance.value(QStringLiteral("updated_at")).toString()), csvCell(state),
      }.join(QLatin1Char(';'));
    }
    QSaveFile output(path);
    const QByteArray bytes = QByteArray::fromHex("efbbbf") + lines.join(QLatin1Char('\n')).toUtf8();
    if (!output.open(QIODevice::WriteOnly) || output.write(bytes) != bytes.size() || !output.commit()) {
      showError(QStringLiteral("Saldos não exportados"), QStringLiteral("Não foi possível salvar o CSV."));
      return;
    }
    setStatus(QStringLiteral("Saldos exportados para %1.").arg(QDir::toNativeSeparators(path)));
  });
  headerLayout->addWidget(_balancesExport);
  layout->addWidget(card(QStringLiteral("Disponibilidade"), header));

  auto* filters = new QWidget;
  auto* filterRows = new QVBoxLayout(filters);
  filterRows->setContentsMargins(0, 0, 0, 0);
  filterRows->setSpacing(10);
  auto* filtersLayout = new QHBoxLayout;
  filtersLayout->setContentsMargins(0, 0, 0, 0);
  _balancesSearch = new QLineEdit;
  _balancesSearch->setPlaceholderText(QStringLiteral("Buscar conta por e-mail"));
  _balancesSearch->setClearButtonEnabled(true);
  connect(_balancesSearch, &QLineEdit::textChanged, this, &MainWindow::applyBalanceFilter);
  filterRows->addWidget(_balancesSearch);
  filterRows->addLayout(filtersLayout);
  _balancesOnlyAvailable = new QCheckBox(QStringLiteral("Só com saldo disponível"));
  connect(_balancesOnlyAvailable, &QCheckBox::toggled, this, [this](bool checked) {
    if (checked) _balancesOnlyPending->setChecked(false);
    applyBalanceFilter();
  });
  filtersLayout->addWidget(_balancesOnlyAvailable);
  _balancesOnlyPending = new QCheckBox(QStringLiteral("Só pendentes de atualização"));
  _balancesOnlyPending->setToolTip(QStringLiteral(
      "Mostra contas Crowtado conectadas sem leitura confirmada, com erro ou com saldo de mais de 24 horas."));
  connect(_balancesOnlyPending, &QCheckBox::toggled, this, [this](bool checked) {
    if (checked) _balancesOnlyAvailable->setChecked(false);
    applyBalanceFilter();
  });
  filtersLayout->addWidget(_balancesOnlyPending);
  filtersLayout->addWidget(_balancesRefreshNeeded);
  _balancesFilterState = quietLabel(QStringLiteral("0 contas"));
  filtersLayout->addWidget(_balancesFilterState);


  _balancesTable = new QTableWidget(0, 5);
  _balancesTable->setMinimumHeight(300);
  configureTable(_balancesTable, QStringLiteral("Uma visão dos seus saldos"),
                 QStringLiteral("Adicione suas contas e consulte os saldos para verificar os valores disponíveis."));
  _balancesTable->setHorizontalHeaderLabels(
      {QStringLiteral("Conta"), QStringLiteral("Disponível"), QStringLiteral("Pendente"),
       QStringLiteral("Atualizado"), QStringLiteral("Ação")});
  _balancesTable->horizontalHeader()->setSectionResizeMode(0, QHeaderView::Stretch);
  _balancesTable->horizontalHeader()->setSectionResizeMode(1, QHeaderView::Fixed);
  _balancesTable->horizontalHeader()->setSectionResizeMode(2, QHeaderView::Fixed);
  _balancesTable->horizontalHeader()->setSectionResizeMode(3, QHeaderView::Interactive);
  _balancesTable->horizontalHeader()->setSectionResizeMode(4, QHeaderView::Fixed);
  _balancesTable->setColumnWidth(1, 126);
  _balancesTable->setColumnWidth(2, 126);
  _balancesTable->setColumnWidth(3, 176);
  _balancesTable->setColumnWidth(4, 365);
  _balancesTable->horizontalHeaderItem(1)->setToolTip(
      QStringLiteral("Valor em dólar liberado para solicitar saque."));
  _balancesTable->horizontalHeaderItem(2)->setToolTip(
      QStringLiteral("Valor em dólar registrado pelo Crowtado que ainda não foi liberado para saque."));
  auto* accountsBody = new QWidget;
  auto* accountsLayout = new QVBoxLayout(accountsBody);
  accountsLayout->setContentsMargins(0, 0, 0, 0);
  accountsLayout->setSpacing(16);
  accountsLayout->addWidget(filters);
  accountsLayout->addWidget(_balancesTable, 1);
  layout->addWidget(card(QStringLiteral("Saldos por conta"), accountsBody), 1);

  auto* summaryBody = new QWidget;
  auto* summaryLayout = new QVBoxLayout(summaryBody);
  summaryLayout->setContentsMargins(0, 0, 0, 0);
  summaryLayout->setSpacing(10);
  auto* totals = new QWidget;
  auto* totalsLayout = new QHBoxLayout(totals);
  totalsLayout->setContentsMargins(0, 0, 0, 0);
  totalsLayout->setSpacing(28);
  const auto totalBlock = [](const QString& title, QLabel** usd, QLabel** brl) {
    auto* block = new QWidget;
    auto* blockLayout = new QVBoxLayout(block);
    blockLayout->setContentsMargins(0, 0, 0, 0);
    blockLayout->setSpacing(3);
    auto* caption = new QLabel(title.toUpper());
    caption->setObjectName(QStringLiteral("balanceTotalCaption"));
    blockLayout->addWidget(caption);
    *usd = new QLabel(QStringLiteral("US$ —"));
    (*usd)->setObjectName(QStringLiteral("balanceTotalUsd"));
    blockLayout->addWidget(*usd);
    *brl = new QLabel(QStringLiteral("≈ R$ —"));
    (*brl)->setObjectName(QStringLiteral("balanceTotalBrl"));
    blockLayout->addWidget(*brl);
    return block;
  };
  totalsLayout->addWidget(totalBlock(
      QStringLiteral("Total aprovado"), &_balancesApprovedUsd, &_balancesApprovedBrl), 1);
  auto* divider = new QFrame;
  divider->setFrameShape(QFrame::VLine);
  divider->setObjectName(QStringLiteral("balanceDivider"));
  totalsLayout->addWidget(divider);
  totalsLayout->addWidget(totalBlock(
      QStringLiteral("Total pendente"), &_balancesPendingUsd, &_balancesPendingBrl), 1);
  summaryLayout->addWidget(totals);
  _balancesTotalsNote = quietLabel(QString());
  _balancesTotalsNote->setWordWrap(true);
  _balancesTotalsNote->hide();
  summaryLayout->addWidget(_balancesTotalsNote);
  _balancesExchange = quietLabel(QStringLiteral("Carregando cotação USD/BRL…"));
  _balancesExchange->setObjectName(QStringLiteral("balanceExchange"));
  summaryLayout->addWidget(_balancesExchange);
  auto* summary = card(QStringLiteral("PATRIMÔNIO DA OPERAÇÃO"), summaryBody);
  summary->setObjectName(QStringLiteral("walletContext"));
  summary->setSizePolicy(QSizePolicy::Expanding, QSizePolicy::Fixed);
  layout->insertWidget(0, summary);

  auto* scroll = new QScrollArea;
  scroll->setWidgetResizable(true);
  scroll->setFrameShape(QFrame::NoFrame);
  scroll->setWidget(body);
  return pageShell(QStringLiteral("Carteira"),
                   QStringLiteral("Acompanhe valores disponíveis e pendentes e solicite o link de saque."), scroll);
}

QWidget* MainWindow::buildBannedPage() {
  auto* body = new QWidget;
  auto* layout = new QVBoxLayout(body);
  layout->setContentsMargins(0, 0, 0, 0);
  auto* actions = new QHBoxLayout;
  _bannedState = quietLabel(QStringLiteral("Carregando contas banidas…"));
  actions->addWidget(_bannedState, 1);
  _bannedRefresh = primaryButton(QStringLiteral("Consultar saldos e status"));
  actions->addWidget(_bannedRefresh);
  layout->addLayout(actions);
  auto* help = quietLabel(QStringLiteral(
      "O status é consultado no Minute e o saldo no Crowtado. O saque depende do saldo Crowtado confirmado, "
      "mesmo quando o Minute está desativado. Valores com * são da última consulta de saldo concluída."));
  help->setWordWrap(true);
  layout->addWidget(help);
  _bannedTable = new QTableWidget(0, 8);
  configureTable(_bannedTable, QStringLiteral("Contas que precisam de atenção"),
                 QStringLiteral("As contas restritas identificadas na consulta aparecem aqui."));
  _bannedTable->setHorizontalHeaderLabels({QStringLiteral("E-mail"), QStringLiteral("Status atual"),
      QStringLiteral("Disponível (USD)"), QStringLiteral("Pendente (USD)"),
      QStringLiteral("Banimento"), QStringLiteral("Última consulta"),
      QStringLiteral("Senha"), QStringLiteral("Saque")});
  _bannedTable->horizontalHeader()->setSectionResizeMode(QHeaderView::ResizeToContents);
  _bannedTable->horizontalHeader()->setSectionResizeMode(0, QHeaderView::Stretch);
  layout->addWidget(_bannedTable, 1);
  _bannedPoll.setInterval(2000);
  connect(&_bannedPoll, &QTimer::timeout, this, &MainWindow::loadBanned);
  connect(_bannedRefresh, &QPushButton::clicked, this, [this] {
    _bannedRefresh->setEnabled(false);
    _api.post(QStringLiteral("/api/accounts/banned/refresh"), {},
      [this](bool ok, const QJsonDocument&, const QString& error) {
        if (!ok) {
          _bannedRefresh->setEnabled(true);
          return showError(QStringLiteral("Consulta não iniciada"), error);
        }
        _bannedPoll.start();
        loadBanned();
      });
  });
  return pageShell(QStringLiteral("Banidas"), QStringLiteral("Saldos e situação atual das contas removidas."), body);
}

void MainWindow::loadBanned() {
  if (_bannedPolling) return;
  _bannedPolling = true;
  _api.get(QStringLiteral("/api/accounts/banned/monitor"),
    [this](bool ok, const QJsonDocument& doc, const QString& error) {
      _bannedPolling = false;
      if (!ok) { _bannedState->setText(error); return; }
      const auto root = doc.object();
      const auto rows = root.value(QStringLiteral("accounts")).toArray();
      const auto runner = root.value(QStringLiteral("runner")).toObject();
      const bool running = runner.value(QStringLiteral("state")).toString() == QStringLiteral("running");
      _bannedRefresh->setEnabled(!running && !rows.isEmpty());
      if (running) {
        _bannedState->setText(QStringLiteral("Consultando %1 de %2 contas…")
            .arg(runner.value(QStringLiteral("completed")).toInt()).arg(runner.value(QStringLiteral("total")).toInt()));
        if (_pages->currentIndex() == 8) _bannedPoll.start();
      } else {
        _bannedPoll.stop();
        _bannedState->setText(runner.value(QStringLiteral("error")).toString(
            rows.isEmpty() ? QStringLiteral("Nenhuma conta banida registrada.") : QStringLiteral("%1 contas no registro de banidas").arg(rows.size())));
      }
      _bannedTable->setRowCount(rows.size());
      for (int row = 0; row < rows.size(); ++row) {
        const auto account = rows[row].toObject();
        const auto monitor = account.value(QStringLiteral("monitor")).toObject();
        const auto balance = monitor.value(QStringLiteral("balance")).toObject();
        const QString suffix = monitor.value(QStringLiteral("balance_stale")).toBool() ? QStringLiteral(" *") : QString();
        const QString absent = monitor.value(QStringLiteral("balance_status")).toString() == QStringLiteral("not_applicable") ? QStringLiteral("Não se aplica") : QStringLiteral("Não consultado");
        QStringList values{account.value(QStringLiteral("email")).toString(),
            monitor.value(QStringLiteral("status_label")).toString(account.value(QStringLiteral("has_password")).toBool() ? QStringLiteral("Ainda não consultada") : QStringLiteral("Sem senha salva")),
            balance.contains(QStringLiteral("availableCents")) ? usdMoney(balance.value(QStringLiteral("availableCents")).toInteger()) + suffix : absent,
            balance.contains(QStringLiteral("pendingCents")) ? usdMoney(balance.value(QStringLiteral("pendingCents")).toInteger()) + suffix : absent,
            friendlyDate(account.value(QStringLiteral("banned_at")).toString()),
            friendlyDate(monitor.value(QStringLiteral("checked_at")).toString())};
        const QString detail = monitor.value(QStringLiteral("detail")).toString() + QStringLiteral("\n")
            + monitor.value(QStringLiteral("balance_detail")).toString() + QStringLiteral("\nSaldo atualizado: ")
            + friendlyDate(monitor.value(QStringLiteral("balance_updated_at")).toString());
        for (int col = 0; col < values.size(); ++col) {
          auto* item = new QTableWidgetItem(values[col]);
          item->setToolTip(detail.trimmed());
          _bannedTable->setItem(row, col, item);
        }
        const QString email = account.value(QStringLiteral("email")).toString();
        auto* reveal = new QPushButton(QStringLiteral("Ver senha"));
        reveal->setMinimumHeight(32);
        reveal->setEnabled(account.value(QStringLiteral("has_password")).toBool());
        reveal->setToolTip(reveal->isEnabled()
            ? QStringLiteral("Mostrar a senha salva para acesso manual.")
            : QStringLiteral("Esta conta não possui senha salva."));
        connect(reveal, &QPushButton::clicked, this, [this, email] {
          _api.post(QStringLiteral("/api/accounts/banned/password"),
                    {{QStringLiteral("email"), email}},
                    [this, email](bool ok, const QJsonDocument& doc, const QString& error) {
            if (!ok) return showError(QStringLiteral("Senha indisponível"), error);
            QMessageBox dialog(this);
            dialog.setWindowTitle(QStringLiteral("Senha da conta banida"));
            dialog.setTextFormat(Qt::PlainText);
            dialog.setText(QStringLiteral("%1\n\nSenha: %2")
                .arg(email, doc.object().value(QStringLiteral("password")).toString()));
            dialog.setTextInteractionFlags(Qt::TextSelectableByMouse);
            dialog.exec();
          });
        });
        _bannedTable->setCellWidget(row, 6, reveal);
        auto* withdraw = new QPushButton(QStringLiteral("Saque"));
        withdraw->setMinimumHeight(32);
        withdraw->setEnabled(!running && account.value(QStringLiteral("withdraw_eligible")).toBool());
        withdraw->setToolTip(account.value(QStringLiteral("withdraw_reason")).toString());
        connect(withdraw, &QPushButton::clicked, this, [this, withdraw, email] {
          if (QMessageBox::question(this, QStringLiteral("Solicitar saque"),
                QStringLiteral("Solicitar à Crowtado o link de saque de %1? A conclusão ocorre no Dots.").arg(email))
              != QMessageBox::Yes) return;
          withdraw->setEnabled(false);
          _api.post(QStringLiteral("/api/accounts/banned/withdraw"),
                    {{QStringLiteral("email"), email}},
                    [this](bool ok, const QJsonDocument& doc, const QString& error) {
            if (!ok) showError(QStringLiteral("Saque não solicitado"), error);
            else QMessageBox::information(this, QStringLiteral("Solicitação enviada"),
                                          doc.object().value(QStringLiteral("message")).toString());
            loadBanned();
          });
        });
        _bannedTable->setCellWidget(row, 7, withdraw);
      }
    });
}

QWidget* MainWindow::buildHistoryPage() {
  auto* body = new QWidget;
  auto* layout = new QVBoxLayout(body);
  layout->setContentsMargins(0, 0, 0, 0);
  layout->setSpacing(14);
  _historyTable = new QTableWidget(0, 4);
  configureTable(_historyTable, QStringLiteral("Sua operação deixa um histórico"),
                 QStringLiteral("Após executar uma campanha, consulte aqui os resultados de cada conta."));
  _historyTable->setMinimumHeight(180);
  _historyTable->setMaximumHeight(250);
  _historyTable->setHorizontalHeaderLabels(
      {QStringLiteral("Início"), QStringLiteral("Contas"), QStringLiteral("Vídeos"), QStringLiteral("Envios OK")});
  _historyTable->horizontalHeader()->setSectionResizeMode(0, QHeaderView::Fixed);
  _historyTable->horizontalHeader()->setSectionResizeMode(1, QHeaderView::Stretch);
  _historyTable->horizontalHeader()->setSectionResizeMode(2, QHeaderView::Fixed);
  _historyTable->horizontalHeader()->setSectionResizeMode(3, QHeaderView::Fixed);
  _historyTable->setColumnWidth(0, 185);
  _historyTable->setColumnWidth(2, 92);
  _historyTable->setColumnWidth(3, 110);
  _historyTable->setSelectionBehavior(QAbstractItemView::SelectRows);
  _historyTable->setSelectionMode(QAbstractItemView::SingleSelection);
  _historyDetail = new QPlainTextEdit;
  _historyDetail->setReadOnly(true);
  _historyDetail->setMinimumHeight(220);
  _historyDetail->setPlaceholderText(QStringLiteral("Selecione uma campanha para ver o registro completo."));
  _historyEvidence = new QTableWidget(0,4);
  configureTable(_historyEvidence, QStringLiteral("Evidências de cada envio"),
                 QStringLiteral("Selecione uma campanha para consultar conta, clipe, sessão e confirmação."));
  _historyEvidence->setHorizontalHeaderLabels({QStringLiteral("Conta"),QStringLiteral("Clipe"),QStringLiteral("Sessão"),QStringLiteral("Confirmação")});
  _historyEvidence->horizontalHeader()->setSectionResizeMode(QHeaderView::Stretch);
  _historyEvidence->setEditTriggers(QAbstractItemView::NoEditTriggers);
  auto* verifyPreviews = new QPushButton(QStringLiteral("Verificar prévias no Minute"));
  connect(verifyPreviews, &QPushButton::clicked, this, [this, verifyPreviews] {
    const int row = _historyTable->currentRow();
    if (row < 0 || !_historyTable->item(row, 0)) {
      return showError(QStringLiteral("Selecione uma campanha"),
                       QStringLiteral("Escolha uma linha do histórico antes de verificar as prévias."));
    }
    const QString name = _historyTable->item(row, 0)->data(Qt::UserRole).toString();
    if (name.isEmpty()) return;
    verifyPreviews->setEnabled(false);
    verifyPreviews->setText(QStringLiteral("Consultando o Minute…"));
    setStatus(QStringLiteral("Consultando o processamento real das prévias no Minute…"));
    _api.post(QStringLiteral("/api/logs/") + encoded(name) + QStringLiteral("/status"), {},
              [this, verifyPreviews, name](bool ok, const QJsonDocument& doc, const QString& error) {
      verifyPreviews->setEnabled(true);
      verifyPreviews->setText(QStringLiteral("Verificar prévias no Minute"));
      const int selected = _historyTable->currentRow();
      if (selected < 0 || !_historyTable->item(selected, 0)
          || _historyTable->item(selected, 0)->data(Qt::UserRole).toString() != name) return;
      if (!ok) return showError(QStringLiteral("Falha ao verificar prévias"), error);
      const auto summary = doc.object().value(QStringLiteral("summary")).toObject();
      const int ready = summary.value(QStringLiteral("ready")).toInt();
      const int pending = summary.value(QStringLiteral("pending")).toInt();
      const int unavailable = summary.value(QStringLiteral("unavailable")).toInt();
      const int errors = summary.value(QStringLiteral("errors")).toInt();
      QStringList attention;
      for (const auto value : doc.object().value(QStringLiteral("results")).toArray()) {
        const auto result = value.toObject();
        const QString status = result.value(QStringLiteral("status")).toString();
        const QString email = result.value(QStringLiteral("email")).toString();
        if (status.startsWith(QStringLiteral("unprocessed:unavailable"))) {
          attention << QStringLiteral("× %1 — prévia indisponível").arg(email);
        } else if (status != QStringLiteral("preview_ready")
                   && status != QStringLiteral("unprocessed:pending")
                   && status != QStringLiteral("processing")) {
          attention << QStringLiteral("× %1 — %2").arg(email, status);
        }
      }
      QStringList lines;
      lines << _historyDetail->toPlainText()
            << QString()
            << QStringLiteral("PROCESSAMENTO NO MINUTE")
            << QStringLiteral("✓ %1 arquivo(s) pronto(s)  ·  %2 processando  ·  %3 indisponível(is)  ·  %4 erro(s)")
                   .arg(ready).arg(pending).arg(unavailable).arg(errors);
      if (pending > 0)
        lines << QStringLiteral("Os arquivos foram recebidos; o Minute ainda não publicou essas prévias.");
      lines << attention;
      _historyDetail->setPlainText(lines.join(QLatin1Char('\n')));
      setStatus(QStringLiteral("Arquivos de prévia: %1 prontos, %2 processando, %3 indisponíveis.")
                    .arg(ready).arg(pending).arg(unavailable));
    });
  });
  connect(_historyTable, &QTableWidget::currentCellChanged, this, [this](int row, int) {
    _historyDetail->clear();
    _historyEvidence->setRowCount(0);
    if (row < 0 || !_historyTable->item(row, 0)) return;
    const QString name = _historyTable->item(row, 0)->data(Qt::UserRole).toString();
    if (name.isEmpty()) return;
    _api.get(QStringLiteral("/api/logs/") + encoded(name),
             [this, name](bool ok, const QJsonDocument& doc, const QString& error) {
      const int selected = _historyTable->currentRow();
      if(selected < 0 || !_historyTable->item(selected,0) || _historyTable->item(selected,0)->data(Qt::UserRole).toString()!=name) return;
      if (!ok) return showError(QStringLiteral("Falha ao abrir registro"), error);
      const auto root = doc.object();
      const auto summary = root.value(QStringLiteral("summary")).toObject();
      _historyEvidence->setRowCount(0);
      for(const auto itemValue:root.value("items").toArray()) {
        const auto item=itemValue.toObject();
        for(const auto resultValue:item.value("accounts").toArray()) {
          const auto result=resultValue.toObject();
          const auto confirmation=result.value("confirmation").toString();
          const auto status=result.value("status").toString();
          const QString proof=confirmation=="remote_ack"?QStringLiteral("Finalização confirmada"):
              confirmation=="legacy_record"?QStringLiteral("Registro legado"):
              status=="skipped"?QStringLiteral("Pulado"):
              status=="failed"?QStringLiteral("Falhou"):QStringLiteral("Sem confirmação");
          const int row=_historyEvidence->rowCount();
          _historyEvidence->insertRow(row);
          const QStringList values{result.value("email").toString(),item.value("clip_uid").toString(QStringLiteral("Não registrado")),result.value("session_id").toString(QStringLiteral("Não registrada")),proof};
          for(int column=0;column<values.size();++column) {
            auto* value=new QTableWidgetItem(values[column]);
            value->setToolTip(values[column]+QStringLiteral("\n")+result.value("detail").toString());
            _historyEvidence->setItem(row,column,value);
          }
          _historyEvidence->setRowHeight(row,48);
        }
      }
      QStringList lines;
      lines << QStringLiteral("RESULTADO DA CAMPANHA")
            << friendlyDate(root.value(QStringLiteral("started_at")).toString())
            << QString()
            << QStringLiteral("%1 envio(s) sem confirmação de finalização").arg(summary.value("pending").toInt())
            << QStringLiteral("VISÃO GERAL")
            << QStringLiteral("%1 vídeo(s) · %2 envio(s) concluído(s) · %3 ignorado(s) · %4 falha(s)")
                   .arg(summary.value(QStringLiteral("videos")).toInt())
                   .arg(summary.value(QStringLiteral("success")).toInt())
                   .arg(summary.value(QStringLiteral("skipped")).toInt())
                   .arg(summary.value(QStringLiteral("failed")).toInt())
            << QString();
      const auto issues = root.value(QStringLiteral("issues")).toArray();
      if (!issues.isEmpty()) {
        lines << QStringLiteral("PENDÊNCIAS");
        for (const auto issueValue : issues) {
          const auto issue = issueValue.toObject();
          lines << QStringLiteral("! %1 — %2")
                       .arg(issue.value(QStringLiteral("title")).toString(),
                            issue.value(QStringLiteral("detail")).toString());
        }
        lines << QString();
      }
      lines << QStringLiteral("POR CONTA");
      for (const auto accountValue : root.value(QStringLiteral("accounts")).toArray()) {
        const auto account = accountValue.toObject();
        const int success = account.value(QStringLiteral("success")).toInt();
        const int skipped = account.value(QStringLiteral("skipped")).toInt();
        const int failed = account.value(QStringLiteral("failed")).toInt();
        const QString marker = failed > 0 ? QStringLiteral("×")
                             : success > 0 ? QStringLiteral("✓") : QStringLiteral("•");
        lines << QStringLiteral("%1  %2").arg(marker, account.value(QStringLiteral("email")).toString())
              << QStringLiteral("    %1 envio(s) concluído(s) · %2 ignorado(s) · %3 falha(s)")
                     .arg(success).arg(skipped).arg(failed);
      }
      lines << QString() << QStringLiteral("CONTEÚDO PROCESSADO");
      for (const auto itemValue : root.value(QStringLiteral("items")).toArray()) {
        const auto item = itemValue.toObject();
        const int duration = item.value(QStringLiteral("duration_s")).toInt();
        const int minutes = duration / 60;
        const int seconds = duration % 60;
        const QString durationText = minutes > 0
            ? QStringLiteral("%1min %2s").arg(minutes).arg(seconds, 2, 10, QLatin1Char('0'))
            : QStringLiteral("%1s").arg(seconds);
        const int failed = item.value(QStringLiteral("failed")).toInt();
        const QString marker = failed > 0 ? QStringLiteral("×") : QStringLiteral("✓");
        lines << QStringLiteral("%1  %2 · %3")
                     .arg(marker, item.value(QStringLiteral("task")).toString(), durationText)
              << QStringLiteral("    %1 envio(s) concluído(s) · %2 ignorado(s) · %3 falha(s)")
                     .arg(item.value(QStringLiteral("success")).toInt())
                     .arg(item.value(QStringLiteral("skipped")).toInt())
                     .arg(failed);
        for (const auto resultValue : item.value(QStringLiteral("accounts")).toArray()) {
          const auto result = resultValue.toObject();
          if (result.value(QStringLiteral("status")).toString() == QStringLiteral("success")) continue;
          lines << QStringLiteral("      ! %1 — %2")
                       .arg(result.value(QStringLiteral("email")).toString(),
                            result.value(QStringLiteral("detail")).toString());
        }
      }
      _historyDetail->setPlainText(lines.join(QLatin1Char('\n')));
    });
  });
  auto* detailBody = new QWidget;
  auto* detailLayout = new QVBoxLayout(detailBody);
  detailLayout->setContentsMargins(0, 0, 0, 0);
  detailLayout->setSpacing(10);
  detailLayout->addWidget(verifyPreviews, 0, Qt::AlignRight);
  auto* evidenceTabs = new QTabWidget;
  evidenceTabs->addTab(_historyDetail,QStringLiteral("Resumo"));
  evidenceTabs->addTab(_historyEvidence,QStringLiteral("Evidências por envio"));
  detailLayout->addWidget(evidenceTabs,1);
  layout->addWidget(card(QStringLiteral("Campanhas"), _historyTable));
  layout->addWidget(card(QStringLiteral("Registro"), detailBody), 1);
  auto* scroll = new QScrollArea;
  scroll->setWidgetResizable(true);
  scroll->setFrameShape(QFrame::NoFrame);
  scroll->setWidget(body);
  return pageShell(QStringLiteral("Histórico"),
                   QStringLiteral("Audite campanhas anteriores e seus resultados por conta."), scroll);
}

void MainWindow::applyStructuralStyle(bool dark) {
  const QString bg = dark ? QStringLiteral("#191a20") : QStringLiteral("#f3f4f7");
  const QString panel = dark ? QStringLiteral("#23242c") : QStringLiteral("#ffffff");
  const QString sidebar = QStringLiteral("#18191d");
  const QString text = dark ? QStringLiteral("#f2f3f1") : QStringLiteral("#20212b");
  const QString muted = dark ? QStringLiteral("#b2b3c3") : QStringLiteral("#606477");
  const QString border = dark ? QStringLiteral("#383a48") : QStringLiteral("#dfe1ea");
  const QString selected = QStringLiteral("#30294c");
  const QString field = dark ? QStringLiteral("#1c1d25") : QStringLiteral("#fafafe");
  const QString soft = dark ? QStringLiteral("#2b2c36") : QStringLiteral("#edeef5");

  setStyleSheet(QStringLiteral(R"(
    * { font-family: "Segoe UI"; }
    #operationSurface { background: %8; border: 1px solid %6; border-radius: 14px; }
    #operationInspector { background: #23242c; border: 1px solid #383a48; border-radius: 14px; }
    #walletContext, #libraryContext, #integrationsContext { background: #23242c; border: 1px solid #383a48; border-radius: 14px; }
    #walletContext QLabel, #libraryContext QLabel, #integrationsContext QLabel { color: #f2f3f7; }
    #walletContext #quiet, #libraryContext #quiet, #integrationsContext #quiet { color: #bfc2ce; }
    #walletContext #cardTitle, #libraryContext #cardTitle { color: #bfc2ce; letter-spacing: 1px; }
    #integrationsContext #securityBadge { color: #a8e4ca; background: #293b37; border: 1px solid #466358; }
    #operationTab, #operationTabActive { font-size: 16px; font-weight: 500; padding: 14px 16px; }
    #operationTabActive { color: #7046ff; border-bottom: 2px solid #7046ff; border-radius: 0; }
    #workspaceBrand { color: %4; font-size: 15px; font-weight: 650; }
    #workspaceTitle { color: %4; font-size: 44px; font-weight: 750; letter-spacing: -1px; }
    #commandSearch { color: %5; background: transparent; text-align: left; min-height: 28px; font-weight: 400; }
    #operatorIdentity { color: %4; font-size: 12px; }
    #inspectorTitle { color: #f5f6fa; font-size: 25px; font-weight: 700; }
    #inspectorBalanceCaption { color: #c3c6d2; font-size: 12px; letter-spacing: 1.5px; border-top: 1px solid #4a4e5c; padding-top: 28px; }
    #inspectorBalanceAction { background: transparent; color: #f5f6fa; border: 1px solid #a4aabd; border-radius: 7px; padding: 8px 16px; }
    #inspectorBalance { color: #f5f6fa; font-size: 36px; font-weight: 700; }
    #operationInspector #heroDescription { font-size: 15px; }
    #liveBadge { color: #00845d; background: #d9f4e9; border-radius: 12px; padding: 10px 14px; font-size: 14px; font-weight: 600; }
    #operationTotal { color: %4; font-size: 38px; font-weight: 750; padding-top: 12px; }
    #operationTable { background: transparent; border: none; }
    #operationTable QHeaderView::section { background-color: %2; font-size: 12px; font-weight: 500; padding: 16px 8px; }
    #operationTitle { color: %4; font-size: 34px; font-weight: 700; }
    #operationSubtitle { color: %5; font-size: 18px; }
    #operationProgress { background: #dce0ea; min-height: 9px; max-height: 9px; border-radius: 4px; }
    #operationStages { color: #9179ff; border-top: 2px solid #7357ec; padding-top: 18px; padding-bottom: 12px; font-size: 12px; font-weight: 600; }
    #operationHero { background: #23242c; border: 1px solid #383a48; border-radius: 16px; }
    #heroEyebrow { color: #b9b2d8; font-size: 10px; font-weight: 700; letter-spacing: 1.4px; }
    #heroTitle { color: #ffffff; font-family: "Bahnschrift", "Segoe UI"; font-size: 30px; font-weight: 650; }
    #heroDescription { color: #d1d0df; font-size: 12px; }
    #journeyIndex { color: %5; font-family: "Bahnschrift", "Segoe UI"; font-size: 23px; }
    #journeyRow { border-bottom: 1px solid %6; }
    QScrollArea, QScrollArea > QWidget > QWidget { background: %1; }
    QPushButton { color: %4; background: %9; border: 1px solid %6; border-radius: 9px; padding: 8px 14px; font-weight: 600; }
    QPushButton:hover { border-color: #7357ec; }
    QPushButton:pressed { background: %8; }
    QPushButton:focus { border-color: #7357ec; }
    QPushButton[role="primary"] { color: #ffffff; background: #7357ec; border-color: #7357ec; }
    QPushButton[role="primary"]:hover { background: #9179ff; }
    QPushButton[role="primary"]:pressed { background: #6245d8; }
    QPushButton:disabled, QPushButton[role="primary"]:disabled { color: %5; background: %8; border-color: %6; }
    QPushButton:flat { background: transparent; border-color: transparent; }
    QPushButton:flat:hover { color: #7357ec; background: %9; }
    QTabWidget::pane { border: none; background: %1; }
    QTabBar::tab { color: %5; background: transparent; padding: 12px 18px; border-bottom: 2px solid transparent; font-size: 13px; }
    QTabBar::tab:selected { color: #7357ec; border-bottom: 2px solid #7357ec; }
    #workspace { background: %1; }
    #sidebar { background: %3; color: #f7f8f7; border-right: 1px solid #282c2e; }
    #brandMark { background: transparent; border: none; color: #aa94ff; font-size: 44px; font-weight: 800; }
    #brand { color: #ffffff; font-size: 16px; font-weight: 750; letter-spacing: -0.3px; }
    #brandRole { color: #a9a8bc; font-size: 9px; font-weight: 750; letter-spacing: 1.3px; }
    #navigationLabel { color: #a9a8bc; font-size: 9px; font-weight: 800; letter-spacing: 1.2px; padding-left: 7px; padding-bottom: 4px; }
    #sidebar #quiet { color: #858d91; font-size: 9px; font-weight: 700; letter-spacing: 0.8px; }
    #navigation { background: transparent; color: #c9c7d8; outline: none; border: none; }
    #navigation::item { border-radius: 0; padding: 12px 0; margin: 0; }
    #railSettings { color: #c9c7d8; background: transparent; border: none; font-size: 12px; }
    #railProfile { color: #c9c7d8; padding: 22px 0; border-top: 1px solid #383944; }
    #navigation::item:hover { background: #191c1e; color: #ffffff; }
    #navigation::item:selected { background: %7; color: #ffffff; font-weight: 650; border-left: 2px solid #7357ec; }
    #connectionCard { background: #22232b; border: 1px solid #383944; border-radius: 8px; }
    #backendState { color: #dfe3e1; font-size: 12px; font-weight: 650; }
    #sidebarUtility { color: #c9c7d8; background: #22232b; border: 1px solid #383944; border-radius: 7px; text-align: left; padding: 8px 12px; }
    #sidebarUtility:hover { color: white; background: #202427; border-color: #3a4144; }
    #pageContext { color: %5; font-size: 9px; font-weight: 850; letter-spacing: 1.45px; }
    #modeBadge { color: %5; background: %8; border: 1px solid %6; border-radius: 10px; padding: 4px 10px; font-size: 9px; font-weight: 750; letter-spacing: 0.8px; }
    #pageTitle { color: %4; font-family: "Bahnschrift", "Segoe UI"; font-size: 30px; font-weight: 650; letter-spacing: -0.6px; }
    #pageSubtitle { color: %5; font-size: 13px; }
    #card, #pulseCard, #metricCard { background: %2; border: 1px solid %6; border-radius: 14px; }
    #pulseCard { border-top: 2px solid #7357ec; }
    #metricCard:hover { border-color: #7357ec; }
    #cardTitle { color: %4; font-size: 13px; font-weight: 720; letter-spacing: 0.15px; }
    #metricCaption { color: %5; font-size: 9px; font-weight: 800; letter-spacing: 1.1px; }
    #metricValue { color: %4; font-family: "Cascadia Mono", Consolas; font-size: 32px; font-weight: 700; }
    #metricTiming { color: %5; font-size: 8px; font-weight: 700; letter-spacing: 0.9px; }
    #balanceTotalCaption { color: %5; font-size: 9px; font-weight: 800; letter-spacing: 1.05px; }
    #balanceTotalUsd { color: %4; font-family: "Cascadia Mono", Consolas; font-size: 23px; font-weight: 720; }
    #balanceTotalBrl { color: #7357ec; font-family: "Cascadia Mono", Consolas; font-size: 12px; font-weight: 680; }
    #balanceExchange { color: %5; font-size: 10px; border-top: 1px solid %6; padding-top: 8px; }
    #balanceDivider { color: %6; }
    #pulseTitle { color: %4; font-size: 21px; font-weight: 735; letter-spacing: -0.2px; }
    #securityBadge { color: #61c694; background: %9; border: 1px solid %6; border-radius: 13px; padding: 8px 13px; font-size: 10px; font-weight: 700; }
    #integrationStatus { color: %4; font-size: 13px; font-weight: 700; }
    #integrationStatus[integrationState="ok"] { color: #48c78e; }
    #integrationStatus[integrationState="missing"] { color: #e3aa55; }
    #integrationMiniTitle { color: %4; font-size: 13px; font-weight: 720; }
    #campaignStage { color: %4; font-size: 17px; font-weight: 735; letter-spacing: -0.1px; }
    #campaignStats { color: %5; font-family: "Cascadia Mono", Consolas; font-size: 10px; font-weight: 650; }
    #kicker { color: #7357ec; font-size: 9px; font-weight: 850; letter-spacing: 1.35px; }
    #signalRail { background: #111416; border: 1px solid #2d3336; border-radius: 7px; }
    #signalLabel { color: #7357ec; font-size: 8px; font-weight: 850; letter-spacing: 1.1px; }
    #signalDot { color: #48c78e; font-size: 24px; }
    #signalPort { color: #8d9599; font-family: "Cascadia Mono", Consolas; font-size: 9px; font-weight: 650; }
    #sequenceRow { color: %4; border-bottom: 1px solid %6; padding-left: 4px; font-family: "Cascadia Mono", Consolas; font-size: 11px; }
    #quickAction { color: %4; background: %9; border: 1px solid %6; border-radius: 6px; text-align: left; padding: 8px 12px; font-weight: 620; }
    #quickAction:hover { border-color: #7357ec; color: #7357ec; }
    #quiet { color: %5; }
    #reviewTitle { color: %4; font-size: 28px; font-weight: 700; }
    #reviewMetric { background: %9; border: 1px solid %6; border-radius: 12px; }
    #reviewValue { color: %4; font-size: 28px; font-weight: 700; }
    #reviewWarning { color: %4; border-left: 3px solid #7357ec; padding: 10px; background: %9; }
    #emptyHeading { color: %4; font-size: 18px; font-weight: 650; }
    #tableEmptyState { background: transparent; }
    #appStatusBar { background: %2; border-top: 1px solid %6; }
    #statusVersion { color: %5; font-family: "Cascadia Mono", Consolas; font-size: 9px; }
    QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox, QPlainTextEdit, QListWidget, QTableWidget {
      color: %4; background-color: %8; border: 1px solid %6; border-radius: 6px;
    }
    QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox { min-height: 40px; padding-left: 12px; padding-right: 12px; }
    QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus, QPlainTextEdit:focus, QListWidget:focus { border-color: #7357ec; }
    QComboBox { combobox-popup: 0; padding-right: 44px; }
    QComboBox::drop-down { width: 40px; border-left: 1px solid %6; }
    QComboBox::down-arrow { image: url(:/qmoney/icons/chevron-down.svg); width: 14px; height: 14px; }
    QComboBox QAbstractItemView { color: %4; background-color: %2; border: 1px solid %6; border-radius: 0; selection-background-color: #7357ec; selection-color: white; padding: 0; }
    QComboBox QAbstractItemView::item { padding: 0 12px; }
    QListWidget::item { padding: 7px 9px; border-radius: 4px; }
    QListWidget::item:selected { background: #55439a; color: white; }
    QTableWidget { gridline-color: %6; selection-background-color: #55439a; selection-color: white; alternate-background-color: %9; }
    QTableWidget::item { padding: 8px 10px; border-bottom: 1px solid %6; }
    QTableWidget::item:selected { color: white; background-color: #55439a; }
    QHeaderView::section { color: %5; background: %2; border: none; border-bottom: 1px solid %6; padding: 9px 10px; font-size: 12px; font-weight: 600; }
    QTableCornerButton::section { background: %2; border: none; border-bottom: 1px solid %6; }
    QPlainTextEdit { font-family: "Cascadia Mono", Consolas, monospace; padding: 10px; }
    #campaignTimeline { font-family: "Inter", "Segoe UI"; font-size: 12px; line-height: 1.35; padding: 12px; }
    QProgressBar { min-height: 7px; max-height: 7px; background: %8; border: none; border-radius: 3px; }
    QProgressBar::chunk { background: #7357ec; border-radius: 3px; }
    #campaignProgress, #bulkRegisterProgress { min-height: 22px; max-height: 22px; color: %4; text-align: center; font-family: "Cascadia Mono", Consolas; font-size: 9px; font-weight: 700; }
    QScrollBar:vertical { background: transparent; width: 10px; margin: 2px; }
    QScrollBar::handle:vertical { background: %6; min-height: 30px; border-radius: 4px; }
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
    QScrollBar:horizontal { background: transparent; height: 10px; margin: 2px; }
    QScrollBar::handle:horizontal { background: %6; min-width: 30px; border-radius: 4px; }
    QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0; }
    QScrollBar::add-page, QScrollBar::sub-page { background: transparent; }
    #sidebar QScrollBar::handle:vertical { background: #3a4144; }
    QToolTip { color: #f6f7f6; background: #202426; border: 1px solid #3a4144; padding: 6px; }
  )").arg(bg, panel, sidebar, text, muted, border, selected, field, soft));
}

void MainWindow::setDarkTheme(bool dark) {
  QSettings().setValue(QStringLiteral("darkTheme"), dark);
  _style->setThemeJsonPath(dark ? QStringLiteral(":/qmoney/theme-dark.json")
                                : QStringLiteral(":/qmoney/theme-light.json"));
  applyStructuralStyle(dark);
  _themeButton->setText(dark ? QStringLiteral("☀  Usar tema claro")
                             : QStringLiteral("◐  Usar tema escuro"));
}

void MainWindow::checkForUpdates(bool interactive) {
  if (_updates.isBusy()) return;
  _updateButton->setEnabled(false);
  _updateButton->setText(QStringLiteral("Verificando…"));
  _updates.check(interactive);
}

void MainWindow::installUpdate(const QString& packagePath) {
  const QString appDir = QCoreApplication::applicationDirPath();
  const QString updater = appDir + QStringLiteral("/QMoneyUpdater.exe");
  if (!QFileInfo::exists(updater)) {
    _updateButton->setEnabled(true);
    _updateButton->setText(QStringLiteral("↻  Verificar atualização"));
    return showError(QStringLiteral("Atualização"),
                     QStringLiteral("O componente QMoneyUpdater.exe não foi encontrado."));
  }
  const QStringList arguments = {
      QStringLiteral("--package"), packagePath,
      QStringLiteral("--target"), appDir,
      QStringLiteral("--pid"), QString::number(QCoreApplication::applicationPid()),
      QStringLiteral("--launch"), QStringLiteral("QMoney.exe")};
  if (!QProcess::startDetached(updater, arguments, appDir)) {
    _updateButton->setEnabled(true);
    return showError(QStringLiteral("Atualização"),
                     QStringLiteral("Não foi possível iniciar o instalador da atualização."));
  }
  setStatus(QStringLiteral("Fechando para instalar a atualização…"));
  QTimer::singleShot(150, qApp, &QCoreApplication::quit);
}

void MainWindow::startBackend() {
  ++_probeGeneration;
  _probeInFlight = false;
  const QString appDir = QCoreApplication::applicationDirPath();
  QString packagedService = provisionEmbeddedService();
  if (packagedService.isEmpty())
    packagedService = appDir + QStringLiteral("/runtime/QMoneyService.exe");
  QString program;
  QStringList arguments;
  QString workingDirectory;
  QProcessEnvironment environment = QProcessEnvironment::systemEnvironment();
  if (QFileInfo::exists(packagedService)) {
    terminatePackagedServiceTree();
    program = packagedService;
    arguments = {QStringLiteral("--no-browser"), QStringLiteral("--porta"),
                 QStringLiteral("8876"), QStringLiteral("--parent-pid"),
                 QString::number(QCoreApplication::applicationPid())};
    // Prefere a biblioteca de mídia que acompanha a instalação. No layout de
    // desenvolvimento o executável vive em dist/QMoney e os dados ficam dois
    // níveis acima; numa distribuição portátil eles podem ficar ao lado do EXE.
    workingDirectory = QStandardPaths::writableLocation(QStandardPaths::GenericDataLocation)
                       + QStringLiteral("/QMoney");
    QString libraryRoot = workingDirectory;
    QStringList libraryCandidates;
    const QString savedLibrary = QSettings().value(QStringLiteral("libraryRoot")).toString();
    if (!savedLibrary.isEmpty()) libraryCandidates << savedLibrary;
    libraryCandidates << appDir << QDir(appDir).absoluteFilePath(QStringLiteral("../.."));
    for (const QString& candidate : libraryCandidates) {
      const QString data = QDir::cleanPath(candidate + QStringLiteral("/data/ego4d"));
      if (QFileInfo::exists(data + QStringLiteral("/timed_narrations.jsonl")) ||
          QFileInfo::exists(data + QStringLiteral("/clip_narrations.json")) ||
          QFileInfo::exists(QDir::cleanPath(candidate + QStringLiteral("/data/holoassist")))) {
        libraryRoot = QDir::cleanPath(candidate);
        break;
      }
    }
    QDir().mkpath(workingDirectory);
    // Contas e segredos pertencem ao usuário Windows, nunca à biblioteca.
    for (const QString& key : environment.keys()) {
      if (key.startsWith(QStringLiteral("AWS_"), Qt::CaseInsensitive) ||
          key.startsWith(QStringLiteral("HOSTINGER_"), Qt::CaseInsensitive) ||
          key.startsWith(QStringLiteral("MINUTE_"), Qt::CaseInsensitive) ||
          key.startsWith(QStringLiteral("EGO4D_"), Qt::CaseInsensitive) ||
          key.startsWith(QStringLiteral("CROWTADO_"), Qt::CaseInsensitive))
        environment.remove(key);
    }
    environment.insert(QStringLiteral("QMONEY_USER_ROOT"), workingDirectory);
    environment.insert(QStringLiteral("AWS_SHARED_CREDENTIALS_FILE"), workingDirectory + QStringLiteral("/secrets/aws/credentials"));
    environment.insert(QStringLiteral("AWS_CONFIG_FILE"), workingDirectory + QStringLiteral("/secrets/aws/config"));
    environment.insert(QStringLiteral("AWS_EC2_METADATA_DISABLED"), QStringLiteral("true"));
    environment.insert(QStringLiteral("QMONEY_LIBRARY_ROOT"), libraryRoot);
    environment.insert(QStringLiteral("QMONEY_RUNTIME_ROOT"), appDir + QStringLiteral("/runtime"));
    environment.insert(QStringLiteral("QMONEY_APP_VERSION"),
                       QCoreApplication::applicationVersion());
    environment.insert(QStringLiteral("PLAYWRIGHT_BROWSERS_PATH"),
                       appDir + QStringLiteral("/runtime/ms-playwright"));
  } else {
    const QString root = QString::fromUtf8(QMONEY_PROJECT_ROOT);
    program = root + QStringLiteral("/.venv/Scripts/python.exe");
    if (!QFileInfo::exists(program)) program = QStringLiteral("python");
    arguments = {QStringLiteral("-m"), QStringLiteral("moneymin.web"),
                 QStringLiteral("--no-browser"), QStringLiteral("--porta"),
                 QStringLiteral("8876")};
    workingDirectory = root;
  }
  const QString localApiToken = QUuid::createUuid().toString(QUuid::Id128)
                                + QUuid::createUuid().toString(QUuid::Id128);
  environment.insert(QStringLiteral("QMONEY_LOCAL_API_TOKEN"), localApiToken);
  _api.setSessionToken(localApiToken.toUtf8());
  _backend.setWorkingDirectory(workingDirectory);
  _backend.setProcessEnvironment(environment);
  _backend.setProcessChannelMode(QProcess::MergedChannels);
#ifdef Q_OS_WIN
  _backend.setCreateProcessArgumentsModifier([](QProcess::CreateProcessArguments* args) {
    args->flags |= CREATE_NO_WINDOW;
  });
#endif
  disconnect(&_backend, nullptr, this, nullptr);
  connect(&_backend, &QProcess::readyReadStandardOutput, this, [this] {
    const QString output = QString::fromUtf8(_backend.readAllStandardOutput()).trimmed();
    const QStringList lines = output.split('\n', Qt::SkipEmptyParts);
    for (const QString& rawLine : lines) {
      const QString line = rawLine.trimmed();
      if (!line.contains(QStringLiteral("HTTP/1.1")) && !line.isEmpty()) setStatus(line);
    }
  });
  connect(&_backend, &QProcess::errorOccurred, this, [this](QProcess::ProcessError) {
    if (!_backendReady) setStatus(QStringLiteral("O motor local não pôde ser iniciado."));
  });
  connect(&_backend, qOverload<int, QProcess::ExitStatus>(&QProcess::finished), this,
          [this](int, QProcess::ExitStatus) {
    if (_closing || _restartingBackend) return;
    setBackendReady(false, QStringLiteral("Reiniciando motor…"));
    if (_backendRestarts++ < 3)
      QTimer::singleShot(1200, this, &MainWindow::startBackend);
    else
      setStatus(QStringLiteral("O motor parou repetidamente. Exporte o diagnóstico para suporte."));
  });
  _backend.start(program, arguments);
  _probeAttempts = 0;
  _backendProbe.start();
  QTimer::singleShot(60, this, &MainWindow::probeBackend);
}

void MainWindow::stopBackend() {
  _backendProbe.stop();
  ++_probeGeneration;
  _probeInFlight = false;
  if (_backend.state() != QProcess::NotRunning) {
    _backend.terminate();
    if (!_backend.waitForFinished(1800)) {
      _backend.kill();
      _backend.waitForFinished(1000);
    }
  }
}

void MainWindow::restartBackend() {
  _backendReady = false;
  _backendRestarts = 0;
  _restartingBackend = true;
  stopBackend();
  QTimer::singleShot(350, this, [this] {
    _restartingBackend = false;
    startBackend();
  });
}

void MainWindow::probeBackend() {
  if (_closing || _restartingBackend || _backendReady || _probeInFlight) return;
  _probeInFlight = true;
  const int generation = _probeGeneration;
  ++_probeAttempts;
  _api.get(QStringLiteral("/api/health"),
           [this, generation](bool ok, const QJsonDocument& document, const QString&) {
    if (generation != _probeGeneration || _closing) return;
    _probeInFlight = false;
    const QString serviceVersion = document.object()
                                       .value(QStringLiteral("service"))
                                       .toObject()
                                       .value(QStringLiteral("app_version"))
                                       .toString();
    const bool compatible = ok && serviceVersion == QCoreApplication::applicationVersion();
    if (compatible) {
      _backendProbe.stop();
      _backendRestarts = 0;
      setBackendReady(true);
      _api.get(QStringLiteral("/api/preferences"), [this, generation](bool loaded, const QJsonDocument& prefs, const QString&) {
        if (!loaded || _closing || generation != _probeGeneration) return;
        if (prefs.object().value(QStringLiteral("holoassist_enabled")) != QJsonValue(false)) return;
        for (QComboBox* combo : {_dataset, _cacheProvider}) {
          if (!combo) continue;
          for (int index = combo->count() - 1; index >= 0; --index) {
            const QString value = combo->itemData(index).toString();
            if (value == QStringLiteral("holoassist") || value == QStringLiteral("all"))
              combo->removeItem(index);
          }
        }
      });
      refreshCurrentPage();
      if (!_runtimeChecked && _backend.program().endsWith(QStringLiteral("QMoneyService.exe"), Qt::CaseInsensitive)) {
        _runtimeChecked = true;
        _api.get(QStringLiteral("/api/runtime"),
                 [this, generation](bool checked, const QJsonDocument& result, const QString&) {
          if (!checked || _closing || generation != _probeGeneration) return;
          if (!result.object().value(QStringLiteral("ready")).toBool() && !_updates.isBusy()) {
            setStatus(QStringLiteral("Componentes ausentes detectados. Preparando reparo da instalação…"));
            _updates.repair();
          }
        });
      }
    } else if (_probeAttempts > 22) {
      _backendProbe.stop();
      setBackendReady(false, QStringLiteral("Serviço indisponível"));
    } else if (ok && !serviceVersion.isEmpty()) {
      setBackendReady(false, QStringLiteral("Versão do motor incompatível"));
    }
  });
}

void MainWindow::setBackendReady(bool ready, const QString& message) {
  _backendReady = ready;
  _backendState->setText(ready ? QStringLiteral("●  Conectado em 8876")
                               : QStringLiteral("●  %1").arg(message));
  _backendState->setStyleSheet(ready ? QStringLiteral("color:#61c694")
                                     : QStringLiteral("color:#e66c76"));
  if (ready) {
    setStatus(QStringLiteral("Serviço local pronto."));
    // O pacote Release não distribui scripts de manutenção. Assim que o
    // motor sobe, a própria interface verifica e prepara o catálogo Ego4D
    // quando já existem credenciais protegidas neste computador.
    loadIntegrations();
    loadBulkRegisterDomains();
    const QString healthPath = qApp->property("updateHealthPath").toString();
    if (!healthPath.isEmpty()) {
      QSaveFile marker(healthPath);
      if (marker.open(QIODevice::WriteOnly)) {
        marker.write("ok\n");
        marker.commit();
      }
      qApp->setProperty("updateHealthPath", QString());
    }
    const QString savedPreview =
        QSettings().value(QStringLiteral("previewLogName")).toString();
    if (_previewLogName.isEmpty() && !savedPreview.isEmpty()) {
      _previewLogName = QFileInfo(savedPreview).fileName();
      _previewPoll.start();
      QTimer::singleShot(0, this, &MainWindow::pollCampaignPreviews);
    }
  }
}

void MainWindow::navigate(int index) {
  if (index == 9) {
    // Settings is an action; retain the selected page so it can be opened again.
    const QSignalBlocker blocker(_navigation);
    _navigation->setCurrentRow(_pages->currentIndex());
    _settingsMenu->popup(_navigation->mapToGlobal(QPoint(136, qMin(480, _navigation->height()))));
    return;
  }
  if (index < 0) return;
  _pages->setCurrentIndex(index);
  if (index == 3 && _campaignStop->isEnabled()) _campaignPoll.start();
  else if (index != 3) _campaignPoll.stop();
  if (index != 4) _cachePoll.stop();
  if (index != 6) _balancePoll.stop();
  if (index != 8) _bannedPoll.stop();
  if (_backendReady) refreshCurrentPage();
}

void MainWindow::refreshCurrentPage() {
  if (!_backendReady) return probeBackend();
  switch (_pages->currentIndex()) {
    case 0: loadHome(); break;
    case 1: loadReadiness(); break;
    case 2: loadIntegrations(); break;
    case 3: loadCampaignData(); break;
    case 4: loadAccelerator(); break;
    case 5:
      loadAccounts();
      if (!_bulkRegisterPolling) loadBulkRegisterDomains();
      break;
    case 6: loadBalances(); break;
    case 7: loadHistory(); break;
    case 8: loadBanned(); break;
    default: break;
  }
}

void MainWindow::showError(const QString& title, const QString& error) {
  QMessageBox::warning(this, title, error);
  setStatus(error);
}

void MainWindow::showAccountIssues(const QString& title, const QStringList& blockers,
                                  const QJsonArray& issues, std::function<void()> continueAction) {
  const QString checkedAt = QDateTime::currentDateTime().toString(QStringLiteral("dd/MM/yyyy HH:mm:ss"));
  QStringList sections;
  sections << QStringLiteral("Verificação em %1").arg(checkedAt);
  if (!blockers.isEmpty()) sections << blockers.join(QLatin1Char('\n'));
  for (const auto& value : issues) {
    const auto issue = value.toObject();
    const QString email = issue.value(QStringLiteral("email")).toString();
    sections << QStringLiteral("Conta: %1\nEtapa: %2\nMotivo: %3\nComo resolver: %4\nDetalhe: %5")
        .arg(email, issue.value(QStringLiteral("stage")).toString(),
             issue.value(QStringLiteral("reason")).toString(),
             issue.value(QStringLiteral("action")).toString(),
             issue.value(QStringLiteral("detail")).toString());
  }
  const QString report = sections.join(QStringLiteral("\n\n"));
  QDialog dialog(this);
  dialog.setWindowTitle(title);
  dialog.setSizeGripEnabled(true);
  const QSize available = screen()->availableGeometry().size();
  dialog.resize(qMin(820, available.width() - 40), qMin(540, available.height() - 80));
  auto* layout = new QVBoxLayout(&dialog);
  layout->setContentsMargins(22, 20, 22, 20);
  layout->setSpacing(14);
  auto* heading = new QLabel(title);
  heading->setWordWrap(true);
  auto titleFont = heading->font();
  titleFont.setPointSize(16);
  titleFont.setBold(true);
  heading->setFont(titleFont);
  layout->addWidget(heading);
  auto* explanation = new QLabel(QStringLiteral(
      "A verificação não foi concluída para os itens abaixo. "
      "Um token no prazo não garante acesso ao serviço. "
      "Falhas de rede não significam necessariamente senha incorreta."));
  explanation->setWordWrap(true);
  layout->addWidget(explanation);
  if (continueAction)
    explanation->setText(explanation->text() + QStringLiteral(
        "\nRemover contas e continuar apaga permanentemente os acessos com restrição confirmada, "
        "salva o registro em banned_accounts.json e inicia com as contas aprovadas, sem repetir a verificação."));
  bool continueRequested = false;
  auto* details = new QPlainTextEdit;
  details->setReadOnly(true);
  details->setLineWrapMode(QPlainTextEdit::WidgetWidth);
  details->setPlainText(report);
  layout->addWidget(details, 1);
  auto* buttons = new QDialogButtonBox;
  if (continueAction) {
    auto* remove = buttons->addButton(QStringLiteral("Remover contas e continuar"), QDialogButtonBox::ActionRole);
    connect(remove, &QPushButton::clicked, &dialog, [&dialog, &continueRequested] {
      continueRequested = true;
      dialog.accept();
    });
  }
  auto* copy = buttons->addButton(QStringLiteral("Copiar diagnóstico"), QDialogButtonBox::ActionRole);
  auto* accounts = buttons->addButton(QStringLiteral("Abrir Contas"), QDialogButtonBox::ActionRole);
  auto* close = buttons->addButton(QStringLiteral("Fechar"), QDialogButtonBox::RejectRole);
  close->setDefault(true);
  connect(copy, &QPushButton::clicked, &dialog, [report] { QApplication::clipboard()->setText(report); });
  connect(accounts, &QPushButton::clicked, &dialog, [this, &dialog] {
    dialog.accept();
    _navigation->setCurrentRow(5);
    navigate(5);
  });
  connect(buttons, &QDialogButtonBox::rejected, &dialog, &QDialog::reject);
  layout->addWidget(buttons);
  setStatus(title);
  dialog.exec();
  if (continueRequested) continueAction();
}

void MainWindow::setStatus(const QString& text) {
  const QString full = text.simplified();
  const QString compact = full.size() > 180 ? full.left(177) + QStringLiteral("…") : full;
  _status->setText(compact);
  _status->setToolTip(full == compact ? QString() : full);
}

void MainWindow::openRecovery() {
  auto* dialog = new QDialog(this);
  dialog->setAttribute(Qt::WA_DeleteOnClose);
  dialog->setWindowTitle(QStringLiteral("Recuperação de envios"));
  dialog->resize(900, 600);
  auto* layout = new QVBoxLayout(dialog);
  layout->setContentsMargins(24, 24, 24, 24);
  layout->setSpacing(16);
  auto* title = new QLabel(QStringLiteral("Retome com o estado conhecido"));
  title->setObjectName(QStringLiteral("reviewTitle"));
  layout->addWidget(title);
  auto* summary = quietLabel(QStringLiteral("Lendo os registros desta instalação…"));
  layout->addWidget(summary);
  auto* table = new QTableWidget(0, 4);
  configureTable(table, QStringLiteral("Registros de recuperação"),
                 QStringLiteral("As sessões que ainda precisam de atenção aparecem aqui."));
  table->setHorizontalHeaderLabels({QStringLiteral("Conta"), QStringLiteral("Clipe"), QStringLiteral("Sessão"), QStringLiteral("Situação")});
  table->horizontalHeader()->setSectionResizeMode(QHeaderView::Stretch);
  table->setEditTriggers(QAbstractItemView::NoEditTriggers);
  layout->addWidget(table, 1);
  layout->addWidget(quietLabel(QStringLiteral("Reconciliar registra na lista local apenas finalizações já confirmadas. Essa ação não envia mídia e não solicita saques.")));
  auto* resumeRow = new QHBoxLayout;
  auto* resumeAccount = new ComboBox(dialog);
  resumeAccount->setAccessibleName(QStringLiteral("Conta para retomar envios"));
  resumeRow->addWidget(resumeAccount, 1);
  auto* resume = new QPushButton(QStringLiteral("Retomar envios desta conta"), dialog);
  resume->setEnabled(false);
  resumeRow->addWidget(resume);
  layout->addLayout(resumeRow);
  auto* poll = new QTimer(dialog);
  poll->setInterval(1500);
  auto* buttons = new QHBoxLayout;
  auto* wallet = new QPushButton(QStringLiteral("Restaurar Wise / Dots na carteira"));
  wallet->hide();
  connect(wallet, &QPushButton::clicked, dialog, [this, dialog] { dialog->close(); _navigation->setCurrentRow(6); });
  buttons->addWidget(wallet);
  buttons->addStretch();
  auto* reconcile = primaryButton(QStringLiteral("Reconciliar confirmações"));
  reconcile->setEnabled(false);
  buttons->addWidget(reconcile);
  layout->addLayout(buttons);
  const QPointer<QDialog> guard(dialog);
  const auto render = [guard, table, summary, reconcile, resume, resumeAccount, poll](bool ok, const QJsonDocument& document, const QString& error) {
    if (!guard) return;
    if (!ok) { summary->setText(error); reconcile->setEnabled(false); resume->setEnabled(false); return; }
    const auto data = document.object();
    const auto items = data.value("items").toArray();
    const QString selectedAccount = resumeAccount->currentText();
    resumeAccount->clear();
    QSet<QString> resumable;
    table->setRowCount(items.size());
    for (int row = 0; row < items.size(); ++row) {
      const auto item = items[row].toObject();
      if (item.value("can_resume").toBool()) resumable.insert(item.value("email").toString());
      const QString status = item.value("status").toString() == "confirmed" ? QStringLiteral("Confirmado; reconciliar") :
                             item.value("status").toString() == "pending" ? QStringLiteral("Envio pendente") : QStringLiteral("Revisar histórico");
      const QStringList values{item.value("email").toString(), item.value("clip_uid").toString(QStringLiteral("Não identificado")), item.value("session_id").toString(), status};
      for (int column = 0; column < values.size(); ++column) {
        auto* value = cell(values[column]);
        value->setToolTip(values[column] + QStringLiteral("\n") + item.value("detail").toString());
        table->setItem(row, column, value);
      }
    }
    summary->setText(QStringLiteral("%1 sessão(ões) pendente(s) · %2 confirmação(ões) para reconciliar")
        .arg(data.value("pending").toInt()).arg(data.value("confirmed").toInt()));
    QStringList accountNames(resumable.begin(), resumable.end());
    accountNames.sort();
    resumeAccount->addItems(accountNames);
    if (accountNames.contains(selectedAccount)) resumeAccount->setCurrentText(selectedAccount);
    const auto worker = data.value("worker").toObject();
    const bool running = worker.value("state").toString() == "running";
    if (running) {
      summary->setText(QStringLiteral("Retomando as sessões existentes de %1…").arg(worker.value("email").toString()));
      poll->start();
    } else {
      poll->stop();
      if (!worker.value("error").toString().isEmpty()) summary->setText(worker.value("error").toString());
    }
    reconcile->setEnabled(!running && data.value("confirmed").toInt() > 0);
    resume->setEnabled(!running && !accountNames.isEmpty());
    resumeAccount->setEnabled(!running);
  };
  const auto refresh = [this, guard, wallet, render] {
    if (!guard || guard->property("readingRecovery").toBool()) return;
    guard->setProperty("readingRecovery", true);
    _api.get(QStringLiteral("/api/recovery"), [guard, wallet, render](bool ok, const QJsonDocument& document, const QString& error) {
      if (!guard) return;
      guard->setProperty("readingRecovery", false);
      wallet->setVisible(document.object().value("wise_cleanup").toObject().value("pending").toBool());
      render(ok, document, error);
    });
  };
  connect(poll, &QTimer::timeout, dialog, refresh);
  connect(resume, &QPushButton::clicked, dialog, [this, guard, resume, resumeAccount, summary, refresh] {
    const QString email = resumeAccount->currentText();
    if (email.isEmpty() || !guard) return;
    if (QMessageBox::question(guard, QStringLiteral("Retomar envios existentes"),
        QStringLiteral("Retomar os envios interrompidos de %1? Esta ação pode transferir mídia pendente e concluir as sessões existentes no serviço.").arg(email),
        QMessageBox::Yes | QMessageBox::No, QMessageBox::No) != QMessageBox::Yes) return;
    resume->setEnabled(false);
    _api.post(QStringLiteral("/api/recovery/resume"), {{"email", email}, {"confirmed", true}},
              [guard, summary, resume, refresh](bool ok, const QJsonDocument&, const QString& error) {
      if (!guard) return;
      if (!ok) { summary->setText(error); resume->setEnabled(true); }
      else refresh();
    });
  });
  refresh();
  connect(reconcile, &QPushButton::clicked, dialog, [this, reconcile, render] {
    reconcile->setEnabled(false);
    _api.post(QStringLiteral("/api/recovery/reconcile"), {}, render);
  });
  dialog->show();
}

void MainWindow::openCommandPalette() {
  auto* dialog = new QDialog(this);
  dialog->setAttribute(Qt::WA_DeleteOnClose);
  dialog->setWindowTitle(QStringLiteral("Buscar no QMoney"));
  dialog->resize(680, 500);
  auto* layout = new QVBoxLayout(dialog);
  layout->setContentsMargins(24,24,24,24);
  auto* search = new QLineEdit;
  search->setPlaceholderText(QStringLiteral("Buscar conta, campanha ou ação"));
  auto* results = new QListWidget;
  layout->addWidget(search);
  layout->addWidget(results, 1);
  auto filter = [results, search] {
    for (int i=0;i<results->count();++i)
      results->item(i)->setHidden(!results->item(i)->text().contains(search->text(),Qt::CaseInsensitive));
  };
  auto add = [results, filter](const QString& label, int page, const QString& identity=QString()) {
    auto* item = new QListWidgetItem(label, results);
    item->setData(Qt::UserRole, page);
    item->setData(Qt::UserRole+1, identity);
    filter();
  };
  const QStringList names{QStringLiteral("Operação"),QStringLiteral("Requisitos"),QStringLiteral("Integrações"),QStringLiteral("Nova campanha"),QStringLiteral("Biblioteca"),QStringLiteral("Contas"),QStringLiteral("Carteira"),QStringLiteral("Histórico"),QStringLiteral("Contas restritas")};
  for (int i=0;i<names.size();++i) add(names[i],i);
  connect(search,&QLineEdit::textChanged,dialog,filter);
  auto activate = [this, dialog](QListWidgetItem* item) {
    if (!item) return;
    const int page=item->data(Qt::UserRole).toInt();
    if(page==5) _pendingAccountFocus=item->data(Qt::UserRole+1).toString();
    if(page==7) _pendingHistoryFocus=item->data(Qt::UserRole+1).toString();
    dialog->accept();
    if(_navigation->currentRow()==page) refreshCurrentPage(); else _navigation->setCurrentRow(page);
  };
  connect(results,&QListWidget::itemActivated,dialog,activate);
  connect(search,&QLineEdit::returnPressed,dialog,[results, activate] {
    for(int i=0;i<results->count();++i) if(!results->item(i)->isHidden()) {activate(results->item(i));break;}
  });
  QPointer<QDialog> guard(dialog);
  if (_backendReady) {
    _api.get(QStringLiteral("/api/accounts"),[guard,add](bool ok,const QJsonDocument& doc,const QString&) {
      if(!guard || !ok) return;
      for(const auto value:doc.object().value("accounts").toArray()) {
        const auto email=value.toObject().value("email").toString();
        add(QStringLiteral("Conta  ·  ")+email,5,email);
      }
    });
    _api.get(QStringLiteral("/api/logs"),[this,guard,add](bool ok,const QJsonDocument& doc,const QString&) {
      if(!guard || !ok) return;
      for(const auto value:doc.object().value("logs").toArray()) {
        const auto log=value.toObject();
        add(QStringLiteral("Campanha  ·  ")+friendlyDate(log.value("started_at").toString()),7,log.value("name").toString());
      }
    });
  }
  dialog->open();
  search->setFocus();
}

void MainWindow::loadHome() {
  const int generation = ++_homeGeneration;
  _homeSync->setText(QStringLiteral("Atualizando…"));
  _homeNextAction->setEnabled(false);
  auto failed = [this, generation](const QString& error) {
    if (generation != _homeGeneration) return;
    _homeSync->setText(QStringLiteral("Leitura indisponível • tente sincronizar"));
    _homePulseTitle->setText(QStringLiteral("Não foi possível atualizar a operação"));
    _homePulseBody->setText(QStringLiteral("Os dados anteriores podem estar desatualizados. Use Sincronizar dados para tentar novamente."));
    _homeNextAction->setEnabled(false);
    setStatus(error);
  };
  _api.get(QStringLiteral("/api/accounts"), [this, generation, failed](bool ok, const QJsonDocument& doc, const QString& error) {
    if (generation != _homeGeneration) return;
    if (!ok || !doc.object().value("accounts").isArray()) return failed(error);
    const auto accounts = doc.object().value("accounts").toArray();
    _homeAccounts->setText(QString::number(accounts.size()));
    _homeAccountStep->setText(accounts.isEmpty()
        ? QStringLiteral("Comece conectando sua primeira conta.")
        : QStringLiteral("%1 conta(s) cadastrada(s). A validação de acesso acontece antes do envio.").arg(accounts.size()));
    _api.get(QStringLiteral("/api/campaigns/current"), [this, generation, accounts, failed](bool ok, const QJsonDocument& doc, const QString& error) {
      if (generation != _homeGeneration) return;
      if (!ok || !doc.object().value("state").isString()) return failed(error);
      const auto summary = OperationSummary::from(accounts, doc.object());
      renderOperation(doc.object());
      _homePulseTitle->setText(summary.title);
      _homePulseBody->setText(summary.detail);
      _homePulseProgress->setValue(summary.progress);
      const auto state = doc.object().value("state").toString();
      _operationProgressRow->setVisible(state == "running" || state == "stopping");
      _homeDestination = summary.destination;
      _homeNextAction->setText(summary.action + QStringLiteral(" →"));
      _homeNextAction->setEnabled(true);
      _homeSync->setText(QStringLiteral("Atualizado às %1").arg(QTime::currentTime().toString(QStringLiteral("HH:mm:ss"))));
    });
  });
  _api.get(QStringLiteral("/api/balances"), [this, generation](bool ok, const QJsonDocument& doc, const QString&) {
    if (generation != _homeGeneration) return;
    if (!ok) {
      _operationBalance->setText(QStringLiteral("US$ —"));
      _operationBalanceNote->setText(QStringLiteral("Não foi possível consultar os saldos."));
      return;
    }
    const auto root = doc.object();
    const auto balances = root.value("balances").toObject();
    const auto kinds = root.value("account_kinds").toObject();
    qint64 cents = 0;
    int known = 0, eligible = 0;
    bool stale = false;
    for (const auto value : root.value("accounts").toArray()) {
      const auto email = value.toString();
      if (kinds.value(email).toString() == "claru") continue;
      ++eligible;
      const auto balance = balances.value(email).toObject();
      if (balance.value("availableCents").isDouble()) {
        cents += qint64(balance.value("availableCents").toDouble());
        ++known;
        stale |= balance.value("stale").toBool() || !balance.value("error").toString().isEmpty();
      }
    }
    _operationBalance->setText(known ? usdMoney(cents) : QStringLiteral("US$ —"));
    _operationBalanceNote->setText(QStringLiteral("Última leitura salva • %1 de %2 contas%3")
        .arg(known).arg(eligible).arg(stale ? QStringLiteral(" • requer atualização") : QString()));
  });
  _api.get(QStringLiteral("/api/logs"), [this, generation](bool ok, const QJsonDocument& doc, const QString&) {
    if (generation != _homeGeneration) return;
    if (!ok || !doc.object().value("logs").isArray()) {
      _homeCampaigns->setText(QStringLiteral("—"));
      _homeSuccess->setText(QStringLiteral("—"));
      _homeRecent->setText(QStringLiteral("Histórico indisponível. Sincronize os dados para tentar novamente."));
      return;
    }
    const auto logs = doc.object().value("logs").toArray();
    _homeCampaigns->setText(QString::number(logs.size()));
    if (logs.isEmpty()) {
      _homeSuccess->setText(QStringLiteral("—"));
      _homeRecent->setText(QStringLiteral("Sua primeira campanha aparecerá aqui. Comece pelos passos acima."));
    } else {
      const auto last = logs.first().toObject();
      _homeSuccess->setText(QStringLiteral("%1 / %2").arg(last.value("ok").toInt()).arg(last.value("sends").toInt()));
      _homeRecent->setText(QStringLiteral("%1 • %2 clipe(s) • %3 conta(s). Consulte os resultados por conta no histórico.")
          .arg(friendlyDate(last.value("started_at").toString())).arg(last.value("items").toInt()).arg(last.value("accounts").toArray().size()));
    }
  });
}

void MainWindow::loadReadiness() {
  _readinessRefresh->setEnabled(false);
  _readinessRefresh->setText(QStringLiteral("Verificando…"));
  const QString provider = _dataset ? _dataset->currentData().toString() : QStringLiteral("all");
  _api.get(QStringLiteral("/api/readiness?dataset=%1").arg(encoded(provider)),
           [this](bool ok, const QJsonDocument& doc, const QString& error) {
    _readinessRefresh->setEnabled(true);
    _readinessRefresh->setText(QStringLiteral("Executar verificação"));
    if (!ok) return showError(QStringLiteral("Prontidão indisponível"), error);
    const auto root = doc.object();
    const auto checks = root.value(QStringLiteral("checks")).toArray();
    int passed = 0;
    _readinessTable->setRowCount(checks.size());
    for (int row = 0; row < checks.size(); ++row) {
      const auto check = checks[row].toObject();
      const QString status = check.value(QStringLiteral("status")).toString();
      QString label;
      if (status == QStringLiteral("ok")) { label = QStringLiteral("● PRONTO"); ++passed; }
      else if (status == QStringLiteral("warning")) label = QStringLiteral("● ATENÇÃO");
      else label = QStringLiteral("● BLOQUEIO");
      auto* state = cell(label);
      state->setForeground(status == QStringLiteral("ok") ? QColor(QStringLiteral("#48c78e"))
                           : status == QStringLiteral("warning") ? QColor(QStringLiteral("#e3aa55"))
                                                                  : QColor(QStringLiteral("#e66c76")));
      _readinessTable->setItem(row, 0, state);
      _readinessTable->setItem(row, 1, cell(check.value(QStringLiteral("name")).toString()));
      _readinessTable->setItem(row, 2, cell(check.value(QStringLiteral("detail")).toString()));
    }
    const int percent = checks.isEmpty() ? 0 : passed * 100 / checks.size();
    _readinessProgress->setValue(percent);
    const bool ready = root.value(QStringLiteral("ready")).toBool();
    _readinessHeadline->setText(ready ? QStringLiteral("Operação liberada")
                                      : QStringLiteral("Ação necessária antes da campanha"));
    _readinessSummary->setText(QStringLiteral("%1 de %2 verificações prontas · origem %3")
                                   .arg(passed).arg(checks.size())
                                   .arg(root.value(QStringLiteral("provider")).toString()));
    setStatus(ready ? QStringLiteral("Ambiente pronto para campanhas.")
                    : QStringLiteral("A prontidão encontrou bloqueios; consulte a matriz."));
  });

  _api.get(QStringLiteral("/api/storage/library"),
           [this](bool ok, const QJsonDocument& doc, const QString& error) {
    if (!ok) { _libraryUsage->setText(error); return; }
    const auto storage = doc.object();
    _currentLibraryRoot = storage.value(QStringLiteral("root")).toString();
    _libraryPath->setText(_currentLibraryRoot);
    _libraryUsage->setText(
        QStringLiteral("Biblioteca %1 · Ego4D %2 (%3 arquivos) · HoloAssist %4 (%5 arquivos) · livres %6")
            .arg(bytesText(static_cast<qint64>(storage.value(QStringLiteral("data_bytes")).toDouble())))
            .arg(bytesText(static_cast<qint64>(storage.value(QStringLiteral("ego4d_bytes")).toDouble())))
            .arg(storage.value(QStringLiteral("ego4d_files")).toInt())
            .arg(bytesText(static_cast<qint64>(storage.value(QStringLiteral("holoassist_bytes")).toDouble())))
            .arg(storage.value(QStringLiteral("holoassist_files")).toInt())
            .arg(bytesText(static_cast<qint64>(storage.value(QStringLiteral("free_bytes")).toDouble()))));
  });
}

void MainWindow::loadIntegrations() {
  _api.get(QStringLiteral("/api/integrations"),
           [this](bool ok, const QJsonDocument& doc, const QString& error) {
    if (!ok) return showError(QStringLiteral("Integrações indisponíveis"), error);
    const auto root = doc.object();
    const auto ego = root.value(QStringLiteral("ego4d")).toObject();
    const auto host = root.value(QStringLiteral("hostinger")).toObject();
    const auto holo = root.value(QStringLiteral("holoassist")).toObject();
    const auto runtime = root.value(QStringLiteral("runtime")).toObject();
    const auto security = root.value(QStringLiteral("security")).toObject();
    const bool egoConfigured = ego.value(QStringLiteral("configured")).toBool();
    const bool egoCatalog = ego.value(QStringLiteral("catalog_ready")).toBool();
    const bool hostConfigured = host.value(QStringLiteral("configured")).toBool();
    const bool holoReady = holo.value(QStringLiteral("catalog_ready")).toBool()
                        && holo.value(QStringLiteral("indexes_ready")).toBool();
    const bool runtimeReady = runtime.value(QStringLiteral("ffmpeg_ready")).toBool()
                           && runtime.value(QStringLiteral("ffprobe_ready")).toBool()
                           && runtime.value(QStringLiteral("browser_ready")).toBool();
    const int readyCount = int(egoConfigured) + int(egoCatalog)
                         + int(hostConfigured) + int(holoReady) + int(runtimeReady);
    _integrationsHeadline->setText(readyCount == 5
        ? QStringLiteral("Todas as conexões estão prontas")
        : QStringLiteral("%1 de 5 componentes configurados").arg(readyCount));
    _integrationsSummary->setText(readyCount == 5
        ? QStringLiteral("Credenciais, catálogos e ferramentas estão disponíveis para a operação.")
        : QStringLiteral("Conclua os itens pendentes abaixo; cada teste explica exatamente o que corrigir."));
    _integrationSecurity->setText(QStringLiteral("🔒  %1")
        .arg(security.value(QStringLiteral("provider")).toString(
            QStringLiteral("Proteção do Windows"))));
    _integrationSecurity->setToolTip(
        security.value(QStringLiteral("detail")).toString());

    const QString hint = ego.value(QStringLiteral("access_hint")).toString();
    _ego4dStatus->setText(egoConfigured
        ? QStringLiteral("● Credencial protegida %1").arg(hint)
        : QStringLiteral("● Credencial ainda não configurada"));
    _ego4dStatus->setProperty("integrationState", egoConfigured ? "ok" : "missing");
    _ego4dStatus->style()->unpolish(_ego4dStatus);
    _ego4dStatus->style()->polish(_ego4dStatus);
    _ego4dCatalog->setText(egoCatalog
        ? QStringLiteral("✓ Catálogo instalado")
        : QStringLiteral("! Catálogo pendente"));
    _ego4dAccessKey->setPlaceholderText(egoConfigured
        ? QStringLiteral("%1 — digite somente para substituir").arg(hint)
        : QStringLiteral("Access Key ID recebido por email"));
    _ego4dSecretKey->setPlaceholderText(egoConfigured
        ? QStringLiteral("Protegido — digite somente para substituir")
        : QStringLiteral("Secret Access Key"));
    const QString region = ego.value(QStringLiteral("region")).toString();
    if (_ego4dRegion->text().isEmpty() && region != QStringLiteral("automática"))
      _ego4dRegion->setPlaceholderText(region);
    _ego4dTest->setEnabled(egoConfigured);
    _ego4dPrepare->setEnabled(egoConfigured && !egoCatalog);
    if (egoConfigured && !egoCatalog && !_ego4dCatalogPreparing)
      prepareEgo4dCatalog();

    const auto hostProfiles = host.value(QStringLiteral("profiles")).toArray();
    const QString selectedHostId = _hostingerProfile->currentData().toMap()
                                       .value(QStringLiteral("id")).toString();
    {
      const QSignalBlocker blocker(_hostingerProfile);
      _hostingerProfile->clear();
      int selectedIndex = -1;
      for (const QJsonValue& value : hostProfiles) {
        const auto profile = value.toObject();
        const QString name = profile.value(QStringLiteral("name")).toString(
            QStringLiteral("Caixa Hostinger"));
        const QString hint = profile.value(QStringLiteral("token_hint")).toString();
        _hostingerProfile->addItem(
            hint.isEmpty() ? name : QStringLiteral("%1 · %2").arg(name, hint),
            profile.toVariantMap());
        if (profile.value(QStringLiteral("id")).toString() == selectedHostId)
          selectedIndex = _hostingerProfile->count() - 1;
      }
      if (_hostingerProfile->count() > 0)
        _hostingerProfile->setCurrentIndex(selectedIndex >= 0 ? selectedIndex : 0);
    }
    selectHostingerIntegration(_hostingerProfile->currentIndex());
    const int hostCount = host.value(QStringLiteral("connection_count")).toInt();
    _hostingerStatus->setText(hostConfigured
        ? QStringLiteral("● %1 caixa(s) identificada(s) e disponível(is)").arg(hostCount)
        : QStringLiteral("● Nenhuma caixa conectada"));
    _hostingerStatus->setProperty("integrationState", hostConfigured ? "ok" : "missing");
    _hostingerStatus->style()->unpolish(_hostingerStatus);
    _hostingerStatus->style()->polish(_hostingerStatus);
    if (!hostConfigured) selectHostingerIntegration(-1);

    _holoIntegrationStatus->setText(holoReady
        ? QStringLiteral("✓ Catálogo e índices instalados. Nenhuma credencial necessária.")
        : QStringLiteral("○ Será preparado automaticamente quando HoloAssist for usado."));
    _runtimeIntegrationStatus->setText(runtimeReady
        ? QStringLiteral("✓ Motor, FFmpeg, FFprobe e navegador acompanham o aplicativo.")
        : QStringLiteral("! Componente ausente; use Reparar instalação nesta tela."));
    setStatus(QStringLiteral("Estado das integrações atualizado."));
  });
}

void MainWindow::saveEgo4dIntegration() {
  QJsonObject body;
  if (!_ego4dAccessKey->text().trimmed().isEmpty())
    body.insert(QStringLiteral("access_key_id"), _ego4dAccessKey->text().trimmed());
  if (!_ego4dSecretKey->text().trimmed().isEmpty())
    body.insert(QStringLiteral("secret_access_key"), _ego4dSecretKey->text().trimmed());
  if (!_ego4dSessionToken->text().trimmed().isEmpty())
    body.insert(QStringLiteral("session_token"), _ego4dSessionToken->text().trimmed());
  if (!_ego4dRegion->text().trimmed().isEmpty())
    body.insert(QStringLiteral("region"), _ego4dRegion->text().trimmed());
  _ego4dSave->setEnabled(false);
  _ego4dSave->setText(QStringLiteral("Validando…"));
  _api.put(QStringLiteral("/api/integrations/ego4d"), body,
           [this](bool ok, const QJsonDocument&, const QString& error) {
    _ego4dSave->setEnabled(true);
    _ego4dSave->setText(QStringLiteral("Validar e salvar"));
    if (!ok) return showError(QStringLiteral("Ego4D não configurado"), error);
    _ego4dAccessKey->clear();
    _ego4dSecretKey->clear();
    _ego4dSessionToken->clear();
    setStatus(QStringLiteral("Credencial validada. Preparando o catálogo Ego4D…"));
    loadIntegrations();
    loadReadiness();
  });
}

void MainWindow::testEgo4dIntegration() {
  QJsonObject body;
  if (!_ego4dAccessKey->text().trimmed().isEmpty())
    body.insert(QStringLiteral("access_key_id"), _ego4dAccessKey->text().trimmed());
  if (!_ego4dSecretKey->text().trimmed().isEmpty())
    body.insert(QStringLiteral("secret_access_key"), _ego4dSecretKey->text().trimmed());
  if (!_ego4dSessionToken->text().trimmed().isEmpty())
    body.insert(QStringLiteral("session_token"), _ego4dSessionToken->text().trimmed());
  if (!_ego4dRegion->text().trimmed().isEmpty())
    body.insert(QStringLiteral("region"), _ego4dRegion->text().trimmed());
  _ego4dTest->setEnabled(false);
  _ego4dTest->setText(QStringLiteral("Testando…"));
  _api.post(QStringLiteral("/api/integrations/ego4d/test"), body,
            [this](bool ok, const QJsonDocument& doc, const QString& error) {
    _ego4dTest->setEnabled(true);
    _ego4dTest->setText(QStringLiteral("Testar acesso"));
    if (!ok) return showError(QStringLiteral("Teste Ego4D"), error);
    QMessageBox::information(this, QStringLiteral("Ego4D conectado"),
        doc.object().value(QStringLiteral("message")).toString(
            QStringLiteral("Acesso ao catálogo confirmado.")));
  });
}

void MainWindow::prepareEgo4dCatalog() {
  if (_ego4dCatalogPreparing) return;
  _ego4dCatalogPreparing = true;
  _ego4dPrepare->setEnabled(false);
  _ego4dPrepare->setText(QStringLiteral("Preparando…"));
  _api.post(QStringLiteral("/api/integrations/ego4d/catalog"), {},
            [this](bool ok, const QJsonDocument& doc, const QString& error) {
    _ego4dCatalogPreparing = false;
    _ego4dPrepare->setText(QStringLiteral("Preparar catálogo"));
    if (!ok) {
      _ego4dPrepare->setEnabled(true);
      return showError(QStringLiteral("Catálogo Ego4D"), error);
    }
    setStatus(doc.object().value(QStringLiteral("message")).toString(
        QStringLiteral("Catálogo Ego4D preparado.")));
    loadIntegrations();
    loadReadiness();
  });
}

void MainWindow::saveHostingerIntegration() {
  QJsonObject body;
  body.insert(QStringLiteral("auto_detect"), true);
  body.insert(QStringLiteral("token"), _hostingerToken->text().trimmed());
  _hostingerSave->setEnabled(false);
  _hostingerSave->setText(QStringLiteral("Identificando…"));
  _api.put(QStringLiteral("/api/integrations/hostinger"), body,
           [this](bool ok, const QJsonDocument& doc, const QString& error) {
    _hostingerSave->setEnabled(true);
    _hostingerSave->setText(QStringLiteral("Identificar e conectar"));
    if (!ok) return showError(QStringLiteral("Hostinger não configurada"), error);
    _hostingerToken->clear();
    const int detected = doc.object().value(QStringLiteral("detected_count")).toInt();
    setStatus(QStringLiteral("%1 caixa(s) identificada(s) e conectada(s) automaticamente.")
                  .arg(detected));
    loadBulkRegisterDomains();
    loadIntegrations();
    loadReadiness();
  });
}

void MainWindow::testHostingerIntegration() {
  QJsonObject body;
  const auto profile = _hostingerProfile->currentData().toMap();
  const QString profileId = profile.value(QStringLiteral("id")).toString();
  if (!_hostingerToken->text().trimmed().isEmpty()) {
    body.insert(QStringLiteral("token"), _hostingerToken->text().trimmed());
  } else if (!profileId.isEmpty()) {
    body.insert(QStringLiteral("profile_id"), profileId);
  }
  _hostingerTest->setEnabled(false);
  _hostingerTest->setText(QStringLiteral("Testando…"));
  _api.post(QStringLiteral("/api/integrations/hostinger/test"), body,
            [this](bool ok, const QJsonDocument& doc, const QString& error) {
    _hostingerTest->setEnabled(true);
    _hostingerTest->setText(QStringLiteral("Testar"));
    if (!ok) return showError(QStringLiteral("Teste Hostinger"), error);
    const int boxes = doc.object().value(QStringLiteral("mailboxes")).toInt();
    QMessageBox::information(this, QStringLiteral("Hostinger conectada"),
        QStringLiteral("Credencial válida · %1 caixa(s) disponível(is).").arg(boxes));
  });
}

void MainWindow::selectHostingerIntegration(int index) {
  _hostingerToken->clear();
  _hostingerToken->setPlaceholderText(
      QStringLiteral("Cole o token de outra conta Hostinger"));
  _hostingerRemove->setEnabled(index >= 0);
  _hostingerTest->setEnabled(index >= 0);
  _hostingerSave->setEnabled(false);
  _hostingerSave->setText(QStringLiteral("Identificar e conectar"));
}

void MainWindow::removeHostingerIntegration() {
  const auto profile = _hostingerProfile->currentData().toMap();
  const QString profileId = profile.value(QStringLiteral("id")).toString();
  const QString name = profile.value(QStringLiteral("name")).toString();
  if (profileId.isEmpty()) return;
  if (QMessageBox::question(
          this, QStringLiteral("Remover conexão Hostinger"),
          QStringLiteral("Remover a conexão “%1”?\n\n"
                         "Os códigos dos domínios associados deixarão de ser lidos por ela.")
              .arg(name)) != QMessageBox::Yes) return;
  _hostingerRemove->setEnabled(false);
  _api.remove(QStringLiteral("/api/integrations/hostinger/") + encoded(profileId),
              [this](bool ok, const QJsonDocument&, const QString& error) {
    if (!ok) {
      _hostingerRemove->setEnabled(true);
      return showError(QStringLiteral("Conexão não removida"), error);
    }
    setStatus(QStringLiteral("Conexão Hostinger removida."));
    loadIntegrations();
    loadReadiness();
  });
}

void MainWindow::chooseLibrary() {
  const QString selected = QFileDialog::getExistingDirectory(
      this, QStringLiteral("Escolher raiz da biblioteca QMoney"), _currentLibraryRoot);
  if (selected.isEmpty()) return;
  const QDir root(selected);
  const bool hasCatalog = QFileInfo::exists(root.filePath(QStringLiteral("data/ego4d/timed_narrations.jsonl")))
                       || QFileInfo::exists(root.filePath(QStringLiteral("data/ego4d/clip_narrations.json")))
                       || QFileInfo::exists(root.filePath(QStringLiteral("data/holoassist")));
  if (!hasCatalog) {
    return showError(QStringLiteral("Biblioteca não reconhecida"),
                     QStringLiteral("Escolha a pasta raiz que contém data\\ego4d ou data\\holoassist."));
  }
  QSettings().setValue(QStringLiteral("libraryRoot"), QDir::cleanPath(selected));
  setStatus(QStringLiteral("Reiniciando o motor com a biblioteca selecionada…"));
  restartBackend();
}

void MainWindow::exportDiagnostics() {
  const QString suggested = QDir::homePath() + QStringLiteral("/QMoney-diagnostico-%1.json")
      .arg(QDateTime::currentDateTime().toString(QStringLiteral("yyyyMMdd-HHmmss")));
  const QString path = QFileDialog::getSaveFileName(
      this, QStringLiteral("Exportar diagnóstico sanitizado"), suggested,
      QStringLiteral("Relatório JSON (*.json)"));
  if (path.isEmpty()) return;
  _diagnosticsExport->setEnabled(false);
  _api.get(QStringLiteral("/api/diagnostics"),
           [this, path](bool ok, const QJsonDocument& doc, const QString& error) {
    _diagnosticsExport->setEnabled(true);
    if (!ok) return showError(QStringLiteral("Diagnóstico não exportado"), error);
    QSaveFile output(path);
    if (!output.open(QIODevice::WriteOnly) || output.write(doc.toJson(QJsonDocument::Indented)) < 0
        || !output.commit()) {
      return showError(QStringLiteral("Diagnóstico não exportado"),
                       QStringLiteral("Não foi possível gravar o arquivo escolhido."));
    }
    setStatus(QStringLiteral("Diagnóstico sanitizado exportado com sucesso."));
  });
}

void MainWindow::loadCampaignData() {
  // Até o motor responder, mantenha ações destrutivas e uma nova partida
  // bloqueadas. Isso evita uma janela curta em que a tela ainda não conhece
  // uma campanha em execução ou encerramento.
  _campaignActive = true;
  _campaignStart->setEnabled(false);
  _campaignReset->setEnabled(false);
  _api.get(QStringLiteral("/api/accounts"), [this](bool ok, const QJsonDocument& doc, const QString& error) {
    if (!ok) return showError(QStringLiteral("Falha ao carregar contas"), error);
    QSet<QString> selectedBefore;
    const bool hadAccounts = _campaignAccounts->count() > 0;
    for (int i = 0; i < _campaignAccounts->count(); ++i)
      if (_campaignAccounts->item(i)->checkState() == Qt::Checked)
        selectedBefore.insert(_campaignAccounts->item(i)->data(Qt::UserRole).toString());
    const QSignalBlocker blocker(_campaignAccounts);
    _campaignAccounts->clear();
    for (const auto value : doc.object().value(QStringLiteral("accounts")).toArray()) {
      const auto account = value.toObject();
      auto* item = new QListWidgetItem(account.value(QStringLiteral("email")).toString());
      item->setSizeHint(QSize(0, 38));
      item->setFlags(item->flags() | Qt::ItemIsUserCheckable);
      const QString email = account.value(QStringLiteral("email")).toString();
      const bool selected = hadAccounts ? selectedBefore.contains(email)
          : _campaignDraftLoaded ? _campaignDraftAccounts.contains(email) : true;
      item->setCheckState(selected ? Qt::Checked : Qt::Unchecked);
      item->setData(Qt::UserRole, account.value(QStringLiteral("email")).toString());
      item->setHidden(!email.contains(_campaignAccountSearch->text().trimmed(), Qt::CaseInsensitive));
      _campaignAccounts->addItem(item);
    }
    _campaignAccountCount->setMaximum(qMax(1, _campaignAccounts->count()));
    if (!hadAccounts && _campaignDraftLoaded) {
      const QSignalBlocker blocker(_campaignAccountCount);
      _campaignAccountCount->setValue(_campaignDraftQuantity);
    }
    _campaignAccountCount->setEnabled(_campaignAccounts->count() > 0);
    _campaignDrawAccounts->setEnabled(_campaignAccounts->count() > 0);
    int selectedCount = 0;
    for (int i = 0; i < _campaignAccounts->count(); ++i)
      if (_campaignAccounts->item(i)->checkState() == Qt::Checked) ++selectedCount;
    const bool automatic = _campaignAccountMode->currentData().toString() != QStringLiteral("manual");
    if (_campaignAccountMode->currentData().toString().startsWith(QStringLiteral("balance_"))) {
      _campaignBalancesLoaded = false;
      drawCampaignAccounts();
    } else if (automatic && _campaignAccounts->count() > 0
        && selectedCount != _campaignAccountCount->value()) drawCampaignAccounts();
    else { updateCampaignAccountCount(); loadTasks(); }
  });
  pollCampaign();
}

void MainWindow::loadTasks() {
  _taskReload.stop();
  const int generation = ++_taskLoadGeneration;
  QString account;
  for (int i = 0; i < _campaignAccounts->count(); ++i) {
    if (_campaignAccounts->item(i)->checkState() == Qt::Checked) {
      account = _campaignAccounts->item(i)->data(Qt::UserRole).toString();
      break;
    }
  }
  if (account.isEmpty()) {
    const QSignalBlocker blocker(_campaignTasks);
    _campaignTasks->clear();
    _campaignStart->setEnabled(false);
    return;
  }
  {
    const QSignalBlocker blocker(_campaignTasks);
    _campaignTasks->clear();
    _campaignTasks->addItem(QStringLiteral("Carregando categorias…"));
  }
  _campaignStart->setEnabled(false);
  const QString path = QStringLiteral(
      "/api/tasks?email=%1&min_dur_s=%2&max_dur_s=%3&dataset=%4&content_mode=%5")
      .arg(encoded(account)).arg(_minDuration->value() * 60)
      .arg(_maxDuration->value() * 60)
      .arg(encoded(_dataset->currentData().toString()))
      .arg(encoded(_contentMode->currentData().toString()));
  _api.get(path, [this, generation](bool ok, const QJsonDocument& doc,
                                   const QString& error) {
    if (generation != _taskLoadGeneration) return;
    const QSignalBlocker blocker(_campaignTasks);
    _campaignTasks->clear();
    if (!ok) {
      _campaignTasks->addItem(QStringLiteral("Falha: ") + error);
      return;
    }
    _taskRecords = doc.object().value(QStringLiteral("tasks")).toArray();
    int compatible = 0;
    for (const auto value : _taskRecords) {
      const auto task = value.toObject();
      const bool available = task.value(QStringLiteral("available_for_duration")).toBool(false);
      QString label = task.value(QStringLiteral("name_pt")).toString();
      if (label.isEmpty()) label = task.value(QStringLiteral("name")).toString();
      if (available) label += QStringLiteral("  ·  %1 trecho(s)")
                                  .arg(task.value(QStringLiteral("clip_count")).toInt());
      if (task.value(QStringLiteral("boosted")).toBool()) label += QStringLiteral("  ·  turbinada");
      if (!available) label += QStringLiteral("  ·  sem clipe compatível");
      auto* item = new QListWidgetItem(label);
      item->setSizeHint(QSize(0, 38));
      item->setFlags(item->flags() | Qt::ItemIsUserCheckable);
      item->setData(Qt::UserRole, jsonId(task.value(QStringLiteral("id"))));
      const QString taskId = item->data(Qt::UserRole).toString();
      item->setCheckState(available && (!_campaignTaskSelectionTouched
          || _campaignSelectedTaskIds.contains(taskId)) ? Qt::Checked : Qt::Unchecked);
      if (available) ++compatible;
      if (available && task.contains(QStringLiteral("parent_video_count"))) {
        const bool cacheOnly = _contentMode->currentData().toString() == QStringLiteral("cache");
        item->setToolTip(QStringLiteral("%1 trechos de %2 vídeos de origem.\n%3\n"
                                       "Os totais consideram a categoria, os sensores e a duração escolhida.")
                             .arg(task.value(QStringLiteral("clip_count")).toInt())
                             .arg(task.value(QStringLiteral("parent_video_count")).toInt())
                             .arg(cacheOnly
                                  ? QStringLiteral("Todos já estão preparados neste computador.")
                                  : QStringLiteral("A seleção pode incluir conteúdo ainda não baixado.")));
      }
      if (!available) {
        item->setFlags(item->flags() & ~Qt::ItemIsEnabled);
        item->setToolTip(task.value(QStringLiteral("unavailable_reason")).toString(
            QStringLiteral("Categoria sem clipe compatível no conjunto escolhido.")));
      }
      _campaignTasks->addItem(item);
    }
    int selectedTasks = 0;
    for (int i = 0; i < _campaignTasks->count(); ++i)
      if (_campaignTasks->item(i)->checkState() == Qt::Checked) ++selectedTasks;
    _campaignStart->setEnabled(selectedTasks > 0 && !_campaignActive);
    if (compatible == 0) {
      setStatus(QStringLiteral("Nenhuma categoria tem clipe compatível nesta origem e duração."));
    } else {
      setStatus(QStringLiteral("%1 categoria(s) compatível(is) de %2 carregadas.")
                    .arg(compatible).arg(_campaignTasks->count()));
    }
  });
}

void MainWindow::startCampaign() {
  if (_campaignAccountMode->currentData().toString().startsWith(QStringLiteral("balance_"))
      && !_campaignBalancesLoaded)
    return showError(QStringLiteral("Saldos ainda não carregados"),
                     QStringLiteral("Aguarde a leitura dos saldos antes de iniciar."));
  if (_taskReload.isActive())
    return showError(QStringLiteral("Categorias ainda não atualizadas"),
                     QStringLiteral("Aguarde a atualização das categorias após mudar as contas."));
  QJsonArray accounts;
  QStringList selectedAccountNames;
  const QString selectionMode = _campaignAccountMode->currentData().toString();
  const QString balanceField = selectionMode.contains(QStringLiteral("available"))
      ? QStringLiteral("availableCents") : QStringLiteral("pendingCents");
  const auto balanceRecords = _campaignBalances.value(QStringLiteral("balances")).toObject();
  for (int i = 0; i < _campaignAccounts->count(); ++i) {
    auto* item = _campaignAccounts->item(i);
    if (item->checkState() == Qt::Checked) {
      const QString email = item->data(Qt::UserRole).toString();
      accounts.append(email);
      const auto amount = balanceRecords.value(email).toObject().value(balanceField);
      selectedAccountNames << (selectionMode.startsWith(QStringLiteral("balance_")) && amount.isDouble()
          ? QStringLiteral("%1 · %2").arg(email, usdMoney(static_cast<qint64>(amount.toDouble())))
          : email);
    }
  }
  if (selectionMode.startsWith(QStringLiteral("balance_"))
      && accounts.size() != _campaignAccountCount->value())
    return showError(QStringLiteral("Seleção por saldo incompleta"),
                     QStringLiteral("A quantidade pedida supera as contas com saldo confirmado. "
                                    "Reduza a quantidade ou atualize os saldos na aba Saldos."));
  QJsonArray tasks;
  for (int i = 0; i < _campaignTasks->count(); ++i) {
    auto* item = _campaignTasks->item(i);
    if (item->checkState() == Qt::Checked && !item->data(Qt::UserRole).toString().isEmpty()) {
      tasks.append(QJsonObject{{QStringLiteral("task_id"), item->data(Qt::UserRole).toString()}});
    }
  }
  if (accounts.isEmpty() || tasks.isEmpty()) {
    return showError(QStringLiteral("Seleção incompleta"),
                     QStringLiteral("Marque ao menos uma conta e uma categoria."));
  }
  QJsonObject body{
      {QStringLiteral("include_clip_plan"), true},
      {QStringLiteral("accounts"), accounts},
      {QStringLiteral("dataset"), _dataset->currentData().toString()},
      {QStringLiteral("content_mode"), _contentMode->currentData().toString()},
      {QStringLiteral("tasks"), tasks},
      {QStringLiteral("count"), 1},
      {QStringLiteral("target_hours"), _targetHours->value()},
      {QStringLiteral("account_workers"), _accountWorkers->currentData().toInt()},
      {QStringLiteral("min_dur_s"), _minDuration->value() * 60},
      {QStringLiteral("max_dur_s"), _maxDuration->value() * 60},
      {QStringLiteral("delay_mode"), _delayMode->currentData().toString()},
      {QStringLiteral("delay_s"), _delaySeconds->value()},
      {QStringLiteral("cleanup_after_upload"), _cleanupAfter->isChecked()}};
  if (_activeHours->isChecked()) {
    body.insert(QStringLiteral("active_hours"), QJsonArray{_hourStart->value(), _hourEnd->value()});
  } else {
    body.insert(QStringLiteral("active_hours"), QJsonValue::Null);
  }
  _campaignStart->setEnabled(false);
  _campaignStart->setText(QStringLiteral("Verificando campanha…"));
  _api.post(QStringLiteral("/api/campaigns/preflight"), body,
            [this, body, selectedAccountNames](bool ok, const QJsonDocument& doc, const QString& error) {
    if (!ok) {
      _campaignStart->setText(QStringLiteral("Iniciar campanha"));
      _campaignStart->setEnabled(true);
      return showError(QStringLiteral("Verificação não concluída"), error);
    }
    const auto result = doc.object();
    const auto blockers = result.value(QStringLiteral("blockers")).toArray();
    QStringList blockerLines;
    for (const auto& value : blockers) blockerLines << QStringLiteral("• ") + value.toString();
    if (!result.value(QStringLiteral("ok")).toBool() || !blockerLines.isEmpty()) {
      _campaignStart->setText(QStringLiteral("Iniciar campanha"));
      _campaignStart->setEnabled(true);
      const auto issues = result.value(QStringLiteral("account_issues")).toArray();
      const auto errors = result.value(QStringLiteral("account_errors")).toArray();
      if (!issues.isEmpty()) {
        for (const auto& value : errors)
          blockerLines.removeAll(QStringLiteral("• ") + value.toString());
      } else {
        // Compatível com motores anteriores que já retornavam o erro da conta,
        // mas cuja interface mostrava somente a contagem de falhas.
        for (const auto& value : errors) {
          const QString line = QStringLiteral("• ") + value.toString();
          if (!blockerLines.contains(line)) blockerLines << line;
        }
      }
      if (issues.isEmpty())
        return showError(QStringLiteral("Campanha não iniciada"),
                         blockerLines.join(QLatin1Char('\n')));
      std::function<void()> continueAction;
      if (result.value(QStringLiteral("can_remove_and_continue")).toBool()) {
        QJsonObject continuation = body;
        continuation.insert(QStringLiteral("preflight_id"), result.value(QStringLiteral("preflight_id")));
        continuation.insert(QStringLiteral("remove_restricted"), true);
        continueAction = [this, continuation] { submitCampaign(continuation); };
      }
      return showAccountIssues(QStringLiteral("Campanha não iniciada — verificação pendente"),
                               blockerLines, issues, continueAction);
    }

    CampaignReviewDialog review(result, selectedAccountNames, this, body);
    if (review.exec() != QDialog::Accepted) {
      _campaignStart->setText(QStringLiteral("Iniciar campanha"));
      _campaignStart->setEnabled(true);
      return;
    }

    QJsonObject approved = body;
    approved.insert(QStringLiteral("preflight_id"), result.value(QStringLiteral("preflight_id")));
    submitCampaign(approved);
  });
}

void MainWindow::submitCampaign(QJsonObject body) {
  _campaignStart->setEnabled(false);
    _campaignStart->setText(QStringLiteral("Iniciando…"));
    _api.post(QStringLiteral("/api/campaigns"), body,
              [this](bool started, const QJsonDocument& startDoc, const QString& startError) {
      _campaignStart->setText(QStringLiteral("Iniciar campanha"));
      if (!started) {
        _campaignStart->setEnabled(!_campaignActive);
        return showError(QStringLiteral("Campanha não iniciada"), startError);
      }
      if (startDoc.object().value(QStringLiteral("already_running")).toBool()) {
        _campaignActive = true;
        _campaignStart->setEnabled(false);
        _campaignReset->setEnabled(false);
        _campaignPoll.start();
        pollCampaign();
        return showError(QStringLiteral("Campanha já em andamento"),
                         QStringLiteral("A campanha anterior ainda está executando ou encerrando. "
                                        "Aguarde a conclusão antes de iniciar outra."));
      }
      auto used = QJsonDocument::fromJson(QSettings().value(
          QStringLiteral("campaign/accountLastUsed")).toByteArray()).object();
      const double startedAt = static_cast<double>(QDateTime::currentMSecsSinceEpoch());
      for (const auto value : startDoc.object().value(QStringLiteral("accounts")).toArray())
        used.insert(value.toString(), startedAt);
      QSettings().setValue(QStringLiteral("campaign/accountLastUsed"),
                           QJsonDocument(used).toJson(QJsonDocument::Compact));
      {
        const QSignalBlocker blocker(_campaignAccounts);
        for (const auto& removed : startDoc.object().value(QStringLiteral("removed_accounts")).toArray()) {
          for (int i = _campaignAccounts->count() - 1; i >= 0; --i)
            if (_campaignAccounts->item(i)->data(Qt::UserRole).toString() == removed.toString())
              delete _campaignAccounts->takeItem(i);
        }
      }
      updateCampaignAccountCount();
      _campaignActive = true;
      _campaignReset->setEnabled(false);
      _lastCampaignSeq = 0;
      _previewPoll.stop();
      _previewLogName.clear();
      _previewCheckActive = false;
      QSettings().remove(QStringLiteral("previewLogName"));
      _campaignFeed->clear();
      _campaignPoll.start();
      pollCampaign();
      setStatus(QStringLiteral("Campanha iniciada após preflight aprovado."));
    });
  }


void MainWindow::pollCampaign() {
  _api.get(QStringLiteral("/api/campaigns/current?since=%1").arg(_lastCampaignSeq),
           [this](bool ok, const QJsonDocument& doc, const QString&) {
    if (!ok) return;
    const auto snap = doc.object();
    const QString state = snap.value(QStringLiteral("state")).toString();
    const bool running = state == QStringLiteral("running") || state == QStringLiteral("stopping");
    _campaignActive = running;
    _campaignStop->setEnabled(running && state != QStringLiteral("stopping"));
    _campaignStart->setEnabled(!running);
    _campaignReset->setEnabled(!running);
    const QHash<QString, QString> stateLabels{
        {QStringLiteral("idle"), QStringLiteral("Aguardando")},
        {QStringLiteral("running"), QStringLiteral("Em andamento")},
        {QStringLiteral("stopping"), QStringLiteral("Encerrando")},
        {QStringLiteral("done"), QStringLiteral("Concluída")},
        {QStringLiteral("stopped"), QStringLiteral("Encerrada")},
        {QStringLiteral("error"), QStringLiteral("Atenção necessária")},
    };
    _campaignStage->setText(snap.value(QStringLiteral("stage")).toString(
        stateLabels.value(state, QStringLiteral("Aguardando"))));
    QString current = snap.value(QStringLiteral("current")).toString();
    if (current.isEmpty()) {
      if (state == QStringLiteral("done")) current = QStringLiteral("Resultados salvos no Histórico.");
      else if (state == QStringLiteral("stopped")) current = QStringLiteral("Parada concluída com segurança.");
      else if (state == QStringLiteral("error")) current = snap.value(QStringLiteral("error")).toString(
          QStringLiteral("A operação não foi concluída."));
      else if (running) current = QStringLiteral("Campanha em andamento…");
      else current = QStringLiteral("Nenhuma campanha em andamento.");
    }
    _campaignCurrent->setText(current);
    const auto totals = snap.value(QStringLiteral("totals")).toObject();
    const int total = totals.value(QStringLiteral("total_sends")).toInt();
    const int done = totals.value(QStringLiteral("done_sends")).toInt();
    const int successful = totals.value(QStringLiteral("ok_sends")).toInt();
    const int failed = totals.value(QStringLiteral("failed_sends")).toInt();
    const int skipped = totals.value(QStringLiteral("skipped_sends")).toInt();
    const int percent = total > 0 ? qBound(0, done * 100 / total, 100) : 0;
    _campaignProgress->setValue(percent);
    _campaignProgress->setFormat(total > 0
        ? QStringLiteral("%1 de %2 envios · %p%").arg(done).arg(total)
        : QStringLiteral("Calculando os envios…"));
    _campaignStats->setText(QStringLiteral("%1 sucesso · %2 ignorados · %3 falhas")
        .arg(successful).arg(skipped).arg(failed));
    for (const auto eventValue : snap.value(QStringLiteral("events")).toArray()) {
      const auto event = eventValue.toObject();
      if (event.value(QStringLiteral("permanently_removed")).toBool()) {
        const QString removedEmail = event.value(QStringLiteral("email")).toString();
        _accountChecks.remove(removedEmail);
        const QSignalBlocker blocker(_campaignAccounts);
        for (int i = _campaignAccounts->count() - 1; i >= 0; --i)
          if (_campaignAccounts->item(i)->data(Qt::UserRole).toString() == removedEmail)
            delete _campaignAccounts->takeItem(i);
        updateCampaignAccountCount();
        for (auto* table : {_accountsTable, _balancesTable})
          for (int row = table->rowCount() - 1; row >= 0; --row)
            if (table->item(row, 0) && table->item(row, 0)->text() == removedEmail)
              table->removeRow(row);
      }
      _lastCampaignSeq = qMax(_lastCampaignSeq, event.value(QStringLiteral("seq")).toInt());
      const QString level = event.value(QStringLiteral("level")).toString();
      const QString marker = level == QStringLiteral("success") ? QStringLiteral("✓")
                           : level == QStringLiteral("warning") ? QStringLiteral("!")
                           : level == QStringLiteral("error") ? QStringLiteral("×")
                           : QStringLiteral("•");
      const qint64 seconds = static_cast<qint64>(event.value(QStringLiteral("ts")).toDouble());
      const QString time = QDateTime::fromSecsSinceEpoch(seconds).toLocalTime()
                               .toString(QStringLiteral("HH:mm:ss"));
      const QString title = event.value(QStringLiteral("title")).toString();
      const QString detail = event.value(QStringLiteral("detail")).toString();
      if (title.isEmpty()) continue;
      QString line = QStringLiteral("%1   %2  %3").arg(time, marker, title);
      if (!detail.isEmpty()) line += QStringLiteral("\n             %1").arg(detail);
      _campaignFeed->appendPlainText(line);
    }
    if (!running) {
      _campaignPoll.stop();
      const QString logName = QFileInfo(
          snap.value(QStringLiteral("log_path")).toString()).fileName();
      if (state == QStringLiteral("done") && successful > 0 && !logName.isEmpty()
          && _previewLogName.isEmpty()) {
        _previewLogName = logName;
        QSettings().setValue(QStringLiteral("previewLogName"), logName);
        _previewPoll.start();
        pollCampaignPreviews();
      }
    }
  });
}

void MainWindow::pollCampaignPreviews() {
  if (_previewLogName.isEmpty() || _previewCheckActive) return;
  _previewCheckActive = true;
  _api.post(QStringLiteral("/api/logs/") + encoded(_previewLogName)
                + QStringLiteral("/status"), {},
            [this](bool ok, const QJsonDocument& doc, const QString& error) {
    _previewCheckActive = false;
    if (!ok) {
      if (error.contains(QStringLiteral("log não encontrado"), Qt::CaseInsensitive)) {
        _previewPoll.stop();
        _previewLogName.clear();
        QSettings().remove(QStringLiteral("previewLogName"));
        _campaignStage->setText(QStringLiteral("Atenção no histórico"));
        _campaignCurrent->setText(QStringLiteral(
            "O registro salvo para acompanhar as prévias não foi encontrado."));
        setStatus(error);
        return;
      }
      _campaignStage->setText(QStringLiteral("Aguardando o Minute"));
      _campaignCurrent->setText(QStringLiteral(
          "Os vídeos foram enviados. A consulta das prévias será repetida automaticamente."));
      setStatus(QStringLiteral("Minute ainda não respondeu sobre as prévias: %1").arg(error));
      return;
    }
    const auto summary = doc.object().value(QStringLiteral("summary")).toObject();
    const int total = summary.value(QStringLiteral("total")).toInt();
    const int ready = summary.value(QStringLiteral("ready")).toInt();
    const int pending = summary.value(QStringLiteral("pending")).toInt();
    const int unavailable = summary.value(QStringLiteral("unavailable")).toInt();
    const int errors = summary.value(QStringLiteral("errors")).toInt();
    const int transientErrors = summary.value(QStringLiteral("transient_errors")).toInt();
    const int terminalErrors = qMax(0, errors - transientErrors);
    const int finished = ready + unavailable + terminalErrors;
    const int percent = total > 0 ? qBound(0, finished * 100 / total, 100) : 100;
    _campaignProgress->setValue(percent);
    _campaignProgress->setFormat(total > 0
        ? QStringLiteral("%1 de %2 prévias prontas · %p%").arg(ready).arg(total)
        : QStringLiteral("Nenhuma sessão enviada"));
    _campaignStats->setText(QStringLiteral(
        "%1 prontas · %2 processando · %3 falhas")
        .arg(ready).arg(pending).arg(unavailable + errors));

    if (total <= 0) {
      _previewPoll.stop();
      _previewLogName.clear();
      QSettings().remove(QStringLiteral("previewLogName"));
      _campaignStage->setText(QStringLiteral("Nenhum envio confirmado"));
      _campaignCurrent->setText(QStringLiteral(
          "A campanha terminou sem uma sessão remota para acompanhar. Veja o Histórico para identificar o motivo."));
      setStatus(QStringLiteral("A campanha não possui arquivos enviados ao Minute."));
      return;
    }

    if (pending > 0 || transientErrors > 0) {
      _campaignStage->setText(transientErrors > 0
          ? QStringLiteral("Confirmando no Minute")
          : QStringLiteral("Processamento no Minute"));
      _campaignCurrent->setText(transientErrors > 0
          ? QStringLiteral(
                "%1 de %2 arquivos publicados. %3 consulta(s) falharam temporariamente e serão repetidas automaticamente.")
                .arg(ready).arg(total).arg(transientErrors)
          : QStringLiteral(
                "%1 de %2 arquivos publicados. Os vídeos já foram recebidos; o Minute está processando o restante.")
                .arg(ready).arg(total));
      setStatus(QStringLiteral("Prévias no Minute: %1 prontas, %2 processando, %3 consultas pendentes.")
                    .arg(ready).arg(pending).arg(transientErrors));
      return;
    }

    _previewPoll.stop();
    _previewLogName.clear();
    QSettings().remove(QStringLiteral("previewLogName"));
    if (unavailable > 0 || errors > 0) {
      _campaignStage->setText(QStringLiteral("Atenção nas prévias"));
      _campaignCurrent->setText(QStringLiteral(
          "%1 prévia(s) pronta(s); %2 precisam de atenção. Veja os detalhes no Histórico.")
          .arg(ready).arg(unavailable + errors));
      _campaignFeed->appendPlainText(QStringLiteral(
          "!   Minute concluiu a fila com %1 prévia(s) que precisam de atenção.")
          .arg(unavailable + errors));
    } else {
      _campaignStage->setText(QStringLiteral("Concluída"));
      _campaignCurrent->setText(QStringLiteral(
          "Todas as %1 prévias foram publicadas no Minute.").arg(ready));
      _campaignFeed->appendPlainText(QStringLiteral(
          "✓   Minute publicou todas as %1 prévias.").arg(ready));
      setStatus(QStringLiteral("Campanha concluída: todas as prévias estão prontas no Minute."));
    }
  });
}

void MainWindow::loadAccelerator() {
  const QString provider = _cacheProvider && !_cacheProvider->currentData().toString().isEmpty()
      ? _cacheProvider->currentData().toString() : QStringLiteral("holoassist");
  const QString requestedTask = _cacheTask->count() ? _cacheTask->currentText() : QString();
  QString path = QStringLiteral("/api/holo-cache?provider=%1").arg(encoded(provider));
  if (!requestedTask.isEmpty()) {
    path += QStringLiteral("&task=%1&limit=%2").arg(encoded(requestedTask)).arg(_cacheLimit->value());
  }
  if (provider == QStringLiteral("ego4d") && _cacheBudget) {
    if (_cacheBudgetLoaded)
      path += QStringLiteral("&budget_gb=%1").arg(_cacheBudget->value());
    path += QStringLiteral("&min_free_gb=%1").arg(_cacheReserve->value());
  }
  const bool live = _cachePoll.isActive()
      && _cacheCatalogSnapshot.value(QStringLiteral("provider")).toString() == provider
      && (requestedTask.isEmpty()
          || _cacheCatalogSnapshot.value(QStringLiteral("task")).toString() == requestedTask);
  if (live) path += QStringLiteral("&live=1");
  if (_cacheInFlightKey == path) return;
  if (_cacheRequestKey != path) {
    _cacheRequestKey = path;
    ++_cacheRequestId;
  }
  const auto requestId = _cacheRequestId;
  _cacheInFlightKey = path;
  _api.get(path, [this, provider, requestedTask, requestId, path, live](bool ok, const QJsonDocument& doc, const QString& error) {
    if (_cacheInFlightKey == path) _cacheInFlightKey.clear();
    if (requestId != _cacheRequestId) return;
    if (!ok) {
      _cacheState->setText(QStringLiteral("Não foi possível consultar o acelerador"));
      _cacheLastRun->setText(error);
      return setStatus(error);
    }
    if (_cacheProvider && _cacheProvider->currentData().toString() != provider) return;
    if (!requestedTask.isEmpty() && _cacheTask->currentText() != requestedTask) return;
    const auto root = doc.object();
    if (_cacheTask->count() == 0) {
      const QString preferred = root.value(QStringLiteral("default_task")).toString();
      _cacheTask->blockSignals(true);
      int select = 0;
      int index = 0;
      for (const auto task : root.value(QStringLiteral("tasks")).toArray()) {
        const QString name = task.toString();
        _cacheTask->addItem(name);
        if (name == preferred) select = index;
        ++index;
      }
      if (_cacheTask->count()) _cacheTask->setCurrentIndex(select);
      _cacheTask->blockSignals(false);
      const QString served = root.value(QStringLiteral("cache")).toObject()
          .value(QStringLiteral("task")).toString();
      if (_cacheTask->count() && !served.isEmpty() && _cacheTask->currentText() != served) {
        loadAccelerator();
        return;
      }
    }
    QJsonObject cache;
    if (live) {
      cache = _cacheCatalogSnapshot;
      cache.insert(QStringLiteral("last_run"), root.value(QStringLiteral("last_run")));
    } else {
      cache = root.value(QStringLiteral("cache")).toObject();
      cache.insert(QStringLiteral("provider"), provider);
      _cacheCatalogSnapshot = cache;
    }
    if (!live && provider == QStringLiteral("ego4d") && cache.contains(QStringLiteral("max_budget_gb"))) {
      _cacheBudget->blockSignals(true);
      _cacheBudget->setMaximum(cache.value(QStringLiteral("max_budget_gb")).toInt());
      if (!_cacheBudgetLoaded) {
        _cacheBudget->setValue(cache.value(QStringLiteral("requested_budget_gb")).toInt());
        _cacheBudgetLoaded = true;
      }
      _cacheBudget->setSuffix(_cacheBudget->value() == 0 ? QString() : QStringLiteral(" GB"));
      _cacheBudget->setToolTip(QStringLiteral("Máximo neste disco: %1 GB · livres: %2 GiB · reserva: %3 GiB")
          .arg(_cacheBudget->maximum()).arg(cache.value(QStringLiteral("free_gb")).toDouble())
          .arg(_cacheReserve->value()));
      _cacheBudget->blockSignals(false);
      _cacheStart->setText(_cacheBudget->value() == 0 ? QStringLiteral("Desativar pré-cache")
                                                    : QStringLiteral("Preparar cache"));
      _cacheBudgetHelp->setText(_cacheBudget->maximum() > 0
          ? QStringLiteral("Até %1 GB neste disco, mantendo %2 GiB livres. 0 GB desativa o pré-cache; a campanha ainda pode buscar vídeos sob demanda.")
                .arg(_cacheBudget->maximum()).arg(_cacheReserve->value())
          : QStringLiteral("Sem espaço para novo cache acima da reserva de %1 GiB. Reduza a reserva ou libere espaço no disco.")
                .arg(_cacheReserve->value()));
    }
    const auto runner = root.value(QStringLiteral("runner")).toObject();
    const int total = cache.value(QStringLiteral("total")).toInt();
    const int ready = cache.value(QStringLiteral("ready")).toInt();
    const int partial = cache.value(QStringLiteral("partial")).toInt();
    const int pending = cache.value(QStringLiteral("pending")).toInt();
    const QString state = runner.value(QStringLiteral("state")).toString();
    const bool running = state == QStringLiteral("running") || state == QStringLiteral("stopping");
    const bool runningHere = running && runner.value(QStringLiteral("provider")).toString() == provider;
    const auto lastRun = cache.value(QStringLiteral("last_run")).toObject();
    const QString lastStatus = lastRun.value(QStringLiteral("status")).toString();
    const QString catalogError = cache.value(QStringLiteral("catalog_error")).toString();
    const int runTotal = runner.value(QStringLiteral("total")).toInt();
    const int runReady = runner.value(QStringLiteral("ready")).toInt();
    const int runFailed = runner.value(QStringLiteral("failed")).toInt();
    const int processed = qMin(runTotal, runReady + runFailed);
    if (runningHere)
      _cacheState->setText(state == QStringLiteral("stopping")
          ? QStringLiteral("Parando após o clipe atual…")
          : runner.value(QStringLiteral("current")).toString(QStringLiteral("Preparando catálogo…")));
    else if (running)
      _cacheState->setText(QStringLiteral("Outro acelerador está em execução"));
    else if (state == QStringLiteral("error"))
      _cacheState->setText(QStringLiteral("Acelerador falhou: %1").arg(runner.value(QStringLiteral("error")).toString()));
    else if (lastStatus == QStringLiteral("running"))
      _cacheState->setText(QStringLiteral("Execução anterior interrompida; confira antes de retomar"));
    else if (!catalogError.isEmpty())
      _cacheState->setText(QStringLiteral("Catálogo indisponível: %1").arg(catalogError));
    else if (lastStatus == QStringLiteral("complete"))
      _cacheState->setText(lastRun.value(QStringLiteral("failed")).toInt() > 0
          ? QStringLiteral("Última preparação concluída com falhas")
          : QStringLiteral("Última preparação concluída"));
    else if (lastStatus == QStringLiteral("budget"))
      _cacheState->setText(QStringLiteral("Parou no limite de cache escolhido"));
    else if (lastStatus == QStringLiteral("disk_limit"))
      _cacheState->setText(QStringLiteral("Parou para preservar espaço livre"));
    else if (lastStatus == QStringLiteral("stopped"))
      _cacheState->setText(QStringLiteral("Preparação parada; pode retomar"));
    else
      _cacheState->setText(QStringLiteral("Nenhuma preparação em andamento"));
    const int budgetGb = cache.value(QStringLiteral("budget_gb")).toInt();
    const double usedGb = cache.value(QStringLiteral("used_gb")).toDouble();
    if (runningHere && runTotal <= 0) {
      _cacheProgress->setRange(0, 0);
      _cacheProgress->setFormat(QStringLiteral("Montando fila de clipes…"));
      _cacheNumbers->setText(QStringLiteral("Preparando catálogo e conferindo arquivos locais"));
    } else if (runningHere) {
      _cacheProgress->setRange(0, 100);
      _cacheProgress->setValue(processed * 100 / runTotal);
      _cacheProgress->setFormat(QStringLiteral("%1 de %2 clipes processados · %p%")
          .arg(processed).arg(runTotal));
      _cacheNumbers->setText(QStringLiteral("%1 pronto(s) · %2 falha(s) · clipe %3 de %4")
          .arg(runReady).arg(runFailed).arg(runner.value(QStringLiteral("index")).toInt()).arg(runTotal));
    } else {
      _cacheProgress->setRange(0, 100);
      _cacheProgress->setValue(total > 0 ? ready * 100 / total : 0);
      _cacheProgress->setFormat(total > 0
          ? QStringLiteral("%1 de %2 clipes prontos · %p%").arg(ready).arg(total)
          : QStringLiteral("Nenhum clipe planejado"));
      _cacheNumbers->setText(provider == QStringLiteral("ego4d") && budgetGb > 0
          ? QStringLiteral("%1 de %2 GB ocupados · %3 prontos · %4 parciais · %5 pendentes")
                .arg(QLocale().toString(usedGb, 'f', 1)).arg(budgetGb).arg(ready).arg(partial).arg(pending)
          : QStringLiteral("%1 prontos · %2 parciais · %3 pendentes")
                .arg(ready).arg(partial).arg(pending));
    }
    if (!running && catalogError.isEmpty() && lastStatus.isEmpty()
        && provider == QStringLiteral("ego4d") && _cacheBudget->value() == 0)
      _cacheState->setText(root.value(QStringLiteral("configured_budget_gb")).toInt() == 0
          ? QStringLiteral("Pré-cache Ego4D desativado")
          : QStringLiteral("0 GB selecionado · clique para desativar o pré-cache"));
    const double updatedAt = lastRun.value(QStringLiteral("updated_at")).toDouble();
    const QString when = updatedAt > 0
        ? QDateTime::fromSecsSinceEpoch(static_cast<qint64>(updatedAt)).toLocalTime()
              .toString(QStringLiteral("dd/MM/yyyy HH:mm"))
        : QStringLiteral("horário indisponível");
    if (lastStatus.isEmpty())
      _cacheLastRun->setText(QStringLiteral("Nenhuma preparação anterior registrada neste computador."));
    else {
      const QHash<QString, QString> statusLabels{
          {QStringLiteral("running"), QStringLiteral("em andamento")},
          {QStringLiteral("complete"), QStringLiteral("concluída")},
          {QStringLiteral("stopped"), QStringLiteral("parada pelo usuário")},
          {QStringLiteral("budget"), QStringLiteral("limite de cache atingido")},
          {QStringLiteral("disk_limit"), QStringLiteral("limite de espaço livre")},
          {QStringLiteral("provider"), QStringLiteral("pré-cache desativado")},
      };
      _cacheLastRun->setText(QStringLiteral("Última execução (%1): %2 · %3 de %4 prontos · %5 falhas · registro em %6")
          .arg(lastRun.value(QStringLiteral("task")).toString(QStringLiteral("tarefa não registrada")))
          .arg(lastStatus == QStringLiteral("running") && !runningHere
                   ? QStringLiteral("interrompida") : statusLabels.value(lastStatus, lastStatus),
               QString::number(lastRun.value(QStringLiteral("ready")).toInt()),
               QString::number(lastRun.value(QStringLiteral("total")).toInt()),
               QString::number(lastRun.value(QStringLiteral("failed")).toInt()), when));
      const int reclaimedFiles = lastRun.value(QStringLiteral("reclaimed_files")).toInt();
      if (reclaimedFiles > 0)
        _cacheLastRun->setText(_cacheLastRun->text()
            + QStringLiteral("\nCache obsoleto recuperado: %1 arquivo(s), %2 GB.")
                  .arg(reclaimedFiles)
                  .arg(QLocale().toString(
                      lastRun.value(QStringLiteral("reclaimed_bytes")).toDouble() / (1024.0 * 1024 * 1024), 'f', 1)));
      const auto errors = lastRun.value(QStringLiteral("errors")).toArray();
      if (!errors.isEmpty())
        _cacheLastRun->setText(_cacheLastRun->text() + QStringLiteral("\nÚltima falha: ")
            + errors.last().toObject().value(QStringLiteral("error")).toString());
    }
    const bool disablingCache = provider == QStringLiteral("ego4d")
        && _cacheBudget && _cacheBudget->value() == 0;
    _cacheStart->setEnabled(!running && (catalogError.isEmpty() || disablingCache));
    if (!disablingCache && provider == QStringLiteral("ego4d")
        && (lastStatus == QStringLiteral("running") || lastStatus == QStringLiteral("stopped")
            || lastStatus == QStringLiteral("disk_limit") || lastStatus == QStringLiteral("budget")))
      _cacheStart->setText(QStringLiteral("Retomar preparação"));
    _cacheStop->setEnabled(running && state != QStringLiteral("stopping"));
    if (running) _cachePoll.start(); else _cachePoll.stop();
    if (live && !running) QTimer::singleShot(0, this, &MainWindow::loadAccelerator);
  });
}

void MainWindow::startAccelerator() {
  if (_cacheTask->currentText().isEmpty()) return;
  const QString provider = _cacheProvider && !_cacheProvider->currentData().toString().isEmpty()
      ? _cacheProvider->currentData().toString() : QStringLiteral("holoassist");
  QJsonObject body{{QStringLiteral("provider"), provider},
                   {QStringLiteral("task"), _cacheTask->currentText()},
                   {QStringLiteral("min_free_gb"), _cacheReserve->value()}};
  if (_cacheLimit->value() > 0) body.insert(QStringLiteral("limit"), _cacheLimit->value());
  else body.insert(QStringLiteral("limit"), QJsonValue::Null);
  if (provider == QStringLiteral("ego4d") && _cacheBudget)
    body.insert(QStringLiteral("budget_gb"), _cacheBudget->value());
  _cacheStart->setEnabled(false);
  _api.post(QStringLiteral("/api/holo-cache/start"), body,
            [this](bool ok, const QJsonDocument&, const QString& error) {
    if (!ok) {
      _cacheStart->setEnabled(true);
      return showError(QStringLiteral("Acelerador não iniciado"), error);
    }
    _cachePoll.start();
    loadAccelerator();
  });
}

void MainWindow::setAccountTransferBusy(bool busy) {
  _accountTransferBusy = busy;
  busy = busy || _orgMigrationRunning;
  _accountsImport->setEnabled(!busy);
  _accountsExport->setEnabled(!busy);
  _accountsExportSelected->setEnabled(!busy);
  _accountAdd->setEnabled(!busy);
  _accountRegister->setEnabled(!busy);
  _accountsCheckAll->setEnabled(!busy);
  _accountsTable->setEnabled(!busy);
  _accountsMigrate->setEnabled(!busy && _accountsTable->rowCount() > 0);
}

void MainWindow::startOrgMigration() {
  setAccountTransferBusy(true);
  _migrationStatus->setText(QStringLiteral("Iniciando atualização das organizações…"));
  _api.post(QStringLiteral("/api/accounts/migration"), {},
    [this](bool ok, const QJsonDocument& doc, const QString& error) {
      if (ok) {
        _orgMigrationSnapshot = doc.object();
        _orgMigrationRunning = true;
      }
      setAccountTransferBusy(false);
      if (!ok) {
        _migrationStatus->setText(error);
        pollOrgMigration();
        return showError(QStringLiteral("Migração não iniciada"), error);
      }
      _orgMigrationPoll.start();
      pollOrgMigration();
    });
}

void MainWindow::pollOrgMigration() {
  if (_orgMigrationPolling) return;
  _orgMigrationPolling = true;
  _api.get(QStringLiteral("/api/accounts/migration"),
    [this](bool ok, const QJsonDocument& doc, const QString&) {
      _orgMigrationPolling = false;
      if (!ok) {
        if (_orgMigrationRunning)
          _migrationStatus->setText(QStringLiteral("Reconectando ao progresso da migração…"));
        return;
      }
      const bool wasRunning = _orgMigrationRunning;
      _orgMigrationSnapshot = doc.object();
      const QString state = _orgMigrationSnapshot.value("state").toString();
      _orgMigrationRunning = state == QStringLiteral("running");
      setAccountTransferBusy(_accountTransferBusy);
      _migrationReport->setEnabled(!_orgMigrationSnapshot.value("results").toArray().isEmpty());
      if (_orgMigrationRunning) {
        _orgMigrationPoll.start();
        _migrationStatus->setText(QStringLiteral("Atualizando %1 de %2 · %3 · mantenha o QMoney aberto")
            .arg(_orgMigrationSnapshot.value("completed").toInt())
            .arg(_orgMigrationSnapshot.value("total").toInt())
            .arg(_orgMigrationSnapshot.value("current_email").toString()));
      } else {
        _orgMigrationPoll.stop();
        const auto counts = _orgMigrationSnapshot.value("counts").toObject();
        if (state == QStringLiteral("completed"))
          _migrationStatus->setText(QStringLiteral("%1 atualizadas · %2 já corretas · %3 Claru preservadas · %4 com pendências")
              .arg(counts.value("migrated").toInt()).arg(counts.value("already").toInt())
              .arg(counts.value("skipped").toInt())
              .arg(counts.value("error").toInt() + counts.value("restricted").toInt()));
        else if (state == QStringLiteral("interrupted"))
          _migrationStatus->setText(QStringLiteral("Migração interrompida. Confira o relatório e execute novamente para concluir."));
        else if (state == QStringLiteral("error"))
          _migrationStatus->setText(_orgMigrationSnapshot.value("error").toString(QStringLiteral("Migração interrompida. Tente novamente.")));
        if (wasRunning) {
          _accountChecks.clear();
          loadAccounts();
        }
      }
    });
}

void MainWindow::showOrgMigrationReport() {
  QDialog dialog(this);
  dialog.setWindowTitle(QStringLiteral("Migração das organizações"));
  dialog.resize(850, 440);
  auto* layout = new QVBoxLayout(&dialog);
  auto* table = new QTableWidget(0, 3, &dialog);
  configureTable(table);
  table->setHorizontalHeaderLabels({QStringLiteral("Conta"), QStringLiteral("Tipo"), QStringLiteral("Resultado")});
  table->horizontalHeader()->setSectionResizeMode(0, QHeaderView::Stretch);
  table->horizontalHeader()->setSectionResizeMode(1, QHeaderView::ResizeToContents);
  table->horizontalHeader()->setSectionResizeMode(2, QHeaderView::Stretch);
  const auto report = _orgMigrationSnapshot;
  for (const auto value : report.value("results").toArray()) {
    const auto result = value.toObject();
    const int row = table->rowCount();
    table->insertRow(row);
    table->setItem(row, 0, cell(result.value("email").toString()));
    table->setItem(row, 1, cell(result.value("account_kind").toString() == "claru" ? QStringLiteral("Claru") : QStringLiteral("Crowtado")));
    auto* message = cell(result.value("message").toString());
    const auto issue = result.value("issue").toObject();
    message->setToolTip(issue.value("action").toString());
    table->setItem(row, 2, message);
  }
  layout->addWidget(table);
  auto* buttons = new QDialogButtonBox(QDialogButtonBox::Close, &dialog);
  auto* save = buttons->addButton(QStringLiteral("Salvar relatório"), QDialogButtonBox::ActionRole);
  connect(buttons, &QDialogButtonBox::rejected, &dialog, &QDialog::reject);
  connect(save, &QPushButton::clicked, &dialog, [this, report, &dialog] {
    QString path = QFileDialog::getSaveFileName(&dialog, QStringLiteral("Salvar relatório"),
        QStringLiteral("QMoney-migracao.json"), QStringLiteral("JSON (*.json)"));
    if (path.isEmpty()) return;
    if (!path.endsWith(".json", Qt::CaseInsensitive)) path += QStringLiteral(".json");
    QSaveFile output(path);
    const auto bytes = QJsonDocument(report).toJson(QJsonDocument::Indented);
    if (!output.open(QIODevice::WriteOnly) || output.write(bytes) != bytes.size() || !output.commit())
      showError(QStringLiteral("Relatório não salvo"), QStringLiteral("Confira o espaço e as permissões da pasta."));
  });
  layout->addWidget(buttons);
  dialog.exec();
}

void MainWindow::importAccounts() {
  const QString path = QFileDialog::getOpenFileName(this, QStringLiteral("Importar contas"),
      QString(), QStringLiteral("Contas JSON (*.json)"));
  if (path.isEmpty()) return;
  QFile file(path);
  if (!file.open(QIODevice::ReadOnly) || file.size() > 10 * 1024 * 1024) {
    QMessageBox::warning(this, QStringLiteral("Importação"),
        QStringLiteral("Não foi possível ler o arquivo. O limite é de 10 MB."));
    return;
  }
  QByteArray bytes = file.readAll();
  if (bytes.startsWith("\xEF\xBB\xBF")) bytes.remove(0, 3);
  const QString content = QString::fromUtf8(bytes);
  if (content.toUtf8() != bytes) {
    QMessageBox::warning(this, QStringLiteral("Importação"), QStringLiteral("Salve o JSON usando UTF-8."));
    return;
  }
  setAccountTransferBusy(true);
  setStatus(QStringLiteral("Validando arquivo de contas…"));
  _api.post(QStringLiteral("/api/accounts/import"), {{"content", content}, {"apply", false}},
    [this, content](bool ok, const QJsonDocument& doc, const QString& error) {
      if (!ok) {
        setAccountTransferBusy(false);
        QMessageBox::warning(this, QStringLiteral("Importação"), error);
        return;
      }
      const auto counts = doc.object().value("counts").toObject();
      QStringList details;
      for (const auto value : doc.object().value("results").toArray()) {
        const auto row = value.toObject();
        details << QStringLiteral("Linha %1 · %2 · %3").arg(row.value("row").toInt())
            .arg(row.value("email").toString(), row.value("message").toString());
      }
      const int fresh = counts.value("new").toInt();
      QMessageBox review(QMessageBox::Information, QStringLiteral("Revisar importação"),
          QStringLiteral("%1 novas · %2 duplicadas · %3 inválidas\n\n"
                         "Contas existentes serão preservadas. Somente as novas e válidas serão importadas. "
                         "A validade dos acessos será conferida em Verificar todas.")
              .arg(fresh).arg(counts.value("duplicate").toInt()).arg(counts.value("invalid").toInt()),
          fresh > 0 ? QMessageBox::Ok | QMessageBox::Cancel : QMessageBox::Close, this);
      review.setDetailedText(details.join('\n'));
      if (fresh > 0) review.button(QMessageBox::Ok)->setText(QStringLiteral("Importar %1 conta(s)").arg(fresh));
      if (review.exec() != QMessageBox::Ok || fresh == 0) {
        setAccountTransferBusy(false);
        return;
      }
      setStatus(QStringLiteral("Importando contas… Aguarde a conclusão antes de fechar o aplicativo."));
      _api.post(QStringLiteral("/api/accounts/import"), {{"content", content}, {"apply", true}},
        [this](bool saved, const QJsonDocument& result, const QString& failure) {
          setAccountTransferBusy(false);
          loadAccounts();
          if (!saved) {
            QMessageBox::warning(this, QStringLiteral("Importação"),
                failure + QStringLiteral("\nAtualize a lista antes de tentar novamente; contas já importadas serão ignoradas."));
            return;
          }
          const auto count = result.object().value("counts").toObject();
          QStringList details;
          for (const auto value : result.object().value("results").toArray()) {
            const auto row = value.toObject();
            details << QStringLiteral("Linha %1 · %2 · %3").arg(row.value("row").toInt())
                .arg(row.value("email").toString(), row.value("message").toString());
          }
          const QString summary = QStringLiteral("%1 importadas · %2 duplicadas preservadas · %3 inválidas · %4 falhas")
              .arg(count.value("imported").toInt()).arg(count.value("duplicate").toInt())
              .arg(count.value("invalid").toInt()).arg(count.value("error").toInt());
          QMessageBox report(QMessageBox::Information, QStringLiteral("Resultado da importação"),
              summary + QStringLiteral("\n\nUse Verificar todas para conferir os acessos importados."), QMessageBox::Ok, this);
          report.setDetailedText(details.join('\n'));
          setStatus(summary);
          report.exec();
        });
    });
}

void MainWindow::exportAccounts(bool selectedOnly) {
  QJsonObject body;
  if (selectedOnly) {
    QJsonArray emails;
    for (const auto& index : _accountsTable->selectionModel()->selectedRows())
      emails.append(_accountsTable->item(index.row(), 0)->text());
    if (emails.isEmpty()) {
      QMessageBox::information(this, QStringLiteral("Exportação"), QStringLiteral("Selecione uma ou mais contas na tabela."));
      return;
    }
    body.insert("emails", emails);
  }
  QString path = QFileDialog::getSaveFileName(this,
      QStringLiteral("Salvar backup de contas — contém credenciais de acesso"),
      QStringLiteral("QMoney-contas-%1.json").arg(QDateTime::currentDateTime().toString("yyyyMMdd-HHmmss")),
      QStringLiteral("Contas JSON (*.json)"));
  if (path.isEmpty()) return;
  if (!path.endsWith(".json", Qt::CaseInsensitive)) path += QStringLiteral(".json");
  setAccountTransferBusy(true);
  _api.post(QStringLiteral("/api/accounts/export"), body,
    [this, path](bool ok, const QJsonDocument& doc, const QString& error) {
      setAccountTransferBusy(false);
      if (!ok) { QMessageBox::warning(this, QStringLiteral("Exportação"), error); return; }
      QSaveFile output(path);
      const auto bytes = doc.toJson(QJsonDocument::Indented);
      if (!output.open(QIODevice::WriteOnly) || output.write(bytes) != bytes.size() || !output.commit()) {
        QMessageBox::warning(this, QStringLiteral("Exportação"),
            QStringLiteral("Não foi possível salvar o backup completo. Confira as permissões e o espaço disponível."));
        return;
      }
      QFile::setPermissions(path, QFileDevice::ReadOwner | QFileDevice::WriteOwner);
      const int count = doc.object().value("accounts").toArray().size();
      setStatus(QStringLiteral("%1 conta(s) exportada(s).").arg(count));
      QMessageBox::information(this, QStringLiteral("Backup salvo"),
          QStringLiteral("%1 conta(s) exportada(s) para:\n%2\n\nO JSON contém credenciais de acesso. Guarde em local seguro.")
              .arg(count).arg(QDir::toNativeSeparators(path)));
    });
}

void MainWindow::loadAccounts() {
  pollOrgMigration();
  _api.get(QStringLiteral("/api/accounts"), [this](bool ok, const QJsonDocument& doc, const QString& error) {
    if (!ok) {
      setAccountTransferBusy(_accountTransferBusy);
      return showError(QStringLiteral("Falha ao carregar contas"), error);
    }
    const auto accounts = doc.object().value(QStringLiteral("accounts")).toArray();
    _accountsTable->setRowCount(accounts.size());
    int row = 0;
    for (const auto value : accounts) {
      const auto account = value.toObject();
      const QString email = account.value(QStringLiteral("email")).toString();
      _accountsTable->setItem(row, 0, cell(email));
      if (!_pendingAccountFocus.isEmpty() && _pendingAccountFocus == email) {
        _accountsTable->selectRow(row);
        _accountsTable->scrollToItem(_accountsTable->item(row,0));
        _pendingAccountFocus.clear();
      }
      const bool isClaru = account.value(QStringLiteral("account_kind")).toString() == QStringLiteral("claru");
      const QString org = account.value(QStringLiteral("org_key")).toString();
      const QString kind = isClaru ? QStringLiteral("Claru") : QStringLiteral("Crowtado");
      auto* orgCell = cell(kind + QStringLiteral(" · ") + account.value("org_name").toString(QStringLiteral("Não verificada")));
      orgCell->setToolTip(kind + QStringLiteral("\n") + org);
      _accountsTable->setItem(row, 1, orgCell);
      const qint64 expiry = static_cast<qint64>(account.value(QStringLiteral("expires_at")).toDouble());
      const auto lastCheck = _accountChecks.contains(email)
          ? _accountChecks.value(email) : account.value(QStringLiteral("last_check")).toObject();
      QString state = expiry > QDateTime::currentSecsSinceEpoch()
          ? QStringLiteral("Token no prazo · não verificada")
          : QStringLiteral("Acesso precisa ser verificado");
      if (!lastCheck.isEmpty()) {
        state = lastCheck.value(QStringLiteral("status_label")).toString(
                    lastCheck.value(QStringLiteral("status")).toString() == QStringLiteral("active")
                        ? QStringLiteral("Acesso verificado") : QStringLiteral("Verificação inconclusiva"))
                + QStringLiteral(" · ") + friendlyDate(lastCheck.value(QStringLiteral("checked_at")).toString());
      }
      auto* statusCell = cell(state);
      QString checkDetails = state + QStringLiteral("\n") + lastCheck.value(QStringLiteral("error")).toString();
      if (!lastCheck.value("last_success_at").toString().isEmpty())
        checkDetails += QStringLiteral("\nÚltimo acesso confirmado: ") + friendlyDate(lastCheck.value("last_success_at").toString());
      statusCell->setToolTip(checkDetails);
      _accountsTable->setItem(row, 2, statusCell);
      auto* actions = new QWidget;
      auto* actionsLayout = new QHBoxLayout(actions);
      actionsLayout->setContentsMargins(5, 5, 5, 5);
      actionsLayout->setSpacing(7);
      auto* check = new QPushButton(QStringLiteral("Verificar"));
      check->setMinimumSize(86, 32);
      connect(check, &QPushButton::clicked, this, [this, email] {
        _api.post(QStringLiteral("/api/accounts/") + encoded(email) + QStringLiteral("/check"), {},
          [this, email](bool ok, const QJsonDocument& doc, const QString& error) {
            auto check = doc.object();
            if (!check.contains(QStringLiteral("status"))) {
              check.insert(QStringLiteral("status"), QStringLiteral("inconclusive"));
              check.insert(QStringLiteral("status_label"), QStringLiteral("Verificação inconclusiva"));
              check.insert(QStringLiteral("error"), error);
            }
            check.insert(QStringLiteral("checked_at"), QDateTime::currentDateTime().toString(Qt::ISODate));
            _accountChecks.insert(email, check);
            if (!ok) {
              const auto issue = check.value(QStringLiteral("issue")).toObject();
              showAccountIssues(QStringLiteral("Conta não verificada"),
                  issue.isEmpty() ? QStringList{email + QStringLiteral(": ") + error} : QStringList{},
                  issue.isEmpty() ? QJsonArray{} : QJsonArray{issue});
            } else setStatus(QStringLiteral("Acesso de %1 verificado agora.").arg(email));
            loadAccounts();
          });
      });
      auto* remove = new QPushButton(QStringLiteral("Remover"));
      remove->setMinimumSize(86, 32);
      connect(remove, &QPushButton::clicked, this, [this, email] {
        removeAccount(email);
      });
      auto* reveal = new QPushButton(QStringLiteral("Ver senha"));
      reveal->setMinimumSize(100, 32);
      reveal->setEnabled(account.value(QStringLiteral("has_password")).toBool());
      reveal->setToolTip(reveal->isEnabled()
          ? QStringLiteral("Mostrar a senha salva para acesso manual.")
          : QStringLiteral("Esta conta não possui senha salva."));
      connect(reveal, &QPushButton::clicked, this, [this, email] {
        _api.post(QStringLiteral("/api/accounts/password"),
                  {{QStringLiteral("email"), email}},
                  [this, email](bool ok, const QJsonDocument& doc, const QString& error) {
          if (!ok) return showError(QStringLiteral("Senha indisponível"), error);
          QMessageBox dialog(this);
          dialog.setWindowTitle(QStringLiteral("Senha da conta"));
          dialog.setTextFormat(Qt::PlainText);
          dialog.setText(QStringLiteral("%1\n\nSenha: %2")
              .arg(email, doc.object().value(QStringLiteral("password")).toString()));
          dialog.setTextInteractionFlags(Qt::TextSelectableByMouse);
          dialog.exec();
        });
      });
      actionsLayout->addWidget(check);
      actionsLayout->addWidget(reveal);
      actionsLayout->addWidget(remove);
      _accountsTable->setCellWidget(row, 3, actions);
      ++row;
    }
    setAccountTransferBusy(_accountTransferBusy);
    if (_accountsCheckAll) _accountsCheckAll->setEnabled(!accounts.isEmpty() && !_accountTransferBusy && !_orgMigrationRunning);
    setStatus(QStringLiteral("%1 conta(s) cadastrada(s).").arg(accounts.size()));
  });
}

void MainWindow::removeAccount(const QString& email, std::function<void()> onRemoved) {
  if (QMessageBox::question(this, QStringLiteral("Remover conta"),
        QStringLiteral("Remover definitivamente %1 deste QMoney?\n\n"
                       "Falhas de verificação, saldo ou envio não comprovam que a conta está inválida. "
                       "Para corrigir o acesso, verifique novamente ou conecte a mesma conta com a senha.\n\n"
                       "O acesso salvo será apagado e não voltará ao reiniciar. "
                       "O histórico de campanhas será preservado.").arg(email),
        QMessageBox::Yes | QMessageBox::No, QMessageBox::No)
      != QMessageBox::Yes) return;
  _api.remove(QStringLiteral("/api/accounts/") + encoded(email),
              [this, email, onRemoved = std::move(onRemoved)](
                  bool ok, const QJsonDocument&, const QString& error) mutable {
    if (!ok) return showError(QStringLiteral("Conta não removida"), error);
    _accountChecks.remove(email);
    setStatus(QStringLiteral("%1 removida definitivamente deste QMoney.").arg(email));
    loadAccounts();
    if (onRemoved) onRemoved();
  });
}

void MainWindow::checkAllAccounts() {
  _accountsCheckAll->setEnabled(false);
  _accountsCheckAll->setText(QStringLiteral("Verificando…"));
  setStatus(QStringLiteral("Verificando todas as contas em paralelo…"));
  _api.post(QStringLiteral("/api/accounts/check-all"), {},
            [this](bool ok, const QJsonDocument& doc, const QString& error) {
    _accountsCheckAll->setText(QStringLiteral("Verificar todas"));
    if (!ok) {
      _accountsCheckAll->setEnabled(true);
      return showError(QStringLiteral("Verificação não concluída"), error);
    }
    const auto result = doc.object();
    const int total = result.value(QStringLiteral("total")).toInt();
    const int active = result.value(QStringLiteral("active")).toInt();
    const int disabled = result.value(QStringLiteral("disabled")).toArray().size();
    const int errors = result.value(QStringLiteral("errors")).toArray().size();
    QJsonArray issues;
    QStringList legacyErrors;
    for (const auto& value : result.value(QStringLiteral("results")).toArray()) {
      auto check = value.toObject();
      const QString email = check.value(QStringLiteral("email")).toString();
      check.insert(QStringLiteral("checked_at"), QDateTime::currentDateTime().toString(Qt::ISODate));
      _accountChecks.insert(email, check);
      if (check.value(QStringLiteral("status")).toString() != QStringLiteral("active")) {
        const auto issue = check.value(QStringLiteral("issue")).toObject();
        if (!issue.isEmpty()) issues.append(issue);
        else legacyErrors << email + QStringLiteral(": ") + check.value(QStringLiteral("error")).toString();
      }
    }
    setStatus(QStringLiteral("Verificação: %1 acessos confirmados · %2 restrições confirmadas · %3 pendências · %4 no total. Não remova contas por falhas temporárias.")
                  .arg(active).arg(disabled).arg(errors).arg(total));
    loadAccounts();
    if (!issues.isEmpty() || !legacyErrors.isEmpty())
      showAccountIssues(QStringLiteral("Verificação concluída com pendências"), legacyErrors, issues);
  });
}

void MainWindow::addAccount(bool registerNew) {
  const QString email = _accountEmail->text().trimmed();
  const QString password = _accountPassword->text();
  if (email.isEmpty() || password.isEmpty()) {
    return showError(QStringLiteral("Dados incompletos"), QStringLiteral("Informe email e senha."));
  }
  _accountAdd->setEnabled(false);
  _accountRegister->setEnabled(false);
  const QString endpoint = registerNew ? QStringLiteral("/api/accounts/register")
                                       : QStringLiteral("/api/accounts");
  if (registerNew) {
    setStatus(QStringLiteral("Registrando %1 — fluxo completo (Crowtado + Minute)… pode levar alguns minutos.")
                  .arg(email));
  }
  _api.post(endpoint, {{QStringLiteral("email"), email}, {QStringLiteral("password"), password}},
            [this, email, registerNew](bool ok, const QJsonDocument& doc, const QString& error) {
    _accountAdd->setEnabled(true);
    _accountRegister->setEnabled(true);
    if (!ok) return showError(QStringLiteral("Conta não conectada"), error);
    _accountChecks.remove(email);
    _accountEmail->clear();
    _accountPassword->clear();
    if (registerNew) {
      // Montar resumo das etapas para o status
      const auto steps = doc.object().value(QStringLiteral("steps")).toObject();
      QStringList summary;
      const QStringList stepNames = {
          QStringLiteral("Credenciais"), QStringLiteral("Crowtado"),
          QStringLiteral("Demografia"), QStringLiteral("Minute"),
          QStringLiteral("Vínculo"), QStringLiteral("Validação"),
      };
      const QStringList stepKeys = {
          QStringLiteral("save_partial"), QStringLiteral("crowtado_signup"),
          QStringLiteral("demographics"), QStringLiteral("minute_register"),
          QStringLiteral("link_minute"), QStringLiteral("validate"),
      };
      for (int s = 0; s < stepKeys.size(); ++s) {
        const auto stepObj = steps.value(stepKeys.at(s)).toObject();
        const QString status = stepObj.value(QStringLiteral("status")).toString();
        if (status == QStringLiteral("ok")) summary << QStringLiteral("✓ ") + stepNames.at(s);
        else if (status == QStringLiteral("skip")) summary << QStringLiteral("↷ ") + stepNames.at(s);
        else summary << QStringLiteral("✗ ") + stepNames.at(s);
      }
      setStatus(QStringLiteral("Conta %1 registrada — %2").arg(email, summary.join(QStringLiteral(" · "))));
    } else {
      setStatus(QStringLiteral("Conta conectada."));
    }
    loadAccounts();
  });
}

void MainWindow::loadBulkRegisterDomains() {
  if (!_backendReady || _bulkRegisterPolling || _bulkRegisterStarting) return;
  const int revision = ++_bulkRegisterDomainsRevision;
  const QString selectedDomain = _bulkRegisterDomain->currentData().toString();
  ++_bulkRegisterPreflightRevision;
  const QSignalBlocker blocker(_bulkRegisterDomain);
  _bulkRegisterStart->setEnabled(false);
  _bulkRegisterDomain->clear();
  _bulkRegisterDomain->addItem(QStringLiteral("carregando domínios…"));
  _bulkRegisterDomain->setEnabled(false);
  _api.get(QStringLiteral("/api/accounts/domains"), [this, revision, selectedDomain](bool ok, const QJsonDocument& doc, const QString& error) {
    if (revision != _bulkRegisterDomainsRevision || _bulkRegisterPolling || _bulkRegisterStarting) return;
    const QSignalBlocker blocker(_bulkRegisterDomain);
    _bulkRegisterDomain->clear();
    _bulkRegisterWebmail->hide();
    if (!ok) {
      _bulkRegisterDomain->addItem(QStringLiteral("não foi possível carregar"));
      _bulkRegisterDomain->setEnabled(false);
      _bulkRegisterStatus->setText(QStringLiteral("Falha ao carregar domínios: ") + error);
      return;
    }
    const auto root = doc.object();
    const auto domains = root.value(QStringLiteral("domains")).toArray();
    const QString webmailUrl = root.value(QStringLiteral("webmail_url")).toString();
    if (domains.isEmpty()) {
      _bulkRegisterDomain->addItem(QStringLiteral("configure um domínio em Integrações"));
      _bulkRegisterDomain->setEnabled(false);
      _bulkRegisterStart->setEnabled(false);
      const QString warning = root.value(QStringLiteral("warning")).toString();
      _bulkRegisterStatus->setText(warning.isEmpty()
          ? QStringLiteral("Nenhum domínio disponível. Em Integrações, use Identificar e conectar com o token da API Mail da Hostinger; depois clique em Atualizar domínios.")
          : warning);
      return;
    }
    for (const auto value : domains) {
      const auto entry = value.toObject();
      const QString domain = entry.value(QStringLiteral("domain")).toString();
      const QString profile = entry.value(QStringLiteral("profile_name")).toString();
      _bulkRegisterDomain->addItem(profile.isEmpty() ? domain : QStringLiteral("%1 (%2)").arg(domain, profile), domain);
    }
    const int previousIndex = _bulkRegisterDomain->findData(selectedDomain);
    if (previousIndex >= 0) _bulkRegisterDomain->setCurrentIndex(previousIndex);
    _bulkRegisterDomain->setEnabled(true);
    if (!webmailUrl.isEmpty()) {
      _bulkRegisterWebmail->setText(
          QStringLiteral("Conferir caixa de entrada: <a href=\"%1\">%1</a>")
              .arg(webmailUrl));
      _bulkRegisterWebmail->show();
    }
    checkBulkRegisterDomain();
  });
}

void MainWindow::checkBulkRegisterDomain() {
    if (!_backendReady || _bulkRegisterPolling || _bulkRegisterStarting) return;
    const int revision = ++_bulkRegisterPreflightRevision;
    const QString domain = _bulkRegisterDomain->currentData().toString();
    _bulkRegisterStart->setEnabled(false);
    if (domain.isEmpty()) return;
    _bulkRegisterStatus->setText(QStringLiteral("Validando dependências (Hostinger, Chrome, APIs)…"));
    _api.get(QStringLiteral("/api/accounts/bulk-register/preflight?domain=")
                 + QString::fromLatin1(QUrl::toPercentEncoding(domain)),
             [this, revision](bool ok, const QJsonDocument& doc, const QString& error) {
      if (revision != _bulkRegisterPreflightRevision || _bulkRegisterPolling || _bulkRegisterStarting) return;
      if (!ok) {
        _bulkRegisterStatus->setText(QStringLiteral("Preflight falhou: ") + error);
        return;
      }
      const auto root = doc.object();
      const bool ready = root.value(QStringLiteral("ready")).toBool();
      const auto checks = root.value(QStringLiteral("checks")).toObject();
      QStringList issues;
      const QStringList checkKeys = {
          QStringLiteral("hostinger"), QStringLiteral("chrome"),
          QStringLiteral("crowtado_api"), QStringLiteral("minute_api"),
      };
      const QStringList checkNames = {
          QStringLiteral("Hostinger"), QStringLiteral("Chrome"),
          QStringLiteral("Crowtado API"), QStringLiteral("Minute API"),
      };
      for (int i = 0; i < checkKeys.size(); ++i) {
        const auto check = checks.value(checkKeys.at(i)).toObject();
        if (!check.value(QStringLiteral("ok")).toBool()) {
          issues << QStringLiteral("%1: %2")
                        .arg(checkNames.at(i), check.value(QStringLiteral("detail")).toString());
        }
      }
      if (ready) {
        _bulkRegisterStatus->setText(QStringLiteral(
            "Todas as dependências OK. Pronto para criar contas."));
        _bulkRegisterStart->setEnabled(true);
      } else {
        _bulkRegisterStatus->setText(
            QStringLiteral("Dependências com problema: %1").arg(issues.join(QStringLiteral("; "))));
      }
    });
}

void MainWindow::startBulkRegister() {
  if (!_backendReady || _bulkRegisterPolling || _bulkRegisterStarting) return;
  const QString domain = _bulkRegisterDomain->currentData().toString();
  if (domain.isEmpty()) {
    return showError(QStringLiteral("Domínio indisponível"),
                     QStringLiteral("Configure um domínio catch-all em Integrações."));
  }
  const int count = _bulkRegisterCount->value();
  _bulkRegisterStarting = true;
  ++_bulkRegisterDomainsRevision;
  ++_bulkRegisterPreflightRevision;
  _bulkRegisterStart->setEnabled(false);
  _bulkRegisterDomain->setEnabled(false);
  _bulkRegisterCount->setEnabled(false);
  _bulkRegisterTable->setRowCount(0);
  _bulkRegisterProgress->setRange(0, count);
  _bulkRegisterProgress->setValue(0);
  _bulkRegisterStatus->setText(QStringLiteral("Iniciando criação de %1 contas…").arg(count));
  _api.post(QStringLiteral("/api/accounts/bulk-register"),
            {{QStringLiteral("count"), count}, {QStringLiteral("domain"), domain}},
            [this](bool ok, const QJsonDocument&, const QString& error) {
    _bulkRegisterStarting = false;
    if (!ok) {
      _bulkRegisterStart->setEnabled(true);
      _bulkRegisterDomain->setEnabled(true);
      _bulkRegisterCount->setEnabled(true);
      _bulkRegisterStatus->setText(QStringLiteral("Falha ao iniciar: ") + error);
      return;
    }
    _bulkRegisterPolling = true;
    _bulkRegisterPoll.start();
  });
}

void MainWindow::openWebmail() {
  const QString text = _bulkRegisterWebmail->text();
  // Extrai o href do link
  const QRegularExpression hrefRe(QStringLiteral("href=\"([^\"]+)\""));
  const auto match = hrefRe.match(text);
  if (match.hasMatch()) {
    QDesktopServices::openUrl(QUrl(match.captured(1)));
  }
}

void MainWindow::pollBulkRegister() {
  if (_bulkRegisterRequestInFlight) return;
  _bulkRegisterRequestInFlight = true;
  _api.get(QStringLiteral("/api/accounts/bulk-register/status"),
           [this](bool ok, const QJsonDocument& doc, const QString& error) {
    _bulkRegisterRequestInFlight = false;
    if (!ok) {
      _bulkRegisterPoll.setInterval(3000);
      _bulkRegisterStatus->setText(QStringLiteral("Conexão interrompida. Tentando recuperar o progresso… ") + error);
      return;
    }
    _bulkRegisterPoll.setInterval(900);
    const auto root = doc.object();
    const QString state = root.value(QStringLiteral("state")).toString();
    if (state == QStringLiteral("idle")) {
      _bulkRegisterPoll.stop();
      _bulkRegisterPolling = false;
      _bulkRegisterStart->setEnabled(true);
      _bulkRegisterDomain->setEnabled(true);
      _bulkRegisterCount->setEnabled(true);
      _bulkRegisterStatus->setText(QStringLiteral(
          "O serviço não possui um lote em andamento. Confira as contas salvas antes de iniciar outro cadastro."));
      loadAccounts();
      return;
    }
    const int total = root.value(QStringLiteral("total")).toInt();
    const int completed = root.value(QStringLiteral("completed")).toInt();
    const int created = root.value(QStringLiteral("created")).toInt();
    const int failed = root.value(QStringLiteral("failed")).toInt();
    const QString current = root.value(QStringLiteral("current_email")).toString();
    const QString currentStep = root.value(QStringLiteral("current_step")).toString();
    _bulkRegisterProgress->setRange(0, qMax(1, total));
    _bulkRegisterProgress->setValue(completed);
    const auto results = root.value(QStringLiteral("results")).toArray();
    const bool terminal = state == QStringLiteral("done") || state == QStringLiteral("failed");
    _bulkRegisterTable->setRowCount(results.size());
    for (int row = 0; row < results.size(); ++row) {
      const auto item = results.at(row).toObject();
      auto* emailItem = cell(item.value(QStringLiteral("email")).toString());
      _bulkRegisterTable->setItem(row, 0, emailItem);
      _bulkRegisterTable->setItem(row, 1, cell(item.value(QStringLiteral("nome")).toString()));
      _bulkRegisterTable->setItem(row, 2, cell(item.value(QStringLiteral("sobrenome")).toString()));
      _bulkRegisterTable->setItem(row, 3, cell(item.value(QStringLiteral("gender")).toString()));
      // Construir tooltip com todas as etapas
      QStringList stepLines;
      const auto steps = item.value(QStringLiteral("steps")).toObject();
      const QStringList stepKeys = {
          QStringLiteral("ban_check"), QStringLiteral("save_partial"),
          QStringLiteral("crowtado_signup"), QStringLiteral("demographics"),
          QStringLiteral("minute_register"), QStringLiteral("link_minute"),
          QStringLiteral("validate"),
      };
      const QStringList stepNames = {
          QStringLiteral("Verificação"), QStringLiteral("Credenciais"),
          QStringLiteral("Crowtado"), QStringLiteral("Demografia"),
          QStringLiteral("Minute"), QStringLiteral("Vínculo"),
          QStringLiteral("Validação"),
      };
      for (int s = 0; s < stepKeys.size(); ++s) {
        const auto stepObj = steps.value(stepKeys.at(s)).toObject();
        const QString status = stepObj.value(QStringLiteral("status")).toString();
        const QString detail = stepObj.value(QStringLiteral("detail")).toString();
        QString icon;
        if (status == QStringLiteral("ok")) icon = QStringLiteral("✓");
        else if (status == QStringLiteral("skip")) icon = QStringLiteral("↷");
        else if (status == QStringLiteral("fail")) icon = QStringLiteral("✗");
        else icon = QStringLiteral("·");
        const QString line = detail.isEmpty()
            ? QStringLiteral("%1 %2").arg(icon, stepNames.at(s))
            : QStringLiteral("%1 %2: %3").arg(icon, stepNames.at(s), detail);
        stepLines << line;
      }
      emailItem->setToolTip(stepLines.join(QStringLiteral("\n")));
      const QString errorText = item.value(QStringLiteral("error")).toString();
      const bool removed = item.value(QStringLiteral("removed")).toBool(false);
      QString outcome;
      if (removed) {
        outcome = QStringLiteral("Removida deste QMoney");
      } else if (errorText.isEmpty()) {
        outcome = QStringLiteral("✓ completa");
      } else {
        // Descobrir em qual etapa falhou
        QString failedStep;
        for (int s = stepKeys.size() - 1; s >= 0; --s) {
          const auto stepObj = steps.value(stepKeys.at(s)).toObject();
          if (stepObj.value(QStringLiteral("status")).toString() == QStringLiteral("fail")) {
            failedStep = stepNames.at(s);
            break;
          }
        }
        outcome = failedStep.isEmpty()
            ? QStringLiteral("✗ ") + errorText
            : QStringLiteral("✗ falhou em: ") + failedStep;
      }
      auto* outcomeItem = cell(outcome);
      outcomeItem->setToolTip(stepLines.join(QStringLiteral("\n")));
      _bulkRegisterTable->setItem(row, 4, outcomeItem);
      auto* remove = new QPushButton(QStringLiteral("Remover"));
      remove->setMinimumSize(88, 32);
      const bool removable = item.value(QStringLiteral("removable"))
                                 .toBool(item.value(QStringLiteral("created")).toBool(false));
      remove->setEnabled(terminal && removable && !removed);
      if (removed)
        remove->setToolTip(QStringLiteral("Esta conta já foi removida deste QMoney."));
      else if (!terminal)
        remove->setToolTip(QStringLiteral("Aguarde o cadastro em lote terminar."));
      else if (!removable)
        remove->setToolTip(QStringLiteral("Nenhum acesso local foi criado para esta tentativa."));
      connect(remove, &QPushButton::clicked, this, [this, email = item.value(QStringLiteral("email")).toString()] {
        removeAccount(email, [this] { pollBulkRegister(); });
      });
      _bulkRegisterTable->setCellWidget(row, 5, remove);
    }
    if (terminal) {
      _bulkRegisterPoll.stop();
      _bulkRegisterPolling = false;
      _bulkRegisterStart->setEnabled(true);
      _bulkRegisterDomain->setEnabled(true);
      _bulkRegisterCount->setEnabled(true);
      if (state == QStringLiteral("failed")) {
        _bulkRegisterStatus->setText(QStringLiteral("Falha: ")
                                     + root.value(QStringLiteral("error")).toString());
      } else {
        _bulkRegisterStatus->setText(
            QStringLiteral("Concluído: %1 criada(s), %2 falha(s) de %3.")
                .arg(created).arg(failed).arg(total));
        loadAccounts();
      }
    } else {
      QString label;
      if (current.isEmpty()) {
        label = QStringLiteral("Processando…");
      } else if (currentStep.isEmpty()) {
        label = QStringLiteral("Registrando %1").arg(current);
      } else {
        label = QStringLiteral("%1 — etapa: %2").arg(current, currentStep);
      }
      _bulkRegisterStatus->setText(
          QStringLiteral("%1 — %2/%3 concluído(s).")
              .arg(label).arg(completed).arg(total));
    }
  });
}

void MainWindow::loadBalances() {
  _api.get(QStringLiteral("/api/balances"), [this](bool ok, const QJsonDocument& doc, const QString& error) {
    if (!ok) return showError(QStringLiteral("Falha ao carregar saldos"), error);
    const auto root = doc.object();
    _balancesSnapshot = root;
    _balancesExport->setEnabled(!root.value(QStringLiteral("accounts")).toArray().isEmpty());
    const auto accounts = root.value(QStringLiteral("accounts")).toArray();
    const auto balances = root.value(QStringLiteral("balances")).toObject();
    const auto accountKinds = root.value(QStringLiteral("account_kinds")).toObject();
    const auto withPassword = root.value(QStringLiteral("with_password")).toArray();
    const auto withSavedPassword = root.value(QStringLiteral("with_saved_password")).toArray();
    const auto runner = root.value(QStringLiteral("runner")).toObject();
    const auto bulk = root.value(QStringLiteral("withdraw_bulk")).toObject();
    const auto payout = root.value(QStringLiteral("payout_method_bulk")).toObject();
    const auto wiseCleanup = root.value(QStringLiteral("wise_cleanup")).toObject();
    const bool cleanupPending = wiseCleanup.value(QStringLiteral("pending")).toBool();
    _balancesWiseCleanup->setVisible(cleanupPending);
    _lastWithdrawBulk = bulk;
    _balancesWithdrawHistory->setEnabled(
        bulk.value(QStringLiteral("state")).toString() != QStringLiteral("idle"));
    const auto exchange = root.value(QStringLiteral("exchange")).toObject();
    QStringList passwordAccounts;
    for (const auto value : withPassword) passwordAccounts << value.toString();
    QStringList savedPasswordAccounts;
    for (const auto value : withSavedPassword) savedPasswordAccounts << value.toString();
    QStringList orderedAccounts;
    for (const auto value : accounts) orderedAccounts << value.toString();
    std::sort(orderedAccounts.begin(), orderedAccounts.end(), [&balances](const QString& left, const QString& right) {
      const auto leftValue = balances.value(left).toObject().value(QStringLiteral("availableCents"));
      const auto rightValue = balances.value(right).toObject().value(QStringLiteral("availableCents"));
      const bool leftKnown = leftValue.isDouble();
      const bool rightKnown = rightValue.isDouble();
      if (leftKnown != rightKnown) return leftKnown;
      if (leftKnown && leftValue.toDouble() != rightValue.toDouble())
        return leftValue.toDouble() > rightValue.toDouble();
      return left.compare(right, Qt::CaseInsensitive) < 0;
    });
    _balancesTable->setRowCount(orderedAccounts.size());
    qint64 approvedTotal = 0;
    qint64 pendingTotal = 0;
    int staleTotals = 0;
    int eligibleWithdrawals = 0;
    int row = 0;
    for (const QString& email : orderedAccounts) {
      const bool isClaru = accountKinds.value(email).toString() == QStringLiteral("claru");
      const auto balance = balances.value(email).toObject();
      _balancesTable->setItem(row, 0, cell(email));
      const bool hasAvailable = balance.value(QStringLiteral("availableCents")).isDouble();
      const bool hasPending = balance.value(QStringLiteral("pendingCents")).isDouble();
      const qint64 availableCents = hasAvailable
          ? static_cast<qint64>(balance.value(QStringLiteral("availableCents")).toDouble()) : 0;
      const qint64 pendingCents = hasPending
          ? static_cast<qint64>(balance.value(QStringLiteral("pendingCents")).toDouble()) : 0;
      if (!isClaru && hasAvailable) approvedTotal += availableCents;
      if (!isClaru && hasPending) pendingTotal += pendingCents;
      if (!isClaru && (hasAvailable || hasPending)
          && (!balance.value(QStringLiteral("error")).toString().isEmpty()
              || balance.value(QStringLiteral("stale")).toBool())) ++staleTotals;
      QString available = isClaru
          ? QStringLiteral("não se aplica")
          : hasAvailable
          ? usdMoney(availableCents)
          : QStringLiteral("—");
      QString pending = isClaru
          ? QStringLiteral("não se aplica")
          : hasPending
          ? usdMoney(pendingCents)
          : QStringLiteral("—");
      if (!isClaru && (!balance.value(QStringLiteral("error")).toString().isEmpty()
                       || balance.value(QStringLiteral("stale")).toBool())) {
        available = hasAvailable ? available + QStringLiteral(" *") : QStringLiteral("não confirmado");
        pending = hasPending ? pending + QStringLiteral(" *") : QStringLiteral("não confirmado");
      }
      auto* availableItem = cell(available);
      auto* pendingItem = cell(pending);
      if (isClaru) {
        const QString hint = QStringLiteral(
            "Conta Claru preservada. A plataforma Crowtado não fornece saldo para esta identidade.");
        availableItem->setToolTip(hint);
        pendingItem->setToolTip(hint);
      } else if (!balance.value(QStringLiteral("error")).toString().isEmpty()
                 || balance.value(QStringLiteral("stale")).toBool()) {
        const QString hint = QStringLiteral("Consulta inconclusiva. * indica o último saldo salvo, não um saldo atualizado.\n")
                             + balance.value(QStringLiteral("error")).toString();
        availableItem->setToolTip(hint);
        pendingItem->setToolTip(hint);
      }
      availableItem->setTextAlignment(Qt::AlignRight | Qt::AlignVCenter);
      pendingItem->setTextAlignment(Qt::AlignRight | Qt::AlignVCenter);
      _balancesTable->setItem(row, 1, availableItem);
      _balancesTable->setItem(row, 2, pendingItem);
      _balancesTable->setItem(row, 3, cell(friendlyDate(balance.value(QStringLiteral("updated_at")).toString())));
      const bool hasPassword = passwordAccounts.contains(email);
      const bool hasBalanceError = !balance.value(QStringLiteral("error")).toString().isEmpty()
          || balance.value(QStringLiteral("stale")).toBool();
      if (!isClaru && hasPassword && !hasBalanceError && hasAvailable && availableCents > 0)
        ++eligibleWithdrawals;
      auto* actions = new QWidget;
      auto* actionsLayout = new QHBoxLayout(actions);
      actionsLayout->setContentsMargins(5, 5, 5, 5);
      actionsLayout->setSpacing(7);
      auto* credentials = new QPushButton(
          isClaru ? QStringLiteral("Claru · preservada")
          : hasPassword ? QStringLiteral("Alterar acesso")
                      : QStringLiteral("Conectar Crowtado"));
      credentials->setMinimumHeight(32);
      credentials->setEnabled(!isClaru);
      credentials->setToolTip(isClaru
          ? QStringLiteral("A conta Claru continua ativa; saldo Crowtado não se aplica.")
          : QStringLiteral("Informa e valida a senha desta identidade diretamente no Crowtado."));
      connect(credentials, &QPushButton::clicked, this,
              [this, email] { configureCrowtadoAccess(email); });
      actionsLayout->addWidget(credentials);
      auto* reveal = new QPushButton(QStringLiteral("Ver senha"));
      reveal->setMinimumHeight(32);
      reveal->setEnabled(savedPasswordAccounts.contains(email));
      reveal->setToolTip(reveal->isEnabled()
          ? QStringLiteral("Mostrar a senha salva para acesso manual.")
          : QStringLiteral("Esta conta não possui senha salva."));
      connect(reveal, &QPushButton::clicked, this, [this, email] {
        _api.post(QStringLiteral("/api/accounts/password"),
                  {{QStringLiteral("email"), email}},
                  [this, email](bool ok, const QJsonDocument& doc, const QString& error) {
          if (!ok) return showError(QStringLiteral("Senha indisponível"), error);
          QMessageBox dialog(this);
          dialog.setWindowTitle(QStringLiteral("Senha da conta"));
          dialog.setTextFormat(Qt::PlainText);
          dialog.setText(QStringLiteral("%1\n\nSenha: %2")
              .arg(email, doc.object().value(QStringLiteral("password")).toString()));
          dialog.setTextInteractionFlags(Qt::TextSelectableByMouse);
          dialog.exec();
        });
      });
      actionsLayout->addWidget(reveal);
      auto* withdraw = new QPushButton(QStringLiteral("Solicitar saque"));
      withdraw->setMinimumHeight(32);
      withdraw->setEnabled(!cleanupPending && !isClaru && hasPassword && !hasBalanceError
                           && hasAvailable && availableCents > 0
                           && runner.value(QStringLiteral("state")).toString() != QStringLiteral("running")
                           && bulk.value(QStringLiteral("state")).toString() != QStringLiteral("running"));
      withdraw->setToolTip(withdraw->isEnabled()
          ? QStringLiteral("Escolha o método e confirme o saque do saldo disponível desta conta.")
          : QStringLiteral("Conecte o Crowtado e confirme um saldo disponível positivo antes de solicitar saque."));
      connect(withdraw, &QPushButton::clicked, this, [this, email, withdraw] {
        QJsonObject request{{QStringLiteral("email"), email}};
        if (!confirmWithdrawal(this, 1, request)) return;
        withdraw->setEnabled(false);
        _api.post(QStringLiteral("/api/balances/withdraw"), request,
                  [this, withdraw](bool ok, const QJsonDocument& doc, const QString& error) {
          withdraw->setEnabled(true);
          loadBalances();
          if (!ok) return showError(QStringLiteral("Solicitação precisa de atenção"), error);
          QMessageBox::information(this, QStringLiteral("Solicitação enviada"),
                                   doc.object().value(QStringLiteral("message")).toString());
        });
      });
      actionsLayout->addWidget(withdraw);
      _balancesTable->setCellWidget(row, 4, actions);
      ++row;
    }
    applyBalanceFilter();
    _balancesApprovedUsd->setText(usdMoney(approvedTotal));
    _balancesPendingUsd->setText(usdMoney(pendingTotal));
    _balancesTotalsNote->setVisible(staleTotals > 0);
    if (staleTotals > 0)
      _balancesTotalsNote->setText(QStringLiteral(
          "Os totais incluem o último valor salvo de %1 conta(s) com consulta inconclusiva; atualize os saldos antes de decidir sobre saques.")
          .arg(staleTotals));
    const bool exchangeAvailable = exchange.value(QStringLiteral("available")).toBool();
    const double usdBrlRate = exchange.value(QStringLiteral("rate")).toDouble();
    if (exchangeAvailable && usdBrlRate > 0.0) {
      _balancesApprovedBrl->setText(QStringLiteral("≈ %1").arg(brlMoney(approvedTotal, usdBrlRate)));
      _balancesPendingBrl->setText(QStringLiteral("≈ %1").arg(brlMoney(pendingTotal, usdBrlRate)));
      const QDate quoteDate = QDate::fromString(
          exchange.value(QStringLiteral("quote_date")).toString(), Qt::ISODate);
      const QString dateText = quoteDate.isValid()
          ? QLocale(QStringLiteral("pt_BR")).toString(quoteDate, QStringLiteral("dd/MM/yyyy"))
          : QStringLiteral("data indisponível");
      _balancesExchange->setText(QStringLiteral("%1Cotação BCB de venda · US$ 1 = R$ %2 · %3")
          .arg(exchange.value(QStringLiteral("stale")).toBool()
                   ? QStringLiteral("Última cotação salva · ") : QString())
          .arg(QLocale(QStringLiteral("pt_BR")).toString(usdBrlRate, 'f', 4))
          .arg(dateText));
    } else {
      _balancesApprovedBrl->setText(QStringLiteral("≈ R$ —"));
      _balancesPendingBrl->setText(QStringLiteral("≈ R$ —"));
      _balancesExchange->setText(
          QStringLiteral("Conversão para BRL indisponível · os totais em USD permanecem válidos."));
    }
    const bool running = runner.value(QStringLiteral("state")).toString() == QStringLiteral("running");
    _balancesRefresh->setEnabled(!running);
    const bool bulkRunning = bulk.value(QStringLiteral("state")).toString() == QStringLiteral("running");
    const bool payoutRunning = payout.value(QStringLiteral("state")).toString() == QStringLiteral("running");
    const int refreshNeeded = root.value(QStringLiteral("refresh_needed")).toArray().size();
    _balancesRefreshNeeded->setText(QStringLiteral("Atualizar pendentes (%1)").arg(refreshNeeded));
    _balancesRefreshNeeded->setEnabled(!running && !bulkRunning && refreshNeeded > 0);
    _balancesWithdrawAll->setProperty("eligibleCount", eligibleWithdrawals);
    _balancesWithdrawAll->setEnabled(!cleanupPending && !running && !bulkRunning && !payoutRunning && eligibleWithdrawals > 0);
    _balancesPayoutMethod->setEnabled(!cleanupPending && !running && !bulkRunning && !payoutRunning && !passwordAccounts.isEmpty());
    if (_payoutMethodAwaitingResult && !payoutRunning &&
        payout.value(QStringLiteral("state")).toString() != QStringLiteral("idle")) {
      _payoutMethodAwaitingResult = false;
      QStringList failures;
      int successes = 0;
      for (const auto value : payout.value(QStringLiteral("results")).toArray()) {
        const auto row = value.toObject();
        if (row.value(QStringLiteral("ok")).toBool()) ++successes;
        else failures << QStringLiteral("%1: %2").arg(
            row.value(QStringLiteral("email")).toString(), row.value(QStringLiteral("message")).toString());
      }
      QMessageBox::information(this, QStringLiteral("Método de saque"),
          QStringLiteral("Configurado em %1 de %2 conta(s).%3")
              .arg(successes).arg(payout.value(QStringLiteral("total")).toInt())
              .arg((payout.value(QStringLiteral("message")).toString().isEmpty()
                      ? QString() : QStringLiteral("\n\n") + payout.value(QStringLiteral("message")).toString())
                   + (failures.isEmpty() ? QString() : QStringLiteral("\n\nFalhas:\n")
                      + failures.join(QStringLiteral("\n")))));
    }
    if (_bulkWithdrawAwaitingResult && !bulkRunning
        && bulk.value(QStringLiteral("state")).toString() != QStringLiteral("idle")) {
      _bulkWithdrawAwaitingResult = false;
      showWithdrawalReport(bulk);
    }
    int claruCount = 0;
    for (const auto value : accounts) {
      if (accountKinds.value(value.toString()).toString() == QStringLiteral("claru"))
        ++claruCount;
    }
    _balancesState->setText(cleanupPending
        ? QStringLiteral("Limpeza Wise pendente em %1 · novos saques bloqueados")
              .arg(wiseCleanup.value(QStringLiteral("email")).toString())
        : payoutRunning
        ? QStringLiteral("Configurando método de saque: %1 de %2 conta(s)…")
              .arg(payout.value(QStringLiteral("done")).toInt())
              .arg(payout.value(QStringLiteral("total")).toInt())
        : bulkRunning
        ? QStringLiteral("Solicitando saques: %1 de %2 conta(s)…")
              .arg(bulk.value(QStringLiteral("done")).toInt())
              .arg(bulk.value(QStringLiteral("total")).toInt())
        : bulk.value(QStringLiteral("state")).toString() == QStringLiteral("interrupted")
        ? QStringLiteral("Último lote interrompido · confira o relatório antes de solicitar novamente")
        : bulk.value(QStringLiteral("state")).toString() == QStringLiteral("error")
        ? QStringLiteral("Último lote precisa de atenção · abra o relatório")
        : running
        ? runner.value(QStringLiteral("current")).toString(QStringLiteral("Consultando contas…"))
        : QStringLiteral("%1 identidade(s) · %2 Crowtado conectado(s) · %3 Claru preservada(s)")
              .arg(accounts.size()).arg(passwordAccounts.size()).arg(claruCount));
    if (running || bulkRunning || payoutRunning) _balancePoll.start(); else _balancePoll.stop();
  });
}

void MainWindow::updateCampaignAccountCount() {
  if (!_campaignAccounts || !_campaignAccountSelection) return;
  int selected = 0;
  for (int i = 0; i < _campaignAccounts->count(); ++i)
    if (_campaignAccounts->item(i)->checkState() == Qt::Checked) ++selected;
  _campaignAccountSelection->setText(QStringLiteral("%1 de %2 contas selecionadas")
      .arg(selected).arg(_campaignAccounts->count()));
}

void MainWindow::saveCampaignDraft() {
  if (!_campaignAccounts) return;
  QJsonArray accounts;
  for (int i = 0; i < _campaignAccounts->count(); ++i)
    if (_campaignAccounts->item(i)->checkState() == Qt::Checked)
      accounts.append(_campaignAccounts->item(i)->data(Qt::UserRole).toString());
  QJsonArray tasks;
  for (const QString& id : _campaignSelectedTaskIds) tasks.append(id);
  const QJsonObject draft{
      {QStringLiteral("accounts"), accounts},
      {QStringLiteral("tasks"), tasks},
      {QStringLiteral("tasks_touched"), _campaignTaskSelectionTouched},
      {QStringLiteral("mode"), _campaignAccountMode->currentData().toString()},
      {QStringLiteral("quantity"), _campaignAccountCount->value()},
      {QStringLiteral("dataset"), _dataset->currentData().toString()},
      {QStringLiteral("content_mode"), _contentMode->currentData().toString()},
      {QStringLiteral("target_hours"), _targetHours->value()},
      {QStringLiteral("account_workers"), _accountWorkers->currentData().toInt()},
      {QStringLiteral("min_duration"), _minDuration->value()},
      {QStringLiteral("max_duration"), _maxDuration->value()},
      {QStringLiteral("delay_mode"), _delayMode->currentData().toString()},
      {QStringLiteral("delay_seconds"), _delaySeconds->value()},
      {QStringLiteral("cleanup"), _cleanupAfter->isChecked()},
      {QStringLiteral("active_hours"), _activeHours->isChecked()},
      {QStringLiteral("hour_start"), _hourStart->value()},
      {QStringLiteral("hour_end"), _hourEnd->value()},
  };
  QSettings().setValue(QStringLiteral("campaign/draft"),
                       QJsonDocument(draft).toJson(QJsonDocument::Compact));
}

void MainWindow::drawCampaignAccounts() {
  if (!_campaignAccounts || _campaignAccounts->count() == 0) {
    updateCampaignAccountCount();
    return;
  }
  const QString mode = _campaignAccountMode->currentData().toString();
  if (mode.startsWith(QStringLiteral("balance_")) && !_campaignBalancesLoaded) {
    loadCampaignBalances();
    return;
  }
  const QSignalBlocker accountChanges(_campaignAccounts);
  QVector<int> indexes;
  indexes.reserve(_campaignAccounts->count());
  for (int i = 0; i < _campaignAccounts->count(); ++i) indexes.append(i);
  if (mode.startsWith(QStringLiteral("balance_"))) {
    const auto balances = _campaignBalances.value(QStringLiteral("balances")).toObject();
    const auto kinds = _campaignBalances.value(QStringLiteral("account_kinds")).toObject();
    QSet<QString> connected;
    for (const auto value : _campaignBalances.value(QStringLiteral("with_password")).toArray())
      connected.insert(value.toString());
    QSet<QString> needsRefresh;
    for (const auto value : _campaignBalances.value(QStringLiteral("refresh_needed")).toArray())
      needsRefresh.insert(value.toString());
    const QString field = mode.contains(QStringLiteral("available"))
        ? QStringLiteral("availableCents") : QStringLiteral("pendingCents");
    const bool descending = mode.endsWith(QStringLiteral("desc"));
    QVector<int> eligible;
    for (int i : indexes) {
      auto* item = _campaignAccounts->item(i);
      const QString email = item->data(Qt::UserRole).toString();
      const auto record = balances.value(email).toObject();
      const auto value = record.value(field);
      item->setText(email);
      if (!connected.contains(email) || kinds.value(email).toString() != QStringLiteral("crowtado")
          || needsRefresh.contains(email) || !record.value(QStringLiteral("error")).toString().isEmpty()
          || record.value(QStringLiteral("stale")).toBool() || !value.isDouble()
          || !std::isfinite(value.toDouble()) || value.toDouble() < 0) {
        item->setToolTip(QStringLiteral("Sem saldo Crowtado confirmado nas últimas 24 horas. Atualize na aba Saldos."));
        continue;
      }
      item->setToolTip(QStringLiteral("%1: %2 · atualizado em %3")
          .arg(field == QStringLiteral("availableCents") ? QStringLiteral("Aprovado")
               : QStringLiteral("Pendente"), usdMoney(static_cast<qint64>(value.toDouble())),
               friendlyDate(record.value(QStringLiteral("updated_at")).toString())));
      item->setText(QStringLiteral("%1  ·  %2").arg(email,
          usdMoney(static_cast<qint64>(value.toDouble()))));
      eligible.append(i);
    }
    std::sort(eligible.begin(), eligible.end(), [this, &balances, &field, descending](int left, int right) {
      const QString leftEmail = _campaignAccounts->item(left)->data(Qt::UserRole).toString();
      const QString rightEmail = _campaignAccounts->item(right)->data(Qt::UserRole).toString();
      const double leftValue = balances.value(leftEmail).toObject().value(field).toDouble();
      const double rightValue = balances.value(rightEmail).toObject().value(field).toDouble();
      if (leftValue != rightValue) return descending ? leftValue > rightValue : leftValue < rightValue;
      return leftEmail.compare(rightEmail, Qt::CaseInsensitive) < 0;
    });
    indexes = eligible;
    _campaignBalanceHint->setText(QStringLiteral(
        "%1 conta(s) com %2 confirmado nas últimas 24 horas. %3 Atualize os valores na aba Saldos antes de decidir.")
        .arg(eligible.size())
        .arg(field == QStringLiteral("availableCents") ? QStringLiteral("saldo aprovado")
             : QStringLiteral("saldo pendente"))
        .arg(eligible.size() < _campaignAccountCount->value()
             ? QStringLiteral("A quantidade pedida supera as contas elegíveis.") : QString()));
  } else {
    for (int i : indexes) {
      auto* item = _campaignAccounts->item(i);
      item->setText(item->data(Qt::UserRole).toString());
      item->setToolTip(item->data(Qt::UserRole).toString());
    }
    for (int i = 0; i < indexes.size(); ++i) {
      const int pick = i + QRandomGenerator::global()->bounded(indexes.size() - i);
      std::swap(indexes[i], indexes[pick]);
    }
  }
  if (mode == QStringLiteral("rotation")) {
    const auto used = QJsonDocument::fromJson(QSettings().value(
        QStringLiteral("campaign/accountLastUsed")).toByteArray()).object();
    std::stable_sort(indexes.begin(), indexes.end(), [this, &used](int left, int right) {
      const auto emailLeft = _campaignAccounts->item(left)->data(Qt::UserRole).toString();
      const auto emailRight = _campaignAccounts->item(right)->data(Qt::UserRole).toString();
      return used.value(emailLeft).toDouble() < used.value(emailRight).toDouble();
    });
  }
  const int wanted = qMin(_campaignAccountCount->value(), indexes.size());
  QSet<int> selected;
  for (int i = 0; i < wanted; ++i) selected.insert(indexes[i]);
  {
    const QSignalBlocker blocker(_campaignAccounts);
    for (int i = 0; i < _campaignAccounts->count(); ++i)
      _campaignAccounts->item(i)->setCheckState(selected.contains(i) ? Qt::Checked : Qt::Unchecked);
  }
  updateCampaignAccountCount();
  _campaignStart->setEnabled(false);
  _taskReload.start();
  _campaignDraftSave.start();
}

void MainWindow::loadCampaignBalances() {
  const int requestId = ++_campaignBalanceRequestId;
  _campaignBalanceHint->setText(QStringLiteral("Lendo saldos salvos…"));
  _campaignDrawAccounts->setEnabled(false);
  _campaignStart->setEnabled(false);
  {
    const QSignalBlocker blocker(_campaignAccounts);
    for (int i = 0; i < _campaignAccounts->count(); ++i)
      _campaignAccounts->item(i)->setCheckState(Qt::Unchecked);
  }
  updateCampaignAccountCount();
  _api.get(QStringLiteral("/api/balances"), [this, requestId](bool ok, const QJsonDocument& doc,
                                                             const QString& error) {
    if (requestId != _campaignBalanceRequestId
        || !_campaignAccountMode->currentData().toString().startsWith(QStringLiteral("balance_"))) return;
    _campaignDrawAccounts->setEnabled(true);
    if (!ok) {
      _campaignBalanceHint->setText(QStringLiteral("Não foi possível ler os saldos: %1").arg(error));
      const QSignalBlocker blocker(_campaignAccounts);
      for (int i = 0; i < _campaignAccounts->count(); ++i)
        _campaignAccounts->item(i)->setCheckState(Qt::Unchecked);
      updateCampaignAccountCount();
      _campaignStart->setEnabled(false);
      return;
    }
    _campaignBalances = doc.object();
    _campaignBalancesLoaded = true;
    drawCampaignAccounts();
  });
}

void MainWindow::applyBalanceFilter() {
  if (!_balancesTable || !_balancesSearch || !_balancesOnlyAvailable || !_balancesOnlyPending) return;
  const QString search = _balancesSearch->text().trimmed();
  const bool onlyAvailable = _balancesOnlyAvailable->isChecked();
  const bool onlyPending = _balancesOnlyPending->isChecked();
  const auto balances = _balancesSnapshot.value(QStringLiteral("balances")).toObject();
  QSet<QString> pendingEmails;
  for (const auto value : _balancesSnapshot.value(QStringLiteral("refresh_needed")).toArray())
    pendingEmails.insert(value.toString());
  int visible = 0;
  for (int row = 0; row < _balancesTable->rowCount(); ++row) {
    const auto* item = _balancesTable->item(row, 0);
    const QString email = item ? item->text() : QString();
    const bool matches = email.contains(search, Qt::CaseInsensitive);
    const auto balance = balances.value(email).toObject();
    const bool hasAvailable = balance.value(QStringLiteral("availableCents")).toDouble() > 0
        && balance.value(QStringLiteral("error")).toString().isEmpty()
        && !balance.value(QStringLiteral("stale")).toBool();
    const bool show = matches && (!onlyAvailable || hasAvailable)
        && (!onlyPending || pendingEmails.contains(email));
    _balancesTable->setRowHidden(row, !show);
    if (show) ++visible;
  }
  if (_balancesFilterState)
    _balancesFilterState->setText(QStringLiteral("%1 de %2 contas")
        .arg(visible).arg(_balancesTable->rowCount()));
}

void MainWindow::showWithdrawalReport(const QJsonObject& bulk) {
  const QString state = bulk.value(QStringLiteral("state")).toString();
  const int total = bulk.value(QStringLiteral("total")).toInt();
  const int done = bulk.value(QStringLiteral("done")).toInt();
  int sent = 0;
  QStringList details;
  for (const auto value : bulk.value(QStringLiteral("results")).toArray()) {
    const auto result = value.toObject();
    if (result.value(QStringLiteral("ok")).toBool()) ++sent;
    details << QStringLiteral("%1: %2")
        .arg(result.value(QStringLiteral("email")).toString(),
             result.value(QStringLiteral("message")).toString());
  }
  const QString current = bulk.value(QStringLiteral("current")).toString();
  if ((state == QStringLiteral("interrupted") || state == QStringLiteral("error"))
      && !current.isEmpty())
    details << QStringLiteral("%1: resultado inconclusivo após interrupção; confira se o link chegou antes de repetir.").arg(current);
  const QString message = bulk.value(QStringLiteral("message")).toString();
  if (!message.isEmpty()) details << message;
  QMessageBox report(QMessageBox::Information, QStringLiteral("Último lote de saques"),
      QStringLiteral("%1 de %2 conta(s) processadas; %3 solicitação(ões) aceita(s). "
                     "Confira os detalhes por conta. No Dots, conclua pelo link recebido.")
          .arg(done).arg(total).arg(sent), QMessageBox::Ok, this);
  report.setDetailedText(details.join('\n'));
  report.exec();
}

void MainWindow::configureCrowtadoAccess(const QString& email) {
  bool accepted = false;
  const QString password = QInputDialog::getText(
      this, QStringLiteral("Conectar Crowtado"),
      QStringLiteral("Senha do Crowtado para %1:").arg(email),
      QLineEdit::Password, QString(), &accepted);
  if (!accepted || password.isEmpty()) return;

  setStatus(QStringLiteral("Validando acesso de %1 no Crowtado…").arg(email));
  _api.put(QStringLiteral("/api/balances/credentials"),
           {{QStringLiteral("email"), email},
            {QStringLiteral("password"), password}},
           [this, email](bool ok, const QJsonDocument&, const QString& error) {
    if (!ok)
      return showError(QStringLiteral("Crowtado não conectado"), error);
    setStatus(QStringLiteral("Crowtado conectado para %1. Consultando saldo…").arg(email));
    QJsonObject refresh;
    refresh.insert(QStringLiteral("emails"), QJsonArray{email});
    _api.post(QStringLiteral("/api/balances/refresh"), refresh,
              [this](bool refreshed, const QJsonDocument&, const QString& refreshError) {
      if (!refreshed)
        return showError(QStringLiteral("Acesso salvo; saldo ainda não consultado"),
                         refreshError);
      _balancePoll.start();
      loadBalances();
    });
  });
}

void MainWindow::loadHistory() {
  _api.get(QStringLiteral("/api/logs"), [this](bool ok, const QJsonDocument& doc, const QString& error) {
    if (!ok) return showError(QStringLiteral("Falha ao carregar histórico"), error);
    const auto logs = doc.object().value(QStringLiteral("logs")).toArray();
    _historyTable->setRowCount(logs.size());
    int row = 0;
    for (const auto value : logs) {
      const auto log = value.toObject();
      auto* date = cell(friendlyDate(log.value(QStringLiteral("started_at")).toString()));
      date->setData(Qt::UserRole, log.value(QStringLiteral("name")).toString());
      _historyTable->setItem(row, 0, date);
      QStringList accounts;
      for (const auto account : log.value(QStringLiteral("accounts")).toArray()) accounts << account.toString();
      _historyTable->setItem(row, 1, cell(accounts.join(QStringLiteral(", "))));
      _historyTable->setItem(row, 2, cell(QString::number(log.value(QStringLiteral("items")).toInt())));
      _historyTable->setItem(row, 3, cell(QStringLiteral("%1/%2")
          .arg(log.value(QStringLiteral("ok")).toInt()).arg(log.value(QStringLiteral("sends")).toInt())));
      ++row;
    }
    _historyTable->resizeRowsToContents();
    if (!_pendingHistoryFocus.isEmpty()) {
      for(int i=0;i<_historyTable->rowCount();++i) if(_historyTable->item(i,0)->data(Qt::UserRole).toString()==_pendingHistoryFocus) {
        _historyTable->setCurrentCell(i,0);
        _historyTable->scrollToItem(_historyTable->item(i,0));
        break;
      }
      _pendingHistoryFocus.clear();
    }
    setStatus(QStringLiteral("%1 campanha(s) no histórico.").arg(logs.size()));
  });
}

QString MainWindow::usdMoney(qint64 cents) const {
  return QLocale(QStringLiteral("pt_BR")).toCurrencyString(cents / 100.0, QStringLiteral("US$"));
}

QString MainWindow::brlMoney(qint64 usdCents, double usdBrlRate) const {
  return QLocale(QStringLiteral("pt_BR")).toCurrencyString(
      (usdCents / 100.0) * usdBrlRate, QStringLiteral("R$"));
}

QString MainWindow::friendlyDate(const QString& iso) const {
  if (iso.isEmpty()) return QStringLiteral("—");
  QDateTime dt = QDateTime::fromString(iso, Qt::ISODate);
  if (!dt.isValid()) return iso;
  return QLocale(QStringLiteral("pt_BR")).toString(dt.toLocalTime(), QStringLiteral("dd/MM/yyyy HH:mm"));
}

QString MainWindow::encoded(const QString& value) const {
  return QString::fromLatin1(QUrl::toPercentEncoding(value));
}
