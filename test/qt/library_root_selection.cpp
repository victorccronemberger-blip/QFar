#include "../../desktop/src/LibraryRootSelection.hpp"
#include "../../desktop/src/PackagedServiceEnvironment.hpp"
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
  QFile companion(installed + "/nymeria_plus_download_urls.json");
  if (!companion.open(QIODevice::WriteOnly)) return 7;
  companion.write("{}"); companion.close();
  if (selectLibraryRoot({}, installed, user) != installed) return 8;
  if (selectLibraryRoot(saved, installed, user) != saved) return 9;
  QProcessEnvironment inherited;
  inherited.insert("NYMERIA_ROOT", "C:/another-PC/old-library");
  inherited.insert("nymeria_venv", "C:/another-PC/old-python");
  inherited.insert("AWS_SECRET_ACCESS_KEY", "fixture-private");
  inherited.insert("PATH", "runtime-system-path");
  inherited.insert("LOCALAPPDATA", user);
  auto clean = packagedServiceEnvironment(inherited);
  if (clean.contains("NYMERIA_ROOT") || clean.contains("nymeria_venv") ||
      clean.contains("AWS_SECRET_ACCESS_KEY")) return 10;
  if (clean.value("PATH") != "runtime-system-path" || clean.value("LOCALAPPDATA") != user) return 11;
  return 0;
}
