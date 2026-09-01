#include "UpdateManager.hpp"

#include <QCoreApplication>
#include <QCryptographicHash>
#include <QDir>
#include <QFile>
#include <QJsonArray>
#include <QJsonDocument>
#include <QJsonObject>
#include <QNetworkReply>
#include <QNetworkRequest>
#include <QRegularExpression>
#include <QStandardPaths>
#include <QVersionNumber>

namespace {
constexpr auto kLatestRelease =
    "https://api.github.com/repos/victorccronemberger-blip/QFar/releases/latest";
constexpr auto kPackageName = "QMoney-windows-x64.zip";
constexpr auto kChecksumName = "QMoney-windows-x64.zip.sha256";

QNetworkRequest requestFor(const QUrl& url) {
  QNetworkRequest request(url);
  request.setRawHeader("Accept", "application/vnd.github+json");
  request.setRawHeader("X-GitHub-Api-Version", "2022-11-28");
  request.setRawHeader("User-Agent", "QMoney-Updater");
  request.setAttribute(QNetworkRequest::RedirectPolicyAttribute,
                       QNetworkRequest::NoLessSafeRedirectPolicy);
  return request;
}

QString normalizedVersion(QString value) {
  value = value.trimmed();
  if (value.startsWith(QLatin1Char('v'), Qt::CaseInsensitive)) value.remove(0, 1);
  return value;
}
}  // namespace

UpdateManager::UpdateManager(QObject* parent) : QObject(parent) {}

void UpdateManager::check(bool interactive) {
  if (_busy) return;
  _busy = true;
  _interactive = interactive;
  emit statusChanged(QStringLiteral("Verificando atualizações…"));
  auto* reply = _network.get(requestFor(QUrl(QString::fromLatin1(kLatestRelease))));
  connect(reply, &QNetworkReply::finished, this, [this, reply] {
    const int status = reply->attribute(QNetworkRequest::HttpStatusCodeAttribute).toInt();
    const QByteArray payload = reply->readAll();
    const QString networkError = reply->errorString();
    const bool failed = reply->error() != QNetworkReply::NoError;
    reply->deleteLater();
    if (status == 404) {
      _busy = false;
      emit statusChanged(QStringLiteral("Nenhuma versão publicada ainda."));
      emit checkFinished(false, _interactive);
      return;
    }
    if (failed) return fail(QStringLiteral("Não foi possível consultar o GitHub: %1").arg(networkError));

    QJsonParseError parseError;
    const QJsonDocument document = QJsonDocument::fromJson(payload, &parseError);
    if (parseError.error != QJsonParseError::NoError || !document.isObject())
      return fail(QStringLiteral("O GitHub retornou uma resposta inválida."));

    const QJsonObject release = document.object();
    _version = normalizedVersion(release.value(QStringLiteral("tag_name")).toString());
    _notes = release.value(QStringLiteral("body")).toString().left(12000);
    _packageUrl = QUrl();
    _checksumUrl = QUrl();
    for (const QJsonValue& value : release.value(QStringLiteral("assets")).toArray()) {
      const QJsonObject asset = value.toObject();
      const QString name = asset.value(QStringLiteral("name")).toString();
      const QUrl url(asset.value(QStringLiteral("browser_download_url")).toString());
      if (name == QString::fromLatin1(kPackageName)) _packageUrl = url;
      if (name == QString::fromLatin1(kChecksumName)) _checksumUrl = url;
    }

    const QVersionNumber latest = QVersionNumber::fromString(_version);
    const QVersionNumber current = QVersionNumber::fromString(QCoreApplication::applicationVersion());
    if (latest.isNull() || QVersionNumber::compare(latest, current) <= 0) {
      _busy = false;
      emit statusChanged(QStringLiteral("QMoney está atualizado — versão %1.")
                             .arg(QCoreApplication::applicationVersion()));
      emit checkFinished(false, _interactive);
      return;
    }
    if (!_packageUrl.isValid() || !_checksumUrl.isValid())
      return fail(QStringLiteral("A versão %1 não contém o pacote e o checksum obrigatórios.").arg(_version));

    _busy = false;
    emit statusChanged(QStringLiteral("Atualização %1 disponível.").arg(_version));
    emit updateAvailable(_version, _notes);
    emit checkFinished(true, _interactive);
  });
}

