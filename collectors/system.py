"""Host system telemetry via ctypes, with no third-party dependency.

Wraps just enough of the Win32 API to cover what a local inference dashboard
needs: CPU load, memory, per-core clock, fixed-disk capacity and the host
memory held by llama-server processes.

CPU load comes from GetSystemTimes deltas.  On Windows the kernel time
already includes idle time, so busy is (kernel + user - idle).
"""

import ctypes
import os
import sys
from ctypes import wintypes

IS_WINDOWS = sys.platform.startswith("win")

TH32CS_SNAPPROCESS = 0x00000002
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
DRIVE_FIXED = 3
PROCESSOR_INFORMATION = 11


class FILETIME(ctypes.Structure):
    _fields_ = [
        ("dwLowDateTime", wintypes.DWORD),
        ("dwHighDateTime", wintypes.DWORD),
    ]


class MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [
        ("dwLength", wintypes.DWORD),
        ("dwMemoryLoad", wintypes.DWORD),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


class PROCESSOR_POWER_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("Number", wintypes.ULONG),
        ("MaxMhz", wintypes.ULONG),
        ("CurrentMhz", wintypes.ULONG),
        ("MhzLimit", wintypes.ULONG),
        ("MaxIdleState", wintypes.ULONG),
        ("CurrentIdleState", wintypes.ULONG),
    ]


class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("PageFaultCount", wintypes.DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
    ]


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", ctypes.c_wchar * 260),
    ]


MIB = 1024 * 1024


class PERFORMANCE_INFORMATION(ctypes.Structure):
    """Backs GetPerformanceInfo; every SIZE_T field is measured in pages."""

    _fields_ = [
        ("cb", wintypes.DWORD),
        ("CommitTotal", ctypes.c_size_t),
        ("CommitLimit", ctypes.c_size_t),
        ("CommitPeak", ctypes.c_size_t),
        ("PhysicalTotal", ctypes.c_size_t),
        ("PhysicalAvailable", ctypes.c_size_t),
        ("SystemCache", ctypes.c_size_t),
        ("KernelTotal", ctypes.c_size_t),
        ("KernelPaged", ctypes.c_size_t),
        ("KernelNonpaged", ctypes.c_size_t),
        ("PageSize", ctypes.c_size_t),
        ("HandleCount", wintypes.DWORD),
        ("ProcessCount", wintypes.DWORD),
        ("ThreadCount", wintypes.DWORD),
    ]


if IS_WINDOWS:
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _psapi = ctypes.WinDLL("psapi", use_last_error=True)
    try:
        _powrprof = ctypes.WinDLL("powrprof", use_last_error=True)
    except OSError:
        _powrprof = None

    # Handles are pointer sized.  Without an explicit restype ctypes truncates
    # them to a C int and the value becomes unusable.
    _kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    _kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.Process32FirstW.argtypes = [
        wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    _kernel32.Process32NextW.argtypes = [
        wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    _kernel32.GetLogicalDrives.restype = wintypes.DWORD
    _kernel32.GetDriveTypeW.restype = wintypes.UINT
    _kernel32.GetDriveTypeW.argtypes = [wintypes.LPCWSTR]
    _kernel32.GetDiskFreeSpaceExW.argtypes = [
        wintypes.LPCWSTR,
        ctypes.POINTER(ctypes.c_ulonglong),
        ctypes.POINTER(ctypes.c_ulonglong),
        ctypes.POINTER(ctypes.c_ulonglong),
    ]
    _kernel32.GetSystemTimes.argtypes = [
        ctypes.POINTER(FILETIME), ctypes.POINTER(FILETIME),
        ctypes.POINTER(FILETIME)]
    _kernel32.GlobalMemoryStatusEx.argtypes = [ctypes.POINTER(MEMORYSTATUSEX)]
    _psapi.GetProcessMemoryInfo.argtypes = [
        wintypes.HANDLE, ctypes.POINTER(PROCESS_MEMORY_COUNTERS), wintypes.DWORD]
    _psapi.GetPerformanceInfo.argtypes = [
        ctypes.POINTER(PERFORMANCE_INFORMATION), wintypes.DWORD]
    if _powrprof is not None:
        _powrprof.CallNtPowerInformation.restype = ctypes.c_long
else:  # pragma: no cover - the dashboard targets Windows
    _kernel32 = _psapi = _powrprof = None

# Process names worth attributing host memory to.  Matched case-insensitively
# as a substring so wrapper names such as llama-server-cuda.exe still land.
WATCHED_PROCESSES = ("llama-server", "llama-server.exe")


def _filetime_to_int(value):
    return (value.dwHighDateTime << 32) | value.dwLowDateTime


def _cpu_times():
    idle = FILETIME()
    kernel = FILETIME()
    user = FILETIME()
    if not _kernel32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kernel),
                                    ctypes.byref(user)):
        return None
    idle_t = _filetime_to_int(idle)
    kernel_t = _filetime_to_int(kernel)
    user_t = _filetime_to_int(user)
    # Kernel time includes idle time; total is kernel + user.
    return idle_t, kernel_t + user_t


