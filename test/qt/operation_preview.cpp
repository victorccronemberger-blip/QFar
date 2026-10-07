// Offscreen visual fixture. Never starts a backend or an update check.
#include <memory>
#include "../../desktop/src/MainWindow.hpp"
#include "../../desktop/src/OperationSummary.hpp"
#include "../../desktop/src/CampaignReviewDialog.hpp"
#include <oclero/qlementine/style/QlementineStyle.hpp>
#include <QApplication>
#include <QClipboard>
#include <QDialog>
#include <QDialogButtonBox>
#include <QMessageBox>
#include <QMenu>
#include <QMouseEvent>
#include <QEventLoop>
#include <QRegularExpression>
#include <QComboBox>
#include <QDir>
#include <QLabel>
#include <QPushButton>
#include <QPointer>
#include <QProgressBar>
#include <QSettings>
#include <QFile>
#include <QTemporaryDir>
#include <QStackedWidget>
#include <QListWidget>
#include <QSignalBlocker>
#include <QTimer>
#include <QTcpServer>
#include <QTcpSocket>
#include <QTableWidget>
#include <QTabWidget>
#include <QSpinBox>
#include <QCheckBox>
#include <QLineEdit>
#include <QPlainTextEdit>
#include <QScrollBar>
#include <QUrlQuery>
#include <QFileInfo>

