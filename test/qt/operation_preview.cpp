// Offscreen visual fixture. Never starts a backend or an update check.
#include <memory>
#include "../../desktop/src/MainWindow.hpp"
#include "../../desktop/src/OperationSummary.hpp"
#include "../../desktop/src/CampaignReviewDialog.hpp"
#include <oclero/qlementine/style/QlementineStyle.hpp>
#include <QApplication>
#include <QRegularExpression>
#include <QComboBox>
#include <QDir>
#include <QLabel>
#include <QPushButton>
#include <QProgressBar>
#include <QSettings>
#include <QStackedWidget>
#include <QListWidget>
#include <QSignalBlocker>
#include <QTimer>
#include <QTcpServer>
#include <QTcpSocket>
#include <QTableWidget>

class OperationPreview {
public:
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
        if (!input.startsWith("POST /api/campaigns/preflight ")) { qApp->exit(31); return; }
        if(qApp->arguments().contains("--expired-review")) {
          const auto requestBody=QJsonDocument::fromJson(input.mid(headerEnd+4)).object();
          if(requestBody.contains("preflight_id") || requestBody.contains("remove_restricted")) {qApp->exit(35);return;}
        }
        ++*requests;
        const QJsonObject result{{"ok",false},{"can_remove_and_continue",true},{"preflight_id","fixture"},
          {"blockers",QJsonArray{"blocked"}},{"account_errors",QJsonArray{"blocked"}},
          {"removable_accounts",QJsonArray{"blocked@example.com"}},
          {"account_issues",QJsonArray{QJsonObject{{"email","blocked@example.com"},{"restriction_confirmed",true}}}},
          {"accounts",QJsonObject{{"validated",2}}},{"account_workers",2},{"estimated_sends",2}};
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
        else if (request.startsWith("GET /api/recovery ")) response = {
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
            || window._historyEvidence->item(0,3)->text()!=QStringLiteral("Finalização confirmada")
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
    window.renderOperation(QJsonObject{{"state", active?"running":"idle"}, {"operation",QJsonObject{{"accounts",rows}, {"counts",QJsonObject{{"confirmed",active?38:0},{"sending",active?6:0},{"confirming",active?4:0}}}}},
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
  const bool dark = app.arguments().contains("--dark");
  auto* style = new oclero::qlementine::QlementineStyle(&app);
  style->setAnimationsEnabled(false);
  style->setThemeJsonPath(dark ? ":/qmoney/theme-dark.json" : ":/qmoney/theme-light.json");
  app.setStyle(style);
  MainWindow window(style, nullptr, false);
  window.resize(app.arguments().contains("--compact") ? QSize(980, 680) : QSize(1586, 992));
  if (app.arguments().contains("--continuation-smoke")) {
    QTimer::singleShot(0, &window, [&window]{OperationPreview::continuationSmoke(window);});
    return app.exec();
  }
  window.show();
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
