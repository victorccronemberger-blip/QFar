#include "MainWindow.hpp"

#include <QApplication>
#include <QCheckBox>
#include <QCloseEvent>
#include <QComboBox>
#include <QDateTime>
#include <QDebug>
#include <QDesktopServices>
#include <QDir>
#include <QDialog>
#include <QDialogButtonBox>
#include <QDoubleSpinBox>
#include <QFileInfo>
#include <QFile>
#include <QFormLayout>
#include <QFrame>
#include <QGroupBox>
#include <QHeaderView>
#include <QHBoxLayout>
#include <QJsonDocument>
#include <QJsonValue>
#include <QLabel>
#include <QLineEdit>
#include <QListWidget>
#include <QLocale>
#include <QMessageBox>
#include <QPlainTextEdit>
#include <QProgressBar>
#include <QProcessEnvironment>
#include <QPushButton>
#include <QScrollArea>
#include <QSettings>
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

namespace {
QString jsonId(const QJsonValue& value) {
  if (value.isString()) return value.toString();
  if (value.isDouble()) return QString::number(value.toDouble(), 'f', 0);
  return value.toVariant().toString();
}

QTableWidgetItem* cell(const QString& text) {
  auto* item = new QTableWidgetItem(text);
  item->setFlags(item->flags() & ~Qt::ItemIsEditable);
  return item;
}

QLabel* quietLabel(const QString& text) {
  auto* label = new QLabel(text);
  label->setObjectName(QStringLiteral("quiet"));
  label->setWordWrap(true);
  return label;
}
}  // namespace

MainWindow::MainWindow(oclero::qlementine::QlementineStyle* style, QWidget* parent)
    : QMainWindow(parent), _style(style), _api(this), _backend(this) {
  setWindowTitle(QStringLiteral("QMoney — Central de campanhas"));
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
            if (interactive) QMessageBox::warning(this, QStringLiteral("Atualizações"), message);
          });
  connect(&_updates, &UpdateManager::checkFinished, this,
          [this](bool available, bool interactive) {
            _updateButton->setEnabled(true);
            if (!available) {
              _updateButton->setText(QStringLiteral("✓  QMoney %1").arg(QCoreApplication::applicationVersion()));
              if (interactive)
                QMessageBox::information(this, QStringLiteral("Atualizações"),
                                         QStringLiteral("Você já está usando a versão mais recente."));
            }
          });
  connect(&_updates, &UpdateManager::updateAvailable, this,
          [this](const QString& version, const QString& notes) {
            _updateButton->setEnabled(true);
            _updateButton->setText(QStringLiteral("⬇  Instalar QMoney %1").arg(version));
            const QString safeNotes = notes.trimmed().isEmpty()
                                          ? QStringLiteral("Esta versão não possui notas adicionais.")
                                          : notes.trimmed();
            const auto answer = QMessageBox::question(
                this, QStringLiteral("QMoney %1 disponível").arg(version),
                QStringLiteral("Uma nova versão está pronta para baixar.\n\n%1\n\nInstalar agora?")
                    .arg(safeNotes));
            if (answer == QMessageBox::Yes) {
              _updateButton->setEnabled(false);
              _updateButton->setText(QStringLiteral("Baixando atualização…"));
              _updates.downloadAndInstall();
            }
          });
  connect(&_updates, &UpdateManager::progress, this,
          [this](qint64 received, qint64 total) {
            if (total > 0)
              _updateButton->setText(QStringLiteral("Baixando… %1%").arg(received * 100 / total));
          });
  connect(&_updates, &UpdateManager::installReady, this, &MainWindow::installUpdate);
  QTimer::singleShot(1800, this, [this] { checkForUpdates(false); });
}

MainWindow::~MainWindow() {
  if (_backend.state() != QProcess::NotRunning) {
    _backend.terminate();
    _backend.waitForFinished(1500);
  }
}

void MainWindow::closeEvent(QCloseEvent* event) {
  _campaignPoll.stop();
  _cachePoll.stop();
  _balancePoll.stop();
  QMainWindow::closeEvent(event);
}

