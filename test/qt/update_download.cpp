#include "../../desktop/src/UpdateManager.hpp"
#include <QCoreApplication>
#include <QCryptographicHash>
#include <QDir>
#include <QEventLoop>
#include <QFile>
#include <QStandardPaths>
#include <QTcpServer>
#include <QTcpSocket>
#include <QTemporaryDir>
#include <QTimer>
#include <iostream>

class UpdateManagerTests {
public:
  static bool downloadFailure(bool breakOutput, bool validChecksum = false) {
    UpdateManager manager;
    QTcpServer server;
    if (!server.listen(QHostAddress::LocalHost, 0)) return false;
    manager._packageUrl = QUrl(QStringLiteral("http://127.0.0.1:%1/package").arg(server.serverPort()));
    manager._expectedSha256 = validChecksum
        ? QString::fromLatin1(QCryptographicHash::hash("package", QCryptographicHash::Sha256).toHex())
        : QString(64, QLatin1Char('0'));
    manager._signature = QByteArray(256, 'x');
    manager._busy = true;
    QEventLoop loop;
    QString error;
    bool installEmitted = false;
    QObject::connect(&manager, &UpdateManager::errorOccurred, &loop, [&](const QString& text, bool) {
      error = text;
      loop.quit();
    });
    QObject::connect(&manager, &UpdateManager::installReady, &loop, [&] { installEmitted = true; loop.quit(); });
    QObject::connect(&server, &QTcpServer::newConnection, &server, [&] {
      auto* socket = server.nextPendingConnection();
      QObject::connect(socket, &QTcpSocket::readyRead, socket, [&, socket] {
        socket->readAll();
        // Fail writes after the output opened successfully, without filling a disk.
        if (breakOutput && manager._output) manager._output->close();
        socket->write("HTTP/1.1 200 OK\r\nContent-Length: 7\r\nConnection: close\r\n\r\npackage");
        socket->disconnectFromHost();
      });
    });
    manager.fetchPackage();
    QTimer::singleShot(5000, &loop, &QEventLoop::quit);
    loop.exec();
    return !manager.isBusy() && !installEmitted && !QFile::exists(manager._packagePath)
        && error.contains(breakOutput ? QStringLiteral("gravar")
                                     : validChecksum ? QStringLiteral("assinatura") : QStringLiteral("SHA-256"));
  }
};

int main(int argc, char** argv) {
  QCoreApplication app(argc, argv);
  QTemporaryDir root;
  if (!root.isValid()) return 1;
  qputenv("TMP", root.path().toUtf8());
  qputenv("TEMP", root.path().toUtf8());
  qputenv("TMPDIR", root.path().toUtf8());
  if (QDir::cleanPath(QStandardPaths::writableLocation(QStandardPaths::TempLocation)) != QDir::cleanPath(root.path())) {
    std::cerr << "Temporary download path was not isolated\n";
    return 1;
  }
  if (!UpdateManagerTests::downloadFailure(true) || !UpdateManagerTests::downloadFailure(false)
      || !UpdateManagerTests::downloadFailure(false, true)) return 1;
  std::cout << "Download failure tests passed\n";
}
