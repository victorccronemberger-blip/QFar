#include <QApplication>
#include <QCloseEvent>
#include <QDir>
#include <QEventLoop>
#include <QFileDialog>
#include <QFileInfo>
#include <QJsonArray>
#include <QJsonDocument>
#include <QJsonObject>
#include <QMainWindow>
#include <QProcess>
#include <QPushButton>
#include <QRegularExpression>
#include <QSettings>
#include <QTemporaryDir>
#include <QTimer>
#include <functional>
#include <iostream>

// Exact production method bodies below; every external collaborator is inert.
struct InertProcess : QProcess {
  static inline int launches{};
  static inline bool launchResult{true};
  static inline QStringList launchArguments;
  static bool startDetached(const QString&, const QStringList& args, const QString&) {
    ++launches; launchArguments = args; return launchResult;
  }
};
struct InertFileInfo { static bool exists(const QString&) { return true; } };
struct InertFileDialog {
  static inline QString selected;
  static inline std::function<void()> duringSelection;
  static QString getExistingDirectory(QWidget*, const QString&, const QString&) {
    if (duringSelection) duringSelection();
    return selected;
  }
};
struct InertApi {
  using Callback = std::function<void(bool, const QJsonDocument&, const QString&)>;
  QStringList paths;
  Callback pending;
  void post(const QString& path, const QJsonObject&, Callback callback) {
    paths.append(path); pending = std::move(callback);
  }
  void reply(const QJsonObject& value, bool ok = true) {
    if (!pending) return;
    auto callback = std::move(pending); pending = {};
    callback(ok, QJsonDocument(value), QStringLiteral("inert"));
  }
};
class MainWindow : public QMainWindow {
public:
  MainWindow() { setCentralWidget(new QWidget(this)); _updateButton = new QPushButton(this); }
  void closeEvent(QCloseEvent* event) override;
  void beginCampaignDrain();
  void completeCampaignDrain();
  void pollCampaignClose();
  void restartBackend();
  void chooseLibrary();
  void installUpdate(const QString& packagePath, const QString& verifiedSha256);
  bool launchPendingUpdate();
  void saveCampaignDraft() { ++draftSaves; }
  void stopBackend() { ++backendStops; }
  void startBackend() { ++backendStarts; }
  void setStatus(const QString& value) { status = value; }
  void showError(const QString&, const QString&) { ++errors; }
  InertApi _api;
  InertProcess _backend;
  QPushButton* _updateButton{};
  QTimer _campaignDraftSave, _campaignPoll, _previewPoll, _cachePoll;
  QTimer _orgMigrationPoll, _balancePoll, _backendProbe, _campaignClosePoll, _operationPoll;
  bool _closing{}, _backendReady{}, _campaignActive{}, _campaignStartPending{};
  bool _campaignPreflightPending{}, _campaignStartUncertain{};
  bool _campaignClosePending{}, _campaignCloseReady{}, _campaignCloseInFlight{};
  bool _campaignRestartPending{}, _resumeOperationPoll{}, _restartingBackend{};
  bool _campaignTransitionCommitted{}, _campaignExitRequested{};
  int _backendRestarts{}, backendStops{}, backendStarts{}, draftSaves{}, errors{};
  QString _pendingLibraryRoot, _pendingUpdatePackage, _pendingUpdateSha256, _currentLibraryRoot, status;
  QString _campaignRequestedPreflight;
};

#define QProcess InertProcess
#define QFileInfo InertFileInfo
#define QFileDialog InertFileDialog
#include "campaign-exit-methods.inc"
#undef QFileDialog
#undef QFileInfo
#undef QProcess

static void spin(int ms) {
  QEventLoop loop;
  QTimer::singleShot(ms, &loop, &QEventLoop::quit);
  loop.exec();
}

