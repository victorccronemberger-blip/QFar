#include "MainWindow.hpp"

#include <QApplication>
#include <QCoreApplication>
#include <QGuiApplication>
#include <QDebug>
#include <QDir>
#include <QFile>
#include <QIcon>
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
  qInfo() << "QMoney: estilo aplicado";

  MainWindow window(style);
  qInfo() << "QMoney: janela construida";
  window.show();
  return app.exec();
}