void MainWindow::buildShell() {
  auto* root = new QWidget;
  auto* rootLayout = new QHBoxLayout(root);
  rootLayout->setContentsMargins(0, 0, 0, 0);
  rootLayout->setSpacing(0);

  auto* sidebar = new QWidget;
  sidebar->setObjectName(QStringLiteral("sidebar"));
  sidebar->setFixedWidth(238);
  auto* side = new QVBoxLayout(sidebar);
  side->setContentsMargins(22, 24, 18, 18);
  side->setSpacing(12);

  auto* brandRow = new QHBoxLayout;
  auto* mark = new QLabel(QStringLiteral("Q"));
  mark->setObjectName(QStringLiteral("brandMark"));
  mark->setAlignment(Qt::AlignCenter);
  mark->setFixedSize(38, 38);
  auto* brandCopy = new QVBoxLayout;
  brandCopy->setSpacing(0);
  auto* brand = new QLabel(QStringLiteral("QMoney"));
  brand->setObjectName(QStringLiteral("brand"));
  brandCopy->addWidget(brand);
  brandCopy->addWidget(quietLabel(QStringLiteral("Minute operations")));
  brandRow->addWidget(mark);
  brandRow->addSpacing(6);
  brandRow->addLayout(brandCopy, 1);
  side->addLayout(brandRow);
  side->addSpacing(14);

  _navigation = new QListWidget;
  _navigation->setObjectName(QStringLiteral("navigation"));
  _navigation->setFrameShape(QFrame::NoFrame);
  _navigation->setHorizontalScrollBarPolicy(Qt::ScrollBarAlwaysOff);
  const QStringList pages = {
      QStringLiteral("Visão geral"), QStringLiteral("Nova campanha"),
      QStringLiteral("Acelerador"), QStringLiteral("Contas"),
      QStringLiteral("Saldos"), QStringLiteral("Histórico")};
  const QStringList glyphs = {QStringLiteral("⌂  "), QStringLiteral("▶  "),
                              QStringLiteral("⚡  "), QStringLiteral("◎  "),
                              QStringLiteral("$  "), QStringLiteral("≡  ")};
  for (int i = 0; i < pages.size(); ++i) {
    auto* item = new QListWidgetItem(glyphs[i] + pages[i]);
    item->setSizeHint(QSize(190, 43));
    _navigation->addItem(item);
  }
  _navigation->setCurrentRow(0);
  connect(_navigation, &QListWidget::currentRowChanged, this, &MainWindow::navigate);
  side->addWidget(_navigation, 1);

  auto* connectionCard = new QFrame;
  connectionCard->setObjectName(QStringLiteral("connectionCard"));
  auto* connectionLayout = new QVBoxLayout(connectionCard);
  connectionLayout->setContentsMargins(12, 10, 12, 10);
  connectionLayout->setSpacing(4);
  connectionLayout->addWidget(quietLabel(QStringLiteral("SERVIÇO LOCAL")));
  _backendState = new QLabel(QStringLiteral("●  Iniciando…"));
  _backendState->setObjectName(QStringLiteral("backendState"));
  connectionLayout->addWidget(_backendState);
  side->addWidget(connectionCard);

  _themeButton = new QPushButton;
  connect(_themeButton, &QPushButton::clicked, this, [this] {
    setDarkTheme(!QSettings().value(QStringLiteral("darkTheme"), false).toBool());
  });
  side->addWidget(_themeButton);

  _updateButton = new QPushButton(
      QStringLiteral("↻  Verificar atualização"));
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
  statusLayout->addWidget(_status, 1);
  auto* refresh = new QPushButton(QStringLiteral("Atualizar"));
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
  outer->setContentsMargins(34, 26, 34, 28);
  outer->setSpacing(16);
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
  layout->setContentsMargins(20, 18, 20, 18);
  layout->setSpacing(12);
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
  layout->setContentsMargins(2, 3, 2, 3);
  layout->setSpacing(1);
  *valueLabel = new QLabel(value);
  (*valueLabel)->setObjectName(QStringLiteral("metricValue"));
  layout->addWidget(*valueLabel);
  layout->addWidget(quietLabel(caption));
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
  layout->setSpacing(16);

  auto* pulseContent = new QWidget;
  auto* pulse = new QVBoxLayout(pulseContent);
  pulse->setContentsMargins(0, 0, 0, 0);
  auto* pulseHeader = new QHBoxLayout;
  auto* pulseKicker = new QLabel(QStringLiteral("PULSO DE CAMPANHA"));
  pulseKicker->setObjectName(QStringLiteral("kicker"));
  pulseHeader->addWidget(pulseKicker);
  pulseHeader->addStretch();
  auto* newCampaign = primaryButton(QStringLiteral("Criar campanha"));
  connect(newCampaign, &QPushButton::clicked, this, [this] { _navigation->setCurrentRow(1); });
  pulseHeader->addWidget(newCampaign);
  pulse->addLayout(pulseHeader);
  _homePulseTitle = new QLabel(QStringLiteral("Aguardando o serviço local"));
  _homePulseTitle->setObjectName(QStringLiteral("pulseTitle"));
  pulse->addWidget(_homePulseTitle);
  _homePulseBody = quietLabel(QStringLiteral("Os dados aparecerão assim que o motor responder."));
  pulse->addWidget(_homePulseBody);
  _homePulseProgress = new QProgressBar;
  _homePulseProgress->setRange(0, 100);
  _homePulseProgress->setValue(0);
  _homePulseProgress->setTextVisible(false);
  pulse->addWidget(_homePulseProgress);
  auto* pulseCard = card(QString(), pulseContent);
  pulseCard->setObjectName(QStringLiteral("pulseCard"));
  layout->addWidget(pulseCard);

  auto* stats = new QWidget;
  auto* statsLayout = new QHBoxLayout(stats);
  statsLayout->setContentsMargins(0, 0, 0, 0);
  statsLayout->setSpacing(14);
  statsLayout->addWidget(card(QString(), metric(QStringLiteral("—"), QStringLiteral("contas conectadas"), &_homeAccounts)));
  statsLayout->addWidget(card(QString(), metric(QStringLiteral("—"), QStringLiteral("campanhas registradas"), &_homeCampaigns)));
  statsLayout->addWidget(card(QString(), metric(QStringLiteral("—"), QStringLiteral("envios ok na última"), &_homeSuccess)));
  layout->addWidget(stats);
  layout->addStretch();

  return pageShell(QStringLiteral("Visão geral"),
                   QStringLiteral("Acompanhe o estado da operação e siga para a próxima ação."), body);
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
  _dataset->addItem(QStringLiteral("Conteúdo combinado"), QStringLiteral("all"));
  _dataset->addItem(QStringLiteral("Somente Ego4D"), QStringLiteral("ego4d"));
  _dataset->addItem(QStringLiteral("Somente HoloAssist"), QStringLiteral("holoassist"));
  _dataset->setCurrentIndex(0);
  _dataset->setMinimumContentsLength(24);
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
  taskCol->addWidget(_campaignTasks);
  selectionLayout->addLayout(accountCol, 1);
  selectionLayout->addLayout(taskCol, 2);
  layout->addWidget(card(QStringLiteral("Seleção"), selection));

  auto* parameters = new QWidget;
  auto* form = new QFormLayout(parameters);
  form->setContentsMargins(0, 0, 0, 0);
  form->setHorizontalSpacing(18);
  form->setVerticalSpacing(10);
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
  _delayMode->addItem(QStringLiteral("Sem intervalo"), QStringLiteral("off"));
  _delayMode->addItem(QStringLiteral("Duração do clipe"), QStringLiteral("clip"));
  _delayMode->addItem(QStringLiteral("Intervalo fixo"), QStringLiteral("fixed"));
  _delayMode->setCurrentIndex(0);
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
  _campaignCurrent = quietLabel(QStringLiteral("Nenhuma campanha em andamento."));
  executionLayout->addWidget(_campaignCurrent);
  _campaignProgress = new QProgressBar;
  _campaignProgress->setRange(0, 100);
  executionLayout->addWidget(_campaignProgress);
  _campaignFeed = new QPlainTextEdit;
  _campaignFeed->setReadOnly(true);
  _campaignFeed->setMaximumBlockCount(500);
  _campaignFeed->setMinimumHeight(130);
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
  form->setContentsMargins(0, 0, 0, 0);
  _cacheTask = new QComboBox;
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
  _accountsTable->setHorizontalHeaderLabels(
      {QStringLiteral("Conta"), QStringLiteral("Organização"), QStringLiteral("Token"), QStringLiteral("Ações")});
  _accountsTable->horizontalHeader()->setSectionResizeMode(0, QHeaderView::Stretch);
  _accountsTable->horizontalHeader()->setSectionResizeMode(1, QHeaderView::ResizeToContents);
  _accountsTable->horizontalHeader()->setSectionResizeMode(2, QHeaderView::ResizeToContents);
  _accountsTable->horizontalHeader()->setSectionResizeMode(3, QHeaderView::ResizeToContents);
  _accountsTable->verticalHeader()->setVisible(false);
  _accountsTable->setSelectionBehavior(QAbstractItemView::SelectRows);
  layout->addWidget(card(QStringLiteral("Contas cadastradas"), _accountsTable), 1);

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
  _balancesState = quietLabel(QStringLiteral("Aguardando leitura…"));
  headerLayout->addWidget(_balancesState, 1);
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

  _balancesTable = new QTableWidget(0, 4);
  _balancesTable->setHorizontalHeaderLabels(
      {QStringLiteral("Conta"), QStringLiteral("Disponível"), QStringLiteral("Atualizado"), QStringLiteral("Ação")});
  _balancesTable->horizontalHeader()->setSectionResizeMode(0, QHeaderView::Stretch);
  _balancesTable->horizontalHeader()->setSectionResizeMode(1, QHeaderView::ResizeToContents);
  _balancesTable->horizontalHeader()->setSectionResizeMode(2, QHeaderView::ResizeToContents);
  _balancesTable->horizontalHeader()->setSectionResizeMode(3, QHeaderView::ResizeToContents);
  _balancesTable->verticalHeader()->setVisible(false);
  layout->addWidget(card(QStringLiteral("Saldos no Crowtado"), _balancesTable), 1);

  return pageShell(QStringLiteral("Saldos"),
                   QStringLiteral("Consulte valores disponíveis e solicite o link de saque."), body);
}

