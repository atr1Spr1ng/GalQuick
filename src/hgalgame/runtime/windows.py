from __future__ import annotations

import ctypes
import os
import sys
import time
from ctypes import wintypes
from dataclasses import dataclass


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class _HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    ]


class _INPUT_UNION(ctypes.Union):
    _fields_ = [
        ("mi", _MOUSEINPUT),
        ("ki", _KEYBDINPUT),
        ("hi", _HARDWAREINPUT),
    ]


class _INPUT(ctypes.Structure):
    _anonymous_ = ("union",)
    _fields_ = [("type", wintypes.DWORD), ("union", _INPUT_UNION)]


class _MONITORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", wintypes.RECT),
        ("rcWork", wintypes.RECT),
        ("dwFlags", wintypes.DWORD),
    ]


@dataclass(frozen=True)
class WindowRect:
    left: int
    top: int
    right: int
    bottom: int

    @property
    def width(self) -> int:
        return max(0, self.right - self.left)

    @property
    def height(self) -> int:
        return max(0, self.bottom - self.top)


class WindowsWindowController:
    """Small Win32 surface shared by engine runtimes and the overlay host."""

    SW_HIDE = 0
    SW_SHOW = 5
    SW_SHOWNOACTIVATE = 4
    SW_RESTORE = 9
    WM_CLOSE = 0x0010
    BM_CLICK = 0x00F5
    WM_KEYDOWN = 0x0100
    WM_KEYUP = 0x0101
    GW_OWNER = 4
    GWLP_HWNDPARENT = -8
    GWL_STYLE = -16
    GWL_EXSTYLE = -20
    WS_CAPTION = 0x00C00000
    WS_THICKFRAME = 0x00040000
    WS_MINIMIZEBOX = 0x00020000
    WS_MAXIMIZEBOX = 0x00010000
    WS_SYSMENU = 0x00080000
    WS_EX_TOOLWINDOW = 0x00000080
    WS_EX_TRANSPARENT = 0x00000020
    WS_EX_TOPMOST = 0x00000008
    WS_EX_APPWINDOW = 0x00040000
    WS_EX_NOACTIVATE = 0x08000000
    SWP_NOSIZE = 0x0001
    SWP_NOMOVE = 0x0002
    SWP_NOACTIVATE = 0x0010
    SWP_FRAMECHANGED = 0x0020
    SWP_SHOWWINDOW = 0x0040
    VK_UP = 0x26
    VK_DOWN = 0x28
    VK_RETURN = 0x0D
    KEYEVENTF_EXTENDEDKEY = 0x0001
    KEYEVENTF_KEYUP = 0x0002
    KEYEVENTF_SCANCODE = 0x0008
    KEY_HOLD_SECONDS = 0.06
    KEY_RELEASE_SECONDS = 0.04
    INPUT_KEYBOARD = 1
    HWND_TOP = 0
    HWND_TOPMOST = -1
    GA_ROOT = 2
    MONITOR_DEFAULTTONEAREST = 2

    def __init__(self) -> None:
        if sys.platform != "win32":
            raise RuntimeError("the overlay and input controller require Windows")
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
        self._configure_signatures()

    def _configure_signatures(self) -> None:
        self._enum_callback = ctypes.WINFUNCTYPE(
            wintypes.BOOL, wintypes.HWND, wintypes.LPARAM
        )
        self.user32.EnumWindows.argtypes = [self._enum_callback, wintypes.LPARAM]
        self.user32.EnumWindows.restype = wintypes.BOOL
        self.user32.GetWindowThreadProcessId.argtypes = [
            wintypes.HWND,
            ctypes.POINTER(wintypes.DWORD),
        ]
        self.user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        self.user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
        self.user32.GetWindowTextLengthW.restype = ctypes.c_int
        self.user32.GetWindowTextW.argtypes = [
            wintypes.HWND,
            wintypes.LPWSTR,
            ctypes.c_int,
        ]
        self.user32.GetWindowTextW.restype = ctypes.c_int
        self.user32.IsWindow.argtypes = [wintypes.HWND]
        self.user32.IsWindow.restype = wintypes.BOOL
        self.user32.IsWindowVisible.argtypes = [wintypes.HWND]
        self.user32.IsWindowVisible.restype = wintypes.BOOL
        self.user32.IsIconic.argtypes = [wintypes.HWND]
        self.user32.IsIconic.restype = wintypes.BOOL
        self.user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
        self.user32.GetWindow.restype = wintypes.HWND
        self.user32.GetTopWindow.argtypes = [wintypes.HWND]
        self.user32.GetTopWindow.restype = wintypes.HWND
        self.user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
        self.user32.GetAncestor.restype = wintypes.HWND
        self.user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        self.user32.GetClientRect.restype = wintypes.BOOL
        self.user32.GetWindowRect.argtypes = [
            wintypes.HWND,
            ctypes.POINTER(wintypes.RECT),
        ]
        self.user32.GetWindowRect.restype = wintypes.BOOL
        self.user32.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
        self.user32.ClientToScreen.restype = wintypes.BOOL
        self.user32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
        self.user32.MonitorFromWindow.restype = wintypes.HANDLE
        self.user32.GetMonitorInfoW.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(_MONITORINFO),
        ]
        self.user32.GetMonitorInfoW.restype = wintypes.BOOL
        self.user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        self.user32.ShowWindow.restype = wintypes.BOOL
        self.user32.SetWindowPos.argtypes = [
            wintypes.HWND,
            wintypes.HWND,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.UINT,
        ]
        self.user32.SetWindowPos.restype = wintypes.BOOL
        self.user32.SetForegroundWindow.argtypes = [wintypes.HWND]
        self.user32.SetForegroundWindow.restype = wintypes.BOOL
        self.user32.BringWindowToTop.argtypes = [wintypes.HWND]
        self.user32.BringWindowToTop.restype = wintypes.BOOL
        self.user32.SetFocus.argtypes = [wintypes.HWND]
        self.user32.SetFocus.restype = wintypes.HWND
        self.user32.GetDlgItem.argtypes = [wintypes.HWND, ctypes.c_int]
        self.user32.GetDlgItem.restype = wintypes.HWND
        self.user32.SendMessageW.argtypes = [
            wintypes.HWND,
            wintypes.UINT,
            wintypes.WPARAM,
            wintypes.LPARAM,
        ]
        self.user32.SendMessageW.restype = ctypes.c_ssize_t
        self.user32.GetForegroundWindow.restype = wintypes.HWND
        self.user32.GetDC.argtypes = [wintypes.HWND]
        self.user32.GetDC.restype = wintypes.HDC
        self.user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
        self.user32.ReleaseDC.restype = ctypes.c_int
        self.user32.AttachThreadInput.argtypes = [
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.BOOL,
        ]
        self.user32.AttachThreadInput.restype = wintypes.BOOL
        self.user32.PostMessageW.argtypes = [
            wintypes.HWND,
            wintypes.UINT,
            wintypes.WPARAM,
            wintypes.LPARAM,
        ]
        self.user32.PostMessageW.restype = wintypes.BOOL
        self.user32.SendInput.argtypes = [
            wintypes.UINT,
            ctypes.POINTER(_INPUT),
            ctypes.c_int,
        ]
        self.user32.SendInput.restype = wintypes.UINT
        self.user32.MapVirtualKeyW.argtypes = [wintypes.UINT, wintypes.UINT]
        self.user32.MapVirtualKeyW.restype = wintypes.UINT
        self.kernel32.GetCurrentThreadId.restype = wintypes.DWORD
        self.gdi32.GetPixel.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
        self.gdi32.GetPixel.restype = wintypes.DWORD
        if ctypes.sizeof(ctypes.c_void_p) == 8:
            self._get_window_long = self.user32.GetWindowLongPtrW
            self._set_window_long = self.user32.SetWindowLongPtrW
            restype = ctypes.c_ssize_t
        else:
            self._get_window_long = self.user32.GetWindowLongW
            self._set_window_long = self.user32.SetWindowLongW
            restype = ctypes.c_long
        self._get_window_long.argtypes = [wintypes.HWND, ctypes.c_int]
        self._get_window_long.restype = restype
        self._set_window_long.argtypes = [wintypes.HWND, ctypes.c_int, restype]
        self._set_window_long.restype = restype

    def is_window(self, hwnd: int | None) -> bool:
        return bool(hwnd and self.user32.IsWindow(hwnd))

    def find_main_window(self, pid: int) -> int | None:
        candidates: list[tuple[int, int]] = []

        @self._enum_callback
        def callback(hwnd: int, _lparam: int) -> bool:
            process_id = wintypes.DWORD()
            self.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(process_id))
            if process_id.value != pid or not self.user32.IsWindowVisible(hwnd):
                return True
            title = self.window_title(hwnd)
            if not title:
                return True
            owner = self.user32.GetWindow(hwnd, self.GW_OWNER)
            candidates.append((0 if not owner else 1, int(hwnd)))
            return True

        self.user32.EnumWindows(callback, 0)
        if not candidates:
            return None
        candidates.sort()
        return candidates[0][1]

    def find_window_by_title(self, token: str) -> int | None:
        result: list[int] = []

        @self._enum_callback
        def callback(hwnd: int, _lparam: int) -> bool:
            if self.user32.IsWindowVisible(hwnd) and token in self.window_title(hwnd):
                result.append(int(hwnd))
                return False
            return True

        self.user32.EnumWindows(callback, 0)
        return result[0] if result else None

    def window_title(self, hwnd: int) -> str:
        length = self.user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return ""
        buffer = ctypes.create_unicode_buffer(length + 1)
        self.user32.GetWindowTextW(hwnd, buffer, len(buffer))
        return buffer.value

    def client_rect_on_screen(self, hwnd: int) -> WindowRect | None:
        rect = wintypes.RECT()
        if not self.user32.GetClientRect(hwnd, ctypes.byref(rect)):
            return None
        top_left = wintypes.POINT(rect.left, rect.top)
        bottom_right = wintypes.POINT(rect.right, rect.bottom)
        if not self.user32.ClientToScreen(hwnd, ctypes.byref(top_left)):
            return None
        if not self.user32.ClientToScreen(hwnd, ctypes.byref(bottom_right)):
            return None
        return WindowRect(
            top_left.x,
            top_left.y,
            bottom_right.x,
            bottom_right.y,
        )

    def window_rect(self, hwnd: int) -> WindowRect | None:
        rect = wintypes.RECT()
        if not self.user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return None
        return WindowRect(rect.left, rect.top, rect.right, rect.bottom)

    def visual_signature(
        self,
        hwnd: int,
        *,
        columns: int = 48,
        rows: int = 27,
    ) -> tuple[int, ...] | None:
        """Sample the visible game client without taking focus or installing a hook.

        Covered points are marked -1, not sampled from the overlay/desktop.
        Normalized points survive resolution changes. Occlusion rectangles are
        conservative and include click-through windows, whose pixels still
        cover the game even though WindowFromPoint may ignore their input.
        """

        if columns < 2 or rows < 2:
            raise ValueError("visual signature needs at least a 2 x 2 grid")
        rect = self.client_rect_on_screen(hwnd)
        if not rect or rect.width < 64 or rect.height < 64:
            return None
        occluders = self._occluding_rectangles(hwnd)
        if occluders is None:
            return None
        hdc = self.user32.GetDC(0)
        if not hdc:
            return None
        colors: list[int] = []
        try:
            left = rect.left + round(rect.width * 0.06)
            top = rect.top + round(rect.height * 0.24)
            width = max(1, round(rect.width * 0.88))
            height = max(1, round(rect.height * 0.70))
            for row in range(rows):
                y = top + round(height * row / (rows - 1))
                for column in range(columns):
                    x = left + round(width * column / (columns - 1))
                    if any(r.left <= x < r.right and r.top <= y < r.bottom
                           for r in occluders):
                        colors.append(-1)
                        continue
                    color = int(self.gdi32.GetPixel(hdc, x, y))
                    if color == 0xFFFFFFFF:
                        return None
                    colors.append(color & 0x00FFFFFF)
        finally:
            self.user32.ReleaseDC(0, hdc)
        if sum(color >= 0 for color in colors) < max(1, (len(colors) + 4) // 5):
            return None
        return tuple(colors)

    def _occluding_rectangles(self, hwnd: int) -> list[WindowRect] | None:
        """Snapshot visible top-level rectangles above the target in Z order.

        Do not change focus/visibility or trust input hit-testing here. A lost
        target or unstable window list makes the sample unavailable.
        """
        target = self.root_window(hwnd)
        current = self.user32.GetTopWindow(None)
        seen = set()
        result = []
        while current and current not in seen and len(seen) < 512:
            if current == target:
                return result
            seen.add(current)
            if self.user32.IsWindowVisible(current) and not self.user32.IsIconic(current):
                rect = wintypes.RECT()
                if self.user32.GetWindowRect(current, ctypes.byref(rect)):
                    result.append(WindowRect(rect.left, rect.top, rect.right, rect.bottom))
            current = self.user32.GetWindow(current, 2)  # GW_HWNDNEXT
        return None

    def monitor_rect(
        self,
        hwnd: int,
        *,
        work_area: bool = True,
    ) -> WindowRect | None:
        """Return the nearest monitor bounds in the caller's DPI coordinate space."""

        monitor = self.user32.MonitorFromWindow(
            hwnd,
            self.MONITOR_DEFAULTTONEAREST,
        )
        if not monitor:
            return None
        info = _MONITORINFO()
        info.cbSize = ctypes.sizeof(info)
        if not self.user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
            return None
        rect = info.rcWork if work_area else info.rcMonitor
        return WindowRect(rect.left, rect.top, rect.right, rect.bottom)

    def root_window(self, hwnd: int) -> int:
        """Return the native top-level HWND for a toolkit child window."""

        root = self.user32.GetAncestor(hwnd, self.GA_ROOT)
        return int(root or hwnd)

    def window_owner(self, hwnd: int) -> int | None:
        owner = self.user32.GetWindow(hwnd, self.GW_OWNER)
        return int(owner) if owner else None

    def window_exstyle(self, hwnd: int) -> int:
        return int(self._get_window_long(hwnd, self.GWL_EXSTYLE))

    def make_noactivate_tool(
        self,
        hwnd: int,
        *,
        click_through: bool = False,
    ) -> None:
        """Make an independent always-on-top tool window that cannot take focus.

        Unlike ``make_frameless_owned``, this deliberately never assigns a game
        window as the owner.  Cross-process ownership is unsafe for older game
        renderers and was the source of the browser-overlay focus failures.
        """

        exstyle = self.window_exstyle(hwnd)
        exstyle |= self.WS_EX_TOOLWINDOW | self.WS_EX_NOACTIVATE
        exstyle &= ~self.WS_EX_APPWINDOW
        if click_through:
            exstyle |= self.WS_EX_TRANSPARENT
        else:
            exstyle &= ~self.WS_EX_TRANSPARENT
        self._set_window_long(hwnd, self.GWL_EXSTYLE, exstyle)
        self.user32.SetWindowPos(
            hwnd,
            self.HWND_TOPMOST,
            0,
            0,
            0,
            0,
            self.SWP_NOMOVE
            | self.SWP_NOSIZE
            | self.SWP_NOACTIVATE
            | self.SWP_FRAMECHANGED
            | self.SWP_SHOWWINDOW,
        )

    def set_click_through(self, hwnd: int, enabled: bool) -> None:
        exstyle = self.window_exstyle(hwnd)
        if enabled:
            exstyle |= self.WS_EX_TRANSPARENT
        else:
            exstyle &= ~self.WS_EX_TRANSPARENT
        exstyle |= self.WS_EX_TOOLWINDOW | self.WS_EX_NOACTIVATE
        exstyle &= ~self.WS_EX_APPWINDOW
        self._set_window_long(hwnd, self.GWL_EXSTYLE, exstyle)
        self.user32.SetWindowPos(
            hwnd,
            self.HWND_TOPMOST,
            0,
            0,
            0,
            0,
            self.SWP_NOMOVE
            | self.SWP_NOSIZE
            | self.SWP_NOACTIVATE
            | self.SWP_FRAMECHANGED,
        )

    def show_no_activate(self, hwnd: int) -> None:
        self.user32.ShowWindow(hwnd, self.SW_SHOWNOACTIVATE)
        self.user32.SetWindowPos(
            hwnd,
            self.HWND_TOPMOST,
            0,
            0,
            0,
            0,
            self.SWP_NOMOVE
            | self.SWP_NOSIZE
            | self.SWP_NOACTIVATE
            | self.SWP_SHOWWINDOW,
        )

    def make_frameless_owned(self, hwnd: int, owner: int | None) -> None:
        style = int(self._get_window_long(hwnd, self.GWL_STYLE))
        style &= ~(
            self.WS_CAPTION
            | self.WS_THICKFRAME
            | self.WS_MINIMIZEBOX
            | self.WS_MAXIMIZEBOX
            | self.WS_SYSMENU
        )
        self._set_window_long(hwnd, self.GWL_STYLE, style)
        exstyle = int(self._get_window_long(hwnd, self.GWL_EXSTYLE))
        self._set_window_long(hwnd, self.GWL_EXSTYLE, exstyle | self.WS_EX_TOOLWINDOW)
        if owner:
            self._set_window_long(hwnd, self.GWLP_HWNDPARENT, owner)
        self.user32.SetWindowPos(
            hwnd,
            self.HWND_TOP,
            0,
            0,
            0,
            0,
            self.SWP_NOMOVE
            | self.SWP_NOSIZE
            | self.SWP_NOACTIVATE
            | self.SWP_FRAMECHANGED,
        )

    def position(self, hwnd: int, rect: WindowRect, activate: bool = False) -> None:
        flags = 0 if activate else self.SWP_NOACTIVATE
        self.user32.SetWindowPos(
            hwnd,
            self.HWND_TOP,
            rect.left,
            rect.top,
            rect.width,
            rect.height,
            flags,
        )

    def show(self, hwnd: int, activate: bool = False) -> None:
        self.user32.ShowWindow(hwnd, self.SW_RESTORE if activate else self.SW_SHOW)
        if activate:
            self.foreground(hwnd)

    def hide(self, hwnd: int) -> None:
        self.user32.ShowWindow(hwnd, self.SW_HIDE)

    def foreground(self, hwnd: int) -> None:
        if not self.is_window(hwnd):
            raise ValueError("target window is no longer available")
        self.user32.ShowWindow(hwnd, self.SW_RESTORE)
        foreground = self.user32.GetForegroundWindow()
        foreground_thread = (
            self.user32.GetWindowThreadProcessId(foreground, None)
            if foreground
            else 0
        )
        target_thread = self.user32.GetWindowThreadProcessId(hwnd, None)
        current_thread = self.kernel32.GetCurrentThreadId()
        attached_threads: list[int] = []
        if foreground_thread and foreground_thread != current_thread:
            if self.user32.AttachThreadInput(current_thread, foreground_thread, True):
                attached_threads.append(foreground_thread)
        if (
            target_thread
            and target_thread != current_thread
            and target_thread not in attached_threads
        ):
            if self.user32.AttachThreadInput(current_thread, target_thread, True):
                attached_threads.append(target_thread)
        try:
            for _attempt in range(3):
                self.user32.BringWindowToTop(hwnd)
                self.user32.SetForegroundWindow(hwnd)
                self.user32.SetFocus(hwnd)
                if int(self.user32.GetForegroundWindow() or 0) == int(hwnd):
                    break
                time.sleep(0.03)
        finally:
            for thread_id in reversed(attached_threads):
                self.user32.AttachThreadInput(current_thread, thread_id, False)
        if int(self.user32.GetForegroundWindow() or 0) != int(hwnd):
            raise ValueError(
                "无法把键盘焦点交给原版游戏；请先点击一次游戏画面，再重试"
            )

    def click_dialog_button(self, hwnd: int, control_id: int = 1) -> bool:
        """Activate a standard dialog button (IDOK by default)."""

        button = self.user32.GetDlgItem(hwnd, control_id)
        if not button:
            return False
        self.user32.SendMessageW(button, self.BM_CLICK, 0, 0)
        return True

    def send_choice_path(
        self,
        hwnd: int,
        page_index: int,
        item_index: int,
        *,
        settle_seconds: float = 0.45,
    ) -> None:
        if page_index < 0 or item_index < 0:
            raise ValueError("choice indices must be non-negative")
        self.foreground(hwnd)
        def press(key, count=1):
            for _ in range(count):
                if self.user32.GetForegroundWindow() != hwnd:
                    raise ValueError('Game lost foreground during menu dispatch; no further keys sent')
                self._press(key)
                if key != self.VK_RETURN:
                    time.sleep(0.018)
        press(self.VK_UP, 12)
        press(self.VK_DOWN, page_index)
        press(self.VK_RETURN)
        time.sleep(settle_seconds)
        press(self.VK_UP, 12)
        press(self.VK_DOWN, item_index)
        press(self.VK_RETURN)

    def close(self, hwnd: int) -> None:
        if self.is_window(hwnd):
            self.user32.PostMessageW(hwnd, self.WM_CLOSE, 0, 0)

    def is_iconic(self, hwnd: int) -> bool:
        return bool(self.user32.IsIconic(hwnd))

    def _press_many(self, virtual_key: int, count: int) -> None:
        for _ in range(count):
            self._press(virtual_key)
            time.sleep(0.018)

    def _press(self, virtual_key: int) -> None:
        scan_code = int(self.user32.MapVirtualKeyW(virtual_key, 0))
        extended = virtual_key in {self.VK_UP, self.VK_DOWN}
        if scan_code:
            key_value = 0
            base_flags = self.KEYEVENTF_SCANCODE
            if extended:
                base_flags |= self.KEYEVENTF_EXTENDEDKEY
        else:
            key_value = virtual_key
            base_flags = 0
        inputs = (_INPUT * 2)(
            _INPUT(
                type=self.INPUT_KEYBOARD,
                ki=_KEYBDINPUT(
                    wVk=key_value,
                    wScan=scan_code,
                    dwFlags=base_flags,
                ),
            ),
            _INPUT(
                type=self.INPUT_KEYBOARD,
                ki=_KEYBDINPUT(
                    wVk=key_value,
                    wScan=scan_code,
                    dwFlags=base_flags | self.KEYEVENTF_KEYUP,
                ),
            ),
        )
        sent = self.user32.SendInput(1, ctypes.byref(inputs[0]), ctypes.sizeof(_INPUT))
        if sent != 1:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            time.sleep(self.KEY_HOLD_SECONDS)
        finally:
            sent = self.user32.SendInput(1, ctypes.byref(inputs[1]), ctypes.sizeof(_INPUT))
            if sent != 1:
                raise ctypes.WinError(ctypes.get_last_error())
        time.sleep(self.KEY_RELEASE_SECONDS)
