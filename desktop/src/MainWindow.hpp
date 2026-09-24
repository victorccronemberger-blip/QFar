#pragma once
#include <functional>

#include "ApiClient.hpp"
#include "UpdateManager.hpp"

#include <QJsonArray>
#include <QHash>
#include <QJsonObject>
#include <QMainWindow>
#include <QProcess>
#include <QSet>
#include <QTimer>

class QCheckBox;
class QComboBox;
class QLabel;
class QLineEdit;
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
  QWidget* buildBannedPage();
  void loadBanned();
  QTableWidget* _bannedTable{};
  QPushButton* _bannedRefresh{};
  QLabel* _bannedState{};
  QTimer _bannedPoll;
  bool _bannedPolling{false};
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
  void showAccountIssues(const QString& title, const QStringList& blockers,
                         const QJsonArray& issues, std::function<void()> continueAction = {});
  void submitCampaign(QJsonObject body);
  void setStatus(const QString& text);

  void loadHome();
  void loadReadiness();
  void loadIntegrations();
  void saveEgo4dIntegration();
  void testEgo4dIntegration();
  void prepareEgo4dCatalog();
  void saveHostingerIntegration();
  void testHostingerIntegration();
  void selectHostingerIntegration(int index);
  void removeHostingerIntegration();
  void chooseLibrary();
  void exportDiagnostics();
  void loadCampaignData();
  void updateCampaignAccountCount();
  void drawCampaignAccounts();
  void loadCampaignBalances();
  void saveCampaignDraft();
  void loadTasks();
  void startCampaign();
  void pollCampaign();
  void pollCampaignPreviews();
  void loadAccelerator();
  void startAccelerator();
  void loadAccounts();
  void removeAccount(const QString& email, std::function<void()> onRemoved = {});
  void checkAllAccounts();
  void addAccount(bool registerNew);
  void importAccounts();
  void exportAccounts(bool selectedOnly);
  void loadBulkRegisterDomains();
  void checkBulkRegisterDomain();
  void startBulkRegister();
  void pollBulkRegister();
  void openWebmail();
  void startOrgMigration();
  void pollOrgMigration();
  void showOrgMigrationReport();
  QTimer _orgMigrationPoll;
  bool _orgMigrationRunning{false};
  bool _orgMigrationPolling{false};
  bool _accountTransferBusy{false};
  QJsonObject _orgMigrationSnapshot;

  QComboBox* _bulkRegisterDomain{};
  QSpinBox* _bulkRegisterCount{};
  QPushButton* _bulkRegisterStart{};
  QProgressBar* _bulkRegisterProgress{};
  QLabel* _bulkRegisterStatus{};
  QLabel* _bulkRegisterWebmail{};
  QTableWidget* _bulkRegisterTable{};
  QTimer _bulkRegisterPoll;
  bool _bulkRegisterPolling{false};
  bool _bulkRegisterStarting{false};
  bool _bulkRegisterRequestInFlight{false};
  int _bulkRegisterDomainsRevision{0};
  int _bulkRegisterPreflightRevision{0};
  QPushButton* _accountsMigrate{};
  QPushButton* _migrationReport{};
  QLabel* _migrationStatus{};
  QPushButton* _accountsImport{};
  QPushButton* _accountsExport{};
  QPushButton* _accountsExportSelected{};
  void setAccountTransferBusy(bool busy);
  void loadBalances();
  void applyBalanceFilter();
  void showWithdrawalReport(const QJsonObject& bulk);
  void configureCrowtadoAccess(const QString& email);
  void loadHistory();

  QString usdMoney(qint64 cents) const;
  QString brlMoney(qint64 usdCents, double usdBrlRate) const;
  QString friendlyDate(const QString& iso) const;
  QString encoded(const QString& value) const;
  oclero::qlementine::QlementineStyle* _style{};
  ApiClient _api;
  QHash<QString, QJsonObject> _accountChecks;
  UpdateManager _updates;
  QProcess _backend;
  QTimer _backendProbe;
  QTimer _campaignPoll;
  QTimer _previewPoll;
  QTimer _taskReload;
  QTimer _balancePoll;
  QTimer _cachePoll;
  int _probeAttempts{};
  int _probeGeneration{};
  bool _probeInFlight{};
  int _backendRestarts{};
  bool _backendReady{};
  bool _runtimeChecked{};
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
  class QComboBox* _hostingerProfile{};
  class QLineEdit* _hostingerToken{};
  QPushButton* _hostingerRemove{};
  QPushButton* _hostingerSave{};
  QPushButton* _hostingerTest{};
  QLabel* _holoIntegrationStatus{};
  QLabel* _runtimeIntegrationStatus{};
  QLabel* _integrationSecurity{};

  QComboBox* _dataset{};
  QComboBox* _contentMode{};
  QListWidget* _campaignAccounts{};
  QLineEdit* _campaignAccountSearch{};
  QComboBox* _campaignAccountMode{};
  QSpinBox* _campaignAccountCount{};
  QPushButton* _campaignDrawAccounts{};
  QLabel* _campaignAccountSelection{};
  QLabel* _campaignBalanceHint{};
  QJsonObject _campaignBalances;
  bool _campaignBalancesLoaded{};
  int _campaignBalanceRequestId{};
  QListWidget* _campaignTasks{};
  QSet<QString> _campaignSelectedTaskIds;
  bool _campaignTaskSelectionTouched{};
  QSet<QString> _campaignDraftAccounts;
  bool _campaignDraftLoaded{};
  int _campaignDraftQuantity{1};
  QTimer _campaignDraftSave;
  QDoubleSpinBox* _targetHours{};
  QSpinBox* _minDuration{};
  QSpinBox* _maxDuration{};
  QComboBox* _delayMode{};
  QComboBox* _accountWorkers{};
  QSpinBox* _delaySeconds{};
  QCheckBox* _cleanupAfter{};
  QCheckBox* _activeHours{};
  QSpinBox* _hourStart{};
  QSpinBox* _hourEnd{};
  QPushButton* _campaignStart{};
  QPushButton* _campaignStop{};
  QPushButton* _campaignReset{};
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
  bool _campaignActive{};

  QComboBox* _cacheProvider{};
  quint64 _cacheRequestId{};
  QString _cacheRequestKey;
  QString _cacheInFlightKey;
  QJsonObject _cacheCatalogSnapshot;
  bool _cacheBudgetLoaded{};
  QLabel* _cacheTaskLabel{};
  QLabel* _cacheProviderHelp{};
  QLabel* _cacheTaskHelp{};
  QLabel* _cacheBudgetHelp{};
  QLabel* _cacheDiskHelp{};
  QLabel* _cacheLimitHelp{};
  QComboBox* _cacheTask{};
  QSpinBox* _cacheLimit{};
  QLabel* _cacheBudgetLabel{};
  QSpinBox* _cacheBudget{};
  QSpinBox* _cacheReserve{};
  QLabel* _cacheState{};
  QLabel* _cacheNumbers{};
  QLabel* _cacheLastRun{};
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
  QPushButton* _balancesRefreshNeeded{};
  QLineEdit* _balancesSearch{};
  QCheckBox* _balancesOnlyAvailable{};
  QCheckBox* _balancesOnlyPending{};
  QLabel* _balancesFilterState{};
  QPushButton* _balancesWithdrawAll{};
  QPushButton* _balancesWithdrawHistory{};
  QPushButton* _balancesExport{};
  QJsonObject _balancesSnapshot;
  QJsonObject _lastWithdrawBulk;
  bool _bulkWithdrawAwaitingResult{};
  QLabel* _balancesApprovedUsd{};
  QLabel* _balancesApprovedBrl{};
  QLabel* _balancesPendingUsd{};
  QLabel* _balancesPendingBrl{};
  QLabel* _balancesTotalsNote{};
  QLabel* _balancesExchange{};

  QTableWidget* _historyTable{};
  QPlainTextEdit* _historyDetail{};
};