QWidget* MainWindow::buildHistoryPage() {
  auto* body = new QWidget;
  auto* layout = new QHBoxLayout(body);
  layout->setContentsMargins(0, 0, 0, 0);
  layout->setSpacing(14);
  _historyTable = new QTableWidget(0, 4);
  _historyTable->setHorizontalHeaderLabels(
      {QStringLiteral("Início"), QStringLiteral("Contas"), QStringLiteral("Clipes"), QStringLiteral("Sucesso")});
  _historyTable->horizontalHeader()->setSectionResizeMode(0, QHeaderView::ResizeToContents);
  _historyTable->horizontalHeader()->setSectionResizeMode(1, QHeaderView::Stretch);
  _historyTable->horizontalHeader()->setSectionResizeMode(2, QHeaderView::ResizeToContents);
  _historyTable->horizontalHeader()->setSectionResizeMode(3, QHeaderView::ResizeToContents);
  _historyTable->verticalHeader()->setVisible(false);
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
      _historyDetail->setPlainText(QString::fromUtf8(doc.toJson(QJsonDocument::Indented)));
    });
  });
  layout->addWidget(card(QStringLiteral("Campanhas"), _historyTable), 3);
  layout->addWidget(card(QStringLiteral("Registro"), _historyDetail), 2);
  return pageShell(QStringLiteral("Histórico"),
                   QStringLiteral("Audite campanhas anteriores e seus resultados por conta."), body);
}

