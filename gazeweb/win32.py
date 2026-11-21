"""Windows 螢幕幾何：DPI 感知、顯示器列表、游標位置、瀏覽器內容區的實體像素矩形。

Ghost 也是用 SetProcessDpiAwareness + EnumDisplayMonitors + GetMonitorInfo
（見 Tobii.Gaming.Tracking.Net.dll 的 P/Invoke）把正規化視線換成螢幕像素。
"""
from __future__ import annotations

import ctypes as C
import ctypes.wintypes as W
from dataclasses import dataclass
from typing import Optional

user32 = C.WinDLL("user32", use_last_error=True)

try:  # 每顯示器 DPI 感知：之後取得的座標全是實體像素
    C.WinDLL("shcore").SetProcessDpiAwareness(2)
except Exception:  # noqa: BLE001
    try:
        user32.SetProcessDPIAware()
    except Exception:  # noqa: BLE001
        pass


@dataclass
class Monitor:
    index: int
    left: int
    top: int
    width: int
    height: int
    primary: bool
    name: str

    def as_dict(self):
        return self.__dict__.copy()


class MONITORINFOEXW(C.Structure):
    _fields_ = [("cbSize", W.DWORD), ("rcMonitor", W.RECT), ("rcWork", W.RECT),
                ("dwFlags", W.DWORD), ("szDevice", W.WCHAR * 32)]


MONITORENUMPROC = C.WINFUNCTYPE(W.BOOL, W.HMONITOR, W.HDC, C.POINTER(W.RECT), W.LPARAM)


def monitors() -> list[Monitor]:
    out: list[Monitor] = []

    def cb(hmon, _hdc, _rc, _lp):
        mi = MONITORINFOEXW()
        mi.cbSize = C.sizeof(MONITORINFOEXW)
        user32.GetMonitorInfoW(hmon, C.byref(mi))
        r = mi.rcMonitor
        out.append(Monitor(len(out), r.left, r.top, r.right - r.left, r.bottom - r.top,
                           bool(mi.dwFlags & 1), mi.szDevice))
        return True

    user32.EnumDisplayMonitors(None, None, MONITORENUMPROC(cb), 0)
    return out


def pick_monitor(index: Optional[int]) -> Monitor:
    ms = monitors()
    if index is None:
        return next((m for m in ms if m.primary), ms[0])
    return ms[index]


def cursor_pos() -> tuple[int, int]:
    p = W.POINT()
    user32.GetCursorPos(C.byref(p))
    return p.x, p.y


user32.GetForegroundWindow.restype = W.HWND
user32.GetWindowRect.argtypes = [W.HWND, C.POINTER(W.RECT)]
user32.GetClassNameW.argtypes = [W.HWND, W.LPWSTR, C.c_int]
user32.IsWindowVisible.argtypes = [W.HWND]
user32.GetWindowThreadProcessId.argtypes = [W.HWND, C.POINTER(W.DWORD)]
user32.GetAncestor.argtypes = [W.HWND, W.UINT]
user32.GetAncestor.restype = W.HWND
WNDENUMPROC = C.WINFUNCTYPE(W.BOOL, W.HWND, W.LPARAM)


def _class(h) -> str:
    buf = C.create_unicode_buffer(256)
    user32.GetClassNameW(h, buf, 256)
    return buf.value


def window_pid(h) -> int:
    pid = W.DWORD()
    user32.GetWindowThreadProcessId(h, C.byref(pid))
    return pid.value


user32.WindowFromPoint.argtypes = [W.POINT]
user32.WindowFromPoint.restype = W.HWND
user32.IsIconic.argtypes = [W.HWND]
user32.GetWindowTextW.argtypes = [W.HWND, W.LPWSTR, C.c_int]


def foreground_viewport_rect() -> Optional[tuple[int, int, int, int, int]]:
    """若前景視窗是 Chromium 系瀏覽器，回傳其網頁內容區（Chrome_RenderWidgetHostHWND）
    的實體像素矩形 (left, top, width, height, top_level_hwnd)。"""
    fg = user32.GetForegroundWindow()
    if not fg or _class(fg) != "Chrome_WidgetWin_1":
        return None
    return viewport_rect_of(fg)


def root_at_point(x: float, y: float) -> int:
    """螢幕上這個點最上層視窗所屬的頂層視窗（用來判斷視線是否落在瀏覽器上、沒被別的視窗擋住）。"""
    h = user32.WindowFromPoint(W.POINT(int(x), int(y)))
    return int(user32.GetAncestor(h, 2) or 0) if h else 0  # GA_ROOT


def window_usable(hwnd: int) -> bool:
    return bool(hwnd) and bool(user32.IsWindowVisible(hwnd)) and not user32.IsIconic(hwnd)


def find_browser_window(title: str) -> Optional[int]:
    """瀏覽器還沒取得過前景時，用分頁標題找它的頂層視窗（Chrome/Edge 視窗標題以分頁標題開頭）。"""
    if not title:
        return None
    hits: list[int] = []

    def cb(h, _lp):
        if user32.IsWindowVisible(h) and _class(h) == "Chrome_WidgetWin_1":
            buf = C.create_unicode_buffer(512)
            user32.GetWindowTextW(h, buf, 512)
            if buf.value.startswith(title):
                hits.append(int(h))
        return True

    user32.EnumWindows(WNDENUMPROC(cb), 0)
    return hits[0] if len(hits) == 1 else None


def viewport_rect_of(top) -> Optional[tuple[int, int, int, int, int]]:
    found: list[tuple[int, int, int, int]] = []
    fg = top

    def cb(h, _lp):
        if _class(h) == "Chrome_RenderWidgetHostHWND" and user32.IsWindowVisible(h):
            r = W.RECT()
            user32.GetWindowRect(h, C.byref(r))
            w, hh = r.right - r.left, r.bottom - r.top
            if w > 0 and hh > 0:
                found.append((r.left, r.top, w, hh))
        return True

    user32.EnumChildWindows(fg, WNDENUMPROC(cb), 0)
    if not found:
        return None
    # DevTools 停靠時會有多個；取面積最大的當作網頁本體
    l, t, w, h = max(found, key=lambda r: r[2] * r[3])
    return l, t, w, h, int(fg)
