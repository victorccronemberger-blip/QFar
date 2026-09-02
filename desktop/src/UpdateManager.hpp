#pragma once

#include <QByteArray>
#include <QNetworkAccessManager>
#include <QObject>
#include <QUrl>

class QFile;
class QNetworkReply;

class UpdateManager final : public QObject {
  Q_OBJECT

public:
  explicit UpdateManager(QObject* parent = nullptr);
  static bool verifyHashSignature(const QByteArray& hash,
                                  const QByteArray& signature);
  void check(bool interactive = false);
  void repair();
  void downloadAndInstall();
  bool isBusy() const { return _busy; }
  bool isRepair() const { return _repair; }

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
  void fetchSignature();
  void fetchPackage();

  QNetworkAccessManager _network;
  QUrl _packageUrl;
  QUrl _checksumUrl;
  QUrl _signatureUrl;
  QString _version;
  QString _notes;
  QString _expectedSha256;
  QByteArray _signature;
  QString _packagePath;
  QFile* _output{};
  bool _busy{};
  bool _interactive{};
  bool _repair{};
};
