"""单实例互斥：抢锁、唤回已有窗口、已在运行的提示。"""

from __future__ import annotations

from core import applog, i18n
import os
import sys
import tempfile

_SINGLE_INSTANCE_MUTEX = "Local\\WhoShitsOnMyC_SingleInstance"
_single_instance_file = None


def _acquire_single_instance() -> bool:
    """登记本进程为唯一实例；已有实例在运行返回 False。"""
    global _single_instance_handle, _single_instance_file
    if sys.platform == "darwin" or (os.name != "nt"):
        try:
            import fcntl
            lock_dir = os.path.join(tempfile.gettempdir(), "WhoShitsOnMyC")
            os.makedirs(lock_dir, exist_ok=True)
            lock_path = os.path.join(lock_dir, "app.lock")
            f = open(lock_path, "a+")
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            _single_instance_file = f
            return True
        except (IOError, OSError):
            return False
        except Exception:
            return True

    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        kernel32.CreateMutexW.argtypes = [
            ctypes.c_void_p, wintypes.BOOL, ctypes.c_wchar_p
        ]
        handle = kernel32.CreateMutexW(None, True, _SINGLE_INSTANCE_MUTEX)
        if not handle:
            return True
        if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
            kernel32.CloseHandle(ctypes.c_void_p(handle))
            return False
        _single_instance_handle = handle
        return True
    except Exception:  # noqa: BLE001
        return True


def _activate_existing_window() -> bool:
    """把已运行实例的主窗口还原并带到前台；找不到返回 False。"""
    if sys.platform == "darwin":
        try:
            import subprocess
            cmd = """osascript -e 'tell application "System Events" to set frontmost of first process whose name contains "WhoShitsOnMyC" to true'"""
            subprocess.run(cmd, shell=True, timeout=2, check=False)
            return True
        except Exception:
            return False
    if os.name != "nt":
        return False
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        found: list[int] = []

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def on_window(hwnd, _lparam):
            length = user32.GetWindowTextLengthW(hwnd)
            if length > 0 and user32.IsWindowVisible(hwnd):
                buf = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buf, length + 1)
                title = buf.value
                if title == "WhoShitsOnMyC" or title.startswith("WhoShitsOnMyC "):
                    found.append(hwnd)
                    return False
            return True

        if not user32.EnumWindows(on_window, 0) or not found:
            return False
        hwnd = found[0]
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        user32.SetForegroundWindow(hwnd)
        return True
    except Exception:  # noqa: BLE001 - 激活失败退回弹提示，不拦退出
        return False


_single_instance_handle = None


def _warn_already_running() -> None:
    """已有实例时弹原生提示：本次启动没有继续，且不改变已有窗口的权限。

    找到旧窗口能唤回也不能静默退出：用户以管理员方式重新启动时，看到的旧窗口
    仍是非管理员权限，没有提示会让人以为权限已经跟着改变。
    """
    msg = i18n.t(
        (
            "WhoShitsOnMyC 已经在运行，本次启动没有继续。\n\n"
            "已有窗口的权限不会因为本次启动而改变。如果需要在管理员权限下使用，"
            "请先关闭已有窗口，再重新启动程序。"
        ),
        (
            "WhoShitsOnMyC is already running, so this launch did not continue.\n\n"
            "This launch does not change the permissions of the existing window. "
            "If you need administrator rights, close the existing window first and "
            "then start the app again."
        ),
    )
    title = "WhoShitsOnMyC"
    # 如果系统有 ctypes.windll.user32（或测试打桩），优先调用供验证
    try:
        import ctypes
        if hasattr(ctypes, "windll") and hasattr(ctypes.windll, "user32"):
            ctypes.windll.user32.MessageBoxW(0, msg, title, 0x30)
            return
    except Exception:
        pass

    if sys.platform == "darwin":
        try:
            import subprocess
            escaped_msg = msg.replace('"', '\\"')
            escaped_title = title.replace('"', '\\"')
            cmd = f'''osascript -e 'display alert "{escaped_title}" message "{escaped_msg}" as warning' '''
            subprocess.run(cmd, shell=True, timeout=5, check=False)
            return
        except Exception:
            pass
    try:
        import ctypes

        # MB_OK | MB_ICONWARNING
        ctypes.windll.user32.MessageBoxW(0, msg, title, 0x30)
    except Exception:  # noqa: BLE001
        applog.warn(f"already running (MessageBox failed): {msg}")
