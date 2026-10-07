#include "../../desktop/src/LibraryRootSelection.hpp"
#include <QCoreApplication>
#include <QTemporaryDir>
#include <QFile>

int main(int argc, char** argv) {
  QCoreApplication app(argc, argv);
  QTemporaryDir temporary;
  if (!temporary.isValid()) return 1;
  const QString root = temporary.path();
  const QString saved = root + "/other-user/empty library";
  const QString installed = root + "/portable";
  const QString user = root + "/state";
  QDir().mkpath(saved);
  QDir().mkpath(installed + "/data/nymeria/_catalog");
  QFile manifest(installed + "/data/nymeria/_catalog/download_urls.json");
  if (!manifest.open(QIODevice::WriteOnly)) return 2;
  manifest.write("{}"); manifest.close();
  if (selectLibraryRoot(saved, installed, user) != saved) return 3;
  if (selectLibraryRoot(root + "/missing", installed, user) != installed) return 4;
  manifest.remove();
  if (selectLibraryRoot({}, installed, user) != user) return 5;
  QDir().mkpath(saved + "/data/nymeria/_catalog");
  if (selectLibraryRoot(saved, installed, user) != saved) return 6;
  return 0;
}
