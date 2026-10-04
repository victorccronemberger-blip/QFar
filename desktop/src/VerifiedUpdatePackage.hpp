#pragma once

#ifdef _WIN32
#include <windows.h>
#include <bcrypt.h>

#include <array>
#include <filesystem>
#include <string>
#include <vector>

namespace qmoney {

// The native consumer verifies the exact bytes handed off by the GUI. The
// file and ancestor handles remain alive while the extractor opens the same
// path; Windows denies file mutation/deletion and ancestor renaming, and
// preexisting ancestor reparse points are rejected before file consumption.
// Signature/version policy is a separate contract, performed by the GUI.
class VerifiedUpdatePackage final {
public:
  VerifiedUpdatePackage() = default;
  VerifiedUpdatePackage(const VerifiedUpdatePackage&) = delete;
  VerifiedUpdatePackage& operator=(const VerifiedUpdatePackage&) = delete;
  ~VerifiedUpdatePackage() { close(); }

  bool open(const std::filesystem::path& path, const std::wstring& expectedHex) {
    if (_file != INVALID_HANDLE_VALUE || !_directories.empty() || !path.is_absolute() || expectedHex.size() != 64)
      return false;
    for (const auto& part : path)
      if (part == L"." || part == L"..") return false;
    std::array<unsigned char, 32> expected{};
    auto nibble = [](wchar_t c) {
      if (c >= L'0' && c <= L'9') return int(c - L'0');
      if (c >= L'a' && c <= L'f') return int(c - L'a' + 10);
      if (c >= L'A' && c <= L'F') return int(c - L'A' + 10);
      return -1;
    };
    for (std::size_t index = 0; index < expected.size(); ++index) {
      const int high = nibble(expectedHex[index * 2]);
      const int low = nibble(expectedHex[index * 2 + 1]);
      if (high < 0 || low < 0) return false;
      expected[index] = static_cast<unsigned char>((high << 4) | low);
    }
    // Lock from the volume root toward the containing directory. Opening a
    // parent first prevents its name from being substituted while opening the
    // next child; OPEN_REPARSE_POINT observes the directory itself, not its target.
    std::vector<std::filesystem::path> ancestors;
    auto parent = path.parent_path();
    while (!parent.empty()) {
      ancestors.push_back(parent);
      const auto next = parent.parent_path();
      if (next == parent) break;
      parent = next;
    }
    for (auto ancestor = ancestors.rbegin(); ancestor != ancestors.rend(); ++ancestor) {
      const HANDLE directory = CreateFileW(ancestor->c_str(), FILE_READ_ATTRIBUTES,
          FILE_SHARE_READ | FILE_SHARE_WRITE, nullptr, OPEN_EXISTING,
          FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT, nullptr);
      BY_HANDLE_FILE_INFORMATION information{};
      if (directory == INVALID_HANDLE_VALUE) { close(); return false; }
      _directories.push_back(directory);
      if (GetFileType(directory) != FILE_TYPE_DISK || !GetFileInformationByHandle(directory, &information)
          || !(information.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY)
          || (information.dwFileAttributes & FILE_ATTRIBUTE_REPARSE_POINT)) {
        close();
        return false;
      }
    }
    _file = CreateFileW(path.c_str(), GENERIC_READ, FILE_SHARE_READ, nullptr,
                         OPEN_EXISTING, FILE_FLAG_OPEN_REPARSE_POINT | FILE_FLAG_SEQUENTIAL_SCAN,
                         nullptr);
    if (_file == INVALID_HANDLE_VALUE) { close(); return false; }
    BY_HANDLE_FILE_INFORMATION information{};
    if (GetFileType(_file) != FILE_TYPE_DISK || !GetFileInformationByHandle(_file, &information)
        || (information.dwFileAttributes & (FILE_ATTRIBUTE_DIRECTORY | FILE_ATTRIBUTE_REPARSE_POINT))) {
      close();
      return false;
    }
    if (!matches(expected)) {
      close();
      return false;
    }
    return true;
  }

  bool isOpen() const { return _file != INVALID_HANDLE_VALUE; }

private:
  void close() {
    if (_file != INVALID_HANDLE_VALUE) CloseHandle(_file);
    _file = INVALID_HANDLE_VALUE;
    for (auto directory = _directories.rbegin(); directory != _directories.rend(); ++directory)
      CloseHandle(*directory);
    _directories.clear();
  }

  bool matches(const std::array<unsigned char, 32>& expected) {
    struct CryptoHandles {
      BCRYPT_ALG_HANDLE algorithm{};
      BCRYPT_HASH_HANDLE hash{};
      std::vector<unsigned char> object;
      ~CryptoHandles() {
        if (hash) BCryptDestroyHash(hash);
        if (algorithm) BCryptCloseAlgorithmProvider(algorithm, 0);
      }
    } crypto;
    if (BCryptOpenAlgorithmProvider(&crypto.algorithm, BCRYPT_SHA256_ALGORITHM, nullptr, 0) < 0)
      return false;
    DWORD objectSize = 0, used = 0;
    if (BCryptGetProperty(crypto.algorithm, BCRYPT_OBJECT_LENGTH,
                          reinterpret_cast<PUCHAR>(&objectSize), sizeof(objectSize), &used, 0) < 0
        || !objectSize || used != sizeof(objectSize)) return false;
    crypto.object.resize(objectSize);
    if (BCryptCreateHash(crypto.algorithm, &crypto.hash, crypto.object.data(), objectSize, nullptr, 0, 0) < 0)
      return false;
    std::array<unsigned char, 65536> buffer{};
    while (true) {
      DWORD bytes = 0;
      if (!ReadFile(_file, buffer.data(), static_cast<DWORD>(buffer.size()), &bytes, nullptr))
        return false;
      if (!bytes) break;
      if (BCryptHashData(crypto.hash, buffer.data(), bytes, 0) < 0) return false;
    }
    std::array<unsigned char, 32> actual{};
    if (BCryptFinishHash(crypto.hash, actual.data(), static_cast<ULONG>(actual.size()), 0) < 0)
      return false;
    unsigned int difference = 0;
    for (std::size_t index = 0; index < actual.size(); ++index)
      difference |= actual[index] ^ expected[index];
    return difference == 0;
  }

  HANDLE _file{INVALID_HANDLE_VALUE};
  std::vector<HANDLE> _directories;
};

}  // namespace qmoney
#endif
