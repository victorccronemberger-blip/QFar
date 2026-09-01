#include "ApiClient.hpp"

#include <QJsonObject>
#include <QNetworkReply>
#include <QNetworkRequest>
#include <QUrl>

ApiClient::ApiClient(QObject* parent) : QObject(parent), _network(this) {}

void ApiClient::setBaseUrl(const QString& baseUrl) {
  _baseUrl = baseUrl;
  while (_baseUrl.endsWith('/')) _baseUrl.chop(1);
}

void ApiClient::get(const QString& path, Callback callback) {
  request("GET", path, nullptr, std::move(callback));
}

void ApiClient::post(const QString& path, const QJsonObject& body, Callback callback) {
  request("POST", path, &body, std::move(callback));
}

void ApiClient::put(const QString& path, const QJsonObject& body, Callback callback) {
  request("PUT", path, &body, std::move(callback));
}

void ApiClient::remove(const QString& path, Callback callback) {
  request("DELETE", path, nullptr, std::move(callback));
}

void ApiClient::request(const QByteArray& method, const QString& path,
                        const QJsonObject* body, Callback callback) {
  QNetworkRequest req(QUrl(_baseUrl + path));
  req.setHeader(QNetworkRequest::ContentTypeHeader, QStringLiteral("application/json"));
  req.setRawHeader("Accept", "application/json");

  QNetworkReply* reply = nullptr;
  const QByteArray payload = body ? QJsonDocument(*body).toJson(QJsonDocument::Compact) : QByteArray();
  if (method == "GET") reply = _network.get(req);
  else if (method == "POST") reply = _network.post(req, payload);
  else if (method == "PUT") reply = _network.put(req, payload);
  else reply = _network.sendCustomRequest(req, method, payload);

  connect(reply, &QNetworkReply::finished, this, [reply, callback = std::move(callback)]() {
    const QByteArray bytes = reply->readAll();
    QJsonParseError parseError;
    const QJsonDocument doc = QJsonDocument::fromJson(bytes, &parseError);
    const int status = reply->attribute(QNetworkRequest::HttpStatusCodeAttribute).toInt();
    const bool ok = reply->error() == QNetworkReply::NoError && status >= 200 && status < 300;
    QString error;
    if (!ok) {
      if (doc.isObject()) error = doc.object().value(QStringLiteral("error")).toString();
      if (error.isEmpty()) error = reply->errorString();
      if (status) error = QStringLiteral("%1 (HTTP %2)").arg(error).arg(status);
    } else if (parseError.error != QJsonParseError::NoError && !bytes.isEmpty()) {
      error = QStringLiteral("Resposta inválida do serviço: %1").arg(parseError.errorString());
    }
    reply->deleteLater();
    callback(ok && error.isEmpty(), doc, error);
  });
}

