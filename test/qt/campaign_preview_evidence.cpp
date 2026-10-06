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
#include "../../desktop/src/CampaignReviewDialog.hpp"

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
  QLabel stage{QStringLiteral("Com pendências")},current{QStringLiteral("44 contas abaixo da meta")},
      stats{QStringLiteral("Envios nesta execução: 41 concluídos · 0 ignorados · 3 falhas")},status,
      indicator{QStringLiteral("3.60 / 352 h · 1%")};
  QLabel previewStage,previewDetail,previewStats,receiptsLabel;
  QWidget previewPanel;
  QProgressBar progress,previewProgress;
  QPlainTextEdit feed;
  QLabel *_campaignStage=&stage,*_campaignCurrent=&current,*_campaignStats=&stats;
  QProgressBar *_campaignProgress=&progress;
  QWidget* _campaignPreviewPanel=&previewPanel;
  QLabel *_campaignPreviewStage=&previewStage,*_campaignPreviewDetail=&previewDetail,
      *_campaignPreviewStats=&previewStats,*_campaignReceiptStats=&receiptsLabel;
  QProgressBar *_campaignPreviewProgress=&previewProgress;
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
    check(window.previewStage.text().contains("Prévias prontas"),"preview readiness has its own label");
    check(window.stage.text()=="Com pendências" && !window.status.text().contains("Campanha concluída"),"preview never completes campaign");
    check(window.receiptsLabel.text().contains("recibo",Qt::CaseInsensitive),"receipt evidence remains visible separately");
    check(window.stats.text().contains("3 falhas") && window.current.text().contains("44 contas"),"attempt outcomes and goal shortfall are preserved");
    if(variant!=1) check(window.previewStage.text().contains("pendências"),"uncertain receipt stays uncertain");
  }
  for (const auto malformed : {QJsonObject{},QJsonObject{{"total",1},{"ready",2},{"pending",0},{"unavailable",0},{"errors",0},{"transient_errors",0}},
                               QJsonObject{{"total",1},{"ready",1},{"pending",0},{"unavailable",0},{"errors",0},{"transient_errors",1}}}) {
    MainWindow window; window._api.response={{"summary",malformed},{"delivery_summary",receipts(1,0)}};
    window.pollCampaignPreviews();
    check(!window._previewLogName.isEmpty(),"malformed reply keeps query available for retry");
    check(window.previewStage.text().contains("não confirmada"),"malformed reply is not terminal success");
    check(!window.status.text().contains("concluída",Qt::CaseInsensitive),"malformed reply never claims conclusion");
  }
  MainWindow empty;
  empty._api.response={{"summary",QJsonObject{{"total",0},{"ready",0},{"pending",0},{"unavailable",0},{"errors",0},{"transient_errors",0}}},
                       {"delivery_summary",receipts(0,1)}};
  empty.pollCampaignPreviews();
  check(empty.previewStage.text().contains("Sem prévias"),"empty preview is not proof of no upload");
  check(!empty.status.text().contains("enviados"),"empty preview does not deny transport");
  MainWindow screenshot;
  screenshot.progress.setValue(1);
  screenshot._api.response={{"attempt_status","partial"},
      {"summary",QJsonObject{{"total",44},{"ready",41},{"pending",3},{"unavailable",0},{"errors",0},{"transient_errors",0}}},
      {"delivery_summary",QJsonObject{{"confirmed",41},{"pending",1},{"review",2},{"unknown",0}}}};
  screenshot.pollCampaignPreviews();
  check(screenshot.stats.text().contains("41 concluídos") && screenshot.stats.text().contains("3 falhas"),"screenshot attempt counters survive preview refresh");
  check(screenshot.progress.value()==1 && screenshot.previewProgress.value()==93,"hour goal and preview progress remain independent");
  check(screenshot.indicator.text()=="3.60 / 352 h · 1%","preview never replaces the campaign indicator");
  check(screenshot.receiptsLabel.text().contains("41 confirmado") && screenshot.receiptsLabel.text().contains("1 pendente")
      && screenshot.receiptsLabel.text().contains("2 para revisar"),"screenshot receipt evidence has its own counts");
  check(screenshot.previewStats.text().contains("3 aguardando publicação") && !screenshot.previewStats.text().contains("processando"),"unpublished preview is not assumed to be active processing");
  MainWindow unavailable;
  unavailable._api.response={{"summary",QJsonObject{{"total",2},{"ready",1},{"pending",0},{"unavailable",1},{"errors",0},{"transient_errors",0}}},
      {"delivery_summary",receipts(2,0)}};
  unavailable.pollCampaignPreviews();
  check(unavailable.previewProgress.value()==50 && unavailable.previewStage.text().contains("Atenção"),"unavailable preview does not count as published in the availability progress");
  check(unavailable.stats.text().contains("3 falhas"),"terminal preview warning preserves original upload failures");
  auto capacityFixture=[](double seconds) {
    QJsonArray rows;
    for(int i=0;i<44;++i) rows.append(QJsonObject{{"email",QStringLiteral("fixture-%1@example.invalid").arg(i)},
        {"available_seconds",seconds},{"unique_footage_upper_bound_seconds",seconds},{"unique_footage_seconds",seconds},{"estimated_executable_seconds",seconds},
        {"shortfall_seconds",qMax(0.,28800.-seconds)},{"eligible_clips",seconds>=28800?32:1},{"estimated_sends",seconds>=28800?32:1},
        {"recorded_clips",0},{"pending_clips",0}});
    return QJsonObject{{"ok",true},{"accounts",QJsonObject{{"validated",44}}},{"tasks",QJsonObject{{"compatible",10}}},
        {"clips",seconds>=28800?32:1},{"estimated_sends",seconds>=28800?1408:44},
        {"capacity",QJsonObject{{"schema",1},{"known",true},{"basis","estimated_admissible_delivery_duration_before_preparation"},
            {"target_seconds_per_account",28800},{"can_reach_goal",seconds>=28800},
            {"available_seconds_min",seconds},{"available_seconds_max",seconds},{"total_available_seconds",seconds*44},
            {"shortfall_account_count",seconds>=28800?0:44},{"estimated_sends",seconds>=28800?1408:44},
            {"candidate_clips",seconds>=28800?32:1},{"accounts",rows}}}};
  };
  QStringList accounts; for(int i=0;i<44;++i) accounts<<QStringLiteral("fixture-%1@example.invalid").arg(i);
  const QJsonObject requested{{"target_hours",8},{"min_dur_s",300},{"max_dur_s",1800}};
  auto insufficient=capacityFixture(316);
  CampaignReviewDialog blocked(insufficient,accounts,nullptr,requested);
  check(!blocked.findChild<QPushButton*>("campaignReviewConfirm")->isEnabled(),"8h goal with316s pool cannot confirm even if server incorrectly says ok");
  auto* capacitySummary=blocked.findChild<QLabel*>("campaignCapacitySummary");
  check(capacitySummary && capacitySummary->text().contains("8.00") && capacitySummary->text().contains("0.09")
      && capacitySummary->text().contains("44 conta"),"review displays requested goal and real per-account shortage");
  auto* capacityTable=blocked.findChild<QTableWidget*>("campaignCapacityTable");
  check(capacityTable && capacityTable->rowCount()==44 && capacityTable->item(0,1)->text()=="0.09"
      && capacityTable->item(0,2)->text()=="7.91","review exposes capacity and deficit per account");
  for(int variant=0;variant<4;++variant) {
    auto invalid=capacityFixture(28800);
    auto capacity=invalid.value("capacity").toObject();
    if(variant==0) invalid.remove("capacity");
    if(variant==1) {capacity["known"]=false;invalid["capacity"]=capacity;}
    if(variant==2) {capacity["target_seconds_per_account"]=3600;invalid["capacity"]=capacity;}
    if(variant==3) {capacity["available_seconds_min"]="28800";invalid["capacity"]=capacity;}
    CampaignReviewDialog invalidDialog(invalid,accounts,nullptr,requested);
    check(!invalidDialog.findChild<QPushButton*>("campaignReviewConfirm")->isEnabled(),"missing unknown stale or malformed capacity fails closed for positive goal");
  }
  auto sufficient=capacityFixture(28800);
  CampaignReviewDialog allowed(sufficient,accounts,nullptr,requested);
  check(allowed.findChild<QPushButton*>("campaignReviewConfirm")->isEnabled(),"confirmed sufficient capacity permits review confirmation");
  auto overlapAllowed=sufficient;
  auto overlapCapacity=overlapAllowed.value("capacity").toObject();
  auto overlapAccounts=overlapCapacity.value("accounts").toArray();
  for(int i=0;i<overlapAccounts.size();++i) {
    auto row=overlapAccounts[i].toObject(); row["unique_footage_seconds"]=21600;
    row["unique_footage_upper_bound_seconds"]=21600; overlapAccounts[i]=row;
  }
  overlapCapacity["accounts"]=overlapAccounts; overlapAllowed["capacity"]=overlapCapacity;
  CampaignReviewDialog admissibleOverlap(overlapAllowed,accounts,nullptr,requested);
  check(admissibleOverlap.findChild<QPushButton*>("campaignReviewConfirm")->isEnabled(),"admissible delivery duration is not falsely blocked by the smaller source-footage union");
  sufficient["blockers"]=QJsonArray{"readiness fixture blocker"};
  CampaignReviewDialog prerequisite(sufficient,accounts,nullptr,requested);
  check(!prerequisite.findChild<QPushButton*>("campaignReviewConfirm")->isEnabled(),"independent backend blocker still prevents confirmation");
  CampaignReviewDialog noGoal(QJsonObject{{"ok",true}},accounts,nullptr,QJsonObject{{"target_hours",0}});
  check(noGoal.findChild<QPushButton*>("campaignReviewConfirm")->isEnabled(),"explicit zero goal remains supported without a hours capacity requirement");
  std::cout<<"checks="<<checks<<" passed="<<checks-failures<<" failed="<<failures<<'\n';
  return failures?1:0;
}
