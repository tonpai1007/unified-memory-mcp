"""RSS probe: shared by watchdog and the server itself.

The watchdog's tracked subprocess is the venv-python wrapper (very small),
while the real worker is a python child several layers deep in OpenCode's
spawn chain — so only an in-process probe gets the real number.

Windows-only via psapi/kernel32. Returns None on any failure; callers fall
back to service-alive signals instead of aborting.
"""

from __future__ import annotations

import os


def child_rss_mb(pid: int) -> int | None:
    """Return the process's resident set size in MB, or None."""
    if os.name != "nt" or not pid:
        return None
    try:
        import ctypes
        from ctypes import POINTER, Structure, byref, wintypes

        SIZE_T = ctypes.c_size_t

        class PROCESS_MEMORY_COUNTERS(Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", SIZE_T),
                ("WorkingSetSize", SIZE_T),
                ("QuotaPeakPagedPoolUsage", SIZE_T),
                ("QuotaPagedPoolUsage", SIZE_T),
                ("QuotaPeakNonPagedPoolUsage", SIZE_T),
                ("QuotaNonPagedPoolUsage", SIZE_T),
                ("PagefileUsage", SIZE_T),
                ("PeakPagefileUsage", SIZE_T),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        psapi = ctypes.WinDLL("psapi", use_last_error=True)

        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, POINTER(PROCESS_MEMORY_COUNTERS), wintypes.DWORD]
        psapi.GetProcessMemoryInfo.restype = wintypes.BOOL

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not handle:
            return None
        try:
            counters = PROCESS_MEMORY_COUNTERS()
            counters.cb = ctypes.sizeof(PROCESS_MEMORY_COUNTERS)
            if not psapi.GetProcessMemoryInfo(handle, byref(counters), counters.cb):
                return None
            return int(counters.WorkingSetSize) // (1024 * 1024)
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        return None


def self_rss_mb() -> int | None:
    """RSS of the calling process (no PID needed)."""
    return child_rss_mb(os.getpid())
