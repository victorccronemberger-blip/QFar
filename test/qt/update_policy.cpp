#include "../../desktop/src/UpdateManager.hpp"
#include <QCoreApplication>
#include <QEventLoop>
#include <QJsonArray>
#include <QJsonDocument>
#include <QJsonObject>
#include <QTcpServer>
#include <QTcpSocket>
#include <QTimer>
#include <iostream>

class UpdateManagerTests {
public:
  static bool release(const QString& version, bool repair, bool offer, bool errorExpected) {
    QTcpServer server;
    if (!server.listen(QHostAddress::LocalHost,0)) return false;
    UpdateManager manager;
    const QString base=QStringLiteral("http://127.0.0.1:%1").arg(server.serverPort());
    manager._releaseEndpoint=QUrl(base+QStringLiteral("/release"));
    QJsonArray assets;
    for (const QString& name : {QStringLiteral("QMoney-windows-x64.zip"),QStringLiteral("QMoney-windows-x64.zip.sha256"),QStringLiteral("QMoney-windows-x64.zip.sig")})
      assets.append(QJsonObject{{"name",name},{"browser_download_url",base+QStringLiteral("/asset")}});
    const QByteArray body=QJsonDocument(QJsonObject{{"tag_name",version},{"assets",assets}}).toJson(QJsonDocument::Compact);
    int requests=0, offers=0, errors=0;
    QEventLoop loop;
    QObject::connect(&manager,&UpdateManager::updateAvailable,&loop,[&]{++offers;});
    QObject::connect(&manager,&UpdateManager::checkFinished,&loop,[&]{loop.quit();});
    QObject::connect(&manager,&UpdateManager::errorOccurred,&loop,[&]{++errors;loop.quit();});
    QObject::connect(&server,&QTcpServer::newConnection,&server,[&]{
      auto* socket=server.nextPendingConnection();
      QObject::connect(socket,&QTcpSocket::readyRead,socket,[&,socket]{
        const auto request=socket->readAll();++requests;
        if (!request.startsWith("GET /release ")) { ++errors; loop.quit(); return; }
        socket->write("HTTP/1.1 200 OK\r\nContent-Length: "+QByteArray::number(body.size())+"\r\nConnection: close\r\n\r\n"+body);
        socket->disconnectFromHost();
      });
    });
    if (repair) manager.repair(); else manager.check();
    QTimer::singleShot(2000,&loop,&QEventLoop::quit);
    loop.exec();
    return !manager.isBusy() && requests==1 && offers==int(offer) && errors==int(errorExpected);
  }
  static bool rejectedDownload(const QString& version, bool repair) {
    UpdateManager manager;
    manager._version=version;manager._repair=repair;
    manager._packageUrl=QUrl(QStringLiteral("http://127.0.0.1:1/package"));
    manager._checksumUrl=QUrl(QStringLiteral("http://127.0.0.1:1/checksum"));
    int errors=0, installs=0;
    QObject::connect(&manager,&UpdateManager::errorOccurred,&manager,[&]{++errors;});
    QObject::connect(&manager,&UpdateManager::installReady,&manager,[&]{++installs;});
    manager.downloadAndInstall();
    if (manager.isBusy() || errors!=1 || installs || !manager._packagePath.isEmpty()) return false;
    manager._busy=true;
    manager.fetchPackage();
    return !manager.isBusy() && errors==2 && !installs && manager._packagePath.isEmpty();
  }
};

int main(int argc,char** argv) {
  QCoreApplication app(argc,argv);
  app.setApplicationVersion(QStringLiteral("2.0.27"));
  for (const QString& bad : {QString(),QStringLiteral("2.0"),QStringLiteral("2.0.27junk"),QStringLiteral("2.0.27.1"),QStringLiteral("-2.0.27"),QStringLiteral("999999999999999999.0.0"),QStringLiteral("2.0.27\n")})
    if (UpdateManager::versionAllowed(bad,QStringLiteral("2.0.27"),true)
        || !UpdateManagerTests::rejectedDownload(bad,true)) return 1;
  if (!UpdateManager::versionAllowed("2.0.27","2.0.27",true)
      || UpdateManager::versionAllowed("2.0.27","2.0.27",false)
      || !UpdateManager::versionAllowed("2.0.28","2.0.27",false)
      || UpdateManager::versionAllowed("2.0.26","2.0.27",true)) return 1;
  if (!UpdateManagerTests::release("v2.0.27",true,true,false)
      || !UpdateManagerTests::release("2.0.28",true,true,false)
      || !UpdateManagerTests::release("2.0.26",true,false,true)
      || !UpdateManagerTests::release("2.0.27junk",true,false,true)
      || !UpdateManagerTests::release("2.0.27",false,false,false)
      || !UpdateManagerTests::release("2.0.26",false,false,false)
      || !UpdateManagerTests::release("2.0.28",false,true,false)
      || !UpdateManagerTests::release("2.0.27junk",false,false,true)
      || !UpdateManagerTests::rejectedDownload("2.0.26",true)
      || !UpdateManagerTests::rejectedDownload("2.0.27",false)) return 1;
  std::cout << "Version gates: current repair/newer allowed; malformed/downgrade rejected before downloads; loopback metadata only\n";
}
