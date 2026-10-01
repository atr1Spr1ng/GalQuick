from __future__ import annotations

import json
import mimetypes
import sys
import threading
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from hgalgame.guides import StrategyGuideStore
from hgalgame.output import write_json
from hgalgame.story import EngineAdapterRegistry, StoryFlowBuilder
from hgalgame.story.adapter import StoryPlaybackSession


class _StoryRuntimeContext:
    """Coordinate generic UI actions, an optional engine session, and overlay."""

    def __init__(
        self,
        adapter,
        document: dict[str, Any],
        game_dir: Path,
        output_dir: Path,
        playback: StoryPlaybackSession | None,
    ) -> None:
        self.adapter = adapter
        self.game_dir = game_dir
        self.output_dir = output_dir
        self.playback = playback
        self.overlay = None
        self.known_scenes = {scene["id"]: scene for scene in document["scenes"]}
        self.supported = set(document["capabilities"]["actions"])
        self.action_lock = threading.Lock()

    def perform(self, action: str, parameters: dict[str, Any]) -> dict[str, Any]:
        if action not in self.supported:
            raise ValueError(f"unsupported action: {action}")
        if action != "scene_replay":
            raise ValueError(f"no generic action gateway for: {action}")
        scene_id = str(parameters.get("scene_id", ""))
        scene = self.known_scenes.get(scene_id)
        if scene is None:
            raise ValueError("unknown scene_id")
        if not self.action_lock.acquire(blocking=False):
            raise ValueError("another playback action is already running")
        try:
            if self.overlay:
                self.overlay.set_mode("compact")
            action_parameters = {"replay_key": scene["replay_key"]}
            if self.playback:
                return self.playback.perform(action, action_parameters)
            return self.adapter.perform_story_action(
                action,
                action_parameters,
                self.game_dir,
                self.output_dir,
            )
        except Exception:
            if self.overlay:
                self.overlay.set_mode("full", activate=True)
            raise
        finally:
            self.action_lock.release()

    def status(self) -> dict[str, Any]:
        return {
            "mode": "overlay" if self.overlay else "browser",
            "playback": self.playback.status() if self.playback else None,
            "overlay": self.overlay.status() if self.overlay else None,
        }

    def overlay_command(self, parameters: dict[str, Any]) -> dict[str, Any]:
        if not self.overlay:
            raise ValueError("the UI is not running in overlay mode")
        command = str(parameters.get("command", "set"))
        if command == "toggle":
            result = self.overlay.toggle()
        elif command == "set":
            mode = str(parameters.get("mode", ""))
            result = self.overlay.set_mode(mode, activate=mode == "full")
        else:
            raise ValueError("overlay command must be set or toggle")
        if result.get("mode") == "full" and self.playback:
            self.playback.reader_resumed()
        return result