def _memory():
    status = MEMORYSTATUSEX()
    status.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
    if not _kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
        return None
    total_mb = status.ullTotalPhys / MIB
    avail_mb = status.ullAvailPhys / MIB
    memory = {
        "total_mb": total_mb,
        "avail_mb": avail_mb,
        "used_mb": total_mb - avail_mb,
        "used_ratio": (total_mb - avail_mb) / total_mb if total_mb else None,
        "load_pct": float(status.dwMemoryLoad),
    }

    # GetPerformanceInfo is the honest source for commit charge and the file
    # cache.  MEMORYSTATUSEX's page-file fields blend physical and paging
    # memory, so any "swap used" derived from them comes out larger than the
    # page file itself.
    perf = PERFORMANCE_INFORMATION()
    perf.cb = ctypes.sizeof(PERFORMANCE_INFORMATION)
    if _psapi.GetPerformanceInfo(ctypes.byref(perf), perf.cb):
        to_mb = (perf.PageSize or 4096) / MIB
        commit_used = perf.CommitTotal * to_mb
        commit_limit = perf.CommitLimit * to_mb
        memory["commit_used_mb"] = commit_used
        memory["commit_limit_mb"] = commit_limit
        memory["commit_ratio"] = commit_used / commit_limit if commit_limit else None
        memory["cache_mb"] = perf.SystemCache * to_mb
        memory["handle_count"] = perf.HandleCount
        memory["process_count"] = perf.ProcessCount
        memory["thread_count"] = perf.ThreadCount
    return memory


def _cpu_clock():
    if _powrprof is None:
        return None
    count = os.cpu_count() or 1
    size = ctypes.sizeof(PROCESSOR_POWER_INFORMATION) * count
    buffer = ctypes.create_string_buffer(size)
    status = _powrprof.CallNtPowerInformation(
        PROCESSOR_INFORMATION, None, 0, buffer, size)
    if status != 0:
        return None
    array = ctypes.cast(
        buffer, ctypes.POINTER(PROCESSOR_POWER_INFORMATION))
    currents = [array[i].CurrentMhz for i in range(count)]
    maxes = [array[i].MaxMhz for i in range(count)]
    currents = [c for c in currents if c]
    maxes = [m for m in maxes if m]
    if not currents:
        return None
    return {
        "cores": count,
        "current_mhz": sum(currents) / len(currents),
        "max_mhz": max(maxes) if maxes else None,
        "per_core_mhz": currents,
    }


def _disks():
    if not IS_WINDOWS:
        return []
    mask = _kernel32.GetLogicalDrives()
    disks = []
    for index in range(26):
        if not (mask >> index) & 1:
            continue
        root = "%s:\\" % chr(ord("A") + index)
        if _kernel32.GetDriveTypeW(root) != DRIVE_FIXED:
            continue
        free_to_caller = ctypes.c_ulonglong(0)
        total = ctypes.c_ulonglong(0)
        free_total = ctypes.c_ulonglong(0)
        ok = _kernel32.GetDiskFreeSpaceExW(
            root,
            ctypes.byref(free_to_caller),
            ctypes.byref(total),
            ctypes.byref(free_total),
        )
        if not ok:
            continue
        total_mb = total.value / (1024 * 1024)
        free_mb = free_total.value / (1024 * 1024)
        disks.append({
            "root": root,
            "total_mb": total_mb,
            "free_mb": free_mb,
            "used_mb": total_mb - free_mb,
            "used_ratio": (total_mb - free_mb) / total_mb if total_mb else None,
        })
    return disks


def _process_memory():
    """Host working set of every watched process, via a toolhelp snapshot."""
    if not IS_WINDOWS:
        return []
    snapshot = _kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snapshot == INVALID_HANDLE_VALUE:
        return []
    entry = PROCESSENTRY32W()
    entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
    found = []
    try:
        if not _kernel32.Process32FirstW(snapshot, ctypes.byref(entry)):
            return []
        while True:
            name = (entry.szExeFile or "").lower()
            if any(watched in name for watched in WATCHED_PROCESSES):
                counters = PROCESS_MEMORY_COUNTERS()
                counters.cb = ctypes.sizeof(PROCESS_MEMORY_COUNTERS)
                handle = _kernel32.OpenProcess(
                    PROCESS_QUERY_LIMITED_INFORMATION, False, entry.th32ProcessID)
                if handle:
                    try:
                        if _psapi.GetProcessMemoryInfo(
                                handle, ctypes.byref(counters), counters.cb):
                            found.append({
                                "pid": entry.th32ProcessID,
                                "name": entry.szExeFile,
                                "threads": entry.cntThreads,
                                "working_set_mb": counters.WorkingSetSize / (1024 * 1024),
                                "peak_working_set_mb": counters.PeakWorkingSetSize / (1024 * 1024),
                                "pagefile_mb": counters.PagefileUsage / (1024 * 1024),
                            })
                    finally:
                        _kernel32.CloseHandle(handle)
            if not _kernel32.Process32NextW(snapshot, ctypes.byref(entry)):
                break
    finally:
        _kernel32.CloseHandle(snapshot)
    # Largest first: the router parent is tiny while the child holding the
    # model is huge, and callers want the one that owns the weights.
    found.sort(key=lambda item: item["working_set_mb"], reverse=True)
    return found


class SystemCollector(object):
    """Stateful because CPU load is a derivative of two samples."""

    def __init__(self):
        self._prev = None

    def collect(self):
        payload = {"available": IS_WINDOWS, "error": None}
        if not IS_WINDOWS:
            payload["error"] = "system collector supports Windows only"
            return payload

        current = _cpu_times()
        cpu = {"cores": os.cpu_count()}
        if current and self._prev:
            idle_delta = current[0] - self._prev[0]
            total_delta = current[1] - self._prev[1]
            if total_delta > 0:
                busy = total_delta - idle_delta
                cpu["load_pct"] = max(0.0, min(100.0, busy / total_delta * 100.0))
        self._prev = current
        if "load_pct" not in cpu:
            cpu["load_pct"] = None

        clock = _cpu_clock()
        if clock:
            cpu.update(clock)
        payload["cpu"] = cpu
        payload["memory"] = _memory()
        payload["disks"] = _disks()
        payload["processes"] = _process_memory()
        return payload
