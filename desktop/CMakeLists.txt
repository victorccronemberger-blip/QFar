cmake_minimum_required(VERSION 3.21)

if(DEFINED ENV{QMONEY_VERSION} AND NOT "$ENV{QMONEY_VERSION}" STREQUAL "")
  set(QMONEY_VERSION "$ENV{QMONEY_VERSION}")
else()
set(QMONEY_VERSION "2.0.85")
endif()
project(QMoney VERSION ${QMONEY_VERSION} LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)
set(CMAKE_AUTOMOC ON)
set(CMAKE_AUTORCC ON)
set(CMAKE_AUTOUIC ON)

find_package(Qt6 6.4 REQUIRED COMPONENTS Core Gui Network Svg Widgets)

set(QLEMENTINE_SANDBOX OFF CACHE BOOL "" FORCE)
set(QLEMENTINE_SHOWCASE OFF CACHE BOOL "" FORCE)
if(EXISTS "${CMAKE_CURRENT_SOURCE_DIR}/../vendor/qlementine/CMakeLists.txt")
  add_subdirectory(${CMAKE_CURRENT_SOURCE_DIR}/../vendor/qlementine
                   ${CMAKE_CURRENT_BINARY_DIR}/qlementine)
else()
  include(FetchContent)
  FetchContent_Declare(qlementine
    GIT_REPOSITORY https://github.com/oclero/qlementine.git
    GIT_TAG v1.4.2
    GIT_SHALLOW TRUE
  )
  FetchContent_MakeAvailable(qlementine)
endif()

include(cmake/FixQlementineMenus.cmake)

set(QMONEY_EMBEDDED_SERVICE_RESOURCE)
set(QMONEY_HAS_EMBEDDED_SERVICE 0)
if(WIN32 AND DEFINED ENV{QMONEY_EMBEDDED_SERVICE}
   AND EXISTS "$ENV{QMONEY_EMBEDDED_SERVICE}")
  file(TO_CMAKE_PATH "$ENV{QMONEY_EMBEDDED_SERVICE}"
       QMONEY_EMBEDDED_SERVICE_PATH)
  configure_file(resources/embedded_service.rc.in
                 ${CMAKE_CURRENT_BINARY_DIR}/embedded_service.rc @ONLY)
  list(APPEND QMONEY_EMBEDDED_SERVICE_RESOURCE
       ${CMAKE_CURRENT_BINARY_DIR}/embedded_service.rc)
  set(QMONEY_HAS_EMBEDDED_SERVICE 1)
endif()

qt_add_executable(QMoney WIN32
  src/main.cpp
  src/ApiClient.hpp
  src/ApiClient.cpp
  src/MainWindow.hpp
  src/ComboBox.hpp
  src/MainWindow.cpp
  src/UpdateManager.hpp
  src/UpdateManager.cpp
  resources/qmoney.qrc
  resources/qmoney.rc
  ${QMONEY_EMBEDDED_SERVICE_RESOURCE}
)

add_executable(QMoneyUpdater WIN32
  src/updater_main.cpp
  resources/qmoney.rc
)
if(MINGW)
  target_link_options(QMoneyUpdater PRIVATE -static -static-libgcc -static-libstdc++)
endif()
if(MSVC)
  set_property(TARGET QMoneyUpdater PROPERTY
               MSVC_RUNTIME_LIBRARY "MultiThreaded$<$<CONFIG:Debug>:Debug>")
endif()
if(WIN32)
  foreach(QMONEY_BINARY_NAME QMoney QMoneyUpdater)
    configure_file(resources/version.rc.in
                   ${CMAKE_CURRENT_BINARY_DIR}/${QMONEY_BINARY_NAME}_version.rc @ONLY)
    target_sources(${QMONEY_BINARY_NAME} PRIVATE
                   ${CMAKE_CURRENT_BINARY_DIR}/${QMONEY_BINARY_NAME}_version.rc)
  endforeach()
  target_link_libraries(QMoneyUpdater PRIVATE shell32 bcrypt)
  target_link_libraries(QMoney PRIVATE bcrypt crypt32)
endif()

target_link_libraries(QMoney PRIVATE
  qlementine
  Qt6::Core
  Qt6::Gui
  Qt6::Network
  Qt6::Svg
  Qt6::Widgets
)

