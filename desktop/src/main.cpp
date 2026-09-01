#include "MainWindow.hpp"

#include <QApplication>
#include <QCoreApplication>
#include <QGuiApplication>
#include <QDebug>
#include <QCryptographicHash>
#include <QDir>
#include <QFile>
#include <QFont>
#include <QIcon>
#include <QLocalServer>
#include <QLocalSocket>
#include <QSettings>

#include <oclero/qlementine/style/QlementineStyle.hpp>

int main(int argc, char* argv[]) {
  QApplication::setHighDpiScaleFactorRoundingPolicy(
      Qt::HighDpiScaleFactorRoundingPolicy::PassThrough);
  QApplication app(argc, argv);
  qInfo() << "QMoney: QApplication pronta";

  QCoreApplication::setApplicationName(QStringLiteral("QMoney"));
  QCoreApplication::setOrganizationName(QStringLiteral("QMoney"));
  QCoreApplication::setApplicationVersion(QStringLiteral(QMONEY_VERSION));
  QGuiApplication::setApplicationDisplayName(QStringLiteral("QMoney"));
  QApplication::setWindowIcon(QIcon(QStringLiteral(":/qmoney/icon.png")));
  const QStringList commandLine = QCoreApplication::arguments();
  const int signatureArg = commandLine.indexOf(QStringLiteral("--verify-update-signature"));
  if (signatureArg >= 0 && signatureArg + 2 < commandLine.size()) {
    QFile package(commandLine[signatureArg + 1]);
    QFile signatureFile(commandLine[signatureArg + 2]);
    if (!package.open(QIODevice::ReadOnly) || !signatureFile.open(QIODevice::ReadOnly)) return 8;
    QCryptographicHash hash(QCryptographicHash::Sha256);
    if (!hash.addData(&package)) return 8;
    const QByteArray signature = QByteArray::fromBase64(signatureFile.readAll().trimmed());
    return UpdateManager::verifyHashSignature(hash.result(), signature) ? 0 : 9;
  }
  const int healthArg = commandLine.indexOf(QStringLiteral("--update-health"));
  if (healthArg >= 0 && healthArg + 1 < commandLine.size())
    app.setProperty("updateHealthPath", commandLine[healthArg + 1]);

  const QString instanceName = QStringLiteral("QMoney-OperationsDesk-v1");
  QLocalSocket existing;
  existing.connectToServer(instanceName);
  if (existing.waitForConnected(300)) {
    existing.write("activate");
    existing.waitForBytesWritten(150);
    return 0;
  }
  QLocalServer::removeServer(instanceName);
  QLocalServer instanceServer;
  if (!instanceServer.listen(instanceName))
    qWarning() << "QMoney: não foi possível registrar a instância única";

#ifdef Q_OS_WIN
  // O atualizador não consegue substituir a si próprio enquanto está em uso.
  // Ele deixa a próxima versão com este nome; a aplicação conclui a troca no
  // primeiro início, quando nenhum updater está bloqueado pelo Windows.
  const QString appDir = QCoreApplication::applicationDirPath();
  const QString updater = appDir + QStringLiteral("/QMoneyUpdater.exe");
  const QString pendingUpdater = appDir + QStringLiteral("/QMoneyUpdater.new.exe");
  if (QFile::exists(pendingUpdater)) {
    QFile::remove(updater);
    QFile::rename(pendingUpdater, updater);
  }
#endif

  auto* style = new oclero::qlementine::QlementineStyle(&app);
  qInfo() << "QMoney: QlementineStyle criado";
  style->setAnimationsEnabled(true);
  style->setThemeJsonPath(QSettings().value(QStringLiteral("darkTheme"), false).toBool()
                              ? QStringLiteral(":/qmoney/theme-dark.json")
                              : QStringLiteral(":/qmoney/theme-light.json"));
  qInfo() << "QMoney: tema carregado";
  app.setStyle(style);
  app.setFont(QFont(QStringLiteral("Inter"), 10));
  qInfo() << "QMoney: estilo aplicado";

  MainWindow window(style);
  QObject::connect(&instanceServer, &QLocalServer::newConnection, &window,
                   [&instanceServer, &window] {
    while (auto* socket = instanceServer.nextPendingConnection()) {
      socket->readAll();
      socket->disconnectFromServer();
      socket->deleteLater();
    }
    window.showNormal();
    window.raise();
    window.activateWindow();
  });
  qInfo() << "QMoney: janela construida";
  window.show();
  return app.exec();
}
