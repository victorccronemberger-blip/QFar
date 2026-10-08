// Included inside OperationPreview. Uses only fictional accounts and loopback HTTP.
static QJsonArray accountsFixture() {
  QJsonArray accounts;
  for (int i=0;i<24;++i) {
    const auto email=QStringLiteral("conta-%1@example.invalid").arg(i+1,2,10,QLatin1Char('0'));
    accounts.append(QJsonObject{{"email",email},{"has_password",true},{"has_minute_access",i!=1},
      {"expires_at",1792000000},{"account_kind","crowtado"},{"org_key","fixture-org"},{"org_name","Datoric"},
      {"last_check",QJsonObject{{"status",i==2?"inconclusive":"active"},{"status_label",i==2?"Verificação inconclusiva":"Acesso verificado"},
        {"checked_at","2026-10-05T10:30:00-03:00"},{"error",i==2?"A consulta não respondeu. Tente verificar novamente.":""}}},
      {"registration",QJsonObject{{"state",i==1?"incomplete":"complete"},{"error",i==1?"Vínculo não confirmado; retome esta conta.":""},
        {"steps",QJsonObject{{"save_partial",QJsonObject{{"status","ok"},{"detail","Credencial protegida"}}},
                            {"link_minute",QJsonObject{{"status",i==1?"fail":"ok"},{"detail",i==1?"Conexão interrompida":"Vínculo confirmado"}}}}}}},
      {"restriction",QJsonObject{{"code",i==0?"disabled":i==2?"unknown":"clear"},
        {"label",i==0?"Conta desativada · saque retido":i==2?"Restrição não verificada":"Sem retenção informada"},
        {"reason",i==0?"A Crowtado reteve o saque. Um login aceito não confirma liberação.":"Última leitura registrada."}}}});
  }
  return accounts;
}

static void accountsQaPreview(MainWindow& window) {
  window.applyStructuralStyle(qApp->arguments().contains("--dark"));
  window._backendReady=true; window._accountsAvailable=true;
  window._pages->setCurrentIndex(5); window._navigation->setCurrentRow(5);
  window.renderAccounts(accountsFixture());
  if(qApp->arguments().contains("--creator")) {
    window._pages->widget(5)->findChild<QTabWidget*>()->setCurrentIndex(2);
    window._bulkRegisterDomain->clear();window._bulkRegisterDomain->addItem("example.invalid","example.invalid");
    for(auto* combo:{window._accountProxy,window._bulkRegisterProxy}) {
      combo->clear();combo->addItem("Sem proxy","");combo->addItem("Automático · alternar por conta","auto");
      combo->addItem("203.0.113.10:8080","fixture-proxy-id");combo->setCurrentIndex(1);
    }
    window._bulkRegisterStatus->setText(QStringLiteral("Crowtado + Minute, com código de e-mail automático. Depois, conclua idade, equipamento e vínculo manualmente no site Crowtado."));
  }
  window._backendState->setText(QStringLiteral("● Prévia isolada"));
  QTimer::singleShot(200,&window,[&window] {qApp->exit(window.grab().save(qApp->arguments().at(1))?0:1);});
}

static void accountServicesSmoke(MainWindow& window) {
  window.applyStructuralStyle(qApp->arguments().contains("--dark"));
  QJsonArray accounts;
  const QStringList states{"active", "disabled", "inconclusive"};
  for (int i=0; i<3; ++i) for (int j=0; j<3; ++j) {
    const auto minute=QJsonObject{{"status",states.at(i)},
      {"status_label",i==0?"Acesso verificado":i==1?"Banida · Minute":"Verificação inconclusiva"},
      {"checked_at","2026-10-05T12:00:00Z"},{"error",i==1?"A organização Minute confirmou restrição.":i==2?"Minute não respondeu.":""}};
    const auto site=QJsonObject{{"status",states.at(j)},
      {"status_label",j==0?"Sem restrição informada":j==1?"Banida · saque suspenso":"Login aceito · saque não verificado"},
      {"checked_at","2026-10-05T12:00:00Z"},{"access_status","active"},
      {"error",j==1?"A Crowtado reteve os saques.":j==2?"Elegibilidade Crowtado não respondeu.":""}};
    accounts.append(QJsonObject{{"email",QStringLiteral("caso-%1-%2@example.invalid").arg(i).arg(j)},
      {"has_minute_access",i!=2},{"has_password",true},{"account_kind","crowtado"},{"org_name","Datoric"},
      {"last_check",QJsonObject{{"status",i==0&&j==0?"active":i==1||j==1?"disabled":"inconclusive"},
        {"status_label","Resultado agregado"},{"providers",QJsonObject{{"minute",minute},{"crowtado",site}}}}},
      {"restriction",QJsonObject{{"code",i==0&&j==0?"clear":"restricted"},{"label","Diagnóstico da carteira"},{"reason","Motivo do saque"}}}});
  }
  window._backendReady=true; window._accountsAvailable=true;
  window._pages->setCurrentIndex(5); window._navigation->setCurrentRow(5);
  window.renderAccounts(accounts);
  for (int row=0; row<9; ++row) {
    const auto minute=accounts.at(row).toObject().value("last_check").toObject().value("providers").toObject().value("minute").toObject();
    const auto site=accounts.at(row).toObject().value("last_check").toObject().value("providers").toObject().value("crowtado").toObject();
    const QString expectedMinute=row/3==2?QStringLiteral("Minute ainda não conectado"):minute.value("status_label").toString().section(QStringLiteral(" · "),0,0);
    if(window._accountsTable->item(row,2)->text()!=expectedMinute
        || window._accountsTable->item(row,3)->text()!=site.value("status_label").toString()
        || !window._accountsTable->item(row,3)->toolTip().contains(QStringLiteral("Login Crowtado aceito"))) {qApp->exit(254); return;}
    bool enabled=false;
    for(const auto* menu:window._accountsTable->cellWidget(row,4)->findChildren<QMenu*>())
      for(const auto* action:menu->actions()) if(action->text()==QStringLiteral("Verificar Minute e Crowtado")) enabled=action->isEnabled();
    if(!enabled) {qApp->exit(255);return;}
  }
  window._accountsAttention->setChecked(true);
  int visible=0; for(int row=0;row<9;++row) visible+=!window._accountsTable->isRowHidden(row);
  if(visible!=8) {qApp->exit(256); return;}
  window._accountsAttention->setChecked(false);
  if(qApp->arguments().contains("--account-services-preview")) {
    QTimer::singleShot(150,&window,[&window] {qApp->exit(window.grab().save(qApp->arguments().at(1))?0:1);});
  } else qApp->exit(0);
}

