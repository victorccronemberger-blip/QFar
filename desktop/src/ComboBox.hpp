#pragma once

#include <QComboBox>
#include <QApplication>
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
    // Qt's reveal animation snapshots the pre-correction popup rectangle.
    const bool animate = QApplication::isEffectEnabled(Qt::UI_AnimateCombo);
    QApplication::setEffectEnabled(Qt::UI_AnimateCombo, false);
    QComboBox::showPopup();
    QApplication::setEffectEnabled(Qt::UI_AnimateCombo, animate);
    // The theme can resize the view during Show using only the space below
    // it. Reconcile the final geometry so a low field opens upwards instead
    // of clipping the last option or keeping a stale fixed height.
    auto* popup = view()->window();
    if (!popup || !screen() || count() == 0) return;
    const QRect available = screen()->availableGeometry().adjusted(4, 4, -4, -4);
    const QPoint top = mapToGlobal(QPoint(0, 0));
    const QPoint bottom = mapToGlobal(QPoint(0, height()));
    const int below = qMax(0, available.bottom() - bottom.y() + 1);
    const int above = qMax(0, top.y() - available.top());
    const int rowHeight = view()->sizeHintForRow(0);
    const int frame = view()->frameWidth() * 2;
    const int desiredHeight = qMin(count(), maxVisibleItems()) * rowHeight + frame;
    const bool openBelow = desiredHeight <= below || below >= above;
    const int heightLimit = openBelow ? below : above;
    const int visibleRows = qMax(1, (heightLimit - frame) / qMax(1, rowHeight));
    view()->setFixedHeight(qMin(desiredHeight, visibleRows * rowHeight + frame));
    view()->setFixedWidth(qMin(popupWidth, available.width()));
    if (popup->layout()) popup->layout()->setContentsMargins(0, 0, 0, 0);
    popup->adjustSize();
    const int x = qBound(available.left(), top.x(), qMax(available.left(), available.right() - popup->width() + 1));
    const int y = openBelow ? bottom.y() : top.y() - popup->height();
    popup->move(x, qBound(available.top(), y, qMax(available.top(), available.bottom() - popup->height() + 1)));
    view()->scrollTo(view()->currentIndex(), QAbstractItemView::EnsureVisible);
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
