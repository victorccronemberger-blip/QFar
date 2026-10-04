#pragma once

#include <QByteArray>
#include <QString>
#include <QDir>
#include <QVector>
#include <algorithm>

namespace qmoney::service {
inline bool isExpectedServiceExecutable(const QString& launched, const QString& provisioned) {
  return !launched.isEmpty() && !provisioned.isEmpty()
      && QDir::isAbsolutePath(launched) && QDir::isAbsolutePath(provisioned)
      && QDir::cleanPath(QDir::fromNativeSeparators(launched)).compare(
          QDir::cleanPath(QDir::fromNativeSeparators(provisioned)), Qt::CaseInsensitive) == 0;
}
struct ProcessFact {
  quint32 pid{};
  quint32 parentPid{};
  quint64 created{};
  QString executable;
  QByteArray owner;
  bool identityKnown{};
};

// Facts must come from process handles, not executable basenames. An orphan
// without a live, proven root of this QProcess is deliberately not selected.
inline QVector<ProcessFact> selectOwnedServiceTree(
    const QVector<ProcessFact>& facts, quint32 rootPid, quint32 appPid,
    quint64 appCreated, const QByteArray& appOwner, const QString& expectedPath) {
  QVector<ProcessFact> selected;
  if (!rootPid || rootPid == appPid || !appCreated || appOwner.isEmpty()
      || expectedPath.isEmpty()) return selected;
  const auto root = std::find_if(facts.cbegin(), facts.cend(),
      [rootPid](const ProcessFact& p) { return p.pid == rootPid; });
  auto known = [&](const ProcessFact& p) {
    return p.identityKnown && p.created && p.owner == appOwner
        && isExpectedServiceExecutable(p.executable, expectedPath);
  };
  if (root == facts.cend() || !known(*root) || root->parentPid != appPid
      || root->created < appCreated) return selected;
  selected.append(*root);
  for (int i = 0; i < selected.size(); ++i) {
    const ProcessFact parent = selected[i];
    for (const ProcessFact& p : facts) {
      if (!p.pid || p.pid == appPid || p.pid == rootPid || p.parentPid != parent.pid
          || p.created < parent.created || !known(p)) continue;
      if (std::none_of(selected.cbegin(), selected.cend(),
          [&](const ProcessFact& found) { return found.pid == p.pid; }))
        selected.append(p);
    }
  }
  std::reverse(selected.begin(), selected.end()); // children before their parent
  return selected;
}
} // namespace qmoney::service