static void accountsCreationBusyGuardSmoke(MainWindow& window) {
  struct Flow { int preflights=0; int posts=0; int accountWrites=0; int mode=0; };
  auto flow=std::make_shared<Flow>();
  auto* server=new QTcpServer(&window);
  if(!server->listen(QHostAddress::LocalHost)){qApp->exit(260);return;}
  QObject::connect(server,&QTcpServer::newConnection,server,[server,flow]{
    auto* socket=server->nextPendingConnection();
    QObject::connect(socket,&QTcpSocket::readyRead,socket,[socket,flow]{
      const auto input=socket->property("input").toByteArray()+socket->readAll();socket->setProperty("input",input);
      const int split=input.indexOf("\r\n\r\n");if(split<0||socket->property("answered").toBool())return;
      const QRegularExpression length("Content-Length: (\\d+)",QRegularExpression::CaseInsensitiveOption);
      const auto match=length.match(QString::fromLatin1(input.left(split)));
      if(match.hasMatch()&&input.size()-split-4<match.captured(1).toInt())return;
      socket->setProperty("answered",true);QJsonObject out{{"ok",true}};
      if(input.startsWith("GET /api/accounts/bulk-register/preflight")){
        ++flow->preflights;QJsonObject checks{{"hostinger",QJsonObject{{"ok",true}}},
          {"chrome",QJsonObject{{"ok",true}}},{"crowtado_api",QJsonObject{{"ok",true}}},
          {"minute_api",QJsonObject{{"ok",true}}},{"invite_code",QJsonObject{{"ok",flow->mode!=2}}},
          {"proxy",QJsonObject{{"ok",true}}}};
        if(flow->mode==1)checks.remove("minute_api");
        out={{"ready",true},{"checks",checks}};
      } else if(input.startsWith("POST /api/accounts/bulk-register ")) ++flow->posts;
      else if(input.startsWith("POST /api/accounts/")) ++flow->accountWrites;
      const auto bytes=QJsonDocument(out).toJson(QJsonDocument::Compact);
      socket->write("HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\nContent-Length: "+QByteArray::number(bytes.size())+"\r\n\r\n"+bytes);
      socket->disconnectFromHost();
    });
  });
  window._api.setBaseUrl(QStringLiteral("http://127.0.0.1:%1").arg(server->serverPort()));
  window._backendReady=true;window._accountsAvailable=true;window._registrationProxiesReady=true;
  window._bulkRegisterDomain->clear();window._bulkRegisterDomain->addItem("example.invalid","example.invalid");window._bulkRegisterDomain->addItem("other.invalid","other.invalid");
  window._bulkRegisterProxy->clear();window._bulkRegisterProxy->addItem("Auto","auto");
  window._bulkRegisterPreflightReady=false;window.checkBulkRegisterDomain();
  window._accountEmail->setText("fixture@example.invalid");window._accountPassword->setText("fixture-only-password");
  auto* timer=new QTimer(&window);auto stage=std::make_shared<int>(0);
  QObject::connect(timer,&QTimer::timeout,&window,[&window,flow,stage]{
    if(*stage==0&&window._bulkRegisterPreflightReady){
      if(!window._bulkRegisterStart->isEnabled()){qApp->exit(261);return;}
      window._accountsCheckRunning=true;window.setAccountTransferBusy(false);
      if(window._bulkRegisterStart->isEnabled()){qApp->exit(262);return;}
      window.startBulkRegister();
      window.addAccount(true);window.resumeAccount("fixture@example.invalid");
      if(flow->posts||flow->accountWrites){qApp->exit(263);return;}
      window._accountsCheckRunning=false;window.setAccountTransferBusy(false);
      if(!window._bulkRegisterStart->isEnabled()){qApp->exit(264);return;}
      window._orgMigrationRunning=true;window.setAccountTransferBusy(false);
      if(window._bulkRegisterStart->isEnabled()){qApp->exit(265);return;}
      window.startBulkRegister();window.addAccount(true);window.resumeAccount("fixture@example.invalid");
      if(flow->posts||flow->accountWrites){qApp->exit(266);return;}
      window._orgMigrationRunning=false;window.setAccountTransferBusy(false);
      if(!window._bulkRegisterStart->isEnabled()){qApp->exit(267);return;}
      window._accountTransferBusy=true;window._bulkRegisterDomain->setCurrentIndex(1);
      if(window._bulkRegisterPreflightReady){qApp->exit(268);return;}
      window.addAccount(true);window.resumeAccount("fixture@example.invalid");
      if(flow->accountWrites){qApp->exit(268);return;}
      window._accountTransferBusy=false;window.setAccountTransferBusy(false);flow->mode=1;window.checkBulkRegisterDomain();
      if(window._bulkRegisterStart->isEnabled()){qApp->exit(268);return;}
      ++*stage;
    } else if(*stage==1&&flow->preflights>=2&&!window._bulkRegisterPreflightReady){
      if(window._bulkRegisterStart->isEnabled()||flow->posts){qApp->exit(269);return;}
      flow->mode=2;window.checkBulkRegisterDomain();++*stage;
    } else if(*stage==2&&flow->preflights>=3&&!window._bulkRegisterPreflightReady){
      if(window._bulkRegisterStart->isEnabled()||flow->posts){qApp->exit(270);return;}
      qApp->exit(0);
    }
  });
  timer->start(20);QTimer::singleShot(4000,&window,[]{qApp->exit(270);});
}