int main(int argc, char** argv) {
  QApplication app(argc, argv);
  app.setQuitOnLastWindowClosed(false);
  QTemporaryDir settings;
  QSettings::setDefaultFormat(QSettings::IniFormat);
  QSettings::setPath(QSettings::IniFormat, QSettings::UserScope, settings.path());
  app.setOrganizationName("InertCampaignExitFixture");
  app.setApplicationName("InertCampaignExitFixture");
  QSettings().setValue("libraryRoot", "original-library");
  QJsonArray checks;
  int failures = 0;
  auto check = [&](const QString& name, bool pass) {
    checks.append(QJsonObject{{"name", name}, {"passed", pass}}); failures += !pass;
  };
  MainWindow window;
  window._backendReady = true;
  window.show();
  const auto mode = app.arguments().value(1);
  const QJsonObject busy{{"ok", true}, {"draining", true}, {"ready", false}};
  const QJsonObject ready{{"ok", true}, {"draining", true}, {"ready", true}};
  const QJsonObject invalid{{"ok", true}, {"draining", true}, {"ready", "true"}};
  if (mode == "restart" || mode == "restart-close") {
    InertFileDialog::selected = settings.path() + "/selected-library";
    window._operationPoll.start(10000);
    window._campaignDraftSave.start(10000);
    window.chooseLibrary();
    check("library-setting-held-before-drain", QSettings().value("libraryRoot") == "original-library");
    check("restart-held-before-drain", window.backendStops == 0 && window.backendStarts == 0);
    check("restart-drain-requested", window._api.paths == QStringList{"/api/campaigns/drain"});
    window.restartBackend();
    check("repeat-restart-no-duplicate-request", window._api.paths.size() == 1);
    window._api.reply(invalid); spin(5);
    check("restart-malformed-ready-held", window.backendStops == 0);
    window.pollCampaignClose(); window._api.reply(busy); spin(5);
    check("restart-busy-held", window.backendStops == 0 && window.isVisible());
    if (mode == "restart-close") window.close();
    window.pollCampaignClose(); window._api.reply(ready); spin(420);
    if (mode == "restart-close") {
      check("close-cancels-library-commit", QSettings().value("libraryRoot") == "original-library");
      check("close-cancels-restart-timer", window.backendStarts == 0 && window.backendStops == 1 && !window.isVisible());
    } else {
      check("restart-once-after-ready", window.backendStops == 1 && window.backendStarts == 1);
      check("library-commit-after-ready", QSettings().value("libraryRoot") == InertFileDialog::selected);
      check("draft-saved-before-restart", window.draftSaves == 1);
      check("window-restored", window.isVisible() && window.centralWidget()->isEnabled() && !window._closing);
      check("drain-flags-renewed", !window._campaignClosePending && !window._campaignCloseReady && !window._campaignRestartPending);
      check("operation-poll-restored", window._operationPoll.isActive());
    }
  } else if (mode == "update" || mode == "update-failure" || mode == "update-close-failure") {
    InertProcess::launchResult = mode == "update";
    window.installUpdate("inert-package.zip", QString(64, 'a'));
    check("updater-not-launched-before-drain", InertProcess::launches == 0);
    check("update-drain-requested", window._api.paths == QStringList{"/api/campaigns/drain"});
    window.installUpdate("different-package.zip", QString(64, 'b'));
    check("repeat-update-held", InertProcess::launches == 0 && window._api.paths.size() == 1);
    window._api.reply(invalid); spin(5);
    check("update-malformed-ready-held", InertProcess::launches == 0 && window.backendStops == 0);
    window.pollCampaignClose(); window._api.reply(busy); spin(5);
    check("update-busy-held", InertProcess::launches == 0 && window.backendStops == 0);
    if (mode == "update-close-failure") window.close();
    window.pollCampaignClose(); window._api.reply(ready); spin(420);
    check("updater-one-launch-after-ready", InertProcess::launches == 1);
    const auto args = InertProcess::launchArguments;
    check("updater-original-package-and-digest", args.value(1) == "inert-package.zip" && args.value(3) == QString(64, 'a'));
    check("updater-original-parent-and-launch", args.value(6) == "--pid" && args.value(7) == QString::number(app.applicationPid())
          && args.value(8) == "--launch" && args.value(9) == "QMoney.exe");
    if (mode == "update") {
      check("update-closes-after-ready", !window.isVisible() && window.backendStops == 1 && window.backendStarts == 0);
    } else if (mode == "update-failure") {
      check("failed-update-renews-drained-service", window.isVisible() && window.backendStops == 1 && window.backendStarts == 1 && window.errors == 1);
      check("failed-update-restores-window", window.centralWidget()->isEnabled() && !window._closing && !window._campaignCloseReady);
    } else {
      check("close-wins-after-launch-failure", !window.isVisible() && window.backendStarts == 0 && window.backendStops == 1 && window.errors == 1);
    }
  } else if (mode == "library-reentrant") {
    InertFileDialog::selected = settings.path() + "/stale-selection";
    InertFileDialog::duringSelection = [&] { window.restartBackend(); };
    window.chooseLibrary();
    check("reentrant-library-no-late-pending-root", window._pendingLibraryRoot.isEmpty());
    window._api.reply(ready); spin(420);
    check("reentrant-library-no-stale-commit", QSettings().value("libraryRoot") == "original-library");
    check("reentrant-restart-once", window.backendStarts == 1 && window.backendStops == 1);
  } else if (mode == "quit") {
    QTimer::singleShot(0, &app, &QCoreApplication::quit);
    QTimer::singleShot(30, &window, [&] {
      check("application-quit-held", window.isVisible() && window.backendStops == 0 && window._api.paths.size() == 1);
      window._api.reply(busy);
    });
    QTimer::singleShot(60, &window, [&] { window.pollCampaignClose(); window._api.reply(ready); });
    QTimer::singleShot(100, &app, [&] { app.exit(0); });
    app.exec();
    check("application-quit-requested-drain", !window._api.paths.isEmpty());
    check("application-quit-finished-after-ready", window._campaignCloseReady && window.backendStops == 1 && !window.isVisible());
  } else return 2;
  std::cout << QJsonDocument(QJsonObject{{"mode", mode}, {"checks", checks}, {"failures", failures}}).toJson(QJsonDocument::Compact).constData() << '\n';
  return failures ? 1 : 0;
}
