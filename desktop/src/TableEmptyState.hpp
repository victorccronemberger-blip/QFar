#pragma once

#include <QAbstractItemModel>
#include <QEvent>
#include <QLabel>
#include <QAbstractItemView>
#include <QVBoxLayout>

// Lives in the viewport so the guidance follows resizing and never hides headers.
class TableEmptyState final : public QWidget {
public:
  TableEmptyState(QAbstractItemView* table, const QString& title, const QString& detail)
      : QWidget(table->viewport()), _table(table) {
    setObjectName(QStringLiteral("tableEmptyState"));
    setAttribute(Qt::WA_TransparentForMouseEvents);
    auto* layout = new QVBoxLayout(this);
    layout->setContentsMargins(24, 20, 24, 20);
    layout->setSpacing(8);
    layout->addStretch();
    auto* heading = new QLabel(title, this);
    heading->setObjectName(QStringLiteral("emptyHeading"));
    heading->setAlignment(Qt::AlignCenter);
    heading->setWordWrap(true);
    auto* body = new QLabel(detail, this);
    body->setObjectName(QStringLiteral("quiet"));
    body->setAlignment(Qt::AlignCenter);
    body->setWordWrap(true);
    layout->addWidget(heading);
    layout->addWidget(body);
    layout->addStretch();
    table->viewport()->installEventFilter(this);
    connect(table->model(), &QAbstractItemModel::rowsInserted, this, [this] { refresh(); });
    connect(table->model(), &QAbstractItemModel::rowsRemoved, this, [this] { refresh(); });
    connect(table->model(), &QAbstractItemModel::modelReset, this, [this] { refresh(); });
    refresh();
  }

protected:
  bool eventFilter(QObject* object, QEvent* event) override {
    if (object == _table->viewport() && event->type() == QEvent::Resize) refresh();
    return QWidget::eventFilter(object, event);
  }

private:
  void refresh() {
    setGeometry(_table->viewport()->rect());
    setVisible(_table->model()->rowCount() == 0);
    raise();
  }
  QAbstractItemView* _table;
};
