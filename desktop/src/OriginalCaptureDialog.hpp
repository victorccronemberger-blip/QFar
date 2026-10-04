#pragma once
#include <QComboBox>
#include <QDialog>
#include <QDialogButtonBox>
#include <QFileDialog>
#include <QFileInfo>
#include <QFormLayout>
#include <QHeaderView>
#include <QJsonArray>
#include <QJsonObject>
#include <QLabel>
#include <QPointer>
#include <QPushButton>
#include <QTableWidget>
#include <QUuid>
#include <QVBoxLayout>
#include <functional>
#include <cmath>
#include <utility>

class OriginalCaptureSelectionDialog final : public QDialog {
public:
  using TaskReply = std::function<void(bool,const QJsonObject&,const QString&)>;
  using TaskLoader = std::function<void(const QString&,TaskReply)>;
  OriginalCaptureSelectionDialog(const QStringList& accounts,TaskLoader loader,QWidget* parent=nullptr)
      : QDialog(parent),_loader(std::move(loader)) {
    setWindowTitle(QStringLiteral("Usar captura original"));resize(780,590);
    auto* layout=new QVBoxLayout(this);
    auto* note=new QLabel(QStringLiteral("Escolha uma conta e a tarefa do serviço. Adicione cada MP4 com seu ZIP original, na ordem dos chunks. Os arquivos, IDs e clocks serão preservados."));
    note->setWordWrap(true);layout->addWidget(note);
    auto* form=new QFormLayout;
    _accounts=new QComboBox;_accounts->setAccessibleName(QStringLiteral("Conta da captura original"));
    for(const auto& email:accounts)if(!email.trimmed().isEmpty())_accounts->addItem(email,email);
    _tasks=new QComboBox;_tasks->setAccessibleName(QStringLiteral("Tarefa da captura original"));_tasks->setEnabled(false);
    auto* accountLabel=new QLabel(QStringLiteral("&Conta"));accountLabel->setBuddy(_accounts);
    auto* taskLabel=new QLabel(QStringLiteral("&Tarefa"));taskLabel->setBuddy(_tasks);
    form->addRow(accountLabel,_accounts);form->addRow(taskLabel,_tasks);layout->addLayout(form);
    _state=new QLabel;_state->setWordWrap(true);_state->setAccessibleName(QStringLiteral("Estado do catálogo de tarefas"));layout->addWidget(_state);
    auto* retry=new QPushButton(QStringLiteral("Atualizar &tarefas"));retry->setAccessibleName(QStringLiteral("Atualizar catálogo de tarefas da conta"));
    connect(retry,&QPushButton::clicked,this,[this]{loadTasks();});layout->addWidget(retry);
    _pairs=new QTableWidget(0,3);_pairs->setAccessibleName(QStringLiteral("Pares MP4 e ZIP na ordem dos chunks"));
    _pairs->setHorizontalHeaderLabels({QStringLiteral("Ordem"),QStringLiteral("MP4 original"),QStringLiteral("ZIP original")});
    _pairs->horizontalHeader()->setSectionResizeMode(QHeaderView::Stretch);
    _pairs->setSelectionBehavior(QAbstractItemView::SelectRows);_pairs->setSelectionMode(QAbstractItemView::SingleSelection);
    _pairs->setEditTriggers(QAbstractItemView::NoEditTriggers);layout->addWidget(_pairs,1);
    auto* controls=new QHBoxLayout;
    auto* add=new QPushButton(QStringLiteral("Adicionar &par…"));add->setAccessibleName(QStringLiteral("Adicionar par MP4 e ZIP original"));
    auto* remove=new QPushButton(QStringLiteral("&Remover par"));remove->setAccessibleName(QStringLiteral("Remover par selecionado"));
    auto* up=new QPushButton(QStringLiteral("&Subir"));up->setAccessibleName(QStringLiteral("Mover par selecionado para cima"));
    auto* down=new QPushButton(QStringLiteral("&Descer"));down->setAccessibleName(QStringLiteral("Mover par selecionado para baixo"));
    for(auto* button:{add,remove,up,down})controls->addWidget(button);layout->addLayout(controls);
    connect(add,&QPushButton::clicked,this,[this]{
      const auto video=QFileDialog::getOpenFileName(this,QStringLiteral("MP4 original"),{},QStringLiteral("Vídeo MP4 (*.mp4)"));
      if(video.isEmpty())return;
      const auto zip=QFileDialog::getOpenFileName(this,QStringLiteral("ZIP original correspondente"),{},QStringLiteral("Sidecar ZIP (*.zip)"));
      if(!zip.isEmpty())addPair(video,zip);
    });
    connect(remove,&QPushButton::clicked,this,[this]{if(_pairs->currentRow()>=0)_pairs->removeRow(_pairs->currentRow());updateReady();});
    connect(up,&QPushButton::clicked,this,[this]{movePair(-1);});connect(down,&QPushButton::clicked,this,[this]{movePair(1);});
    auto* buttons=new QDialogButtonBox(QDialogButtonBox::Cancel);
    _review=buttons->addButton(QStringLiteral("&Validar e revisar"),QDialogButtonBox::AcceptRole);
    _review->setAccessibleName(QStringLiteral("Validar captura original e abrir revisão"));_review->setAutoDefault(false);
    buttons->button(QDialogButtonBox::Cancel)->setDefault(true);
    connect(buttons,&QDialogButtonBox::accepted,this,[this]{if(_review->isEnabled())accept();});
    connect(buttons,&QDialogButtonBox::rejected,this,&QDialog::reject);layout->addWidget(buttons);
    connect(_accounts,&QComboBox::currentIndexChanged,this,[this]{loadTasks();});
    connect(_tasks,&QComboBox::currentIndexChanged,this,[this]{updateReady();});
    setTabOrder(_accounts,_tasks);setTabOrder(_tasks,_pairs);setTabOrder(_pairs,add);
    setTabOrder(add,remove);setTabOrder(remove,up);setTabOrder(up,down);setTabOrder(down,_review);
    loadTasks();
  }
  void addPair(const QString& video,const QString& zip) {
    if(video.trimmed().isEmpty()||zip.trimmed().isEmpty())return;
    const int row=_pairs->rowCount();_pairs->insertRow(row);
    for(int column=1;column<=2;++column){
      const auto path=QFileInfo(column==1?video:zip).absoluteFilePath();
      auto* item=new QTableWidgetItem(QFileInfo(path).fileName());item->setData(Qt::UserRole,path);item->setToolTip(path);_pairs->setItem(row,column,item);
    }
    _pairs->selectRow(row);updateReady();
  }
  QJsonObject requestBody() const {
    QJsonArray pairs;
    for(int row=0;row<_pairs->rowCount();++row)pairs.append(QJsonObject{
      {QStringLiteral("video_path"),_pairs->item(row,1)->data(Qt::UserRole).toString()},
      {QStringLiteral("sidecar_path"),_pairs->item(row,2)->data(Qt::UserRole).toString()}});
    return {{QStringLiteral("account_email"),_accounts->currentData().toString()},
      {QStringLiteral("task_id"),_tasks->currentData().toString()},{QStringLiteral("file_pairs"),pairs},
      {QStringLiteral("expected_chunk_count"),pairs.size()},{QStringLiteral("evaluate"),true},{QStringLiteral("finalize"),true},
      {QStringLiteral("start_request_id"),_operationId}};
  }
private:
  void updateReady(){
    for(int row=0;row<_pairs->rowCount();++row)_pairs->setItem(row,0,new QTableWidgetItem(QString::number(row)));
    _review->setEnabled(!_loading&&!_accounts->currentData().toString().isEmpty()&&!_tasks->currentData().toString().isEmpty()&&_pairs->rowCount()>0);
  }
  void movePair(int offset){
    const int row=_pairs->currentRow(),other=row+offset;if(row<0||other<0||other>=_pairs->rowCount())return;
    for(int column=1;column<=2;++column){auto* item=_pairs->takeItem(row,column);auto* swap=_pairs->takeItem(other,column);_pairs->setItem(row,column,swap);_pairs->setItem(other,column,item);}
    _pairs->selectRow(other);updateReady();
  }
  void loadTasks(){
    const int generation=++_generation;const auto email=_accounts->currentData().toString();
    _tasks->clear();_tasks->setEnabled(false);_loading=true;updateReady();
    _state->setText(QStringLiteral("Consultando tarefas desta conta no serviço…"));
    if(email.isEmpty()){_loading=false;_state->setText(QStringLiteral("Conecte uma conta antes de selecionar a captura."));updateReady();return;}
    QPointer<OriginalCaptureSelectionDialog> guard(this);
    _loader(email,[guard,generation,email](bool ok,const QJsonObject& result,const QString&){
      if(!guard||generation!=guard->_generation||guard->_accounts->currentData().toString()!=email)return;
      guard->_loading=false;
      bool valid=ok&&result.value("ok")==QJsonValue(true)&&result.value("account_email")==QJsonValue(email)
          &&result.value("physical_provenance_verified")==QJsonValue(false)&&result.value("tasks").isArray();
      const auto tasks=result.value("tasks").toArray();QStringList ids;
      for(const auto& value:tasks){const auto task=value.toObject();const auto id=task.value("id");const auto name=task.value("name");
        valid=valid&&value.isObject()&&id.isString()&&!id.toString().trimmed().isEmpty()&&id.toString()==id.toString().trimmed()
            &&name.isString()&&!name.toString().trimmed().isEmpty()&&!ids.contains(id.toString());ids.append(id.toString());}
      if(!valid){guard->_state->setText(QStringLiteral("Não foi possível confirmar o catálogo desta conta. Atualize as tarefas para tentar novamente."));guard->updateReady();return;}
      for(const auto& value:tasks){const auto task=value.toObject();guard->_tasks->addItem(task.value("name").toString(),task.value("id"));}
      guard->_tasks->setEnabled(!tasks.isEmpty());guard->_state->setText(tasks.isEmpty()?QStringLiteral("Esta conta não possui tarefa disponível."):
          QStringLiteral("Catálogo confirmado. A validação local dos arquivos não comprova a origem física da gravação."));guard->updateReady();
    });
  }
  TaskLoader _loader;QComboBox *_accounts{},*_tasks{};QLabel* _state{};QTableWidget* _pairs{};QPushButton* _review{};
  const QString _operationId=QUuid::createUuid().toString(QUuid::WithoutBraces);
  int _generation=0;bool _loading=false;
};

