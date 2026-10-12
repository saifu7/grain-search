"""Windows-only helpers so background indexing never gets in the way.
On other systems every check returns False / does nothing."""

from __future__ import annotations

import sys

_WIN = sys.platform == "win32"

# SHQueryUserNotificationState results that mean "don't disturb"
QUNS_BUSY = 2  # a full-screen app is running
QUNS_RUNNING_D3D_FULL_SCREEN = 3  # a full-screen Direct3D game
QUNS_PRESENTATION_MODE = 4


def fullscreen_app_running() -> bool:
    if not _WIN:
        return False
    try:
        import ctypes

        state = ctypes.c_int(0)
        if ctypes.windll.shell32.SHQueryUserNotificationState(ctypes.byref(state)) != 0:
            return False
        return state.value in (QUNS_BUSY, QUNS_RUNNING_D3D_FULL_SCREEN, QUNS_PRESENTATION_MODE)
    except Exception:
        return False


def on_battery() -> bool:
    if not _WIN:
        return False
    try:
        import ctypes
        from ctypes import wintypes

        class SYSTEM_POWER_STATUS(ctypes.Structure):
            _fields_ = [
                ("ACLineStatus", wintypes.BYTE),
                ("BatteryFlag", wintypes.BYTE),
                ("BatteryLifePercent", wintypes.BYTE),
                ("SystemStatusFlag", wintypes.BYTE),
                ("BatteryLifeTime", wintypes.DWORD),
                ("BatteryFullLifeTime", wintypes.DWORD),
            ]

        s = SYSTEM_POWER_STATUS()
        if not ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(s)):
            return False
        return s.ACLineStatus == 0  # 0 = on battery, 1 = plugged in, 255 = unknown
    except Exception:
        return False


def lower_current_thread_priority() -> None:
    """Below-normal priority for the indexing thread, so the PC stays snappy."""
    if not _WIN:
        return
    try:
        import ctypes

        THREAD_PRIORITY_BELOW_NORMAL = -1
        k = ctypes.windll.kernel32
        k.SetThreadPriority(k.GetCurrentThread(), THREAD_PRIORITY_BELOW_NORMAL)
    except Exception:
        pass
