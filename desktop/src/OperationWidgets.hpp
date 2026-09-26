#pragma once
#include <QWidget>
#include <QPainter>
#include <QJsonObject>
#include <QStyledItemDelegate>
#include <QPainterPath>
#include <QJsonArray>
#include <QDateTime>

class OperationTimeline final : public QWidget {
public:
  explicit OperationTimeline(QWidget* parent=nullptr) : QWidget(parent) { setMinimumHeight(260); }
  void setEvents(const QJsonArray& events) {
    _events = {};
    QStringList accessible;
    for (int i=qMax(0,int(events.size())-3); i<events.size(); ++i) {
      _events.append(events[i]);
      const auto event=events[i].toObject();
      accessible << event.value("title").toString()+": "+event.value("detail").toString();
    }
    setAccessibleName(accessible.join("\n"));
    update();
  }
protected:
  void paintEvent(QPaintEvent*) override {
    QPainter p(this); p.setRenderHint(QPainter::Antialiasing);
    if (_events.isEmpty()) {
      p.setPen(QColor("#c8cbd6")); p.drawText(rect().adjusted(0,20,0,0),Qt::AlignTop|Qt::TextWordWrap,QStringLiteral("Os eventos da campanha aparecerão aqui."));
      return;
    }
    const int spacing=qMin(124,qMax(85,(height()-25)/qMax(1,int(_events.size()))));
    p.setPen(QPen(QColor("#686d7b"),1));
    if(_events.size()>1)p.drawLine(QPoint(12,22),QPoint(12,22+spacing*(_events.size()-1)));
    for(int i=0;i<_events.size();++i) {
      const auto event=_events[i].toObject();const int y=22+i*spacing;
      p.setPen(Qt::NoPen);p.setBrush(QColor("#8058ff"));p.drawEllipse(QPoint(12,y),7,7);
      if(i==0){p.setPen(QColor("#8058ff"));p.setBrush(Qt::NoBrush);p.drawEllipse(QPoint(12,y),11,11);}
      QFont font("Segoe UI",11);p.setFont(font);p.setPen(QColor("#c8cbd6"));
      const auto time=QDateTime::fromSecsSinceEpoch(qint64(event.value("ts").toDouble())).toString("HH:mm");
      p.drawText(QRect(36,y-12,60,25),Qt::AlignLeft,time);
      font.setWeight(QFont::DemiBold);p.setFont(font);p.setPen(QColor("#f5f6fa"));
      p.drawText(QRect(112,y-12,qMax(30,width()-115),45),Qt::AlignTop|Qt::TextWordWrap,event.value("title").toString());
      font.setWeight(QFont::Normal);font.setPointSize(10);p.setFont(font);p.setPen(QColor("#c8cbd6"));
      p.drawText(QRect(112,y+29,qMax(30,width()-115),spacing-36),Qt::AlignTop|Qt::TextWordWrap,event.value("detail").toString());
    }
  }
private:
  QJsonArray _events;
};

class OperationTrack final : public QWidget {
public:
  explicit OperationTrack(QWidget* parent=nullptr) : QWidget(parent) { setMinimumHeight(154); }
  void setCounts(const QJsonObject& counts) { _counts=counts; update(); }
protected:
  void paintEvent(QPaintEvent*) override {
    QPainter p(this); p.setRenderHint(QPainter::Antialiasing);
    const QColor ink=palette().color(QPalette::WindowText);
    const QColor purple("#754dff"), muted(ink.lightness()>128?"#b6bdcf":"#667085");
    const QStringList names{"Preparação", "Envio", "Confirmação", "Concluído"};
    const QStringList keys{"preparing", "sending", "confirming", "confirmed"};
    const double step=(width()-44.)/3.;
    for(int i=0;i<4;++i) {
      const double x=22+i*step;
      const bool active=_counts.value(keys[i]).toInt()>0;
      if(i<3) { p.setPen(QPen(active?purple:QColor("#d4d7e2"),3)); p.drawLine(QPointF(x+25,29),QPointF(x+step-25,29)); }
      p.setBrush(active?purple:palette().color(QPalette::Base));
      p.setPen(QPen(active?purple:QColor("#a4aabd"),1.5)); p.drawEllipse(QPointF(x,29),18,18);
      QFont font("Segoe UI",11,QFont::DemiBold); p.setFont(font); p.setPen(active?Qt::white:ink);
      p.drawText(QRectF(x-18,11,36,36),Qt::AlignCenter,QString::number(i+1));
      const double left=i==3?x-86:x-18;
      p.setPen(ink); p.drawText(QRectF(left,59,115,24),Qt::AlignLeft,names[i]);
      font.setPointSize(19); font.setWeight(QFont::Bold); p.setFont(font);
      p.drawText(QRectF(left,83,100,30),Qt::AlignLeft,QString::number(_counts.value(keys[i]).toInt()));
      font.setPointSize(10); font.setWeight(QFont::Normal); p.setFont(font); p.setPen(muted);
      p.drawText(QRectF(left,115,100,24),Qt::AlignLeft,QStringLiteral("contas"));
    }
  }
private:
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
    const bool failed=state=="failed" || state=="excluded";
    const bool active=state=="sending" || state=="confirming" || state=="preparing";
    const QColor ink=option.palette.color(QPalette::Text);
    const bool dark=ink.lightness()>128;
    const QColor purple("#7046ff"), gray(dark?"#b6bdcf":"#667085");
    QColor tint=success?QColor("#d9f4e9"):failed?QColor("#fce3e6"):active?QColor("#ece7ff"):QColor("#e8eaf1");
    QColor color=success?QColor("#00875e"):failed?QColor("#b4233b"):active?purple:QColor("#525b70");
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
      p->drawText(text,Qt::AlignBottom,p->fontMetrics().elidedText(row.value("session_id").toString(QStringLiteral("Sem sessão confirmada")),Qt::ElideRight,text.width()));
    } else if(index.column()==1) {
      QRect pill(rect.left(),rect.center().y()-15,qMin(rect.width(),128),30);
      p->setPen(Qt::NoPen); p->setBrush(tint); p->drawRoundedRect(pill,15,15);
      p->setPen(color); p->drawText(pill,Qt::AlignCenter,index.data().toString());
    } else if(index.column()==2) {
      const auto value=row.value("progress");
      const int percent=success?100:value.toInt();
      const QRectF bar(rect.left(),rect.center().y()-5,qMax(20,rect.width()-50),10);
      p->setPen(Qt::NoPen); p->setBrush(QColor("#e1e4ed")); p->drawRoundedRect(bar,5,5);
      if(percent>0) { p->setBrush(purple); auto fill=bar;fill.setWidth(bar.width()*qBound(0,percent,100)/100.);p->drawRoundedRect(fill,5,5); }
      p->setPen(gray);p->drawText(rect,Qt::AlignRight|Qt::AlignVCenter,(success||value.isDouble())?QStringLiteral("%1%").arg(percent):QStringLiteral("—"));
    } else {
      if(dark) color=success?QColor("#58d9ae"):failed?QColor("#ff9caf"):active?QColor("#b49aff"):gray;
      p->setPen(Qt::NoPen);p->setBrush(color);p->drawEllipse(QPointF(rect.left()+5,rect.center().y()),5,5);
      p->setPen(color);p->drawText(rect.adjusted(19,0,0,0),Qt::AlignVCenter,index.data().toString());
    }
    p->restore();
  }
  QSize sizeHint(const QStyleOptionViewItem&,const QModelIndex&) const override {return {120,58};}
};
