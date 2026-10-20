// AC8 Chinese localization: a narrowly scoped, offline-only PAK reader hook.
// The game executable on disk is never changed.
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <dinput.h>
#include <MinHook.h>
#include <cstdint>
#include <cstring>
#include <cwchar>
#include <cwctype>
#include <string>

extern "C" {
void AC8EntryGate();
void* AC8OriginalEntry{};
}

namespace {
constexpr wchar_t kPakName[] = L"pakchunk0-Windows_998_P.pak";
HMODULE g_system_dinput{};
INIT_ONCE g_proxy_once = INIT_ONCE_STATIC_INIT;
std::wstring g_binary_dir;
std::wstring g_expected_pak;

using PakConstructor = void* (__fastcall *)(void*, void*, const wchar_t*, bool, bool);
PakConstructor g_constructor{};
volatile LONG g_logged_target{};
unsigned char g_entry_original[16]{};
unsigned char g_entry_jump[16]{};
DWORD g_entry_protection{};
bool g_entry_armed{};
bool g_activation_requested{};

std::wstring ModuleFilename(HMODULE module) {
    std::wstring result(32768, L'\0');
    DWORD length = GetModuleFileNameW(module, result.data(), static_cast<DWORD>(result.size()));
    if (!length || length >= result.size()) return {};
    result.resize(length);
    return result;
}

std::wstring Directory(const std::wstring& path) {
    size_t slash = path.find_last_of(L"\\/");
    return slash == std::wstring::npos ? std::wstring() : path.substr(0, slash);
}

std::wstring FullPath(const std::wstring& path) {
    DWORD needed = GetFullPathNameW(path.c_str(), 0, nullptr, nullptr);
    if (!needed) return {};
    std::wstring result(needed, L'\0');
    DWORD length = GetFullPathNameW(path.c_str(), needed, result.data(), nullptr);
    if (!length || length >= needed) return {};
    result.resize(length);
    return result;
}

void Log(const char* message) {
    OutputDebugStringA("AC8 localization: ");
    OutputDebugStringA(message);
    OutputDebugStringA("\n");
}

bool IsOurPak(const wchar_t* filename) {
    if (!filename || !*filename) return false;
    const wchar_t* basename = filename;
    for (const wchar_t* p = filename; *p; ++p)
        if (*p == L'/' || *p == L'\\') basename = p + 1;
    if (_wcsicmp(basename, kPakName) != 0) return false;
    std::wstring candidate(filename);
    for (wchar_t& ch : candidate) if (ch == L'/') ch = L'\\';
    bool absolute = candidate.size() > 1 && (candidate[1] == L':' || candidate[0] == L'\\');
    std::wstring resolved = FullPath(absolute ? candidate : g_binary_dir + L"\\" + candidate);
    if (!resolved.empty() && _wcsicmp(resolved.c_str(), g_expected_pak.c_str()) == 0) return true;
    // Unreal can pass paths relative to its working directory rather than the EXE directory.
    resolved = FullPath(candidate);
    return !resolved.empty() && _wcsicmp(resolved.c_str(), g_expected_pak.c_str()) == 0;
}

void* __fastcall LocalizedPakConstructor(void* self, void* platform_file,
                                        const wchar_t* filename, bool signed_reader, bool load_index) {
    if (IsOurPak(filename)) {
        if (InterlockedCompareExchange(&g_logged_target, 1, 0) == 0)
            Log("Own localization PAK: selecting unsigned reader for this PAK only.");
        return g_constructor(self, platform_file, filename, false, load_index);
    }
    return g_constructor(self, platform_file, filename, signed_reader, load_index);
}

// FPakFile constructor prologue. Only RIP-relative displacement bytes are masked.
constexpr unsigned char kConstructor[] = {
    0x48,0x89,0x5c,0x24,0x10,0x48,0x89,0x6c,0x24,0x18,0x48,0x89,0x74,0x24,0x20,0x57,
    0x41,0x54,0x41,0x55,0x41,0x56,0x41,0x57,0x48,0x83,0xec,0x30,0x48,0x8d,0x05,0x00,
    0x00,0x00,0x00,0x4c,0x8b,0xea,0x48,0x89,0x01,0x4c,0x8b,0xf1,0x48,0x8d,0x05,0x00,
    0x00,0x00,0x00,0x33,0xff,0x48,0x89,0x41,0x10,0x49,0x8b,0xd0,0x89,0x79,0x08,0x41
};
constexpr unsigned char kSignedStore[] = {0x41,0x88,0xb6,0x10,0x02,0x00,0x00};
constexpr size_t kSignedStoreOffset = 0x20e;

bool MatchesConstructor(const unsigned char* code) {
    for (size_t i = 0; i < sizeof(kConstructor); ++i) {
        if ((i >= 31 && i <= 34) || (i >= 47 && i <= 50)) continue;
        if (code[i] != kConstructor[i]) return false;
    }
    return std::memcmp(code + kSignedStoreOffset, kSignedStore, sizeof(kSignedStore)) == 0;
}

void* FindConstructor(HMODULE host) {
    auto base = reinterpret_cast<unsigned char*>(host);
    auto dos = reinterpret_cast<const IMAGE_DOS_HEADER*>(base);
    if (dos->e_magic != IMAGE_DOS_SIGNATURE || dos->e_lfanew < 0) return nullptr;
    auto nt = reinterpret_cast<const IMAGE_NT_HEADERS64*>(base + dos->e_lfanew);
    if (nt->Signature != IMAGE_NT_SIGNATURE || nt->FileHeader.Machine != IMAGE_FILE_MACHINE_AMD64 ||
        nt->OptionalHeader.Magic != IMAGE_NT_OPTIONAL_HDR64_MAGIC) return nullptr;
    void* found{};
    auto section = IMAGE_FIRST_SECTION(nt);
    const size_t required = kSignedStoreOffset + sizeof(kSignedStore);
    for (WORD i = 0; i < nt->FileHeader.NumberOfSections; ++i, ++section) {
        if (!(section->Characteristics & IMAGE_SCN_MEM_EXECUTE)) continue;
        size_t size = section->Misc.VirtualSize;
        if (section->VirtualAddress >= nt->OptionalHeader.SizeOfImage ||
            size > nt->OptionalHeader.SizeOfImage - section->VirtualAddress || size < required) continue;
        unsigned char* start = base + section->VirtualAddress;
        for (size_t offset = 0; offset <= size - required; ++offset) {
            if (start[offset] != kConstructor[0] || !MatchesConstructor(start + offset)) continue;
            if (found) return nullptr; // Ambiguous code is never hooked.
            found = start + offset;
        }
    }
    return found;
}

bool Initialize() {
    std::wstring host_path = ModuleFilename(nullptr);
    g_binary_dir = Directory(host_path);
    size_t slash = host_path.find_last_of(L"\\/");
    const wchar_t* basename = host_path.c_str() + (slash == std::wstring::npos ? 0 : slash + 1);
    if (_wcsicmp(basename, L"AceCombat8.exe") != 0) return false;
    wchar_t offline[8]{};
    if (GetEnvironmentVariableW(L"EOS_USE_ANTICHEATCLIENTNULL", offline, 8) != 1 || offline[0] != L'1') {
        Log("Inactive: offline null-client environment is required.");
        return false;
    }
    std::wstring command(GetCommandLineW());
    for (wchar_t& ch : command) ch = static_cast<wchar_t>(towlower(ch));
    if (command.find(L"-signed") != std::wstring::npos) {
        Log("Inactive: signed-container launch requested.");
        return false;
    }
    g_expected_pak = FullPath(g_binary_dir + L"\\..\\..\\Content\\Paks\\" + kPakName);
    DWORD attributes = g_expected_pak.empty() ? INVALID_FILE_ATTRIBUTES :
        GetFileAttributesW(g_expected_pak.c_str());
    if (attributes == INVALID_FILE_ATTRIBUTES || (attributes & FILE_ATTRIBUTE_DIRECTORY)) {
        Log("Inactive: localization PAK is missing.");
        return false;
    }
    void* target = FindConstructor(GetModuleHandleW(nullptr));
    if (!target) {
        Log("Inactive: PAK constructor signature is absent or ambiguous; game memory was not changed.");
        return false;
    }
    MH_STATUS status = MH_Initialize();
    if (status != MH_OK) {
        Log("Inactive: hook library initialization failed.");
        return false;
    }
    status = MH_CreateHook(target, reinterpret_cast<void*>(&LocalizedPakConstructor),
                           reinterpret_cast<void**>(&g_constructor));
    if (status == MH_OK) status = MH_EnableHook(target);
    if (status != MH_OK) {
        MH_RemoveHook(target);
        MH_Uninitialize();
        Log("Inactive: constructor hook could not be installed.");
        return false;
    }
    Log("Ready: offline localization loader; only pakchunk0-Windows_998_P.pak is eligible.");
    return true;
}

bool InstallEntryGate() {
    // DllMain may hold the loader lock. Do not allocate, load DLLs, wait, or call MinHook here.
    wchar_t host_path[32768]{};
    DWORD length = GetModuleFileNameW(nullptr, host_path, 32768);
    if (!length || length >= 32768) return false;
    const wchar_t* basename = host_path;
    for (DWORD i = 0; i < length; ++i)
        if (host_path[i] == L'/' || host_path[i] == L'\\') basename = host_path + i + 1;
    if (_wcsicmp(basename, L"AceCombat8.exe") != 0) return false;
    wchar_t offline[8]{};
    if (GetEnvironmentVariableW(L"EOS_USE_ANTICHEATCLIENTNULL", offline, 8) != 1 || offline[0] != L'1')
        return false;
    // The DLL remains a transparent system proxy when our own PAK is absent.
    // This guard precedes even the temporary PE-entry change.
    wchar_t pak_path[32768]{};
    size_t directory_length = static_cast<size_t>(basename - host_path);
    constexpr wchar_t relative_pak[] = L"..\\..\\Content\\Paks\\pakchunk0-Windows_998_P.pak";
    constexpr size_t relative_length = sizeof(relative_pak) / sizeof(wchar_t);
    if (directory_length + relative_length > 32768) return false;
    std::memcpy(pak_path, host_path, directory_length * sizeof(wchar_t));
    std::memcpy(pak_path + directory_length, relative_pak, sizeof(relative_pak));
    DWORD pak_attributes = GetFileAttributesW(pak_path);
    if (pak_attributes == INVALID_FILE_ATTRIBUTES || (pak_attributes & FILE_ATTRIBUTE_DIRECTORY))
        return false;
    g_activation_requested = true;
    auto base = reinterpret_cast<unsigned char*>(GetModuleHandleW(nullptr));
    auto dos = reinterpret_cast<const IMAGE_DOS_HEADER*>(base);
    if (!base || dos->e_magic != IMAGE_DOS_SIGNATURE || dos->e_lfanew < 0 || dos->e_lfanew > 0x100000)
        return false;
    auto nt = reinterpret_cast<const IMAGE_NT_HEADERS64*>(base + dos->e_lfanew);
    if (nt->Signature != IMAGE_NT_SIGNATURE || nt->FileHeader.Machine != IMAGE_FILE_MACHINE_AMD64 ||
        nt->OptionalHeader.Magic != IMAGE_NT_OPTIONAL_HDR64_MAGIC) return false;
    DWORD entry_rva = nt->OptionalHeader.AddressOfEntryPoint;
    if (entry_rva >= nt->OptionalHeader.SizeOfImage ||
        sizeof(g_entry_original) > nt->OptionalHeader.SizeOfImage - entry_rva) return false;
    // Two complete local instructions from the protected game entry point. An existing detour fails closed.
    constexpr unsigned char expected[] = {
        0x48,0x8d,0xa4,0x24,0x68,0xff,0xff,0xff,0x48,0x89,0x84,0x24,0x20,0x00,0x00,0x00
    };
    unsigned char* entry = base + entry_rva;
    if (std::memcmp(entry, expected, sizeof(expected)) != 0) return false;
    std::memcpy(g_entry_original, entry, sizeof(g_entry_original));
    // FF 25 [RIP+0] / address: an absolute jump which does not clobber a register.
    g_entry_jump[0] = 0xff;
    g_entry_jump[1] = 0x25;
    auto gate = reinterpret_cast<std::uintptr_t>(&AC8EntryGate);
    std::memcpy(g_entry_jump + 6, &gate, sizeof(gate));
    g_entry_jump[14] = g_entry_jump[15] = 0x90;
    DWORD old{};
    if (!VirtualProtect(entry, sizeof(g_entry_jump), PAGE_EXECUTE_READWRITE, &old)) return false;
    AC8OriginalEntry = entry;
    g_entry_protection = old;
    std::memcpy(entry, g_entry_jump, sizeof(g_entry_jump));
    FlushInstructionCache(GetCurrentProcess(), entry, sizeof(g_entry_jump));
    DWORD ignored{};
    VirtualProtect(entry, sizeof(g_entry_jump), old, &ignored);
    g_entry_armed = true;
    return true;
}

bool RestoreEntry() {
    if (!g_entry_armed || !AC8OriginalEntry ||
        std::memcmp(AC8OriginalEntry, g_entry_jump, sizeof(g_entry_jump)) != 0) return false;
    DWORD old{};
    if (!VirtualProtect(AC8OriginalEntry, sizeof(g_entry_original), PAGE_EXECUTE_READWRITE, &old))
        return false;
    std::memcpy(AC8OriginalEntry, g_entry_original, sizeof(g_entry_original));
    FlushInstructionCache(GetCurrentProcess(), AC8OriginalEntry, sizeof(g_entry_original));
    DWORD ignored{};
    bool restored = VirtualProtect(AC8OriginalEntry, sizeof(g_entry_original), g_entry_protection, &ignored) != FALSE;
    g_entry_armed = false;
    return restored;
}

BOOL CALLBACK LoadSystemDinput(PINIT_ONCE, PVOID, PVOID*) {
    wchar_t directory[MAX_PATH]{};
    UINT length = GetSystemDirectoryW(directory, MAX_PATH);
    if (!length || length >= MAX_PATH) return FALSE;
    std::wstring path(directory);
    path += L"\\dinput8.dll";
    g_system_dinput = LoadLibraryW(path.c_str());
    return g_system_dinput != nullptr;
}

FARPROC RealExport(const char* name) {
    if (!InitOnceExecuteOnce(&g_proxy_once, LoadSystemDinput, nullptr, nullptr)) return nullptr;
    return GetProcAddress(g_system_dinput, name);
}
}