target_compile_definitions(QMoney PRIVATE
  QMONEY_PROJECT_ROOT="${CMAKE_CURRENT_SOURCE_DIR}/.."
  QMONEY_VERSION="${PROJECT_VERSION}"
  QMONEY_HAS_EMBEDDED_SERVICE=${QMONEY_HAS_EMBEDDED_SERVICE}
)

set_target_properties(QMoney PROPERTIES
  OUTPUT_NAME "QMoney"
  VS_DEBUGGER_WORKING_DIRECTORY "${CMAKE_CURRENT_SOURCE_DIR}/.."
)

install(TARGETS QMoney QMoneyUpdater RUNTIME DESTINATION .)

option(QMONEY_BUILD_VISUAL_PREVIEW "Build isolated offscreen visual fixture" OFF)
if(QMONEY_BUILD_VISUAL_PREVIEW)
  enable_testing()
  add_subdirectory(../test/qt ${CMAKE_CURRENT_BINARY_DIR}/contract_tests)
  qt_add_executable(operation_preview
    ../test/qt/operation_preview.cpp
    src/MainWindow.cpp src/MainWindow.hpp src/ApiClient.cpp src/ApiClient.hpp
    src/UpdateManager.cpp src/UpdateManager.hpp resources/qmoney.qrc)
  target_link_libraries(operation_preview PRIVATE qlementine Qt6::Core Qt6::Gui Qt6::Network Qt6::Svg Qt6::Widgets)
  target_compile_definitions(operation_preview PRIVATE QMONEY_PROJECT_ROOT="${CMAKE_CURRENT_SOURCE_DIR}/.." QMONEY_VERSION="${PROJECT_VERSION}" QMONEY_HAS_EMBEDDED_SERVICE=0)
  if(WIN32)
    target_link_libraries(operation_preview PRIVATE bcrypt crypt32)
  endif()
  add_test(NAME settings_navigation COMMAND operation_preview --settings-smoke)
  add_test(NAME campaign_history_evidence COMMAND operation_preview --campaign-history-evidence-smoke)
  add_test(NAME campaign_capacity_review COMMAND operation_preview --campaign-capacity-smoke)
  add_test(NAME campaign_full_reset COMMAND operation_preview --campaign-reset-smoke)
  add_test(NAME campaign_restriction_continue COMMAND operation_preview --campaign-restriction-smoke)
  add_test(NAME nymeria_library_workflow COMMAND operation_preview --nymeria-library-smoke)
  add_test(NAME campaign_all_compatible_preference COMMAND operation_preview --campaign-all-compatible-smoke)
  add_test(NAME campaign_on_demand_flow COMMAND operation_preview --campaign-on-demand-smoke)
  set_tests_properties(campaign_on_demand_flow PROPERTIES ENVIRONMENT "QT_QPA_PLATFORM=offscreen" TIMEOUT 15)
  set_tests_properties(campaign_all_compatible_preference PROPERTIES ENVIRONMENT "QT_QPA_PLATFORM=offscreen" TIMEOUT 15)
  add_test(NAME nymeria_library_invalid_plan COMMAND operation_preview --nymeria-library-smoke --nymeria-plan-malformed)
  add_test(NAME nymeria_library_invalid_space COMMAND operation_preview --nymeria-library-smoke --nymeria-space-malformed)
  add_test(NAME nymeria_library_insufficient_space COMMAND operation_preview --nymeria-library-smoke --nymeria-space-insufficient)
  set_tests_properties(nymeria_library_workflow nymeria_library_invalid_plan nymeria_library_invalid_space nymeria_library_insufficient_space PROPERTIES ENVIRONMENT "QT_QPA_PLATFORM=offscreen" TIMEOUT 20)
  add_test(NAME campaign_restriction_capacity_shortfall COMMAND operation_preview --campaign-restriction-smoke --restriction-shortfall)
  add_test(NAME campaign_restriction_capacity_unknown COMMAND operation_preview --campaign-restriction-smoke --restriction-capacity-unknown)
  add_test(NAME campaign_restriction_transient_failure COMMAND operation_preview --campaign-restriction-smoke --restriction-transient)
  add_test(NAME campaign_pending_access_continue COMMAND operation_preview --campaign-restriction-smoke --restriction-skip)
  add_test(NAME campaign_pending_access_capacity_unknown COMMAND operation_preview --campaign-restriction-smoke --restriction-skip --restriction-capacity-unknown)
  add_test(NAME campaign_pending_access_capacity_shortfall COMMAND operation_preview --campaign-restriction-smoke --restriction-skip --restriction-shortfall)
  set_tests_properties(campaign_pending_access_continue campaign_pending_access_capacity_unknown campaign_pending_access_capacity_shortfall PROPERTIES ENVIRONMENT "QT_QPA_PLATFORM=offscreen" TIMEOUT 15)
  set_tests_properties(campaign_restriction_continue campaign_restriction_capacity_shortfall campaign_restriction_capacity_unknown campaign_restriction_transient_failure PROPERTIES ENVIRONMENT "QT_QPA_PLATFORM=offscreen" TIMEOUT 15)
  set_tests_properties(campaign_full_reset PROPERTIES ENVIRONMENT "QT_QPA_PLATFORM=offscreen" TIMEOUT 20)
  set_tests_properties(campaign_capacity_review PROPERTIES ENVIRONMENT "QT_QPA_PLATFORM=offscreen" TIMEOUT 20)
  add_test(NAME campaign_close_live COMMAND operation_preview --campaign-close-smoke)
  add_test(NAME campaign_quit_live COMMAND operation_preview --campaign-quit-smoke)
  set_tests_properties(campaign_quit_live PROPERTIES ENVIRONMENT "QT_QPA_PLATFORM=offscreen" TIMEOUT 10)
  set_tests_properties(campaign_close_live PROPERTIES ENVIRONMENT "QT_QPA_PLATFORM=offscreen" TIMEOUT 10)
  set_tests_properties(campaign_history_evidence PROPERTIES ENVIRONMENT "QT_QPA_PLATFORM=offscreen" TIMEOUT 10)
  add_test(NAME original_library_browse COMMAND operation_preview --original-library-smoke)
  set_tests_properties(original_library_browse PROPERTIES ENVIRONMENT "QT_QPA_PLATFORM=offscreen" TIMEOUT 15 WORKING_DIRECTORY "${CMAKE_CURRENT_SOURCE_DIR}/..")
  set_tests_properties(settings_navigation PROPERTIES ENVIRONMENT "QT_QPA_PLATFORM=offscreen" TIMEOUT 15)
  add_test(NAME operation_ui_live COMMAND operation_preview --smoke)
  add_test(NAME operation_qa_lifecycle COMMAND operation_preview --operation-qa-smoke)
  add_test(NAME accounts_qa_lifecycle COMMAND operation_preview --accounts-qa-smoke)
  add_test(NAME account_services_independent COMMAND operation_preview --account-services-smoke)
  set_tests_properties(account_services_independent PROPERTIES ENVIRONMENT "QT_QPA_PLATFORM=offscreen" TIMEOUT 15)
  set_tests_properties(accounts_qa_lifecycle PROPERTIES ENVIRONMENT "QT_QPA_PLATFORM=offscreen" TIMEOUT 20)
  set_tests_properties(operation_qa_lifecycle PROPERTIES ENVIRONMENT "QT_QPA_PLATFORM=offscreen" TIMEOUT 15)
  add_test(NAME accelerator_lifecycle COMMAND operation_preview --accelerator-smoke)
  set_tests_properties(accelerator_lifecycle PROPERTIES ENVIRONMENT "QT_QPA_PLATFORM=offscreen" TIMEOUT 15)
  add_test(NAME prepared_library_browse COMMAND operation_preview --prepared-library-smoke)
  set_tests_properties(prepared_library_browse PROPERTIES ENVIRONMENT "QT_QPA_PLATFORM=offscreen" TIMEOUT 20 WORKING_DIRECTORY "${CMAKE_CURRENT_SOURCE_DIR}/..")
  add_test(NAME local_media_library_browse COMMAND operation_preview --local-media-library-smoke)
  set_tests_properties(local_media_library_browse PROPERTIES ENVIRONMENT "QT_QPA_PLATFORM=offscreen" TIMEOUT 20 WORKING_DIRECTORY "${CMAKE_CURRENT_SOURCE_DIR}/..")
  add_test(NAME credential_copy COMMAND operation_preview --credential-copy-smoke)
  set_tests_properties(credential_copy PROPERTIES ENVIRONMENT "QT_QPA_PLATFORM=offscreen" TIMEOUT 15)
  add_test(NAME credential_view COMMAND operation_preview --credential-view-smoke)
  set_tests_properties(credential_view PROPERTIES ENVIRONMENT "QT_QPA_PLATFORM=offscreen" TIMEOUT 15)
  add_test(NAME catalog_loading_recovery COMMAND operation_preview --catalog-loading-smoke)
  add_test(NAME catalog_timeout_result_recovery COMMAND operation_preview --catalog-timeout-recovery-smoke)
  add_test(NAME catalog_real_progress_limits COMMAND operation_preview --catalog-progress-smoke)
  set_tests_properties(catalog_real_progress_limits PROPERTIES ENVIRONMENT "QT_QPA_PLATFORM=offscreen" TIMEOUT 15)
  add_test(NAME campaign_catalog_account_fallback COMMAND operation_preview --catalog-account-fallback-smoke)
  add_test(NAME campaign_catalog_inconclusive_fallback COMMAND operation_preview --catalog-account-inconclusive-smoke)
  add_test(NAME campaign_catalog_unrelated_error_no_fallback COMMAND operation_preview --catalog-account-unrelated-error-smoke)
  add_test(NAME campaign_catalog_double_removal_refresh COMMAND operation_preview --catalog-account-double-removal-smoke)
  add_test(NAME campaign_catalog_archive_failure_visible COMMAND operation_preview --catalog-account-archive-failure-smoke)
  set_tests_properties(campaign_catalog_account_fallback campaign_catalog_inconclusive_fallback
    campaign_catalog_unrelated_error_no_fallback campaign_catalog_double_removal_refresh
    campaign_catalog_archive_failure_visible PROPERTIES ENVIRONMENT "QT_QPA_PLATFORM=offscreen" TIMEOUT 15)
  set_tests_properties(catalog_timeout_result_recovery PROPERTIES ENVIRONMENT "QT_QPA_PLATFORM=offscreen" TIMEOUT 20)
  add_test(NAME recovery_diagnostics_and_retry COMMAND operation_preview --recovery-diagnostics-smoke)
  set_tests_properties(recovery_diagnostics_and_retry PROPERTIES ENVIRONMENT "QT_QPA_PLATFORM=offscreen" TIMEOUT 15)
  add_test(NAME recovery_worker_state_and_poll_budget COMMAND operation_preview --recovery-worker-state-smoke)
  set_tests_properties(recovery_worker_state_and_poll_budget PROPERTIES ENVIRONMENT "QT_QPA_PLATFORM=offscreen" TIMEOUT 15)
  add_test(NAME recovery_command_close_lifecycle COMMAND operation_preview --recovery-command-close-smoke)
  set_tests_properties(recovery_command_close_lifecycle PROPERTIES ENVIRONMENT "QT_QPA_PLATFORM=offscreen" TIMEOUT 15)
  set_tests_properties(catalog_loading_recovery PROPERTIES ENVIRONMENT "QT_QPA_PLATFORM=offscreen" TIMEOUT 15)
  add_test(NAME balance_polling_recovery COMMAND operation_preview --balance-polling-smoke)
  add_test(NAME wallet_readings_and_restrictions COMMAND operation_preview --wallet-smoke)
  set_tests_properties(wallet_readings_and_restrictions PROPERTIES ENVIRONMENT "QT_QPA_PLATFORM=offscreen" TIMEOUT 15)
  add_test(NAME wallet_cooldown_and_hostinger_token COMMAND operation_preview --wallet-cooldown-smoke)
  set_tests_properties(wallet_cooldown_and_hostinger_token PROPERTIES ENVIRONMENT "QT_QPA_PLATFORM=offscreen" TIMEOUT 15)
  add_test(NAME mail_cleanup_review COMMAND operation_preview --mail-cleanup-smoke)
  set_tests_properties(mail_cleanup_review PROPERTIES ENVIRONMENT "QT_QPA_PLATFORM=offscreen" TIMEOUT 15)
  set_tests_properties(balance_polling_recovery PROPERTIES ENVIRONMENT "QT_QPA_PLATFORM=offscreen" TIMEOUT 15)
  add_test(NAME campaign_parameters_restore COMMAND operation_preview --campaign-parameters-smoke)
  set_tests_properties(campaign_parameters_restore PROPERTIES ENVIRONMENT "QT_QPA_PLATFORM=offscreen" TIMEOUT 15)
  add_test(NAME campaign_controls_lifecycle COMMAND operation_preview --campaign-controls-smoke)
  add_test(NAME campaign_evaluation_resume_terminal COMMAND operation_preview --campaign-evaluation-resume-smoke)
  set_tests_properties(campaign_evaluation_resume_terminal PROPERTIES ENVIRONMENT "QT_QPA_PLATFORM=offscreen" TIMEOUT 15)
  add_test(NAME campaign_preflight_transport_recovery COMMAND operation_preview --campaign-controls-smoke --preflight-recovery)
  add_test(NAME campaign_preflight_recovery_bounded COMMAND operation_preview --campaign-controls-smoke --preflight-exhaust)
  set_tests_properties(campaign_preflight_transport_recovery campaign_preflight_recovery_bounded PROPERTIES ENVIRONMENT "QT_QPA_PLATFORM=offscreen" TIMEOUT 15)
  add_test(NAME campaign_start_persistence COMMAND operation_preview --start-persistence-smoke)
  add_test(NAME campaign_start_persistence_terminal COMMAND operation_preview --start-persistence-smoke --start-terminal-persistence)
  add_test(NAME campaign_start_confirmed_restart COMMAND operation_preview --confirmed-start-restart-smoke)
  set_tests_properties(campaign_start_confirmed_restart PROPERTIES ENVIRONMENT "QT_QPA_PLATFORM=offscreen" TIMEOUT 15)
  set_tests_properties(campaign_start_persistence_terminal PROPERTIES ENVIRONMENT "QT_QPA_PLATFORM=offscreen" TIMEOUT 15)
  set_tests_properties(campaign_start_persistence PROPERTIES ENVIRONMENT "QT_QPA_PLATFORM=offscreen" TIMEOUT 15)
  set_tests_properties(campaign_controls_lifecycle PROPERTIES ENVIRONMENT "QT_QPA_PLATFORM=offscreen" TIMEOUT 15)
  add_test(NAME campaign_activity_indicator COMMAND operation_preview --indicator-smoke)
  add_test(NAME campaign_start_completed_response_lost COMMAND operation_preview --indicator-smoke --start-terminal)
  set_tests_properties(campaign_start_completed_response_lost PROPERTIES ENVIRONMENT "QT_QPA_PLATFORM=offscreen" TIMEOUT 15)
  add_test(NAME campaign_start_malformed_response COMMAND operation_preview --indicator-smoke --start-malformed)
  set_tests_properties(campaign_start_malformed_response PROPERTIES ENVIRONMENT "QT_QPA_PLATFORM=offscreen" TIMEOUT 15)
  add_test(NAME campaign_start_response_lost COMMAND operation_preview --indicator-smoke --start-uncertain)
  set_tests_properties(campaign_start_response_lost PROPERTIES ENVIRONMENT "QT_QPA_PLATFORM=offscreen" TIMEOUT 15)
  set_tests_properties(campaign_activity_indicator PROPERTIES ENVIRONMENT "QT_QPA_PLATFORM=offscreen" TIMEOUT 15)
  add_test(NAME campaign_continuation_review COMMAND operation_preview --continuation-smoke)
  add_test(NAME campaign_expired_review COMMAND operation_preview --continuation-smoke --expired-review)
  set_tests_properties(campaign_expired_review PROPERTIES ENVIRONMENT "QT_QPA_PLATFORM=offscreen" TIMEOUT 15)
  add_test(NAME campaign_continuation_accept COMMAND operation_preview --continuation-smoke --continuation-accept)
  set_tests_properties(campaign_continuation_accept PROPERTIES ENVIRONMENT "QT_QPA_PLATFORM=offscreen" TIMEOUT 15)
  set_tests_properties(campaign_continuation_review PROPERTIES ENVIRONMENT "QT_QPA_PLATFORM=offscreen" TIMEOUT 15)
  set_tests_properties(operation_ui_live PROPERTIES ENVIRONMENT "QT_QPA_PLATFORM=offscreen" TIMEOUT 15)
endif()
