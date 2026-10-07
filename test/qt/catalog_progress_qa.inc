  static void catalogProgressSmoke(MainWindow& window) {
    auto* server = new QTcpServer(&window);
    if (!server->listen(QHostAddress::LocalHost)) { qApp->exit(250); return; }
    auto requests = std::make_shared<int>(0);
    const QString job(32, QLatin1Char('d'));
    QObject::connect(server, &QTcpServer::newConnection, server, [server, requests, job] {
      auto* socket = server->nextPendingConnection();
      QObject::connect(socket, &QTcpSocket::readyRead, socket, [socket, requests, job] {
        const auto input = socket->property("input").toByteArray() + socket->readAll();
        socket->setProperty("input", input);
        if (!input.contains("\r\n\r\n") || socket->property("answered").toBool()) return;
        socket->setProperty("answered", true);
        const int number = ++*requests;
        if (!input.startsWith("GET /api/tasks?") || number > 5
            || (number > 1 && !input.contains(("job_id=" + job).toUtf8()))) {
          qApp->exit(251); return;
        }
        const int completed = number == 1 ? 0 : number <= 3 ? 1 : number == 4 ? 2 : 3;
        const auto body = QJsonDocument(QJsonObject{{"loading", true}, {"state", "running"},
            {"message", QStringLiteral("Classificando Ego4D: %1/20 vídeos concluídos").arg(completed)},
            {"phase", "ego4d_annotations"}, {"elapsed_s", 300 + number},
            {"job_id", job}, {"identity_bound", true}}).toJson(QJsonDocument::Compact);
        socket->write("HTTP/1.1 202 Accepted\r\nContent-Type: application/json\r\nContent-Length: "
            + QByteArray::number(body.size()) + "\r\nConnection: close\r\n\r\n" + body);
        socket->disconnectFromHost();
      });
      QObject::connect(socket, &QTcpSocket::disconnected, socket, &QObject::deleteLater);
    });
    window._api.setBaseUrl(QStringLiteral("http://127.0.0.1:%1").arg(server->serverPort()));
    window._pages->setCurrentIndex(3);
    {
      const QSignalBlocker accounts(window._campaignAccounts);
      window._campaignAccounts->clear();
      auto* account = new QListWidgetItem("fixture@example.invalid", window._campaignAccounts);
      account->setData(Qt::UserRole, "fixture@example.invalid"); account->setCheckState(Qt::Checked);
    }
    window._taskReload.stop(); window._backendReady = true;
    auto phase = std::make_shared<int>(0);
    auto* check = new QTimer(&window);
    QObject::connect(check, &QTimer::timeout, &window, [&window, requests, phase, job, check] {
      const auto* item = window._campaignTasks->item(0);
      if (!item || window._taskRequestPending || *requests != *phase + 1) return;
      const bool paused = item->text().contains("Acompanhamento pausado");
      if (window._taskCatalogJobId != job || window._campaignStart->isEnabled()) {
        qApp->exit(252); return;
      }
      if (*phase == 0) {
        window._taskCatalogPollCount = 249;
        window._taskCatalogIdlePollCount = 249;
      } else if (*phase == 1) {
        // Real progress passes the former 250-poll limit and resets idle budget.
        if (paused || !window._taskReload.isActive()
            || window._taskCatalogPollCount != 250 || window._taskCatalogIdlePollCount != 1) {
          qApp->exit(253); return;
        }
        window._taskCatalogIdlePollCount = 249;
      } else if (*phase == 2) {
        // Only elapsed_s changed: a stalled classification must still pause.
        if (!paused || window._taskReload.isActive()) { qApp->exit(254); return; }
        ++*phase; window.loadTasks(); return;
      } else if (*phase == 3) {
        if (paused || !window._taskReload.isActive()) { qApp->exit(255); return; }
        window._taskCatalogPollCount = 1499;
      } else {
        // Changing counters cannot remove the hard total budget.
        if (!paused || window._taskReload.isActive()) { qApp->exit(256); return; }
        check->stop();
        QTimer::singleShot(150, &window, [requests] { qApp->exit(*requests == 5 ? 0 : 257); });
        return;
      }
      ++*phase; window._taskReload.start(0);
    });
    check->start(15); window.loadTasks();
    QTimer::singleShot(10000, &window, [] { qApp->exit(258); });
  }
