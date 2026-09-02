#include "MainWindow.hpp"

#include <QApplication>
#include <QCheckBox>
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
#include <QJsonValue>
#include <QLabel>
#include <QLineEdit>
#include <QListWidget>
#include <QLocale>
#include <QMessageBox>
#include <QPlainTextEdit>
#include <QPixmap>
#include <QProgressBar>
#include <QProcess>
#include <QProcessEnvironment>
#include <QPushButton>
#include <QScrollArea>
#include <QSaveFile>
#include <QSettings>
#include <QSizePolicy>
#include <QSpinBox>
#include <QStackedWidget>
#include <QStandardPaths>
#include <QSignalBlocker>
#include <QTableWidget>
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
  combo->setMaxVisibleItems(10);
  combo->setSizeAdjustPolicy(QComboBox::AdjustToMinimumContentsLengthWithIcon);
  combo->setMinimumContentsLength(24);
  combo->view()->setTextElideMode(Qt::ElideNone);
  combo->view()->setHorizontalScrollBarPolicy(Qt::ScrollBarAsNeeded);
  combo->view()->setVerticalScrollBarPolicy(Qt::ScrollBarAsNeeded);
  combo->view()->setVerticalScrollMode(QAbstractItemView::ScrollPerPixel);
}

void fitComboPopup(QComboBox* combo) {
  const QFontMetrics metrics(combo->view()->font());
  int width = qMax(combo->width(), combo->minimumWidth());
  for (int i = 0; i < combo->count(); ++i) {
    const QString text = combo->itemText(i);
    width = qMax(width, metrics.horizontalAdvance(text) + 76);
    combo->setItemData(i, text, Qt::ToolTipRole);
  }

  // Categorias do Minute/HoloAssist são descritivas e frequentemente longas.
  // O popup pode crescer além do campo, mas mantém rolagem em telas menores.
  const int popupWidth = qMin(width, 900);
  const int visibleRows = qMin(qMax(combo->count(), 1), combo->maxVisibleItems());
  const int rowHeight = qMax(40, combo->view()->sizeHintForRow(0));
  const int popupHeight = visibleRows * rowHeight + 14;
  combo->view()->setMinimumWidth(popupWidth);
  combo->view()->setMaximumWidth(popupWidth);
  combo->view()->setMinimumHeight(popupHeight);
  combo->view()->setMaximumHeight(popupHeight);
}

void configureTable(QTableWidget* table) {
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
}

void configureForm(QFormLayout* form) {
  form->setFieldGrowthPolicy(QFormLayout::AllNonFixedFieldsGrow);
  form->setRowWrapPolicy(QFormLayout::WrapLongRows);
  form->setLabelAlignment(Qt::AlignLeft | Qt::AlignVCenter);
  form->setFormAlignment(Qt::AlignTop);
  form->setHorizontalSpacing(18);
  form->setVerticalSpacing(12);
}

void copyIfNewer(const QString& source, const QString& destination) {
  const QFileInfo src(source);
  if (!src.isFile()) return;
  const QFileInfo dst(destination);
  if (dst.exists() && dst.lastModified() >= src.lastModified()) return;
  QDir().mkpath(QFileInfo(destination).absolutePath());
  if (dst.exists()) QFile::remove(destination);
  QFile::copy(source, destination);
  QFile(destination).setFileTime(src.lastModified(), QFileDevice::FileModificationTime);
}

void migrateLegacyState(const QString& legacyRoot, const QString& userRoot) {
  if (QDir::cleanPath(legacyRoot) == QDir::cleanPath(userRoot)) return;
  const QDir legacyData(legacyRoot + QStringLiteral("/data"));
  const QDir userData(userRoot + QStringLiteral("/data"));
  const QStringList stateFiles = legacyData.entryList(
      {QStringLiteral("*.json"), QStringLiteral("*.jsonl"), QStringLiteral("*.pkl")},
      QDir::Files);
  for (const QString& name : stateFiles)
    copyIfNewer(legacyData.filePath(name), userData.filePath(name));

  const QStringList stateDirectories = {QStringLiteral("device_state")};
  for (const QString& directory : stateDirectories) {
    const QString sourceRoot = legacyData.filePath(directory);
    QDirIterator files(sourceRoot, QDir::Files, QDirIterator::Subdirectories);
    while (files.hasNext()) {
      const QString source = files.next();
      const QString relative = QDir(sourceRoot).relativeFilePath(source);
      copyIfNewer(source, userData.filePath(directory + QLatin1Char('/') + relative));
    }
  }

  const QDir legacySecrets(legacyRoot + QStringLiteral("/secrets"));
  const QDir userSecrets(userRoot + QStringLiteral("/secrets"));
  QDirIterator secretFiles(legacySecrets.absolutePath(), QDir::Files, QDirIterator::Subdirectories);
  while (secretFiles.hasNext()) {
    const QString source = secretFiles.next();
    const QString relative = legacySecrets.relativeFilePath(source);
    copyIfNewer(source, userSecrets.filePath(relative));
  }
}
}  // namespace

MainWindow::MainWindow(oclero::qlementine::QlementineStyle* style, QWidget* parent)
    : QMainWindow(parent), _style(style), _api(this), _backend(this) {
  setWindowTitle(QStringLiteral("QMoney — Operations Desk"));
  resize(1280, 820);
  setMinimumSize(980, 680);

  _backendProbe.setInterval(650);
  _campaignPoll.setInterval(1100);
  _balancePoll.setInterval(1500);
  _cachePoll.setInterval(1300);
  connect(&_backendProbe, &QTimer::timeout, this, &MainWindow::probeBackend);
  connect(&_campaignPoll, &QTimer::timeout, this, &MainWindow::pollCampaign);
  connect(&_balancePoll, &QTimer::timeout, this, &MainWindow::loadBalances);
  connect(&_cachePoll, &QTimer::timeout, this, &MainWindow::loadAccelerator);

  buildShell();
  qInfo() << "QMoney: shell pronto";
  const bool dark = QSettings().value(QStringLiteral("darkTheme"), false).toBool();
  // Aplicado apos a primeira passagem do event loop. O Qlementine instala
  // filtros de evento durante a construcao dos widgets; adiar o stylesheet
  // evita uma repolish aninhada nessa mesma pilha.
  QTimer::singleShot(0, this, [this, dark] { applyStructuralStyle(dark); });
  _themeButton->setText(dark ? QStringLiteral("☀  Usar tema claro")
                             : QStringLiteral("◐  Usar tema escuro"));
  qInfo() << "QMoney: tema estrutural pronto";
  startBackend();
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
  QTimer::singleShot(1800, this, [this] { checkForUpdates(false); });
}

MainWindow::~MainWindow() {
  _closing = true;
  stopBackend();
}

void MainWindow::closeEvent(QCloseEvent* event) {
  _closing = true;
  _campaignPoll.stop();
  _cachePoll.stop();
  _balancePoll.stop();
  _backendProbe.stop();
  stopBackend();
  QMainWindow::closeEvent(event);
}