inline bool originalCaptureSummaryValid(const QJsonObject& summary,const QJsonObject& body){
  auto integer=[](const QJsonValue& v){return v.isDouble()&&std::isfinite(v.toDouble())&&v.toDouble()==std::floor(v.toDouble());};
  auto digest=[](const QJsonValue& v){if(!v.isString()||v.toString().size()!=64)return false;
    for(auto c:v.toString())if(!QStringLiteral("0123456789abcdefABCDEF").contains(c))return false;return true;};
  const auto binding=summary.value("task_binding").toObject();
  return body.value("task_id").isString()&&!body.value("task_id").toString().trimmed().isEmpty()
      &&integer(body.value("expected_chunk_count"))&&body.value("expected_chunk_count").toDouble()>0
      &&summary.value("group_count")==QJsonValue(1)&&integer(summary.value("expected_chunk_count"))
      &&summary.value("expected_chunk_count")==body.value("expected_chunk_count")
      &&integer(summary.value("total_duration_ms"))&&summary.value("total_duration_ms").toDouble()>0
      &&digest(summary.value("plan_digest"))&&digest(summary.value("content_digest"))
      &&summary.value("task_binding").isObject()&&binding.value("task_id")==body.value("task_id")
      &&binding.value("name").isString()&&!binding.value("name").toString().trimmed().isEmpty()
      &&(binding.value("binding")==QJsonValue(QStringLiteral("source"))||binding.value("binding")==QJsonValue(QStringLiteral("declared")))
      &&summary.value("physical_provenance_verified")==QJsonValue(false);
}