static void accountsCatalogAutoInitializationSmoke(MainWindow& window) {
  const bool restoredDone=qApp->arguments().contains("--accounts-catalog-auto-restored-done");
  struct Flow {
    int domains=0,proxies=0,preflights=0,statusReads=0,posts=0,stage=0;
    int baseDomains=0,baseProxies=0,basePreflights=0,baseStatusReads=0;
  };
  auto flow=std::make_shared<Flow>();
  auto* server=new QTcpServer(&window);
  if(!server->listen(QHostAddress::LocalHost)){qApp->exit(286);return;}
  QObject::connect(server,&QTcpServer::newConnection,server,[server,flow,restoredDone]{
    auto* socket=server->nextPendingConnection();
    QObject::connect(socket,&QTcpSocket::readyRead,socket,[socket,flow,restoredDone]{
      const auto input=socket->property("input").toByteArray()+socket->readAll();socket->setProperty("input",input);
      const int split=input.indexOf("\r\n\r\n");if(split<0||socket->property("answered").toBool())return;
      const QRegularExpression length("Content-Length: (\\d+)",QRegularExpression::CaseInsensitiveOption);
      const auto match=length.match(QString::fromLatin1(input.left(split)));
      if(match.hasMatch()&&input.size()-split-4<match.captured(1).toInt())return;
      socket->setProperty("answered",true);
      QJsonObject out{{"ok",true}};
      if(input.startsWith("GET /api/accounts/domains ")){
        ++flow->domains;
        out={{"domains",QJsonArray{QJsonObject{{"domain","fixture.invalid"},{"profile_name","Fixture"}}}}};
      } else if(input.startsWith("GET /api/accounts/proxies ")){
        ++flow->proxies;
        out={{"proxies",QJsonArray{QJsonObject{{"id","fixture-proxy"},{"label","Proxy fixture"}}}}};
      } else if(input.startsWith("GET /api/accounts/bulk-register/preflight")){
        ++flow->preflights;
        const QJsonObject good{{"ok",true}};
        out={{"ready",true},{"checks",QJsonObject{{"hostinger",good},{"chrome",good},
          {"crowtado_api",good},{"minute_api",good},{"invite_code",good},{"proxy",good}}}};
      } else if(input.startsWith("GET /api/accounts/bulk-register/status ")){
        ++flow->statusReads;
        out=restoredDone
          ? QJsonObject{{"state","done"},{"request_id","fixture-old-batch"},{"total",0},
              {"completed",0},{"created",0},{"failed",0},{"results",QJsonArray{}}}
          : QJsonObject{{"state","idle"},{"request_id",""},{"total",0},
              {"completed",0},{"created",0},{"failed",0},{"results",QJsonArray{}}};
      } else if(input.startsWith("GET /api/accounts ")){
        out={{"accounts",QJsonArray{}}};
      } else if(input.startsWith("GET /api/accounts/migration ")){
        out={{"state","idle"}};
      } else if(input.startsWith("GET /api/integrations ")){
        out={{"ego4d",QJsonObject{}},{"hostinger",QJsonObject{}},
          {"holoassist",QJsonObject{}},{"runtime",QJsonObject{}},{"security",QJsonObject{}}};
      } else if(input.startsWith("POST /api/accounts/")){
        ++flow->posts;out={{"error","Cadastro desativado nesta fixture."}};
      }
      const auto bytes=QJsonDocument(out).toJson(QJsonDocument::Compact);
      socket->write("HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\nContent-Length: "+QByteArray::number(bytes.size())+"\r\n\r\n"+bytes);
      socket->disconnectFromHost();
    });
  });
  window._api.setBaseUrl(QStringLiteral("http://127.0.0.1:%1").arg(server->serverPort()));
  window._pages->setCurrentIndex(5);window._navigation->setCurrentRow(5);
  window._pages->widget(5)->findChild<QTabWidget*>()->setCurrentIndex(2);
  if(window._registrationProxiesReady||!window._bulkRegisterDomain->currentData().toString().isEmpty()
      ||!window._bulkRegisterProxy->currentData().toString().isEmpty()) {qApp->exit(287);return;}
  window.setBackendReady(true);
  auto* timer=new QTimer(&window);
  QObject::connect(timer,&QTimer::timeout,&window,[&window,flow,restoredDone]{
    if(flow->posts){qApp->exit(288);return;}
    const bool ready=window._bulkRegisterPreflightReady&&window._bulkRegisterStart->isEnabled();
    if(flow->stage==0&&flow->domains>0&&flow->proxies>0&&flow->preflights>0&&ready){
      if(window._bulkRegisterDomain->currentData().toString()!=QStringLiteral("fixture.invalid")
          ||window._bulkRegisterProxy->currentData().toString()!=QStringLiteral("auto")
          ||!window._registrationProxiesReady||!window._accountsAvailable
          ||(restoredDone&&(flow->statusReads==0
              ||!window._bulkRegisterStatus->text().startsWith(QStringLiteral("Concluído:"))))){qApp->exit(289);return;}
      flow->basePreflights=flow->preflights;
      flow->baseStatusReads=flow->statusReads;
      flow->stage=1;
      window.beginRegistrationPolling();
      return;
    }
    if(flow->stage==1&&flow->statusReads>flow->baseStatusReads
        &&flow->preflights>flow->basePreflights&&ready){
      if(window._bulkRegisterDomain->currentData().toString()!=QStringLiteral("fixture.invalid")
          ||window._bulkRegisterProxy->currentData().toString()!=QStringLiteral("auto")
          ||!window._registrationProxiesReady||!window._accountsAvailable
          ||(restoredDone&&!window._bulkRegisterStatus->text().startsWith(QStringLiteral("Concluído:")))){qApp->exit(291);return;}
      flow->baseDomains=flow->domains;
      flow->baseProxies=flow->proxies;
      flow->basePreflights=flow->preflights;
      flow->baseStatusReads=flow->statusReads;
      flow->stage=2;
      window.setBackendReady(false,QStringLiteral("Reconexão de fixture"));
      window.setBackendReady(true);
      return;
    }
    if(flow->stage==2&&flow->domains>flow->baseDomains&&flow->proxies>flow->baseProxies
        &&flow->preflights>flow->basePreflights&&flow->statusReads>flow->baseStatusReads&&ready){
      if(window._bulkRegisterDomain->currentData().toString()!=QStringLiteral("fixture.invalid")
          ||window._bulkRegisterProxy->currentData().toString()!=QStringLiteral("auto")
          ||!window._registrationProxiesReady||!window._accountsAvailable
          ||(restoredDone&&!window._bulkRegisterStatus->text().startsWith(QStringLiteral("Concluído:")))){qApp->exit(292);return;}
      qApp->exit(0);
    }
  });
  timer->start(20);QTimer::singleShot(8000,&window,[]{qApp->exit(290);});
}

