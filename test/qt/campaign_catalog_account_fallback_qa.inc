  static void campaignCatalogAccountFallbackSmoke(MainWindow& window, bool permanentRemoval,
                                                   bool unrelatedError = false,
                                                   bool twoPermanentRemovals = false,
                                                   bool archiveFailure = false) {
    struct Flow {
      int taskRequests{};
      int accountRefreshes{};
      int accountRefreshReplies{};
      int otherRequests{};
      QStringList taskEmails;
    };
    const QString first = QStringLiteral("ana@example.invalid");
    const QString second = QStringLiteral("karina@example.invalid");
    const QString third = QStringLiteral("bia@example.invalid");
    const QString fourth = QStringLiteral("clara@example.invalid");
    auto flow = std::make_shared<Flow>();
    auto reloadStarted = std::make_shared<bool>(false);
    auto* server = new QTcpServer(&window);
    if (!server->listen(QHostAddress::LocalHost)) { qApp->exit(270); return; }
    QObject::connect(server, &QTcpServer::newConnection, server, [server, flow, first, second, third, fourth,
                                                                  permanentRemoval, unrelatedError, twoPermanentRemovals,
                                                                  archiveFailure] {
      auto* socket = server->nextPendingConnection();
      QObject::connect(socket, &QTcpSocket::readyRead, socket,
          [socket, flow, first, second, third, fourth, permanentRemoval, unrelatedError,
           twoPermanentRemovals, archiveFailure] {
        const auto input = socket->property("input").toByteArray() + socket->readAll();
        socket->setProperty("input", input);
        if (!input.contains("\r\n\r\n") || socket->property("answered").toBool()) return;
        socket->setProperty("answered", true);
        const auto requestLine = input.left(input.indexOf("\r\n")).split(' ');
        if (requestLine.size() < 2) { qApp->exit(271); return; }
        const QString method = QString::fromUtf8(requestLine[0]);
        const QUrl url(QStringLiteral("http://127.0.0.1") + QString::fromUtf8(requestLine[1]));
        QByteArray status = "200 OK";
        QJsonObject payload;
        int delayMs = 10;
        if (method == QStringLiteral("GET") && url.path() == QStringLiteral("/api/tasks")) {
          const QUrlQuery query(url);
          const QString requested = query.queryItemValue(QStringLiteral("email"), QUrl::FullyDecoded);
          flow->taskEmails.append(requested);
          const int number = ++flow->taskRequests;
          if (unrelatedError) {
            if (number != 1 || requested != first) { qApp->exit(272); return; }
            status = "400 Error";
            payload = {{"code", "dataset_prepare_failed"}, {"error", "fixture local failure"}};
          } else if (number <= (twoPermanentRemovals ? 2 : permanentRemoval || archiveFailure ? 1 : 2)) {
            const QString expected = number == 1 ? first : second;
            if (requested != expected) { qApp->exit(273); return; }
            status = "400 Error";
            payload = {{"code", "catalog_account_unavailable"},
                {"error", "fixture remote account failure"},
                {"issue", QJsonObject{{"email", requested},
                    {"code", archiveFailure ? "restricted" : "authentication"},
                    {"stage", "Carregamento remoto de categorias"},
                    {"restriction_confirmed", archiveFailure}, {"reason", "fixture"}, {"action", "fixture"}}}};
            if ((permanentRemoval && number == 1) || twoPermanentRemovals)
              payload.insert("permanently_removed", true);
            if (archiveFailure) {
              payload.insert("removal_failed", true);
              payload.insert("archive_issue", QJsonObject{{"code", "local_archive_failed"},
                  {"stage", "Arquivamento local"},
                  {"reason", "O arquivo local da restrição não foi concluído."},
                  {"detail", "OSError"}});
            }
          } else {
            const QString expected = twoPermanentRemovals ? third
                : permanentRemoval || archiveFailure ? second : number == 3 ? third : first;
            if (requested != expected) { qApp->exit(274); return; }
            if (!permanentRemoval && number == 4 && query.queryItemValue(QStringLiteral("refresh")) != QStringLiteral("1")) {
              qApp->exit(275); return;
            }
            payload = {{"tasks", QJsonArray{QJsonObject{{"id", "garden"}, {"name", "Gardening"},
                {"clip_count", 3}, {"available_for_duration", true}}}}};
          }
        } else if (method == QStringLiteral("GET") && url.path() == QStringLiteral("/api/accounts")) {
          ++flow->accountRefreshes;
          if (!(permanentRemoval || twoPermanentRemovals)
              || flow->accountRefreshes > (twoPermanentRemovals ? 2 : 1)) { qApp->exit(276); return; }
          if (twoPermanentRemovals && flow->accountRefreshes == 1) {
            payload = {{"accounts", QJsonArray{QJsonObject{{"email", second}},
                QJsonObject{{"email", third}}, QJsonObject{{"email", fourth}}}}};
            delayMs = 300;
          } else if (twoPermanentRemovals) {
            payload = {{"accounts", QJsonArray{QJsonObject{{"email", third}},
                QJsonObject{{"email", fourth}}}}};
          } else {
            payload = {{"accounts", QJsonArray{QJsonObject{{"email", second}},
                QJsonObject{{"email", third}}}}};
          }
        } else {
          ++flow->otherRequests;
          qApp->exit(277); return;
        }
        const QByteArray body = QJsonDocument(payload).toJson(QJsonDocument::Compact);
        const QByteArray response = "HTTP/1.1 " + status
            + "\r\nContent-Type: application/json\r\nConnection: close\r\nContent-Length: "
            + QByteArray::number(body.size()) + "\r\n\r\n" + body;
        QTimer::singleShot(delayMs, socket, [socket, response, flow,
                                             accountRefresh = method == QStringLiteral("GET")
                                                 && url.path() == QStringLiteral("/api/accounts")] {
          socket->write(response);
          if (accountRefresh) ++flow->accountRefreshReplies;
          socket->disconnectFromHost();
        });
      });
      QObject::connect(socket, &QTcpSocket::disconnected, socket, &QObject::deleteLater);
    });

    window._api.setBaseUrl(QStringLiteral("http://127.0.0.1:%1").arg(server->serverPort()));
    window._backendReady = true;
    window._campaignAccountsLoaded = true;
    window._campaignDraftLoaded = false;
    window._campaignDraftAccounts.clear();
    window._campaignSelectedTaskIds.clear();
    window._campaignTaskSelectionTouched = false;
    window._taskRequestPending = false;
    window._taskReload.stop();
    window._taskCatalogSelection.clear();
    window._taskCatalogJobId.clear();
    window._taskCatalogFallbackSelection.clear();
    window._taskCatalogFallbackAnchor.clear();
    window._taskCatalogFallbackAttempted.clear();
    window._taskCatalogFallbackExhausted = false;
    window._taskCatalogForceRefresh = false;
    window._campaignActive = false;
    window._campaignPreflightPending = false;
    window._campaignStartPending = false;
    window._campaignStartUncertain = false;
    {
      const QSignalBlocker accounts(window._campaignAccounts), mode(window._campaignAccountMode);
      window._campaignAccounts->clear();
      const QStringList fixtureAccounts = twoPermanentRemovals
          ? QStringList{first, second, third, fourth} : QStringList{first, second, third};
      for (const auto& email : fixtureAccounts) {
        auto* item = new QListWidgetItem(email, window._campaignAccounts);
        item->setData(Qt::UserRole, email);
        item->setCheckState(Qt::Checked);
      }
      window._campaignAccountMode->setCurrentIndex(
          window._campaignAccountMode->findData(QStringLiteral("manual")));
    }
    window._campaignAccountCount->setValue(twoPermanentRemovals ? 4 : 3);
    window._taskRecords = {};
    window._campaignTasks->clear();

    auto* check = new QTimer(&window);
    QObject::connect(check, &QTimer::timeout, &window,
        [&window, flow, reloadStarted, first, second, third, fourth, permanentRemoval,
         unrelatedError, twoPermanentRemovals, archiveFailure, check] {
      if (unrelatedError) {
        if (flow->taskRequests != 1 || window._taskRequestPending) return;
        if (window._campaignAccounts->count() != 3 || window._campaignAccounts->item(0)->checkState() != Qt::Checked
            || window._campaignStart->isEnabled() || window._taskCatalogFallbackExhausted
            || flow->accountRefreshes || flow->otherRequests) { qApp->exit(278); return; }
        check->stop();
        QTimer::singleShot(100, &window, [flow] { qApp->exit(flow->taskRequests == 1 ? 0 : 279); });
        return;
      }
      const int expectedRequests = twoPermanentRemovals ? 3
          : permanentRemoval || archiveFailure ? 2 : 3;
      const auto* task = window._campaignTasks->count() ? window._campaignTasks->item(0) : nullptr;
      if (flow->taskRequests < expectedRequests || !task
          || task->data(Qt::UserRole).toString() != QStringLiteral("garden")) return;
      if (twoPermanentRemovals && flow->accountRefreshReplies < 2) return;
      if (!window._campaignStart->isEnabled() || window._taskRequestPending
          || window._taskCatalogFallbackExhausted || flow->otherRequests) { qApp->exit(280); return; }
      if (archiveFailure) {
        const auto* item = window._campaignAccounts->item(0);
        if (flow->taskEmails != QStringList{first, second} || flow->accountRefreshes
            || window._campaignAccounts->count() != 3 || !item
            || item->data(Qt::UserRole).toString() != first
            || item->checkState() != Qt::Checked
            || !item->toolTip().contains(QStringLiteral("arquivamento local não foi concluído"))) {
          qApp->exit(292); return;
        }
        check->stop();
        QTimer::singleShot(100, &window, [flow] { qApp->exit(flow->taskRequests == 2 ? 0 : 293); });
        return;
      } else if (twoPermanentRemovals) {
        if (flow->taskEmails != QStringList{first, second, third} || flow->accountRefreshes != 2
            || window._campaignAccounts->count() != 2
            || window._campaignAccounts->item(0)->data(Qt::UserRole).toString() != third
            || window._campaignAccounts->item(0)->checkState() != Qt::Checked
            || window._campaignAccounts->item(1)->data(Qt::UserRole).toString() != fourth
            || window._campaignAccounts->item(1)->checkState() != Qt::Checked) { qApp->exit(290); return; }
      } else if (permanentRemoval) {
        if (flow->taskEmails != QStringList{first, second} || flow->accountRefreshes != 1
            || window._campaignAccounts->count() != 2
            || window._campaignAccounts->item(0)->data(Qt::UserRole).toString() != second
            || window._campaignAccounts->item(0)->checkState() != Qt::Checked
            || window._campaignAccounts->item(1)->data(Qt::UserRole).toString() != third
            || window._campaignAccounts->item(1)->checkState() != Qt::Checked) { qApp->exit(281); return; }
      } else {
        if (!*reloadStarted) {
          if (flow->taskEmails != QStringList{first, second, third} || flow->accountRefreshes
              || window._campaignAccounts->count() != 3) { qApp->exit(282); return; }
          for (int i = 0; i < window._campaignAccounts->count(); ++i)
            if (window._campaignAccounts->item(i)->checkState() != Qt::Checked) { qApp->exit(283); return; }
          *reloadStarted = true;
          auto* reload = window.findChild<QPushButton*>(QStringLiteral("campaignReloadTasks"));
          if (!reload) { qApp->exit(284); return; }
          reload->click();
          return;
        }
        if (flow->taskRequests != 4 || flow->taskEmails != QStringList{first, second, third, first}
            || flow->accountRefreshes) { qApp->exit(287); return; }
      }
      check->stop();
      QTimer::singleShot(350, &window, [&window, flow, reloadStarted, first, second, third,
                                        permanentRemoval, twoPermanentRemovals] {
        if (permanentRemoval || twoPermanentRemovals) {
          const auto draft = QJsonDocument::fromJson(QSettings().value(QStringLiteral("campaign/draft")).toByteArray()).object();
          const auto saved = draft.value(QStringLiteral("accounts")).toArray();
          for (const auto value : saved)
            if (value.toString().compare(first, Qt::CaseInsensitive) == 0
                || (twoPermanentRemovals && value.toString().compare(second, Qt::CaseInsensitive) == 0)) {
              qApp->exit(285); return;
            }
          qApp->exit(twoPermanentRemovals
              ? flow->taskEmails == QStringList{first, second, third} && flow->accountRefreshes == 2
                    && flow->accountRefreshReplies == 2 && window._campaignAccounts->count() == 2 ? 0 : 291
              : flow->taskEmails == QStringList{first, QStringLiteral("karina@example.invalid")}
                    && flow->accountRefreshes == 1 ? 0 : 286);
        } else {
          qApp->exit(*reloadStarted && flow->taskRequests == 4 && flow->taskEmails.last() == first
              && flow->accountRefreshes == 0 ? 0 : 289);
        }
      });
    });
    check->start(15);
    window.loadTasks();
    QTimer::singleShot(10000, &window, [] { qApp->exit(288); });
  }
