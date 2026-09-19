#include "../../desktop/src/updater_main.cpp"
#include <iostream>
#include <stdexcept>

void require(bool condition, const char* message) {
  if (!condition) throw std::runtime_error(message);
}
void writeFile(const fs::path& path, const std::string& value) {
  fs::create_directories(path.parent_path());
  std::ofstream(path, std::ios::binary) << value;
}
std::string readFile(const fs::path& path) {
  std::ifstream stream(path, std::ios::binary);
  return {std::istreambuf_iterator<char>(stream), {}};
}

int main() {
  const auto root = fs::temp_directory_path() /
      (L"qmoney-updater-test-" + std::to_wstring(GetCurrentProcessId()) + L"-" + std::to_wstring(GetTickCount64()));
  const auto source = root / "source";
  const auto target = root / "target";
  const auto backup = root / "backup";
  try {
    require(parseProcessId(L"123") == 123, "valid process id rejected");
    for (const auto* invalid : {L"0", L"-1", L"123extra", L" 123", L"4294967296", L"999999999999999999999999"})
      require(parseProcessId(invalid) == 0, "invalid process id accepted");
    require(!waitForParentExit(0, 0), "system process accepted as exited parent");
    require(!waitForParentExit(GetCurrentProcessId(), 0), "live process accepted as exited parent");
    writeFile(source / "app.exe", "new app");
    writeFile(source / "runtime.dll", "new runtime");
    writeFile(target / "app.exe", "old app");
    writeFile(target / "runtime.dll", "old runtime");
    writeFile(target / "secrets/token_private.json", "private token");
    writeFile(target / "data/history.json", "private history");
    writeFile(target / ".env", "private settings");
    writeFile(target / "custom.txt", "keep this file");
    writeFile(source / "secrets/token_private.json", "foreign token");
    require(installPackage(source, target, backup) == InstallResult::InvalidPackage, "private package accepted");
    require(readFile(target / "secrets/token_private.json") == "private token", "private data overwritten");
    fs::remove_all(source / "secrets");
    // A blocked backup destination must never trigger a rollback using its
    // incomplete contents (which would delete untouched installed files).
    writeFile(backup, "not a directory");
    HANDLE lock = CreateFileW(backup.c_str(), GENERIC_READ, 0, nullptr, OPEN_EXISTING, 0, nullptr);
    require(lock != INVALID_HANDLE_VALUE, "cannot lock backup fixture");
    const auto failed = installPackage(source, target, backup);
    CloseHandle(lock);
    require(failed == InstallResult::BackupFailed, "backup failure not reported");
    require(readFile(target / "app.exe") == "old app", "app altered after failed backup");
    require(readFile(target / "runtime.dll") == "old runtime", "runtime altered after failed backup");
    fs::remove(backup);
    require(installPackage(source, target, backup) == InstallResult::Installed, "successful install failed");
    require(readFile(target / "app.exe") == "new app", "new app not installed");
    require(readFile(backup / "app.exe") == "old app", "backup incomplete");
    require(readFile(target / "data/history.json") == "private history", "history changed");
    require(readFile(target / ".env") == "private settings", "settings changed");
    require(readFile(target / "custom.txt") == "keep this file", "custom file removed");
    require(installPackage(source, target, backup) == InstallResult::BackupFailed, "existing backup reused");
    require(rollbackPackage(source, target, backup), "rollback failed");
    require(readFile(target / "app.exe") == "old app", "rollback did not restore app");
    require(readFile(target / "runtime.dll") == "old runtime", "rollback did not restore runtime");
    writeFile(source / "QMoneyUpdater.exe", "new updater");
    writeFile(target / "QMoneyUpdater.exe", "running updater");
    HANDLE updaterLock = CreateFileW((target / "QMoneyUpdater.exe").c_str(), GENERIC_READ,
                                     FILE_SHARE_READ, nullptr, OPEN_EXISTING, 0, nullptr);
    require(updaterLock != INVALID_HANDLE_VALUE, "cannot lock installed updater");
    const auto secondBackup = root / "backup-2";
    const auto installed = installPackage(source, target, secondBackup);
    const bool restored = installed == InstallResult::Installed && rollbackPackage(source, target, secondBackup);
    CloseHandle(updaterLock);
    require(restored, "rollback tried to overwrite the running updater");
    require(readFile(target / "QMoneyUpdater.exe") == "running updater", "running updater changed");
    require(readFile(target / "QMoneyUpdater.new.exe") == "running updater", "updater restoration not staged");
    fs::remove_all(root);
    std::cout << "Updater transaction tests passed\n";
    return 0;
  } catch (const std::exception& error) {
    std::cerr << error.what() << "\nFixtures: " << root << '\n';
    return 1;
  }
}
