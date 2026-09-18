#pragma once

#include <QComboBox>
#include <QListView>
#include <QLayout>
#include <QScreen>
#include <QStyledItemDelegate>

// Use a regular list popup. Qlementine's menu popup and stylesheet sizing
// disagree about the viewport height, leaving options clipped or unreachable.
class ComboBox final : public QComboBox {
public:
  explicit ComboBox(QWidget* parent = nullptr) : QComboBox(parent) {
    setMaxVisibleItems(8);
    setMinimumHeight(42);
    setSizePolicy(QSizePolicy::Expanding, QSizePolicy::Fixed);
    auto* list = new QListView(this);
    list->setUniformItemSizes(true);
    list->setWordWrap(false);
    list->setTextElideMode(Qt::ElideRight);
    list->setHorizontalScrollBarPolicy(Qt::ScrollBarAlwaysOff);
    list->setVerticalScrollBarPolicy(Qt::ScrollBarAsNeeded);
    list->setVerticalScrollMode(QAbstractItemView::ScrollPerItem);
    setView(list);
  }

  void showPopup() override {
    // Theme polish can replace the delegate. Install ours after polish, so
    // drawing, hit testing and popup geometry all use the same row size.
    ensurePolished();
    view()->ensurePolished();
    // Qlementine adds menu-shadow margins to the private popup container.
    // Qt's list-popup sizing does not account for those extra margins.
    if (auto* popup = view()->parentWidget()) {
      popup->ensurePolished();
      if (popup->layout()) popup->layout()->setContentsMargins(0, 0, 0, 0);
    }
    if (!_rows) _rows = new Rows(view());
    setItemDelegate(_rows);
    int popupWidth = width();
    const QFontMetrics metrics(view()->font());
    for (int row = 0; row < count(); ++row) {
      const auto text = itemText(row);
      popupWidth = qMax(popupWidth, metrics.horizontalAdvance(text) + 56);
      setItemData(row, text, Qt::ToolTipRole);
    }
    const int screenWidth = screen() ? screen()->availableGeometry().width() : 900;
    view()->setMinimumWidth(qMin(popupWidth, qMin(900, screenWidth - 24)));
    // Let Qt calculate height and placement at opening time, including DPI,
    // current item count and available space above/below the field.
    QComboBox::showPopup();
  }

private:
  class Rows final : public QStyledItemDelegate {
  public:
    using QStyledItemDelegate::QStyledItemDelegate;
    QSize sizeHint(const QStyleOptionViewItem& option, const QModelIndex& index) const override {
      auto size = QStyledItemDelegate::sizeHint(option, index);
      size.setHeight(qMax(36, option.fontMetrics.height() + 16));
      return size;
    }
  };
  Rows* _rows{};
};