class OperationPreview {
public:
#include "operation_qa.inc"
#include "accounts_qa.inc"
#include "recovery_qa.inc"
#include "prepared_library_qa.inc"
#include "local_media_library_qa.inc"
#include "campaign_capacity_qa.inc"
#include "campaign_reset_qa.inc"
#include "campaign_restriction_qa.inc"
#include "nymeria_library_qa.inc"
#include "campaign_all_compatible_qa.inc"
#include "campaign_on_demand_qa.inc"
#include "catalog_timeout_qa.inc"
#include "catalog_progress_qa.inc"
  static void campaignCloseSmoke(MainWindow& window, bool requestQuit = false) {
    auto* server = new QTcpServer(&window);
    if (!server->listen(QHostAddress::LocalHost)) { qApp->exit(190); return; }
    auto requests = std::make_shared<int>(0);
    auto invalidSession = std::make_shared<bool>(false);
    QObject::connect(server, &QTcpServer::newConnection, server, [server, requests, invalidSession] {
      auto* socket = server->nextPendingConnection();
      QObject::connect(socket, &QTcpSocket::readyRead, socket, [socket, requests, invalidSession] {
        const auto input = socket->property("input").toByteArray() + socket->readAll();
        socket->setProperty("input", input);
        if (!input.contains("\r\n\r\n") || socket->property("answered").toBool()) return;
        socket->setProperty("answered", true);
        QJsonObject result{{"ok", true}};
        if (input.startsWith("POST /api/campaigns/drain ")) {
          ++*requests;
          bool authorized = false;
          for (const auto& line : input.split('\n')) {
            const auto colon = line.indexOf(':');
            if (colon > 0 && line.left(colon).toLower() == "x-qmoney-session"
                && line.mid(colon + 1).trimmed() == "inert-close-fixture") authorized = true;
          }
          if (!authorized) *invalidSession = true;
          result.insert("draining", true);
          result.insert("ready", *requests == 1 ? QJsonValue("true") : QJsonValue(*requests >= 3));
        }
        const auto body = QJsonDocument(result).toJson(QJsonDocument::Compact);
        socket->write("HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\nContent-Length: "
                      + QByteArray::number(body.size()) + "\r\n\r\n" + body);
        socket->disconnectFromHost();
      });
    });
    window._api.setBaseUrl(QStringLiteral("http://127.0.0.1:%1").arg(server->serverPort()));
    window._api.setSessionToken("inert-close-fixture");
    window._backendReady = true;
    if (requestQuit) qApp->quit();
    else window.close();
    if (!window.isVisible() || !window._campaignClosePending) { qApp->exit(191); return; }
    auto premature = std::make_shared<bool>(false);
    auto* monitor = new QTimer(&window);
    QObject::connect(monitor, &QTimer::timeout, &window, [&window, requests, invalidSession, premature] {
      if (*requests < 3 && !window.isVisible()) *premature = true;
      if (!window.isVisible() && window._campaignCloseReady)
        qApp->exit(*premature || *invalidSession || *requests != 3 ? 192 : 0);
    });
    monitor->start(20);
    QTimer::singleShot(5000, &window, [] { qApp->exit(193); });
  }
  static bool campaignCloseFinished(const MainWindow& window) {
    return window._campaignCloseReady && !window.isVisible();
  }
  static void campaignHistoryEvidenceSmoke(MainWindow& window) {
    auto* server=new QTcpServer(&window);
    if(!server->listen(QHostAddress::LocalHost)) {qApp->exit(180);return;}
    auto queries=std::make_shared<int>(0);
    QObject::connect(server,&QTcpServer::newConnection,server,[server,queries] {
      auto* socket=server->nextPendingConnection();
      QObject::connect(socket,&QTcpSocket::readyRead,socket,[socket,queries] {
        const auto input=socket->property("input").toByteArray()+socket->readAll();
        socket->setProperty("input",input);
        if(!input.contains("\r\n\r\n") || socket->property("answered").toBool()) return;
        socket->setProperty("answered",true);
        if(!input.startsWith("GET /api/logs/campaign_fixture.json ")) {qApp->exit(181);return;}
        const bool valid=++*queries==1;
        const QJsonObject current{{"status","confirmed"},{"evidence_source",valid?"journal":"preview"},
          {"reconciled",true},{"chunks_found",1},{"chunks_expected",1},{"detail","Confirmacao posterior do recibo"}};
        const QJsonObject result{{"email","fixture@example.com"},{"status","failed"},{"confirmation","not_confirmed"},
          {"session_id","session-fixture"},{"detail","Falha original 503"},{"current_result",current}};
        const QJsonObject item{{"task","Fixture"},{"clip_uid","clip-fixture"},{"duration_s",60},
          {"success",0},{"failed",1},{"pending",0},{"skipped",0},{"accounts",QJsonArray{result}}};
        const QJsonObject payload{{"schema",2},{"status","error"},
          {"summary",QJsonObject{{"videos",1},{"success",0},{"failed",1},{"pending",0},{"skipped",0}}},
          {"current_summary",QJsonObject{{"confirmed",1},{"pending",0},{"review",0},{"unknown",0}}},
          {"items",QJsonArray{item}}};
        const auto bytes=QJsonDocument(payload).toJson(QJsonDocument::Compact);
        socket->write("HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\nContent-Length: "
          +QByteArray::number(bytes.size())+"\r\n\r\n"+bytes);
        socket->disconnectFromHost();
      });
      QObject::connect(socket,&QTcpSocket::disconnected,socket,&QObject::deleteLater);
    });
    window._api.setBaseUrl(QStringLiteral("http://127.0.0.1:%1").arg(server->serverPort()));
    window._historyTable->setRowCount(1);
    auto* entry=new QTableWidgetItem("Fixture"); entry->setData(Qt::UserRole,"campaign_fixture.json");
    window._historyTable->setItem(0,0,entry); window._historyTable->setCurrentCell(0,0);
    auto* poll=new QTimer(&window); auto phase=std::make_shared<int>(0);
    QObject::connect(poll,&QTimer::timeout,&window,[&window,queries,phase] {
      if(window._historyEvidence->rowCount()!=1) return;
      const auto proof=window._historyEvidence->item(0,3);
      const auto text=window._historyDetail->toPlainText();
      if(*phase==0 && *queries==1 && proof && proof->text()=="Recibo atual confirmado") {
        if(!text.contains("RESULTADO DA TENTATIVA ORIGINAL") || !text.contains("SITUAÇÃO ATUAL DOS RECIBOS")
            || !text.contains("1 confirmado(s)") || !text.contains("0 envio(s) concluído(s)")
            || !proof->toolTip().contains("Falha original 503") || !proof->toolTip().contains("Confirmacao posterior")) {
          qApp->exit(182);return;
        }
        *phase=1; window._historyEvidence->setRowCount(0);
        window._historyTable->setCurrentCell(-1,-1); window._historyTable->setCurrentCell(0,0);
      } else if(*phase==1 && *queries==2 && proof && proof->text()=="Falhou") {
        qApp->exit(text.contains("0 confirmado(s)") && text.contains("1 sem recibo atual")?0:183);
      }
    });
    poll->start(20); QTimer::singleShot(5000,qApp,[]{qApp->exit(184);});
  }
  static void originalLibrarySmoke(MainWindow& window) {
    const QString previousClipboard = QApplication::clipboard()->text();
    QObject::connect(qApp, &QCoreApplication::aboutToQuit, qApp, [previousClipboard] { QApplication::clipboard()->setText(previousClipboard); });
    auto* server = new QTcpServer(&window);
    if (!server->listen(QHostAddress::LocalHost)) { qApp->exit(130); return; }
    auto builds = std::make_shared<int>(0);
    QObject::connect(server, &QTcpServer::newConnection, server, [server, builds] {
      auto* socket = server->nextPendingConnection();
      QObject::connect(socket, &QTcpSocket::readyRead, socket, [socket, builds] {
        auto input = socket->property("input").toByteArray() + socket->readAll();
        socket->setProperty("input", input);
        if (!input.contains("\r\n\r\n") || socket->property("answered").toBool()) return;
        socket->setProperty("answered", true);
        QJsonObject payload;
        int status = 200;
        if (input.startsWith("GET /api/library/ego4d ")) payload = {{"state","missing"},{"needs_index",true}};
        else if (input.startsWith("POST /api/library/ego4d/index ")) {
          if (++*builds == 1) { status = 202; payload = {{"loading",true},{"message","Indexando atividades…"}}; }
          else if (*builds == 2) payload = {}; // HTTP 200 does not prove index readiness.
          else payload = {{"state","ready"},{"needs_index",false},{"videos",55},{"clips",100},{"annotations",500}};
        } else if (input.startsWith("GET /api/library/ego4d/videos?")) {
          const bool empty = input.contains("q=missing");
          const int offset = input.contains("offset=50") ? 50 : 0;
          QJsonArray items;
          if (!empty) for (int i = 0; i < (offset ? 5 : 50); ++i)
            items.append(QJsonObject{{"uid",QStringLiteral("original-%1").arg(offset+i)},
              {"duration_s",600.123},{"scenarios","Gardening"},{"device","Original camera"},
              {"has_imu",1},{"imu_local",false}});
          payload = {{"items",items},{"total",empty?0:55},{"offset",offset},{"provenance","ego4d_original"}};
          if (input.contains("q=broken")) payload = {};
          if (input.contains("q=invalidrow")) payload["items"] = QJsonArray{QJsonObject{{"uid","bad"}}};
        } else { qApp->exit(131); return; }
        const auto bytes = QJsonDocument(payload).toJson(QJsonDocument::Compact);
        socket->write("HTTP/1.1 " + QByteArray::number(status) + " OK\r\nContent-Type: application/json\r\nConnection: close\r\nContent-Length: "
          + QByteArray::number(bytes.size()) + "\r\n\r\n" + bytes);
        socket->disconnectFromHost();
      });
      QObject::connect(socket, &QTcpSocket::disconnected, socket, &QObject::deleteLater);
    });
    window._api.setBaseUrl(QStringLiteral("http://127.0.0.1:%1").arg(server->serverPort()));
    window._pages->setCurrentIndex(4);
    auto* explore = window.findChild<QPushButton*>(QStringLiteral("egoLibraryExplore"));
    if (!explore) { qApp->exit(132); return; }
    explore->click();
    auto* poll = new QTimer(&window);
    auto phase = std::make_shared<int>(0);
    QObject::connect(poll, &QTimer::timeout, &window, [&window, phase, builds, poll] {
      auto* dialog = window.findChild<QDialog*>(QStringLiteral("egoLibraryDialog"));
      if (!dialog) return;
      auto* summary = dialog->findChild<QLabel*>(QStringLiteral("egoLibrarySummary"));
      auto* table = dialog->findChild<QTableWidget*>(QStringLiteral("egoLibraryTable"));
      auto* search = dialog->findChild<QPushButton*>(QStringLiteral("egoLibrarySearch"));
      auto* index = dialog->findChild<QPushButton*>(QStringLiteral("egoLibraryIndex"));
      if (*phase == 0 && summary->text().contains("Atualize")) {
        if (search->isEnabled()) { qApp->exit(133); return; }
        *phase = 1; index->click(); index->click();
      } else if (*phase == 1 && summary->text().contains("incompleta")) {
        if (search->isEnabled() || table->rowCount()) { qApp->exit(139); return; }
        *phase = 4; index->click();
      } else if (*phase == 4 && search->isEnabled() && table->rowCount() == 50) {
        if (*builds != 3) { qApp->exit(134); return; }
        table->setCurrentCell(0,0);
        for (auto* button : dialog->findChildren<QPushButton*>()) if (button->text() == "Copiar ID") button->click();
        if (QApplication::clipboard()->text() != "original-0") { qApp->exit(135); return; }
        dialog->grab().save(QStringLiteral("build/ego4d-library-preview.png"));
        *phase = 2;
        for (auto* button : dialog->findChildren<QPushButton*>()) if (button->text() == "Próxima") button->click();
      } else if (*phase == 2 && search->isEnabled() && table->rowCount() == 5) {
        if (table->item(0,0)->text() != "original-50") { qApp->exit(136); return; }
        *phase = 3;
        dialog->findChild<QLineEdit*>(QStringLiteral("egoLibraryQuery"))->setText(QStringLiteral("missing"));
        search->click();
      } else if (*phase == 3 && search->isEnabled() && table->rowCount() == 0) {
        if (!dialog->findChild<QLabel*>(QStringLiteral("egoLibraryCount"))->text().contains("Nenhum")) { qApp->exit(137); return; }
        *phase = 5;
        dialog->findChild<QLineEdit*>(QStringLiteral("egoLibraryQuery"))->setText(QStringLiteral("broken"));
        search->click();
      } else if (*phase == 5 && search->isEnabled()
          && dialog->findChild<QLabel*>(QStringLiteral("egoLibraryCount"))->text().contains("incompleta")) {
        if (table->rowCount()) { qApp->exit(140); return; }
        *phase = 7;
        dialog->findChild<QLineEdit*>(QStringLiteral("egoLibraryQuery"))->setText(QStringLiteral("invalidrow"));
        search->click();
      } else if (*phase == 7 && search->isEnabled()
          && dialog->findChild<QLabel*>(QStringLiteral("egoLibraryCount"))->text().contains("incompletos")) {
        if (table->rowCount()) { qApp->exit(141); return; }
        *phase = 6;
        dialog->findChild<QLineEdit*>(QStringLiteral("egoLibraryQuery"))->clear();
        search->click();
      } else if (*phase == 6 && search->isEnabled() && table->rowCount() == 50) {
        poll->stop(); dialog->close();
        QTimer::singleShot(100, &window, [] { qApp->exit(0); });
      }
    });
    poll->start(25);
    QTimer::singleShot(9000, &window, [] { qApp->exit(138); });
  }
  static void originalLiveLab(MainWindow& window) {
    const auto endpoint = qEnvironmentVariable("QMONEY_TEST_API");
    if (!endpoint.startsWith(QStringLiteral("http://127.0.0.1:"))) { qApp->exit(160); return; }
    QFile input(qEnvironmentVariable("QMONEY_TEST_CAPTURE_BODY"));
    if (!input.open(QIODevice::ReadOnly)) { qApp->exit(161); return; }
    const auto body = QJsonDocument::fromJson(input.readAll()).object();
    if (body.isEmpty()) { qApp->exit(162); return; }
    window._api.setBaseUrl(endpoint);
    window._api.setSessionToken(qgetenv("QMONEY_TEST_SESSION"));
    window._backendReady = true;
    window.setWindowTitle(QStringLiteral("QMoney — campanha com mídia real em laboratório local"));
    window._api.post(QStringLiteral("/api/campaigns/original/preflight"), body,
      [&window, body](bool ok, const QJsonDocument& doc, const QString& error) {
        if (!ok || doc.object().value("ok") != QJsonValue(true)) {
          qWarning() << "LAB_PREFLIGHT" << error; qApp->exit(163); return;
        }
        auto approved = body;
        approved.insert(QStringLiteral("preflight_id"), doc.object().value("preflight_id"));
        window.submitOriginalCapture(approved, doc.object().value("original_summary").toObject());
      });
    auto phase = std::make_shared<int>(0);
    auto* timer = new QTimer(&window);
    QObject::connect(timer, &QTimer::timeout, &window, [&window, phase, timer] {
      if (*phase == 0 && window._campaignActive && !window._campaignStartPending) {
        *phase = 1; window.loadHome();
      } else if (*phase == 1 && window._operationPause->isEnabled()) {
        *phase = 10;
        window._api.get(QStringLiteral("/fixture/progress"), [&window, phase](bool ok, const QJsonDocument& doc, const QString&) {
          if (!ok) { qApp->exit(166); return; }
          if (doc.object().value("staged_blocks").toInt() < 1) { *phase = 1; return; }
          *phase = 2;
          if (qApp->arguments().contains("--lab-stop")) window._campaignStop->click();
          else window._operationPause->click();
        });
      } else if (*phase == 2 && window._operationPauseRequested && window._operationPause->isEnabled()) {
        *phase = 3; window._operationPause->click();
      } else if ((*phase == 3 || (*phase == 2 && qApp->arguments().contains("--lab-stop")))
                 && !window._campaignActive && !window._campaignStartPending && !window._campaignStartUncertain) {
        *phase = 4;
        window._api.get(QStringLiteral("/api/campaigns/current"), [&window](bool ok, const QJsonDocument& doc, const QString&) {
          const auto expected = qApp->arguments().contains("--lab-stop") ? QStringLiteral("stopped") : QStringLiteral("done");
          if (!ok || doc.object().value("state").toString() != expected) { qApp->exit(165); return; }
          window._pages->setCurrentIndex(7); window.loadHistory();
        });
      } else if (*phase == 4 && window._historyTable->rowCount() == 1) {
        timer->stop();
        qInfo() << "LAB_UI_FULL_FLOW_OK" << (qApp->arguments().contains("--lab-stop") ? "stop" : "pause-resume");
        if (!qApp->arguments().contains("--lab-ui-hold")) qApp->exit(0);
      }
    });
    timer->start(25);
    QTimer::singleShot(qApp->arguments().contains("--lab-ui-hold") ? 180000 : 45000,
      &window, [phase] { qApp->exit(*phase == 4 ? 0 : 164); });
  }
  static void campaignStartPersistenceSmoke(MainWindow& window) {
    auto* server = new QTcpServer(&window);
    if (!server->listen(QHostAddress::LocalHost)) { qApp->exit(150); return; }
    auto posts = std::make_shared<int>(0);
    auto lookups = std::make_shared<int>(0);
    const QString identity = QStringLiteral("ec7c75cc-9a46-4b70-bd62-f531a3b0309e");
    QObject::connect(server, &QTcpServer::newConnection, server, [server, posts, lookups, identity] {
      auto* socket = server->nextPendingConnection();
      QObject::connect(socket, &QTcpSocket::readyRead, socket, [socket, posts, lookups, identity] {
        auto input = socket->property("input").toByteArray() + socket->readAll();
        socket->setProperty("input", input);
        const int end = input.indexOf("\r\n\r\n");
        if (end < 0 || socket->property("answered").toBool()) return;
        const auto match = QRegularExpression(QStringLiteral("Content-Length: (\\d+)"), QRegularExpression::CaseInsensitiveOption)
          .match(QString::fromLatin1(input.left(end)));
        if (match.hasMatch() && input.size() < end + 4 + match.captured(1).toInt()) return;
        socket->setProperty("answered", true);
        QJsonObject reply; int status = 200;
        if (input.startsWith("POST /api/campaigns ")) {
          ++*posts;
          const auto saved = QJsonDocument::fromJson(QSettings().value("campaign/pendingStart").toByteArray()).object();
          if (saved.value("start_request_id").toString() != identity || *posts != 1) { qApp->exit(151); return; }
          status = 503; reply = {{"error_code", "request_outcome_unknown"}, {"error", "Resposta perdida"}};
        } else if (input.startsWith("GET /api/campaigns/starts/")) {
          ++*lookups;
          if (!input.startsWith(("GET /api/campaigns/starts/" + identity + " ").toUtf8())) { qApp->exit(152); return; }
          reply = {{"ok", true}, {"found", true}, {"start_request_id", identity},
            {"status", "review"}, {"may_start", false}, {"kind", "dataset"}, {"outcome_unknown", true}};
          if (qApp->arguments().contains("--start-terminal-persistence")) {
            reply.insert("terminal", true); reply.insert("execution_state", "error");
            reply.insert("log_name", "campaign_fixture.json"); reply.insert("delivery_confirmed", false);
          }
        } else if (input.startsWith("GET /api/campaigns/current?")) {
          reply = {{"state", "idle"}, {"start_request_id", "foreign-operation"}, {"totals", QJsonObject{}}};
        } else { qApp->exit(153); return; }
        const auto bytes = QJsonDocument(reply).toJson(QJsonDocument::Compact);
        socket->write("HTTP/1.1 " + QByteArray::number(status) + " OK\r\nContent-Type: application/json\r\nConnection: close\r\nContent-Length: "
          + QByteArray::number(bytes.size()) + "\r\n\r\n" + bytes);
        socket->disconnectFromHost();
      });
      QObject::connect(socket, &QTcpSocket::disconnected, socket, &QObject::deleteLater);
    });
    const auto base = QStringLiteral("http://127.0.0.1:%1").arg(server->serverPort());
    window._api.setBaseUrl(base);
    if (qApp->arguments().contains("--start-persistence-read")) {
      if (!window._campaignStartUncertain || window._campaignRequestedPreflight != identity) { qApp->exit(157); return; }
      window.pollCampaign();
    } else window.submitCampaign({{"preflight_id", identity}});
    auto* probe = new QTimer(&window);
    QObject::connect(probe, &QTimer::timeout, &window, [&window, posts, lookups, identity, base, probe] {
      if (*lookups == 0) return;
      if (qApp->arguments().contains("--start-terminal-persistence")) {
        probe->stop();
        qApp->exit(!window._campaignStartUncertain && window._campaignRequestedPreflight.isEmpty()
          && QSettings().value("campaign/pendingStart").toByteArray().isEmpty() && *posts == 1 ? 0 : 158);
        return;
      }
      if (!window._campaignStartUncertain) return;
      if (qApp->arguments().contains("--start-persistence-write")) { probe->stop(); qApp->exit(*posts == 1 ? 0 : 159); return; }
      probe->stop(); window._campaignPoll.stop();
      auto* restored = new MainWindow(qobject_cast<oclero::qlementine::QlementineStyle*>(qApp->style()), nullptr, false);
      restored->_api.setBaseUrl(base);
      if (!restored->_campaignStartUncertain || restored->_campaignRequestedPreflight != identity
          || restored->_campaignStart->isEnabled()) { qApp->exit(154); return; }
      restored->pollCampaign();
      QTimer::singleShot(300, restored, [restored, posts, lookups, identity] {
        const auto saved = QJsonDocument::fromJson(QSettings().value("campaign/pendingStart").toByteArray()).object();
        const int expectedPosts = qApp->arguments().contains("--start-persistence-read") ? 0 : 1;
        qApp->exit(*posts == expectedPosts && *lookups >= 2 && restored->_campaignStartUncertain
          && !restored->_campaignStart->isEnabled() && saved.value("start_request_id").toString() == identity ? 0 : 155);
      });
    });
    probe->start(25); QTimer::singleShot(8000, &window, [] { qApp->exit(156); });
  }
  static void campaignConfirmedRestartSmoke(MainWindow& window) {
    // Inert HTTP service: exercise real UI/ApiClient methods in one window,
    // replacing only the restarted Runner's snapshot. No backend is launched.
    const QString identity = QStringLiteral("d1affac3-94b5-4c7c-8732-267b151f7b34");
    auto phase = std::make_shared<int>(0);
    auto posts = std::make_shared<int>(0);
    auto lookups = std::make_shared<int>(0);
    auto foreignReplies = std::make_shared<int>(0);
    auto terminal = std::make_shared<bool>(false);
    auto* server = new QTcpServer(&window);
    if (!server->listen(QHostAddress::LocalHost)) { qApp->exit(170); return; }
    QObject::connect(server, &QTcpServer::newConnection, server,
        [server, identity, phase, posts, lookups, foreignReplies, terminal] {
      auto* socket = server->nextPendingConnection();
      QObject::connect(socket, &QTcpSocket::readyRead, socket,
          [socket, identity, phase, posts, lookups, foreignReplies, terminal] {
        auto input = socket->property("input").toByteArray() + socket->readAll();
        socket->setProperty("input", input);
        const int end = input.indexOf("\r\n\r\n");
        if (end < 0 || socket->property("answered").toBool()) return;
        const auto length = QRegularExpression(QStringLiteral("Content-Length: (\\d+)"),
            QRegularExpression::CaseInsensitiveOption).match(QString::fromLatin1(input.left(end)));
        if (length.hasMatch() && input.size() < end + 4 + length.captured(1).toInt()) return;
        socket->setProperty("answered", true);
        QJsonObject reply;
        if (input.startsWith("POST /api/campaigns ")) {
          ++*posts;
          const auto body = QJsonDocument::fromJson(input.mid(end + 4)).object();
          const auto saved = QJsonDocument::fromJson(QSettings().value("campaign/pendingStart").toByteArray()).object();
          if (*posts != 1 || body.value("preflight_id") != identity || saved.value("start_request_id") != identity) {
            qApp->exit(171); return;
          }
          reply = {{"ok", true}, {"already_running", false}, {"start_request_id", identity},
            {"preflight_id", identity}, {"total_sends", 1}, {"accounts", QJsonArray{"fixture@example.com"}}};
        } else if (input.startsWith("GET /api/campaigns/current?")) {
          if (*phase != 0) ++*foreignReplies;
          reply = {{"state", *phase == 0 ? "running" : "idle"},
            {"start_request_id", *phase == 0 ? identity : QStringLiteral("foreign-operation")},
            {"totals", QJsonObject{}}};
        } else if (input.startsWith(("GET /api/campaigns/starts/" + identity + " ").toUtf8())) {
          ++*lookups;
          reply = {{"ok", true}, {"found", true}, {"start_request_id", identity}, {"may_start", false},
            {"status", "admitted"}, {"terminal", *terminal}, {"execution_state", *terminal ? "error" : "review"},
            {"delivery_confirmed", false}, {"log_name", *terminal ? QJsonValue("campaign_fixture.json") : QJsonValue()}};
        } else if (input.startsWith("GET /api/integrations ") || input.startsWith("GET /api/accounts/domains ")) {
          reply = {{"ok", true}};
        } else if(input.startsWith("GET /api/accounts ")) {
          reply={{"accounts",QJsonArray{}}};
        } else if(input.startsWith("GET /api/accounts/proxies ")) {
          reply={{"proxies",QJsonArray{}}};
        } else if(input.startsWith("GET /api/accounts/migration ") || input.startsWith("GET /api/accounts/bulk-register/status ")) {
          reply={{"state","idle"}};
        } else { qApp->exit(172); return; }
        const auto bytes = QJsonDocument(reply).toJson(QJsonDocument::Compact);
        socket->write("HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\nContent-Length: "
          + QByteArray::number(bytes.size()) + "\r\n\r\n" + bytes);
        socket->disconnectFromHost();
      });
      QObject::connect(socket, &QTcpSocket::disconnected, socket, &QObject::deleteLater);
    });
    window._api.setBaseUrl(QStringLiteral("http://127.0.0.1:%1").arg(server->serverPort()));
    {
      const QSignalBlocker a(window._campaignAccounts), t(window._campaignTasks);
      auto* account = new QListWidgetItem("fixture@example.com", window._campaignAccounts);
      account->setData(Qt::UserRole, "fixture@example.com"); account->setCheckState(Qt::Checked);
      auto* task = new QListWidgetItem("Fixture", window._campaignTasks);
      task->setData(Qt::UserRole, "fixture-task"); task->setCheckState(Qt::Checked);
    }
    window._taskReload.stop(); window._backendReady = true;
    window.submitCampaign({{"preflight_id", identity}});
    auto* probe = new QTimer(&window);
    QObject::connect(probe, &QTimer::timeout, &window,
        [&window, probe, identity, phase, posts, lookups, foreignReplies, terminal] {
      if (window._campaignStartPending || window._campaignPollInFlight || window._campaignStartLookupInFlight) return;
      const auto saved = QJsonDocument::fromJson(QSettings().value("campaign/pendingStart").toByteArray()).object();
      if (*phase == 0 && *posts == 1 && window._campaignActive) {
        if (window._campaignStartUncertain || window._campaignRequestedPreflight != identity
            || saved.value("start_request_id") != identity) { qApp->exit(173); return; }
        *phase = 1; window._campaignPoll.stop();
        window.setBackendReady(false, QStringLiteral("Reiniciando motor de demonstração"));
        window.setBackendReady(true); window.pollCampaign();
      } else if (*phase == 1 && *foreignReplies > 0) {
        if (!window._campaignStartUncertain || window._campaignRequestedPreflight != identity
            || saved.value("start_request_id") != identity || window._campaignStart->isEnabled()
            || window._campaignOriginal->isEnabled() || *posts != 1) {
          qInfo() << "CONFIRMED_RESTART_LOST_BINDING" << window._campaignStartUncertain
                  << window._campaignStart->isEnabled() << window._campaignRequestedPreflight;
          qApp->exit(174); return;
        }
        if (*lookups == 0) return;
        window._campaignStart->click(); window._campaignOriginal->click();
        if (window.rememberCampaignStart(QStringLiteral("different-operation"))
            || window._campaignRequestedPreflight != identity
            || QJsonDocument::fromJson(QSettings().value("campaign/pendingStart").toByteArray()).object()
                 .value("start_request_id") != identity) { qApp->exit(175); return; }
        *phase = 2; *terminal = true; window.pollCampaign();
      } else if (*phase == 2 && !window._campaignStartUncertain) {
        probe->stop();
        const bool resolved = window._campaignRequestedPreflight.isEmpty()
          && QSettings().value("campaign/pendingStart").toByteArray().isEmpty()
          && window._campaignStart->isEnabled() && window._campaignOriginal->isEnabled()
          && *posts == 1 && *lookups >= 2;
        qInfo() << "CONFIRMED_RESTART_RESOLVED" << resolved << "posts" << *posts << "lookups" << *lookups;
        qApp->exit(resolved ? 0 : 176);
      }
    });
    probe->start(25); QTimer::singleShot(8000, &window, [] { qApp->exit(177); });
  }
  static void campaignParametersSmoke() {
    QSettings().setValue(QStringLiteral("campaign/draft"),QJsonDocument(QJsonObject{
      {"delay_mode","fixed"},{"delay_seconds",75},{"active_hours",false},{"hour_start",7},{"hour_end",18}
    }).toJson(QJsonDocument::Compact));
    MainWindow restored(qobject_cast<oclero::qlementine::QlementineStyle*>(qApp->style()),nullptr,false);
    if(restored._delaySeconds->isHidden() || restored._delaySeconds->value()!=75
        || restored._hourStart->isEnabled() || restored._hourEnd->isEnabled()) { qApp->exit(110); return; }
    restored._activeHours->setChecked(true); restored._hourStart->setValue(23);
    if(restored._hourEnd->value()!=24) { qApp->exit(111); return; }
    restored._hourEnd->setValue(4);
    if(restored._hourStart->value()!=3) { qApp->exit(112); return; }
    restored._delayMode->setCurrentIndex(restored._delayMode->findData("off"));
    if(!restored._delaySeconds->isHidden()) { qApp->exit(113); return; }
    restored._delayMode->setCurrentIndex(restored._delayMode->findData("fixed"));
    qApp->exit(!restored._delaySeconds->isHidden() && restored._delaySeconds->value()==75 ? 0:114);
  }
  static void campaignControlsSmoke(MainWindow& window) {
    const bool recover = qApp->arguments().contains("--preflight-recovery");
    const bool exhaust = qApp->arguments().contains("--preflight-exhaust");
    auto* server = new QTcpServer(&window);
    if (!server->listen(QHostAddress::LocalHost)) { qApp->exit(100); return; }
    struct Flow { int preflights=0, starts=0, stops=0, pauses=0, resumes=0, phase=0; bool running=false, paused=false; QByteArray preflightPath, preflightBody; };
    auto flow=std::make_shared<Flow>();
    QObject::connect(server,&QTcpServer::newConnection,server,[server,flow,recover,exhaust] {
      auto* socket=server->nextPendingConnection();
      QObject::connect(socket,&QTcpSocket::readyRead,socket,[socket,flow,recover,exhaust] {
        auto input=socket->property("input").toByteArray()+socket->readAll(); socket->setProperty("input",input);
        const int end=input.indexOf("\r\n\r\n");
        if(end<0 || socket->property("answered").toBool()) return;
        const auto match=QRegularExpression(QStringLiteral("Content-Length: (\\d+)"),QRegularExpression::CaseInsensitiveOption)
          .match(QString::fromLatin1(input.left(end)));
        if(match.hasMatch() && input.size()<end+4+match.captured(1).toInt()) return;
        socket->setProperty("answered",true);
        QJsonObject result; int delay=0; QByteArray status="200 OK";
        if(input.startsWith("POST /api/campaigns/preflight?async=1&request_id=")) {
          ++flow->preflights; delay=250;
          const auto path = input.split(' ').at(1);
          const int expected = recover ? 4 : 3;
          if (flow->preflights == 1) { flow->preflightPath = path; flow->preflightBody = input.mid(end+4); }
          if ((exhaust || flow->preflights <= expected)
              && (flow->preflightPath != path || flow->preflightBody != input.mid(end+4))) { qApp->exit(115); return; }
          if (exhaust || (recover && flow->preflights == 1)) {
            status="504 Gateway Timeout";
            result={{"loading",true},{"code","catalog_work_pending"},{"error","Verificação continua no serviço"}};
          } else if (recover && flow->preflights == 2) { socket->abort(); return; }
          else if (flow->preflights < expected) result={{"loading",true},{"message","Conferindo acesso…"},{"elapsed_s",137}};
          else result={{"ok",true},{"preflight_id","fixture"},{"accounts",QJsonObject{{"validated",1}}},
                  {"tasks",QJsonObject{{"compatible",1}}},{"blockers",QJsonArray{}},{"warnings",QJsonArray{}}};
          if (!exhaust && flow->preflights >= expected) result["capacity"] = capacityFixture(QJsonDocument::fromJson(input.mid(end+4)).object(), {"fixture@example.com"});
        } else if(input.startsWith("POST /api/campaigns ")) {
          ++flow->starts; flow->running=true; delay=200; result={{"ok",true},{"accounts",QJsonArray{"fixture@example.com"}}};
        } else if(input.startsWith("POST /api/campaigns/pause ")) {
          ++flow->pauses; flow->paused=true; result={{"ok",true}};
        } else if(input.startsWith("POST /api/campaigns/resume ")) {
          ++flow->resumes; flow->paused=false; result={{"ok",true}};
        } else if(input.startsWith("POST /api/campaigns/stop ")) {
          ++flow->stops; flow->running=false; delay=200; result={{"ok",true}};
        } else if(input.startsWith("GET /api/accounts ")) {
          result={{"accounts",QJsonArray{QJsonObject{{"email","fixture@example.com"}}}}};
        } else if(input.startsWith("GET /api/balances ")) { result={{"balances",QJsonObject{}}};
        } else if(input.startsWith("GET /api/campaigns/current")) {
          result={{"start_request_id",flow->starts>0?"fixture":""},{"state",flow->running?"running":flow->stops?"stopped":"idle"},{"pause_requested",flow->paused},
                  {"operation",QJsonObject{{"accounts",QJsonArray{}},{"counts",QJsonObject{}}}},
                  {"totals",QJsonObject{{"total_sends",1},{"ok_sends",0}}}};
        } else if(input.startsWith("GET /api/logs ")) { result={{"logs",QJsonArray{}}};
        } else if(input.startsWith("GET /api/tasks?")) {
          result={{"tasks",QJsonArray{QJsonObject{{"id","fixture-task"},{"name","Fixture"},
            {"available_for_duration",true},{"clip_count",1}}}}};
        } else { qApp->exit(101); return; }
        const auto bytes=QJsonDocument(result).toJson(QJsonDocument::Compact);
        QTimer::singleShot(delay,socket,[socket,bytes,status] {
          socket->write("HTTP/1.1 " + status + "\r\nContent-Type: application/json\r\nConnection: close\r\nContent-Length: "
            +QByteArray::number(bytes.size())+"\r\n\r\n"+bytes); socket->disconnectFromHost();
        });
      });
      QObject::connect(socket,&QTcpSocket::disconnected,socket,&QObject::deleteLater);
    });
    window._api.setBaseUrl(QStringLiteral("http://127.0.0.1:%1").arg(server->serverPort()));
    window._pages->setCurrentIndex(3);
    {
      const QSignalBlocker a(window._campaignAccounts),t(window._campaignTasks);
      auto* account=new QListWidgetItem("fixture@example.com",window._campaignAccounts);
      account->setData(Qt::UserRole,"fixture@example.com"); account->setCheckState(Qt::Checked);
      auto* task=new QListWidgetItem("Fixture",window._campaignTasks);
      task->setData(Qt::UserRole,"fixture-task"); task->setCheckState(Qt::Checked);
    }
    window._taskReload.stop(); window._campaignActive=false; window.updateCampaignActions();
    if(qApp->arguments().contains("--campaign-controls-manual")) {
      window.setWindowTitle(QStringLiteral("QMoney — teste isolado da campanha"));
      window.loadHome();
      return;
    }
    auto* timer=new QTimer(&window);
    QObject::connect(timer,&QTimer::timeout,&window,[&window,flow,recover,exhaust] {
      auto* dialog=qobject_cast<QDialog*>(QApplication::activeModalWidget());
      if (exhaust && dialog && dialog->windowTitle()==QStringLiteral("Verificação não concluída")) {
        if (flow->preflights != 5 || flow->starts || window._campaignPreflightRecoveries != 4) { qApp->exit(116); return; }
        flow->phase = 99;
        dialog->accept();
        QTimer::singleShot(150, &window, [&window,flow] {
          qApp->exit(flow->preflights==5 && !flow->starts && !window._campaignPreflightPending ? 0 : 117);
        });
        return;
      }
      if(dialog && dialog->windowTitle()==QStringLiteral("Revisar campanha")) {
        if(window._campaignStart->isEnabled() || window._campaignReset->isEnabled()) { qApp->exit(102); return; }
        if(flow->phase==1) { flow->phase=2; dialog->reject(); }
        else if(flow->phase==3) { flow->phase=4; dialog->accept(); }
        return;
      }
      if(flow->phase==0) {
        window._campaignTasks->item(0)->setCheckState(Qt::Unchecked); window.pollCampaign();
        if(window._campaignStart->isEnabled()) { qApp->exit(103); return; }
        window._campaignTasks->item(0)->setCheckState(Qt::Checked);
        flow->phase=1; window._campaignStart->click(); window.startCampaign(); window.pollCampaign();
        if(!window._campaignPreflightPending || window._campaignStart->isEnabled()) qApp->exit(104);
      } else if(flow->phase==1 && window._campaignIndicatorTitle->text()!=QStringLiteral("Verificando campanha")) qApp->exit(105);
      else if(flow->phase==2 && !window._campaignPreflightPending) {
        if(flow->preflights!=(recover ? 4 : 3) || flow->starts || !window._campaignStart->isEnabled()) { qApp->exit(106); return; }
        flow->phase=3; window._campaignStart->click();
      } else if(flow->phase==4 && window._campaignActive && !window._campaignStartPending) {
        if(flow->starts!=1 || window._campaignStart->isEnabled() || !window._campaignStop->isEnabled()) { qApp->exit(107); return; }
        flow->phase=5; window.loadHome();
      } else if(flow->phase==5 && window._operationPause->isEnabled()) {
        flow->phase=6; window._operationPause->click();
      } else if(flow->phase==6 && window._operationPauseRequested && window._operationPause->isEnabled()) {
        flow->phase=7; window._operationPause->click();
      } else if(flow->phase==7 && !window._operationPauseRequested && flow->resumes==1 && window._operationPause->isEnabled()) {
        flow->phase=8; window._campaignStop->click(); window._campaignStop->click();
      } else if(flow->phase==8 && !window._campaignActive && !window._campaignStopPending) {
        qApp->exit(flow->stops==1 && flow->pauses==1 && flow->resumes==1 && window._campaignStart->isEnabled()
          && !window._campaignStop->isEnabled() ? 0:108);
      }
    });
    timer->start(25); QTimer::singleShot(10000,&window,[] { qApp->exit(109); });
  }
  static void campaignEvaluationResumeSmoke(MainWindow& window) {
    struct Flow { int reads=0; int recoveryReads=0; int resumeRequests=0; bool terminal=false; bool wrongResume=false; int phase=0; };
    auto flow = std::make_shared<Flow>();
    auto* server = new QTcpServer(&window);
    if (!server->listen(QHostAddress::LocalHost)) { qApp->exit(220); return; }
    QObject::connect(server, &QTcpServer::newConnection, server, [server, flow] {
      auto* socket = server->nextPendingConnection();
      QObject::connect(socket, &QTcpSocket::readyRead, socket, [socket, flow] {
        const auto input = socket->property("input").toByteArray() + socket->readAll();
        socket->setProperty("input", input);
        const int headerEnd = input.indexOf("\r\n\r\n");
        if (headerEnd < 0 || socket->property("answered").toBool()) return;
        const auto length = QRegularExpression(QStringLiteral("Content-Length: (\\d+)"),
            QRegularExpression::CaseInsensitiveOption).match(QString::fromLatin1(input.left(headerEnd)));
        if (length.hasMatch() && input.size() < headerEnd + 4 + length.captured(1).toInt()) return;
        socket->setProperty("answered", true);
        QJsonObject payload;
        if (input.startsWith("GET /api/campaigns/current")) {
          ++flow->reads;
          if (!flow->terminal) {
            payload = QJsonObject{
              {"state", "running"}, {"stage", "Retomando avaliação"},
              {"current", "Retomando avaliação do lote existente; aguardando confirmação do serviço."},
              {"pause_requested", false},
              {"totals", QJsonObject{{"total_sends", 1}, {"ok_sends", 0}, {"failed_sends", 0}}},
              {"operation", QJsonObject{
                {"accounts", QJsonArray{QJsonObject{{"email", "fixture@example.invalid"},
                  {"state", "recovering"}, {"detail", "Retomando avaliação do envio existente."}}}},
                {"counts", QJsonObject{{"recovering", 1}}}}},
              {"events", QJsonArray{}}};
          } else {
            payload = QJsonObject{
              {"state", "error"}, {"stage", "Lote não confirmado"},
              {"current", "Lote não confirmado; a avaliação não confirmou a conclusão dos envios."},
              {"pause_requested", false},
              {"totals", QJsonObject{{"total_sends", 1}, {"ok_sends", 0}, {"failed_sends", 1}}},
              {"operation", QJsonObject{
                {"accounts", QJsonArray{QJsonObject{{"email", "fixture@example.invalid"},
                  {"state", "unconfirmed"}, {"failed", 1},
                  {"detail", "A avaliação do lote não confirmou os envios."}}}},
                {"counts", QJsonObject{{"unconfirmed", 1}}}}},
              {"events", QJsonArray{}}};
          }
        } else if (input.startsWith("GET /api/recovery?async=1")) {
          ++flow->recoveryReads;
          payload = QJsonObject{{"pending", 2}, {"confirmed", 0},
            {"items", QJsonArray{
              QJsonObject{{"email", "fixture@example.invalid"}, {"clip_uid", "clip-keep"},
                {"session_id", "session-keep-001"}, {"status", "pending"}, {"can_resume", true},
                {"detail", "Outra sessão preservada para recuperação."}},
              QJsonObject{{"email", "fixture@example.invalid"}, {"clip_uid", "clip-selected"},
                {"session_id", "session-selected-002"}, {"status", "pending"}, {"can_resume", true},
                {"detail", "Sessão escolhida para retomada."}}}},
            {"worker", QJsonObject{{"state", "idle"}}}};
        } else if (input.startsWith("POST /api/recovery/resume ")) {
          ++flow->resumeRequests;
          const auto requestBody = QJsonDocument::fromJson(input.mid(headerEnd + 4)).object();
          flow->wrongResume = requestBody.value("email") != QStringLiteral("fixture@example.invalid")
              || requestBody.value("session_id") != QStringLiteral("session-selected-002")
              || requestBody.value("confirmed") != QJsonValue(true);
          payload = QJsonObject{{"ok", true}};
        } else { qCritical() << "Unexpected campaign evaluation QA request" << input.left(input.indexOf('\r')); qApp->exit(221); return; }
        const auto body = QJsonDocument(payload).toJson(QJsonDocument::Compact);
        socket->write("HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\nContent-Length: "
            + QByteArray::number(body.size()) + "\r\n\r\n" + body);
        socket->disconnectFromHost();
      });
      QObject::connect(socket, &QTcpSocket::disconnected, socket, &QObject::deleteLater);
    });

    window._api.setBaseUrl(QStringLiteral("http://127.0.0.1:%1").arg(server->serverPort()));
    page(window, 3);
    window._taskReload.stop();
    {
      const QSignalBlocker accounts(window._campaignAccounts), tasks(window._campaignTasks);
      auto* account = new QListWidgetItem(QStringLiteral("fixture@example.invalid"), window._campaignAccounts);
      account->setData(Qt::UserRole, QStringLiteral("fixture@example.invalid"));
      account->setCheckState(Qt::Checked);
      auto* task = new QListWidgetItem(QStringLiteral("Fixture"), window._campaignTasks);
      task->setData(Qt::UserRole, QStringLiteral("fixture-task"));
      task->setCheckState(Qt::Checked);
    }
    window._campaignActive = false;
    window._campaignStart->setText(QStringLiteral("Iniciar campanha"));
    window.updateCampaignActions();
    auto phase = std::make_shared<int>(0);
    auto* poll = new QTimer(&window);
    QObject::connect(poll, &QTimer::timeout, &window, [&window, flow, phase, poll] {
      if (*phase == 0 && flow->reads == 1 && window._campaignStage->text() == QStringLiteral("Retomando avaliação")) {
        const auto visible = window._campaignStage->text() + window._campaignCurrent->text()
            + window._campaignIndicatorTitle->text() + window._campaignIndicatorDetail->text()
            + window._campaignProgress->format() + window._campaignIndicatorMetric->text();
        if (!window._campaignStop->isEnabled() || window._campaignStart->isEnabled()) { qApp->exit(222); return; }
        if (visible.contains(QStringLiteral("h confirmadas"), Qt::CaseInsensitive)
            || visible.contains(QStringLiteral("hora"), Qt::CaseInsensitive)
            || visible.contains(QStringLiteral("preparo"), Qt::CaseInsensitive)
            || visible.contains(QStringLiteral("preparação"), Qt::CaseInsensitive)) { qApp->exit(223); return; }
        if (!window._campaignCurrent->text().contains(QStringLiteral("Retomando avaliação"))) { qApp->exit(224); return; }
        flow->terminal = true;
        *phase = 1;
        window.pollCampaign();
      } else if (*phase == 1 && flow->reads == 2 && window._campaignStage->text() == QStringLiteral("Lote não confirmado")) {
        const auto visible = window._campaignStage->text() + window._campaignCurrent->text()
            + window._campaignIndicatorTitle->text() + window._campaignIndicatorDetail->text()
            + window._campaignProgress->format() + window._campaignIndicatorMetric->text();
        if (window._campaignStop->isEnabled() || !window._campaignStart->isEnabled()
            || window._campaignStart->text() != QStringLiteral("Iniciar campanha")) { qApp->exit(225); return; }
        if (!window._campaignCurrent->text().contains(QStringLiteral("não confirmou"))
            || !window._campaignStats->text().contains(QStringLiteral("1 falhas"))) { qApp->exit(226); return; }
        if (visible.contains(QStringLiteral("h confirmadas"), Qt::CaseInsensitive)
            || visible.contains(QStringLiteral("hora"), Qt::CaseInsensitive)
            || visible.contains(QStringLiteral("preparo"), Qt::CaseInsensitive)
            || visible.contains(QStringLiteral("preparação"), Qt::CaseInsensitive)) { qApp->exit(227); return; }
        *phase = 2;
        window.openRecovery();
      } else if (*phase == 2 && flow->recoveryReads > 0) {
        for (auto* dialog : window.findChildren<QDialog*>()) {
          if (dialog->windowTitle() != QStringLiteral("Recuperação de envios")) continue;
          auto* table = dialog->findChild<QTableWidget*>();
          auto* search = dialog->findChild<QLineEdit*>(QStringLiteral("recoverySearch"));
          QPushButton* resume = nullptr;
          for (auto* button : dialog->findChildren<QPushButton*>())
            if (button->text().startsWith(QStringLiteral("Retomar"))) resume = button;
          if (!table || !search || !resume || table->rowCount() != 2 || !resume->isEnabled()) continue;
          search->setText(QStringLiteral("session-selected-002"));
          table->setCurrentCell(1, 0);
          table->selectRow(1);
          if (table->isRowHidden(1) || !table->isRowHidden(0)
              || resume->text() != QStringLiteral("Retomar sessão selecionada")) { qApp->exit(229); return; }
          *phase = 3;
          QTimer::singleShot(100, dialog, [&window] {
            for (auto* message : window.findChildren<QMessageBox*>())
              if (message->windowTitle() == QStringLiteral("Retomar envios existentes")) {
                if (auto* yes = message->button(QMessageBox::Yes)) yes->click();
              }
          });
          resume->click();
          return;
        }
      } else if (*phase == 3 && flow->resumeRequests == 1) {
        poll->stop();
        qApp->exit(flow->wrongResume ? 230 : 0);
      }
    });
    poll->start(20);
    window.pollCampaign();
    QTimer::singleShot(8000, &window, [] { qApp->exit(228); });
  }
  static void acceleratorSmoke(MainWindow& window) {
    auto* server = new QTcpServer(&window);
    if (!server->listen(QHostAddress::LocalHost)) { qApp->exit(90); return; }
    auto started = std::make_shared<bool>(false);
    auto liveSeen = std::make_shared<bool>(false);
    auto preparedRefreshed = std::make_shared<bool>(false);
    auto localRefreshed = std::make_shared<bool>(false);
    QObject::connect(server, &QTcpServer::newConnection, server, [server, started, liveSeen, preparedRefreshed, localRefreshed] {
      auto* socket = server->nextPendingConnection();
      QObject::connect(socket, &QTcpSocket::readyRead, socket, [socket, started, liveSeen, preparedRefreshed, localRefreshed] {
        auto input = socket->property("input").toByteArray() + socket->readAll();
        socket->setProperty("input", input);
        if (!input.contains("\r\n\r\n") || socket->property("answered").toBool()) return;
        socket->setProperty("answered", true);
        QJsonObject payload;
        if (input.startsWith("GET /api/library/ego4d/prepared?")) {
          *preparedRefreshed = input.contains("refresh=");
          payload = {{"schema", 1}, {"provider", "ego4d"}, {"inventory_scope", "local_prepared_media"},
              {"state", "ready"}, {"library_root", "C:/inert-library-fixture"}, {"verified_at", 1791241200},
              {"total", 0}, {"offset", 0}, {"items", QJsonArray{}}, {"protection_status", "verified"},
              {"counts", QJsonObject{{"ready", 0}, {"partial", 0}, {"missing", 0}, {"stale", 0}, {"protected", 0}}}};
        } else if (input.startsWith("GET /api/storage/library/items?")) {
          *localRefreshed = input.contains("refresh=");
          payload = {{"schema", 1}, {"inventory_scope", "local_media_files"}, {"state", "ready"},
              {"library_root", "C:/inert-library-fixture"}, {"file_count", 0}, {"total_bytes", 0},
              {"total", 0}, {"offset", 0}, {"items", QJsonArray{}}};
        } else if (input.startsWith("GET /api/storage/library ")) {
          payload = {{"free_bytes", 53687091200.0}};
        } else if (input.startsWith("POST /api/holo-cache/start")) {
          *started = true;
          payload = {{"ok", true}, {"runner", QJsonObject{{"state", "running"}}}};
        } else if (input.contains("live=1")) {
          const bool selectedHolo = input.contains("provider=holoassist");
          if (selectedHolo) *liveSeen = true;
          payload = {{"live", true}, {"runner", QJsonObject{{"state", selectedHolo ? "done" : "running"}, {"provider", "ego4d"}}}};
        } else {
          payload = {{"tasks", QJsonArray{"Furniture Assembly"}}, {"default_task", "Furniture Assembly"},
            {"cache", QJsonObject{{"task", "Furniture Assembly"}, {"total", 2}, {"ready", *started ? 2 : 0},
              {"pending", *started ? 0 : 2}, {"last_run", QJsonObject{{"status", *started ? "complete" : "stopped"}}}}},
            {"runner", QJsonObject{{"state", *liveSeen ? "done" : *started ? "running" : "idle"}, {"provider", "ego4d"}}}};
        }
        const auto body = QJsonDocument(payload).toJson(QJsonDocument::Compact);
        socket->write("HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: "
          + QByteArray::number(body.size()) + "\r\nConnection: close\r\n\r\n" + body);
        socket->disconnectFromHost();
      });
    });
    window._api.setBaseUrl(QStringLiteral("http://127.0.0.1:%1").arg(server->serverPort()));
    window._pages->setCurrentIndex(4);
    auto phase = std::make_shared<int>(0);
    auto* poll = new QTimer(&window);
    QObject::connect(poll, &QTimer::timeout, &window, [&window, phase, liveSeen, preparedRefreshed, localRefreshed] {
      if (*phase == 0 && window._cacheTask->count() && window._cacheStart->isEnabled()) {
        *phase = 1;
        window._cacheStart->click();
      } else if (*phase == 1 && !window._cacheStartPending && window._cachePoll.isActive() && !window._cacheStart->isEnabled()) {
        *phase = 2;
        window._cacheProvider->setCurrentIndex(1); // Ego warm continues after switching the visible provider.
      } else if (*phase == 2 && *liveSeen && *localRefreshed && window._cacheStart->isEnabled() && !window._cachePoll.isActive()) {
        if (*preparedRefreshed || !window._preparedRefreshPending) { qApp->exit(93); return; }
        *phase = 3; window._libraryTabs->setCurrentIndex(1);
      } else if (*phase == 3 && *preparedRefreshed && window._preparedVerified) {
        qApp->exit(window._cacheProgress->value() == 100 && !QApplication::activeModalWidget() ? 0 : 91);
      }
    });
    poll->start(25);
    window.loadAccelerator();
    QTimer::singleShot(8000, &window, [] { qApp->exit(92); });
  }
  static void credentialCopySmoke(MainWindow& window) {
    const QString previous = QApplication::clipboard()->text();
    QObject::connect(qApp, &QCoreApplication::aboutToQuit, qApp, [previous] {
      QApplication::clipboard()->setText(previous);
    });
    auto* server = new QTcpServer(&window);
    if (!server->listen(QHostAddress::LocalHost)) { qApp->exit(80); return; }
    QObject::connect(server, &QTcpServer::newConnection, server, [server] {
      auto* socket = server->nextPendingConnection();
      QObject::connect(socket, &QTcpSocket::readyRead, socket, [socket] {
        auto input = socket->property("input").toByteArray() + socket->readAll();
        socket->setProperty("input", input);
        if (!input.contains("fixture@example.com") || socket->property("answered").toBool()) return;
        socket->setProperty("answered", true);
        if (!input.startsWith("POST /api/accounts/password ")) { qApp->exit(81); return; }
        const QByteArray body = R"({"email":"fixture@example.com","password":"fixture-only-password"})";
        socket->write("HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: "
          + QByteArray::number(body.size()) + "\r\nConnection: close\r\n\r\n" + body);
        socket->disconnectFromHost();
      });
    });
    window._api.setBaseUrl(QStringLiteral("http://127.0.0.1:%1").arg(server->serverPort()));
    auto* panel = window.credentialCopyActions("fixture@example.com", true);
    panel->setParent(&window);
    auto* copyEmail = panel->findChild<QPushButton*>(QStringLiteral("copyEmailButton"));
    auto* copyPassword = panel->findChild<QPushButton*>(QStringLiteral("copyPasswordButton"));
    if (!copyEmail || !copyPassword) { qApp->exit(82); return; }
    copyEmail->click();
    if (QApplication::clipboard()->text() != "fixture@example.com") { qApp->exit(83); return; }
    copyPassword->click();
    if (copyPassword->isEnabled()) { qApp->exit(84); return; }
    auto* poll = new QTimer(&window);
    QObject::connect(poll, &QTimer::timeout, &window, [copyPassword] {
      if (!copyPassword->isEnabled()) return;
      qApp->exit(QApplication::clipboard()->text() == "fixture-only-password"
          && copyPassword->text() == QStringLiteral("Copiado!")
          && !QApplication::activeModalWidget() ? 0 : 85);
    });
    poll->start(25);
    QTimer::singleShot(5000, &window, [] { qApp->exit(86); });
  }
  static void credentialViewSmoke(MainWindow& window) {
    // Only literal fixture data and an isolated loopback server. No clipboard calls.
    const QString fixtureEmail = QStringLiteral("fixture@example.invalid");
    const QString activePassword = QStringLiteral("  fixture active password  ");
    const QString bannedPassword = QStringLiteral(" banned fixture password ");
    struct Flow { int requests=0, phase=1, cleared=0; QPointer<QDialog> dialog; };
    auto flow = std::make_shared<Flow>();
    auto* server = new QTcpServer(&window);
    if (!server->listen(QHostAddress::LocalHost)) { qApp->exit(140); return; }
    QObject::connect(server, &QTcpServer::newConnection, server,
                     [server, flow, fixtureEmail, activePassword, bannedPassword] {
      auto* socket = server->nextPendingConnection();
      QObject::connect(socket, &QTcpSocket::readyRead, socket,
                       [socket, flow, fixtureEmail, activePassword, bannedPassword] {
        auto input = socket->property("input").toByteArray() + socket->readAll();
        socket->setProperty("input", input);
        const int headerEnd = input.indexOf("\r\n\r\n");
        if (headerEnd < 0 || socket->property("answered").toBool()) return;
        const auto length = QRegularExpression(QStringLiteral("Content-Length: (\\d+)"),
            QRegularExpression::CaseInsensitiveOption).match(QString::fromLatin1(input.left(headerEnd)));
        if (!length.hasMatch()) { qApp->exit(141); return; }
        if (input.size() < headerEnd + 4 + length.captured(1).toInt()) return;
        socket->setProperty("answered", true);
        const auto request = QJsonDocument::fromJson(input.mid(headerEnd + 4)).object();
        const int number = ++flow->requests;
        const auto route = number == 2 ? QByteArray("POST /api/accounts/banned/password ")
                                      : QByteArray("POST /api/accounts/password ");
        if (!input.startsWith(route) || request.size()!=1 || request.value("email").toString()!=fixtureEmail
            || number > 5) { qApp->exit(142); return; }
        int status=200;
        QJsonObject response{{"email",fixtureEmail},{"password",number==2 ? bannedPassword : activePassword}};
        if (number == 3) { status=409; response["error"]=activePassword; }
        if (number == 4) response["email"]=QStringLiteral("other@example.invalid");
        if (number == 5) response["password"]=QString();
        const auto bytes=QJsonDocument(response).toJson(QJsonDocument::Compact);
        socket->write("HTTP/1.1 " + QByteArray::number(status) + " Fixture\r\nContent-Type: application/json\r\n"
                      "Connection: close\r\nContent-Length: " + QByteArray::number(bytes.size()) + "\r\n\r\n" + bytes);
        socket->disconnectFromHost();
      });
      QObject::connect(socket, &QTcpSocket::disconnected, socket, &QObject::deleteLater);
    });
    window._api.setBaseUrl(QStringLiteral("http://127.0.0.1:%1").arg(server->serverPort()));
    auto* activePanel=window.credentialCopyActions(fixtureEmail,true,false);
    auto* bannedPanel=window.credentialCopyActions(fixtureEmail,true,true);
    auto* missingPanel=window.credentialCopyActions(fixtureEmail,false,false);
    for (auto* panel : {activePanel,bannedPanel,missingPanel}) panel->setParent(&window);
    auto* active=activePanel->findChild<QPushButton*>(QStringLiteral("viewCredentialsButton"));
    auto* banned=bannedPanel->findChild<QPushButton*>(QStringLiteral("viewCredentialsButton"));
    auto* missing=missingPanel->findChild<QPushButton*>(QStringLiteral("viewCredentialsButton"));
    auto* missingCopy=missingPanel->findChild<QPushButton*>(QStringLiteral("copyPasswordButton"));
    if (!active || !banned || !missing || !missingCopy || !active->isEnabled() || !banned->isEnabled()
        || missing->isEnabled() || missingCopy->isEnabled()
        || active->text()!=QStringLiteral("Ver acesso")) { qApp->exit(143); return; }
    missing->click();
    auto* poll=new QTimer(&window);
    QObject::connect(poll,&QTimer::timeout,&window,
                     [&window,flow,active,banned,poll,fixtureEmail,activePassword,bannedPassword] {
      const auto privateStatus=window._status->text()+window._status->toolTip();
      if (privateStatus.contains(fixtureEmail,Qt::CaseInsensitive)
          || privateStatus.contains(activePassword.trimmed()) || privateStatus.contains(bannedPassword.trimmed())) {
        qApp->exit(144); return;
      }
      auto* dialog=window.findChild<QDialog*>(QStringLiteral("savedAccountCredentialsDialog"));
      if (flow->phase==1 || flow->phase==3) {
        if (!dialog || !dialog->isVisible()) return;
        const int expectedRequest=flow->phase==1 ? 1 : 2;
        const QString expectedPassword=flow->phase==1 ? activePassword : bannedPassword;
        auto* email=dialog->findChild<QLineEdit*>(QStringLiteral("savedAccountEmail"));
        auto* password=dialog->findChild<QLineEdit*>(QStringLiteral("savedAccountPassword"));
        auto* hide=dialog->findChild<QCheckBox*>(QStringLiteral("hideSavedAccountPassword"));
        auto* buttons=dialog->findChild<QDialogButtonBox*>();
        if (flow->requests!=expectedRequest || !dialog->isModal() || !dialog->testAttribute(Qt::WA_DeleteOnClose)
            || !email || !password || !hide || !buttons || !buttons->button(QDialogButtonBox::Close)
            || !email->isReadOnly() || !password->isReadOnly() || email->text()!=fixtureEmail
            || password->text()!=expectedPassword || password->echoMode()!=QLineEdit::Normal || hide->isChecked()) {
          qApp->exit(145); return;
        }
        hide->setChecked(true);
        if (password->echoMode()!=QLineEdit::Password || password->text()!=expectedPassword) { qApp->exit(146); return; }
        hide->setChecked(false);
        if (password->echoMode()!=QLineEdit::Normal || password->text()!=expectedPassword) { qApp->exit(147); return; }
        flow->dialog=dialog;
        QObject::connect(dialog,&QDialog::finished,dialog,[flow,email,password] {
          if (!email->text().isEmpty() || !password->text().isEmpty()) { qApp->exit(148); return; }
          ++flow->cleared;
        });
        ++flow->phase;
        buttons->button(QDialogButtonBox::Close)->click();
      } else if (flow->phase==2 || flow->phase==4) {
        if (flow->dialog || dialog) return;
        if (!active->isEnabled() || !banned->isEnabled()) { qApp->exit(149); return; }
        if (flow->phase==2) { ++flow->phase; banned->click(); }
        else { ++flow->phase; active->click(); }
      } else {
        const int expectedRequest=flow->phase-2;
        if (flow->requests!=expectedRequest || !active->isEnabled()) return;
        if (dialog || QApplication::activeModalWidget() || active->text()!=QStringLiteral("Ver acesso")
            || window._status->text().isEmpty()) { qApp->exit(150); return; }
        if (flow->phase<7) { ++flow->phase; active->click(); }
        else { poll->stop(); qApp->exit(flow->cleared==2 && flow->requests==5 ? 0 : 151); }
      }
    });
    poll->start(25);
    active->click();
    if (active->isEnabled()) { qApp->exit(152); return; }
    QTimer::singleShot(8000,&window,[] { qApp->exit(153); });
  }
  static void settingsSmoke(MainWindow& window) {
    const auto click = [](QWidget* widget, QPoint point) {
      QMouseEvent move(QEvent::MouseMove, point, widget->mapToGlobal(point), Qt::NoButton, Qt::NoButton, Qt::NoModifier);
      QApplication::sendEvent(widget, &move);
      QMouseEvent press(QEvent::MouseButtonPress, point, widget->mapToGlobal(point), Qt::LeftButton, Qt::LeftButton, Qt::NoModifier);
      QApplication::sendEvent(widget, &press);
      QMouseEvent release(QEvent::MouseButtonRelease, point, widget->mapToGlobal(point), Qt::LeftButton, Qt::NoButton, Qt::NoModifier);
      QApplication::sendEvent(widget, &release);
      QEventLoop settle;
      QTimer::singleShot(200, &settle, &QEventLoop::quit);
      settle.exec();
    };
    for (int pass = 0; pass < 2; ++pass) {
      const QList<int> pages{1, 2, 4, 8};
      for (int i = 0; i < pages.size(); ++i) {
        click(window._navigation->viewport(), window._navigation->visualItemRect(window._navigation->item(9)).center());
        if (!window._settingsMenu->isVisible()) { qWarning("settings menu did not open"); qApp->exit(90); return; }
        auto* action = window._settingsMenu->actions().at(i);
        if (!action->isEnabled()) { qApp->exit(92); return; }
        click(window._settingsMenu, window._settingsMenu->actionGeometry(action).center());
        if (window._pages->currentIndex() != pages[i]) { qWarning("settings action failed: %d, page=%d", i, window._pages->currentIndex()); qApp->exit(91); return; }
      }
      const bool darkBefore = QSettings().value("darkTheme", false).toBool();
      click(window._navigation->viewport(), window._navigation->visualItemRect(window._navigation->item(9)).center());
      click(window._settingsMenu, window._settingsMenu->actionGeometry(window._settingsMenu->actions().at(6)).center());
      if (QSettings().value("darkTheme", false).toBool() == darkBefore) { qApp->exit(93); return; }
    }
    window._api.setBaseUrl(QStringLiteral("http://127.0.0.1:1")); // No live customer service.
    click(window._navigation->viewport(), window._navigation->visualItemRect(window._navigation->item(9)).center());
    click(window._settingsMenu, window._settingsMenu->actionGeometry(window._settingsMenu->actions().at(5)).center());
    QDialog* recovery = nullptr;
    for (auto* dialog : window.findChildren<QDialog*>())
      if (dialog->isVisible() && dialog->windowTitle() == QStringLiteral("Recuperação de envios")) recovery = dialog;
    if (!recovery) { qApp->exit(94); return; }
    recovery->close();
    // Verify menu dispatch without contacting GitHub or installing an update.
    QObject::disconnect(window._updateButton, &QPushButton::clicked, &window, nullptr);
    int updateClicks = 0;
    QObject::connect(window._updateButton, &QPushButton::clicked, &window, [&updateClicks] { ++updateClicks; });
    click(window._navigation->viewport(), window._navigation->visualItemRect(window._navigation->item(9)).center());
    click(window._settingsMenu, window._settingsMenu->actionGeometry(window._settingsMenu->actions().at(7)).center());
    qApp->exit(updateClicks == 1 ? 0 : 95);
  }
  static void mailCleanupSmoke(MainWindow& window) {
    auto* server = new QTcpServer(&window);
    if (!server->listen(QHostAddress::LocalHost)) { qApp->exit(80); return; }
    QObject::connect(server, &QTcpServer::newConnection, server, [server] {
      auto* socket = server->nextPendingConnection();
      QObject::connect(socket, &QTcpSocket::readyRead, socket, [socket] {
        auto input = socket->property("input").toByteArray() + socket->readAll();
        socket->setProperty("input", input);
        if (!input.contains("\r\n\r\n") || socket->property("answered").toBool()) return;
        socket->setProperty("answered", true);
        if (!input.startsWith("GET /api/mail-cleanup ")) { qApp->exit(81); return; }
        const auto body = QJsonDocument(QJsonObject{{"state", "ready"}, {"id", "fixture"},
          {"mailbox", "Caixa de demonstração"},
          {"profiles", QJsonArray{QJsonObject{{"id", "demo"}, {"name", "Caixa de demonstração"}}}},
          {"items", QJsonArray{
            QJsonObject{{"uid", 1}, {"subject", "Your $38.64 Crowtado payout is ready"}, {"action", "payment"}, {"reason", "Assunto de saque ou pagamento"}},
            QJsonObject{{"uid", 2}, {"subject", "Código de verificação antigo"}, {"action", "move"}, {"reason", "Sem indicação de saque ou pagamento"}},
            QJsonObject{{"uid", 3}, {"subject", "Mensagem com anexo"}, {"action", "review"}, {"reason", "Conteúdo não analisável integralmente"}}}}}).toJson(QJsonDocument::Compact);
        socket->write("HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: "
            + QByteArray::number(body.size()) + "\r\nConnection: close\r\n\r\n" + body);
        socket->disconnectFromHost();
      });
    });
    window._api.setBaseUrl(QStringLiteral("http://127.0.0.1:%1").arg(server->serverPort()));
    window.openMailCleanup();
    QTimer::singleShot(500, &window, [&window] {
      auto* table = window.findChild<QTableWidget*>(QStringLiteral("mailCleanupReview"));
      if (!table || table->rowCount() != 3) { qApp->exit(82); return; }
      if ((table->item(0, 0)->flags() & Qt::ItemIsUserCheckable)
          || (table->item(2, 0)->flags() & Qt::ItemIsUserCheckable)
          || table->item(1, 0)->checkState() != Qt::Checked) { qApp->exit(83); return; }
      table->item(1, 0)->setCheckState(Qt::Unchecked);
      if (table->item(1, 0)->checkState() != Qt::Unchecked) { qApp->exit(84); return; }
      const QString output = qEnvironmentVariable("QMONEY_MAIL_PREVIEW_IMAGE");
      if (!output.isEmpty()) {
        auto* dialog = qobject_cast<QDialog*>(table->window());
        if (!dialog || !dialog->grab().save(output)) { qApp->exit(85); return; }
      }
      qApp->exit(0);
    });
  }
  static void liveCatalogSmoke(MainWindow& window) {
    const auto email = qEnvironmentVariable("QMONEY_TEST_ACCOUNT");
    const auto base = qEnvironmentVariable("QMONEY_TEST_API");
    if (email.isEmpty() || base.isEmpty()) { qApp->exit(70); return; }
    window._api.setBaseUrl(base);
    window._pages->setCurrentIndex(3);
    {
      const QSignalBlocker blocker(window._campaignAccounts);
      window._campaignAccounts->clear();
      auto* account = new QListWidgetItem(QStringLiteral("Conta de validação"), window._campaignAccounts);
      account->setData(Qt::UserRole, email);
      account->setCheckState(Qt::Checked);
    }
    window._dataset->setCurrentIndex(window._dataset->findData(QStringLiteral("ego4d")));
    window._minDuration->setValue(5);
    window._maxDuration->setValue(30);
    auto* poll = new QTimer(&window);
    QObject::connect(poll, &QTimer::timeout, &window, [&window] {
      if (window._taskRequestPending || window._taskReload.isActive()) return;
      if (window._taskRecords.isEmpty()) { qApp->exit(71); return; }
      int available = 0;
      for (const auto value : window._taskRecords)
        if (value.toObject().value("available_for_duration").toBool()) ++available;
      if (!available || !window._campaignStart->isEnabled()) { qApp->exit(72); return; }
      if (QApplication::activeModalWidget()) { qApp->exit(73); return; }
      qApp->exit(0);
    });
    poll->start(250);
    window.loadTasks();
    QTimer::singleShot(900000, &window, [] { qApp->exit(74); });
  }
  static void catalogLoadingSmoke(MainWindow& window) {
    auto* server = new QTcpServer(&window);
    if (!server->listen(QHostAddress::LocalHost)) { qApp->exit(60); return; }
    auto requests = std::make_shared<int>(0);
    QObject::connect(server, &QTcpServer::newConnection, server, [server, requests] {
      auto* socket = server->nextPendingConnection();
      QObject::connect(socket, &QTcpSocket::readyRead, socket, [socket, requests] {
        auto input = socket->property("input").toByteArray() + socket->readAll();
        socket->setProperty("input", input);
        if (!input.contains("\r\n\r\n") || socket->property("answered").toBool()) return;
        socket->setProperty("answered", true);
        if (!input.contains("async=1")) { qApp->exit(61); return; }
        const int number = ++*requests;
        const bool changed = input.contains("min_dur_s=720");
        if (input.contains("min_dur_s=660")) { qApp->exit(66); return; }
        const bool loading = number < 3;
        const auto body = QJsonDocument(changed ? QJsonObject{{"error", "fixture category failure"}}
            : loading ? QJsonObject{{"loading", true}, {"state", "running"}, {"message", "Indexando catálogo realista"}, {"elapsed_s", 90}}
            : QJsonObject{{"tasks", QJsonArray{QJsonObject{{"id", "garden"}, {"name", "Gardening"}, {"clip_count", 3}, {"available_for_duration", true}}}}}).toJson(QJsonDocument::Compact);
        QTimer::singleShot(700, socket, [socket, body, loading, changed] {
          socket->write("HTTP/1.1 " + QByteArray(changed ? "400 Error" : loading ? "202 Accepted" : "200 OK")
              + "\r\nContent-Type: application/json\r\nContent-Length: " + QByteArray::number(body.size())
              + "\r\nConnection: close\r\n\r\n" + body);
          socket->disconnectFromHost();
        });
      });
    });
    window._api.setBaseUrl(QStringLiteral("http://127.0.0.1:%1").arg(server->serverPort()));
    window._pages->setCurrentIndex(3);
    {
      const QSignalBlocker block(window._campaignAccounts);
      window._campaignAccounts->clear();
      auto* item = new QListWidgetItem("fixture@example.com", window._campaignAccounts);
      item->setData(Qt::UserRole, "fixture@example.com");
      item->setCheckState(Qt::Checked);
    }
    window._minDuration->setValue(5);
    auto phase = std::make_shared<int>(0);
    auto sawLoading = std::make_shared<bool>(false);
    auto* check = new QTimer(&window);
    QObject::connect(check, &QTimer::timeout, &window, [&window, requests, phase, sawLoading] {
      const auto* item = window._campaignTasks->item(0);
      if (item && item->text().contains(QStringLiteral("90 s"))) {
        *sawLoading = true;
        if (window._campaignStart->isEnabled()) { qApp->exit(62); return; }
      }
      if (*phase == 0 && item && item->data(Qt::UserRole).toString() == "garden") {
        if (!*sawLoading || !window._campaignStart->isEnabled() || *requests > 4) { qApp->exit(63); return; }
        *phase = 1;
        window._minDuration->setValue(10);
        window.loadTasks();
        QTimer::singleShot(50, &window, [&window] {
          window._minDuration->setValue(11);
          window.loadTasks();
          window._minDuration->setValue(12);
          window.loadTasks();
        });
      } else if (*phase == 1 && item && item->text().contains("fixture category failure")) {
        qApp->exit(!window._campaignStart->isEnabled() && !QApplication::activeModalWidget() ? 0 : 64);
      }
    });
    check->start(25);
    for (int i = 0; i < 20; ++i) window.loadTasks();
    if (window._taskReload.isActive()) { qApp->exit(67); return; }
    QTimer::singleShot(10000, &window, [] { qApp->exit(65); });
  }
  static QJsonObject walletFixture() {
    const auto stamp = QDateTime::currentDateTimeUtc().toString(Qt::ISODate);
    QJsonArray accounts, connected;
    QJsonObject balances, rows, kinds;
    const QStringList names{QStringLiteral("regular@example.com"), QStringLiteral("restrita@example.com"),
        QStringLiteral("vencida@example.com"), QStringLiteral("falha@example.com"), QStringLiteral("sem-acesso@example.com")};
    for (int i = 0; i < names.size(); ++i) {
      const auto email = names[i];
      accounts.append(email); kinds.insert(email, "crowtado");
      if (i < 4) connected.append(email);
      QJsonObject record{{"availableCents", i == 0 ? 4500 : i == 1 ? 5100 : 990000},
          {"pendingCents", i == 0 ? 2300 : 2000}, {"inTransitCents",0}, {"lifetimeCents", 1200000},
          {"updated_at", i == 2 ? "2020-01-01T12:00:00+00:00" : stamp}, {"checked_at", stamp}};
      if (i == 1) record.insert("holdReason", QStringLiteral("Conta desativada — fale com o suporte."));
      if (i == 3) {record.insert("error", QStringLiteral("Não foi possível alcançar a Crowtado."));record.insert("stale",true);}
      if (i < 4) balances.insert(email, record);
      const bool confirmed = i < 2;
      const auto reason = i == 1 ? QStringLiteral("Conta desativada — fale com o suporte.")
          : i == 2 ? QStringLiteral("A leitura venceu. Consulte novamente.")
          : i == 3 ? QStringLiteral("Falha de conexão. O último valor foi preservado.")
          : i == 4 ? QStringLiteral("Conecte o acesso Crowtado.") : QStringLiteral("Leitura completa nas últimas 24 horas.");
      rows.insert(email, QJsonObject{{"kind","crowtado"},{"connected",i < 4},
          {"reading",QJsonObject{{"confirmed",confirmed},{"code",confirmed?"confirmed":i==2?"expired":i==3?"error":"unqueried"},
              {"label",confirmed?QStringLiteral("Saldo confirmado"):i==2?QStringLiteral("Leitura vencida"):i==3?QStringLiteral("Consulta inconclusiva"):QStringLiteral("Ainda não consultado")},{"reason",reason}}},
          {"payout",QJsonObject{{"eligible",i==0},{"code",i==0?"ready":i==1?"hold":"refresh"},
              {"label",i==0?QStringLiteral("Disponível para saque"):i==1?QStringLiteral("Saque retido"):QStringLiteral("Atualizar saldo")},{"reason",reason}}},
          {"restriction",QJsonObject{{"code",i==0?"clear":i==1?"disabled":"unknown"},
              {"label",i==0?QStringLiteral("Sem retenção informada"):i==1?QStringLiteral("Conta desativada · saque retido"):QStringLiteral("Restrição não verificada")},{"reason",reason}}}});
    }
    return {{"accounts",accounts},{"with_password",connected},{"with_saved_password",QJsonArray{}},
        {"balances",balances},{"account_kinds",kinds},{"refresh_needed",QJsonArray{names[2],names[3]}},
        {"runner",QJsonObject{{"state","done"},{"total",4},{"done",4},{"failed",1}}},
        {"withdraw_bulk",QJsonObject{{"state","idle"}}},{"payout_method_bulk",QJsonObject{{"state","idle"}}},
        {"wallet",QJsonObject{{"accounts",rows},{"withdrawable_total_cents",4500},{"counts",QJsonObject{{"confirmed",2},{"crowtado",5},{"attention",3},{"eligible",1}}},
            {"totals",QJsonObject{{"availableCents",9600},{"pendingCents",4300},{"inTransitCents",0},{"onHoldCents",0}}},
            {"field_coverage",QJsonObject{{"onHoldCents",0}}}}},
        {"exchange",QJsonObject{{"available",true},{"rate",5.1},{"quote_date","2026-10-02"}}}};
  }