class OriginalCaptureReviewDialog final : public QDialog {
public:
  OriginalCaptureReviewDialog(const QJsonObject& summary,const QJsonObject& body,QWidget* parent=nullptr):QDialog(parent){
    setWindowTitle(QStringLiteral("Revisar captura original"));resize(760,510);auto* layout=new QVBoxLayout(this);
    const auto task=summary.value("task_binding").toObject();
    auto* description=new QLabel(QStringLiteral("Conta: %1\nTarefa: %2 (%3)\nUma sessão · %4 partes · %5 min\n\nMP4 e ZIP permanecem intactos. IDs, metadados e clocks originais serão preservados. Evaluate e finalize estão exigidos; o envio ainda não começou.")
      .arg(body.value("account_email").toString(),task.value("name").toString(),body.value("task_id").toString())
      .arg(summary.value("expected_chunk_count").toInt()).arg(summary.value("total_duration_ms").toDouble()/60000.,0,'f',2));
    description->setTextFormat(Qt::PlainText);description->setWordWrap(true);description->setAccessibleName(QStringLiteral("Resumo da captura original e política de conclusão"));layout->addWidget(description);
    auto* hashes=new QLabel(QStringLiteral("SHA-256 do conteúdo: %1\nSHA-256 do plano: %2").arg(summary.value("content_digest").toString(),summary.value("plan_digest").toString()));
    hashes->setWordWrap(true);hashes->setTextInteractionFlags(Qt::TextSelectableByKeyboard|Qt::TextSelectableByMouse);hashes->setAccessibleName(QStringLiteral("Hashes da captura original"));layout->addWidget(hashes);
    auto* provenance=new QLabel(QStringLiteral("Origem física não comprovada: a inspeção local confere os arquivos e seus vínculos; não certifica câmera, sensores, localização ou aprovação do provedor."));
    provenance->setWordWrap(true);provenance->setAccessibleName(QStringLiteral("Limite de verificação da origem física"));layout->addWidget(provenance);
    auto* buttons=new QDialogButtonBox;auto* back=buttons->addButton(QStringLiteral("Voltar sem enviar"),QDialogButtonBox::RejectRole);
    back->setDefault(true);auto* confirm=buttons->addButton(QStringLiteral("Confirmar e enviar original"),QDialogButtonBox::AcceptRole);
    confirm->setAutoDefault(false);confirm->setAccessibleName(QStringLiteral("Confirmar envio da captura original preservada"));
    connect(buttons,&QDialogButtonBox::accepted,this,&QDialog::accept);connect(buttons,&QDialogButtonBox::rejected,this,&QDialog::reject);layout->addWidget(buttons);
  }
};
