#pragma once
#include <QWidget>
#include <QPainter>
#include <QJsonObject>
#include <QStyledItemDelegate>
#include <QPainterPath>
#include <QJsonArray>
#include <QDateTime>
#include <QScrollArea>
#include <QVBoxLayout>
#include <QLabel>
#include <QScrollBar>

class CampaignStatusIcon final : public QWidget {
public:
  explicit CampaignStatusIcon(QWidget* parent = nullptr) : QWidget(parent) {
    setFixedSize(34, 34);
  }
  void setState(const QString& state, const QString& label) {
    _state = state;
    setAccessibleName(label);
    update();
  }
protected:
  void paintEvent(QPaintEvent*) override {
    QPainter p(this);
    p.setRenderHint(QPainter::Antialiasing);
    const bool warning = _state == "error" || _state == "unknown";
    const bool done = _state == "done";
    const bool active = _state == "running" || _state == "starting" || _state == "preflight";
    const QColor color(warning ? "#d58b25" : done ? "#159a72" : active ? "#8058ff" : "#8b90a2");
    auto tint = color;
    tint.setAlpha(32);
    p.setPen(Qt::NoPen);
    p.setBrush(tint);
    p.drawEllipse(QRectF(1, 1, 32, 32));
    p.setPen(QPen(color, 2.5, Qt::SolidLine, Qt::RoundCap, Qt::RoundJoin));
    p.setBrush(Qt::NoBrush);
    if (done) {
      p.drawLine(QPointF(10, 17), QPointF(15, 22));
      p.drawLine(QPointF(15, 22), QPointF(24, 12));
    } else if (warning) {
      p.drawLine(QPointF(17, 10), QPointF(17, 18));
      p.drawPoint(QPointF(17, 24));
    } else if (_state == "paused") {
      p.drawLine(QPointF(13, 11), QPointF(13, 23));
      p.drawLine(QPointF(21, 11), QPointF(21, 23));
    } else if (_state == "stopping" || _state == "stopped") {
      p.drawRoundedRect(QRectF(11, 11, 12, 12), 2, 2);
    } else if (active) {
      p.setBrush(color);
      p.setPen(Qt::NoPen);
      const QPointF points[] = {{13, 10}, {24, 17}, {13, 24}};
      p.drawPolygon(points, 3);
    } else {
      p.drawEllipse(QRectF(11, 11, 12, 12));
    }
  }
private:
  QString _state;
};

class OperationTimeline final : public QScrollArea {
public:
  explicit OperationTimeline(QWidget* parent=nullptr) : QScrollArea(parent) {
    setWidgetResizable(true);
    setFrameShape(QFrame::NoFrame);
    setMinimumHeight(160);
    setObjectName(QStringLiteral("operationTimeline"));
    setAccessibleName(QStringLiteral("Atividade recente da campanha"));
  }
  void setEvents(const QJsonArray& events) {
    if (_events == events && widget()) return;
    _events = events;
    const int previous = verticalScrollBar()->value();
    auto* content = new QWidget;
    auto* layout = new QVBoxLayout(content);
    layout->setContentsMargins(0, 0, 6, 0);
    layout->setSpacing(12);
    QStringList accessible;
    for (int i=events.size()-1; i>=qMax(0,int(events.size())-12); --i) {
      const auto event = events[i].toObject();
      const auto title = event.value("title").toString();
      if (title.isEmpty()) continue;
      auto* entry = new QWidget;
      auto* lines = new QVBoxLayout(entry);
      lines->setContentsMargins(10, 5, 5, 5);
      lines->setSpacing(4);
      const QString color = event.value("level").toString()=="error" ? "#ff9caf"
          : event.value("level").toString()=="warning" ? "#f5c470" : "#b49aff";
      entry->setStyleSheet(QStringLiteral("border-left: 2px solid %1;").arg(color));
      const auto ts = event.value("ts");
      const auto time = ts.isDouble() ? QDateTime::fromSecsSinceEpoch(qint64(ts.toDouble())).toLocalTime().toString("HH:mm:ss") : QStringLiteral("—");
      auto* heading = new QLabel(time + QStringLiteral(" · ") + title);
      heading->setTextFormat(Qt::PlainText);
      heading->setWordWrap(true);
      heading->setTextInteractionFlags(Qt::TextSelectableByMouse);
      heading->setStyleSheet(QStringLiteral("color: #f5f6fa; font-size: 13px; font-weight: 600; border: none;"));
      lines->addWidget(heading);
      const auto detail = event.value("detail").toString();
      if (!detail.isEmpty()) {
        auto* label = new QLabel(detail);
        label->setTextFormat(Qt::PlainText);
        label->setWordWrap(true);
        label->setTextInteractionFlags(Qt::TextSelectableByMouse);
        label->setStyleSheet(QStringLiteral("color: #c8cbd6; font-size: 12px; border: none;"));
        lines->addWidget(label);
      }
      layout->addWidget(entry);
      accessible << heading->text() + ": " + detail;
    }
    if (accessible.isEmpty()) {
      auto* empty = new QLabel(QStringLiteral("Nenhum evento nesta execução."));
      empty->setWordWrap(true);
      empty->setStyleSheet(QStringLiteral("color: #c8cbd6; font-size: 13px;"));
      layout->addWidget(empty);
    }
    layout->addStretch();
    content->setStyleSheet(QStringLiteral("background: #23242c;"));
    setWidget(content);
    verticalScrollBar()->setValue(previous);
    setAccessibleName(accessible.isEmpty() ? QStringLiteral("Nenhum evento nesta execução") : accessible.join("\n"));
  }
private:
  QJsonArray _events;
};