static void accountsRegistrationOutcomeSmoke(MainWindow& window) {
  const QStringList args=qApp->arguments();
  const bool beforeAdmission=args.contains("--accounts-outcome-before-admission");
  const bool afterAdmission=args.contains("--accounts-outcome-after-admission");
  const bool foreign=args.contains("--accounts-outcome-foreign-batch");
  const bool oldTerminal=args.contains("--accounts-outcome-old-terminal");
  const bool notAdmitted=args.contains("--accounts-outcome-not-admitted");
  const bool conflict=args.contains("--accounts-outcome-definitive-conflict");
  const bool pollRecovery=args.contains("--accounts-outcome-poll-recovery");
  const bool rateLimit=args.contains("--accounts-outcome-rate-limit");
  struct Flow { QString state="idle",requestId;QStringList ids;int posts=0,statusReads=0,pollFailures=0;bool changedToForeign=false,recoveryObserved=false; };
  auto flow=std::make_shared<Flow>();
  if(oldTerminal){flow->state="done";flow->requestId="previous-batch";}
  if(notAdmitted){flow->state="failed";flow->requestId.clear();}
  auto* server=new QTcpServer(&window);
  if(!server->listen(QHostAddress::LocalHost)){qApp->exit(271);return;}
  QObject::connect(server,&QTcpServer::newConnection,server,[server,flow,beforeAdmission,afterAdmission,foreign,oldTerminal,notAdmitted,conflict,pollRecovery,rateLimit]{
    auto* socket=server->nextPendingConnection();
    QObject::connect(socket,&QTcpSocket::readyRead,socket,[socket,flow,beforeAdmission,afterAdmission,foreign,oldTerminal,notAdmitted,conflict,pollRecovery,rateLimit]{
      const auto input=socket->property("input").toByteArray()+socket->readAll();socket->setProperty("input",input);
      const int split=input.indexOf("\r\n\r\n");if(split<0||socket->property("answered").toBool())return;
      const QRegularExpression length("Content-Length: (\\d+)",QRegularExpression::CaseInsensitiveOption);
      const auto match=length.match(QString::fromLatin1(input.left(split)));
      if(match.hasMatch()&&input.size()-split-4<match.captured(1).toInt())return;
      socket->setProperty("answered",true);QJsonObject out{{"ok",true}};int httpStatus=200;
      if(input.startsWith("GET /api/accounts/bulk-register/status ")){
        ++flow->statusReads;
        if(foreign&&flow->posts&&!flow->changedToForeign){flow->changedToForeign=true;flow->state="running";flow->requestId="other-batch";}
        if(flow->state=="running"&&flow->requestId!="other-batch"&&flow->statusReads>=3)flow->state="done";
        const bool done=flow->state=="done";
        out={{"state",flow->state},{"request_id",flow->requestId},{"total",1},{"completed",done?1:0},
          {"created",done?1:0},{"failed",0},{"results",done?QJsonArray{QJsonObject{{"email","new@example.invalid"},
            {"created",true},{"steps",QJsonObject{}},{"error",QJsonValue()}}}:QJsonArray{}},
          {"current_email",done?QString():QStringLiteral("new@example.invalid")},{"current_step",done?QString():QStringLiteral("Criando")}};
        if(pollRecovery&&flow->posts&&flow->pollFailures==0){++flow->pollFailures;httpStatus=503;out={{"error","Consulta temporariamente indisponível."}};}
      } else if(input.startsWith("GET /api/accounts ")) out={{"accounts",QJsonArray{}}};
      else if(input.startsWith("GET /api/accounts/migration ")) out={{"state","idle"}};
      else if(input.startsWith("GET /api/accounts/bulk-register/preflight")){
        const QJsonObject good{{"ok",true}};
        out={{"ready",true},{"checks",QJsonObject{{"hostinger",good},{"chrome",good},
          {"crowtado_api",good},{"minute_api",good},{"invite_code",good},{"proxy",good}}}};
      }
      else if(input.startsWith("POST /api/accounts/register?async=1 ")){
        ++flow->posts;const auto body=QJsonDocument::fromJson(input.mid(split+4)).object();
        const QString id=body.value("request_id").toString();flow->ids.append(id);
        if(id.isEmpty()||body.value("password").toString()!=QStringLiteral("fixture-only-password")){qApp->exit(272);return;}
        if(conflict){httpStatus=409;out={{"error","Criação recusada pelo fixture."}};}
        else if(notAdmitted){httpStatus=503;out={{"error","Falha ao gravar estado inicial."},{"error_code","request_outcome_unknown"},{"not_admitted",true}};}
        else if(rateLimit){httpStatus=429;out={{"error","Limite de criação atingido. Aguarde 30 minutos antes de tentar novamente."},{"error_code","rate_limit"},{"retry_after_seconds",1800},{"not_admitted",true}};}
        else if(foreign&&flow->posts==1){httpStatus=503;out={{"error","Resposta inconclusiva."},{"error_code","request_outcome_unknown"}};}
        else if((beforeAdmission||oldTerminal)&&flow->posts==1){httpStatus=503;out={{"error","Resposta inconclusiva."},{"error_code","request_outcome_unknown"}};}
        else if(afterAdmission&&flow->posts==1){flow->state="running";flow->requestId=id;httpStatus=503;out={{"error","Resposta perdida depois da admissão."},{"error_code","request_outcome_unknown"}};}
        else {if(flow->requestId!=id){flow->requestId=id;flow->state="running";}out={{"ok",true},{"request_id",id}};}
      }
      const auto bytes=QJsonDocument(out).toJson(QJsonDocument::Compact);
      socket->write("HTTP/1.1 "+QByteArray::number(httpStatus)+(httpStatus==200?" OK\r\n":" Fixture\r\n")+
        "Content-Type: application/json\r\nConnection: close\r\nContent-Length: "+QByteArray::number(bytes.size())+"\r\n\r\n"+bytes);
      socket->disconnectFromHost();
    });
  });
  window._api.setBaseUrl(QStringLiteral("http://127.0.0.1:%1").arg(server->serverPort()));
  window._backendReady=true;window._accountsAvailable=true;
  window._registrationProxiesReady=true;
  window._bulkRegisterDomain->clear();window._bulkRegisterDomain->addItem("fixture.invalid","fixture.invalid");
  window._bulkRegisterProxy->clear();window._bulkRegisterProxy->addItem("Fixture proxy","fixture-proxy-id");
  window._accountProxy->clear();window._accountProxy->addItem("fixture proxy","fixture-proxy-id");
  window._accountEmail->setText("new@example.invalid");window._accountPassword->setText("fixture-only-password");
  window._bulkRegisterTable->setRowCount(1);window._bulkRegisterTable->setItem(0,0,new QTableWidgetItem("prior-result"));
  window.addAccount(true);
  auto* timer=new QTimer(&window);
  QObject::connect(timer,&QTimer::timeout,&window,[&window,flow,foreign,beforeAdmission,afterAdmission,oldTerminal,notAdmitted,conflict,pollRecovery,rateLimit]{
    if(pollRecovery&&flow->pollFailures&&flow->statusReads==2
        &&window._bulkRegisterStatus->text().contains(QStringLiteral("Conexão interrompida"))){
      if(window._status->text()!=window._bulkRegisterStatus->text()){qApp->exit(284);return;}
      flow->recoveryObserved=true;
    }
    if(foreign&&flow->changedToForeign){
      if(flow->posts!=1||window._accountPassword->text()!=QStringLiteral("fixture-only-password")
          ||window._bulkRegisterTable->rowCount()!=1||window._bulkRegisterTable->item(0,0)->text()!=QStringLiteral("prior-result")
          ||window._bulkRegisterStop->isEnabled()||window._bulkRegisterPendingRequestId.isEmpty()){qApp->exit(273);return;}
      qApp->exit(0);return;
    }
    if((notAdmitted||conflict||rateLimit)&&window._bulkRegisterPendingRequestId.isEmpty()){
      if(window._accountPassword->text()!=QStringLiteral("fixture-only-password")||window._bulkRegisterPolling
          ||window._status->text()!=window._bulkRegisterStatus->text()
          ||window._bulkRegisterTable->rowCount()!=1||window._bulkRegisterTable->item(0,0)->text()!=QStringLiteral("prior-result")){qApp->exit(274);return;}
      if(notAdmitted&&flow->statusReads!=1){qApp->exit(275);return;}
      if(rateLimit&&(flow->posts!=1||flow->ids.size()!=1||flow->statusReads!=1
          ||!window._bulkRegisterStatus->text().contains(QStringLiteral("aguarde 30 minutos"),Qt::CaseInsensitive)
          ||!window._bulkRegisterStatus->text().contains(QStringLiteral("HTTP 429")))){qApp->exit(286);return;}
      qApp->exit(0);return;
    }
    if(flow->state=="done"&&!window._bulkRegisterPolling&&window._bulkRegisterPendingRequestId.isEmpty()
        &&window._bulkRegisterStart->isEnabled()){
      const auto creatorStatus=window._bulkRegisterStatus->text();
      const int detailSeparator=creatorStatus.indexOf('\n');
      const auto expectedFooter=detailSeparator<0?creatorStatus:creatorStatus.left(detailSeparator);
      if(window._accountPassword->text()!=QString()||flow->ids.isEmpty()
          ||!creatorStatus.startsWith(QStringLiteral("Concluído:"))
          ||window._status->text()!=expectedFooter){qApp->exit(276);return;}
      for(const auto& id:flow->ids)if(id.isEmpty()||id!=flow->ids.first()){qApp->exit(277);return;}
      if(beforeAdmission&&flow->posts<2){qApp->exit(278);return;}
      if(afterAdmission&&flow->posts!=1){qApp->exit(279);return;}
      if(oldTerminal&&flow->posts<2){qApp->exit(280);return;}
      if(pollRecovery&&(!flow->recoveryObserved||flow->posts!=1)){qApp->exit(285);return;}
      qApp->exit(0);
    }
  });
  timer->start(25);QTimer::singleShot(7000,&window,[]{qApp->exit(281);});
}

