#ifdef _WIN32
#include <windows.h>

#include <filesystem>
#include <fstream>
#include <string>
#include <thread>
#include <vector>
#include <cwctype>

namespace fs = std::filesystem;

namespace {
DWORD parseProcessId(const std::wstring& text) {
  if (text.empty() || text.find_first_not_of(L"0123456789") != std::wstring::npos) return 0;
  try {
    const auto value = std::stoull(text);
    return value > 0 && value <= MAXDWORD ? static_cast<DWORD>(value) : 0;
  } catch (const std::exception&) {
    return 0;
  }
}

bool waitForParentExit(DWORD pid, DWORD timeout = 60000) {
  if (!pid) return false;
  HANDLE process = OpenProcess(SYNCHRONIZE, FALSE, pid);
  // Acesso negado não significa que o processo terminou.
  if (!process) return GetLastError() == ERROR_INVALID_PARAMETER;
  const DWORD result = WaitForSingleObject(process, timeout);
  CloseHandle(process);
  return result == WAIT_OBJECT_0;
}

std::wstring quote(const std::wstring& value) { return L"\"" + value + L"\""; }

std::string utf8(const std::wstring& value) {
  if (value.empty()) return {};
  const int size = WideCharToMultiByte(CP_UTF8, 0, value.data(),
                                       static_cast<int>(value.size()),
                                       nullptr, 0, nullptr, nullptr);
  if (size <= 0) return {};
  std::string output(static_cast<std::size_t>(size), '\0');
  WideCharToMultiByte(CP_UTF8, 0, value.data(), static_cast<int>(value.size()),
                      output.data(), size, nullptr, nullptr);
  return output;
}

void logLine(const fs::path& target, const std::wstring& line) {
  wchar_t local[MAX_PATH]{};
  const DWORD length = GetEnvironmentVariableW(L"LOCALAPPDATA", local, MAX_PATH);
  const fs::path root = length > 0 ? fs::path(local) / L"QMoney" : target;
  std::error_code ec;
  fs::create_directories(root, ec);
  // O locale padrão de std::wofstream no Windows corrompe acentos. O arquivo
  // de diagnóstico é UTF-8 explícito para permanecer legível em qualquer PC.
  std::ofstream log(root / L"update.log", std::ios::app | std::ios::binary);
  SYSTEMTIME time{};
  GetLocalTime(&time);
  log << "[" << time.wYear << "-" << time.wMonth << "-" << time.wDay << " "
      << time.wHour << ":" << time.wMinute << ":" << time.wSecond << "] "
      << utf8(line) << "\n";
}

bool runProcess(const std::wstring& command, DWORD timeout = 300000,
                bool terminateOnTimeout = true) {
  std::vector<wchar_t> mutableCommand(command.begin(), command.end());
  mutableCommand.push_back(L'\0');
  STARTUPINFOW startup{sizeof(startup)};
  PROCESS_INFORMATION process{};
  if (!CreateProcessW(nullptr, mutableCommand.data(), nullptr, nullptr, FALSE,
                      CREATE_NO_WINDOW, nullptr, nullptr, &startup, &process)) return false;
  const DWORD wait = WaitForSingleObject(process.hProcess, timeout);
  if (wait != WAIT_OBJECT_0 && terminateOnTimeout) {
    TerminateProcess(process.hProcess, 5);
    WaitForSingleObject(process.hProcess, 5000);
  }
  DWORD exitCode = 1;
  if (wait == WAIT_OBJECT_0) GetExitCodeProcess(process.hProcess, &exitCode);
  CloseHandle(process.hThread);
  CloseHandle(process.hProcess);
  return wait == WAIT_OBJECT_0 && exitCode == 0;
}

std::wstring argument(int argc, wchar_t** argv, const std::wstring& name) {
  for (int i = 1; i + 1 < argc; ++i)
    if (argv[i] == name) return argv[i + 1];
  return {};
}

bool hasFlag(int argc, wchar_t** argv, const std::wstring& name) {
  for (int i = 1; i < argc; ++i)
    if (argv[i] == name) return true;
  return false;
}

bool copyPackage(const fs::path& source, const fs::path& target) {
  std::error_code ec;
  for (const auto& entry : fs::recursive_directory_iterator(source, ec)) {
    if (ec) return false;
    const fs::path relative = fs::relative(entry.path(), source, ec);
    if (ec) return false;
    fs::path destination = target / relative;
    if (entry.is_directory()) {
      fs::create_directories(destination, ec);
    } else if (entry.is_regular_file()) {
      fs::create_directories(destination.parent_path(), ec);
      if (destination.filename() == L"QMoneyUpdater.exe")
        destination = target / L"QMoneyUpdater.new.exe";
      // O std::filesystem do MinGW 13 devolve ERROR_FILE_EXISTS mesmo com
      // overwrite_existing em alguns volumes NTFS. O backup transacional já
      // foi concluído, então remova explicitamente antes da cópia.
      if (fs::exists(destination)) fs::remove(destination, ec);
      if (!ec) fs::copy_file(entry.path(), destination, fs::copy_options::none, ec);
    }
    if (ec) {
      const std::string narrow = ec.message();
      logLine(target, L"Falha ao copiar " + entry.path().wstring() + L" -> "
                          + destination.wstring() + L": "
                          + std::wstring(narrow.begin(), narrow.end()));
      return false;
    }
  }
  return true;
}

fs::path installedDestination(const fs::path& relative, const fs::path& target) {
  if (relative.filename() == L"QMoneyUpdater.exe")
    return target / L"QMoneyUpdater.new.exe";
  return target / relative;
}

bool backupPackageTargets(const fs::path& source, const fs::path& target,
                          const fs::path& backup) {
  std::error_code ec;
  // Backups anteriores pertencem ao usuário; nunca reutilize/apague um deles.
  if (fs::exists(backup, ec) || ec) return false;
  fs::create_directories(backup, ec);
  if (ec) return false;
  for (const auto& entry : fs::recursive_directory_iterator(source, ec)) {
    if (ec) return false;
    if (!entry.is_regular_file()) continue;
    const fs::path relative = fs::relative(entry.path(), source, ec);
    if (ec) return false;
    const fs::path installed = installedDestination(relative, target);
    if (!fs::exists(installed)) continue;
    const fs::path saved = backup / relative;
    fs::create_directories(saved.parent_path(), ec);
    fs::copy_file(installed, saved, fs::copy_options::overwrite_existing, ec);
    if (ec) return false;
  }
  const fs::path updater = target / L"QMoneyUpdater.exe";
  if (fs::exists(updater)) {
    fs::copy_file(updater, backup / L"QMoneyUpdater.installed.exe",
                  fs::copy_options::overwrite_existing, ec);
  }
  return !ec;
}

bool rollbackPackage(const fs::path& source, const fs::path& target,
                     const fs::path& backup) {
  std::error_code ec;
  for (const auto& entry : fs::recursive_directory_iterator(source, ec)) {
    if (ec) return false;
    if (!entry.is_regular_file()) continue;
    const fs::path relative = fs::relative(entry.path(), source, ec);
    if (ec) return false;
    const fs::path installed = installedDestination(relative, target);
    const fs::path saved = backup / relative;
    if (fs::exists(saved)) {
      fs::create_directories(installed.parent_path(), ec);
      if (fs::exists(installed)) fs::remove(installed, ec);
      if (!ec) fs::copy_file(saved, installed, fs::copy_options::none, ec);
    } else {
      fs::remove(installed, ec);
    }
    if (ec) return false;
  }
  const fs::path savedUpdater = backup / L"QMoneyUpdater.installed.exe";
  if (fs::exists(savedUpdater)) {
    // This updater is still executing from QMoneyUpdater.exe on Windows.
    // Stage its restoration for the next launch instead of deleting a running binary.
    const fs::path pendingUpdater = target / L"QMoneyUpdater.new.exe";
    if (fs::exists(pendingUpdater)) fs::remove(pendingUpdater, ec);
    if (!ec) fs::copy_file(savedUpdater, pendingUpdater,
                           fs::copy_options::none, ec);
  }
  return !ec;
}

enum class InstallResult { Installed, BackupFailed, CopyFailed, RollbackFailed, InvalidPackage };

bool packagePreservesPrivateData(const fs::path& source) {
  std::error_code ec;
  for (const auto& entry : fs::recursive_directory_iterator(source, ec)) {
    if (ec || entry.is_symlink()) return false;
    const auto relative = entry.path().lexically_relative(source);
    for (const auto& part : relative) {
      std::wstring name = part.wstring();
      for (auto& c : name) c = static_cast<wchar_t>(std::towlower(c));
      if (name == L"data" || name == L"secrets" || name == L".env" ||
          name == L".qmoney-rollback" || name == L"..") return false;
    }
  }
  return !ec;
}

InstallResult installPackage(const fs::path& source, const fs::path& target,
                             const fs::path& backup) {
  // A partial backup must never be used to restore an untouched installation.
  if (!packagePreservesPrivateData(source)) return InstallResult::InvalidPackage;
  if (!backupPackageTargets(source, target, backup)) return InstallResult::BackupFailed;
  if (copyPackage(source, target)) return InstallResult::Installed;
  return rollbackPackage(source, target, backup) ? InstallResult::CopyFailed
                                                : InstallResult::RollbackFailed;
}

bool launchAndValidate(const fs::path& executable, const fs::path& marker,
                       DWORD timeoutMs = 60000) {
  std::wstring command = quote(executable.wstring()) + L" --update-health "
                         + quote(marker.wstring());
  std::vector<wchar_t> mutableCommand(command.begin(), command.end());
  mutableCommand.push_back(L'\0');
  STARTUPINFOW startup{sizeof(startup)};
  PROCESS_INFORMATION process{};
  if (!CreateProcessW(nullptr, mutableCommand.data(), nullptr, nullptr, FALSE,
                      0, nullptr, executable.parent_path().c_str(),
                      &startup, &process)) return false;
  CloseHandle(process.hThread);
  const DWORD step = 250;
  DWORD elapsed = 0;
  bool healthy = false;
  while (elapsed < timeoutMs) {
    if (fs::exists(marker)) { healthy = true; break; }
    if (WaitForSingleObject(process.hProcess, 0) == WAIT_OBJECT_0) break;
    Sleep(step);
    elapsed += step;
  }
  if (!healthy && WaitForSingleObject(process.hProcess, 0) != WAIT_OBJECT_0) {
    TerminateProcess(process.hProcess, 5);
    WaitForSingleObject(process.hProcess, 5000);
  }
  CloseHandle(process.hProcess);
  return healthy;
}
}  // namespace

