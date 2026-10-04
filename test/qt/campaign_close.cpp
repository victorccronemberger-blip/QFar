#include <QApplication>
#include <QCloseEvent>
#include <QJsonDocument>
#include <QJsonArray>
#include <QJsonObject>
#include <QMainWindow>
#include <QProcess>
#include <QSettings>
#include <QTimer>
#include <functional>
#include <iostream>
#include <utility>

struct FakeApi {
  using Callback = std::function<void(bool, const QJsonDocument&, const QString&)>;
  QStringList paths;
  Callback pending;
  void post(const QString& path, const QJsonObject&, Callback callback) {
    paths.append(path);
    pending = std::move(callback);
  }
  void reply(bool ok, const QJsonObject& value) {
    if (!pending) return;
    auto callback = std::move(pending);
    pending = {};
    callback(ok, QJsonDocument(value), QStringLiteral("fixture"));
  }
};

class MainWindow : public QMainWindow {
public:
  MainWindow() { setCentralWidget(new QWidget(this)); }
  void closeEvent(QCloseEvent* event) override;
  void pollCampaignClose();
  void beginCampaignDrain();
  void completeCampaignDrain();
  bool launchPendingUpdate() { return true; }
  void startBackend() {}
  void saveCampaignDraft() { ++draftSaves; }
  void stopBackend() { ++backendStops; }
  void setStatus(const QString& text) { status = text; }
  FakeApi _api;
  QProcess _backend;
  QTimer _campaignDraftSave, _campaignPoll, _previewPoll, _cachePoll;
  QTimer _orgMigrationPoll, _balancePoll, _backendProbe, _campaignClosePoll, _operationPoll;
  bool _closing{}, _backendReady{}, _campaignActive{}, _campaignStartPending{};
  bool _campaignPreflightPending{}, _campaignStartUncertain{};
  bool _campaignClosePending{}, _campaignCloseReady{}, _campaignCloseInFlight{};
  bool _campaignRestartPending{}, _resumeOperationPoll{}, _restartingBackend{};
  bool _campaignTransitionCommitted{}, _campaignExitRequested{};
  int _backendRestarts{};
  QString _pendingLibraryRoot, _pendingUpdatePackage, _pendingUpdateSha256;
  QString _campaignRequestedPreflight;
  int backendStops{}, draftSaves{};
  QString status;
};

#include "campaign-close-methods.inc"

int main(int argc, char** argv) {
  QApplication app(argc, argv);
  app.setQuitOnLastWindowClosed(false);
  QJsonArray checks;
  int failures = 0;
  auto check = [&](const QString& name, bool pass) {
    checks.append(QJsonObject{{"name", name}, {"passed", pass}});
    failures += !pass;
  };
  for (int guard = 0; guard < 5; ++guard) {
    MainWindow window;
    window._backendReady = guard == 0;
    window._campaignActive = guard == 1;
    window._campaignStartPending = guard == 2;
    window._campaignPreflightPending = guard == 3;
    window._campaignStartUncertain = guard == 4;
    window.show();
    QCloseEvent first;
    window.closeEvent(&first);
    const auto prefix = QStringLiteral("guard-%1/").arg(guard);
    check(prefix + "first-close-held", !first.isAccepted() && window.backendStops == 0);
    check(prefix + "drain-authenticated-client-route", window._api.paths == QStringList{"/api/campaigns/drain"});
    QCloseEvent duplicate;
    window.closeEvent(&duplicate);
    check(prefix + "repeat-close-no-duplicate-inflight", !duplicate.isAccepted() && window._api.paths.size() == 1);
    window._api.reply(true, {{"ok", true}, {"draining", true}, {"ready", false}});
    check(prefix + "busy-held", window.backendStops == 0 && !window._campaignCloseReady);
    QCloseEvent retry;
    window.closeEvent(&retry);
    window._api.reply(true, {{"ok", true}, {"draining", true}, {"ready", true}});
    QApplication::processEvents();
    check(prefix + "ready-closes-once", window.backendStops == 1 && !window.isVisible() && window._campaignCloseReady);
  }
  const QList<QJsonObject> invalid = {
    {{"ok", true}, {"draining", true}, {"ready", "true"}},
    {{"ok", false}, {"draining", true}, {"ready", true}},
    {{"ok", true}, {"ready", true}},
    {{"ok", true}, {"draining", true}},
    {{"ok", "true"}, {"draining", true}, {"ready", true}},
  };
  for (int i = 0; i < invalid.size(); ++i) {
    MainWindow window;
    window._backendReady = true;
    QCloseEvent event;
    window.closeEvent(&event);
    window._api.reply(true, invalid.at(i));
    QApplication::processEvents();
    check(QStringLiteral("invalid-%1/held").arg(i), window.backendStops == 0 && !window._campaignCloseReady);
  }
  {
    MainWindow window;
    window._backendReady = true;
    QCloseEvent event;
    window.closeEvent(&event);
    window._api.reply(false, {{"ok", true}, {"draining", true}, {"ready", true}});
    QApplication::processEvents();
    check("failed-request/held", window.backendStops == 0 && !window._campaignCloseReady);
  }
  {
    MainWindow window;
    QCloseEvent event;
    window.closeEvent(&event);
    check("no-service/direct-close", event.isAccepted() && window.backendStops == 1 && window._api.paths.isEmpty());
  }
  std::cout << QJsonDocument(QJsonObject{{"checks", checks}, {"failures", failures}}).toJson(QJsonDocument::Compact).constData() << '\n';
  return failures ? 1 : 0;
}
