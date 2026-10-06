#pragma once

#include <QJsonDocument>
#include <QNetworkAccessManager>
#include <QObject>
#include <functional>

class ApiClient final : public QObject {
  Q_OBJECT

public:
  using Callback = std::function<void(bool, const QJsonDocument&, const QString&)>;

  explicit ApiClient(QObject* parent = nullptr);

  void setBaseUrl(const QString& baseUrl);
  void setSessionToken(const QByteArray& token) { _sessionToken = token; }
  void get(const QString& path, Callback callback);
  void post(const QString& path, const QJsonObject& body, Callback callback);
  void put(const QString& path, const QJsonObject& body, Callback callback);
  void remove(const QString& path, Callback callback);
  // Discard replies belonging to local campaign state from before a reset.
  void invalidateCampaignRequests() { ++_campaignRequestEpoch; }

private:
  void request(const QByteArray& method, const QString& path,
               const QJsonObject* body, Callback callback);

  QNetworkAccessManager _network;
  QByteArray _sessionToken;
  quint64 _campaignRequestEpoch{};
  QString _baseUrl{QStringLiteral("http://127.0.0.1:8876")};
};