int WINAPI WinMain(HINSTANCE, HINSTANCE, LPSTR, int) {
  int argc = 0;
  wchar_t** argv = CommandLineToArgvW(GetCommandLineW(), &argc);
  if (!argv) return 2;
  const fs::path package = argument(argc, argv, L"--package");
  const fs::path target = argument(argc, argv, L"--target");
  const std::wstring pidText = argument(argc, argv, L"--pid");
  const std::wstring launch = argument(argc, argv, L"--launch");
  const bool silent = hasFlag(argc, argv, L"--silent");
  LocalFree(argv);
  if (package.empty() || target.empty() || pidText.empty() || launch.empty()) return 2;

  const DWORD pid = parseProcessId(pidText);
  if (!pid) return 2;
  if (!waitForParentExit(pid)) {
    logLine(target, L"O aplicativo não encerrou; atualização cancelada antes de alterar arquivos.");
    if (!silent)
      MessageBoxW(nullptr, L"Feche o QMoney e tente atualizar novamente.",
                  L"QMoney", MB_OK | MB_ICONERROR);
    return 4;
  }

  wchar_t tempPath[MAX_PATH]{};
  const DWORD tempLength = GetTempPathW(MAX_PATH, tempPath);
  if (!tempLength || tempLength >= MAX_PATH) return 3;
  const std::wstring runId = std::to_wstring(GetCurrentProcessId()) + L"-" + std::to_wstring(GetTickCount64());
  const fs::path staging = fs::path(tempPath) / (L"QMoney-" + runId);
  const fs::path payload = staging / L"payload";
  const fs::path marker = staging / L"health-marker";
  const fs::path backup = target / L".qmoney-rollback" /
      runId;
  std::error_code ec;
  // Não remova diretórios de uma execução anterior, mesmo se o PID foi reutilizado.
  if (!fs::create_directory(staging, ec) || ec) return 3;
  if (!fs::create_directory(payload, ec) || ec) return 3;

  const std::wstring extract = L"tar.exe -xf " + quote(package.wstring()) + L" -C " + quote(payload.wstring());
  if (!runProcess(extract)) {
    logLine(target, L"Falha ao extrair o pacote; instalação original preservada.");
    fs::remove_all(staging, ec);
    if (!silent)
      MessageBoxW(nullptr, L"Não foi possível extrair a atualização. A instalação não foi alterada.",
                  L"QMoney", MB_OK | MB_ICONERROR);
    return 4;
  }
  const auto installation = installPackage(payload, target, backup);
  if (installation != InstallResult::Installed) {
    logLine(target, installation == InstallResult::InvalidPackage
        ? L"Pacote contém caminhos privados; instalação preservada."
        : installation == InstallResult::BackupFailed
        ? L"Falha no backup; instalação original preservada."
        : installation == InstallResult::RollbackFailed
            ? L"Falha na cópia e na restauração; backup mantido para recuperação."
            : L"Falha na cópia; instalação anterior restaurada.");
    fs::remove_all(staging, ec);
    if (!silent)
      MessageBoxW(nullptr, installation == InstallResult::RollbackFailed
                  ? L"A atualização e a restauração falharam. Consulte update.log e preserve a pasta .qmoney-rollback."
                  : L"A atualização não pôde ser instalada. A instalação anterior foi preservada.",
                  L"QMoney", MB_OK | MB_ICONERROR);
    return 4;
  }

  if (!launchAndValidate(target / launch, marker)) {
    logLine(target, L"Nova versão falhou no teste de saúde; iniciando rollback.");
    if (!rollbackPackage(payload, target, backup)) {
      if (!silent)
        MessageBoxW(nullptr,
                    L"A nova versão não iniciou e a restauração automática falhou. Consulte update.log.",
                    L"QMoney", MB_OK | MB_ICONERROR);
      fs::remove_all(staging, ec);
      return 5;
    }
    runProcess(quote((target / launch).wstring()), 1000, false);
    if (!silent)
      MessageBoxW(nullptr,
                  L"A nova versão não iniciou. O QMoney restaurou automaticamente a versão anterior.",
                  L"QMoney", MB_OK | MB_ICONWARNING);
    fs::remove_all(staging, ec);
    return 6;
  }

  fs::remove(marker, ec);
  fs::remove_all(staging, ec);
  // Preserve o ZIP e todos os backups para recuperação manual.
  logLine(target, L"Atualização instalada e validada com sucesso.");
  return 0;
}
#else
int main() { return 1; }
#endif