  static void walletContract(MainWindow& window, bool preview) {
    window.applyStructuralStyle(qApp->arguments().contains("--dark"));
    auto* server = new QTcpServer(&window);
    if (!server->listen(QHostAddress::LocalHost)) { qApp->exit(200); return; }
    auto phase = std::make_shared<int>(0);
    QObject::connect(server, &QTcpServer::newConnection, server, [server,phase] {
      auto* socket=server->nextPendingConnection();
      QObject::connect(socket,&QTcpSocket::readyRead,socket,[socket,phase] {
        const auto input=socket->property("input").toByteArray()+socket->readAll();
        socket->setProperty("input",input);
        if (!input.contains("\r\n\r\n") || socket->property("answered").toBool()) return;
        socket->setProperty("answered",true);
        if (!input.startsWith("GET /api/balances ")) {qApp->exit(201);return;}
        const auto body=QJsonDocument(*phase==1 ? QJsonObject{} : walletFixture()).toJson(QJsonDocument::Compact);
        socket->write("HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\nContent-Length: "+QByteArray::number(body.size())+"\r\n\r\n"+body);
        socket->disconnectFromHost();
      });
      QObject::connect(socket,&QTcpSocket::disconnected,socket,&QObject::deleteLater);
    });
    window._api.setBaseUrl(QStringLiteral("http://127.0.0.1:%1").arg(server->serverPort()));
    page(window,6);
    window.loadBalances();
    QTimer::singleShot(350,&window,[&window,phase,preview] {
      if (preview) {qApp->exit(window.grab().save(qApp->arguments().at(1)) ? 0 : 202);return;}
      int enabled=0;bool restricted=false,historical=false;
      for(int row=0;row<window._balancesTable->rowCount();++row) {
        const auto email=window._balancesTable->item(row,0)->text();
        if(email=="restrita@example.com") restricted=window._balancesTable->item(row,3)->text().contains(QStringLiteral("Conta desativada"));
        if(email=="vencida@example.com") historical=window._balancesTable->item(row,2)->text().endsWith(" *");
      }
      for(auto* button:window._balancesTable->findChildren<QPushButton*>())
        if(button->text()==QStringLiteral("Sacar") && button->isEnabled()) ++enabled;
      if(window._balancesTable->rowCount()!=5 || enabled!=1 || !restricted || !historical
          || !window._balancesApprovedUsd->text().contains("45") || window._walletMonitorTimer.isActive()) {qApp->exit(203);return;}
      window._balancesOnlyAvailable->setChecked(true);
      int visible=0;for(int row=0;row<5;++row) visible+=!window._balancesTable->isRowHidden(row);
      if(visible!=2) {qApp->exit(204);return;}
      window._balancesOnlyAvailable->setChecked(false);
      *phase=1;window.loadBalances();
      QTimer::singleShot(250,&window,[&window,phase] {
        if(window._balancesTable->rowCount()!=5 || window._balancesWithdrawAll->isEnabled()
            || !window._balancesApprovedUsd->text().contains(QStringLiteral("—"))) {qApp->exit(205);return;}
        for(auto* button:window._balancesTable->findChildren<QPushButton*>())
          if(button->property("walletAction").toBool() && button->isEnabled()) {qApp->exit(206);return;}
        *phase=2;window.loadBalances();
        QTimer::singleShot(250,&window,[&window] {
          if(!window._balancesWithdrawAll->isEnabled() || !window._walletSnapshotHealthy) {qApp->exit(207);return;}
          window._backendReady=true;
          window._walletMonitoring->setChecked(true);
          if(!window._walletMonitorTimer.isActive() || window._walletMonitorTimer.interval()!=900000) {qApp->exit(209);return;}
          window._balancesSnapshot.insert("runner",QJsonObject{{"state","running"}});
          QMetaObject::invokeMethod(&window._walletMonitorTimer,"timeout",Qt::DirectConnection);
          if(!window._walletMonitorState->text().contains(QStringLiteral("adiada"))) {qApp->exit(210);return;}
          window._walletMonitoring->setChecked(false);
          window._balancesSnapshot.insert("runner",QJsonObject{{"state","done"}});
          window.loadBalances();
          window.disableWalletActions(); // A response started before a command must not re-enable it.
          QTimer::singleShot(250,&window,[&window] {
            bool disabled=!window._balancesWithdrawAll->isEnabled();
            for(auto* button:window._balancesTable->findChildren<QPushButton*>())
              if(button->property("walletAction").toBool() && button->isEnabled()) disabled=false;
            qApp->exit(disabled && !window._walletMonitorTimer.isActive() ? 0 : 211);
          });
        });
      });
    });
    QTimer::singleShot(8000,&window,[]{qApp->exit(208);});
  }