static void accountsRegistrationNativePreview(MainWindow& window) {
  window.applyStructuralStyle(qApp->arguments().contains("--dark"));
  const bool rateLimitPreview=qApp->arguments().contains("--registration-native-rate-limit-preview");
  auto* server=new QTcpServer(&window);
  if(!server->listen(QHostAddress::LocalHost)){qApp->exit(282);return;}
  QObject::connect(server,&QTcpServer::newConnection,server,[server,rateLimitPreview]{
    auto* socket=server->nextPendingConnection();
    QObject::connect(socket,&QTcpSocket::readyRead,socket,[socket,rateLimitPreview]{
      const auto input=socket->property("input").toByteArray()+socket->readAll();socket->setProperty("input",input);
      const int split=input.indexOf("\r\n\r\n");if(split<0||socket->property("answered").toBool())return;
      const QRegularExpression length("Content-Length: (\\d+)",QRegularExpression::CaseInsensitiveOption);
      const auto match=length.match(QString::fromLatin1(input.left(split)));
      if(match.hasMatch()&&input.size()-split-4<match.captured(1).toInt())return;
      socket->setProperty("answered",true);QJsonObject out{{"ok",true}};int status=200;
      const QJsonObject good{{"ok",true}};
      if(input.startsWith("POST /api/campaigns/drain "))out={{"ok",true},{"draining",true},{"ready",true}};
      else if(input.startsWith("GET /api/accounts "))out={{"accounts",accountsFixture()}};
      else if(input.startsWith("GET /api/accounts/migration "))out={{"state","idle"}};
      else if(input.startsWith("GET /api/accounts/proxies "))out={{"proxies",QJsonArray{QJsonObject{{"id","preview-proxy"},{"label","203.0.113.10:8080"}}}}};
      else if(input.startsWith("GET /api/accounts/domains "))out={{"domains",QJsonArray{QJsonObject{{"domain","example.invalid"}}}}};
      else if(input.startsWith("GET /api/accounts/bulk-register/preflight"))out={{"ready",true},{"checks",QJsonObject{{"hostinger",good},{"chrome",good},{"crowtado_api",good},{"minute_api",good},{"invite_code",good},{"proxy",good}}}};
      else if(input.startsWith("GET /api/accounts/bulk-register/status "))out={{"state","idle"},{"total",0},{"completed",0},{"created",0},{"failed",0},{"results",QJsonArray{}}};
      else if(input.startsWith("POST /api/accounts/register?async=1 ")||input.startsWith("POST /api/accounts/bulk-register ")){
        if(rateLimitPreview){
          status=429;out={{"error","Limite de criação atingido. Aguarde 30 minutos antes de tentar novamente."},
            {"error_code","rate_limit"},{"retry_after_seconds",1800},{"not_admitted",true}};
        } else {
          status=409;out={{"error","Prévia local: comandos de criação estão desativados."}};
        }
      }
      const auto bytes=QJsonDocument(out).toJson(QJsonDocument::Compact);
      const QByteArray reason=status==200?" OK\r\n":status==429?" Too Many Requests\r\n":" Conflict\r\n";
      socket->write("HTTP/1.1 "+QByteArray::number(status)+reason+
        "Content-Type: application/json\r\nConnection: close\r\nContent-Length: "+QByteArray::number(bytes.size())+"\r\n\r\n"+bytes);
      socket->disconnectFromHost();
    });
  });
  window._api.setBaseUrl(QStringLiteral("http://127.0.0.1:%1").arg(server->serverPort()));
  window._backendReady=true;window._accountsAvailable=true;window._registrationProxiesReady=true;
  window._pages->setCurrentIndex(5);window._navigation->setCurrentRow(5);
  window._pages->widget(5)->findChild<QTabWidget*>()->setCurrentIndex(2);
  window._bulkRegisterDomain->clear();window._bulkRegisterDomain->addItem("example.invalid","example.invalid");
  window._bulkRegisterProxy->clear();window._bulkRegisterProxy->addItem("Preview proxy","preview-proxy");
  window._accountProxy->clear();window._accountProxy->addItem("Preview proxy","preview-proxy");
  window._accountEmail->clear();window._accountPassword->clear();
  window.renderAccounts(accountsFixture());
  window._backendState->setText(QStringLiteral("● Prévia isolada · serviço fictício local"));
  window.checkBulkRegisterDomain();
  if(qApp->arguments().contains("--registration-native-close-smoke")) {
    QTimer::singleShot(250,&window,&QWidget::close);
    QTimer::singleShot(2500,&window,[]{qApp->exit(283);});
  }
  QTimer::singleShot(10*60*1000,&window,&QWidget::close);
}

