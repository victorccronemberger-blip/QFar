  static void campaignResetSmoke(MainWindow& window) {
    struct Flow { int phase=0, resets=0, held=0, tasks=0; qint64 completedAt=0; };
    auto flow=std::make_shared<Flow>();
    auto* server=new QTcpServer(&window);
    if (!server->listen(QHostAddress::LocalHost)) { qApp->exit(270); return; }
    QObject::connect(server,&QTcpServer::newConnection,server,[server,flow] {
      auto* socket=server->nextPendingConnection();
      QObject::connect(socket,&QTcpSocket::readyRead,socket,[socket,flow] {
        auto input=socket->property("input").toByteArray()+socket->readAll(); socket->setProperty("input",input);
        const int end=input.indexOf("\r\n\r\n");
        if (end<0 || socket->property("answered").toBool()) return;
        const auto match=QRegularExpression("Content-Length: (\\d+)",QRegularExpression::CaseInsensitiveOption).match(QString::fromLatin1(input.left(end)));
        if (match.hasMatch() && input.size()<end+4+match.captured(1).toInt()) return;
        socket->setProperty("answered",true);
        QJsonObject result; int status=200; bool delayed=false;
        if (input.startsWith("POST /api/campaign/reset ")) {
          if (input.mid(end+4)!="{}" || !input.toLower().contains("x-qmoney-session: reset-fixture")) { qApp->exit(271);return; }
          ++flow->resets;
          if (flow->resets==1) { status=409;result={{"error","A recuperação está em andamento."}}; }
          else if (flow->resets==2) result={{"removed_files",100}}; // Missing ok is never success.
          else result={{"ok",true},{"state","idle"},{"removed_files",5},{"removed_bytes",1024}};
        } else if (input.startsWith("GET /api/tasks?")) {
          ++flow->tasks; delayed=flow->phase==4;
          result={{"tasks",QJsonArray{QJsonObject{{"id",delayed?"old-task":"fresh-task"},{"name","Fixture"},
              {"available_for_duration",true},{"clip_count",1}}}}};
        } else if (input.startsWith("GET /api/campaigns/current?")) {
          delayed=true; result={{"state","done"},{"stage","OLD"},{"current","OLD"},{"log_path","campaign_old.json"},
              {"totals",QJsonObject{{"ok_sends",41},{"failed_sends",3},{"total_sends",44}}},
              {"events",QJsonArray{QJsonObject{{"seq",100},{"title","OLD FEED"}}}}};
        } else if (input.startsWith("GET /api/logs ")) {
          delayed=true; result={{"logs",QJsonArray{QJsonObject{{"name","campaign_old.json"},{"items",41}}}}};
        } else if (input.startsWith("POST /api/logs/campaign_old.json/status ")) {
          delayed=true; result={{"summary",QJsonObject{{"total",44},{"ready",41},{"pending",3},{"unavailable",0},{"errors",0}}},
              {"delivery_summary",QJsonObject{{"confirmed",41},{"pending",1},{"review",2},{"unknown",0}}}};
        } else if (input.startsWith("GET /api/recovery?")) {
          delayed=true; result={{"items",QJsonArray{}},{"pending",3},{"confirmed",0},{"worker",QJsonObject{{"state","idle"}}}};
        } else { qApp->exit(272);return; }
        if (delayed) ++flow->held;
        const auto send=[socket,result,status] {
          const auto bytes=QJsonDocument(result).toJson(QJsonDocument::Compact);
          socket->write("HTTP/1.1 "+QByteArray::number(status)+" Test\r\nContent-Type: application/json\r\nConnection: close\r\nContent-Length: "+QByteArray::number(bytes.size())+"\r\n\r\n"+bytes);
          socket->disconnectFromHost();
        };
        if (delayed) QTimer::singleShot(1200,socket,send); else send();
      });
      QObject::connect(socket,&QTcpSocket::disconnected,socket,&QObject::deleteLater);
    });
    window._api.setBaseUrl(QStringLiteral("http://127.0.0.1:%1").arg(server->serverPort()));
    window._api.setSessionToken("reset-fixture");
    window._pages->setCurrentIndex(3);
    window._operationSnapshot={{"state","idle"}};
    window._campaignResetRunnerBusy=true;
    if (!window.campaignResetBlocked()) {qApp->exit(284);return;}
    window._campaignResetRunnerBusy=false;
    window._operationSnapshot={{"state","running"}}; // An old card cannot override a newer idle status.
    if (window.campaignResetBlocked()) {qApp->exit(285);return;}
    window._operationSnapshot={{"state","idle"}};
    for (bool* flag : {&window._campaignActive,&window._campaignStartPending,&window._campaignPreflightPending,
                       &window._recoveryCommandPending,&window._campaignStopPending}) {
      *flag=true; window.updateCampaignActions();
      if (window._campaignReset->isEnabled() || !window.campaignResetBlocked()) {qApp->exit(273);return;}
      *flag=false;
    }
    window._campaignStartUncertain=true;
    window._campaignRequestedPreflight="old-start-uuid";
    window._campaignFeed->setPlainText("saved feed");
    window._campaignCurrent->setText("saved result"); window._campaignStats->setText("41 concluídos · 3 falhas");
    window._campaignProgress->setValue(61); window._lastCampaignSeq=88;
    window._homeCampaigns->setText("14"); window._homeSuccess->setText("41 / 44");
    window._historyTable->setRowCount(1); window._historyDetail->setPlainText("saved history");
    window._historyEvidence->setRowCount(1); window._operationTable->setRowCount(1);
    window._historyVerifyPreviews->setText(QStringLiteral("Consultando o Minute…"));
    window._historyVerifyPreviews->setEnabled(false);
    window._historyVerifyPending=true;
    window._operationStages->setText("saved phases");window._operationFeed->setText("saved operation");
    window._campaignSelectedTaskIds={"old-task"}; window._campaignTaskSelectionTouched=true;
    window._campaignDraftLoaded=true;
    auto* account=new QListWidgetItem("fixture@example.invalid",window._campaignAccounts);
    account->setData(Qt::UserRole,"fixture@example.invalid"); account->setCheckState(Qt::Checked);
    window._dataset->setCurrentIndex(1);
    window._taskReload.stop();window._campaignDraftSave.stop();
    QSettings settings;
    for (const auto key : {"campaign/pendingStart","campaign/draft","campaign/accountLastUsed","previewLogName"}) settings.setValue(key,"fixture-old");
    settings.setValue("libraryRoot","fixture-library-preserved");settings.setValue("darkTheme",true);settings.sync();
    window.updateCampaignActions();
    auto* timer=new QTimer(&window);
    QObject::connect(timer,&QTimer::timeout,&window,[&window,flow] {
      if (auto* box=qobject_cast<QMessageBox*>(QApplication::activeModalWidget())) {
        if (box->windowTitle()=="Reset completo") {
          if (!box->text().contains("recibos") || !box->text().contains("pendências")
              || !box->text().contains("Biblioteca") || !box->text().contains("Minute")
              || box->defaultButton()!=box->button(QMessageBox::Cancel)) {qApp->exit(274);return;}
          if(flow->phase==0) {flow->phase=1;box->button(QMessageBox::Cancel)->click();}
          else for(auto* button:box->buttons()) if(button->text()=="Reset completo") button->click();
          return;
        }
        if (box->windowTitle()=="Reset completo não realizado") {
          if (window._campaignFeed->toPlainText()!="saved feed" || window._campaignProgress->value()!=61
              || window._historyTable->rowCount()!=1 || !QSettings().contains("campaign/pendingStart")) {qApp->exit(275);return;}
          const int next=flow->phase==2?3:4; flow->phase=next; box->accept();
          QTimer::singleShot(0,&window,[&window,flow] {
            if(flow->phase==3) window._campaignReset->click();
            else {
              window._previewLogName="campaign_old.json";
              window.pollCampaign();window.loadTasks();window.loadHistory();window.pollCampaignPreviews();window.openRecovery();
            }
          });
          return;
        }
      }
      if (flow->phase==1) {
        if(flow->resets || window._campaignFeed->toPlainText()!="saved feed") {qApp->exit(276);return;}
        flow->phase=2;QTimer::singleShot(0,&window,[&window]{window._campaignReset->click();});return;
      }
      if (flow->phase==4 && flow->held>=5) {
        flow->phase=5;QTimer::singleShot(0,&window,[&window]{window._campaignReset->click();});return;
      }
      if (flow->phase==5 && flow->resets==3 && !window._campaignResetPending && window._campaignFeed->toPlainText().isEmpty()) {
        flow->phase=6;flow->completedAt=QDateTime::currentMSecsSinceEpoch();return;
      }
      if(flow->phase==6 && QDateTime::currentMSecsSinceEpoch()-flow->completedAt>1600) {
        bool stale=false; for(auto* dialog:window.findChildren<QDialog*>()) stale|=dialog->objectName()=="campaignRecoveryDialog";
        const bool clean=window._campaignProgress->value()==0 && window._lastCampaignSeq==0
            && window._campaignFeed->toPlainText().isEmpty() && window._previewLogName.isEmpty()
            && !window._previewPoll.isActive() && !window._campaignStartUncertain && window._campaignRequestedPreflight.isEmpty()
            && window._historyTable->rowCount()==0 && window._historyEvidence->rowCount()==0 && window._historyDetail->toPlainText().isEmpty()
            && !window._historyVerifyPending && window._historyVerifyPreviews->isEnabled() && window._historyVerifyPreviews->text()=="Verificar prévias no Minute"
            && window._operationTable->rowCount()==0 && window._operationStages->text().isEmpty() && window._operationFeed->text().isEmpty()
            && window._homeCampaigns->text()=="0" && window._homeSuccess->text()=="0 / 0"
            && window._campaignPreviewPanel->isHidden() && window._campaignStats->text().contains("0 falhas")
            && window._campaignAccounts->count()==1 && window._campaignAccounts->item(0)->checkState()==Qt::Checked
            && window._dataset->currentData().toString()=="nymeria" && window._campaignTasks->count()==1
            && window._campaignTasks->item(0)->data(Qt::UserRole).toString()=="fresh-task" && !stale
            && QSettings().value("libraryRoot").toString()=="fixture-library-preserved" && QSettings().value("darkTheme").toBool();
        for(const auto key:{"campaign/pendingStart","campaign/accountLastUsed","previewLogName"})
          if(QSettings().contains(key)) {qApp->exit(277);return;}
        if(!clean) {qApp->exit(278);return;}
        // A file where QSettings needs a directory reproduces AccessError
        // without touching the user's settings or relying on Windows ACLs.
        QTemporaryDir blockedSettings;
        QFile blocker(blockedSettings.filePath(QStringLiteral("QMoneyVisualTests")));
        if(!blocker.open(QIODevice::WriteOnly)) {qApp->exit(280);return;}
        blocker.write("fixture");blocker.close();
        QSettings::setPath(QSettings::IniFormat,QSettings::UserScope,blockedSettings.path());
        QSettings failedPreferences;
        failedPreferences.setValue(QStringLiteral("campaign/pendingStart"),QStringLiteral("fixture-unsaved"));
        failedPreferences.sync();
        if(failedPreferences.status()==QSettings::NoError) {qApp->exit(283);return;}
        if(window.clearCampaignUiAfterReset() || !window._campaignStartUncertain) {qApp->exit(281);return;}
        window.updateCampaignActions();
        qApp->exit(window._campaignReset->isEnabled()?0:282);
      }
    });
    timer->start(20);window._campaignReset->click();
    QTimer::singleShot(9000,&window,[&window,flow]{
      QFile failure(QStringLiteral("build/library-ui/reset-fixture-failure.json"));
      if(failure.open(QIODevice::WriteOnly)) failure.write(QJsonDocument(QJsonObject{{"phase",flow->phase},{"resets",flow->resets},{"held",flow->held},{"pending",window._campaignResetPending},{"active",window._campaignActive},{"blocked",window.campaignResetBlocked()},{"tasks",flow->tasks},{"reset_enabled",window._campaignReset->isEnabled()},{"modal",QApplication::activeModalWidget()?QApplication::activeModalWidget()->windowTitle():QString()}}).toJson());
      qApp->exit(279);
    });
  }
