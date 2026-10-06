  static void localMediaLibrarySmoke(MainWindow& window) {
    // Only inert files and loopback API replies. No player, backend or campaign is started.
    struct Flow {
      int phase = 0, reads = 0, nonceReads = 0, cleanupCalls = 0, preparedRefreshes = 0;
      QString nonce;
      bool loadingSeen = false, filteredSeen = false;
      std::shared_ptr<QTemporaryDir> root = std::make_shared<QTemporaryDir>();
      std::shared_ptr<QTemporaryDir> outside = std::make_shared<QTemporaryDir>();
    };
    auto flow = std::make_shared<Flow>();
    const QString priorClipboard = QApplication::clipboard()->text();
    QObject::connect(qApp, &QCoreApplication::aboutToQuit, qApp, [priorClipboard] {
      QApplication::clipboard()->setText(priorClipboard);
    });
    const QStringList names{"original.mp4", "holoassist_native.mp4", "imu.csv", "marker.source.json",
        "catalog.jsonl", "frames.npy", "local.mov", "unsupported.vrs"};
    for (const QString& name : names) {
      QFile file(flow->root->filePath(name));
      if (!file.open(QIODevice::WriteOnly)) { qApp->exit(230); return; }
      file.write("INERT LOCAL UI FIXTURE");
    }
    QFile outside(flow->outside->filePath("outside.mp4"));
    if (!outside.open(QIODevice::WriteOnly)) { qApp->exit(231); return; }
    outside.write("INERT OUTSIDE FIXTURE"); outside.close();
    auto* server = new QTcpServer(&window);
    if (!server->listen(QHostAddress::LocalHost)) { qApp->exit(232); return; }
    QObject::connect(server, &QTcpServer::newConnection, server, [server, flow, names] {
      auto* socket = server->nextPendingConnection();
      QObject::connect(socket, &QTcpSocket::readyRead, socket, [socket, flow, names] {
        const auto input = socket->property("input").toByteArray() + socket->readAll();
        socket->setProperty("input", input);
        const int headerEnd = input.indexOf("\r\n\r\n");
        if (headerEnd < 0 || socket->property("answered").toBool()) return;
        int length = 0;
        for (const auto& line : input.left(headerEnd).split('\n'))
          if (line.toLower().startsWith("content-length:")) length = line.mid(15).trimmed().toInt();
        if (input.size() < headerEnd + 4 + length) return;
        socket->setProperty("answered", true);
        const auto request = input.split(' ');
        if (request.size() < 2) { qApp->exit(233); return; }
        const QUrl url(QStringLiteral("http://fixture") + QString::fromUtf8(request.at(1)));
        const QUrlQuery query(url);
        QJsonObject payload;
        int status = 200, delay = 0;
        if (url.path() == QStringLiteral("/api/storage/library/items")) {
          ++flow->reads;
          const QString search = query.queryItemValue("q");
          const QString nonce = query.queryItemValue("refresh");
          const int offset = query.queryItemValue("offset").toInt();
          if (query.queryItemValue("async") != "1" || query.queryItemValue("limit") != "50"
              || (offset > 0 && !nonce.isEmpty())) { qApp->exit(234); return; }
          if (!nonce.isEmpty()) {
            if (flow->nonce != nonce) { flow->nonce = nonce; flow->nonceReads = 0; }
            ++flow->nonceReads;
          }
          const bool loading = flow->reads == 1 || search == "hidden" || (!nonce.isEmpty() && flow->nonceReads == 1);
          const bool empty = search == "empty";
          const bool filter = query.queryItemValue("provider") != "all" || query.queryItemValue("kind") != "all";
          if (filter && query.queryItemValue("provider") == "holoassist" && query.queryItemValue("kind") == "video")
            flow->filteredSeen = true;
          const bool single = !search.isEmpty() || filter;
          const int total = empty ? 0 : single ? 1 : 55;
          QJsonArray items;
          for (int i = 0; !empty && i < (single ? 1 : offset ? 5 : 50); ++i) {
            const int index = filter ? 1 : (offset + i) % names.size();
            const QString name = single && !filter ? search + ".mp4" : names.at(index);
            const QString fileName = filter ? names.at(1) : names.at(index);
            const QString provider = index == 1 ? "holoassist" : index == 7 ? "nymeria" : index >= 4 ? "local" : "ego4d";
            const QString kind = index == 2 ? "sensor" : index == 3 ? "sidecar" : index == 4 ? "catalog" : index == 5 ? "derivative" : "video";
            QJsonObject item{{"name", name}, {"relative_path", fileName}, {"path", flow->root->filePath(fileName)},
                {"provider", provider}, {"kind", kind}, {"stage", index == 1 ? "normalizado" : kind == "video" ? "baixado/original" : kind == "sensor" ? "sensores" : "arquivo auxiliar"},
                {"size_bytes", index == 0 ? 8589934592.0 : index == 1 ? 2048 : index == 6 ? 1048576 : 22}, {"modified_at", 1791241200},
                {"protected", index == 0 || index == 2},
                {"protection_reasons", index == 2 ? QJsonArray{"protection_state_unknown"} : index == 0 ? QJsonArray{"pending_delivery"} : QJsonArray{}}};
            if (search == "outside") item["path"] = flow->outside->filePath("outside.mp4");
            if (search == "gone") item["path"] = flow->root->filePath("gone.mp4");
            if (search == "null") item["size_bytes"] = QJsonValue::Null;
            items.append(item);
          }
          payload = {{"schema", 1}, {"inventory_scope", "local_media_files"}, {"state", "ready"},
              {"library_root", flow->root->path()}, {"total", total}, {"offset", offset}, {"limit", 50},
              {"file_count", 55}, {"total_bytes", 8589936000.0}, {"disk_free_bytes", 53687091200.0},
              {"verified_at", 1791241200}, {"by_provider", QJsonObject{}}, {"items", items}};
          if (loading) {
            flow->loadingSeen = true; status = 202;
            payload = {{"loading", true}, {"state", "running"}, {"message", "Verificando arquivos locais…"}};
          }
          if (search == "broken") payload = {};
          if (search == "partialscan") { payload["scan_complete"] = false; payload["scan_errors"] = 1; }
          if (search == "race-old") delay = 450;
        } else if (url.path() == QStringLiteral("/api/library/ego4d/prepared")) {
          if (!query.queryItemValue("refresh").isEmpty()) ++flow->preparedRefreshes;
          payload = {{"schema", 1}, {"provider", "ego4d"}, {"inventory_scope", "local_prepared_media"},
              {"state", "ready"}, {"library_root", flow->root->path()}, {"total", 0}, {"offset", 0},
              {"items", QJsonArray{}}, {"verified_at", 1791241200}, {"protection_status", "verified"},
              {"counts", QJsonObject{{"ready", 0}, {"partial", 0}, {"missing", 0}, {"stale", 0}, {"protected", 0}}}};
        } else if (url.path() == QStringLiteral("/api/storage/cleanup")) {
          const auto body = QJsonDocument::fromJson(input.mid(headerEnd + 4)).object();
          if (body.value("provider").toString() != "holoassist") { qApp->exit(235); return; }
          ++flow->cleanupCalls; payload = {{"files", 0}, {"bytes", 0}};
        } else if (url.path() == QStringLiteral("/api/holo-cache")) {
          payload = {{"tasks", QJsonArray{"Gardening"}}, {"default_task", "Gardening"},
              {"cache", QJsonObject{{"total", 0}, {"ready", 0}, {"pending", 0}}},
              {"runner", QJsonObject{{"state", "idle"}, {"provider", "ego4d"}}}};
        } else { qApp->exit(236); return; }
        const auto bytes = QJsonDocument(payload).toJson(QJsonDocument::Compact);
        const QPointer<QTcpSocket> guard(socket);
        QTimer::singleShot(delay, socket, [guard, status, bytes] {
          if (!guard) return;
          guard->write("HTTP/1.1 " + QByteArray::number(status) + " Fixture\r\nContent-Type: application/json\r\nConnection: close\r\nContent-Length: "
              + QByteArray::number(bytes.size()) + "\r\n\r\n" + bytes);
          guard->disconnectFromHost();
        });
      });
      QObject::connect(socket, &QTcpSocket::disconnected, socket, &QObject::deleteLater);
    });
    window._api.setBaseUrl(QStringLiteral("http://127.0.0.1:%1").arg(server->serverPort()));
    window._pages->setCurrentIndex(4);
    if (window._libraryTabs->currentIndex() != 0) { qApp->exit(237); return; }
    window.loadLocalMediaLibrary();
    auto* poll = new QTimer(&window);
    QObject::connect(poll, &QTimer::timeout, &window, [&window, flow, poll] {
      const auto fail = [](int code) { qApp->exit(code); };
      if (flow->phase == 0 && window._localMediaVerified && window._localMediaTable->rowCount() == 50) {
        window._localMediaTable->setCurrentCell(0,0);
        if (!flow->loadingSeen || !window._localMediaOpen->isEnabled() || !window._libraryOpenFolder->isEnabled()
            || !window._libraryStorageSummary->text().contains("55 arquivos")
            || !window._localMediaTable->item(0,2)->text().contains("baixado/original")
            || !window._localMediaTable->item(1,2)->text().contains("normalizado")
            || !window._localMediaTable->item(0,3)->text().contains("GiB")
            || !window._localMediaTable->item(1,3)->text().contains("KiB")
            || window._localMediaTable->item(2,3)->text() != "22 B"
            || window._localMediaTable->item(2,3)->toolTip() != "22 bytes"
            || !window._localMediaTable->item(6,3)->text().contains("MiB")
            || window._localMediaTable->item(0,4)->text() != "Em uso"
            || window._localMediaTable->item(2,4)->text() != "Preservado") { fail(238); return; }
        window._localMediaCopy->click();
        if (QApplication::clipboard()->text() != QFileInfo(flow->root->filePath("original.mp4")).canonicalFilePath()) { fail(239); return; }
        for (int row : {2,3,4,5,7}) {
          window._localMediaTable->setCurrentCell(row,0);
          if (window._localMediaOpen->isEnabled() || !window._localMediaCopy->isEnabled()) { fail(240); return; }
        }
        if (qApp->arguments().contains("--local-media-library-preview")) {
          window._localMediaTable->setCurrentCell(0,0);
          window.resize(1440,1000);
          window.grab().save(QStringLiteral("build/library-ui/local-media-library-preview.png"));
        }
        flow->phase = 1; window._localMediaRefresh->click();
      } else if (flow->phase == 1 && window._localMediaVerified && flow->nonceReads >= 2) {
        flow->phase = 2; window._localMediaNext->click();
      } else if (flow->phase == 2 && window._localMediaVerified && window._localMediaTable->rowCount() == 5) {
        if (!window._localMediaRefreshNonce.isEmpty() || !window._localMediaPrevious->isEnabled()) { fail(241); return; }
        flow->phase = 3; window._localMediaProvider->setCurrentIndex(2); window._localMediaKind->setCurrentIndex(1);
      } else if (flow->phase == 3 && window._localMediaVerified && flow->filteredSeen) {
        if (window._localMediaTable->rowCount() != 1 || window._localMediaTable->item(0,1)->text() != "HoloAssist"
            || window._localMediaOffset != 0) { fail(242); return; }
        flow->phase = 4; window._localMediaProvider->setCurrentIndex(0); window._localMediaKind->setCurrentIndex(0);
        window._localMediaQuery->setText("outside");
      } else if (flow->phase == 4 && window._localMediaVerified && window._localMediaTable->rowCount() == 1) {
        window._localMediaTable->setCurrentCell(0,0);
        if (window._localMediaOpen->isEnabled() || window._localMediaCopy->isEnabled()) { fail(243); return; }
        flow->phase = 5; window._localMediaQuery->setText("gone");
      } else if (flow->phase == 5 && window._localMediaVerified) {
        window._localMediaTable->setCurrentCell(0,0);
        if (window._localMediaOpen->isEnabled() || window._localMediaCopy->isEnabled()) { fail(244); return; }
        flow->phase = 6; window._localMediaQuery->setText("empty");
      } else if (flow->phase == 6 && window._localMediaVerified && !window._localMediaTable->rowCount()) {
        if (!window._localMediaCount->text().contains("Nenhum") || window._localMediaOpen->isEnabled()) { fail(245); return; }
        flow->phase = 18; window._localMediaOffset = 50; window.loadLocalMediaLibrary();
      } else if (flow->phase == 18 && window._localMediaVerified && !window._localMediaTable->rowCount()) {
        if (!window._localMediaPrevious->isEnabled()) { fail(254); return; }
        flow->phase = 7; window._localMediaQuery->setText("null");
      } else if (flow->phase == 7 && !window._localMediaVerified && window._localMediaSummary->text().contains("incompleta")) {
        if (window._localMediaTable->rowCount() || window._libraryOpenFolder->isEnabled()) { fail(246); return; }
        flow->phase = 8; window._localMediaQuery->setText("broken");
        window._localMediaSummary->setText("Aguardando resposta inválida da fixture");
      } else if (flow->phase == 8 && !window._localMediaVerified && window._localMediaSummary->text().contains("incompleta")) {
        flow->phase = 9; window._localMediaQuery->setText("race-old");
        QTimer::singleShot(40, &window, [&window] { window._localMediaQuery->setText("race-current"); });
      } else if (flow->phase == 9 && window._localMediaVerified && window._localMediaTable->rowCount() == 1) {
        if (window._localMediaTable->item(0,0)->text() != "race-current.mp4") { fail(247); return; }
        flow->phase = 10;
        QTimer::singleShot(600, &window, [&window, flow] {
          if (window._localMediaTable->item(0,0)->text() != "race-current.mp4") { qApp->exit(248); return; }
          flow->phase = 11; window._localMediaQuery->setText("hidden");
        });
      } else if (flow->phase == 11 && window._localMediaPoll.isActive()) {
        flow->phase = 12; window._libraryTabs->setCurrentIndex(1);
        const int reads = flow->reads;
        QTimer::singleShot(1350, &window, [&window, flow, reads] {
          if (window._localMediaPoll.isActive() || flow->reads != reads) { qApp->exit(249); return; }
          window._libraryTabs->setCurrentIndex(0);
          flow->phase = 13; window._localMediaQuery->setText("race-current");
        });
      } else if (flow->phase == 13 && window._localMediaVerified) {
        flow->phase = 14; window._localMediaQuery->clear();
      } else if (flow->phase == 14 && window._localMediaVerified && window._localMediaTable->rowCount() == 50) {
        flow->phase = 15;
        window.findChild<QComboBox*>(QStringLiteral("cacheCleanupProvider"))->setCurrentIndex(1);
        QTimer::singleShot(0, &window, [] {
          if (auto* message = qobject_cast<QMessageBox*>(QApplication::activeModalWidget()))
            if (auto* yes = message->button(QMessageBox::Yes)) yes->click();
        });
        window.findChild<QPushButton*>(QStringLiteral("cacheCleanupButton"))->click();
      } else if (flow->phase == 15 && flow->cleanupCalls && window._localMediaVerified && flow->nonceReads >= 2) {
        if (flow->preparedRefreshes || !window._preparedRefreshPending) { fail(252); return; }
        flow->phase = 16; window._libraryTabs->setCurrentIndex(1);
      } else if (flow->phase == 16 && flow->preparedRefreshes && window._preparedVerified) {
        flow->phase = 17; window._libraryTabs->setCurrentIndex(0); window._localMediaQuery->setText("partialscan");
      } else if (flow->phase == 17 && window._localMediaVerified && window._localMediaTable->rowCount() == 1) {
        if (!window._localMediaSummary->text().contains("Leitura incompleta")) { fail(253); return; }
        poll->stop(); qApp->exit(flow->cleanupCalls == 1 && !QApplication::activeModalWidget() ? 0 : 250);
      }
    });
    poll->start(25);
    QTimer::singleShot(15000, &window, [&window, flow] {
      QFile report(QStringLiteral("build/library-ui/local-media-smoke-timeout.json"));
      if (report.open(QIODevice::WriteOnly)) report.write(QJsonDocument(QJsonObject{
          {"phase", flow->phase}, {"reads", flow->reads}, {"cleanup_calls", flow->cleanupCalls},
          {"prepared_refreshes", flow->preparedRefreshes}, {"verified", window._localMediaVerified},
          {"rows", window._localMediaTable->rowCount()}, {"summary", window._localMediaSummary->text()}}).toJson());
      qApp->exit(251);
    });
  }