static void accountsQaSmoke(MainWindow& window) {
  if(qApp->arguments().contains("--registration-native-preview")
      ||qApp->arguments().contains("--registration-native-rate-limit-preview")){
    accountsRegistrationNativePreview(window);return;
  }
  if(qApp->arguments().contains("--accounts-creation-busy-smoke")){accountsCreationBusyGuardSmoke(window);return;}
  if(qApp->arguments().contains("--accounts-catalog-auto-idle")
      ||qApp->arguments().contains("--accounts-catalog-auto-restored-done")){
    accountsCatalogAutoInitializationSmoke(window);return;
  }
  if(qApp->arguments().contains("--accounts-outcome-before-admission")
      ||qApp->arguments().contains("--accounts-outcome-after-admission")
      ||qApp->arguments().contains("--accounts-outcome-foreign-batch")
      ||qApp->arguments().contains("--accounts-outcome-old-terminal")
      ||qApp->arguments().contains("--accounts-outcome-not-admitted")
      ||qApp->arguments().contains("--accounts-outcome-rate-limit")
      ||qApp->arguments().contains("--accounts-outcome-poll-recovery")
      ||qApp->arguments().contains("--accounts-outcome-definitive-conflict")){
    accountsRegistrationOutcomeSmoke(window);return;
  }
  struct Flow { bool malformed=false, dropped=false, imported=false; std::shared_ptr<QTemporaryDir> folder=std::make_shared<QTemporaryDir>(); int posts=0, requests=0, statusReads=0; QString state="idle", requestId; };
  auto flow=std::make_shared<Flow>();
  auto* server=new QTcpServer(&window);
  if (!server->listen(QHostAddress::LocalHost)) {qApp->exit(231);return;}
  QObject::connect(server,&QTcpServer::newConnection,server,[server,flow] {
    auto* socket=server->nextPendingConnection();
    QObject::connect(socket,&QTcpSocket::readyRead,socket,[socket,flow] {
      const auto input=socket->property("input").toByteArray()+socket->readAll(); socket->setProperty("input",input);
      const int split=input.indexOf("\r\n\r\n");
      if (split<0 || socket->property("answered").toBool()) return;
      const QRegularExpression length("Content-Length: (\\d+)",QRegularExpression::CaseInsensitiveOption);
      const auto match=length.match(QString::fromLatin1(input.left(split)));
      if (match.hasMatch() && input.size()-split-4<match.captured(1).toInt()) return;
      socket->setProperty("answered",true);
      QJsonObject out{{"ok",true}};
      if (input.startsWith("GET /api/accounts ")) out=flow->malformed?QJsonObject{{"accounts","invalid"}}:QJsonObject{{"accounts",accountsFixture()}};
      else if (input.startsWith("GET /api/accounts/migration ")) out={{"state","idle"}};
      else if (input.startsWith("GET /api/accounts/proxies ")) out={{"proxies",flow->imported?QJsonArray{QJsonObject{{"id","fixture-proxy-id"},{"label","203.0.113.10:8080"}}}:QJsonArray{}}};
      else if (input.startsWith("GET /api/accounts/domains ")) out={{"domains",QJsonArray{QJsonObject{{"domain","example.invalid"}}}}};
      else if (input.startsWith("GET /api/accounts/bulk-register/preflight")) out={{"ready",true},{"checks",QJsonObject{}}};
      else if(input.startsWith("POST /api/accounts/proxies/import ")) {
        const auto body=QJsonDocument::fromJson(input.mid(split+4)).object();
        if(body.value("text").toString()!=QStringLiteral("203.0.113.10:8080:fixture-user:fixture-password\n")) {qApp->exit(249);return;}
        flow->imported=true;out={{"ok",true},{"total",1}};
      }
      else if (input.startsWith("POST /api/accounts/register?async=1 ")) {
        ++flow->requests;
        const auto body=QJsonDocument::fromJson(input.mid(split+4)).object();
        const auto requestId=body.value("request_id").toString();
        if(requestId.isEmpty()) {qApp->exit(248);return;}
        if (body.contains("birth_month") || body.contains("birth_year") || body.contains("gender") || body.value("proxy_id").toString()!=QStringLiteral("fixture-proxy-id")) {qApp->exit(232);return;}
        if(flow->requestId!=requestId) {
          ++flow->posts; flow->requestId=requestId;
          flow->state="running"; flow->statusReads=0;
        }
        out={{"ok",true},{"request_id",requestId}};
        // Simulate losing the POST response after the server has admitted the job.
        if (!flow->dropped) {flow->dropped=true;socket->disconnectFromHost();return;}
      } else if (input.startsWith("GET /api/accounts/bulk-register/status ")) {
        if (flow->state=="running" && ++flow->statusReads>=2) flow->state="done";
        const bool done=flow->state=="done";
        out={{"state",flow->state},{"request_id",flow->requestId},{"total",1},{"completed",done?1:0},{"created",done?1:0},{"failed",0},
          {"results",done?QJsonArray{QJsonObject{{"email","new@example.invalid"},{"nome","QA"},{"sobrenome","Fixture"},
            {"gender","female"},{"created",true},{"removed",false},{"removable",true},{"steps",QJsonObject{{"link_minute",QJsonObject{{"status","manual"},{"detail","Confirmar no site"}}}}},{"error",QJsonValue()}}}:QJsonArray{}},
          {"current_email",done?"":"new@example.invalid"},{"current_step",done?"":"Confirmando e-mail"}};
      }
      const auto bytes=QJsonDocument(out).toJson(QJsonDocument::Compact);
      socket->write("HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\nContent-Length: "+QByteArray::number(bytes.size())+"\r\n\r\n"+bytes);
      socket->disconnectFromHost();
    });
  });
  window._api.setBaseUrl(QStringLiteral("http://127.0.0.1:%1").arg(server->serverPort()));
  window._backendReady=true; window._pages->setCurrentIndex(5); window.loadAccounts();
  auto* timer=new QTimer(&window); auto stage=std::make_shared<int>(0);
  QObject::connect(timer,&QTimer::timeout,&window,[&window,flow,stage] {
    if (*stage==0 && window._accountsAvailable) {
      if (window._accountsTable->rowCount()!=24 || window._accountsTable->columnCount()!=5) {qApp->exit(233);return;}
      window._accountsTable->selectRow(4); window.renderAccounts(accountsFixture());
      if (!window._accountsTable->item(4,0)->isSelected()) {qApp->exit(234);return;}
      window._accountsAttention->setChecked(true);
      int visible=0; for(int i=0;i<24;++i) visible+=!window._accountsTable->isRowHidden(i);
      if(visible!=3) {qApp->exit(235);return;}
      window._accountsSearch->setText("conta-02");
      if(window._accountsTable->isRowHidden(1)||!window._accountsTable->isRowHidden(0)) {qApp->exit(236);return;}
      flow->malformed=true; window.loadAccounts(); ++*stage;
    } else if(*stage==1 && !window._accountsAvailable) {
      if(window._accountsTable->rowCount()!=24 || window._accountAdd->isEnabled()) {qApp->exit(237);return;}
      flow->malformed=false;window.loadAccounts();++*stage;
    } else if(*stage==2 && window._accountsAvailable && !window._accountTransferBusy) {
      QFile file(flow->folder->filePath("proxies.txt"));
      if(!file.open(QIODevice::WriteOnly)) {qApp->exit(250);return;}
      file.write("203.0.113.10:8080:fixture-user:fixture-password\n");file.close();
      window.importRegistrationProxiesFile(file.fileName());++*stage;
    } else if(*stage==3 && flow->imported && window._registrationProxiesReady && window._accountProxy->currentData().toString()=="auto") {
      if(window._bulkRegisterProxy->currentData().toString()!="auto" || window._accountProxy->count()!=3) {qApp->exit(251);return;}
      window._accountProxy->setCurrentIndex(window._accountProxy->findData("fixture-proxy-id"));
      window._accountEmail->setText("new@example.invalid"); window._accountPassword->setText("fixture-only-password");
      if(!window._accountBirthMonth->isHidden() && window._accountBirthMonth->isVisible()) {qApp->exit(252);return;}
      window.addAccount(true);window.addAccount(true);++*stage;
    } else if(*stage==4 && flow->state=="done" && !window._bulkRegisterPolling && window._accountsAvailable) {
      if(flow->posts!=1 || !flow->dropped || window._bulkRegisterTable->rowCount()!=1 || !window._accountPassword->text().isEmpty()) {
        qWarning("Accounts QA: posts=%d dropped=%d rows=%d passwordCleared=%d",flow->posts,flow->dropped,window._bulkRegisterTable->rowCount(),window._accountPassword->text().isEmpty());
        qApp->exit(flow->posts!=1?245:window._bulkRegisterTable->rowCount()!=1?246:247);return;
      }
      if(!window._bulkRegisterTable->item(0,2)->text().contains("site manual")) {qApp->exit(253);return;}
      qApp->exit(0);
    }
  });
  timer->start(25); QTimer::singleShot(12000,&window,[]{qApp->exit(239);});
}

