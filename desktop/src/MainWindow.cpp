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
#include "OriginalCaptureDialog.hpp"
#include "OwnedServiceProcesses.hpp"
#include "LibraryRootSelection.hpp"
#include "PackagedServiceEnvironment.hpp"
#include <QMenu>
#include <QWidgetAction>
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
#include <QScrollBar>
#include <QScreen>
#include <QSaveFile>
#include <QSet>
#include <QSettings>
#include <QSizePolicy>
#include <QSpinBox>
#include <QStackedWidget>
#include <QStandardPaths>
#include <QSignalBlocker>
#include <QScopeGuard>
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
  method->addItem(QStringLiteral("PayPal"), QStringLiteral("paypal"));
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
    name->setVisible(wise);
    email->setVisible(wise);
    form->labelForField(name)->setVisible(wise);
    form->labelForField(email)->setVisible(wise);
    explanation->setText(wise
        ? QStringLiteral("Uma conta por vez: vincular Wise → solicitar saque → desvincular Wise → voltar para Dots. "
                         "A limpeza é tentada mesmo em caso de erro. Novos saques ficam bloqueados até confirmá-la.")
        : method->currentData().toString() == QStringLiteral("paypal")
            ? QStringLiteral("Seleciona PayPal na Crowtado e solicita uma conta por vez. "
                             "Siga as instruções de pagamento enviadas pela plataforma. Não usa o e-mail da Wise.")
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
  } else if (method->currentData().toString() == QStringLiteral("paypal")) {
    request.insert(QStringLiteral("method"), QStringLiteral("paypal"));
    request.insert(QStringLiteral("paypal_confirmed"), true);
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

void terminateOwnedServiceTree(qint64 rootPid, const QString& expectedExecutable) {
#ifdef Q_OS_WIN
  if (rootPid <= 0 || expectedExecutable.isEmpty()) return;
  auto identity = [](HANDLE process, quint32 pid, quint32 parent) {
    qmoney::service::ProcessFact fact;
    fact.pid = pid;
    fact.parentPid = parent;
    wchar_t path[32768];
    DWORD length = DWORD(std::size(path));
    FILETIME created{}, exited{}, kernel{}, user{};
    HANDLE token = nullptr;
    if (!QueryFullProcessImageNameW(process, 0, path, &length)
        || !GetProcessTimes(process, &created, &exited, &kernel, &user)
        || !OpenProcessToken(process, TOKEN_QUERY, &token)) return fact;
    DWORD bytes = 0;
    GetTokenInformation(token, TokenUser, nullptr, 0, &bytes);
    QByteArray data(int(bytes), Qt::Uninitialized);
    const bool ownerKnown = bytes > 0 && GetTokenInformation(
        token, TokenUser, data.data(), bytes, &bytes);
    CloseHandle(token);
    if (!ownerKnown) return fact;
    auto* owner = reinterpret_cast<TOKEN_USER*>(data.data());
    if (!IsValidSid(owner->User.Sid)) return fact;
    fact.owner = QByteArray(static_cast<const char*>(owner->User.Sid),
                           int(GetLengthSid(owner->User.Sid)));
    fact.executable = QDir::cleanPath(QDir::fromNativeSeparators(
        QString::fromWCharArray(path, int(length))));
    fact.created = (quint64(created.dwHighDateTime) << 32) | created.dwLowDateTime;
    fact.identityKnown = true;
    return fact;
  };
  const quint32 appPid = GetCurrentProcessId();
  const auto app = identity(GetCurrentProcess(), appPid, 0);
  const QString expected = QDir::cleanPath(QDir::fromNativeSeparators(
      QFileInfo(expectedExecutable).canonicalFilePath()));
  HANDLE snapshot = CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0);
  if (snapshot == INVALID_HANDLE_VALUE || !app.identityKnown || expected.isEmpty()) {
    if (snapshot != INVALID_HANDLE_VALUE) CloseHandle(snapshot);
    qWarning() << "QMoney: árvore do motor preservada; propriedade não comprovada";
    return;
  }
  PROCESSENTRY32W entry{};
  entry.dwSize = sizeof(entry);
  QVector<qmoney::service::ProcessFact> facts;
  if (Process32FirstW(snapshot, &entry)) {
    do {
      HANDLE process = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, FALSE, entry.th32ProcessID);
      auto fact = process ? identity(process, entry.th32ProcessID, entry.th32ParentProcessID)
                          : qmoney::service::ProcessFact{};
      if (!process) { fact.pid = entry.th32ProcessID; fact.parentPid = entry.th32ParentProcessID; }
      facts.append(fact);
      if (process) CloseHandle(process);
    } while (Process32NextW(snapshot, &entry));
  }
  CloseHandle(snapshot);
  const auto selected = qmoney::service::selectOwnedServiceTree(
      facts, quint32(rootPid), appPid, app.created, app.owner, expected);
  if (selected.isEmpty()) {
    qWarning() << "QMoney: árvore do motor preservada; propriedade não comprovada";
    return;
  }
  for (const auto& fact : selected) {
    HANDLE process = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_TERMINATE
                                 | SYNCHRONIZE, FALSE, fact.pid);
    if (!process) continue;
    // Revalidate the open handle after selection: PID reuse must never turn
    // a previously owned process into permission to terminate its successor.
    const auto now = identity(process, fact.pid, fact.parentPid);
    if (now.identityKnown && now.created == fact.created && now.owner == fact.owner
        && now.executable.compare(fact.executable, Qt::CaseInsensitive) == 0) {
      if (TerminateProcess(process, 0)) WaitForSingleObject(process, 3000);
    }
      CloseHandle(process);
  }
#else
  Q_UNUSED(rootPid)
  Q_UNUSED(expectedExecutable)
#endif
}

QString jsonId(const QJsonValue& value) {
  if (value.isString()) return value.toString();
  if (value.isDouble()) return QString::number(value.toDouble(), 'f', 0);
  return value.toVariant().toString();
}

QString bytesText(qint64 bytes) {
  if (bytes < 1024) return QLocale().toString(bytes) + QStringLiteral(" B");
  if (bytes < 1024 * 1024) return QLocale().toString(static_cast<double>(bytes) / 1024.0, 'f', 1) + QStringLiteral(" KiB");
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
  connect(&_taskReload, &QTimer::timeout, this, [this] {
    _taskCatalogAutomaticPoll = true;
    loadTasks();
  });
  connect(&_balancePoll, &QTimer::timeout, this, &MainWindow::loadBalances);
  connect(&_cachePoll, &QTimer::timeout, this, &MainWindow::loadAccelerator);

  buildShell();
  auto* commandShortcut = new QShortcut(QKeySequence(QStringLiteral("Ctrl+K")), this);
  connect(commandShortcut, &QShortcut::activated, this, &MainWindow::openCommandPalette);
  _operationPoll.setInterval(3000);
  connect(&_operationPoll, &QTimer::timeout, this, [this] {
    if (!_backendReady || _pages->currentIndex() != 0 || _operationPolling) return;
    refreshOperation();
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
              _repairInstall->setText(QStringLiteral("Corrigir instalação"));
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
              _repairInstall->setText(QStringLiteral("Corrigir instalação"));
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
                _repairInstall->setText(QStringLiteral("Corrigir instalação"));
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
  _campaignExitRequested = true;
  _campaignRestartPending = false;
  _pendingLibraryRoot.clear();
  if (_campaignCloseReady && !_pendingUpdatePackage.isEmpty()
      && !_campaignTransitionCommitted) {
    event->ignore();
    completeCampaignDrain();
    return;
  }
  if (!_campaignCloseReady && (_campaignClosePending || _backendReady
      || _campaignActive || _campaignStartPending || _campaignPreflightPending
      || _campaignStartUncertain || _backend.state() != QProcess::NotRunning)) {
    event->ignore();
    beginCampaignDrain();
    return;
  }
  if (_campaignDraftSave.isActive()) {
    _campaignDraftSave.stop();
    saveCampaignDraft();
  }
  _closing = true;
  _campaignCloseReady = true;
  _campaignPoll.stop();
  _previewPoll.stop();
  _cachePoll.stop();
  _orgMigrationPoll.stop();
  _balancePoll.stop();
  _walletMonitorTimer.stop();
  _backendProbe.stop();
  stopBackend();
  QMainWindow::closeEvent(event);
}

void MainWindow::beginCampaignDrain() {
  if (!_campaignClosePending) {
    _campaignTransitionCommitted = false;
    _campaignClosePending = true;
    _closing = true;
    _resumeOperationPoll = _operationPoll.isActive();
    _operationPoll.stop();
    _backendProbe.stop();
    _campaignPoll.stop();
    _previewPoll.stop();
    _cachePoll.stop();
    _orgMigrationPoll.stop();
    _balancePoll.stop();
    if (centralWidget()) centralWidget()->setEnabled(false);
    connect(&_campaignClosePoll, &QTimer::timeout, this, &MainWindow::pollCampaignClose,
            Qt::UniqueConnection);
    _campaignClosePoll.start(1000);
    setStatus(QStringLiteral("Aguardando os envios iniciados e a gravação dos resultados…"));
  }
  pollCampaignClose();
}

void MainWindow::pollCampaignClose() {
  if (!_campaignClosePending || _campaignCloseReady || _campaignCloseInFlight) return;
  _campaignCloseInFlight = true;
  _api.post(QStringLiteral("/api/campaigns/drain"), {},
            [this](bool ok, const QJsonDocument& document, const QString&) {
    if (!_campaignClosePending || _campaignCloseReady) return;
    _campaignCloseInFlight = false;
    const auto result = document.object();
    const bool valid = ok && result.value(QStringLiteral("ok")) == QJsonValue(true)
        && result.value(QStringLiteral("draining")) == QJsonValue(true)
        && result.value(QStringLiteral("ready")).isBool();
    const bool serviceExited = !_backend.program().isEmpty()
        && _backend.state() == QProcess::NotRunning;
    if ((valid && result.value(QStringLiteral("ready")) == QJsonValue(true)) || serviceExited) {
      _campaignCloseReady = true;
      _campaignClosePoll.stop();
      QTimer::singleShot(0, this, &MainWindow::completeCampaignDrain);
      return;
    }
    if (!valid)
      setStatus(QStringLiteral("Não foi possível confirmar o encerramento dos envios. Tentando novamente; o aplicativo permanecerá aberto."));
  });
}

void MainWindow::completeCampaignDrain() {
  if (!_campaignCloseReady || _campaignTransitionCommitted) return;
  _campaignTransitionCommitted = true;
  if (!_pendingUpdatePackage.isEmpty()) {
    if (launchPendingUpdate()) {
      _pendingUpdatePackage.clear();
      _pendingUpdateSha256.clear();
      close();
      return;
    }
    // The old service has accepted a monotonic drain barrier. A fresh service
    // is necessary before the window can admit another campaign.
    _pendingUpdatePackage.clear();
    _pendingUpdateSha256.clear();
    if (_campaignExitRequested) {
      close();
      return;
    }
    _campaignRestartPending = true;
  }
  if (!_campaignRestartPending) {
    close();
    return;
  }
  if (_campaignDraftSave.isActive()) {
    _campaignDraftSave.stop();
    saveCampaignDraft();
  }
  if (!_pendingLibraryRoot.isEmpty()) {
    QSettings().setValue(QStringLiteral("libraryRoot"), _pendingLibraryRoot);
    _pendingLibraryRoot.clear();
  }
  _backendReady = false;
  _backendRestarts = 0;
  _restartingBackend = true;
  stopBackend();
  QTimer::singleShot(350, this, [this] {
    if (!_campaignRestartPending) return;
    _campaignRestartPending = false;
    _campaignClosePending = false;
    _campaignCloseReady = false;
    _campaignCloseInFlight = false;
    _campaignTransitionCommitted = false;
    _campaignExitRequested = false;
    _campaignActive = false;
    _campaignStartPending = false;
    _campaignPreflightPending = false;
    _campaignStartUncertain = !_campaignRequestedPreflight.isEmpty();
    _closing = false;
    _restartingBackend = false;
    if (centralWidget()) centralWidget()->setEnabled(true);
    if (_resumeOperationPoll) _operationPoll.start();
    startBackend();
  });
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
  if (_pages && _pages->count()>0 && !_pageHeaders.isEmpty()) {
    _pageHeaders.first()->setDirection(QBoxLayout::LeftToRight);
    if (auto* search = _pages->widget(0)->findChild<QPushButton*>(QStringLiteral("commandSearch"))) {
      search->setMinimumWidth(compact ? 160 : 460);
      search->setText(compact ? QStringLiteral("Buscar · Ctrl K") : QStringLiteral("Buscar conta, campanha ou ação     Ctrl K"));
    }
  }
  for (auto* identity : _headerIdentities) identity->setVisible(!compact);
  if (_pages && _pages->count()>5 && _pageHeaders.size()>5) {
    _pageHeaders.at(5)->setDirection(QBoxLayout::LeftToRight);
    if (auto* search = _pages->widget(5)->findChild<QPushButton*>(QStringLiteral("commandSearch"))) {
      search->setMinimumWidth(compact ? 160 : 460);
      search->setText(compact ? QStringLiteral("Buscar · Ctrl K") : QStringLiteral("Buscar conta, campanha ou ação     Ctrl K"));
    }
    _accountsTable->setColumnWidth(1, compact?135:155);
    _accountsTable->setColumnWidth(2, compact?165:205);
    _accountsTable->setColumnWidth(3, compact?210:250);
  }
  if (_operationColumns) _operationColumns->setDirection(compact ? QBoxLayout::TopToBottom : QBoxLayout::LeftToRight);
  if (_campaignSelectionColumns) _campaignSelectionColumns->setDirection(compact ? QBoxLayout::TopToBottom : QBoxLayout::LeftToRight);
  if (_libraryColumns) _libraryColumns->setDirection(compact ? QBoxLayout::TopToBottom : QBoxLayout::LeftToRight);
  if (_operationInspector) _operationInspector->setMaximumWidth(compact ? QWIDGETSIZE_MAX : 390);
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
    auto* repair = new QPushButton(QStringLiteral("Corrigir instalação"));
    repair->setObjectName(QStringLiteral("repairInstallation"));
    repair->setToolTip(QStringLiteral("Restaura os componentes oficiais, preservando contas, credenciais e campanhas."));
    connect(repair, &QPushButton::clicked, this, [this] {
      if (_repairInstall && !_updates.isBusy()) _repairInstall->click();
    });
    connect(&_updates, &UpdateManager::statusChanged, repair, [this, repair] {
      repair->setEnabled(!_updates.isBusy());
    });
    headerActionsLayout->addWidget(repair);
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
  layout->setSpacing(12);
  auto* toolbar = new QHBoxLayout;
  auto* overview = quietLabel(QStringLiteral("Campanha atual · acompanhe os resultados por conta"));
  toolbar->addWidget(overview, 1);
  _operationRefresh = new QPushButton(QStringLiteral("Atualizar"));
  _operationRefresh->setObjectName(QStringLiteral("operationRefresh"));
  connect(_operationRefresh, &QPushButton::clicked, this, &MainWindow::refreshCurrentPage);
  toolbar->addWidget(_operationRefresh);
  _operationCreate = primaryButton(QStringLiteral("Nova campanha"));
  _operationCreate->setObjectName(QStringLiteral("operationCreate"));
  connect(_operationCreate, &QPushButton::clicked, this, [this] { _navigation->setCurrentRow(3); });
  toolbar->addWidget(_operationCreate);
  layout->addLayout(toolbar);

  _operationNotice = quietLabel(QString());
  _operationNotice->setObjectName(QStringLiteral("operationNotice"));
  _operationNotice->setTextFormat(Qt::PlainText);
  _operationNotice->setWordWrap(true);
  _operationNotice->hide();
  layout->addWidget(_operationNotice);
  auto* columns = new QHBoxLayout;
  _operationColumns = columns;
  columns->setSpacing(14);
  auto* operation = new QWidget;
  auto* main = new QVBoxLayout(operation);
  main->setContentsMargins(0, 0, 0, 0);
  main->setSpacing(10);
  auto* heading = new QHBoxLayout;
  _homePulseTitle = new QLabel(QStringLiteral("Consultando a operação…"));
  _homePulseTitle->setObjectName(QStringLiteral("operationTitle"));
  _homePulseTitle->setWordWrap(true);
  heading->addWidget(_homePulseTitle, 1);
  _operationLive = new QLabel(QStringLiteral("Consultando"));
  _operationLive->setObjectName(QStringLiteral("liveBadge"));
  _operationLive->setProperty("state", QStringLiteral("idle"));
  heading->addWidget(_operationLive);
  main->addLayout(heading);
  _homePulseBody = quietLabel(QStringLiteral("Aguardando os dados do serviço local."));
  _homePulseBody->setObjectName(QStringLiteral("operationSubtitle"));
  _homePulseBody->setTextFormat(Qt::PlainText);
  main->addWidget(_homePulseBody);
  auto* controls = new QHBoxLayout;
  _operationTotal = new QLabel(QStringLiteral("—"));
  _operationTotal->setObjectName(QStringLiteral("operationTotal"));
  controls->addWidget(_operationTotal, 1);
  _operationPause = new QPushButton(QStringLiteral("Pausar"));
  _operationPause->setObjectName(QStringLiteral("operationPause"));
  _operationPause->setEnabled(false);
  connect(_operationPause, &QPushButton::clicked, this, [this] {
    if (!_operationAvailable || _operationPausePending || _campaignStopPending) return;
    _operationPausePending = true;
    _operationControlError.clear();
    ++_operationGeneration;
    _operationPolling = false;
    _operationPause->setEnabled(false);
    _operationStop->setEnabled(false);
    _operationPause->setText(QStringLiteral("Solicitando…"));
    const QString path = _operationPauseRequested ? QStringLiteral("/api/campaigns/resume") : QStringLiteral("/api/campaigns/pause");
    const int backendGeneration = _operationBackendGeneration;
    _api.post(path, {}, [this, backendGeneration](bool ok, const QJsonDocument&, const QString& error) {
      _operationPausePending = false;
      if (_closing || backendGeneration != _operationBackendGeneration) return;
      if (!ok) {
        _operationControlError = QStringLiteral("Não foi possível confirmar o controle solicitado. ") + error;
        operationUnavailable(_operationControlError);
      }
      refreshOperation();
    });
  });
  controls->addWidget(_operationPause);
  _operationStop = new QPushButton(QStringLiteral("Parar"));
  _operationStop->setObjectName(QStringLiteral("operationStop"));
  _operationStop->setToolTip(QStringLiteral("Solicita o encerramento seguro da campanha. Acompanhe a confirmação da parada."));
  _operationStop->setEnabled(false);
  connect(_operationStop, &QPushButton::clicked, this, &MainWindow::stopOperation);
  controls->addWidget(_operationStop);
  main->addLayout(controls);
  _homePulseProgress = new QProgressBar;
  _homePulseProgress->setObjectName(QStringLiteral("operationProgress"));
  _homePulseProgress->setRange(0, 100);
  _homePulseProgress->setTextVisible(false);
  _homePulseProgress->setFixedHeight(8);
  _operationProgressRow = new QWidget;
  auto* progress = new QHBoxLayout(_operationProgressRow);
  progress->setContentsMargins(0, 0, 0, 0);
  progress->addWidget(_homePulseProgress, 1);
  auto* percent = new QLabel(QStringLiteral("0%"));
  percent->setMinimumWidth(40);
  progress->addWidget(percent);
  connect(_homePulseProgress, &QProgressBar::valueChanged, percent, [percent](int value) { percent->setText(QStringLiteral("%1%").arg(value)); });
  _operationProgressRow->hide();
  main->addWidget(_operationProgressRow);
  _operationTrack = new OperationTrack;
  main->addWidget(_operationTrack);
  auto* filters = new QHBoxLayout;
  _operationSearch = new QLineEdit;
  _operationSearch->setObjectName(QStringLiteral("operationSearch"));
  _operationSearch->setPlaceholderText(QStringLiteral("Buscar conta, clipe ou sessão"));
  _operationSearch->setClearButtonEnabled(true);
  _operationSearch->setAccessibleName(QStringLiteral("Buscar na operação"));
  filters->addWidget(_operationSearch, 1);
  _operationAttention = new QCheckBox(QStringLiteral("Só pendências"));
  _operationAttention->setObjectName(QStringLiteral("operationAttention"));
  filters->addWidget(_operationAttention);
  _operationDetails = new QPushButton(QStringLiteral("Detalhes"));
  _operationDetails->setObjectName(QStringLiteral("operationDetails"));
  _operationDetails->setEnabled(false);
  filters->addWidget(_operationDetails);
  main->addLayout(filters);
  connect(_operationSearch, &QLineEdit::textChanged, this, [this] { filterOperationRows(); });
  connect(_operationAttention, &QCheckBox::toggled, this, [this] { filterOperationRows(); });
  connect(_operationDetails, &QPushButton::clicked, this, &MainWindow::showOperationAccount);
  _operationEmpty = quietLabel(QStringLiteral("Nenhuma campanha carregada. Conecte suas contas e revise uma prévia para começar."));
  _operationEmpty->setWordWrap(true);
  main->addWidget(_operationEmpty);
  _operationTable = new QTableWidget(0, 4);
  _operationTable->setHorizontalHeaderLabels({QStringLiteral("Conta"), QStringLiteral("Etapa atual"), QStringLiteral("Transferência"), QStringLiteral("Resultados")});
  _operationTable->verticalHeader()->hide();
  _operationTable->setItemDelegate(new OperationRowDelegate(_operationTable));
  _operationTable->setEditTriggers(QAbstractItemView::NoEditTriggers);
  _operationTable->setSelectionBehavior(QAbstractItemView::SelectRows);
  _operationTable->setSelectionMode(QAbstractItemView::SingleSelection);
  _operationTable->setShowGrid(false);
  _operationTable->setMinimumHeight(280);
  _operationTable->horizontalHeader()->setSectionResizeMode(QHeaderView::Stretch);
  _operationTable->horizontalHeader()->setMinimumSectionSize(120);
  _operationTable->horizontalHeader()->setSectionResizeMode(0, QHeaderView::Interactive);
  _operationTable->setColumnWidth(0, 280);
  _operationTable->setObjectName(QStringLiteral("operationTable"));
  _operationTable->horizontalHeader()->setFixedHeight(36);
  _operationTable->horizontalHeader()->setDefaultAlignment(Qt::AlignLeft | Qt::AlignVCenter);
  _operationTable->setAccessibleName(QStringLiteral("Contas da campanha atual"));
  connect(_operationTable, &QTableWidget::itemSelectionChanged, this, [this] { _operationDetails->setEnabled(_operationTable->currentRow() >= 0 && !_operationTable->isRowHidden(_operationTable->currentRow())); });
  connect(_operationTable, &QTableWidget::cellDoubleClicked, this, [this] { showOperationAccount(); });
  main->addWidget(_operationTable, 1);
  _operationVisible = quietLabel(QStringLiteral("Nenhuma conta nesta execução"));
  main->addWidget(_operationVisible);
  _operationIdleSpace = new QWidget;
  auto* setup = new QVBoxLayout(_operationIdleSpace);
  setup->setContentsMargins(0, 10, 0, 0);
  auto* steps = quietLabel(QStringLiteral("1. Conecte as contas que participarão.\n\n2. Confira a biblioteca e os requisitos.\n\n3. Revise a prévia antes de iniciar a campanha."));
  setup->addWidget(steps);
  setup->addStretch();
  main->addWidget(_operationIdleSpace, 1);
  _operationIdleSpace->hide();
  _operationStages = new QLabel(operation);
  _operationStages->hide();
  auto* surface = card(QString(), operation);
  surface->setObjectName(QStringLiteral("operationSurface"));
  columns->addWidget(surface, 72);

  auto* inspector = new QFrame;
  _operationInspector = inspector;
  inspector->setObjectName(QStringLiteral("operationInspector"));
  inspector->setMinimumWidth(280);
  inspector->setMaximumWidth(390);
  auto* context = new QVBoxLayout(inspector);
  context->setContentsMargins(20, 20, 20, 20);
  context->setSpacing(12);
  auto* kicker = new QLabel(QStringLiteral("PRÓXIMO PASSO"));
  kicker->setObjectName(QStringLiteral("heroEyebrow"));
  context->addWidget(kicker);
  _operationContextTitle = new QLabel(QStringLiteral("Prepare sua operação"));
  _operationContextTitle->setObjectName(QStringLiteral("inspectorTitle"));
  _operationContextTitle->setWordWrap(true);
  context->addWidget(_operationContextTitle);
  _homeAccountStep = new QLabel(QStringLiteral("Conecte suas contas e prepare o conteúdo."));
  _homeAccountStep->setObjectName(QStringLiteral("heroDescription"));
  _homeAccountStep->setTextFormat(Qt::PlainText);
  _homeAccountStep->setWordWrap(true);
  context->addWidget(_homeAccountStep);
  _homeNextAction = primaryButton(QStringLiteral("Consultando…"));
  _homeNextAction->setEnabled(false);
  connect(_homeNextAction, &QPushButton::clicked, this, [this] { _navigation->setCurrentRow(_homeDestination); });
  context->addWidget(_homeNextAction);
  auto* eventsTitle = new QLabel(QStringLiteral("ATIVIDADE RECENTE"));
  eventsTitle->setObjectName(QStringLiteral("heroEyebrow"));
  context->addWidget(eventsTitle);
  _operationTimeline = new OperationTimeline;
  context->addWidget(_operationTimeline, 1);
  _operationFeed = new QLabel(inspector);
  _operationFeed->setTextFormat(Qt::PlainText);
  _operationFeed->hide();
  auto* balanceCaption = new QLabel(QStringLiteral("DISPONÍVEL PARA SAQUE"));
  balanceCaption->setObjectName(QStringLiteral("inspectorBalanceCaption"));
  context->addWidget(balanceCaption);
  _operationBalance = new QLabel(QStringLiteral("US$ —"));
  _operationBalance->setObjectName(QStringLiteral("inspectorBalance"));
  context->addWidget(_operationBalance);
  _operationBalanceNote = new QLabel(QStringLiteral("Consulte os saldos das suas contas."));
  _operationBalanceNote->setObjectName(QStringLiteral("heroDescription"));
  _operationBalanceNote->setTextFormat(Qt::PlainText);
  _operationBalanceNote->setWordWrap(true);
  context->addWidget(_operationBalanceNote);
  auto* balances = new QPushButton(QStringLiteral("Abrir Carteira →"));
  balances->setObjectName(QStringLiteral("inspectorBalanceAction"));
  connect(balances, &QPushButton::clicked, this, [this] { _navigation->setCurrentRow(6); });
  context->addWidget(balances);
  columns->addWidget(inspector, 28);
  layout->addLayout(columns, 1);
  auto* hidden = new QWidget(body);
  auto* stats = new QHBoxLayout(hidden);
  stats->addWidget(metric(QStringLiteral("—"), QStringLiteral("contas cadastradas"), &_homeAccounts));
  stats->addWidget(metric(QStringLiteral("—"), QStringLiteral("campanhas no histórico"), &_homeCampaigns));
  stats->addWidget(metric(QStringLiteral("—"), QStringLiteral("envios confirmados"), &_homeSuccess));
  hidden->hide();
  _homeRecent = new QLabel(body);
  _homeRecent->hide();
  _homeSync = quietLabel(QStringLiteral("Aguardando leitura"));
  _homeSync->setWordWrap(true);
  layout->addWidget(_homeSync);
  auto* scroll = new QScrollArea;
  scroll->setObjectName(QStringLiteral("operationScroll"));
  scroll->setWidgetResizable(true);
  scroll->setFrameShape(QFrame::NoFrame);
  scroll->setWidget(body);
  auto* page = pageShell(QStringLiteral("Visão da operação"), QString(), scroll);
  page->setObjectName(QStringLiteral("operationPage"));
  return page;
}

void MainWindow::operationUnavailable(const QString& error) {
  _operationAvailable = false;
  _operationRefresh->setEnabled(true);
  _operationPause->setEnabled(false);
  _operationStop->setEnabled(false);
  _homeNextAction->setEnabled(false);
  _operationCreate->setEnabled(false);
  _operationNotice->setText(QStringLiteral("Leitura indisponível. Os dados exibidos são da última consulta; os controles aguardam uma nova leitura. ") + error);
  _operationNotice->show();
  _operationLive->setText(QStringLiteral("Sem conexão"));
  _operationLive->setProperty("state", QStringLiteral("error"));
  _operationLive->style()->unpolish(_operationLive);
  _operationLive->style()->polish(_operationLive);
  _homeSync->setText(QStringLiteral("Atualização interrompida · tentando novamente"));
}

void MainWindow::refreshOperation() {
  if (_campaignResetPending) return;
  const int generation = ++_operationGeneration;
  _operationPolling = true;
  _api.get(QStringLiteral("/api/campaigns/current"), [this, generation](bool ok, const QJsonDocument& doc, const QString& error) {
    if (_closing || generation != _operationGeneration) return;
    _operationPolling = false;
    if (!ok) return operationUnavailable(error);
    renderOperation(doc.object());
    if (QDateTime::currentMSecsSinceEpoch()-_operationBalanceReadAt>=30000) loadOperationBalance();
  });
}

void MainWindow::stopOperation() {
  if (!_operationAvailable || _operationPausePending || _campaignStopPending) return;
  _campaignStopPending = true;
  _operationControlError.clear();
  ++_operationGeneration;
  _operationPolling = false;
  _operationPause->setEnabled(false);
  _operationStop->setEnabled(false);
  _operationStop->setText(QStringLiteral("Solicitando…"));
  const int backendGeneration = _operationBackendGeneration;
  _api.post(QStringLiteral("/api/campaigns/stop"), {}, [this, backendGeneration](bool ok, const QJsonDocument&, const QString& error) {
    _campaignStopPending = false;
    if (_closing || backendGeneration != _operationBackendGeneration) return;
    if (!ok) {
      _operationControlError = QStringLiteral("Não foi possível confirmar a parada. ") + error;
      operationUnavailable(_operationControlError);
    }
    refreshOperation();
    if (_pages->currentIndex() == 3) pollCampaign();
  });
}

void MainWindow::renderOperation(const QJsonObject& snapshot) {
  if (!OperationSummary::valid(snapshot)) return operationUnavailable(QStringLiteral("O serviço retornou dados incompletos ou inválidos."));
  _operationSnapshot = snapshot;
  _operationAvailable = true;
  _operationRefresh->setEnabled(true);
  _operationNotice->setText(_operationControlError);
  _operationNotice->setVisible(!_operationControlError.isEmpty());
  const auto state = snapshot.value("state").toString();
  const bool active = state == "running";
  _campaignResetRunnerBusy = active || state == "stopping";
  _operationPause->setVisible(active);
  _operationStop->setVisible(active || state == "stopping");
  _operationPauseRequested = active && snapshot.value("pause_requested").toBool();
  const bool pendingCommand = _operationPausePending || _campaignStopPending;
  _operationPause->setEnabled(active && !pendingCommand);
  _operationStop->setEnabled(active && !pendingCommand);
  if (!_operationPausePending) _operationPause->setText(_operationPauseRequested ? QStringLiteral("Retomar") : QStringLiteral("Pausar"));
  if (!_campaignStopPending) _operationStop->setText(state == "stopping" ? QStringLiteral("Encerrando…") : QStringLiteral("Parar"));
  const auto summary = OperationSummary::from(_homeAccountSnapshot, snapshot);
  _homePulseTitle->setText(summary.title);
  _homePulseBody->setText(summary.detail);
  _homePulseProgress->setValue(summary.progress);
  _operationProgressRow->setVisible(active || state == "stopping" || ((state == "done" || state == "stopped" || state == "error") && !snapshot.value("operation").toObject().value("accounts").toArray().isEmpty()));
  _homeDestination = summary.destination;
  _homeNextAction->setText(summary.action + QStringLiteral(" →"));
  _homeNextAction->setEnabled(!pendingCommand);
  _operationCreate->setEnabled(!active && state != "stopping" && !pendingCommand);
  _operationCreate->setToolTip(active || state == "stopping" ? QStringLiteral("Aguarde o encerramento da campanha atual.") : QString());
  const QString badge = state == "stopping" ? QStringLiteral("Encerrando") : _operationPauseRequested ? QStringLiteral("Pausa solicitada")
      : active ? QStringLiteral("Ao vivo") : state == "done" ? (summary.title.contains(QStringLiteral("pendências")) ? QStringLiteral("Com pendências") : QStringLiteral("Encerrada"))
      : state == "stopped" ? QStringLiteral("Interrompida") : state == "error" ? QStringLiteral("Com pendências") : QStringLiteral("Sem campanha");
  _operationLive->setText(badge);
  _operationLive->setProperty("state", state == "running" && _operationPauseRequested ? QStringLiteral("paused")
      : state == "done" && summary.title.contains(QStringLiteral("pendências")) ? QStringLiteral("error") : state);
  _operationLive->style()->unpolish(_operationLive);
  _operationLive->style()->polish(_operationLive);
  _operationLive->setToolTip(_operationPauseRequested ? QStringLiteral("Pausa solicitada; requisições em andamento podem terminar.") : QString());
  const auto rows = snapshot.value("operation").toObject().value("accounts").toArray();
  QJsonObject counts;
  for (const auto value : rows) { const auto phase = value.toObject().value("state").toString(); counts[phase] = counts.value(phase).toInt() + 1; }
  int attention = 0;
  for (const auto value : rows) attention += OperationSummary::attention(value.toObject());
  counts["_attention"] = attention;
  _operationTrack->setCounts(counts);
  _operationTrack->setVisible(!rows.isEmpty());
  _operationContextTitle->setText(state == "error" || summary.title.contains(QStringLiteral("pendências")) ? QStringLiteral("Revisar pendências")
      : state == "done" || state == "stopped" ? QStringLiteral("Conferir resultados")
      : state == "stopping" ? QStringLiteral("Aguardar encerramento")
      : active ? QStringLiteral("Acompanhar a campanha") : _homeAccountSnapshot.isEmpty() ? QStringLiteral("Conectar suas contas") : QStringLiteral("Preparar a campanha"));
  _homeAccountStep->setText(_operationPauseRequested ? QStringLiteral("Use Retomar para liberar os próximos envios.")
      : active ? QStringLiteral("Transferência concluída e recibo confirmado são etapas separadas.")
      : state == "done" || state == "stopped" || state == "error" ? QStringLiteral("O histórico reúne os recibos e as falhas desta execução.")
      : QStringLiteral("Conecte as contas, confira os requisitos e revise a prévia antes de iniciar."));
  const auto totals = snapshot.value("totals").toObject();
  if (rows.isEmpty() && state == "idle") _operationTotal->setText(QStringLiteral("Nenhuma campanha em execução"));
  else if (snapshot.value("run_until_exhausted").toBool()) {
    _operationTotal->setText(QStringLiteral("%1 envios confirmados").arg(totals.value("ok_sends").toDouble(), 0, 'f', 0));
  } else if (totals.value("progress_unit").toString() == "seconds" && OperationSummary::number(totals.value("progress_completed")) && OperationSummary::number(totals.value("progress_target"))) {
    _operationTotal->setText(QStringLiteral("%1 / %2 h confirmadas")
        .arg(QLocale(QLocale::Portuguese, QLocale::Brazil).toString(totals.value("progress_completed").toDouble() / 3600., 'f', 2),
             QLocale(QLocale::Portuguese, QLocale::Brazil).toString(totals.value("progress_target").toDouble() / 3600., 'f', 2)));
  } else if (OperationSummary::number(totals.value("ok_sends")) && OperationSummary::number(totals.value("total_sends"))) {
    _operationTotal->setText(QStringLiteral("%1 / %2 envios confirmados").arg(totals.value("ok_sends").toDouble(), 0, 'f', 0).arg(totals.value("total_sends").toDouble(), 0, 'f', 0));
  } else _operationTotal->setText(QStringLiteral("Meta aguardando confirmação"));
  const auto selected = _operationTable->currentRow() >= 0 && _operationTable->item(_operationTable->currentRow(), 0)
      ? _operationTable->item(_operationTable->currentRow(), 0)->text() : QString();
  const int scroll = _operationTable->verticalScrollBar()->value();
  const int horizontal = _operationTable->horizontalScrollBar()->value();
  const QSignalBlocker blocker(_operationTable);
  _operationTable->setRowCount(rows.size());
  const QHash<QString, QString> labels{{"queued", "Na fila"}, {"waiting", "Aguardando"}, {"preparing", "Preparando"},
    {"sending", "Enviando"}, {"confirming", "Confirmando"}, {"confirmed", "Confirmado"}, {"failed", "Falhou"},
    {"skipped", "Pulado"}, {"recovering", "Recuperando"}, {"excluded", "Excluída"}, {"pending", "Pendente"}, {"unconfirmed", "Sem confirmação"}};
  int selectedRow = -1;
  for (int i = 0; i < rows.size(); ++i) {
    const auto row = rows[i].toObject();
    const auto phase = row.value("state").toString();
    const auto value = row.value("progress");
    QString result;
    const int confirmed = row.value("confirmed").toInt();
    const int failed = row.value("failed").toInt();
    const int skipped = row.value("skipped").toInt();
    if (confirmed || failed || skipped) result = QStringLiteral("%1 OK · %2 falhas · %3 pulados").arg(confirmed).arg(failed).arg(skipped);
    else result = phase == "confirming" ? QStringLiteral("Aguardando recibo") : phase == "sending" ? QStringLiteral("Envio em andamento") : labels.value(phase);
    const QStringList cells{row.value("email").toString(), labels.value(phase),
        phase == "sending" && value.isDouble() ? QStringLiteral("%1%").arg(value.toInt()) : QStringLiteral("—"), result};
    for (int column = 0; column < cells.size(); ++column) {
      auto* cell = new QTableWidgetItem(cells[column]);
      cell->setData(Qt::UserRole, row);
      cell->setToolTip(QStringLiteral("%1\n%2\nClipe: %3\nSessão: %4\n%5")
          .arg(row.value("email").toString(), labels.value(phase), row.value("clip_uid").toString(), row.value("session_id").toString(), row.value("detail").toString()));
      _operationTable->setItem(i, column, cell);
    }
    _operationTable->setRowHeight(i, 56);
    if (row.value("email").toString() == selected) selectedRow = i;
  }
  if (selectedRow >= 0) _operationTable->setCurrentCell(selectedRow, 0);
  else { _operationTable->clearSelection(); _operationTable->setCurrentCell(-1, -1); }
  filterOperationRows();
  _operationTable->verticalScrollBar()->setValue(scroll);
  _operationTable->horizontalScrollBar()->setValue(horizontal);
  _operationDetails->setEnabled(selectedRow >= 0 && !_operationTable->isRowHidden(selectedRow));
  _operationTimeline->setEvents(snapshot.value("events").toArray());
  if (_campaignReset) updateCampaignActions();
  _homeSync->setText(QStringLiteral("Atualizado às %1 · transferência não comprova recebimento").arg(QTime::currentTime().toString(QStringLiteral("HH:mm:ss"))));
}

void MainWindow::filterOperationRows() {
  if (!_operationTable) return;
  const auto query = _operationSearch->text().trimmed();
  int visible = 0;
  for (int i = 0; i < _operationTable->rowCount(); ++i) {
    const auto item = _operationTable->item(i, 0);
    if (!item) continue;
    const auto row = item->data(Qt::UserRole).toJsonObject();
    const auto searchable = row.value("email").toString() + " " + row.value("clip_uid").toString() + " " + row.value("session_id").toString();
    const bool attention = OperationSummary::attention(row);
    const bool show = searchable.contains(query, Qt::CaseInsensitive) && (!_operationAttention->isChecked() || attention);
    _operationTable->setRowHidden(i, !show);
    visible += show;
  }
  _operationEmpty->setVisible(visible == 0);
  _operationEmpty->setText(_operationTable->rowCount() ? QStringLiteral("Nenhuma conta corresponde aos filtros.") : QStringLiteral("Nenhuma conta nesta execução. Conecte suas contas e revise uma prévia para começar."));
  _operationTable->setVisible(_operationTable->rowCount() > 0);
  _operationIdleSpace->setVisible(_operationTable->rowCount() == 0);
  _operationSearch->setEnabled(_operationTable->rowCount() > 0);
  _operationAttention->setEnabled(_operationTable->rowCount() > 0);
  _operationSearch->setVisible(_operationTable->rowCount() > 0);
  _operationAttention->setVisible(_operationTable->rowCount() > 0);
  _operationDetails->setVisible(_operationTable->rowCount() > 0);
  _operationVisible->setVisible(_operationTable->rowCount() > 0);
  _operationVisible->setText(QStringLiteral("%1 de %2 contas · selecione uma conta para ver os detalhes").arg(visible).arg(_operationTable->rowCount()));
  const int current = _operationTable->currentRow();
  _operationDetails->setEnabled(current >= 0 && !_operationTable->isRowHidden(current));
}

void MainWindow::showOperationAccount() {
  const int index = _operationTable->currentRow();
  if (index < 0 || _operationTable->isRowHidden(index) || !_operationTable->item(index, 0)) return;
  const auto row = _operationTable->item(index, 0)->data(Qt::UserRole).toJsonObject();
  QDialog dialog(this);
  dialog.setObjectName(QStringLiteral("operationAccountDialog"));
  dialog.setWindowTitle(QStringLiteral("Detalhes da conta na operação"));
  dialog.resize(640, 480);
  auto* layout = new QVBoxLayout(&dialog);
  auto* title = new QLabel(row.value("email").toString());
  title->setObjectName(QStringLiteral("cardTitle"));
  title->setTextFormat(Qt::PlainText);
  title->setWordWrap(true);
  title->setTextInteractionFlags(Qt::TextSelectableByMouse);
  layout->addWidget(title);
  auto* text = new QPlainTextEdit;
  text->setReadOnly(true);
  text->setPlainText(QStringLiteral("Etapa: %1\n%2\n\nResultados nesta campanha\nConfirmados: %3\nFalhas: %4\nPulados: %5\n\nClipe atual: %6\nSessão: %7\n\nA porcentagem mede a transferência do arquivo. Recebimento e avaliação são confirmados separadamente.%8")
      .arg(_operationTable->item(index, 1)->text(), row.value("detail").toString()).arg(row.value("confirmed").toInt()).arg(row.value("failed").toInt()).arg(row.value("skipped").toInt())
      .arg(row.value("clip_uid").toString(QStringLiteral("Não informado")), row.value("session_id").toString(QStringLiteral("Não informada")),
          _operationAvailable ? QString() : QStringLiteral("\n\nLeitura indisponível: estes dados são da última consulta.")));
  layout->addWidget(text, 1);
  auto* buttons = new QDialogButtonBox(QDialogButtonBox::Close);
  connect(buttons, &QDialogButtonBox::rejected, &dialog, &QDialog::reject);
  layout->addWidget(buttons);
  dialog.exec();
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
      "Cole o token da API Mail da Hostinger para conectar ou renovar o acesso. O QMoney identifica sozinho as caixas, os endereços e "
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
      QStringLiteral("Cole um token novo para conectar ou renovar a caixa"));
  hostForm->addRow(QStringLiteral("Token Hostinger"), _hostingerToken);
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
  _repairInstall = new QPushButton(QStringLiteral("Corrigir instalação"));
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
  _campaignIndicator = new QFrame;
  _campaignIndicator->setObjectName(QStringLiteral("campaignIndicator"));
  auto* indicatorLayout = new QVBoxLayout(_campaignIndicator);
  indicatorLayout->setContentsMargins(18, 14, 18, 14);
  indicatorLayout->setSpacing(7);
  _campaignIndicatorTitle = new QLabel;
  _campaignIndicatorTitle->setObjectName(QStringLiteral("campaignIndicatorTitle"));
  _campaignIndicatorTitle->setWordWrap(true);
  _campaignIndicatorDetail = new QLabel;
  _campaignIndicatorDetail->setWordWrap(true);
  _campaignIndicatorProgress = new QProgressBar;
  _campaignIndicatorProgress->setTextVisible(false);
  auto* indicatorHeader = new QHBoxLayout;
  _campaignIndicatorIcon = new CampaignStatusIcon;
  indicatorHeader->addWidget(_campaignIndicatorIcon);
  indicatorHeader->addWidget(_campaignIndicatorTitle, 1);
  _campaignIndicatorMetric = new QLabel;
  _campaignIndicatorMetric->setObjectName(QStringLiteral("campaignIndicatorMetric"));
  indicatorHeader->addWidget(_campaignIndicatorMetric);
  auto* details = new QToolButton;
  details->setText(QStringLiteral("Detalhes"));
  details->setObjectName(QStringLiteral("campaignDetails"));
  details->setCheckable(true);
  details->setArrowType(Qt::RightArrow);
  details->setToolButtonStyle(Qt::ToolButtonTextBesideIcon);
  details->setAccessibleName(QStringLiteral("Mostrar detalhes da campanha"));
  connect(details, &QToolButton::toggled, this, [this, details](bool expanded) {
    _campaignIndicatorDetail->setVisible(expanded);
    details->setArrowType(expanded ? Qt::DownArrow : Qt::RightArrow);
    details->setAccessibleName(expanded ? QStringLiteral("Ocultar detalhes da campanha")
                                      : QStringLiteral("Mostrar detalhes da campanha"));
  });
  indicatorHeader->addWidget(details);
  indicatorLayout->addLayout(indicatorHeader);
  indicatorLayout->addWidget(_campaignIndicatorProgress);
  indicatorLayout->addWidget(_campaignIndicatorDetail);
  _campaignIndicatorDetail->hide();
  bodyLayout->addWidget(_campaignIndicator);
  auto* tabs = new QTabWidget;
  _campaignTabs = tabs;
  setCampaignIndicator(QStringLiteral("Nenhuma campanha em andamento"),
                       QStringLiteral("Revise o conteúdo e as contas para iniciar."), QStringLiteral("idle"));
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
  _dataset->addItem(QStringLiteral("Ego4D"), QStringLiteral("ego4d"));
  _dataset->addItem(QStringLiteral("Nymeria"), QStringLiteral("nymeria"));
  _dataset->addItem(QStringLiteral("Ambos"), QStringLiteral("ambos"));
  _dataset->setCurrentIndex(0);
  connect(_dataset, &QComboBox::currentIndexChanged, this,
          [this] { _taskReload.start(); });
  sourceLayout->addWidget(new QLabel(QStringLiteral("Origem")));
  sourceLayout->addWidget(_dataset, 1);
  _contentMode = new ComboBox;
  configureCombo(_contentMode, 190);
  _contentMode->addItem(QStringLiteral("Sob demanda"), QStringLiteral("both"));
  _contentMode->addItem(QStringLiteral("Somente cache pronto"), QStringLiteral("cache"));
  _contentMode->addItem(QStringLiteral("Catálogo do dataset"), QStringLiteral("dataset"));
  _contentMode->setToolTip(QStringLiteral(
      "Sob demanda reaproveita arquivos locais ou baixa uma fonte quando necessário, prepara, envia e libera a mídia após confirmar todas as contas. "
      "Somente cache usa clipes já preparados. Catálogo seleciona clipes elegíveis sem priorizar os arquivos locais."));
  connect(_contentMode, &QComboBox::currentIndexChanged, this,
          [this] { _taskReload.start(); });
  auto* reloadTasks = new QPushButton(QStringLiteral("Recarregar categorias"));
  reloadTasks->setObjectName(QStringLiteral("campaignReloadTasks"));
  connect(reloadTasks, &QPushButton::clicked, this, [this] {
    _taskCatalogFallbackSelection.clear();
    _taskCatalogFallbackAnchor.clear();
    _taskCatalogFallbackAttempted.clear();
    _taskCatalogFallbackExhausted = false;
    _taskCatalogForceRefresh = true;
    loadTasks();
  });
  sourceLayout->addWidget(reloadTasks);
  _campaignReset = new QPushButton(QStringLiteral("Reset completo"));
  _campaignReset->setObjectName(QStringLiteral("campaignFullReset"));
  _campaignReset->setToolTip(QStringLiteral(
      "Apaga os registros locais das campanhas e envios. Mantém contas e Biblioteca."));
  connect(_campaignReset, &QPushButton::clicked, this, &MainWindow::resetCampaigns);
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
    updateCampaignActions();
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
  auto* allTasks = new QPushButton(QStringLiteral("Todas compatíveis"));
  allTasks->setObjectName(QStringLiteral("campaignSelectAllCompatible"));
  allTasks->setToolTip(QStringLiteral("Inclui todas as categorias compatíveis com a origem e a duração, inclusive após recarregar o catálogo."));
  allTasks->setFlat(true);
  allTasks->setFixedHeight(36);
  connect(allTasks, &QPushButton::clicked, this, &MainWindow::selectAllCompatibleTasks);
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
    updateCampaignActions();
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
  auto* campaignEnd = quietLabel(QStringLiteral("A campanha continua até acabar o conteúdo elegível ou você apertar Parar."));
  campaignEnd->setObjectName(QStringLiteral("campaignUntilExhausted"));
  campaignEnd->setWordWrap(true);
  form->addRow(QStringLiteral("Encerramento"), campaignEnd);
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
  _cleanupAfter = new QCheckBox(QStringLiteral("Liberar mídia após confirmar os envios do recorte"));
  _cleanupAfter->setChecked(true);
  _cleanupAfter->setToolTip(QStringLiteral(
      "Apaga fontes, recortes e derivados após a confirmação de todas as contas. Arquivos em uso ou necessários para recuperar um envio pendente são preservados."));
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
  connect(_hourStart, &QSpinBox::valueChanged, this, [this](int start) {
    if (_hourEnd->value() <= start) _hourEnd->setValue(start + 1);
  });
  connect(_hourEnd, &QSpinBox::valueChanged, this, [this](int end) {
    if (_hourStart->value() >= end) _hourStart->setValue(end - 1);
  });
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
      "Escolha conteúdo e contas para começar."));
  _campaignCurrent->setWordWrap(true);
  executionCopy->addWidget(_campaignCurrent);
  executionHead->addLayout(executionCopy, 1);
  _campaignStats = quietLabel(QStringLiteral("0 concluídos · 0 falhas"));
  _campaignStats->setObjectName(QStringLiteral("campaignStats"));
  _campaignStats->setWordWrap(true);
  _campaignStats->setAlignment(Qt::AlignRight | Qt::AlignVCenter);
  executionHead->addWidget(_campaignStats);
  executionLayout->addLayout(executionHead);
  _campaignProgress = new QProgressBar;
  _campaignProgress->setObjectName(QStringLiteral("campaignProgress"));
  _campaignProgress->setRange(0, 100);
  _campaignProgress->setValue(0);
  _campaignProgress->setFormat(QStringLiteral("Nenhum envio iniciado"));
  executionLayout->addWidget(_campaignProgress);
  _campaignPreviewPanel = new QWidget;
  _campaignPreviewPanel->setObjectName(QStringLiteral("campaignPreviewPanel"));
  auto* previewLayout = new QVBoxLayout(_campaignPreviewPanel);
  previewLayout->setContentsMargins(0, 0, 0, 0);
  previewLayout->setSpacing(6);
  _campaignReceiptStats = quietLabel(QStringLiteral("Recibos atuais ainda não consultados."));
  _campaignReceiptStats->setObjectName(QStringLiteral("campaignReceiptStats"));
  _campaignReceiptStats->setWordWrap(true);
  previewLayout->addWidget(_campaignReceiptStats);
  _campaignPreviewStage = new QLabel(QStringLiteral("Prévias ainda não consultadas"));
  _campaignPreviewStage->setObjectName(QStringLiteral("campaignPreviewStage"));
  previewLayout->addWidget(_campaignPreviewStage);
  _campaignPreviewStats = quietLabel(QString());
  _campaignPreviewStats->setObjectName(QStringLiteral("campaignPreviewStats"));
  _campaignPreviewStats->setWordWrap(true);
  previewLayout->addWidget(_campaignPreviewStats);
  _campaignPreviewProgress = new QProgressBar;
  _campaignPreviewProgress->setObjectName(QStringLiteral("campaignPreviewProgress"));
  _campaignPreviewProgress->setRange(0, 100);
  _campaignPreviewProgress->setValue(0);
  previewLayout->addWidget(_campaignPreviewProgress);
  _campaignPreviewDetail = quietLabel(QString());
  _campaignPreviewDetail->setObjectName(QStringLiteral("campaignPreviewDetail"));
  _campaignPreviewDetail->setWordWrap(true);
  previewLayout->addWidget(_campaignPreviewDetail);
  _campaignPreviewPanel->hide();
  executionLayout->addWidget(_campaignPreviewPanel);
  _campaignFeed = new QPlainTextEdit;
  _campaignFeed->setObjectName(QStringLiteral("campaignTimeline"));
  _campaignFeed->setReadOnly(true);
  _campaignFeed->setMaximumBlockCount(500);
  _campaignFeed->setMinimumHeight(190);
  _campaignFeed->setPlaceholderText(QStringLiteral(
      "Atividade da campanha"));
  executionLayout->addWidget(_campaignFeed);
  auto* actions = new QHBoxLayout;
  actions->addStretch();
  _campaignStop = new QPushButton(QStringLiteral("Parar com segurança"));
  _campaignStop->setEnabled(false);
  connect(_campaignStop, &QPushButton::clicked, this, [this] {
    if (_campaignStopPending) return;
    _campaignStopPending = true;
    _campaignStop->setEnabled(false);
    _campaignStop->setText(QStringLiteral("Solicitando parada…"));
    _api.post(QStringLiteral("/api/campaigns/stop"), {}, [this](bool ok, const QJsonDocument&, const QString& error) {
      _campaignStopPending = false;
      _campaignStop->setText(QStringLiteral("Parar com segurança"));
      if (!ok) showError(QStringLiteral("Não foi possível confirmar a parada"), error);
      if (ok) setStatus(QStringLiteral("Parada solicitada; o envio atual será concluído."));
      pollCampaign();
    });
  });
  actions->addWidget(_campaignStop);
  _campaignOriginal = new QPushButton(QStringLiteral("Usar captura original"));
  _campaignOriginal->setAccessibleName(QStringLiteral("Selecionar captura original MP4 e ZIP para uma conta"));
  _campaignOriginal->setToolTip(QStringLiteral("Revisa arquivos originais preservando seus bytes, identificadores e clocks."));
  connect(_campaignOriginal, &QPushButton::clicked, this, &MainWindow::chooseOriginalCapture);
  actions->addWidget(_campaignOriginal);
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
    _minDuration->setValue(draft.value(QStringLiteral("min_duration")).toInt(1));
    _maxDuration->setValue(draft.value(QStringLiteral("max_duration")).toInt(30));
    _delaySeconds->setValue(draft.value(QStringLiteral("delay_seconds")).toInt());
    _cleanupAfter->setChecked(draft.value(QStringLiteral("cleanup")).toBool(true));
    _activeHours->setChecked(draft.value(QStringLiteral("active_hours")).toBool(true));
    _hourStart->setValue(draft.value(QStringLiteral("hour_start")).toInt(7));
    _hourEnd->setValue(draft.value(QStringLiteral("hour_end")).toInt(18));
  }
  QSettings campaignSettings;
  const QString allCompatibleNextStart = QStringLiteral("campaign/useAllCompatibleNextStart");
  if (campaignSettings.value(allCompatibleNextStart, false).toBool()) {
    const QSignalBlocker datasetBlocker(_dataset);
    _dataset->setCurrentIndex(_dataset->findData(QStringLiteral("ambos")));
    _campaignSelectedTaskIds.clear();
    _campaignTaskSelectionTouched = false;
    // The destination list is not loaded yet. Update the restored draft
    // directly so this one-time preference never clears the saved accounts.
    auto migratedDraft = draft;
    migratedDraft.insert(QStringLiteral("dataset"), QStringLiteral("ambos"));
    migratedDraft.remove(QStringLiteral("target_hours"));
    migratedDraft.insert(QStringLiteral("run_until_exhausted"), true);
    migratedDraft.insert(QStringLiteral("tasks"), QJsonArray{});
    migratedDraft.insert(QStringLiteral("tasks_touched"), false);
    campaignSettings.setValue(QStringLiteral("campaign/draft"), QJsonDocument(migratedDraft).toJson(QJsonDocument::Compact));
    campaignSettings.remove(allCompatibleNextStart);
    campaignSettings.sync();
  }
  form->setRowVisible(_delaySeconds, _delayMode->currentData().toString() == QStringLiteral("fixed"));
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
  for (auto* spin : {_minDuration, _maxDuration, _delaySeconds, _hourStart, _hourEnd})
    connect(spin, &QSpinBox::valueChanged, this, scheduleDraft);
  connect(_delayMode, &QComboBox::currentIndexChanged, this, scheduleDraft);
  connect(_accountWorkers, &QComboBox::currentIndexChanged, this, scheduleDraft);
  connect(_cleanupAfter, &QCheckBox::toggled, this, scheduleDraft);
  connect(_activeHours, &QCheckBox::toggled, this, scheduleDraft);

  const auto pendingStart = QSettings().value(QStringLiteral("campaign/pendingStart")).toByteArray();
  if (!pendingStart.isEmpty()) {
    const auto saved = QJsonDocument::fromJson(pendingStart).object();
    const auto identity = saved.value(QStringLiteral("start_request_id")).toString();
    if (saved.value(QStringLiteral("schema_version")) == QJsonValue(1)
        && QRegularExpression(QStringLiteral("^[A-Za-z0-9_-]{1,160}$")).match(identity).hasMatch())
      _campaignRequestedPreflight = identity;
    _campaignStartUncertain = true;
    updateCampaignActions();
    setCampaignIndicator(QStringLiteral("Início anterior requer consulta"),
        QStringLiteral("O identificador salvo será consultado antes de permitir outro início."), QStringLiteral("unknown"));
  }

  return pageShell(QStringLiteral("Nova campanha"),
                   QStringLiteral("Escolha o conteúdo, calibre a operação e acompanhe cada envio."), body);
}

QWidget* MainWindow::buildAcceleratorPage() {
  auto* body = new QWidget;
  auto* layout = new QHBoxLayout(body);
  _libraryColumns = layout;
  layout->setContentsMargins(0, 0, 0, 0);
  layout->setSpacing(14);

  auto* library = new QWidget;
  auto* libraryLayout = new QVBoxLayout(library);
  libraryLayout->setContentsMargins(0, 0, 0, 0);
  libraryLayout->setSpacing(12);
  _libraryStorageSummary = quietLabel(QStringLiteral("Consultando espaço ocupado e livre…"));
  _libraryStorageSummary->setObjectName(QStringLiteral("localLibraryStorageSummary"));
  libraryLayout->addWidget(_libraryStorageSummary);
  auto* base = new QHBoxLayout;
  _libraryBase = quietLabel(QStringLiteral("Aguardando pasta da biblioteca"));
  _libraryBase->setObjectName(QStringLiteral("localLibraryBase"));
  _libraryBase->setTextInteractionFlags(Qt::TextSelectableByMouse);
  _libraryBase->setWordWrap(true);
  base->addWidget(_libraryBase, 1);
  _libraryOpenFolder = new QPushButton(QStringLiteral("Abrir pasta"));
  _libraryOpenFolder->setObjectName(QStringLiteral("localLibraryOpenFolder"));
  _libraryOpenFolder->setEnabled(false);
  connect(_libraryOpenFolder, &QPushButton::clicked, this, [this] {
    const QFileInfo root(_localMediaRoot);
    const QString canonical = root.canonicalFilePath();
    if (_localMediaVerified && root.isAbsolute() && root.isDir() && !canonical.isEmpty())
      QDesktopServices::openUrl(QUrl::fromLocalFile(canonical));
  });
  base->addWidget(_libraryOpenFolder);
  libraryLayout->addLayout(base);
  _libraryTabs = new QTabWidget;
  _libraryTabs->setObjectName(QStringLiteral("localLibraryTabs"));
  _libraryTabs->setDocumentMode(true);
  libraryLayout->addWidget(_libraryTabs);
  auto* local = new QWidget;
  auto* localLayout = new QVBoxLayout(local);
  localLayout->setContentsMargins(0, 12, 0, 0);
  localLayout->setSpacing(12);
  _localMediaSummary = quietLabel(QStringLiteral("Vídeos de origem, recortes, sensores e arquivos de apoio neste computador."));
  _localMediaSummary->setObjectName(QStringLiteral("localMediaSummary"));
  localLayout->addWidget(_localMediaSummary);
  auto* localSearch = new QHBoxLayout;
  _localMediaQuery = new QLineEdit;
  _localMediaQuery->setObjectName(QStringLiteral("localMediaQuery"));
  _localMediaQuery->setPlaceholderText(QStringLiteral("Buscar nome ou caminho"));
  _localMediaQuery->setMaxLength(200);
  localSearch->addWidget(_localMediaQuery, 1);
  _localMediaRefresh = new QPushButton(QStringLiteral("Verificar arquivos"));
  _localMediaRefresh->setObjectName(QStringLiteral("localMediaRefresh"));
  localSearch->addWidget(_localMediaRefresh);
  localLayout->addLayout(localSearch);
  auto* localFilters = new QHBoxLayout;
  _localMediaProvider = new ComboBox;
  _localMediaProvider->setObjectName(QStringLiteral("localMediaProvider"));
  for (const auto& entry : QList<QPair<QString, QString>>{
      {QStringLiteral("Todas as origens"), QStringLiteral("all")},
      {QStringLiteral("Ego4D"), QStringLiteral("ego4d")},
      {QStringLiteral("HoloAssist"), QStringLiteral("holoassist")},
      {QStringLiteral("Nymeria"), QStringLiteral("nymeria")},
      {QStringLiteral("Local"), QStringLiteral("local")}})
    _localMediaProvider->addItem(entry.first, entry.second);
  _localMediaKind = new ComboBox;
  _localMediaKind->setObjectName(QStringLiteral("localMediaKind"));
  for (const auto& entry : QList<QPair<QString, QString>>{
      {QStringLiteral("Todos os tipos"), QStringLiteral("all")},
      {QStringLiteral("Vídeos"), QStringLiteral("video")},
      {QStringLiteral("Sensores / IMU"), QStringLiteral("sensor")},
      {QStringLiteral("Arquivos de apoio"), QStringLiteral("sidecar")},
      {QStringLiteral("Catálogos"), QStringLiteral("catalog")},
      {QStringLiteral("Derivados"), QStringLiteral("derivative")}})
    _localMediaKind->addItem(entry.first, entry.second);
  localFilters->addWidget(_localMediaProvider);
  localFilters->addWidget(_localMediaKind);
  localFilters->addStretch();
  localLayout->addLayout(localFilters);
  _localMediaTable = new QTableWidget(0, 5);
  _localMediaTable->setObjectName(QStringLiteral("localMediaTable"));
  _localMediaTable->setHorizontalHeaderLabels({QStringLiteral("Nome"), QStringLiteral("Origem"),
      QStringLiteral("Tipo / estágio"), QStringLiteral("Tamanho"), QStringLiteral("Proteção")});
  _localMediaTable->setEditTriggers(QAbstractItemView::NoEditTriggers);
  _localMediaTable->setSelectionBehavior(QAbstractItemView::SelectRows);
  _localMediaTable->setSelectionMode(QAbstractItemView::SingleSelection);
  _localMediaTable->setMinimumHeight(320);
  _localMediaTable->setMaximumHeight(480);
  _localMediaTable->setTextElideMode(Qt::ElideMiddle);
  _localMediaTable->horizontalHeader()->setSectionResizeMode(0, QHeaderView::Stretch);
  for (int column = 1; column < 5; ++column)
    _localMediaTable->horizontalHeader()->setSectionResizeMode(column, QHeaderView::ResizeToContents);
  _localMediaTable->verticalHeader()->hide();
  new TableEmptyState(_localMediaTable, QStringLiteral("Seus arquivos locais aparecem aqui"),
      QStringLiteral("Consulte todas as origens ou use Preparar acervo para obter vídeos e sensores."));
  localLayout->addWidget(_localMediaTable, 1);
  _localMediaCount = quietLabel(QStringLiteral("Aguardando leitura dos arquivos"));
  _localMediaCount->setObjectName(QStringLiteral("localMediaCount"));
  localLayout->addWidget(_localMediaCount);
  auto* localActions = new QHBoxLayout;
  _localMediaPrevious = new QPushButton(QStringLiteral("Anterior"));
  _localMediaNext = new QPushButton(QStringLiteral("Próxima"));
  _localMediaCopy = new QPushButton(QStringLiteral("Copiar caminho"));
  _localMediaOpen = new QPushButton(QStringLiteral("Abrir arquivo"));
  _localMediaPrevious->setObjectName(QStringLiteral("localMediaPrevious"));
  _localMediaNext->setObjectName(QStringLiteral("localMediaNext"));
  _localMediaCopy->setObjectName(QStringLiteral("localMediaCopy"));
  _localMediaOpen->setObjectName(QStringLiteral("localMediaOpen"));
  for (auto* button : {_localMediaPrevious, _localMediaNext, _localMediaCopy, _localMediaOpen}) button->setEnabled(false);
  localActions->addWidget(_localMediaPrevious);
  localActions->addWidget(_localMediaNext);
  localActions->addStretch();
  localActions->addWidget(_localMediaCopy);
  localActions->addWidget(_localMediaOpen);
  localLayout->addLayout(localActions);
  connect(_localMediaQuery, &QLineEdit::textChanged, this, &MainWindow::localMediaFiltersChanged);
  connect(_localMediaQuery, &QLineEdit::returnPressed, this, [this] { loadLocalMediaLibrary(); });
  connect(_localMediaProvider, qOverload<int>(&QComboBox::currentIndexChanged), this, &MainWindow::localMediaFiltersChanged);
  connect(_localMediaKind, qOverload<int>(&QComboBox::currentIndexChanged), this, &MainWindow::localMediaFiltersChanged);
  connect(_localMediaRefresh, &QPushButton::clicked, this, [this] { loadLocalMediaLibrary(true); });
  connect(_localMediaPrevious, &QPushButton::clicked, this, [this] { _localMediaOffset = qMax(0, _localMediaOffset - 50); loadLocalMediaLibrary(); });
  connect(_localMediaNext, &QPushButton::clicked, this, [this] { _localMediaOffset += 50; loadLocalMediaLibrary(); });
  connect(_localMediaTable, &QTableWidget::itemSelectionChanged, this, &MainWindow::updateLocalMediaActions);
  connect(_localMediaCopy, &QPushButton::clicked, this, [this] {
    const QString path = localMediaPath();
    if (!path.isEmpty()) QApplication::clipboard()->setText(path);
  });
  connect(_localMediaOpen, &QPushButton::clicked, this, [this] {
    const QString path = localMediaPath(true);
    if (!path.isEmpty()) QDesktopServices::openUrl(QUrl::fromLocalFile(path));
  });
  _localMediaPoll.setInterval(1200);
  connect(&_localMediaPoll, &QTimer::timeout, this, [this] { loadLocalMediaLibrary(); });
  _libraryTabs->addTab(local, QStringLiteral("Arquivos locais"));

  auto* inventory = new QWidget;
  auto* inventoryLayout = new QVBoxLayout(inventory);
  inventoryLayout->setContentsMargins(0, 0, 0, 0);
  inventoryLayout->setSpacing(12);
  _preparedSummary = quietLabel(QStringLiteral("Consulte os recortes e sensores preparados neste computador."));
  _preparedSummary->setObjectName(QStringLiteral("preparedLibrarySummary"));
  inventoryLayout->addWidget(_preparedSummary);
  auto* filters = new QHBoxLayout;
  _preparedQuery = new QLineEdit;
  _preparedQuery->setObjectName(QStringLiteral("preparedLibraryQuery"));
  _preparedQuery->setPlaceholderText(QStringLiteral("Buscar recorte ou arquivo"));
  _preparedQuery->setMaxLength(200);
  filters->addWidget(_preparedQuery, 1);
  _preparedState = new ComboBox;
  _preparedState->setObjectName(QStringLiteral("preparedLibraryState"));
  for (const auto& entry : QList<QPair<QString, QString>>{
      {QStringLiteral("Todos os estados"), QStringLiteral("all")},
      {QStringLiteral("Mídia preparada"), QStringLiteral("ready")},
      {QStringLiteral("Preparação incompleta"), QStringLiteral("partial")},
      {QStringLiteral("Arquivo ausente"), QStringLiteral("missing")},
      {QStringLiteral("Revisar"), QStringLiteral("stale")}})
    _preparedState->addItem(entry.first, entry.second);
  filters->addWidget(_preparedState);
  inventoryLayout->addLayout(filters);
  auto* durations = new QHBoxLayout;
  durations->addWidget(quietLabel(QStringLiteral("Duração")));
  _preparedMinimum = new QSpinBox;
  _preparedMinimum->setObjectName(QStringLiteral("preparedLibraryMinimum"));
  _preparedMinimum->setRange(0, 1440);
  _preparedMinimum->setSuffix(QStringLiteral(" min"));
  _preparedMinimum->setKeyboardTracking(false);
  _preparedMinimum->setToolTip(QStringLiteral("Duração mínima do recorte preparado, em minutos"));
  durations->addWidget(_preparedMinimum);
  durations->addWidget(quietLabel(QStringLiteral("até")));
  _preparedMaximum = new QSpinBox;
  _preparedMaximum->setObjectName(QStringLiteral("preparedLibraryMaximum"));
  _preparedMaximum->setRange(0, 1440);
  _preparedMaximum->setSpecialValueText(QStringLiteral("Sem limite"));
  _preparedMaximum->setSuffix(QStringLiteral(" min"));
  _preparedMaximum->setKeyboardTracking(false);
  _preparedMaximum->setToolTip(QStringLiteral("Duração máxima do recorte preparado, em minutos; 0 remove o limite"));
  durations->addWidget(_preparedMaximum);
  durations->addStretch();
  _preparedRefresh = new QPushButton(QStringLiteral("Verificar acervo"));
  _preparedRefresh->setObjectName(QStringLiteral("preparedLibraryRefresh"));
  connect(_preparedRefresh, &QPushButton::clicked, this, [this] { loadPreparedLibrary(true); });
  durations->addWidget(_preparedRefresh);
  inventoryLayout->addLayout(durations);
  _preparedTable = new QTableWidget(0, 4);
  _preparedTable->setObjectName(QStringLiteral("preparedLibraryTable"));
  _preparedTable->setHorizontalHeaderLabels({QStringLiteral("Estado"), QStringLiteral("Duração"),
      QStringLiteral("Recorte"), QStringLiteral("Arquivos locais")});
  _preparedTable->setEditTriggers(QAbstractItemView::NoEditTriggers);
  _preparedTable->setSelectionBehavior(QAbstractItemView::SelectRows);
  _preparedTable->setSelectionMode(QAbstractItemView::SingleSelection);
  _preparedTable->setMinimumHeight(320);
  _preparedTable->setMaximumHeight(480);
  _preparedTable->setTextElideMode(Qt::ElideMiddle);
  _preparedTable->horizontalHeader()->setSectionResizeMode(0, QHeaderView::ResizeToContents);
  _preparedTable->horizontalHeader()->setSectionResizeMode(1, QHeaderView::ResizeToContents);
  _preparedTable->horizontalHeader()->setSectionResizeMode(2, QHeaderView::Stretch);
  _preparedTable->horizontalHeader()->setSectionResizeMode(3, QHeaderView::ResizeToContents);
  _preparedTable->verticalHeader()->hide();
  new TableEmptyState(_preparedTable, QStringLiteral("Seu acervo preparado aparece aqui"),
      QStringLiteral("Use Preparar acervo para guardar vídeos e sensores antes da campanha."));
  inventoryLayout->addWidget(_preparedTable, 1);
  _preparedCount = quietLabel(QStringLiteral("Aguardando leitura do acervo"));
  _preparedCount->setObjectName(QStringLiteral("preparedLibraryCount"));
  inventoryLayout->addWidget(_preparedCount);
  auto* files = new QHBoxLayout;
  _preparedPrevious = new QPushButton(QStringLiteral("Anterior"));
  _preparedNext = new QPushButton(QStringLiteral("Próxima"));
  _preparedPrevious->setObjectName(QStringLiteral("preparedLibraryPrevious"));
  _preparedNext->setObjectName(QStringLiteral("preparedLibraryNext"));
  files->addWidget(_preparedPrevious);
  files->addWidget(_preparedNext);
  files->addStretch();
  _preparedCopy = new QPushButton(QStringLiteral("Copiar ID"));
  _preparedOpenNative = new QPushButton(QStringLiteral("Abrir recorte"));
  _preparedOpenSource = new QPushButton(QStringLiteral("Abrir origem"));
  _preparedCopy->setObjectName(QStringLiteral("preparedLibraryCopy"));
  _preparedOpenNative->setObjectName(QStringLiteral("preparedLibraryOpenNative"));
  _preparedOpenSource->setObjectName(QStringLiteral("preparedLibraryOpenSource"));
  for (auto* button : {_preparedPrevious, _preparedNext, _preparedCopy, _preparedOpenNative, _preparedOpenSource})
    button->setEnabled(false);
  files->addWidget(_preparedCopy);
  files->addWidget(_preparedOpenNative);
  files->addWidget(_preparedOpenSource);
  inventoryLayout->addLayout(files);
  connect(_preparedQuery, &QLineEdit::returnPressed, this, [this] { loadPreparedLibrary(); });
  connect(_preparedQuery, &QLineEdit::textChanged, this, &MainWindow::preparedLibraryFiltersChanged);
  connect(_preparedState, qOverload<int>(&QComboBox::currentIndexChanged), this, &MainWindow::preparedLibraryFiltersChanged);
  connect(_preparedMinimum, qOverload<int>(&QSpinBox::valueChanged), this, &MainWindow::preparedLibraryFiltersChanged);
  connect(_preparedMaximum, qOverload<int>(&QSpinBox::valueChanged), this, &MainWindow::preparedLibraryFiltersChanged);
  connect(_preparedTable, &QTableWidget::itemSelectionChanged, this, &MainWindow::updatePreparedLibraryActions);
  connect(_preparedPrevious, &QPushButton::clicked, this, [this] { _preparedOffset = qMax(0, _preparedOffset - 50); loadPreparedLibrary(); });
  connect(_preparedNext, &QPushButton::clicked, this, [this] { _preparedOffset += 50; loadPreparedLibrary(); });
  connect(_preparedCopy, &QPushButton::clicked, this, [this] {
    const auto* item = _preparedTable->item(_preparedTable->currentRow(), 2);
    if (_preparedVerified && item) QApplication::clipboard()->setText(item->data(Qt::UserRole).toJsonObject().value(QStringLiteral("clip_uid")).toString());
  });
  for (const auto& action : QList<QPair<QPushButton*, QString>>{
      {_preparedOpenNative, QStringLiteral("native")}, {_preparedOpenSource, QStringLiteral("source")}})
    connect(action.first, &QPushButton::clicked, this, [this, role = action.second] {
      const QString path = preparedLibraryVideoPath(role);
      if (!path.isEmpty()) QDesktopServices::openUrl(QUrl::fromLocalFile(path));
    });
  _preparedPoll.setInterval(1200);
  connect(&_preparedPoll, &QTimer::timeout, this, [this] { loadPreparedLibrary(); });
  _libraryTabs->addTab(inventory, QStringLiteral("Recortes preparados Ego4D"));
  _libraryTabs->addTab(buildNymeriaLibraryTab(), QStringLiteral("Acervo Nymeria"));
  connect(_libraryTabs, &QTabWidget::currentChanged, this, [this](int index) {
    _localMediaPoll.stop();
    _preparedPoll.stop();
    _nymeriaPoll.stop();
    if (_pages->currentIndex() != 4) return;
    if (index == 0) loadLocalMediaLibrary();
    else if (index == 1) loadPreparedLibrary();
    else { loadNymeriaLibrary(); pollNymeriaJob(); }
  });
  libraryLayout->addWidget(quietLabel(QStringLiteral(
      "A presença de arquivos não aprova uma tarefa. O ZIP final é montado por conta na hora do envio.")));

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
  auto* libraryContext = card(QStringLiteral("PREPARAÇÃO ANTECIPADA · OPCIONAL"), hero);
  libraryContext->setObjectName(QStringLiteral("libraryContext"));
  libraryContext->setMinimumWidth(280);
  _cacheState->setWordWrap(true);
  auto* explore = new QPushButton(QStringLiteral("Catálogo de origem Ego4D"));
  explore->setObjectName(QStringLiteral("egoLibraryExplore"));
  connect(explore, &QPushButton::clicked, this, &MainWindow::openEgoLibrary);
  heroLayout->addWidget(explore);
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
    if (_cacheMinimum) form->setRowVisible(_cacheMinimum->parentWidget(), ego);
    if (_cacheProviderHelp) _cacheProviderHelp->setText(ego
        ? QStringLiteral("Preparação manual opcional. A campanha pode baixar, preparar e liberar cada recorte quando precisar.")
        : QStringLiteral("HoloAssist: prepara os clipes da tarefa escolhida para uso posterior na campanha."));
    if (_cacheTaskHelp) _cacheTaskHelp->setText(ego
        ? QStringLiteral("A categoria escolhida entra primeiro. O limite em GB é compartilhado entre categorias.")
        : QStringLiteral("Somente a tarefa escolhida entra nesta preparação."));
    if (_cacheDiskHelp) _cacheDiskHelp->setText(ego
        ? QStringLiteral("O QMoney preserva o espaço livre indicado e ajusta o limite ao disco disponível.")
        : QStringLiteral("A preparação para quando o espaço livre cair abaixo da reserva."));
    if (_cacheLimitHelp) _cacheLimitHelp->setText(ego
        ? QStringLiteral("Todos usa o espaço escolhido; um número menor limita somente esta execução.")
        : QStringLiteral("Todos prepara todos os clipes disponíveis da tarefa escolhida."));
    _cacheProvider->setToolTip(_cacheProviderHelp->text());
    _cacheLimit->setToolTip(_cacheLimitHelp->text());
    if (_cacheStart) {
      const bool off = ego && _cacheBudget && _cacheBudget->value() == 0;
      _cacheStart->setText(off ? QStringLiteral("Desativar pré-cache")
                               : QStringLiteral("Preparar cache"));
    }
    loadAccelerator();
  });
  form->addRow(QStringLiteral("Provedor"), _cacheProvider);
  _cacheProviderHelp = quietLabel(QStringLiteral(
      "Preparação manual opcional. A campanha pode baixar, preparar e liberar cada recorte quando precisar."));
  _cacheProviderHelp->setWordWrap(true);
  form->addRow(QString(), _cacheProviderHelp);
  _cacheProvider->setToolTip(_cacheProviderHelp->text());
  _cacheTask = new ComboBox;
  configureCombo(_cacheTask, 240);
  connect(_cacheTask, qOverload<int>(&QComboBox::currentIndexChanged), this, [this] {
    loadAccelerator();
  });
  _cacheTaskLabel = new QLabel(QStringLiteral("Priorizar categoria"));
  form->addRow(_cacheTaskLabel, _cacheTask);
  _cacheTaskHelp = quietLabel(QStringLiteral(
      "A categoria escolhida entra primeiro. O limite em GB é compartilhado entre categorias."));
  _cacheTaskHelp->setWordWrap(true);
  form->addRow(QString(), _cacheTaskHelp);
  _cacheBudgetLabel = new QLabel(QStringLiteral("Espaço para o cache"));
  _cacheBudget = new QSpinBox;
  _cacheBudget->setMaximumWidth(220);
  _cacheBudget->setRange(0, 2147483647);
  _cacheBudget->setKeyboardTracking(false);
  _cacheBudget->setSpecialValueText(QStringLiteral("0 GB · desativado"));
  _cacheBudget->setValue(0);
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
      "0 GB desativa a preparação antecipada. A campanha ainda pode buscar vídeos quando precisar."));
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
  _cacheLimit->setToolTip(_cacheLimitHelp->text());
  form->setRowVisible(_cacheLimitHelp, false);
  auto* preparationDurations = new QWidget;
  auto* preparationDurationLayout = new QHBoxLayout(preparationDurations);
  preparationDurationLayout->setContentsMargins(0, 0, 0, 0);
  _cacheMinimum = new QSpinBox;
  _cacheMaximum = new QSpinBox;
  _cacheMinimum->setObjectName(QStringLiteral("cacheMinimumDuration"));
  _cacheMaximum->setObjectName(QStringLiteral("cacheMaximumDuration"));
  for (auto* duration : {_cacheMinimum, _cacheMaximum}) {
    duration->setRange(1, 30);
    duration->setSuffix(QStringLiteral(" min"));
    duration->setKeyboardTracking(false);
    duration->setValue(duration == _cacheMinimum ? 1 : 30);
    connect(duration, qOverload<int>(&QSpinBox::valueChanged), this, [this] { loadAccelerator(); });
  }
  preparationDurationLayout->addWidget(_cacheMinimum);
  preparationDurationLayout->addWidget(quietLabel(QStringLiteral("até")));
  preparationDurationLayout->addWidget(_cacheMaximum);
  form->addRow(QStringLiteral("Duração dos recortes"), preparationDurations);
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
  auto* actionLayout = new QVBoxLayout(actions);
  actionLayout->setContentsMargins(0, 0, 0, 0);
  auto* cleanupActions = new QHBoxLayout;
  auto* cleanupProvider = new ComboBox;
  cleanupProvider->setObjectName(QStringLiteral("cacheCleanupProvider"));
  cleanupProvider->addItem(QStringLiteral("Ego4D"), QStringLiteral("ego4d"));
  cleanupProvider->addItem(QStringLiteral("HoloAssist"), QStringLiteral("holoassist"));
  cleanupProvider->addItem(QStringLiteral("Ambos"), QStringLiteral("all"));
  cleanupProvider->setToolTip(QStringLiteral("Escolha o cache a apagar"));
  cleanupActions->addWidget(cleanupProvider);
  auto* cleanup = new QPushButton(QStringLiteral("Apagar cache"));
  cleanup->setObjectName(QStringLiteral("cacheCleanupButton"));
  connect(cleanup, &QPushButton::clicked, this, [this, cleanupProvider] {
    const QString provider = cleanupProvider->currentData().toString();
    const QString name = cleanupProvider->currentText();
    if (QMessageBox::question(this, QStringLiteral("Limpar mídia"),
          QStringLiteral("Apagar o cache de %1? Catálogos, contas e histórico serão preservados.").arg(name))
        != QMessageBox::Yes) return;
    _api.post(QStringLiteral("/api/storage/cleanup"), {{QStringLiteral("provider"), provider}}, [this, provider](bool ok, const QJsonDocument& doc, const QString& error) {
      if (!ok) return showError(QStringLiteral("Falha na limpeza"), error);
      const auto result = doc.object();
      setStatus(QStringLiteral("%1 arquivo(s) removido(s).").arg(result.value(QStringLiteral("files")).toInt()));
      invalidatePreparedLibrary();
      loadLocalMediaLibrary(true);
      loadAccelerator();
    });
  });
  cleanupActions->addWidget(cleanup);
  cleanupActions->addStretch();
  actionLayout->addLayout(cleanupActions);
  auto* preparationActions = new QHBoxLayout;
  _cacheStop = new QPushButton(QStringLiteral("Parar com segurança"));
  _cacheStop->setEnabled(false);
  connect(_cacheStop, &QPushButton::clicked, this, [this] {
    _api.post(QStringLiteral("/api/holo-cache/stop"), {}, [this](bool ok, const QJsonDocument&, const QString& error) {
      if (!ok) showError(QStringLiteral("Falha ao parar"), error);
      else setStatus(QStringLiteral("O acelerador parará depois do clipe atual."));
    });
  });
  preparationActions->addWidget(_cacheStop);
  _cacheStart = primaryButton(QStringLiteral("Preparar cache"));
  connect(_cacheStart, &QPushButton::clicked, this, &MainWindow::startAccelerator);
  preparationActions->addWidget(_cacheStart);
  actionLayout->addLayout(preparationActions);
  auto* preparation = new QWidget;
  auto* preparationLayout = new QVBoxLayout(preparation);
  preparationLayout->setContentsMargins(0, 0, 0, 0);
  preparationLayout->setSpacing(20);
  preparationLayout->addWidget(libraryContext);
  preparationLayout->addWidget(config);
  preparationLayout->addWidget(actions);
  layout->addWidget(card(QStringLiteral("Armazenamento local"), library), 2, Qt::AlignTop);
  layout->addWidget(card(QStringLiteral("Preparar acervo"), preparation), 1);
  auto* scroll = new QScrollArea;
  scroll->setWidgetResizable(true);
  scroll->setFrameShape(QFrame::NoFrame);
  scroll->setWidget(body);

  return pageShell(QStringLiteral("Biblioteca"),
                   QStringLiteral("Consulte os arquivos de todas as origens e prepare mídia para suas campanhas."), scroll);
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
  auto* birth = new QWidget(formBody);
  auto* birthLayout = new QHBoxLayout(birth);
  birthLayout->setContentsMargins(0, 0, 0, 0);
  _accountBirthMonth = new QSpinBox;
  _accountBirthMonth->setRange(0, 12);
  _accountBirthMonth->setSpecialValueText(QStringLiteral("Mês"));
  _accountBirthYear = new QSpinBox;
  _accountBirthYear->setRange(0, QDate::currentDate().year());
  _accountBirthYear->setSpecialValueText(QStringLiteral("Ano"));
  _accountBirthMonth->setAccessibleName(QStringLiteral("Mês de nascimento"));
  _accountBirthYear->setAccessibleName(QStringLiteral("Ano de nascimento"));
  birthLayout->addWidget(_accountBirthMonth);
  birthLayout->addWidget(_accountBirthYear);
  birthLayout->addStretch();
  birth->hide();
  _accountGender = new ComboBox(formBody);
  _accountGender->addItem(QStringLiteral("Selecione para registrar"), QString());
  _accountGender->addItem(QStringLiteral("Masculino"), QStringLiteral("male"));
  _accountGender->addItem(QStringLiteral("Feminino"), QStringLiteral("female"));
  _accountGender->addItem(QStringLiteral("Não binário"), QStringLiteral("non_binary"));
  _accountGender->addItem(QStringLiteral("Prefiro não informar"), QStringLiteral("prefer_not_to_say"));
  _accountGender->hide();
  _accountUseReferral = new QCheckBox(QStringLiteral("Usar indicação configurada"));
  _accountUseReferral->setChecked(true);
  form->addRow(QString(), _accountUseReferral);
  form->addRow(quietLabel(QStringLiteral("Criamos Crowtado + Minute, confirmamos o e-mail e verificamos bloqueios de acesso. Idade, equipamento e vínculo em Tarefas ficam para você concluir no site.")));
  _accountProxy = new ComboBox;
  _accountProxy->addItem(QStringLiteral("Carregando proxies…"), QString());
  _accountProxyImport = new QPushButton(QStringLiteral("Importar proxies…"));
  auto* accountProxyRow = new QWidget;
  auto* accountProxyLayout = new QHBoxLayout(accountProxyRow);
  accountProxyLayout->setContentsMargins(0,0,0,0);
  accountProxyLayout->addWidget(_accountProxy,1); accountProxyLayout->addWidget(_accountProxyImport);
  form->addRow(QStringLiteral("Proxy (nova conta)"), accountProxyRow);
  connect(_accountProxyImport, &QPushButton::clicked, this, [this] {
    const auto path=QFileDialog::getOpenFileName(this,QStringLiteral("Importar proxies"),{},QStringLiteral("Lista de proxies (*.txt);;Todos os arquivos (*)"));
    if (!path.isEmpty()) importRegistrationProxiesFile(path);
  });
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
  _bulkRegisterCount->setValue(1);
  _bulkRegisterCount->setMaximumWidth(200);
  bulkForm->addRow(QStringLiteral("Quantidade"), _bulkRegisterCount);
  _bulkRegisterProxy = new ComboBox;
  _bulkRegisterProxy->addItem(QStringLiteral("Carregando proxies…"), QString());
  _bulkProxyImport = new QPushButton(QStringLiteral("Importar proxies…"));
  auto* proxyRow = new QWidget;
  auto* proxyLayout = new QHBoxLayout(proxyRow);
  proxyLayout->setContentsMargins(0,0,0,0);
  proxyLayout->addWidget(_bulkRegisterProxy,1); proxyLayout->addWidget(_bulkProxyImport);
  bulkForm->addRow(QStringLiteral("Proxy"), proxyRow);
  bulkForm->addRow(quietLabel(QStringLiteral("Arquivo TXT: host:porta:login:senha. Em automático, cada conta recebe um proxy; retomadas mantêm o mesmo proxy.")));
  connect(_bulkRegisterProxy,qOverload<int>(&QComboBox::currentIndexChanged),this,[this] {checkBulkRegisterDomain();});
  connect(_bulkProxyImport,&QPushButton::clicked,_accountProxyImport,&QPushButton::click);
  _bulkRegisterUseReferral = new QCheckBox(QStringLiteral("Usar indicação configurada"));
  _bulkRegisterUseReferral->setChecked(true);
  bulkForm->addRow(QString(), _bulkRegisterUseReferral);
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
  _bulkRegisterStop = new QPushButton(QStringLiteral("Parar após esta conta"));
  _bulkRegisterStop->setEnabled(false);
  connect(_bulkRegisterStop, &QPushButton::clicked, this, [this] {
    if (!_backendReady || !_bulkRegisterPolling) return;
    _bulkRegisterStop->setEnabled(false);
    _api.post(QStringLiteral("/api/accounts/bulk-register/stop"), {}, [this](bool ok, const QJsonDocument&, const QString& error) {
      if (!ok) _bulkRegisterStatus->setText(QStringLiteral("Não foi possível confirmar a parada: ") + error);
      pollBulkRegister();
    });
  });
  bulkActionLayout->addWidget(_bulkRegisterStop);
  bulkForm->addRow(QString(), bulkActions);
  _bulkRegisterStatus = quietLabel(QStringLiteral(
      "Crowtado + Minute, com e-mail automático e verificação de bloqueios. "
      "Depois, conclua idade, equipamento e vínculo manualmente no site Crowtado. CAPTCHA pode exigir sua intervenção no Chrome."));
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
  _bulkRegisterTable = new QTableWidget(0, 4);
  configureTable(_bulkRegisterTable, QStringLiteral("Acompanhe as novas contas"),
                 QStringLiteral("Ao iniciar a criação, o resultado de cada conta aparece aqui."));
  _bulkRegisterTable->setMinimumHeight(160);
  _bulkRegisterTable->setHorizontalHeaderLabels({
      QStringLiteral("Conta"), QStringLiteral("Identificação"), QStringLiteral("Resultado"), QStringLiteral("Ações"),
  });
  _bulkRegisterTable->horizontalHeader()->setSectionResizeMode(0, QHeaderView::Stretch);
  _bulkRegisterTable->horizontalHeader()->setSectionResizeMode(1, QHeaderView::Interactive);
  _bulkRegisterTable->horizontalHeader()->setSectionResizeMode(2, QHeaderView::Interactive);
  _bulkRegisterTable->horizontalHeader()->setSectionResizeMode(3, QHeaderView::Fixed);
  _bulkRegisterTable->setColumnWidth(1, 170);
  _bulkRegisterTable->setColumnWidth(2, 230);
  _bulkRegisterTable->setColumnWidth(3, 108);
  _bulkRegisterTable->verticalHeader()->setDefaultSectionSize(56);
  _bulkRegisterTable->setWordWrap(false);
  _bulkRegisterTable->setSelectionBehavior(QAbstractItemView::SelectRows);
  _bulkRegisterTable->setSelectionMode(QAbstractItemView::SingleSelection);
  connect(_bulkRegisterTable, &QTableWidget::cellDoubleClicked, this, [this](int row,int) {
    if(row<0 || row>=_bulkRegisterResults.size()) return;
    const auto result=_bulkRegisterResults.at(row).toObject();
    showAccountDetails({{"email",result.value("email")},{"registration",QJsonObject{{"steps",result.value("steps")},{"error",result.value("error")}}}});
  });
  bulkForm->addRow(quietLabel(QStringLiteral("Resultados")));
  bulkForm->addRow(_bulkRegisterTable);
  _bulkRegisterPoll.setInterval(900);
  connect(&_bulkRegisterPoll, &QTimer::timeout, this, &MainWindow::pollBulkRegister);
  _bulkRegisterStart->setEnabled(false);
  addTab(QStringLiteral("Criar contas"), card(QStringLiteral("Criador de contas"), bulkBody));

  _accountsTable = new QTableWidget(0, 5);
  configureTable(_accountsTable, QStringLiteral("Tudo começa pelas suas contas"),
                 QStringLiteral("Adicione uma conta ou importe suas credenciais para começar."));
  _accountsTable->setMinimumHeight(230);
  _accountsTable->setHorizontalHeaderLabels(
      {QStringLiteral("Conta"), QStringLiteral("Cadastro"), QStringLiteral("Minute"), QStringLiteral("Crowtado · acesso / saques"), QStringLiteral("Ações")});
  _accountsTable->horizontalHeader()->setSectionResizeMode(0, QHeaderView::Stretch);
  _accountsTable->horizontalHeader()->setSectionResizeMode(1, QHeaderView::Interactive);
  _accountsTable->horizontalHeader()->setSectionResizeMode(2, QHeaderView::Interactive);
  _accountsTable->horizontalHeader()->setSectionResizeMode(3, QHeaderView::Fixed);
  _accountsTable->setColumnWidth(1, 155);
  _accountsTable->setColumnWidth(2, 205);
  _accountsTable->setColumnWidth(3, 250);
  _accountsTable->horizontalHeader()->setSectionResizeMode(4, QHeaderView::Fixed);
  _accountsTable->setColumnWidth(4, 108);
  _accountsTable->verticalHeader()->setDefaultSectionSize(56);
  _accountsTable->setWordWrap(false);
  connect(_accountsTable, &QTableWidget::cellDoubleClicked, this, [this](int row, int) {
    if (row >= 0 && row < _accountsSnapshot.size()) showAccountDetails(_accountsSnapshot.at(row).toObject());
  });
  _accountsTable->setSelectionBehavior(QAbstractItemView::SelectRows);
  _accountsTable->setSelectionMode(QAbstractItemView::ExtendedSelection);

  auto* accountsBody = new QWidget;
  auto* accountsLayout = new QVBoxLayout(accountsBody);
  accountsLayout->setContentsMargins(0, 0, 0, 0);
  accountsLayout->setSpacing(10);
  auto* tableActions = new QHBoxLayout;
  _accountsSummary = quietLabel(QStringLiteral("Aguardando leitura das contas…"));
  tableActions->addWidget(_accountsSummary, 1);
  _accountsCheckAll = primaryButton(QStringLiteral("Verificar todas"));
  connect(_accountsCheckAll, &QPushButton::clicked, this, &MainWindow::checkAllAccounts);
  tableActions->addWidget(_accountsCheckAll);
  accountsLayout->addLayout(tableActions);
  auto* searchActions = new QHBoxLayout;
  _accountsSearch = new QLineEdit;
  _accountsSearch->setPlaceholderText(QStringLiteral("Buscar conta, organização ou motivo…"));
  _accountsSearch->setClearButtonEnabled(true);
  _accountsAttention = new QCheckBox(QStringLiteral("Somente pendências"));
  connect(_accountsSearch, &QLineEdit::textChanged, this, &MainWindow::filterAccounts);
  connect(_accountsAttention, &QCheckBox::toggled, this, &MainWindow::filterAccounts);
  searchActions->addWidget(_accountsSearch, 1);
  searchActions->addWidget(_accountsAttention);
  accountsLayout->addLayout(searchActions);
  auto* transferActions = new QHBoxLayout;
  _accountsImport = new QPushButton(QStringLiteral("Importar JSON"));
  _accountsExport = new QPushButton(QStringLiteral("Exportar todas"));
  _accountsExportSelected = new QPushButton(QStringLiteral("Exportar selecionadas"));
  connect(_accountsImport, &QPushButton::clicked, this, &MainWindow::importAccounts);
  connect(_accountsExport, &QPushButton::clicked, this, [this] { exportAccounts(false); });
  connect(_accountsExportSelected, &QPushButton::clicked, this, [this] { exportAccounts(true); });
  transferActions->addWidget(_accountsImport);
  _accountsExport->setParent(accountsBody); _accountsExport->hide();
  _accountsExportSelected->setParent(accountsBody); _accountsExportSelected->hide();
  auto* exportButton = new QPushButton(QStringLiteral("Exportar JSON"));
  auto* exportMenu = new QMenu(exportButton);
  auto* exportAllAction = exportMenu->addAction(QStringLiteral("Todas as contas"),_accountsExport,&QPushButton::click);
  auto* exportSelectedAction = exportMenu->addAction(QStringLiteral("Contas selecionadas"),_accountsExportSelected,&QPushButton::click);
  exportButton->setMenu(exportMenu);
  transferActions->addWidget(exportButton);
  auto* exportBanned = new QPushButton(QStringLiteral("Exportar banidas"));
  exportBanned->setParent(accountsBody);exportBanned->hide();
  auto* exportBannedAction = exportMenu->addAction(QStringLiteral("Registro de contas restritas"),exportBanned,&QPushButton::click);
  connect(exportMenu,&QMenu::aboutToShow,this,[this,exportAllAction,exportSelectedAction,exportBannedAction] {
    exportAllAction->setEnabled(_accountsExport->isEnabled());
    exportSelectedAction->setEnabled(_accountsExportSelected->isEnabled() && !_accountsTable->selectionModel()->selectedRows().isEmpty());
    exportBannedAction->setEnabled(_accountsExport->isEnabled());
  });
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
  _accountsMigrate = new QPushButton(QStringLiteral("Atualizar organização Crowtado"));
  _accountsMigrate->setToolTip(QStringLiteral("Atualiza todas as contas Crowtado cadastradas. Contas Claru são ignoradas."));
  _migrationReport = new QPushButton(QStringLiteral("Ver relatório"));
  _migrationReport->setEnabled(false);
  connect(_accountsMigrate, &QPushButton::clicked, this, &MainWindow::startOrgMigration);
  connect(_migrationReport, &QPushButton::clicked, this, &MainWindow::showOrgMigrationReport);
  _accountsMigrate->setParent(accountsBody);_accountsMigrate->hide();
  _migrationReport->setParent(accountsBody);_migrationReport->hide();
  auto* maintenance = new QPushButton(QStringLiteral("Organizações"));
  auto* maintenanceMenu = new QMenu(maintenance);
  auto* migrationAction = maintenanceMenu->addAction(QStringLiteral("Atualizar organização Crowtado"),_accountsMigrate,&QPushButton::click);
  auto* reportAction = maintenanceMenu->addAction(QStringLiteral("Ver relatório"),_migrationReport,&QPushButton::click);
  connect(maintenanceMenu,&QMenu::aboutToShow,this,[this,migrationAction,reportAction] {
    migrationAction->setEnabled(_accountsMigrate->isEnabled());reportAction->setEnabled(_migrationReport->isEnabled());
  });
  maintenance->setMenu(maintenanceMenu);transferActions->addWidget(maintenance);
  transferActions->addStretch(); accountsLayout->addLayout(transferActions);
  _migrationStatus = quietLabel(QString());
  _migrationStatus->setWordWrap(true);
  _migrationStatus->hide();
  accountsLayout->addWidget(_migrationStatus);
  _orgMigrationPoll.setInterval(1200);
  connect(&_orgMigrationPoll, &QTimer::timeout, this, &MainWindow::pollOrgMigration);
  exportButton->setToolTip(QStringLiteral("O backup contém credenciais. Use Ctrl ou Shift para selecionar contas e guarde o arquivo em local seguro."));
  accountsLayout->addWidget(_accountsTable, 1);
  auto* accountsScroll = new QScrollArea;
  accountsScroll->setWidgetResizable(true);
  accountsScroll->setFrameShape(QFrame::NoFrame);
  accountsScroll->setWidget(card(QStringLiteral("Contas cadastradas"), accountsBody));
  tabs->insertTab(0, accountsScroll, QStringLiteral("Contas cadastradas"));
  tabs->setCurrentIndex(0);
  return pageShell(QStringLiteral("Contas"),
                   QStringLiteral("Verifique o acesso e os banimentos de Minute e Crowtado separadamente."), body);
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
  headerLayout->setSpacing(10);
  auto* extraActions = new QWidget(header);
  extraActions->hide();
  headerLayout->setContentsMargins(0, 0, 0, 0);
  auto* identityCopy = new QWidget;
  auto* identityLayout = new QVBoxLayout(identityCopy);
  identityLayout->setContentsMargins(0, 0, 0, 0);
  identityLayout->setSpacing(3);
  _balancesState = quietLabel(QStringLiteral("Aguardando leitura…"));
  identityLayout->addWidget(_balancesState);
  _balancesWithdrawReceipt = new QLabel;
  _balancesWithdrawReceipt->setObjectName(QStringLiteral("withdrawalReceipt"));
  _balancesWithdrawReceipt->setTextFormat(Qt::PlainText);
  _balancesWithdrawReceipt->setWordWrap(true);
  _balancesWithdrawReceipt->setTextInteractionFlags(Qt::TextSelectableByMouse);
  _balancesWithdrawReceipt->hide();
  identityLayout->addWidget(_balancesWithdrawReceipt);
  auto* identityHelp = quietLabel(QStringLiteral(
      "Confira os saldos e as restrições por conta. O acesso pode ser revisado em Detalhes."));
  identityHelp->setWordWrap(true);
  identityHelp->setParent(identityCopy);
  identityHelp->hide();
  headerContainer->addWidget(identityCopy);
  headerContainer->addLayout(headerLayout);
  auto* cleanMail = new QPushButton(QStringLiteral("Limpar caixa de e-mail"));
  cleanMail->setObjectName(QStringLiteral("mailCleanupButton"));
  cleanMail->setToolTip(QStringLiteral("Revisar a caixa de entrada, preservar saques e mover os demais para a lixeira."));
  cleanMail->setParent(extraActions);
  connect(cleanMail, &QPushButton::clicked, this, &MainWindow::openMailCleanup);
  _balancesRefresh = primaryButton(QStringLiteral("Atualizar todos"));
  connect(_balancesRefresh, &QPushButton::clicked, this, [this] {
    refreshBalanceAccounts();
  });
  headerLayout->addWidget(_balancesRefresh);
  _balancesRefreshNeeded = new QPushButton(QStringLiteral("Atualizar pendentes"));
  _balancesRefreshNeeded->setEnabled(false);
  _balancesRefreshNeeded->setToolTip(QStringLiteral(
      "Consulta só contas Crowtado conectadas sem saldo confirmado, com erro ou com leitura de mais de 24 horas."));
  connect(_balancesRefreshNeeded, &QPushButton::clicked, this, [this] {
    const auto emails = _balancesSnapshot.value(QStringLiteral("refresh_needed")).toArray();
    if (emails.isEmpty()) return;
    refreshBalanceAccounts(emails);
  });
  _balancesStop = new QPushButton(QStringLiteral("Interromper consulta"));
  _balancesStop->hide();
  _balancesStop->setToolTip(QStringLiteral("Conclui a conta atual e deixa as próximas para uma nova consulta."));
  headerLayout->addWidget(_balancesStop);
  connect(_balancesStop, &QPushButton::clicked, this, [this] {
    _balancesStop->setEnabled(false);
    _api.post(QStringLiteral("/api/balances/stop"), {}, [this](bool ok, const QJsonDocument&, const QString& error) {
      loadBalances();
      if (!ok) showError(QStringLiteral("Consulta não interrompida"), error);
    });
  });
  _balancesProgress = new QProgressBar;
  _balancesProgress->hide();
  headerContainer->addWidget(_balancesProgress);
  auto* monitorRow = new QHBoxLayout;
  _walletMonitoring = new QCheckBox(QStringLiteral("Consultar automaticamente"));
  _walletMonitoring->setToolTip(QStringLiteral("Consulta os saldos de contas com acesso salvo enquanto o aplicativo estiver aberto. Não solicita saques."));
  _walletMonitorInterval = new ComboBox;
  for (int minutes : {5, 15, 30})
    _walletMonitorInterval->addItem(QStringLiteral("A cada %1 min").arg(minutes), minutes);
  _walletMonitorInterval->setCurrentIndex(1);
  _walletMonitorState = quietLabel(QStringLiteral("Monitoramento desativado"));
  monitorRow->addWidget(_walletMonitoring);
  monitorRow->addWidget(_walletMonitorInterval);
  monitorRow->addWidget(_walletMonitorState, 1);
  headerContainer->addLayout(monitorRow);
  const auto scheduleMonitor = [this] {
    _walletMonitorTimer.stop();
    _walletMonitorInterval->setEnabled(!_walletMonitoring->isChecked());
    if (_walletMonitoring->isChecked()) {
      _walletMonitorTimer.start(_walletMonitorInterval->currentData().toInt() * 60000);
      _walletMonitorState->setText(QStringLiteral("Próxima consulta em %1 min").arg(_walletMonitorInterval->currentData().toInt()));
    } else _walletMonitorState->setText(QStringLiteral("Monitoramento desativado"));
  };
  connect(_walletMonitoring, &QCheckBox::toggled, this, scheduleMonitor);
  connect(_walletMonitorInterval, &QComboBox::currentIndexChanged, this, scheduleMonitor);
  connect(&_walletMonitorTimer, &QTimer::timeout, this, [this] {
    if (_closing || !_backendReady || !_walletSnapshotHealthy || _balanceStarting || _balancePolling || _balancesSnapshot.isEmpty()) return;
    for (const auto key : {"runner", "withdraw_bulk", "payout_method_bulk"})
      if (_balancesSnapshot.value(QLatin1String(key)).toObject().value(QStringLiteral("state")).toString() == QStringLiteral("running")) {
        _walletMonitorState->setText(QStringLiteral("Consulta adiada: operação em andamento"));
        return;
      }
    if (_balancesSnapshot.value(QStringLiteral("wise_cleanup")).toObject().value(QStringLiteral("pending")).toBool()) return;
    const auto emails = _balancesSnapshot.value(QStringLiteral("with_password")).toArray();
    if (emails.isEmpty()) { _walletMonitorState->setText(QStringLiteral("Nenhuma conta com acesso salvo")); return; }
    _walletMonitorState->setText(QStringLiteral("Consultando contas…"));
    refreshBalanceAccounts(emails);
  });
  _balancesWithdrawAll = new QPushButton(QStringLiteral("Sacar tudo"));
  _balancesWithdrawAll->setEnabled(false);
  _balancesWithdrawAll->setToolTip(QStringLiteral(
      "Solicita saques apenas de contas Crowtado com saldo aprovado superior a US$ 25,00, inclusive as ocultas pelo filtro."));
  connect(_balancesWithdrawAll, &QPushButton::clicked, this, [this] {
    const int eligible = _balancesWithdrawAll->property("eligibleCount").toInt();
    QJsonObject request;
    if (!confirmWithdrawal(this, eligible, request)) return;
    disableWalletActions();
    _balanceStarting = true;
    _api.post(QStringLiteral("/api/balances/withdraw-all"), request,
              [this](bool ok, const QJsonDocument& doc, const QString& error) {
      _balanceStarting = false;
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
  _balancesWiseCleanup->setParent(extraActions);
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
        "PayPal usa o fluxo de pagamento da Crowtado e não exige cadastrar um destino manual aqui. "
        "Contas Claru não serão alteradas."));
    help->setWordWrap(true);
    layout->addWidget(help);
    auto* form = new QFormLayout;
    auto* method = new ComboBox(&dialog);
    method->addItem(QStringLiteral("Dots"), QStringLiteral("dots"));
    method->addItem(QStringLiteral("PayPal"), QStringLiteral("paypal"));
    form->addRow(QStringLiteral("Método"), method);
    layout->addLayout(form);
    auto* buttons = new QDialogButtonBox(QDialogButtonBox::Ok | QDialogButtonBox::Cancel);
    buttons->button(QDialogButtonBox::Ok)->setText(QStringLiteral("Aplicar em todas"));
    connect(buttons, &QDialogButtonBox::accepted, &dialog, &QDialog::accept);
    connect(buttons, &QDialogButtonBox::rejected, &dialog, &QDialog::reject);
    layout->addWidget(buttons);
    if (dialog.exec() != QDialog::Accepted) return;
    const QString selected = method->currentData().toString();
    disableWalletActions();
    _balanceStarting = true;
    _api.post(QStringLiteral("/api/balances/payout-methods/apply-all"),
              {{QStringLiteral("method"), selected}},
              [this](bool ok, const QJsonDocument& doc, const QString& error) {
      _balanceStarting = false;
      if (!ok) { loadBalances(); return showError(QStringLiteral("Configuração não iniciada"), error); }
      _payoutMethodAwaitingResult = true;
      _balancePoll.start();
      setStatus(QStringLiteral("Configurando método de saque em %1 conta(s)…")
          .arg(doc.object().value(QStringLiteral("total")).toInt()));
    });
  });
  _balancesPayoutMethod->setParent(extraActions);
  _balancesWithdrawHistory = new QPushButton(QStringLiteral("Último lote"));
  _balancesWithdrawHistory->setEnabled(false);
  _balancesWithdrawHistory->setToolTip(QStringLiteral(
      "Mostra o resultado por conta do último lote de solicitações, mesmo após reiniciar o aplicativo."));
  connect(_balancesWithdrawHistory, &QPushButton::clicked, this, [this] {
    showWithdrawalReport(_lastWithdrawBulk);
  });
  _balancesWithdrawHistory->setParent(extraActions);
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
    QStringList lines{QStringLiteral("Conta;Tipo;Em revisão (USD);Disponível para saque (USD);Trânsito (USD);Retido (USD);Não aprovado (USD);Acumulado (USD);Leitura;Última tentativa;Consulta;Restrição;Motivo")};
    const auto walletRows = _balancesSnapshot.value(QStringLiteral("wallet")).toObject().value(QStringLiteral("accounts")).toObject();
    for (int row = 0; row < _balancesTable->rowCount(); ++row) {
      if (_balancesTable->isRowHidden(row)) continue;
      const auto* emailItem = _balancesTable->item(row, 0);
      if (!emailItem) continue;
      const QString email = emailItem->text();
      const auto balance = balances.value(email).toObject();
      const auto meta = walletRows.value(email).toObject();
      const bool isClaru = kinds.value(email).toString() == QStringLiteral("claru");
      QStringList values{csvCell(email), csvCell(isClaru ? QStringLiteral("Claru") : QStringLiteral("Crowtado"))};
      for (const auto key : {"pendingCents", "availableCents", "inTransitCents", "onHoldCents", "notApprovedCents", "lifetimeCents"}) {
        const auto value = balance.value(QLatin1String(key));
        const double amount = value.toDouble(-1);
        const bool valid = !isClaru && value.isDouble() && std::isfinite(amount) && amount >= 0
            && amount <= 9007199254740991.0 && std::floor(amount) == amount;
        values << csvCell(valid ? QString::number(amount / 100.0, 'f', 2).replace(QLatin1Char('.'), QLatin1Char(',')) : QString());
      }
      values << csvCell(balance.value(QStringLiteral("updated_at")).toString())
             << csvCell(balance.value(QStringLiteral("checked_at")).toString())
             << csvCell(isClaru ? QStringLiteral("Não se aplica") : !_walletSnapshotHealthy
                  ? QStringLiteral("Serviço indisponível; histórico")
                  : meta.value(QStringLiteral("reading")).toObject().value(QStringLiteral("label")).toString())
             << csvCell(meta.value(QStringLiteral("restriction")).toObject().value(QStringLiteral("label")).toString())
             << csvCell(meta.value(QStringLiteral("restriction")).toObject().value(QStringLiteral("reason")).toString());
      lines << values.join(QLatin1Char(';'));
    }
    QSaveFile output(path);
    const QByteArray bytes = QByteArray::fromHex("efbbbf") + lines.join(QLatin1Char('\n')).toUtf8();
    if (!output.open(QIODevice::WriteOnly) || output.write(bytes) != bytes.size() || !output.commit()) {
      showError(QStringLiteral("Saldos não exportados"), QStringLiteral("Não foi possível salvar o CSV."));
      return;
    }
    setStatus(QStringLiteral("Saldos exportados para %1.").arg(QDir::toNativeSeparators(path)));
  });
  _balancesExport->setParent(extraActions);
  auto* restrictions = new QPushButton(QStringLiteral("Restrições registradas"));
  restrictions->setToolTip(QStringLiteral("Abre o histórico das contas removidas após restrição confirmada. O login Crowtado pode funcionar mesmo com saque retido no Minute."));
  connect(restrictions, &QPushButton::clicked, this, [this] { _pages->setCurrentIndex(8); loadBanned(); });
  restrictions->setParent(extraActions);
  auto* more = new QToolButton;
  more->setText(QStringLiteral("Mais ações"));
  more->setPopupMode(QToolButton::InstantPopup);
  auto* menu = new QMenu(more);
  for (auto* button : {_balancesPayoutMethod, _balancesWiseCleanup, _balancesWithdrawHistory,
                       _balancesExport, restrictions, cleanMail}) {
    auto* action = menu->addAction(button->text());
    action->setToolTip(button->toolTip());
    connect(action, &QAction::triggered, button, &QPushButton::click);
    connect(menu, &QMenu::aboutToShow, button, [action,button,this] {
      action->setEnabled(button->isEnabled());
      if (button == _balancesWiseCleanup) action->setVisible(!button->isHidden());
    });
  }
  more->setMenu(menu);
  headerLayout->addWidget(more);
  headerLayout->addStretch();
  layout->addWidget(card(QStringLiteral("Consultas e saques"), header));

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
  headerLayout->insertWidget(1, _balancesRefreshNeeded);
  _balancesFilterState = quietLabel(QStringLiteral("0 contas"));
  filtersLayout->addWidget(_balancesFilterState);


  _balancesTable = new QTableWidget(0, 6);
  _balancesTable->setMinimumHeight(300);
  configureTable(_balancesTable, QStringLiteral("Uma visão dos seus saldos"),
                 QStringLiteral("Adicione suas contas e consulte os saldos para verificar os valores disponíveis."));
  _balancesTable->setHorizontalHeaderLabels({QStringLiteral("Conta"), QStringLiteral("Em revisão"),
      QStringLiteral("Disponível para saque"), QStringLiteral("Consulta / restrição"), QStringLiteral("Leitura"), QStringLiteral("Ações")});
  _balancesTable->horizontalHeader()->setMinimumSectionSize(100);
  _balancesTable->horizontalHeader()->setSectionResizeMode(0, QHeaderView::Stretch);
  _balancesTable->setColumnWidth(0, 245);
  _balancesTable->setColumnWidth(1, 120);
  _balancesTable->setColumnWidth(2, 155);
  _balancesTable->setColumnWidth(3, 255);
  _balancesTable->setColumnWidth(4, 150);
  _balancesTable->setColumnWidth(5, 270);
  _balancesTable->horizontalHeader()->setSectionResizeMode(5, QHeaderView::Fixed);
  _balancesTable->horizontalHeaderItem(1)->setToolTip(QStringLiteral("Valor pendente de revisão ou liberação pela Crowtado. Pode ser uma estimativa."));
  _balancesTable->horizontalHeaderItem(2)->setToolTip(QStringLiteral("Saldo disponível informado pela Crowtado. Uma retenção ou restrição de conta ainda pode impedir o saque."));
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
      QStringLiteral("Disponível para saque"), &_balancesApprovedUsd, &_balancesApprovedBrl), 1);
  auto* divider = new QFrame;
  divider->setFrameShape(QFrame::VLine);
  divider->setObjectName(QStringLiteral("balanceDivider"));
  totalsLayout->addWidget(divider);
  totalsLayout->addWidget(totalBlock(
      QStringLiteral("Em revisão"), &_balancesPendingUsd, &_balancesPendingBrl), 1);
  summaryLayout->addWidget(totals);
  auto* secondaryTotals = new QHBoxLayout;
  _balancesTransitUsd = quietLabel(QStringLiteral("Em trânsito: US$ —"));
  _balancesHoldUsd = quietLabel(QStringLiteral("Retido: US$ —"));
  secondaryTotals->addWidget(_balancesTransitUsd);
  secondaryTotals->addWidget(_balancesHoldUsd);

  delete secondaryTotals;
  _balancesTransitUsd->setParent(summaryBody);
  _balancesTransitUsd->hide();
  _balancesHoldUsd->setParent(summaryBody);
  _balancesHoldUsd->hide();
  _balancesCoverage = quietLabel(QStringLiteral("Nenhuma leitura confirmada"));
  _balancesCoverage->setWordWrap(true);
  summaryLayout->addWidget(_balancesCoverage);
  _balancesTotalsNote = quietLabel(QString());
  _balancesTotalsNote->setWordWrap(true);
  _balancesTotalsNote->hide();
  summaryLayout->addWidget(_balancesTotalsNote);
  _balancesExchange = quietLabel(QStringLiteral("Carregando cotação USD/BRL…"));
  _balancesExchange->setObjectName(QStringLiteral("balanceExchange"));
  summaryLayout->addWidget(_balancesExchange);
  auto* summary = card(QStringLiteral("SALDOS CONSULTADOS"), summaryBody);
  summary->setObjectName(QStringLiteral("walletContext"));
  summary->setSizePolicy(QSizePolicy::Expanding, QSizePolicy::Fixed);
  layout->insertWidget(0, summary);

  auto* scroll = new QScrollArea;
  scroll->setWidgetResizable(true);
  scroll->setFrameShape(QFrame::NoFrame);
  scroll->setWidget(body);
  return pageShell(QStringLiteral("Carteira"),
                   QStringLiteral("Em revisão, disponível para saque e situação de cada conta."), scroll);
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
      "Minute e Crowtado são verificados separadamente, incluindo acesso e retenção de saque. O saque depende do saldo Crowtado confirmado, "
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
        QString detail = monitor.value(QStringLiteral("detail")).toString() + QStringLiteral("\n")
            + monitor.value(QStringLiteral("balance_detail")).toString() + QStringLiteral("\nSaldo atualizado: ")
            + friendlyDate(monitor.value(QStringLiteral("balance_updated_at")).toString());
        const auto providers = monitor.value("providers").toObject();
        for (const auto& name : QStringList{"minute", "crowtado"}) {
          const auto check = providers.value(name).toObject();
          if (!check.isEmpty()) detail += QStringLiteral("\n%1: %2\n%3").arg(name == "minute" ? QStringLiteral("Minute") : QStringLiteral("Crowtado"),
              check.value("status_label").toString(), check.value("error").toString());
        }
        for (int col = 0; col < values.size(); ++col) {
          auto* item = new QTableWidgetItem(values[col]);
          item->setToolTip(detail.trimmed());
          _bannedTable->setItem(row, col, item);
        }
        const QString email = account.value(QStringLiteral("email")).toString();
        auto* reveal = credentialCopyActions(email, account.value(QStringLiteral("has_password")).toBool(), true);
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
  _historyVerifyPreviews = new QPushButton(QStringLiteral("Verificar prévias no Minute"));
  auto* verifyPreviews = _historyVerifyPreviews;
  connect(verifyPreviews, &QPushButton::clicked, this, [this, verifyPreviews] {
    if (_campaignResetPending || _historyVerifyPending) return;
    const int row = _historyTable->currentRow();
    if (row < 0 || !_historyTable->item(row, 0)) {
      return showError(QStringLiteral("Selecione uma campanha"),
                       QStringLiteral("Escolha uma linha do histórico antes de verificar as prévias."));
    }
    const QString name = _historyTable->item(row, 0)->data(Qt::UserRole).toString();
    if (name.isEmpty()) return;
    _historyVerifyPending = true;
    verifyPreviews->setEnabled(false);
    verifyPreviews->setText(QStringLiteral("Consultando o Minute…"));
    setStatus(QStringLiteral("Consultando o processamento real das prévias no Minute…"));
    _api.post(QStringLiteral("/api/logs/") + encoded(name) + QStringLiteral("/status"), {},
              [this, verifyPreviews, name](bool ok, const QJsonDocument& doc, const QString& error) {
      _historyVerifyPending = false;
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
            << QStringLiteral("PRÉVIAS NO MINUTE")
            << QStringLiteral("✓ %1 prévia(s) disponível(is)  ·  %2 aguardando publicação  ·  %3 indisponível(is)  ·  %4 falha(s) de consulta")
                   .arg(ready).arg(pending).arg(unavailable).arg(errors);
      if (pending > 0)
        lines << QStringLiteral("A ausência de uma prévia publicada não confirma o recebimento. Confira os recibos desta execução separadamente.");
      lines << attention;
      _historyDetail->setPlainText(lines.join(QLatin1Char('\n')));
      setStatus(QStringLiteral("Prévias: %1 disponíveis, %2 aguardando publicação, %3 indisponíveis.")
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
      int currentConfirmed=0, currentPending=0, currentReview=0, currentUnknown=0;
      for(const auto itemValue:root.value("items").toArray()) {
        const auto item=itemValue.toObject();
        for(const auto resultValue:item.value("accounts").toArray()) {
          const auto result=resultValue.toObject();
          const auto confirmation=result.value("confirmation").toString();
          const auto status=result.value("status").toString();
          const auto current=result.value("current_result").toObject();
          const auto found=current.value("chunks_found");
          const auto expected=current.value("chunks_expected");
          const bool confirmedNow=current.value("status").toString()=="confirmed"
              && current.value("evidence_source").toString()=="journal"
              && current.value("reconciled").isBool() && found.isDouble() && expected.isDouble()
              && expected.toDouble()>0 && std::floor(expected.toDouble())==expected.toDouble()
              && found.toDouble()==expected.toDouble() && !result.value("session_id").toString().isEmpty()
              && !result.value("email").toString().isEmpty() && !item.value("clip_uid").toString().isEmpty();
          if(status!="skipped") {
            if(confirmedNow) ++currentConfirmed;
            else if(current.value("status").toString()=="pending") ++currentPending;
            else if(current.value("status").toString()=="review") ++currentReview;
            else ++currentUnknown;
          }
          const QString proof=confirmedNow?QStringLiteral("Recibo atual confirmado"):
              confirmation=="remote_ack"?QStringLiteral("Confirmado na tentativa original"):
              confirmation=="legacy_record"?QStringLiteral("Registro legado"):
              status=="skipped"?QStringLiteral("Pulado"):
              status=="failed"?QStringLiteral("Falhou"):QStringLiteral("Sem confirmação");
          const int row=_historyEvidence->rowCount();
          _historyEvidence->insertRow(row);
          const QStringList values{result.value("email").toString(),item.value("clip_uid").toString(QStringLiteral("Não registrado")),result.value("session_id").toString(QStringLiteral("Não registrada")),proof};
          for(int column=0;column<values.size();++column) {
            auto* value=new QTableWidgetItem(values[column]);
            value->setToolTip(values[column]+QStringLiteral("\nTentativa original: ")+result.value("detail").toString()
                +QStringLiteral("\nRecibo atual: ")+current.value("detail").toString());
            _historyEvidence->setItem(row,column,value);
          }
          _historyEvidence->setRowHeight(row,48);
        }
      }
      QStringList lines;
      lines << QStringLiteral("RESULTADO DA TENTATIVA ORIGINAL")
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
      lines << QStringLiteral("SITUAÇÃO ATUAL DOS RECIBOS")
            << QStringLiteral("%1 confirmado(s) · %2 pendente(s) · %3 para revisar · %4 sem recibo atual")
                .arg(currentConfirmed).arg(currentPending).arg(currentReview).arg(currentUnknown)
            << QStringLiteral("A confirmação posterior não reescreve o resultado da tentativa original.")
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
    #campaignIndicator { background: %2; border: 1px solid %6; border-left: 4px solid #8b90a2; border-radius: 10px; }
    #campaignIndicator[state="running"], #campaignIndicator[state="starting"] { border-left-color: #754dff; }
    #campaignIndicator[state="error"], #campaignIndicator[state="unknown"] { border-left-color: #d58b25; }
    #campaignIndicator[state="done"] { border-left-color: #159a72; }
    #campaignIndicatorTitle { font-size: 18px; font-weight: 700; }
    #campaignIndicatorMetric { font-size: 15px; font-weight: 650; }
    #campaignDetails { color: %4; background: transparent; border: 1px solid %6; border-radius: 6px; padding: 6px 10px; }
    #campaignDetails:hover, #campaignDetails:checked { background: %5; }
    #campaignDetails:focus { border-color: #8058ff; }
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
    #inspectorBalanceCaption { color: #c3c6d2; font-size: 12px; letter-spacing: 1.5px; border-top: 1px solid #4a4e5c; padding-top: 14px; }
    #inspectorBalanceAction { background: transparent; color: #f5f6fa; border: 1px solid #a4aabd; border-radius: 7px; padding: 8px 16px; }
    #inspectorBalance { color: #f5f6fa; font-size: 36px; font-weight: 700; }
    #operationInspector #heroDescription { font-size: 13px; }
    #operationPage #workspaceTitle { font-size: 30px; letter-spacing: 0px; }
    #operationNotice { color: %4; background: %9; border: 1px solid #d58b25; border-radius: 8px; padding: 10px; }
    #liveBadge { color: %5; background: %9; border-radius: 8px; padding: 7px 10px; font-size: 12px; font-weight: 600; }
    #liveBadge[state="running"], #liveBadge[state="done"] { color: #006b4c; background: #d9f4e9; }
    #liveBadge[state="paused"], #liveBadge[state="stopping"], #liveBadge[state="error"] { color: #805000; background: #ffedcb; }
    #operationTotal { color: %4; font-size: 17px; font-weight: 650; }
    #operationTable { background: transparent; border: none; }
    #operationTable QHeaderView::section { background-color: %2; font-size: 12px; font-weight: 500; padding: 8px 8px; }
    #operationTitle { color: %4; font-size: 24px; font-weight: 700; }
    #operationSubtitle { color: %5; font-size: 14px; }
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
    #campaignProgress, #campaignPreviewProgress, #bulkRegisterProgress { min-height: 22px; max-height: 22px; color: %4; text-align: center; font-family: "Cascadia Mono", Consolas; font-size: 9px; font-weight: 700; }
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

void MainWindow::installUpdate(const QString& packagePath, const QString& verifiedSha256) {
  if (_closing || _campaignClosePending) return;
  const QRegularExpression digest(QStringLiteral("^[a-f0-9]{64}$"));
  if (verifiedSha256.size() != 64 || !digest.match(verifiedSha256).hasMatch()) {
    _updateButton->setEnabled(true);
    return showError(QStringLiteral("Atualização"),
                     QStringLiteral("A verificação do pacote de atualização não foi concluída."));
  }
  const QString appDir = QCoreApplication::applicationDirPath();
  const QString updater = appDir + QStringLiteral("/QMoneyUpdater.exe");
  if (!QFileInfo::exists(updater)) {
    _updateButton->setEnabled(true);
    _updateButton->setText(QStringLiteral("↻  Verificar atualização"));
    return showError(QStringLiteral("Atualização"),
                     QStringLiteral("O componente QMoneyUpdater.exe não foi encontrado."));
  }
  _pendingUpdatePackage = packagePath;
  _pendingUpdateSha256 = verifiedSha256;
  beginCampaignDrain();
}

bool MainWindow::launchPendingUpdate() {
  const QString appDir = QCoreApplication::applicationDirPath();
  const QString updater = appDir + QStringLiteral("/QMoneyUpdater.exe");
  const QRegularExpression digest(QStringLiteral("^[a-f0-9]{64}$"));
  if (_pendingUpdateSha256.size() != 64 || !digest.match(_pendingUpdateSha256).hasMatch()
      || !QFileInfo::exists(updater)) {
    _updateButton->setEnabled(true);
    showError(QStringLiteral("Atualização"),
              QStringLiteral("O instalador ou a verificação do pacote não está disponível."));
    return false;
  }
  const QStringList arguments = {
      QStringLiteral("--package"), _pendingUpdatePackage,
      QStringLiteral("--sha256"), _pendingUpdateSha256,
      QStringLiteral("--target"), appDir,
      QStringLiteral("--pid"), QString::number(QCoreApplication::applicationPid()),
      QStringLiteral("--launch"), QStringLiteral("QMoney.exe")};
  if (!QProcess::startDetached(updater, arguments, appDir)) {
    _updateButton->setEnabled(true);
    showError(QStringLiteral("Atualização"),
              QStringLiteral("Não foi possível iniciar o instalador da atualização."));
    return false;
  }
  setStatus(QStringLiteral("Fechando para instalar a atualização…"));
  return true;
}

void MainWindow::startBackend() {
  if (_closing || _campaignClosePending) return;
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
    _packagedServiceExecutable = QFileInfo(packagedService).canonicalFilePath();
    program = packagedService;
    arguments = {QStringLiteral("--no-browser"), QStringLiteral("--porta"),
                 QStringLiteral("8876"), QStringLiteral("--parent-pid"),
                 QString::number(QCoreApplication::applicationPid())};
    // Prefere a biblioteca de mídia que acompanha a instalação. No layout de
    // desenvolvimento o executável vive em dist/QMoney e os dados ficam dois
    // níveis acima; numa distribuição portátil eles podem ficar ao lado do EXE.
    workingDirectory = QStandardPaths::writableLocation(QStandardPaths::GenericDataLocation)
                       + QStringLiteral("/QMoney");
    // A escolha explícita da biblioteca também vale antes de sincronizar
    // seu catálogo. Fontes portáteis são procuradas só na ausência dessa escolha.
    const QString savedLibrary = QSettings().value(QStringLiteral("libraryRoot")).toString();
    const QString libraryRoot = selectLibraryRoot(savedLibrary, appDir, workingDirectory);
    QDir().mkpath(workingDirectory);
    // Contas e segredos pertencem ao usuário Windows, nunca à biblioteca.
    environment = packagedServiceEnvironment(environment);
    environment.insert(QStringLiteral("QMONEY_USER_ROOT"), workingDirectory);
    environment.insert(QStringLiteral("AWS_SHARED_CREDENTIALS_FILE"), workingDirectory + QStringLiteral("/secrets/aws/credentials"));
    environment.insert(QStringLiteral("AWS_CONFIG_FILE"), workingDirectory + QStringLiteral("/secrets/aws/config"));
    environment.insert(QStringLiteral("AWS_EC2_METADATA_DISABLED"), QStringLiteral("true"));
    environment.insert(QStringLiteral("QMONEY_LIBRARY_ROOT"), libraryRoot);
    environment.insert(QStringLiteral("QMONEY_RUNTIME_ROOT"), appDir + QStringLiteral("/runtime"));
    environment.insert(QStringLiteral("QMONEY_PORTABLE_ROOT"), appDir);
    environment.insert(QStringLiteral("QMONEY_APP_VERSION"),
                       QCoreApplication::applicationVersion());
    environment.insert(QStringLiteral("PLAYWRIGHT_BROWSERS_PATH"),
                       appDir + QStringLiteral("/runtime/ms-playwright"));
  } else {
    _packagedServiceExecutable.clear();
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
  drainBackendOutput();
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

void MainWindow::drainBackendOutput() {
  connect(&_backend, &QProcess::readyReadStandardOutput, this, [this] {
    // Subprocess output contains raw server diagnostics and access-log fragments.
    // Consume it without presenting it as user-facing status.
    _backend.readAllStandardOutput();
  });
}

void MainWindow::stopBackend() {
  _backendProbe.stop();
  ++_probeGeneration;
  _probeInFlight = false;
  if (_backend.state() != QProcess::NotRunning) {
    terminateOwnedServiceTree(_backend.processId(), _backend.program());
    _backend.terminate();
    if (!_backend.waitForFinished(1800)) {
      _backend.kill();
      _backend.waitForFinished(1000);
    }
  }
}

void MainWindow::restartBackend() {
  if (_closing || _campaignClosePending) return;
  _campaignRestartPending = true;
  _restartingBackend = true;
  beginCampaignDrain();
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
      if (!_runtimeChecked && qmoney::service::isExpectedServiceExecutable(
              QFileInfo(_backend.program()).canonicalFilePath(), _packagedServiceExecutable)) {
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
  if (_accountsSummary) {
    if (!ready) {
      ++_accountsRevision;
      ++_bulkRegisterDomainsRevision;
      ++_bulkRegisterPreflightRevision;
      ++_registrationProxiesRevision;
      _registrationProxiesReady = false;
      _bulkRegisterPreflightReady = false;
      _bulkRegisterPoll.stop();
      _bulkRegisterRequestInFlight = false;
      _bulkRegisterStarting = false;
      _accountConnecting = false;
      if (!_bulkRegisterPendingRequestId.isEmpty() && !_bulkRegisterBaselineReady) {
        _bulkRegisterPendingRequestId.clear();
        _bulkRegisterPendingPath.clear();
        _bulkRegisterPendingBody = {};
        _bulkRegisterPendingManualRegistration = false;
        _bulkRegisterPendingClearResults = false;
        _bulkRegisterOutcomeUnknown = false;
        _bulkRegisterRetryInFlight = false;
        _bulkRegisterStatus->setText(QStringLiteral("O serviço reiniciou antes do envio. Nenhum cadastro foi iniciado; os dados digitados foram mantidos."));
      } else if (!_bulkRegisterPendingRequestId.isEmpty() && !_bulkRegisterOwnRequestAccepted) {
        _bulkRegisterOutcomeUnknown = true;
      }
      _bulkRegisterStart->setEnabled(false);
      _bulkRegisterStop->setEnabled(false);
      _accountsAvailable = false;
      _accountsSummary->setText(QStringLiteral("Serviço indisponível · dados anteriores preservados."));
    }
    setAccountTransferBusy(_accountTransferBusy);
  }
  if (!ready && _operationPause) {
    ++_operationBackendGeneration;
    ++_operationGeneration;
    _operationPolling = false;
    operationUnavailable(message.isEmpty() ? QStringLiteral("Serviço local indisponível.") : message);
  }
  if (_nymeriaImport) {
    if (!ready) {
      _nymeriaPoll.stop();
      ++_nymeriaRequestId; ++_nymeriaJobRevision;
      _nymeriaInventoryPending = _nymeriaCommandPending = _nymeriaJobPending = false;
      _nymeriaRunning = false;
      invalidateNymeriaPlan();
    }
    updateNymeriaLibraryActions();
  }
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
    loadAccounts();
    if (!_bulkRegisterPendingRequestId.isEmpty() && _bulkRegisterBaselineReady && !_bulkRegisterOwnRequestAccepted)
      _bulkRegisterOutcomeUnknown = true;
    beginRegistrationPolling();
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
    if (_campaignStartUncertain) {
      _campaignPoll.start();
      QTimer::singleShot(0, this, &MainWindow::pollCampaign);
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
  if (index != 4) { _cachePoll.stop(); _preparedPoll.stop(); _localMediaPoll.stop(); _nymeriaPoll.stop(); }
  if (index != 6 && (!_walletMonitoring || !_walletMonitoring->isChecked())) _balancePoll.stop();
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
    case 4: loadAccelerator(); loadLocalMediaLibrary();
      if (_libraryTabs->currentIndex() == 1) loadPreparedLibrary();
      if (_libraryTabs->currentIndex() == 2) { loadNymeriaLibrary(); pollNymeriaJob(); }
      break;
    case 5:
      loadAccounts();
      beginRegistrationPolling();
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
        "\nContinue com as contas aprovadas usando esta mesma verificação. "
        "As contas com diagnóstico inconclusivo ficam fora somente desta campanha e permanecem cadastradas."));
  bool continueRequested = false;
  auto* details = new QPlainTextEdit;
  details->setReadOnly(true);
  details->setLineWrapMode(QPlainTextEdit::WidgetWidth);
  details->setPlainText(report);
  layout->addWidget(details, 1);
  auto* buttons = new QDialogButtonBox;
  if (continueAction) {
    auto* remove = buttons->addButton(QStringLiteral("Revisar contas aprovadas"), QDialogButtonBox::ActionRole);
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
  if (_campaignResetPending) return;
  if (_recoveryWorkerWatch) _recoveryWorkerWatch->stop();
  _recoveryWorkerWatchPolls = 0;
  auto* dialog = new QDialog(this);
  dialog->setObjectName(QStringLiteral("campaignRecoveryDialog"));
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
  summary->setObjectName(QStringLiteral("recoverySummary"));
  summary->setTextFormat(Qt::PlainText);
  summary->setTextInteractionFlags(Qt::TextSelectableByMouse);
  layout->addWidget(summary);
  auto* recoverySearch = new QLineEdit(dialog);
  recoverySearch->setObjectName(QStringLiteral("recoverySearch"));
  recoverySearch->setPlaceholderText(QStringLiteral("Buscar conta, clipe ou sessão"));
  recoverySearch->setAccessibleName(QStringLiteral("Buscar registros de recuperação"));
  layout->addWidget(recoverySearch);
  auto* table = new QTableWidget(0, 4);
  configureTable(table, QStringLiteral("Registros de recuperação"),
                 QStringLiteral("As sessões que ainda precisam de atenção aparecem aqui."));
  table->setHorizontalHeaderLabels({QStringLiteral("Conta"), QStringLiteral("Clipe"), QStringLiteral("Sessão"), QStringLiteral("Situação")});
  table->horizontalHeader()->setSectionResizeMode(QHeaderView::Stretch);
  table->setEditTriggers(QAbstractItemView::NoEditTriggers);
  layout->addWidget(table, 1);
  const auto filterRecovery = [table, recoverySearch] {
    for (int row = 0; row < table->rowCount(); ++row) {
      bool match = recoverySearch->text().isEmpty();
      for (int column = 0; column < table->columnCount(); ++column)
        if (table->item(row, column) && table->item(row, column)->text().contains(recoverySearch->text(), Qt::CaseInsensitive)) match = true;
      table->setRowHidden(row, !match);
    }
  };
  connect(recoverySearch, &QLineEdit::textChanged, dialog, filterRecovery);
  auto* failed = card(QStringLiteral("Leitura não concluída"), quietLabel(QStringLiteral(
      "A lista não foi carregada. Ainda não foi possível confirmar quais envios precisam de atenção.\n\n"
      "Clique em Tentar novamente. Se o erro persistir, copie o diagnóstico para identificar a causa.\n\n"
      "Os registros de envios anteriores foram preservados.")));
  failed->hide();
  layout->addWidget(failed, 1);
  auto* reconciliationHint = quietLabel(QStringLiteral("Reconciliar registra na lista local apenas finalizações já confirmadas. Essa ação não envia mídia e não solicita saques."));
  layout->addWidget(reconciliationHint);
  auto* resumePanel = new QWidget(dialog);
  auto* resumeRow = new QHBoxLayout;
  resumeRow->setContentsMargins(0, 0, 0, 0);
  resumePanel->setLayout(resumeRow);
  auto* resumeAccount = new ComboBox(dialog);
  resumeAccount->setAccessibleName(QStringLiteral("Conta para retomar envios"));
  resumeRow->addWidget(resumeAccount, 1);
  auto* resume = new QPushButton(QStringLiteral("Retomar envios desta conta"), dialog);
  resume->setEnabled(false);
  resumeRow->addWidget(resume);
  connect(table, &QTableWidget::itemSelectionChanged, dialog, [table, resume] {
    resume->setText(table->selectedItems().isEmpty() ? QStringLiteral("Retomar envios desta conta")
                    : QStringLiteral("Retomar sessão selecionada"));
  });
  layout->addWidget(resumePanel);
  auto* poll = new QTimer(dialog);
  poll->setObjectName(QStringLiteral("recoveryPoll"));
  poll->setInterval(1500);
  auto pollCount = std::make_shared<int>(0);
  auto* buttons = new QHBoxLayout;
  auto* retry = new QPushButton(QStringLiteral("Tentar novamente"), dialog);
  retry->setObjectName(QStringLiteral("recoveryRetry"));
  buttons->addWidget(retry);
  auto* copyDiagnostic = new QPushButton(QStringLiteral("Copiar diagnóstico"), dialog);
  copyDiagnostic->setObjectName(QStringLiteral("recoveryCopyDiagnostic"));
  copyDiagnostic->hide();
  buttons->addWidget(copyDiagnostic);
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
  const auto render = [this, guard, table, summary, reconcile, resume, resumeAccount, poll, pollCount, retry, copyDiagnostic, failed, resumePanel, reconciliationHint, filterRecovery](bool ok, const QJsonDocument& document, const QString& error) {
    if (!guard) return;
    retry->setEnabled(true);
    const auto data = document.object();
    const auto worker = data.value(QStringLiteral("worker")).toObject();
    const QString workerState = worker.value(QStringLiteral("state")).toString();
    if (!workerState.isEmpty()) {
      _recoveryWorkerRunning = workerState == QStringLiteral("running");
      guard->setProperty("recoveryBusy", _recoveryWorkerRunning);
      updateCampaignActions();
    }
    if (!ok) {
      poll->stop();
      table->hide();
      failed->show();
      resumePanel->hide();
      reconciliationHint->hide();
      reconcile->hide();
      summary->setText(error);
      if (data.value(QStringLiteral("code")).toString() == QStringLiteral("catalog_work_pending")
          && data.value(QStringLiteral("loading")).toBool()) {
        summary->setText(QStringLiteral("A leitura ainda está em andamento no serviço. O acompanhamento automático foi pausado; tente novamente para consultar o mesmo trabalho."));
      }
      reconcile->setEnabled(false);
      resume->setEnabled(false);
      resumeAccount->setEnabled(false);
      const auto diagnostic = document.object().value(QStringLiteral("recovery_error")).toObject();
      const auto report = QJsonObject{{"version", QCoreApplication::applicationVersion()},
          {"recovery_error", diagnostic}};
      guard->setProperty("recoveryDiagnostic", QJsonDocument(report).toJson(QJsonDocument::Indented));
      copyDiagnostic->setVisible(!diagnostic.isEmpty());
      return;
    }
    table->show();
    failed->hide();
    resumePanel->show();
    reconciliationHint->show();
    reconcile->show();
    copyDiagnostic->hide();
    guard->setProperty("recoveryDiagnostic", QByteArray());
    if (data.value(QStringLiteral("loading")).toBool()) {
      summary->setText(data.value(QStringLiteral("message")).toString());
      reconcile->setEnabled(false);
      resume->setEnabled(false);
      const int budget = qMax(1, guard->property("recoveryPollBudget").toInt() > 0
          ? guard->property("recoveryPollBudget").toInt() : 300);
      ++*pollCount;
      if (*pollCount >= budget) {
        poll->stop();
        retry->setEnabled(true);
        summary->setText(QStringLiteral("A leitura ainda está em andamento no serviço. O acompanhamento automático foi pausado; tente novamente para consultar o mesmo trabalho."));
      } else {
        retry->setEnabled(false);
        poll->start();
      }
      return;
    }
    *pollCount = 0;
    const auto items = data.value("items").toArray();
    const QString selectedAccount = resumeAccount->currentText();
    resumeAccount->clear();
    QSet<QString> resumable;
    table->setRowCount(items.size());
    for (int row = 0; row < items.size(); ++row) {
      const auto item = items[row].toObject();
      if (item.value("can_resume").toBool()) resumable.insert(item.value("email").toString());
      const QString status = item.value("status").toString() == "confirmed" ?
                             (item.value("publication_pending").toBool() ? QStringLiteral("Confirmado; revisar histórico") : QStringLiteral("Confirmado; reconciliar")) :
                             item.value("status").toString() == "archived_failed" ? QStringLiteral("Encerrado como falho no Minute") :
                             item.value("status").toString() == "pending" ? QStringLiteral("Envio pendente") : QStringLiteral("Revisar histórico");
      const QStringList values{item.value("email").toString(), item.value("clip_uid").toString(QStringLiteral("Não identificado")), item.value("session_id").toString(), status};
      for (int column = 0; column < values.size(); ++column) {
        auto* value = cell(values[column]);
        value->setData(Qt::UserRole, item.value("can_resume").toBool());
        value->setToolTip(values[column] + QStringLiteral("\n") + item.value("detail").toString()
            + (item.value("blocks_campaign").toBool(true)
               ? QStringLiteral("\nBloqueia novas campanhas nesta conta até identificar o clipe.")
               : item.value("terminal_remote_failure").toBool()
                 ? QStringLiteral("\nEste recibo não reserva mais o clipe; uma nova campanha pode tentar o conteúdo.")
                 : QStringLiteral("\nSomente este clipe fica reservado. A conta pode enviar outros conteúdos.")));
        table->setItem(row, column, value);
      }
    }
    filterRecovery();
    const int reconciliationPending = data.contains("reconciliation_pending") ? data.value("reconciliation_pending").toInt() : data.value("confirmed").toInt();
    const int publicationPending = data.value("publication_pending").toInt();
    summary->setText(QStringLiteral("%1 sessão(ões) pendente(s) · %2 confirmação(ões) para reconciliar")
        .arg(data.value("pending").toInt()).arg(reconciliationPending));
    QStringList accountNames(resumable.begin(), resumable.end());
    accountNames.sort();
    resumeAccount->addItems(accountNames);
    if (accountNames.contains(selectedAccount)) resumeAccount->setCurrentText(selectedAccount);
    const bool running = worker.value("state").toString() == "running";
    _recoveryWorkerRunning = running;
    guard->setProperty("recoveryBusy", running);
    updateCampaignActions();
    if (running) {
      summary->setText(QStringLiteral("Retomando as sessões existentes de %1…").arg(worker.value("email").toString()));
      poll->start();
    } else {
      poll->stop();
      if (!worker.value("error").toString().isEmpty()) summary->setText(worker.value("error").toString());
      else if (worker.value("state").toString()=="done") {
        const auto result=worker.value("result").toObject();
        summary->setText(QStringLiteral("Retomada concluída · %1 sessão(ões) reconciliada(s). Consulte os recibos atuais no Histórico.")
            .arg(result.value("reconciled").toInt()));
      } else if (worker.value("state").toString()=="pending") {
        summary->setText(QStringLiteral("Retomada encerrada com registros que ainda precisam de atenção. Consulte os recibos atuais no Histórico."));
      } else if (data.value("reconciled").toInt()>0) {
        summary->setText(QStringLiteral("%1 sessão(ões) reconciliada(s). A tentativa original permanece registrada no Histórico.")
            .arg(data.value("reconciled").toInt()));
      }
    }
    if (publicationPending > 0) {
      summary->setText(summary->text() + QStringLiteral(" · %1 confirmação(ões) preservada(s) nesta tela; revise o registro pendente no Histórico.").arg(publicationPending));
    }
    reconcile->setEnabled(!running && reconciliationPending > 0);
    resume->setEnabled(!running && !accountNames.isEmpty());
    resumeAccount->setEnabled(!running);
  };
  const auto fetch = [this, guard, wallet, render, pollCount](bool force) {
    if (!guard || guard->property("readingRecovery").toBool()) return;
    if (force) *pollCount = 0;
    guard->setProperty("readingRecovery", true);
    _api.get(force ? QStringLiteral("/api/recovery?async=1&refresh=1") : QStringLiteral("/api/recovery?async=1"), [guard, wallet, render](bool ok, const QJsonDocument& document, const QString& error) {
      if (!guard) return;
      guard->setProperty("readingRecovery", false);
      wallet->setVisible(document.object().value("wise_cleanup").toObject().value("pending").toBool());
      render(ok, document, error);
    });
  };
  const auto refresh = [fetch] { fetch(false); };
  connect(poll, &QTimer::timeout, dialog, refresh);
  connect(retry, &QPushButton::clicked, dialog, [fetch] { fetch(true); });
  connect(copyDiagnostic, &QPushButton::clicked, dialog, [guard] {
    if (guard) QApplication::clipboard()->setText(QString::fromUtf8(guard->property("recoveryDiagnostic").toByteArray()));
  });
  connect(resume, &QPushButton::clicked, dialog, [this, guard, table, resume, resumeAccount, summary, refresh] {
    QString email = resumeAccount->currentText();
    QString sessionId;
    if (!table->selectedItems().isEmpty() && table->currentRow() >= 0) {
      const auto* selected = table->item(table->currentRow(), 0);
      const auto* session = table->item(table->currentRow(), 2);
      if (!selected || !session || table->isRowHidden(table->currentRow())
          || !selected->data(Qt::UserRole).toBool()) {
        summary->setText(QStringLiteral("A sessão selecionada precisa de revisão e não será retomada automaticamente."));
        return;
      }
      email = selected->text();
      sessionId = session->text();
    }
    if (email.isEmpty() || !guard || _campaignResetPending || _recoveryCommandPending) return;
    if (QMessageBox::question(guard, QStringLiteral("Retomar envios existentes"),
        (sessionId.isEmpty() ? QStringLiteral("Verificar e retomar os envios interrompidos de %1? O recibo será consultado antes de transferir mídia. Envios encerrados como falhos serão preservados, liberando o conteúdo para uma nova campanha.").arg(email)
         : QStringLiteral("Verificar e retomar somente a sessão %1 de %2? O recibo será consultado primeiro. Se o Minute já o encerrou como falho, não haverá transferência; o diagnóstico será preservado e o conteúdo ficará disponível para uma nova campanha.").arg(sessionId, email)),
        QMessageBox::Yes | QMessageBox::No, QMessageBox::No) != QMessageBox::Yes) return;
    if (_campaignResetPending || _recoveryCommandPending) return;
    _recoveryCommandPending = true;
    updateCampaignActions();
    resume->setEnabled(false);
    QJsonObject request{{"email", email}, {"confirmed", true}};
    if (!sessionId.isEmpty()) request.insert(QStringLiteral("session_id"), sessionId);
    _api.post(QStringLiteral("/api/recovery/resume"), request,
              [this, guard, summary, resume, refresh](bool ok, const QJsonDocument&, const QString& error) {
      _recoveryCommandPending = false;
      const bool uncertain = !ok;
      if (ok || uncertain) _recoveryWorkerRunning = true;
      if (!guard && _recoveryWorkerRunning) watchRecoveryWorker();
      updateCampaignActions();
      if (!guard) return;
      if (!ok) {
        summary->setText(error);
        resume->setEnabled(!uncertain);
        if (uncertain) refresh();
      }
      else refresh();
    });
  });
  refresh();
  connect(reconcile, &QPushButton::clicked, dialog, [this, reconcile, render] {
    if (_campaignResetPending || _recoveryCommandPending) return;
    _recoveryCommandPending = true;
    updateCampaignActions();
    reconcile->setEnabled(false);
    _api.post(QStringLiteral("/api/recovery/reconcile"), {},
        [this, render](bool ok, const QJsonDocument& document, const QString& error) {
      _recoveryCommandPending = false;
      render(ok, document, error);
      updateCampaignActions();
    });
  });
  connect(dialog, &QObject::destroyed, this, [this] {
    if (_closing) return;
    if (_recoveryWorkerRunning) watchRecoveryWorker();
    updateCampaignActions();
  });
  dialog->show();
}

void MainWindow::watchRecoveryWorker() {
  if (!_recoveryWorkerRunning || _closing) return;
  if (!_recoveryWorkerWatch) {
    _recoveryWorkerWatch = new QTimer(this);
    _recoveryWorkerWatch->setObjectName(QStringLiteral("recoveryWorkerWatch"));
    _recoveryWorkerWatch->setInterval(1500);
    connect(_recoveryWorkerWatch, &QTimer::timeout, this, [this] {
      if (!_recoveryWorkerRunning || _closing) {
        _recoveryWorkerWatch->stop();
        return;
      }
      // Keep the UI lock conservative if status cannot be confirmed. A later
      // open of Recovery provides the manual retry path.
      if (_recoveryWorkerWatchPolls >= 1200) {
        _recoveryWorkerWatch->stop();
        return;
      }
      if (_recoveryStatusRequestPending) return;
      ++_recoveryWorkerWatchPolls;
      _recoveryStatusRequestPending = true;
      _api.get(QStringLiteral("/api/recovery?async=1"), [this](bool ok,
          const QJsonDocument& document, const QString&) {
        _recoveryStatusRequestPending = false;
        if (!ok) return;
        const QString state = document.object().value(QStringLiteral("worker")).toObject()
            .value(QStringLiteral("state")).toString();
        if (state.isEmpty()) return;
        _recoveryWorkerRunning = state == QStringLiteral("running");
        if (!_recoveryWorkerRunning) {
          _recoveryWorkerWatch->stop();
          _recoveryWorkerWatchPolls = 0;
        }
        updateCampaignActions();
      });
    });
  }
  _recoveryWorkerWatchPolls = 0;
  _recoveryWorkerWatch->start();
}

void MainWindow::openCommandPalette() {
  if (_campaignResetPending) return;
  auto* dialog = new QDialog(this);
  dialog->setObjectName(QStringLiteral("campaignCommandPalette"));
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

void MainWindow::loadOperationBalance() {
  const int generation = ++_operationBalanceGeneration;
  _operationBalanceReadAt = QDateTime::currentMSecsSinceEpoch();
  _api.get(QStringLiteral("/api/balances"), [this, generation](bool ok, const QJsonDocument& doc, const QString&) {
    if (_closing || generation != _operationBalanceGeneration) return;
    if (!ok) {
      _operationBalance->setText(QStringLiteral("US$ —"));
      _operationBalanceNote->setText(QStringLiteral("Não foi possível consultar os saldos."));
      return;
    }
    const auto root = doc.object();
    const auto wallet = root.value("wallet").toObject();
    const auto counts = wallet.value("counts").toObject();
    const auto cents = wallet.value("withdrawable_total_cents");
    const bool known = wallet.value("accounts").isObject() && counts.value("confirmed").toInt() > 0 && OperationSummary::number(cents);
    _operationBalance->setText(known ? usdMoney(qint64(cents.toDouble())) : QStringLiteral("US$ —"));
    _operationBalanceNote->setText(known ? QStringLiteral("%1 conta(s) elegível(is) · %2 requer(em) atenção")
        .arg(counts.value("eligible").toInt()).arg(counts.value("attention").toInt())
        : QStringLiteral("Saldo sem leitura confirmada. Atualize as contas na Carteira."));
  });
}

void MainWindow::loadHome() {
  if (_campaignResetPending) return;
  const int generation = ++_homeGeneration;
  _operationControlError.clear();
  _operationRefresh->setEnabled(false);
  _homeSync->setText(QStringLiteral("Atualizando…"));
  _homeNextAction->setEnabled(false);
  refreshOperation();
  _api.get(QStringLiteral("/api/accounts"), [this, generation](bool ok, const QJsonDocument& doc, const QString&) {
    if (_closing || generation != _homeGeneration) return;
    if (!ok || !doc.object().value("accounts").isArray()) {
      _homeAccounts->setText(QStringLiteral("—"));
      return;
    }
    _homeAccountSnapshot = doc.object().value("accounts").toArray();
    _homeAccounts->setText(QString::number(_homeAccountSnapshot.size()));
    if (_operationAvailable && !_operationPolling) renderOperation(_operationSnapshot);
  });
  loadOperationBalance();
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
        : QStringLiteral("! Componente ausente; use Corrigir instalação nesta tela."));
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
      QStringLiteral("Cole um token novo para conectar ou renovar a caixa"));
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
  if (_closing || _campaignClosePending) return;
  const QString selected = QFileDialog::getExistingDirectory(
      this, QStringLiteral("Escolher raiz da biblioteca QMoney"), _currentLibraryRoot);
  if (selected.isEmpty() || _closing || _campaignClosePending) return;
  const QDir root(selected);
  const bool hasCatalog = QFileInfo::exists(root.filePath(QStringLiteral("data/ego4d/timed_narrations.jsonl")))
                       || QFileInfo::exists(root.filePath(QStringLiteral("data/ego4d/clip_narrations.json")))
                       || QFileInfo::exists(root.filePath(QStringLiteral("data/holoassist")));
  if (!hasCatalog) {
    return showError(QStringLiteral("Biblioteca não reconhecida"),
                     QStringLiteral("Escolha a pasta raiz que contém data\\ego4d ou data\\holoassist."));
  }
  _pendingLibraryRoot = QDir::cleanPath(selected);
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

bool MainWindow::campaignResetBlocked() const {
  bool recoveryBusy = _recoveryCommandPending;
  for (auto* dialog : findChildren<QDialog*>())
    if (dialog->objectName() == QStringLiteral("campaignRecoveryDialog"))
      recoveryBusy |= dialog->property("recoveryBusy").toBool();
  return _closing || _campaignClosePending || _campaignActive || _campaignPreflightPending
      || _campaignStartPending || _campaignResetPending || _campaignOriginalDialogPending
      || _campaignStopPending || _operationPausePending || recoveryBusy
      || _campaignResetRunnerBusy;
}

void MainWindow::invalidateCampaignUiRequests() {
  _api.invalidateCampaignRequests();
  ++_campaignUiEpoch;
  ++_campaignPollRevision;
  ++_taskLoadGeneration;
  ++_homeGeneration;
  ++_operationGeneration;
  ++_campaignBalanceRequestId;
  _campaignPoll.stop();
  _previewPoll.stop();
  _operationPoll.stop();
  _taskReload.stop();
  _campaignDraftSave.stop();
  _campaignPollInFlight = false;
  _previewCheckActive = false;
  _campaignStartLookupInFlight = false;
  _taskRequestPending = false;
  _taskCatalogJobId.clear();
  _taskCatalogSelection.clear();
  _taskCatalogFallbackSelection.clear();
  _taskCatalogFallbackAnchor.clear();
  _taskCatalogFallbackAttempted.clear();
  _taskCatalogFallbackExhausted = false;
  _taskCatalogAutomaticPoll = false;
  _taskCatalogForceRefresh = false;
  _operationPolling = false;
  _historyVerifyPending = false;
  _historyVerifyPreviews->setText(QStringLiteral("Verificar prévias no Minute"));
}

void MainWindow::resetCampaigns() {
  if (campaignResetBlocked()) return;
  QMessageBox confirmation(QMessageBox::Warning, QStringLiteral("Reset completo"),
      QStringLiteral("Apaga o histórico de campanhas e envios, recibos, pendências, registro de vídeos usados e logs das campanhas.\n\n"
                     "Mantém as contas e a Biblioteca. Não altera os envios já feitos no Minute."),
      QMessageBox::NoButton, this);
  auto* reset = confirmation.addButton(QStringLiteral("Reset completo"), QMessageBox::DestructiveRole);
  auto* cancel = confirmation.addButton(QMessageBox::Cancel);
  confirmation.setDefaultButton(cancel);
  confirmation.exec();
  if (confirmation.clickedButton() != reset || campaignResetBlocked()) return;
  _campaignResetPending = true;
  invalidateCampaignUiRequests();
  const quint64 epoch = _campaignUiEpoch;
  _campaignReset->setText(QStringLiteral("Resetando…"));
  updateCampaignActions();
  _api.post(QStringLiteral("/api/campaign/reset"), {},
      [this, epoch](bool ok, const QJsonDocument&, const QString& error) {
    if (epoch != _campaignUiEpoch) return;
    const bool preferencesCleared = !ok || clearCampaignUiAfterReset();
    _campaignResetPending = false;
    _campaignReset->setText(QStringLiteral("Reset completo"));
    updateCampaignActions();
    if (!ok) {
      if (_campaignActive || _campaignStartUncertain) _campaignPoll.start();
      if (!_previewLogName.isEmpty()) _previewPoll.start();
      if (_pages->currentIndex() == 0) _operationPoll.start();
      loadTasks();
      return showError(QStringLiteral("Reset completo não realizado"), error);
    }
    loadTasks();
    if (_pages->currentIndex() == 0) _operationPoll.start();
    if (!preferencesCleared)
      return showError(QStringLiteral("Preferências do reset não salvas"),
          QStringLiteral("Os registros locais das campanhas foram apagados, mas não foi possível salvar a limpeza das preferências desta tela. "
                         "Use Reset completo novamente para concluir. Contas e Biblioteca foram preservadas."));
    setStatus(QStringLiteral("Reset completo concluído. Contas e Biblioteca preservadas."));
  });
}

bool MainWindow::clearCampaignUiAfterReset() {
  invalidateCampaignUiRequests();
  for (auto* dialog : findChildren<QDialog*>())
    if (dialog->objectName() == QStringLiteral("campaignRecoveryDialog")
        || dialog->objectName() == QStringLiteral("campaignCommandPalette")
        || dialog->objectName() == QStringLiteral("operationAccountDialog")) dialog->close();
  QSettings settings;
  for (const auto& key : {QStringLiteral("campaign/pendingStart"), QStringLiteral("campaign/draft"),
                         QStringLiteral("campaign/accountLastUsed"), QStringLiteral("previewLogName")})
    settings.remove(key);
  settings.sync();
  _campaignRequestedPreflight.clear();
  _campaignStartUncertain = settings.status() != QSettings::NoError;
  _campaignActive = false;
  _campaignResetRunnerBusy = false;
  _campaignStartPending = false;
  _campaignPreflightPending = false;
  _campaignStopPending = false;
  _operationPausePending = false;
  _operationPauseRequested = false;
  _operationControlError.clear();
  _recoveryCommandPending = false;
  _previewLogName.clear();
  _lastCampaignSeq = 0;
  _pendingHistoryFocus.clear();
  _campaignDraftLoaded = false;
  _campaignDraftAccounts.clear();
  _campaignSelectedTaskIds.clear();
  _campaignTaskSelectionTouched = false;
  _taskRecords = {};
  {
    const QSignalBlocker blocker(_campaignTasks);
    _campaignTasks->clear();
  }
  _campaignFeed->clear();
  _campaignProgress->setRange(0, 100);
  _campaignProgress->setValue(0);
  _campaignProgress->setFormat(QStringLiteral("%p%"));
  _campaignStage->setText(QStringLiteral("Aguardando"));
  _campaignCurrent->setText(QStringLiteral("Nenhuma campanha em andamento."));
  _campaignStats->setText(QStringLiteral("Envios nesta execução: 0 concluídos · 0 ignorados · 0 falhas"));
  _campaignStart->setText(QStringLiteral("Iniciar campanha"));
  _campaignStop->setEnabled(false);
  resetCampaignPreviewDisplay();
  setCampaignIndicator(QStringLiteral("Nenhuma campanha em andamento"),
      QStringLiteral("Revise uma nova campanha para começar."), QStringLiteral("idle"));
  _campaignIndicatorProgress->setValue(0);
  {
    const QSignalBlocker blocker(_historyTable);
    _historyTable->setRowCount(0);
    _historyTable->setCurrentCell(-1, -1);
  }
  _historyDetail->clear();
  _historyEvidence->setRowCount(0);
  _operationSearch->clear();
  _operationAttention->setChecked(false);
  renderOperation({{"state", "idle"}, {"operation", QJsonObject{{"accounts", QJsonArray{}}, {"counts", QJsonObject{}}}},
      {"totals", QJsonObject{{"total_sends", 0}, {"ok_sends", 0}, {"failed_sends", 0}, {"skipped_sends", 0}}},
      {"events", QJsonArray{}}});
  _operationStages->clear();
  _operationFeed->clear();
  _operationInspector->hide();
  _homeCampaigns->setText(QStringLiteral("0"));
  _homeSuccess->setText(QStringLiteral("0 / 0"));
  _homeRecent->setText(QStringLiteral("Nenhuma campanha no histórico. Revise uma nova campanha para começar."));
  return settings.status() == QSettings::NoError;
}

void MainWindow::updateCampaignActions() {
  bool accounts = false, tasks = false;
  for (int i = 0; i < _campaignAccounts->count(); ++i)
    accounts |= _campaignAccounts->item(i)->checkState() == Qt::Checked;
  for (int i = 0; i < _campaignTasks->count(); ++i) {
    auto* item = _campaignTasks->item(i);
    tasks |= item->checkState() == Qt::Checked && (item->flags() & Qt::ItemIsEnabled)
        && !item->data(Qt::UserRole).toString().isEmpty();
  }
  const bool busy = _campaignActive || _campaignPreflightPending || _campaignStartPending
      || _campaignStartUncertain || _campaignResetPending || _campaignOriginalDialogPending
      || _recoveryCommandPending || _recoveryWorkerRunning;
  const bool balancesReady = !_campaignAccountMode->currentData().toString().startsWith(QStringLiteral("balance_"))
      || _campaignBalancesLoaded;
  _campaignStart->setEnabled(!busy && accounts && tasks && balancesReady
      && !_taskRequestPending && !_taskReload.isActive());
  _campaignReset->setEnabled(!campaignResetBlocked());
  if (_historyVerifyPreviews) _historyVerifyPreviews->setEnabled(!_campaignResetPending && !_historyVerifyPending);
  if (_campaignOriginal) _campaignOriginal->setEnabled(!busy && !_closing && !_campaignClosePending
      && _backendReady && _campaignAccounts->count() > 0);
}

void MainWindow::loadCampaignData() {
  if (_campaignResetPending) return;
  // Até o motor responder, mantenha ações destrutivas e uma nova partida
  // bloqueadas. Isso evita uma janela curta em que a tela ainda não conhece
  // uma campanha em execução ou encerramento.
  _campaignActive = true;
  _campaignStart->setEnabled(false);
  _campaignReset->setEnabled(false);
  const quint64 epoch = _campaignUiEpoch;
  const int accountRefreshGeneration = ++_campaignAccountRefreshGeneration;
  _api.get(QStringLiteral("/api/accounts"), [this, epoch, accountRefreshGeneration](
      bool ok, const QJsonDocument& doc, const QString& error) {
    if (epoch != _campaignUiEpoch || accountRefreshGeneration != _campaignAccountRefreshGeneration) return;
    if (!ok) return showError(QStringLiteral("Falha ao carregar contas"), error);
    _campaignAccountsLoaded = true;
    QSet<QString> selectedBefore;
    const bool hadAccounts = _campaignAccounts->count() > 0;
    for (int i = 0; i < _campaignAccounts->count(); ++i)
      if (_campaignAccounts->item(i)->checkState() == Qt::Checked)
        selectedBefore.insert(_campaignAccounts->item(i)->data(Qt::UserRole).toString());
    const QSignalBlocker blocker(_campaignAccounts);
    _campaignAccounts->clear();
    for (const auto value : doc.object().value(QStringLiteral("accounts")).toArray()) {
      const auto account = value.toObject();
      const QString email = account.value(QStringLiteral("email")).toString().trimmed();
      if (_campaignPermanentlyRemovedAccounts.contains(email.toCaseFolded())) continue;
      auto* item = new QListWidgetItem(account.value(QStringLiteral("email")).toString());
      item->setSizeHint(QSize(0, 38));
      item->setFlags(item->flags() | Qt::ItemIsUserCheckable);
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

QStringList MainWindow::selectedCampaignAccountEmails() const {
  QStringList selected;
  if (!_campaignAccounts) return selected;
  for (int i = 0; i < _campaignAccounts->count(); ++i) {
    const auto* item = _campaignAccounts->item(i);
    if (item->checkState() != Qt::Checked) continue;
    const QString email = item->data(Qt::UserRole).toString().trimmed();
    if (!email.isEmpty()) selected.append(email);
  }
  return selected;
}

QString MainWindow::campaignCatalogSelectionKey() const {
  QStringList selection = selectedCampaignAccountEmails();
  for (auto& email : selection) email = email.toCaseFolded();
  selection << _dataset->currentData().toString()
            << _contentMode->currentData().toString()
            << QString::number(_minDuration->value())
            << QString::number(_maxDuration->value());
  return selection.join(QChar(0x1f));
}

void MainWindow::refreshCampaignAccountsAfterPermanentRemoval(const QString& email) {
  const QString removed = email.trimmed().toCaseFolded();
  if (removed.isEmpty()) return;
  _campaignPermanentlyRemovedAccounts.insert(removed);
  const int refreshGeneration = ++_campaignAccountRefreshGeneration;

  _campaignDraftAccounts.remove(email);
  for (auto it = _campaignDraftAccounts.begin(); it != _campaignDraftAccounts.end();) {
    if (it->toCaseFolded() == removed) it = _campaignDraftAccounts.erase(it);
    else ++it;
  }
  {
    const QSignalBlocker blocker(_campaignAccounts);
    for (int i = _campaignAccounts->count() - 1; i >= 0; --i) {
      if (_campaignAccounts->item(i)->data(Qt::UserRole).toString().trimmed().toCaseFolded() == removed)
        delete _campaignAccounts->takeItem(i);
    }
  }
  _campaignAccountsLoaded = true;
  _campaignAccountCount->setMaximum(qMax(1, _campaignAccounts->count()));
  _campaignAccountCount->setEnabled(_campaignAccounts->count() > 0);
  _campaignDrawAccounts->setEnabled(_campaignAccounts->count() > 0);
  updateCampaignAccountCount();
  saveCampaignDraft();
  updateCampaignActions();

  const quint64 epoch = _campaignUiEpoch;
  const int backendGeneration = _operationBackendGeneration;
  _api.get(QStringLiteral("/api/accounts"),
      [this, removed, epoch, backendGeneration, refreshGeneration](
          bool ok, const QJsonDocument& doc, const QString&) {
    if (epoch != _campaignUiEpoch || backendGeneration != _operationBackendGeneration || !_backendReady
        || refreshGeneration != _campaignAccountRefreshGeneration)
      return;
    const auto accountValues = doc.object().value(QStringLiteral("accounts")).toArray();
    if (!ok || !doc.object().value(QStringLiteral("accounts")).isArray()) {
      setStatus(QStringLiteral("A conta confirmada como removida saiu da seleção. A lista de contas não pôde ser atualizada agora."));
      return;
    }
    QSet<QString> owners;
    for (const auto value : accountValues) {
      const QString owner = value.toObject().value(QStringLiteral("email")).toString().trimmed();
      const QString key = owner.toCaseFolded();
      if (key.isEmpty() || owners.contains(key)) {
        setStatus(QStringLiteral("A conta removida saiu da seleção. A lista atual de contas veio inconsistente e foi preservada."));
        return;
      }
      owners.insert(key);
    }

    QSet<QString> selected;
    QHash<QString, QPair<QString, QString>> presentation;
    for (int i = 0; i < _campaignAccounts->count(); ++i) {
      const auto* item = _campaignAccounts->item(i);
      const QString key = item->data(Qt::UserRole).toString().trimmed().toCaseFolded();
      if (item->checkState() == Qt::Checked) selected.insert(key);
      presentation.insert(key, {item->text(), item->toolTip()});
    }
    {
      const QSignalBlocker blocker(_campaignAccounts);
      _campaignAccounts->clear();
      for (const auto value : accountValues) {
        const auto account = value.toObject();
        const QString owner = account.value(QStringLiteral("email")).toString().trimmed();
        const QString key = owner.toCaseFolded();
        if (key == removed || _campaignPermanentlyRemovedAccounts.contains(key)) continue;
        auto* item = new QListWidgetItem(owner, _campaignAccounts);
        item->setSizeHint(QSize(0, 38));
        item->setFlags(item->flags() | Qt::ItemIsUserCheckable);
        item->setData(Qt::UserRole, owner);
        item->setCheckState(selected.contains(key) ? Qt::Checked : Qt::Unchecked);
        item->setHidden(!owner.contains(_campaignAccountSearch->text().trimmed(), Qt::CaseInsensitive));
        const auto old = presentation.value(key);
        if (!old.first.isEmpty()) item->setText(old.first);
        if (!old.second.isEmpty()) item->setToolTip(old.second);
      }
    }
    _campaignAccountsLoaded = true;
    _campaignAccountCount->setMaximum(qMax(1, _campaignAccounts->count()));
    _campaignAccountCount->setEnabled(_campaignAccounts->count() > 0);
    _campaignDrawAccounts->setEnabled(_campaignAccounts->count() > 0);
    updateCampaignAccountCount();
    saveCampaignDraft();
    updateCampaignActions();
  });
}

void MainWindow::loadTasks() {
  const bool automaticPoll = _taskCatalogAutomaticPoll;
  _taskCatalogAutomaticPoll = false;
  if (_campaignResetPending) return;
  _taskReload.stop();
  const int generation = ++_taskLoadGeneration;
  if (_taskRequestPending) {
    // The reply schedules the latest selection. A repeating timer here
    // invalidates slow replies and needlessly polls while HTTP is in flight.
    _campaignStart->setEnabled(false);
    return;
  }
  const bool forceRefresh = _taskCatalogForceRefresh;
  const QStringList selectedAccounts = selectedCampaignAccountEmails();
  const QString fallbackSelection = campaignCatalogSelectionKey();
  if (forceRefresh || _taskCatalogFallbackSelection != fallbackSelection) {
    _taskCatalogFallbackSelection = fallbackSelection;
    _taskCatalogFallbackAttempted.clear();
    _taskCatalogFallbackAnchor.clear();
    _taskCatalogFallbackExhausted = false;
  }
  QString account;
  if (!_taskCatalogFallbackAnchor.isEmpty()) {
    for (const auto& candidate : selectedAccounts) {
      if (candidate.compare(_taskCatalogFallbackAnchor, Qt::CaseInsensitive) == 0
          && !_taskCatalogFallbackAttempted.contains(candidate.toCaseFolded())) {
        account = candidate;
        break;
      }
    }
  }
  if (account.isEmpty()) {
    for (const auto& candidate : selectedAccounts) {
      if (!_taskCatalogFallbackAttempted.contains(candidate.toCaseFolded())) {
        account = candidate;
        break;
      }
    }
  }
  if (account.isEmpty()) {
    if (!selectedAccounts.isEmpty() && _taskCatalogFallbackExhausted) {
      const QSignalBlocker blocker(_campaignTasks);
      _campaignTasks->clear();
      _taskRecords = {};
      auto* failure = new QListWidgetItem(QStringLiteral(
          "Falha ao carregar categorias nas contas selecionadas. As contas sem restrição confirmada continuam selecionadas; use Recarregar categorias para tentar novamente."),
          _campaignTasks);
      failure->setFlags(Qt::NoItemFlags);
      _campaignStart->setEnabled(false);
      return;
    }
    const QSignalBlocker blocker(_campaignTasks);
    _campaignTasks->clear();
    _taskRecords = {};
    _taskCatalogJobId.clear();
    _taskCatalogSelection.clear();
    _campaignStart->setEnabled(false);
    return;
  }
  if (!automaticPoll) {
    const QSignalBlocker blocker(_campaignTasks);
    _campaignTasks->clear();
    auto* loading = new QListWidgetItem(QStringLiteral("Carregando categorias…"), _campaignTasks);
    loading->setFlags(Qt::NoItemFlags);
  }
  _campaignStart->setEnabled(false);
  QString path = QStringLiteral(
      "/api/tasks?async=1&email=%1&min_dur_s=%2&max_dur_s=%3&dataset=%4&content_mode=%5")
      .arg(encoded(account)).arg(_minDuration->value() * 60)
      .arg(_maxDuration->value() * 60)
      .arg(encoded(_dataset->currentData().toString()))
      .arg(encoded(_contentMode->currentData().toString()));
  const bool selectionChanged = path != _taskCatalogSelection;
  if (selectionChanged) {
    _taskCatalogSelection = path;
    _taskCatalogJobId.clear();
  }
  if (selectionChanged || !automaticPoll || !_taskCatalogPollTimer.isValid()) {
    _taskCatalogPollCount = 0;
    _taskCatalogIdlePollCount = 0;
    _taskCatalogProgress.clear();
    _taskCatalogTimeoutPollCount = 0;
    _taskCatalogTimedOut = false;
    _taskCatalogPollTimer.restart();
    _taskCatalogIdleTimer.restart();
    _taskCatalogTimeoutTimer.invalidate();
  }
  if (!_taskCatalogJobId.isEmpty()) path += QStringLiteral("&job_id=%1").arg(encoded(_taskCatalogJobId));
  else if (_taskCatalogForceRefresh) path += QStringLiteral("&refresh=1");
  _taskCatalogForceRefresh = false;
  _taskRequestPending = true;
  _api.get(path, [this, generation, account, selectedAccounts, fallbackSelection](
                 bool ok, const QJsonDocument& doc, const QString& error) {
    _taskRequestPending = false;
    if (generation != _taskLoadGeneration || _taskReload.isActive()) {
      loadTasks();
      return;
    }
    if (fallbackSelection != campaignCatalogSelectionKey()
        || std::none_of(selectedAccounts.cbegin(), selectedAccounts.cend(),
                        [&account](const QString& selected) {
                          return selected.compare(account, Qt::CaseInsensitive) == 0;
                        })) {
      loadTasks();
      return;
    }
    const QSignalBlocker blocker(_campaignTasks);
    _campaignTasks->clear();
    _taskRecords = {};
    const auto body = doc.object();
    const QString code = body.value(QStringLiteral("code")).toString();
    const QString jobId = body.value(QStringLiteral("job_id")).toString();
    const bool boundJob = body.value(QStringLiteral("identity_bound")).toBool()
        && QRegularExpression(QStringLiteral("^[0-9a-f]{32}$")).match(jobId).hasMatch();
    const bool liveTimeout = !ok && code == QStringLiteral("catalog_work_pending")
        && body.value(QStringLiteral("loading")).toBool() && boundJob;
    if (!ok && !liveTimeout) {
      const QJsonObject issue = body.value(QStringLiteral("issue")).toObject();
      const QString issueEmail = issue.value(QStringLiteral("email")).toString().trimmed();
      const QString issueCode = issue.value(QStringLiteral("code")).toString().trimmed();
      const QString issueStage = issue.value(QStringLiteral("stage")).toString();
      const bool boundAccountIssue = code == QStringLiteral("catalog_account_unavailable")
          && issueEmail.compare(account, Qt::CaseInsensitive) == 0
          && !issueCode.isEmpty()
          && issueStage == QStringLiteral("Carregamento remoto de categorias");
      if (boundAccountIssue) {
        _taskCatalogFallbackAttempted.insert(account.toCaseFolded());
        QString archiveWarning;
        if (body.value(QStringLiteral("removal_failed")).toBool()) {
          const QJsonObject archiveIssue = body.value(QStringLiteral("archive_issue")).toObject();
          const QString archiveCode = archiveIssue.value(QStringLiteral("code")).toString();
          const QString archiveStage = archiveIssue.value(QStringLiteral("stage")).toString();
          const QString archiveReason = archiveIssue.value(QStringLiteral("reason")).toString();
          if (archiveCode == QStringLiteral("local_archive_failed")
              && archiveStage == QStringLiteral("Arquivamento local") && !archiveReason.isEmpty()) {
            archiveWarning = QStringLiteral("Restrição remota confirmada, mas o arquivamento local não foi concluído. A conta permanece na lista e precisa de revisão.");
            for (int i = 0; i < _campaignAccounts->count(); ++i) {
              auto* item = _campaignAccounts->item(i);
              if (item->data(Qt::UserRole).toString().compare(account, Qt::CaseInsensitive) != 0) continue;
              const QString previous = item->toolTip().trimmed();
              item->setToolTip((previous.isEmpty() ? QString() : previous + QLatin1Char('\n')) + archiveWarning);
              break;
            }
          }
        }
        if (body.value(QStringLiteral("permanently_removed")).isBool()
            && body.value(QStringLiteral("permanently_removed")).toBool()) {
          refreshCampaignAccountsAfterPermanentRemoval(account);
          _taskCatalogFallbackSelection = campaignCatalogSelectionKey();
          _taskCatalogFallbackAttempted.remove(account.toCaseFolded());
        }
        QString nextAccount;
        for (const auto& candidate : selectedCampaignAccountEmails()) {
          if (!_taskCatalogFallbackAttempted.contains(candidate.toCaseFolded())) {
            nextAccount = candidate;
            break;
          }
        }
        if (!nextAccount.isEmpty()) {
          _taskCatalogFallbackAnchor = nextAccount;
          setStatus(archiveWarning.isEmpty()
              ? QStringLiteral("Não foi possível consultar o catálogo de %1. Tentando a próxima conta selecionada: %2.")
                    .arg(account, nextAccount)
              : archiveWarning + QStringLiteral(" Tentando a próxima conta selecionada: %1.").arg(nextAccount));
          loadTasks();
          return;
        }
        _taskCatalogFallbackAnchor.clear();
        _taskCatalogFallbackExhausted = true;
        auto* failure = new QListWidgetItem(QStringLiteral(
            "Falha ao carregar categorias nas contas selecionadas. As contas sem restrição confirmada continuam selecionadas; use Recarregar categorias para tentar novamente."),
            _campaignTasks);
        failure->setFlags(Qt::NoItemFlags);
        _campaignStart->setEnabled(false);
        setStatus(QStringLiteral("A consulta do catálogo falhou para todas as contas selecionadas restantes."));
        return;
      }
      if (body.value(QStringLiteral("error_code")).toString() != QStringLiteral("request_outcome_unknown"))
        _taskCatalogJobId.clear();
      auto* failure = new QListWidgetItem(QStringLiteral("Falha: ") + error, _campaignTasks);
      failure->setFlags(Qt::NoItemFlags);
      return;
    }
    if (body.value(QStringLiteral("loading")).toBool()) {
      if (boundJob) {
        if (!_taskCatalogJobId.isEmpty() && _taskCatalogJobId != jobId) {
          auto* failure = new QListWidgetItem(QStringLiteral("A consulta de categorias mudou. Use Recarregar categorias."), _campaignTasks);
          failure->setFlags(Qt::NoItemFlags);
          _taskCatalogJobId.clear();
          return;
        }
        _taskCatalogJobId = jobId;
      }
      QString message = body.value(QStringLiteral("message")).toString(
          QStringLiteral("Preparando categorias…"));
      const QString progress = body.value(QStringLiteral("phase")).toString()
          + QLatin1Char('\n') + message;
      if (progress != _taskCatalogProgress) {
        _taskCatalogProgress = progress;
        _taskCatalogIdlePollCount = 0;
        _taskCatalogIdleTimer.restart();
      }
      const int elapsed = body.value(QStringLiteral("elapsed_s")).toInt();
      if (elapsed > 0) message += QStringLiteral(" (%1 s)").arg(elapsed);
      if (liveTimeout) {
        if (!_taskCatalogTimedOut) {
          _taskCatalogTimedOut = true;
          _taskCatalogTimeoutTimer.restart();
        }
        message = error + QStringLiteral(" · ") + message;
      }
      ++_taskCatalogPollCount;
      ++_taskCatalogIdlePollCount;
      if (_taskCatalogTimedOut) ++_taskCatalogTimeoutPollCount;
      const bool paused = _taskCatalogTimedOut
          ? _taskCatalogTimeoutPollCount >= 30 || _taskCatalogTimeoutTimer.elapsed() >= 60000
          : _taskCatalogPollCount >= 1500 || _taskCatalogPollTimer.elapsed() >= 1800000
              || _taskCatalogIdlePollCount >= 250 || _taskCatalogIdleTimer.elapsed() >= 300000;
      if (paused) message += QStringLiteral(
          " · Acompanhamento pausado. O cálculo continua no serviço; use Recarregar categorias para consultar esta mesma consulta.");
      auto* item = new QListWidgetItem(message, _campaignTasks);
      item->setFlags(Qt::NoItemFlags);
      item->setToolTip(message);
      if (_pages->currentIndex() == 3 && !paused) _taskReload.start(_taskCatalogTimedOut ? 2000 : 1200);
      return;
    }
    _taskCatalogJobId.clear();
    _taskCatalogTimedOut = false;
    _taskCatalogFallbackAnchor = account;
    _taskCatalogFallbackExhausted = false;
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
                                  : QStringLiteral("Inclui cortes narrados e clipes oficiais compatíveis com a tarefa. Os sensores são verificados antes do envio.")));
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
    updateCampaignActions();
    if (compatible == 0) {
      setStatus(QStringLiteral("Nenhuma categoria tem clipe compatível nesta origem e duração."));
    } else {
      setStatus(QStringLiteral("%1 categoria(s) compatível(is) de %2 carregadas.")
                    .arg(compatible).arg(_campaignTasks->count()));
    }
  });
}

void MainWindow::selectAllCompatibleTasks() {
  _campaignSelectedTaskIds.clear();
  _campaignTaskSelectionTouched = false;
  {
    const QSignalBlocker blocker(_campaignTasks);
    for (int i=0; i<_campaignTasks->count(); ++i) {
      auto* item = _campaignTasks->item(i);
      const bool compatible = (item->flags() & Qt::ItemIsEnabled) && !item->data(Qt::UserRole).toString().isEmpty();
      item->setCheckState(compatible ? Qt::Checked : Qt::Unchecked);
    }
  }
  updateCampaignActions();
  saveCampaignDraft();
  if (_backendReady) loadTasks();
}

void MainWindow::chooseOriginalCapture() {
  if (_closing || _campaignClosePending || !_backendReady || _campaignActive || _campaignPreflightPending
      || _campaignStartPending || _campaignStartUncertain || _campaignResetPending || _campaignOriginalDialogPending) return;
  QStringList accounts;
  for (int i = 0; i < _campaignAccounts->count(); ++i) {
    const auto email = _campaignAccounts->item(i)->data(Qt::UserRole).toString();
    if (!email.trimmed().isEmpty() && !accounts.contains(email)) accounts << email;
  }
  if (accounts.isEmpty()) return showError(QStringLiteral("Conta necessária"), QStringLiteral("Conecte uma conta antes de selecionar uma captura original."));
  _campaignOriginalDialogPending = true;
  updateCampaignActions();
  const auto finishSelection = qScopeGuard([this] { _campaignOriginalDialogPending = false; updateCampaignActions(); });
  OriginalCaptureSelectionDialog dialog(accounts, [this](const QString& email, OriginalCaptureSelectionDialog::TaskReply reply) {
    _api.get(QStringLiteral("/api/campaigns/original/tasks?account_email=%1").arg(encoded(email)),
      [reply = std::move(reply)](bool ok, const QJsonDocument& doc, const QString& error) {
        reply(ok && doc.isObject(), doc.object(), error);
      });
  }, this);
  if (dialog.exec() != QDialog::Accepted || _closing || _campaignClosePending) return;
  preflightOriginalCapture(dialog.requestBody());
}

void MainWindow::preflightOriginalCapture(QJsonObject body) {
  if (_closing || _campaignClosePending || !_backendReady || _campaignActive || _campaignPreflightPending
      || _campaignStartPending || _campaignStartUncertain || _campaignResetPending) return;
  _campaignPreflightPending = true;
  ++_campaignPollRevision;
  updateCampaignActions();
  setCampaignIndicator(QStringLiteral("Verificando captura original"),
      QStringLiteral("Conferindo grupo, hashes e vínculos. Nenhum envio iniciado."), QStringLiteral("starting"), true);
  _api.post(QStringLiteral("/api/campaigns/original/preflight"), body,
    [this, body](bool ok, const QJsonDocument& doc, const QString& error) {
      const auto finish = qScopeGuard([this] { _campaignPreflightPending = false; updateCampaignActions(); });
      if (_closing || _campaignClosePending) return;
      const auto result = doc.object();
      const auto receipt = result.value(QStringLiteral("preflight_id"));
      const auto summary = result.value(QStringLiteral("original_summary")).toObject();
      const bool valid = ok && doc.isObject() && result.value(QStringLiteral("ok")) == QJsonValue(true)
          && receipt.isString() && !receipt.toString().trimmed().isEmpty() && receipt.toString() == receipt.toString().trimmed()
          && result.value(QStringLiteral("receipt")) == receipt
            && result.value(QStringLiteral("account_email")).isString()
            && result.value(QStringLiteral("account_email")) == body.value(QStringLiteral("account_email"))
            && result.value(QStringLiteral("task_id")).isString()
            && result.value(QStringLiteral("task_id")) == body.value(QStringLiteral("task_id"))
            && result.value(QStringLiteral("start_request_id")).isString()
            && result.value(QStringLiteral("start_request_id")) == body.value(QStringLiteral("start_request_id"))
          && result.value(QStringLiteral("completion_policy")) == QJsonValue(QStringLiteral("explicit_finalize"))
          && result.value(QStringLiteral("readiness")).toObject().value(QStringLiteral("ready")) == QJsonValue(true)
          && originalCaptureSummaryValid(summary, body);
      if (!valid) {
        const auto message = ok ? QStringLiteral("O serviço não confirmou uma prévia válida da captura original. Revise os arquivos e tente novamente.") : error;
        setCampaignIndicator(QStringLiteral("Captura original não iniciada"), message, QStringLiteral("error"));
        return showError(QStringLiteral("Captura original não iniciada"), message);
      }
      OriginalCaptureReviewDialog review(summary, body, this);
      setCampaignIndicator(QStringLiteral("Revisar captura original"),
          QStringLiteral("Confira o grupo e a conta. Origem física não comprovada pela validação local."), QStringLiteral("idle"));
      if (review.exec() != QDialog::Accepted) {
        setCampaignIndicator(QStringLiteral("Captura original não iniciada"), QStringLiteral("Revisão cancelada. Nenhum envio iniciado."), QStringLiteral("idle"));
        return;
      }
      auto approved = body;
      approved.insert(QStringLiteral("preflight_id"), receipt);
      submitOriginalCapture(approved, summary);
    });
}

void MainWindow::submitOriginalCapture(QJsonObject body, QJsonObject reviewedSummary) {
  if (_closing || _campaignClosePending || !_backendReady || _campaignActive || _campaignStartPending || _campaignStartUncertain || _campaignResetPending) return;
  const auto receipt = body.value(QStringLiteral("preflight_id"));
  const auto operation = body.value(QStringLiteral("start_request_id"));
  if (!receipt.isString() || receipt.toString().trimmed().isEmpty() || receipt.toString() != receipt.toString().trimmed()
      || !operation.isString() || QUuid(operation.toString()).isNull()
      || body.value(QStringLiteral("evaluate")) != QJsonValue(true) || body.value(QStringLiteral("finalize")) != QJsonValue(true)
      || !originalCaptureSummaryValid(reviewedSummary, body)) {
    return showError(QStringLiteral("Captura original não iniciada"), QStringLiteral("A revisão original não está disponível. Revise novamente antes de enviar."));
  }
  if (!rememberCampaignStart(operation.toString())) return;
  ++_campaignPollRevision;
  _campaignStartPending = true;
  _lastCampaignSeq = 0;
  updateCampaignActions();
  _campaignTabs->setCurrentIndex(2);
  _campaignProgress->setRange(0, 0);
  _campaignStage->setText(QStringLiteral("Iniciando captura original…"));
  _campaignCurrent->setText(QStringLiteral("Aguardando confirmação da operação original, sem alterar seus arquivos."));
  setCampaignIndicator(QStringLiteral("Iniciando captura original…"), _campaignCurrent->text(), QStringLiteral("starting"), true);
  _api.post(QStringLiteral("/api/campaigns/original"), body,
    [this, body, reviewedSummary](bool ok, const QJsonDocument& doc, const QString& error) {
      _campaignStartPending = false;
      if (_closing || _campaignClosePending) return;
      const auto result = doc.object();
      const auto accounts = result.value(QStringLiteral("accounts")).toArray();
        const auto tasks = result.value(QStringLiteral("selected_tasks")).toArray();
        const auto sends = result.value(QStringLiteral("total_sends"));
      const bool valid = ok && doc.isObject() && result.value(QStringLiteral("ok")) == QJsonValue(true)
          && result.value(QStringLiteral("already_running")).isBool()
          && result.value(QStringLiteral("start_request_id")) == body.value(QStringLiteral("start_request_id"))
          && result.value(QStringLiteral("preflight_id")) == body.value(QStringLiteral("preflight_id"))
          && accounts.size() == 1 && accounts[0] == body.value(QStringLiteral("account_email"))
            && tasks.size() == 1 && tasks[0].isObject()
            && tasks[0].toObject().value(QStringLiteral("task_id")) == body.value(QStringLiteral("task_id"))
            && sends.isDouble() && std::isfinite(sends.toDouble()) && sends.toDouble() > 0 && sends.toDouble() == std::floor(sends.toDouble())
          && originalCaptureSummaryValid(result.value(QStringLiteral("original_summary")).toObject(), body)
            && result.value(QStringLiteral("original_summary")).toObject().value(QStringLiteral("plan_digest")) == reviewedSummary.value(QStringLiteral("plan_digest"))
            && result.value(QStringLiteral("original_summary")).toObject().value(QStringLiteral("content_digest")) == reviewedSummary.value(QStringLiteral("content_digest"));
      if (!valid) {
        const auto code = result.value(QStringLiteral("error_code")).toString();
          const QStringList rejectedCodes{QStringLiteral("original_invalid_request"), QStringLiteral("original_capture_invalid"),
              QStringLiteral("original_account_unavailable"), QStringLiteral("original_identity_changed"),
              QStringLiteral("original_policy_unavailable"), QStringLiteral("original_catalog_unavailable"),
              QStringLiteral("original_task_unavailable"), QStringLiteral("original_preflight_missing"),
              QStringLiteral("original_preflight_expired"), QStringLiteral("original_request_changed"),
              QStringLiteral("original_source_changed"), QStringLiteral("original_busy"),
              QStringLiteral("original_recovery_pending"), QStringLiteral("original_start_conflict"), QStringLiteral("campaign_closing")};
          const bool safelyRejected = !ok && result.value(QStringLiteral("ok")) == QJsonValue(false) && rejectedCodes.contains(code);
        if (safelyRejected) {
          clearCampaignStart();
          _campaignProgress->setRange(0, 100);
          setCampaignIndicator(QStringLiteral("Captura original não iniciada"), error, QStringLiteral("error"));
          updateCampaignActions();
          return showError(QStringLiteral("Captura original não iniciada"), error);
        }
        _campaignStartUncertain = true;
        _campaignProgress->setRange(0, 100);
        _campaignStage->setText(QStringLiteral("Início original não confirmado"));
        _campaignCurrent->setText(QStringLiteral("A operação pode ter sido aceita. Consultando o mesmo identificador sem solicitar outro início."));
        setCampaignIndicator(QStringLiteral("Início original não confirmado"), _campaignCurrent->text(), QStringLiteral("unknown"));
        updateCampaignActions();_campaignPoll.start();pollCampaign();return;
      }
      _campaignActive = true;
      _campaignStop->setEnabled(true);
      _previewPoll.stop();_previewLogName.clear();_previewCheckActive = false;
      resetCampaignPreviewDisplay();
      QSettings().remove(QStringLiteral("previewLogName"));
        if (!result.value(QStringLiteral("already_running")).toBool()) _campaignFeed->clear();
      _campaignStart->setText(QStringLiteral("Campanha em andamento"));
      setCampaignIndicator(QStringLiteral("Captura original em andamento"),
          QStringLiteral("Operação confirmada; recebimento e qualidade serão acompanhados separadamente."), QStringLiteral("running"), true);
      updateCampaignActions();_campaignPoll.start();pollCampaign();
    });
}

void MainWindow::startCampaign() {
  if (_campaignActive || _campaignPreflightPending || _campaignStartPending
      || _campaignStartUncertain || _campaignResetPending) return;
  if (_campaignAccountMode->currentData().toString().startsWith(QStringLiteral("balance_"))
      && !_campaignBalancesLoaded)
    return showError(QStringLiteral("Saldos ainda não carregados"),
                     QStringLiteral("Aguarde a leitura dos saldos antes de iniciar."));
  if (_taskReload.isActive() || _taskRequestPending)
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
      {QStringLiteral("run_until_exhausted"), true},
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
  preflightCampaign(body, selectedAccountNames);
}

void MainWindow::preflightCampaign(QJsonObject body, QStringList selectedAccountNames) {
  if (_campaignPreflightPending || _campaignStartPending) return;
  _campaignPreflightPending = true;
  _campaignPreflightRecoveries = 0;
  updateCampaignActions();
  ++_campaignPollRevision;
  setCampaignIndicator(QStringLiteral("Verificando campanha"),
                       QStringLiteral("Validando contas e clipes. Nenhum envio iniciado."), QStringLiteral("starting"), true);
  _campaignStart->setEnabled(false);
  _campaignStart->setText(QStringLiteral("Verificando campanha…"));
  const QString path = QStringLiteral("/api/campaigns/preflight?async=1&request_id=%1")
      .arg(QUuid::createUuid().toString(QUuid::Id128));
  pollCampaignPreflight(body, selectedAccountNames, path);
}

void MainWindow::pollCampaignPreflight(QJsonObject body, QStringList selectedAccountNames, const QString& path) {
  _api.post(path, body,
            [this, body, selectedAccountNames, path](bool ok, const QJsonDocument& doc, const QString& error) {
    const auto reply = doc.object();
    const bool uncertain = reply.value(QStringLiteral("error_code")) == QJsonValue(QStringLiteral("request_outcome_unknown"))
        && (reply.value(QStringLiteral("transport_error")) == QJsonValue(QStringLiteral("timeout"))
            || reply.value(QStringLiteral("transport_error")) == QJsonValue(QStringLiteral("connection")));
    const bool pending = reply.value(QStringLiteral("loading")) == QJsonValue(true)
        && reply.value(QStringLiteral("code")) == QJsonValue(QStringLiteral("catalog_work_pending"));
    if (!_closing && !_campaignClosePending && !ok && (uncertain || pending)
        && _campaignPreflightRecoveries < 4) {
      ++_campaignPreflightRecoveries;
      setCampaignIndicator(QStringLiteral("Verificando campanha"),
                           QStringLiteral("Recuperando a consulta da mesma verificação (%1/4). Nenhum envio iniciado.")
                               .arg(_campaignPreflightRecoveries), QStringLiteral("starting"), true);
      QTimer::singleShot(1200, this, [this, body, selectedAccountNames, path] {
        if (_closing || _campaignClosePending) {
          _campaignPreflightPending = false;
          return;
        }
        pollCampaignPreflight(body, selectedAccountNames, path);
      });
      return;
    }
    if (!_closing && !_campaignClosePending && ok && doc.object().value(QStringLiteral("loading")) == QJsonValue(true)) {
      QString message = doc.object().value(QStringLiteral("message")).toString(QStringLiteral("Verificando campanha…"));
      const int elapsed = doc.object().value(QStringLiteral("elapsed_s")).toInt();
      if (elapsed > 0) message += QStringLiteral(" (%1 s)").arg(elapsed);
      setCampaignIndicator(QStringLiteral("Verificando campanha"), message, QStringLiteral("starting"), true);
      QTimer::singleShot(1200, this, [this, body, selectedAccountNames, path] {
        if (_closing || _campaignClosePending) {
          _campaignPreflightPending = false;
          return;
        }
        pollCampaignPreflight(body, selectedAccountNames, path);
      });
      return;
    }
    const auto finishVerification = qScopeGuard([this] {
      _campaignPreflightPending = false;
      if (!_campaignStartPending) _campaignStart->setText(QStringLiteral("Iniciar campanha"));
      updateCampaignActions();
    });
    if (_closing || _campaignClosePending) return;
    if (!ok) {
      _campaignStart->setText(QStringLiteral("Iniciar campanha"));
      updateCampaignActions();
      setCampaignIndicator(QStringLiteral("Verificação não concluída"), error, QStringLiteral("error"));
      return showError(QStringLiteral("Verificação não concluída"), error);
    }
    const auto result = doc.object();
    const auto receipt = result.value(QStringLiteral("preflight_id"));
    const bool canSkip = result.value(QStringLiteral("can_skip_and_continue")).toBool();
    const bool canContinue = result.value(QStringLiteral("can_remove_and_continue")).toBool() || canSkip;
    const bool actionable = result.value(QStringLiteral("ok")).toBool() || canContinue;
    const bool receiptValid = receipt.isString() && !receipt.toString().trimmed().isEmpty()
        && receipt.toString() == receipt.toString().trimmed();
    if (!doc.isObject() || !result.value(QStringLiteral("ok")).isBool()
        || (result.contains(QStringLiteral("can_remove_and_continue"))
            && !result.value(QStringLiteral("can_remove_and_continue")).isBool())
        || (result.contains(QStringLiteral("can_skip_and_continue"))
            && !result.value(QStringLiteral("can_skip_and_continue")).isBool())
        || (actionable && !receiptValid)) {
      const QString message = QStringLiteral("O serviço não confirmou uma prévia válida. Revise a campanha novamente antes de iniciar.");
      setCampaignIndicator(QStringLiteral("Verificação não concluída"), message, QStringLiteral("error"));
      return showError(QStringLiteral("Verificação não concluída"), message);
    }
    QSet<QString> removedNow;
    for (const auto value : result.value(QStringLiteral("removed_accounts")).toArray())
      removedNow.insert(value.toString());
    QStringList reviewNames = selectedAccountNames;
    if (!removedNow.isEmpty()) {
      const QSignalBlocker listBlocker(_campaignAccounts);
      for (int i = _campaignAccounts->count() - 1; i >= 0; --i)
        if (removedNow.contains(_campaignAccounts->item(i)->data(Qt::UserRole).toString()))
          delete _campaignAccounts->takeItem(i);
      updateCampaignAccountCount();
      reviewNames.clear();
      const auto requestedAccounts = body.value(QStringLiteral("accounts")).toArray();
      for (int i = 0; i < requestedAccounts.size(); ++i)
        if (!removedNow.contains(requestedAccounts.at(i).toString()))
          reviewNames.append(selectedAccountNames.value(i));
      setStatus(QStringLiteral("%1 conta(s) com restrição confirmada foram para Banidas e saíram desta campanha.")
          .arg(removedNow.size()));
      loadAccounts();
    }
    const auto blockers = result.value(QStringLiteral("blockers")).toArray();
    QStringList blockerLines;
    for (const auto& value : blockers) blockerLines << QStringLiteral("• ") + value.toString();
    if (CampaignReviewDialog::requestedSeconds(result, body) > 0
        && result.value("accounts").toObject().value("validated").toInt() > 0
        && result.value("account_issues").toArray().isEmpty() && !result.value("recovery_error").isObject()
        && !CampaignReviewDialog::capacityAllowsStart(result, body)) {
      setCampaignIndicator(QStringLiteral("Campanha não iniciada"),
          QStringLiteral("O conteúdo novo não foi confirmado como suficiente para a meta. Revise a capacidade por conta."), QStringLiteral("error"));
      CampaignReviewDialog review(result, reviewNames, this, body);
      review.exec();
      return;
    }
    if (!result.value(QStringLiteral("ok")).toBool() || !blockerLines.isEmpty()) {
      setCampaignIndicator(QStringLiteral("Campanha não iniciada"),
                           QStringLiteral("Revise as pendências da verificação."), QStringLiteral("error"));
      _campaignStart->setText(QStringLiteral("Iniciar campanha"));
      updateCampaignActions();
      if (result.value(QStringLiteral("recovery_error")).isObject()) {
        QMessageBox message(QMessageBox::Warning, QStringLiteral("Campanha não iniciada"),
                            blockerLines.join(QLatin1Char('\n')), QMessageBox::Close, this);
        auto* open = message.addButton(QStringLiteral("Abrir recuperação"), QMessageBox::ActionRole);
        message.exec();
        if (message.clickedButton() == open) openRecovery();
        return;
      }
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
      if (canContinue) {
        QJsonObject continuation = body;
        continuation.insert(QStringLiteral("preflight_id"), result.value(QStringLiteral("preflight_id")));
        if (result.value(QStringLiteral("can_remove_and_continue")).toBool())
          continuation.insert(QStringLiteral("remove_restricted"), true);
        if (canSkip) continuation.insert(QStringLiteral("skip_unverified"), true);
        continueAction = [this, continuation, result, selectedAccountNames] {
          QSet<QString> removed;
          for (const auto value : result.value("removable_accounts").toArray()) removed.insert(value.toString());
          for (const auto value : result.value("skippable_accounts").toArray()) removed.insert(value.toString());
          for (const auto value : result.value("removed_accounts").toArray()) removed.insert(value.toString());
          QStringList included;
          const auto requested = continuation.value("accounts").toArray();
          for (int i = 0; i < requested.size(); ++i)
            if (!removed.contains(requested[i].toString())) included.append(selectedAccountNames.value(i));
          auto reviewed = result;
          reviewed.insert("ok", true);
          reviewed.insert("blockers", QJsonArray{});
          reviewed.insert("account_errors", QJsonArray{});
          reviewed.insert("account_issues", QJsonArray{});
          auto metrics = reviewed.value("accounts").toObject();
          const int validated = metrics.value("validated").toInt();
          metrics.insert("validated", included.size());
          reviewed.insert("accounts", metrics);
          reviewed.insert("account_workers", qMin(reviewed.value("account_workers").toInt(), int(included.size())));
          if (CampaignReviewDialog::validCapacity(reviewed, continuation))
            reviewed.insert("estimated_sends", reviewed.value("capacity").toObject().value("estimated_sends"));
          else if (validated > 0)
            reviewed.insert("estimated_sends", reviewed.value("estimated_sends").toInt() * int(included.size()) / validated);
          auto warnings = reviewed.value("warnings").toArray();
          if (continuation.value("skip_unverified").toBool())
            warnings.append(QStringLiteral("%1 conta(s) com verificação pendente ficam fora somente desta campanha. Os acessos permanecem cadastrados.")
                .arg(result.value("skippable_accounts").toArray().size()));
          if (continuation.value("remove_restricted").toBool())
            warnings.append(QStringLiteral("Ao confirmar, %1 conta(s) com restrição confirmada serão movidas para Banidas.")
                .arg(result.value("removable_accounts").toArray().size()));
          reviewed.insert("warnings", warnings);
          CampaignReviewDialog review(reviewed, included, this, continuation);
          setCampaignIndicator(QStringLiteral("Aguardando sua confirmação"),
                               QStringLiteral("Confira a prévia antes de iniciar os envios."), QStringLiteral("idle"));
          if (review.exec() == QDialog::Accepted) submitCampaign(continuation);
          else setCampaignIndicator(QStringLiteral("Campanha não iniciada"), QStringLiteral("A revisão foi cancelada. Nenhum envio iniciado."), QStringLiteral("idle"));
        };
      }
      return showAccountIssues(QStringLiteral("Campanha não iniciada — verificação pendente"),
                               blockerLines, issues, continueAction);
    }

    if (!removedNow.isEmpty()) {
      // The Start action already authorized this campaign. A confirmed
      // restriction changes only its participants; the server-owned receipt
      // and capacity for the survivors remain the same validated operation.
      QJsonObject approved = body;
      approved.insert(QStringLiteral("preflight_id"), receipt);
      submitCampaign(approved);
      return;
    }

    CampaignReviewDialog review(result, reviewNames, this, body);
    setCampaignIndicator(QStringLiteral("Aguardando sua confirmação"),
                         QStringLiteral("Confira a prévia antes de iniciar os envios."), QStringLiteral("idle"));
    if (review.exec() != QDialog::Accepted) {
      setCampaignIndicator(QStringLiteral("Campanha não iniciada"), QStringLiteral("A revisão foi cancelada. Nenhum envio iniciado."), QStringLiteral("idle"));
      _campaignStart->setText(QStringLiteral("Iniciar campanha"));
      updateCampaignActions();
      return;
    }

    QJsonObject approved = body;
    approved.insert(QStringLiteral("preflight_id"), result.value(QStringLiteral("preflight_id")));
    submitCampaign(approved);
  });
}

void MainWindow::submitCampaign(QJsonObject body) {
  if (_closing) return;
  const auto receipt = body.value(QStringLiteral("preflight_id"));
  if (!receipt.isString() || receipt.toString().trimmed().isEmpty()
      || receipt.toString() != receipt.toString().trimmed()) {
    const QString message = QStringLiteral("A prévia da campanha não está disponível. Revise novamente antes de iniciar.");
    setCampaignIndicator(QStringLiteral("Campanha não iniciada"), message, QStringLiteral("error"));
    updateCampaignActions();
    return showError(QStringLiteral("Campanha não iniciada"), message);
  }
  if (!rememberCampaignStart(body.value(QStringLiteral("preflight_id")).toString())) return;
  ++_campaignPollRevision;
  _campaignStartPending = true;
  _campaignStage->setText(QStringLiteral("Iniciando campanha…"));
  _campaignCurrent->setText(QStringLiteral("Validando os registros locais e aguardando o serviço."));
  _campaignProgress->setRange(0, 0);
  _campaignTabs->setCurrentIndex(2);
  setCampaignIndicator(QStringLiteral("Iniciando campanha…"),
                       QStringLiteral("Aguardando confirmação do serviço. Os envios ainda não foram confirmados."), QStringLiteral("starting"), true);
  _campaignStart->setEnabled(false);
    _campaignStart->setText(QStringLiteral("Iniciando…"));
    _api.post(QStringLiteral("/api/campaigns"), body,
              [this, body](bool started, const QJsonDocument& startDoc, const QString& startError) {
      _campaignStartPending = false;
      if (_closing) return;
      _campaignStart->setText(QStringLiteral("Iniciar campanha"));
      if (!started) {
        setCampaignIndicator(QStringLiteral("Não foi possível confirmar o início"), startError, QStringLiteral("error"));
        updateCampaignActions();
        const auto code = startDoc.object().value(QStringLiteral("error_code")).toString();
        _campaignProgress->setRange(0, 100);
        if (code == "request_outcome_unknown" || code == "start_outcome_unknown"
            || code == "start_state_unreadable" || code == "start_request_conflict") {
          _campaignStartUncertain = true;
          _campaignStart->setEnabled(false);
          _campaignStart->setText(QStringLiteral("Início não confirmado"));
          _campaignStage->setText(QStringLiteral("Resposta do serviço indisponível"));
          _campaignCurrent->setText(QStringLiteral("A solicitação pode ter sido aceita. Consultando a execução sem solicitar outro início."));
          setCampaignIndicator(QStringLiteral("Início não confirmado"), _campaignCurrent->text(), QStringLiteral("unknown"));
          _campaignPoll.start();
          pollCampaign();
          return;
        }
        clearCampaignStart();
        _campaignStage->setText(QStringLiteral("Campanha não iniciada"));
        _campaignCurrent->setText(startError);
        if (code == "recovery_unidentified") {
          setStatus(startError);
          openRecovery();
          return;
        }
        if (code == "preflight_expired" || code == "preflight_missing"
            || code == "preflight_accounts_changed" || code == "preflight_request_changed"
            || code == "preflight_history_changed") {
          auto refreshed = body;
          refreshed.remove(QStringLiteral("preflight_id"));
          refreshed.remove(QStringLiteral("remove_restricted"));
          refreshed.remove(QStringLiteral("skip_unverified"));
          QStringList names;
          for (const auto account : refreshed.value("accounts").toArray()) names.append(account.toString());
          setStatus(QStringLiteral("Atualizando a prévia. Revise novamente antes de confirmar o início."));
          preflightCampaign(refreshed, names);
          return;
        }
        return showError(QStringLiteral("Campanha não iniciada"), startError);
      }
      if (startDoc.object().value(QStringLiteral("already_running")).toBool()) {
        setCampaignIndicator(QStringLiteral("Campanha em andamento"), QStringLiteral("Consultando a atividade atual…"), QStringLiteral("running"), true);
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
      _campaignStart->setEnabled(false);
      _campaignStart->setText(QStringLiteral("Campanha em andamento"));
      _campaignStop->setEnabled(true);
      setCampaignIndicator(QStringLiteral("Campanha em andamento"), QStringLiteral("Início confirmado. Preparando os primeiros envios…"), QStringLiteral("running"), true);
      _campaignReset->setEnabled(false);
      _lastCampaignSeq = 0;
      _previewPoll.stop();
      _previewLogName.clear();
      resetCampaignPreviewDisplay();
      _previewCheckActive = false;
      QSettings().remove(QStringLiteral("previewLogName"));
      _campaignFeed->clear();
      _campaignPoll.start();
      pollCampaign();
      setStatus(QStringLiteral("Campanha iniciada."));
    });
  }


void MainWindow::setCampaignIndicator(const QString& title, const QString& detail, const QString& state, bool busy) {
  _campaignIndicatorTitle->setText(title);
  _campaignIndicatorDetail->setText(detail);
  _campaignIndicatorTitle->setToolTip(detail);
  static_cast<CampaignStatusIcon*>(_campaignIndicatorIcon)->setState(state, title);
  _campaignIndicatorMetric->clear();
  _campaignIndicator->setProperty("state", state);
  _campaignIndicator->style()->unpolish(_campaignIndicator);
  _campaignIndicator->style()->polish(_campaignIndicator);
  _campaignIndicatorProgress->setRange(0, busy ? 0 : 100);
  _campaignIndicatorProgress->setVisible(busy || state == QStringLiteral("running"));
  if (_campaignTabs && _campaignTabs->count() > 2)
    _campaignTabs->setTabText(2, state == "running" ? QStringLiteral("Acompanhamento · em andamento") : QStringLiteral("Acompanhamento"));
}

bool MainWindow::rememberCampaignStart(const QString& identity) {
  if (!QRegularExpression(QStringLiteral("^[A-Za-z0-9_-]{1,160}$")).match(identity).hasMatch()) return false;
  if (!_campaignRequestedPreflight.isEmpty() && _campaignRequestedPreflight != identity) {
    // A new identity cannot replace a start whose outcome still needs review.
    _campaignStartUncertain = true;
    updateCampaignActions();
    _campaignPoll.start();
    pollCampaign();
    return false;
  }
  QSettings settings;
  settings.setValue(QStringLiteral("campaign/pendingStart"), QJsonDocument(QJsonObject{
      {QStringLiteral("schema_version"), 1}, {QStringLiteral("start_request_id"), identity}
  }).toJson(QJsonDocument::Compact));
  settings.sync();
  if (settings.status() != QSettings::NoError) {
    _campaignStartUncertain = true;
    updateCampaignActions();
    showError(QStringLiteral("Campanha não iniciada"),
        QStringLiteral("Não foi possível salvar o identificador para recuperação. Preserve os dados e revise o armazenamento local."));
    return false;
  }
  _campaignRequestedPreflight = identity;
  return true;
}

void MainWindow::clearCampaignStart() {
  QSettings settings;
  settings.remove(QStringLiteral("campaign/pendingStart"));
  settings.sync();
  _campaignRequestedPreflight.clear();
  _campaignStartUncertain = settings.status() != QSettings::NoError;
}

void MainWindow::lookupCampaignStart() {
  if (_campaignStartLookupInFlight || _campaignRequestedPreflight.isEmpty()) return;
  _campaignStartLookupInFlight = true;
  const auto identity = _campaignRequestedPreflight;
  const int revision = _campaignPollRevision;
  _api.get(QStringLiteral("/api/campaigns/starts/%1").arg(identity),
      [this, identity, revision](bool ok, const QJsonDocument& doc, const QString&) {
    _campaignStartLookupInFlight = false;
    if (revision != _campaignPollRevision || identity != _campaignRequestedPreflight) return;
    const auto result = doc.object();
    const auto status = result.value(QStringLiteral("status")).toString();
    const bool valid = ok && doc.isObject() && result.value(QStringLiteral("ok")) == QJsonValue(true)
        && result.value(QStringLiteral("start_request_id")) == QJsonValue(identity)
        && result.value(QStringLiteral("may_start")) == QJsonValue(false)
        && result.value(QStringLiteral("found")).isBool()
        && QStringList{"admitted", "review", "not_found"}.contains(status);
    const auto execution = result.value(QStringLiteral("execution_state")).toString();
    const auto logName = result.value(QStringLiteral("log_name")).toString();
    const bool resolved = valid && result.value(QStringLiteral("found")) == QJsonValue(true)
        && result.value(QStringLiteral("terminal")) == QJsonValue(true)
        && result.value(QStringLiteral("delivery_confirmed")).isBool()
        && QStringList{"done", "partial", "stopped", "error"}.contains(execution)
        && QRegularExpression(QStringLiteral("^campaign_[A-Za-z0-9_-]+\\.json$")).match(logName).hasMatch();
    if (resolved) {
      clearCampaignStart();
      _pendingHistoryFocus = logName;
      setCampaignIndicator(QStringLiteral("Execução anterior identificada no Histórico"),
          result.value(QStringLiteral("delivery_confirmed")).toBool()
            ? QStringLiteral("O Histórico e os recibos confirmam a entrega anterior.")
            : QStringLiteral("A execução anterior terminou. Consulte o Histórico e a Recuperação para os resultados e pendências."),
          execution == QStringLiteral("error") || execution == QStringLiteral("partial") ? QStringLiteral("error") : QStringLiteral("done"));
      updateCampaignActions();
      if (!_campaignActive) _campaignPoll.stop();
      return;
    }
    const auto detail = !valid ? QStringLiteral("O registro de início não pôde ser confirmado. Preserve os dados locais e consulte a Recuperação.")
      : status == QStringLiteral("admitted") ? QStringLiteral("O serviço confirma a admissão desta solicitação. Consulte o Histórico e a Recuperação para confirmar o resultado dos envios.")
      : status == QStringLiteral("not_found") ? QStringLiteral("Este identificador não consta no registro consultado. A ausência não confirma que nada foi enviado; revise o Histórico e a Recuperação.")
      : QStringLiteral("A solicitação foi registrada e seu resultado exige revisão na Recuperação. Ela não será repetida automaticamente.");
    setCampaignIndicator(QStringLiteral("Início anterior requer revisão"), detail, QStringLiteral("unknown"));
    _campaignCurrent->setText(detail);
    updateCampaignActions();
  });
}

void MainWindow::pollCampaign() {
  if (_campaignResetPending || _campaignPreflightPending || _campaignStartPending || _campaignPollInFlight) return;
  _campaignPollInFlight = true;
  const int revision = _campaignPollRevision;
  _api.get(QStringLiteral("/api/campaigns/current?since=%1").arg(_lastCampaignSeq),
           [this, revision](bool ok, const QJsonDocument& doc, const QString&) {
    _campaignPollInFlight = false;
    if (revision != _campaignPollRevision) return;
    if (!ok || !QStringList{"idle", "running", "stopping", "done", "stopped", "error"}
                   .contains(doc.object().value(QStringLiteral("state")).toString())) {
      _campaignActive = true;
      updateCampaignActions();
      setCampaignIndicator(QStringLiteral("Sem atualização do serviço"),
          QStringLiteral("Não foi possível confirmar o estado atual. Tentando novamente; isso não significa que a campanha parou."), QStringLiteral("unknown"));
      _campaignPoll.start();
      return;
    }
    const auto snap = doc.object();
    const QString state = snap.value(QStringLiteral("state")).toString();
    const bool running = state == QStringLiteral("running") || state == QStringLiteral("stopping");
    _campaignResetRunnerBusy = running;
    const bool requestedOperation = !_campaignRequestedPreflight.isEmpty()
        && snap.value(QStringLiteral("start_request_id")).toString() == _campaignRequestedPreflight;
    if ((_campaignStartUncertain || !_campaignRequestedPreflight.isEmpty()) && !requestedOperation) {
      // A restarted service's idle/foreign snapshot cannot resolve this UUID.
      _campaignStartUncertain = true;
      _campaignActive = running;
      _campaignStop->setEnabled(running && state != QStringLiteral("stopping") && !_campaignStopPending);
      updateCampaignActions();
      setCampaignIndicator(QStringLiteral("Início não confirmado"),
          running ? QStringLiteral("Há outra campanha em execução no serviço. O identificador desta solicitação ainda não foi confirmado. Ela não será repetida automaticamente.")
                  : QStringLiteral("Consultando o registro persistente desta solicitação. Ela não será repetida automaticamente."), QStringLiteral("unknown"));
      lookupCampaignStart();
      _campaignPoll.start();
      return;
    }
    _campaignStartUncertain = false;
    if (requestedOperation && !running) clearCampaignStart();
    _campaignProgress->setRange(0, 100);

    _campaignActive = running;
    _campaignStop->setEnabled(running && state != QStringLiteral("stopping") && !_campaignStopPending);
    updateCampaignActions();
    _campaignStart->setText(running ? QStringLiteral("Campanha em andamento") : QStringLiteral("Iniciar campanha"));
    updateCampaignActions();
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
    const double total = totals.value("progress_target").toDouble(totals.value("total_sends").toDouble());
    const double done = totals.value("progress_completed").toDouble(totals.value("ok_sends").toDouble());
    const bool hoursGoal = totals.value("progress_unit").toString() == "seconds";
    const int successful = totals.value(QStringLiteral("ok_sends")).toInt();
    const int failed = totals.value(QStringLiteral("failed_sends")).toInt();
    const int skipped = totals.value(QStringLiteral("skipped_sends")).toInt();
    const int percent = total > 0 ? qBound(0, int(done * 100 / total), 100) : 0;
    const bool paused = snap.value(QStringLiteral("pause_requested")).toBool();
    const QString headline = state == "running" ? (paused ? QStringLiteral("Pausa solicitada") : QStringLiteral("Campanha em andamento"))
        : state == "done" ? snap.value(QStringLiteral("stage")).toString(stateLabels.value(state))
        : stateLabels.value(state, QStringLiteral("Estado não reconhecido"));
    const QString goalText = hoursGoal
        ? QStringLiteral("%1 de %2 h confirmadas nesta campanha").arg(done / 3600., 0, 'f', 2).arg(total / 3600., 0, 'f', 2)
        : QStringLiteral("%1 de %2 novos envios confirmados").arg(done).arg(total);
    const bool untilExhausted = snap.value("run_until_exhausted").toBool();
    const QString progressText = untilExhausted ? QStringLiteral("%1 envios confirmados · até acabar o conteúdo ou você parar\n").arg(successful)
        : total > 0 ? goalText + QStringLiteral(" · %1%\n").arg(percent) : QString();
    setCampaignIndicator(headline, progressText + current, state, running && !paused && total == 0);
    _campaignIndicatorMetric->setText(untilExhausted ? QStringLiteral("%1 envios confirmados").arg(successful) : total > 0
        ? (hoursGoal ? QStringLiteral("%1 / %2 h · %3%")
              .arg(done / 3600., 0, 'f', 2).arg(total / 3600., 0, 'f', 2).arg(percent)
                     : QStringLiteral("%1 / %2 envios · %3%").arg(done).arg(total).arg(percent))
        : QString());
    if (paused) static_cast<CampaignStatusIcon*>(_campaignIndicatorIcon)->setState(QStringLiteral("paused"), headline);
    else if (state == "done" && headline.contains(QStringLiteral("pendências"), Qt::CaseInsensitive))
      static_cast<CampaignStatusIcon*>(_campaignIndicatorIcon)->setState(QStringLiteral("error"), headline);
    _campaignIndicatorProgress->setValue(percent);
    _campaignProgress->setValue(percent);
    _campaignProgress->setFormat(untilExhausted ? QStringLiteral("%1 envios confirmados · sem meta de horas").arg(successful) : total > 0
        ? hoursGoal ? QStringLiteral("Meta: %1 de %2 h · %p%")
            .arg(done / 3600., 0, 'f', 2).arg(total / 3600., 0, 'f', 2)
            : QStringLiteral("Envios confirmados: %1 de %2 · %p%").arg(successful).arg(total)
        : QStringLiteral("Calculando os envios…"));
    _campaignStats->setText(QStringLiteral("Envios nesta execução: %1 concluídos · %2 ignorados · %3 falhas")
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

void MainWindow::resetCampaignPreviewDisplay() {
  _campaignPreviewPanel->hide();
  _campaignPreviewStage->setText(QStringLiteral("Prévias ainda não consultadas"));
  _campaignPreviewDetail->clear();
  _campaignPreviewStats->clear();
  _campaignReceiptStats->setText(QStringLiteral("Recibos atuais ainda não consultados."));
  _campaignPreviewProgress->setValue(0);
}

void MainWindow::pollCampaignPreviews() {
  if (_campaignResetPending || _campaignActive || _campaignPreflightPending || _campaignStartPending || _previewLogName.isEmpty() || _previewCheckActive) return;
  const auto previewLog = _previewLogName;
  const auto revision = _campaignPollRevision;
  _previewCheckActive = true;
  _campaignPreviewPanel->show();
  _api.post(QStringLiteral("/api/logs/") + encoded(_previewLogName)
                + QStringLiteral("/status"), {},
            [this, previewLog, revision](bool ok, const QJsonDocument& doc, const QString& error) {
    _previewCheckActive = false;
    if (_campaignActive || _campaignStartPending || previewLog != _previewLogName
        || revision != _campaignPollRevision) return;
    if (!ok) {
      if (error.contains(QStringLiteral("log não encontrado"), Qt::CaseInsensitive)) {
        _previewPoll.stop();
        _previewLogName.clear();
        QSettings().remove(QStringLiteral("previewLogName"));
        _campaignPreviewStage->setText(QStringLiteral("Atenção no histórico"));
        _campaignPreviewDetail->setText(QStringLiteral(
            "O registro salvo para acompanhar as prévias não foi encontrado."));
        setStatus(error);
        return;
      }
      _campaignPreviewStage->setText(QStringLiteral("Aguardando o Minute"));
      _campaignPreviewDetail->setText(QStringLiteral(
          "Não foi possível consultar as prévias. A consulta será repetida automaticamente; confira os recibos no Histórico."));
      setStatus(QStringLiteral("Minute ainda não respondeu sobre as prévias: %1").arg(error));
      return;
    }
    const auto summary = doc.object().value(QStringLiteral("summary")).toObject();
    const auto validCount = [](const QJsonValue& value) {
      return value.isDouble() && value.toDouble()>=0 && value.toDouble()<=2147483647.
          && std::floor(value.toDouble())==value.toDouble();
    };
    bool validPreview=true;
    for (const auto* key : {"total", "ready", "pending", "unavailable", "errors", "transient_errors"})
      validPreview=validPreview && validCount(summary.value(QLatin1String(key)));
    if (!validPreview || summary.value("transient_errors").toInt()>summary.value("errors").toInt()
        || qint64(summary.value("ready").toInt())+summary.value("pending").toInt()
            +summary.value("unavailable").toInt()+summary.value("errors").toInt()!=summary.value("total").toInt()) {
      _campaignPreviewStage->setText(QStringLiteral("Consulta de prévias não confirmada"));
      _campaignPreviewDetail->setText(QStringLiteral("A resposta sobre as prévias está incompleta ou inválida. A consulta será repetida; os resultados de envio permanecem no Histórico."));
      setStatus(QStringLiteral("Não foi possível validar o progresso das prévias."));
      return;
    }
    const auto deliveries=doc.object().value("delivery_summary").toObject();
    bool validDelivery=true;
    for (const auto* key : {"confirmed", "pending", "review", "unknown"})
      validDelivery=validDelivery && validCount(deliveries.value(QLatin1String(key)));
    const qint64 deliveryTotal=qint64(deliveries.value("confirmed").toInt())+deliveries.value("pending").toInt()
        +deliveries.value("review").toInt()+deliveries.value("unknown").toInt();
    const bool allConfirmed=validDelivery && deliveryTotal>0 && deliveryTotal==deliveries.value("confirmed").toInt();
    const QString deliveryDetail=validDelivery
        ? QStringLiteral("Recibos atuais: %1 confirmado(s), %2 pendente(s), %3 para revisar, %4 sem recibo atual.")
            .arg(deliveries.value("confirmed").toInt()).arg(deliveries.value("pending").toInt())
            .arg(deliveries.value("review").toInt()).arg(deliveries.value("unknown").toInt())
        : QStringLiteral("Não foi possível validar os recibos atuais nesta consulta. Confira o Histórico.");
    _campaignReceiptStats->setText(deliveryDetail);
    const int total = summary.value(QStringLiteral("total")).toInt();
    const int ready = summary.value(QStringLiteral("ready")).toInt();
    const int pending = summary.value(QStringLiteral("pending")).toInt();
    const int unavailable = summary.value(QStringLiteral("unavailable")).toInt();
    const int errors = summary.value(QStringLiteral("errors")).toInt();
    const int transientErrors = summary.value(QStringLiteral("transient_errors")).toInt();
    const int percent = total > 0 ? qBound(0, int(qint64(ready) * 100 / total), 100) : 0;
    _campaignPreviewProgress->setValue(percent);
    _campaignPreviewProgress->setFormat(total > 0
        ? QStringLiteral("Prévias disponíveis: %1 de %2 · %p%").arg(ready).arg(total)
        : QStringLiteral("Sem prévias para acompanhar"));
    _campaignPreviewStats->setText(QStringLiteral(
        "Prévias: %1 disponíveis · %2 aguardando publicação · %3 indisponíveis · %4 falhas de consulta")
        .arg(ready).arg(pending).arg(unavailable).arg(errors));

    if (total <= 0) {
      _previewPoll.stop();
      _previewLogName.clear();
      QSettings().remove(QStringLiteral("previewLogName"));
      _campaignPreviewStage->setText(QStringLiteral("Sem prévias para acompanhar"));
      _campaignPreviewDetail->setText(QStringLiteral(
          "A consulta não identificou prévias para acompanhar. Confira os resultados de envio no Histórico.")
          );
      setStatus(QStringLiteral("Não há prévias nesta consulta."));
      return;
    }

    if (pending > 0 || transientErrors > 0) {
      _campaignPreviewStage->setText(transientErrors > 0
          ? QStringLiteral("Confirmando no Minute")
          : QStringLiteral("Prévias aguardando publicação"));
      _campaignPreviewDetail->setText((transientErrors > 0
          ? QStringLiteral(
                "%1 de %2 prévias disponíveis. %3 consulta(s) falharam temporariamente e serão repetidas automaticamente.")
                .arg(ready).arg(total).arg(transientErrors)
          : QStringLiteral(
                "%1 de %2 prévias disponíveis. As demais ainda não têm publicação confirmada; confira os recibos separadamente.")
                .arg(ready).arg(total)));
      setStatus(QStringLiteral("Prévias no Minute: %1 disponíveis, %2 aguardando publicação, %3 consultas pendentes.")
                    .arg(ready).arg(pending).arg(transientErrors));
      return;
    }

    _previewPoll.stop();
    _previewLogName.clear();
    QSettings().remove(QStringLiteral("previewLogName"));
    if (unavailable > 0 || errors > 0) {
      _campaignPreviewStage->setText(QStringLiteral("Atenção nas prévias"));
      _campaignPreviewDetail->setText(QStringLiteral(
          "%1 prévia(s) pronta(s); %2 precisam de atenção. Veja os detalhes no Histórico.")
          .arg(ready).arg(unavailable + errors));
      _campaignFeed->appendPlainText(QStringLiteral(
          "!   Minute concluiu a fila com %1 prévia(s) que precisam de atenção.")
          .arg(unavailable + errors));
    } else {
      const QString title=allConfirmed?QStringLiteral("Prévias prontas"):
          QStringLiteral("Prévias prontas · envios com pendências");
      _campaignPreviewStage->setText(title);
      _campaignPreviewDetail->setText(QStringLiteral(
          "Todas as %1 prévias desta consulta estão disponíveis no Minute. A meta e os resultados da execução permanecem acima.").arg(ready));
      _campaignFeed->appendPlainText(QStringLiteral(
          "✓   Minute publicou todas as %1 prévias.").arg(ready));
      setStatus(title+QStringLiteral(". Confira o resultado da tentativa original e os recibos atuais no Histórico."));
    }
  });
}

void MainWindow::openEgoLibrary() {
  const auto validSummary = [](const QJsonObject& root) {
    if (root.value(QStringLiteral("state")).toString() != QStringLiteral("ready")
        || !root.value(QStringLiteral("needs_index")).isBool()
        || root.value(QStringLiteral("needs_index")).toBool()) return false;
    for (const auto* key : {"videos", "clips", "annotations"}) {
      const auto value = root.value(QLatin1String(key));
      if (!value.isDouble() || value.toDouble() < 0
          || std::floor(value.toDouble()) != value.toDouble()) return false;
    }
    return true;
  };
  auto* dialog = new QDialog(this);
  dialog->setAttribute(Qt::WA_DeleteOnClose);
  dialog->setObjectName(QStringLiteral("egoLibraryDialog"));
  dialog->setWindowTitle(QStringLiteral("Catálogo de origem Ego4D"));
  dialog->resize(1000, 650);
  auto* layout = new QVBoxLayout(dialog);
  auto* summary = new QLabel(QStringLiteral("Consultando catálogo…"));
  summary->setWordWrap(true);
  summary->setObjectName(QStringLiteral("egoLibrarySummary"));
  layout->addWidget(summary);
  auto* controls = new QHBoxLayout;
  auto* query = new QLineEdit;
  query->setObjectName(QStringLiteral("egoLibraryQuery"));
  query->setPlaceholderText(QStringLiteral("Atividade ou cenário, ex.: gardening"));
  query->setMaxLength(200);
  controls->addWidget(query, 1);
  auto* minimum = new QSpinBox;
  minimum->setRange(0, 1440);
  minimum->setSuffix(QStringLiteral(" min ou mais"));
  minimum->setToolTip(QStringLiteral("Duração do vídeo original"));
  controls->addWidget(minimum);
  auto* sensors = new QCheckBox(QStringLiteral("IMU declarada"));
  controls->addWidget(sensors);
  auto* find = new QPushButton(QStringLiteral("Buscar"));
  find->setObjectName(QStringLiteral("egoLibrarySearch"));
  find->setEnabled(false);
  controls->addWidget(find);
  auto* index = new QPushButton(QStringLiteral("Atualizar índice"));
  index->setObjectName(QStringLiteral("egoLibraryIndex"));
  controls->addWidget(index);
  layout->addLayout(controls);
  auto* progress = new QProgressBar;
  progress->setRange(0, 0);
  progress->setVisible(false);
  layout->addWidget(progress);
  auto* table = new QTableWidget(0, 5);
  table->setObjectName(QStringLiteral("egoLibraryTable"));
  table->setHorizontalHeaderLabels({QStringLiteral("Vídeo de origem"), QStringLiteral("Duração"),
      QStringLiteral("Cenários"), QStringLiteral("Dispositivo original"), QStringLiteral("Sensores")});
  table->setEditTriggers(QAbstractItemView::NoEditTriggers);
  table->setSelectionBehavior(QAbstractItemView::SelectRows);
  table->setSelectionMode(QAbstractItemView::SingleSelection);
  table->horizontalHeader()->setSectionResizeMode(QHeaderView::ResizeToContents);
  table->horizontalHeader()->setSectionResizeMode(2, QHeaderView::Stretch);
  table->verticalHeader()->hide();
  layout->addWidget(table, 1);
  auto* footer = new QHBoxLayout;
  auto* count = new QLabel;
  count->setObjectName(QStringLiteral("egoLibraryCount"));
  footer->addWidget(count, 1);
  auto* previous = new QPushButton(QStringLiteral("Anterior"));
  previous->setEnabled(false);
  auto* next = new QPushButton(QStringLiteral("Próxima"));
  next->setEnabled(false);
  footer->addWidget(previous);
  footer->addWidget(next);
  auto* copy = new QPushButton(QStringLiteral("Copiar ID"));
  footer->addWidget(copy);
  connect(copy, &QPushButton::clicked, dialog, [table] {
    if (table->currentRow() >= 0 && table->item(table->currentRow(), 0))
      QApplication::clipboard()->setText(table->item(table->currentRow(), 0)->data(Qt::UserRole).toString());
  });
  auto* close = new QPushButton(QStringLiteral("Fechar"));
  footer->addWidget(close);
  connect(close, &QPushButton::clicked, dialog, &QDialog::close);
  layout->addLayout(footer);
  layout->addWidget(quietLabel(QStringLiteral("Origem: Ego4D. CSV presente ou IMU declarada não garantem continuidade dos sensores nem elegibilidade para campanha.")));
  const QPointer<QDialog> guard(dialog);
  dialog->setProperty("offset", 0);
  dialog->setProperty("generation", 0);
  dialog->setProperty("indexReady", false);
  auto browse = [this, guard, query, minimum, sensors, table, find, previous, next, count] {
    if (!guard) return;
    const int generation = guard->property("generation").toInt() + 1;
    guard->setProperty("generation", generation);
    QUrlQuery params;
    params.addQueryItem(QStringLiteral("q"), query->text());
    params.addQueryItem(QStringLiteral("min_s"), QString::number(minimum->value() * 60));
    params.addQueryItem(QStringLiteral("imu"), sensors->isChecked() ? QStringLiteral("declared") : QStringLiteral("all"));
    params.addQueryItem(QStringLiteral("offset"), QString::number(guard->property("offset").toInt()));
    find->setEnabled(false); previous->setEnabled(false); next->setEnabled(false);
    count->setText(QStringLiteral("Buscando…"));
    _api.get(QStringLiteral("/api/library/ego4d/videos?") + params.toString(QUrl::FullyEncoded),
      [guard, generation, table, find, previous, next, count](bool ok, const QJsonDocument& doc, const QString& error) {
        if (!guard || generation != guard->property("generation").toInt()) return;
        find->setEnabled(true);
        if (!ok) { count->setText(error); table->setRowCount(0); return; }
        const auto root = doc.object();
        const auto totalValue = root.value(QStringLiteral("total"));
        const auto offsetValue = root.value(QStringLiteral("offset"));
        if (!root.value(QStringLiteral("items")).isArray()
            || root.value(QStringLiteral("provenance")).toString() != QStringLiteral("ego4d_original")
            || !totalValue.isDouble() || totalValue.toDouble() < 0
            || totalValue.toDouble() != std::floor(totalValue.toDouble())
            || !offsetValue.isDouble() || offsetValue.toInt(-1) != guard->property("offset").toInt()) {
          count->setText(QStringLiteral("Resposta incompleta da biblioteca. Tente buscar novamente."));
          table->setRowCount(0); return;
        }
        const auto items = root.value(QStringLiteral("items")).toArray();
        bool validItems = items.size() <= 100 && items.size() <= totalValue.toDouble();
        for (const auto& value : items) {
          const auto item = value.toObject();
          const auto duration = item.value(QStringLiteral("duration_s"));
          const auto declared = item.value(QStringLiteral("has_imu"));
          validItems = validItems && value.isObject()
            && !item.value(QStringLiteral("uid")).toString().isEmpty()
            && duration.isDouble() && duration.toDouble() > 0 && std::isfinite(duration.toDouble())
            && item.value(QStringLiteral("imu_local")).isBool()
            && (declared.isNull() || (declared.isDouble()
                && (declared.toDouble() == 0 || declared.toDouble() == 1)));
        }
        if (!validItems) {
          count->setText(QStringLiteral("Dados incompletos da biblioteca. Tente buscar novamente."));
          table->setRowCount(0); return;
        }
        table->setRowCount(items.size());
        for (int row = 0; row < items.size(); ++row) {
          const auto item = items.at(row).toObject();
          const QString uid = item.value(QStringLiteral("uid")).toString();
          auto* identity = new QTableWidgetItem(uid);
          identity->setData(Qt::UserRole, uid);
          identity->setToolTip(uid);
          table->setItem(row, 0, identity);
          const int seconds = qRound(item.value(QStringLiteral("duration_s")).toDouble());
          table->setItem(row, 1, new QTableWidgetItem(QStringLiteral("%1:%2").arg(seconds / 60).arg(seconds % 60, 2, 10, QChar('0'))));
          table->item(row, 1)->setToolTip(QStringLiteral("Duração original: %1 segundos").arg(item.value(QStringLiteral("duration_s")).toDouble(), 0, 'f', 3));
          table->setItem(row, 2, new QTableWidgetItem(item.value(QStringLiteral("scenarios")).toString()));
          table->setItem(row, 3, new QTableWidgetItem(item.value(QStringLiteral("device")).toString()));
          const auto declared = item.value(QStringLiteral("has_imu"));
          const QString state = item.value(QStringLiteral("imu_local")).toBool() ? QStringLiteral("CSV local · verificar")
            : declared.isNull() ? QStringLiteral("Sem informação")
            : declared.toInt() == 1 ? QStringLiteral("Declarado · sem CSV local") : QStringLiteral("Não declarado");
          table->setItem(row, 4, new QTableWidgetItem(state));
        }
        const int offset = root.value(QStringLiteral("offset")).toInt();
        const int total = root.value(QStringLiteral("total")).toInt();
        count->setText(items.isEmpty() ? QStringLiteral("Nenhum vídeo encontrado")
          : QStringLiteral("%1–%2 de %3 vídeos de origem").arg(offset + 1).arg(offset + items.size()).arg(total));
        previous->setEnabled(offset > 0);
        next->setEnabled(offset + items.size() < total);
      });
  };
  connect(find, &QPushButton::clicked, dialog, [guard, browse] { if (guard) { guard->setProperty("offset", 0); browse(); } });
  connect(query, &QLineEdit::returnPressed, find, &QPushButton::click);
  auto filterChanged = [guard, find, previous, next, count] {
    if (!guard) return;
    guard->setProperty("generation", guard->property("generation").toInt() + 1);
    guard->setProperty("offset", 0);
    previous->setEnabled(false); next->setEnabled(false);
    find->setEnabled(guard->property("indexReady").toBool());
    count->setText(QStringLiteral("Buscar para aplicar os filtros"));
  };
  connect(query, &QLineEdit::textChanged, dialog, filterChanged);
  connect(minimum, qOverload<int>(&QSpinBox::valueChanged), dialog, filterChanged);
  connect(sensors, &QCheckBox::toggled, dialog, filterChanged);
  connect(previous, &QPushButton::clicked, dialog, [guard, browse] { if (guard) { guard->setProperty("offset", qMax(0, guard->property("offset").toInt() - 50)); browse(); } });
  connect(next, &QPushButton::clicked, dialog, [guard, browse] { if (guard) { guard->setProperty("offset", guard->property("offset").toInt() + 50); browse(); } });
  auto* poll = new QTimer(dialog);
  poll->setInterval(1200);
  auto update = [this, guard, summary, index, progress, poll, browse, validSummary] {
    if (!guard) return;
    if (QDateTime::currentMSecsSinceEpoch() - guard->property("indexStarted").toLongLong() > 180000) {
      poll->stop(); index->setEnabled(true); progress->hide();
      guard->setProperty("indexGeneration", guard->property("indexGeneration").toInt() + 1);
      guard->setProperty("indexInFlight", false);
      summary->setText(QStringLiteral("Indexação demorou mais que o esperado. Consulte novamente em instantes."));
      return;
    }
    if (guard->property("indexInFlight").toBool()) return;
    guard->setProperty("indexInFlight", true);
    const int indexGeneration = guard->property("indexGeneration").toInt();
    _api.post(QStringLiteral("/api/library/ego4d/index"), {},
      [guard, summary, index, progress, poll, browse, validSummary, indexGeneration](bool ok, const QJsonDocument& doc, const QString& error) {
        if (!guard || indexGeneration != guard->property("indexGeneration").toInt()) return;
        guard->setProperty("indexInFlight", false);
        const auto root = doc.object();
        if (!ok || !root.value(QStringLiteral("loading")).toBool()) {
          poll->stop(); index->setEnabled(true); progress->hide();
          if (!ok) { summary->setText(error); return; }
          if (!validSummary(root)) {
            summary->setText(QStringLiteral("Resposta incompleta da indexação. Tente atualizar novamente."));
            return;
          }
          guard->setProperty("indexReady", true);
          guard->setProperty("offset", 0);
          summary->setText(QStringLiteral("%1 vídeos · %2 clipes · %3 anotações originais")
            .arg(root.value(QStringLiteral("videos")).toInt()).arg(root.value(QStringLiteral("clips")).toInt())
            .arg(root.value(QStringLiteral("annotations")).toInt()));
          browse();
        } else summary->setText(root.value(QStringLiteral("message")).toString());
      });
  };
  connect(poll, &QTimer::timeout, dialog, update);
  connect(index, &QPushButton::clicked, dialog, [guard, index, progress, poll, update] {
    if (!guard) return;
    guard->setProperty("indexStarted", QDateTime::currentMSecsSinceEpoch());
    guard->setProperty("indexGeneration", guard->property("indexGeneration").toInt() + 1);
    index->setEnabled(false); progress->show(); poll->start(); update();
  });
  _api.get(QStringLiteral("/api/library/ego4d"), [guard, summary, browse, validSummary](bool ok, const QJsonDocument& doc, const QString& error) {
    if (!guard) return;
    if (guard->property("indexStarted").isValid()) return;
    if (!ok) { summary->setText(error); return; }
    const auto root = doc.object();
    if (root.value(QStringLiteral("needs_index")).toBool()) {
      summary->setText(QStringLiteral("Atualize o índice para explorar o catálogo original."));
      return;
    }
    if (!validSummary(root)) {
      summary->setText(QStringLiteral("Resposta incompleta da biblioteca. Atualize o índice."));
      return;
    }
    guard->setProperty("indexReady", true);
    summary->setText(QStringLiteral("%1 vídeos · %2 clipes · %3 anotações originais")
      .arg(root.value(QStringLiteral("videos")).toInt()).arg(root.value(QStringLiteral("clips")).toInt())
      .arg(root.value(QStringLiteral("annotations")).toInt()));
    browse();
  });
  dialog->show();
}

void MainWindow::localMediaFiltersChanged() {
  _localMediaOffset = 0;
  _localMediaRefreshNonce.clear();
  _localMediaTable->setRowCount(0);
  loadLocalMediaLibrary();
}

QString MainWindow::localMediaPath(bool videoOnly) const {
  if (!_localMediaVerified || !_localMediaTable || _localMediaRoot.isEmpty()) return {};
  const auto* selected = _localMediaTable->item(_localMediaTable->currentRow(), 0);
  if (!selected) return {};
  const auto item = selected->data(Qt::UserRole).toJsonObject();
  const QFileInfo candidate(item.value(QStringLiteral("path")).toString());
  const QString root = QFileInfo(_localMediaRoot).canonicalFilePath();
  const QString actual = candidate.canonicalFilePath();
  if (root.isEmpty() || actual.isEmpty() || !candidate.isAbsolute() || !candidate.isFile()) return {};
  const QString relative = QDir(root).relativeFilePath(actual);
  if (relative == QStringLiteral("..") || relative.startsWith(QStringLiteral("../")) || QDir::isAbsolutePath(relative)) return {};
  const QSet<QString> videoSuffixes{QStringLiteral("mp4"), QStringLiteral("mkv"), QStringLiteral("mov"),
      QStringLiteral("avi"), QStringLiteral("webm"), QStringLiteral("m4v")};
  if (videoOnly && (item.value(QStringLiteral("kind")).toString() != QStringLiteral("video")
      || !videoSuffixes.contains(candidate.suffix().toLower()))) return {};
  return actual;
}

QWidget* MainWindow::buildNymeriaLibraryTab() {
  auto* tab = new QWidget;
  auto* layout = new QVBoxLayout(tab);
  layout->setContentsMargins(0, 12, 0, 0);
  layout->setSpacing(12);
  _nymeriaSummary = quietLabel(QStringLiteral("Importe o manifesto Nymeria e sincronize as anotações para ampliar o acervo local."));
  _nymeriaSummary->setObjectName(QStringLiteral("nymeriaLibrarySummary"));
  _nymeriaSummary->setWordWrap(true);
  layout->addWidget(_nymeriaSummary);
  auto* sources = new QHBoxLayout;
  _nymeriaImport = new QPushButton(QStringLiteral("Importar manifesto JSON…"));
  _nymeriaImport->setObjectName(QStringLiteral("nymeriaImportManifest"));
  _nymeriaSync = new QPushButton(QStringLiteral("Sincronizar catálogo"));
  _nymeriaSync->setObjectName(QStringLiteral("nymeriaSyncCatalog"));
  _nymeriaRefresh = new QPushButton(QStringLiteral("Atualizar acervo"));
  _nymeriaRefresh->setObjectName(QStringLiteral("nymeriaRefresh"));
  sources->addWidget(_nymeriaImport); sources->addWidget(_nymeriaSync); sources->addWidget(_nymeriaRefresh);
  sources->addStretch(); layout->addLayout(sources);
  connect(_nymeriaImport, &QPushButton::clicked, this, [this] {
    const QString path = QFileDialog::getOpenFileName(this, QStringLiteral("Importar links Nymeria"),
        {}, QStringLiteral("Manifesto JSON (*.json)"));
    if (!path.isEmpty()) importNymeriaManifest(path);
  });
  connect(_nymeriaSync, &QPushButton::clicked, this, &MainWindow::syncNymeriaCatalog);
  connect(_nymeriaRefresh, &QPushButton::clicked, this, [this] { loadNymeriaLibrary(); pollNymeriaJob(); });
  layout->addWidget(quietLabel(QStringLiteral("Preparação manual opcional · categorias")));
  _nymeriaTasks = new QListWidget;
  _nymeriaTasks->setObjectName(QStringLiteral("nymeriaTaskSelection"));
  _nymeriaTasks->setMinimumHeight(100); _nymeriaTasks->setMaximumHeight(150);
  layout->addWidget(_nymeriaTasks);
  connect(_nymeriaTasks, &QListWidget::itemChanged, this, [this] { invalidateNymeriaPlan(); });
  auto* parameters = new QHBoxLayout;
  parameters->addWidget(quietLabel(QStringLiteral("Meta por conta")));
  _nymeriaTargetHours = new QDoubleSpinBox;
  _nymeriaTargetHours->setObjectName(QStringLiteral("nymeriaTargetHours"));
  _nymeriaTargetHours->setRange(0.25, 12); _nymeriaTargetHours->setValue(8);
  _nymeriaTargetHours->setSuffix(QStringLiteral(" h")); _nymeriaTargetHours->setKeyboardTracking(false);
  parameters->addWidget(_nymeriaTargetHours);
  parameters->addWidget(quietLabel(QStringLiteral("Recortes")));
  _nymeriaMinimum = new QSpinBox; _nymeriaMaximum = new QSpinBox;
  _nymeriaMinimum->setObjectName(QStringLiteral("nymeriaMinimumDuration"));
  _nymeriaMaximum->setObjectName(QStringLiteral("nymeriaMaximumDuration"));
  for (auto* spin : {_nymeriaMinimum, _nymeriaMaximum}) {
    spin->setRange(1, 30); spin->setSuffix(QStringLiteral(" min")); spin->setKeyboardTracking(false);
  }
  _nymeriaMinimum->setValue(5); _nymeriaMaximum->setValue(30);
  parameters->addWidget(_nymeriaMinimum); parameters->addWidget(quietLabel(QStringLiteral("até"))); parameters->addWidget(_nymeriaMaximum);
  parameters->addStretch(); layout->addLayout(parameters);
  auto* preparation = new QHBoxLayout;
  preparation->addWidget(quietLabel(QStringLiteral("Manter livre")));
  _nymeriaReserve = new QSpinBox;
  _nymeriaReserve->setObjectName(QStringLiteral("nymeriaDiskReserve"));
  _nymeriaReserve->setRange(5, 1000); _nymeriaReserve->setValue(50); _nymeriaReserve->setSuffix(QStringLiteral(" GiB"));
  preparation->addWidget(_nymeriaReserve); preparation->addStretch();
  _nymeriaPlanButton = new QPushButton(QStringLiteral("Estimar conteúdo"));
  _nymeriaPlanButton->setObjectName(QStringLiteral("nymeriaPlan"));
  _nymeriaAcquire = new QPushButton(QStringLiteral("Baixar plano antecipadamente"));
  _nymeriaAcquire->setObjectName(QStringLiteral("nymeriaAcquirePlan"));
  preparation->addWidget(_nymeriaPlanButton); preparation->addWidget(_nymeriaAcquire); layout->addLayout(preparation);
  connect(_nymeriaTargetHours, qOverload<double>(&QDoubleSpinBox::valueChanged), this, [this] { invalidateNymeriaPlan(); });
  for (auto* spin : {_nymeriaMinimum, _nymeriaMaximum, _nymeriaReserve})
    connect(spin, qOverload<int>(&QSpinBox::valueChanged), this, [this] { invalidateNymeriaPlan(); });
  connect(_nymeriaPlanButton, &QPushButton::clicked, this, &MainWindow::planNymeriaLibrary);
  connect(_nymeriaAcquire, &QPushButton::clicked, this, &MainWindow::acquireNymeriaPlan);
  _nymeriaPotential = quietLabel(QStringLiteral("As anotações estimam o conteúdo. Na campanha, cada fonte é baixada e seu vídeo e IMU são medidos antes do envio. O download antecipado é opcional."));
  _nymeriaPotential->setObjectName(QStringLiteral("nymeriaPlanSummary")); _nymeriaPotential->setWordWrap(true);
  layout->addWidget(_nymeriaPotential);
  auto* campaignOrigin = new QHBoxLayout;
  auto* originHelp = quietLabel(QStringLiteral("Escolha Ambos em Nova campanha para adquirir Ego4D e Nymeria sob demanda, preparar, enviar e liberar cada recorte confirmado."));
  originHelp->setWordWrap(true); campaignOrigin->addWidget(originHelp, 1);
  _nymeriaUseCombined = new QPushButton(QStringLiteral("Usar Ego4D + Nymeria"));
  _nymeriaUseCombined->setObjectName(QStringLiteral("nymeriaUseCombined"));
  campaignOrigin->addWidget(_nymeriaUseCombined); layout->addLayout(campaignOrigin);
  connect(_nymeriaUseCombined, &QPushButton::clicked, this, [this] {
    if (_campaignActive || _campaignStartPending || _campaignPreflightPending || _campaignStartUncertain) return;
    const int index = _dataset->findData(QStringLiteral("ambos"));
    if (index >= 0) _dataset->setCurrentIndex(index);
    selectAllCompatibleTasks();
    _navigation->setCurrentRow(3);
  });
  auto* progress = new QHBoxLayout;
  _nymeriaState = quietLabel(QStringLiteral("Nenhuma preparação em andamento"));
  _nymeriaState->setObjectName(QStringLiteral("nymeriaJobState")); _nymeriaState->setWordWrap(true);
  progress->addWidget(_nymeriaState, 1);
  _nymeriaStop = new QPushButton(QStringLiteral("Parar com segurança"));
  _nymeriaStop->setObjectName(QStringLiteral("nymeriaStop"));
  progress->addWidget(_nymeriaStop); layout->addLayout(progress);
  connect(_nymeriaStop, &QPushButton::clicked, this, &MainWindow::stopNymeriaJob);
  _nymeriaProgress = new QProgressBar;
  _nymeriaProgress->setObjectName(QStringLiteral("nymeriaJobProgress")); _nymeriaProgress->setRange(0, 100); _nymeriaProgress->setValue(0);
  layout->addWidget(_nymeriaProgress);
  auto* filters = new QHBoxLayout;
  _nymeriaQuery = new QLineEdit;
  _nymeriaQuery->setObjectName(QStringLiteral("nymeriaLibraryQuery")); _nymeriaQuery->setPlaceholderText(QStringLiteral("Buscar sequência ou atividade")); _nymeriaQuery->setMaxLength(200);
  _nymeriaFilter = new ComboBox;
  _nymeriaFilter->setObjectName(QStringLiteral("nymeriaLibraryFilter"));
  for (const auto& pair : QList<QPair<QString, QString>>{{"Todos", "all"}, {"Catalogadas", "cataloged"},
      {"Fontes baixadas", "downloaded"}, {"Vídeo e IMU medidos", "measured"}, {"Parciais", "partial"},
      {"Ainda não baixadas", "missing"}, {"Sem anotações", "no_annotations"}})
    _nymeriaFilter->addItem(pair.first, pair.second);
  filters->addWidget(_nymeriaQuery, 1); filters->addWidget(_nymeriaFilter); layout->addLayout(filters);
  const auto filterChanged = [this] {
    ++_nymeriaRequestId; _nymeriaInventoryPending = false;
    _nymeriaOffset = 0; loadNymeriaLibrary();
  };
  connect(_nymeriaQuery, &QLineEdit::returnPressed, this, filterChanged);
  connect(_nymeriaFilter, qOverload<int>(&QComboBox::currentIndexChanged), this, filterChanged);
  _nymeriaTable = new QTableWidget(0, 5);
  _nymeriaTable->setObjectName(QStringLiteral("nymeriaLibraryTable"));
  _nymeriaTable->setHorizontalHeaderLabels({QStringLiteral("Sequência"), QStringLiteral("Atividade"), QStringLiteral("Duração declarada"), QStringLiteral("Estado local"), QStringLiteral("Download restante")});
  _nymeriaTable->setEditTriggers(QAbstractItemView::NoEditTriggers); _nymeriaTable->setSelectionBehavior(QAbstractItemView::SelectRows);
  _nymeriaTable->setMinimumHeight(240); _nymeriaTable->setMaximumHeight(420); _nymeriaTable->verticalHeader()->hide();
  _nymeriaTable->horizontalHeader()->setSectionResizeMode(0, QHeaderView::ResizeToContents);
  _nymeriaTable->horizontalHeader()->setSectionResizeMode(1, QHeaderView::Stretch);
  for (int column=2; column<5; ++column) _nymeriaTable->horizontalHeader()->setSectionResizeMode(column, QHeaderView::ResizeToContents);
  layout->addWidget(_nymeriaTable);
  auto* paging = new QHBoxLayout;
  _nymeriaCount = quietLabel(QStringLiteral("Aguardando catálogo")); _nymeriaCount->setObjectName(QStringLiteral("nymeriaLibraryCount"));
  paging->addWidget(_nymeriaCount, 1);
  _nymeriaPrevious = new QPushButton(QStringLiteral("Anterior")); _nymeriaNext = new QPushButton(QStringLiteral("Próxima"));
  _nymeriaPrevious->setObjectName(QStringLiteral("nymeriaPrevious")); _nymeriaNext->setObjectName(QStringLiteral("nymeriaNext"));
  paging->addWidget(_nymeriaPrevious); paging->addWidget(_nymeriaNext); layout->addLayout(paging);
  connect(_nymeriaPrevious, &QPushButton::clicked, this, [this] { _nymeriaOffset = qMax(0, _nymeriaOffset - 50); loadNymeriaLibrary(); });
  connect(_nymeriaNext, &QPushButton::clicked, this, [this] { _nymeriaOffset += 50; loadNymeriaLibrary(); });
  _nymeriaPoll.setInterval(1200);
  connect(&_nymeriaPoll, &QTimer::timeout, this, &MainWindow::pollNymeriaJob);
  updateNymeriaLibraryActions();
  return tab;
}

void MainWindow::updateNymeriaLibraryActions() {
  if (!_nymeriaImport) return;
  const bool ready = _backendReady && !_closing && !_campaignClosePending;
  const bool idle = ready && !_nymeriaCommandPending && !_nymeriaRunning && !_nymeriaPlanPending;
  bool hasTask = false;
  for (int i=0; i<_nymeriaTasks->count(); ++i) hasTask |= _nymeriaTasks->item(i)->checkState()==Qt::Checked;
  _nymeriaImport->setEnabled(idle); _nymeriaSync->setEnabled(idle);
  _nymeriaRefresh->setEnabled(ready && !_nymeriaInventoryPending);
  _nymeriaPlanButton->setEnabled(idle && hasTask && _nymeriaMinimum->value()<=_nymeriaMaximum->value());
  const bool spaceComplete = _nymeriaPlan.value("space_check_complete")==QJsonValue(true);
  const bool spaceAllowed = spaceComplete ? _nymeriaPlan.value("fits_available_disk")==QJsonValue(true)
      : _nymeriaPlan.value("fits_source_downloads")==QJsonValue(true);
  _nymeriaAcquire->setText(!_nymeriaPlan.isEmpty() && !spaceComplete
      ? QStringLiteral("Verificar espaço e baixar antecipadamente") : QStringLiteral("Baixar plano antecipadamente"));
  _nymeriaAcquire->setEnabled(idle && !_nymeriaPlan.value("selected_seq_ids").toArray().isEmpty() && spaceAllowed
      && _nymeriaPlan.value("disk_free_bytes").toDouble() >= _nymeriaPlan.value("download_bytes").toDouble()
          + double(_nymeriaReserve->value()) * 1024 * 1024 * 1024);
  _nymeriaStop->setEnabled(ready && _nymeriaRunning && !_nymeriaCommandPending);
  _nymeriaPrevious->setEnabled(ready && !_nymeriaInventoryPending && _nymeriaOffset>0);
  _nymeriaNext->setEnabled(ready && !_nymeriaInventoryPending && _nymeriaOffset+50<_nymeriaTotal);
  _nymeriaUseCombined->setEnabled(ready && !_campaignActive && !_campaignStartPending
      && !_campaignPreflightPending && !_campaignStartUncertain);
}

void MainWindow::invalidateNymeriaPlan() {
  ++_nymeriaPlanRevision;
  _nymeriaPlanPending = false;
  _nymeriaPlan = {};
  _nymeriaPotential->setText(QStringLiteral("Planeje o conteúdo para as categorias e a duração escolhidas. O vídeo e o IMU serão medidos após o download."));
  updateNymeriaLibraryActions();
}

void MainWindow::loadNymeriaLibrary() {
  if (!_backendReady || _closing || _nymeriaInventoryPending) return;
  _nymeriaInventoryPending = true;
  const quint64 revision = ++_nymeriaRequestId;
  const quint64 jobRevision = _nymeriaJobRevision;
  const int generation = _operationBackendGeneration;
  QUrlQuery query;
  query.addQueryItem("q", _nymeriaQuery->text().trimmed()); query.addQueryItem("state", _nymeriaFilter->currentData().toString());
  query.addQueryItem("limit", "50"); query.addQueryItem("offset", QString::number(_nymeriaOffset));
  updateNymeriaLibraryActions();
  _api.get(QStringLiteral("/api/library/nymeria/sequences?")+query.toString(QUrl::FullyEncoded),
      [this, revision, jobRevision, generation](bool ok, const QJsonDocument& doc, const QString& error) {
    if (_closing || revision!=_nymeriaRequestId || generation!=_operationBackendGeneration) return;
    _nymeriaInventoryPending = false;
    if (ok && doc.object().value("loading")==QJsonValue(true)) {
      _nymeriaSummary->setText(doc.object().value("message").toString(QStringLiteral("Lendo catálogo Nymeria…")));
      QTimer::singleShot(1200, this, [this, revision] {
        if (revision==_nymeriaRequestId && _pages->currentIndex()==4 && _libraryTabs->currentIndex()==2) loadNymeriaLibrary();
      });
    } else if (!ok) _nymeriaSummary->setText(QStringLiteral("Leitura indisponível · dados anteriores preservados. ")+error);
    else {
      renderNymeriaLibrary(doc.object());
      if (jobRevision==_nymeriaJobRevision && !_nymeriaCommandPending && doc.object().value("worker").isObject())
        renderNymeriaJob(doc.object().value("worker").toObject());
    }
    updateNymeriaLibraryActions();
  });
}

void MainWindow::renderNymeriaLibrary(const QJsonObject& result) {
  const auto summary = result.value("summary").toObject();
  if (!result.value("items").isArray() || !result.value("task_names").isArray()
      || !result.value("total").isDouble() || result.value("offset").toInt(-1)!=_nymeriaOffset
      || !summary.value("sequence_count").isDouble()) {
    _nymeriaSummary->setText(QStringLiteral("Resposta do catálogo inválida · dados anteriores preservados.")); return;
  }
  _nymeriaTotal = result.value("total").toInt();
  const auto counts = summary.value("by_state").toObject();
  _nymeriaSummary->setText(QStringLiteral("%1 sequências · %2 h declaradas na origem · %3 ações anotadas\n%4 catalogadas · %5 com fontes baixadas · %6 medidas · %7 livres no disco")
      .arg(summary.value("sequence_count").toInt()).arg(summary.value("declared_hours").toDouble(),0,'f',2)
      .arg(summary.value("atomic_action_count").toInt()).arg(counts.value("cataloged").toInt())
      .arg(counts.value("downloaded").toInt()).arg(counts.value("measured").toInt())
      .arg(bytesText(qint64(summary.value("disk_free_bytes").toDouble()))));
  const QString setupMessage = result.value("setup").toObject().value("message").toString();
  if (!setupMessage.isEmpty())
    _nymeriaSummary->setText(_nymeriaSummary->text() + QStringLiteral("\n") + setupMessage);
  QSet<QString> checked;
  for (int i=0; i<_nymeriaTasks->count(); ++i)
    if (_nymeriaTasks->item(i)->checkState()==Qt::Checked) checked.insert(_nymeriaTasks->item(i)->data(Qt::UserRole).toString());
  const bool firstTasks = _nymeriaTasks->count()==0;
  if (firstTasks && !_campaignTaskSelectionTouched)
    for (const auto& value : result.value("task_names").toArray()) checked.insert(value.toString());
  if (firstTasks) for (const auto& value : _taskRecords) {
    const auto task = value.toObject();
    if (_campaignSelectedTaskIds.contains(task.value("id").toString())) checked.insert(task.value("name").toString());
  }
  {
    const QSignalBlocker blocker(_nymeriaTasks);
    _nymeriaTasks->clear();
    for (const auto& value : result.value("task_names").toArray()) {
      const QString name = value.toString();
      if (name.trimmed().isEmpty()) continue;
      auto* item = new QListWidgetItem(name, _nymeriaTasks); item->setData(Qt::UserRole,name);
      item->setCheckState(checked.contains(name)?Qt::Checked:Qt::Unchecked);
    }
  }
  const auto items = result.value("items").toArray();
  _nymeriaTable->setRowCount(items.size());
  const QHash<QString,QString> states{{"missing","Ainda não baixada"},{"cataloged","Anotações disponíveis"},
      {"downloaded","Fontes baixadas"},{"measured","Vídeo e IMU medidos"},
      {"partial","Download parcial"},{"no_annotations","Sem anotações"}};
  for (int i=0; i<items.size(); ++i) {
    const auto row = items[i].toObject();
    _nymeriaTable->setItem(i,0,cell(row.value("seq_id").toString()));
    _nymeriaTable->setItem(i,1,cell(row.value("script").toString()));
    _nymeriaTable->setItem(i,2,cell(row.value("declared_duration_s").isDouble()
        ? QStringLiteral("%1 min").arg(row.value("declared_duration_s").toDouble()/60,0,'f',1) : QStringLiteral("—")));
    _nymeriaTable->setItem(i,3,cell(states.value(row.value("state").toString(),QStringLiteral("Revisar"))));
    _nymeriaTable->setItem(i,4,cell(bytesText(qint64(row.value("download_bytes").toDouble()))));
  }
  _nymeriaCount->setText(QStringLiteral("%1–%2 de %3 sequências").arg(items.isEmpty()?0:_nymeriaOffset+1)
      .arg(_nymeriaOffset+items.size()).arg(_nymeriaTotal));
}

void MainWindow::startNymeriaCommand(const QString& path, const QJsonObject& body) {
  if (!_backendReady || _closing || _nymeriaCommandPending || _nymeriaRunning || _nymeriaPlanPending) return;
  _nymeriaCommandPending = true;
  const quint64 revision = ++_nymeriaJobRevision;
  const int generation = _operationBackendGeneration;
  invalidateNymeriaPlan();
  _nymeriaState->setText(QStringLiteral("Solicitando preparação da Biblioteca…"));
  updateNymeriaLibraryActions();
  _api.post(path,body,[this, revision, generation](bool ok,const QJsonDocument& doc,const QString& error) {
    if (_closing || revision!=_nymeriaJobRevision || generation!=_operationBackendGeneration) return;
    _nymeriaCommandPending = false;
    if (!ok || doc.object().value("ok")!=QJsonValue(true) || !doc.object().value("worker").isObject()) {
      _nymeriaState->setText(QStringLiteral("Preparação não confirmada. ")+error);
      updateNymeriaLibraryActions(); pollNymeriaJob(); return;
    }
    renderNymeriaJob(doc.object().value("worker").toObject());
    pollNymeriaJob();
  });
}

void MainWindow::importNymeriaManifest(const QString& path) {
  startNymeriaCommand(QStringLiteral("/api/library/nymeria/import"),{{"path",path}});
}

void MainWindow::syncNymeriaCatalog() {
  startNymeriaCommand(QStringLiteral("/api/library/nymeria/sync"),{});
}

void MainWindow::planNymeriaLibrary() {
  if (!_nymeriaPlanButton->isEnabled()) return;
  QJsonArray tasks;
  for (int i=0; i<_nymeriaTasks->count(); ++i)
    if (_nymeriaTasks->item(i)->checkState()==Qt::Checked) tasks.append(_nymeriaTasks->item(i)->data(Qt::UserRole).toString());
  if (tasks.isEmpty()) return;
  _nymeriaPlan = {}; _nymeriaPlanPending = true;
  const quint64 revision = ++_nymeriaPlanRevision;
  _nymeriaPotential->setText(QStringLiteral("Procurando conteúdo nas anotações do catálogo completo…"));
  updateNymeriaLibraryActions();
  pollNymeriaPlan({{"task_names",tasks},{"min_dur_s",_nymeriaMinimum->value()*60},
      {"max_dur_s",_nymeriaMaximum->value()*60},{"target_seconds",_nymeriaTargetHours->value()*3600},
      {"min_free_gb",_nymeriaReserve->value()}},revision);
}

void MainWindow::pollNymeriaPlan(const QJsonObject& body, quint64 revision) {
  const int generation = _operationBackendGeneration;
  _api.post(QStringLiteral("/api/library/nymeria/plan"),body,
      [this,body,revision,generation](bool ok,const QJsonDocument& doc,const QString& error) {
    if (_closing || revision!=_nymeriaPlanRevision || generation!=_operationBackendGeneration) return;
    const auto result = doc.object();
    if (ok && result.value("loading")==QJsonValue(true)) {
      _nymeriaPotential->setText(result.value("message").toString(QStringLiteral("Planejando conteúdo Nymeria…")));
      QTimer::singleShot(1200,this,[this,body,revision] {
        if (!_closing && _backendReady && revision==_nymeriaPlanRevision) pollNymeriaPlan(body,revision);
      }); return;
    }
    _nymeriaPlanPending = false;
    const auto ids = result.value("selected_seq_ids").toArray();
    const bool spaceComplete = result.value("space_check_complete")==QJsonValue(true);
    bool valid = result.value("selected_seq_ids").isArray() && result.value("campaign_ready")==QJsonValue(false)
        && result.value("target_found_in_annotations").isBool() && result.value("space_check_complete").isBool()
        && result.value("fits_source_downloads").isBool()
        && (spaceComplete ? result.value("fits_available_disk").isBool() : result.value("fits_available_disk").isNull())
        && result.value("unknown_extraction_seq_ids").isArray();
    for (const auto* key : {"potential_seconds","selected_potential_seconds","download_bytes","disk_free_bytes","extraction_bytes"})
      valid &= result.value(QLatin1String(key)).isDouble() && std::isfinite(result.value(QLatin1String(key)).toDouble())
          && result.value(QLatin1String(key)).toDouble()>=0 && result.value(QLatin1String(key)).toDouble()<=9007199254740991.;
    for (const auto& value : ids) valid &= value.isString() && !value.toString().trimmed().isEmpty();
    const auto unknownExtraction = result.value("unknown_extraction_seq_ids").toArray();
    valid &= spaceComplete == unknownExtraction.isEmpty();
    for (const auto& value : unknownExtraction) valid &= value.isString() && !value.toString().trimmed().isEmpty() && ids.contains(value);
    if (!ok || !valid) _nymeriaPotential->setText(QStringLiteral("Plano não confirmado. ")+(ok?QStringLiteral("Resposta inválida do serviço."):error));
    else {
      _nymeriaPlan = result;
      QString summary = QStringLiteral("%1 h compatíveis nas anotações · %2 sequências no plano · %3 para baixar\n%4 h no plano para a meta de %5 h. Vídeo e IMU ainda precisam ser medidos.")
          .arg(result.value("potential_seconds").toDouble()/3600,0,'f',2).arg(ids.size())
          .arg(bytesText(qint64(result.value("download_bytes").toDouble())))
          .arg(result.value("selected_potential_seconds").toDouble()/3600,0,'f',2).arg(body.value("target_seconds").toDouble()/3600,0,'f',2);
      if (!spaceComplete)
        summary += QStringLiteral("\nO espaço para extrair o IMU será conferido antes de baixar as fontes.");
      else if (result.value("fits_available_disk")!=QJsonValue(true))
        summary += QStringLiteral("\nEspaço insuficiente para as fontes, a extração dos sensores e a reserva escolhida.");
      _nymeriaPotential->setText(summary);
    }
    updateNymeriaLibraryActions();
  });
}

void MainWindow::acquireNymeriaPlan() {
  if (!_nymeriaAcquire->isEnabled()) return;
  startNymeriaCommand(QStringLiteral("/api/library/nymeria/download"),
      {{"seq_ids",_nymeriaPlan.value("selected_seq_ids")},{"min_free_gb",_nymeriaReserve->value()}});
}

void MainWindow::pollNymeriaJob() {
  if (!_backendReady || _closing || _nymeriaJobPending) return;
  _nymeriaJobPending = true;
  const quint64 revision = _nymeriaJobRevision;
  const int generation = _operationBackendGeneration;
  _api.get(QStringLiteral("/api/library/nymeria/operation"),[this,revision,generation](bool ok,const QJsonDocument& doc,const QString& error) {
    if (_closing || generation!=_operationBackendGeneration) return;
    _nymeriaJobPending = false;
    if (revision!=_nymeriaJobRevision) return;
    if (ok) renderNymeriaJob(doc.object());
    else {
      _nymeriaState->setText(QStringLiteral("Estado da preparação indisponível. ")+error);
      if (_pages->currentIndex()==4 && _libraryTabs->currentIndex()==2) _nymeriaPoll.start();
    }
  });
}

void MainWindow::renderNymeriaJob(const QJsonObject& result) {
  const QString state = result.value("state").toString();
  if (!result.value("running").isBool() || !QStringList{"idle","running","done","stopped","error"}.contains(state)) {
    _nymeriaState->setText(QStringLiteral("Resposta da preparação inválida; atualize para confirmar o estado.")); return;
  }
  _nymeriaRunning = result.value("running").toBool();
  const QString message = result.value("message").toString();
  _nymeriaState->setText(message.isEmpty() ? (state=="idle"?QStringLiteral("Nenhuma preparação em andamento"):QStringLiteral("Preparando Biblioteca…")) : message);
  const double total = result.value("total").toDouble();
  const double completed = result.value("completed").toDouble();
  if (_nymeriaRunning && total<=0) _nymeriaProgress->setRange(0,0);
  else {
    _nymeriaProgress->setRange(0,100);
    _nymeriaProgress->setValue(state=="done" && !_nymeriaRunning ? 100
        : total>0 && std::isfinite(total) && std::isfinite(completed)
            ? int(qBound(0.,100*completed/total,100.)) : 0);
  }
  if (_nymeriaRunning && _pages->currentIndex()==4 && _libraryTabs->currentIndex()==2) _nymeriaPoll.start();
  else _nymeriaPoll.stop();
  if (!_nymeriaRunning && state!="idle") {
    const QString run = result.value("run_id").toString();
    if (!run.isEmpty() && run!=_nymeriaLastTerminalRun) {
      _nymeriaLastTerminalRun = run;
      invalidateNymeriaPlan(); loadNymeriaLibrary(); loadLocalMediaLibrary(true);
      if (state=="done" && result.value("operation")=="download") {
        ++_taskLoadGeneration;
        _taskRequestPending = false;
        if (_dataset->currentData().toString()!="ego4d") loadTasks();
      }
    }
  }
  updateNymeriaLibraryActions();
}

void MainWindow::stopNymeriaJob() {
  if (!_nymeriaStop->isEnabled()) return;
  _nymeriaCommandPending = true;
  const quint64 revision = ++_nymeriaJobRevision;
  const int generation = _operationBackendGeneration;
  updateNymeriaLibraryActions();
  _api.post(QStringLiteral("/api/library/nymeria/stop"),{},[this,revision,generation](bool ok,const QJsonDocument& doc,const QString& error) {
    if (_closing || revision!=_nymeriaJobRevision || generation!=_operationBackendGeneration) return;
    _nymeriaCommandPending = false;
    if (ok && doc.object().value("ok")==QJsonValue(true) && doc.object().value("worker").isObject())
      renderNymeriaJob(doc.object().value("worker").toObject());
    else _nymeriaState->setText(QStringLiteral("Parada não confirmada. ")+error);
    updateNymeriaLibraryActions(); pollNymeriaJob();
  });
}

void MainWindow::updateLocalMediaActions() {
  if (!_localMediaTable) return;
  _localMediaCopy->setEnabled(!localMediaPath().isEmpty());
  _localMediaOpen->setEnabled(!localMediaPath(true).isEmpty());
  const QFileInfo root(_localMediaRoot);
  _libraryOpenFolder->setEnabled(_localMediaVerified && root.isAbsolute() && root.isDir()
      && !root.canonicalFilePath().isEmpty());
}

void MainWindow::loadLocalMediaLibrary(bool refresh) {
  if (!_localMediaTable) return;
  if (refresh) {
    _localMediaRefreshNonce = QUuid::createUuid().toString(QUuid::WithoutBraces);
    _localMediaTable->setRowCount(0);
  }
  _localMediaVerified = false;
  updateLocalMediaActions();
  _localMediaPrevious->setEnabled(false);
  _localMediaNext->setEnabled(false);
  QUrlQuery params;
  params.addQueryItem(QStringLiteral("async"), QStringLiteral("1"));
  params.addQueryItem(QStringLiteral("q"), _localMediaQuery->text());
  params.addQueryItem(QStringLiteral("provider"), _localMediaProvider->currentData().toString());
  params.addQueryItem(QStringLiteral("kind"), _localMediaKind->currentData().toString());
  params.addQueryItem(QStringLiteral("limit"), QStringLiteral("50"));
  params.addQueryItem(QStringLiteral("offset"), QString::number(_localMediaOffset));
  if (!_localMediaRefreshNonce.isEmpty()) params.addQueryItem(QStringLiteral("refresh"), _localMediaRefreshNonce);
  const QString path = QStringLiteral("/api/storage/library/items?") + params.toString(QUrl::FullyEncoded);
  if (_localMediaInFlightKey == path) return;
  if (_localMediaRequestKey != path) { _localMediaRequestKey = path; ++_localMediaRequestId; }
  const quint64 requestId = _localMediaRequestId;
  const int requestedOffset = _localMediaOffset;
  _localMediaInFlightKey = path;
  _localMediaSummary->setText(refresh ? QStringLiteral("Verificando arquivos locais…") : QStringLiteral("Consultando arquivos locais…"));
  _api.get(path, [this, requestId, requestedOffset, path](bool ok, const QJsonDocument& doc, const QString& error) {
    if (_localMediaInFlightKey == path) _localMediaInFlightKey.clear();
    if (requestId != _localMediaRequestId) return;
    const auto fail = [this](const QString& message) {
      _localMediaPoll.stop();
      _localMediaRefreshNonce.clear();
      _localMediaVerified = false;
      _localMediaTable->setRowCount(0);
      _localMediaSummary->setText(message);
      _localMediaCount->setText(QStringLiteral("Nenhum arquivo confirmado nesta consulta"));
      updateLocalMediaActions();
    };
    if (!ok) { fail(error); return; }
    const auto root = doc.object();
    if (root.value(QStringLiteral("loading")).toBool() || root.value(QStringLiteral("refreshing")).toBool()
        || root.value(QStringLiteral("state")).toString() == QStringLiteral("loading")) {
      _localMediaSummary->setText(root.value(QStringLiteral("message")).toString(QStringLiteral("Verificando arquivos locais…")));
      if (_pages->currentIndex() == 4 && _libraryTabs->currentIndex() == 0) _localMediaPoll.start();
      return;
    }
    const auto integer = [](const QJsonValue& value) {
      return value.isDouble() && std::isfinite(value.toDouble()) && value.toDouble() >= 0
          && value.toDouble() <= 9007199254740991.0 && value.toDouble() == std::floor(value.toDouble());
    };
    const QHash<QString, QString> providers{{QStringLiteral("ego4d"), QStringLiteral("Ego4D")},
        {QStringLiteral("holoassist"), QStringLiteral("HoloAssist")}, {QStringLiteral("nymeria"), QStringLiteral("Nymeria")},
        {QStringLiteral("local"), QStringLiteral("Local")}};
    const QHash<QString, QString> kinds{{QStringLiteral("video"), QStringLiteral("Vídeo")},
        {QStringLiteral("sensor"), QStringLiteral("Sensores / IMU")}, {QStringLiteral("sidecar"), QStringLiteral("Apoio")},
        {QStringLiteral("catalog"), QStringLiteral("Catálogo")}, {QStringLiteral("derivative"), QStringLiteral("Derivado")}};
    const auto items = root.value(QStringLiteral("items")).toArray();
    bool valid = root.value(QStringLiteral("schema")).toInt() == 1
        && root.value(QStringLiteral("inventory_scope")).toString() == QStringLiteral("local_media_files")
        && root.value(QStringLiteral("state")).toString() == QStringLiteral("ready")
        && QFileInfo(root.value(QStringLiteral("library_root")).toString()).isAbsolute()
        && root.value(QStringLiteral("items")).isArray() && integer(root.value(QStringLiteral("total")))
        && integer(root.value(QStringLiteral("offset"))) && root.value(QStringLiteral("offset")).toInt(-1) == requestedOffset
        && integer(root.value(QStringLiteral("file_count"))) && integer(root.value(QStringLiteral("total_bytes")))
        && items.size() <= 50 && items.size() <= root.value(QStringLiteral("total")).toDouble()
        && (items.isEmpty() || requestedOffset + items.size() <= root.value(QStringLiteral("total")).toDouble());
    for (const auto& value : items) {
      const auto item = value.toObject();
      valid = valid && value.isObject() && !item.value(QStringLiteral("name")).toString().isEmpty()
          && !item.value(QStringLiteral("relative_path")).toString().isEmpty()
          && !item.value(QStringLiteral("path")).toString().isEmpty()
          && providers.contains(item.value(QStringLiteral("provider")).toString())
          && kinds.contains(item.value(QStringLiteral("kind")).toString())
          && item.value(QStringLiteral("stage")).isString() && integer(item.value(QStringLiteral("size_bytes")))
          && item.value(QStringLiteral("protected")).isBool() && item.value(QStringLiteral("protection_reasons")).isArray();
    }
    if (!valid) { fail(QStringLiteral("Resposta incompleta dos arquivos locais. Clique em Verificar arquivos para tentar novamente.")); return; }
    _localMediaPoll.stop();
    _localMediaRoot = root.value(QStringLiteral("library_root")).toString();
    _libraryBase->setText(_localMediaRoot);
    _localMediaTable->setRowCount(items.size());
    for (int row = 0; row < items.size(); ++row) {
      const auto item = items.at(row).toObject();
      auto* name = new QTableWidgetItem(item.value(QStringLiteral("name")).toString());
      name->setData(Qt::UserRole, item);
      name->setToolTip(item.value(QStringLiteral("relative_path")).toString() + QLatin1Char('\n')
          + item.value(QStringLiteral("path")).toString());
      _localMediaTable->setItem(row, 0, name);
      _localMediaTable->setItem(row, 1, new QTableWidgetItem(providers.value(item.value(QStringLiteral("provider")).toString())));
      _localMediaTable->setItem(row, 2, new QTableWidgetItem(QStringLiteral("%1 · %2")
          .arg(kinds.value(item.value(QStringLiteral("kind")).toString()), item.value(QStringLiteral("stage")).toString())));
      const qint64 size = static_cast<qint64>(item.value(QStringLiteral("size_bytes")).toDouble());
      auto* sizeCell = new QTableWidgetItem(bytesText(size));
      sizeCell->setToolTip(QStringLiteral("%1 bytes").arg(size));
      _localMediaTable->setItem(row, 3, sizeCell);
      QStringList reasons;
      for (const auto& reason : item.value(QStringLiteral("protection_reasons")).toArray()) reasons << reason.toString();
      const bool unknown = reasons.contains(QStringLiteral("protection_state_unknown"))
          || reasons.contains(QStringLiteral("digest_protection_unverified"));
      auto* protection = new QTableWidgetItem(item.value(QStringLiteral("protected")).toBool()
          ? unknown ? QStringLiteral("Preservado") : QStringLiteral("Em uso") : QStringLiteral("—"));
      protection->setToolTip(reasons.join(QLatin1Char('\n')));
      _localMediaTable->setItem(row, 4, protection);
    }
    _localMediaVerified = true;
    _localMediaRefreshNonce.clear();
    const auto free = root.value(QStringLiteral("disk_free_bytes"));
    _libraryStorageSummary->setText(QStringLiteral("%1 arquivos · %2 ocupados · %3 livres no disco")
        .arg(root.value(QStringLiteral("file_count")).toInt())
        .arg(bytesText(static_cast<qint64>(root.value(QStringLiteral("total_bytes")).toDouble())))
        .arg(integer(free) ? bytesText(static_cast<qint64>(free.toDouble())) : QStringLiteral("espaço indisponível")));
    _localMediaSummary->setText(QStringLiteral("Arquivos locais de todas as origens. O estágio indica a presença física; os recortes são conferidos na outra aba."));
    if (root.value(QStringLiteral("scan_complete")).isBool() && !root.value(QStringLiteral("scan_complete")).toBool())
      _localMediaSummary->setText(QStringLiteral("Leitura incompleta: %1 falha(s) ao acessar arquivos ou pastas. Os totais incluem somente os arquivos encontrados.")
          .arg(root.value(QStringLiteral("scan_errors")).toInt()));
    const int total = root.value(QStringLiteral("total")).toInt();
    _localMediaCount->setText(items.isEmpty() ? QStringLiteral("Nenhum arquivo encontrado nesta consulta")
        : QStringLiteral("%1–%2 de %3 arquivos nesta consulta").arg(requestedOffset + 1).arg(requestedOffset + items.size()).arg(total));
    _localMediaPrevious->setEnabled(requestedOffset > 0);
    _localMediaNext->setEnabled(requestedOffset + items.size() < total);
    updateLocalMediaActions();
  });
}

void MainWindow::preparedLibraryFiltersChanged() {
  _preparedOffset = 0;
  _preparedRefreshNonce.clear();
  _preparedVerified = false;
  _preparedTable->setRowCount(0);
  updatePreparedLibraryActions();
  loadPreparedLibrary();
}

QString MainWindow::preparedLibraryVideoPath(const QString& role) const {
  if (!_preparedVerified || !_preparedTable || _preparedLibraryRoot.isEmpty()) return {};
  const auto* selected = _preparedTable->item(_preparedTable->currentRow(), 2);
  if (!selected) return {};
  const auto file = selected->data(Qt::UserRole).value<QJsonObject>().value(role).toObject();
  if (!file.value(QStringLiteral("present")).toBool()) return {};
  const QFileInfo candidate(file.value(QStringLiteral("path")).toString());
  const QString root = QFileInfo(_preparedLibraryRoot).canonicalFilePath();
  const QString actual = candidate.canonicalFilePath();
  if (root.isEmpty() || actual.isEmpty() || !candidate.isAbsolute() || !candidate.isFile()
      || candidate.suffix().compare(QStringLiteral("mp4"), Qt::CaseInsensitive) != 0) return {};
  const QString relative = QDir(root).relativeFilePath(actual);
  if (relative == QStringLiteral("..") || relative.startsWith(QStringLiteral("../"))
      || QDir::isAbsolutePath(relative)) return {};
  return actual;
}

void MainWindow::updatePreparedLibraryActions() {
  if (!_preparedTable) return;
  _preparedCopy->setEnabled(_preparedVerified && _preparedTable->currentRow() >= 0);
  _preparedOpenNative->setEnabled(!preparedLibraryVideoPath(QStringLiteral("native")).isEmpty());
  _preparedOpenSource->setEnabled(!preparedLibraryVideoPath(QStringLiteral("source")).isEmpty());
}

void MainWindow::invalidatePreparedLibrary() {
  _preparedPoll.stop();
  ++_preparedRequestId;
  _preparedRequestKey.clear();
  _preparedInFlightKey.clear();
  _preparedRefreshNonce.clear();
  _preparedVerified = false;
  _preparedRefreshPending = true;
  _preparedTable->setRowCount(0);
  _preparedPrevious->setEnabled(false);
  _preparedNext->setEnabled(false);
  _preparedSummary->setText(QStringLiteral("O acervo mudou. Abra esta aba para verificar os recortes preparados."));
  updatePreparedLibraryActions();
  if (_pages->currentIndex() == 4 && _libraryTabs->currentIndex() == 1) loadPreparedLibrary();
}

void MainWindow::loadPreparedLibrary(bool refresh) {
  if (!_preparedTable) return;
  refresh = refresh || _preparedRefreshPending;
  _preparedRefreshPending = false;
  if (refresh) {
    _preparedRefreshNonce = QUuid::createUuid().toString(QUuid::WithoutBraces);
    _preparedTable->setRowCount(0);
  }
  _preparedVerified = false;
  updatePreparedLibraryActions();
  _preparedPrevious->setEnabled(false);
  _preparedNext->setEnabled(false);
  const int minimum = _preparedMinimum->value() * 60;
  const int maximum = _preparedMaximum->value() * 60;
  if (maximum > 0 && maximum < minimum) {
    ++_preparedRequestId;
    _preparedRequestKey.clear();
    _preparedPoll.stop();
    _preparedTable->setRowCount(0);
    _preparedSummary->setText(QStringLiteral("A duração máxima deve ser igual ou maior que a mínima."));
    return;
  }
  QUrlQuery params;
  params.addQueryItem(QStringLiteral("async"), QStringLiteral("1"));
  params.addQueryItem(QStringLiteral("q"), _preparedQuery->text());
  params.addQueryItem(QStringLiteral("state"), _preparedState->currentData().toString());
  params.addQueryItem(QStringLiteral("min_s"), QString::number(minimum));
  if (maximum > 0) params.addQueryItem(QStringLiteral("max_s"), QString::number(maximum));
  params.addQueryItem(QStringLiteral("limit"), QStringLiteral("50"));
  params.addQueryItem(QStringLiteral("offset"), QString::number(_preparedOffset));
  if (!_preparedRefreshNonce.isEmpty()) params.addQueryItem(QStringLiteral("refresh"), _preparedRefreshNonce);
  const QString path = QStringLiteral("/api/library/ego4d/prepared?") + params.toString(QUrl::FullyEncoded);
  if (_preparedInFlightKey == path) return;
  if (_preparedRequestKey != path) {
    _preparedRequestKey = path;
    ++_preparedRequestId;
  }
  const quint64 requestId = _preparedRequestId;
  const int requestedOffset = _preparedOffset;
  _preparedInFlightKey = path;
  _preparedSummary->setText(refresh ? QStringLiteral("Verificando arquivos locais…") : QStringLiteral("Consultando acervo preparado…"));
  _api.get(path, [this, requestId, requestedOffset, path](bool ok, const QJsonDocument& doc, const QString& error) {
    if (_preparedInFlightKey == path) _preparedInFlightKey.clear();
    if (requestId != _preparedRequestId) return;
    const auto fail = [this](const QString& message) {
      _preparedPoll.stop();
      _preparedRefreshNonce.clear();
      _preparedVerified = false;
      _preparedTable->setRowCount(0);
      _preparedSummary->setText(message);
      _preparedCount->setText(QStringLiteral("Nenhum arquivo confirmado nesta consulta"));
      updatePreparedLibraryActions();
    };
    if (!ok) { fail(error); return; }
    const auto root = doc.object();
    if (root.value(QStringLiteral("loading")).toBool() || root.value(QStringLiteral("refreshing")).toBool()
        || root.value(QStringLiteral("state")).toString() == QStringLiteral("loading")) {
      _preparedSummary->setText(root.value(QStringLiteral("message")).toString(QStringLiteral("Verificando arquivos locais…")));
      if (_pages->currentIndex() == 4 && _libraryTabs->currentIndex() == 1) _preparedPoll.start();
      return;
    }
    if (root.value(QStringLiteral("schema")).toInt() != 1
        || root.value(QStringLiteral("provider")).toString() != QStringLiteral("ego4d")
        || root.value(QStringLiteral("inventory_scope")).toString() != QStringLiteral("local_prepared_media")) {
      fail(QStringLiteral("Resposta incompleta do acervo. Clique em Verificar acervo para tentar novamente."));
      return;
    }
    _preparedPoll.stop();
    const auto integer = [](const QJsonValue& value) {
      return value.isDouble() && std::isfinite(value.toDouble()) && value.toDouble() >= 0
          && value.toDouble() == std::floor(value.toDouble());
    };
    const auto counts = root.value(QStringLiteral("counts")).toObject();
    bool valid = root.value(QStringLiteral("state")).toString() == QStringLiteral("ready")
        && root.value(QStringLiteral("items")).isArray() && integer(root.value(QStringLiteral("total")))
        && integer(root.value(QStringLiteral("offset"))) && root.value(QStringLiteral("offset")).toInt(-1) == requestedOffset
        && !root.value(QStringLiteral("library_root")).toString().isEmpty();
    for (const auto* key : {"ready", "partial", "missing", "stale", "protected"})
      valid = valid && integer(counts.value(QLatin1String(key)));
    const auto items = root.value(QStringLiteral("items")).toArray();
    valid = valid && items.size() <= 50 && items.size() <= root.value(QStringLiteral("total")).toDouble();
    const QHash<QString, QString> stateLabels{
        {QStringLiteral("ready"), QStringLiteral("Mídia preparada")},
        {QStringLiteral("partial"), QStringLiteral("Preparação incompleta")},
        {QStringLiteral("missing"), QStringLiteral("Arquivo ausente")},
        {QStringLiteral("stale"), QStringLiteral("Revisar")}};
    for (const auto& value : items) {
      const auto item = value.toObject();
      const auto duration = item.value(QStringLiteral("duration_ms"));
      valid = valid && value.isObject() && !item.value(QStringLiteral("clip_uid")).toString().isEmpty()
          && stateLabels.contains(item.value(QStringLiteral("cache_state")).toString())
          && item.value(QStringLiteral("protected")).isBool() && item.value(QStringLiteral("reasons")).isArray()
          && (duration.isNull() || integer(duration));
      if (item.value(QStringLiteral("cache_state")).toString() == QStringLiteral("ready"))
        valid = valid && duration.isDouble() && duration.toDouble() > 0;
      for (const auto* key : {"source", "native", "imu"}) {
        const auto fileValue = item.value(QLatin1String(key));
        if (fileValue.isNull()) {
          valid = valid && item.value(QStringLiteral("cache_state")).toString() != QStringLiteral("ready");
          continue;
        }
        const auto file = fileValue.toObject();
        valid = valid && fileValue.isObject()
            && file.value(QStringLiteral("present")).isBool() && integer(file.value(QStringLiteral("bytes")));
        if (file.value(QStringLiteral("present")).toBool())
          valid = valid && !file.value(QStringLiteral("path")).toString().isEmpty();
        if (item.value(QStringLiteral("cache_state")).toString() == QStringLiteral("ready"))
          valid = valid && file.value(QStringLiteral("present")).toBool() && file.value(QStringLiteral("bytes")).toDouble() > 0;
      }
    }
    if (!valid) {
      fail(root.value(QStringLiteral("error")).toString(QStringLiteral("Dados incompletos do acervo. Clique em Verificar acervo para tentar novamente.")));
      return;
    }
    _preparedLibraryRoot = root.value(QStringLiteral("library_root")).toString();
    _preparedTable->setRowCount(items.size());
    for (int row = 0; row < items.size(); ++row) {
      const auto item = items.at(row).toObject();
      QString status = stateLabels.value(item.value(QStringLiteral("cache_state")).toString());
      QStringList reasons;
      for (const auto reason : item.value(QStringLiteral("reasons")).toArray()) reasons << reason.toString();
      for (const auto reason : item.value(QStringLiteral("protection_reasons")).toArray()) reasons << reason.toString();
      const bool protectionUnconfirmed = root.value(QStringLiteral("protection_status")).toString() == QStringLiteral("unknown")
          || reasons.contains(QStringLiteral("protection_state_unknown")) || reasons.contains(QStringLiteral("digest_protection_unverified"));
      if (item.value(QStringLiteral("protected")).toBool())
        status += protectionUnconfirmed ? QStringLiteral(" · Preservado") : QStringLiteral(" · Em uso");
      if (protectionUnconfirmed) reasons << QStringLiteral("Proteção a confirmar; o arquivo é preservado nesta operação.");
      auto* state = new QTableWidgetItem(status);
      state->setToolTip(reasons.join(QLatin1Char('\n')));
      _preparedTable->setItem(row, 0, state);
      const int milliseconds = item.value(QStringLiteral("duration_ms")).toInt();
      const int seconds = milliseconds / 1000;
      auto* duration = new QTableWidgetItem(milliseconds > 0
          ? QStringLiteral("%1:%2").arg(seconds / 60).arg(seconds % 60, 2, 10, QLatin1Char('0')) : QStringLiteral("—"));
      duration->setToolTip(QStringLiteral("Duração do recorte: %1 ms").arg(milliseconds));
      _preparedTable->setItem(row, 1, duration);
      auto* clip = new QTableWidgetItem(item.value(QStringLiteral("clip_uid")).toString());
      clip->setData(Qt::UserRole, item);
      const auto window = item.value(QStringLiteral("window_s")).toArray();
      clip->setToolTip(QStringLiteral("%1\nVídeo de origem: %2\nJanela: %3–%4 s\nTarefa escolhida na campanha")
          .arg(clip->text(), item.value(QStringLiteral("parent_video_uid")).toString())
          .arg(window.size() > 0 ? window.at(0).toDouble() : 0, 0, 'f', 3)
          .arg(window.size() > 1 ? window.at(1).toDouble() : 0, 0, 'f', 3));
      _preparedTable->setItem(row, 2, clip);
      QStringList fileNames, paths;
      for (const auto& entry : QList<QPair<QString, QString>>{
          {QStringLiteral("native"), QStringLiteral("Recorte")},
          {QStringLiteral("source"), QStringLiteral("Origem")},
          {QStringLiteral("imu"), QStringLiteral("Sensores")}}) {
        const auto file = item.value(entry.first).toObject();
        if (file.value(QStringLiteral("present")).toBool()) fileNames << entry.second;
        paths << QStringLiteral("%1: %2").arg(entry.second,
            file.value(QStringLiteral("present")).toBool() ? file.value(QStringLiteral("path")).toString() : QStringLiteral("ausente"));
      }
      auto* local = new QTableWidgetItem(QStringLiteral("%1 de 3").arg(fileNames.size()));
      local->setToolTip(paths.join(QLatin1Char('\n')));
      _preparedTable->setItem(row, 3, local);
    }
    _preparedVerified = true;
    _preparedRefreshNonce.clear();
    _preparedSummary->setText(QStringLiteral("%1 com mídia preparada · %2 incompletos · %3 ausentes · %4 para revisar · %5 preservados")
        .arg(counts.value(QStringLiteral("ready")).toInt()).arg(counts.value(QStringLiteral("partial")).toInt())
        .arg(counts.value(QStringLiteral("missing")).toInt()).arg(counts.value(QStringLiteral("stale")).toInt())
        .arg(counts.value(QStringLiteral("protected")).toInt()));
    if (root.value(QStringLiteral("protection_status")).toString() == QStringLiteral("unknown"))
      _preparedSummary->setText(_preparedSummary->text() + QStringLiteral("\nA proteção de arquivos em uso não pôde ser confirmada nesta leitura."));
    const double timestamp = root.value(QStringLiteral("verified_at")).toDouble();
    const QString checked = timestamp > 0 ? QDateTime::fromSecsSinceEpoch(static_cast<qint64>(timestamp)).toLocalTime()
        .toString(QStringLiteral("dd/MM HH:mm")) : QStringLiteral("horário indisponível");
    const int total = root.value(QStringLiteral("total")).toInt();
    _preparedCount->setText(items.isEmpty() ? QStringLiteral("Nenhum recorte encontrado · verificado em %1").arg(checked)
        : QStringLiteral("%1–%2 de %3 recortes · verificado em %4").arg(requestedOffset + 1).arg(requestedOffset + items.size()).arg(total).arg(checked));
    _preparedPrevious->setEnabled(requestedOffset > 0);
    _preparedNext->setEnabled(requestedOffset + items.size() < total);
    updatePreparedLibraryActions();
  });
}

void MainWindow::loadAccelerator() {
  const QString provider = _cacheProvider && !_cacheProvider->currentData().toString().isEmpty()
      ? _cacheProvider->currentData().toString() : QStringLiteral("holoassist");
  const QString requestedTask = _cacheTask->count() ? _cacheTask->currentText() : QString();
  if (provider == QStringLiteral("ego4d") && _cacheMinimum->value() > _cacheMaximum->value()) {
    ++_cacheRequestId;
    _cacheRequestKey.clear();
    _cacheStart->setEnabled(false);
    _cacheState->setText(QStringLiteral("A duração máxima deve ser igual ou maior que a mínima."));
    return;
  }
  QString path = QStringLiteral("/api/holo-cache?async=1&provider=%1").arg(encoded(provider));
  if (provider == QStringLiteral("ego4d"))
    path += QStringLiteral("&min_dur_s=%1&max_dur_s=%2").arg(_cacheMinimum->value() * 60).arg(_cacheMaximum->value() * 60);
  if (!requestedTask.isEmpty()) {
    path += QStringLiteral("&task=%1&limit=%2").arg(encoded(requestedTask)).arg(_cacheLimit->value());
  }
  if (provider == QStringLiteral("ego4d") && _cacheBudget) {
    if (_cacheBudgetLoaded)
      path += QStringLiteral("&budget_gb=%1").arg(_cacheBudget->value());
    path += QStringLiteral("&min_free_gb=%1").arg(_cacheReserve->value());
  }
  const bool live = !_cacheCatalogPending && _cachePoll.isActive()
      && _cacheCatalogSnapshot.value(QStringLiteral("provider")).toString() == provider
      && (provider != QStringLiteral("ego4d")
          || (_cacheCatalogSnapshot.value(QStringLiteral("min_dur_s")).toInt(-1) == _cacheMinimum->value() * 60
              && _cacheCatalogSnapshot.value(QStringLiteral("max_dur_s")).toInt(-1) == _cacheMaximum->value() * 60))
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
    if (requestId != _cacheRequestId || _cacheStartPending) return;
    if (!ok) {
      _cacheState->setText(QStringLiteral("Não foi possível consultar o acelerador"));
      _cacheLastRun->setText(error);
      return setStatus(error);
    }
    if (_cacheProvider && _cacheProvider->currentData().toString() != provider) return;
    if (!requestedTask.isEmpty() && _cacheTask->currentText() != requestedTask) return;
    const auto root = doc.object();
    const QString runnerState = root.value(QStringLiteral("runner")).toObject()
        .value(QStringLiteral("state")).toString();
    if (live && runnerState != QStringLiteral("running") && runnerState != QStringLiteral("stopping")) {
      // Refresh disk counts once the live run ends; the pre-run snapshot is stale.
      _cachePoll.stop();
      _cacheCatalogPending = false;
      invalidatePreparedLibrary();
      loadLocalMediaLibrary(true);
      loadAccelerator();
      return;
    }
    if (root.value(QStringLiteral("loading")).toBool()) {
      _cacheCatalogPending = true;
      _cacheState->setText(root.value(QStringLiteral("message")).toString());
      _cacheStart->setEnabled(false);
      if (_pages->currentIndex() == 4) _cachePoll.start();
      return;
    }
    _cacheCatalogPending = false;
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
      if (provider == QStringLiteral("ego4d")) {
        cache.insert(QStringLiteral("min_dur_s"), _cacheMinimum->value() * 60);
        cache.insert(QStringLiteral("max_dur_s"), _cacheMaximum->value() * 60);
      }
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
    const int runProtected = runner.value(QStringLiteral("protected")).toInt();
    const int processed = qMin(runTotal, runReady + runFailed + runProtected);
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
      if (runProtected > 0)
        _cacheNumbers->setText(_cacheNumbers->text() + QStringLiteral(" · %1 preservado(s)").arg(runProtected));
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
      const int lastProtected = lastRun.value(QStringLiteral("protected")).toInt();
      if (lastProtected > 0)
        _cacheLastRun->setText(_cacheLastRun->text()
            + QStringLiteral("\n%1 clipe(s) preservado(s).").arg(lastProtected));
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
  if (_cacheStartPending) return;
  if (_cacheTask->currentText().isEmpty()) return;
  const QString provider = _cacheProvider && !_cacheProvider->currentData().toString().isEmpty()
      ? _cacheProvider->currentData().toString() : QStringLiteral("holoassist");
  if (provider == QStringLiteral("ego4d") && _cacheMinimum->value() > _cacheMaximum->value()) {
    _cacheState->setText(QStringLiteral("A duração máxima deve ser igual ou maior que a mínima."));
    return;
  }
  QJsonObject body{{QStringLiteral("provider"), provider},
                   {QStringLiteral("task"), _cacheTask->currentText()},
                   {QStringLiteral("min_free_gb"), _cacheReserve->value()}};
  if (_cacheLimit->value() > 0) body.insert(QStringLiteral("limit"), _cacheLimit->value());
  else body.insert(QStringLiteral("limit"), QJsonValue::Null);
  if (provider == QStringLiteral("ego4d") && _cacheBudget)
    body.insert(QStringLiteral("budget_gb"), _cacheBudget->value());
  if (provider == QStringLiteral("ego4d")) {
    body.insert(QStringLiteral("min_dur_s"), _cacheMinimum->value() * 60);
    body.insert(QStringLiteral("max_dur_s"), _cacheMaximum->value() * 60);
  }
  _cacheStartPending = true;
  ++_cacheRequestId;
  _cacheStart->setEnabled(false);
  _cacheState->setText(QStringLiteral("Iniciando preparação…"));
  _api.post(QStringLiteral("/api/holo-cache/start"), body,
            [this](bool ok, const QJsonDocument&, const QString& error) {
    _cacheStartPending = false;
    if (!ok) {
      _cacheStart->setEnabled(true);
      _cacheState->setText(QStringLiteral("Preparação não iniciada"));
      return showError(QStringLiteral("Acelerador não iniciado"), error);
    }
    _cacheCatalogPending = false;
    _cachePoll.start();
    loadAccelerator();
  });
}

void MainWindow::setAccountTransferBusy(bool busy) {
  _accountTransferBusy = busy;
  busy = busy || _orgMigrationRunning || _accountConnecting || _accountsCheckRunning
      || _bulkRegisterStarting || _bulkRegisterPolling || _bulkRegisterOutcomeUnknown
      || !_backendReady || !_accountsAvailable;
  _accountsImport->setEnabled(!busy);
  _accountsExport->setEnabled(!busy);
  _accountsExportSelected->setEnabled(!busy);
  _accountAdd->setEnabled(!busy);
  _accountRegister->setEnabled(!busy && _registrationProxiesReady);
  _accountProxy->setEnabled(!busy && _registrationProxiesReady);
  _bulkRegisterProxy->setEnabled(!busy && _registrationProxiesReady);
  _accountProxyImport->setEnabled(!busy);
  _bulkProxyImport->setEnabled(!busy);
  _accountsCheckAll->setEnabled(!busy && _accountsTable->rowCount() > 0);
  _accountsTable->setEnabled(!busy);
  _accountsMigrate->setEnabled(!busy && _accountsTable->rowCount() > 0);
  updateBulkRegisterStartEnabled();
}

void MainWindow::updateBulkRegisterStartEnabled() {
  if (!_bulkRegisterStart) return;
  const bool busy = _accountTransferBusy || _orgMigrationRunning || _accountConnecting
      || _accountsCheckRunning || _bulkRegisterStarting || _bulkRegisterPolling
      || _bulkRegisterOutcomeUnknown || !_backendReady || !_accountsAvailable;
  const bool samePreflight = _bulkRegisterPreflightReady
      && _bulkRegisterPreflightDomain == _bulkRegisterDomain->currentData().toString()
      && _bulkRegisterPreflightProxy == _bulkRegisterProxy->currentData().toString();
  _bulkRegisterStart->setEnabled(!busy && _registrationProxiesReady && samePreflight);
}

void MainWindow::startOrgMigration() {
  _migrationStatus->show();
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
      _migrationStatus->setVisible(state != QStringLiteral("idle"));
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
  if (!_backendReady) return;
  pollOrgMigration();
  const int revision = ++_accountsRevision;
  const int generation = _operationBackendGeneration;
  _api.get(QStringLiteral("/api/accounts"), [this, revision, generation](bool ok, const QJsonDocument& doc, const QString& error) {
    if (revision != _accountsRevision || generation != _operationBackendGeneration || !_backendReady) return;
    if (!ok) {
      _accountsAvailable = false;
      setAccountTransferBusy(_accountTransferBusy);
      _accountsSummary->setText(QStringLiteral("Leitura interrompida · dados anteriores preservados. ") + error);
      return;
    }
    if (!doc.object().value("accounts").isArray()) {
      _accountsAvailable = false;
      setAccountTransferBusy(_accountTransferBusy);
      _accountsSummary->setText(QStringLiteral("Resposta de contas inválida · dados anteriores preservados."));
      return;
    }
    const auto accounts = doc.object().value(QStringLiteral("accounts")).toArray();
    QSet<QString> owners;
    for (const auto value : accounts) {
      const auto account = value.toObject();
      const auto owner = account.value("email").toString().trimmed().toCaseFolded();
      if (owner.isEmpty() || owners.contains(owner) || !account.value("last_check").isObject()
          || !account.value("has_password").isBool()) {
        _accountsAvailable = false;
        setAccountTransferBusy(_accountTransferBusy);
        _accountsSummary->setText(QStringLiteral("Resposta de contas inconsistente · dados anteriores preservados."));
        return;
      }
      owners.insert(owner);
    }
    _accountsAvailable = true;
    renderAccounts(accounts);
  });
}

void MainWindow::renderAccounts(const QJsonArray& accounts) {
    QSet<QString> selected;
    for (const auto* item : _accountsTable->selectedItems())
      if (item->column() == 0) selected.insert(item->text());
    const int scroll = _accountsTable->verticalScrollBar()->value();
    _accountsSnapshot = accounts;
    _accountsTable->clearContents();
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
      _accountsTable->item(row, 0)->setToolTip(orgCell->toolTip() + QStringLiteral("\n") + orgCell->text());
      delete orgCell;
      const auto registration = account.value("registration").toObject();
      const QString registrationState = registration.value("state").toString();
      bool siteManual = false;
      const auto registrationSteps = registration.value("steps").toObject();
      for (const auto value : registrationSteps) siteManual |= value.toObject().value("status").toString() == "manual";
      auto* registrationCell = cell(registrationState == "complete" ? (siteManual ? QStringLiteral("Criada · site manual") : QStringLiteral("Completo"))
          : registrationState.isEmpty() ? QStringLiteral("Conta conectada") : QStringLiteral("Cadastro incompleto"));
      registrationCell->setToolTip(registration.value("error").toString(QStringLiteral("Abra os detalhes para conferir as etapas.")));
      _accountsTable->setItem(row, 1, registrationCell);
      const qint64 expiry = static_cast<qint64>(account.value(QStringLiteral("expires_at")).toDouble());
      const auto lastCheck = _accountChecks.contains(email)
          ? _accountChecks.value(email) : account.value(QStringLiteral("last_check")).toObject();
      const auto providers = lastCheck.value("providers").toObject();
      const auto minuteCheck = providers.isEmpty() ? lastCheck : providers.value("minute").toObject();
      const auto crowtadoCheck = providers.value("crowtado").toObject();
      QString state = expiry > QDateTime::currentSecsSinceEpoch()
          ? QStringLiteral("Token no prazo · não verificada")
          : QStringLiteral("Acesso precisa ser verificado");
      if (!minuteCheck.isEmpty()) {
        state = minuteCheck.value(QStringLiteral("status_label")).toString(
                    minuteCheck.value(QStringLiteral("status")).toString() == QStringLiteral("active")
                        ? QStringLiteral("Acesso verificado") : QStringLiteral("Verificação inconclusiva"))
                + QStringLiteral(" · ") + friendlyDate(minuteCheck.value(QStringLiteral("checked_at")).toString());
      }
      if (!account.value("has_minute_access").toBool(true)) state = QStringLiteral("Minute ainda não conectado");
      auto* statusCell = cell(state.section(QStringLiteral(" · "),0,0));
      QString checkDetails = state + QStringLiteral("\n") + minuteCheck.value(QStringLiteral("error")).toString();
      if (!minuteCheck.value("last_success_at").toString().isEmpty())
        checkDetails += QStringLiteral("\nÚltimo acesso confirmado: ") + friendlyDate(minuteCheck.value("last_success_at").toString());
      if (!minuteCheck.value("last_restriction").toObject().isEmpty())
        checkDetails += QStringLiteral("\nÚltima restrição registrada: ") + friendlyDate(minuteCheck.value("last_restriction").toObject().value("checked_at").toString());
      statusCell->setToolTip(checkDetails);
      _accountsTable->setItem(row, 2, statusCell);
      const auto restriction = account.value("restriction").toObject();
      auto* restrictionCell = cell(isClaru ? QStringLiteral("Não se aplica")
          : crowtadoCheck.value("status_label").toString(QStringLiteral("Crowtado não verificada")));
      QString crowtadoDetails = crowtadoCheck.value("status_label").toString()
          + QStringLiteral("\n") + friendlyDate(crowtadoCheck.value("checked_at").toString())
          + QStringLiteral("\n") + crowtadoCheck.value("error").toString()
          + QStringLiteral("\nCarteira (última consulta): ") + restriction.value("label").toString()
          + QStringLiteral("\n") + restriction.value("reason").toString();
      if (crowtadoCheck.value("access_status").toString() == "active")
        crowtadoDetails += QStringLiteral("\nLogin Crowtado aceito nesta consulta.");
      if (!crowtadoCheck.value("last_success_at").toString().isEmpty())
        crowtadoDetails += QStringLiteral("\nÚltima verificação confirmada: ") + friendlyDate(crowtadoCheck.value("last_success_at").toString());
      if (!crowtadoCheck.value("last_restriction").toObject().isEmpty())
        crowtadoDetails += QStringLiteral("\nÚltima restrição registrada: ") + friendlyDate(crowtadoCheck.value("last_restriction").toObject().value("checked_at").toString());
      restrictionCell->setToolTip(crowtadoDetails);
      _accountsTable->setItem(row, 3, restrictionCell);
      auto* actions = new QWidget;
      auto* actionsLayout = new QHBoxLayout(actions);
      actionsLayout->setContentsMargins(5, 5, 5, 5);
      actionsLayout->setSpacing(7);
      auto* check = new QPushButton(QStringLiteral("Verificar"));
      check->setMinimumSize(86, 32);
      connect(check, &QPushButton::clicked, this, [this, email] {
        setAccountTransferBusy(true);
        _api.post(QStringLiteral("/api/accounts/") + encoded(email) + QStringLiteral("/check"), {},
          [this, email](bool ok, const QJsonDocument& doc, const QString& error) {
            setAccountTransferBusy(false);
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
                  check.value("issues").isArray() ? check.value("issues").toArray() : issue.isEmpty() ? QJsonArray{} : QJsonArray{issue});
            } else setStatus(QStringLiteral("Acesso de %1 verificado agora.").arg(email));
            loadAccounts();
          });
      });
      auto* remove = new QPushButton(QStringLiteral("Remover"));
      remove->setMinimumSize(86, 32);
      connect(remove, &QPushButton::clicked, this, [this, email] {
        removeAccount(email);
      });
      auto* reveal = credentialCopyActions(email, account.value(QStringLiteral("has_password")).toBool(), false);
      auto* menuButton = new QPushButton(QStringLiteral("Ações"));
      auto* menu = new QMenu(menuButton);
      menu->addAction(QStringLiteral("Ver detalhes"), this, [this, account] { showAccountDetails(account); });
      auto* verify = menu->addAction(QStringLiteral("Verificar Minute e Crowtado"), check, &QPushButton::click);
      verify->setEnabled(check->isEnabled());
      check->setParent(menuButton); check->hide();
      if (!registrationState.isEmpty() && registrationState != "complete") {
        menu->addAction(QStringLiteral("Retomar cadastro desta conta"), this, [this, email] { resumeAccount(email); });
      }
      menu->addAction(QStringLiteral("Reconectar com senha"), this, [this, email] {
        _accountEmail->setText(email); _accountPassword->clear();
        _pages->widget(5)->findChild<QTabWidget*>()->setCurrentIndex(1);
        _accountPassword->setFocus();
      });
      auto* credentialsAction = new QWidgetAction(menu);
      credentialsAction->setDefaultWidget(reveal); menu->addAction(credentialsAction);
      menu->addSeparator();
      menu->addAction(QStringLiteral("Remover deste computador"), remove, &QPushButton::click);
      remove->setParent(menuButton); remove->hide();
      menuButton->setMenu(menu);
      actionsLayout->addWidget(menuButton);
      _accountsTable->setCellWidget(row, 4, actions);
      if (selected.contains(email)) for (int col=0; col<4; ++col) _accountsTable->item(row,col)->setSelected(true);
      ++row;
    }
    setAccountTransferBusy(_accountTransferBusy);
    _accountsTable->verticalScrollBar()->setValue(scroll);
    filterAccounts();
}

void MainWindow::filterAccounts() {
  if (!_accountsSearch || !_accountsAttention) return;
  int visible = 0, pending = 0;
  const QString query = _accountsSearch->text().trimmed();
  for (int row=0; row<_accountsSnapshot.size(); ++row) {
    const auto account = _accountsSnapshot.at(row).toObject();
    const QString registration = account.value("registration").toObject().value("state").toString();
    const auto restriction = account.value("restriction").toObject();
    const bool attention = (!registration.isEmpty() && registration != "complete")
        || account.value("last_check").toObject().value("status").toString() != "active"
        || (account.value("account_kind").toString() != "claru" && restriction.value("code").toString() != "clear");
    pending += attention;
    const bool match = QString::fromUtf8(QJsonDocument(account).toJson(QJsonDocument::Compact)).contains(query,Qt::CaseInsensitive)
        && (!_accountsAttention->isChecked() || attention);
    _accountsTable->setRowHidden(row,!match); visible += match;
  }
  if (_accountsAvailable) _accountsSummary->setText(QStringLiteral("%1 de %2 contas · %3 requerem atenção").arg(visible).arg(_accountsSnapshot.size()).arg(pending));
}

void MainWindow::showAccountDetails(const QJsonObject& account) {
  auto* dialog = new QDialog(this);
  dialog->setAttribute(Qt::WA_DeleteOnClose);
  dialog->setWindowTitle(QStringLiteral("Detalhes da conta"));
  dialog->resize(680, 480);
  auto* layout = new QVBoxLayout(dialog);
  auto* text = new QPlainTextEdit;
  text->setReadOnly(true);
  QStringList lines{account.value("email").toString(), account.value("org_name").toString(),
    account.value("last_check").toObject().value("status_label").toString(),
    account.value("last_check").toObject().value("error").toString(),
    account.value("restriction").toObject().value("reason").toString(), QStringLiteral("\nEtapas do cadastro:")};
  const auto health = _accountChecks.value(account.value("email").toString(), account.value("last_check").toObject());
  const auto providers = health.value("providers").toObject();
  for (const auto& name : QStringList{"minute", "crowtado"}) {
    const auto check = name == "minute" && providers.isEmpty() ? health : providers.value(name).toObject();
    const auto prior = check.value("last_restriction").toObject();
    lines.insert(lines.size()-1, QStringLiteral("\n%1: %2\nVerificada em: %3\n%4\nÚltima restrição: %5\n%6").arg(
        name == "minute" ? QStringLiteral("Minute") : QStringLiteral("Crowtado"),
        check.value("status_label").toString(QStringLiteral("Não verificada")),
        friendlyDate(check.value("checked_at").toString()), check.value("error").toString(),
        friendlyDate(prior.value("checked_at").toString()), prior.value("issue").toObject().value("reason").toString()));
  }
  const auto registration = account.value("registration").toObject();
  const auto steps = registration.value("steps").toObject();
  const QStringList keys{"proxy","ban_check","save_partial","crowtado_signup","demographics","minute_register","link_minute","validate"};
  const QStringList names{QStringLiteral("Proxy / IP de saída"),QStringLiteral("Verificação de restrições"),QStringLiteral("Proteção da credencial"),QStringLiteral("Criação Crowtado"),
      QStringLiteral("Confirmações manuais no site"),QStringLiteral("Registro Minute"),QStringLiteral("Vínculo Crowtado / Minute"),QStringLiteral("Validação final")};
  for (int i=0; i<keys.size(); ++i) {
    const auto& key=keys.at(i);
    const auto step=steps.value(key).toObject();
    const QString status=step.value("status").toString();
    lines << QStringLiteral("%1 · %2\n%3").arg(names.at(i),status=="ok"?QStringLiteral("Confirmada")
      :status=="skip"?QStringLiteral("Já confirmada"):status=="manual"?QStringLiteral("Pendente no site"):status=="fail"?QStringLiteral("Não concluída"):QStringLiteral("Não executada"),step.value("detail").toString());
  }
  lines << registration.value("error").toString();
  text->setPlainText(lines.join(QStringLiteral("\n")));
  layout->addWidget(text);
  auto* close=new QDialogButtonBox(QDialogButtonBox::Close);
  connect(close,&QDialogButtonBox::rejected,dialog,&QDialog::close);
  layout->addWidget(close); dialog->open();
}

QWidget* MainWindow::credentialCopyActions(const QString& email, bool hasPassword, bool banned) {
  auto* widget = new QWidget;
  auto* layout = new QHBoxLayout(widget);
  layout->setContentsMargins(0, 0, 0, 0);
  layout->setSpacing(7);
  auto* copyEmail = new QPushButton(QStringLiteral("Copiar e-mail"), widget);
  auto* copyPassword = new QPushButton(QStringLiteral("Copiar senha"), widget);
  auto* viewCredentials = new QPushButton(QStringLiteral("Ver acesso"), widget);
  copyEmail->setObjectName(QStringLiteral("copyEmailButton"));
  copyPassword->setObjectName(QStringLiteral("copyPasswordButton"));
  viewCredentials->setObjectName(QStringLiteral("viewCredentialsButton"));
  copyEmail->setMinimumHeight(32);
  copyPassword->setMinimumHeight(32);
  viewCredentials->setMinimumHeight(32);
  copyEmail->setAccessibleName(QStringLiteral("Copiar e-mail de %1").arg(email));
  copyPassword->setAccessibleName(QStringLiteral("Copiar senha de %1").arg(email));
  viewCredentials->setAccessibleName(QStringLiteral("Ver e-mail e senha de %1").arg(email));
  copyPassword->setEnabled(hasPassword);
  viewCredentials->setEnabled(hasPassword);
  copyPassword->setToolTip(hasPassword
      ? QStringLiteral("Copiar a senha salva sem exibi-la na tela.")
      : QStringLiteral("Esta conta não possui senha salva."));
  viewCredentials->setToolTip(hasPassword
      ? QStringLiteral("Mostrar o e-mail e a senha salva desta conta.")
      : QStringLiteral("Esta conta não possui senha salva."));
  layout->addWidget(copyEmail);
  layout->addWidget(copyPassword);
  layout->addWidget(viewCredentials);
  const auto feedback = [this](QPushButton* button, const QString& label, const QString& message) {
    button->setText(QStringLiteral("Copiado!"));
    setStatus(message);
    QTimer::singleShot(2000, button, [button, label] { button->setText(label); });
  };
  connect(copyEmail, &QPushButton::clicked, widget, [email, copyEmail, feedback] {
    QApplication::clipboard()->setText(email);
    feedback(copyEmail, QStringLiteral("Copiar e-mail"), QStringLiteral("E-mail copiado."));
  });
  connect(copyPassword, &QPushButton::clicked, widget, [this, email, banned, copyPassword, feedback] {
    copyPassword->setEnabled(false);
    copyPassword->setText(QStringLiteral("Copiando…"));
    const QPointer<QPushButton> guard(copyPassword);
    _api.post(banned ? QStringLiteral("/api/accounts/banned/password")
                     : QStringLiteral("/api/accounts/password"),
              {{QStringLiteral("email"), email}},
              [this, email, guard, feedback](bool ok, const QJsonDocument& doc, const QString&) {
      if (!guard) return;
      guard->setEnabled(true);
      const auto record = doc.object();
      const auto ownerValue = record.value(QStringLiteral("email"));
      const QString savedEmail = ownerValue.toString().trimmed();
      const QString password = record.value(QStringLiteral("password")).toString();
      if (!ok || !ownerValue.isString() || savedEmail.isEmpty() || password.isEmpty()
          || savedEmail.compare(email.trimmed(), Qt::CaseInsensitive) != 0) {
        guard->setText(QStringLiteral("Copiar senha"));
        setStatus(QStringLiteral("Não foi possível copiar a senha. Tente novamente."));
        return;
      }
      QApplication::clipboard()->setText(password);
      feedback(guard, QStringLiteral("Copiar senha"), QStringLiteral("Senha copiada."));
    });
  });
  connect(viewCredentials, &QPushButton::clicked, widget, [this, email, banned, viewCredentials] {
    viewCredentials->setEnabled(false);
    viewCredentials->setText(QStringLiteral("Abrindo…"));
    const QPointer<QPushButton> guard(viewCredentials);
    _api.post(banned ? QStringLiteral("/api/accounts/banned/password")
                     : QStringLiteral("/api/accounts/password"),
              {{QStringLiteral("email"), email}},
              [this, email, guard](bool ok, const QJsonDocument& doc, const QString&) {
      if (!guard) return;
      guard->setEnabled(true);
      guard->setText(QStringLiteral("Ver acesso"));
      const auto record = doc.object();
      const QString savedEmail = record.value(QStringLiteral("email")).toString().trimmed();
      const QString password = record.value(QStringLiteral("password")).toString();
      if (!ok || password.isEmpty() || savedEmail.compare(email.trimmed(), Qt::CaseInsensitive) != 0) {
        setStatus(QStringLiteral("Não foi possível consultar a senha salva desta conta."));
        return;
      }
      showSavedAccountCredentials(savedEmail, password);
    });
  });
  return widget;
}

void MainWindow::showSavedAccountCredentials(const QString& email, const QString& password) {
  auto* dialog = new QDialog(this);
  dialog->setObjectName(QStringLiteral("savedAccountCredentialsDialog"));
  dialog->setWindowTitle(QStringLiteral("Acesso salvo"));
  dialog->setAttribute(Qt::WA_DeleteOnClose);
  dialog->setWindowModality(Qt::WindowModal);
  dialog->resize(540, 240);
  auto* layout = new QVBoxLayout(dialog);
  layout->setContentsMargins(24, 20, 24, 20);
  layout->setSpacing(14);
  auto* help = new QLabel(QStringLiteral("Você pode selecionar os campos para copiar."), dialog);
  help->setWordWrap(true);
  layout->addWidget(help);
  auto* form = new QFormLayout;
  form->setSpacing(12);
  auto* emailField = new QLineEdit(email, dialog);
  emailField->setObjectName(QStringLiteral("savedAccountEmail"));
  emailField->setReadOnly(true);
  emailField->setAccessibleName(QStringLiteral("E-mail salvo"));
  auto* passwordField = new QLineEdit(password, dialog);
  passwordField->setObjectName(QStringLiteral("savedAccountPassword"));
  passwordField->setReadOnly(true);
  passwordField->setEchoMode(QLineEdit::Normal);
  passwordField->setAccessibleName(QStringLiteral("Senha salva"));
  form->addRow(QStringLiteral("E-mail"), emailField);
  form->addRow(QStringLiteral("Senha"), passwordField);
  layout->addLayout(form);
  auto* hidePassword = new QCheckBox(QStringLiteral("Ocultar senha"), dialog);
  hidePassword->setObjectName(QStringLiteral("hideSavedAccountPassword"));
  connect(hidePassword, &QCheckBox::toggled, passwordField, [passwordField](bool hide) {
    passwordField->setEchoMode(hide ? QLineEdit::Password : QLineEdit::Normal);
  });
  layout->addWidget(hidePassword);
  auto* buttons = new QDialogButtonBox(QDialogButtonBox::Close, dialog);
  buttons->button(QDialogButtonBox::Close)->setText(QStringLiteral("Fechar"));
  connect(buttons, &QDialogButtonBox::rejected, dialog, &QDialog::reject);
  connect(dialog, &QDialog::finished, dialog, [emailField, passwordField] {
    emailField->clear();
    passwordField->clear();
  });
  layout->addWidget(buttons);
  dialog->open();
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
  if (!_backendReady || _accountsCheckRunning || _accountTransferBusy || _bulkRegisterPolling || _bulkRegisterStarting) return;
  _accountsCheckRunning = true;
  setAccountTransferBusy(_accountTransferBusy);
  _accountsCheckAll->setEnabled(false);
  _accountsCheckAll->setText(QStringLiteral("Verificando…"));
  setStatus(QStringLiteral("Verificando todas as contas em paralelo…"));
  _api.post(QStringLiteral("/api/accounts/check-all"), {},
            [this](bool ok, const QJsonDocument& doc, const QString& error) {
    _accountsCheckAll->setText(QStringLiteral("Verificar todas"));
    _accountsCheckRunning = false;
    setAccountTransferBusy(_accountTransferBusy);
    if (!ok) {
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
        if (check.value("issues").isArray()) {
          for (const auto& item : check.value("issues").toArray()) issues.append(item);
        } else if (!issue.isEmpty()) issues.append(issue);
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
  if (!_backendReady || _accountConnecting || _accountTransferBusy || _bulkRegisterPolling || _bulkRegisterStarting
      || _bulkRegisterOutcomeUnknown || _orgMigrationRunning || _accountsCheckRunning || !_accountsAvailable) return;
  const QString email = _accountEmail->text().trimmed();
  const QString password = _accountPassword->text();
  if (email.isEmpty() || password.isEmpty()) {
    return showError(QStringLiteral("Dados incompletos"), QStringLiteral("Informe email e senha."));
  }
  const QString endpoint = registerNew ? QStringLiteral("/api/accounts/register?async=1")
                                       : QStringLiteral("/api/accounts");
  _accountConnecting = true;
  setAccountTransferBusy(_accountTransferBusy);
  _accountAdd->setEnabled(false);
  _accountRegister->setEnabled(false);
  QJsonObject body{{QStringLiteral("email"), email}, {QStringLiteral("password"), password}};
  if (registerNew) {
    body.insert("request_id", QUuid::createUuid().toString(QUuid::WithoutBraces));
    body.insert("proxy_id", _accountProxy->currentData().toString());
    body.insert("use_referral", _accountUseReferral->isChecked());
    _bulkRegisterPendingRequestId = body.value("request_id").toString();
    _bulkRegisterPendingPath = endpoint;
    _bulkRegisterPendingBody = body;
    _bulkRegisterPendingManualRegistration = true;
    _bulkRegisterPendingClearResults = false;
    _bulkRegisterPendingTotal = 1;
    _bulkRegisterOwnRequestAccepted = false;
    _bulkRegisterOutcomeUnknown = false;
    _bulkRegisterPreflightReady = false;
    ++_bulkRegisterPreflightRevision;
    _bulkRegisterStarting = true;
    _pages->widget(5)->findChild<QTabWidget*>()->setCurrentIndex(2);
    setStatus(QStringLiteral("Registrando %1 — fluxo completo (Crowtado + Minute)… pode levar alguns minutos.")
                  .arg(email));
    submitPendingRegistrationRequest();
    return;
  }
  const int generation = _operationBackendGeneration;
  _api.post(endpoint, body, [this, email, generation](bool ok, const QJsonDocument&, const QString& error) {
    if (!_backendReady || generation != _operationBackendGeneration) return;
    _accountConnecting = false;
    setAccountTransferBusy(_accountTransferBusy);
    if (!ok) return showError(QStringLiteral("Conta não conectada"), error);
    _accountChecks.remove(email);
    _accountEmail->clear();
    _accountPassword->clear();
    setStatus(QStringLiteral("Conta conectada."));
    loadAccounts();
  });
}

void MainWindow::loadRegistrationProxies(bool selectAuto, bool preserveStatus) {
  if (!_backendReady || _bulkRegisterPolling || _bulkRegisterStarting || _accountTransferBusy) return;
  _bulkRegisterPreflightReady = false;
  const int revision=++_registrationProxiesRevision, generation=_operationBackendGeneration;
  _api.get(QStringLiteral("/api/accounts/proxies"), [this,revision,generation,selectAuto,preserveStatus](bool ok,const QJsonDocument& doc,const QString& error) {
    if (!_backendReady || generation!=_operationBackendGeneration || revision!=_registrationProxiesRevision
        || _bulkRegisterPolling || _bulkRegisterStarting) return;
    const auto rows=doc.object().value("proxies");
    _registrationProxiesReady=ok && rows.isArray();
    if (!_registrationProxiesReady) {
      const QString prefix = preserveStatus ? _bulkRegisterStatus->text().section('\n',0,0) + QStringLiteral("\n") : QString();
      _bulkRegisterStatus->setText(prefix + QStringLiteral("Não foi possível carregar os proxies. Atualize antes de criar contas. ")+error);
      setAccountTransferBusy(_accountTransferBusy); return;
    }
    for (auto* combo : {_accountProxy,_bulkRegisterProxy}) {
      const QSignalBlocker block(combo);
      const auto previous=combo->currentData().toString();
      const bool initial=combo->count()==1 && combo->currentText().contains(QStringLiteral("Carregando"));
      combo->clear(); combo->addItem(QStringLiteral("Sem proxy"),QString());
      if (!rows.toArray().isEmpty()) combo->addItem(QStringLiteral("Automático · alternar por conta"),QStringLiteral("auto"));
      for (const auto value : rows.toArray()) {
        const auto row=value.toObject();
        if (!row.value("id").isString() || !row.value("label").isString()) {
          _registrationProxiesReady=false; break;
        }
        combo->addItem(row.value("label").toString(),row.value("id").toString());
      }
      const auto selected=((selectAuto || initial) && !rows.toArray().isEmpty()) ? QStringLiteral("auto") : previous;
      combo->setCurrentIndex(qMax(0,combo->findData(selected)));
    }
    setAccountTransferBusy(_accountTransferBusy); checkBulkRegisterDomain(preserveStatus);
  });
}

void MainWindow::importRegistrationProxiesFile(const QString& path) {
  if (!_backendReady || _bulkRegisterPolling || _bulkRegisterStarting || _accountTransferBusy || _accountConnecting) return;
  QFile file(path);
  if (!file.open(QIODevice::ReadOnly) || file.size()>256*1024) {
    showError(QStringLiteral("Importação não concluída"),QStringLiteral("Selecione um TXT acessível de até 256 KB.")); return;
  }
  const auto bytes=file.readAll();
  if (!bytes.isValidUtf8()) {
    showError(QStringLiteral("Importação não concluída"),QStringLiteral("Salve o arquivo TXT em UTF-8.")); return;
  }
  ++_registrationProxiesRevision;
  const int generation=_operationBackendGeneration;
  setAccountTransferBusy(true);
  _bulkRegisterStart->setEnabled(false);
  _api.post(QStringLiteral("/api/accounts/proxies/import"),{{"text",QString::fromUtf8(bytes)}},
    [this,generation](bool ok,const QJsonDocument& doc,const QString& error) {
      if (!_backendReady || generation!=_operationBackendGeneration) return;
      setAccountTransferBusy(false);
      if (!ok) {loadRegistrationProxies();showError(QStringLiteral("Importação não concluída"),error);return;}
      setStatus(QStringLiteral("%1 proxies disponíveis. Login e senha armazenados no cofre local.").arg(doc.object().value("total").toInt()));
      loadRegistrationProxies(true);
    });
}

void MainWindow::loadBulkRegisterDomains(bool preserveStatus) {
  loadRegistrationProxies(false, preserveStatus);
  if (!_backendReady || _bulkRegisterPolling || _bulkRegisterStarting) return;
  const QString statusPrefix = preserveStatus ? _bulkRegisterStatus->text().section('\n',0,0) + QStringLiteral("\n") : QString();
  _bulkRegisterPreflightReady = false;
  const int revision = ++_bulkRegisterDomainsRevision;
  const QString selectedDomain = _bulkRegisterDomain->currentData().toString();
  ++_bulkRegisterPreflightRevision;
  const QSignalBlocker blocker(_bulkRegisterDomain);
  _bulkRegisterStart->setEnabled(false);
  _bulkRegisterDomain->clear();
  _bulkRegisterDomain->addItem(QStringLiteral("carregando domínios…"));
  _bulkRegisterDomain->setEnabled(false);
  _api.get(QStringLiteral("/api/accounts/domains"), [this, revision, selectedDomain, preserveStatus, statusPrefix](bool ok, const QJsonDocument& doc, const QString& error) {
    if (revision != _bulkRegisterDomainsRevision || _bulkRegisterPolling || _bulkRegisterStarting) return;
    const QSignalBlocker blocker(_bulkRegisterDomain);
    _bulkRegisterDomain->clear();
    _bulkRegisterWebmail->hide();
    if (!ok) {
      _bulkRegisterDomain->addItem(QStringLiteral("não foi possível carregar"));
      _bulkRegisterDomain->setEnabled(false);
      _bulkRegisterStatus->setText(statusPrefix + QStringLiteral("Falha ao carregar domínios: ") + error);
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
      _bulkRegisterStatus->setText(statusPrefix + (warning.isEmpty()
          ? QStringLiteral("Nenhum domínio disponível. Em Integrações, use Identificar e conectar com o token da API Mail da Hostinger; depois clique em Atualizar domínios.")
          : warning));
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
    checkBulkRegisterDomain(preserveStatus);
  });
}

void MainWindow::checkBulkRegisterDomain(bool preserveStatus) {
    const QString statusPrefix = preserveStatus ? _bulkRegisterStatus->text().section('\n',0,0) + QStringLiteral("\n") : QString();
    const QString domain = _bulkRegisterDomain->currentData().toString();
    const QString proxy = _bulkRegisterProxy->currentData().toString();
    _bulkRegisterPreflightReady = false;
    const int revision = ++_bulkRegisterPreflightRevision;
    updateBulkRegisterStartEnabled();
    if (!_backendReady || _bulkRegisterPolling || _bulkRegisterStarting || _bulkRegisterOutcomeUnknown
        || _accountTransferBusy || _orgMigrationRunning || _accountConnecting || _accountsCheckRunning
        || !_registrationProxiesReady || domain.isEmpty()) return;
    if (!preserveStatus)
      _bulkRegisterStatus->setText(QStringLiteral("Validando dependências (Hostinger, Chrome, APIs)…"));
    _api.get(QStringLiteral("/api/accounts/bulk-register/preflight?domain=")
                 + QString::fromLatin1(QUrl::toPercentEncoding(domain)) + QStringLiteral("&proxy_id=") + encoded(_bulkRegisterProxy->currentData().toString()),
             [this, revision, domain, proxy, statusPrefix](bool ok, const QJsonDocument& doc, const QString& error) {
      if (revision != _bulkRegisterPreflightRevision || _bulkRegisterPolling || _bulkRegisterStarting
          || _bulkRegisterOutcomeUnknown || domain != _bulkRegisterDomain->currentData().toString()
          || proxy != _bulkRegisterProxy->currentData().toString()) return;
      if (!ok) {
        _bulkRegisterPreflightReady = false;
        updateBulkRegisterStartEnabled();
        _bulkRegisterStatus->setText(statusPrefix + QStringLiteral("Preflight falhou: ") + error);
        return;
      }
      const auto root = doc.object();
      const bool ready = root.value(QStringLiteral("ready")).toBool();
      const auto checks = root.value(QStringLiteral("checks")).toObject();
      QStringList issues;
      const QStringList checkKeys = {
          QStringLiteral("hostinger"), QStringLiteral("chrome"),
          QStringLiteral("crowtado_api"), QStringLiteral("minute_api"),
          QStringLiteral("invite_code"),
      };
      const QStringList checkNames = {
          QStringLiteral("Hostinger"), QStringLiteral("Chrome"),
          QStringLiteral("Crowtado API"), QStringLiteral("Minute API"),
          QStringLiteral("Código de convite"),
      };
      bool checksReady = true;
      for (int i = 0; i < checkKeys.size(); ++i) {
        const auto checkValue = checks.value(checkKeys.at(i));
        const auto check = checkValue.toObject();
        if (!checkValue.isObject() || !check.value(QStringLiteral("ok")).isBool()
            || !check.value(QStringLiteral("ok")).toBool()) {
          checksReady = false;
          issues << QStringLiteral("%1: %2")
                        .arg(checkNames.at(i), check.value(QStringLiteral("detail")).toString());
        }
      }
      if (!proxy.isEmpty()) {
        const auto proxyValue = checks.value(QStringLiteral("proxy"));
        const auto proxyCheck = proxyValue.toObject();
        if (!proxyValue.isObject() || !proxyCheck.value(QStringLiteral("ok")).isBool()
            || !proxyCheck.value(QStringLiteral("ok")).toBool()) {
          checksReady = false;
          issues.prepend(QStringLiteral("Proxy: ") + proxyCheck.value(QStringLiteral("detail")).toString());
        }
      } else if (checks.contains("proxy") && !checks.value("proxy").toObject().value("ok").toBool()) {
        checksReady = false;
        issues.prepend(QStringLiteral("Proxy: ") + checks.value("proxy").toObject().value("detail").toString());
      }
      _bulkRegisterPreflightReady = ready && checksReady && _registrationProxiesReady;
      _bulkRegisterPreflightDomain = domain;
      _bulkRegisterPreflightProxy = proxy;
      if (_bulkRegisterPreflightReady) {
        _bulkRegisterStatus->setText(statusPrefix + QStringLiteral(
            "Pronto para criar Crowtado + Minute. Depois, conclua as etapas manuais no site Crowtado."));
      } else {
        _bulkRegisterStatus->setText(
            statusPrefix + QStringLiteral("Dependências com problema: %1").arg(issues.join(QStringLiteral("; "))));
      }
      updateBulkRegisterStartEnabled();
    });
}

void MainWindow::startBulkRegister() {
  if (!_backendReady || _bulkRegisterPolling || _bulkRegisterStarting || _bulkRegisterOutcomeUnknown
      || _accountTransferBusy || _orgMigrationRunning || _accountConnecting || _accountsCheckRunning
      || !_accountsAvailable || !_registrationProxiesReady || !_bulkRegisterPreflightReady
      || _bulkRegisterPreflightDomain != _bulkRegisterDomain->currentData().toString()
      || _bulkRegisterPreflightProxy != _bulkRegisterProxy->currentData().toString()) return;
  const QString domain = _bulkRegisterDomain->currentData().toString();
  if (domain.isEmpty()) {
    return showError(QStringLiteral("Domínio indisponível"),
                     QStringLiteral("Configure um domínio catch-all em Integrações."));
  }
  const int count = _bulkRegisterCount->value();
  _bulkRegisterStarting = true;
  setAccountTransferBusy(_accountTransferBusy);
  ++_bulkRegisterDomainsRevision;
  ++_bulkRegisterPreflightRevision;
  _bulkRegisterStart->setEnabled(false);
  _bulkRegisterDomain->setEnabled(false);
  _bulkRegisterCount->setEnabled(false);
  _bulkRegisterPendingRequestId = QUuid::createUuid().toString(QUuid::WithoutBraces);
  _bulkRegisterPendingPath = QStringLiteral("/api/accounts/bulk-register");
  _bulkRegisterPendingBody = {{QStringLiteral("proxy_id"), _bulkRegisterProxy->currentData().toString()},
      {QStringLiteral("count"), count}, {QStringLiteral("domain"), domain},
      {QStringLiteral("use_referral"), _bulkRegisterUseReferral->isChecked()},
      {QStringLiteral("request_id"), _bulkRegisterPendingRequestId}};
  _bulkRegisterPendingManualRegistration = false;
  _bulkRegisterPendingClearResults = true;
  _bulkRegisterPendingTotal = count;
  _bulkRegisterOwnRequestAccepted = false;
  _bulkRegisterOutcomeUnknown = false;
  _bulkRegisterPreflightReady = false;
  _bulkRegisterStatus->setText(QStringLiteral("Iniciando criação de %1 contas…").arg(count));
  submitPendingRegistrationRequest();
}

void MainWindow::submitPendingRegistrationRequest() {
  if (!_backendReady || _bulkRegisterPendingRequestId.isEmpty() || _bulkRegisterPendingPath.isEmpty()
      || _bulkRegisterPendingBody.isEmpty() || _bulkRegisterRequestInFlight) return;
  if (!_bulkRegisterBaselineReady) {
    _bulkRegisterStarting = true;
    _bulkRegisterRequestInFlight = true;
    setAccountTransferBusy(_accountTransferBusy);
    const int generation = _operationBackendGeneration;
    _api.get(QStringLiteral("/api/accounts/bulk-register/status"),
      [this, generation, expectedRequestId = _bulkRegisterPendingRequestId](bool ok, const QJsonDocument& doc, const QString& error) {
        if (!_backendReady || generation != _operationBackendGeneration
            || expectedRequestId != _bulkRegisterPendingRequestId) return;
        _bulkRegisterRequestInFlight = false;
        _bulkRegisterStarting = false;
        const auto root = doc.object();
        const QString state = root.value(QStringLiteral("state")).toString();
        const QString requestId = root.value(QStringLiteral("request_id")).toString();
        const bool knownState = state == QStringLiteral("idle") || state == QStringLiteral("running")
            || state == QStringLiteral("stopping") || state == QStringLiteral("done")
            || state == QStringLiteral("failed");
        if (!ok || !knownState) {
          const QString detail = !ok ? error : QStringLiteral("o estado atual não pôde ser identificado");
          _bulkRegisterPendingRequestId.clear();
          _bulkRegisterPendingPath.clear();
          _bulkRegisterPendingBody = {};
          _bulkRegisterPendingManualRegistration = false;
          _bulkRegisterPendingClearResults = false;
          _bulkRegisterStarting = false;
          _accountConnecting = false;
          _bulkRegisterDomain->setEnabled(true);
          _bulkRegisterCount->setEnabled(true);
          _bulkRegisterStatus->setText(QStringLiteral("Não enviei o cadastro porque não consegui confirmar o estado anterior do serviço. Os dados digitados foram mantidos. ") + detail);
          setStatus(_bulkRegisterStatus->text());
          setAccountTransferBusy(_accountTransferBusy);
          return;
        }
        if (state == QStringLiteral("running") || state == QStringLiteral("stopping")) {
          _bulkRegisterPendingRequestId.clear();
          _bulkRegisterPendingPath.clear();
          _bulkRegisterPendingBody = {};
          _bulkRegisterPendingManualRegistration = false;
          _bulkRegisterPendingClearResults = false;
          _bulkRegisterDomain->setEnabled(true);
          _bulkRegisterCount->setEnabled(true);
          _bulkRegisterStatus->setText(QStringLiteral("Já existe uma criação em andamento no serviço. Aguarde antes de iniciar outra."));
          setStatus(_bulkRegisterStatus->text());
          setAccountTransferBusy(_accountTransferBusy);
          beginRegistrationPolling();
          return;
        }
        _bulkRegisterBaselineState = state;
        _bulkRegisterBaselineRequestId = requestId;
        _bulkRegisterBaselineReady = true;
        _bulkRegisterBaselineInvalid = false;
        setAccountTransferBusy(_accountTransferBusy);
        submitPendingRegistrationRequest();
      });
    return;
  }
  const bool retry = _bulkRegisterRetryInFlight || _bulkRegisterOutcomeUnknown;
  _bulkRegisterRetryInFlight = retry;
  _bulkRegisterStarting = true;
  _bulkRegisterOutcomeUnknown = false;
  setAccountTransferBusy(_accountTransferBusy);
  const int generation = _operationBackendGeneration;
  const QString expectedRequestId = _bulkRegisterPendingRequestId;
  const QJsonObject body = _bulkRegisterPendingBody;
  const QString path = _bulkRegisterPendingPath;
  _api.post(path, body, [this, generation, expectedRequestId, retry](bool ok, const QJsonDocument& doc, const QString& error) {
    if (!_backendReady || generation != _operationBackendGeneration
        || expectedRequestId != _bulkRegisterPendingRequestId) return;
    _bulkRegisterStarting = false;
    _accountConnecting = false;
    _bulkRegisterRetryInFlight = false;
    const auto response = doc.object();
    const QString responseRequestId = response.value(QStringLiteral("request_id")).toString();
    if (ok && response.value(QStringLiteral("ok")).toBool()
        && responseRequestId == expectedRequestId) {
      acceptPendingRegistrationRequest();
      return;
    }
    const bool unknown = response.value(QStringLiteral("error_code")).toString()
        == QStringLiteral("request_outcome_unknown");
    if (response.value(QStringLiteral("not_admitted")).toBool()) {
      rejectPendingRegistrationRequest(error.isEmpty()
          ? response.value(QStringLiteral("error")).toString(QStringLiteral("O serviço confirmou que não iniciou o cadastro."))
          : error);
      return;
    }
    if (unknown || retry) {
      _bulkRegisterOutcomeUnknown = true;
      setAccountTransferBusy(_accountTransferBusy);
      _bulkRegisterStatus->setText(unknown
          ? QStringLiteral("A resposta do cadastro não chegou. Mantive os dados e vou confirmar a solicitação antes de continuar.")
          : QStringLiteral("A solicitação ainda não foi confirmada. Mantive os dados e vou consultar o serviço antes de tentar novamente."));
      setStatus(_bulkRegisterStatus->text());
      if (!_bulkRegisterPolling) beginRegistrationPolling();
      else _bulkRegisterPoll.setInterval(3000);
      return;
    }
    rejectPendingRegistrationRequest(error.isEmpty()
        ? response.value(QStringLiteral("error")).toString(QStringLiteral("O serviço recusou a solicitação."))
        : error);
  });
}

void MainWindow::acceptPendingRegistrationRequest() {
  if (_bulkRegisterPendingRequestId.isEmpty()) return;
  _bulkRegisterOwnRequestAccepted = true;
  _bulkRegisterOutcomeUnknown = false;
  _bulkRegisterRetryInFlight = false;
  _bulkRegisterStarting = false;
  _accountConnecting = false;
  if (_bulkRegisterPendingManualRegistration
      && _accountPassword->text() == _bulkRegisterPendingBody.value(QStringLiteral("password")).toString())
    _accountPassword->clear();
  if (_bulkRegisterPendingClearResults) {
    _bulkRegisterTable->setRowCount(0);
    _bulkRegisterResults = {};
    _bulkRegisterProgress->setRange(0, _bulkRegisterPendingTotal);
    _bulkRegisterProgress->setValue(0);
  }
  _bulkRegisterPendingPath.clear();
  _bulkRegisterPendingBody = {};
  _bulkRegisterPendingManualRegistration = false;
  _bulkRegisterPendingClearResults = false;
  setAccountTransferBusy(_accountTransferBusy);
  if (!_bulkRegisterPolling) beginRegistrationPolling();
}

void MainWindow::rejectPendingRegistrationRequest(const QString& error) {
  QString safeError = error;
  const QString pendingPassword = _bulkRegisterPendingBody.value(QStringLiteral("password")).toString();
  if (!pendingPassword.isEmpty()) safeError.replace(pendingPassword, QStringLiteral("[oculto]"));
  _bulkRegisterPoll.stop();
  _bulkRegisterPolling = false;
  _bulkRegisterStarting = false;
  _bulkRegisterOutcomeUnknown = false;
  _bulkRegisterOwnRequestAccepted = false;
  _bulkRegisterRetryInFlight = false;
  _accountConnecting = false;
  _bulkRegisterPendingRequestId.clear();
  _bulkRegisterPendingPath.clear();
  _bulkRegisterPendingBody = {};
  _bulkRegisterPendingManualRegistration = false;
  _bulkRegisterPendingClearResults = false;
  _bulkRegisterBaselineReady = false;
  _bulkRegisterBaselineInvalid = false;
  _bulkRegisterBaselineState.clear();
  _bulkRegisterBaselineRequestId.clear();
  _bulkRegisterPreflightReady = false;
  _bulkRegisterDomain->setEnabled(true);
  _bulkRegisterCount->setEnabled(true);
  _bulkRegisterStatus->setText(QStringLiteral("Solicitação recusada: ") + safeError);
  setStatus(_bulkRegisterStatus->text());
  setAccountTransferBusy(_accountTransferBusy);
}

void MainWindow::retryPendingRegistrationRequest() {
  if (_bulkRegisterPendingRequestId.isEmpty() || _bulkRegisterOwnRequestAccepted
      || _bulkRegisterStarting || _bulkRegisterRetryInFlight || !_backendReady
      || !_bulkRegisterBaselineReady || _bulkRegisterBaselineInvalid) return;
  _bulkRegisterRetryInFlight = true;
  submitPendingRegistrationRequest();
}

void MainWindow::beginRegistrationPolling() {
  if (!_backendReady || _bulkRegisterStarting || _bulkRegisterRequestInFlight) return;
  _bulkRegisterPolling = true;
  ++_bulkRegisterDomainsRevision; ++_bulkRegisterPreflightRevision;
  _bulkRegisterStart->setEnabled(false);
  _bulkRegisterDomain->setEnabled(false); _bulkRegisterCount->setEnabled(false);
  setAccountTransferBusy(_accountTransferBusy);
  _bulkRegisterPoll.start();
  pollBulkRegister();
}

void MainWindow::resumeAccount(const QString& email) {
  if (!_backendReady || _bulkRegisterPolling || _bulkRegisterStarting || _bulkRegisterOutcomeUnknown
      || _accountTransferBusy || _accountConnecting || _orgMigrationRunning || _accountsCheckRunning
      || !_accountsAvailable) return;
  _bulkRegisterPendingRequestId = QUuid::createUuid().toString(QUuid::WithoutBraces);
  _bulkRegisterPendingPath = QStringLiteral("/api/accounts/") + encoded(email) + QStringLiteral("/resume");
  _bulkRegisterPendingBody = {{QStringLiteral("request_id"), _bulkRegisterPendingRequestId}};
  _bulkRegisterPendingManualRegistration = false;
  _bulkRegisterPendingClearResults = false;
  _bulkRegisterPendingTotal = 1;
  _bulkRegisterOwnRequestAccepted = false;
  _bulkRegisterOutcomeUnknown = false;
  _bulkRegisterPreflightReady = false;
  _bulkRegisterStarting = true;
  ++_bulkRegisterDomainsRevision; ++_bulkRegisterPreflightRevision;
  _bulkRegisterStart->setEnabled(false);
  setAccountTransferBusy(_accountTransferBusy);
  _pages->widget(5)->findChild<QTabWidget*>()->setCurrentIndex(2);
  submitPendingRegistrationRequest();
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
  if (!_backendReady || _bulkRegisterRequestInFlight || _bulkRegisterStarting) return;
  _bulkRegisterRequestInFlight = true;
  const int generation = _operationBackendGeneration;
  _api.get(QStringLiteral("/api/accounts/bulk-register/status"),
           [this,generation](bool ok, const QJsonDocument& doc, const QString& error) {
    if (!_backendReady || generation != _operationBackendGeneration) return;
    _bulkRegisterRequestInFlight = false;
    if (!ok) {
      _bulkRegisterPoll.setInterval(3000);
      _bulkRegisterStatus->setText(QStringLiteral("Conexão interrompida. Tentando recuperar o progresso… ") + error);
      setStatus(_bulkRegisterStatus->text());
      return;
    }
    _bulkRegisterPoll.setInterval(900);
    const auto root = doc.object();
    const QString state = root.value(QStringLiteral("state")).toString();
    if (state != "idle" && state != "running" && state != "stopping" && state != "done" && state != "failed") {
      _bulkRegisterStatus->setText(QStringLiteral("Progresso inválido · dados anteriores preservados. Tentando recuperar…"));
      setStatus(_bulkRegisterStatus->text());
      _bulkRegisterPoll.setInterval(3000); return;
    }
    const QString statusRequestId = root.value(QStringLiteral("request_id")).toString();
    if (!_bulkRegisterPendingRequestId.isEmpty()) {
      if (statusRequestId != _bulkRegisterPendingRequestId) {
        _bulkRegisterStop->setEnabled(false);
        _bulkRegisterPoll.setInterval(3000);
        const bool unchangedBaseline = !_bulkRegisterOwnRequestAccepted && _bulkRegisterBaselineReady
            && !_bulkRegisterBaselineInvalid && state == _bulkRegisterBaselineState
            && statusRequestId == _bulkRegisterBaselineRequestId
            && (state == QStringLiteral("idle")
                || ((state == QStringLiteral("done") || state == QStringLiteral("failed"))
                    && !_bulkRegisterBaselineRequestId.isEmpty()));
        if (unchangedBaseline) {
          _bulkRegisterStatus->setText(QStringLiteral("O estado do serviço permanece igual ao observado antes do envio. Reenviando o mesmo identificador para confirmar com segurança…"));
          setStatus(_bulkRegisterStatus->text());
          retryPendingRegistrationRequest();
        } else if (!_bulkRegisterOwnRequestAccepted) {
          _bulkRegisterBaselineInvalid = true;
          _bulkRegisterStatus->setText(QStringLiteral("O estado do serviço mudou desde antes do envio e não corresponde a esta solicitação. Mantive os dados bloqueados; não vou reenviar automaticamente."));
          setStatus(_bulkRegisterStatus->text());
        } else {
          _bulkRegisterStatus->setText(QStringLiteral("O serviço ainda não confirmou o resultado deste cadastro. Mantive os dados bloqueados e continuarei consultando sem misturar outro lote."));
          setStatus(_bulkRegisterStatus->text());
        }
        return;
      }
      if (!_bulkRegisterOwnRequestAccepted) acceptPendingRegistrationRequest();
    }
    if (state == QStringLiteral("idle")) {
      if (!_bulkRegisterPendingRequestId.isEmpty()) {
        _bulkRegisterPoll.setInterval(3000);
        _bulkRegisterStatus->setText(QStringLiteral("O serviço ainda não confirmou o resultado deste cadastro. Mantive os dados bloqueados e continuarei consultando."));
        setStatus(_bulkRegisterStatus->text());
        return;
      }
      _bulkRegisterPoll.stop();
      _bulkRegisterPolling = false;
      _bulkRegisterStop->setEnabled(false);
      _bulkRegisterDomain->setEnabled(true);
      _bulkRegisterCount->setEnabled(true);
      _bulkRegisterStatus->setText(QStringLiteral(
          "O serviço não possui um lote em andamento. Confira as contas salvas antes de iniciar outro cadastro."));
      setStatus(_bulkRegisterStatus->text());
      loadAccounts();
      _bulkRegisterPreflightReady = false;
      setAccountTransferBusy(_accountTransferBusy);
      if (!_registrationProxiesReady || _bulkRegisterDomain->currentData().toString().isEmpty())
        loadBulkRegisterDomains();
      else
        checkBulkRegisterDomain();
      return;
    }
    const int total = root.value(QStringLiteral("total")).toInt();
    const int completed = root.value(QStringLiteral("completed")).toInt();
    const int created = root.value(QStringLiteral("created")).toInt();
    const int failed = root.value(QStringLiteral("failed")).toInt();
    const QString current = root.value(QStringLiteral("current_email")).toString();
    const QString currentStep = root.value(QStringLiteral("current_step")).toString();
    const auto results = root.value(QStringLiteral("results")).toArray();
    const auto validCount = [](const QJsonValue& value) { return value.isDouble() && value.toDouble() >= 0
        && value.toDouble() <= 50 && std::floor(value.toDouble()) == value.toDouble(); };
    if (!validCount(root.value("total")) || !validCount(root.value("completed"))
        || !validCount(root.value("created")) || !validCount(root.value("failed"))
        || !root.value("results").isArray()
        || completed > total || created + failed != completed || results.size() != completed) {
      _bulkRegisterStatus->setText(QStringLiteral("Progresso inconsistente · dados anteriores preservados."));
      setStatus(_bulkRegisterStatus->text()); return;
    }
    QSet<QString> resultOwners;
    int confirmedCreated = 0;
    bool validResults = true;
    for (const auto& value : results) {
      const auto item = value.toObject();
      const QString email = item.value("email").toString().trimmed().toLower();
      const auto errorValue = item.value("error");
      if (!value.isObject() || email.isEmpty() || !email.contains('@') || resultOwners.contains(email)
          || !item.value("created").isBool() || !item.value("steps").isObject()
          || !(errorValue.isNull() || errorValue.isString())
          || (item.value("created").toBool() && !errorValue.isNull())
          || (!item.value("created").toBool() && errorValue.toString().trimmed().isEmpty())) {
        validResults = false; break;
      }
      resultOwners.insert(email);
      if (item.value("created").toBool()) ++confirmedCreated;
    }
    if (!validResults || confirmedCreated != created) {
      _bulkRegisterStatus->setText(QStringLiteral("Resultados inconsistentes · dados anteriores preservados."));
      setStatus(_bulkRegisterStatus->text());
      _bulkRegisterPoll.setInterval(3000); return;
    }
    _bulkRegisterProgress->setRange(0, qMax(1, total));
    _bulkRegisterProgress->setValue(completed);
    _bulkRegisterStop->setEnabled(state == "running");
    _bulkRegisterResults = results;
    const bool terminal = state == QStringLiteral("done") || state == QStringLiteral("failed");
    _bulkRegisterTable->setRowCount(results.size());
    for (int row = 0; row < results.size(); ++row) {
      const auto item = results.at(row).toObject();
      auto* emailItem = cell(item.value(QStringLiteral("email")).toString());
      _bulkRegisterTable->setItem(row, 0, emailItem);
      _bulkRegisterTable->setItem(row, 1, cell((item.value("nome").toString() + QStringLiteral(" ") + item.value("sobrenome").toString()).trimmed()));
      // Construir tooltip com todas as etapas
      QStringList stepLines;
      const auto steps = item.value(QStringLiteral("steps")).toObject();
      const QStringList stepKeys = {
          QStringLiteral("proxy"), QStringLiteral("ban_check"), QStringLiteral("save_partial"),
          QStringLiteral("crowtado_signup"), QStringLiteral("demographics"),
          QStringLiteral("minute_register"), QStringLiteral("link_minute"),
          QStringLiteral("validate"),
      };
      const QStringList stepNames = {
          QStringLiteral("Proxy"), QStringLiteral("Verificação"), QStringLiteral("Credenciais"),
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
      } else if (item.value("created").toBool() && errorText.isEmpty()) {
        outcome = QStringLiteral("✓ criada · site manual");
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
        outcome = item.value("partial").toBool() ? QStringLiteral("Cadastro incompleto · retomar") : failedStep.isEmpty()
            ? QStringLiteral("✗ ") + errorText
            : QStringLiteral("✗ falhou em: ") + failedStep;
      }
      auto* outcomeItem = cell(outcome);
      outcomeItem->setToolTip(stepLines.join(QStringLiteral("\n")));
      _bulkRegisterTable->setItem(row, 2, outcomeItem);
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
      auto* menuButton = new QPushButton(QStringLiteral("Ações"));
      auto* menu = new QMenu(menuButton);
      menu->addAction(QStringLiteral("Ver etapas e diagnóstico"),this,[this,item] {
        showAccountDetails({{"email",item.value("email")},{"registration",QJsonObject{{"steps",item.value("steps")},{"error",item.value("error")}}}});
      });
      if (terminal && item.value("partial").toBool() && !removed)
        menu->addAction(QStringLiteral("Retomar esta conta"),this,[this,item] {resumeAccount(item.value("email").toString());});
      auto* removeAction=menu->addAction(QStringLiteral("Remover deste computador"),remove,&QPushButton::click);
      removeAction->setEnabled(remove->isEnabled());remove->setParent(menuButton);remove->hide();
      menuButton->setMenu(menu);
      _bulkRegisterTable->setCellWidget(row, 3, menuButton);
    }
    if (terminal) {
      if (!_bulkRegisterPendingRequestId.isEmpty() && statusRequestId == _bulkRegisterPendingRequestId) {
        _bulkRegisterPendingRequestId.clear();
        _bulkRegisterOwnRequestAccepted = false;
        _bulkRegisterBaselineReady = false;
        _bulkRegisterBaselineInvalid = false;
        _bulkRegisterBaselineState.clear();
        _bulkRegisterBaselineRequestId.clear();
      }
      _bulkRegisterPoll.stop();
      _bulkRegisterPolling = false;
      _bulkRegisterStop->setEnabled(false);
      _bulkRegisterDomain->setEnabled(true);
      _bulkRegisterCount->setEnabled(true);
      if (state == QStringLiteral("failed")) {
        _bulkRegisterStatus->setText(QStringLiteral("Falha: ")
                                     + root.value(QStringLiteral("error")).toString());
      } else {
        _bulkRegisterStatus->setText(
            (root.value("stopped").toBool() ? QStringLiteral("Lote parado: %1 criada(s), %2 pendência(s), %3 planejada(s).")
                                            : QStringLiteral("Concluído: %1 criada(s), %2 pendência(s) de %3."))
                .arg(created).arg(failed).arg(total));
      }
      setStatus(_bulkRegisterStatus->text());
      loadAccounts();
      _bulkRegisterPreflightReady = false;
      setAccountTransferBusy(_accountTransferBusy);
      if (!_registrationProxiesReady || _bulkRegisterDomain->currentData().toString().isEmpty())
        loadBulkRegisterDomains(true);
      else
        checkBulkRegisterDomain(true);
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
      setStatus(_bulkRegisterStatus->text());
    }
  });
}

void MainWindow::openMailCleanup() {
  auto* dialog = new QDialog(this);
  dialog->setWindowModality(Qt::WindowModal);
  dialog->setAttribute(Qt::WA_DeleteOnClose);
  dialog->setWindowTitle(QStringLiteral("Limpar caixa de e-mail"));
  dialog->resize(920, 650);
  auto* layout = new QVBoxLayout(dialog);
  auto* help = new QLabel(QStringLiteral(
      "Escolha a caixa e analise a entrada. Saques, pagamentos e mensagens duvidosas ficam preservados. "
      "Revise a lista antes de mover os demais para a lixeira. O QMoney não esvazia a lixeira. "
      "A retenção automática da Hostinger continua valendo; revise a lixeira pelo webmail."), dialog);
  help->setWordWrap(true);
  layout->addWidget(help);
  auto* controls = new QHBoxLayout;
  auto* profiles = new ComboBox(dialog);
  profiles->addItem(QStringLiteral("Carregando caixas…"));
  profiles->setObjectName(QStringLiteral("mailCleanupProfile"));
  auto* preview = new QPushButton(QStringLiteral("Analisar entrada"), dialog);
  auto* stop = new QPushButton(QStringLiteral("Parar"), dialog);
  controls->addWidget(profiles, 1);
  controls->addWidget(preview);
  controls->addWidget(stop);
  layout->addLayout(controls);
  auto* state = new QLabel(QStringLiteral("Carregando caixas…"), dialog);
  state->setWordWrap(true);
  layout->addWidget(state);
  auto* table = new QTableWidget(0, 4, dialog);
  table->setObjectName(QStringLiteral("mailCleanupReview"));
  table->setHorizontalHeaderLabels({QStringLiteral("Destino"), QStringLiteral("Assunto"),
      QStringLiteral("Data"), QStringLiteral("Motivo")});
  table->horizontalHeader()->setSectionResizeMode(1, QHeaderView::Stretch);
  table->setEditTriggers(QAbstractItemView::NoEditTriggers);
  layout->addWidget(table, 1);
  auto* apply = new QPushButton(QStringLiteral("Mover selecionados para a lixeira"), dialog);
  apply->setEnabled(false);
  preview->setEnabled(false);
  layout->addWidget(apply);
  auto* close = new QPushButton(QStringLiteral("Fechar"), dialog);
  layout->addWidget(close);
  connect(close, &QPushButton::clicked, dialog, &QDialog::reject);
  // All asynchronous replies use the dialog's lifetime guard.
  QPointer<QDialog> guard(dialog);
  auto* poll = new QTimer(dialog);
  poll->setInterval(1500);
  auto refresh = [this, guard, profiles, preview, stop, state, table, apply, poll] {
    if (!guard || guard->property("requestPending").toBool()) return;
    guard->setProperty("requestPending", true);
    _api.get(QStringLiteral("/api/mail-cleanup"),
        [guard, profiles, preview, stop, state, table, apply, poll]
        (bool ok, const QJsonDocument& doc, const QString& error) {
      if (!guard) return;
      guard->setProperty("requestPending", false);
      if (!ok) {
        state->setText(QStringLiteral("Falha ao consultar: %1. Reconectando; nenhuma operação será repetida.").arg(error));
        apply->setEnabled(false);
        preview->setEnabled(false);
        poll->start();
        return;
      }
      const auto root = doc.object();
      if (!profiles->property("loaded").toBool()) {
        profiles->clear();
        for (const auto value : root.value(QStringLiteral("profiles")).toArray()) {
          const auto profile = value.toObject();
          profiles->addItem(profile.value(QStringLiteral("name")).toString(), profile.value(QStringLiteral("id")).toString());
        }
        profiles->setProperty("loaded", true);
      }
      const QString phase = root.value(QStringLiteral("state")).toString();
      const bool busy = phase == QStringLiteral("scanning") || phase == QStringLiteral("moving");
      profiles->setEnabled(!busy);
      preview->setEnabled(!busy && profiles->count() > 0);
      stop->setEnabled(busy);
      const auto items = root.value(QStringLiteral("items")).toArray();
      int payments = 0, review = 0, candidates = 0;
      for (const auto value : items) {
        const auto action = value.toObject().value(QStringLiteral("action")).toString();
        if (action == QStringLiteral("move")) ++candidates;
        else if (action == QStringLiteral("payment")) ++payments;
        else ++review;
      }
      const QString id = root.value(QStringLiteral("id")).toString();
      if (guard->property("planId").toString() != id || table->rowCount() != items.size()) {
        guard->setProperty("planId", id);
        table->setRowCount(items.size());
        for (int row = 0; row < items.size(); ++row) {
          const auto item = items[row].toObject();
          const bool movable = item.value(QStringLiteral("action")).toString() == QStringLiteral("move");
          auto* destination = new QTableWidgetItem(movable ? QStringLiteral("Lixeira") : QStringLiteral("Preservar"));
          destination->setData(Qt::UserRole, item.value(QStringLiteral("uid")).toVariant());
          if (movable) destination->setCheckState(Qt::Checked);
          else destination->setFlags(destination->flags() & ~Qt::ItemIsUserCheckable);
          table->setItem(row, 0, destination);
          table->setItem(row, 1, new QTableWidgetItem(item.value(QStringLiteral("subject")).toString()));
          table->setItem(row, 2, new QTableWidgetItem(item.value(QStringLiteral("date")).toString()));
          table->setItem(row, 3, new QTableWidgetItem(item.value(QStringLiteral("reason")).toString()));
        }
      }
      const QString label = phase == QStringLiteral("scanning") ? QStringLiteral("Analisando")
          : phase == QStringLiteral("moving") ? QStringLiteral("Movendo")
          : phase == QStringLiteral("ready") ? QStringLiteral("Prévia pronta — revise antes de confirmar")
          : phase == QStringLiteral("done") ? QStringLiteral("Concluído")
          : phase == QStringLiteral("cancelled") ? QStringLiteral("Interrompido")
          : phase == QStringLiteral("error") ? QStringLiteral("Falha — operação interrompida")
          : QStringLiteral("Escolha uma caixa");
      state->setText(QStringLiteral("%1 · %2\n%3 pagamento(s) preservado(s), %4 para revisão, %5 candidato(s). "
          "%6 movido(s), %7 preservado(s) na revalidação. %8")
          .arg(label, root.value(QStringLiteral("mailbox")).toString())
          .arg(payments).arg(review).arg(candidates)
          .arg(root.value(QStringLiteral("moved")).toInt()).arg(root.value(QStringLiteral("skipped")).toInt())
          .arg(root.value(QStringLiteral("error")).toString()));
      apply->setEnabled(phase == QStringLiteral("ready") && candidates > 0);
      if (busy) poll->start(); else poll->stop();
    });
  };
  connect(poll, &QTimer::timeout, dialog, refresh);
  auto response = [this, guard, state, refresh](bool ok, const QJsonDocument&, const QString& error) {
    if (!guard) return;
    if (!ok) showError(QStringLiteral("Limpeza de e-mail"), error);
    refresh();
  };
  connect(preview, &QPushButton::clicked, dialog, [this, profiles, preview, apply, response] {
    preview->setEnabled(false);
    apply->setEnabled(false);
    _api.post(QStringLiteral("/api/mail-cleanup/preview"),
        {{QStringLiteral("profile_id"), profiles->currentData().toString()}}, response);
  });
  connect(stop, &QPushButton::clicked, dialog, [this, stop, response] {
    stop->setEnabled(false);
    _api.post(QStringLiteral("/api/mail-cleanup/stop"), {}, response);
  });
  connect(apply, &QPushButton::clicked, dialog, [this, guard, table, apply, response] {
    QJsonArray selected;
    for (int row = 0; row < table->rowCount(); ++row)
      if (table->item(row, 0)->checkState() == Qt::Checked)
        selected.append(table->item(row, 0)->data(Qt::UserRole).toLongLong());
    if (selected.isEmpty()) return;
    if (QMessageBox::question(guard, QStringLiteral("Mover para a lixeira"),
        QStringLiteral("Mover %1 e-mail(s) revisado(s) da caixa exibida para a lixeira?\n"
                       "E-mails protegidos permanecem na entrada. A lixeira não será esvaziada.").arg(selected.size()),
        QMessageBox::Yes | QMessageBox::No, QMessageBox::No) != QMessageBox::Yes) return;
    apply->setEnabled(false);
    _api.post(QStringLiteral("/api/mail-cleanup/apply"), {{QStringLiteral("id"), guard->property("planId").toString()},
        {QStringLiteral("uids"), selected}, {QStringLiteral("confirmed"), true}}, response);
  });
  connect(dialog, &QDialog::finished, this, [this](int) {
    _api.post(QStringLiteral("/api/mail-cleanup/stop"), {}, [](bool, const QJsonDocument&, const QString&) {});
  });
  dialog->show();
  refresh();
}

void MainWindow::disableWalletActions() {
  ++_walletRequestEpoch;
  for (auto* button : {_balancesRefresh, _balancesRefreshNeeded, _balancesWithdrawAll, _balancesPayoutMethod})
    if (button) button->setEnabled(false);
  if (_balancesTable)
    for (auto* button : _balancesTable->findChildren<QPushButton*>())
      if (button->property("walletAction").toBool()) button->setEnabled(false);
}

void MainWindow::refreshBalanceAccounts(const QJsonArray& emails) {
  if (_closing || _balanceStarting) return;
  _balanceStarting = true;
  disableWalletActions();
  QJsonObject request;
  if (!emails.isEmpty()) request.insert(QStringLiteral("emails"), emails);
  _api.post(QStringLiteral("/api/balances/refresh"), request,
            [this](bool ok, const QJsonDocument&, const QString& error) {
    _balanceStarting = false;
    loadBalances();
    _balancePoll.start(1500);
    if (!ok) {
      _walletMonitorState->setText(QStringLiteral("Consulta não iniciada: %1").arg(error));
      setStatus(QStringLiteral("Não foi possível iniciar a consulta: %1").arg(error));
      return;
    }
    setStatus(QStringLiteral("Consulta de saldos iniciada."));
  });
}

void MainWindow::showBalanceDetails(const QString& email) {
  const auto balance = _balancesSnapshot.value(QStringLiteral("balances")).toObject().value(email).toObject();
  const auto meta = _balancesSnapshot.value(QStringLiteral("wallet")).toObject()
      .value(QStringLiteral("accounts")).toObject().value(email).toObject();
  QDialog dialog(this);
  dialog.setWindowTitle(QStringLiteral("Carteira · %1").arg(email));
  dialog.resize(650, 590);
  auto* layout = new QVBoxLayout(&dialog);
  auto* identity = new QLabel(email);
  identity->setTextFormat(Qt::PlainText);
  identity->setWordWrap(true);
  identity->setTextInteractionFlags(Qt::TextSelectableByMouse);
  layout->addWidget(identity);
  auto* status = new QLabel(meta.value(QStringLiteral("reading")).toObject().value(QStringLiteral("label")).toString()
      + QStringLiteral("\n") + meta.value(QStringLiteral("restriction")).toObject().value(QStringLiteral("label")).toString());
  status->setTextFormat(Qt::PlainText);
  status->setWordWrap(true);
  layout->addWidget(status);
  auto* form = new QFormLayout;
  const QStringList fields{"availableCents", "pendingCents", "inTransitCents", "onHoldCents", "notApprovedCents", "lifetimeCents"};
  const QStringList labels{QStringLiteral("Disponível"), QStringLiteral("Pendente"), QStringLiteral("Em trânsito"),
      QStringLiteral("Retido"), QStringLiteral("Não aprovado"), QStringLiteral("Acumulado histórico")};
  for (int i = 0; i < fields.size(); ++i) {
    const auto value = balance.value(fields[i]);
    const double amount = value.toDouble(-1);
    const bool valid = value.isDouble() && std::isfinite(amount) && amount >= 0 && amount <= 9007199254740991.0 && std::floor(amount) == amount;
    form->addRow(labels[i], new QLabel(valid ? usdMoney(static_cast<qint64>(amount)) : QStringLiteral("Não informado")));
  }
  const auto addPlain = [form](const QString& label, const QString& value) {
    auto* text = new QLabel(value.isEmpty() ? QStringLiteral("Não informado") : value);
    text->setTextFormat(Qt::PlainText);
    text->setWordWrap(true);
    text->setTextInteractionFlags(Qt::TextSelectableByMouse);
    form->addRow(label, text);
  };
  addPlain(QStringLiteral("Leitura confirmada em"), balance.value(QStringLiteral("updated_at")).toString());
  addPlain(QStringLiteral("Última tentativa"), balance.value(QStringLiteral("checked_at")).toString());
  addPlain(QStringLiteral("Restrição de saque"), meta.value(QStringLiteral("restriction")).toObject().value(QStringLiteral("reason")).toString());
  const auto eligibility = balance.value(QStringLiteral("contributorEligibility")).toObject();
  addPlain(QStringLiteral("Elegibilidade de saque"), !eligibility.value(QStringLiteral("checked")).toBool()
      ? QStringLiteral("Não confirmada pela Crowtado")
      : eligibility.value(QStringLiteral("withdrawalOverride")).toBool()
      ? QStringLiteral("Exceção de saque informada pela Crowtado")
      : eligibility.value(QStringLiteral("available")).toBool() && !eligibility.value(QStringLiteral("blocked")).toBool()
      ? QStringLiteral("Regular na última consulta Crowtado") : QStringLiteral("Pendente ou bloqueada pela Crowtado · confira o motivo"));
  addPlain(QStringLiteral("Método informado"), balance.value(QStringLiteral("payoutPreference")).toString());
  addPlain(QStringLiteral("Motivo da retenção"), balance.value(QStringLiteral("onHoldReason")).toString(
      balance.value(QStringLiteral("holdReason")).toString()));
  addPlain(QStringLiteral("Liberação prevista"), balance.value(QStringLiteral("holdReleaseAt")).toString());
  addPlain(QStringLiteral("Último erro"), balance.value(QStringLiteral("error")).toString());
  auto* detailsBody = new QWidget;
  detailsBody->setLayout(form);
  auto* detailsScroll = new QScrollArea;
  detailsScroll->setWidgetResizable(true);
  detailsScroll->setFrameShape(QFrame::NoFrame);
  detailsScroll->setWidget(detailsBody);
  layout->addWidget(detailsScroll, 1);
  auto* help = quietLabel(QStringLiteral("Disponível, pendente, retenção e trânsito são estados distintos. O acumulado é histórico e não deve ser somado ao disponível. Acesso salvo não confirma a saúde da conta nem o recebimento bancário."));
  help->setWordWrap(true);
  layout->addWidget(help);
  auto* accessRow = new QHBoxLayout;
  auto* access = new QPushButton(meta.value(QStringLiteral("connected")).toBool()
      ? QStringLiteral("Alterar acesso Crowtado") : QStringLiteral("Conectar Crowtado"));
  access->setEnabled(meta.value(QStringLiteral("kind")).toString() != QStringLiteral("claru") && !_balanceStarting && _walletSnapshotHealthy
      && _balancesSnapshot.value(QStringLiteral("runner")).toObject().value(QStringLiteral("state")).toString() != QStringLiteral("running")
      && _balancesSnapshot.value(QStringLiteral("withdraw_bulk")).toObject().value(QStringLiteral("state")).toString() != QStringLiteral("running")
      && _balancesSnapshot.value(QStringLiteral("payout_method_bulk")).toObject().value(QStringLiteral("state")).toString() != QStringLiteral("running"));
  connect(access, &QPushButton::clicked, &dialog, [this, email, &dialog] { dialog.accept(); configureCrowtadoAccess(email); });
  accessRow->addWidget(access);
  const bool saved = _balancesSnapshot.value(QStringLiteral("with_saved_password")).toArray().contains(email);
  accessRow->addWidget(credentialCopyActions(email, saved, false));
  layout->addLayout(accessRow);
  auto* close = new QDialogButtonBox(QDialogButtonBox::Close);
  connect(close, &QDialogButtonBox::rejected, &dialog, &QDialog::reject);
  layout->addWidget(close);
  dialog.exec();
}

void MainWindow::loadBalances() {
  if (_balancePolling || _closing) return;
  _balancePolling = true;
  _api.get(QStringLiteral("/api/balances"), [this, epoch = _walletRequestEpoch](bool ok, const QJsonDocument& doc, const QString& error) {
    _balancePolling = false;
    if (epoch != _walletRequestEpoch) { _balancePoll.start(1500); return; }
    const auto response = doc.object();
    const bool validResponse = doc.isObject() && response.value(QStringLiteral("accounts")).isArray()
        && response.value(QStringLiteral("balances")).isObject() && response.value(QStringLiteral("runner")).isObject();
    if (!ok || !validResponse) {
      _walletSnapshotHealthy = false;
      disableWalletActions();
      _balancesState->setText(QStringLiteral("Não foi possível atualizar os saldos: %1. Os valores exibidos são da última leitura.")
          .arg(error.isEmpty() ? QStringLiteral("resposta incompleta do serviço") : error));
      _balancesCoverage->setText(QStringLiteral("Serviço indisponível · leituras exibidas sem confirmação atual"));
      _balancesApprovedUsd->setText(QStringLiteral("US$ —"));
      _balancesPendingUsd->setText(QStringLiteral("US$ —"));
      _balancesApprovedBrl->setText(QStringLiteral("≈ R$ —"));
      _balancesPendingBrl->setText(QStringLiteral("≈ R$ —"));
      _balancesTransitUsd->setText(QStringLiteral("Em trânsito: US$ —"));
      _balancesHoldUsd->setText(QStringLiteral("Retido: US$ —"));
      for (int row = 0; row < _balancesTable->rowCount(); ++row) {
        for (int column = 1; column <= 2; ++column) {
          auto* item = _balancesTable->item(row, column);
          if (item && item->text().startsWith(QStringLiteral("US$")) && !item->text().endsWith(QStringLiteral(" *")))
            item->setText(item->text() + QStringLiteral(" *"));
        }
        if (auto* item = _balancesTable->item(row, 3)) item->setText(QStringLiteral("Sem confirmação atual"));
      }
      _balancesWithdrawAll->setEnabled(false);
      _balancesPayoutMethod->setEnabled(false);
      _balancesRefresh->setEnabled(false);
      _balancePoll.setInterval(5000);
      if (_pages->currentIndex() == 6) _balancePoll.start();
      setStatus(QStringLiteral("Falha ao consultar saldos: %1").arg(error));
      return;
    }
    _walletSnapshotHealthy = true;
    _balancePoll.setInterval(1500);
    const auto root = doc.object();
    const QString previousBalanceState = _balancesSnapshot.value(QStringLiteral("runner"))
        .toObject().value(QStringLiteral("state")).toString();
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
    const bool cleanupAutomatic = wiseCleanup.value(QStringLiteral("automatic")).toBool()
        && bulk.value(QStringLiteral("state")).toString() == QStringLiteral("running");
    _balancesWiseCleanup->setVisible(cleanupPending);
    _balancesWiseCleanup->setEnabled(!cleanupAutomatic);
    const auto receipt = root.value(QStringLiteral("last_withdrawal")).toObject();
    _balancesWithdrawReceipt->setVisible(!receipt.isEmpty());
    if (!receipt.isEmpty()) {
      _balancesWithdrawReceipt->setText(QStringLiteral("%1 · %2\n%3")
          .arg(receipt.value(QStringLiteral("email")).toString(),
               friendlyDate(receipt.value(QStringLiteral("finished_at")).toString()),
               receipt.value(QStringLiteral("message")).toString()));
      _balancesWithdrawReceipt->setStyleSheet(receipt.value(QStringLiteral("accepted")).toBool()
          ? QStringLiteral("padding:12px; border:1px solid #20a475; border-radius:8px;")
          : QStringLiteral("padding:12px; border:1px solid #bb873c; border-radius:8px;"));
    }
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
    const auto orderMetadata = root.value(QStringLiteral("wallet")).toObject().value(QStringLiteral("accounts")).toObject();
    std::sort(orderedAccounts.begin(), orderedAccounts.end(), [&balances, &orderMetadata](const QString& left, const QString& right) {
      const auto isConfirmed = [&orderMetadata](const QString& email) {
        return orderMetadata.value(email).toObject().value(QStringLiteral("reading")).toObject().value(QStringLiteral("confirmed")).toBool();
      };
      if (isConfirmed(left) != isConfirmed(right)) return isConfirmed(left);
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
    const auto wallet = root.value(QStringLiteral("wallet")).toObject();
    const auto walletRows = wallet.value(QStringLiteral("accounts")).toObject();
    const auto coverage = wallet.value(QStringLiteral("counts")).toObject();
    const auto totals = wallet.value(QStringLiteral("totals")).toObject();
    const auto fieldCoverage = wallet.value(QStringLiteral("field_coverage")).toObject();
    const bool anyConfirmed = coverage.value(QStringLiteral("confirmed")).toInt() > 0
        && totals.value(QStringLiteral("availableCents")).isDouble()
        && totals.value(QStringLiteral("pendingCents")).isDouble();
    const qint64 approvedTotal = static_cast<qint64>(wallet.value(QStringLiteral("withdrawable_total_cents")).toDouble());
    const qint64 pendingTotal = static_cast<qint64>(totals.value(QStringLiteral("pendingCents")).toDouble());
    const bool busy = _balanceStarting || runner.value(QStringLiteral("state")).toString() == QStringLiteral("running")
        || bulk.value(QStringLiteral("state")).toString() == QStringLiteral("running")
        || payout.value(QStringLiteral("state")).toString() == QStringLiteral("running");
    int eligibleWithdrawals = 0;
    int row = 0;
    for (const QString& email : orderedAccounts) {
      const bool isClaru = accountKinds.value(email).toString() == QStringLiteral("claru");
      const auto balance = balances.value(email).toObject();
      const auto meta = walletRows.value(email).toObject();
      const auto reading = meta.value(QStringLiteral("reading")).toObject();
      const auto eligibility = meta.value(QStringLiteral("payout")).toObject();
      const bool confirmed = reading.value(QStringLiteral("confirmed")).toBool();
      const bool hasPassword = passwordAccounts.contains(email);
      const bool eligible = !isClaru && hasPassword && confirmed && eligibility.value(QStringLiteral("eligible")).toBool();
      if (eligible) ++eligibleWithdrawals;
      auto* emailItem = cell(email);
      emailItem->setToolTip(isClaru ? QStringLiteral("Conta Claru · saldo Crowtado não se aplica")
          : hasPassword ? QStringLiteral("Acesso Crowtado salvo; a consulta verifica a sessão")
                        : QStringLiteral("Acesso Crowtado não conectado"));
      _balancesTable->setItem(row, 0, emailItem);
      int column = 1;
      for (const auto field : {"pendingCents", "availableCents"}) {
        const auto value = balance.value(QLatin1String(field));
        const double amount = value.toDouble(-1);
        const bool valid = value.isDouble() && std::isfinite(amount) && amount >= 0
            && amount <= 9007199254740991.0 && std::floor(amount) == amount;
        auto* item = cell(isClaru ? QStringLiteral("—") : valid
            ? usdMoney(static_cast<qint64>(amount)) + (confirmed ? QString() : QStringLiteral(" *"))
            : QStringLiteral("—"));
        item->setTextAlignment(Qt::AlignRight | Qt::AlignVCenter);
        item->setToolTip(isClaru ? QStringLiteral("Não se aplica") : !confirmed
            ? QStringLiteral("Valor histórico, excluído dos totais confirmados.\n") + reading.value(QStringLiteral("reason")).toString()
            : !valid ? QStringLiteral("Campo não informado pela Crowtado")
            : QStringLiteral("Valor da última leitura confirmada. A disponibilidade será verificada novamente antes do saque."));
        _balancesTable->setItem(row, column++, item);
      }
      QString state = isClaru ? QStringLiteral("Claru · preservada")
          : !hasPassword ? QStringLiteral("Conectar acesso")
          : meta.value(QStringLiteral("refreshing")).toBool() ? QStringLiteral("Consultando…")
          : !confirmed ? reading.value(QStringLiteral("label")).toString(QStringLiteral("Atualizar saldo"))
          : eligibility.value(QStringLiteral("label")).toString(QStringLiteral("Saldo confirmado"));
      const auto restriction = meta.value(QStringLiteral("restriction")).toObject();
      const auto restrictionCode = restriction.value(QStringLiteral("code")).toString();
      if (!isClaru && restrictionCode != QStringLiteral("clear")) {
        if (confirmed && restrictionCode != QStringLiteral("unknown"))
          state = restriction.value(QStringLiteral("label")).toString();
        else state += QStringLiteral("\n") + restriction.value(QStringLiteral("label")).toString();
      }
      auto* stateItem = cell(state);
      if (restrictionCode == QStringLiteral("disabled") || restrictionCode == QStringLiteral("restricted"))
        stateItem->setForeground(QColor(palette().color(QPalette::Window).lightness() < 128 ? QStringLiteral("#ffa9ae") : QStringLiteral("#b13042")));
      stateItem->setToolTip(reading.value(QStringLiteral("reason")).toString() + QStringLiteral("\n")
                           + eligibility.value(QStringLiteral("reason")).toString() + QStringLiteral("\n") + restriction.value(QStringLiteral("reason")).toString());
      _balancesTable->setItem(row, 3, stateItem);
      auto* dateItem = cell(friendlyDate(balance.value(QStringLiteral("updated_at")).toString()));
      dateItem->setToolTip(QStringLiteral("Leitura: %1\nÚltima tentativa: %2")
          .arg(balance.value(QStringLiteral("updated_at")).toString(), balance.value(QStringLiteral("checked_at")).toString()));
      _balancesTable->setItem(row, 4, dateItem);
      auto* actions = new QWidget;
      auto* actionsLayout = new QHBoxLayout(actions);
      actionsLayout->setContentsMargins(5, 5, 5, 5);
      actionsLayout->setSpacing(5);
      auto* details = new QPushButton(QStringLiteral("Detalhes"));
      details->setMinimumHeight(32);
      connect(details, &QPushButton::clicked, this, [this, email] { showBalanceDetails(email); });
      actionsLayout->addWidget(details);
      auto* update = new QPushButton(QStringLiteral("Atualizar"));
      update->setMinimumHeight(32);
      update->setProperty("walletAction", true);
      update->setEnabled(!isClaru && hasPassword && !busy && !cleanupPending);
      update->setToolTip(hasPassword ? QStringLiteral("Consulta somente esta conta") : QStringLiteral("Conecte o acesso em Detalhes"));
      connect(update, &QPushButton::clicked, this, [this, email] { refreshBalanceAccounts(QJsonArray{email}); });
      actionsLayout->addWidget(update);
      auto* withdraw = new QPushButton(QStringLiteral("Sacar"));
      withdraw->setMinimumHeight(32);
      withdraw->setProperty("walletAction", true);
      withdraw->setEnabled(eligible && !busy && !cleanupPending);
      withdraw->setToolTip(cleanupPending ? QStringLiteral("Conclua a limpeza Wise antes de solicitar saque")
          : busy ? QStringLiteral("Aguarde a operação em andamento")
          : eligibility.value(QStringLiteral("reason")).toString(QStringLiteral("Consulte esta conta para confirmar o saldo. Pagamentos em trânsito impedem novo saque.")));
      connect(withdraw, &QPushButton::clicked, this, [this, email] {
        if (_balanceStarting) return;
        QJsonObject request{{QStringLiteral("email"), email}};
        if (!confirmWithdrawal(this, 1, request)) return;
        if (request.value(QStringLiteral("method")).toString() == QStringLiteral("wise"))
          request.insert(QStringLiteral("background"), true);
        _balanceStarting = true;
        disableWalletActions();
        _api.post(QStringLiteral("/api/balances/withdraw"), request,
                  [this](bool ok, const QJsonDocument& doc, const QString& error) {
          _balanceStarting = false;
          loadBalances();
          if (!ok) return showError(QStringLiteral("Solicitação precisa de atenção"), error);
          if (doc.object().value(QStringLiteral("background")).toBool()) {
            _bulkWithdrawAwaitingResult = true;
            _balancePoll.start(1500);
            setStatus(QStringLiteral("Processando saque Wise e limpeza automática…"));
            return;
          }
          QMessageBox::information(this, QStringLiteral("Solicitação enviada"),
                                   doc.object().value(QStringLiteral("message")).toString());
        });
      });
      actionsLayout->addWidget(withdraw);
      _balancesTable->setCellWidget(row, 5, actions);
      _balancesTable->setColumnWidth(5, std::max(245, actions->sizeHint().width() + 12));
      _balancesTable->setRowHeight(row, 62);
      ++row;
    }
    applyBalanceFilter();
    const bool payoutTotalValid = anyConfirmed && wallet.value(QStringLiteral("withdrawable_total_cents")).isDouble();
    _balancesApprovedUsd->setText(payoutTotalValid ? usdMoney(approvedTotal) : QStringLiteral("US$ —"));
    _balancesPendingUsd->setText(anyConfirmed ? usdMoney(pendingTotal) : QStringLiteral("US$ —"));
    _balancesTransitUsd->setText(QStringLiteral("Em trânsito: %1").arg(anyConfirmed
        ? usdMoney(static_cast<qint64>(totals.value(QStringLiteral("inTransitCents")).toDouble())) : QStringLiteral("US$ —")));
    _balancesHoldUsd->setText(QStringLiteral("Retido: %1").arg(anyConfirmed && fieldCoverage.value(QStringLiteral("onHoldCents")).toInt() > 0
        ? usdMoney(static_cast<qint64>(totals.value(QStringLiteral("onHoldCents")).toDouble())) : QStringLiteral("US$ —")));
    _balancesCoverage->setText(QStringLiteral("%1 de %2 leituras confirmadas · %3 precisam de atenção · %4 conta(s) elegível(is) para saque")
        .arg(coverage.value(QStringLiteral("confirmed")).toInt()).arg(coverage.value(QStringLiteral("crowtado")).toInt())
        .arg(coverage.value(QStringLiteral("attention")).toInt()).arg(eligibleWithdrawals));
    _balancesTotalsNote->show();
    _balancesTotalsNote->setText(QStringLiteral("Disponível para saque exclui retenções conhecidas e contas não elegíveis. * Histórico excluído dos totais. O envio será validado novamente."));
    const bool exchangeAvailable = exchange.value(QStringLiteral("available")).toBool();
    const double usdBrlRate = exchange.value(QStringLiteral("rate")).toDouble();
    if (anyConfirmed && exchangeAvailable && std::isfinite(usdBrlRate) && usdBrlRate > 0.0) {
      _balancesApprovedBrl->setText(payoutTotalValid ? QStringLiteral("≈ %1").arg(brlMoney(approvedTotal, usdBrlRate)) : QStringLiteral("≈ R$ —"));
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
    const QString balanceState = runner.value(QStringLiteral("state")).toString();
    if (balanceState != previousBalanceState &&
        (balanceState == QStringLiteral("done") || balanceState == QStringLiteral("error"))) {
      if (balanceState == QStringLiteral("error"))
        setStatus(QStringLiteral("Consulta de saldos interrompida. Confira o diagnóstico na Carteira."));
      else
        setStatus(QStringLiteral("Consulta de saldos concluída: %1 conta(s), %2 com erro.")
            .arg(runner.value(QStringLiteral("total")).toInt())
            .arg(runner.value(QStringLiteral("failed")).toInt()));
    }
    _balancesRefresh->setEnabled(!busy && !cleanupPending && !passwordAccounts.isEmpty());
    const bool bulkRunning = bulk.value(QStringLiteral("state")).toString() == QStringLiteral("running");
    const bool payoutRunning = payout.value(QStringLiteral("state")).toString() == QStringLiteral("running");
    const int refreshNeeded = root.value(QStringLiteral("refresh_needed")).toArray().size();
    _balancesRefreshNeeded->setText(QStringLiteral("Atualizar pendentes (%1)").arg(refreshNeeded));
    _balancesRefreshNeeded->setEnabled(!busy && !cleanupPending && refreshNeeded > 0);
    _balancesWithdrawAll->setProperty("eligibleCount", eligibleWithdrawals);
    _balancesWithdrawAll->setEnabled(!cleanupPending && !busy && eligibleWithdrawals > 0);
    _balancesPayoutMethod->setEnabled(!cleanupPending && !busy && !passwordAccounts.isEmpty());
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
        ? (cleanupAutomatic
            ? QStringLiteral("Limpando Wise automaticamente em %1 · tentativa %2 de %3 · aguardando antes da próxima conta")
                .arg(wiseCleanup.value(QStringLiteral("email")).toString())
                .arg(wiseCleanup.value(QStringLiteral("attempt")).toInt())
                .arg(wiseCleanup.value(QStringLiteral("max_attempts")).toInt())
            : QStringLiteral("Limpeza Wise pendente em %1 · novos saques bloqueados")
                .arg(wiseCleanup.value(QStringLiteral("email")).toString()))
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
        : runner.value(QStringLiteral("state")).toString() == QStringLiteral("error")
        ? runner.value(QStringLiteral("error")).toString(QStringLiteral("A consulta foi interrompida. Atualize as contas pendentes."))
        : runner.value(QStringLiteral("failed")).toInt() > 0
        ? QStringLiteral("Consulta concluída: %1 de %2 conta(s) com erro. Confira o diagnóstico em cada linha.")
              .arg(runner.value(QStringLiteral("failed")).toInt())
              .arg(runner.value(QStringLiteral("total")).toInt())
        : QStringLiteral("%1 identidade(s) · %2 Crowtado conectado(s) · %3 Claru preservada(s)")
              .arg(accounts.size()).arg(passwordAccounts.size()).arg(claruCount));
    _balancesStop->setVisible(running);
    _balancesStop->setEnabled(running && runner.value(QStringLiteral("phase")).toString() != QStringLiteral("stopping"));
    _balancesProgress->setVisible(running);
    _balancesProgress->setRange(0, std::max(1, runner.value(QStringLiteral("total")).toInt()));
    _balancesProgress->setValue(runner.value(QStringLiteral("done")).toInt());
    _balancesProgress->setFormat(QStringLiteral("%v de %m contas · %1 com erro").arg(runner.value(QStringLiteral("failed")).toInt()));
    if (runner.value(QStringLiteral("phase")).toString() == QStringLiteral("stopping"))
      _balancesState->setText(QStringLiteral("Interrompendo após concluir a conta atual…"));
    else if (balanceState == QStringLiteral("stopped"))
      _balancesState->setText(QStringLiteral("Consulta interrompida: %1 de %2 contas consultadas. As restantes podem ser atualizadas depois.")
          .arg(runner.value(QStringLiteral("done")).toInt()).arg(runner.value(QStringLiteral("total")).toInt()));
    if (_pages->currentIndex() == 6 || busy || _walletMonitoring->isChecked())
      _balancePoll.start(busy ? 1500 : 30000);
    else _balancePoll.stop();
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
  int quantity = _campaignAccountCount->value();
  for (int i = 0; i < _campaignAccounts->count(); ++i)
    if (_campaignAccounts->item(i)->checkState() == Qt::Checked)
      accounts.append(_campaignAccounts->item(i)->data(Qt::UserRole).toString());
  if (!_campaignAccountsLoaded && _campaignAccounts->count()==0) {
    const auto saved = QJsonDocument::fromJson(QSettings().value(QStringLiteral("campaign/draft")).toByteArray()).object();
    accounts = saved.value(QStringLiteral("accounts")).toArray();
    quantity = saved.value(QStringLiteral("quantity")).toInt(_campaignDraftQuantity);
  }
  QJsonArray tasks;
  for (const QString& id : _campaignSelectedTaskIds) tasks.append(id);
  const QJsonObject draft{
      {QStringLiteral("accounts"), accounts},
      {QStringLiteral("tasks"), tasks},
      {QStringLiteral("tasks_touched"), _campaignTaskSelectionTouched},
      {QStringLiteral("mode"), _campaignAccountMode->currentData().toString()},
      {QStringLiteral("quantity"), quantity},
      {QStringLiteral("dataset"), _dataset->currentData().toString()},
      {QStringLiteral("content_mode"), _contentMode->currentData().toString()},
      {QStringLiteral("run_until_exhausted"), true},
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
        && !balance.value(QStringLiteral("stale")).toBool()
        && _balancesSnapshot.value(QStringLiteral("wallet")).toObject().value(QStringLiteral("accounts"))
            .toObject().value(email).toObject().value(QStringLiteral("reading")).toObject().value(QStringLiteral("confirmed")).toBool();
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
  if (_campaignResetPending) return;
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
