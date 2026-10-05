// Included inside OperationPreview: exercises the real MainWindow with a loopback service.
static QJsonObject operationFixture(QString state = QStringLiteral("running"), bool paused = false) {
  const QStringList phases{"sending", "excluded", "confirming", "preparing", "queued", "confirmed",
      "failed", "unconfirmed", "waiting", "recovering", "pending", "skipped"};
  const QHash<QString,QString> details{{"confirming","Aguardando confirmação no serviço"},{"preparing","Preparando arquivo para enviar"},
      {"queued","Aguardando o próximo envio"},{"failed","Falha no envio; confira o histórico"},{"unconfirmed","Envio sem confirmação de finalização"},
      {"waiting","Aguardando conteúdo para enviar"},{"recovering","Recuperando o envio anterior"},{"pending","Execução encerrada antes da confirmação"},
      {"skipped","Este clipe já foi enviado a esta conta"}};
  QJsonArray rows;
  QJsonObject counts;
  if (state != "idle") for (int i = 0; i < 48; ++i) {
    auto phase = phases[i % phases.size()];
    if ((state=="done" || state=="stopped" || state=="error")
        && (phase=="queued" || phase=="waiting" || phase=="preparing" || phase=="sending" || phase=="confirming" || phase=="recovering")) phase="pending";
    const auto email = i == 0 ? QStringLiteral("conta-com-identificacao-muito-longa-para-verificar-leitura@example.invalid")
        : QStringLiteral("conta-%1@example.invalid").arg(i+1, 2, 10, QLatin1Char('0'));
    rows.append(QJsonObject{{"email",email},{"state",phase},{"progress",phase=="sending"?QJsonValue(100):QJsonValue()},
      {"clip_uid",QStringLiteral("clip-%1").arg(i)},{"session_id",phase=="confirmed"?QJsonValue(QStringLiteral("receipt-%1").arg(i)):QJsonValue()},
      {"confirmed",phase=="confirmed"?2:0},{"failed",phase=="failed" || i==5?1:0},{"skipped",phase=="skipped"?1:0},
      {"needs_attention",i==11},
      {"detail",phase=="sending"?QStringLiteral("Transferindo; recebimento ainda não confirmado")
          : phase=="excluded"?QStringLiteral("Conta restrita; confira o motivo em Contas restritas")
          : phase=="confirmed"?QStringLiteral("Finalização confirmada")
          : details.value(phase)}});
    counts[phase] = counts.value(phase).toInt()+1;
  }
  return {{"state",state},{"pause_requested",paused},{"current","Biblioteca principal · 48 contas"},
    {"totals",QJsonObject{{"total_sends",72},{"done_sends",46},{"ok_sends",36},{"failed_sends",4},{"skipped_sends",6}}},
    {"operation",QJsonObject{{"accounts",rows},{"counts",counts}}},
    {"events",state=="idle"?QJsonArray{}:QJsonArray{
      QJsonObject{{"ts",1790431380},{"level","success"},{"title","Recebimento confirmado"},{"detail","Conta 06 · 2 envios finalizados"}},
      QJsonObject{{"ts",1790431440},{"level","warning"},{"title","Conta excluída desta campanha"},{"detail","Acesso restrito. Revise o motivo registrado antes de planejar outro envio."}},
      QJsonObject{{"ts",1790431500},{"level","info"},{"title","Transferência concluída; aguardando a confirmação de finalização do arquivo no serviço"},
        {"detail","O arquivo terminou de transferir. Isso não confirma o recebimento nem a avaliação. A confirmação aparece separadamente por conta."}}}}};
}

