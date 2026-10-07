#pragma once
#include <QDir>
#include <QFileInfo>
#include <QString>
#include <QStringList>

inline QString selectLibraryRoot(const QString& savedRoot, const QString& appDir,
                                 const QString& userRoot) {
  // An explicit local choice also works for a new/empty or Nymeria-only
  // library. Dataset setup must populate that root, not another directory.
  if (!savedRoot.isEmpty() && QFileInfo(savedRoot).isDir())
    return QDir::cleanPath(QFileInfo(savedRoot).absoluteFilePath());
  const QStringList candidates{appDir, QDir(appDir).absoluteFilePath("../..")};
  for (const auto& candidate : candidates) {
    const QDir root(candidate);
    if (QFileInfo::exists(root.filePath("data/ego4d/timed_narrations.jsonl")) ||
        QFileInfo::exists(root.filePath("data/ego4d/clip_narrations.json")) ||
        QFileInfo::exists(root.filePath("data/holoassist")) ||
        QFileInfo::exists(root.filePath("data/nymeria/_catalog/download_urls.json")))
      return QDir::cleanPath(QFileInfo(candidate).absoluteFilePath());
  }
  return QDir::cleanPath(userRoot);
}
