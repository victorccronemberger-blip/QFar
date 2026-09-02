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

#ifdef Q_OS_WIN
#include <windows.h>
#include <bcrypt.h>
#include <wincrypt.h>
#endif

namespace {
constexpr auto kLatestRelease =
    "https://api.github.com/repos/victorccronemberger-blip/QFar/releases/latest";
constexpr auto kPackageName = "QMoney-windows-x64.zip";
constexpr auto kChecksumName = "QMoney-windows-x64.zip.sha256";
constexpr auto kSignatureName = "QMoney-windows-x64.zip.sig";
constexpr auto kPublicKeyDerBase64 =
    "MIIBojANBgkqhkiG9w0BAQEFAAOCAY8AMIIBigKCAYEAzJmeC3IlDgbZMJMyHKWS8wKg"
    "Gn7zQbbzPN6cjYEmbfymGEYjuCdOKgedRb90y7ne5+3jDpeSfSgGNePoglxr/u7FEr8+"
    "BSPFYqXXGN62+58vL7OZi71hz4ZmX3UkEvfi0v0K03rWZVgvYim+KcoWbd/uZLMqBSoj"
    "uVEujgCK7tlA9JerfR2ECm68/vVS+Y5S12bmdpPrqG8v7BiUD9bip0pUb/oGw/R8i14l"
    "JZ4cOaTS/Kj1u4mbNKX4rByucUYGTMCiKdXJFrUEgG0ERKGYtT4ckCVeGgT2+q4Q9AQE"
    "cd3WOx5idUw2qAMl1htQwNnOM4bfjCzkfeAbAtFh00xcvo2P8x/xiNvRjQkBF3+JRpJY"
    "pd3pcFYKuxW590OW/An4x8HcA8Ja5l4pNMFnSy2dDIq7+QUY6URhj4tFhabmTIXoVlGm"
    "bo9hTAbPQKbYrEluiFtyFDqo0djg+VJeS3tVgZ2b1v8Hc8UMuot+Aa0AJwE45wHTh12m"
    "qjgADgHiSY0VAgMBAAE=";

bool verifySignature(const QByteArray& hash, const QByteArray& signature) {
#ifdef Q_OS_WIN
  const QByteArray der = QByteArray::fromBase64(QByteArray(kPublicKeyDerBase64));
  CERT_PUBLIC_KEY_INFO* info = nullptr;
  DWORD infoSize = 0;
  if (!CryptDecodeObjectEx(X509_ASN_ENCODING, X509_PUBLIC_KEY_INFO,
                           reinterpret_cast<const BYTE*>(der.constData()),
                           static_cast<DWORD>(der.size()),
                           CRYPT_DECODE_ALLOC_FLAG, nullptr, &info, &infoSize))
    return false;
  BCRYPT_KEY_HANDLE key = nullptr;
  const BOOL imported = CryptImportPublicKeyInfoEx2(
      X509_ASN_ENCODING, info, 0, nullptr, &key);
  LocalFree(info);
  if (!imported || !key) return false;
  BCRYPT_PKCS1_PADDING_INFO padding{BCRYPT_SHA256_ALGORITHM};
  const NTSTATUS status = BCryptVerifySignature(
      key, &padding,
      reinterpret_cast<PUCHAR>(const_cast<char*>(hash.constData())),
      static_cast<ULONG>(hash.size()),
      reinterpret_cast<PUCHAR>(const_cast<char*>(signature.constData())),
      static_cast<ULONG>(signature.size()), BCRYPT_PAD_PKCS1);
  BCryptDestroyKey(key);
  return status >= 0;
#else
  Q_UNUSED(hash)
  Q_UNUSED(signature)
  return false;
#endif
}

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

bool UpdateManager::verifyHashSignature(const QByteArray& hash,
                                        const QByteArray& signature) {
  return verifySignature(hash, signature);
}

void UpdateManager::check(bool interactive) {
  if (_busy) return;
  _busy = true;
  _interactive = interactive;
  _repair = false;
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
    _signatureUrl = QUrl();
    for (const QJsonValue& value : release.value(QStringLiteral("assets")).toArray()) {
      const QJsonObject asset = value.toObject();
      const QString name = asset.value(QStringLiteral("name")).toString();
      const QUrl url(asset.value(QStringLiteral("browser_download_url")).toString());
      if (name == QString::fromLatin1(kPackageName)) _packageUrl = url;
      if (name == QString::fromLatin1(kChecksumName)) _checksumUrl = url;
      if (name == QString::fromLatin1(kSignatureName)) _signatureUrl = url;
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
    if (!_packageUrl.isValid() || !_checksumUrl.isValid() || !_signatureUrl.isValid())
      return fail(QStringLiteral("A versão %1 não contém pacote, checksum e assinatura obrigatórios.").arg(_version));

    _busy = false;
    emit statusChanged(QStringLiteral("Atualização %1 disponível.").arg(_version));
    emit updateAvailable(_version, _notes);
    emit checkFinished(true, _interactive);
  });
}

