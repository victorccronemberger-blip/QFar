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
  void get(const QString& path, Callback callback);
  void post(const QString& path, const QJsonObject& body, Callback callback);
  void put(const QString& path, const QJsonObject& body, Callback callback);
  void remove(const QString& path, Callback callback);

private:
  void request(const QByteArray& method, const QString& path,
               const QJsonObject* body, Callback callback);

  QNetworkAccessManager _network;
  QString _baseUrl{QStringLiteral("http://127.0.0.1:8876")};
};
