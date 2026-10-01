from __future__ import annotations

import ctypes
import os
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from hgalgame.runtime.windows import WindowRect, WindowsWindowController


class WindowsOverlayHost:
    """Run the existing web UI as a frameless window attached to the game."""

    def __init__(
        self,
        output_dir: Path,
        game_pid: Callable[[], int | None],
        on_closed: Callable[[], None] | None = None,
    ) -> None:
        self.output_dir = output_dir.resolve()
        self._game_pid = game_pid
        self._on_closed = on_closed
        self._windows = WindowsWindowController()
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._thread: threading.Thread | None = None
        self._edge_process: subprocess.Popen[bytes] | None = None
        self._overlay_hwnd: int | None = None
        self._game_hwnd: int | None = None
        self._mode = "full"
        self._token = f"HGAL-{uuid.uuid4().hex[:12]}"
        self._error: str | None = None

    def start(self, url: str, timeout: float = 15.0) -> dict[str, object]:
        edge = self._find_edge()
        overlay_url = self._with_query(
            url,
            {"overlay": "1", "overlay_token": self._token},
        )
        profile = self.output_dir / "overlay_profile"
        profile.mkdir(parents=True, exist_ok=True)
        command = [
            str(edge),
            f"--app={overlay_url}",
            f"--user-data-dir={profile}",
            "--no-first-run",
            "--disable-session-crashed-bubble",
            "--disable-features=msEdgeSidebarV2",
            "--window-size=1200,800",
        ]
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self._edge_process = subprocess.Popen(
            command,
            cwd=str(self.output_dir),
            creationflags=creationflags,
        )
        self._thread = threading.Thread(
            target=self._monitor,
            name="hgal-overlay",
            daemon=True,
        )
        self._thread.start()
        if not self._ready.wait(timeout):
            raise ValueError(
                self._error
                or "Edge overlay window did not become ready within the timeout"
            )
        if self._error:
            raise ValueError(self._error)
        return self.status()

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            hwnd = self._overlay_hwnd
        if hwnd:
            self._windows.close(hwnd)
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=3)
        process = self._edge_process
        if process and process.poll() is None:
            try:
                process.terminate()
                process.wait(timeout=2)
            except (OSError, subprocess.TimeoutExpired):
                pass

    def set_mode(self, mode: str, *, activate: bool = False) -> dict[str, object]:
        if mode not in {"full", "compact", "hidden"}:
            raise ValueError("overlay mode must be full, compact, or hidden")
        with self._lock:
            self._mode = mode
            hwnd = self._overlay_hwnd
        if hwnd:
            self._apply_layout(hwnd, activate=activate)
        return self.status()

    def toggle(self) -> dict[str, object]:
        with self._lock:
            target = "full" if self._mode != "full" else "compact"
        return self.set_mode(target, activate=target == "full")

    def status(self) -> dict[str, object]:
        with self._lock:
            return {
                "available": self._error is None,
                "mode": self._mode,
                "ready": bool(
                    self._overlay_hwnd
                    and self._windows.is_window(self._overlay_hwnd)
                ),
                "attached": bool(
                    self._game_hwnd and self._windows.is_window(self._game_hwnd)
                ),
                "hotkey": None,
                "error": self._error,
            }

    def _monitor(self) -> None:
        seen = False
        missing_since: float | None = None
        try:
            deadline = time.monotonic() + 15
            while not self._stop.is_set():
                hwnd = self._windows.find_window_by_title(self._token)
                if hwnd:
                    with self._lock:
                        self._overlay_hwnd = hwnd
                    if not seen:
                        seen = True
                        self._refresh_game_window()
                        self._windows.make_frameless_owned(hwnd, self._game_hwnd)
                        self._apply_layout(hwnd, activate=True)
                        self._ready.set()
                    missing_since = None
                elif seen:
                    missing_since = missing_since or time.monotonic()
                    if time.monotonic() - missing_since > 1.5:
                        break
                elif time.monotonic() >= deadline:
                    self._error = "could not locate the Edge app window"
                    self._ready.set()
                    return

                self._refresh_game_window()
                if hwnd:
                    self._apply_layout(hwnd)
                time.sleep(0.08)
        except Exception as exc:
            self._error = f"overlay monitor failed: {exc}"
            self._ready.set()
        finally:
            self._ready.set()
            if seen and not self._stop.is_set() and self._on_closed:
                threading.Thread(target=self._on_closed, daemon=True).start()

    def _refresh_game_window(self) -> None:
        pid = self._game_pid()
        hwnd = self._windows.find_main_window(pid) if pid else None
        with self._lock:
            previous = self._game_hwnd
            self._game_hwnd = hwnd
            overlay = self._overlay_hwnd
        if hwnd and hwnd != previous and overlay:
            self._windows.make_frameless_owned(overlay, hwnd)

    def _apply_layout(self, hwnd: int, activate: bool = False) -> None:
        with self._lock:
            mode = self._mode
            game_hwnd = self._game_hwnd
        if mode == "hidden" or (
            game_hwnd and self._windows.is_iconic(game_hwnd)
        ):
            self._windows.hide(hwnd)
            return
        rect = (
            self._windows.client_rect_on_screen(game_hwnd)
            if game_hwnd
            else None
        )
        if rect is None or rect.width < 320 or rect.height < 240:
            rect = self._fallback_rect()
        if mode == "compact":
            width = min(480, max(360, rect.width - 24))
            height = 76
            rect = WindowRect(
                rect.right - width - 12,
                rect.top + 12,
                rect.right - 12,
                rect.top + 12 + height,
            )
        self._windows.show(hwnd, activate=activate)
        self._windows.position(hwnd, rect, activate=activate)

    def _fallback_rect(self) -> WindowRect:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        width = int(user32.GetSystemMetrics(0))
        height = int(user32.GetSystemMetrics(1))
        window_width = min(1280, max(800, width - 160))
        window_height = min(900, max(600, height - 120))
        left = max(0, (width - window_width) // 2)
        top = max(0, (height - window_height) // 2)
        return WindowRect(left, top, left + window_width, top + window_height)

    def _find_edge(self) -> Path:
        candidates = [
            shutil.which("msedge.exe"),
            os.path.join(
                os.environ.get("ProgramFiles(x86)", ""),
                "Microsoft",
                "Edge",
                "Application",
                "msedge.exe",
            ),
            os.path.join(
                os.environ.get("ProgramFiles", ""),
                "Microsoft",
                "Edge",
                "Application",
                "msedge.exe",
            ),
        ]
        for value in candidates:
            if value and Path(value).is_file():
                return Path(value).resolve()
        raise FileNotFoundError(
            "Microsoft Edge was not found; use story-ui without --overlay"
        )

    def _with_query(self, url: str, values: dict[str, str]) -> str:
        parts = urlsplit(url)
        query = dict(parse_qsl(parts.query, keep_blank_values=True))
        query.update(values)
        return urlunsplit(
            (parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment)
        )