class OperationTrack final : public QWidget {
public:
  explicit OperationTrack(QWidget* parent=nullptr) : QWidget(parent) {
    setFixedHeight(76);
    setToolTip(QStringLiteral("Etapa atual por conta. Atenção reúne falhas, restrições e resultados pendentes; uma conta com recibo também pode ter falhas anteriores. Os resultados acumulados estão na tabela."));
  }
  void setCounts(const QJsonObject& counts) {
    _counts = counts;
    setAccessibleName(QStringLiteral("Na fila: %1. Em andamento: %2. Confirmadas: %3. Pendências: %4.")
        .arg(queued()).arg(active()).arg(_counts.value("confirmed").toInt()).arg(attention()));
    update();
  }
protected:
  void paintEvent(QPaintEvent*) override {
    QPainter p(this);
    p.setRenderHint(QPainter::Antialiasing);
    const QColor ink = palette().color(QPalette::WindowText);
    const bool dark = ink.lightness()>128;
    const QStringList names{"Na fila", "Em andamento", "Com recibo", "Atenção"};
    const QList<int> counts{queued(), active(), _counts.value("confirmed").toInt(), attention()};
    const QList<QColor> colors{QColor(dark?"#b6bdcf":"#667085"), QColor(dark?"#b49aff":"#6540d2"), QColor(dark?"#58d9ae":"#007351"), QColor(dark?"#f5c470":"#805000")};
    const double step = width()/4.;
    for (int i=0; i<4; ++i) {
      QRectF box(i*step+3, 2, step-6, height()-4);
      p.setPen(QPen(QColor(dark?"#454855":"#dfe1ea"), 1));
      p.setBrush(QColor(dark?"#2b2c36":"#f3f4fa"));
      p.drawRoundedRect(box, 8, 8);
      QFont font("Segoe UI", 9);
      p.setFont(font);
      p.setPen(QColor(dark?"#b6bdcf":"#606477"));
      p.drawText(box.adjusted(12,8,-12,-42), Qt::AlignLeft|Qt::AlignVCenter, names[i]);
      font.setPointSize(20); font.setWeight(QFont::DemiBold); p.setFont(font);
      p.setPen(colors[i]);
      p.drawText(box.adjusted(12,28,-12,-4), Qt::AlignLeft|Qt::AlignVCenter, QString::number(counts[i]));
      font.setPointSize(8); font.setWeight(QFont::Normal); p.setFont(font);
      p.setPen(QColor(dark?"#b6bdcf":"#606477"));
      p.drawText(box.adjusted(12,28,-12,-8), Qt::AlignRight|Qt::AlignBottom, QStringLiteral("contas"));
    }
  }
private:
  int queued() const { return _counts.value("queued").toInt()+_counts.value("waiting").toInt(); }
  int active() const { return _counts.value("preparing").toInt()+_counts.value("sending").toInt()+_counts.value("confirming").toInt()+_counts.value("recovering").toInt(); }
  int attention() const { return _counts.contains("_attention") ? _counts.value("_attention").toInt()
      : _counts.value("failed").toInt()+_counts.value("excluded").toInt()+_counts.value("pending").toInt()+_counts.value("unconfirmed").toInt(); }
  QJsonObject _counts;
};