void MainWindow::buildShell() {
  auto* root = new QWidget;
  auto* rootLayout = new QHBoxLayout(root);
  rootLayout->setContentsMargins(0, 0, 0, 0);
  rootLayout->setSpacing(0);

  auto* sidebar = new QWidget;
  sidebar->setObjectName(QStringLiteral("sidebar"));
  sidebar->setFixedWidth(264);
  auto* side = new QVBoxLayout(sidebar);
  side->setContentsMargins(22, 26, 20, 18);
  side->setSpacing(10);

  auto* brandRow = new QHBoxLayout;
  auto* mark = new QLabel;
  mark->setObjectName(QStringLiteral("brandMark"));
  mark->setAlignment(Qt::AlignCenter);
  mark->setFixedSize(44, 44);
  mark->setPixmap(QPixmap(QStringLiteral(":/qmoney/icon.png"))
                      .scaled(42, 42, Qt::KeepAspectRatio, Qt::SmoothTransformation));
  auto* brandCopy = new QVBoxLayout;
  brandCopy->setSpacing(0);
  auto* brand = new QLabel(QStringLiteral("QMoney"));
  brand->setObjectName(QStringLiteral("brand"));
  brandCopy->addWidget(brand);
  auto* brandRole = quietLabel(QStringLiteral("OPERATIONS DESK"));
  brandRole->setObjectName(QStringLiteral("brandRole"));
  brandCopy->addWidget(brandRole);
  brandRow->addWidget(mark);
  brandRow->addSpacing(6);
  brandRow->addLayout(brandCopy, 1);
  side->addLayout(brandRow);
  side->addSpacing(20);

  auto* navigationLabel = new QLabel(QStringLiteral("ESPAÇOS DE TRABALHO"));
  navigationLabel->setObjectName(QStringLiteral("navigationLabel"));
  side->addWidget(navigationLabel);

  _navigation = new QListWidget;
  _navigation->setObjectName(QStringLiteral("navigation"));
  _navigation->setFrameShape(QFrame::NoFrame);
  _navigation->setHorizontalScrollBarPolicy(Qt::ScrollBarAlwaysOff);
  const QStringList pages = {
      QStringLiteral("Visão geral"), QStringLiteral("Prontidão"),
      QStringLiteral("Integrações"), QStringLiteral("Nova campanha"),
      QStringLiteral("Acelerador"),
      QStringLiteral("Contas"), QStringLiteral("Saldos"),
      QStringLiteral("Histórico")};
  const QStringList icons = {QStringLiteral(":/qmoney/icons/home.svg"),
                             QStringLiteral(":/qmoney/icons/readiness.svg"),
                             QStringLiteral(":/qmoney/icons/integrations.svg"),
                             QStringLiteral(":/qmoney/icons/play.svg"),
                             QStringLiteral(":/qmoney/icons/bolt.svg"),
                             QStringLiteral(":/qmoney/icons/users.svg"),
                             QStringLiteral(":/qmoney/icons/wallet.svg"),
                             QStringLiteral(":/qmoney/icons/history.svg")};
  _navigation->setIconSize(QSize(19, 19));
  for (int i = 0; i < pages.size(); ++i) {
    auto* item = new QListWidgetItem(QIcon(icons[i]), pages[i]);
    item->setSizeHint(QSize(210, 46));
    _navigation->addItem(item);
  }
  _navigation->setCurrentRow(0);
  connect(_navigation, &QListWidget::currentRowChanged, this, &MainWindow::navigate);
  side->addWidget(_navigation, 1);

  auto* connectionCard = new QFrame;
  connectionCard->setObjectName(QStringLiteral("connectionCard"));
  auto* connectionLayout = new QVBoxLayout(connectionCard);
  connectionLayout->setContentsMargins(14, 12, 14, 12);
  connectionLayout->setSpacing(5);
  connectionLayout->addWidget(quietLabel(QStringLiteral("MOTOR LOCAL")));
  _backendState = new QLabel(QStringLiteral("●  Iniciando…"));
  _backendState->setObjectName(QStringLiteral("backendState"));
  connectionLayout->addWidget(_backendState);
  side->addWidget(connectionCard);

  _themeButton = new QPushButton;
  _themeButton->setObjectName(QStringLiteral("sidebarUtility"));
  connect(_themeButton, &QPushButton::clicked, this, [this] {
    setDarkTheme(!QSettings().value(QStringLiteral("darkTheme"), false).toBool());
  });
  side->addWidget(_themeButton);

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
  side->addWidget(_updateButton);

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

  rootLayout->addWidget(sidebar);
  rootLayout->addWidget(workspace, 1);
  setCentralWidget(root);
}

QWidget* MainWindow::pageShell(const QString& title, const QString& subtitle, QWidget* body) {
  auto* shell = new QWidget;
  auto* outer = new QVBoxLayout(shell);
  outer->setContentsMargins(40, 30, 40, 30);
  outer->setSpacing(15);
  auto* contextRow = new QHBoxLayout;
  auto* context = new QLabel(QStringLiteral("QMONEY  /  %1").arg(title.toUpper()));
  context->setObjectName(QStringLiteral("pageContext"));
  contextRow->addWidget(context);
  contextRow->addStretch();
  auto* mode = new QLabel(QStringLiteral("CONSOLE OPERACIONAL"));
  mode->setObjectName(QStringLiteral("modeBadge"));
  contextRow->addWidget(mode);
  outer->addLayout(contextRow);
  auto* titleLabel = new QLabel(title);
  titleLabel->setObjectName(QStringLiteral("pageTitle"));
  outer->addWidget(titleLabel);
  auto* subtitleLabel = quietLabel(subtitle);
  subtitleLabel->setObjectName(QStringLiteral("pageSubtitle"));
  outer->addWidget(subtitleLabel);
  outer->addSpacing(2);
  outer->addWidget(body, 1);
  return shell;
}

QWidget* MainWindow::card(const QString& title, QWidget* content) {
  auto* frame = new QFrame;
  frame->setObjectName(QStringLiteral("card"));
  auto* layout = new QVBoxLayout(frame);
  layout->setContentsMargins(22, 20, 22, 20);
  layout->setSpacing(13);
  if (!title.isEmpty()) {
    auto* heading = new QLabel(title);
    heading->setObjectName(QStringLiteral("cardTitle"));
    layout->addWidget(heading);
  }
  if (content) layout->addWidget(content);
  return frame;
}

