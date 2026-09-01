#pragma once

#include <QNetworkAccessManager>
#include <QObject>
#include <QUrl>

class QFile;
class QNetworkReply;

class UpdateManager final : public QObject {
  Q_OBJECT

public:
  explicit UpdateManager(QObject* parent = nullptr);
  void check(bool interactive = false);
  void downloadAndInstall();
  bool isBusy() const { return _busy; }

signals:
  void statusChanged(const QString& text);
  void checkFinished(bool updateAvailable, bool interactive);
  void updateAvailable(const QString& version, const QString& notes);
  void progress(qint64 received, qint64 total);
  void errorOccurred(const QString& message, bool interactive);
  void installReady(const QString& packagePath);

private:
  void fail(const QString& message);
  void fetchChecksum();
  void fetchPackage();

  QNetworkAccessManager _network;
  QUrl _packageUrl;
  QUrl _checksumUrl;
  QString _version;
  QString _notes;
  QString _expectedSha256;
  QString _packagePath;
  QFile* _output{};
  bool _busy{};
  bool _interactive{};
};
