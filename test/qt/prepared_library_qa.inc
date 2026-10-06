  static void preparedLibrarySmoke(MainWindow& window) {
    struct Flow {
      int phase = 0, reads = 0, refreshReads = 0, posts = 0;
      bool loadingSeen = false, durationQuerySeen = false, cacheDurationSeen = false;
      QString refreshNonce;
      std::shared_ptr<QTemporaryDir> library = std::make_shared<QTemporaryDir>();
      std::shared_ptr<QTemporaryDir> outside = std::make_shared<QTemporaryDir>();
    };
    auto flow = std::make_shared<Flow>();
    const QString priorClipboard = QApplication::clipboard()->text();
    QObject::connect(qApp, &QCoreApplication::aboutToQuit, qApp, [priorClipboard] {
      QApplication::clipboard()->setText(priorClipboard);
    });
    for (const QString& path : {flow->library->filePath("native.mp4"), flow->library->filePath("source.mp4"),
                                flow->library->filePath("sensor.csv"), flow->outside->filePath("outside.mp4")}) {
      QFile file(path); if (!file.open(QIODevice::WriteOnly)) { qApp->exit(210); return; }
      file.write("INERT LOCAL UI FIXTURE");
    }
    auto* server = new QTcpServer(&window);
    if (!server->listen(QHostAddress::LocalHost)) { qApp->exit(211); return; }
    QObject::connect(server, &QTcpServer::newConnection, server, [server, flow] {
      auto* socket = server->nextPendingConnection();
      QObject::connect(socket, &QTcpSocket::readyRead, socket, [socket, flow] {
        const auto input = socket->property("input").toByteArray() + socket->readAll();
        socket->setProperty("input", input);
        const int headerEnd = input.indexOf("\r\n\r\n");
        if (headerEnd < 0 || socket->property("answered").toBool()) return;
        int contentLength = 0;
        for (const auto& line : input.left(headerEnd).split('\n'))
          if (line.toLower().startsWith("content-length:")) contentLength = line.mid(15).trimmed().toInt();
        if (input.size() < headerEnd + 4 + contentLength) return;
        socket->setProperty("answered", true);
        const auto request = input.split(' ');
        if (request.size() < 2) { qApp->exit(212); return; }
        const QUrl url(QStringLiteral("http://fixture") + QString::fromUtf8(request.at(1)));
        const QUrlQuery query(url);
        QJsonObject payload;
        int status = 200;
        int delay = 0;
        if (url.path() == QStringLiteral("/api/library/ego4d/prepared")) {
          ++flow->reads;
          const QString search = query.queryItemValue("q");
          const int offset = query.queryItemValue("offset").toInt();
          const QString nonce = query.queryItemValue("refresh");
          if (offset > 0 && !nonce.isEmpty()) { qApp->exit(228); return; }
          if (!nonce.isEmpty()) {
            if (flow->refreshNonce != nonce) { flow->refreshNonce = nonce; flow->refreshReads = 0; }
            ++flow->refreshReads;
          }
          const bool loading = flow->reads == 1 || (!nonce.isEmpty() && flow->refreshReads == 1);
          const bool empty = search == QStringLiteral("missing");
          const QString requestedState = query.queryItemValue("state");
          const bool filtered = requestedState != QStringLiteral("all");
          const bool single = filtered || search.startsWith("race") || search == QStringLiteral("unknown") || search == QStringLiteral("digestunknown");
          const int total = empty ? 0 : single ? 1 : 55;
          QJsonArray items;
          if (!empty) for (int i = 0; i < (single ? 1 : offset ? 5 : 50); ++i) {
            auto asset = [](const QString& path) {
              return QJsonObject{{"name", QFileInfo(path).fileName()}, {"path", path}, {"present", true}, {"bytes", 22}};
            };
            const int index = filtered ? (requestedState == "partial" ? 1 : requestedState == "stale" ? 2 : requestedState == "missing" ? 3 : 0) : offset + i;
            const QString state = index == 1 ? "partial" : index == 2 ? "stale" : index == 3 ? "missing" : "ready";
            QJsonObject row{{"clip_uid", single ? search : QStringLiteral("prepared-%1").arg(index)},
                {"parent_video_uid", "source-parent"}, {"window_s", QJsonArray{100, 291.117}},
                {"duration_ms", 191117}, {"cache_state", state}, {"task_name", QJsonValue::Null},
                {"protected", index == 0}, {"protection_reasons", QJsonArray{"Pending delivery"}},
                {"reasons", QJsonArray{}}, {"source", asset(flow->library->filePath("source.mp4"))},
                {"native", asset(flow->library->filePath("native.mp4"))}, {"imu", asset(flow->library->filePath("sensor.csv"))}};
            if (index == 1) row["imu"] = QJsonObject{{"path", flow->library->filePath("missing.csv")}, {"present", false}, {"bytes", 0}};
            if (index == 2) row["source"] = asset(flow->outside->filePath("outside.mp4"));
            if (index == 3) { row["imu"] = QJsonValue::Null; row["source"] = QJsonValue::Null; row["native"] = QJsonValue::Null; row["duration_ms"] = QJsonValue::Null; }
            if (search == QStringLiteral("digestunknown")) row["protection_reasons"] = QJsonArray{"digest_protection_unverified"};
            items.append(row);
          }
          payload = {{"schema", 1}, {"provider", "ego4d"}, {"inventory_scope", "local_prepared_media"},
              {"library_root", flow->library->path()}, {"state", loading ? "loading" : "ready"},
              {"refreshing", loading}, {"verified_at", 1791241200}, {"items", items}, {"total", total}, {"offset", offset},
              {"protection_status", search == QStringLiteral("unknown") ? "unknown" : "known"},
              {"counts", QJsonObject{{"ready", 52}, {"partial", 1}, {"missing", 1}, {"stale", 1}, {"protected", 1}}}};
          if (loading) {
            flow->loadingSeen = true;
            status = 202;
            payload = {{"loading", true}, {"state", "running"}, {"message", "Verificando arquivos locais…"}};
          }
          if (search == QStringLiteral("broken")) payload = {};
          if (search == QStringLiteral("invalidrow")) payload["items"] = QJsonArray{QJsonObject{{"clip_uid", "invalid"}}};
          if (search == QStringLiteral("race-old")) delay = 450;
          if (query.queryItemValue("min_s") == "180" && query.queryItemValue("max_s") == "780") flow->durationQuerySeen = true;
        } else if (url.path() == QStringLiteral("/api/holo-cache/start")) {
          ++flow->posts;
          const auto body = QJsonDocument::fromJson(input.mid(headerEnd + 4)).object();
          if (body.value("min_dur_s").toInt() != 180 || body.value("max_dur_s").toInt() != 780) { qApp->exit(213); return; }
          payload = {{"ok", true}, {"runner", QJsonObject{{"state", "running"}}}};
        } else if (url.path() == QStringLiteral("/api/holo-cache")) {
          if (query.queryItemValue("min_dur_s") == "180" && query.queryItemValue("max_dur_s") == "780") flow->cacheDurationSeen = true;
          payload = {{"tasks", QJsonArray{"Gardening"}}, {"default_task", "Gardening"},
              {"cache", QJsonObject{{"task", "Gardening"}, {"total", 2}, {"ready", 2}, {"pending", 0}}},
              {"runner", QJsonObject{{"state", "idle"}, {"provider", "ego4d"}}}};
        } else if (url.path() == QStringLiteral("/api/storage/library/items")) {
          payload = {{"schema", 1}, {"inventory_scope", "local_media_files"}, {"state", "ready"},
              {"library_root", flow->library->path()}, {"file_count", 0}, {"total_bytes", 0},
              {"total", 0}, {"offset", 0}, {"items", QJsonArray{}}};
        } else if (url.path() == QStringLiteral("/api/storage/library")) {
          payload = {{"free_bytes", 53687091200.0}};
        } else { qApp->exit(214); return; }
        const auto bytes = QJsonDocument(payload).toJson(QJsonDocument::Compact);
        const QPointer<QTcpSocket> guard(socket);
        QTimer::singleShot(delay, socket, [guard, bytes, status] {
          if (!guard) return;
          guard->write("HTTP/1.1 " + QByteArray::number(status) + " OK\r\nContent-Type: application/json\r\nConnection: close\r\nContent-Length: "
                       + QByteArray::number(bytes.size()) + "\r\n\r\n" + bytes);
          guard->disconnectFromHost();
        });
      });
      QObject::connect(socket, &QTcpSocket::disconnected, socket, &QObject::deleteLater);
    });
    window._api.setBaseUrl(QStringLiteral("http://127.0.0.1:%1").arg(server->serverPort()));
    window._pages->setCurrentIndex(4);
    window._libraryTabs->setCurrentIndex(1);
    window.loadPreparedLibrary();
    auto* poll = new QTimer(&window);
    QObject::connect(poll, &QTimer::timeout, &window, [&window, flow, poll] {
      const auto fail = [](int code) { qApp->exit(code); };
      if (flow->phase == 0 && window._preparedVerified && window._preparedTable->rowCount() == 50) {
        window._preparedTable->setCurrentCell(0, 2);
        if (!flow->loadingSeen || window._preparedTable->item(0,0)->text() != QStringLiteral("Mídia preparada · Em uso")
            || window._preparedTable->item(1,0)->text() != QStringLiteral("Preparação incompleta")
            || window._preparedTable->item(2,0)->text() != QStringLiteral("Revisar")
            || window._preparedTable->item(3,0)->text() != QStringLiteral("Arquivo ausente")
            || window._preparedTable->item(0,1)->text() != QStringLiteral("3:11")
            || !window._preparedOpenNative->isEnabled() || !window._preparedOpenSource->isEnabled()) { fail(215); return; }
        window._preparedCopy->click();
        if (QApplication::clipboard()->text() != QStringLiteral("prepared-0")) { fail(216); return; }
        window._preparedTable->setCurrentCell(2,2);
        if (window._preparedOpenSource->isEnabled()) { fail(217); return; }
        if (qApp->arguments().contains("--prepared-library-preview")) {
          window._preparedTable->setCurrentCell(0,2);
          window.resize(qApp->arguments().contains("--compact") ? 1000 : 1440, 1000);
          window.grab().save(QStringLiteral("build/library-ui/prepared-library-preview%1.png")
              .arg(qApp->arguments().contains("--compact") ? "-compact" : ""));
        }
        flow->phase = 1; window._preparedNext->click();
      } else if (flow->phase == 1 && window._preparedVerified && window._preparedTable->rowCount() == 5) {
        if (window._preparedTable->item(0,2)->text() != QStringLiteral("prepared-50") || !window._preparedPrevious->isEnabled()) { fail(218); return; }
        flow->phase = 2; window._preparedQuery->setText("missing");
      } else if (flow->phase == 2 && window._preparedVerified && !window._preparedTable->rowCount()) {
        if (!window._preparedCount->text().contains("Nenhum") || window._preparedCopy->isEnabled()) { fail(219); return; }
        flow->phase = 3; window._preparedQuery->setText("broken");
      } else if (flow->phase == 3 && window._preparedSummary->text().contains("incompleta")) {
        if (window._preparedVerified || window._preparedTable->rowCount() || window._preparedOpenNative->isEnabled()) { fail(220); return; }
        flow->phase = 4; window._preparedQuery->setText("invalidrow");
      } else if (flow->phase == 4 && window._preparedSummary->text().contains("incompletos")) {
        flow->phase = 5; window._preparedQuery->setText("race-old");
        QTimer::singleShot(40, &window, [&window] { window._preparedQuery->setText("race-current"); });
      } else if (flow->phase == 5 && window._preparedVerified && window._preparedTable->rowCount() == 1) {
        if (window._preparedTable->item(0,2)->text() != QStringLiteral("race-current")) { fail(221); return; }
        flow->phase = 6;
        QTimer::singleShot(600, &window, [&window, flow] {
          if (window._preparedTable->item(0,2)->text() != QStringLiteral("race-current")) { qApp->exit(222); return; }
          flow->phase = 7; window._preparedRefresh->click();
        });
      } else if (flow->phase == 7 && window._preparedVerified && flow->refreshReads >= 2) {
        if (flow->refreshNonce.isEmpty()) { fail(223); return; }
        flow->phase = 17; window._preparedQuery->clear();
      } else if (flow->phase == 17 && window._preparedVerified && window._preparedTable->rowCount() == 50) {
        flow->phase = 19; window._preparedRefresh->click();
      } else if (flow->phase == 19 && window._preparedVerified && flow->refreshReads >= 2) {
        flow->phase = 20; window._preparedNext->click();
      } else if (flow->phase == 20 && window._preparedVerified && window._preparedTable->rowCount() == 5) {
        flow->phase = 8; window._preparedQuery->setText("unknown");
      } else if (flow->phase == 8 && window._preparedVerified) {
        if (!window._preparedSummary->text().contains("não pôde ser confirmada")
            || !window._preparedTable->item(0,0)->text().contains("Preservado")) { fail(224); return; }
        flow->phase = 18; window._preparedQuery->setText("digestunknown");
      } else if (flow->phase == 18 && window._preparedVerified) {
        if (!window._preparedTable->item(0,0)->text().contains("Preservado")) { fail(229); return; }
        flow->phase = 9; window._preparedMinimum->setValue(3); window._preparedMaximum->setValue(2);
        if (window._preparedVerified || !window._preparedSummary->text().contains("máxima")) { fail(225); return; }
        window._preparedMaximum->setValue(13);
      } else if (flow->phase == 9 && window._preparedVerified && flow->durationQuerySeen) {
        flow->phase = 21; window._preparedState->setCurrentIndex(2);
      } else if (flow->phase == 21 && window._preparedVerified) {
        if (window._preparedTable->rowCount() != 1
            || window._preparedTable->item(0,0)->text() != QStringLiteral("Preparação incompleta")) { fail(230); return; }
        flow->phase = 10; window._cacheMinimum->setValue(3); window._cacheMaximum->setValue(13);
      } else if (flow->phase == 10 && flow->cacheDurationSeen && window._cacheTask->count() && window._cacheStart->isEnabled()) {
        flow->phase = 11; window._cacheStart->click(); window._cacheStart->click();
      } else if (flow->phase == 11 && flow->posts) {
        poll->stop(); qApp->exit(flow->posts == 1 ? 0 : 226);
      }
    });
    poll->start(25);
    QTimer::singleShot(12000, &window, [] { qApp->exit(227); });
  }