  static void walletCooldownSmoke(MainWindow& window) {
    auto* integrations = window._pages->widget(2)->findChild<QTabWidget*>();
    if (!integrations || !window._hostingerToken) { qApp->exit(212); return; }
    page(window, 2);
    integrations->setCurrentIndex(1);
    QApplication::processEvents();
    if (!window._hostingerToken->isVisible()
        || window._hostingerToken->echoMode() != QLineEdit::Password) {
      qApp->exit(213); return;
    }

    auto* server = new QTcpServer(&window);
    if (!server->listen(QHostAddress::LocalHost)) { qApp->exit(214); return; }
    QObject::connect(server, &QTcpServer::newConnection, server, [server] {
      auto* socket = server->nextPendingConnection();
      QObject::connect(socket, &QTcpSocket::readyRead, socket, [socket] {
        const auto input = socket->property("input").toByteArray() + socket->readAll();
        socket->setProperty("input", input);
        if (!input.contains("\r\n\r\n") || socket->property("answered").toBool()) return;
        socket->setProperty("answered", true);
        if (!input.startsWith("GET /api/balances ")) { qApp->exit(215); return; }
        auto payload = walletFixture();
        payload.insert("runner", QJsonObject{
            {"state", "running"}, {"phase", "cooldown"}, {"current", "Crowtado limitou as consultas. Aguardando 17s para tentar novamente automaticamente. Parar cancela a espera."},
            {"total", 4}, {"done", 0}, {"failed", 0}});
        const auto body = QJsonDocument(payload).toJson(QJsonDocument::Compact);
        socket->write("HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\nContent-Length: "
            + QByteArray::number(body.size()) + "\r\n\r\n" + body);
        socket->disconnectFromHost();
      });
      QObject::connect(socket, &QTcpSocket::disconnected, socket, &QObject::deleteLater);
    });
    window._api.setBaseUrl(QStringLiteral("http://127.0.0.1:%1").arg(server->serverPort()));
    page(window, 6);
    window.loadBalances();
    QTimer::singleShot(350, &window, [&window] {
      if (!window._balancesState->text().contains(QStringLiteral("Crowtado limitou as consultas"))) {
        qApp->exit(216); return;
      }
      if (!window._balancesStop->isVisible() || !window._balancesStop->isEnabled()) {
        qApp->exit(217); return;
      }
      bool financialActionEnabled = window._balancesWithdrawAll->isEnabled()
          || window._balancesPayoutMethod->isEnabled();
      for (auto* button : window._balancesTable->findChildren<QPushButton*>())
        if (button->property("walletAction").toBool() && button->isEnabled())
          financialActionEnabled = true;
      qApp->exit(financialActionEnabled ? 218 : 0);
    });
    QTimer::singleShot(8000, &window, [] { qApp->exit(219); });
  }

