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

static void accountsQaSmoke(MainWindow& window) {
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
        // Simulate losing the POST response after the server has admitted the job.
        if (!flow->dropped) {flow->dropped=true;socket->disconnectFromHost();return;}
      } else if (input.startsWith("GET /api/accounts/bulk-register/status ")) {
        if (flow->state=="running" && ++flow->statusReads>=2) flow->state="done";
        const bool done=flow->state=="done";
        out={{"state",flow->state},{"total",1},{"completed",done?1:0},{"created",done?1:0},{"failed",0},
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
