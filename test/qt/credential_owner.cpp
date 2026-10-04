// Q06a: exact UI method excerpts, fictional responses and memory clipboard.
// This fixture never uses the system clipboard, network or personal accounts.
#include <QApplication>
#include <QCheckBox>
#include <QDialog>
#include <QDialogButtonBox>
#include <QFormLayout>
#include <QHBoxLayout>
#include <QJsonArray>
#include <QJsonDocument>
#include <QJsonObject>
#include <QLabel>
#include <QLineEdit>
#include <QMainWindow>
#include <QPointer>
#include <QPushButton>
#include <QTimer>
#include <QVBoxLayout>
#include <functional>
#include <iostream>
#include <vector>

struct MemoryClipboard {
  QString value=QStringLiteral("artificial-existing-memory-value");
  int writes=0;
  void setText(const QString& text){value=text;++writes;}
};
static MemoryClipboard memoryClipboard;
struct FixtureApplication {static MemoryClipboard* clipboard(){return &memoryClipboard;}};
class MemoryApi {
public:
  using Callback=std::function<void(bool,const QJsonDocument&,const QString&)>;
  struct Call {QString path;QJsonObject body;Callback callback;};
  std::vector<Call> calls;
  void post(const QString& path,const QJsonObject& body,Callback callback){
    calls.push_back({path,body,std::move(callback)});
  }
};
class MainWindow : public QMainWindow {
public:
  MemoryApi _api;
  QString status;
  void setStatus(const QString& message){status=message;}
  QWidget* credentialCopyActions(const QString&,bool,bool);
  void showSavedAccountCredentials(const QString&,const QString&);
};
// Includes are already loaded; only the two exact UI method bodies substitute
// the clipboard API. This is a local seam, not a production configuration.
#define QApplication FixtureApplication
#include "credential-review-methods.inc"
#undef QApplication

static QJsonArray checks;
static int failures=0;
static void check(const QString& name,bool passed){
  checks.append(QJsonObject{{"name",name},{"passed",passed}});
  if(!passed)++failures;
}
int main(int argc,char** argv){
  QApplication app(argc,argv);app.setQuitOnLastWindowClosed(false);
  const QString owner=QStringLiteral("fixture-a@example.invalid");
  const QString artificialValue=QStringLiteral("artificial-memory-only-value");
  auto record=[&](const QJsonValue& email){return QJsonDocument(QJsonObject{{"email",email},{"password",artificialValue}});};
  struct Case {const char* name;QJsonDocument doc;bool ok,allowed;};
  const std::vector<Case> cases={
    {"matching",record(owner),true,true},
    {"case_whitespace_matching",record(QStringLiteral(" FIXTURE-A@EXAMPLE.INVALID ")),true,true},
    {"different_owner",record(QStringLiteral("fixture-b@example.invalid")),true,false},
    {"missing_owner",QJsonDocument(QJsonObject{{"password",artificialValue}}),true,false},
    {"number_owner",record(42),true,false},
    {"null_owner",record(QJsonValue(QJsonValue::Null)),true,false},
    {"object_owner",record(QJsonObject{}),true,false},
    {"empty_owner",record(QStringLiteral("")),true,false},
    {"whitespace_owner",record(QStringLiteral("  ")),true,false},
    {"missing_password",QJsonDocument(QJsonObject{{"email",owner}}),true,false},
    {"number_password",QJsonDocument(QJsonObject{{"email",owner},{"password",42}}),true,false},
    {"empty_password",QJsonDocument(QJsonObject{{"email",owner},{"password",""}}),true,false},
    {"transport_failure",record(owner),false,false},
    // ApiClient rejects non-object successful JSON before calling the consumer.
    {"nonobject_failure",QJsonDocument(QJsonArray{}),false,false}
  };
  for(bool archived:{false,true})for(bool view:{false,true})for(const auto& item:cases){
    MainWindow window;memoryClipboard=MemoryClipboard{};
    const QString originalMemory=memoryClipboard.value;
    auto* panel=window.credentialCopyActions(owner,true,archived);panel->setParent(&window);
    auto* action=panel->findChild<QPushButton*>(view?"viewCredentialsButton":"copyPasswordButton");
    const QString name=QString("%1.%2.%3.").arg(archived?"archived":"active",view?"view":"copy",item.name);
    check(name+"control_present",action!=nullptr);if(!action)continue;
    action->click();action->click();
    check(name+"pending_click_single_request",window._api.calls.size()==1&&!action->isEnabled());
    if(window._api.calls.size()!=1)continue;
    auto& call=window._api.calls.front();
    check(name+"request_route",call.path==(archived?"/api/accounts/banned/password":"/api/accounts/password"));
    check(name+"request_owner",call.body.value("email").toString()==owner);
    call.callback(item.ok,item.doc,QStringLiteral("artificial-private-error"));
    auto* dialog=window.findChild<QDialog*>("savedAccountCredentialsDialog");
    check(name+"owner_contract",view?(bool(dialog)==item.allowed):
          (memoryClipboard.writes==int(item.allowed)&&memoryClipboard.value==(item.allowed?artificialValue:originalMemory)));
    check(name+"control_restored",action->isEnabled());
    if(!item.allowed){
      check(name+"original_label_restored",action->text()==(view?QStringLiteral("Ver acesso"):QStringLiteral("Copiar senha")));
      check(name+"private_error",!window.status.isEmpty()&&!window.status.contains(owner)&&!window.status.contains(artificialValue)&&!window.status.contains("artificial-private-error"));
    }
    if(dialog)dialog->reject();
    QCoreApplication::sendPostedEvents(nullptr,QEvent::DeferredDelete);
  }
  for(bool archived:{false,true}){
    MainWindow window;memoryClipboard=MemoryClipboard{};
    auto* panel=window.credentialCopyActions(owner,true,archived);panel->setParent(&window);
    panel->findChild<QPushButton*>("copyPasswordButton")->click();
    delete panel;
    window._api.calls.front().callback(true,record(owner),QString());
    check(QString("%1.destroyed_guard_ignored").arg(archived?"archived":"active"),memoryClipboard.writes==0);
  }
  std::cout<<QJsonDocument(QJsonObject{{"status",failures?"failed":"passed"},{"failures",failures},
    {"check_count",checks.size()},{"response_cases",int(cases.size())},{"routes",2},{"actions",2},
    {"clipboard","memory-only"},{"transport","memory-only"},{"checks",checks}}).toJson(QJsonDocument::Compact).constData()<<std::endl;
  return failures?1:0;
}