static void operationQaPreview(MainWindow& window) {
  window.applyStructuralStyle(qApp->arguments().contains("--dark"));
  QString state = QStringLiteral("running");
  const int arg = qApp->arguments().indexOf("--qa-state");
  if (arg>=0 && arg+1<qApp->arguments().size()) state=qApp->arguments().at(arg+1);
  const bool paused = state == "paused";
  const bool disconnected = state == "disconnected";
  const bool hours = state == "hours";
  if (paused || disconnected || hours) state="running";
  window._homeAccountSnapshot = state=="idle"?QJsonArray{}:QJsonArray{QJsonObject{{"email","fixture@example.invalid"}}};
  auto snap = operationFixture(state,paused);
  if (hours) snap["totals"] = QJsonObject{{"progress_unit","seconds"},{"progress_target",172800},{"progress_completed",86400},{"ok_sends",36}};
  window.renderOperation(snap);
  if (disconnected) window.operationUnavailable(QStringLiteral("Falha simulada no serviço local."));
  window._operationBalance->setText(state=="idle"?QStringLiteral("US$ —"):QStringLiteral("US$ 45,00"));
  window._operationBalanceNote->setText(state=="idle"?QStringLiteral("Consulte as contas na Carteira."):QStringLiteral("1 conta elegível · 2 requerem atenção"));
  window._backendState->setText(QStringLiteral("● Prévia isolada"));
  window._homeSync->setText(QStringLiteral("QA visual · contas e eventos simulados"));
  QTimer::singleShot(150,&window,[&window] { qApp->exit(window.grab().save(qApp->arguments().at(1))?0:1); });
}

