  static QJsonObject capacityFixture(const QJsonObject& request, const QStringList& emails, double available = -1) {
    const double target = request.value("target_hours").toDouble() * 3600;
    if (available < 0) available = qMax(900., target);
    const int sends = qMax(1, int(std::ceil(available / 900.)));
    QJsonArray rows;
    for (const auto& email : emails) rows.append(QJsonObject{{"email",email},{"available_seconds",available},
        {"unique_footage_upper_bound_seconds",available},{"unique_footage_seconds",available},{"estimated_executable_seconds",available},
        {"shortfall_seconds",qMax(0.,target-available)},{"eligible_clips",sends},{"estimated_sends",sends},
        {"recorded_clips",0},{"pending_clips",0}});
    return {{"schema",1},{"known",true},{"basis","estimated_admissible_delivery_duration_before_preparation"},
        {"target_seconds_per_account",target},{"can_reach_goal",available>=target},
        {"available_seconds_min",available},{"available_seconds_max",available},
        {"total_available_seconds",available*emails.size()},{"shortfall_account_count",available>=target?0:int(emails.size())},
        {"estimated_sends",sends*emails.size()},{"candidate_clips",sends},{"accounts",rows}};
  }

  static void campaignCapacitySmoke(MainWindow& window) {
    struct Flow { int phase=0, preflights=0, starts=0, previewReads=0; };
    auto flow=std::make_shared<Flow>();
    QStringList accounts; for(int i=0;i<44;++i) accounts<<QStringLiteral("fixture-%1@example.invalid").arg(i);
    auto* server=new QTcpServer(&window);
    if(!server->listen(QHostAddress::LocalHost)) {qApp->exit(255);return;}
    QObject::connect(server,&QTcpServer::newConnection,server,[server,flow,accounts] {
      auto* socket=server->nextPendingConnection();
      QObject::connect(socket,&QTcpSocket::readyRead,socket,[socket,flow,accounts] {
        const auto input=socket->property("input").toByteArray()+socket->readAll(); socket->setProperty("input",input);
        const int end=input.indexOf("\r\n\r\n");
        if(end<0 || socket->property("answered").toBool()) return;
        const auto match=QRegularExpression("Content-Length: (\\d+)",QRegularExpression::CaseInsensitiveOption).match(QString::fromLatin1(input.left(end)));
        if(match.hasMatch() && input.size()<end+4+match.captured(1).toInt()) return;
        socket->setProperty("answered",true);
        if(input.startsWith("POST /api/campaigns ")) {++flow->starts;qApp->exit(256);return;}
        if(input.startsWith("GET /api/campaigns/current?") || input.startsWith("POST /api/logs/fixture-monitor.json/status ")) {
          const bool preview=input.startsWith("POST");
          if(preview) ++flow->previewReads;
          const QJsonObject response=preview ? QJsonObject{{"attempt_status","partial"},
              {"summary",QJsonObject{{"total",44},{"ready",41},{"pending",3},{"unavailable",0},{"errors",0},{"transient_errors",0}}},
              {"delivery_summary",QJsonObject{{"confirmed",41},{"pending",1},{"review",2},{"unknown",0}}}}
              : QJsonObject{{"state","done"},{"stage","Com pendências"},
                  {"current","41 envios concluídos · 44 contas abaixo da meta · 3 falhas"},
                  {"log_path","fixture-monitor.json"},{"events",QJsonArray{}},
                  {"totals",QJsonObject{{"total_sends",44},{"done_sends",44},{"ok_sends",41},{"failed_sends",3},{"skipped_sends",0},
                      {"progress_unit","seconds"},{"progress_target",1267200},{"progress_completed",12956}}}};
          const auto bytes=QJsonDocument(response).toJson(QJsonDocument::Compact);
          socket->write("HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\nContent-Length: "+QByteArray::number(bytes.size())+"\r\n\r\n"+bytes);
          socket->disconnectFromHost(); return;
        }
        if(!input.startsWith("POST /api/campaigns/preflight?async=1&request_id=")) {qApp->exit(257);return;}
        ++flow->preflights;
        const auto request=QJsonDocument::fromJson(input.mid(end+4)).object();
        QJsonObject reply{{"ok",flow->phase!=0},{"preflight_id","fixture-capacity"},
            {"accounts",QJsonObject{{"validated",44}}},{"tasks",QJsonObject{{"compatible",10}}},
            {"clips",1},{"estimated_sends",44},{"account_workers",6},
            {"blockers",flow->phase==0?QJsonArray{"Conteúdo novo insuficiente para a meta"}:QJsonArray{}},
            {"warnings",QJsonArray{}},{"capacity",capacityFixture(request,accounts,316)}};
        if(flow->phase==2) reply.remove("capacity");
        const auto bytes=QJsonDocument(reply).toJson(QJsonDocument::Compact);
        socket->write("HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\nContent-Length: "+QByteArray::number(bytes.size())+"\r\n\r\n"+bytes);
        socket->disconnectFromHost();
      });
      QObject::connect(socket,&QTcpSocket::disconnected,socket,&QObject::deleteLater);
    });
    window._api.setBaseUrl(QStringLiteral("http://127.0.0.1:%1").arg(server->serverPort()));
    window._pages->setCurrentIndex(3);
    const QJsonObject request{{"accounts",QJsonArray::fromStringList(accounts)},{"target_hours",8},
        {"include_clip_plan",true},{"min_dur_s",300},{"max_dur_s",1800}};
    auto* timer=new QTimer(&window);
    QObject::connect(timer,&QTimer::timeout,&window,[&window,flow,accounts,request] {
      if(flow->phase==3 && flow->previewReads && window._campaignReceiptStats->text().contains("41 confirmado")) {
        if(flow->starts || flow->preflights!=3 || !window._campaignStats->text().contains("3 falhas")
            || window._campaignStage->text()!="Com pendências" || window._campaignProgress->value()!=1
            || window._campaignPreviewProgress->value()!=93 || window._campaignPreviewProgress->height()<22
            || !window._campaignReceiptStats->text().contains("2 para revisar")
            || window._campaignPreviewStats->text().contains("processando")) {qApp->exit(262);return;}
        if(qApp->arguments().contains("--campaign-capacity-preview")) {
          window.resize(1440,1000); window.grab().save(QStringLiteral("build/library-ui/campaign-monitor-preview.png"));
        }
        qApp->exit(0); return;
      }
      auto* dialog=qobject_cast<QDialog*>(QApplication::activeModalWidget());
      if(!dialog || dialog->windowTitle()!=QStringLiteral("Revisar campanha")) return;
      auto* confirm=dialog->findChild<QPushButton*>("campaignReviewConfirm");
      auto* summary=dialog->findChild<QLabel*>("campaignCapacitySummary");
      if(!confirm || confirm->isEnabled() || !summary || flow->starts) {qApp->exit(258);return;}
      if(flow->phase<2 && (!summary->text().contains("0.09") || !summary->text().contains("44 conta"))) {qApp->exit(259);return;}
      if(qApp->arguments().contains("--campaign-capacity-preview") && flow->phase==0)
        dialog->grab().save(QStringLiteral("build/library-ui/campaign-capacity-preview.png"));
      dialog->reject(); ++flow->phase;
      QTimer::singleShot(30,&window,[&window,flow,accounts,request] {
        if(flow->phase>=3) {window._campaignTabs->setCurrentIndex(2); window.pollCampaign();return;}
        window.preflightCampaign(request,accounts);
      });
    });
    timer->start(25); window.preflightCampaign(request,accounts);
    QTimer::singleShot(8000,&window,[]{qApp->exit(261);});
  }