// The assembly gate calls this only after Windows has completed DLL initialization.
// It restores the original entry before touching MinHook, then returns for a register-preserving JMP.
extern "C" void AC8RestoreAndInitialize() noexcept {
    if (!RestoreEntry()) {
        MessageBoxW(nullptr, L"中文补丁无法安全恢复游戏入口，已停止启动。", L"AC8中文翻译", MB_OK | MB_ICONERROR);
        ExitProcess(ERROR_DLL_INIT_FAILED);
    }
    try {
        if (Initialize()) return;
    } catch (...) {
        // Do not unwind through the raw PE entry contract.
    }
    // Our unsigned PAK must never reach normal mounting after its loader failed.
    MessageBoxW(nullptr, L"中文资源补丁无法加载，游戏尚未启动。请用翻译工具重新导入或撤回修改。",
                L"AC8翻译工具", MB_OK | MB_ICONERROR);
    ExitProcess(ERROR_DLL_INIT_FAILED);
}

extern "C" HRESULT WINAPI DirectInput8Create(HINSTANCE instance, DWORD version, REFIID iid,
                                             LPVOID* output, LPUNKNOWN outer) {
    using Fn = HRESULT (WINAPI *)(HINSTANCE, DWORD, REFIID, LPVOID*, LPUNKNOWN);
    auto fn = reinterpret_cast<Fn>(RealExport("DirectInput8Create"));
    return fn ? fn(instance, version, iid, output, outer) : E_FAIL;
}
extern "C" HRESULT WINAPI DllCanUnloadNow() {
    using Fn = HRESULT (WINAPI *)();
    auto fn = reinterpret_cast<Fn>(RealExport("DllCanUnloadNow"));
    return fn ? fn() : S_FALSE;
}
extern "C" HRESULT WINAPI DllGetClassObject(REFCLSID clsid, REFIID iid, LPVOID* output) {
    using Fn = HRESULT (WINAPI *)(REFCLSID, REFIID, LPVOID*);
    auto fn = reinterpret_cast<Fn>(RealExport("DllGetClassObject"));
    return fn ? fn(clsid, iid, output) : E_FAIL;
}
extern "C" HRESULT WINAPI DllRegisterServer() {
    using Fn = HRESULT (WINAPI *)();
    auto fn = reinterpret_cast<Fn>(RealExport("DllRegisterServer"));
    return fn ? fn() : E_FAIL;
}
extern "C" HRESULT WINAPI DllUnregisterServer() {
    using Fn = HRESULT (WINAPI *)();
    auto fn = reinterpret_cast<Fn>(RealExport("DllUnregisterServer"));
    return fn ? fn() : E_FAIL;
}

BOOL WINAPI DllMain(HINSTANCE instance, DWORD reason, LPVOID) {
    if (reason == DLL_PROCESS_ATTACH) {
        // Initialization is synchronized with the original PE entry, never a racing worker thread.
        if (!InstallEntryGate() && g_activation_requested) return FALSE;
    }
    return TRUE;
}