void UpdateManager::repair() {
  if (_busy) return;
  _busy = true;
  _interactive = true;
  _repair = true;
  emit statusChanged(QStringLiteral("Localizando o pacote completo do QMoney…"));
  auto* reply = _network.get(requestFor(QUrl(QString::fromLatin1(kLatestRelease))));
  connect(reply, &QNetworkReply::finished, this, [this, reply] {
    const QByteArray payload = reply->readAll();
    const QString networkError = reply->errorString();
    const bool failed = reply->error() != QNetworkReply::NoError;
    reply->deleteLater();
    if (failed)
      return fail(QStringLiteral("Não foi possível localizar o pacote de reparo: %1")
                      .arg(networkError));

    QJsonParseError parseError;
    const QJsonDocument document = QJsonDocument::fromJson(payload, &parseError);
    if (parseError.error != QJsonParseError::NoError || !document.isObject())
      return fail(QStringLiteral("O GitHub retornou um pacote de reparo inválido."));

    const QJsonObject release = document.object();
    _version = normalizedVersion(release.value(QStringLiteral("tag_name")).toString());
    _notes.clear();
    _packageUrl = QUrl();
    _checksumUrl = QUrl();
    _signatureUrl = QUrl();
    for (const QJsonValue& value : release.value(QStringLiteral("assets")).toArray()) {
      const QJsonObject asset = value.toObject();
      const QString name = asset.value(QStringLiteral("name")).toString();
      const QUrl url(asset.value(QStringLiteral("browser_download_url")).toString());
      if (name == QString::fromLatin1(kPackageName)) _packageUrl = url;
      if (name == QString::fromLatin1(kChecksumName)) _checksumUrl = url;
      if (name == QString::fromLatin1(kSignatureName)) _signatureUrl = url;
    }
    if (!_packageUrl.isValid() || !_checksumUrl.isValid() || !_signatureUrl.isValid())
      return fail(QStringLiteral("A versão publicada não contém todos os componentes de reparo."));
    _busy = false;
    emit statusChanged(QStringLiteral("Pacote completo pronto para reparar a instalação."));
    emit updateAvailable(_version, QString());
    emit checkFinished(true, true);
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
    fetchSignature();
  });
}

void UpdateManager::fetchSignature() {
  emit statusChanged(QStringLiteral("Verificando assinatura da versão…"));
  auto* reply = _network.get(requestFor(_signatureUrl));
  connect(reply, &QNetworkReply::finished, this, [this, reply] {
    const QByteArray payload = reply->readAll().trimmed();
    const QString networkError = reply->errorString();
    const bool failed = reply->error() != QNetworkReply::NoError;
    reply->deleteLater();
    if (failed) return fail(QStringLiteral("Falha ao baixar a assinatura: %1").arg(networkError));
    _signature = QByteArray::fromBase64(payload);
    if (_signature.size() < 256)
      return fail(QStringLiteral("A assinatura da atualização é inválida."));
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
    if (!verifyHashSignature(QByteArray::fromHex(actual.toLatin1()), _signature)) {
      QFile::remove(_packagePath);
      return fail(QStringLiteral("A atualização foi recusada: assinatura RSA não reconhecida."));
    }

    _busy = false;
    emit statusChanged(QStringLiteral("Atualização autenticada e pronta para instalar."));
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
