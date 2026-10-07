#pragma once
#include <QProcessEnvironment>

inline QProcessEnvironment packagedServiceEnvironment(QProcessEnvironment environment) {
  for (const QString& key : environment.keys()) {
    for (const QString& prefix : {QStringLiteral("AWS_"), QStringLiteral("HOSTINGER_"),
                                 QStringLiteral("MINUTE_"), QStringLiteral("EGO4D_"),
                                 QStringLiteral("CROWTADO_"), QStringLiteral("NYMERIA_")}) {
      if (key.startsWith(prefix, Qt::CaseInsensitive)) {
        environment.remove(key);
        break;
      }
    }
  }
  return environment;
}
