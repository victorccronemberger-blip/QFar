// Exact UI methods + ApiClient, artificial offscreen UI and loopback only.
// The request wrapper is a test-local deadline seam; production has no override.
#include "../../desktop/src/ApiClient.hpp"
#include <QApplication>
#include <QCheckBox>
#include <QClipboard>
#include <QDialog>
#include <QDialogButtonBox>
#include <QElapsedTimer>
#include <QEventLoop>
#include <QFormLayout>
#include <QJsonObject>
#include <QLabel>
#include <QLineEdit>
#include <QMainWindow>
#include <QNetworkProxy>
#include <QNetworkReply>
#include <QNetworkRequest>
#include <QPointer>
#include <QPushButton>
#include <QTcpServer>
#include <QTcpSocket>
#include <QTimer>
#include <QVector>
#include <QVBoxLayout>
#include <iostream>

static QVector<int> configuredTimeouts;
class DeadlineFixtureRequest : public QNetworkRequest {
public:
  using QNetworkRequest::QNetworkRequest;
  void setTransferTimeout(int milliseconds) {
    configuredTimeouts.append(milliseconds);
    QNetworkRequest::setTransferTimeout(50);
  }
};
// Qt headers are already loaded; only the included product request declaration
// and its setter bind to this local wrapper. ApiClient's method body is unchanged.
#define QNetworkRequest DeadlineFixtureRequest
#include "../../desktop/src/ApiClient.cpp"
#undef QNetworkRequest

class MainWindow : public QMainWindow {
public:
  ApiClient _api{this};
  QString status;
  void setStatus(const QString& text) { status=text; }
  QWidget* credentialCopyActions(const QString&,bool,bool);
  void showSavedAccountCredentials(const QString&,const QString&);
};
#include "credential-review-methods.inc"

static bool waitUntil(std::function<bool()> predicate,int maximumMs) {
  QElapsedTimer clock;clock.start();
  while(!predicate()&&clock.elapsed()<maximumMs) {
    QEventLoop loop;QTimer::singleShot(5,&loop,&QEventLoop::quit);loop.exec();
  }
  return predicate();
}

int main(int argc,char** argv) {
  QApplication app(argc,argv);
  app.setQuitOnLastWindowClosed(false);
  QNetworkProxy::setApplicationProxy(QNetworkProxy::NoProxy);
  const QString email="deadline-fixture@example.invalid";
  const QString privateFixture="never-show-fixture-value";
  for(bool banned:{false,true}) {
    configuredTimeouts.clear();
    QTcpServer server;
    if(!server.listen(QHostAddress::LocalHost,0))return 1;
    int requests=0;
    bool expectedRoute=false;
    QObject::connect(&server,&QTcpServer::newConnection,&server,[&]{
      auto* socket=server.nextPendingConnection();
      QObject::connect(socket,&QTcpSocket::readyRead,socket,[&,socket]{
        auto bytes=socket->property("fixture-bytes").toByteArray()+socket->readAll();
        socket->setProperty("fixture-bytes",bytes);
        if(!bytes.contains("\r\n\r\n")||socket->property("fixture-counted").toBool())return;
        socket->setProperty("fixture-counted",true);
        ++requests;
        expectedRoute=bytes.startsWith(banned?"POST /api/accounts/banned/password ":"POST /api/accounts/password ");
        // Accept and hold the connection. No body/secret/provider response.
      });
      QObject::connect(socket,&QTcpSocket::disconnected,socket,&QObject::deleteLater);
    });
    MainWindow window;
    window._api.setBaseUrl(QStringLiteral("http://127.0.0.1:%1").arg(server.serverPort()));
    auto* panel=window.credentialCopyActions(email,true,banned);
    panel->setParent(&window);
    auto* view=panel->findChild<QPushButton*>("viewCredentialsButton");
    if(!view)return 2;
    view->click();view->click();
    if(view->isEnabled())return 3;
    if(configuredTimeouts.size()!=1||configuredTimeouts.first()!=60000) {
      std::cerr<<"Credential POST lacks the declared production deadline\n";return 4;
    }
    if(!waitUntil([&]{return view->isEnabled();},1500)) {
      std::cerr<<"Credential action remained pending after fixture deadline\n";return 5;
    }
    if(requests!=1||!expectedRoute||view->text()!=QStringLiteral("Ver acesso"))return 6;
    if(window.findChild<QDialog*>("savedAccountCredentialsDialog"))return 7;
    if(window.status.isEmpty()||window.status.contains(email,Qt::CaseInsensitive)
        ||window.status.contains(privateFixture)||window.status.contains(QString::number(server.serverPort())))return 8;
    waitUntil([]{return false;},100);
    if(requests!=1)return 9;
  }
  std::cout<<"Credential deadline: 2 routes; production=60000ms; fixture=50ms; button restored; generic error; no dialog or automatic retry\n";
  return 0;
}
