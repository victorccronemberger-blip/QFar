  static void recoveryDiagnosticsSmoke(MainWindow& window) {
    QCoreApplication::setApplicationVersion(QStringLiteral("fixture-2.0.39"));
    window.applyStructuralStyle(qApp->arguments().contains("--dark"));
    auto* server = new QTcpServer(&window);
    if (!server->listen(QHostAddress::LocalHost)) { qApp->exit(220); return; }
    struct State { int reads=0, preflights=0, starts=0, phase=0; };
    auto state = std::make_shared<State>();
    const QJsonObject detail{{"code", "journal_unreadable"}, {"record_ref", "012345abcdef"},
                            {"message", "Registro ilegível preservado."}};
    QObject::connect(server, &QTcpServer::newConnection, server, [server, state, detail] {
      auto* socket = server->nextPendingConnection();
      QObject::connect(socket, &QTcpSocket::readyRead, socket, [socket, state, detail] {
        const auto input = socket->property("input").toByteArray() + socket->readAll();
        socket->setProperty("input", input);
        if (!input.contains("\r\n\r\n") || socket->property("answered").toBool()) return;
        socket->setProperty("answered", true);
        QJsonObject result;
        QByteArray status = "200 OK";
        if (input.startsWith("POST /api/campaigns/preflight?async=1&request_id=")) {
          ++state->preflights;
          result = {{"ok", false}, {"preflight_id", QJsonValue::Null},
                    {"blockers", QJsonArray{"Registro ilegível preservado."}}, {"recovery_error", detail}};
        } else if (input.startsWith("GET /api/recovery?async=1")) {
          ++state->reads;
          if (state->reads == 1) {
            status = "409 Conflict";
            result = {{"error", "Registro ilegível preservado."}, {"recovery_error", detail}};
          } else {
            if (!input.startsWith("GET /api/recovery?async=1&refresh=1 ")) { qApp->exit(226); return; }
            result = {{"items", QJsonArray{}}, {"pending", 0}, {"confirmed", 0},
                      {"worker", QJsonObject{{"state", "idle"}}}};
          }
        } else {
          ++state->starts;
          result = {{"error", "Unexpected mutation"}};
          status = "409 Conflict";
        }
        const auto body = QJsonDocument(result).toJson(QJsonDocument::Compact);
        socket->write("HTTP/1.1 " + status + "\r\nContent-Type: application/json\r\nConnection: close\r\nContent-Length: "
                      + QByteArray::number(body.size()) + "\r\n\r\n" + body);
        socket->disconnectFromHost();
      });
    });
    window._api.setBaseUrl(QStringLiteral("http://127.0.0.1:%1").arg(server->serverPort()));
    auto* monitor = new QTimer(&window);
    QObject::connect(monitor, &QTimer::timeout, &window, [&window, state, monitor] {
      if (state->phase == 0) {
        for (auto* message : window.findChildren<QMessageBox*>()) {
          for (auto* button : message->buttons()) {
            if (button->text() == QStringLiteral("Abrir recuperação")) {
              state->phase = 1;
              button->click();
              return;
            }
          }
        }
      }
      for (auto* dialog : window.findChildren<QDialog*>()) {
        auto* copy = dialog->findChild<QPushButton*>(QStringLiteral("recoveryCopyDiagnostic"));
        auto* retry = dialog->findChild<QPushButton*>(QStringLiteral("recoveryRetry"));
        auto* summary = dialog->findChild<QLabel*>(QStringLiteral("recoverySummary"));
        if (!copy || !retry || !summary) continue;
        if (state->phase == 1 && !copy->isHidden()) {
          const auto tables = dialog->findChildren<QTableWidget*>();
          if (tables.isEmpty() || !tables.first()->isHidden() || !retry->isEnabled()) { qApp->exit(221); return; }
          copy->click();
          const auto copied = QJsonDocument::fromJson(QApplication::clipboard()->text().toUtf8()).object();
          if (copied.value("recovery_error").toObject().value("code") != "journal_unreadable"
              || copied.value("version").toString().isEmpty()) { qApp->exit(222); return; }
          if (qApp->arguments().contains("--recovery-preview")
              && !dialog->grab().save(qApp->arguments().at(1))) { qApp->exit(224); return; }
          state->phase = 2;
          retry->click();
          return;
        }
        if (state->phase == 2 && state->reads >= 2 && copy->isHidden()) {
          const auto tables = dialog->findChildren<QTableWidget*>();
          if (tables.isEmpty() || tables.first()->isHidden() || !summary->text().contains("0 sessão")
              || state->preflights != 1 || state->starts != 0) { qApp->exit(223); return; }
          monitor->stop();
          qApp->exit(0);
          return;
        }
      }
    });
    monitor->start(25);
    QTimer::singleShot(7000, &window, [] { qApp->exit(225); });
    window.preflightCampaign({{"accounts", QJsonArray{"fixture@example.com"}}}, {"fixture@example.com"});
  }

  static void recoveryWorkerStateSmoke(MainWindow& window) {
    auto* server = new QTcpServer(&window);
    if (!server->listen(QHostAddress::LocalHost)) { qApp->exit(227); return; }
    struct State { int reads=0, phase=0; };
    auto state = std::make_shared<State>();
    QObject::connect(server, &QTcpServer::newConnection, server, [server, state] {
      auto* socket = server->nextPendingConnection();
      QObject::connect(socket, &QTcpSocket::readyRead, socket, [socket, state] {
        const auto input = socket->property("input").toByteArray() + socket->readAll();
        socket->setProperty("input", input);
        if (!input.contains("\r\n\r\n") || socket->property("answered").toBool()) return;
        socket->setProperty("answered", true);
        if (!input.startsWith("GET /api/recovery?async=1")) { qApp->exit(228); return; }
        ++state->reads;
        const bool running = state->reads <= 4;
        const QJsonObject result = state->reads == 3
            ? QJsonObject{{"loading", true}, {"code", "catalog_work_pending"},
                          {"error", "The read is still running."},
                          {"worker", QJsonObject{{"state", "running"}, {"email", "fixture@example.com"}}}}
            : running
                ? QJsonObject{{"loading", true}, {"message", "Lendo registros de recuperação…"},
                              {"worker", QJsonObject{{"state", "running"}, {"email", "fixture@example.com"}}}}
                : QJsonObject{{"loading", false}, {"items", QJsonArray{}}, {"pending", 0}, {"confirmed", 0},
                              {"worker", QJsonObject{{"state", "done"}, {"email", "fixture@example.com"}}}};
        const QByteArray status = state->reads == 3 ? "504 Gateway Timeout"
            : running ? "202 Accepted" : "200 OK";
        const auto body = QJsonDocument(result).toJson(QJsonDocument::Compact);
        socket->write("HTTP/1.1 " + status + "\r\nContent-Type: application/json\r\nConnection: close\r\nContent-Length: "
                      + QByteArray::number(body.size()) + "\r\n\r\n" + body);
        socket->disconnectFromHost();
      });
    });
    window._api.setBaseUrl(QStringLiteral("http://127.0.0.1:%1").arg(server->serverPort()));
    auto* account = new QListWidgetItem(QStringLiteral("fixture@example.com"));
    account->setFlags(account->flags() | Qt::ItemIsUserCheckable);
    account->setCheckState(Qt::Checked);
    account->setData(Qt::UserRole, QStringLiteral("fixture@example.com"));
    window._campaignAccounts->addItem(account);
    auto* task = new QListWidgetItem(QStringLiteral("Fixture task"));
    task->setFlags(task->flags() | Qt::ItemIsUserCheckable);
    task->setCheckState(Qt::Checked);
    task->setData(Qt::UserRole, QStringLiteral("fixture-task"));
    window._campaignTasks->addItem(task);
    window.updateCampaignActions();
    if (!window._campaignStart->isEnabled()) { qApp->exit(229); return; }

    window.openRecovery();
    QDialog* dialog = nullptr;
    for (auto* candidate : window.findChildren<QDialog*>())
      if (candidate->objectName() == QStringLiteral("campaignRecoveryDialog")) dialog = candidate;
    if (!dialog) { qApp->exit(230); return; }
    dialog->setProperty("recoveryPollBudget", 2);
    auto* poll = dialog->findChild<QTimer*>(QStringLiteral("recoveryPoll"));
    auto* retry = dialog->findChild<QPushButton*>(QStringLiteral("recoveryRetry"));
    auto* summary = dialog->findChild<QLabel*>(QStringLiteral("recoverySummary"));
    if (!poll || !retry || !summary) { qApp->exit(231); return; }
    poll->setInterval(20);
    auto* monitor = new QTimer(&window);
    QObject::connect(monitor, &QTimer::timeout, &window, [&window, dialog, state, monitor, retry, summary] {
      if (state->phase == 0 && state->reads >= 2 && retry->isEnabled()) {
        if (!summary->text().contains(QStringLiteral("acompanhamento automático foi pausado"))
            || window._campaignStart->isEnabled() || !window._recoveryWorkerRunning) {
          qApp->exit(232); return;
        }
        state->phase = 1;
        retry->click();
        if (window._campaignStart->isEnabled()) { qApp->exit(233); return; }
      }
      if (state->phase == 1 && state->reads >= 3 && retry->isEnabled()) {
        if (!summary->text().contains(QStringLiteral("A leitura ainda está em andamento no serviço"))
            || window._campaignStart->isEnabled() || !window._recoveryWorkerRunning) {
          qApp->exit(234); return;
        }
        state->phase = 2;
        retry->click();
      }
      if (state->phase == 2 && state->reads >= 4 && !retry->isEnabled()) {
        state->phase = 3;
        dialog->close();
        if (window._campaignStart->isEnabled()) { qApp->exit(235); return; }
      }
      if (state->phase == 3 && window._recoveryWorkerWatch) {
        window._recoveryWorkerWatch->setInterval(20);
        state->phase = 4;
      }
      if (state->phase == 4 && state->reads >= 5 && !window._recoveryWorkerRunning) {
        if (!window._campaignStart->isEnabled()) { qApp->exit(237); return; }
        monitor->stop();
        qApp->exit(0);
      }
    });
    monitor->start(10);
    QTimer::singleShot(7000, &window, [] { qApp->exit(236); });
  }

  static void recoveryCommandCloseSmoke(MainWindow& window) {
    auto* server = new QTcpServer(&window);
    if (!server->listen(QHostAddress::LocalHost)) { qApp->exit(238); return; }
    struct State { int posts=0, watchReads=0, phase=0; };
    auto state = std::make_shared<State>();
    QObject::connect(server, &QTcpServer::newConnection, server, [server, state] {
      auto* socket = server->nextPendingConnection();
      QObject::connect(socket, &QTcpSocket::readyRead, socket, [socket, state] {
        const auto input = socket->property("input").toByteArray() + socket->readAll();
        socket->setProperty("input", input);
        if (!input.contains("\r\n\r\n") || socket->property("answered").toBool()) return;
        socket->setProperty("answered", true);
        const auto respond = [socket](const QByteArray& status, const QJsonObject& result) {
          const auto body = QJsonDocument(result).toJson(QJsonDocument::Compact);
          socket->write("HTTP/1.1 " + status + "\r\nContent-Type: application/json\r\nConnection: close\r\nContent-Length: "
                        + QByteArray::number(body.size()) + "\r\n\r\n" + body);
          socket->disconnectFromHost();
        };
        if (input.startsWith("POST /api/recovery/resume ")) {
          ++state->posts;
          QTimer::singleShot(200, socket, [respond] {
            respond("200 OK", QJsonObject{{"ok", true}, {"state", "running"}});
          });
          return;
        }
        if (!input.startsWith("GET /api/recovery?async=1")) { qApp->exit(239); return; }
        if (state->posts == 0) {
          respond("200 OK", QJsonObject{{"items", QJsonArray{QJsonObject{
                      {"email", "fixture@example.com"}, {"clip_uid", "clip-1"}, {"session_id", "session-1"},
                      {"status", "pending"}, {"can_resume", true}, {"blocks_campaign", false}}}},
                  {"pending", 1}, {"confirmed", 0}, {"worker", QJsonObject{{"state", "idle"}}}});
          return;
        }
        ++state->watchReads;
        if (state->watchReads == 1) {
          respond("202 Accepted", QJsonObject{{"loading", true}, {"message", "Retomada ativa"},
                  {"worker", QJsonObject{{"state", "running"}, {"email", "fixture@example.com"}}}});
        } else {
          respond("200 OK", QJsonObject{{"items", QJsonArray{}}, {"pending", 0}, {"confirmed", 0},
                  {"worker", QJsonObject{{"state", "done"}, {"email", "fixture@example.com"}}}});
        }
      });
    });
    window._api.setBaseUrl(QStringLiteral("http://127.0.0.1:%1").arg(server->serverPort()));
    auto* account = new QListWidgetItem(QStringLiteral("fixture@example.com"));
    account->setFlags(account->flags() | Qt::ItemIsUserCheckable);
    account->setCheckState(Qt::Checked);
    account->setData(Qt::UserRole, QStringLiteral("fixture@example.com"));
    window._campaignAccounts->addItem(account);
    auto* task = new QListWidgetItem(QStringLiteral("Fixture task"));
    task->setFlags(task->flags() | Qt::ItemIsUserCheckable);
    task->setCheckState(Qt::Checked);
    task->setData(Qt::UserRole, QStringLiteral("fixture-task"));
    window._campaignTasks->addItem(task);
    window.updateCampaignActions();
    if (!window._campaignStart->isEnabled()) { qApp->exit(240); return; }
    window.openRecovery();

    auto recoveryDialog = std::make_shared<QPointer<QDialog>>();
    auto* monitor = new QTimer(&window);
    QObject::connect(monitor, &QTimer::timeout, &window, [&window, state, monitor, recoveryDialog] {
      if (!*recoveryDialog) {
        for (auto* candidate : window.findChildren<QDialog*>())
          if (candidate->objectName() == QStringLiteral("campaignRecoveryDialog")) *recoveryDialog = candidate;
      }
      if (state->phase == 0 && *recoveryDialog) {
        for (auto* button : (*recoveryDialog)->findChildren<QPushButton*>()) {
          if (button->text().startsWith(QStringLiteral("Retomar")) && button->isEnabled()) {
            state->phase = 1;
            QTimer::singleShot(100, &window, [] {
              for (auto* widget : QApplication::topLevelWidgets()) {
                auto* message = qobject_cast<QMessageBox*>(widget);
                if (!message || !message->isVisible()) continue;
                if (auto* yes = message->button(QMessageBox::Yes)) yes->click();
                return;
              }
              qApp->exit(241);
            });
            button->click();
            state->phase = 2;
            return;
          }
        }
      }
      if (state->phase == 2 && state->posts > 0 && window._recoveryCommandPending) {
        (*recoveryDialog)->close();
        state->phase = 3;
        if (window._campaignStart->isEnabled()) { qApp->exit(242); return; }
      }
      if (state->phase == 3 && !window._recoveryCommandPending && window._recoveryWorkerRunning) {
        if (window._campaignStart->isEnabled() || !window._recoveryWorkerWatch) { qApp->exit(243); return; }
        window._recoveryWorkerWatch->setInterval(20);
        state->phase = 4;
      }
      if (state->phase == 4 && state->watchReads >= 2 && !window._recoveryWorkerRunning) {
        if (!window._campaignStart->isEnabled()) { qApp->exit(244); return; }
        monitor->stop();
        qApp->exit(0);
      }
    });
    monitor->start(10);
    QTimer::singleShot(7000, &window, [] { qApp->exit(245); });
  }
