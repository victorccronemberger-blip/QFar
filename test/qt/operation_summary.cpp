#include "../../desktop/src/OperationSummary.hpp"
#include <iostream>
int main() {
  int failures = 0;
  auto expect = [&failures](bool ok) { if (!ok) ++failures; };
  const QJsonArray accounts{QJsonObject{{"email", "fixture@example.com"}}};
  expect(OperationSummary::from({}, {{"state", "idle"}}).destination == 5);
  expect(OperationSummary::from(accounts, {{"state", "idle"}}).destination == 1);
  expect(OperationSummary::from({}, {{"state", "running"}}).destination == 3);
  expect(OperationSummary::from(accounts, {{"state", "error"}}).destination == 3);
  expect(OperationSummary::from(accounts, {{"state", "stopping"}}).title.contains(QStringLiteral("Encerrando")));
  expect(OperationSummary::from(accounts, {{"state", "running"}, {"totals", QJsonObject{{"total_sends", 4}, {"ok_sends", 2}}}}).progress == 50);
  expect(OperationSummary::from(accounts, {{"totals", QJsonObject{{"total_sends", 0}, {"ok_sends", 5}}}}).progress == 0);
  expect(OperationSummary::from(accounts, {{"totals", QJsonObject{{"total_sends", 1}, {"ok_sends", 5}}}}).progress == 100);
  expect(OperationSummary::from(accounts, {{"totals", QJsonObject{{"total_sends", 4}, {"done_sends", 100}, {"ok_sends", 0}}}}).progress == 0);
  expect(OperationSummary::from(accounts, {{"totals", QJsonObject{{"progress_target", 7200}, {"progress_completed", 900}, {"done_sends", 100}}}}).progress == 12);
  expect(OperationSummary::from(accounts, {{"state","running"},{"pause_requested",true}}).title == QStringLiteral("Pausa solicitada"));
  expect(OperationSummary::from(accounts, {{"state","done"}}).destination == 7);
  expect(OperationSummary::from(accounts, {{"state","stopped"}}).title.contains(QStringLiteral("interrompida")));
  expect(OperationSummary::from(accounts, {{"state","done"},{"totals",QJsonObject{{"failed_sends",1}}}}).title.contains(QStringLiteral("pendências")));
  const QJsonObject row{{"email","fixture@example.invalid"},{"state","sending"},{"progress",100}};
  QJsonObject valid{{"state","running"},{"pause_requested",false},{"totals",QJsonObject{{"total_sends",2},{"ok_sends",1}}},
    {"operation",QJsonObject{{"accounts",QJsonArray{row}},{"counts",QJsonObject{{"sending",1}}}}}};
  expect(OperationSummary::valid(valid));
  expect(!OperationSummary::valid({}));
  auto terminal=valid;terminal["state"]="done";expect(!OperationSummary::valid(terminal));
  for (const auto state : {"unknown", "", "RUNNING"}) { auto bad=valid;bad["state"]=state;expect(!OperationSummary::valid(bad)); }
  for (const auto value : {QJsonValue(true),QJsonValue(-1),QJsonValue(101),QJsonValue("50")}) {
    auto badRow=row;badRow["progress"]=value;auto bad=valid;bad["operation"]=QJsonObject{{"accounts",QJsonArray{badRow}},{"counts",QJsonObject{}}};expect(!OperationSummary::valid(bad));
  }
  for (const auto value : {QJsonValue(true),QJsonValue(-1),QJsonValue(1.5),QJsonValue("10"),QJsonValue(9007199254740992.)}) {
    auto bad=valid;bad["totals"]=QJsonObject{{"ok_sends",value}};expect(!OperationSummary::valid(bad));
  }
  auto bad=valid;bad["operation"]=QJsonObject{{"accounts",QJsonArray{row,row}},{"counts",QJsonObject{}}};expect(!OperationSummary::valid(bad));
  bad=valid;bad["pause_requested"]="false";expect(!OperationSummary::valid(bad));
  bad=valid;bad["events"]=QJsonArray{QJsonObject{{"ts",1e50},{"title","bad time"}}};expect(!OperationSummary::valid(bad));
  expect(OperationSummary::from(accounts,{{"totals",QJsonObject{{"progress_target",1e-100},{"progress_completed",1e100}}}}).progress==100);
  if (failures) std::cerr << failures << " operation summary assertions failed\n";
  return failures ? 1 : 0;
}
