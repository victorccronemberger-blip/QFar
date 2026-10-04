// Exact production preview method in an isolated widget shell, with inert API.
// No service, account, network request or provider execution.
#include <QApplication>
#include <QJsonDocument>
#include <QJsonObject>
#include <QLabel>
#include <QPlainTextEdit>
#include <QProgressBar>
#include <QSettings>
#include <QTemporaryDir>
#include <QTimer>
#include <QUrl>
#include <cmath>
#include <functional>
#include <iostream>

static QString encoded(const QString& text) { return QString::fromLatin1(QUrl::toPercentEncoding(text)); }
struct InertApi {
  QJsonObject response;
  int requests=0;
  void post(const QString& path, const QJsonObject&, std::function<void(bool,const QJsonDocument&,const QString&)> callback) {
    ++requests;
    if (!path.startsWith("/api/logs/") || !path.endsWith("/status")) throw std::runtime_error("Unexpected inert API route");
    callback(true,QJsonDocument(response),QString());
  }
};
class MainWindow {
public:
  InertApi _api;
  bool _campaignActive=false, _campaignPreflightPending=false, _campaignStartPending=false, _previewCheckActive=false;
  int _campaignPollRevision=0;
  QString _previewLogName="campaign_fixture.json";
  QTimer _previewPoll;
  QLabel stage,current,stats,status,indicator;
  QProgressBar progress;
  QPlainTextEdit feed;
  QLabel *_campaignStage=&stage,*_campaignCurrent=&current,*_campaignStats=&stats;
  QProgressBar *_campaignProgress=&progress;
  QPlainTextEdit *_campaignFeed=&feed;
  void setStatus(const QString& text) { status.setText(text); }
  void setCampaignIndicator(const QString& title,const QString&,const QString& state,bool=false) {
    indicator.setText(title); indicator.setProperty("state",state);
  }
  void pollCampaignPreviews();
};
#include "campaign-preview-methods.inc"

int main(int argc,char** argv) {
  QApplication app(argc,argv);
  app.setOrganizationName("QMoneyCampaignEvidenceTests"); app.setApplicationName("PreviewEvidence");
  QTemporaryDir settings;
  QSettings::setDefaultFormat(QSettings::IniFormat);
  QSettings::setPath(QSettings::IniFormat,QSettings::UserScope,settings.path());
  int checks=0, failures=0;
  auto check=[&](bool passed,const char* name) { ++checks; if(!passed) {++failures; std::cerr<<"FAIL "<<name<<'\n';} };
  auto ready=[] {return QJsonObject{{"total",1},{"ready",1},{"pending",0},{"unavailable",0},{"errors",0},{"transient_errors",0}};};
  auto receipts=[](int confirmed,int unknown) {return QJsonObject{{"confirmed",confirmed},{"pending",0},{"review",0},{"unknown",unknown}};};
  for (int variant=0;variant<4;++variant) {
    MainWindow window;
    window._api.response={{"summary",ready()},{"attempt_status",variant==0?"partial":"done"}};
    if(variant!=3) window._api.response["delivery_summary"]=receipts(1,variant==0?1:0);
    if(variant==2) window._api.response["delivery_summary"]=QJsonObject{{"confirmed","1"},{"pending",0},{"review",0},{"unknown",0}};
    window.pollCampaignPreviews();
    check(window._api.requests==1,"one inert preview query");
    check(window._previewLogName.isEmpty(),"completed preview polling stopped");
    check(window.stage.text().contains("Prévias prontas"),"preview readiness has its own label");
    check(!window.stage.text().contains("Concluída") && !window.status.text().contains("Campanha concluída"),"preview never completes campaign");
    check(window.current.text().contains("recibo",Qt::CaseInsensitive),"receipt evidence remains visible");
    if(variant!=1) check(window.stage.text().contains("pendências"),"uncertain receipt stays uncertain");
  }
  for (const auto malformed : {QJsonObject{},QJsonObject{{"total",1},{"ready",2},{"pending",0},{"unavailable",0},{"errors",0},{"transient_errors",0}},
                               QJsonObject{{"total",1},{"ready",1},{"pending",0},{"unavailable",0},{"errors",0},{"transient_errors",1}}}) {
    MainWindow window; window._api.response={{"summary",malformed},{"delivery_summary",receipts(1,0)}};
    window.pollCampaignPreviews();
    check(!window._previewLogName.isEmpty(),"malformed reply keeps query available for retry");
    check(window.stage.text().contains("não confirmada"),"malformed reply is not terminal success");
    check(!window.status.text().contains("concluída",Qt::CaseInsensitive),"malformed reply never claims conclusion");
  }
  MainWindow empty;
  empty._api.response={{"summary",QJsonObject{{"total",0},{"ready",0},{"pending",0},{"unavailable",0},{"errors",0},{"transient_errors",0}}},
                       {"delivery_summary",receipts(0,1)}};
  empty.pollCampaignPreviews();
  check(empty.stage.text().contains("Sem prévias"),"empty preview is not proof of no upload");
  check(!empty.status.text().contains("enviados"),"empty preview does not deny transport");
  std::cout<<"checks="<<checks<<" passed="<<checks-failures<<" failed="<<failures<<'\n';
  return failures?1:0;
}
