cmake_minimum_required(VERSION 3.21)

if(DEFINED ENV{QMONEY_VERSION} AND NOT "$ENV{QMONEY_VERSION}" STREQUAL "")
  set(QMONEY_VERSION "$ENV{QMONEY_VERSION}")
else()
  set(QMONEY_VERSION "1.0.8")
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
  target_link_libraries(QMoneyUpdater PRIVATE shell32)
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
