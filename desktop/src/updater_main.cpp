#ifdef _WIN32
#include <windows.h>

#include <filesystem>
#include <fstream>
#include <string>
#include <vector>

namespace fs = std::filesystem;

namespace {
std::wstring quote(const std::wstring& value) { return L"\"" + value + L"\""; }

void logLine(const fs::path& target, const std::wstring& line) {
  wchar_t local[MAX_PATH]{};
  const DWORD length = GetEnvironmentVariableW(L"LOCALAPPDATA", local, MAX_PATH);
  const fs::path root = length > 0 ? fs::path(local) / L"QMoney" : target;
  std::error_code ec;
  fs::create_directories(root, ec);
  std::wofstream log(root / L"update.log", std::ios::app);
  SYSTEMTIME time{};
  GetLocalTime(&time);
  log << L"[" << time.wYear << L"-" << time.wMonth << L"-" << time.wDay << L" "
      << time.wHour << L":" << time.wMinute << L":" << time.wSecond << L"] "
      << line << L"\n";
}

bool runProcess(const std::wstring& command, DWORD timeout = 300000) {
  std::vector<wchar_t> mutableCommand(command.begin(), command.end());
  mutableCommand.push_back(L'\0');
  STARTUPINFOW startup{sizeof(startup)};
  PROCESS_INFORMATION process{};
  if (!CreateProcessW(nullptr, mutableCommand.data(), nullptr, nullptr, FALSE,
                      CREATE_NO_WINDOW, nullptr, nullptr, &startup, &process)) return false;
  const DWORD wait = WaitForSingleObject(process.hProcess, timeout);
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
      fs::copy_file(entry.path(), destination, fs::copy_options::overwrite_existing, ec);
    }
    if (ec) return false;
  }
  return true;
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
  LocalFree(argv);
  if (package.empty() || target.empty() || pidText.empty() || launch.empty()) return 2;

  const DWORD pid = std::wcstoul(pidText.c_str(), nullptr, 10);
  if (HANDLE process = OpenProcess(SYNCHRONIZE, FALSE, pid)) {
    WaitForSingleObject(process, 60000);
    CloseHandle(process);
  }

  wchar_t tempPath[MAX_PATH]{};
  GetTempPathW(MAX_PATH, tempPath);
  const fs::path staging = fs::path(tempPath) / (L"QMoney-" + std::to_wstring(GetCurrentProcessId()));
  std::error_code ec;
  fs::remove_all(staging, ec);
  fs::create_directories(staging, ec);
  if (ec) return 3;

  const std::wstring extract = L"tar.exe -xf " + quote(package.wstring()) + L" -C " + quote(staging.wstring());
  if (!runProcess(extract) || !copyPackage(staging, target)) {
    logLine(target, L"Falha ao extrair ou copiar o pacote.");
    fs::remove_all(staging, ec);
    MessageBoxW(nullptr, L"A atualização não pôde ser instalada. Seus dados não foram alterados.",
                L"QMoney", MB_OK | MB_ICONERROR);
    return 4;
  }

  fs::remove_all(staging, ec);
  fs::remove(package, ec);
  logLine(target, L"Atualização instalada com sucesso.");
  runProcess(quote((target / launch).wstring()), 1000);
  return 0;
}
#else
int main() { return 1; }
#endif