class OperationRowDelegate final : public QStyledItemDelegate {
public:
  using QStyledItemDelegate::QStyledItemDelegate;
  void paint(QPainter* p,const QStyleOptionViewItem& option,const QModelIndex& index) const override {
    p->save(); p->setRenderHint(QPainter::Antialiasing);
    const auto row=index.data(Qt::UserRole).toJsonObject();
    const auto state=row.value("state").toString();
    const bool success=state=="confirmed";
    const bool failed=state=="failed" || state=="excluded" || state=="pending" || state=="unconfirmed";
    const bool active=state=="sending" || state=="confirming" || state=="preparing";
    const QColor ink=option.palette.color(QPalette::Text);
    const bool dark=ink.lightness()>128;
    const QColor purple("#7046ff"), gray(dark?"#b6bdcf":"#667085");
    QColor tint=success?QColor("#d9f4e9"):failed?QColor("#fce3e6"):active?QColor("#ece7ff"):QColor("#e8eaf1");
    QColor color=success?QColor("#006b4c"):failed?QColor("#b4233b"):active?purple:QColor("#525b70");
    const auto rect=option.rect.adjusted(7,0,-8,0);
    if(option.state & QStyle::State_Selected) p->fillRect(option.rect,QColor(dark?"#39304f":"#eee8ff"));
    p->setPen(QColor(dark?"#3b3e4a":"#e0e3ec")); p->drawLine(option.rect.bottomLeft(),option.rect.bottomRight());
    QFont font("Segoe UI",10); p->setFont(font);
    if(index.column()==0) {
      const QRect avatar(rect.left(),rect.center().y()-18,36,36);
      p->setPen(Qt::NoPen); p->setBrush(QColor("#e3e6ee")); p->drawEllipse(avatar);
      p->setPen(QColor("#344054")); p->drawText(avatar,Qt::AlignCenter,QStringLiteral("C%1").arg(index.row()+1));
      const QRect text=rect.adjusted(49,8,0,-8);
      p->setPen(ink); font.setWeight(QFont::DemiBold); p->setFont(font);
      p->drawText(text,Qt::AlignTop,p->fontMetrics().elidedText(row.value("email").toString(),Qt::ElideRight,text.width()));
      font.setPointSize(9); font.setWeight(QFont::Normal); p->setFont(font); p->setPen(gray);
      p->drawText(text,Qt::AlignBottom,p->fontMetrics().elidedText(row.value("detail").toString(QStringLiteral("Selecione para ver os detalhes")),Qt::ElideRight,text.width()));
    } else if(index.column()==1) {
      QRect pill(rect.left(),rect.center().y()-15,qMin(rect.width(),128),30);
      p->setPen(Qt::NoPen); p->setBrush(tint); p->drawRoundedRect(pill,15,15);
      p->setPen(color); p->drawText(pill.adjusted(5,0,-5,0),Qt::AlignCenter,p->fontMetrics().elidedText(index.data().toString(),Qt::ElideRight,pill.width()-10));
    } else if(index.column()==2) {
      const auto value=row.value("progress");
      const int percent=qBound(0,value.toInt(),100);
      if (state!="sending" || !value.isDouble()) { p->setPen(gray); p->drawText(rect,Qt::AlignCenter,QStringLiteral("—")); p->restore(); return; }
      const QRectF bar(rect.left(),rect.center().y()-5,qMax(20,rect.width()-50),10);
      p->setPen(Qt::NoPen); p->setBrush(QColor("#e1e4ed")); p->drawRoundedRect(bar,5,5);
      if(percent>0) { p->setBrush(purple); auto fill=bar;fill.setWidth(bar.width()*qBound(0,percent,100)/100.);p->drawRoundedRect(fill,5,5); }
      p->setPen(gray);p->drawText(rect,Qt::AlignRight|Qt::AlignVCenter,value.isDouble()?QStringLiteral("%1%").arg(percent):QStringLiteral("—"));
    } else {
      if(dark) color=success?QColor("#58d9ae"):failed?QColor("#ff9caf"):active?QColor("#b49aff"):gray;
      if (row.value("failed").toInt()>0 && !failed) color=QColor(dark?"#f5c470":"#805000");
      p->setPen(Qt::NoPen);p->setBrush(color);p->drawEllipse(QPointF(rect.left()+5,rect.center().y()),5,5);
      p->setPen(color);p->drawText(rect.adjusted(19,0,0,0),Qt::AlignVCenter,p->fontMetrics().elidedText(index.data().toString(),Qt::ElideRight,rect.width()-19));
    }
    p->restore();
  }
  QSize sizeHint(const QStyleOptionViewItem&,const QModelIndex&) const override {return {120,58};}
};