void UpdateManager::downloadAndInstall() {
  if (_busy || !_packageUrl.isValid() || !_checksumUrl.isValid()) return;
  _busy = true;
  fetchChecksum();
}

void UpdateManager::fetchChecksum() {
  emit statusChanged(QStringLiteral("Validando a atualização…"));
  auto* reply = _network.get(requestFor(_checksumUrl));
  connect(reply, &QNetworkReply::finished, this, [this, reply] {
    const QByteArray payload = reply->readAll();
    const QString networkError = reply->errorString();
    const bool failed = reply->error() != QNetworkReply::NoError;
    reply->deleteLater();
    if (failed) return fail(QStringLiteral("Falha ao baixar o checksum: %1").arg(networkError));
    const QRegularExpression re(QStringLiteral("\\b([A-Fa-f0-9]{64})\\b"));
    const auto match = re.match(QString::fromUtf8(payload));
    if (!match.hasMatch()) return fail(QStringLiteral("Checksum SHA-256 inválido na versão publicada."));
    _expectedSha256 = match.captured(1).toLower();
    fetchPackage();
  });
}

void UpdateManager::fetchPackage() {
  const QString tempDir = QStandardPaths::writableLocation(QStandardPaths::TempLocation)
                          + QStringLiteral("/QMoneyUpdate");
  QDir().mkpath(tempDir);
  _packagePath = tempDir + QStringLiteral("/") + QString::fromLatin1(kPackageName);
  QFile::remove(_packagePath);
  _output = new QFile(_packagePath, this);
  if (!_output->open(QIODevice::WriteOnly))
    return fail(QStringLiteral("Não foi possível criar o pacote temporário."));

  emit statusChanged(QStringLiteral("Baixando QMoney %1…").arg(_version));
  auto* reply = _network.get(requestFor(_packageUrl));
  connect(reply, &QNetworkReply::readyRead, this, [this, reply] {
    if (_output) _output->write(reply->readAll());
  });
  connect(reply, &QNetworkReply::downloadProgress, this, &UpdateManager::progress);
  connect(reply, &QNetworkReply::finished, this, [this, reply] {
    if (_output) {
      _output->write(reply->readAll());
      _output->close();
      _output->deleteLater();
      _output = nullptr;
    }
    const QString networkError = reply->errorString();
    const bool failed = reply->error() != QNetworkReply::NoError;
    reply->deleteLater();
    if (failed) return fail(QStringLiteral("Falha ao baixar a atualização: %1").arg(networkError));

    QFile package(_packagePath);
    if (!package.open(QIODevice::ReadOnly)) return fail(QStringLiteral("O pacote baixado não pôde ser lido."));
    QCryptographicHash hash(QCryptographicHash::Sha256);
    if (!hash.addData(&package)) return fail(QStringLiteral("Não foi possível verificar o pacote baixado."));
    const QString actual = QString::fromLatin1(hash.result().toHex());
    if (actual != _expectedSha256) {
      QFile::remove(_packagePath);
      return fail(QStringLiteral("A atualização foi recusada: o SHA-256 não confere."));
    }

    _busy = false;
    emit statusChanged(QStringLiteral("Atualização verificada e pronta para instalar."));
    emit installReady(_packagePath);
  });
}

void UpdateManager::fail(const QString& message) {
  if (_output) {
    _output->close();
    _output->deleteLater();
    _output = nullptr;
  }
  _busy = false;
  emit statusChanged(message);
  emit errorOccurred(message, _interactive);
}
