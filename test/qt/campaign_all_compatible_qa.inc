  static void campaignAllCompatiblePreferenceSmoke(MainWindow& parent) {
    const QJsonArray accounts{"fixture-a@example.invalid","fixture-b@example.invalid"};
    const QJsonObject draft{{"accounts",accounts},{"tasks",QJsonArray{"old-task"}},{"tasks_touched",true},
        {"dataset","ego4d"},{"content_mode","both"},{"target_hours",3.5},{"min_duration",5},{"max_duration",17},
        {"mode","manual"},{"quantity",2},{"account_workers",3},{"delay_mode","fixed"},{"delay_seconds",75},
        {"cleanup",false},{"active_hours",false},{"hour_start",7},{"hour_end",18}};
    const auto pending=QJsonDocument(QJsonObject{{"schema_version",1},{"start_request_id","inert-saved-start"}}).toJson(QJsonDocument::Compact);
    QSettings settings;
    settings.setValue("campaign/draft",QJsonDocument(draft).toJson(QJsonDocument::Compact));
    settings.setValue("campaign/useAllCompatibleNextStart",true);
    settings.setValue("campaign/pendingStart",pending);
    settings.setValue("fixture/credential","inert-preserved-credential");
    settings.sync();
    auto restored=std::make_shared<MainWindow>(qobject_cast<oclero::qlementine::QlementineStyle*>(qApp->style()),nullptr,false);
    const auto migrated=QJsonDocument::fromJson(QSettings().value("campaign/draft").toByteArray()).object();
    if(QSettings().contains("campaign/useAllCompatibleNextStart") || restored->_dataset->currentData().toString()!="ambos"
        || migrated.contains("target_hours") || !migrated.value("run_until_exhausted").toBool()
        || restored->_minDuration->value()!=5 || restored->_maxDuration->value()!=17
        || restored->_campaignTaskSelectionTouched || !restored->_campaignSelectedTaskIds.isEmpty()
        || restored->_campaignDraftAccounts.size()!=2 || migrated.value("accounts")!=accounts
        || migrated.value("delay_seconds").toInt()!=75 || migrated.value("account_workers").toInt()!=3
        || QSettings().value("campaign/pendingStart").toByteArray()!=pending
        || !restored->_campaignStartUncertain || restored->_campaignRequestedPreflight!="inert-saved-start") {qApp->exit(160);return;}
    // The Library shortcut can be used before destinations have loaded.
    // It must not rewrite an empty widget as an empty saved account selection.
    restored->selectAllCompatibleTasks();
    const auto shortcutDraft=QJsonDocument::fromJson(QSettings().value("campaign/draft").toByteArray()).object();
    if(shortcutDraft.value("accounts")!=accounts || shortcutDraft.value("quantity").toInt()!=2) {qApp->exit(161);return;}
    auto reads=std::make_shared<int>(0);
    auto phase=std::make_shared<int>(0);
    auto* server=new QTcpServer(&parent);
    if(!server->listen(QHostAddress::LocalHost)) {qApp->exit(162);return;}
    QObject::connect(server,&QTcpServer::newConnection,server,[server,reads] {
      auto* socket=server->nextPendingConnection();
      QObject::connect(socket,&QTcpSocket::readyRead,socket,[socket,reads] {
        const auto input=socket->property("input").toByteArray()+socket->readAll(); socket->setProperty("input",input);
        if(!input.contains("\r\n\r\n") || socket->property("answered").toBool()) return;
        socket->setProperty("answered",true);
        if(!input.startsWith("GET /api/tasks?")) {qApp->exit(163);return;}
        const QUrl url(QStringLiteral("http://fixture")+QString::fromUtf8(input.split(' ').value(1)));
        const QUrlQuery query(url);
        if(query.queryItemValue("dataset")!="ambos" || query.queryItemValue("min_dur_s")!="300"
            || query.queryItemValue("max_dur_s")!="1020") {qApp->exit(164);return;}
        ++*reads;
        const auto result=QJsonDocument(QJsonObject{{"tasks",QJsonArray{
            QJsonObject{{"id","task-one"},{"name","First"},{"available_for_duration",true},{"clip_count",10}},
            QJsonObject{{"id","task-two"},{"name","Second"},{"available_for_duration",true},{"clip_count",10}},
            QJsonObject{{"id","task-unavailable"},{"name","Unavailable"},{"available_for_duration",false},{"clip_count",0}}}}}).toJson(QJsonDocument::Compact);
        socket->write("HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\nContent-Length: "+QByteArray::number(result.size())+"\r\n\r\n"+result);
        socket->disconnectFromHost();
      });
      QObject::connect(socket,&QTcpSocket::disconnected,socket,&QObject::deleteLater);
    });
    restored->_api.setBaseUrl(QStringLiteral("http://127.0.0.1:%1").arg(server->serverPort()));
    for(const auto& account:accounts) {
      auto* item=new QListWidgetItem(account.toString(),restored->_campaignAccounts);
      item->setData(Qt::UserRole,account.toString());item->setCheckState(Qt::Checked);
    }
    restored->_campaignAccountCount->setMaximum(2);restored->_campaignAccountCount->setValue(2);
    restored->_taskReload.stop();restored->loadTasks();
    auto* timer=new QTimer(&parent);
    QObject::connect(timer,&QTimer::timeout,&parent,[restored,reads,phase,accounts,pending] {
      if(restored->_taskRequestPending || restored->_campaignTasks->count()!=3) return;
      if(restored->_campaignTasks->item(0)->checkState()!=Qt::Checked || restored->_campaignTasks->item(1)->checkState()!=Qt::Checked
          || restored->_campaignTasks->item(2)->checkState()!=Qt::Unchecked
          || (restored->_campaignTasks->item(2)->flags() & Qt::ItemIsEnabled)) {qApp->exit(165);return;}
      if(*phase==0) {
        ++*phase;
        restored->_campaignTasks->item(0)->setCheckState(Qt::Unchecked);
        auto* all=restored->findChild<QPushButton*>("campaignSelectAllCompatible");
        if(!all) {qApp->exit(166);return;}
        restored->_backendReady=true;all->click();return;
      }
      if(*reads<2) return;
      if(restored->_campaignTaskSelectionTouched || !restored->_campaignSelectedTaskIds.isEmpty()) {qApp->exit(167);return;}
      restored->_backendReady=false;
      restored->_campaignTasks->item(1)->setCheckState(Qt::Unchecked);
      restored->_dataset->setCurrentIndex(restored->_dataset->findData("ego4d"));
      restored->_taskReload.stop();restored->_campaignDraftSave.stop();restored->saveCampaignDraft();
      MainWindow second(qobject_cast<oclero::qlementine::QlementineStyle*>(qApp->style()),nullptr,false);
      const bool preserved=second._dataset->currentData().toString()=="ego4d"
          && second._minDuration->value()==5 && second._maxDuration->value()==17 && second._delaySeconds->value()==75
          && second._accountWorkers->currentData().toInt()==3 && second._campaignDraftAccounts.size()==2
          && second._campaignTaskSelectionTouched && second._campaignSelectedTaskIds==QSet<QString>{"task-one"}
          && !QSettings().contains("campaign/useAllCompatibleNextStart")
          && QSettings().value("campaign/pendingStart").toByteArray()==pending
          && QSettings().value("fixture/credential").toString()=="inert-preserved-credential"
          && !second._campaignStart->isEnabled()
          && QJsonDocument::fromJson(QSettings().value("campaign/draft").toByteArray()).object().value("accounts")==accounts;
      qApp->exit(preserved?0:168);
    });
    timer->start(25);
    QTimer::singleShot(8000,&parent,[]{qApp->exit(169);});
  }
