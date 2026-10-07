  static void catalogTimeoutRecoverySmoke(MainWindow& window) {
    auto* server = new QTcpServer(&window);
    if (!server->listen(QHostAddress::LocalHost)) { qApp->exit(240); return; }
    auto requests = std::make_shared<int>(0);
    const QString firstJob(32, QLatin1Char('a'));
    const QString secondJob(32, QLatin1Char('b'));
    const QString thirdJob(32, QLatin1Char('c'));
    QObject::connect(server, &QTcpServer::newConnection, server,
        [server, requests, firstJob, secondJob, thirdJob] {
      auto* socket = server->nextPendingConnection();
      QObject::connect(socket, &QTcpSocket::readyRead, socket,
          [socket, requests, firstJob, secondJob, thirdJob] {
        const auto input = socket->property("input").toByteArray() + socket->readAll();
        socket->setProperty("input", input);
        if (!input.contains("\r\n\r\n") || socket->property("answered").toBool()) return;
        socket->setProperty("answered", true);
        const int number = ++*requests;
        const bool poll = number == 2 || number == 3 || number == 5 || number == 6 || number == 8;
        const QString expectedJob = number <= 3 ? firstJob : number <= 6 ? secondJob : thirdJob;
        const QByteArray requiredDuration = number <= 6 ? "min_dur_s=300" : "min_dur_s=600";
        if (!input.startsWith("GET /api/tasks?") || !input.contains("async=1")
            || !input.contains("dataset=ambos") || !input.contains("content_mode=both")
            || !input.contains(requiredDuration) || !input.contains("max_dur_s=1800")
            || (poll && !input.contains(("job_id=" + expectedJob).toUtf8()))
            || (!poll && input.contains("job_id=")) || number > 8) {
          qApp->exit(241); return;
        }
        QByteArray status = "202 Accepted";
        QJsonObject payload{{"loading", true}, {"state", "running"},
            {"message", number <= 3 ? "Contando anotações" : "Consulta para retomada"},
            {"phase", "annotations"}, {"elapsed_s", 90},
            {"job_id", expectedJob}, {"identity_bound", true}};
        if (number == 2 || number == 5) {
          status = "504 Gateway Timeout";
          payload.insert("elapsed_s", 301);
          payload.insert("code", "catalog_work_pending");
          payload.insert("error", "Tempo esperado excedido; cálculo continua no serviço.");
        } else if (number == 3 || number == 6) {
          status = "200 OK";
          payload = {{"tasks", QJsonArray{QJsonObject{{"id", "garden"}, {"name", "Gardening"},
              {"clip_count", 3}, {"available_for_duration", true}}}}};
        } else if (number == 8) {
          status = "409 Conflict";
          payload = {{"code", "catalog_identity_changed"}, {"error", "Acesso da conta mudou."}};
        }
        const auto body = QJsonDocument(payload).toJson(QJsonDocument::Compact);
        QTimer::singleShot(30, socket, [socket, status, body] {
          socket->write("HTTP/1.1 " + status + "\r\nContent-Type: application/json\r\nContent-Length: "
              + QByteArray::number(body.size()) + "\r\nConnection: close\r\n\r\n" + body);
          socket->disconnectFromHost();
        });
      });
    });
    window._api.setBaseUrl(QStringLiteral("http://127.0.0.1:%1").arg(server->serverPort()));
    window._pages->setCurrentIndex(3);
    {
      const QSignalBlocker accounts(window._campaignAccounts), dataset(window._dataset),
          mode(window._contentMode), minimum(window._minDuration), maximum(window._maxDuration),
          hours(window._targetHours), cleanup(window._cleanupAfter);
      window._campaignAccounts->clear();
      auto* account = new QListWidgetItem("fixture@example.invalid", window._campaignAccounts);
      account->setData(Qt::UserRole, "fixture@example.invalid"); account->setCheckState(Qt::Checked);
      window._dataset->setCurrentIndex(window._dataset->findData("ambos"));
      window._contentMode->setCurrentIndex(window._contentMode->findData("both"));
      window._minDuration->setValue(5); window._maxDuration->setValue(30);
      window._targetHours->setValue(8); window._cleanupAfter->setChecked(true);
    }
    window._taskReload.stop(); window._backendReady = true;
    auto phase = std::make_shared<int>(0);
    auto sawTimeout = std::make_shared<bool>(false);
    auto* check = new QTimer(&window);
    QObject::connect(check, &QTimer::timeout, &window,
        [&window, requests, phase, sawTimeout, secondJob] {
      const auto* item = window._campaignTasks->item(0);
      if (!item) return;
      if (item->text().contains("HTTP 504")) {
        *sawTimeout = true;
        if (window._campaignStart->isEnabled()) { qApp->exit(242); return; }
      }
      const bool ready = item->data(Qt::UserRole).toString() == "garden";
      if (*phase == 0 && ready) {
        if (!*sawTimeout || *requests != 3 || !window._campaignStart->isEnabled()
            || !window._taskCatalogJobId.isEmpty() || window._targetHours->value() != 8
            || !window._cleanupAfter->isChecked()) { qApp->exit(243); return; }
        *phase = 1; window.loadTasks();
      } else if (*phase == 1 && item->text().contains("Consulta para retomada")
                 && window._taskCatalogJobId == secondJob && *requests == 4) {
        // Advance the finite follow-up budget without sleeping a real minute.
        *phase = 2;
        window._taskCatalogTimedOut = true;
        window._taskCatalogTimeoutTimer.restart();
        window._taskCatalogTimeoutPollCount = 29;
        window._taskReload.start(0);
      } else if (*phase == 2 && item->text().contains("Acompanhamento pausado")) {
        if (*requests != 5 || window._taskReload.isActive() || window._taskRequestPending
            || window._campaignStart->isEnabled() || window._taskCatalogJobId != secondJob) {
          qApp->exit(244); return;
        }
        *phase = 3;
        QTimer::singleShot(150, &window, [&window, requests] {
          if (*requests != 5) { qApp->exit(245); return; }
          window.loadTasks();
        });
      } else if (*phase == 3 && ready) {
        if (*requests != 6 || !window._campaignStart->isEnabled()) { qApp->exit(246); return; }
        *phase = 4;
        { const QSignalBlocker minimum(window._minDuration); window._minDuration->setValue(10); }
        window.loadTasks();
      } else if (*phase == 4 && item->text().contains("Acesso da conta mudou")) {
        if (*requests != 8 || window._taskReload.isActive() || window._taskRequestPending
            || window._campaignStart->isEnabled() || !window._taskCatalogJobId.isEmpty()
            || !window._taskRecords.isEmpty()) { qApp->exit(247); return; }
        *phase = 5;
        QTimer::singleShot(150, &window, [requests] { qApp->exit(*requests == 8 ? 0 : 248); });
      }
    });
    check->start(15);
    window.loadTasks();
    QTimer::singleShot(15000, &window, [] { qApp->exit(249); });
  }