  static void balancePollingSmoke(MainWindow& window) {
    auto* server = new QTcpServer(&window);
    if (!server->listen(QHostAddress::LocalHost)) { qApp->exit(50); return; }
    auto requests = std::make_shared<int>(0);
    QObject::connect(server, &QTcpServer::newConnection, server, [server, requests] {
      auto* socket = server->nextPendingConnection();
      QObject::connect(socket, &QTcpSocket::readyRead, socket, [socket, requests] {
        auto input = socket->property("input").toByteArray() + socket->readAll();
        socket->setProperty("input", input);
        if (!input.contains("\r\n\r\n") || socket->property("answered").toBool()) return;
        socket->setProperty("answered", true);
        const int number = ++*requests;
        QTimer::singleShot(250, socket, [socket, number] {
          QByteArray body = number == 1 ? QByteArray("{\"error\":\"fixture offline\"}")
              : QByteArray("{\"accounts\":[\"fixture@example.com\"],\"with_password\":[\"fixture@example.com\"],\"balances\":{\"fixture@example.com\":{\"availableCents\":3685,\"inTransitCents\":2704,\"inTransitReplaceable\":false}},\"runner\":{\"state\":\"done\",\"failed\":1,\"total\":2}}");
          if (number != 1) {
            auto data = QJsonDocument::fromJson(body).object();
            data.insert("last_withdrawal", QJsonObject{{"email","fixture@example.com"},
                {"accepted",true},{"finished_at","2026-09-29T15:00:00+00:00"},
                {"message",QStringLiteral("Saque Wise aceito pela Crowtado. Limpeza pendente.")}});
            body = QJsonDocument(data).toJson(QJsonDocument::Compact);
          }
          socket->write("HTTP/1.1 " + QByteArray(number == 1 ? "503 Unavailable" : "200 OK")
              + "\r\nContent-Type: application/json\r\nContent-Length: " + QByteArray::number(body.size())
              + "\r\nConnection: close\r\n\r\n" + body);
          socket->disconnectFromHost();
        });
      });
    });
    window._api.setBaseUrl(QStringLiteral("http://127.0.0.1:%1").arg(server->serverPort()));
    window._pages->setCurrentIndex(6);
    for (int i = 0; i < 20; ++i) window.loadBalances();
    QTimer::singleShot(450, &window, [&window, requests] {
      if (*requests != 1 || window._balancePolling || QApplication::activeModalWidget()
          || !window._balancesState->text().contains("fixture offline")
          || window._balancesWithdrawAll->isEnabled()) { qApp->exit(51); return; }
      for (int i = 0; i < 20; ++i) window.loadBalances();
      QTimer::singleShot(450, &window, [&window, requests] {
        const bool recovered = *requests == 2 && !window._balancePolling
            && window._balancesState->text().contains(QStringLiteral("1 de 2"))
            && window._balancePoll.isActive() && !QApplication::activeModalWidget()
            && !window._balancesWithdrawAll->isEnabled();
        bool foundWithdrawal = false;
        for (auto* button : window._balancesTable->findChildren<QPushButton*>()) {
          if (button->text() == QStringLiteral("Sacar")) foundWithdrawal = true;
          if (button->text() == QStringLiteral("Sacar")
              && (button->isEnabled() || !button->toolTip().contains(QStringLiteral("em trânsito")))) {
            qApp->exit(54); return;
          }
        }
        const bool receiptVisible = window._balancesWithdrawReceipt->isVisible()
            && window._balancesWithdrawReceipt->text().contains(QStringLiteral("aceito pela Crowtado"));
        qApp->exit(recovered && foundWithdrawal && receiptVisible ? 0 : 52);
      });
    });
    QTimer::singleShot(8000, &window, [] { qApp->exit(53); });
  }
  static void campaignIndicatorSmoke(MainWindow& window) {
    {
      const QSignalBlocker a(window._campaignAccounts),t(window._campaignTasks);
      auto* account=new QListWidgetItem("fixture@example.com",window._campaignAccounts);
      account->setData(Qt::UserRole,"fixture@example.com"); account->setCheckState(Qt::Checked);
      auto* task=new QListWidgetItem("Fixture",window._campaignTasks);
      task->setData(Qt::UserRole,"fixture-task"); task->setCheckState(Qt::Checked);
    }
    window._taskReload.stop();

    page(window,3);
    window.applyStructuralStyle(qApp->arguments().contains("--dark"));
    auto* server=new QTcpServer(&window);
    if(!server->listen(QHostAddress::LocalHost)){qApp->exit(40);return;}
    auto phase=std::make_shared<int>(0);
    auto oldPreviewReturned=std::make_shared<bool>(false);
    QObject::connect(server,&QTcpServer::newConnection,server,[server,phase,oldPreviewReturned]{
      auto* socket=server->nextPendingConnection();
      QObject::connect(socket,&QTcpSocket::readyRead,socket,[socket,phase,oldPreviewReturned]{
        auto input=socket->property("input").toByteArray()+socket->readAll();
        socket->setProperty("input",input);
        if(!input.contains("\r\n\r\n") || socket->property("answered").toBool())return;
        socket->setProperty("answered",true);
        if(input.startsWith("POST /api/logs/old-campaign.json/status ")) {
          QTimer::singleShot(150,socket,[socket,oldPreviewReturned]{
            const QByteArray body=R"({"summary":{"total":100,"ready":100}})";
            socket->write("HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\nContent-Length: "+QByteArray::number(body.size())+"\r\n\r\n"+body);
            socket->disconnectFromHost();
            QTimer::singleShot(100,qApp,[oldPreviewReturned]{*oldPreviewReturned=true;});
          });
          return;
        }
        QJsonObject reply; bool failed=false;
        if(input.startsWith("POST /api/campaigns ")) {
          const int starts=qApp->property("campaignStartRequests").toInt()+1;
          qApp->setProperty("campaignStartRequests",starts);
          if(starts!=1){qApp->exit(49);return;}
          failed=qApp->arguments().contains("--start-uncertain") || qApp->arguments().contains("--start-terminal");
          reply=failed ? QJsonObject{{"error_code","request_outcome_unknown"},{"error","Resposta perdida"}}
                       : qApp->arguments().contains("--start-malformed") ? QJsonObject{}
                       : QJsonObject{{"ok",true},{"accounts",QJsonArray{}}};
        }
        else if(input.startsWith("GET /api/campaigns/current?")) {
          failed=*phase==2;
          reply={{"state",qApp->arguments().contains("--start-terminal")?"done":*phase==3?"done":*phase==4?"error":"running"},
                 {"start_request_id","fixture-request"},
                 {"current",*phase==0?"Preparando o primeiro clipe":*phase==1?"Enviando clipe para conta de demonstração":"Execução encerrada"},
                 {"totals",QJsonObject{{"total_sends",4},{"done_sends",100},{"skipped_sends",99},
                   {"progress_unit","seconds"},{"progress_target",*phase==0?0:3600},
                   {"progress_completed",*phase==0?0:*phase==1?900:3600}}}};
        } else {qApp->exit(41);return;}
        const auto bytes=QJsonDocument(reply).toJson(QJsonDocument::Compact);
        socket->write(QByteArray(failed?"HTTP/1.1 503 Unavailable\r\n":"HTTP/1.1 200 OK\r\n")+
            "Content-Type: application/json\r\nConnection: close\r\nContent-Length: "+QByteArray::number(bytes.size())+"\r\n\r\n"+bytes);
        socket->disconnectFromHost();
      });
      QObject::connect(socket,&QTcpSocket::disconnected,socket,&QObject::deleteLater);
    });
    window._api.setBaseUrl(QStringLiteral("http://127.0.0.1:%1").arg(server->serverPort()));
    window._previewLogName="old-campaign.json";
    window.pollCampaignPreviews();
    window.submitCampaign({{"preflight_id","fixture-request"}});
    if(window._campaignTabs->currentIndex()!=2 || window._campaignIndicatorTitle->text()!=QStringLiteral("Iniciando campanha…")) {
      qApp->exit(42);return;
    }
    if(qApp->arguments().contains("--start-terminal")) {
      auto* probe=new QTimer(&window);
      QObject::connect(probe,&QTimer::timeout,&window,[&window] {
        if(window._campaignIndicatorTitle->text()==QStringLiteral("Concluída"))
          qApp->exit(window._campaignStart->isEnabled() && !window._campaignStartUncertain
            && qApp->property("campaignStartRequests").toInt()==1 ? 0:50);
      });
      probe->start(25); QTimer::singleShot(8000,&window,[] { qApp->exit(51); });
      return;
    }
    auto* timer=new QTimer(&window);
    QObject::connect(timer,&QTimer::timeout,&window,[&window,phase,oldPreviewReturned]{
      const auto title=window._campaignIndicatorTitle->text();
      if(*phase==0 && window._campaignIndicatorDetail->text().contains("Preparando o primeiro")) {
        if(!window._campaignIndicator->isVisible() || window._campaignIndicatorProgress->maximum()!=0
            || !window._campaignStop->isEnabled() || window._campaignStart->isEnabled()){qApp->exit(43);return;}
        *phase=1;window.pollCampaign();
      } else if(*phase==1 && *oldPreviewReturned && window._campaignIndicatorDetail->text().contains("25%")) {
        if(window._campaignProgress->value()!=25){qApp->exit(47);return;}
        window.renderOperation({{"state","running"},{"operation",QJsonObject{{"accounts",QJsonArray{}},{"counts",QJsonObject{}}}},{"totals",QJsonObject{
            {"progress_unit","seconds"},{"progress_target",3600},{"progress_completed",900},
            {"total_sends",4},{"done_sends",100}}}});
        if(!window._operationTotal->text().contains("0,25 / 1,00")){qApp->exit(48);return;}
        if(qApp->arguments().contains("--indicator-proof")) window.grab().save(qApp->arguments().at(1));
        window._campaignTabs->setCurrentIndex(0);
        if(!window._campaignIndicator->isVisible() || window._campaignIndicatorProgress->value()!=25){qApp->exit(44);return;}
        *phase=2;window.pollCampaign();
      } else if(*phase==2 && title==QStringLiteral("Sem atualização do serviço")) {
        *phase=3;window.pollCampaign();
      } else if(*phase==3 && title==QStringLiteral("Concluída")) {
        if(!window._campaignStart->isEnabled() || window._campaignStop->isEnabled()){qApp->exit(45);return;}
        *phase=4;window.pollCampaign();
      } else if(*phase==4 && title==QStringLiteral("Atenção necessária")) qApp->exit(0);
    });
    timer->start(25);
    QTimer::singleShot(8000,&window,[]{qApp->exit(46);});
  }
  static void continuationSmoke(MainWindow& window) {
    auto* server = new QTcpServer(&window);
    if (!server->listen(QHostAddress::LocalHost)) { qApp->exit(30); return; }
    auto requests = std::make_shared<int>(0);
    QObject::connect(server, &QTcpServer::newConnection, server, [server, requests] {
      auto* socket = server->nextPendingConnection();
      QObject::connect(socket, &QTcpSocket::readyRead, socket, [socket, requests] {
        auto input = socket->property("request").toByteArray() + socket->readAll();
        socket->setProperty("request", input);
        const int headerEnd=input.indexOf("\r\n\r\n");
        if (headerEnd<0 || socket->property("answered").toBool()) return;
        const auto match=QRegularExpression(QStringLiteral("Content-Length: (\\d+)"),QRegularExpression::CaseInsensitiveOption)
            .match(QString::fromLatin1(input.left(headerEnd)));
        if(match.hasMatch() && input.size()<headerEnd+4+match.captured(1).toInt()) return;
        socket->setProperty("answered", true);
        if(input.startsWith("POST /api/campaigns ") && qApp->arguments().contains("--expired-review") && *requests==0) {
          ++*requests;
          const QByteArray body=R"({"error_code":"preflight_expired","error":"Expired fixture"})";
          socket->write("HTTP/1.1 409 Conflict\r\nContent-Type: application/json\r\nConnection: close\r\nContent-Length: "+QByteArray::number(body.size())+"\r\n\r\n"+body);
          socket->disconnectFromHost(); return;
        }
        if(input.startsWith("POST /api/campaigns ") && qApp->arguments().contains("--continuation-accept")) {
          const auto body=QJsonDocument::fromJson(input.mid(headerEnd+4)).object();
          const bool correct=*requests==1 && body.value("preflight_id").toString()=="fixture"
              && body.value("remove_restricted").toBool() && body.value("accounts").toArray().size()==2;
          qApp->exit(correct?0:34); return;
        }
        if (!input.startsWith("POST /api/campaigns/preflight?async=1&request_id=")) { qApp->exit(31); return; }
        if(qApp->arguments().contains("--expired-review")) {
          const auto requestBody=QJsonDocument::fromJson(input.mid(headerEnd+4)).object();
          if(requestBody.contains("preflight_id") || requestBody.contains("remove_restricted")) {qApp->exit(35);return;}
        }
        ++*requests;
        QJsonObject result{{"ok",false},{"can_remove_and_continue",true},{"preflight_id","fixture"},
          {"blockers",QJsonArray{"blocked"}},{"account_errors",QJsonArray{"blocked"}},
          {"removable_accounts",QJsonArray{"blocked@example.com"}},
          {"account_issues",QJsonArray{QJsonObject{{"email","blocked@example.com"},{"restriction_confirmed",true}}}},
          {"accounts",QJsonObject{{"validated",2}}},{"account_workers",2},{"estimated_sends",2}};
        result["capacity"] = capacityFixture(QJsonDocument::fromJson(input.mid(headerEnd+4)).object(), {"ok@example.com"});
        const auto body=QJsonDocument(result).toJson(QJsonDocument::Compact);
        socket->write("HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\nContent-Length: "+QByteArray::number(body.size())+"\r\n\r\n"+body);
        socket->disconnectFromHost();
      });
      QObject::connect(socket,&QTcpSocket::disconnected,socket,&QObject::deleteLater);
    });
    window._api.setBaseUrl(QStringLiteral("http://127.0.0.1:%1").arg(server->serverPort()));
    {
      const QSignalBlocker accountsBlocker(window._campaignAccounts), tasksBlocker(window._campaignTasks);
      for(const auto email:{"ok@example.com","blocked@example.com"}) {
        auto* item=new QListWidgetItem(QString::fromLatin1(email),window._campaignAccounts);
        item->setData(Qt::UserRole,QString::fromLatin1(email)); item->setCheckState(Qt::Checked);
      }
      auto* task=new QListWidgetItem(QStringLiteral("Fixture"),window._campaignTasks);
      task->setData(Qt::UserRole,QStringLiteral("fixture-task")); task->setCheckState(Qt::Checked);
    }
    window._taskReload.stop();
    auto* timer=new QTimer(&window);
    QObject::connect(timer,&QTimer::timeout,&window,[timer,requests] {
      auto* dialog=qobject_cast<QDialog*>(QApplication::activeModalWidget());
      if(!dialog) return;
      if(dialog->windowTitle()==QStringLiteral("Revisar campanha")) {
        auto* table=dialog->findChild<QTableWidget*>();
        const bool correct=table && table->rowCount()==1 && table->item(0,0)->text()==QStringLiteral("ok@example.com");
        timer->stop();
        if(correct && qApp->arguments().contains("--continuation-accept")) {dialog->accept();return;}
        dialog->reject();
        QTimer::singleShot(150,qApp,[requests,correct]{
          const int expected=qApp->arguments().contains("--expired-review")?2:1;
          qApp->exit(correct && *requests==expected ? 0:32);
        });
        return;
      }
      for(auto* button:dialog->findChildren<QPushButton*>())
        if(button->text()==QStringLiteral("Revisar contas aprovadas")) {button->click();return;}
    });
    timer->start(25);
    QTimer::singleShot(8000,&window,[]{qApp->exit(33);});
    if(qApp->arguments().contains("--expired-review")) {
      window.submitCampaign({{"accounts",QJsonArray{"ok@example.com","blocked@example.com"}},
                             {"tasks",QJsonArray{QJsonObject{{"task_id","fixture-task"}}}},
                             {"preflight_id","expired"},{"remove_restricted",true}});
    } else window.startCampaign();
  }
  static void smoke(MainWindow& window) {
    auto* server = new QTcpServer(&window);
    if (!server->listen(QHostAddress::LocalHost)) { qApp->exit(10); return; }
    auto paused = std::make_shared<bool>(false);
    auto reconciled = std::make_shared<bool>(false);
    QObject::connect(server, &QTcpServer::newConnection, server, [server, paused, reconciled] {
      auto* socket = server->nextPendingConnection();
      QObject::connect(socket, &QTcpSocket::readyRead, socket, [socket, paused, reconciled] {
        auto request = socket->property("request").toByteArray() + socket->readAll();
        socket->setProperty("request", request);
        if (!request.contains("\r\n\r\n") || socket->property("answered").toBool()) return;
        socket->setProperty("answered",true);
        QJsonObject response;
        if (request.startsWith("GET /api/accounts ")) response = {{"accounts", QJsonArray{QJsonObject{{"email","fixture@example.com"}}}}};
        else if (request.startsWith("GET /api/logs ")) response = {{"logs",QJsonArray{QJsonObject{
          {"name","fixture.json"},{"started_at","2026-09-26T12:00:00"},
          {"accounts",QJsonArray{"fixture@example.com"}},{"items",2},{"ok",1},{"sends",2}}}}};
        else if (request.startsWith("GET /api/logs/fixture.json ")) response = {
          {"summary",QJsonObject{{"success",1},{"pending",1},{"videos",2}}},
          {"items",QJsonArray{
            QJsonObject{{"clip_uid","clip-confirmed"},{"accounts",QJsonArray{QJsonObject{
              {"email","fixture@example.com"},{"session_id","session-confirmed"},{"confirmation","remote_ack"}}}}},
            QJsonObject{{"clip_uid","clip-pending"},{"accounts",QJsonArray{QJsonObject{
              {"email","fixture@example.com"},{"session_id","session-pending"},{"status","pending"}}}}}}}};
        else if (request.startsWith("GET /api/balances ")) response = {{"accounts",QJsonArray{}},{"balances",QJsonObject{}}};
        else if (request.startsWith("POST /api/campaigns/pause ")) { *paused=true; response={{"ok",true}}; }
        else if (request.startsWith("POST /api/campaigns/resume ")) { *paused=false; response={{"ok",true}}; }
        else if (request.startsWith("GET /api/recovery?async=1 ")) response = {
          {"pending",0},{"confirmed",1},{"items",QJsonArray{QJsonObject{{"email","fixture@example.com"},
              {"session_id","fixture-session"},{"clip_uid","fixture-clip"},{"status","confirmed"}}}}};
        else if (request.startsWith("POST /api/recovery/reconcile ")) {
          *reconciled=true; response={{"pending",0},{"confirmed",0},{"items",QJsonArray{}}};
        }
        else if (request.startsWith("GET /api/campaigns/current ")) response = {
          {"state","running"},{"pause_requested",*paused},{"totals",QJsonObject{{"total_sends",1},{"done_sends",0}}},
          {"operation",QJsonObject{{"accounts",QJsonArray{QJsonObject{{"email","fixture@example.com"},{"state","sending"},{"progress",60}}}},{"counts",QJsonObject{{"sending",1}}}}}};
        else { qApp->exit(11); return; }
        const auto body=QJsonDocument(response).toJson(QJsonDocument::Compact);
        socket->write("HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\nContent-Length: "+QByteArray::number(body.size())+"\r\n\r\n"+body);
        socket->disconnectFromHost();
      });
      QObject::connect(socket,&QTcpSocket::disconnected,socket,&QObject::deleteLater);
    });
    window._api.setBaseUrl(QStringLiteral("http://127.0.0.1:%1").arg(server->serverPort()));
    window.loadHome();
    auto* timer = new QTimer(&window);
    auto step = std::make_shared<int>(0);
    QObject::connect(timer,&QTimer::timeout,&window,[&window,step,reconciled] {
      if (!window._operationPause->isEnabled() || window._operationTable->rowCount()!=1) return;
      if (*step==0) {
        if(window._operationTable->item(0,0)->text()!=QStringLiteral("fixture@example.com")) {qApp->exit(12);return;}
        ++*step; window._operationPause->click();
      } else if (*step==1 && window._operationPauseRequested) {
        ++*step; window._operationPause->click();
      } else if (*step==2 && !window._operationPauseRequested) {
        ++*step; window.openRecovery();
      } else if (*step==3) {
        for (auto* dialog : window.findChildren<QDialog*>()) {
          if (dialog->windowTitle()!=QStringLiteral("Recuperação de envios")) continue;
          auto* table = dialog->findChild<QTableWidget*>();
          if (!table || table->rowCount()!=1) continue;
          for (auto* button : dialog->findChildren<QPushButton*>()) {
            if (button->text()==QStringLiteral("Reconciliar confirmações") && button->isEnabled()) {
              ++*step; button->click(); return;
            }
          }
        }
      } else if (*step==4 && *reconciled) {
        for (auto* dialog : window.findChildren<QDialog*>()) {
          auto* table = dialog->findChild<QTableWidget*>();
          if (dialog->windowTitle()==QStringLiteral("Recuperação de envios") && table && table->rowCount()==0) {
            dialog->close(); ++*step; page(window, 7); window.loadHistory(); return;
          }
        }
      } else if (*step==5 && window._historyTable->rowCount()==1) {
        ++*step; window._historyTable->setCurrentCell(0,0);
      } else if (*step==6 && window._historyEvidence->rowCount()==2) {
        if(window._historyEvidence->item(0,2)->text()!="session-confirmed"
            || window._historyEvidence->item(0,3)->text()!=QStringLiteral("Confirmado na tentativa original")
            || window._historyEvidence->item(1,3)->text()!=QStringLiteral("Sem confirmação")) {
          qApp->exit(23); return;
        }
        if(qApp->arguments().contains("--history-proof")) {
          for(auto* tabs:window._pages->widget(7)->findChildren<QTabWidget*>()) tabs->setCurrentIndex(1);
          window.grab().save(qApp->arguments().at(1));
        }
        qApp->exit(0);
      }
    });
    timer->start(25);
    QTimer::singleShot(8000,&window,[]{qApp->exit(13);});
  }
  static void page(MainWindow& window, int index) {
    const QSignalBlocker blocker(window._navigation);
    window._navigation->setCurrentRow(index);
    window._pages->setCurrentIndex(index);
  }
  static void withdrawalReview(MainWindow& window, const QString& output) {
    window._balancesWithdrawAll->setProperty("eligibleCount", 5);
    window._balancesWithdrawAll->setEnabled(true);
    QTimer::singleShot(100, &window, [&window, output] {
      auto* dialog = qobject_cast<QDialog*>(QApplication::activeModalWidget());
      if (!dialog) { qApp->exit(21); return; }
      auto* method = dialog->findChild<QComboBox*>();
      if (method) method->setCurrentIndex(1);
      QTimer::singleShot(100, dialog, [dialog, output] {
        const bool saved = dialog->grab().save(output);
        dialog->reject(); // Never authorizes or submits a withdrawal.
        qApp->exit(saved ? 0 : 22);
      });
    });
    window._balancesWithdrawAll->click();
  }
  static void populate(MainWindow& window, bool dark, bool active) {
    window.applyStructuralStyle(dark);
    const QJsonArray accounts = active ? QJsonArray{QJsonObject{{"email", "fixture@example.com"}}} : QJsonArray{};
    const auto summary = OperationSummary::from(accounts, {{"state", active ? "running" : "idle"}, {"current", "Enviando clipe • conta de demonstração"}});
    QJsonArray rows;
    if (active) {
      for (int i=0;i<48;++i) {
        const QString state = i<38 ? "confirmed" : i<44 ? "sending" : "confirming";
        rows.append(QJsonObject{{"email", QStringLiteral("Conta %1").arg(i+1,2,10,QLatin1Char('0'))}, {"state",state}, {"session_id",QStringLiteral("demo_%1").arg(i+1)}, {"confirmed", i<38?1:0}, {"progress", i<38?100:60}});
      }
    }
    window.renderOperation(QJsonObject{{"state", active?"running":"idle"}, {"totals",QJsonObject{{"total_sends",active?48:0},{"ok_sends",active?38:0}}}, {"operation",QJsonObject{{"accounts",rows}, {"counts",QJsonObject{{"confirmed",active?38:0},{"sending",active?6:0},{"confirming",active?4:0}}}}},
      {"events", QJsonArray{QJsonObject{{"ts", 1790431200}, {"title", "Vídeo enviado"}, {"detail", "Conta 03"}}, QJsonObject{{"ts", 1790431380}, {"title", "Processando"}, {"detail", "Aguardando confirmação do recebimento."}}}}});
    window._operationBalance->setText(active ? QStringLiteral("US$ 284,50") : QStringLiteral("US$ —"));
    window._operationBalanceNote->setText(QStringLiteral("Dados de demonstração"));
    window._homePulseTitle->setText(active ? QStringLiteral("Campanha em andamento") : summary.title);
    window._homePulseBody->setText(active ? QStringLiteral("Biblioteca principal · 48 contas") : summary.detail);
    window._homeNextAction->setText(summary.action + QStringLiteral(" →"));
    window._homeNextAction->setEnabled(true);
    window._homeAccounts->setText(active ? "1" : "0");
    window._homeCampaigns->setText(active ? "3" : "0");
    window._homeSuccess->setText(active ? "8 / 8" : "—");
    window._homeAccountStep->setText(active ? QStringLiteral("Acompanhe a confirmação antes do próximo envio.") : QStringLiteral("Comece conectando sua primeira conta."));
    window._homeRecent->setText(active ? QStringLiteral("Dados de demonstração • consulte os resultados por conta no histórico.") : QStringLiteral("Sua primeira campanha aparecerá aqui. Comece pelos passos acima."));
    window._homeSync->setText(QStringLiteral("Prévia visual • dados de demonstração"));
    window._homePulseProgress->setValue(active ? 79 : 0);
    window._operationProgressRow->setVisible(active);
    window._backendState->setText(QStringLiteral("●  Prévia isolada"));
    window._status->setText(QStringLiteral("Demonstração visual • nenhum serviço iniciado"));
  }
};

