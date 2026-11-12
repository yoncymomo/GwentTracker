# -*- coding: utf-8 -*-
"""Windows DWM 毛玻璃效果工具（Acrylic / Mica 降级链）。"""
import ctypes
import sys
import winreg

ACCENT_DISABLED = 0
ACCENT_ENABLE_ACRYLICBLURBEHIND = 4

DWMWA_USE_IMMERSIVE_DARK_MODE = 20
DWMWA_SYSTEMBACKDROP_TYPE = 38
DWMSBT_TRANSIENTWINDOW = 3  # 亚克力
DWMWA_WINDOW_CORNER_PREFERENCE = 33
DWMWCP_ROUND = 2            # 系统标准圆角（含阴影保留）
DWMWA_BORDER_COLOR = 34
DWMWA_CAPTION_COLOR = 35
DWMWA_TEXT_COLOR = 36


def _win_build():
    try:
        key = winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\Microsoft\Windows NT\CurrentVersion")
        build, _ = winreg.QueryValueEx(key, "CurrentBuildNumber")
        return int(build)
    except OSError:
        return 0


def _dwm_set(hwnd, attr, val):
    v = ctypes.c_uint(val)
    return ctypes.windll.dwmapi.DwmSetWindowAttribute(
        ctypes.c_void_p(hwnd), ctypes.c_uint(attr),
        ctypes.byref(v), ctypes.sizeof(v)) == 0


class _ACCENTPOLICY(ctypes.Structure):
    _fields_ = [("AccentState", ctypes.c_int),
                ("AccentFlags", ctypes.c_int),
                ("GradientColor", ctypes.c_uint),
                ("AnimationId", ctypes.c_int)]


class _WINCOMPATTRDATA(ctypes.Structure):
    _fields_ = [("Attribute", ctypes.c_int),
                ("Data", ctypes.c_void_p),
                ("SizeOfData", ctypes.c_size_t)]


def _accent_glass(hwnd, gradient=0xD01B1C1F):
    """Win10 亚克力：SetWindowCompositionAttribute。GradientColor=AABBGGRR。"""
    accent = _ACCENTPOLICY(ACCENT_ENABLE_ACRYLICBLURBEHIND, 2, gradient, 0)
    data = _WINCOMPATTRDATA(
        19, ctypes.cast(ctypes.pointer(accent), ctypes.c_void_p),
        ctypes.sizeof(accent))
    return bool(ctypes.windll.user32.SetWindowCompositionAttribute(
        ctypes.c_void_p(hwnd), ctypes.byref(data)))


def apply_round(hwnd):
    """Win11 22000+：显式把窗口四角设为圆角（保留系统边框与阴影）。

    tk 窗口在部分 Win11 机器上默认直角，显式设置 ROUND 强制圆角；
    Win10 无此属性，调用安全无害（返回 False）。
    """
    if sys.platform != "win32":
        return False
    try:
        return _dwm_set(hwnd, DWMWA_WINDOW_CORNER_PREFERENCE, DWMWCP_ROUND)
    except Exception:  # noqa: BLE001
        return False


def _colorref(hexstr):
    """'#RRGGBB' → COLORREF（0x00BBGGRR，与亚克力的 ABGR 不同）。"""
    h = hexstr.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return (b << 16) | (g << 8) | r


def apply_caption(hwnd, bg, fg=None, border=None):
    """Win11 22000+：标题栏着色为应用底色，消除系统默认深灰标题栏。

    未设置时系统在深色模式下给的是 #3a3a3a 一类的灰，与 HUD 底色脱节；
    显式着色后标题栏、边框与面板同色，窗口看起来是一整块。
    """
    if sys.platform != "win32" or _win_build() < 22000:
        return False
    try:
        ok = _dwm_set(hwnd, DWMWA_CAPTION_COLOR, _colorref(bg))
        if fg:
            _dwm_set(hwnd, DWMWA_TEXT_COLOR, _colorref(fg))
        if border:
            _dwm_set(hwnd, DWMWA_BORDER_COLOR, _colorref(border))
        return ok
    except Exception:  # noqa: BLE001
        return False


def apply_glass(hwnd, dark=True, tint=None):
    """给窗口启用毛玻璃背景 + 圆角。返回 'backdrop'/'accent'/''（失败）。

    tint: 亚克力底色 0xAABBGGRR，浅色主题可传 0xD0ECF5F8 之类。
    """
    if sys.platform != "win32":
        return ""
    build = _win_build()
    gradient = tint if tint else (0xD01B1C1F if dark else 0xD0ECF5F8)
    try:
        apply_round(hwnd)   # 圆角独立于毛玻璃，无论 blur 是否成功都尝试
        if build >= 22621:  # Win11 22H2+
            if _dwm_set(hwnd, DWMWA_SYSTEMBACKDROP_TYPE, DWMSBT_TRANSIENTWINDOW):
                # 只设系统背景材质；不要再叠加 AccentBlur，否则会整窗蒙一层乳白
                _dwm_set(hwnd, DWMWA_USE_IMMERSIVE_DARK_MODE, 1 if dark else 0)
                return "backdrop"
        if build >= 17134:  # Win10 1809+
            if _accent_glass(hwnd, gradient):
                _dwm_set(hwnd, DWMWA_USE_IMMERSIVE_DARK_MODE, 1 if dark else 0)
                return "accent"
    except Exception:  # noqa: BLE001
        pass
    return ""
