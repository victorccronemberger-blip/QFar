  static void recoveryDiagnosticsSmoke(MainWindow& window) {
    QCoreApplication::setApplicationVersion(QStringLiteral("fixture-2.0.37"));
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
        if (input.startsWith("POST /api/campaigns/preflight ")) {
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