int main(int argc, char** argv) {
  QApplication app(argc, argv);
  if (app.arguments().contains("--export-brand")) {
    return QIcon(QStringLiteral(":/qmoney/icons/brand.svg")).pixmap(256, 256)
        .save(app.arguments().at(1)) ? 0 : 1;
  }
  app.setOrganizationName("QMoneyVisualTests");
  app.setApplicationName("OperationPreview");
  QTemporaryDir testSettings;
  QSettings::setDefaultFormat(QSettings::IniFormat);
  const auto persistentTestSettings = qEnvironmentVariable("QMONEY_TEST_SETTINGS_DIR");
  QSettings::setPath(QSettings::IniFormat, QSettings::UserScope,
    (app.arguments().contains("--start-persistence-write") || app.arguments().contains("--start-persistence-read"))
      && !persistentTestSettings.isEmpty() ? persistentTestSettings : testSettings.path());
  const bool dark = app.arguments().contains("--dark");
  auto* style = new oclero::qlementine::QlementineStyle(&app);
  style->setAnimationsEnabled(false);
  style->setThemeJsonPath(dark ? ":/qmoney/theme-dark.json" : ":/qmoney/theme-light.json");
  app.setStyle(style);
  app.setFont(QFont(QStringLiteral("Inter"), 10));
  MainWindow window(style, nullptr, false);
  window.resize(app.arguments().contains("--compact") ? QSize(980, 680) : QSize(1586, 992));
  if (app.arguments().contains("--account-services-smoke") || app.arguments().contains("--account-services-preview")) {
    window.show();
    QTimer::singleShot(100,&window,[&window] {OperationPreview::accountServicesSmoke(window);});
    return app.exec();
  }
  if (app.arguments().contains("--accounts-qa-smoke") || app.arguments().contains("--accounts-qa-preview") || app.arguments().contains("--accounts-live-create")) {
    window.show();
    QTimer::singleShot(100,&window,[&window] {
      if(qApp->arguments().contains("--accounts-live-create")) OperationPreview::accountsLiveCreate(window);
      else if(qApp->arguments().contains("--accounts-qa-preview")) OperationPreview::accountsQaPreview(window);
      else OperationPreview::accountsQaSmoke(window);
    });
    return app.exec();
  }
  if (app.arguments().contains("--operation-qa-smoke") || app.arguments().contains("--operation-qa-preview")) {
    window.show();
    const bool preview = app.arguments().contains("--operation-qa-preview");
    QTimer::singleShot(100,&window,[&window,preview] {
      if (preview) OperationPreview::operationQaPreview(window); else OperationPreview::operationQaSmoke(window);
    });
    return app.exec();
  }
  if (app.arguments().contains("--continuation-smoke")) {
    QTimer::singleShot(0, &window, [&window]{OperationPreview::continuationSmoke(window);});
    return app.exec();
  }
  window.show();
  if (app.arguments().contains("--campaign-close-smoke") || app.arguments().contains("--campaign-quit-smoke")) {
    app.setQuitOnLastWindowClosed(false);
    const bool requestQuit = app.arguments().contains("--campaign-quit-smoke");
    QTimer::singleShot(100, &window, [&window, requestQuit]{OperationPreview::campaignCloseSmoke(window, requestQuit);});
    const int result = app.exec();
    return result ? result : (OperationPreview::campaignCloseFinished(window) ? 0 : 194);
  }
  if (app.arguments().contains("--campaign-history-evidence-smoke")) {
    QTimer::singleShot(100,&window,[&window]{OperationPreview::campaignHistoryEvidenceSmoke(window);});
    return app.exec();
  }
  if (app.arguments().contains("--campaign-parameters-smoke")) {
    QTimer::singleShot(100,&window,[] { OperationPreview::campaignParametersSmoke(); });
    return app.exec();
  }
  if (app.arguments().contains("--original-live-lab")) {
    QTimer::singleShot(100, &window, [&window] { OperationPreview::originalLiveLab(window); });
    return app.exec();
  }
  if (app.arguments().contains("--confirmed-start-restart-smoke")) {
    QTimer::singleShot(100, &window, [&window] { OperationPreview::campaignConfirmedRestartSmoke(window); });
    return app.exec();
  }
  if (app.arguments().contains("--start-persistence-smoke") || app.arguments().contains("--start-persistence-write")
      || app.arguments().contains("--start-persistence-read")) {
    QTimer::singleShot(100, &window, [&window] { OperationPreview::campaignStartPersistenceSmoke(window); });
    return app.exec();
  }
  if (app.arguments().contains("--campaign-controls-smoke") || app.arguments().contains("--campaign-controls-manual")) {
    QTimer::singleShot(100,&window,[&window] { OperationPreview::campaignControlsSmoke(window); });
    return app.exec();
  }
  if (app.arguments().contains("--campaign-evaluation-resume-smoke")) {
    QTimer::singleShot(100, &window, [&window] { OperationPreview::campaignEvaluationResumeSmoke(window); });
    return app.exec();
  }
  if (app.arguments().contains("--accelerator-smoke")) {
    QTimer::singleShot(100, &window, [&window] { OperationPreview::acceleratorSmoke(window); });
    return app.exec();
  }
  if (app.arguments().contains("--prepared-library-smoke")) {
    QTimer::singleShot(100, &window, [&window] { OperationPreview::preparedLibrarySmoke(window); });
    return app.exec();
  }
  if (app.arguments().contains("--local-media-library-smoke")) {
    QTimer::singleShot(100, &window, [&window] { OperationPreview::localMediaLibrarySmoke(window); });
    return app.exec();
  }
  if (app.arguments().contains("--campaign-capacity-smoke")) {
    QTimer::singleShot(100, &window, [&window] { OperationPreview::campaignCapacitySmoke(window); });
    return app.exec();
  }
  if (app.arguments().contains("--campaign-reset-smoke")) {
    QTimer::singleShot(100, &window, [&window] { OperationPreview::campaignResetSmoke(window); });
    return app.exec();
  }
  if (app.arguments().contains("--campaign-restriction-smoke")) {
    QTimer::singleShot(100, &window, [&window] { OperationPreview::campaignRestrictionSmoke(window); });
    return app.exec();
  }
  if (app.arguments().contains("--nymeria-library-smoke")) {
    QTimer::singleShot(100, &window, [&window] { OperationPreview::nymeriaLibrarySmoke(window); });
    return app.exec();
  }
  if (app.arguments().contains("--campaign-all-compatible-smoke")) {
    QTimer::singleShot(100, &window, [&window] { OperationPreview::campaignAllCompatiblePreferenceSmoke(window); });
    return app.exec();
  }
  if (app.arguments().contains("--campaign-on-demand-smoke")) {
    QTimer::singleShot(100, &window, [&window] { OperationPreview::campaignOnDemandSmoke(window); });
    return app.exec();
  }
  if (app.arguments().contains("--original-library-smoke")) {
    QTimer::singleShot(100, &window, [&window] { OperationPreview::originalLibrarySmoke(window); });
    return app.exec();
  }
  if (app.arguments().contains("--credential-copy-smoke")) {
    QTimer::singleShot(100, &window, [&window] { OperationPreview::credentialCopySmoke(window); });
    return app.exec();
  }
  if (app.arguments().contains("--credential-view-smoke")) {
    QTimer::singleShot(100, &window, [&window] { OperationPreview::credentialViewSmoke(window); });
    return app.exec();
  }
  if (app.arguments().contains("--settings-smoke")) {
    QTimer::singleShot(100, &window, [&window] { OperationPreview::settingsSmoke(window); });
    return app.exec();
  }
  if (app.arguments().contains("--recovery-diagnostics-smoke") || app.arguments().contains("--recovery-preview")) {
    QTimer::singleShot(100, &window, [&window] { OperationPreview::recoveryDiagnosticsSmoke(window); });
    return app.exec();
  }
  if (app.arguments().contains("--live-catalog-smoke")) {
    QTimer::singleShot(100, &window, [&window] { OperationPreview::liveCatalogSmoke(window); });
    return app.exec();
  }
  if (app.arguments().contains("--catalog-loading-smoke")) {
    QTimer::singleShot(100, &window, [&window] { OperationPreview::catalogLoadingSmoke(window); });
    return app.exec();
  }
  if (app.arguments().contains("--catalog-timeout-recovery-smoke")) {
    QTimer::singleShot(100, &window, [&window] { OperationPreview::catalogTimeoutRecoverySmoke(window); });
    return app.exec();
  }
  if (app.arguments().contains("--catalog-progress-smoke")) {
    QTimer::singleShot(100, &window, [&window] { OperationPreview::catalogProgressSmoke(window); });
    return app.exec();
  }
  if (app.arguments().contains("--mail-cleanup-smoke")) {
    QTimer::singleShot(100, &window, [&window] { OperationPreview::mailCleanupSmoke(window); });
    return app.exec();
  }
  if (app.arguments().contains("--balance-polling-smoke")) {
    QTimer::singleShot(100, &window, [&window] { OperationPreview::balancePollingSmoke(window); });
    return app.exec();
  }
  if (app.arguments().contains("--wallet-smoke") || app.arguments().contains("--wallet-preview")) {
    const bool preview = app.arguments().contains("--wallet-preview");
    QTimer::singleShot(100,&window,[&window,preview] { OperationPreview::walletContract(window,preview); });
    return app.exec();
  }
  if (app.arguments().contains("--wallet-cooldown-smoke")) {
    QTimer::singleShot(100, &window, [&window] { OperationPreview::walletCooldownSmoke(window); });
    return app.exec();
  }
  if (app.arguments().contains("--indicator-smoke")) {
    QTimer::singleShot(100,&window,[&window]{OperationPreview::campaignIndicatorSmoke(window);});
    return app.exec();
  }
  if (app.arguments().contains("--smoke")) {
    QTimer::singleShot(100,&window,[&window]{OperationPreview::smoke(window);});
    return app.exec();
  }
  QTimer::singleShot(100, &window, [&] {
    OperationPreview::populate(window, dark, app.arguments().contains("--active"));
    if (app.arguments().contains("--withdraw-review")) {
      OperationPreview::withdrawalReview(window, app.arguments().at(1));
      return;
    }
    if (app.arguments().contains("--review")) {
      QStringList accounts;
      for (int i = 1; i <= 48; ++i) accounts << QStringLiteral("conta-%1@example.com").arg(i);
      auto* review = new CampaignReviewDialog({
          {"accounts", QJsonObject{{"validated", 48}}}, {"tasks", QJsonObject{{"compatible", 8}}},
          {"account_workers", 6}, {"clips", 210}, {"estimated_sends", 1344},
          {"clip_plan", QJsonArray{QJsonObject{{"clip_uid", "demo-furniture-001"}, {"task", "Montagem de móveis"},
              {"duration_s", 600}, {"eligible_accounts", QJsonArray{"conta-2@example.com", "conta-3@example.com"}},
              {"excluded_accounts", QJsonArray{"conta-1@example.com"}}}}},
          {"warnings", QJsonArray{QStringLiteral("Demonstração visual. Nenhum envio será iniciado.")}}}, accounts, &window);
      if (app.arguments().contains("--review-clips")) {
        review->findChild<QTabWidget*>()->setCurrentIndex(1);
        const auto tables = review->findChildren<QTableWidget*>();
        for (auto* table : tables) if (table->columnCount() == 5) table->setCurrentCell(0, 0);
      }
      review->show();
      QTimer::singleShot(100, review, [&, review] { app.exit(review->grab().save(app.arguments().at(1)) ? 0 : 1); });
      return;
    }
    const int pageArg = app.arguments().indexOf("--page");
    if (pageArg >= 0 && pageArg + 1 < app.arguments().size())
      OperationPreview::page(window, app.arguments().at(pageArg + 1).toInt());
    QTimer::singleShot(100, &window, [&] {
      app.exit(window.grab().save(app.arguments().at(1)) ? 0 : 1);
    });
  });
  return app.exec();
}