void MainWindow::applyStructuralStyle(bool dark) {
  const QString bg = dark ? QStringLiteral("#191715") : QStringLiteral("#f2efeb");
  const QString panel = dark ? QStringLiteral("#211f1d") : QStringLiteral("#ffffff");
  const QString sidebar = dark ? QStringLiteral("#171513") : QStringLiteral("#26211e");
  const QString text = dark ? QStringLiteral("#eee9e4") : QStringLiteral("#2b2724");
  const QString muted = dark ? QStringLiteral("#aaa29b") : QStringLiteral("#766e67");
  const QString border = dark ? QStringLiteral("#3d3834") : QStringLiteral("#ded8d1");
  const QString selected = dark ? QStringLiteral("#4a3022") : QStringLiteral("#493326");

  setStyleSheet(QStringLiteral(R"(
    #workspace { background: %1; }
    #sidebar { background: %3; color: #f8f3ee; border-right: 1px solid %6; }
    #brandMark { background: #f47b35; color: white; border-radius: 11px; font-size: 20px; font-weight: 800; }
    #brand { color: white; font-size: 20px; font-weight: 750; }
    #sidebar #quiet { color: #aaa29b; }
    #navigation { background: transparent; color: #c9c1ba; outline: none; }
    #navigation::item { border-radius: 8px; padding-left: 12px; margin: 2px 0; }
    #navigation::item:hover { background: rgba(255,255,255,0.07); color: white; }
    #navigation::item:selected { background: %7; color: white; font-weight: 650; }
    #connectionCard { background: rgba(255,255,255,0.05); border: 1px solid rgba(255,255,255,0.08); border-radius: 9px; }
    #backendState { color: #d9d2cc; font-weight: 600; }
    #pageTitle { color: %4; font-size: 27px; font-weight: 750; }
    #pageSubtitle { color: %5; font-size: 14px; }
    #card, #pulseCard { background: %2; border: 1px solid %6; border-radius: 11px; }
    #pulseCard { border-left: 4px solid #f47b35; }
    #cardTitle { color: %4; font-size: 15px; font-weight: 700; }
    #metricValue { color: %4; font-size: 29px; font-weight: 760; }
    #pulseTitle { color: %4; font-size: 20px; font-weight: 730; }
    #kicker { color: #f47b35; font-size: 11px; font-weight: 800; letter-spacing: 1px; }
    #quiet { color: %5; }
    #appStatusBar { background: %2; border-top: 1px solid %6; }
    QComboBox { color: %4; background-color: %2; padding-left: 10px; }
    QComboBox QAbstractItemView { color: %4; background-color: %2; selection-background-color: #f47b35; }
    QPlainTextEdit { font-family: "Cascadia Mono", Consolas, monospace; }
  )").arg(bg, panel, sidebar, text, muted, border, selected));
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
  const QString packagedService = appDir + QStringLiteral("/runtime/QMoneyService.exe");
  QString program;
  QStringList arguments;
  QString workingDirectory;
  QProcessEnvironment environment = QProcessEnvironment::systemEnvironment();
  if (QFileInfo::exists(packagedService)) {
    program = packagedService;
    arguments = {QStringLiteral("--no-browser"), QStringLiteral("--porta"),
                 QStringLiteral("8876")};
    workingDirectory = QStandardPaths::writableLocation(QStandardPaths::AppLocalDataLocation);
    QDir().mkpath(workingDirectory);
    environment.insert(QStringLiteral("QMONEY_USER_ROOT"), workingDirectory);
    environment.insert(QStringLiteral("QMONEY_RUNTIME_ROOT"), appDir + QStringLiteral("/runtime"));
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
  connect(&_backend, &QProcess::readyReadStandardOutput, this, [this] {
    const QString output = QString::fromUtf8(_backend.readAllStandardOutput()).trimmed();
    const QStringList lines = output.split('\n', Qt::SkipEmptyParts);
    for (const QString& rawLine : lines) {
      const QString line = rawLine.trimmed();
      if (!line.contains(QStringLiteral("HTTP/1.1")) && !line.isEmpty()) setStatus(line);
    }
  });
  connect(&_backend, &QProcess::errorOccurred, this, [this](QProcess::ProcessError) {
    if (!_backendReady) setStatus(QStringLiteral("Não foi possível iniciar o Python. Execute CONFIGURAR_QMONEY.bat."));
  });
  _backend.start(program, arguments);
  _probeAttempts = 0;
  _backendProbe.start();
  QTimer::singleShot(60, this, &MainWindow::probeBackend);
}

void MainWindow::probeBackend() {
  ++_probeAttempts;
  _api.get(QStringLiteral("/api/accounts"), [this](bool ok, const QJsonDocument&, const QString&) {
    if (ok) {
      _backendProbe.stop();
      setBackendReady(true);
      refreshCurrentPage();
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
  if (ready) setStatus(QStringLiteral("Serviço local pronto."));
}

void MainWindow::navigate(int index) {
  if (index < 0) return;
  _pages->setCurrentIndex(index);
  if (index == 1 && _campaignStop->isEnabled()) _campaignPoll.start();
  else if (index != 1) _campaignPoll.stop();
  if (index != 2) _cachePoll.stop();
  if (index != 4) _balancePoll.stop();
  if (_backendReady) refreshCurrentPage();
}

void MainWindow::refreshCurrentPage() {
  if (!_backendReady) return probeBackend();
  switch (_pages->currentIndex()) {
    case 0: loadHome(); break;
    case 1: loadCampaignData(); break;
    case 2: loadAccelerator(); break;
    case 3: loadAccounts(); break;
    case 4: loadBalances(); break;
    case 5: loadHistory(); break;
    default: break;
  }
}

void MainWindow::showError(const QString& title, const QString& error) {
  QMessageBox::warning(this, title, error);
  setStatus(error);
}

void MainWindow::setStatus(const QString& text) {
  _status->setText(text.simplified());
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

void MainWindow::loadCampaignData() {
  _api.get(QStringLiteral("/api/accounts"), [this](bool ok, const QJsonDocument& doc, const QString& error) {
    if (!ok) return showError(QStringLiteral("Falha ao carregar contas"), error);
    const QSignalBlocker blocker(_campaignAccounts);
    _campaignAccounts->clear();
    for (const auto value : doc.object().value(QStringLiteral("accounts")).toArray()) {
      const auto account = value.toObject();
      auto* item = new QListWidgetItem(account.value(QStringLiteral("email")).toString());
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
    for (const auto value : _taskRecords) {
      const auto task = value.toObject();
      const bool available = task.value(QStringLiteral("available")).toBool(true);
      QString label = task.value(QStringLiteral("name_pt")).toString();
      if (label.isEmpty()) label = task.value(QStringLiteral("name")).toString();
      if (task.value(QStringLiteral("boosted")).toBool()) label += QStringLiteral("  ·  turbinada");
      auto* item = new QListWidgetItem(label);
      item->setFlags(item->flags() | Qt::ItemIsUserCheckable);
      item->setData(Qt::UserRole, jsonId(task.value(QStringLiteral("id"))));
      item->setCheckState(available ? Qt::Checked : Qt::Unchecked);
      if (!available) {
        item->setFlags(item->flags() & ~Qt::ItemIsEnabled);
        item->setToolTip(QStringLiteral("Categoria sem clipe compatível no conjunto escolhido."));
      }
      _campaignTasks->addItem(item);
    }
    setStatus(QStringLiteral("%1 categoria(s) carregada(s).").arg(_campaignTasks->count()));
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
  _campaignStart->setText(QStringLiteral("Validando…"));
  _api.post(QStringLiteral("/api/campaigns"), body,
            [this](bool ok, const QJsonDocument&, const QString& error) {
    _campaignStart->setText(QStringLiteral("Iniciar campanha"));
    if (!ok) {
      _campaignStart->setEnabled(true);
      return showError(QStringLiteral("Campanha não iniciada"), error);
    }
    _lastCampaignSeq = 0;
    _campaignFeed->clear();
    _campaignPoll.start();
    pollCampaign();
    setStatus(QStringLiteral("Campanha iniciada."));
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
    _campaignCurrent->setText(snap.value(QStringLiteral("current")).toString(
        running ? QStringLiteral("Campanha em andamento…") : QStringLiteral("Nenhuma campanha em andamento.")));
    const auto totals = snap.value(QStringLiteral("totals")).toObject();
    const int total = totals.value(QStringLiteral("total_sends")).toInt();
    const int done = totals.value(QStringLiteral("done_sends")).toInt();
    _campaignProgress->setValue(total > 0 ? done * 100 / total : 0);
    for (const auto eventValue : snap.value(QStringLiteral("events")).toArray()) {
      const auto event = eventValue.toObject();
      _lastCampaignSeq = qMax(_lastCampaignSeq, event.value(QStringLiteral("seq")).toInt());
      QString message = event.value(QStringLiteral("message")).toString();
      if (message.isEmpty()) {
        message = QStringLiteral("[%1] %2").arg(event.value(QStringLiteral("kind")).toString(),
                                                event.value(QStringLiteral("email")).toString());
      }
      _campaignFeed->appendPlainText(message.trimmed());
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
    if (!ok) return showError(QStringLiteral("Falha ao carregar contas"), error);
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
      actionsLayout->setContentsMargins(0, 0, 0, 0);
      auto* check = new QPushButton(QStringLiteral("Verificar"));
      connect(check, &QPushButton::clicked, this, [this, email] {
        _api.post(QStringLiteral("/api/accounts/") + encoded(email) + QStringLiteral("/check"), {},
          [this](bool ok, const QJsonDocument&, const QString& error) {
            if (!ok) showError(QStringLiteral("Conta não validada"), error);
            else { setStatus(QStringLiteral("Conta validada.")); loadAccounts(); }
          });
      });
      auto* remove = new QPushButton(QStringLiteral("Remover"));
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
    _accountsTable->resizeRowsToContents();
    setStatus(QStringLiteral("%1 conta(s) cadastrada(s).").arg(accounts.size()));
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
    QStringList passwordAccounts;
    for (const auto value : withPassword) passwordAccounts << value.toString();
    _balancesTable->setRowCount(accounts.size());
    int row = 0;
    for (const auto value : accounts) {
      const QString email = value.toString();
      const auto balance = balances.value(email).toObject();
      _balancesTable->setItem(row, 0, cell(email));
      QString amount = balance.contains(QStringLiteral("availableCents"))
          ? money(static_cast<qint64>(balance.value(QStringLiteral("availableCents")).toDouble()))
          : QStringLiteral("—");
      if (!balance.value(QStringLiteral("error")).toString().isEmpty()) amount = QStringLiteral("erro");
      _balancesTable->setItem(row, 1, cell(amount));
      _balancesTable->setItem(row, 2, cell(friendlyDate(balance.value(QStringLiteral("updated_at")).toString())));
      auto* withdraw = new QPushButton(QStringLiteral("Solicitar saque"));
      withdraw->setEnabled(passwordAccounts.contains(email));
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
      _balancesTable->setCellWidget(row, 3, withdraw);
      ++row;
    }
    const bool running = runner.value(QStringLiteral("state")).toString() == QStringLiteral("running");
    _balancesRefresh->setEnabled(!running);
    _balancesState->setText(running
        ? runner.value(QStringLiteral("current")).toString(QStringLiteral("Consultando contas…"))
        : QStringLiteral("%1 conta(s) · %2 com senha de saldo configurada")
              .arg(accounts.size()).arg(passwordAccounts.size()));
    if (running) _balancePoll.start(); else _balancePoll.stop();
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

QString MainWindow::money(qint64 cents) const {
  return QLocale(QStringLiteral("pt_BR")).toCurrencyString(cents / 100.0, QStringLiteral("R$"));
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
