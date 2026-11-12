# -*- coding: utf-8 -*-
"""GWENT 游戏窗口查找与定位（悬浮窗/主窗口贴靠用）。"""
import ctypes
import ctypes.wintypes
import os

try:
    import win32gui
except ImportError:  # pragma: no cover
    win32gui = None


def ensure_dpi_aware():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:  # noqa: BLE001
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:  # noqa: BLE001
            pass


def _process_name(hwnd):
    """返回窗口所属进程的 exe 文件名（如 Gwent.exe）。"""
    pid = ctypes.wintypes.DWORD()
    ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if not pid.value:
        return ""
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    h = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(512)
        size = ctypes.wintypes.DWORD(512)
        if not ctypes.windll.kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return ""
        return os.path.basename(buf.value)
    finally:
        ctypes.windll.kernel32.CloseHandle(h)


def find_gwent_window():
    """按进程名 Gwent.exe 查找游戏窗口，返回 (hwnd, title)，未找到返回 (0, '')。"""
    if win32gui is None:
        return 0, ""
    result = []

    def cb(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd):
            return
        if _process_name(hwnd).lower() != "gwent.exe":
            return
        result.append((hwnd, win32gui.GetWindowText(hwnd)))

    win32gui.EnumWindows(cb, None)
    if not result:
        return 0, ""
    # 优先取有实际尺寸的窗口
    for hwnd, title in result:
        r = win32gui.GetWindowRect(hwnd)
        if r[2] - r[0] > 400 and r[3] - r[1] > 300:
            return hwnd, title
    return result[0]


def is_window_visible(hwnd):
    """窗口是否可见且未最小化。"""
    try:
        import win32gui as wg
        if not wg.IsWindowVisible(hwnd):
            return False
        return not wg.IsIconic(hwnd)
    except Exception:  # noqa: BLE001
        return False


def get_client_rect_screen(hwnd):
    """窗口客户区在屏幕上的坐标 (left, top, width, height)。"""
    l, t, r, b = win32gui.GetClientRect(hwnd)
    pt = ctypes.wintypes.POINT(0, 0)
    ctypes.windll.user32.ClientToScreen(hwnd, ctypes.byref(pt))
    return pt.x, pt.y, r - l, b - t
