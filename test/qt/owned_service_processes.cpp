#include "../../desktop/src/OwnedServiceProcesses.hpp"
#include <QCoreApplication>
#include <iostream>

int main(int argc, char** argv) {
  QCoreApplication app(argc, argv);
  using qmoney::service::ProcessFact;
  using qmoney::service::selectOwnedServiceTree;
  const QString expected = QStringLiteral("C:/fixture/current/QMoneyService.exe");
  const QByteArray owner("fixture-owner");
  using qmoney::service::isExpectedServiceExecutable;
  const QString versioned=QStringLiteral("C:/fixture/current/QMoneyService-2.0.27.exe");
  if (!isExpectedServiceExecutable(versioned,versioned)
      || !isExpectedServiceExecutable(expected,expected)
      || isExpectedServiceExecutable(QStringLiteral("C:/fixture/other/QMoneyService-2.0.27.exe"),versioned)
      || isExpectedServiceExecutable(QStringLiteral("C:/fixture/current/QMoneyService-2.0.27-other.exe"),versioned)
      || isExpectedServiceExecutable(QStringLiteral("QMoneyService-2.0.27.exe"),versioned)
      || isExpectedServiceExecutable(versioned,{})
      || isExpectedServiceExecutable(versioned,expected)) return 1;
  QVector<ProcessFact> facts{
    {100,10,200,expected,owner,true}, {101,100,210,expected,owner,true},
    {102,101,220,expected,owner,true},
    {103,100,230,QStringLiteral("C:/fixture/other/QMoneyService.exe"),owner,true},
    {104,103,240,expected,owner,true},
    {105,100,230,expected,QByteArray("other-owner"),true},
    {106,105,240,expected,owner,true},
    {107,100,190,expected,owner,true}, // stale parent PID relation
    {108,100,230,expected,{},false}, // unavailable owner/path
  };
  const auto selected = selectOwnedServiceTree(facts,100,10,100,owner,expected);
  if (selected.size()!=3 || selected[0].pid!=102 || selected[1].pid!=101 || selected[2].pid!=100) return 1;
  for (const auto& variant : QVector<ProcessFact>{
      {100,999,200,expected,owner,true}, // orphan: no ownership inference
      {100,20,200,expected,owner,true}, // another active parent
      {100,10,200,expected,QByteArray("other-owner"),true},
      {100,10,200,QStringLiteral("C:/fixture/other/QMoneyService.exe"),owner,true},
      {100,10,200,expected,{},false},
      {100,10,50,expected,owner,true}, // reused app PID
  }) {
    auto copy=facts;copy[0]=variant;
    if (!selectOwnedServiceTree(copy,100,10,100,owner,expected).isEmpty()) return 1;
  }
  if (!selectOwnedServiceTree(facts,0,10,100,owner,expected).isEmpty()
      || !selectOwnedServiceTree(facts,100,10,0,owner,expected).isEmpty()
      || !selectOwnedServiceTree(facts,100,10,100,{},expected).isEmpty()
      || !selectOwnedServiceTree(facts,100,10,100,owner,{}).isEmpty()) return 1;
  std::cout << "Owned process facts: descendants selected, foreign/unknown/orphan/reused PIDs preserved; no real process queried or terminated\n";
  return 0;
}
