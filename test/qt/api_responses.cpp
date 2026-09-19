#include "../../desktop/src/ApiClient.hpp"
#include <QCoreApplication>
#include <QEventLoop>
#include <QJsonObject>
#include <QElapsedTimer>
#include <QTcpServer>
#include <QTcpSocket>
#include <QTimer>
#include <iostream>

bool checkResponse(const QByteArray& body, int status, bool expected) {
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
  api.get(QStringLiteral("/api/test"), [&](bool ok, const QJsonDocument& doc, const QString& error) {
    passed = ok == expected && (expected ? doc.isObject() && error.isEmpty() : !error.isEmpty());
    loop.quit();
  });
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
  api.get(QStringLiteral("/api/accounts/bulk-register/status"),
          [&](bool ok, const QJsonDocument&, const QString& error) {
    timedOut = !ok && !error.isEmpty() && elapsed.elapsed() >= 50000;
    loop.quit();
  });
  QTimer::singleShot(70000, &loop, &QEventLoop::quit);
  loop.exec();
  if (!timedOut) {
    std::cerr << "Stalled GET did not time out\n";
    return 1;
  }
  std::cout << "API response tests passed\n";
}