// Explicitly invoked by the local live QA launcher; credentials exist only in process memory.
static void accountsLiveCreate(MainWindow& window) {
  QFile metadata(qApp->arguments().at(1));
  if (!metadata.open(QIODevice::ReadOnly)) {qApp->exit(241);return;}
  const auto input=QJsonDocument::fromJson(metadata.readAll()).object();
  window._api.setBaseUrl(input.value("url").toString());
  window._api.setSessionToken(qEnvironmentVariable("QMONEY_ACCOUNT_QA_SESSION").toUtf8());
  window._backendReady=true;window._pages->setCurrentIndex(5);window.loadAccounts();
  auto* timer=new QTimer(&window);auto started=std::make_shared<bool>(false);
  QObject::connect(timer,&QTimer::timeout,&window,[&window,input,started] {
    if (!*started && window._accountsAvailable) {
      *started=true;
      if (input.value("resume").toBool()) {window.resumeAccount(input.value("email").toString()); return;}
      window._accountEmail->setText(input.value("email").toString());
      window._accountPassword->setText(qEnvironmentVariable("QMONEY_ACCOUNT_QA_PASSWORD"));
      window._accountBirthMonth->setValue(input.value("birth_month").toInt());
      window._accountBirthYear->setValue(input.value("birth_year").toInt());
      window._accountGender->setCurrentIndex(window._accountGender->findData(input.value("gender").toString()));
      window._accountUseReferral->setChecked(input.value("use_referral").toBool(true));
      window.addAccount(true);
    } else if (*started && !window._accountConnecting && !window._bulkRegisterStarting && !window._bulkRegisterPolling && window._bulkRegisterTable->rowCount()>0) {
      window.grab().save(input.value("screenshot").toString());
      qApp->exit(0);
    }
  });
  timer->start(100);QTimer::singleShot(720000,&window,[]{qApp->exit(242);});
}
