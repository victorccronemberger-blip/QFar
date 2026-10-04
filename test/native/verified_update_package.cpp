#include "../../desktop/src/VerifiedUpdatePackage.hpp"
#include <shellapi.h>
#include <winioctl.h>
#include <cstring>
#include <fstream>
#include <iostream>
#include <stdexcept>

namespace fs = std::filesystem;

void require(bool condition, const char* message) {
  if (!condition) throw std::runtime_error(message);
}

void writeFile(const fs::path& path, const char* value) {
  std::ofstream file(path, std::ios::binary | std::ios::trunc);
  file << value;
  require(bool(file), "cannot write artificial package");
}

bool writesAndDeletionBlocked(const fs::path& package) {
  const HANDLE writer = CreateFileW(package.c_str(), GENERIC_WRITE,
      FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE, nullptr, OPEN_EXISTING, 0, nullptr);
  if (writer != INVALID_HANDLE_VALUE) { CloseHandle(writer); return false; }
  if (GetLastError() != ERROR_SHARING_VIOLATION) return false;
  if (DeleteFileW(package.c_str()) || GetLastError() != ERROR_SHARING_VIOLATION) return false;
  const fs::path parent = package.parent_path();
  if (MoveFileExW(parent.c_str(), fs::path(parent.wstring() + L"-moved").c_str(), 0)) return false;
  return true;
}

bool makeJunction(const fs::path& link, const fs::path& target) {
  struct Data {
    DWORD tag;
    USHORT length, reserved, substituteOffset, substituteLength, printOffset, printLength;
    wchar_t buffer[2048];
  } data{};
  const std::wstring print = target.wstring(), substitute = L"\\??\\" + print;
  if (print.size() + substitute.size() + 2 > 2048 || !fs::create_directory(link)) return false;
  data.tag = IO_REPARSE_TAG_MOUNT_POINT;
  data.substituteLength = static_cast<USHORT>(substitute.size() * sizeof(wchar_t));
  data.printOffset = static_cast<USHORT>((substitute.size() + 1) * sizeof(wchar_t));
  data.printLength = static_cast<USHORT>(print.size() * sizeof(wchar_t));
  data.length = static_cast<USHORT>(8 + data.printOffset + data.printLength + sizeof(wchar_t));
  std::memcpy(data.buffer, substitute.data(), data.substituteLength);
  std::memcpy(reinterpret_cast<char*>(data.buffer) + data.printOffset, print.data(), data.printLength);
  HANDLE directory = CreateFileW(link.c_str(), GENERIC_WRITE, FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
      nullptr, OPEN_EXISTING, FILE_FLAG_OPEN_REPARSE_POINT | FILE_FLAG_BACKUP_SEMANTICS, nullptr);
  if (directory == INVALID_HANDLE_VALUE) return false;
  DWORD returned = 0;
  const bool result = DeviceIoControl(directory, FSCTL_SET_REPARSE_POINT, &data, data.length + 8,
      nullptr, 0, &returned, nullptr);
  CloseHandle(directory);
  return result;
}

bool childLockProbe(const fs::path& executable, const fs::path& package) {
  const std::wstring command = L"\"" + executable.wstring() + L"\" --probe-lock \"" + package.wstring() + L"\"";
  std::vector<wchar_t> mutableCommand(command.begin(), command.end());
  mutableCommand.push_back(L'\0');
  STARTUPINFOW startup{sizeof(startup)};
  PROCESS_INFORMATION child{};
  if (!CreateProcessW(executable.c_str(), mutableCommand.data(), nullptr, nullptr, FALSE,
                     CREATE_NO_WINDOW, nullptr, nullptr, &startup, &child)) return false;
  CloseHandle(child.hThread);
  const DWORD wait = WaitForSingleObject(child.hProcess, 10000);
  DWORD code = 1;
  if (wait == WAIT_OBJECT_0) GetExitCodeProcess(child.hProcess, &code);
  CloseHandle(child.hProcess);
  return wait == WAIT_OBJECT_0 && code == 0;
}