class StoryBrowser:
    """Generic route-novel reader. It knows no engine formats or opcodes."""

    def __init__(self, registry: EngineAdapterRegistry) -> None:
        self.registry = registry
        self.assets_dir = Path(__file__).resolve().parent / "assets"

    def build(self, game_dir: Path, output_dir: Path) -> dict[str, Any]:
        adapter = self.registry.select(game_dir)
        document = self._build_document(adapter, game_dir, output_dir)
        if callable(getattr(adapter, "create_story_session", None)):
            self.write_launcher(game_dir, output_dir)
        return document

    def serve(
        self,
        game_dir: Path,
        output_dir: Path,
        host: str = "127.0.0.1",
        port: int = 0,
        open_browser: bool = True,
        overlay: bool = False,
    ) -> None:
        game_dir = game_dir.resolve()
        output_dir = output_dir.resolve()
        adapter = self.registry.select(game_dir)
        document = self._build_document(adapter, game_dir, output_dir)
        if callable(getattr(adapter, "create_story_session", None)):
            self.write_launcher(game_dir, output_dir)
        playback = None
        if overlay:
            create_session = getattr(adapter, "create_story_session", None)
            if not callable(create_session):
                raise ValueError(
                    f"engine adapter {adapter.name} does not support persistent overlay sessions"
                )
            playback = create_session(game_dir, output_dir)
        runtime = _StoryRuntimeContext(
            adapter,
            document,
            game_dir,
            output_dir,
            playback,
        )

        handler = self._handler(document, runtime)
        server = ThreadingHTTPServer((host, port), handler)
        address, actual_port = server.server_address[:2]
        url = f"http://{address}:{actual_port}/"
        print(f"Story Browser: {url}", flush=True)
        server_thread = threading.Thread(
            target=server.serve_forever,
            name="hgal-story-server",
            daemon=True,
        )
        server_thread.start()
        try:
            if playback:
                print("Starting one persistent original-game session...", flush=True)
                playback.start()
                from hgalgame.frontend.windows_overlay import WindowsOverlayHost

                runtime.overlay = WindowsOverlayHost(
                    output_dir,
                    game_pid=lambda: playback.pid,
                    on_closed=server.shutdown,
                )
                runtime.overlay.start(url)
                print("Overlay attached. Use the on-screen controls to switch modes.", flush=True)
            elif open_browser:
                webbrowser.open(url)
            while server_thread.is_alive():
                server_thread.join(timeout=0.5)
        except KeyboardInterrupt:
            server.shutdown()
        finally:
            if runtime.overlay:
                runtime.overlay.stop()
            if playback:
                playback.stop()
            if server_thread.is_alive():
                server.shutdown()
            server_thread.join(timeout=3)
            server.server_close()

    def run_native_overlay(self, game_dir: Path, output_dir: Path) -> None:
        """Run the route reader in a small independent no-focus native window."""

        game_dir = game_dir.resolve()
        output_dir = output_dir.resolve()
        adapter = self.registry.select(game_dir)
        document = self._build_document(adapter, game_dir, output_dir)
        self.write_launcher(game_dir, output_dir)

        create_session = getattr(adapter, "create_story_session", None)
        if not callable(create_session):
            raise ValueError(
                f"engine adapter {adapter.name} does not support persistent overlay sessions"
            )
        playback = create_session(game_dir, output_dir)
        runtime = _StoryRuntimeContext(
            adapter,
            document,
            game_dir,
            output_dir,
            playback,
        )
        overlay = None
        try:
            print("Starting one persistent original-game session...", flush=True)
            playback.start()
            from hgalgame.frontend.native_overlay import NativeStoryOverlay

            overlay = NativeStoryOverlay(
                document,
                output_dir,
                perform_action=runtime.perform,
                playback_status=playback.status,
                on_reader_resumed=playback.reader_resumed,
                game_pid=lambda: playback.pid,
            )
            print(
                "Native route reader ready. Reading and Scene modes use mouse controls only.",
                flush=True,
            )
            overlay.run()
        except KeyboardInterrupt:
            pass
        finally:
            stopped = playback.stop()
            if overlay:
                overlay.mark_playback_stopped(stopped)

    def _build_document(
        self,
        adapter,
        game_dir: Path,
        output_dir: Path,
    ) -> dict[str, Any]:
        document = StoryFlowBuilder().build(adapter.build_story_source(game_dir))
        output_dir.mkdir(parents=True, exist_ok=True)
        StrategyGuideStore().attach(document, output_dir)
        write_json(output_dir / "story_flow.json", document)
        return document

    def write_launcher(self, game_dir: Path, output_dir: Path) -> Path:
        """Write the one-click overlay entry beside the generated game output."""

        game_dir = game_dir.resolve()
        output_dir = output_dir.resolve()
        launcher = output_dir.parent / "启动剧情流程.cmd"
        python = str(Path(sys.executable).resolve())
        source_root = str(Path(__file__).resolve().parents[2])
        lines = [
            "@echo off",
            "setlocal",
            "chcp 65001 >nul",
            'set "PYTHONUTF8=1"',
            f'set "PYTHONPATH={source_root};%PYTHONPATH%"',
            'set "HGAL_LOG=%~dp0startup.log"',
            'if exist "%HGAL_LOG%" copy /y "%HGAL_LOG%" "%HGAL_LOG%.previous" >nul',
            "echo 正在准备剧情加速器，请稍候...",
            'echo 启动日志："%HGAL_LOG%"',
            (
                f'"{python}" -u -X faulthandler -m hgalgame.cli story-ui "{game_dir}" '
                f'"{output_dir}" --overlay >"%HGAL_LOG%" 2>&1'
            ),
            'set "HGAL_EXIT=%ERRORLEVEL%"',
            'if not "%HGAL_EXIT%"=="0" (',
            "  echo.",
            '  type "%HGAL_LOG%"',
            "  echo [ERROR] 剧情加速器启动失败，错误码 %HGAL_EXIT%。",
            "  pause",
            ")",
            "endlocal & exit /b %HGAL_EXIT%",
        ]
        launcher.parent.mkdir(parents=True, exist_ok=True)
        temporary = launcher.with_name(f".{launcher.name}.tmp")
        # cmd.exe must see @echo off as the first bytes, not a UTF-8 BOM.
        # Disable Windows text newline translation: CRLF is already explicit.
        temporary.write_text("\r\n".join(lines) + "\r\n", encoding="utf-8", newline="")
        temporary.replace(launcher)
        return launcher

    def _action_gateway(
        self,
        adapter,
        document: dict[str, Any],
        game_dir: Path,
        output_dir: Path,
    ) -> Callable[[str, dict[str, Any]], dict[str, Any]]:
        """Compatibility seam for tests and non-session frontend integrations."""

        runtime = _StoryRuntimeContext(
            adapter,
            document,
            game_dir,
            output_dir,
            playback=None,
        )
        return runtime.perform

    def _handler(
        self,
        document: dict[str, Any],
        runtime: _StoryRuntimeContext,
    ) -> type[BaseHTTPRequestHandler]:
        assets_dir = self.assets_dir
        story_payload = json.dumps(document, ensure_ascii=False).encode("utf-8")

        class Handler(BaseHTTPRequestHandler):
            server_version = "HGalStory/2.0"

            def do_GET(self) -> None:  # noqa: N802
                path = urlparse(self.path).path
                if path == "/api/story":
                    self._send(story_payload, "application/json; charset=utf-8")
                    return
                if path == "/api/runtime":
                    self._send_json({"ok": True, "runtime": runtime.status()})
                    return
                if path == "/":
                    self._send_asset(assets_dir / "index.html")
                    return
                if path.startswith("/assets/"):
                    name = Path(path.removeprefix("/assets/")).name
                    self._send_asset(assets_dir / name)
                    return
                self.send_error(HTTPStatus.NOT_FOUND)

            def do_POST(self) -> None:  # noqa: N802
                path = urlparse(self.path).path
                if path not in {"/api/actions", "/api/overlay"}:
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if length <= 0 or length > 65536:
                        raise ValueError("invalid request body length")
                    body = json.loads(self.rfile.read(length).decode("utf-8"))
                    if path == "/api/actions":
                        result = runtime.perform(str(body.get("action", "")), body)
                    else:
                        result = runtime.overlay_command(body)
                    payload = {"ok": True, "result": result}
                    status = HTTPStatus.OK
                except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
                    payload = {"ok": False, "error": str(exc)}
                    status = HTTPStatus.BAD_REQUEST
                except Exception as exc:  # keep local server alive while surfacing the failure
                    payload = {"ok": False, "error": f"action failed: {exc}"}
                    status = HTTPStatus.INTERNAL_SERVER_ERROR
                self._send(
                    json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                    "application/json; charset=utf-8",
                    status,
                )

            def _send_json(
                self,
                value: dict[str, Any],
                status: HTTPStatus = HTTPStatus.OK,
            ) -> None:
                self._send(
                    json.dumps(value, ensure_ascii=False).encode("utf-8"),
                    "application/json; charset=utf-8",
                    status,
                )

            def _send_asset(self, path: Path) -> None:
                if not path.is_file() or path.parent != assets_dir:
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return
                content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
                if content_type.startswith("text/") or content_type in {
                    "application/javascript",
                    "application/json",
                }:
                    content_type += "; charset=utf-8"
                self._send(path.read_bytes(), content_type)

            def _send(
                self,
                payload: bytes,
                content_type: str,
                status: HTTPStatus = HTTPStatus.OK,
            ) -> None:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("X-Frame-Options", "DENY")
                self.send_header(
                    "Content-Security-Policy",
                    "default-src 'self'; script-src 'self'; style-src 'self'; "
                    "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'",
                )
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, format: str, *args: object) -> None:
                print(f"[story-ui] {self.address_string()} {format % args}")

        return Handler