static void operationQaSmoke(MainWindow& window) {
  struct Flow { QString state="running"; bool paused=false, malformed=false, reject=false; int posts=0, reads=0, delay=0; };
  auto flow=std::make_shared<Flow>();
  auto* server=new QTcpServer(&window);
  if (!server->listen(QHostAddress::LocalHost)) { qApp->exit(201); return; }
  QObject::connect(server,&QTcpServer::newConnection,server,[server,flow] {
    auto* socket=server->nextPendingConnection();
    QObject::connect(socket,&QTcpSocket::readyRead,socket,[socket,flow] {
      const auto input=socket->property("input").toByteArray()+socket->readAll();
      socket->setProperty("input",input);
      if (!input.contains("\r\n\r\n") || socket->property("answered").toBool()) return;
      socket->setProperty("answered",true);
      QJsonObject payload;
      int status=200, delay=0;
      if (input.startsWith("GET /api/campaigns/current ")) {
        ++flow->reads;
        payload=flow->malformed?QJsonObject{}:operationFixture(flow->state,flow->paused);
        delay=flow->delay; flow->delay=0;
      } else if (input.startsWith("GET /api/accounts ")) payload={{"accounts",QJsonArray{QJsonObject{{"email","fixture@example.invalid"}}}}};
      else if (input.startsWith("GET /api/logs ")) payload={{"logs",QJsonArray{}}};
      else if (input.startsWith("GET /api/balances ")) payload={
        {"accounts",QJsonArray{"fixture@example.invalid"}},{"balances",QJsonObject{{"fixture@example.invalid",QJsonObject{{"availableCents",123456}}}}},
        {"wallet",QJsonObject{{"accounts",QJsonObject{}},{"withdrawable_total_cents",4500},{"counts",QJsonObject{{"confirmed",2},{"eligible",1},{"attention",2}}}}}};
      else if (input.startsWith("POST /api/campaigns/pause ") || input.startsWith("POST /api/campaigns/resume ") || input.startsWith("POST /api/campaigns/stop ")) {
        ++flow->posts;
        if (flow->reject) { flow->reject=false; status=409; payload={{"error","Controle recusado na simulação"}}; }
        else {
          if (input.startsWith("POST /api/campaigns/stop ")) { flow->state="stopping"; flow->paused=false; }
          else flow->paused=input.startsWith("POST /api/campaigns/pause ");
          payload={{"ok",true}};
        }
        delay=20;
      } else { qCritical()<<"Unexpected operation QA request"<<input.left(input.indexOf('\r')); qApp->exit(202); return; }
      const auto body=QJsonDocument(payload).toJson(QJsonDocument::Compact);
      const auto response="HTTP/1.1 "+QByteArray::number(status)+" OK\r\nContent-Type: application/json\r\nConnection: close\r\nContent-Length: "+QByteArray::number(body.size())+"\r\n\r\n"+body;
      QTimer::singleShot(delay,socket,[socket,response] { socket->write(response); socket->disconnectFromHost(); });
    });
    QObject::connect(socket,&QTcpSocket::disconnected,socket,&QObject::deleteLater);
  });
  window._api.setBaseUrl(QStringLiteral("http://127.0.0.1:%1").arg(server->serverPort()));
  window.loadHome();
  auto phase=std::make_shared<int>(0);
  auto previousReads=std::make_shared<int>(0);
  auto* poll=new QTimer(&window);
  QObject::connect(poll,&QTimer::timeout,&window,[&window,flow,phase,previousReads,poll] {
    auto require=[phase](bool ok,const char* detail) { if (!ok) { qCritical()<<"Operation QA phase"<<*phase<<detail; qApp->exit(203); } return ok; };
    if (*phase==0 && window._operationAvailable && window._operationBalance->text().contains("45")) {
      if (!require(window._operationTable->rowCount()==48 && window._operationPause->isEnabled() && !window._operationCreate->isEnabled()
          && window._homePulseProgress->value()==50 && window._operationTotal->text().contains("36 / 72"),"initial state / eligible amount")) return;
      if (!require(window._operationTable->item(0,2)->text()=="100%" && window._operationTable->item(5,2)->text()==QStringLiteral("—"),"transfer is distinct from receipt")) return;
      window._operationSearch->setText("CLIP-47");
      int visible=0;for(int i=0;i<48;++i) visible+=!window._operationTable->isRowHidden(i);
      if (!require(visible==1 && !window._operationTable->isRowHidden(47),"search by clip is case insensitive")) return;
      window._operationSearch->clear(); window._operationAttention->setChecked(true);
      visible=0;for(int i=0;i<48;++i) visible+=!window._operationTable->isRowHidden(i);
      if (!require(visible==18 && !window._operationTable->isRowHidden(5) && !window._operationTable->isRowHidden(11),"pending rows, recovery and previous failures")) return;
      window._operationSearch->setText("no-such-account");
      if (!require(!window._operationEmpty->isHidden() && window._operationEmpty->text().contains("filtros"),"empty filter feedback")) return;
      window._operationSearch->clear(); window._operationAttention->setChecked(false);
      window._operationTable->setCurrentCell(5,0);
      bool reviewed=false;
      QTimer::singleShot(0,&window,[&window,&reviewed] {
        auto* dialog=window.findChild<QDialog*>(QStringLiteral("operationAccountDialog"));
        if (dialog) { auto* text=dialog->findChild<QPlainTextEdit*>(); reviewed=text && text->toPlainText().contains("receipt-5") && text->toPlainText().contains("Falhas: 1"); dialog->reject(); }
      });
      window._operationDetails->click();
      if (!require(reviewed,"account details expose full identity and results")) return;
      window._operationTable->setCurrentCell(38,0); window._operationTable->scrollToBottom();
      const auto selected=window._operationTable->item(38,0)->text();
      const int scroll=window._operationTable->verticalScrollBar()->value();
      auto snap=operationFixture(); auto op=snap.value("operation").toObject(); auto rows=op.value("accounts").toArray();
      QJsonArray reverse; for(int i=rows.size()-1;i>=0;--i) reverse.append(rows[i]); op["accounts"]=reverse;snap["operation"]=op;
      window.renderOperation(snap);
      if (!require(window._operationTable->item(window._operationTable->currentRow(),0)->text()==selected
          && window._operationTable->verticalScrollBar()->value()==scroll,"refresh preserves selection by identity and scroll")) return;
      flow->malformed=true; *phase=1; window.refreshOperation();
    } else if (*phase==1 && !window._operationPolling && !window._operationAvailable) {
      if (!require(window._operationTable->rowCount()==48 && !window._operationPause->isEnabled() && !window._operationStop->isEnabled()
          && !window._operationCreate->isEnabled() && !window._operationNotice->isHidden(),"malformed response preserves rows and blocks controls")) return;
      flow->malformed=false; *phase=2; window.refreshOperation();
    } else if (*phase==2 && window._operationAvailable && !window._operationPolling) {
      if (!require(window._operationNotice->isHidden() && window._operationPause->isEnabled(),"recovery restores controls")) return;
      flow->delay=450; *previousReads=flow->reads; *phase=3; window.refreshOperation();
    } else if (*phase==3 && flow->reads>*previousReads) {
      *phase=4; window._operationPause->click(); window._operationPause->click(); window._operationStop->click();
      QTimer::singleShot(600,&window,[phase] { if (*phase==4) *phase=5; });
    } else if (*phase==5 && !window._operationPausePending) {
      if (!require(flow->posts==1 && window._operationPauseRequested && window._operationPause->text()=="Retomar"
          && window._operationLive->text()=="Pausa solicitada", "late GET cannot undo pause / duplicate controls")) return;
      *phase=6; window._operationPause->click(); window._operationPause->click();
    } else if (*phase==6 && !window._operationPauseRequested && !window._operationPausePending && !window._operationPolling) {
      if (!require(flow->posts==2 && window._operationLive->text()=="Ao vivo","resume")) return;
      flow->reject=true; *phase=7; window._operationPause->click();
    } else if (*phase==7 && !window._operationPausePending && !window._operationPolling && window._operationAvailable) {
      if (!require(flow->posts==3 && !window._operationNotice->isHidden() && window._operationNotice->text().contains("recusado")
          && !window._operationPauseRequested,"control failure is visible and never retried automatically")) return;
      *phase=8; window._operationStop->click(); window._operationStop->click(); window._operationPause->click();
    } else if (*phase==8 && window._operationSnapshot.value("state")=="stopping") {
      if (!require(flow->posts==4 && !window._operationStop->isEnabled() && !window._operationPause->isEnabled()
          && !window._operationCreate->isEnabled() && window._operationLive->text()=="Encerrando","safe stop acknowledges pending completion")) return;
      flow->state="stopped"; *phase=9; window.refreshOperation();
    } else if (*phase==9 && window._operationSnapshot.value("state")=="stopped") {
      if (!require(window._operationCreate->isEnabled() && !window._operationPause->isEnabled() && window._homeDestination==7
          && window._homePulseTitle->text().contains("interrompida"),"terminal state routes to history")) return;
      window.resize(980,680); *phase=10;
      QTimer::singleShot(150,&window,[&window,phase] {
        const auto top=window._operationTable->mapTo(&window,QPoint(0,0)).y();
        if (top>window.height()-140) { qCritical()<<"Accounts below compact viewport"<<top; qApp->exit(204);return; }
        if (window._operationSearch->focusPolicy()==Qt::NoFocus || window._operationDetails->focusPolicy()==Qt::NoFocus) {qApp->exit(205);return;}
        *phase=11;
      });
    } else if (*phase==11) {
      flow->state="running"; window.renderOperation(operationFixture());
      window._operationPause->click();
      window.setBackendReady(false,QStringLiteral("Motor interrompido durante o comando"));
      *phase=12;
      QTimer::singleShot(200,&window,[phase] {if(*phase==12)*phase=13;});
    } else if (*phase==13 && !window._operationPausePending) {
      if (!require(!window._operationAvailable && !window._operationPause->isEnabled() && !window._operationStop->isEnabled()
          && !window._operationCreate->isEnabled() && window._operationTable->rowCount()==48,"late control reply cannot revive a lost backend")) return;
      poll->stop(); qApp->exit(0);
    }
  });
  poll->start(20);
  QTimer::singleShot(9000,&window,[phase] {qCritical()<<"Operation QA timeout at phase"<<*phase;qApp->exit(206);});
}
