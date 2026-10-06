#include "../../desktop/src/ApiClient.hpp"
#include <QCoreApplication>
#include <QEventLoop>
#include <QJsonObject>
#include <QElapsedTimer>
#include <QTcpServer>
#include <QTcpSocket>
#include <QTimer>
#include <iostream>

bool checkResponse(const QByteArray& body, int status, bool expected,
                   bool post=false, const QString& path=QStringLiteral("/api/test")) {
  QTcpServer server;
  if (!server.listen(QHostAddress::LocalHost, 0)) return false;
  QObject::connect(&server, &QTcpServer::newConnection, &server, [&] {
    auto* socket = server.nextPendingConnection();
    QObject::connect(socket, &QTcpSocket::readyRead, socket, [socket, body, status] {
      const QByteArray request = socket->readAll();
      if (!request.toLower().contains("x-qmoney-session: fixture-session")) {
        socket->disconnectFromHost();
        return;
      }
      socket->write("HTTP/1.1 " + QByteArray::number(status) + " Test\r\nContent-Type: application/json\r\nContent-Length: "
                    + QByteArray::number(body.size()) + "\r\nConnection: close\r\n\r\n" + body);
      socket->disconnectFromHost();
    });
  });
  ApiClient api;
  api.setSessionToken("fixture-session");
  api.setBaseUrl(QStringLiteral("http://127.0.0.1:%1").arg(server.serverPort()));
  QEventLoop loop;
  bool passed = false;
  const auto callback = [&](bool ok, const QJsonDocument& doc, const QString& error) {
    passed = ok == expected && (expected ? doc.isObject() && error.isEmpty() : !error.isEmpty());
    loop.quit();
  };
  if (post) api.post(path, {}, callback); else api.get(path, callback);
  QTimer::singleShot(5000, &loop, &QEventLoop::quit);
  loop.exec();
  return passed;
}

int main(int argc, char** argv) {
  QCoreApplication app(argc, argv);
  for (const auto& invalid : {QByteArray(), QByteArray("{"), QByteArray("[]"), QByteArray("null"), QByteArray("<html>error</html>")}) {
    if (!checkResponse(invalid, 200, false)) {
      std::cerr << "Invalid response accepted: " << invalid.constData() << '\n';
      return 1;
    }
  }
  if (!checkResponse("{\"ok\":true}", 200, true) ||
      !checkResponse("{\"error\":\"test failure\"}", 400, false)) return 1;
  for (const auto& body : {QByteArray("{}"), QByteArray("{\"ok\":false}"), QByteArray("{\"ok\":1}"), QByteArray("{\"ok\":\"true\"}")})
    if (!checkResponse(body,200,false,true,QStringLiteral("/api/campaign/reset"))) return 2;
  if (!checkResponse("{\"ok\":true}",200,true,true,QStringLiteral("/api/campaign/reset"))
      || !checkResponse("{\"error\":\"Campanha ativa\"}",409,false,true,QStringLiteral("/api/campaign/reset"))) return 2;
  {
    QTcpServer replies;
    if (!replies.listen(QHostAddress::LocalHost)) return 3;
    QObject::connect(&replies,&QTcpServer::newConnection,&replies,[&] {
      auto* socket=replies.nextPendingConnection();
      QObject::connect(socket,&QTcpSocket::readyRead,socket,[socket] {
        socket->readAll();
        if(socket->property("answered").toBool()) return;
        socket->setProperty("answered",true);
        QTimer::singleShot(60,socket,[socket] {
          socket->write("HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 11\r\nConnection: close\r\n\r\n{\"ok\":true}");
          socket->disconnectFromHost();
        });
      });
      QObject::connect(socket,&QTcpSocket::disconnected,socket,&QObject::deleteLater);
    });
    ApiClient scoped;
    scoped.setBaseUrl(QStringLiteral("http://127.0.0.1:%1").arg(replies.serverPort()));
    int stale=0,accounts=0,current=0;
    for(const auto& path : {QStringLiteral("/api/campaigns/current"),QStringLiteral("/api/logs"),QStringLiteral("/api/recovery?async=1"),QStringLiteral("/api/tasks?async=1")})
      scoped.get(path,[&](bool,const QJsonDocument&,const QString&) {++stale;});
    scoped.get(QStringLiteral("/api/accounts"),[&](bool ok,const QJsonDocument&,const QString&) {accounts+=ok;});
    scoped.invalidateCampaignRequests();
    scoped.get(QStringLiteral("/api/logs"),[&](bool ok,const QJsonDocument&,const QString&) {current+=ok;});
    QEventLoop pending;
    QTimer::singleShot(350,&pending,&QEventLoop::quit);pending.exec();
    if(stale || accounts!=1 || current!=1) return 3;
  }
  // Accept the connection but never answer: the real GET timeout must invoke
  // the callback, allowing MainWindow to clear its in-flight polling flag.
  QTcpServer stalled;
  if (!stalled.listen(QHostAddress::LocalHost, 0)) return 1;
  ApiClient api;
  api.setBaseUrl(QStringLiteral("http://127.0.0.1:%1").arg(stalled.serverPort()));
  QEventLoop loop;
  QElapsedTimer elapsed;
  elapsed.start();
  bool timedOut = false;
  bool postTimedOut = false;
  bool operationTimedOut = false;
  bool preflightTimedOut = false;
  api.post(QStringLiteral("/api/campaigns/preflight?async=1&request_id=fixture"), {},
           [&](bool ok, const QJsonDocument& doc, const QString& error) {
    preflightTimedOut = !ok && elapsed.elapsed() >= 12000 && elapsed.elapsed() < 25000
        && error.contains(QStringLiteral("não respondeu a tempo")) && !error.contains("Operation canceled")
        && doc.object().value("transport_error").toString() == "timeout";
  });
  api.get(QStringLiteral("/api/campaigns/current"),
          [&](bool ok, const QJsonDocument&, const QString& error) {
    operationTimedOut = !ok && !error.isEmpty() && elapsed.elapsed() >= 8000 && elapsed.elapsed() < 20000;
  });
  api.post(QStringLiteral("/api/campaigns"), {},
           [&](bool ok, const QJsonDocument& doc, const QString& error) {
    postTimedOut = !ok && !error.isEmpty() && elapsed.elapsed() >= 50000
        && doc.object().value("error_code").toString() == "request_outcome_unknown";
    if (timedOut) loop.quit();
  });
  api.get(QStringLiteral("/api/accounts/bulk-register/status"),
          [&](bool ok, const QJsonDocument&, const QString& error) {
    timedOut = !ok && !error.isEmpty() && elapsed.elapsed() >= 8000 && elapsed.elapsed() < 20000;
    if (postTimedOut) loop.quit();
  });
  QTimer::singleShot(70000, &loop, &QEventLoop::quit);
  loop.exec();
  if (!timedOut || !postTimedOut || !operationTimedOut || !preflightTimedOut) {
    std::cerr << "Stalled GET did not time out\n";
    return 1;
  }
  std::cout << "API response tests passed\n";
}
