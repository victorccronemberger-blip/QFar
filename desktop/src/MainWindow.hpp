#pragma once

#include "ApiClient.hpp"
#include "UpdateManager.hpp"

#include <QJsonArray>
#include <QJsonObject>
#include <QMainWindow>
#include <QProcess>
#include <QTimer>

class QCheckBox;
class QComboBox;
class QLabel;
class QListWidget;
class QPlainTextEdit;
class QProgressBar;
class QPushButton;
class QSpinBox;
class QDoubleSpinBox;
class QStackedWidget;
class QTableWidget;
class QWidget;

namespace oclero::qlementine { class QlementineStyle; }

class MainWindow final : public QMainWindow {
  Q_OBJECT

public:
  explicit MainWindow(oclero::qlementine::QlementineStyle* style,
                      QWidget* parent = nullptr);
  ~MainWindow() override;

protected:
  void closeEvent(QCloseEvent* event) override;

private:
  QWidget* buildHomePage();
  QWidget* buildReadinessPage();
  QWidget* buildIntegrationsPage();
  QWidget* buildCampaignPage();
  QWidget* buildAcceleratorPage();
  QWidget* buildAccountsPage();
  QWidget* buildBalancesPage();
  QWidget* buildHistoryPage();
  QWidget* pageShell(const QString& title, const QString& subtitle, QWidget* body);
  QWidget* card(const QString& title, QWidget* content = nullptr);
  QWidget* metric(const QString& value, const QString& caption, QLabel** valueLabel);
  QPushButton* primaryButton(const QString& text);
  void buildShell();
  void applyStructuralStyle(bool dark);
  void setDarkTheme(bool dark);
  void checkForUpdates(bool interactive = false);
  void installUpdate(const QString& packagePath);

  void startBackend();
  void stopBackend();
  void restartBackend();
  void probeBackend();
  void setBackendReady(bool ready, const QString& message = {});
  void navigate(int index);
  void refreshCurrentPage();
  void showError(const QString& title, const QString& error);
  void setStatus(const QString& text);

  void loadHome();
  void loadReadiness();
  void loadIntegrations();
  void saveEgo4dIntegration();
  void testEgo4dIntegration();
  void prepareEgo4dCatalog();
  void saveHostingerIntegration();
  void testHostingerIntegration();
  void chooseLibrary();
  void exportDiagnostics();
  void loadCampaignData();
  void loadTasks();
  void startCampaign();
  void pollCampaign();
  void pollCampaignPreviews();
  void loadAccelerator();
  void startAccelerator();
  void loadAccounts();
  void checkAllAccounts();
  void addAccount(bool registerNew);
  void loadBalances();
  void configureCrowtadoAccess(const QString& email);
  void loadHistory();

  QString usdMoney(qint64 cents) const;
  QString brlMoney(qint64 usdCents, double usdBrlRate) const;
  QString friendlyDate(const QString& iso) const;
  QString encoded(const QString& value) const;
  oclero::qlementine::QlementineStyle* _style{};
  ApiClient _api;
  UpdateManager _updates;
  QProcess _backend;
  QTimer _backendProbe;
  QTimer _campaignPoll;
  QTimer _previewPoll;
  QTimer _taskReload;
  QTimer _balancePoll;
  QTimer _cachePoll;
  int _probeAttempts{};
  int _backendRestarts{};
  bool _backendReady{};
  bool _closing{};
  bool _restartingBackend{};

  QListWidget* _navigation{};
  QStackedWidget* _pages{};
  QLabel* _backendState{};
  QLabel* _status{};
  QPushButton* _themeButton{};
  QPushButton* _updateButton{};

  QLabel* _homeAccounts{};
  QLabel* _homeCampaigns{};
  QLabel* _homeSuccess{};
  QLabel* _homePulseTitle{};
  QLabel* _homePulseBody{};
  QProgressBar* _homePulseProgress{};

  QLabel* _readinessHeadline{};
  QLabel* _readinessSummary{};
  QProgressBar* _readinessProgress{};
  QTableWidget* _readinessTable{};
  QLabel* _libraryPath{};
  QLabel* _libraryUsage{};
  QPushButton* _readinessRefresh{};
  QPushButton* _libraryChoose{};
  QPushButton* _diagnosticsExport{};
  QPushButton* _repairInstall{};
  QString _currentLibraryRoot;

  QLabel* _integrationsHeadline{};
  QLabel* _integrationsSummary{};
  QLabel* _ego4dStatus{};
  QLabel* _ego4dCatalog{};
  class QLineEdit* _ego4dAccessKey{};
  class QLineEdit* _ego4dSecretKey{};
  class QLineEdit* _ego4dSessionToken{};
  class QLineEdit* _ego4dRegion{};
  QPushButton* _ego4dSave{};
  QPushButton* _ego4dTest{};
  QPushButton* _ego4dPrepare{};
  bool _ego4dCatalogPreparing{};
  QLabel* _hostingerStatus{};
  class QLineEdit* _hostingerToken{};
  class QLineEdit* _hostingerMailbox{};
  QPushButton* _hostingerSave{};
  QPushButton* _hostingerTest{};
  QLabel* _holoIntegrationStatus{};
  QLabel* _runtimeIntegrationStatus{};
  QLabel* _integrationSecurity{};

  QComboBox* _dataset{};
  QListWidget* _campaignAccounts{};
  QListWidget* _campaignTasks{};
  QDoubleSpinBox* _targetHours{};
  QSpinBox* _minDuration{};
  QSpinBox* _maxDuration{};
  QComboBox* _delayMode{};
  QSpinBox* _delaySeconds{};
  QCheckBox* _cleanupAfter{};
  QCheckBox* _activeHours{};
  QSpinBox* _hourStart{};
  QSpinBox* _hourEnd{};
  QPushButton* _campaignStart{};
  QPushButton* _campaignStop{};
  QProgressBar* _campaignProgress{};
  QLabel* _campaignStage{};
  QLabel* _campaignCurrent{};
  QLabel* _campaignStats{};
  QPlainTextEdit* _campaignFeed{};
  QJsonArray _taskRecords;
  int _taskLoadGeneration{};
  int _lastCampaignSeq{};
  QString _previewLogName;
  bool _previewCheckActive{};

  QComboBox* _cacheTask{};
  QSpinBox* _cacheLimit{};
  QSpinBox* _cacheReserve{};
  QLabel* _cacheState{};
  QLabel* _cacheNumbers{};
  QProgressBar* _cacheProgress{};
  QPushButton* _cacheStart{};
  QPushButton* _cacheStop{};

  QTableWidget* _accountsTable{};
  class QLineEdit* _accountEmail{};
  class QLineEdit* _accountPassword{};
  QPushButton* _accountAdd{};
  QPushButton* _accountRegister{};
  QPushButton* _accountsCheckAll{};

  QTableWidget* _balancesTable{};
  QLabel* _balancesState{};
  QPushButton* _balancesRefresh{};
  QLabel* _balancesApprovedUsd{};
  QLabel* _balancesApprovedBrl{};
  QLabel* _balancesPendingUsd{};
  QLabel* _balancesPendingBrl{};
  QLabel* _balancesExchange{};

  QTableWidget* _historyTable{};
  QPlainTextEdit* _historyDetail{};
};
