  static void nymeriaLibrarySmoke(MainWindow& window) {
    // The entire acquisition uses loopback fixtures, with no downloads or campaign.
    // A selected campaign ID names an activity; its broad dataset scenario
    // cannot identify a task in the Library's canonical task-name list.
    const auto previousRecords=window._taskRecords;
    const auto previousSelected=window._campaignSelectedTaskIds;
    const bool previousTouched=window._campaignTaskSelectionTouched;
    window._taskRecords=QJsonArray{
        QJsonObject{{"id","manual-selected"},{"name","Fixture task"},{"scenario","Cooking"}},
        QJsonObject{{"id","manual-unselected"},{"name","Other fixture task"},{"scenario","Cleaning"}}};
    window._campaignSelectedTaskIds=QSet<QString>{"manual-selected"};
    window._campaignTaskSelectionTouched=true;
    const QJsonObject prefill{{"items",QJsonArray{}},{"total",0},{"offset",window._nymeriaOffset},
        {"task_names",QJsonArray{"Fixture task","Other fixture task"}},
        {"summary",QJsonObject{{"sequence_count",0}}}};
    window._nymeriaTasks->clear();
    window.renderNymeriaLibrary(prefill);
    if(window._nymeriaTasks->count()!=2 || window._nymeriaTasks->item(0)->checkState()!=Qt::Checked
        || window._nymeriaTasks->item(1)->checkState()!=Qt::Unchecked
        || window._nymeriaTasks->item(0)->data(Qt::UserRole).toString()!="Fixture task") {qApp->exit(199);return;}
    window._nymeriaTasks->item(0)->setCheckState(Qt::Unchecked);
    window._nymeriaTasks->item(1)->setCheckState(Qt::Checked);
    window.renderNymeriaLibrary(prefill);
    if(window._nymeriaTasks->item(0)->checkState()!=Qt::Unchecked
        || window._nymeriaTasks->item(1)->checkState()!=Qt::Checked) {qApp->exit(200);return;}
    window._nymeriaTasks->clear();
    window._campaignTaskSelectionTouched=false;
    window.renderNymeriaLibrary(prefill);
    if(window._nymeriaTasks->item(0)->checkState()!=Qt::Checked
        || window._nymeriaTasks->item(1)->checkState()!=Qt::Checked) {qApp->exit(201);return;}
    window._taskRecords=previousRecords;
    window._campaignSelectedTaskIds=previousSelected;
    window._campaignTaskSelectionTouched=previousTouched;
    window._nymeriaTasks->clear();
    window._nymeriaTable->setRowCount(0);
    window._nymeriaTotal=0;
    struct Flow {
      int phase=0, reads=0, imports=0, syncs=0, plans=0, downloads=0, stops=0, jobReads=0;
      QJsonObject planBody;
      QJsonObject worker{{"state","idle"},{"running",false},{"stop_requested",false},{"message",""}};
    };
    auto flow=std::make_shared<Flow>();
    const bool malformed=qApp->arguments().contains("--nymeria-plan-malformed");
    const bool spaceMalformed=qApp->arguments().contains("--nymeria-space-malformed");
    const bool spaceInsufficient=qApp->arguments().contains("--nymeria-space-insufficient");
    auto* server=new QTcpServer(&window);
    if(!server->listen(QHostAddress::LocalHost)) {qApp->exit(180);return;}
    QObject::connect(server,&QTcpServer::newConnection,server,[server,flow,malformed,spaceMalformed,spaceInsufficient] {
      auto* socket=server->nextPendingConnection();
      QObject::connect(socket,&QTcpSocket::readyRead,socket,[socket,flow,malformed,spaceMalformed,spaceInsufficient] {
        const auto input=socket->property("input").toByteArray()+socket->readAll(); socket->setProperty("input",input);
        const int end=input.indexOf("\r\n\r\n");
        if(end<0 || socket->property("answered").toBool()) return;
        const auto match=QRegularExpression("Content-Length: (\\d+)",QRegularExpression::CaseInsensitiveOption).match(QString::fromLatin1(input.left(end)));
        if(match.hasMatch() && input.size()<end+4+match.captured(1).toInt()) return;
        socket->setProperty("answered",true);
        const auto words=input.split(' ');
        const QUrl url(QStringLiteral("http://fixture")+QString::fromUtf8(words.value(1)));
        const QUrlQuery query(url);
        const auto body=QJsonDocument::fromJson(input.mid(end+4)).object();
        QJsonObject result; int status=200;
        if(url.path()=="/api/library/nymeria/sequences") {
          ++flow->reads;
          if(flow->reads==1) {status=202;result={{"loading",true},{"message","Lendo catálogo Nymeria"}};}
          else {
            const int offset=query.queryItemValue("offset").toInt();
            if(query.queryItemValue("limit")!="50" || query.queryItemValue("state")!="all") {qApp->exit(181);return;}
            QJsonArray items;
            for(int i=offset; i<offset+50; ++i) items.append(QJsonObject{{"seq_id",QStringLiteral("sequence-%1").arg(i)},
                {"script","Inert fixture activity"},{"state",i==0?"measured":"cataloged"},{"declared_duration_s",3600},{"download_bytes",1024}});
            result={{"items",items},{"total",1100},{"offset",offset},{"limit",50},{"task_names",QJsonArray{"Fixture task"}},
                {"summary",QJsonObject{{"sequence_count",1100},{"declared_hours",1100},{"atomic_action_count",42000},
                    {"disk_free_bytes",400.*1024*1024*1024},{"by_state",QJsonObject{{"cataloged",1097},{"downloaded",1},{"measured",2}}}}},
                {"worker",flow->worker}};
          }
        } else if(url.path()=="/api/library/nymeria/import") {
          if(body.value("path")!="C:/inert/nymeria_plus_download_urls.json") {qApp->exit(182);return;}
          ++flow->imports;
          flow->worker={{"state","done"},{"running",false},{"run_id","import-run"},{"operation","import"},{"message","Manifesto concluído"}};
          status=202;result={{"ok",true},{"worker",flow->worker}};
        } else if(url.path()=="/api/library/nymeria/sync") {
          if(!body.isEmpty()) {qApp->exit(183);return;}
          ++flow->syncs; flow->jobReads=0;
          flow->worker={{"state","running"},{"running",true},{"run_id","sync-run"},{"operation","sync"},{"message","Sincronizando"},{"completed",5},{"total",1100}};
          status=202;result={{"ok",true},{"worker",flow->worker}};
        } else if(url.path()=="/api/library/nymeria/operation") {
          if(flow->syncs && !flow->downloads && ++flow->jobReads>=2) {
            flow->worker.insert("running",false);flow->worker.insert("state","done");flow->worker.insert("message","Catálogo concluído");
          }
          result=flow->worker;
        } else if(url.path()=="/api/library/nymeria/plan") {
          ++flow->plans;
          if(body.value("task_names").toArray()!=QJsonArray{"Fixture task"} || body.value("target_seconds").toDouble()!=28800
              || body.value("max_dur_s").toInt()!=1800 || body.value("min_dur_s").toInt()!=(flow->phase>=6?360:300)
              || body.value("min_free_gb").toInt()!=(flow->phase>=6?55:50)) {qApp->exit(184);return;}
          if(flow->plans%2==1) {flow->planBody=body;status=202;result={{"loading",true},{"message","Planejando fontes"}};}
          else {
            if(body!=flow->planBody) {qApp->exit(185);return;}
            const bool spaceComplete=flow->phase>=6 || spaceMalformed || spaceInsufficient;
            const QJsonValue fits=spaceMalformed?QJsonValue("true"):spaceComplete?QJsonValue(!spaceInsufficient):QJsonValue(QJsonValue::Null);
            result={{"campaign_ready",malformed},{"fits_available_disk",fits},{"fits_source_downloads",true},
                {"space_check_complete",spaceComplete},{"extraction_bytes",spaceInsufficient?400.*1024*1024*1024:1024},
                {"unknown_extraction_seq_ids",spaceComplete?QJsonArray{}:QJsonArray{"sequence-2","sequence-8"}},
                {"target_found_in_annotations",true},
                {"potential_seconds",72000},{"selected_potential_seconds",30000},{"download_bytes",1024},
                {"disk_free_bytes",400.*1024*1024*1024},{"selected_seq_ids",QJsonArray{"sequence-2","sequence-8"}}};
          }
        } else if(url.path()=="/api/library/nymeria/download") {
          if(body.value("seq_ids").toArray()!=QJsonArray{"sequence-2","sequence-8"} || body.value("min_free_gb").toInt()!=55) {qApp->exit(186);return;}
          ++flow->downloads;
          flow->worker={{"state","running"},{"running",true},{"run_id","download-run"},{"operation","download"},{"message","Baixando e medindo fontes"}};
          status=202;result={{"ok",true},{"worker",flow->worker}};
        } else if(url.path()=="/api/library/nymeria/stop") {
          if(!body.isEmpty()) {qApp->exit(187);return;}
          ++flow->stops;
          flow->worker.insert("running",false);flow->worker.insert("state","stopped");flow->worker.insert("message","Arquivos verificados preservados");
          result={{"ok",true},{"worker",flow->worker}};
        } else if(words.value(0)=="POST" && url.path().startsWith("/api/campaigns")) {qApp->exit(188);return;}
        else {status=503;result={{"error","Inert unrelated read"}};}
        const auto bytes=QJsonDocument(result).toJson(QJsonDocument::Compact);
        socket->write("HTTP/1.1 "+QByteArray::number(status)+" OK\r\nContent-Type: application/json\r\nConnection: close\r\nContent-Length: "+QByteArray::number(bytes.size())+"\r\n\r\n"+bytes);
        socket->disconnectFromHost();
      });
      QObject::connect(socket,&QTcpSocket::disconnected,socket,&QObject::deleteLater);
    });
    window._api.setBaseUrl(QStringLiteral("http://127.0.0.1:%1").arg(server->serverPort()));
    window._backendReady=true;
    window._pages->setCurrentIndex(4); window._libraryTabs->setCurrentIndex(2);
    auto* timer=new QTimer(&window);
    QObject::connect(timer,&QTimer::timeout,&window,[&window,flow,malformed,spaceMalformed,spaceInsufficient] {
      if(QApplication::activeModalWidget()) {qApp->exit(189);return;}
      if(flow->phase==0 && window._nymeriaTasks->count()==1 && !window._nymeriaInventoryPending) {
        if(!window._nymeriaSummary->text().contains("1100 sequências") || !window._nymeriaNext->isEnabled()
            || !window._nymeriaSummary->text().contains("2 medidas")
            || window._nymeriaTable->item(0,3)->text()!="Vídeo e IMU medidos"
            || window._nymeriaFilter->findData("measured")<0 || window._nymeriaAcquire->isEnabled()
            || window._dataset->currentData().toString()!="ego4d") {qApp->exit(190);return;}
        ++flow->phase; window._nymeriaNext->click();
      } else if(flow->phase==1 && !window._nymeriaInventoryPending && window._nymeriaOffset==50) {
        if(window._nymeriaTable->item(0,0)->text()!="sequence-50" || !window._nymeriaPrevious->isEnabled()) {qApp->exit(191);return;}
        ++flow->phase; window._nymeriaPrevious->click();
      } else if(flow->phase==2 && !window._nymeriaInventoryPending && window._nymeriaOffset==0) {
        ++flow->phase; window.importNymeriaManifest("C:/inert/nymeria_plus_download_urls.json");
      } else if(flow->phase==3 && flow->imports==1 && !window._nymeriaCommandPending && !window._nymeriaInventoryPending) {
        ++flow->phase; window._nymeriaSync->click();
      } else if(flow->phase==4 && flow->syncs==1 && !window._nymeriaRunning && !window._nymeriaCommandPending) {
        ++flow->phase; window._nymeriaTasks->item(0)->setCheckState(Qt::Checked); window._nymeriaPlanButton->click();
      } else if(flow->phase==5 && flow->plans==2 && !window._nymeriaPlanPending) {
        if(malformed || spaceMalformed) {
          qApp->exit(window._nymeriaAcquire->isEnabled() || !window._nymeriaPlan.isEmpty() || flow->downloads?192:0);return;
        }
        if(spaceInsufficient) {
          qApp->exit(window._nymeriaAcquire->isEnabled() || window._nymeriaPlan.isEmpty()
              || !window._nymeriaPotential->text().contains("Espaço insuficiente") || flow->downloads?192:0);return;
        }
        if(!window._nymeriaAcquire->isEnabled() || !window._nymeriaPotential->text().contains("ainda precisam ser medidos")
            || !window._nymeriaPotential->text().contains("será conferido antes de baixar")
            || window._nymeriaAcquire->text()!="Verificar espaço e baixar antecipadamente") {qApp->exit(193);return;}
        ++flow->phase; window._nymeriaReserve->setValue(55);
        if(window._nymeriaAcquire->isEnabled() || !window._nymeriaPlan.isEmpty()) {qApp->exit(194);return;}
        window._nymeriaMinimum->setValue(6);
        if(window._nymeriaAcquire->isEnabled() || !window._nymeriaPlan.isEmpty()) {qApp->exit(194);return;}
        window._nymeriaPlanButton->click();
      } else if(flow->phase==6 && flow->plans==4 && window._nymeriaAcquire->isEnabled()) {
        if(window._nymeriaAcquire->text()!="Baixar plano antecipadamente") {qApp->exit(193);return;}
        ++flow->phase; window._nymeriaAcquire->click();
      } else if(flow->phase==7 && flow->downloads==1 && window._nymeriaStop->isEnabled()) {
        if(window._nymeriaSync->isEnabled() || window._nymeriaImport->isEnabled() || window._nymeriaAcquire->isEnabled()) {qApp->exit(195);return;}
        ++flow->phase; window._nymeriaStop->click();
      } else if(flow->phase==8 && flow->stops==1 && !window._nymeriaRunning && !window._nymeriaCommandPending) {
        if(!window._nymeriaState->text().contains("preservados") || window._nymeriaAcquire->isEnabled()
            || window._dataset->currentData().toString()!="ego4d") {qApp->exit(196);return;}
        window._nymeriaUseCombined->click();
        qApp->exit(window._dataset->currentData().toString()=="ambos"?0:197);
      }
    });
    timer->start(25);
    QTimer::singleShot(15000,&window,[]{qApp->exit(198);});
  }