int main() {
  int count = 0;
  wchar_t** arguments = CommandLineToArgvW(GetCommandLineW(), &count);
  if (!arguments) return 2;
  const fs::path executable = arguments[0];
  if (count == 3 && std::wstring(arguments[1]) == L"--probe-lock") {
    const fs::path package = arguments[2];
    LocalFree(arguments);
    return writesAndDeletionBlocked(package) ? 0 : 1;
  }
  if (count == 5 && std::wstring(arguments[1]) == L"--extract") {
    const fs::path package = arguments[2], destination = arguments[4];
    const std::wstring digest = arguments[3];
    LocalFree(arguments);
    qmoney::VerifiedUpdatePackage verified;
    if (!verified.open(package, digest)) return 3;
    wchar_t system[MAX_PATH]{};
    const UINT size = GetSystemDirectoryW(system, MAX_PATH);
    if (!size || size >= MAX_PATH) return 3;
    const fs::path tar = fs::path(system) / L"tar.exe";
    const std::wstring command = L"\"" + tar.wstring() + L"\" -xf \"" + package.wstring()
        + L"\" -C \"" + destination.wstring() + L"\"";
    std::vector<wchar_t> mutableCommand(command.begin(), command.end());
    mutableCommand.push_back(L'\0');
    STARTUPINFOW startup{sizeof(startup)};
    PROCESS_INFORMATION process{};
    if (!CreateProcessW(tar.c_str(), mutableCommand.data(), nullptr, nullptr, FALSE,
                       CREATE_NO_WINDOW, nullptr, nullptr, &startup, &process)) return 3;
    CloseHandle(process.hThread);
    const DWORD wait = WaitForSingleObject(process.hProcess, 10000);
    DWORD code = 1;
    if (wait == WAIT_OBJECT_0) GetExitCodeProcess(process.hProcess, &code);
    CloseHandle(process.hProcess);
    if (wait != WAIT_OBJECT_0 || code != 0) return 3;
    std::cout << "Actual tar extraction passed while verified file and ancestor handles remained held\n";
    return 0;
  }
  LocalFree(arguments);
  const fs::path root = fs::temp_directory_path() / (L"qmoney-verified-package-test-"
      + std::to_wstring(GetCurrentProcessId()) + L"-" + std::to_wstring(GetTickCount64()));
  const fs::path package = root / L"fixture-package.bin";
  const fs::path replacement = root / L"replacement.bin";
  constexpr auto expected = L"ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad";
  try {
    require(fs::create_directory(root), "cannot create unique fixture root");
    writeFile(package, "abc");
    fs::create_directory(root / L"actual");
    writeFile(root / L"actual/package.bin", "abc");
    require(makeJunction(root / L"ancestor-link", root / L"actual"), "cannot create artificial junction");
    { qmoney::VerifiedUpdatePackage candidate;
      require(!candidate.open(root / L"ancestor-link/package.bin", expected), "ancestor junction accepted"); }
    require(RemoveDirectoryW((root / L"ancestor-link").c_str()), "cannot remove fixture junction itself");
    require(fs::remove(root / L"actual/package.bin") && fs::remove(root / L"actual"), "junction fixture cleanup failed");
    for (const auto& invalid : {std::wstring(), std::wstring(63, L'a'), std::wstring(64, L'g'),
                               std::wstring(65, L'a'), std::wstring(64, L'0')}) {
      qmoney::VerifiedUpdatePackage candidate;
      require(!candidate.open(package, invalid) && !candidate.isOpen(), "invalid or mismatched digest accepted");
    }
    const fs::path renamed = fs::path(root.wstring() + L"-renamed");
    require(MoveFileExW(root.c_str(), renamed.c_str(), 0), "ancestor handle was not released after lifetime");
    require(MoveFileExW(renamed.c_str(), root.c_str(), 0), "cannot restore fixture root name");
    { qmoney::VerifiedUpdatePackage candidate;
      require(!candidate.open(L"relative-package.bin", expected), "relative package accepted");
      require(!candidate.open(root / L"actual/../fixture-package.bin", expected), "parent traversal accepted");
      require(!candidate.open(root, expected), "directory accepted as package"); }
    // Mutating bytes between the GUI's handoff and native consumption fails
    // before extraction; the received hash is never recomputed from metadata.
    writeFile(package, "abd");
    { qmoney::VerifiedUpdatePackage candidate;
      require(!candidate.open(package, expected), "post-handoff mutation accepted"); }
    writeFile(package, "abc");
    writeFile(replacement, "replacement");
    {
      qmoney::VerifiedUpdatePackage verified;
      require(verified.open(package, expected), "known SHA-256 vector rejected");
      require(writesAndDeletionBlocked(package), "writer or deletion allowed during verified lifetime");
      require(childLockProbe(executable, package), "another process could alter verified file");
      require(!MoveFileExW(replacement.c_str(), package.c_str(), MOVEFILE_REPLACE_EXISTING),
              "replacement allowed during verified lifetime");
      const HANDLE reader = CreateFileW(package.c_str(), GENERIC_READ, FILE_SHARE_READ, nullptr,
                                         OPEN_EXISTING, 0, nullptr);
      require(reader != INVALID_HANDLE_VALUE, "extractor-style reader was blocked");
      char bytes[3]{};
      DWORD read = 0;
      const bool readable = ReadFile(reader, bytes, sizeof(bytes), &read, nullptr);
      CloseHandle(reader);
      require(readable && read == 3 && std::string(bytes, 3) == "abc", "reader did not observe verified bytes");
    }
    require(MoveFileExW(root.c_str(), renamed.c_str(), 0), "verified ancestor handles were not released after lifetime");
    require(MoveFileExW(renamed.c_str(), root.c_str(), 0), "cannot restore verified fixture root name");
    require(MoveFileExW(replacement.c_str(), package.c_str(), MOVEFILE_REPLACE_EXISTING),
            "verified handle was not released after lifetime");
    require(fs::remove(package) && fs::remove(root), "fixture cleanup failed");
    std::cout << "Verified package tests passed: strict digest, mutation rejected, reader allowed, ancestor junction rejected, cross-process writes/deletion/replacement/ancestor rename blocked, lifetime released\n";
    return 0;
  } catch (const std::exception& error) {
    std::cerr << error.what() << "\nArtificial fixtures retained: " << root << '\n';
    return 1;
  }
}
