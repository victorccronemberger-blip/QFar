# v1.4.2 installs another menu click filter on every stylesheet/theme polish.
# Apply to both local vendor and FetchContent sources before compilation.
get_target_property(_qmoney_qlementine_dir qlementine SOURCE_DIR)
set(_qmoney_menu_source "${_qmoney_qlementine_dir}/src/style/QlementineStyle.cpp")
file(READ "${_qmoney_menu_source}" _qmoney_menu_code)
set(_qmoney_old_filter "    menu->installEventFilter(new MenuEventFilter(menu));")
set(_qmoney_new_filter [=[    // QMoney: preserve exactly one click filter across repeated theme polish.
    if (!menu->findChild<QObject*>(QStringLiteral("qlementine_menu_event_filter"), Qt::FindDirectChildrenOnly)) {
      auto* filter = new MenuEventFilter(menu);
      filter->setObjectName(QStringLiteral("qlementine_menu_event_filter"));
    }]=])
if(_qmoney_menu_code MATCHES "qlementine_menu_event_filter")
  # Already patched during an earlier configuration.
elseif(_qmoney_menu_code MATCHES "menu->installEventFilter\\(new MenuEventFilter\\(menu\\)\\);")
  string(REPLACE "${_qmoney_old_filter}" "${_qmoney_new_filter}" _qmoney_menu_code "${_qmoney_menu_code}")
  file(WRITE "${_qmoney_menu_source}" "${_qmoney_menu_code}")
else()
  message(FATAL_ERROR "Qlementine menu implementation changed; review the click-filter fix.")
endif()
