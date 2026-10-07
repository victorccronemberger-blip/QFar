  static void campaignRestrictionSmoke(MainWindow& window) {
    struct Flow { int preflights=0, starts=0; };
    auto flow=std::make_shared<Flow>();
    const QStringList accounts{QStringLiteral("healthy@example.invalid"), QStringLiteral("restricted@example.invalid")};
    const bool shortfall=qApp->arguments().contains("--restriction-shortfall");
    const bool unknown=qApp->arguments().contains("--restriction-capacity-unknown");
    const bool transient=qApp->arguments().contains("--restriction-transient");
    const bool pending=qApp->arguments().contains("--restriction-skip");
    const QJsonObject request{{"accounts",QJsonArray::fromStringList(accounts)},
        {"tasks",QJsonArray{QJsonObject{{"task_id","fixture-task"}}}},
        {"dataset","ego4d"},{"target_hours",8},{"include_clip_plan",true}};
    auto* server=new QTcpServer(&window);
    if(!server->listen(QHostAddress::LocalHost)) {qApp->exit(210);return;}
    QObject::connect(server,&QTcpServer::newConnection,server,[server,flow,accounts,shortfall,unknown,transient,pending,request,&window] {
      auto* socket=server->nextPendingConnection();
      QObject::connect(socket,&QTcpSocket::readyRead,socket,[socket,flow,accounts,shortfall,unknown,transient,pending,request,&window] {
        const auto input=socket->property("input").toByteArray()+socket->readAll(); socket->setProperty("input",input);
        const int end=input.indexOf("\r\n\r\n");
        if(end<0 || socket->property("answered").toBool()) return;
        const auto match=QRegularExpression("Content-Length: (\\d+)",QRegularExpression::CaseInsensitiveOption).match(QString::fromLatin1(input.left(end)));
        if(match.hasMatch() && input.size()<end+4+match.captured(1).toInt()) return;
        socket->setProperty("answered",true);
        if(input.startsWith("POST /api/campaigns ")) {
          ++flow->starts;
          const auto body=QJsonDocument::fromJson(input.mid(end+4)).object();
          const bool correct=!shortfall && !unknown && !transient && flow->preflights==1 && flow->starts==1
              && body.value("preflight_id")=="restriction-receipt" && body.value("accounts")==request.value("accounts")
              && window._campaignAccounts->count()==(pending?2:1)
              && window._campaignAccounts->item(0)->data(Qt::UserRole).toString()==accounts[0]
              && (!pending || (body.value("skip_unverified").toBool() && !body.contains("remove_restricted")))
              && !QSettings().value("campaign/pendingStart").toByteArray().isEmpty();
          qApp->exit(correct?0:211); return;
        }
        if(!input.startsWith("POST /api/campaigns/preflight?async=1&request_id=")) {qApp->exit(212);return;}
        if(++flow->preflights!=1) {qApp->exit(213);return;}
        const auto body=QJsonDocument::fromJson(input.mid(end+4)).object();
        if(body!=request) {qApp->exit(214);return;}
        QJsonObject reply{{"ok",!shortfall && !transient && !pending},{"can_remove_and_continue",false},
            {"can_skip_and_continue",pending && !shortfall && !unknown},
            {"preflight_id",transient?QJsonValue():QJsonValue("restriction-receipt")},
            {"removed_accounts",pending?QJsonArray{}:QJsonArray{accounts[1]}},
            {"skippable_accounts",pending?QJsonArray{accounts[1]}:QJsonArray{}},
            {"accounts",QJsonObject{{"selected",2},{"validated",transient?0:1}}},
            {"tasks",QJsonObject{{"compatible",1}}},{"clips",32},{"estimated_sends",32},{"account_workers",1},
            {"blockers",shortfall?QJsonArray{"Conteúdo novo insuficiente"}:transient || pending?QJsonArray{"Falha temporária"}:QJsonArray{}},
            {"warnings",pending?QJsonArray{}:QJsonArray{"Conta restrita removida e registrada em Banidas"}},
            {"account_errors",transient || pending?QJsonArray{"Falha temporária"}:QJsonArray{}},
            {"account_issues",transient || pending?QJsonArray{QJsonObject{{"email",pending?accounts[1]:accounts[0]},
                {"restriction_confirmed",false},{"code",pending?"local_permission":"network"},{"stage","Acesso"},
                {"detail",pending?"PermissionError":"NetworkError"},{"reason","Falha temporária"},{"action","Conta preservada"}}}:QJsonArray{}}};
        if(!unknown) reply.insert("capacity",capacityFixture(request,{accounts[0]},shortfall?3600:28800));
        const auto bytes=QJsonDocument(reply).toJson(QJsonDocument::Compact);
        socket->write("HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\nContent-Length: "+QByteArray::number(bytes.size())+"\r\n\r\n"+bytes);
        socket->disconnectFromHost();
      });
      QObject::connect(socket,&QTcpSocket::disconnected,socket,&QObject::deleteLater);
    });
    window._api.setBaseUrl(QStringLiteral("http://127.0.0.1:%1").arg(server->serverPort()));
    {
      const QSignalBlocker blocker(window._campaignAccounts);
      for(const auto& email:accounts) {
        auto* item=new QListWidgetItem(email,window._campaignAccounts);
        item->setData(Qt::UserRole,email); item->setCheckState(Qt::Checked);
      }
    }
    auto* timer=new QTimer(&window);
    QObject::connect(timer,&QTimer::timeout,&window,[&window,flow,shortfall,unknown,transient,pending,timer] {
      auto* dialog=qobject_cast<QDialog*>(QApplication::activeModalWidget());
      if(!dialog) return;
      if(pending && !shortfall && !unknown) {
        if(flow->preflights!=1 || flow->starts!=0 || window._campaignAccounts->count()!=2) {qApp->exit(218);return;}
        if(auto* confirm=dialog->findChild<QPushButton*>("campaignReviewConfirm")) {
          if(!confirm->isEnabled()) {qApp->exit(219);return;}
          timer->stop(); confirm->click(); return;
        }
        for(auto* button:dialog->findChildren<QPushButton*>())
          if(button->text()=="Revisar contas aprovadas") {QTimer::singleShot(0,button,&QPushButton::click);return;}
        qApp->exit(220);return;
      }
      if(!shortfall && !unknown && !transient) {dialog->reject();qApp->exit(215);return;}
      auto* confirm=dialog->findChild<QPushButton*>("campaignReviewConfirm");
      bool offeredSkip=false;
      for(auto* button:dialog->findChildren<QPushButton*>())
        offeredSkip |= button->text()=="Revisar contas aprovadas";
      const bool correct=flow->preflights==1 && flow->starts==0 && window._campaignAccounts->count()==(pending?2:1)
          && (transient || (pending && !offeredSkip) || (dialog->windowTitle()=="Revisar campanha" && confirm && !confirm->isEnabled()));
      timer->stop(); dialog->reject(); QTimer::singleShot(50,qApp,[correct]{qApp->exit(correct?0:216);});
    });
    timer->start(20);
    window.preflightCampaign(request,accounts);
    QTimer::singleShot(8000,&window,[]{qApp->exit(217);});
  }
