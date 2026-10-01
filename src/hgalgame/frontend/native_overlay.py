from __future__ import annotations

import json
import math
import queue
import threading
import time
import tkinter as tk
from tkinter import ttk
from pathlib import Path
from typing import Any, Callable

from hgalgame.frontend.route_reader import NovelGate, NovelTrace, RouteNovelReader
from hgalgame.frontend.translation_ui import TranslationControls
from hgalgame.frontend.hook_controls import HookControls
from hgalgame.output import write_json
from hgalgame.runtime.windows import WindowRect, WindowsWindowController


class NativeStoryOverlay(TranslationControls, HookControls):
    """Resizable engine-neutral story accelerator implemented with native Tk.

    The window is deliberately independent from the game HWND.  It is topmost,
    movable and marked ``WS_EX_NOACTIVATE`` so clicking it does not transfer
    keyboard focus away from older DirectX game windows.
    """

    TITLE = "HGal 剧情加速器"
    SESSION_VERSION = 1
    SETTINGS_VERSION = 2
    MODE_READER = "reader"
    MODE_SCENE = "scene"
    CLICK_THROUGH_SECONDS = 8
    WINDOW_GUARD_INTERVAL_MS = 750
    WINDOW_EDGE_MARGIN = 12
    WHEEL_SCROLL_LINES = 4
    HISTORY_PAGE_SIZE = 2
    CONFIRM_DELAY_SECONDS = 1.0

    TITLE_HEIGHT = 48
    SCENE_STRIP_HEIGHT = 50
    VISIBILITY_STRIP_HEIGHT = TITLE_HEIGHT + 2
    VISIBILITY_STRIP_WIDTH = 58
    DEFAULT_WIDTH = 920
    DEFAULT_HEIGHT = 660
    MIN_WIDTH = 620
    MIN_HEIGHT = 520
    MAX_WIDTH = 2560
    MAX_HEIGHT = 1500
    DEFAULT_FONT_SIZE = 14
    MIN_FONT_SIZE = 11
    MAX_FONT_SIZE = 22
    RESIZE_BORDER = 7

    BG = "#f4f7f6"
    PANEL = "#ffffff"
    SURFACE = "#ffffff"
    PANEL_2 = "#edf3f0"
    BORDER = "#d7e3dd"
    TEXT = "#263d35"
    MUTED = "#64786e"
    ACCENT = "#287a65"
    GOLD = "#98713b"
    DANGER = "#b34e49"

    def __init__(
        self,
        document: dict[str, Any],
        output_dir: Path,
        *,
        perform_action: Callable[[str, dict[str, Any]], dict[str, Any]],
        playback_status: Callable[[], dict[str, Any]] | None = None,
        on_reader_resumed: Callable[[], dict[str, Any]] | None = None,
        game_pid: Callable[[], int | None] | None = None,
        window_controller: WindowsWindowController | None = None,
    ) -> None:
        self.document = document
        self.output_dir = output_dir.resolve()
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.perform_action = perform_action
        self.playback_status = playback_status
        self.on_reader_resumed = on_reader_resumed
        self.game_pid = game_pid or (lambda: None)
        self.windows = window_controller or WindowsWindowController()
        self.session_path = self.output_dir / "reader_session.json"
        self.settings_path = self.output_dir / "native_overlay_state.json"
        self.runtime_path = self.output_dir / "native_overlay_runtime.json"
        self.rewind_backup_path = self.output_dir / "reader_session.before-rewind.json"
        self.fingerprint = str(document.get("game", {}).get("fingerprint", ""))
        self.settings = self._load_settings()
        self.reader = RouteNovelReader(document, self._load_actions())
        self.trace = self.reader.render()
        self.root: tk.Tk | None = None
        self.hwnd: int | None = None
        self.text: tk.Text | None = None
        self.body: tk.Frame | None = None
        self.title_bar: tk.Frame | None = None
        self.gate_panel: tk.Frame | None = None
        self.title_label: tk.Label | None = None
        self.mode_badge: tk.Label | None = None
        self.status_label: tk.Label | None = None
        self.progress_label: tk.Label | None = None
        self.mode_button: tk.Button | None = None
        self.title_content: tk.Frame | None = None
        self.visibility_button: tk.Button | None = None
        self.sync_button: tk.Button | None = None
        self.lock_button: tk.Button | None = None
        self.history_button: tk.Button | None = None
        self._scene_catalog_open = False
        self._scene_catalog_page = 0
        self._mode = self.MODE_READER
        # A persistent click-through window cannot be recovered with the mouse.
        # Keep this session-safe and time-limited instead of relying on a global
        # keyboard shortcut that would also reach the game.
        self._click_through = False
        self._opacity = float(self.settings.get("opacity", 1.0))
        if self._opacity == 0.94:  # Migrate the old translucent default.
            self._opacity = 1.0
        self._font_size = self._integer(
            self.settings.get("font_size"),
            self.DEFAULT_FONT_SIZE,
            self.MIN_FONT_SIZE,
            self.MAX_FONT_SIZE,
        )
        self._expanded_geometry: tuple[int, int, int, int] | None = None
        self._visibility_restore_geometry: tuple[int, int, int, int] | None = None
        self._visibility_collapsed = False
        self._drag_origin: tuple[int, int, int, int] | None = None
        self._resize_origin: tuple[str, int, int, int, int, int, int] | None = None
        self._resize_handles: list[tuple[tk.Widget, dict[str, Any]]] = []
        self._wrapped_widgets: list[tuple[tk.Widget, int]] = []
        self._save_after: str | None = None
        self._click_through_after: str | None = None
        self._events: queue.Queue[tuple[str, Any]] = queue.Queue()
        self._playback_busy = False
        self._closing_after_playback = False
        self._last_error: str | None = None
        self._scene_label: str | None = None
        self._active_scene_gate: NovelGate | None = None
        self._scene_dispatched = False
        self._scene_result: dict[str, Any] | None = None
        self._scene_return_armed = False
        self._scene_return_armed_at: float | None = None
        self._sync_reset_armed = False
        self._sync_reset_armed_at: float | None = None
        self._pending_auto_scene_id: str | None = None
        self._auto_scene_generation = 0
        self._awaiting_scene_continue = bool(self.reader.actions)
        self._scroll_anchor: str | None = None
        self._revealed_choice_guides: set[str] = set()
        self._history_open = False
        self._history_page = 0
        self._window_guard_count = 0
        self._window_recovered_count = 0
        self._runtime_written_at = 0.0
        self._luna_bridge = None
        self._live_translation = None
        self._init_translation()
        self._init_managed_hook()

    def run(self) -> None:
        root = tk.Tk()
        self.root = root
        root.withdraw()
        root.title(self.TITLE)
        root.configure(bg=self.BG)
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        root.attributes("-alpha", self._opacity)
        root.protocol("WM_DELETE_WINDOW", self.close)
        self._build_widgets(root)
        self._apply_initial_geometry(root)
        root.update_idletasks()

        self.hwnd = self.windows.root_window(int(root.winfo_id()))
        self.windows.make_noactivate_tool(
            self.hwnd,
            click_through=self._click_through,
        )
        root.deiconify()
        root.update_idletasks()
        self.hwnd = self.windows.root_window(int(root.winfo_id()))
        self.windows.make_noactivate_tool(
            self.hwnd,
            click_through=self._click_through,
        )
        self.windows.show_no_activate(self.hwnd)
        self._restore_game_focus()
        self._render_trace()
        self._start_luna_bridge()
        self._queue_next_scene()
        self._update_mode_controls()
        self._update_lock_label()
        self._write_runtime_state()
        root.after(60, self._poll)
        root.after(250, self._guard_window)
        try:
            root.mainloop()
        finally:
            if self._luna_bridge:self._luna_bridge.stop()
            self._save_now()
            self.root = None

    def close(self) -> None:
        if self._batch_translation_window and not self._batch_translation_window.closed:
            self._batch_translation_window.close()
        if self._luna_bridge:
            self._luna_bridge.stop()
        self._translation_stop.set()
        self._cancel_auto_scene()
        if not self.root:
            return
        if self._playback_busy:
            self._closing_after_playback = True
            self._set_status("正在完成 Scene 切换，随后自动关闭……", self.GOLD)
            return
        self._disable_click_through()
        self._mode = "closed"
        self._write_runtime_state()
        self._save_now()
        root = self.root
        self.root = None
        root.destroy()

    def _start_luna_bridge(self) -> None:
        """Subscribe to an already running LunaTranslator only when opted in."""
        if self._hook_config.get('enabled'):return
        if not (self.output_dir / 'luna_bridge.json').is_file(): return
        try:
            import json
            config = json.loads((self.output_dir / 'luna_bridge.json').read_text('utf-8'))
            if not config.get('enabled', False):
                return
            from hgalgame.luna_bridge import LunaBridgeConfig, LunaOriginBridge, LocalTranslationMatcher
            self._live_matcher = LocalTranslationMatcher(self._translation_library, self._translation_config.get('target', '简体中文'), self.document)
            bridge_config = LunaBridgeConfig(host=config.get('host', '127.0.0.1'),
                port=int(config.get('port', 2333)), path=config.get('path', '/api/ws/text/origin'))
            def received(text):
                gate = self._active_scene_gate
                self._events.put(('luna_text', (gate.id if gate else None, text)))
            self._luna_bridge = LunaOriginBridge(bridge_config, on_text=received,
                on_status=lambda status: self._events.put(('luna_status', status)))
            self._luna_bridge.start()
            self._set_status('正在连接 Luna 文本桥……', self.MUTED)
        except (OSError, ValueError, TypeError, ImportError, RuntimeError) as exc:
            self._set_status('Luna 文本桥未启用：' + str(exc), self.MUTED)

    def mark_playback_stopped(self, playback: dict[str, Any]) -> None:
        """Finalize the diagnostic state after the engine session is restored."""

        try:
            payload = json.loads(self.runtime_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        payload.update(
            {
                "version": 1,
                "frontend": "native_tk_overlay",
                "mode": "closed",
                "window_running": False,
                "last_hwnd": payload.get("hwnd") or self.hwnd,
                "hwnd": None,
                "playback": playback,
            }
        )
        write_json(self.runtime_path, payload)

    def _build_widgets(self, root: tk.Tk) -> None:
        shell = tk.Frame(
            root,
            bg=self.BORDER,
            highlightthickness=0,
            borderwidth=0,
        )
        shell.pack(fill="both", expand=True, padx=1, pady=1)

        title_bar = tk.Frame(
            shell,
            bg=self.PANEL,
            height=self.TITLE_HEIGHT,
            cursor="fleur",
        )
        title_bar.pack(fill="x")
        title_bar.pack_propagate(False)
        title_bar.bind("<ButtonPress-1>", self._start_drag)
        title_bar.bind("<B1-Motion>", self._drag)
        self.title_bar = title_bar

        self.visibility_button = self._title_button(
            title_bar,
            "👁",
            self._toggle_visibility,
            self.ACCENT,
        )
        self.visibility_button.configure(
            width=3,
            font=("Segoe UI Symbol", 12, "bold"),
        )
        self.visibility_button.pack(side="right", fill="y")

        self.title_content = tk.Frame(
            title_bar,
            bg=self.PANEL,
            cursor="fleur",
        )
        self.title_content.pack(side="left", fill="both", expand=True)
        self.title_content.bind("<ButtonPress-1>", self._start_drag)
        self.title_content.bind("<B1-Motion>", self._drag)

        badge = tk.Label(
            self.title_content,
            text="HG",
            bg=self.ACCENT,
            fg="#ffffff",
            font=("Segoe UI", 9, "bold"),
            padx=8,
            pady=3,
        )
        badge.pack(side="left", padx=(13, 9), pady=10)
        badge.bind("<ButtonPress-1>", self._start_drag)
        badge.bind("<B1-Motion>", self._drag)

        self.mode_badge = tk.Label(
            self.title_content,
            text="阅读",
            bg=self.PANEL_2,
            fg=self.ACCENT,
            font=("Microsoft YaHei UI", 8, "bold"),
            padx=8,
            pady=3,
        )
        self.mode_badge.pack(side="left", padx=(0, 10), pady=10)
        self.mode_badge.bind("<ButtonPress-1>", self._start_drag)
        self.mode_badge.bind("<B1-Motion>", self._drag)

        self.title_label = tk.Label(
            self.title_content,
            text=self.document.get("game", {}).get("title") or self.TITLE,
            bg=self.PANEL,
            fg=self.TEXT,
            anchor="w",
            font=("Microsoft YaHei UI", 11, "bold"),
        )
        self.title_label.pack(side="left", fill="x", expand=True)
        self.title_label.bind("<ButtonPress-1>", self._start_drag)
        self.title_label.bind("<B1-Motion>", self._drag)

        self._title_button(
            self.title_content,
            "×",
            self.close,
            self.DANGER,
        ).pack(side="right")
        self._title_button(
            self.title_content,
            "场景目录",
            self._toggle_scene_catalog,
        ).pack(side="right")
        self._title_button(
            self.title_content, "A+", lambda: self._change_font_size(1)
        ).pack(side="right")
        self._title_button(
            self.title_content, "A−", lambda: self._change_font_size(-1)
        ).pack(side="right")
        self.mode_button = self._title_button(
            self.title_content, "收起", self._toggle_mode
        )
        self.mode_button.pack(side="right")
        self._title_button(self.title_content,'字幕 Hook',self._open_hook_settings).pack(side='right')
        self.sync_button = self._title_button(
            self.title_content,
            "同步异常",
            self._request_sync_reset,
            self.DANGER,
        )

        self.body = tk.Frame(shell, bg=self.BG)
        self.body.pack(fill="both", expand=True)

        text_frame = tk.Frame(
            self.body,
            bg=self.SURFACE,
            highlightbackground=self.BORDER,
            highlightthickness=1,
            borderwidth=0,
        )
        text_frame.pack(fill="both", expand=True, padx=14, pady=(14, 7))
        self._translation_toolbar(text_frame)
        style = ttk.Style(root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure(
            "HGal.Vertical.TScrollbar",
            background=self.BORDER,
            troughcolor=self.SURFACE,
            bordercolor=self.SURFACE,
            lightcolor=self.BORDER,
            darkcolor=self.BORDER,
            arrowcolor=self.MUTED,
            borderwidth=0,
            relief="flat",
            arrowsize=11,
            width=11,
        )
        style.map(
            "HGal.Vertical.TScrollbar",
            background=[("active", "#a9c7ba")],
            arrowcolor=[("active", self.TEXT)],
        )
        scrollbar = ttk.Scrollbar(
            text_frame,
            orient="vertical",
            style="HGal.Vertical.TScrollbar",
        )
        scrollbar.pack(side="right", fill="y")
        self.text = tk.Text(
            text_frame,
            # Keep only a small requested minimum.  The reader still expands
            # to all free space, while a choice gate can claim enough height
            # to keep both route cards fully visible.
            height=4,
            wrap="word",
            undo=False,
            borderwidth=0,
            highlightthickness=0,
            bg=self.SURFACE,
            fg=self.TEXT,
            insertbackground=self.TEXT,
            selectbackground="#d7ebe2",
            font=("Microsoft YaHei UI", self._font_size),
            padx=20,
            pady=16,
            spacing1=0,
            spacing2=2,
            spacing3=0,
            yscrollcommand=scrollbar.set,
            cursor="arrow",
        )
        self.text.pack(side="left", fill="both", expand=True)
        scrollbar.configure(command=self.text.yview)
        self.text.bind(
            "<MouseWheel>",
            self._scroll_wheel,
        )
        self._apply_text_typography()

        self.subtitle_panel = tk.Frame(self.body, bg=self.SURFACE,
            highlightbackground=self.BORDER, highlightthickness=1)
        self.subtitle_panel.pack(fill='x', padx=14, pady=4)
        subtitle_header=tk.Frame(self.subtitle_panel,bg=self.SURFACE)
        subtitle_header.pack(fill='x',padx=12,pady=(6,0))
        tk.Label(subtitle_header,text='实时字幕',bg=self.SURFACE,fg=self.ACCENT,
                 font=('Microsoft YaHei UI',10,'bold')).pack(side='left')
        self.subtitle_status=tk.Label(subtitle_header,text='等待演出',bg=self.SURFACE,fg=self.MUTED)
        self.subtitle_status.pack(side='left',padx=12)
        tk.Button(subtitle_header,text='Hook 设置',command=self._open_hook_settings,
                  bg=self.SURFACE,relief='flat').pack(side='right')
        tk.Button(subtitle_header,text='匹配详情',command=self._show_subtitle_match_details,
                  bg=self.SURFACE,relief='flat').pack(side='right')
        tk.Button(subtitle_header,text='更多',command=self._subtitle_more,
                  bg=self.SURFACE,relief='flat').pack(side='right')
        self.subtitle_label = tk.Label(self.subtitle_panel, text='演出开始后，当前台词会显示在这里。', bg=self.SURFACE,
            fg=self.TEXT, anchor='w', justify='left', font=('Microsoft YaHei UI', self._font_size),
            padx=12, pady=8, wraplength=500)
        self.subtitle_label.pack(fill='x')
        history_frame = tk.Frame(self.subtitle_panel, bg=self.SURFACE)
        history_frame.pack(fill='x', padx=12, pady=(0,6))
        self.subtitle_history = tk.Text(history_frame, height=5, wrap='word', relief='flat',
            bg=self.SURFACE, fg=self.TEXT, state='disabled', font=('Microsoft YaHei UI', self._font_size))
        history_scroll = tk.Scrollbar(history_frame, command=self.subtitle_history.yview)
        self.subtitle_history.configure(yscrollcommand=history_scroll.set)
        history_scroll.pack(side='right', fill='y')
        self.subtitle_history.pack(side='left', fill='both', expand=True)
        self.subtitle_panel.bind('<Configure>', lambda e: self.subtitle_label.configure(wraplength=max(160,e.width-24)))

        self.gate_panel = tk.Frame(
            self.body,
            bg=self.PANEL,
            highlightbackground=self.BORDER,
            highlightthickness=1,
        )
        self.gate_panel.pack(fill="x", padx=14, pady=(7, 6))

        footer = tk.Frame(self.body, bg=self.BG, height=32)
        footer.pack(fill="x", padx=16, pady=(0, 7))
        footer.pack_propagate(False)
        self.status_label = tk.Label(
            footer,
            text="路线阅读器已就绪 · 所有操作均可用鼠标完成",
            bg=self.BG,
            fg=self.MUTED,
            anchor="w",
            font=("Microsoft YaHei UI", 9),
        )
        self.status_label.pack(side="left", fill="both", expand=True)

        for label, command in (
            ("下一屏", lambda: self._scroll_page(1)),
            ("上一屏", lambda: self._scroll_page(-1)),
            ("顶部", self._scroll_to_start),
        ):
            button = self._footer_button(footer, label, command)
            button.pack(side="right")
        self.history_button = self._footer_button(
            footer,
            "路线记录",
            self._toggle_history,
        )
        self.history_button.pack(side="right")
        self.progress_label = tk.Label(
            footer,
            text="",
            bg=self.BG,
            fg=self.MUTED,
            anchor="e",
            font=("Microsoft YaHei UI", 9),
        )
        self.progress_label.pack(side="right", padx=(8, 2))

        self._build_resize_handles(root)
        root.bind("<Configure>", self._geometry_changed)
        root.bind("<ButtonRelease-1>", self._end_drag, add="+")

    def _title_button(
        self,
        parent: tk.Widget,
        text: str,
        command: Callable[[], None],
        foreground: str | None = None,
    ) -> tk.Button:
        return tk.Button(
            parent,
            text=text,
            command=command,
            takefocus=False,
            relief="flat",
            borderwidth=0,
            highlightthickness=0,
            bg=self.PANEL,
            activebackground=self.PANEL_2,
            fg=foreground or self.MUTED,
            activeforeground=self.TEXT,
            font=("Microsoft YaHei UI", 9, "bold"),
            padx=10,
            pady=2,
            cursor="hand2",
        )

    def _footer_button(
        self,
        parent: tk.Widget,
        text: str,
        command: Callable[[], None],
    ) -> tk.Button:
        return tk.Button(
            parent,
            text=text,
            command=command,
            takefocus=False,
            relief="flat",
            borderwidth=0,
            highlightthickness=0,
            bg=self.BG,
            activebackground=self.PANEL_2,
            fg=self.ACCENT,
            activeforeground=self.TEXT,
            font=("Microsoft YaHei UI", 9),
            padx=7,
            cursor="hand2",
        )

    def _build_resize_handles(self, root: tk.Tk) -> None:
        """Add generous resize hit areas to every edge of the borderless window."""

        border = self.RESIZE_BORDER
        corner = border * 2
        specs: tuple[tuple[str, str, dict[str, Any]], ...] = (
            (
                "n",
                "sb_v_double_arrow",
                {
                    "x": corner,
                    "y": 0,
                    "relwidth": 1.0,
                    "width": -(corner * 2),
                    "height": border,
                },
            ),
            (
                "s",
                "sb_v_double_arrow",
                {
                    "x": corner,
                    "rely": 1.0,
                    "y": -border,
                    "relwidth": 1.0,
                    "width": -(corner * 2),
                    "height": border,
                },
            ),
            (
                "w",
                "sb_h_double_arrow",
                {
                    "x": 0,
                    "y": corner,
                    "width": border,
                    "relheight": 1.0,
                    "height": -(corner * 2),
                },
            ),
            (
                "e",
                "sb_h_double_arrow",
                {
                    "relx": 1.0,
                    "x": -border,
                    "y": corner,
                    "width": border,
                    "relheight": 1.0,
                    "height": -(corner * 2),
                },
            ),
            ("nw", "size_nw_se", {"x": 0, "y": 0, "width": corner, "height": corner}),
            (
                "ne",
                "size_ne_sw",
                {"relx": 1.0, "x": -corner, "y": 0, "width": corner, "height": corner},
            ),
            (
                "sw",
                "size_ne_sw",
                {"x": 0, "rely": 1.0, "y": -corner, "width": corner, "height": corner},
            ),
            (
                "se",
                "size_nw_se",
                {
                    "relx": 1.0,
                    "x": -corner,
                    "rely": 1.0,
                    "y": -corner,
                    "width": corner,
                    "height": corner,
                },
            ),
        )
        self._resize_handles.clear()
        for edge, cursor, place_options in specs:
            background = self.PANEL if "n" in edge else self.BG
            if edge == "se":
                handle: tk.Widget = tk.Label(
                    root,
                    text="◢",
                    bg=background,
                    fg=self.MUTED,
                    font=("Segoe UI Symbol", 9),
                    cursor=cursor,
                    anchor="se",
                )
            else:
                handle = tk.Frame(root, bg=background, cursor=cursor)
            handle.bind(
                "<ButtonPress-1>",
                lambda event, value=edge: self._start_resize(event, value),
            )
            handle.bind("<B1-Motion>", self._resize)
            handle.bind("<ButtonRelease-1>", self._end_resize)
            self._resize_handles.append((handle, place_options))
        self._set_resize_handles_visible(True)

    def _set_resize_handles_visible(self, visible: bool) -> None:
        for handle, place_options in self._resize_handles:
            if visible:
                handle.place(**place_options)
                handle.lift()
            else:
                handle.place_forget()

    def _apply_text_typography(self) -> None:
        if not self.text:
            return
        body_size = self._font_size
        self.text.configure(font=("Microsoft YaHei UI", body_size))
        self.text.tag_configure(
            "chapter",
            foreground=self.ACCENT,
            font=("Microsoft YaHei UI", max(10, body_size - 2), "bold"),
            spacing1=16,
            spacing3=10,
        )
        self.text.tag_configure(
            "interval",
            foreground=self.ACCENT,
            font=("Microsoft YaHei UI", max(12, body_size + 1), "bold"),
            spacing1=2,
            spacing3=5,
        )
        self.text.tag_configure(
            "interval_meta",
            foreground=self.MUTED,
            font=("Microsoft YaHei UI", max(9, body_size - 3)),
            spacing3=10,
        )
        self.text.tag_configure(
            "speaker",
            foreground=self.GOLD,
            font=("Microsoft YaHei UI", max(10, body_size - 1), "bold"),
        )
        self.text.tag_configure(
            "dialogue",
            foreground=self.TEXT,
            lmargin1=4,
            lmargin2=4,
            rmargin=10,
            spacing1=1,
            spacing3=3,
        )
        self.text.tag_configure(
            "narration",
            foreground=self.TEXT,
            lmargin1=max(28, body_size * 2 + 4),
            lmargin2=4,
            rmargin=10,
            spacing1=1,
            spacing3=4,
        )

    def _change_font_size(self, delta: int) -> None:
        size = max(
            self.MIN_FONT_SIZE,
            min(self.MAX_FONT_SIZE, self._font_size + delta),
        )
        if size == self._font_size:
            return
        self._font_size = size
        self._apply_text_typography()
        self._set_status(
            f"正文字号 {size} · 拖动窗口四边或四角可调整大小",
            self.MUTED,
        )
        self._schedule_save()

    def _apply_initial_geometry(self, root: tk.Tk) -> None:
        width = self._integer(
            self.settings.get("width"),
            self.DEFAULT_WIDTH,
            self.MIN_WIDTH,
            self.MAX_WIDTH,
        )
        height = self._integer(
            self.settings.get("height"),
            self.DEFAULT_HEIGHT,
            self.MIN_HEIGHT,
            self.MAX_HEIGHT,
        )
        max_width, max_height = self._resize_limits()
        width = min(width, max_width)
        height = min(height, max_height)
        x = self.settings.get("x")
        y = self.settings.get("y")
        if not isinstance(x, int) or not isinstance(y, int):
            x, y = self._default_position(root, width, height)
        bounds = self._active_monitor_bounds()
        if bounds:
            x, y, width, height = self._fit_geometry_to_bounds(
                x,
                y,
                width,
                height,
                bounds,
                margin=self.WINDOW_EDGE_MARGIN,
            )
        root.geometry(f"{width}x{height}{x:+d}{y:+d}")
        self._expanded_geometry = (x, y, width, height)

    def _default_position(self, root: tk.Tk, width: int, height: int) -> tuple[int, int]:
        pid = self.game_pid()
        hwnd = self.windows.find_main_window(pid) if pid else None
        rect = self.windows.client_rect_on_screen(hwnd) if hwnd else None
        if rect:
            return rect.right - width - 18, rect.top + 18
        return (
            max(0, root.winfo_screenwidth() - width - 36),
            max(0, root.winfo_screenheight() - height - 72),
        )

    def _active_monitor_bounds(self) -> WindowRect | None:
        if not self.root:
            return None
        target: int | None = None
        pid = self.game_pid()
        if pid:
            try:
                target = self.windows.find_main_window(pid)
            except (AttributeError, OSError, ValueError):
                target = None
        target = target or self.hwnd
        monitor_rect = getattr(self.windows, "monitor_rect", None)
        if target and callable(monitor_rect):
            try:
                bounds = monitor_rect(target, work_area=True)
            except (OSError, ValueError):
                bounds = None
            if bounds and bounds.width > 0 and bounds.height > 0:
                return bounds
        return WindowRect(
            0,
            0,
            self.root.winfo_screenwidth(),
            self.root.winfo_screenheight(),
        )

    @staticmethod
    def _fit_geometry_to_bounds(
        x: int,
        y: int,
        width: int,
        height: int,
        bounds: WindowRect,
        *,
        margin: int,
    ) -> tuple[int, int, int, int]:
        available_width = max(1, bounds.width - margin * 2)
        available_height = max(1, bounds.height - margin * 2)
        fitted_width = min(width, available_width)
        fitted_height = min(height, available_height)
        minimum_x = bounds.left + margin
        minimum_y = bounds.top + margin
        maximum_x = max(minimum_x, bounds.right - margin - fitted_width)
        maximum_y = max(minimum_y, bounds.bottom - margin - fitted_height)
        return (
            max(minimum_x, min(maximum_x, x)),
            max(minimum_y, min(maximum_y, y)),
            fitted_width,
            fitted_height,
        )

    def _render_trace(self) -> None:
        self._scene_catalog_open = False
        self._history_open = False
        self._update_history_button()
        if not self.text or not self.gate_panel:
            return
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.insert("end", f"{self._current_interval_title()}\n", "interval")
        self.text.insert(
            "end",
            (
                "只显示这两个节点之间的普通剧情"
                f"  ·  本段 {self.trace.current_dialogue_count} 句\n\n"
            ),
            "interval_meta",
        )
        visible_dialogue = False
        translated = self._visible_translation()
        paragraph_index = 0
        for paragraph in self.trace.current_paragraphs:
            if paragraph.kind == "chapter":
                continue
            visible_dialogue = True
            speaker = translated[paragraph_index]['speaker'] if translated else paragraph.speaker
            content = translated[paragraph_index]['text'] if translated else paragraph.text
            paragraph_index += 1
            if speaker:
                self.text.insert("end", f"{speaker}  ", "speaker")
                self.text.insert("end", f"{content}\n", "dialogue")
            else:
                self.text.insert("end", f"{content}\n", "narration")
        if not visible_dialogue:
            self.text.insert("end", "（这两个节点之间没有普通剧情文本。）\n", "narration")
        self.text.configure(state="disabled")
        self._render_gate(self.trace)
        self._scroll_anchor = "1.0"
        self.text.yview_moveto(0.0)
        self._save_session()
        self._update_title()
        self._update_progress()
        if self._mode == self.MODE_READER and not self._last_error:
            self._set_status(self._reader_status(), self.MUTED)
        self._write_runtime_state()

    def _current_interval_title(self) -> str:
        start = "游戏开头"
        if self.reader.actions:
            action = self.reader.actions[-1]
            if action.get("type") == "scene":
                scene = self.reader.scenes.get(str(action.get("id"))) or {}
                start = str(scene.get("label") or "上一个原版演出")
            elif action.get("type") == "choice":
                start = "上一次路线选择"

        gate = self.trace.gate
        if gate and gate.kind == "scene":
            end = str((gate.detail or {}).get("label") or "下一个原版演出")
        elif gate and gate.kind == "choice":
            end = "下一次路线选择"
        else:
            end = "当前路线结尾"
        return f"{start}  →  {end}"

    def _render_gate(self, trace: NovelTrace) -> None:
        assert self.gate_panel is not None
        self._wrapped_widgets.clear()
        for child in self.gate_panel.winfo_children():
            child.destroy()
        if self._scene_catalog_open:
            self._render_scene_catalog()
            return
        if self._history_open:
            self._render_history_gate()
            return
        gate = trace.gate
        if gate and gate.kind == "choice":
            self._render_choice_gate(gate)
            return
        if gate and gate.kind == "scene":
            self._render_scene_gate(gate)
            return
        message = trace.error or trace.ending or "当前路线已结束。"
        color = self.DANGER if trace.error else self.ACCENT
        self._gate_heading("路线异常" if trace.error else "路线终点", message, color)
        row = tk.Frame(self.gate_panel, bg=self.PANEL)
        row.pack(fill="x", padx=12, pady=(0, 10))
        self._action_button(row, "从头阅读", self._restart).pack(side="left")
        entries = self._route_history_entries()
        if entries:
            self._action_button(
                row,
                "重选上一个选项",
                lambda index=entries[-1]["trace_index"]: self._rewind_to_choice(index),
            ).pack(side="left", padx=(8, 0))

    def _toggle_scene_catalog(self) -> None:
        self._scene_catalog_open = not self._scene_catalog_open
        self._history_open = False
        self._cancel_auto_scene()
        if self._scene_catalog_open:
            self._scene_catalog_page = 0
        self._render_gate(self.trace)
        if not self._scene_catalog_open:
            self._queue_next_scene()

    def _scene_catalog_entries(self) -> list[dict[str, Any]]:
        visited = {str(action['id']): index for index, action in enumerate(self.reader.actions)
                   if action.get('type') == 'scene'}
        current = self.trace.gate
        return [dict(scene, trace_index=visited.get(str(scene['id'])),
                     current=bool(current and current.kind == 'scene' and current.id == str(scene['id'])))
                for scene in self.document.get('scenes', [])]

    def _render_scene_catalog(self) -> None:
        busy = self._playback_busy or self._active_scene_gate is not None
        self._gate_heading('场景目录', '在这里选择，无需操作游戏菜单', self.ACCENT,
                           ('当前演出尚未确认结束，暂不能切换。返回正文可查看结束状态。' if busy else
                            '可返回已走过的场景或继续当前节点；其他线路请先在剧情选项中选择。'))
        entries = self._scene_catalog_entries()
        page_size = 3
        pages = max(1, (len(entries) + page_size - 1) // page_size)
        self._scene_catalog_page = max(0, min(pages - 1, self._scene_catalog_page))
        start = self._scene_catalog_page * page_size
        for entry in entries[start:start + page_size]:
            row = tk.Frame(self.gate_panel, bg=self.PANEL)
            row.pack(fill='x', padx=12, pady=3)
            reachable = entry['current'] or entry['trace_index'] is not None
            status = '当前节点' if entry['current'] else ('已走过' if reachable else '尚未到达')
            tk.Label(row, text=f"{entry.get('label') or entry['id']} · {status}",
                     bg=self.PANEL, fg=self.TEXT, anchor='w').pack(side='left', fill='x', expand=True)
            button = self._action_button(row, '切换中' if self._playback_busy else ('演出中' if busy else ('进入' if reachable else '未到达')),
                                         lambda scene_id=str(entry['id']): self._select_catalog_scene(scene_id))
            button.configure(state='normal' if reachable and not self._active_scene_gate
                             and not self._playback_busy else 'disabled', pady=4)
            button.pack(side='right')
        controls = tk.Frame(self.gate_panel, bg=self.PANEL)
        controls.pack(fill='x', padx=12, pady=8)
        back = self._action_button(controls, '返回正文', self._toggle_scene_catalog)
        back.configure(pady=4)
        back.pack(side='left')
        for text, delta in [('上一页', -1), ('下一页', 1)]:
            button = self._action_button(controls, text, lambda step=delta: self._page_scene_catalog(step))
            button.configure(state='normal' if 0 <= self._scene_catalog_page + delta < pages else 'disabled', pady=4)
            button.pack(side='left', padx=5)
        tk.Label(controls, text=f'{self._scene_catalog_page + 1} / {pages}',
                 bg=self.PANEL, fg=self.MUTED).pack(side='right')

    def _page_scene_catalog(self, delta: int) -> None:
        self._scene_catalog_page += delta
        self._render_gate(self.trace)

    def _select_catalog_scene(self, scene_id: str) -> None:
        if self._playback_busy or self._active_scene_gate:
            self._set_status('当前演出尚未结束，暂不切换线路。', self.MUTED)
            return
        entry = next((item for item in self._scene_catalog_entries() if str(item['id']) == scene_id), None)
        if not entry or not (entry['current'] or entry['trace_index'] is not None):
            return
        if not entry['current']:
            write_json(self.output_dir / 'reader_session.before-scene-select.json', self.reader.session())
            self.trace = self.reader.rewind(entry['trace_index'])
        self._last_error = None
        self._scene_catalog_open = False
        self._awaiting_scene_continue = False  # Explicit catalog selection.
        self._render_trace()
        self._queue_next_scene()

    def _render_history_gate(self) -> None:
        entries = self._route_history_entries()
        scene_count = sum(action.get("type") == "scene" for action in self.reader.actions)
        self._gate_heading(
            "路线记录 · 不使用游戏存档",
            f"已记录 {len(entries)} 次选择 · 已处理 {scene_count} 个 Scene",
            self.ACCENT,
            "点击旧选项右侧的重选按钮，会保留它之前的进度并删除其后的阅读记录。原版游戏保持运行。",
        )

        controls = tk.Frame(self.gate_panel, bg=self.PANEL)
        controls.pack(fill="x", padx=12, pady=(0, 8))
        self._action_button(controls, "返回当前节点", self._toggle_history).pack(
            side="left"
        )
        if entries:
            latest = entries[-1]
            latest_button = self._action_button(
                controls,
                "重选最近选项",
                lambda index=latest["trace_index"]: self._rewind_to_choice(index),
            )
            latest_button.configure(
                bg="#297c6d",
                activebackground="#339985",
                fg="#ffffff",
            )
            latest_button.pack(side="left", padx=(8, 0))

        if not entries:
            empty = tk.Label(
                self.gate_panel,
                text="当前还没有做过路线选择；到达第一个选项后会自动记录。",
                bg=self.PANEL,
                fg=self.MUTED,
                anchor="w",
                font=("Microsoft YaHei UI", 10),
            )
            empty.pack(fill="x", padx=16, pady=(2, 12))
            return

        newest_first = list(reversed(entries))
        page_count = max(
            1,
            (len(newest_first) + self.HISTORY_PAGE_SIZE - 1)
            // self.HISTORY_PAGE_SIZE,
        )
        self._history_page = max(0, min(self._history_page, page_count - 1))
        start = self._history_page * self.HISTORY_PAGE_SIZE
        visible = newest_first[start : start + self.HISTORY_PAGE_SIZE]
        for entry in visible:
            self._render_history_entry(entry, entry is entries[-1])

        if page_count > 1:
            pager = tk.Frame(self.gate_panel, bg=self.PANEL)
            pager.pack(fill="x", padx=12, pady=(0, 9))
            newer = self._action_button(
                pager,
                "较新记录",
                lambda: self._change_history_page(-1),
            )
            newer.configure(state="normal" if self._history_page > 0 else "disabled")
            newer.pack(side="left")
            older = self._action_button(
                pager,
                "较早记录",
                lambda: self._change_history_page(1),
            )
            older.configure(
                state="normal" if self._history_page + 1 < page_count else "disabled"
            )
            older.pack(side="left", padx=(8, 0))
            tk.Label(
                pager,
                text=f"第 {self._history_page + 1} / {page_count} 页",
                bg=self.PANEL,
                fg=self.MUTED,
                font=("Microsoft YaHei UI", 9),
            ).pack(side="right")

    def _render_history_entry(
        self,
        entry: dict[str, Any],
        current: bool,
    ) -> None:
        assert self.gate_panel is not None
        card_bg = "#e2f1e9" if current else self.PANEL_2
        card = tk.Frame(
            self.gate_panel,
            bg=card_bg,
            highlightbackground=self.ACCENT if current else self.BORDER,
            highlightthickness=1,
        )
        card.pack(fill="x", padx=12, pady=(0, 7))
        row = tk.Frame(card, bg=card_bg)
        row.pack(fill="x", padx=10, pady=(7, 3))
        title = tk.Label(
            row,
            text=f"选择 {entry['choice_number']} · {entry['text']}",
            bg=card_bg,
            fg=self.TEXT,
            anchor="w",
            justify="left",
            font=("Microsoft YaHei UI", 10, "bold"),
            wraplength=self._content_wraplength(260),
        )
        title.pack(side="left", fill="x", expand=True)
        self._wrapped_widgets.append((title, 260))
        button = self._action_button(
            row,
            "回到此处重选",
            lambda index=entry["trace_index"]: self._rewind_to_choice(index),
        )
        button.configure(padx=10, pady=5)
        button.pack(side="right", padx=(10, 0))

        details: list[str] = []
        if entry.get("route"):
            details.append(f"攻略路线 {entry['route']}")
        played = int(entry.get("played_scene_count", 0))
        skipped = int(entry.get("skipped_scene_count", 0))
        if played:
            details.append(f"已看 Scene {played} 个")
        if skipped:
            details.append(f"已跳过 Scene {skipped} 个")
        if not played and not skipped:
            details.append("此选择后尚未处理 Scene")
        if current:
            details.append("当前采用的路线")
        meta = tk.Label(
            card,
            text="  ·  ".join(details),
            bg=card_bg,
            fg=self.ACCENT if current else self.MUTED,
            anchor="w",
            justify="left",
            font=("Microsoft YaHei UI", 9),
            wraplength=self._content_wraplength(55),
        )
        meta.pack(fill="x", padx=10, pady=(0, 7))
        self._wrapped_widgets.append((meta, 55))

    def _render_choice_gate(self, gate: NovelGate) -> None:
        detail = gate.detail or {}
        strategy = self._choice_strategy(gate.id)
        kind_label = self._choice_kind_label(detail)
        copy = None
        strategy_status = str(self.document.get("strategy", {}).get("status") or "")
        if strategy_status == "invalid":
            copy = "strategy.json 无效，本次已忽略人工攻略注释。"
        self._gate_heading(
            "下一节点 · 路线选择",
            kind_label,
            self.GOLD,
            copy,
        )

        guide_details = self._strategy_has_details(strategy)
        guide_revealed = gate.id in self._revealed_choice_guides
        if guide_details:
            controls = tk.Frame(self.gate_panel, bg=self.PANEL)
            controls.pack(fill="x", padx=12, pady=(0, 8))
            label = "隐藏攻略详情" if guide_revealed else "显示攻略详情（剧透）"
            self._action_button(
                controls,
                label,
                lambda choice_id=gate.id: self._toggle_choice_guide(choice_id),
            ).pack(side="left")
            if guide_revealed and strategy.get("note"):
                note = tk.Label(
                    controls,
                    text=f"攻略备注：{strategy['note']}",
                    bg=self.PANEL,
                    fg=self.GOLD,
                    anchor="w",
                    justify="left",
                    font=("Microsoft YaHei UI", 9),
                    wraplength=self._content_wraplength(250),
                )
                note.pack(side="left", fill="x", expand=True, padx=(10, 0))
                self._wrapped_widgets.append((note, 250))

        options = list(gate.event.get("options", []))
        branches = {
            str(branch.get("option_id")): branch
            for branch in detail.get("branches", [])
        }
        for index, option in enumerate(options):
            option_id = option.get("id")
            self._render_choice_card(
                index + 1,
                option,
                branches.get(str(option_id)),
                detail,
                self._option_strategy(strategy, option_id),
                guide_revealed,
            )

    def _render_choice_card(
        self,
        number: int,
        option: dict[str, Any],
        branch: dict[str, Any] | None,
        choice: dict[str, Any],
        strategy: dict[str, Any],
        guide_revealed: bool,
    ) -> None:
        assert self.gate_panel is not None
        option_id = option.get("id")
        recommended = strategy.get("recommended") is True
        card_bg = "#e2f1e9" if recommended else self.PANEL_2
        border = self.ACCENT if recommended else self.BORDER
        card = tk.Frame(
            self.gate_panel,
            bg=card_bg,
            highlightbackground=border,
            highlightthickness=1,
        )
        card.pack(fill="x", padx=12, pady=(0, 8))

        header = tk.Frame(card, bg=card_bg)
        header.pack(fill="x", padx=10, pady=(9, 4))
        tk.Label(
            header,
            text=str(number),
            bg=self.GOLD if not recommended else self.ACCENT,
            fg="#ffffff",
            font=("Segoe UI", 9, "bold"),
            width=2,
            pady=2,
        ).pack(side="left", padx=(0, 9))
        option_text = self._translated_choice_text(option)
        title = tk.Label(
            header,
            text=option_text,
            bg=card_bg,
            fg=self.TEXT,
            anchor="w",
            justify="left",
            font=("Microsoft YaHei UI", 11, "bold"),
            wraplength=self._content_wraplength(250),
        )
        title.pack(side="left", fill="x", expand=True)
        self._wrapped_widgets.append((title, 250))

        choose = self._action_button(
            header,
            "选择此路线",
            lambda value=option_id: self._choose(value),
        )
        choose.configure(
            bg=self.ACCENT if recommended else "#567568",
            activebackground="#368c75",
            fg="#ffffff",
            padx=11,
            pady=6,
        )
        choose.pack(side="right", padx=(10, 0))

        strip = tk.Label(
            card,
            text=self._branch_route_strip(branch, choice),
            bg=card_bg,
            fg=self.ACCENT,
            anchor="w",
            justify="left",
            font=("Microsoft YaHei UI", 9, "bold"),
            wraplength=self._content_wraplength(55),
        )
        strip.pack(fill="x", padx=12, pady=(1, 2))
        self._wrapped_widgets.append((strip, 55))

        preview = self._branch_preview(branch)
        if preview:
            meta = tk.Label(
                card,
                text=preview,
                bg=card_bg,
                fg=self.MUTED,
                anchor="w",
                justify="left",
                font=("Microsoft YaHei UI", 9),
                wraplength=self._content_wraplength(55),
            )
            meta.pack(fill="x", padx=12, pady=(0, 8 if not guide_revealed else 3))
            self._wrapped_widgets.append((meta, 55))

        if recommended:
            marker = tk.Label(
                card,
                text="★ 攻略推荐",
                bg=card_bg,
                fg=self.GOLD,
                anchor="w",
                font=("Microsoft YaHei UI", 9, "bold"),
            )
            marker.pack(fill="x", padx=12, pady=(0, 6))
        if guide_revealed:
            guide = self._strategy_summary(strategy)
            if guide:
                guide_label = tk.Label(
                    card,
                    text=f"攻略：{guide}",
                    bg=card_bg,
                    fg=self.GOLD,
                    anchor="w",
                    justify="left",
                    font=("Microsoft YaHei UI", 9),
                    wraplength=self._content_wraplength(55),
                )
                guide_label.pack(fill="x", padx=12, pady=(0, 8))
                self._wrapped_widgets.append((guide_label, 55))

    def _translated_choice_text(self, option: dict[str, Any]) -> str:
        """Use an imported local translation for a choice label when present."""
        original = str(option.get("text") or "（无选项文本）")
        if not original or not getattr(self, '_translation_library', None):
            return original
        try:
            rows = [dict(speaker='', text=original)]
            value = self._translation_library.get(rows, self._translation_config.get('target', '简体中文'))
            return value[0].get('text') if value and value[0].get('text') else original
        except Exception:
            return original

    def _render_scene_gate(self, gate: NovelGate) -> None:
        detail = gate.detail or {}
        label = str(detail.get("label") or "Scene")
        replay_key = detail.get("replay_key")
        title = f"到达 {label}"
        if replay_key is not None:
            title += f"  ·  #{replay_key}"
        speakers = " · ".join(
            str(value) for value in (detail.get("speakers") or [])[:6]
        )
        active = self._active_scene_gate is not None
        if active and not self._playback_busy:
            # Playback status and manual fallback live in the subtitle header.
            return
        copy = speakers or "这两个节点之间的完整会话已在上方展开。"
        copy += " 读完后点击继续，按当前线路进入下一段演出；不会倒计时自动播放。"
        self._gate_heading(
            "正在衔接" if self._playback_busy else ("原版演出中" if active else "按线路继续"),
            title, self.ACCENT, copy,
        )
        row = tk.Frame(self.gate_panel, bg=self.PANEL)
        row.pack(fill="x", padx=12, pady=(0, 10))
        if active:
            if not self._playback_busy:
                self._action_button(
                    row,
                    "确认演出已结束" if self._scene_return_armed else "未检测到结束？手动确认",
                    self._request_scene_return,
                ).pack(side="left")
        elif self._last_error:
            self._action_button(row, "重试衔接", self._play_scene).pack(side="left")
        else:
            self._action_button(row, "继续下一段演出", self._play_scene).pack(side="left")

    def _gate_heading(
        self,
        kicker: str,
        title: str,
        color: str,
        copy: str | None = None,
    ) -> None:
        assert self.gate_panel is not None
        tk.Label(
            self.gate_panel,
            text=kicker,
            bg=self.PANEL,
            fg=color,
            anchor="w",
            font=("Segoe UI", 8, "bold"),
        ).pack(fill="x", padx=12, pady=(10, 1))
        title_label = tk.Label(
            self.gate_panel,
            text=title,
            bg=self.PANEL,
            fg=self.TEXT,
            anchor="w",
            justify="left",
            font=("Microsoft YaHei UI", 12, "bold"),
            wraplength=self._content_wraplength(70),
        )
        title_label.pack(fill="x", padx=16, pady=(0, 4))
        self._wrapped_widgets.append((title_label, 70))
        if copy:
            copy_label = tk.Label(
                self.gate_panel,
                text=copy,
                bg=self.PANEL,
                fg=self.MUTED,
                anchor="w",
                justify="left",
                wraplength=self._content_wraplength(70),
                font=("Microsoft YaHei UI", 10),
            )
            copy_label.pack(fill="x", padx=16, pady=(0, 10))
            self._wrapped_widgets.append((copy_label, 70))

    def _action_button(
        self,
        parent: tk.Widget,
        text: str,
        command: Callable[[], None],
        *,
        anchor: str = "center",
    ) -> tk.Button:
        return tk.Button(
            parent,
            text=text,
            command=command,
            takefocus=False,
            anchor=anchor,
            justify="left",
            relief="flat",
            borderwidth=0,
            highlightthickness=0,
            bg=self.PANEL_2,
            activebackground="#dcece3",
            fg=self.TEXT,
            activeforeground=self.TEXT,
            font=("Microsoft YaHei UI", 10),
            padx=14,
            pady=9,
            cursor="hand2",
        )

    def _content_wraplength(self, margin: int) -> int:
        width = self.DEFAULT_WIDTH
        if self.root and self.root.winfo_width() > 1:
            width = self.root.winfo_width()
        elif self._expanded_geometry:
            width = self._expanded_geometry[2]
        return max(320, width - margin)

    def _update_wrapped_controls(self) -> None:
        for widget, margin in list(self._wrapped_widgets):
            try:
                widget.configure(wraplength=self._content_wraplength(margin))
            except tk.TclError:
                self._wrapped_widgets.remove((widget, margin))

    def _branch_preview(self, branch: dict[str, Any] | None) -> str:
        if not branch:
            return "暂时无法分析这条分支"
        analysis = branch.get("analysis")
        if isinstance(analysis, dict):
            parts: list[str] = []
            node_count = int(analysis.get("node_count", 0))
            dialogue_count = int(analysis.get("dialogue_count", 0))
            scene_count = int(analysis.get("scene_count", 0))
            if node_count:
                parts.append(f"{node_count} 个剧情段")
            if dialogue_count:
                parts.append(f"约 {dialogue_count} 句")
            parts.append(f"{scene_count} 个 Scene")
            speakers = [
                str(item.get("name"))
                for item in analysis.get("top_speakers", [])
                if isinstance(item, dict) and item.get("name")
            ]
            if speakers:
                parts.append("主要角色 " + " / ".join(speakers[:3]))
            if analysis.get("path_truncated"):
                parts.append("仅统计当前可确认范围")
            return "  ·  ".join(parts)

        nodes = branch.get("nodes", [])
        labels: list[str] = []
        scene_count = 0
        for node in nodes[:4]:
            label = str(node.get("label") or "")
            if label.casefold().endswith(".ws2"):
                label = f"剧情 {int(node.get('order', 0)) + 1:03d}"
            if label:
                labels.append(label)
            scene_count += len(node.get("scene_ids", []))
        preview = " → ".join(labels)
        if len(nodes) > 4:
            preview += " → …"
        if scene_count:
            preview += f"  ·  原版演出节点 {scene_count} 个"
        return preview

    def _branch_route_strip(
        self,
        branch: dict[str, Any] | None,
        choice: dict[str, Any],
    ) -> str:
        if not branch:
            return "当前选项  →  待分析"
        analysis = branch.get("analysis")
        choice_kind = str(choice.get("analysis", {}).get("kind") or "")
        if not isinstance(analysis, dict):
            return "当前选项  →  " + (self._branch_preview(branch) or "后续剧情")

        pieces = ["当前选项"]
        dialogue_count = int(analysis.get("dialogue_count", 0))
        if dialogue_count:
            pieces.append(f"{dialogue_count} 句剧情")
        next_gate = analysis.get("next_gate", {})
        if isinstance(next_gate, dict):
            gate_kind = str(next_gate.get("kind") or "")
            if gate_kind == "scene":
                pieces.append(str(next_gate.get("label") or "Scene"))
            elif gate_kind == "choice":
                pieces.append("下一次选择")
            elif gate_kind == "merge":
                pieces.append("回到共同剧情")
            elif gate_kind == "ending":
                pieces.append("路线结尾")
            elif gate_kind in {"cycle", "limit"}:
                pieces.append(str(next_gate.get("label") or "后续剧情"))
        if choice_kind == "route_split" and pieces[-1] != "路线结尾":
            pieces.append("独立路线")
        return "  →  ".join(pieces)

    def _choice_kind_label(self, choice: dict[str, Any]) -> str:
        kind = str(choice.get("analysis", {}).get("kind") or "")
        return {
            "short_merge": "短分支，选择后很快回到共同剧情",
            "merged_branch": "较长分支，后续会重新汇合",
            "route_split": "正式路线分歧，分支未检测到汇合",
            "single_path": "单一路线确认",
        }.get(kind, "分支结构尚未完整分析")

    def _choice_strategy(self, choice_id: str) -> dict[str, Any]:
        choices = self.document.get("strategy", {}).get("choices", {})
        if not isinstance(choices, dict):
            return {}
        strategy = choices.get(str(choice_id), {})
        return strategy if isinstance(strategy, dict) else {}

    def _route_history_entries(self) -> list[dict[str, Any]]:
        actions = self.reader.actions
        entries: list[dict[str, Any]] = []
        choice_number = 0
        for index, action in enumerate(actions):
            if action.get("type") != "choice":
                continue
            choice_number += 1
            choice_id = str(action.get("id") or "")
            option_id = action.get("option_id")
            choice = self.reader.choices.get(choice_id) or {}
            branch = next(
                (
                    item
                    for item in choice.get("branches", [])
                    if str(item.get("option_id")) == str(option_id)
                ),
                {},
            )
            strategy = self._option_strategy(
                self._choice_strategy(choice_id),
                option_id,
            )
            following_scenes: list[dict[str, Any]] = []
            for following in actions[index + 1 :]:
                if following.get("type") == "choice":
                    break
                if following.get("type") == "scene":
                    following_scenes.append(following)
            entries.append(
                {
                    "trace_index": index,
                    "choice_number": choice_number,
                    "choice_id": choice_id,
                    "option_id": option_id,
                    "text": self._choice_option_text(
                        choice_id,
                        option_id,
                        branch,
                    ),
                    "route": str(strategy.get("route") or "").strip(),
                    "played_scene_count": sum(
                        scene.get("status") == "played" for scene in following_scenes
                    ),
                    "skipped_scene_count": sum(
                        scene.get("status") == "skipped" for scene in following_scenes
                    ),
                }
            )
        return entries

    def _choice_option_text(
        self,
        choice_id: str,
        option_id: Any,
        branch: dict[str, Any],
    ) -> str:
        branch_text = str(branch.get("text") or "").strip()
        if branch_text:
            return branch_text
        for segment in self.document.get("segments", []):
            for event in segment.get("events", []):
                if event.get("kind") != "choice" or str(event.get("id")) != choice_id:
                    continue
                option = next(
                    (
                        item
                        for item in event.get("options", [])
                        if str(item.get("id")) == str(option_id)
                    ),
                    {},
                )
                text = str(option.get("text") or "").strip()
                return text or "（无选项文本）"
        return "（无选项文本）"

    @staticmethod
    def _option_strategy(
        strategy: dict[str, Any],
        option_id: Any,
    ) -> dict[str, Any]:
        options = strategy.get("options", {})
        if not isinstance(options, dict):
            return {}
        option = options.get(str(option_id), {})
        return option if isinstance(option, dict) else {}

    def _strategy_has_details(self, strategy: dict[str, Any]) -> bool:
        if str(strategy.get("note") or "").strip():
            return True
        options = strategy.get("options", {})
        if not isinstance(options, dict):
            return False
        return any(
            self._strategy_summary(option)
            for option in options.values()
            if isinstance(option, dict)
        )

    @staticmethod
    def _strategy_summary(strategy: dict[str, Any]) -> str:
        parts: list[str] = []
        route = str(strategy.get("route") or "").strip()
        result = str(strategy.get("result") or "").strip()
        ending = str(strategy.get("ending") or "").strip()
        tags = strategy.get("tags", [])
        if route:
            parts.append(f"路线 {route}")
        if result:
            parts.append(result)
        if ending:
            parts.append(f"结局 {ending}")
        if isinstance(tags, list):
            parts.extend(str(tag).strip() for tag in tags if str(tag).strip())
        return "  ·  ".join(parts)

    def _toggle_choice_guide(self, choice_id: str) -> None:
        if choice_id in self._revealed_choice_guides:
            self._revealed_choice_guides.remove(choice_id)
        else:
            self._revealed_choice_guides.add(choice_id)
        gate = self.trace.gate
        if gate and gate.kind == "choice" and gate.id == choice_id:
            self._render_gate(self.trace)

    def _toggle_history(self) -> None:
        if self._playback_busy or self._active_scene_gate or self._mode != self.MODE_READER:
            return
        self._history_open = not self._history_open
        self._scene_catalog_open = False
        if self._history_open:
            self._history_page = 0
        self._render_gate(self.trace)
        self._update_history_button()
        self._update_title()
        if self._history_open:
            self._set_status(
                "路线记录已展开 · 只回退阅读进度，不读取或写入游戏存档",
                self.ACCENT,
            )
        else:
            self._set_status(self._reader_status(), self.MUTED)
            self._queue_next_scene()
        self._write_runtime_state()

    def _change_history_page(self, delta: int) -> None:
        if not self._history_open:
            return
        entries = self._route_history_entries()
        page_count = max(
            1,
            (len(entries) + self.HISTORY_PAGE_SIZE - 1) // self.HISTORY_PAGE_SIZE,
        )
        self._history_page = max(
            0,
            min(page_count - 1, self._history_page + delta),
        )
        self._render_gate(self.trace)

    def _rewind_to_choice(self, trace_index: int) -> None:
        if self._playback_busy or self._active_scene_gate or self._mode != self.MODE_READER:
            return
        if (
            isinstance(trace_index, bool)
            or not isinstance(trace_index, int)
            or not 0 <= trace_index < len(self.reader.actions)
        ):
            self._set_status("无法返回：路线记录位置无效", self.DANGER)
            return
        action = self.reader.actions[trace_index]
        if action.get("type") != "choice":
            self._set_status("无法返回：目标不是路线选择", self.DANGER)
            return

        self._backup_session_before_rewind(trace_index)
        self._cancel_auto_scene()
        expected_id = str(action.get("id") or "")
        self.trace = self.reader.rewind(trace_index)
        self._active_scene_gate = None
        self._scene_dispatched = False
        self._scene_result = None
        self._scene_label = None
        self._last_error = None
        self._history_open = False
        self._history_page = 0
        self._save_session()
        self._render_trace()
        if (
            self.trace.gate
            and self.trace.gate.kind == "choice"
            and self.trace.gate.id == expected_id
        ):
            self._set_status(
                "已返回旧选项 · 请选择另一条路线；原版游戏没有重启",
                self.ACCENT,
            )
        else:
            self._set_status(
                "路线已回退，但剧情图未能重新定位到原选项",
                self.DANGER,
            )

    def _backup_session_before_rewind(self, trace_index: int) -> None:
        write_json(
            self.rewind_backup_path,
            {
                "version": self.SESSION_VERSION,
                "fingerprint": self.fingerprint,
                "reason": "before_route_rewind",
                "rewind_trace_index": trace_index,
                "actions": list(self.reader.actions),
            },
        )

    def _choose(self, option_id: Any) -> None:
        gate = self.trace.gate
        if not gate or gate.kind != "choice" or self._playback_busy:
            return
        try:
            self.trace = self.reader.choose(gate, option_id)
        except ValueError as exc:
            self._last_error = str(exc)
            self._set_status(str(exc), self.DANGER)
            return
        self._last_error = None
        self._render_trace()
        self._queue_next_scene()

    def _skip_scene(self) -> None:
        gate = self.trace.gate
        if not gate or gate.kind != "scene" or self._playback_busy or self._active_scene_gate:
            return
        self.trace = self.reader.finish_scene(gate, "skipped")
        self._render_trace()
        self._queue_next_scene()

    def _play_scene(self) -> None:
        gate = self.trace.gate
        if not gate or gate.kind != "scene" or self._playback_busy or self._active_scene_gate:
            return
        self._cancel_auto_scene()
        self._awaiting_scene_continue = False
        self._playback_busy = True
        self._last_error = None
        self._scene_label = str((gate.detail or {}).get("label") or "Scene")
        self._active_scene_gate = gate
        self._scene_dispatched = False
        self._scene_result = None
        self._clear_scene_confirmations()
        # Dispatch is playback state, not window visibility. Keep the interval
        # text available while the game runs; only the user hides the reader.
        if self.gate_panel:
            self._render_gate(self.trace)
        self._set_status(f"正在切换到 {self._scene_label}……", self.GOLD)
        self._update_mode_controls()
        self._write_runtime_state()

        def worker() -> None:
            try:
                result = self.perform_action("scene_replay", {"scene_id": gate.id})
                self._events.put(("scene_ok", (gate, result)))
            except BaseException as exc:
                self._events.put(("scene_error", (gate, str(exc))))

        threading.Thread(
            target=worker,
            name="native-overlay-scene-dispatch",
            daemon=True,
        ).start()

    def _scene_succeeded(self, gate: NovelGate, result: dict[str, Any]) -> None:
        if self.playback_status:
            try:
                playback=self.playback_status()
            except Exception:
                playback={}
            if playback.get('running') is False:
                self._scene_failed(gate,playback.get('error') or '游戏已退出，未完成演出；进度已保留，请重新启动阅读器。')
                return
        self._playback_busy = False
        self._active_scene_gate = gate
        self._scene_dispatched = True
        self._scene_result = result
        self._clear_scene_confirmations()
        if result.get("completion_detection") == "unavailable":
            status = (
                f"{self._scene_label or 'Scene'} 正在播放 · "
                "未取得自动结束信号，结束后请使用右侧手动确认"
            )
        else:
            status = (
                f"{self._scene_label or 'Scene'} 正在原版游戏中播放 · "
                "等待引擎结束信号；结束后展开下一段正文，点击继续才播放"
            )
        self._set_status(status, self.ACCENT)
        self._update_mode_controls()
        if self.gate_panel:
            self._render_gate(self.trace)
        self._update_title()
        self._write_runtime_state()
        if self._closing_after_playback:
            self.close()

    def _scene_failed(self, _gate: NovelGate, error: str) -> None:
        self._cancel_auto_scene()
        self._playback_busy = False
        self._last_error = error
        self._active_scene_gate = None
        self._scene_dispatched = False
        self._scene_result = None
        self._clear_scene_confirmations()
        self._set_mode(self.MODE_READER)
        self._set_status(f"Scene 切换失败：{error}", self.DANGER)
        if self.gate_panel:
            self._render_gate(self.trace)
        self._write_runtime_state()
        if self._closing_after_playback:
            self.close()

    def _restart(self) -> None:
        if self._playback_busy or self._active_scene_gate:
            return
        self._active_scene_gate = None
        self._scene_dispatched = False
        self._scene_result = None
        self._scene_label = None
        self._clear_scene_confirmations()
        self._pending_auto_scene_id = None
        self._last_error = None
        self._history_open = False
        self._history_page = 0
        self.trace = self.reader.restart()
        self._render_trace()
        self._queue_next_scene()

    def _toggle_visibility(self) -> None:
        """Collapse to a persistent eye tab or restore the previous layout."""

        if not self.root or not self.title_content or not self.body:
            return
        self._disable_click_through()
        if self._visibility_collapsed:
            self._restore_from_visibility_strip()
        else:
            self._collapse_to_visibility_strip()

    def _collapse_to_visibility_strip(self) -> None:
        if (
            not self.root
            or not self.title_content
            or not self.body
            or self._visibility_collapsed
        ):
            return
        self.root.update_idletasks()
        geometry = (
            self.root.winfo_x(),
            self.root.winfo_y(),
            self.root.winfo_width(),
            self.root.winfo_height(),
        )
        self._visibility_restore_geometry = geometry
        if self._mode == self.MODE_READER:
            self._remember_expanded_geometry()

        x, y, width, _height = geometry
        # Use the rendered button width (including DPI scaling) so its screen
        # position stays fixed when the rest of the window disappears.
        button_width = (
            self.visibility_button.winfo_width()
            if self.visibility_button else self.VISIBILITY_STRIP_WIDTH - 2
        )
        strip_width = max(1, button_width) + 2
        x += width - strip_width
        strip_height = self.VISIBILITY_STRIP_HEIGHT
        bounds = self._active_monitor_bounds()
        if bounds:
            x, y, strip_width, strip_height = self._fit_geometry_to_bounds(
                x,
                y,
                strip_width,
                strip_height,
                bounds,
                margin=self.WINDOW_EDGE_MARGIN,
            )

        self._visibility_collapsed = True
        self.title_content.pack_forget()
        self.body.pack_forget()
        self._set_resize_handles_visible(False)
        if self.title_bar:
            self.title_bar.configure(height=self.TITLE_HEIGHT)
        self.root.geometry(f"{strip_width}x{strip_height}{x:+d}{y:+d}")
        if self.visibility_button:
            self.visibility_button.configure(fg=self.ACCENT)
        # Tk queues geometry changes. Finish the resize before Win32's
        # FRAMECHANGED notification can report the previous window bounds.
        self.root.update_idletasks()
        self._reapply_native_style()
        self._write_runtime_state()

    def _restore_from_visibility_strip(self) -> None:
        if (
            not self.root
            or not self.title_content
            or not self.body
            or not self._visibility_collapsed
        ):
            return
        if self._mode == self.MODE_READER:
            x, y, width, height = self._expanded_geometry or (
                self._visibility_restore_geometry
                or (0, 0, self.DEFAULT_WIDTH, self.DEFAULT_HEIGHT)
            )
        else:
            x, y, width, _height = self._expanded_geometry or (
                self._visibility_restore_geometry
                or (0, 0, self.DEFAULT_WIDTH, self.DEFAULT_HEIGHT)
            )
            width = max(660, min(width, 980))
            height = self.SCENE_STRIP_HEIGHT
        bounds = self._active_monitor_bounds()
        if bounds:
            x, y, width, height = self._fit_geometry_to_bounds(
                x,
                y,
                width,
                height,
                bounds,
                margin=self.WINDOW_EDGE_MARGIN,
            )
        self._visibility_collapsed = False
        if self.title_bar:
            self.title_bar.configure(height=self.TITLE_HEIGHT)
        if self.visibility_button:
            self.visibility_button.place_forget()
            self.visibility_button.pack(side="right", fill="y")
        self.root.geometry(f"{width}x{height}{x:+d}{y:+d}")
        self.title_content.pack(side="left", fill="both", expand=True)
        if self._mode == self.MODE_READER:
            self.body.pack(fill="both", expand=True)
            self._set_resize_handles_visible(True)
            self._expanded_geometry = (x, y, width, height)
        else:
            self._set_resize_handles_visible(False)
        self.root.update_idletasks()
        self._reapply_native_style()
        self._update_mode_controls()
        self._update_title()
        self._write_runtime_state()

    def _toggle_mode(self) -> None:
        # Opening the text window must never acknowledge playback completion.
        self._set_mode(self.MODE_READER if self._mode == self.MODE_SCENE else self.MODE_SCENE)

    def _request_scene_return(self) -> None:
        """Require an explicit two-step acknowledgement before advancing.

        Dispatching another controller-menu key path while the previous Scene
        is still consuming input advances dialogue instead of selecting the
        requested replay item.  A single ambiguous "return" click therefore
        must never mark the Scene complete.
        """

        if self._playback_busy:
            return
        if not self._active_scene_gate or not self._scene_dispatched:
            self._return_to_reader()
            return
        now = time.monotonic()
        if not self._scene_return_armed:
            self._scene_return_armed = True
            self._scene_return_armed_at = now
            self._sync_reset_armed = False
            self._sync_reset_armed_at = None
            self._update_mode_controls()
            if self.gate_panel:
                self._render_gate(self.trace)
            self._update_title()
            self._write_runtime_state()
            return
        armed_at = (
            self._scene_return_armed_at
            if self._scene_return_armed_at is not None
            else now
        )
        if now - armed_at < self.CONFIRM_DELAY_SECONDS:
            return
        self._return_to_reader(auto_chain=True)

    def _request_sync_reset(self) -> None:
        """Recover from a reader/game desync without touching the original game."""

        if self._playback_busy or not self._active_scene_gate:
            return
        now = time.monotonic()
        if not self._sync_reset_armed:
            self._sync_reset_armed = True
            self._sync_reset_armed_at = now
            self._scene_return_armed = False
            self._scene_return_armed_at = None
            self._update_mode_controls()
            self._update_title()
            self._write_runtime_state()
            return
        armed_at = (
            self._sync_reset_armed_at
            if self._sync_reset_armed_at is not None
            else now
        )
        if now - armed_at < self.CONFIRM_DELAY_SECONDS:
            return
        self._reset_desynced_session()

    def _reset_desynced_session(self) -> None:
        """Back up and clear route progress, then end the engine session safely."""

        self._cancel_auto_scene()
        write_json(
            self.output_dir / "reader_session.before-sync-reset.json",
            {
                "version": self.SESSION_VERSION,
                "reason": "before_scene_sync_reset",
                "fingerprint": self.fingerprint,
                "actions": self.reader.session()["actions"],
            },
        )
        self.trace = self.reader.restart()
        self._save_session()
        self._active_scene_gate = None
        self._scene_dispatched = False
        self._scene_result = None
        self._scene_label = None
        self._clear_scene_confirmations()
        self._last_error = "Scene 同步已重置；重新启动后从头选择路线。"
        self._write_runtime_state()
        self.close()

    def _clear_scene_confirmations(self) -> None:
        self._scene_return_armed = False
        self._scene_return_armed_at = None
        self._sync_reset_armed = False
        self._sync_reset_armed_at = None

    def _automatic_completion_available(self) -> bool:
        return bool(
            self._scene_result
            and self._scene_result.get("completion_detection") != "unavailable"
        )

    def _poll_playback_completion(self) -> None:
        if (
            not self.playback_status
            or not self._active_scene_gate
            or not self._scene_dispatched
        ):
            return
        try:
            playback = self.playback_status()
        except Exception:
            return
        if playback.get("phase") == "failed" or (
            playback.get("running") is False
        ):
            gate = self._active_scene_gate
            error = playback.get("error") or (
                "游戏已退出，本次进入未完成；阅读进度已保留。请重新启动阅读器。"
            )
            self._scene_failed(gate, str(error))
            if self._visibility_collapsed:
                self._restore_from_visibility_strip()
            return
        if playback.get("phase") != "scene_complete":
            return
        replay_key = (self._active_scene_gate.detail or {}).get("replay_key")
        try:
            completed_key = int(playback.get("last_scene"))
            expected_key = int(replay_key)
        except (TypeError, ValueError):
            return
        if completed_key != expected_key:
            return
        completed_label = self._scene_label or "Scene"
        self._return_to_reader(auto_chain=True)
        if self._last_error:
            self._set_status(self._last_error, self.DANGER)
            return
        gate = self.trace.gate
        if gate and gate.kind == "scene":
            next_label = str((gate.detail or {}).get("label") or "下一个 Scene")
            message = f"{completed_label} 已结束 · 到 {next_label} 的会话已展开，读完后点击继续"
        elif gate and gate.kind == "choice":
            message = f"{completed_label} 已结束 · 已到下一个路线选择"
        else:
            message = f"{completed_label} 已结束 · 当前路线已读完"
        self._set_status(message, self.ACCENT)

    def _set_mode(self, mode: str) -> None:
        if mode not in {self.MODE_READER, self.MODE_SCENE}:
            raise ValueError("native overlay mode must be reader or scene")
        if mode == self.MODE_SCENE:
            self._history_open = False
            self._update_history_button()
        if not self.root or not self.body:
            self._mode = mode
            return
        if mode == self._mode:
            self._update_mode_controls()
            self._update_title()
            return
        self._disable_click_through()
        if self._visibility_collapsed:
            self._mode = mode
            self._update_mode_controls()
            self._update_title()
            self._write_runtime_state()
            return
        if mode == self.MODE_SCENE:
            self._remember_expanded_geometry()
            self._set_resize_handles_visible(False)
            self.body.pack_forget()
            x, y, width, _height = self._expanded_geometry or (
                0,
                0,
                self.DEFAULT_WIDTH,
                self.DEFAULT_HEIGHT,
            )
            width = max(660, min(width, 980))
            strip_height = self.SCENE_STRIP_HEIGHT
            bounds = self._active_monitor_bounds()
            if bounds:
                x, y, width, strip_height = self._fit_geometry_to_bounds(
                    x,
                    y,
                    width,
                    strip_height,
                    bounds,
                    margin=self.WINDOW_EDGE_MARGIN,
                )
            self.root.geometry(
                f"{width}x{strip_height}{x:+d}{y:+d}"
            )
        else:
            x, y, width, height = self._expanded_geometry or (
                0,
                0,
                self.DEFAULT_WIDTH,
                self.DEFAULT_HEIGHT,
            )
            bounds = self._active_monitor_bounds()
            if bounds:
                x, y, width, height = self._fit_geometry_to_bounds(
                    x,
                    y,
                    width,
                    height,
                    bounds,
                    margin=self.WINDOW_EDGE_MARGIN,
                )
                self._expanded_geometry = (x, y, width, height)
            self.root.geometry(f"{width}x{height}{x:+d}{y:+d}")
            self.body.pack(fill="both", expand=True)
            self._set_resize_handles_visible(True)
            if self.text and self._scroll_anchor:
                self.text.see(self._scroll_anchor)
        self._mode = mode
        self._update_mode_controls()
        self._reapply_native_style()
        self._update_title()
        self._write_runtime_state()

    def _return_to_reader(self, *, auto_chain: bool = False) -> None:
        """Leave the game handoff and only then commit the Scene as viewed."""

        if self._playback_busy:
            return
        gate = self._active_scene_gate
        advanced = False
        if (
            gate
            and self._scene_dispatched
            and self.trace.gate
            and self.trace.gate.kind == "scene"
            and self.trace.gate.id == gate.id
        ):
            self.trace = self.reader.finish_scene(gate, "played")
            advanced = True
            self._awaiting_scene_continue = True
            self._cancel_auto_scene()
        self._active_scene_gate = None
        self._scene_dispatched = False
        self._scene_result = None
        self._scene_label = None
        self._clear_scene_confirmations()
        if advanced and self.on_reader_resumed:
            try:
                self.on_reader_resumed()
            except Exception as exc:
                self._last_error = f"reader resume failed: {exc}"
        self._set_mode(self.MODE_READER)
        if advanced and self._visibility_collapsed:
            self._restore_from_visibility_strip()
        if advanced:
            self._render_trace()
        else:
            self._update_title()
            self._set_status(self._reader_status(), self.MUTED)
            self._write_runtime_state()
        # Completion reveals the next interval, but never starts its playback.
        # The pause also survives catalog open/close and route-choice rendering.

    def _cancel_auto_scene(self) -> None:
        self._auto_scene_generation += 1
        self._pending_auto_scene_id = None

    def _queue_next_scene(self) -> None:
        self._cancel_auto_scene()
        gate = self.trace.gate
        if (not gate or gate.kind != "scene" or self._playback_busy
                or self._awaiting_scene_continue
                or self._active_scene_gate or self._last_error or self.trace.error
                or self._history_open or self._scene_catalog_open):
            return
        self._pending_auto_scene_id = gate.id
        generation = self._auto_scene_generation
        if self.root:
            # Yield to Tk's paint cycle, not a reading-duration timer. Dispatch
            # does not collapse or erase the freshly rendered interval.
            self.root.after_idle(lambda: self._play_pending_scene(generation))

    def _play_pending_scene(self, generation: int) -> None:
        if generation != self._auto_scene_generation:
            return
        gate = self.trace.gate
        if (
            not self._pending_auto_scene_id
            or not gate
            or gate.kind != "scene"
            or gate.id != self._pending_auto_scene_id
            or self._playback_busy
            or self._active_scene_gate
            or self._last_error
            or self._history_open
            or self._scene_catalog_open
        ):
            self._pending_auto_scene_id = None
            return
        self._play_scene()

    def _toggle_click_through(self) -> None:
        if not self.hwnd:
            return
        if self._click_through:
            self._disable_click_through()
            return
        self._click_through = True
        self.windows.set_click_through(self.hwnd, True)
        self._update_lock_label()
        self._write_runtime_state()
        if self.root:
            if self._click_through_after:
                self.root.after_cancel(self._click_through_after)
            self._click_through_after = self.root.after(
                self.CLICK_THROUGH_SECONDS * 1000,
                self._disable_click_through,
            )

    def _disable_click_through(self) -> None:
        if self.root and self._click_through_after:
            try:
                self.root.after_cancel(self._click_through_after)
            except tk.TclError:
                pass
        self._click_through_after = None
        if self.hwnd and self._click_through:
            self.windows.set_click_through(self.hwnd, False)
        self._click_through = False
        self._update_lock_label()
        self._write_runtime_state()

    def _update_lock_label(self) -> None:
        if self.lock_button:
            self.lock_button.configure(
                text="正在穿透…" if self._click_through else "穿透 8 秒"
            )

    def _cycle_opacity(self) -> None:
        if not self.root:
            return
        levels = (0.72, 0.84, 0.94, 1.0)
        closest = min(range(len(levels)), key=lambda index: abs(levels[index] - self._opacity))
        self._opacity = levels[(closest + 1) % len(levels)]
        self.root.attributes("-alpha", self._opacity)
        self._reapply_native_style()
        self._set_status(f"透明度 {round(self._opacity * 100)}%", self.MUTED)
        self._schedule_save()

    def _update_title(self) -> None:
        if not self.title_label:
            return
        title = str(self.document.get("game", {}).get("title") or self.TITLE)
        gate = self.trace.gate
        if self._mode == self.MODE_SCENE:
            if self._playback_busy:
                suffix = self._scene_label or "Scene"
                copy = f"正在进入 {suffix}……"
            elif self._active_scene_gate and self._scene_dispatched:
                suffix = self._scene_label or "Scene"
                if self._sync_reset_armed:
                    copy = "将清空阅读进度并安全退出 · 再点一次“确认重置”"
                elif self._scene_return_armed:
                    copy = "请确认游戏已回到 Scene 分组菜单 · 确认后才推进"
                elif self._automatic_completion_available():
                    copy = f"{suffix} · 正在播放 · 结束后自动呈现下一段正文"
                else:
                    copy = f"{suffix} · 未取得结束信号，可展开阅读使用手动确认"
            else:
                copy = "阅读框已收起 · 点击右侧展开阅读"
            self.title_label.configure(text=copy)
        elif self._active_scene_gate:
            label = self._scene_label or "原版演出"
            self.title_label.configure(text=f"剧情加速器 · {label} 播放中")
        elif self._history_open:
            self.title_label.configure(text=f"{title} · 路线记录与重选")
        elif gate and gate.kind == "choice":
            self.title_label.configure(text=f"{title} · 等待路线选择")
        elif gate and gate.kind == "scene":
            label = str((gate.detail or {}).get("label") or "Scene")
            self.title_label.configure(text=f"{title} · {label}")
        else:
            self.title_label.configure(text=f"{title} · 当前路线已读完")

    def _update_mode_controls(self) -> None:
        active = bool(self._active_scene_gate)
        if self.sync_button:
            if active and not self.sync_button.winfo_manager():
                self.sync_button.pack(side="right")
            elif not active and self.sync_button.winfo_manager():
                self.sync_button.pack_forget()
            self.sync_button.configure(
                text="确认重置" if self._sync_reset_armed else "同步异常",
                state="disabled" if self._playback_busy else "normal",
            )
        if self._mode == self.MODE_SCENE:
            if self.mode_badge:
                self.mode_badge.configure(text="SCENE", fg=self.GOLD)
            if self.mode_button:
                if not self.mode_button.winfo_manager():
                    self.mode_button.pack(side="right")
                self.mode_button.configure(text="展开阅读", state="normal")
            self._update_history_button()
            return
        if self.mode_badge:
            self.mode_badge.configure(text="同步阅读" if active else "阅读", fg=self.ACCENT)
        if self.mode_button:
            self.mode_button.configure(text="收起", state="normal")
            if self.mode_button.winfo_manager():
                self.mode_button.pack_forget()
        self._update_history_button()

    def _update_history_button(self) -> None:
        if not self.history_button:
            return
        self.history_button.configure(
            text="返回正文" if self._history_open else "路线记录",
            state=(
                "disabled"
                if self._playback_busy or self._active_scene_gate or self._mode == self.MODE_SCENE
                else "normal"
            ),
        )

    def _update_progress(self) -> None:
        if not self.progress_label:
            return
        self.progress_label.configure(
            text=(
                f"本段 {self.trace.current_dialogue_count} 句  ·  "
                f"已选路线 {self.trace.choice_count} 次  ·  "
                f"已看演出 {self.trace.scene_count} 个"
            )
        )

    def _reader_status(self) -> str:
        gate = self.trace.gate
        if gate and gate.kind == "choice":
            return "本段已结束于路线选择 · 选择后只显示下一个节点区间"
        if gate and gate.kind == "scene":
            label = str((gate.detail or {}).get("label") or "Scene")
            return f"到 {label} 的会话已展开 · 读完后点击「继续下一段演出」"
        if self.trace.error:
            return self.trace.error
        return self.trace.ending or "当前路线已展开完成"

    def _scroll_wheel(self, event: tk.Event) -> str:
        if not self.text:
            return "break"
        delta = int(getattr(event, "delta", 0))
        if not delta:
            return "break"
        direction = -1 if delta > 0 else 1
        notches = max(1, abs(delta) // 120)
        self.text.yview_scroll(
            direction * notches * self.WHEEL_SCROLL_LINES,
            "units",
        )
        return "break"

    def _scroll_page(self, direction: int) -> None:
        if self.text:
            self.text.yview_scroll(-1 if direction < 0 else 1, "pages")

    def _scroll_to_start(self) -> None:
        if self.text:
            self.text.yview_moveto(0.0)

    def _set_status(self, text: str, color: str | None = None) -> None:
        if self.status_label:
            self.status_label.configure(text=text, fg=color or self.MUTED)

    def _poll(self) -> None:
        if not self.root:
            return
        try:
            while True:
                kind, payload = self._events.get_nowait()
                if kind == "scene_ok":
                    self._scene_succeeded(*payload)
                elif kind == "scene_error":
                    self._scene_failed(*payload)
                elif kind == "translation_done":
                    self._translation_finished(*payload)
                elif kind == 'luna_text':
                    self._render_live_subtitle(*payload)
                elif kind == 'managed_hook':
                    self._managed_hook_event(*payload)
                elif kind == 'luna_status':
                    self._set_status({'connected':'Luna 文本桥已连接，等待台词。',
                        'error':'Luna 连接失败，请检查本地服务。',
                        'disconnected':'Luna 文本桥已断开。'}.get(payload,payload), self.MUTED)
                    if payload != 'connected': self._clear_live_subtitle()
        except queue.Empty:
            pass

        self._poll_playback_completion()
        self._tick_managed_hook()
        if getattr(self,'subtitle_status',None):
            phase = '播放中 · ' if self._active_scene_gate else '阅读中 · '
            self.subtitle_status.configure(text=phase+'Hook：'+getattr(self,'_hook_status','未启用'))
        if not self._active_scene_gate: self._clear_live_subtitle()

        if self.root and time.monotonic() - self._runtime_written_at >= 1.0:
            self._write_runtime_state()

        if self.root:
            self.root.after(60, self._poll)

    def _clear_live_subtitle(self):
        self._live_translation = None
        if getattr(self, 'subtitle_label', None):
            self.subtitle_label.configure(text=('等待当前演出的 Hook 台词……' if self._active_scene_gate
                else '等待演出 · 当前台词将在这里显示；上方小说正文保持独立。'))

    def _render_live_subtitle(self, scene_id, original):
        gate = self._active_scene_gate
        if not gate or gate.id != scene_id:
            self._clear_live_subtitle()
            return
        self._live_matcher.target = self._translation_config.get('target', '简体中文')
        translated = self._live_matcher.match(original, scene_id=scene_id)
        self._last_hook_original = original
        self._live_translation = translated
        from hgalgame.subtitle_matcher import MatchResult
        result = getattr(self._live_matcher, 'last_result', None)
        reason = result.reason if isinstance(result, MatchResult) else '未找到唯一匹配译文'
        display = translated
        if isinstance(result, MatchResult) and result.lines:
            display = '\n'.join((name+'：' if name else '')+text for _,name,text in result.lines)
        self.subtitle_label.configure(text=display if translated else
            (reason + ' · 原文\n' + original), font=('Microsoft YaHei UI', self._font_size))
        history = getattr(self, 'subtitle_history', None)
        if history is not None:
            if getattr(self, '_subtitle_history_scene', None) != scene_id:
                self._subtitle_history_scene = scene_id
                self._subtitle_history_seen = set()
                history.configure(state='normal'); history.delete('1.0','end'); history.configure(state='disabled')
            entries = result.lines if isinstance(result, MatchResult) and result.lines else ((None,'',display or original),)
            follow = history.yview()[1] >= .99
            history.configure(state='normal', font=('Microsoft YaHei UI',self._font_size))
            for position,name,text in entries:
                key = (position,name,text)
                if key in self._subtitle_history_seen: continue
                self._subtitle_history_seen.add(key)
                history.insert('end', (name+'：' if name else '')+text+'\n')
            history.configure(state='disabled')
            if follow: history.see('end')

    def _subtitle_more(self):
        menu = tk.Menu(self.root, tearoff=False)
        active = self._active_scene_gate is not None and not self._playback_busy
        menu.add_command(label='确认演出已结束' if self._scene_return_armed else '未检测到结束？手动确认',
                         command=self._request_scene_return, state='normal' if active else 'disabled')
        menu.tk_popup(self.root.winfo_pointerx(),self.root.winfo_pointery())

    def _show_subtitle_match_details(self):
        from tkinter import messagebox
        from hgalgame.subtitle_matcher import MatchResult
        result = getattr(getattr(self, '_live_matcher', None), 'last_result', None)
        if not isinstance(result, MatchResult):
            messagebox.showinfo('匹配详情', '尚未收到可匹配的台词。', parent=self.root)
            return
        messagebox.showinfo('匹配详情',
            f'{result.reason}\n相似度：{result.score:.1%}；不同译文候选：{result.runner_up:.1%}'
            f'\n区间行号：{result.position or "—"}\n\nHook 原文：\n{getattr(self, "_last_hook_original", "")}'
            f'\n\n候选原文：\n{result.source}\n\n译文：\n{result.translation or "未采用"}', parent=self.root)

    def _guard_window(self) -> None:
        """Recover from fullscreen resolution changes without taking game focus."""

        root = self.root
        if not root:
            return
        self._window_guard_count += 1
        try:
            if not self._drag_origin and not self._resize_origin:
                self._recover_visible_geometry()
            if self.hwnd:
                required = (
                    self.windows.WS_EX_TOPMOST
                    | self.windows.WS_EX_TOOLWINDOW
                    | self.windows.WS_EX_NOACTIVATE
                )
                if (self.windows.window_exstyle(self.hwnd) & required) != required:
                    self._reapply_native_style()
                self.windows.show_no_activate(self.hwnd)
        except (AttributeError, OSError, ValueError, tk.TclError):
            # A display mode transition can temporarily invalidate monitor data.
            # The next guard tick retries without disturbing the game window.
            pass
        if self.root:
            self.root.after(self.WINDOW_GUARD_INTERVAL_MS, self._guard_window)

    def _recover_visible_geometry(self) -> None:
        if not self.root:
            return
        width = self.root.winfo_width()
        height = self.root.winfo_height()
        if width <= 1 or height <= 1:
            return
        current = (
            self.root.winfo_x(),
            self.root.winfo_y(),
            width,
            height,
        )
        bounds = self._active_monitor_bounds()
        if not bounds:
            return
        fitted = self._fit_geometry_to_bounds(
            *current,
            bounds,
            margin=self.WINDOW_EDGE_MARGIN,
        )
        if fitted == current:
            return
        x, y, width, height = fitted
        self.root.geometry(f"{width}x{height}{x:+d}{y:+d}")
        self._window_recovered_count += 1
        if self._visibility_collapsed:
            pass
        elif self._mode == self.MODE_READER:
            self._expanded_geometry = fitted
        elif self._expanded_geometry:
            _old_x, _old_y, expanded_width, expanded_height = self._expanded_geometry
            self._expanded_geometry = (x, y, expanded_width, expanded_height)
        self._schedule_save()

    def _start_drag(self, event: tk.Event) -> None:
        if not self.root:
            return
        self._drag_origin = (
            int(event.x_root),
            int(event.y_root),
            self.root.winfo_x(),
            self.root.winfo_y(),
        )

    def _drag(self, event: tk.Event) -> None:
        if not self.root or not self._drag_origin:
            return
        start_x, start_y, window_x, window_y = self._drag_origin
        x = window_x + int(event.x_root) - start_x
        y = window_y + int(event.y_root) - start_y
        self.root.geometry(f"{x:+d}{y:+d}")

    def _end_drag(self, _event: tk.Event) -> None:
        if not self._drag_origin:
            return
        self._drag_origin = None
        self._recover_visible_geometry()
        self._schedule_save()

    def _start_resize(self, event: tk.Event, edge: str) -> str | None:
        if not self.root or self._mode != self.MODE_READER:
            return None
        self._resize_origin = (
            edge,
            int(event.x_root),
            int(event.y_root),
            self.root.winfo_x(),
            self.root.winfo_y(),
            self.root.winfo_width(),
            self.root.winfo_height(),
        )
        return "break"

    def _resize(self, event: tk.Event) -> str | None:
        if not self.root or not self._resize_origin or self._mode != self.MODE_READER:
            return None
        edge, start_x, start_y, x, y, width, height = self._resize_origin
        max_width, max_height = self._resize_limits()
        new_x, new_y, new_width, new_height = self._resized_geometry(
            edge,
            x,
            y,
            width,
            height,
            int(event.x_root) - start_x,
            int(event.y_root) - start_y,
            min_width=self.MIN_WIDTH,
            min_height=self.MIN_HEIGHT,
            max_width=max_width,
            max_height=max_height,
        )
        self.root.geometry(
            f"{new_width}x{new_height}{new_x:+d}{new_y:+d}"
        )
        return "break"

    def _end_resize(self, _event: tk.Event) -> str:
        self._resize_origin = None
        self._remember_expanded_geometry()
        self._schedule_save()
        return "break"

    @staticmethod
    def _resized_geometry(
        edge: str,
        x: int,
        y: int,
        width: int,
        height: int,
        delta_x: int,
        delta_y: int,
        *,
        min_width: int,
        min_height: int,
        max_width: int,
        max_height: int,
    ) -> tuple[int, int, int, int]:
        new_x, new_y = x, y
        new_width, new_height = width, height
        if "e" in edge:
            new_width = max(min_width, min(max_width, width + delta_x))
        elif "w" in edge:
            new_width = max(min_width, min(max_width, width - delta_x))
            new_x = x + width - new_width
        if "s" in edge:
            new_height = max(min_height, min(max_height, height + delta_y))
        elif "n" in edge:
            new_height = max(min_height, min(max_height, height - delta_y))
            new_y = y + height - new_height
        return new_x, new_y, new_width, new_height

    def _resize_limits(self) -> tuple[int, int]:
        if not self.root:
            return self.MAX_WIDTH, self.MAX_HEIGHT
        screen_width = max(self.MIN_WIDTH, self.root.winfo_screenwidth())
        screen_height = max(self.MIN_HEIGHT, self.root.winfo_screenheight() - 40)
        return min(self.MAX_WIDTH, screen_width), min(self.MAX_HEIGHT, screen_height)

    def _geometry_changed(self, _event: tk.Event) -> None:
        if self._visibility_collapsed:
            self._schedule_save()
            return
        if self._mode == self.MODE_READER:
            self._remember_expanded_geometry()
        elif self.root and self._expanded_geometry:
            _x, _y, width, height = self._expanded_geometry
            self._expanded_geometry = (
                self.root.winfo_x(),
                self.root.winfo_y(),
                width,
                height,
            )
        self._update_wrapped_controls()
        self._schedule_save()

    def _remember_expanded_geometry(self) -> None:
        if not self.root or self._visibility_collapsed:
            return
        width = self.root.winfo_width()
        height = self.root.winfo_height()
        if width < self.MIN_WIDTH or height < self.MIN_HEIGHT:
            return
        self._expanded_geometry = (
            self.root.winfo_x(),
            self.root.winfo_y(),
            width,
            height,
        )

    def _restore_game_focus(self) -> None:
        pid = self.game_pid()
        hwnd = self.windows.find_main_window(pid) if pid else None
        if hwnd:
            try:
                self.windows.foreground(hwnd)
            except ValueError:
                pass

    def _reapply_native_style(self) -> None:
        if self.hwnd:
            self.windows.make_noactivate_tool(
                self.hwnd,
                click_through=self._click_through,
            )

    def _load_actions(self) -> list[dict[str, Any]]:
        try:
            payload = json.loads(self.session_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return []
        if not isinstance(payload, dict):
            return []
        if (
            payload.get("version") != self.SESSION_VERSION
            or str(payload.get("fingerprint", "")) != self.fingerprint
            or not isinstance(payload.get("actions"), list)
        ):
            return []
        return payload["actions"]

    def _save_session(self) -> None:
        write_json(
            self.session_path,
            {
                "version": self.SESSION_VERSION,
                "fingerprint": self.fingerprint,
                "actions": self.reader.session()["actions"],
            },
        )

    def _load_settings(self) -> dict[str, Any]:
        try:
            payload = json.loads(self.settings_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return {}
        if not isinstance(payload, dict):
            return {}
        version = payload.get("version")
        if version not in {1, self.SETTINGS_VERSION}:
            return {}
        if version == 1:
            # Version 1 shipped with a cramped 620 x 420 default.  Upgrade that
            # untouched default to the new reading layout while retaining a
            # size that the user had already customized.
            if payload.get("width") == 620 and payload.get("height") == 420:
                for key in ("x", "y", "width", "height"):
                    payload.pop(key, None)
            payload["version"] = self.SETTINGS_VERSION
            payload.setdefault("font_size", self.DEFAULT_FONT_SIZE)
        opacity = payload.get("opacity")
        if (
            not isinstance(opacity, (int, float))
            or isinstance(opacity, bool)
            or not math.isfinite(float(opacity))
        ):
            payload["opacity"] = 0.94
        else:
            payload["opacity"] = max(0.55, min(1.0, float(opacity)))
        payload["font_size"] = self._integer(
            payload.get("font_size"),
            self.DEFAULT_FONT_SIZE,
            self.MIN_FONT_SIZE,
            self.MAX_FONT_SIZE,
        )
        return payload

    def _schedule_save(self) -> None:
        if not self.root:
            return
        if self._save_after:
            self.root.after_cancel(self._save_after)
        self._save_after = self.root.after(250, self._save_now)

    def _save_now(self) -> None:
        if self.root and self._mode == self.MODE_READER:
            self._remember_expanded_geometry()
        x, y, width, height = self._expanded_geometry or (
            0,
            0,
            self.DEFAULT_WIDTH,
            self.DEFAULT_HEIGHT,
        )
        write_json(
            self.settings_path,
            {
                "version": self.SETTINGS_VERSION,
                "x": x,
                "y": y,
                "width": width,
                "height": height,
                "opacity": self._opacity,
                "font_size": self._font_size,
                "click_through": False,
            },
        )
        self._save_after = None

    def _write_runtime_state(self) -> None:
        self._runtime_written_at = time.monotonic()
        gate = self.trace.gate
        owner = self.windows.window_owner(self.hwnd) if self.hwnd else None
        exstyle = self.windows.window_exstyle(self.hwnd) if self.hwnd else 0
        monitor = self._active_monitor_bounds()
        geometry = None
        if self.root:
            geometry = {
                "x": self.root.winfo_x(),
                "y": self.root.winfo_y(),
                "width": self.root.winfo_width(),
                "height": self.root.winfo_height(),
            }
        playback = None
        if self.playback_status:
            try:
                playback = self.playback_status()
            except Exception as exc:
                playback = {"error": str(exc)}
        write_json(
            self.runtime_path,
            {
                "version": 1,
                "frontend": "native_tk_overlay",
                "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "mode": self._mode,
                "visibility_collapsed": self._visibility_collapsed,
                "history_open": self._history_open,
                "window_running": bool(self.root and self.hwnd),
                "click_through": self._click_through,
                "busy": self._playback_busy,
                "scene_return_armed": self._scene_return_armed,
                "sync_reset_armed": self._sync_reset_armed,
                "pending_auto_scene": self._pending_auto_scene_id,
                "scene_active": (
                    {
                        "id": self._active_scene_gate.id,
                        "dispatched": self._scene_dispatched,
                    }
                    if self._active_scene_gate
                    else None
                ),
                "gate": ({"kind": gate.kind, "id": gate.id} if gate else None),
                "actions": len(self.reader.actions),
                "hwnd": self.hwnd,
                "owner_hwnd": owner,
                "independent_window": owner is None,
                "exstyle": f"0x{exstyle:08X}",
                "no_activate": bool(exstyle & self.windows.WS_EX_NOACTIVATE),
                "window_guard_checks": self._window_guard_count,
                "window_recoveries": self._window_recovered_count,
                "window_geometry": geometry,
                "monitor_bounds": (
                    {
                        "left": monitor.left,
                        "top": monitor.top,
                        "right": monitor.right,
                        "bottom": monitor.bottom,
                    }
                    if monitor
                    else None
                ),
                "last_error": self._last_error,
                "playback": playback,
            },
        )

    def _integer(self, value: Any, default: int, minimum: int, maximum: int) -> int:
        if isinstance(value, bool):
            return default
        try:
            number = int(value)
        except (TypeError, ValueError):
            return default
        return max(minimum, min(maximum, number))