QWidget* MainWindow::metric(const QString& value, const QString& caption, QLabel** valueLabel) {
  auto* box = new QWidget;
  auto* layout = new QVBoxLayout(box);
  layout->setContentsMargins(2, 1, 2, 1);
  layout->setSpacing(3);
  auto* label = new QLabel(caption.toUpper());
  label->setObjectName(QStringLiteral("metricCaption"));
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
  layout->setSpacing(14);

  auto* pulseContent = new QWidget;
  auto* pulse = new QHBoxLayout(pulseContent);
  pulse->setContentsMargins(0, 0, 0, 0);
  pulse->setSpacing(22);

  auto* signal = new QFrame;
  signal->setObjectName(QStringLiteral("signalRail"));
  signal->setFixedWidth(104);
  auto* signalLayout = new QVBoxLayout(signal);
  signalLayout->setContentsMargins(12, 12, 12, 12);
  signalLayout->setSpacing(2);
  auto* signalTop = new QLabel(QStringLiteral("AO VIVO"));
  signalTop->setObjectName(QStringLiteral("signalLabel"));
  signalTop->setAlignment(Qt::AlignCenter);
  auto* signalDot = new QLabel(QStringLiteral("●"));
  signalDot->setObjectName(QStringLiteral("signalDot"));
  signalDot->setAlignment(Qt::AlignCenter);
  auto* signalPort = new QLabel(QStringLiteral("MOTOR\n8876"));
  signalPort->setObjectName(QStringLiteral("signalPort"));
  signalPort->setAlignment(Qt::AlignCenter);
  signalLayout->addWidget(signalTop);
  signalLayout->addStretch();
  signalLayout->addWidget(signalDot);
  signalLayout->addStretch();
  signalLayout->addWidget(signalPort);
  pulse->addWidget(signal);

  auto* pulseCopy = new QWidget;
  auto* pulseCopyLayout = new QVBoxLayout(pulseCopy);
  pulseCopyLayout->setContentsMargins(0, 4, 0, 4);
  pulseCopyLayout->setSpacing(8);
  auto* pulseHeader = new QHBoxLayout;
  auto* pulseKicker = new QLabel(QStringLiteral("PULSO DE CAMPANHA"));
  pulseKicker->setObjectName(QStringLiteral("kicker"));
  pulseHeader->addWidget(pulseKicker);
  pulseCopyLayout->addLayout(pulseHeader);
  _homePulseTitle = new QLabel(QStringLiteral("Aguardando o serviço local"));
  _homePulseTitle->setObjectName(QStringLiteral("pulseTitle"));
  pulseCopyLayout->addWidget(_homePulseTitle);
  _homePulseBody = quietLabel(QStringLiteral("Os dados aparecerão assim que o motor responder."));
  pulseCopyLayout->addWidget(_homePulseBody);
  pulseCopyLayout->addSpacing(2);
  _homePulseProgress = new QProgressBar;
  _homePulseProgress->setRange(0, 100);
  _homePulseProgress->setValue(0);
  _homePulseProgress->setTextVisible(false);
  pulseCopyLayout->addWidget(_homePulseProgress);
  pulse->addWidget(pulseCopy, 1);

  auto* newCampaign = primaryButton(QStringLiteral("Criar campanha  →"));
  newCampaign->setMinimumWidth(158);
  connect(newCampaign, &QPushButton::clicked, this, [this] { _navigation->setCurrentRow(3); });
  pulse->addWidget(newCampaign, 0, Qt::AlignVCenter);
  auto* pulseCard = card(QString(), pulseContent);
  pulseCard->setObjectName(QStringLiteral("pulseCard"));
  layout->addWidget(pulseCard);

  auto* stats = new QWidget;
  auto* statsLayout = new QHBoxLayout(stats);
  statsLayout->setContentsMargins(0, 0, 0, 0);
  statsLayout->setSpacing(14);
  auto* accountsMetric = card(QString(), metric(QStringLiteral("—"), QStringLiteral("contas conectadas"), &_homeAccounts));
  accountsMetric->setObjectName(QStringLiteral("metricCard"));
  auto* campaignsMetric = card(QString(), metric(QStringLiteral("—"), QStringLiteral("campanhas registradas"), &_homeCampaigns));
  campaignsMetric->setObjectName(QStringLiteral("metricCard"));
  auto* successMetric = card(QString(), metric(QStringLiteral("—"), QStringLiteral("envios ok na última"), &_homeSuccess));
  successMetric->setObjectName(QStringLiteral("metricCard"));
  statsLayout->addWidget(accountsMetric);
  statsLayout->addWidget(campaignsMetric);
  statsLayout->addWidget(successMetric);
  layout->addWidget(stats);

  auto* lower = new QWidget;
  auto* lowerLayout = new QHBoxLayout(lower);
  lowerLayout->setContentsMargins(0, 0, 0, 0);
  lowerLayout->setSpacing(14);

  auto* sequence = new QWidget;
  auto* sequenceLayout = new QVBoxLayout(sequence);
  sequenceLayout->setContentsMargins(0, 0, 0, 0);
  sequenceLayout->setSpacing(0);
  const QStringList steps = {
      QStringLiteral("01   Conecte e valide as contas de destino"),
      QStringLiteral("02   Prepare o reservatório de mídia"),
      QStringLiteral("03   Inicie uma campanha monitorada")};
  for (const QString& step : steps) {
    auto* row = new QLabel(step);
    row->setObjectName(QStringLiteral("sequenceRow"));
    row->setMinimumHeight(42);
    sequenceLayout->addWidget(row);
  }
  lowerLayout->addWidget(card(QStringLiteral("Próxima sequência"), sequence), 3);

  auto* quick = new QWidget;
  auto* quickLayout = new QVBoxLayout(quick);
  quickLayout->setContentsMargins(0, 0, 0, 0);
  quickLayout->setSpacing(8);
  auto addQuick = [this, quickLayout](const QString& text, int destination) {
    auto* button = new QPushButton(text + QStringLiteral("   →"));
    button->setObjectName(QStringLiteral("quickAction"));
    button->setCursor(Qt::PointingHandCursor);
    connect(button, &QPushButton::clicked, this,
            [this, destination] { _navigation->setCurrentRow(destination); });
    quickLayout->addWidget(button);
  };
  addQuick(QStringLiteral("Checar prontidão"), 1);
  addQuick(QStringLiteral("Configurar integrações"), 2);
  addQuick(QStringLiteral("Gerenciar contas"), 5);
  addQuick(QStringLiteral("Consultar histórico"), 7);
  lowerLayout->addWidget(card(QStringLiteral("Acesso direto"), quick), 2);
  layout->addWidget(lower);
  layout->addStretch();

  return pageShell(QStringLiteral("Visão geral"),
                   QStringLiteral("Acompanhe o estado da operação e siga para a próxima ação."), body);
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
  configureTable(_readinessTable);
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
  _integrationSecurity = quietLabel(QStringLiteral("Proteção do Windows"));
  _integrationSecurity->setObjectName(QStringLiteral("securityBadge"));
  _integrationSecurity->setAlignment(Qt::AlignCenter);
  heroLayout->addWidget(_integrationSecurity);
  auto* heroCard = card(QString(), hero);
  heroCard->setObjectName(QStringLiteral("pulseCard"));
  layout->addWidget(heroCard);

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
  layout->addWidget(card(QStringLiteral("Ego4D · Conteúdo licenciado"), egoBody));

  auto* hostBody = new QWidget;
  auto* hostLayout = new QVBoxLayout(hostBody);
  hostLayout->setContentsMargins(0, 0, 0, 0);
  hostLayout->setSpacing(12);
  _hostingerStatus = new QLabel(QStringLiteral("Verificando token…"));
  _hostingerStatus->setObjectName(QStringLiteral("integrationStatus"));
  hostLayout->addWidget(_hostingerStatus);
  auto* hostHelp = quietLabel(QStringLiteral(
      "A Hostinger lê os códigos de verificação usados em novos cadastros. "
      "Para trocar a API, digite o novo token e salve; o valor protegido nunca é exibido."));
  hostHelp->setWordWrap(true);
  hostLayout->addWidget(hostHelp);
  auto* hostFormBody = new QWidget;
  auto* hostForm = new QFormLayout(hostFormBody);
  configureForm(hostForm);
  hostForm->setContentsMargins(0, 0, 0, 0);
  _hostingerToken = new QLineEdit;
  _hostingerToken->setEchoMode(QLineEdit::Password);
  _hostingerToken->setPlaceholderText(QStringLiteral("Token da API Mail da Hostinger"));
  hostForm->addRow(QStringLiteral("Token da API"), _hostingerToken);
  connect(_hostingerToken, &QLineEdit::textChanged, this, [this] {
    if (!_hostingerToken->text().trimmed().isEmpty())
      _hostingerTest->setEnabled(true);
  });
  _hostingerMailbox = new QLineEdit;
  _hostingerMailbox->setPlaceholderText(QStringLiteral("Em branco usa automaticamente a primeira caixa"));
  hostForm->addRow(QStringLiteral("ID da caixa"), _hostingerMailbox);
  hostLayout->addWidget(hostFormBody);
  auto* hostActions = new QHBoxLayout;
  hostActions->addStretch();
  _hostingerTest = new QPushButton(QStringLiteral("Testar conexão"));
  connect(_hostingerTest, &QPushButton::clicked,
          this, &MainWindow::testHostingerIntegration);
  hostActions->addWidget(_hostingerTest);
  _hostingerSave = primaryButton(QStringLiteral("Validar e salvar"));
  connect(_hostingerSave, &QPushButton::clicked,
          this, &MainWindow::saveHostingerIntegration);
  hostActions->addWidget(_hostingerSave);
  hostLayout->addLayout(hostActions);
  layout->addWidget(card(QStringLiteral("Hostinger · Códigos de verificação"), hostBody));

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
  layout->addWidget(card(QStringLiteral("Componentes incluídos no QMoney"), local));
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

  auto* scroll = new QScrollArea;
  scroll->setWidgetResizable(true);
  scroll->setFrameShape(QFrame::NoFrame);
  auto* content = new QWidget;
  auto* layout = new QVBoxLayout(content);
  layout->setContentsMargins(0, 0, 8, 0);
  layout->setSpacing(14);

  auto* sourceBody = new QWidget;
  auto* sourceLayout = new QHBoxLayout(sourceBody);
  sourceLayout->setContentsMargins(0, 0, 0, 0);
  _dataset = new QComboBox;
  configureCombo(_dataset, 420);
  _dataset->addItem(QStringLiteral("Conteúdo combinado"), QStringLiteral("all"));
  _dataset->addItem(QStringLiteral("Somente Ego4D"), QStringLiteral("ego4d"));
  _dataset->addItem(QStringLiteral("Somente HoloAssist"), QStringLiteral("holoassist"));
  _dataset->setCurrentIndex(0);
  fitComboPopup(_dataset);
  connect(_dataset, &QComboBox::currentIndexChanged, this, [this] { loadTasks(); });
  sourceLayout->addWidget(new QLabel(QStringLiteral("Origem")));
  sourceLayout->addWidget(_dataset, 1);
  auto* reloadTasks = new QPushButton(QStringLiteral("Recarregar categorias"));
  connect(reloadTasks, &QPushButton::clicked, this, &MainWindow::loadTasks);
  sourceLayout->addWidget(reloadTasks);
  layout->addWidget(card(QStringLiteral("Conteúdo da campanha"), sourceBody));

  auto* selection = new QWidget;
  auto* selectionLayout = new QHBoxLayout(selection);
  selectionLayout->setContentsMargins(0, 0, 0, 0);
  auto* accountCol = new QVBoxLayout;
  accountCol->addWidget(quietLabel(QStringLiteral("CONTAS DE DESTINO")));
  _campaignAccounts = new QListWidget;
  _campaignAccounts->setMinimumHeight(190);
  _campaignAccounts->setSpacing(2);
  _campaignAccounts->setUniformItemSizes(true);
  connect(_campaignAccounts, &QListWidget::itemChanged, this, [this] { loadTasks(); });
  accountCol->addWidget(_campaignAccounts);
  auto* taskCol = new QVBoxLayout;
  auto* taskHead = new QHBoxLayout;
  taskHead->addWidget(quietLabel(QStringLiteral("CATEGORIAS DO MINUTE")));
  taskHead->addStretch();
  auto* allTasks = new QPushButton(QStringLiteral("Marcar todas"));
  allTasks->setFlat(true);
  connect(allTasks, &QPushButton::clicked, this, [this] {
    for (int i = 0; i < _campaignTasks->count(); ++i) {
      auto* item = _campaignTasks->item(i);
      if (item->flags() & Qt::ItemIsEnabled) item->setCheckState(Qt::Checked);
    }
  });
  taskHead->addWidget(allTasks);
  taskCol->addLayout(taskHead);
  _campaignTasks = new QListWidget;
  _campaignTasks->setMinimumHeight(190);
  _campaignTasks->setSpacing(2);
  _campaignTasks->setUniformItemSizes(true);
  taskCol->addWidget(_campaignTasks);
  selectionLayout->addLayout(accountCol, 1);
  selectionLayout->addLayout(taskCol, 2);
  layout->addWidget(card(QStringLiteral("Seleção"), selection));

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
  _maxDuration = new QSpinBox;
  _maxDuration->setRange(1, 30);
  _maxDuration->setValue(30);
  _maxDuration->setSuffix(QStringLiteral(" min"));
  connect(_maxDuration, &QSpinBox::valueChanged, this, [this] { loadTasks(); });
  form->addRow(QStringLiteral("Duração máxima"), _maxDuration);
  _delayMode = new QComboBox;
  configureCombo(_delayMode, 420);
  _delayMode->addItem(QStringLiteral("Sem intervalo"), QStringLiteral("off"));
  _delayMode->addItem(QStringLiteral("Duração do clipe"), QStringLiteral("clip"));
  _delayMode->addItem(QStringLiteral("Intervalo fixo"), QStringLiteral("fixed"));
  _delayMode->setCurrentIndex(0);
  fitComboPopup(_delayMode);
  form->addRow(QStringLiteral("Intervalo"), _delayMode);
  _delaySeconds = new QSpinBox;
  _delaySeconds->setRange(0, 3600);
  _delaySeconds->setSuffix(QStringLiteral(" s"));
  form->addRow(QStringLiteral("Intervalo fixo"), _delaySeconds);
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
  hoursLayout->addWidget(_activeHours);
  hoursLayout->addWidget(_hourStart);
  hoursLayout->addWidget(new QLabel(QStringLiteral("e")));
  hoursLayout->addWidget(_hourEnd);
  hoursLayout->addStretch();
  form->addRow(QStringLiteral("Janela ativa"), hours);
  layout->addWidget(card(QStringLiteral("Parâmetros"), parameters));

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
  executionLayout->addLayout(actions);
  layout->addWidget(card(QStringLiteral("Execução"), execution));
  layout->addStretch();
  scroll->setWidget(content);
  bodyLayout->addWidget(scroll);

  return pageShell(QStringLiteral("Nova campanha"),
                   QStringLiteral("Escolha o conteúdo, calibre a operação e acompanhe cada envio."), body);
}

