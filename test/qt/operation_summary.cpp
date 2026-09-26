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
  expect(OperationSummary::from(accounts, {{"state", "running"}, {"totals", QJsonObject{{"total_sends", 4}, {"done_sends", 2}}}}).progress == 50);
  expect(OperationSummary::from(accounts, {{"totals", QJsonObject{{"total_sends", 0}, {"done_sends", 5}}}}).progress == 0);
  expect(OperationSummary::from(accounts, {{"totals", QJsonObject{{"total_sends", 1}, {"done_sends", 5}}}}).progress == 100);
  if (failures) std::cerr << failures << " operation summary assertions failed\n";
  return failures ? 1 : 0;
}