QWidget* MainWindow::buildAcceleratorPage() {
  auto* body = new QWidget;
  auto* layout = new QVBoxLayout(body);
  layout->setContentsMargins(0, 0, 0, 0);
  layout->setSpacing(14);

  auto* hero = new QWidget;
  auto* heroLayout = new QVBoxLayout(hero);
  heroLayout->setContentsMargins(0, 0, 0, 0);
  auto* line = new QHBoxLayout;
  _cacheState = new QLabel(QStringLiteral("Aguardando leitura"));
  _cacheState->setObjectName(QStringLiteral("pulseTitle"));
  line->addWidget(_cacheState);
  line->addStretch();
  _cacheNumbers = quietLabel(QStringLiteral("—"));
  line->addWidget(_cacheNumbers);
  heroLayout->addLayout(line);
  _cacheProgress = new QProgressBar;
  _cacheProgress->setRange(0, 100);
  heroLayout->addWidget(_cacheProgress);
  layout->addWidget(card(QStringLiteral("Reservatório de campanha"), hero));

  auto* config = new QWidget;
  auto* form = new QFormLayout(config);
  configureForm(form);
  form->setContentsMargins(0, 0, 0, 0);
  _cacheTask = new QComboBox;
  configureCombo(_cacheTask, 620);
  form->addRow(QStringLiteral("Tarefa HoloAssist"), _cacheTask);
  _cacheLimit = new QSpinBox;
  _cacheLimit->setRange(0, 1000);
  _cacheLimit->setSpecialValueText(QStringLiteral("Todos"));
  form->addRow(QStringLiteral("Limite de clipes"), _cacheLimit);
  _cacheReserve = new QSpinBox;
  _cacheReserve->setRange(5, 1000);
  _cacheReserve->setValue(50);
  _cacheReserve->setSuffix(QStringLiteral(" GiB livres"));
  form->addRow(QStringLiteral("Reserva de disco"), _cacheReserve);
  auto* actions = new QWidget;
  auto* actionLayout = new QHBoxLayout(actions);
  actionLayout->setContentsMargins(0, 0, 0, 0);
  auto* cleanup = new QPushButton(QStringLiteral("Limpar mídia baixada"));
  connect(cleanup, &QPushButton::clicked, this, [this] {
    if (QMessageBox::question(this, QStringLiteral("Limpar mídia"),
          QStringLiteral("Apagar a mídia e os sensores baixados? Catálogos, contas e histórico serão preservados."))
        != QMessageBox::Yes) return;
    _api.post(QStringLiteral("/api/storage/cleanup"), {}, [this](bool ok, const QJsonDocument& doc, const QString& error) {
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
  form->addRow(QString(), actions);
  layout->addWidget(card(QStringLiteral("Preparação"), config));
  layout->addStretch();

  return pageShell(QStringLiteral("Acelerador"),
                   QStringLiteral("Antecipe downloads e normalização para campanhas mais previsíveis."), body);
}

QWidget* MainWindow::buildAccountsPage() {
  auto* body = new QWidget;
  auto* layout = new QVBoxLayout(body);
  layout->setContentsMargins(0, 0, 0, 0);
  layout->setSpacing(14);

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
  layout->addWidget(card(QStringLiteral("Conectar conta"), formBody));

  _accountsTable = new QTableWidget(0, 4);
  configureTable(_accountsTable);
  _accountsTable->setHorizontalHeaderLabels(
      {QStringLiteral("Conta"), QStringLiteral("Organização"), QStringLiteral("Token"), QStringLiteral("Ações")});
  _accountsTable->horizontalHeader()->setSectionResizeMode(0, QHeaderView::Stretch);
  _accountsTable->horizontalHeader()->setSectionResizeMode(1, QHeaderView::Interactive);
  _accountsTable->horizontalHeader()->setSectionResizeMode(2, QHeaderView::Interactive);
  _accountsTable->horizontalHeader()->setSectionResizeMode(3, QHeaderView::Fixed);
  _accountsTable->setColumnWidth(1, 220);
  _accountsTable->setColumnWidth(2, 175);
  _accountsTable->setColumnWidth(3, 232);
  _accountsTable->setSelectionBehavior(QAbstractItemView::SelectRows);

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
  accountsLayout->addWidget(_accountsTable, 1);
  layout->addWidget(card(QStringLiteral("Contas cadastradas"), accountsBody), 1);

  return pageShell(QStringLiteral("Contas"),
                   QStringLiteral("Gerencie as identidades usadas no Minute e valide cada acesso."), body);
}

QWidget* MainWindow::buildBalancesPage() {
  auto* body = new QWidget;
  auto* layout = new QVBoxLayout(body);
  layout->setContentsMargins(0, 0, 0, 0);
  layout->setSpacing(14);
  auto* header = new QWidget;
  auto* headerLayout = new QHBoxLayout(header);
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
  headerLayout->addWidget(identityCopy, 1);
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
  layout->addWidget(card(QStringLiteral("Disponibilidade"), header));

  _balancesTable = new QTableWidget(0, 5);
  configureTable(_balancesTable);
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
  _balancesTable->setColumnWidth(4, 292);
  _balancesTable->horizontalHeaderItem(1)->setToolTip(
      QStringLiteral("Valor em dólar liberado para solicitar saque."));
  _balancesTable->horizontalHeaderItem(2)->setToolTip(
      QStringLiteral("Valor em dólar registrado pelo Crowtado que ainda não foi liberado para saque."));
  layout->addWidget(card(QStringLiteral("Saldos no Crowtado"), _balancesTable), 1);

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
    *usd = new QLabel(QStringLiteral("US$ 0,00"));
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
  _balancesExchange = quietLabel(QStringLiteral("Carregando cotação USD/BRL…"));
  _balancesExchange->setObjectName(QStringLiteral("balanceExchange"));
  summaryLayout->addWidget(_balancesExchange);
  layout->addWidget(card(QStringLiteral("Resumo em dólar"), summaryBody));

  return pageShell(QStringLiteral("Saldos"),
                   QStringLiteral("Acompanhe valores disponíveis e pendentes e solicite o link de saque."), body);
}

QWidget* MainWindow::buildHistoryPage() {
  auto* body = new QWidget;
  auto* layout = new QHBoxLayout(body);
  layout->setContentsMargins(0, 0, 0, 0);
  layout->setSpacing(14);
  _historyTable = new QTableWidget(0, 4);
  configureTable(_historyTable);
  _historyTable->setHorizontalHeaderLabels(
      {QStringLiteral("Início"), QStringLiteral("Contas"), QStringLiteral("Clipes"), QStringLiteral("Sucesso")});
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
  _historyDetail->setPlaceholderText(QStringLiteral("Selecione uma campanha para ver o registro completo."));
  connect(_historyTable, &QTableWidget::cellClicked, this, [this](int row, int) {
    const QString name = _historyTable->item(row, 0)->data(Qt::UserRole).toString();
    if (name.isEmpty()) return;
    _api.get(QStringLiteral("/api/logs/") + encoded(name),
             [this](bool ok, const QJsonDocument& doc, const QString& error) {
      if (!ok) return showError(QStringLiteral("Falha ao abrir registro"), error);
      const auto root = doc.object();
      const auto summary = root.value(QStringLiteral("summary")).toObject();
      QStringList lines;
      lines << QStringLiteral("RESULTADO DA CAMPANHA")
            << friendlyDate(root.value(QStringLiteral("started_at")).toString())
            << QString()
            << QStringLiteral("VISÃO GERAL")
            << QStringLiteral("%1 vídeo(s) · %2 concluído(s) · %3 ignorado(s) · %4 falha(s)")
                   .arg(summary.value(QStringLiteral("videos")).toInt())
                   .arg(summary.value(QStringLiteral("success")).toInt())
                   .arg(summary.value(QStringLiteral("skipped")).toInt())
                   .arg(summary.value(QStringLiteral("failed")).toInt())
            << QString()
            << QStringLiteral("POR CONTA");
      for (const auto accountValue : root.value(QStringLiteral("accounts")).toArray()) {
        const auto account = accountValue.toObject();
        const int success = account.value(QStringLiteral("success")).toInt();
        const int skipped = account.value(QStringLiteral("skipped")).toInt();
        const int failed = account.value(QStringLiteral("failed")).toInt();
        const QString marker = failed > 0 ? QStringLiteral("×")
                             : success > 0 ? QStringLiteral("✓") : QStringLiteral("•");
        lines << QStringLiteral("%1  %2").arg(marker, account.value(QStringLiteral("email")).toString())
              << QStringLiteral("    %1 concluído(s) · %2 ignorado(s) · %3 falha(s)")
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
              << QStringLiteral("    %1 concluído(s) · %2 ignorado(s) · %3 falha(s)")
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
  layout->addWidget(card(QStringLiteral("Campanhas"), _historyTable), 3);
  layout->addWidget(card(QStringLiteral("Registro"), _historyDetail), 2);
  return pageShell(QStringLiteral("Histórico"),
                   QStringLiteral("Audite campanhas anteriores e seus resultados por conta."), body);
}

void MainWindow::applyStructuralStyle(bool dark) {
  const QString bg = dark ? QStringLiteral("#111315") : QStringLiteral("#f1f3f2");
  const QString panel = dark ? QStringLiteral("#1a1d1f") : QStringLiteral("#ffffff");
  const QString sidebar = QStringLiteral("#0e1011");
  const QString text = dark ? QStringLiteral("#f2f3f1") : QStringLiteral("#202426");
  const QString muted = dark ? QStringLiteral("#969da1") : QStringLiteral("#697277");
  const QString border = dark ? QStringLiteral("#2c3134") : QStringLiteral("#d8dddb");
  const QString selected = QStringLiteral("#35241c");
  const QString field = dark ? QStringLiteral("#15181a") : QStringLiteral("#f7f8f7");
  const QString soft = dark ? QStringLiteral("#202427") : QStringLiteral("#edf0ee");

  setStyleSheet(QStringLiteral(R"(
    * { font-family: "Inter", "Segoe UI"; }
    #workspace { background: %1; }
    #sidebar { background: %3; color: #f7f8f7; border-right: 1px solid #282c2e; }
    #brandMark { background: transparent; border: none; }
    #brand { color: #ffffff; font-size: 21px; font-weight: 750; letter-spacing: -0.3px; }
    #brandRole { color: #747c80; font-size: 9px; font-weight: 750; letter-spacing: 1.3px; }
    #navigationLabel { color: #666e72; font-size: 9px; font-weight: 800; letter-spacing: 1.2px; padding-left: 7px; padding-bottom: 4px; }
    #sidebar #quiet { color: #858d91; font-size: 9px; font-weight: 700; letter-spacing: 0.8px; }
    #navigation { background: transparent; color: #abb1b4; outline: none; border: none; }
    #navigation::item { border-radius: 7px; padding-left: 13px; margin: 2px 0; }
    #navigation::item:hover { background: #191c1e; color: #ffffff; }
    #navigation::item:selected { background: %7; color: #ffffff; font-weight: 650; border-left: 2px solid #ff7a36; }
    #connectionCard { background: #151819; border: 1px solid #292e30; border-radius: 8px; }
    #backendState { color: #dfe3e1; font-size: 12px; font-weight: 650; }
    #sidebarUtility { color: #bac0c2; background: #171a1c; border: 1px solid #292e30; border-radius: 7px; text-align: left; padding: 8px 12px; }
    #sidebarUtility:hover { color: white; background: #202427; border-color: #3a4144; }
    #pageContext { color: #ff7a36; font-size: 9px; font-weight: 850; letter-spacing: 1.45px; }
    #modeBadge { color: %5; background: %8; border: 1px solid %6; border-radius: 10px; padding: 4px 10px; font-size: 9px; font-weight: 750; letter-spacing: 0.8px; }
    #pageTitle { color: %4; font-size: 30px; font-weight: 760; letter-spacing: -0.6px; }
    #pageSubtitle { color: %5; font-size: 13px; }
    #card, #pulseCard, #metricCard { background: %2; border: 1px solid %6; border-radius: 9px; }
    #pulseCard { border-top: 2px solid #ff7a36; }
    #metricCard:hover { border-color: #ff7a36; }
    #cardTitle { color: %4; font-size: 13px; font-weight: 720; letter-spacing: 0.15px; }
    #metricCaption { color: %5; font-size: 9px; font-weight: 800; letter-spacing: 1.1px; }
    #metricValue { color: %4; font-family: "Cascadia Mono", Consolas; font-size: 32px; font-weight: 700; }
    #metricTiming { color: %5; font-size: 8px; font-weight: 700; letter-spacing: 0.9px; }
    #balanceTotalCaption { color: %5; font-size: 9px; font-weight: 800; letter-spacing: 1.05px; }
    #balanceTotalUsd { color: %4; font-family: "Cascadia Mono", Consolas; font-size: 23px; font-weight: 720; }
    #balanceTotalBrl { color: #ff7a36; font-family: "Cascadia Mono", Consolas; font-size: 12px; font-weight: 680; }
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
    #kicker { color: #ff7a36; font-size: 9px; font-weight: 850; letter-spacing: 1.35px; }
    #signalRail { background: #111416; border: 1px solid #2d3336; border-radius: 7px; }
    #signalLabel { color: #ff7a36; font-size: 8px; font-weight: 850; letter-spacing: 1.1px; }
    #signalDot { color: #48c78e; font-size: 24px; }
    #signalPort { color: #8d9599; font-family: "Cascadia Mono", Consolas; font-size: 9px; font-weight: 650; }
    #sequenceRow { color: %4; border-bottom: 1px solid %6; padding-left: 4px; font-family: "Cascadia Mono", Consolas; font-size: 11px; }
    #quickAction { color: %4; background: %9; border: 1px solid %6; border-radius: 6px; text-align: left; padding: 8px 12px; font-weight: 620; }
    #quickAction:hover { border-color: #ff7a36; color: #ff7a36; }
    #quiet { color: %5; }
    #appStatusBar { background: %2; border-top: 1px solid %6; }
    #statusVersion { color: %5; font-family: "Cascadia Mono", Consolas; font-size: 9px; }
    QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox, QPlainTextEdit, QListWidget, QTableWidget {
      color: %4; background-color: %8; border: 1px solid %6; border-radius: 6px;
    }
    QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox { min-height: 40px; padding-left: 12px; padding-right: 12px; }
    QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus, QPlainTextEdit:focus, QListWidget:focus { border-color: #ff7a36; }
    QComboBox { padding-right: 44px; }
    QComboBox::drop-down { width: 40px; border-left: 1px solid %6; }
    QComboBox QAbstractItemView { color: %4; background-color: %2; border: 1px solid %6; selection-background-color: #d9652b; padding: 6px; }
    QComboBox QAbstractItemView::item { min-height: 38px; padding: 7px 12px; }
    QListWidget::item { padding: 7px 9px; border-radius: 4px; }
    QListWidget::item:selected { background: #3b2920; color: white; }
    QTableWidget { gridline-color: %6; selection-background-color: #3b2920; selection-color: white; alternate-background-color: %9; }
    QTableWidget::item { padding: 8px 10px; border-bottom: 1px solid %6; }
    QHeaderView::section { color: %5; background: %2; border: none; border-bottom: 1px solid %6; padding: 9px 10px; font-size: 9px; font-weight: 800; }
    QTableCornerButton::section { background: %2; border: none; border-bottom: 1px solid %6; }
    QPlainTextEdit { font-family: "Cascadia Mono", Consolas, monospace; padding: 10px; }
    #campaignTimeline { font-family: "Inter", "Segoe UI"; font-size: 12px; line-height: 1.35; padding: 12px; }
    QProgressBar { min-height: 7px; max-height: 7px; background: %8; border: none; border-radius: 3px; }
    QProgressBar::chunk { background: #ff7a36; border-radius: 3px; }
    #campaignProgress { min-height: 22px; max-height: 22px; color: %4; text-align: center; font-family: "Cascadia Mono", Consolas; font-size: 9px; font-weight: 700; }
    QScrollBar:vertical { background: transparent; width: 10px; margin: 2px; }
    QScrollBar::handle:vertical { background: %6; min-height: 30px; border-radius: 4px; }
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
    QScrollBar:horizontal { background: transparent; height: 10px; margin: 2px; }
    QScrollBar::handle:horizontal { background: %6; min-width: 30px; border-radius: 4px; }
    QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0; }
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
    migrateLegacyState(libraryRoot, workingDirectory);
    environment.insert(QStringLiteral("QMONEY_USER_ROOT"), workingDirectory);
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
  _backend.setWorkingDirectory(workingDirectory);
  _backend.setProcessEnvironment(environment);
  _backend.setProcessChannelMode(QProcess::MergedChannels);
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
  ++_probeAttempts;
  _api.get(QStringLiteral("/api/diagnostics"),
           [this](bool ok, const QJsonDocument& document, const QString&) {
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
      refreshCurrentPage();
    } else if (ok && !serviceVersion.isEmpty()) {
      setBackendReady(false, QStringLiteral("Ajustando versão do motor…"));
    } else if (_probeAttempts > 22) {
      _backendProbe.stop();
      setBackendReady(false, QStringLiteral("Serviço indisponível"));
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
    const QString healthPath = qApp->property("updateHealthPath").toString();
    if (!healthPath.isEmpty()) {
      QSaveFile marker(healthPath);
      if (marker.open(QIODevice::WriteOnly)) {
        marker.write("ok\n");
        marker.commit();
      }
      qApp->setProperty("updateHealthPath", QString());
    }
  }
}

void MainWindow::navigate(int index) {
  if (index < 0) return;
  _pages->setCurrentIndex(index);
  if (index == 3 && _campaignStop->isEnabled()) _campaignPoll.start();
  else if (index != 3) _campaignPoll.stop();
  if (index != 4) _cachePoll.stop();
  if (index != 6) _balancePoll.stop();
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
    case 5: loadAccounts(); break;
    case 6: loadBalances(); break;
    case 7: loadHistory(); break;
    default: break;
  }
}

void MainWindow::showError(const QString& title, const QString& error) {
  QMessageBox::warning(this, title, error);
  setStatus(error);
}

void MainWindow::setStatus(const QString& text) {
  const QString full = text.simplified();
  const QString compact = full.size() > 180 ? full.left(177) + QStringLiteral("…") : full;
  _status->setText(compact);
  _status->setToolTip(full == compact ? QString() : full);
}

void MainWindow::loadHome() {
  _api.get(QStringLiteral("/api/accounts"), [this](bool ok, const QJsonDocument& doc, const QString& error) {
    if (!ok) return setStatus(error);
    _homeAccounts->setText(QString::number(doc.object().value(QStringLiteral("accounts")).toArray().size()));
  });
  _api.get(QStringLiteral("/api/logs"), [this](bool ok, const QJsonDocument& doc, const QString& error) {
    if (!ok) return setStatus(error);
    const auto logs = doc.object().value(QStringLiteral("logs")).toArray();
    _homeCampaigns->setText(QString::number(logs.size()));
    if (logs.isEmpty()) {
      _homeSuccess->setText(QStringLiteral("—"));
      _homePulseBody->setText(QStringLiteral("Ainda não há campanhas. Configure a primeira quando estiver pronto."));
    } else {
      const auto last = logs.first().toObject();
      _homeSuccess->setText(QStringLiteral("%1/%2")
          .arg(last.value(QStringLiteral("ok")).toInt())
          .arg(last.value(QStringLiteral("sends")).toInt()));
      _homePulseBody->setText(QStringLiteral("Última execução: %1 · %2 clipe(s) · %3 conta(s)")
          .arg(friendlyDate(last.value(QStringLiteral("started_at")).toString()))
          .arg(last.value(QStringLiteral("items")).toInt())
          .arg(last.value(QStringLiteral("accounts")).toArray().size()));
    }
  });
  _api.get(QStringLiteral("/api/campaigns/current"),
           [this](bool ok, const QJsonDocument& doc, const QString&) {
    if (!ok) return;
    const auto snap = doc.object();
    const QString state = snap.value(QStringLiteral("state")).toString();
    const auto totals = snap.value(QStringLiteral("totals")).toObject();
    const int total = totals.value(QStringLiteral("total_sends")).toInt();
    const int done = totals.value(QStringLiteral("done_sends")).toInt();
    const bool running = state == QStringLiteral("running") || state == QStringLiteral("stopping");
    _homePulseTitle->setText(running ? QStringLiteral("Campanha em movimento")
                                     : QStringLiteral("Operação pronta para a próxima campanha"));
    if (running) _homePulseBody->setText(snap.value(QStringLiteral("current")).toString());
    _homePulseProgress->setValue(total > 0 ? done * 100 / total : 0);
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

    const QString hostHint = host.value(QStringLiteral("token_hint")).toString();
    const QString mailboxHint = host.value(QStringLiteral("mailbox_hint")).toString();
    _hostingerStatus->setText(hostConfigured
        ? QStringLiteral("● Token protegido %1 · caixa %2")
              .arg(hostHint, mailboxHint.isEmpty() ? QStringLiteral("automática") : mailboxHint)
        : QStringLiteral("● Token ainda não configurado"));
    _hostingerStatus->setProperty("integrationState", hostConfigured ? "ok" : "missing");
    _hostingerStatus->style()->unpolish(_hostingerStatus);
    _hostingerStatus->style()->polish(_hostingerStatus);
    _hostingerToken->setPlaceholderText(hostConfigured
        ? QStringLiteral("Protegido %1 — digite somente para substituir").arg(hostHint)
        : QStringLiteral("Token da API Mail da Hostinger"));
    _hostingerTest->setEnabled(hostConfigured);
    _hostingerSave->setText(hostConfigured
        ? QStringLiteral("Salvar alterações")
        : QStringLiteral("Validar e salvar"));

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
  if (!_hostingerToken->text().trimmed().isEmpty())
    body.insert(QStringLiteral("token"), _hostingerToken->text().trimmed());
  // O campo sempre vai no PUT: vazio significa voltar para a primeira caixa
  // automática; omitir preservaria para sempre um ID antigo.
  body.insert(QStringLiteral("mailbox_id"), _hostingerMailbox->text().trimmed());
  _hostingerSave->setEnabled(false);
  _hostingerSave->setText(QStringLiteral("Validando…"));
  _api.put(QStringLiteral("/api/integrations/hostinger"), body,
           [this](bool ok, const QJsonDocument&, const QString& error) {
    _hostingerSave->setEnabled(true);
    if (!ok) return showError(QStringLiteral("Hostinger não configurada"), error);
    _hostingerToken->clear();
    _hostingerMailbox->clear();
    setStatus(QStringLiteral("Configuração da Hostinger validada e protegida pelo Windows."));
    loadIntegrations();
    loadReadiness();
  });
}

void MainWindow::testHostingerIntegration() {
  QJsonObject body;
  if (!_hostingerToken->text().trimmed().isEmpty())
    body.insert(QStringLiteral("token"), _hostingerToken->text().trimmed());
  if (!_hostingerMailbox->text().trimmed().isEmpty())
    body.insert(QStringLiteral("mailbox_id"), _hostingerMailbox->text().trimmed());
  _hostingerTest->setEnabled(false);
  _hostingerTest->setText(QStringLiteral("Testando…"));
  _api.post(QStringLiteral("/api/integrations/hostinger/test"), body,
            [this](bool ok, const QJsonDocument& doc, const QString& error) {
    _hostingerTest->setEnabled(true);
    _hostingerTest->setText(QStringLiteral("Testar conexão"));
    if (!ok) return showError(QStringLiteral("Teste Hostinger"), error);
    const int boxes = doc.object().value(QStringLiteral("mailboxes")).toInt();
    QMessageBox::information(this, QStringLiteral("Hostinger conectada"),
        QStringLiteral("Credencial válida · %1 caixa(s) disponível(is).").arg(boxes));
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
  _api.get(QStringLiteral("/api/accounts"), [this](bool ok, const QJsonDocument& doc, const QString& error) {
    if (!ok) return showError(QStringLiteral("Falha ao carregar contas"), error);
    const QSignalBlocker blocker(_campaignAccounts);
    _campaignAccounts->clear();
    for (const auto value : doc.object().value(QStringLiteral("accounts")).toArray()) {
      const auto account = value.toObject();
      auto* item = new QListWidgetItem(account.value(QStringLiteral("email")).toString());
      item->setSizeHint(QSize(0, 38));
      item->setFlags(item->flags() | Qt::ItemIsUserCheckable);
      item->setCheckState(Qt::Checked);
      item->setData(Qt::UserRole, account.value(QStringLiteral("email")).toString());
      _campaignAccounts->addItem(item);
    }
    loadTasks();
  });
  pollCampaign();
}

void MainWindow::loadTasks() {
  QString account;
  for (int i = 0; i < _campaignAccounts->count(); ++i) {
    if (_campaignAccounts->item(i)->checkState() == Qt::Checked) {
      account = _campaignAccounts->item(i)->data(Qt::UserRole).toString();
      break;
    }
  }
  if (account.isEmpty()) {
    _campaignTasks->clear();
    return;
  }
  _campaignTasks->clear();
  _campaignTasks->addItem(QStringLiteral("Carregando categorias…"));
  const QString path = QStringLiteral("/api/tasks?email=%1&max_dur_s=%2&dataset=%3")
      .arg(encoded(account)).arg(_maxDuration->value() * 60)
      .arg(encoded(_dataset->currentData().toString()));
  _api.get(path, [this](bool ok, const QJsonDocument& doc, const QString& error) {
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
      if (task.value(QStringLiteral("boosted")).toBool()) label += QStringLiteral("  ·  turbinada");
      if (!available) label += QStringLiteral("  ·  sem clipe compatível");
      auto* item = new QListWidgetItem(label);
      item->setSizeHint(QSize(0, 38));
      item->setFlags(item->flags() | Qt::ItemIsUserCheckable);
      item->setData(Qt::UserRole, jsonId(task.value(QStringLiteral("id"))));
      item->setCheckState(available ? Qt::Checked : Qt::Unchecked);
      if (available) ++compatible;
      if (!available) {
        item->setFlags(item->flags() & ~Qt::ItemIsEnabled);
        item->setToolTip(QStringLiteral("Categoria sem clipe compatível no conjunto escolhido."));
      }
      _campaignTasks->addItem(item);
    }
    _campaignStart->setEnabled(compatible > 0 && !_campaignStop->isEnabled());
    if (compatible == 0) {
      setStatus(QStringLiteral("Nenhuma categoria tem clipe compatível nesta origem e duração."));
    } else {
      setStatus(QStringLiteral("%1 categoria(s) compatível(is) de %2 carregadas.")
                    .arg(compatible).arg(_campaignTasks->count()));
    }
  });
}

void MainWindow::startCampaign() {
  QJsonArray accounts;
  for (int i = 0; i < _campaignAccounts->count(); ++i) {
    auto* item = _campaignAccounts->item(i);
    if (item->checkState() == Qt::Checked) accounts.append(item->data(Qt::UserRole).toString());
  }
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
      {QStringLiteral("accounts"), accounts},
      {QStringLiteral("dataset"), _dataset->currentData().toString()},
      {QStringLiteral("tasks"), tasks},
      {QStringLiteral("count"), 1},
      {QStringLiteral("target_hours"), _targetHours->value()},
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
  _campaignStart->setText(QStringLiteral("Executando preflight…"));
  _api.post(QStringLiteral("/api/campaigns/preflight"), body,
            [this, body](bool ok, const QJsonDocument& doc, const QString& error) {
    if (!ok) {
      _campaignStart->setText(QStringLiteral("Iniciar campanha"));
      _campaignStart->setEnabled(true);
      return showError(QStringLiteral("Preflight não concluído"), error);
    }
    const auto result = doc.object();
    const auto blockers = result.value(QStringLiteral("blockers")).toArray();
    const auto warnings = result.value(QStringLiteral("warnings")).toArray();
    QStringList blockerLines;
    for (const auto& value : blockers) blockerLines << QStringLiteral("• ") + value.toString();
    QStringList warningLines;
    for (const auto& value : warnings) warningLines << QStringLiteral("• ") + value.toString();
    if (!result.value(QStringLiteral("ok")).toBool() || !blockerLines.isEmpty()) {
      _campaignStart->setText(QStringLiteral("Iniciar campanha"));
      _campaignStart->setEnabled(true);
      return showError(QStringLiteral("Campanha bloqueada pelo preflight"),
                       blockerLines.join(QLatin1Char('\n')));
    }

    const auto accountInfo = result.value(QStringLiteral("accounts")).toObject();
    const auto taskInfo = result.value(QStringLiteral("tasks")).toObject();
    QString preview = QStringLiteral(
        "%1 conta(s) validada(s)\n%2 categoria(s) compatível(is)\n%3 clipes disponíveis\n%4 envio(s) estimado(s)")
        .arg(accountInfo.value(QStringLiteral("validated")).toInt())
        .arg(taskInfo.value(QStringLiteral("compatible")).toInt())
        .arg(result.value(QStringLiteral("clips")).toInt())
        .arg(result.value(QStringLiteral("estimated_sends")).toInt());
    if (!warningLines.isEmpty())
      preview += QStringLiteral("\n\nAtenções:\n") + warningLines.join(QLatin1Char('\n'));
    preview += QStringLiteral("\n\nIniciar esta campanha agora?");
    if (QMessageBox::question(this, QStringLiteral("Prévia da campanha"), preview,
                              QMessageBox::Yes | QMessageBox::No, QMessageBox::No)
        != QMessageBox::Yes) {
      _campaignStart->setText(QStringLiteral("Iniciar campanha"));
      _campaignStart->setEnabled(true);
      return;
    }

    _campaignStart->setText(QStringLiteral("Iniciando…"));
    _api.post(QStringLiteral("/api/campaigns"), body,
              [this](bool started, const QJsonDocument&, const QString& startError) {
      _campaignStart->setText(QStringLiteral("Iniciar campanha"));
      if (!started) {
        _campaignStart->setEnabled(true);
        return showError(QStringLiteral("Campanha não iniciada"), startError);
      }
      _lastCampaignSeq = 0;
      _campaignFeed->clear();
      _campaignPoll.start();
      pollCampaign();
      setStatus(QStringLiteral("Campanha iniciada após preflight aprovado."));
    });
  });
}

void MainWindow::pollCampaign() {
  _api.get(QStringLiteral("/api/campaigns/current?since=%1").arg(_lastCampaignSeq),
           [this](bool ok, const QJsonDocument& doc, const QString&) {
    if (!ok) return;
    const auto snap = doc.object();
    const QString state = snap.value(QStringLiteral("state")).toString();
    const bool running = state == QStringLiteral("running") || state == QStringLiteral("stopping");
    _campaignStop->setEnabled(running && state != QStringLiteral("stopping"));
    _campaignStart->setEnabled(!running);
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
    if (!running) _campaignPoll.stop();
  });
}

void MainWindow::loadAccelerator() {
  QString path = QStringLiteral("/api/holo-cache");
  if (_cacheTask->count()) {
    path += QStringLiteral("?task=%1&limit=%2").arg(encoded(_cacheTask->currentText())).arg(_cacheLimit->value());
  }
  _api.get(path, [this](bool ok, const QJsonDocument& doc, const QString& error) {
    if (!ok) return setStatus(error);
    const auto root = doc.object();
    if (_cacheTask->count() == 0) {
      for (const auto task : root.value(QStringLiteral("tasks")).toArray()) _cacheTask->addItem(task.toString());
      if (_cacheTask->count()) _cacheTask->setCurrentIndex(0);
      fitComboPopup(_cacheTask);
    }
    const auto cache = root.value(QStringLiteral("cache")).toObject();
    const auto runner = root.value(QStringLiteral("runner")).toObject();
    const int total = cache.value(QStringLiteral("total")).toInt();
    const int ready = cache.value(QStringLiteral("ready")).toInt();
    const int partial = cache.value(QStringLiteral("partial")).toInt();
    const int pending = cache.value(QStringLiteral("pending")).toInt();
    const QString state = runner.value(QStringLiteral("state")).toString();
    const bool running = state == QStringLiteral("running") || state == QStringLiteral("stopping");
    _cacheState->setText(running ? runner.value(QStringLiteral("current")).toString()
                                 : QStringLiteral("Reservatório medido"));
    _cacheNumbers->setText(QStringLiteral("%1 prontos · %2 parciais · %3 pendentes")
                               .arg(ready).arg(partial).arg(pending));
    _cacheProgress->setValue(total > 0 ? ready * 100 / total : 0);
    _cacheStart->setEnabled(!running && cache.value(QStringLiteral("catalog_error")).toString().isEmpty());
    _cacheStop->setEnabled(running && state != QStringLiteral("stopping"));
    if (running) _cachePoll.start(); else _cachePoll.stop();
  });
}

void MainWindow::startAccelerator() {
  if (_cacheTask->currentText().isEmpty()) return;
  QJsonObject body{{QStringLiteral("task"), _cacheTask->currentText()},
                   {QStringLiteral("min_free_gb"), _cacheReserve->value()}};
  if (_cacheLimit->value() > 0) body.insert(QStringLiteral("limit"), _cacheLimit->value());
  else body.insert(QStringLiteral("limit"), QJsonValue::Null);
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

void MainWindow::loadAccounts() {
  _api.get(QStringLiteral("/api/accounts"), [this](bool ok, const QJsonDocument& doc, const QString& error) {
    if (!ok) {
      if (_accountsCheckAll) _accountsCheckAll->setEnabled(true);
      return showError(QStringLiteral("Falha ao carregar contas"), error);
    }
    const auto accounts = doc.object().value(QStringLiteral("accounts")).toArray();
    _accountsTable->setRowCount(accounts.size());
    int row = 0;
    for (const auto value : accounts) {
      const auto account = value.toObject();
      const QString email = account.value(QStringLiteral("email")).toString();
      _accountsTable->setItem(row, 0, cell(email));
      _accountsTable->setItem(row, 1, cell(account.value(QStringLiteral("org_key")).toString(QStringLiteral("não verificada"))));
      const qint64 expiry = static_cast<qint64>(account.value(QStringLiteral("expires_at")).toDouble());
      _accountsTable->setItem(row, 2, cell(expiry > QDateTime::currentSecsSinceEpoch()
          ? QStringLiteral("válido") : QStringLiteral("renovação necessária")));
      auto* actions = new QWidget;
      auto* actionsLayout = new QHBoxLayout(actions);
      actionsLayout->setContentsMargins(5, 5, 5, 5);
      actionsLayout->setSpacing(7);
      auto* check = new QPushButton(QStringLiteral("Verificar"));
      check->setMinimumSize(86, 32);
      connect(check, &QPushButton::clicked, this, [this, email] {
        _api.post(QStringLiteral("/api/accounts/") + encoded(email) + QStringLiteral("/check"), {},
          [this](bool ok, const QJsonDocument&, const QString& error) {
            if (!ok) showError(QStringLiteral("Conta não validada"), error);
            else { setStatus(QStringLiteral("Conta validada.")); loadAccounts(); }
          });
      });
      auto* remove = new QPushButton(QStringLiteral("Remover"));
      remove->setMinimumSize(86, 32);
      connect(remove, &QPushButton::clicked, this, [this, email] {
        if (QMessageBox::question(this, QStringLiteral("Remover conta"),
              QStringLiteral("Remover %1 deste QMoney?").arg(email)) != QMessageBox::Yes) return;
        _api.remove(QStringLiteral("/api/accounts/") + encoded(email),
                    [this](bool ok, const QJsonDocument&, const QString& error) {
          if (!ok) showError(QStringLiteral("Conta não removida"), error);
          else loadAccounts();
        });
      });
      actionsLayout->addWidget(check);
      actionsLayout->addWidget(remove);
      _accountsTable->setCellWidget(row, 3, actions);
      ++row;
    }
    if (_accountsCheckAll) _accountsCheckAll->setEnabled(!accounts.isEmpty());
    setStatus(QStringLiteral("%1 conta(s) cadastrada(s).").arg(accounts.size()));
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
    setStatus(QStringLiteral("Verificação concluída: %1 ativas · %2 desativadas · %3 com erro · %4 no total.")
                  .arg(active).arg(disabled).arg(errors).arg(total));
    loadAccounts();
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
  _api.post(endpoint, {{QStringLiteral("email"), email}, {QStringLiteral("password"), password}},
            [this](bool ok, const QJsonDocument&, const QString& error) {
    _accountAdd->setEnabled(true);
    _accountRegister->setEnabled(true);
    if (!ok) return showError(QStringLiteral("Conta não conectada"), error);
    _accountEmail->clear();
    _accountPassword->clear();
    setStatus(QStringLiteral("Conta conectada."));
    loadAccounts();
  });
}

void MainWindow::loadBalances() {
  _api.get(QStringLiteral("/api/balances"), [this](bool ok, const QJsonDocument& doc, const QString& error) {
    if (!ok) return showError(QStringLiteral("Falha ao carregar saldos"), error);
    const auto root = doc.object();
    const auto accounts = root.value(QStringLiteral("accounts")).toArray();
    const auto balances = root.value(QStringLiteral("balances")).toObject();
    const auto withPassword = root.value(QStringLiteral("with_password")).toArray();
    const auto runner = root.value(QStringLiteral("runner")).toObject();
    const auto exchange = root.value(QStringLiteral("exchange")).toObject();
    QStringList passwordAccounts;
    for (const auto value : withPassword) passwordAccounts << value.toString();
    _balancesTable->setRowCount(accounts.size());
    qint64 approvedTotal = 0;
    qint64 pendingTotal = 0;
    int row = 0;
    for (const auto value : accounts) {
      const QString email = value.toString();
      const auto balance = balances.value(email).toObject();
      _balancesTable->setItem(row, 0, cell(email));
      const bool hasAvailable = balance.value(QStringLiteral("availableCents")).isDouble();
      const bool hasPending = balance.value(QStringLiteral("pendingCents")).isDouble();
      const qint64 availableCents = hasAvailable
          ? static_cast<qint64>(balance.value(QStringLiteral("availableCents")).toDouble()) : 0;
      const qint64 pendingCents = hasPending
          ? static_cast<qint64>(balance.value(QStringLiteral("pendingCents")).toDouble()) : 0;
      if (hasAvailable) approvedTotal += availableCents;
      if (hasPending) pendingTotal += pendingCents;
      QString available = hasAvailable
          ? usdMoney(availableCents)
          : QStringLiteral("—");
      QString pending = hasPending
          ? usdMoney(pendingCents)
          : QStringLiteral("—");
      if (!balance.value(QStringLiteral("error")).toString().isEmpty()) {
        available = QStringLiteral("erro");
        pending = QStringLiteral("erro");
      }
      auto* availableItem = cell(available);
      auto* pendingItem = cell(pending);
      availableItem->setTextAlignment(Qt::AlignRight | Qt::AlignVCenter);
      pendingItem->setTextAlignment(Qt::AlignRight | Qt::AlignVCenter);
      _balancesTable->setItem(row, 1, availableItem);
      _balancesTable->setItem(row, 2, pendingItem);
      _balancesTable->setItem(row, 3, cell(friendlyDate(balance.value(QStringLiteral("updated_at")).toString())));
      const bool hasPassword = passwordAccounts.contains(email);
      const bool hasBalanceError = !balance.value(QStringLiteral("error")).toString().isEmpty();
      auto* actions = new QWidget;
      auto* actionsLayout = new QHBoxLayout(actions);
      actionsLayout->setContentsMargins(5, 5, 5, 5);
      actionsLayout->setSpacing(7);
      auto* credentials = new QPushButton(
          hasPassword ? QStringLiteral("Alterar acesso")
                      : QStringLiteral("Conectar Crowtado"));
      credentials->setMinimumHeight(32);
      credentials->setToolTip(QStringLiteral(
          "Informa e valida a senha desta identidade diretamente no Crowtado."));
      connect(credentials, &QPushButton::clicked, this,
              [this, email] { configureCrowtadoAccess(email); });
      actionsLayout->addWidget(credentials);
      auto* withdraw = new QPushButton(QStringLiteral("Solicitar saque"));
      withdraw->setMinimumHeight(32);
      withdraw->setEnabled(hasPassword && !hasBalanceError);
      connect(withdraw, &QPushButton::clicked, this, [this, email, withdraw] {
        withdraw->setEnabled(false);
        _api.post(QStringLiteral("/api/balances/withdraw"), {{QStringLiteral("email"), email}},
                  [this, withdraw](bool ok, const QJsonDocument& doc, const QString& error) {
          withdraw->setEnabled(true);
          if (!ok) return showError(QStringLiteral("Saque não solicitado"), error);
          QMessageBox::information(this, QStringLiteral("Solicitação enviada"),
                                   doc.object().value(QStringLiteral("message")).toString());
        });
      });
      actionsLayout->addWidget(withdraw);
      _balancesTable->setCellWidget(row, 4, actions);
      ++row;
    }
    _balancesApprovedUsd->setText(usdMoney(approvedTotal));
    _balancesPendingUsd->setText(usdMoney(pendingTotal));
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
    _balancesState->setText(running
        ? runner.value(QStringLiteral("current")).toString(QStringLiteral("Consultando contas…"))
        : QStringLiteral("%1 identidade(s) · Crowtado conectado em %2")
              .arg(accounts.size()).arg(passwordAccounts.size()));
    if (running) _balancePoll.start(); else _balancePoll.stop();
  });
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
